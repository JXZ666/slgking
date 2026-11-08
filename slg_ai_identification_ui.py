"""Explicit, bounded AI lookups. Results never write or bind games here."""
import queue
import threading
import tkinter as tk

import customtkinter as ctk
import slg_engines
from slg_candidate_widgets import WrappedLabel, CandidateRow
from slg_image_resources import ResourceButton, ResourceScrollableFrame
from slg_motion import MotionScheduler


def config_signature(config):
    # Memory only; never print this tuple or store it in the result/history.
    return (config.engine, config.base_url, config.api_key, config.model)


def open_ai_identification(app, items, *, on_provider, on_local=None, owner=None):
    """Callbacks receive (original item index, real candidate), on the Tk thread."""
    import slg_gui as gui
    import slg_ai_identification as ai
    if not items:
        return None
    previous = getattr(app, '_ai_identification_dialog', None)
    if previous and previous['win'].winfo_exists():
        previous['win'].destroy()
    identity = getattr(app, '_identity_generation', 0)
    win = app._new_dialog('AI辅助识别 · 查询后仍需确认', '800x720')
    color = lambda name: (gui._LIGHT[name], gui._DARK[name])
    win.configure(fg_color=color('BG'))
    scheduler = MotionScheduler(win)
    stop, pause = threading.Event(), threading.Event()
    inbox = queue.Queue(maxsize=1)
    state = dict(busy=False, selected=0, generation=0, signature=None, cursor=0, blocked='')
    records = [dict(item=dict(item), evidence=ai.normalize_evidence(item), result=None,
                    status='待发送', accepted=False) for item in items]
    footer = ctk.CTkFrame(win, fg_color='transparent')
    footer.pack(side='bottom', fill='x', padx=16, pady=10)
    page = ResourceScrollableFrame(win, fg_color=color('CARD'))
    page.pack(fill='both', expand=True, padx=16, pady=(12, 0))
    WrappedLabel(page, viewport=page._parent_canvas, padding=28,
        text='使用「设置」→「API 配置」中的接口、Key 和模型。只发送下方可编辑的识别线索，不发送完整路径、私人备注或整库。'
             '每项最多一次模型请求；每批20项，结果逐项确认。',
        font=gui.ui_font(12), text_color=color('MUTED')).pack(fill='x', padx=14, pady=10)
    config = slg_engines.resolve_config(app.conn)
    open_settings = getattr(app, 'open_translate_settings', None)
    if open_settings is not None and (config.is_free or not config.api_key):
        def configure_api():
            win.destroy()
            open_settings()
        ResourceButton(page, text='打开 API 配置', command=configure_api,
            fg_color=color('CHIP'), text_color=color('TEXT'),
            hover_color=color('CARD_HOVER'), font=gui.ui_font(12)).pack(anchor='w', padx=14, pady=(0, 8))
    selector = ctk.CTkOptionMenu(page, values=[''], font=gui.ui_font(13),
        fg_color=color('CHIP'), button_color=color('CHIP'), text_color=color('TEXT'))
    selector.pack(fill='x', padx=14, pady=4)
    fields = {}
    for key, label in [('title','游戏名称'), ('folder_name','目录末级名称'),
                       ('engine','引擎线索'), ('developer','开发者'), ('product_ids','已提取编号')]:
        row = ctk.CTkFrame(page, fg_color='transparent')
        row.pack(fill='x', padx=14, pady=2)
        row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text=label, width=94, anchor='w', text_color=color('TEXT'),
                     font=gui.ui_font(13)).grid(row=0,column=0,padx=(0,8))
        field = ctk.CTkEntry(row, height=34, fg_color=color('BG'), text_color=color('TEXT'))
        field.grid(row=0,column=1,sticky='ew')
        fields[key] = field
    source_bar = ctk.CTkFrame(page, fg_color='transparent')
    source_bar.pack(fill='x', padx=14, pady=10)
    source_vars = {}
    source_buttons = []
    for source, label in [('vndb','VNDB'),('bangumi','Bangumi'),('dlsite','DLsite')]:
        var = tk.BooleanVar(value=source != 'dlsite')
        source_vars[source] = var
        checkbox=ctk.CTkCheckBox(source_bar,text=label,variable=var,text_color=color('TEXT'),
                               font=gui.ui_font(13),width=105)
        checkbox.pack(side='left',padx=(0,8))
        source_buttons.append(checkbox)
    summary = WrappedLabel(page,viewport=page._parent_canvas,padding=28,text='',
        text_color=color('MUTED'),font=gui.ui_font(12))
    summary.pack(fill='x',padx=14,pady=6)
    result_area = ctk.CTkFrame(page,fg_color='transparent',width=1,height=1)
    result_area.pack(fill='x',padx=14,pady=6)
    status = WrappedLabel(footer,viewport=footer,padding=8,text='核对发送内容和资料来源后开始。',
        text_color=color('TEXT'),font=gui.ui_font(12))
    status.pack(fill='x',pady=(0,8))
    actions = ctk.CTkFrame(footer,fg_color='transparent')
    actions.pack(fill='x')
    actions.grid_columnconfigure((0,1,2),weight=1,uniform='ai-actions')
    common = dict(height=40,font=gui.ui_font(13),fg_color=color('CHIP'),
        hover_color=color('CARD_HOVER'),text_color=color('TEXT'),text_color_disabled=color('MUTED'))

    def alive():
        try:
            return (not stop.is_set() and bool(win.winfo_exists())
                    and identity == getattr(app, '_identity_generation', 0)
                    and (owner is None or bool(owner.winfo_exists())))
        except tk.TclError:
            return False

    def same_config():
        return (state['signature'] is None or state['signature'] ==
                config_signature(slg_engines.resolve_config(app.conn)))

    def save_editor():
        if state['busy']:
            return
        values = {key:field.get() for key,field in fields.items()}
        values['product_ids'] = values['product_ids'].replace('，',',').split(',')
        records[state['selected']]['evidence'] = ai.normalize_evidence(values)

    def names():
        return [f"{i+1}. {r['status']} · " +
                (r['evidence'].get('title') or r['evidence'].get('folder_name') or '未命名')[:55]
                for i,r in enumerate(records)]

    def use(candidate, local=False):
        if state['busy']:
            return
        if not alive() or not same_config():
            status.configure(text='身份或API配置已变化，请重新查询。')
            return
        i = state['selected']
        callback = on_local if local else on_provider
        if callback is not None:
            callback(i, candidate)
            status.configure(text='候选已送至确认界面；请核对后确认，查询本身不会写入。')

    def paint():
        if not alive():
            return
        choices = names()
        selector.configure(values=choices)
        selector.set(choices[state['selected']])
        record = records[state['selected']]
        for key,field in fields.items():
            field.configure(state='normal')
            field.delete(0,'end')
            value = record['evidence'].get(key,'')
            field.insert(0,', '.join(value) if isinstance(value,list) else value)
            if state['busy'] or state['selected'] < state['cursor']:
                field.configure(state='disabled')
        for button in source_buttons:
            button.configure(state='disabled' if state['busy'] else 'normal')
        for child in result_area.winfo_children():
            child.destroy()
        result = record['result']
        summary.configure(text=record['status'])
        if result:
            text = '检索建议：' + ' / '.join(result['terms']) + '\n' + result.get('reason','')
            errors = result.get('source_errors',[])
            if errors:
                text += '\n' + '\n'.join(str(e['source'])+'：'+str(e['message']) for e in errors)
            summary.configure(text=text)
            if on_local:
                for candidate in result.get('local_candidates',[])[:20]:
                    shown = dict(candidate, source='已收录', external_id=str(candidate['id']))
                    CandidateRow(result_area,shown,lambda c:use(c,True),color=color,font=gui.ui_font,
                        alive=alive,enabled=lambda:alive() and not state['busy'],
                        action_text='选择作品').pack(fill='x',pady=3)
            for candidate in result['candidates'][:30]:
                CandidateRow(result_area,candidate,lambda c:use(c),color=color,font=gui.ui_font,
                    alive=alive,enabled=lambda:alive() and not state['busy']).pack(fill='x',pady=3)
        start_button.configure(state='disabled' if state['busy'] or state['cursor'] >= len(records) else 'normal',
            text='继续下一批' if state['cursor'] else '确认发送并查询')
        pause_button.configure(state='normal' if state['busy'] else 'disabled')

    def select(label):
        save_editor()
        state['selected'] = int(label.split('.',1)[0])-1
        paint()
    selector.configure(command=select)

    def deliver(value):
        while not stop.is_set():
            try:
                inbox.put(value,timeout=.1)
                return
            except queue.Full:
                pass

    def start():
        if not alive() or state['busy'] or state['cursor'] >= len(records):
            return
        if not getattr(app,'_network_enabled',True):
            status.configure(text='当前处于离线模式，普通本机匹配仍可用。')
            return
        save_editor()
        config = slg_engines.resolve_config(app.conn)
        if config.is_free or not config.api_key:
            status.configure(text='请在「设置」→「API 配置」中选择 AI 引擎并保存接口地址、Key 和模型。')
            return
        if not same_config():
            status.configure(text='API配置已变化，请关闭本窗口后重新查询。')
            return
        sources = tuple(key for key,var in source_vars.items() if var.get())
        if not sources:
            status.configure(text='请至少选择一个资料来源。')
            return
        state['signature'] = config_signature(config)
        state['busy'] = True
        state['blocked'] = ''
        state['generation'] += 1
        generation = state['generation']
        config_generation = getattr(app, '_ai_config_generation', 0)
        pause.clear()
        first = state['cursor']
        batch = [(i,dict(records[i]['evidence'])) for i in range(first,min(len(records),first+ai.MAX_BATCH_ITEMS))]
        db_path = app.conn.execute('PRAGMA database_list').fetchone()[2]
        # Per-game opt-outs are resolved before the worker receives its snapshot.
        import slg_library_metadata as metadata
        allowed = {}
        for i,_ in batch:
            gid = records[i]['item'].get('game_id')
            allowed[i] = tuple(s for s in sources if not gid or metadata.query_allowed(app.conn,gid,s))
        def cancelled():
            return (stop.is_set() or generation != state['generation']
                    or identity != getattr(app, '_identity_generation', 0)
                    or config_generation != getattr(app, '_ai_config_generation', 0))
        def worker():
            from slg_game_picker_query import PickerQuery
            query = None
            try:
                query = PickerQuery(db_path)
                def local_search(terms):
                    found = {}
                    for term in terms:
                        for row in query.search(term,stop=stop)['rows']:
                            found[row['id']] = row
                    return list(found.values())[:20]
                for i,evidence in batch:
                    if cancelled() or pause.is_set():
                        break
                    deliver(('stage',generation,i,'正在整理检索建议…'))
                    try:
                        if not allowed[i]:
                            raise ValueError('此游戏的所选资料来源已关闭，请先在资料来源中开启。')
                        result = ai.identify(config,evidence,allowed[i],should_stop=cancelled,
                            local_search=local_search,
                            on_stage=lambda text:deliver(('stage',generation,i,text)))
                        error = ''
                    except Exception as exc:
                        if getattr(exc, 'code', '') == 'request_busy':
                            deliver(('blocked',generation,i,'上次请求仍在结束，本项尚未发送。稍后可继续，不会跳过本项。'))
                            break
                        result, error = None, str(exc)
                    if cancelled():
                        break
                    deliver(('result',generation,i,(result,error)))
            except Exception:
                deliver(('blocked',generation,first,'无法读取本机目录，本批尚未开始。关闭后可重新查询。'))
            finally:
                if query is not None:
                    query.close()
                deliver(('done',generation,None,None))
        paint()
        threading.Thread(target=worker,name='ai-identification-batch',daemon=True).start()

    def poll():
        if not alive():
            stop.set()
            if win.winfo_exists():
                win.destroy()
            return
        if not same_config():
            stop.set()
            win.destroy()
            return
        try:
            kind,generation,i,value = inbox.get_nowait()
        except queue.Empty:
            scheduler.call_later(70,poll)
            return
        if generation == state['generation']:
            if not same_config():
                stop.set()
                win.destroy()
                return
            if kind == 'stage':
                records[i]['status'] = value
                status.configure(text=f'第{i+1}/{len(records)}项 · {value} 可暂停后续请求。')
                scheduler.call_later(70,poll)
                return
            elif kind == 'blocked':
                state['blocked'] = value
                records[i]['status'] = '尚未发送'
                status.configure(text=value)
            elif kind == 'result':
                result,error = value
                records[i]['result'] = result
                records[i]['status'] = ('失败：'+error+' 需要重试时请关闭后重新发起。' if error else '待确认' if
                    result['candidates'] or result['local_candidates'] else '未找到候选')
                state['cursor'] = i+1
            elif kind == 'done':
                state['busy'] = False
                status.configure(text=state['blocked'] or
                    f"本批已结束，已处理{state['cursor']}/{len(records)}项。结果须逐项确认。")
            paint()
        scheduler.call_later(70,poll)

    def destroy(event):
        if event.widget is win:
            stop.set()
            state['generation'] += 1
            scheduler.cancel_all()
    win.bind('<Destroy>',destroy,add='+')
    win._slg_before_destroy = stop.set
    start_button = ResourceButton(actions,text='确认发送并查询',command=start,**common)
    start_button.grid(row=0,column=0,sticky='ew',padx=(0,4))
    pause_button = ResourceButton(actions,text='暂停后续',command=lambda:(pause.set(),status.configure(
        text='当前请求结束后暂停；继续下一批不会重复已处理项。')),**common)
    pause_button.grid(row=0,column=1,sticky='ew',padx=4)
    ResourceButton(actions,text='关闭',command=win.destroy,**common).grid(row=0,column=2,sticky='ew',padx=(4,0))
    app._ai_identification_dialog = dict(win=win,state=state,records=records,start=start,
        fields=fields,status=status,pause=pause,stop=stop,select=select,use=use)
    paint()
    scheduler.call_later(70,poll)
    return win
