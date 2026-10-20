"""Non-modal catalogue binding picker; SQLite and translation work stay off Tk."""
from collections import deque
import queue
import time
import tkinter as tk
import customtkinter as ctk
from slg_image_resources import ResourceButton, ResourceLabel, ResourceScrollableFrame
from slg_game_picker_query import LatestLookup, PickerQuery


def _identity(owner):
    capture = getattr(owner, '_capture_identity_context', None) or getattr(owner, '_library_identity', None)
    return capture() if capture else None


def _theme(owner):
    return (getattr(owner, 'theme_mode', None), getattr(owner, '_sys_theme', None))


class GamePicker:
    def __init__(self, owner, on_select, candidates=(), initial_search=''):
        import slg_gui as g
        self.owner, self.on_select = owner, on_select
        self.identity, self.theme = _identity(owner), _theme(owner)
        self.closed = False
        self.handles = set()
        self.generation = 0
        self.sequence = None
        self.poll_handle = None
        self.page = 1
        self.rendering = False
        self.last_text = str(initial_search or '')
        self.candidates = PickerQuery.candidate_ids(candidates)
        self.widgets = []
        self.batch = None
        self.debounce = None
        self.worker = None
        colors = {k: (g._LIGHT[k], g._DARK[k]) for k in
                  ('BG','CARD','TEXT','MUTED','CHIP','ACCENT','ACCENT_HOVER','ON_ACCENT','DANGER_TEXT')}
        self.colors = colors
        win = self.win = owner._new_dialog('匹配已收录游戏', '780x650')
        win._game_picker = self
        registry = getattr(owner, '_game_pickers', None)
        if registry is None:
            registry = owner._game_pickers = set()
        registry.add(self)
        win._slg_before_destroy = self.invalidate
        win.configure(fg_color=colors['BG'])
        ResourceLabel(win, text='在本机已收录目录中查找作品', text_color=colors['TEXT'],
                      font=g.ui_font(size=17, weight='bold')).pack(fill='x', padx=20, pady=(18, 6))
        ResourceLabel(win, text='仅选择绑定对象；确认导入后才保存。未收录的作品可返回联网补充资料或手动添加。',
                      text_color=colors['MUTED'], font=g.ui_font(size=12), wraplength=700,
                      justify='left').pack(fill='x', padx=20, pady=(0, 10))
        self.entry = ctk.CTkEntry(win, height=42, placeholder_text='名称、别名、译名或作品编号…',
                                 fg_color=colors['CARD'], text_color=colors['TEXT'],
                                 border_color=colors['CHIP'], font=g.ui_font(size=14))
        self.entry.pack(fill='x', padx=20)
        self.entry.insert(0, initial_search or '')
        self.entry.bind('<KeyRelease>', self.changed)
        self.entry.bind('<Return>', lambda _e: self.request(1))
        self.entry.bind('<KP_Enter>', lambda _e: self.request(1))
        self.status = ResourceLabel(win, text='正在读取本机目录…', text_color=colors['MUTED'],
                                    font=g.ui_font(size=12), anchor='w')
        self.status.pack(fill='x', padx=20, pady=8)
        footer = ctk.CTkFrame(win, fg_color='transparent')
        footer.pack(side='bottom', fill='x', padx=20, pady=16)
        self.previous = ResourceButton(footer, text='上一页', width=95, height=40,
                                      command=lambda: self.request(self.page-1), text_color=colors['TEXT'],
                                      text_color_disabled=colors['MUTED'], fg_color=colors['CHIP'])
        self.previous.pack(side='left')
        self.page_label = ResourceLabel(footer, text='第1页', text_color=colors['TEXT'], font=g.ui_font(size=13))
        self.page_label.pack(side='left', padx=14)
        self.next = ResourceButton(footer, text='下一页', width=95, height=40,
                                  command=lambda: self.request(self.page+1), text_color=colors['TEXT'],
                                  text_color_disabled=colors['MUTED'], fg_color=colors['CHIP'])
        self.next.pack(side='left')
        ResourceButton(footer, text='取消', width=95, height=40, command=self.close,
                       text_color=colors['TEXT'], fg_color=colors['CHIP']).pack(side='right')
        if self.candidates:
            ResourceButton(footer, text='优先看扫描候选', width=140, height=40, command=self.show_candidates,
                           text_color=colors['TEXT'], fg_color=colors['CHIP']).pack(side='right', padx=10)
        self.body = ResourceScrollableFrame(win, fg_color='transparent')
        self.body.pack(fill='both', expand=True, padx=18, pady=0)
        self.previous.configure(state='disabled')
        self.next.configure(state='disabled')
        try:
            path = next(row[2] for row in owner.conn.execute('PRAGMA database_list') if row[1] == 'main')
            if not path:
                raise ValueError('当前资料库尚未保存到磁盘，无法建立只读查询。')
            self.worker = LatestLookup(path)
            self.request(1)
        except Exception as exc:
            self.status.configure(text='无法读取本机目录：' + str(exc), text_color=colors['DANGER_TEXT'])
        self.later(20, self.entry.focus_set)

    def valid(self):
        if self.closed:
            return False
        if (_identity(self.owner) != self.identity or _theme(self.owner) != self.theme
                or getattr(self.owner, '_theme_rebuilding', False)):
            self.close()
            return False
        return bool(self.win.winfo_exists())

    def later(self, delay, callback):
        if self.closed:
            return None
        box = []
        def run():
            self.handles.discard(box[0])
            if self.valid():
                callback()
        handle = self.win.after(delay, run)
        box.append(handle)
        self.handles.add(handle)
        return handle

    def cancel(self, handle):
        if handle in self.handles:
            self.handles.discard(handle)
            self.win.after_cancel(handle)

    def invalidate(self):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        if self.worker:
            self.worker.close()
        for handle in tuple(self.handles):
            try:
                self.win.after_cancel(handle)
            except tk.TclError:
                pass  # Already executing/deleted callback, not a business error.
        self.handles.clear()
        self.widgets.clear()
        getattr(self.owner, '_game_pickers', set()).discard(self)

    def close(self):
        self.invalidate()
        if self.win.winfo_exists():
            self.win.destroy()

    def show_candidates(self):
        self.entry.delete(0, 'end')
        self.request(1)

    def changed(self, event=None):
        if event is not None and event.keysym in ('Return', 'KP_Enter'):
            return
        if self.entry.get() == self.last_text:
            return
        self.last_text = self.entry.get()
        self.cancel(self.debounce)
        self.generation += 1
        if self.worker:
            self.worker.cancel()
        # Invalidate the displayed response before the debounce expires.
        self.sequence = None
        self.rendering = True
        for widget in self.widgets:
            if widget.cget('state') != 'disabled':
                widget.configure(state='disabled')
        self.status.configure(text='输入已更新，正在准备查找…')
        self.previous.configure(state='disabled')
        self.next.configure(state='disabled')
        self.debounce = self.later(150, lambda: self.request(1))

    def request(self, page=1):
        if not self.valid() or self.worker is None:
            return 'break'
        self.cancel(self.debounce)
        self.generation += 1
        self.page = max(1, page)
        self.last_text = self.entry.get()
        self.previous.configure(state='disabled')
        self.next.configure(state='disabled')
        self.rendering = True
        for widget in self.widgets:
            if widget.cget('state') != 'disabled':
                widget.configure(state='disabled')
        self.status.configure(text='正在查找本机目录…', text_color=self.colors['MUTED'])
        self.sequence = self.worker.submit(text=self.entry.get(), page=self.page, candidates=self.candidates)
        self.ensure_poll()
        return 'break'

    def choose(self, game_id, generation):
        if not self.valid() or generation != self.generation:
            return
        self.generation += 1
        self.status.configure(text='正在确认作品仍可绑定…')
        self.sequence = self.worker.submit('selected', game_id=game_id)
        self.ensure_poll()

    def ensure_poll(self):
        if self.poll_handle not in self.handles:
            self.poll_handle = self.later(20, self.poll)

    def poll(self):
        self.poll_handle = None
        if self.sequence is None:
            return
        if self.worker:
            try:
                seq, kind, value, error = self.worker.results.get_nowait()
            except queue.Empty:
                pass
            else:
                if seq == self.sequence:
                    self.sequence = None
                    if error:
                        self.rendering = False
                        self.status.configure(text='查询失败，可修改搜索后重试：' + error,
                                              text_color=self.colors['DANGER_TEXT'])
                    elif kind == 'selected':
                        if value:
                            # The fresh read, identity guard, and callback all precede any owner's write.
                            self.close()
                            self.on_select(value)
                            return
                        self.rendering = False
                        self.status.configure(text='作品已删除、隐藏或合并，请重新搜索。')
                    else:
                        self.render(value)
        if self.sequence is not None:
            self.ensure_poll()

    def render(self, result):
        import slg_gui as g
        token = self.generation
        self.cancel(self.batch)
        rows = result['rows']
        work = deque(('update', (self.widgets[i], row)) if i < len(self.widgets) else ('create', row)
                     for i, row in enumerate(rows))
        work.extend(('destroy', widget) for widget in self.widgets[len(rows):])
        self.previous.configure(state='normal' if result['has_previous'] else 'disabled')
        self.next.configure(state='normal' if result['has_next'] else 'disabled')
        self.page_label.configure(text='第%d页' % result['page'])
        text = '第%d页 · %d项 · 点击作品选择' % (result['page'], len(result['rows']))
        if not self.entry.get().strip() and self.candidates:
            text += ' · 扫描候选优先'
        if not result['rows']:
            text = '未找到已收录作品；可调整名称或返回联网补充资料、手动添加。'
        self.status.configure(text=text, text_color=self.colors['MUTED'])
        def batch():
            if token != self.generation:
                return
            deadline = time.monotonic() + .006
            while work and time.monotonic() < deadline:
                action, item = work.popleft()
                if action == 'destroy':
                    item.destroy()
                    if item in self.widgets:
                        self.widgets.remove(item)
                    continue
                existing = None
                if action == 'update':
                    existing, item = item
                label = '%s\n%s · %s · %s' % (item['title'], '自建' if item['origin'] == 'user' else '公共目录',
                        item.get('developer') or '未知作者', item['slug'])
                if existing is not None:
                    existing.configure(text=label, state='normal', command=lambda gid=item['id']: self.choose(gid, token))
                    continue
                widget = ResourceButton(self.body, text=label, anchor='w', height=56,
                        font=g.ui_font(size=13), fg_color=self.colors['CARD'], text_color=self.colors['TEXT'],
                        text_color_disabled=self.colors['MUTED'], hover_color=self.colors['CHIP'],
                        command=lambda gid=item['id']: self.choose(gid, token))
                widget.pack(fill='x', pady=3)
                self.widgets.append(widget)
            if work:
                self.batch = self.later(1, batch)
            else:
                self.rendering = False
        batch()


def open_game_picker(owner, on_select, candidates=(), initial_search=''):
    """Return disposable dialog; callback receives a fresh visible row, never writes."""
    return GamePicker(owner, on_select, candidates, initial_search).win
