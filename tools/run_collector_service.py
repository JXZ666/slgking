"""Fixed production entry points; source lock covers crawl and publication.

The child owns collector.lock. This parent owns run.lock through publication,
so deployment can exclude both manual child and timer/service activity.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import shutil
import sys
import time
import importlib.util

ROOT = Path('/opt/slgking')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slg_source_status import STATUS_PATHS, public_status
from slg_collector_policy import DEFAULTS, validate_limits


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(',', ':'))
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def next_run(source, now=None):
    current = datetime.fromtimestamp(time.time() if now is None else now,
                                     timezone(timedelta(hours=8)))
    target = current.replace(hour=1 if source == 'f95zone' else 2,
                             minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target.isoformat()


def paths(source, root=ROOT):
    prefix = 'f95' if source == 'f95zone' else 'dlsite'
    return root / (prefix + '-data'), root / (prefix + '-worker')


def command(source, requests=None, seconds=None, resume=False, root=ROOT):
    state, worker = paths(source, root)
    python = str(root / '.venv/bin/python')
    if source == 'f95zone':
        args = [python, '-u', str(worker / 'tools/run_f95_worker.py'),
                '--state-dir', str(state), '--catalogue-db', str(root / 'data/slgking/slgking.db'),
                '--catalogue-lock', str(root / 'data/slgking/catalogue-write.lock'),
                '--max-requests', str(requests if requests is not None else DEFAULTS[source][0]), '--max-seconds', str(seconds if seconds is not None else DEFAULTS[source][1]),
                '--status-file', str(state / 'status.json')]
        if resume:
            args.append('--resume')
    else:
        args = [python, '-u', str(worker / 'tools/run_dlsite_worker.py'),
                '--state', str(state), '--output', str(root / 'publish'),
                '--seed', str(worker / 'seed/catalogue.json'),
                '--max-requests', str(requests if requests is not None else DEFAULTS[source][0]), '--max-seconds', str(seconds if seconds is not None else DEFAULTS[source][1]),
                '--status-file', str(state / 'status.json')]
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
               PYTHONNOUSERSITE='1', LOCALAPPDATA=str(root / 'data' if source == 'f95zone' else state))
    # Reuse the verified private Pillow installation; never alter shared venv.
    env['PYTHONPATH'] = os.pathsep.join([str(root / 'dlsite-worker/vendor'), str(worker)])
    return args, env


def recovery_decision(root):
    """Read the same policy as the DLsite child without changing its ledger."""
    path = root / 'dlsite-worker/slg_dlsite_recovery.py'
    spec = importlib.util.spec_from_file_location('_dlsite_recovery_policy', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with closing(sqlite3.connect((root / 'dlsite-data/ledger.db').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        control = {key: json.loads(value) for key, value in db.execute('SELECT key,value FROM control')}
    return module.pause_decision(control, time.time())


def run(source, requests=None, seconds=None, resume=False, root=ROOT, execute=subprocess.run, recovery_only=False, publish_pending=False):
    requests, seconds = validate_limits(source, requests, seconds)
    if recovery_only:
        requests, seconds = min(requests, 10), min(seconds, 900)
    state, _ = paths(source, root)
    target = root / 'publish' / STATUS_PATHS[source].lstrip('/')
    # A conflicting invocation must not overwrite the active runner's report.
    with lock(state / 'run.lock'):
        if publish_pending:
            if source != 'dlsite' or recovery_only:
                raise ValueError('offline publication only applies to DLsite')
            started = time.time()
            args, env = command(source, 10, 900, False, root)
            args.append('--publish-only')
            result = execute(args, env=env, timeout=900, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode:
                return result.returncode
            with lock(state / 'collector.lock'):
                raw = json.loads((state / 'status.json').read_text(encoding='utf-8'))
                if raw.get('source_id') != source or float(raw.get('last_publish') or 0) < started:
                    raise ValueError('offline publication status is stale')
                safe = public_status(raw, source)
                safe['next_run'] = next_run(source)
                atomic_json(target, safe)
            print(json.dumps(dict(operation='offline_publication', upstream_requests=0, acquisition=safe)))
            return 0
        if recovery_only:
            if source != 'dlsite':
                raise ValueError('recovery only applies to DLsite')
            decision = recovery_decision(root)
            if not decision['probe_due']:
                print(json.dumps(dict(source_id=source, recovery='waiting', **decision)))
                return 0
        previous = {}
        try:
            previous = public_status(json.loads(target.read_text(encoding='utf-8')), source)
        except (OSError, ValueError, TypeError):
            pass
        status = dict(previous, source_id=source, state='running', reason_code='running',
                      started_at=time.time(), finished_at=None, next_run=next_run(source))
        atomic_json(target, public_status(status, source))
        # Stop acquisition while enough space remains to report/recover safely.
        if shutil.disk_usage(root / 'publish').free < 1024 * 1024 * 1024:
            status.update(state='paused', reason_code='storage_low', finished_at=time.time())
            atomic_json(target, public_status(status, source))
            print(json.dumps(public_status(status, source), ensure_ascii=False))
            return 2
        code = 1
        try:
            args, env = command(source, requests, seconds, resume, root)
            if recovery_only:
                args.append('--recovery-only')
            result = execute(args, env=env, timeout=seconds + 60,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, check=False)
            code = result.returncode
            if recovery_only and code in (0, 3):
                first = json.loads((state / 'status.json').read_text(encoding='utf-8'))
                if code == 0 and float(first.get('finished_at') or 0) < status['started_at']:
                    # A pause may change between our read and the child's lock.
                    # A zero-request waiting exit never establishes recovery.
                    atomic_json(target, previous)
                    print(json.dumps(dict(source_id=source, recovery='waiting')))
                    return 0
                if first.get('resume_ready') and float(first.get('finished_at') or 0) >= status['started_at']:
                    # Retain run.lock; the child enforces the remaining persistent day budget.
                    more, more_env = command(source, DEFAULTS[source][0], DEFAULTS[source][1], False, root)
                    result = execute(more, env=more_env, timeout=DEFAULTS[source][1] + 60,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, check=False)
                    code = result.returncode
            # Exclude a direct child invocation during status/publish handoff.
            with lock(state / 'collector.lock'):
                raw = json.loads((state / 'status.json').read_text(encoding='utf-8'))
                # A crashed child must never turn an old success into a fresh one.
                if raw.get('source_id') != source or float(raw.get('finished_at') or 0) < status['started_at']:
                    raise ValueError('stale child status')
                status = public_status(raw, source)
                expected = {'complete': 0, 'partial': 3, 'paused': 2, 'error': 1}.get(status['state'])
                if expected != code:
                    raise ValueError('child status/exit mismatch')
                pending = False
                if source == 'f95zone':
                    with closing(sqlite3.connect(state / 'ledger.sqlite3', timeout=15)) as ledger, ledger:
                        pending_row = ledger.execute("SELECT value FROM meta WHERE key='pending_publish'").fetchone()
                        pending = bool(pending_row and pending_row[0] == '1')
                if source == 'f95zone' and code in (0, 3) and (raw.get('changed', 0) or pending):
                    # Facts already committed remain safe if publishing is delayed.
                    with lock(root / 'data/slgking/catalogue-write.lock'):
                        publication = execute([str(root / '.venv/bin/python'), str(root / 'serve_catalog.py'),
                                               '--publish', '--data-dir', str(root / 'data/slgking'),
                                               '--publish-dir', str(root / 'publish')],
                                              env=dict(os.environ, LOCALAPPDATA=str(root / 'data')),
                                              timeout=120, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                        if publication.returncode:
                            raise RuntimeError('publication_failed')
                    status['last_publish'] = time.time()
                    raw['last_publish'] = status['last_publish']
                    atomic_json(state / 'status.json', raw)
                    with closing(sqlite3.connect(state / 'ledger.sqlite3', timeout=15)) as ledger, ledger:
                        ledger.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('pending_publish','0')")
                status['next_run'] = next_run(source)
        except (Exception,) as exc:
            status.update(state='error', reason_code='publication_failed' if isinstance(exc, RuntimeError)
                          else 'worker_or_status_failed', finished_at=time.time(), next_run=next_run(source))
            code = 1
        safe = public_status(status, source)
        atomic_json(target, safe)
        print(json.dumps(safe, ensure_ascii=False))
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=('f95zone', 'dlsite'), required=True)
    parser.add_argument('--max-requests', type=int)
    parser.add_argument('--max-seconds', type=int)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--recovery-only', action='store_true')
    parser.add_argument('--publish-pending', action='store_true')
    args = parser.parse_args(argv)
    try:
        validate_limits(args.source, args.max_requests, args.max_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    if args.resume and args.source != 'f95zone':
        parser.error('resume only applies to an explicit F95 operator request')
    if args.recovery_only and args.source != 'dlsite':
        parser.error('recovery only applies to DLsite')
    if args.publish_pending and (args.source != 'dlsite' or args.recovery_only or args.resume):
        parser.error('offline publication only applies to DLsite and cannot resume acquisition')
    try:
        return run(args.source, args.max_requests, args.max_seconds, args.resume, recovery_only=args.recovery_only, publish_pending=args.publish_pending)
    except OSError:
        print('Collector lock is held or state is unavailable', file=sys.stderr)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
