"""Per-game private source associations. No network or writes until explicit action."""
import queue
import threading
import tkinter as tk
from tkinter import messagebox
import webbrowser
import time

import customtkinter as ctk
import slg_library_metadata as metadata
from slg_source_registry import SOURCES, valid_id
from slg_candidate_widgets import (
    CandidateRow, LookupBar, LookupPanes, OfficialLinkPanel, WrappedLabel,
    enable_button_keys,
)
import slg_library_providers as providers
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame


class LibrarySourceUI:
    def open_library_sources(self, game):
        if not self._require_personal_access('管理资料来源'):
            return
        import slg_gui as gui
        previous = getattr(self, '_library_source_window', None)
        if previous is not None:
            try:
                if previous.winfo_exists():
                    previous.destroy()
            except tk.TclError:
                pass
        game = dict(game)
        identity = self._library_identity()
        window = self._new_dialog('资料来源与私人关联', '720x640')
        self._library_source_window = window
        color = lambda key: (gui._LIGHT[key], gui._DARK[key])
        common = dict(fg_color=color('CHIP'), hover_color=color('CARD_HOVER'),
                      text_color=color('TEXT'), text_color_disabled=color('MUTED'))
        window.configure(fg_color=color('BG'))
        stop, inbox = threading.Event(), queue.Queue(maxsize=1)
        state = dict(generation=0, busy=False, selected=None, polling=None)
        # Reserve actions before the flexible scroll body. Long work titles
        # and source facts belong inside that body, never above the footer.
        header = ctk.CTkFrame(window, fg_color='transparent')
        header.pack(fill='x', padx=16, pady=(10, 0))
        footer = ctk.CTkFrame(window, fg_color='transparent')
        footer.pack(side='bottom', fill='x', padx=16, pady=10)
        bar = LookupBar(header, [entry.label for entry in SOURCES.values()], lambda: run(),
                        color=color, font=gui.ui_font, action_text='查询候选')
        bar.pack(fill='x')
        source, query = bar.source, bar.query
        enabled = tk.BooleanVar(master=window, value=True)
        actions = ctk.CTkFrame(header, fg_color='transparent')
        actions.pack(fill='x', pady=(6, 0))
        enabled_control = ctk.CTkCheckBox(header, text='允许查询此来源', variable=enabled,
            command=lambda: toggle(), text_color=color('TEXT'), font=gui.ui_font(12))
        enabled_control.pack(anchor='w', pady=(7, 0))
        status = WrappedLabel(header, viewport=header, text='', text_color=color('TEXT'),
                              font=gui.ui_font(12))
        status.pack(fill='x', pady=(4, 3))
        panes = LookupPanes(window, color=color, font=gui.ui_font)
        panes.pack(fill='both', expand=True, padx=16, pady=5)
        body, page = panes.results, panes.details
        result_note = WrappedLabel(body, viewport=body._parent_canvas, padding=24, text='',
                                   text_color=color('MUTED'), font=gui.ui_font(12))
        result_note.pack(fill='x', padx=12, pady=8)
        select_note = '选择候选并核对详情后，可保存为本机私人来源关联。'
        facts = WrappedLabel(page, viewport=page._parent_canvas, padding=28,
                             text=select_note, text_color=color('TEXT'), font=gui.ui_font(13))
        facts.pack(fill='x', padx=14, pady=8)
        links = OfficialLinkPanel(page,color=color,font=gui.ui_font,alive=lambda:alive())
        WrappedLabel(page, viewport=page._parent_canvas, padding=28,
            text='当前游戏：' + (game.get('title') or ''),
            text_color=color('MUTED'), font=gui.ui_font(12)).pack(fill='x', padx=14, pady=(4, 8))

        def selected_source():
            return next(key for key,entry in SOURCES.items() if entry.label == source.get())

        def alive():
            try:
                return not stop.is_set() and bool(window.winfo_exists()) and identity == self._library_identity()
            except tk.TclError:
                return False

        def rows():
            return [child for child in body.winfo_children() if isinstance(child, CandidateRow)]

        def show_rows(candidates=(), note='查询结果会显示在这里。'):
            for row in rows():
                row.destroy()
            result_note.configure(text=note)
            for candidate in candidates[:10]:
                CandidateRow(body, candidate, choose, color=color, font=gui.ui_font,
                             alive=alive, action_text='查看详情').pack(fill='x', padx=8, pady=3)
            body._parent_canvas.yview_moveto(0)

        def clear_selection(text=select_note):
            state['selected'] = None
            links.clear()
            links.pack_forget()
            facts.configure(text=text)
            page._parent_canvas.yview_moveto(0)
            save.configure(state='disabled')

        def controls():
            busy = state['busy']
            interactive = 'disabled' if busy else 'normal'
            bar.set_busy(busy)
            for button in (enabled_control, ai_button, auth_button, detach_button):
                button.configure(state=interactive)
            for row in rows():
                row.choose_button.configure(state=interactive)
            save.configure(state='normal' if state['selected'] and not busy else 'disabled')

        def reset_lookup(*_):
            if not alive():
                return
            state['generation'] += 1
            clear_selection()
            show_rows()
            status.configure(text='查询词已修改，请重新查询。')

        def show_link(candidate):
            if links.set_candidate(candidate):
                links.pack(fill='x', after=facts, padx=14, pady=(0, 10))

        def refresh(*_):
            if not alive():
                return
            key = selected_source()
            row = next(r for r in metadata.source_rows(self.conn, game['id']) if r['source'] == key)
            enabled.set(row['enabled'])
            bar.term.set(row['external_id'] or game.get('title') or '')
            auth_button.pack_forget()
            detach_button.pack_forget()
            if key == 'bangumi':
                auth_button.pack(side='left', padx=(8, 0))
            if row['private']:
                detach_button.pack(side='left', padx=(8, 0))
            if row['external_id']:
                scope = '当前私人关联' if row['private'] else '当前目录来源'
                facts.configure(text=f"{scope}：{SOURCES[key].label} · {row['external_id']}\n\n{select_note}")
                show_link(row)
            status.configure(text='选择来源并查询候选。保存关联保留名称、封面和本地路径。')

        def choose(candidate):
            if candidate['source'] != selected_source():
                return
            run('details', candidate['external_id'])

        def poll():
            state['polling'] = None
            if not alive():
                stop.set()
                try:
                    if window.winfo_exists():
                        window.destroy()
                except tk.TclError:
                    pass
                return
            try:
                generation, kind, value, error = inbox.get_nowait()
            except queue.Empty:
                state['polling'] = window.after(60, poll)
                return
            state['busy'] = False
            if generation == state['generation']:
                if error is not None:
                    if kind == 'details':
                        facts.configure(text='详情读取失败。可再次查看此候选，或选择其他作品。')
                        status.configure(text='读取详情失败：' + error)
                    else:
                        show_rows(note='查询未完成。')
                        status.configure(text='查询失败：' + error + '。可重试、修改查询词或切换来源。')
                elif kind == 'search':
                    if value:
                        show_rows(value, note=f'找到 {min(len(value), 10)} 项候选。')
                        status.configure(text='查看候选详情，核对作品后再保存关联。')
                    else:
                        show_rows(note='未找到候选。')
                        status.configure(text='未找到资料。可修改查询词、切换来源或取消。')
                else:
                    state['selected'] = value
                    show_link(value)
                    facts.configure(text=f"{value['title']}\n{SOURCES[value['source']].label} · {value['external_id']}\n开发者：{value.get('developer') or '未知'}\n"
                                         f"发布日期：{value.get('release_date') or '未知'}\n保存只修改本机私人来源关联。")
                    status.configure(text='请核对作品身份，然后确认保存。')
            controls()
            state['polling'] = window.after(60, poll)

        def run(kind='search', term=None):
            if not alive() or state['busy']:
                return
            if not getattr(self, '_network_enabled', True):
                status.configure(text='当前处于离线模式。可查看已有资料，或联网后重试。')
                return
            selected = selected_source()
            if not enabled.get() or not metadata.query_allowed(self.conn, game['id'], selected):
                status.configure(text='此来源的联网查询已关闭；勾选后才能查询。')
                return
            if not SOURCES[selected].search:
                status.configure(text='请在扫描 Steam 游戏库中读取本机 Steam 资料。')
                return
            term = term or query.get().strip()
            if not term:
                status.configure(text='请输入作品名称、编号或官方链接。')
                return
            if kind == 'details' and not valid_id(selected, term):
                status.configure(text='所选资料的编号无效，请重新查询。')
                return
            state['generation'] += 1
            generation = state['generation']
            state['busy'] = True
            clear_selection()
            if kind == 'search':
                show_rows(note='正在查询候选…')
                status.configure(text='正在查找候选，可能需要一些时间。可随时取消。')
            else:
                for row in rows():
                    row.set_selected(row.candidate['external_id'] == term)
                facts.configure(text='正在读取所选作品的详情…')
                status.configure(text='正在读取所选作品的资料，可能需要一些时间。可随时取消。')
            controls()

            def cancelled():
                return stop.is_set() or generation != state['generation']

            def worker():
                try:
                    if kind == 'search':
                        value = providers.search_candidates(selected, term, cancelled)
                    else:
                        raw = (providers.structured_details(term, cancelled) if selected == 'dlsite'
                               else providers.details(selected, term, cancelled))
                        value = metadata.validate(raw)
                        if value['source'] != selected or value['external_id'].casefold() != term.casefold():
                            raise ValueError('所选候选与详情返回的来源或编号不一致，请重新查询。')
                    item = (generation, kind, value, None)
                except Exception as exc:
                    item = (generation, kind, None, str(exc)[:180])
                if not stop.is_set():
                    try:
                        inbox.put_nowait(item)
                    except queue.Full:
                        pass
            threading.Thread(target=worker, daemon=True, name='local-source-query').start()
            if state['polling'] is None:
                state['polling'] = window.after(60, poll)

        def notify_changed():
            def notify():
                if not alive():
                    return
                callback = getattr(self, '_library_personal_changed', None)
                if callback:
                    callback()
                refresh_main = getattr(self, 'refresh', None)
                if refresh_main:
                    refresh_main(preserve_scroll=True)
            window.after_idle(notify)

        def toggle():
            if not alive() or state['busy']:
                return
            try:
                metadata.set_enabled(self.conn, game['id'], selected_source(), enabled.get())
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                current = next(r for r in metadata.source_rows(self.conn, game['id']) if r['source'] == selected_source())
                enabled.set(current['enabled'])
                status.configure(text='保存失败：' + str(exc))
                return
            reset_lookup()
            status.configure(text='已开启此来源查询。' if enabled.get() else '已关闭此来源查询。')
            notify_changed()

        def apply():
            candidate = state['selected']
            if not alive() or state['busy'] or not candidate:
                return
            generation = state['generation']
            if not messagebox.askyesno('确认私人关联', '确认这是同一款作品？只保存来源关联，原名称和个人封面保持不变。', parent=window):
                return
            # Modal confirmation pumps Tk events: identity, source and generation
            # must still match after it returns, before any personal write.
            if not alive() or generation != state['generation']:
                return
            try:
                metadata.attach(self.conn, game['id'], candidate)
                self.conn.commit()
                refresh()
                status.configure(text='已保存私人来源关联。')
                notify_changed()
            except Exception as exc:
                self.conn.rollback()
                status.configure(text=str(exc))

        def detach():
            if not alive() or state['busy']:
                return
            try:
                metadata.detach(self.conn, game['id'], selected_source())
                self.conn.commit()
                refresh()
                status.configure(text='已解除私人来源关联。')
                notify_changed()
            except Exception as exc:
                self.conn.rollback()
                status.configure(text='解除失败，原关联保留：' + str(exc))

        def authorize():
            self.open_bangumi_authorization()

        def ai_lookup():
            if not alive() or state['busy']:
                return
            from slg_ai_identification_ui import open_ai_identification
            generation = state['generation']
            evidence = dict(game, game_id=game['id'], title=query.get().strip() or game.get('title',''))

            def chosen(_index, candidate):
                nonlocal generation
                if not alive() or state['busy'] or generation != state['generation']:
                    return
                key = candidate['source']
                if key not in SOURCES or not SOURCES[key].search or not metadata.query_allowed(self.conn,game['id'],key):
                    status.configure(text='此来源查询已关闭，请先在资料来源中开启。')
                    return
                source.set(SOURCES[key].label)
                refresh()
                show_rows([candidate], note='核对所选候选的详情。')
                run('details',candidate['external_id'])
                generation = state['generation']
                window.lift()
            open_ai_identification(self,[evidence],on_provider=chosen,owner=window)

        ai_button = ResourceButton(actions, text='AI辅助识别…', command=ai_lookup, width=114, **common)
        ai_button.pack(side='left')
        auth_button = ResourceButton(actions, text='Bangumi本机授权…', command=authorize, **common)
        detach_button = ResourceButton(actions, text='解除私人关联', command=detach, width=114, **common)
        save = ResourceButton(footer, text='保存私人来源关联', command=apply, state='disabled',
                              height=40, fg_color=color('ACCENT'), hover_color=color('ACCENT_HOVER'),
                              text_color=color('ON_ACCENT'), text_color_disabled=color('MUTED'),
                              font=gui.ui_font(13))
        save.pack(side='right')
        cancel = ResourceButton(footer, text='取消', command=window.destroy, width=80,
                                height=40, **common)
        cancel.pack(side='right', padx=(0, 8))
        for button in (ai_button, auth_button, detach_button, save, cancel):
            enable_button_keys(button)
        source.configure(command=refresh)
        trace = bar.term.trace_add('write', reset_lookup)

        def destroyed(event):
            if event.widget == window:
                stop.set()
                state['generation'] += 1
                bar.term.trace_remove('write', trace)
                if getattr(self, '_library_source_window', None) is window:
                    self._library_source_window = None
                if state['polling']:
                    try:
                        window.after_cancel(state['polling'])
                    except tk.TclError:
                        pass
        window._slg_before_destroy = stop.set
        window.bind('<Destroy>', destroyed, add='+')
        refresh()
        state['polling'] = window.after(60, poll)
        window._library_sources_view = dict(state=state,source=source,query=query,results=body,
            facts=facts,links=links,run=run,refresh=refresh,page=page,footer=footer,
            save=save,status=status)
        return window

    def open_bangumi_authorization(self):
        """No token enters task payloads, logs, backup sections or cloud APIs."""
        import slg_db
        from slg_bangumi_auth import TokenStore,TOKEN_PAGE
        import os
        previous=getattr(self,'_bangumi_authorization_window',None)
        if previous is not None:
            try:
                if previous.winfo_exists():previous.lift();return previous
            except tk.TclError:pass
        store=TokenStore(os.path.join(slg_db.app_dir(),'private-credentials'))
        window=self._new_dialog('Bangumi本机授权','560x300')
        self._bangumi_authorization_window=window
        ResourceLabel(window,text='在Bangumi官网生成访问令牌；只加密保存在本机，不是软件账号登录。',wraplength=490).pack(padx=18,pady=18)
        token=ctk.CTkEntry(window,show='•');token.pack(fill='x',padx=18)
        note=ResourceLabel(window,text='不填写密码。授权不写入收藏、不上传或同步令牌。',wraplength=490)
        note.pack(padx=18,pady=8)
        inbox=queue.Queue(maxsize=1);closed=threading.Event(); state={'busy':False,'after':None}
        def save():
            if state['busy']:return
            value=token.get().strip();state['busy']=True; note.configure(text='正在验证授权…')
            def worker():
                try:
                    providers._CLIENT.validate_bangumi_token(value,closed.is_set)
                    if closed.is_set():return
                    store.save(value)
                    providers._CLIENT.set_bangumi_token(value)
                    providers._AUTH_LOADED=True
                    result='授权已加密保存于本机'
                except Exception:
                    result='授权验证或保存失败，原授权保留；请检查令牌与网络。'
                if not closed.is_set():inbox.put(result)
            threading.Thread(target=worker,daemon=True,name='bangumi-token-validate').start()
        def remove():
            if state['busy']:return
            try:
                store.remove();providers._CLIENT.set_bangumi_token('');providers._AUTH_LOADED=True;note.configure(text='本机授权已移除')
            except OSError:note.configure(text='移除失败，原文件保留')
        def poll():
            if closed.is_set():return
            try:note.configure(text=inbox.get_nowait());state['busy']=False
            except queue.Empty:pass
            state['after']=window.after(80,poll)
        def destroy(event):
            if event.widget is window:
                if getattr(self,'_bangumi_authorization_window',None) is window:self._bangumi_authorization_window=None
                closed.set()
                if state['after']:
                    try:window.after_cancel(state['after'])
                    except tk.TclError:pass
        actions=ctk.CTkFrame(window,fg_color='transparent');actions.pack(fill='x',padx=18,pady=12)
        for text,command in [('打开官网',lambda:webbrowser.open(TOKEN_PAGE)),('验证并保存',save),('移除授权',remove)]:
            ResourceButton(actions,text=text,command=command).pack(side='left',expand=True,fill='x',padx=3)
        window.bind('<Destroy>',destroy,add='+');poll()
        return window

    def open_steam_library_inspection(self):
        """Explicit library selection, background read, then user-confirmed private import."""
        from tkinter import filedialog
        import slg_db
        from slg_steam_local import inspect_library
        if not self._require_personal_access('识别Steam游戏库'):return
        folder=filedialog.askdirectory(parent=self,title='选择Steam游戏库（包含steamapps的目录）')
        if not folder:return
        identity=self._library_identity();stop=threading.Event();inbox=queue.Queue(maxsize=1)
        window=self._new_dialog('识别Steam本机游戏库','740x600')
        ResourceLabel(window,text='只读取所选游戏库的manifest；不会执行程序。选择确认后才登记本地游戏。',wraplength=650).pack(padx=16,pady=12)
        status=ResourceLabel(window,text='正在识别本机Steam manifest…');status.pack(pady=6)
        body=ResourceScrollableFrame(window);body.pack(fill='both',expand=True,padx=16,pady=8)
        state={'after':None}
        def alive():return not stop.is_set() and identity==self._library_identity()
        def choose(row):
            if not alive():return
            if not messagebox.askyesno('确认游戏与目录',row['title']+'\n'+row['folder_path']+'\n确认登记到本地游戏库？',parent=window):return
            if not alive():return
            try:
                existing=metadata.find_existing(self.conn,'steam',row['external_id'])
                if existing is not None:
                    status.configure(text='此Steam编号已登记，请在本地库管理现有目录绑定。');return
                gid=metadata.confirm(self.conn,row,folder_path=row['folder_path'])
                metadata.attach(self.conn,gid,row);self.conn.commit()
                status.configure(text='已登记：'+row['title'])
                changed=getattr(self,'_library_personal_changed',None)
                if changed:changed()
            except Exception:
                self.conn.rollback();status.configure(text='登记失败，原资料保留')
        def poll():
            if not alive():
                window.destroy();return
            try:result,error=inbox.get_nowait()
            except queue.Empty:state['after']=window.after(80,poll);return
            if error:status.configure(text='无法读取Steam游戏库：'+error);return
            rows=result['candidates'];status.configure(text=f'识别{len(rows)}项；失败{len(result["failures"])}项。逐项确认后登记。')
            pending=iter(rows[:5000])
            def append_batch():
                if not alive():return
                deadline=time.monotonic()+.006
                while time.monotonic()<deadline:
                    try:row=next(pending)
                    except StopIteration:return
                    ResourceButton(body,text=row['title']+' · AppID '+row['external_id'],command=lambda r=row:choose(r)).pack(fill='x',pady=4)
                state['after']=window.after(1,append_batch)
            append_batch()
            for failure in result['failures'][:20]:
                ResourceLabel(body,text=failure['reason'],wraplength=620).pack(fill='x',pady=2)
        def worker():
            try:result,error=inspect_library(folder,stop.is_set),None
            except Exception:result,error=None,'请选择有效的Steam游戏库目录'
            if not stop.is_set():inbox.put((result,error))
        def destroyed(event):
            if event.widget is window:
                stop.set()
                if state['after']:
                    try:window.after_cancel(state['after'])
                    except tk.TclError:pass
        window.bind('<Destroy>',destroyed,add='+')
        ResourceButton(window,text='关闭',command=window.destroy).pack(pady=10)
        threading.Thread(target=worker,daemon=True,name='steam-manifest-inspection').start();poll()
        return window
