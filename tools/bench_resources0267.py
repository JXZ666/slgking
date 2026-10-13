"""Offline real-Tk resource soak; run each source in a fresh process, serially.

Example: python tools/bench_resources0267.py --source-root D:/path --seconds 3600
Uses only a disposable profile/SQLite database and synthetic local covers.
"""
import argparse
from collections import deque
from concurrent.futures import Future
import contextlib
import gc
import http.client
import json
import os
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import time
import urllib.request
import weakref
from unittest.mock import patch


def percentile(values, fraction=.95):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * fraction))] if values else None


def process_memory():
    try:
        import psutil
        memory = psutil.Process().memory_info()
        return memory.rss, getattr(memory, 'private', None)
    except ImportError:
        if os.name != 'nt':
            return None, None
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        get_process = ctypes.windll.kernel32.GetCurrentProcess
        get_process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        if not get_memory(get_process(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.WorkingSetSize, counters.PrivateUsage


def benchmark(args):
    source = args.source_root.resolve()
    if not (source / 'slg_gui.py').is_file():
        raise ValueError('source-root must contain slg_gui.py')
    sys.path.insert(0, str(source))
    with tempfile.TemporaryDirectory(prefix='slg-resources-', ignore_cleanup_errors=True) as data, contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {key: data for key in
            ('LOCALAPPDATA', 'APPDATA', 'HOME', 'USERPROFILE')}))
        def offline(*a, **kw):
            raise RuntimeError('network forbidden in resource soak')
        # Block transports before importing or constructing the app.
        for owner, name in ((socket.socket, 'connect'), (socket, 'create_connection'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open'),
                            (http.client.HTTPConnection, 'request')):
            stack.enter_context(patch.object(owner, name, side_effect=offline))
        import slg_db
        import slg_gui
        import slg_account
        import slg_remote
        import customtkinter as ctk
        from PIL import Image
        Image.init()
        session = {'account_id': 'offline-benchmark', 'token': 'stub', 'nickname': 'Bench'}
        account = {'account_id': session['account_id'], 'nickname': 'Bench', 'points': 100,
                   'balance': 100, 'owned_titles': [], 'owned_cosmetics': [], 'signin': {}}
        def fake_request(path, *a, **kw):
            if 'quest' in path: return {'tasks': [], 'date': '2026-10-10'}
            return dict(account)
        for owner, name, kw in ((slg_account, 'session', {'return_value': session}),
                               (slg_account, 'request', {'side_effect': fake_request}),
                               (slg_remote, 'report', {'return_value': None}),
                               (slg_remote, 'fetch_config', {'return_value': None}),
                               (slg_gui.App, '_require_personal_access', {'return_value': True})):
            if hasattr(owner, name): stack.enter_context(patch.object(owner, name, **kw))
        conn = slg_db.connect()
        covers = Path(slg_db.covers_dir())
        for i in range(args.count):
            name = 'cover-%04d.png' % i
            Image.new('RGB', (360, 540), (i * 31 % 256, i * 17 % 256, 120)).save(covers / name)
            gid, _ = slg_db.upsert_game(conn, 'soak-%04d' % i,
                'https://example.invalid/%d' % i, 'Synthetic game %04d' % i,
                tags=['benchmark'], complete=0)
            conn.execute('UPDATE games SET cover_file=? WHERE id=?', (name, gid))
        conn.commit()
        conn.close()
        app = slg_gui.App(notify=False)
        app.geometry('1180x760')
        errors, samples = [], []
        import traceback
        app.report_callback_exception = lambda *a: errors.append(''.join(traceback.format_exception(*a)))
        heartbeat = deque(maxlen=20000)
        retained = deque(maxlen=2000)
        last_beat = time.perf_counter()
        started = last_beat
        action_index = 0
        actions = {'page': 0, 'detail': 0, 'theme': 0, 'shop': 0}
        stopped = False

        def beat():
            nonlocal last_beat
            now = time.perf_counter()
            heartbeat.append((now - last_beat) * 1000)
            last_beat = now
            if not stopped: app.after(30, beat)

        def widgets():
            result, pending = [], [app]
            while pending:
                widget = pending.pop()
                result.append(widget)
                pending.extend(widget.winfo_children())
            return result

        def snapshot(phase='active'):
            rss, private = process_memory()
            cache = slg_gui._image_cache
            objects = gc.get_objects()
            images = [obj for obj in objects if isinstance(obj, ctk.CTkImage)]
            labels = [obj for obj in objects if isinstance(obj, ctk.CTkLabel)]
            def destroyed(widget):
                try:
                    return not widget.winfo_exists()
                except Exception:
                    return True
            retired_objects = {id(widget): widget for ref in retained
                               if (widget := ref()) is not None}
            commands = getattr(app, '_tclCommands', None) or ()
            pending_decode = getattr(app, '_cover_consumers', None)
            pending = getattr(pending_decode, 'pending_count', None)
            waiters = getattr(app, '_cover_waiters', {})
            if pending is None:
                pending = getattr(waiters, 'pending_count', None)
            if pending is None:
                try: pending = len(waiters)
                except TypeError: pending = None
            executor_tasks = {}
            for name, executor in vars(app).items():
                if name.endswith('_executor') and hasattr(executor, '_work_queue'):
                    executor_tasks[name] = executor._work_queue.qsize()
            row = {'seconds': round(time.perf_counter() - started, 3), 'phase': phase,
                   'rss': rss, 'private_bytes': private, 'tk_images': len(app.tk.splitlist(app.tk.call('image', 'names'))),
                   'widgets': len(widgets()), 'after': len(app.tk.splitlist(app.tk.call('after', 'info'))),
                   'image_objects': len(images), 'image_callbacks': sum(len(im._configure_callback_list) for im in images),
                   'cache': len(cache), 'cache_estimated_bytes': getattr(cache, 'estimated_bytes', None),
                   'queue': app.queue.qsize(), 'decode_pending': pending, 'executor_queued': executor_tasks,
                   'unfinished_futures': sum(isinstance(obj, Future) and not obj.done() for obj in objects),
                   'retired_labels_alive': sum(ref() is not None for ref in retained),
                   'retired_unique_labels_alive': len(retired_objects),
                   'retired_destroyed_labels_alive': sum(destroyed(w) for w in retired_objects.values()),
                   'python_labels': len(labels),
                   'destroyed_python_labels': sum(destroyed(w) for w in labels),
                   'root_tcl_commands': len(commands),
                   'global_scroll_callbacks': sum('_mouse_wheel_all' in name or '_keyboard_shift_' in name
                                                  for name in commands),
                   'heartbeat_p95_ms': percentile(heartbeat), 'actions': dict(actions)}
            samples.append(row)
            if args.jsonl:
                with args.jsonl.open('a', encoding='utf-8') as output:
                    output.write(json.dumps(row) + '\n')
            return row

        def sample():
            snapshot('draining' if stopped else 'active')
            if not stopped: app.after(int(args.sample_seconds * 1000), sample)

        def action():
            nonlocal action_index
            if stopped: return
            kind = ('page', 'detail', 'shop', 'detail', 'theme')[action_index % 5]
            try:
                if kind == 'page':
                    count = max(1, app._page_count())
                    app._goto_page((action_index // 5) % count + 1)
                elif kind == 'detail':
                    if app.rows: app.select(app.rows[(action_index // 5) % len(app.rows)])
                elif kind == 'shop':
                    app.open_shop()
                else:
                    retained.extend(weakref.ref(widget) for widget in widgets()
                                    if isinstance(widget, ctk.CTkLabel))
                    app._apply_theme('dark' if (action_index // 5) % 2 else 'light')
                actions[kind] += 1
            except Exception as exc:
                errors.append('%s: %s' % (kind, exc))
            action_index += 1
            app.after(int(args.action_seconds * 1000), action)

        def stop():
            nonlocal stopped
            stopped = True
            snapshot('stop_actions')
            app.after(int(args.drain_seconds * 1000), app.quit)

        try:
            app.after(30, beat)
            app.after(100, sample)
            app.after(300, action)
            app.after(int(args.seconds * 1000), stop)
            app.mainloop()
            gc.collect()
            final = snapshot('drained')
            late = samples[len(samples) * 2 // 3:]
            platform = {}
            for field in ('private_bytes', 'rss', 'tk_images', 'widgets', 'after',
                          'image_callbacks', 'cache', 'decode_pending',
                          'destroyed_python_labels', 'root_tcl_commands', 'global_scroll_callbacks'):
                values = [row[field] for row in late if row[field] is not None]
                platform[field] = {'min': min(values) if values else None,
                                   'max': max(values) if values else None,
                                   'end_minus_start': values[-1] - values[0] if values else None}
            return {'status': 'ok' if not errors else 'errors', 'source_root': str(source),
                    'source_version': getattr(slg_gui, 'APP_VERSION', None),
                    'python': sys.version, 'pid': os.getpid(),
                    'seconds_requested': args.seconds, 'network': 'blocked; account API stubbed',
                    'data': 'temporary SQLite/profile and synthetic covers', 'actions': actions,
                    'samples': samples, 'late_third_ranges': platform, 'errors': errors,
                    'drained': final['queue'] == 0 and final['decode_pending'] == 0
                               and not any(final['executor_queued'].values())
                               and final['unfinished_futures'] == 0,
                    'note': 'Late ranges show observed retention, not a mathematical no-leak proof. RSS allocator plateaus may remain after objects are released.'}
        finally:
            app.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=3600)
    parser.add_argument('--count', type=int, default=480)
    parser.add_argument('--action-seconds', type=float, default=1.0)
    parser.add_argument('--sample-seconds', type=float, default=10.0)
    parser.add_argument('--drain-seconds', type=float, default=10.0)
    parser.add_argument('--jsonl', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if min(args.seconds, args.count, args.action_seconds, args.sample_seconds) <= 0 or args.drain_seconds < 0:
        parser.error('durations/count must be positive; drain-seconds may be zero')
    result = benchmark(args)
    encoded = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output: args.output.write_text(encoded, encoding='utf-8')
    print(encoded)
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__': sys.exit(main())
