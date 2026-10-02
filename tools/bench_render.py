"""Offline, isolated render benchmark; timings include Tk layout, not display scanout."""
import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def summarize(v):
    s = sorted(v)
    return dict(samples=len(v), median_ms=statistics.median(v),
                p95_ms=s[max(0, math.ceil(len(v)*.95)-1)], max_ms=max(v))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--count', type=int, choices=(1000,10000), default=1000)
    p.add_argument('--rounds', type=int, default=30)
    p.add_argument('--json', type=Path)
    p.add_argument('--covers', action='store_true')
    p.add_argument('--heartbeat', action='store_true')
    a = p.parse_args()
    if a.rounds < 2: p.error('rounds must be at least 2')
    with tempfile.TemporaryDirectory(prefix='slgbench-', ignore_cleanup_errors=True) as data:
        os.environ['LOCALAPPDATA'] = data
        import slg_db, slg_gui, slg_account, slg_remote, urllib.request
        from PIL import Image
        with contextlib.ExitStack() as mocks:
            def offline(*args, **kwargs):
                raise RuntimeError('network disabled in benchmark')
            for target, attr, kw in (
                (urllib.request, 'urlopen', dict(side_effect=offline)),
                (urllib.request.OpenerDirector, 'open', dict(side_effect=offline)),
                (slg_account, 'request', dict(side_effect=offline)),
                (slg_account, 'session', dict(return_value=None)),
                (slg_remote, 'fetch_config', dict(return_value=None)),
                (slg_remote, 'report', dict(return_value=None))):
                mocks.enter_context(patch.object(target, attr, **kw))
            conn = slg_db.connect()
            for i in range(a.count):
                gid, _ = slg_db.upsert_game(conn, 'bench-%d'%i, 'u/%d'%i,
                    'Bench Game %05d v1.0'%i,
                    tags=['netorare','big-tits','anal','bdsm','story'], complete=i%2)
                if i%3 != 2: slg_db.set_state(conn,gid,status='want' if i%3==0 else 'playing')
                if a.covers:
                    name = 'bench-%d.png'%i
                    Image.new('RGB',(480,640),(i%256,70,130)).save(Path(slg_db.covers_dir())/name)
                    conn.execute('UPDATE games SET cover_file=? WHERE id=?',(name,gid))
            conn.commit(); conn.close()
            wall, cpu = time.perf_counter(), time.process_time()
            app = slg_gui.App(notify=False)
            app.geometry('1280x820'); app.update()
            startup = (time.perf_counter()-wall)*1000
            costs, gaps, latency = [], [], []
            import tkinter as tk
            original_call = tk.CallWrapper.__call__
            def timed(wrapper,*args):
                start = time.perf_counter()
                try: return original_call(wrapper,*args)
                finally: costs.append((time.perf_counter()-start)*1000)
            mocks.enter_context(patch.object(tk.CallWrapper, "__call__", timed))
            last, alive = [time.perf_counter()], [True]
            def beat():
                now = time.perf_counter(); gaps.append((now-last[0])*1000); last[0] = now
                if alive[0]: app.after(16,beat)
            if a.heartbeat: app.after(16,beat)
            def settle():
                deadline = time.perf_counter()+3
                while True:
                    app.update()
                    if not getattr(app,'_card_render_pending',False): return
                    if time.perf_counter()>deadline: raise RuntimeError('render did not settle')
                    time.sleep(.001)
            def measure(fn):
                start = time.perf_counter(); fn(); settle()
                return (time.perf_counter()-start)*1000
            def alternating(fn):
                for i in range(6): fn(i); settle()
                return summarize([measure(lambda i=i: fn(i)) for i in range(a.rounds)])
            def cold(i):
                for child in app.list.winfo_children(): child.destroy()
                app._card_pool, app._pool_gid = [], []
                app._cards, app._card_meta, app._card_title = {}, {}, {}
                app._widget_gid, app._card_slot = {}, {}
                app._empty_label = None; app._rendered_ids = []; app.page=1
                app.refresh()
            metrics = dict(cold_render=alternating(cold))
            metrics['sort_change'] = alternating(lambda i: app._on_sort(('名称','最近更新')[i%2]))
            metrics['view_switch'] = alternating(lambda i: app.set_view(('want','playing')[i%2]))
            app.set_view(None); settle()
            metrics['page_turn'] = alternating(lambda i: app._goto_page((1,2)[i%2]))
            metrics['page_jump'] = alternating(lambda i: app._goto_page((1,100)[i%2]))
            dispatch = app._dispatch
            def tracked(kind,payload):
                if kind=='bench_latency': latency.append((time.perf_counter()-payload)*1000)
                else: return dispatch(kind,payload)
            app._dispatch = tracked
            for i in range(200):
                app.queue.put(('progress','offline progress %d'%i))
                app.queue.put(('bench_latency',time.perf_counter()))
            deadline=time.perf_counter()+5
            while len(latency)<200 and time.perf_counter()<deadline:
                app.update(); time.sleep(.001)
            if len(latency)!=200: raise RuntimeError('queue did not drain')
            mixed_gaps, mixed_costs = list(gaps), list(costs)
            # Contiguous progress burst reflects coalescing; start drain explicitly
            # to remove arbitrary idle-poll phase. Existing marker latency above
            # remains a mixed-message test and is not a coalescing benchmark.
            burst_count=len(latency); burst_start=time.perf_counter()
            for i in range(200): app.queue.put(('progress','burst %d'%i))
            app.queue.put(('bench_latency',burst_start))
            app._drain()
            deadline=time.perf_counter()+3
            while len(latency)==burst_count and time.perf_counter()<deadline:
                app.update(); time.sleep(.001)
            burst_ms=latency[-1] if len(latency)>burst_count else None
            main_cpu=(time.process_time()-cpu)*1000
            # Independent four-second steady lightweight Canvas animation.
            # It deliberately excludes SQL/cold builds measured above.
            import tkinter as tk
            from slg_motion import MotionScheduler
            win=tk.Toplevel(app); win.geometry('240x120')
            canvas=tk.Canvas(win,width=240,height=120); canvas.pack(); app.update()
            dot=canvas.create_rectangle(0,40,12,52,fill='#e84393',outline='')
            scheduler=MotionScheduler(canvas); steady=[]
            started=time.perf_counter(); previous=[started]
            def light_tick():
                now=time.perf_counter(); steady.append((now-previous[0])*1000)
                previous[0]=now
                x=((now-started)*60)%228
                canvas.coords(dot,x,40,x+12,52)
                scheduler.call_later(30,light_tick)
            scheduler.call_later(30,light_tick)
            while time.perf_counter()-started<4:
                app.update(); time.sleep(.001)
            scheduler.cancel_all(); win.destroy()
            viewport=app.list._parent_canvas.winfo_height()
            card_h=app._card_pool[0]['frame'].winfo_height()+4
            result=dict(count=a.count,rounds=a.rounds,covers=a.covers,
                window='1280x820',dpi=app.winfo_fpixels('1i'),
                widget_scaling=app.list._get_widget_scaling(),page_size=slg_gui.PAGE_SIZE,
                window_physical_px=[app.winfo_width(),app.winfo_height()],
                screen_physical_px=[app.winfo_screenwidth(),app.winfo_screenheight()],
                viewport_px=viewport,card_height_px=card_h,
                startup_ms=startup,page_slack_px=viewport-card_h*slg_gui.PAGE_SIZE,
                cpu_ms=main_cpu,metrics=metrics,
                light_animation_tick_gap=summarize(steady),
                light_animation_duration_s=4,queue_progress_burst_ms=burst_ms,
                queue_latency=summarize(latency[:200]),callback=summarize(mixed_costs) if mixed_costs else None,
                heartbeat_gap=summarize(mixed_gaps) if mixed_gaps else None,
                network='mocked; offline',data='temporary isolated database',
                note='callback excludes direct handlers; mixed heartbeat includes SQL; steady phase reported separately; warm covers cached after warmup; startup does not await async cover decode')
            alive[0]=False; app.destroy(); app.conn.close()
            output=json.dumps(result,ensure_ascii=False,indent=2)
            if a.json:
                a.json.parent.mkdir(parents=True,exist_ok=True)
                a.json.write_text(output+'\n',encoding='utf-8')
            print(output)

if __name__=='__main__': main()
