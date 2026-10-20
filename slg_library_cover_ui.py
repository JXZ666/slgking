"""On-demand official cover query for an existing game, with explicit save."""
import threading
import tkinter as tk
import customtkinter as ctk

import slg_db
import slg_local_library as library
import slg_library_cover_candidates as covers
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame


class LibraryCoverUI:
    def open_library_cover_query(self, game):
        if not self._require_personal_access('查询个人封面'):
            return
        import slg_gui as gui
        previous = getattr(self, '_library_cover_dialog', None)
        if previous is not None:
            try:
                if previous['win'].winfo_exists():
                    previous['win'].destroy()
            except tk.TclError:
                pass
        game = dict(game)
        identity = self._library_identity()
        win = self._new_dialog('查询与选择个人封面', '680x660')
        color = lambda name: (gui._LIGHT[name], gui._DARK[name])
        win.configure(fg_color=color('BG'))
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(0, weight=1)
        body = ResourceScrollableFrame(win, fg_color=color('BG'))
        body.grid(row=0, column=0, sticky='nsew', padx=8, pady=(8, 0))
        footer = ctk.CTkFrame(win, fg_color=color('BG'))
        footer.grid(row=1, column=0, sticky='ew', padx=14, pady=10)
        footer.grid_columnconfigure((0, 1), weight=1, uniform='actions')
        stop = threading.Event()
        state = dict(busy=False, generation=0, prepared=None, image=None, candidates=[],
                     future=None, cancelled_cleanup=None)
        common = dict(fg_color=color('CHIP'), hover_color=color('CARD_HOVER'),
                      text_color=color('TEXT'), text_color_disabled=color('MUTED'))

        def dispose(value):
            if isinstance(value, covers.PreparedCover):
                value.close()
            elif isinstance(value, covers.SavedCover):
                value.discard()

        def alive():
            try:
                return not stop.is_set() and bool(win.winfo_exists()) and identity == self._library_identity()
            except tk.TclError:
                return False

        def release_preview():
            # Detach CTk callback before closing its PIL sources.
            preview.configure(image=None, text='尚未选择封面')
            state['image'] = None
            dispose(state['prepared'])
            state['prepared'] = None
            save.configure(state='disabled')

        def destroyed(event):
            if event.widget == win:
                stop.set()
                state['generation'] += 1
                future = state['future']
                if future is not None and future.cancel() and state['cancelled_cleanup']:
                    state['cancelled_cleanup']()
                state['future'], state['cancelled_cleanup'] = None, None
                dispose(state['prepared'])
                state['prepared'], state['image'] = None, None
                if getattr(self, '_library_cover_dialog', None) is dialog:
                    self._library_cover_dialog = None

        ResourceLabel(body, text='仅查询封面，不更改作品资料或绑定。只发送查询词或编号，不发送本机路径。',
                      wraplength=620, text_color=color('MUTED')).pack(padx=18, pady=10)
        ResourceLabel(body, text=game.get('title') or game.get('name') or '',
                      wraplength=620, text_color=color('TEXT')).pack(padx=18, pady=4)
        default_source, default_term = covers.default_query(self.conn, game)
        source = ctk.CTkOptionMenu(body, values=['DLsite', 'VNDB'], text_color=color('TEXT'),
                                   fg_color=color('CHIP'), button_color=color('CHIP'),
                                   button_hover_color=color('CARD_HOVER'))
        source.set('DLsite' if default_source == 'dlsite' else 'VNDB')
        source.pack(fill='x', padx=18, pady=4)
        query = ctk.CTkEntry(body, text_color=color('TEXT'), fg_color=color('CARD'))
        query.insert(0, default_term)
        query.pack(fill='x', padx=18, pady=4)
        status = ResourceLabel(body, text='点击查询后才联网；也可以继续使用本地选图。',
                               wraplength=620, text_color=color('MUTED'))
        status.pack(fill='x', padx=18, pady=5)
        results = ResourceScrollableFrame(body, fg_color=color('CARD'), height=140)
        results.pack(fill='x', padx=18, pady=5)
        description = ResourceLabel(body, text='请选择候选，核对来源和编号。',
                                    wraplength=620, text_color=color('TEXT'))
        description.pack(fill='x', padx=18, pady=5)
        preview_area = ctk.CTkFrame(body, width=260, height=256, fg_color=color('CARD'))
        preview_area.pack(fill='x', padx=18, pady=(4, 12))
        preview_area.pack_propagate(False)
        preview = ResourceLabel(preview_area, text='尚未选择封面', text_color=color('MUTED'))
        preview.pack(expand=True)

        # Width is physical at Configure time; wraplength accepts logical pixels.
        # Keep source/title facts inside the scroll body rather than widening it.
        wrap_labels = [child for child in body.winfo_children()
                       if isinstance(child, ResourceLabel)]
        wrap_state = {'width': None}
        def wrap_body(event):
            width = max(80, int(event.width / body._apply_widget_scaling(1.0) - 36))
            if wrap_state['width'] == width:
                return
            wrap_state['width'] = width
            for label in wrap_labels:
                label.configure(wraplength=width)
        body._parent_canvas.bind('<Configure>', wrap_body, add='+')

        def controls():
            enabled = 'disabled' if state['busy'] else 'normal'
            search_btn.configure(state=enabled)
            source.configure(state=enabled)
            query.configure(state=enabled)
            reset_btn.configure(state=enabled)
            save.configure(state='normal' if not state['busy'] and state['prepared'] else 'disabled')

        def work(task, complete, on_cancel=None):
            if state['busy'] or not alive():
                return False
            state['busy'] = True
            state['generation'] += 1
            token = state['generation']
            state['cancelled_cleanup'] = on_cancel
            controls()
            def finish(value, error):
                if not alive() or token != state['generation']:
                    dispose(value)
                    return
                state['future'], state['cancelled_cleanup'] = None, None
                state['busy'] = False
                try:
                    if error:
                        status.configure(text='处理失败，旧封面保留：' + str(error))
                    else:
                        complete(value)
                finally:
                    if alive():
                        controls()
            def worker():
                if stop.is_set():
                    if on_cancel is not None:
                        on_cancel()
                    return
                try:
                    value, error = task(), None
                except Exception as exc:
                    value, error = None, exc
                # Closed windows do not leave PIL/file ownership in the queue.
                if stop.is_set():
                    dispose(value)
                    return
                self.queue.put(('library_callback', (finish, value, error)))
            try:
                state['future'] = self._cover_executor.submit(worker)
            except Exception as exc:
                state['busy'] = False
                state['cancelled_cleanup'] = None
                status.configure(text='无法开始查询：' + str(exc))
                controls()
                return False
            return True

        def pick(candidate):
            if state['busy'] or not alive():
                return
            release_preview()
            status.configure(text='正在读取所选作品封面…')
            def show(prepared):
                state['prepared'] = prepared
                facts = prepared.candidate
                description.configure(text=f"{facts['title']}\n{facts['source']} · {facts['external_id']}\n"
                    f"开发者：{facts['developer'] or '未知'}\n{facts['source_url']}")
                photo = ctk.CTkImage(light_image=prepared.preview, dark_image=prepared.preview,
                                    size=prepared.preview.size)
                state['image'] = photo
                preview.configure(image=photo, text='')
                status.configure(text='请核对图片；点击保存后才替换个人封面。')
            work(lambda: covers.prepare(candidate, should_stop=stop.is_set), show)

        def search():
            if state['busy'] or not alive():
                return
            if not getattr(self, '_network_enabled', True):
                status.configure(text='当前为离线模式，请使用本地选图。')
                return
            provider = 'dlsite' if source.get() == 'DLsite' else 'vndb'
            term = query.get().strip()
            if not term:
                status.configure(text='请输入作品名称或来源编号。')
                return
            release_preview()
            for child in results.winfo_children():
                child.destroy()
            state['candidates'] = []
            description.configure(text='请选择候选，核对来源和编号。')
            status.configure(text='正在查询候选…')
            def show(candidates):
                state['candidates'] = candidates
                for candidate in candidates:
                    label = f"{candidate['title']} · {candidate['source']} · {candidate['external_id']}"
                    ResourceButton(results, text=label,
                        command=lambda c=candidate: pick(c), **common).pack(fill='x', padx=5, pady=3)
                status.configure(text='请选择候选，不会自动采用第一条。' if candidates
                                 else '没有找到封面资料，可修改查询词或使用本地选图。')
            work(lambda: covers.search(provider, term, should_stop=stop.is_set), show)

        def commit():
            if state['busy'] or not alive() or state['prepared'] is None:
                return
            if not self._require_personal_access('保存个人封面'):
                return
            # Worker owns this copy; closing the window may close the preview.
            image = state['prepared'].image.copy()
            directory = slg_db.covers_dir()
            status.configure(text='正在保存所选封面…')
            def task():
                try:
                    return covers.save_image(image, directory)
                finally:
                    image.close()
            def saved(value):
                try:
                    if not self._require_personal_access('保存个人封面'):
                        value.discard()
                        return
                    covers.apply(self.conn, game['id'], game['slug'], value)
                    self.conn.commit()
                    value.retained = True
                except Exception as exc:
                    self.conn.rollback()
                    value.discard()
                    status.configure(text='保存失败，旧封面保留：' + str(exc))
                    return
                self._library_refresh_game(game['id'])
                win.destroy()
            if not work(task, saved, on_cancel=image.close):
                image.close()

        def reset():
            if state['busy'] or not alive() or not self._require_personal_access('恢复默认封面'):
                return
            try:
                row = self.conn.execute('SELECT slug FROM games WHERE id=?', (game['id'],)).fetchone()
                if row is None or row[0] != game['slug']:
                    raise ValueError('原游戏已删除或替换。')
                library.clear_cover_override(self.conn, game['id'])
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                status.configure(text='恢复失败：' + str(exc))
                return
            self._library_refresh_game(game['id'])
            win.destroy()

        search_btn = ResourceButton(footer, text='查询封面候选', command=search, **common)
        search_btn.grid(row=0, column=0, sticky='ew', padx=(0, 5), pady=(0, 5))
        save = ResourceButton(footer, text='保存所选个人封面', command=commit, state='disabled',
                              fg_color=color('ACCENT'), hover_color=color('ACCENT_HOVER'),
                              text_color=color('ON_ACCENT'), text_color_disabled=color('ON_ACCENT'))
        save.grid(row=0, column=1, sticky='ew', padx=(5, 0), pady=(0, 5))
        reset_btn = ResourceButton(footer, text='恢复默认封面', command=reset, **common)
        reset_btn.grid(row=1, column=0, sticky='ew', padx=(0, 5))
        cancel_btn = ResourceButton(footer, text='取消', command=win.destroy, **common)
        cancel_btn.grid(row=1, column=1, sticky='ew', padx=(5, 0))
        dialog = dict(win=win, state=state, search=search, query=query, source=source,
                      pick=pick, save=commit, reset=reset, status=status, preview=preview,
                      body=body, footer=footer, preview_area=preview_area, results=results, description=description,
                      buttons=(search_btn, save, reset_btn, cancel_btn))
        self._library_cover_dialog = dialog
        win.bind('<Destroy>', destroyed, add='+')
        return dialog
