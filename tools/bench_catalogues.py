"""Offline catalogue switches: count is PER catalogue (total = twice count).

Reports query/first Tk layout separately from complete card and cover readiness.
Network calls are forbidden; all profile/application paths are temporary.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.bench_render import summarize


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--count',type=int,choices=(1000,10000),default=1000)
    p.add_argument('--rounds',type=int,default=30)
    p.add_argument('--covers',action='store_true')
    p.add_argument('--json',type=Path)
    a=p.parse_args()
    if a.rounds<2: p.error('rounds must be >=2')
    with tempfile.TemporaryDirectory(prefix='slg-catalogue-bench-',ignore_cleanup_errors=True) as data:
        for key in ('LOCALAPPDATA','APPDATA','HOME','USERPROFILE'): os.environ[key]=data
        import slg_gui,slg_db,slg_account,slg_remote,slg_translate,urllib.request
        from PIL import Image
        from slg_motion import MotionScheduler
        import tkinter as tk
        if not hasattr(slg_gui.App,'set_catalogue'): raise RuntimeError('catalogue UI not integrated')
        with contextlib.ExitStack() as mocks:
            def offline(*args,**kwargs): raise RuntimeError('network forbidden in catalogue benchmark')
            for obj,key,kwargs in (
                (urllib.request,'urlopen',dict(side_effect=offline)),
                (urllib.request.OpenerDirector,'open',dict(side_effect=offline)),
                (slg_account,'request',dict(side_effect=offline)),
                (slg_account,'session',dict(return_value=None)),
                (slg_remote,'report',dict(return_value=None)),
                (slg_remote,'fetch_config',dict(return_value=None)),
                (slg_translate,'translate_title',dict(side_effect=offline)),
                (slg_translate,'translate_overview',dict(side_effect=offline)),
                (slg_gui.App,'_load_dlsite_pilot',dict(return_value=None))):
                mocks.enter_context(patch.object(obj,key,**kwargs))
            conn=slg_db.connect()
            for source in ('western_slg','dlsite'):
                for i in range(a.count):
                    slug=('bench-western-%d'%i if source=='western_slg' else 'dlsite-rj%08d'%i)
                    gid,_=slg_db.upsert_game(conn,slug,'https://example.invalid/'+slug,
                        '%s Game %05d'%(source,i),tags=['story','bench'],complete=0)
                    slg_db.set_game_catalogue(conn,gid,source)
                    if source=='dlsite':
                        slg_db.set_dlsite_metadata(conn,gid,dict(
                            work_type='game',age_rating='R18',rating_value=4.2,
                            rating_count=10,rating_scale_max=5,languages=['ja']))
                    if i%3!=2: slg_db.set_state(conn,gid,status='want' if i%3==0 else 'playing')
                    if a.covers:
                        name=slug+'.png'; Image.new('RGB',(480,640),(i%256,70,130)).save(Path(slg_db.covers_dir())/name)
                        conn.execute('UPDATE games SET cover_file=? WHERE id=?',(name,gid))
            conn.commit(); conn.close()
            app=slg_gui.App(notify=False); app.geometry('1280x820'); app.update()
            def ready():
                deadline=time.perf_counter()+10
                while True:
                    app.update()
                    if not getattr(app,'_card_render_pending',False) and not getattr(app,'_cover_waiters',{}): return
                    if time.perf_counter()>deadline: raise RuntimeError('render/cover readiness timed out')
                    time.sleep(.001)
            first,complete=[],[]
            sequence=('western_slg','dlsite','all')
            for i in range(6): app.set_catalogue(sequence[i%3]); ready()
            for i in range(a.rounds):
                start=time.perf_counter(); app.set_catalogue(sequence[i%3]); app.update_idletasks()
                first.append((time.perf_counter()-start)*1000)
                ready(); complete.append((time.perf_counter()-start)*1000)
            # Western-only sorting remains comparable to the legacy same count.
            app.set_catalogue('western_slg'); ready(); sorts=[]
            for i in range(6+a.rounds):
                start=time.perf_counter(); label=('名称','最近更新')[i%2]
                app.sort_menu.set(label); app._on_sort(label); ready()
                if i>=6: sorts.append((time.perf_counter()-start)*1000)
            win=tk.Toplevel(app); canvas=tk.Canvas(win,width=240,height=100); canvas.pack(); app.update()
            dot=canvas.create_rectangle(0,40,12,52,fill='#e84393'); scheduler=MotionScheduler(canvas)
            started=time.perf_counter(); previous=[started]; gaps=[]
            def tick():
                now=time.perf_counter(); gaps.append((now-previous[0])*1000); previous[0]=now
                x=((now-started)*60)%228; canvas.coords(dot,x,40,x+12,52)
                scheduler.call_later(30,tick)
            scheduler.call_later(30,tick)
            while time.perf_counter()-started<4: app.update(); time.sleep(.001)
            scheduler.cancel_all(); win.destroy()
            result=dict(count_per_catalogue=a.count,total_games=a.count*2,rounds=a.rounds,covers=a.covers,
                dpi=app.winfo_fpixels('1i'),widget_scaling=app.list._get_widget_scaling(),
                first_tk_layout=summarize(first),complete_cards_and_covers=summarize(complete),
                western_sort=summarize(sorts),light_animation_tick_gap=summarize(gaps),
                note='First layout includes synchronous SQL; complete waits for card batches and PIL/Tk cover bindings; warmup uses cached covers; no real snapshot or network',
                network='forbidden; mocked',data='temporary isolated profile')
            app.destroy(); app.conn.close()
            output=json.dumps(result,ensure_ascii=False,indent=2)
            if a.json: a.json.parent.mkdir(parents=True,exist_ok=True); a.json.write_text(output+'\n',encoding='utf-8')
            print(output)

if __name__=='__main__': main()
