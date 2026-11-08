"""Review one RPG data directory registration at a time; retain the game."""
import queue
import threading
import tkinter as tk
import customtkinter as ctk
import slg_db
from slg_candidate_widgets import WrappedLabel
from slg_image_resources import ResourceButton, ResourceScrollableFrame
from slg_motion import MotionScheduler


def open_repairs(app, game_id=None):
    if not app._require_personal_access('修正游戏目录'):
        return
    import slg_gui as gui
    import slg_scan_repair as repair
    identity = app._library_identity()
    win = app._new_dialog('核对RPG数据目录登记','700x560')
    color = lambda key:(gui._LIGHT[key],gui._DARK[key])
    win.configure(fg_color=color('BG'))
    footer = ctk.CTkFrame(win,fg_color='transparent')
    footer.pack(side='bottom',fill='x',padx=16,pady=12)
    page = ResourceScrollableFrame(win,fg_color=color('CARD'))
    page.pack(fill='both',expand=True,padx=16,pady=12)
    stop = threading.Event()
    win._slg_before_destroy=stop.set
    win.bind('<Destroy>',lambda e:stop.set() if e.widget is win else None,add='+')
    scheduler=MotionScheduler(win)
    inbox=queue.Queue(maxsize=1)
    state=dict(rows=[],index=0,busy=False)
    scope = '只检查当前登记的目录及其父目录。' if game_id is not None else '只检查已登记的 www 目录及其父目录。'
    WrappedLabel(page,viewport=page._parent_canvas,padding=28,text=scope+'确认后只修改目录和启动程序，保留原游戏、名称、收藏、备注和封面。',
        text_color=color('MUTED')).pack(fill='x',padx=14,pady=10)
    select=ctk.CTkOptionMenu(page,values=['正在检查…'],text_color=color('TEXT'),fg_color=color('CHIP'),button_color=color('CHIP'))
    select.pack(fill='x',padx=14,pady=6)
    facts=WrappedLabel(page,viewport=page._parent_canvas,padding=28,text='',text_color=color('TEXT'))
    facts.pack(fill='x',padx=14,pady=10)
    exe=ctk.CTkOptionMenu(page,values=['请选择启动程序'],text_color=color('TEXT'),fg_color=color('CHIP'),button_color=color('CHIP'))
    exe.pack(fill='x',padx=14,pady=6)
    status=WrappedLabel(footer,viewport=footer,padding=0,text='正在准备修正预览…',text_color=color('MUTED'))
    status.pack(fill='x',pady=6)
    def alive():
        return not stop.is_set() and win.winfo_exists() and identity['generation']==getattr(app,'_identity_generation',0)
    def show(value=None):
        if state['busy'] or not state['rows']:
            return
        if value is not None:state['index']=int(value.split('.',1)[0])-1
        row=state['rows'][state['index']]
        facts.configure(text='%s\n原目录：%s\n建议目录：%s\n%s' %
            (row['title'],row['old_folder_path'],row['folder_path'],row['reason']))
        paths=row.get('executables',[])
        exe.configure(values=['请选择启动程序']+paths)
        exe.set(paths[0] if len(paths)==1 else '请选择启动程序')
        apply_button.configure(state='normal' if row['can_apply'] else 'disabled')
    select.configure(command=show)
    def apply():
        if not alive() or state['busy'] or identity!=app._library_identity():return
        preview=state['rows'][state['index']]
        path=exe.get()
        if path=='请选择启动程序':
            status.configure(text='请核对并选择一个启动程序。');return
        state['busy']=True
        apply_button.configure(state='disabled')
        select.configure(state='disabled')
        exe.configure(state='disabled')
        status.configure(text='正在重新核验目录与登记，完成后保存…')
        def save_worker():
            error=''
            try:
                with slg_db.session(db_path) as conn:
                    if stop.is_set() or identity['generation']!=getattr(app,'_identity_generation',0):
                        return
                    repair.apply_www_repair(conn,preview,path)
                    if stop.is_set() or identity['generation']!=getattr(app,'_identity_generation',0):
                        conn.rollback()
                        return
                    conn.commit()
            except Exception as exc:
                error=str(exc)
            if not stop.is_set():inbox.put_nowait(('saved',preview,error))
        threading.Thread(target=save_worker,name='www-repair-apply',daemon=True).start()
        scheduler.call_later(70,poll)
    apply_button=ResourceButton(footer,text='确认修正当前条目',command=apply,state='disabled',height=40,
        fg_color=color('CHIP'),hover_color=color('CARD_HOVER'),text_color=color('TEXT'))
    apply_button.pack(fill='x')
    db_path=app.conn.execute('PRAGMA database_list').fetchone()[2]
    def worker():
        from slg_library_query import open_readonly
        conn=None
        try:
            conn=open_readonly(db_path)
            value=('preview',repair.preview_www_repairs(conn,game_id,stop.is_set),'')
        except Exception as error:value=('preview',[],str(error))
        finally:
            if conn is not None:conn.close()
        if not stop.is_set():inbox.put_nowait(value)
    def poll():
        if not alive():
            stop.set()
            if win.winfo_exists():win.destroy()
            return
        try:kind,rows,error=inbox.get_nowait()
        except queue.Empty:scheduler.call_later(70,poll);return
        if kind=='saved':
            state['busy']=False
            select.configure(state='normal')
            exe.configure(state='normal')
            if error:
                status.configure(text='未修改：'+error)
                show()
                return
            rows['can_apply']=False
            status.configure(text='此条目已修正，个人资料保留。其他条目仍需分别确认。')
            app._library_personal_changed()
            app.refresh(preserve_scroll=True)
            return
        if error:status.configure(text='检查失败：'+error);return
        if not rows:status.configure(text='没有发现需要核对的 RPG 数据目录登记。');return
        state['rows']=rows
        names=[f"{i+1}. {r['title']}" for i,r in enumerate(rows)]
        select.configure(values=names);select.set(names[0]);show()
        status.configure(text='发现%d项；请逐项核对父目录与启动程序。'%len(rows))
    threading.Thread(target=worker,name='www-repair-preview',daemon=True).start()
    scheduler.call_later(70,poll)
    win._repair_view=dict(state=state,apply=apply,select=select,exe=exe,status=status)
    return win
