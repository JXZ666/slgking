"""Non-modal, searchable help window with local navigation callbacks only."""
from __future__ import annotations
import tkinter as tk
import customtkinter as ctk
from slg_image_resources import ResourceToplevel, ResourceLabel, ResourceButton, ResourceScrollableFrame
from slg_help_content import HELP_SECTIONS, SECTION_BY_KEY, search_sections

_DEFAULT = dict(BG='#19121b', CARD='#241a27', CARD_HOVER='#352635', TEXT='#f5eff5',
                MUTED='#c3b3c3', ACCENT='#ef6a9b', ACCENT_HOVER='#fb83af',
                ON_ACCENT='#24101a', CHIP='#3a2b3c', DANGER_TEXT='#ff9bb2')

class HelpCenter(ResourceToplevel):
    def __init__(self, parent, *, palette=None, font=None, callbacks=None, metadata=None):
        self.palette = self._palette(palette)
        self.font_factory = font or (lambda size, weight='normal': ('Microsoft YaHei UI', size, weight))
        self.callbacks = dict(callbacks or {})
        self.metadata = dict(metadata or {})
        self.current_key = 'start'
        self.positions = {}
        self._showing_empty = False
        self._jobs = {}
        self._closed = False
        self._generation = 0
        self._owned_global_bindings = []
        super().__init__(parent, fg_color=self.palette['BG'])
        self.title('帮助中心 · 黄油之王')
        self.geometry('920x680')
        self.minsize(620, 420)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda event: self.close(), add='+')
        self._build('')
        self.open_section('start', remember=False)

    def bind_all(self, sequence=None, func=None, add=None):
        fid = super().bind_all(sequence, func, add)
        if func is not None and fid:
            self._owned_global_bindings.append((sequence, fid))
        return fid

    @staticmethod
    def _palette(value):
        result = dict(_DEFAULT)
        for key, color in (value or {}).items():
            if key.upper() in result:
                result[key.upper()] = color
        return result

    def _font(self, size, weight='normal'):
        return self.font_factory(size=size, weight=weight)

    def _schedule(self, name, delay, callback):
        self._cancel(name)
        generation = self._generation
        def run():
            self._jobs.pop(name, None)
            if not self._closed and generation == self._generation:
                callback()
        self._jobs[name] = self.after(delay, run)

    def _cancel(self, name):
        job = self._jobs.pop(name, None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass

    def _build(self, query):
        p = self.palette
        self.shell = ctk.CTkFrame(self, fg_color='transparent')
        self.shell.pack(fill='both', expand=True, padx=18, pady=16)
        ResourceLabel(self.shell, text='帮助中心', text_color=p['TEXT'],
                      font=self._font(22, 'bold')).pack(anchor='w')
        ResourceLabel(self.shell, text='游戏检索 · 本地管理 · 社群功能', text_color=p['MUTED'],
                      font=self._font(12)).pack(anchor='w', pady=(2, 12))
        self.query_var = tk.StringVar(self, value=query)
        self.search_entry = ctk.CTkEntry(self.shell, textvariable=self.query_var,
                placeholder_text='搜索问题、操作或功能…', height=40,
                fg_color=p['CARD'], text_color=p['TEXT'], placeholder_text_color=p['MUTED'],
                border_color=p['CHIP'], font=self._font(14))
        self.search_entry.pack(fill='x', pady=(0, 12))
        self._trace = self.query_var.trace_add('write', self._query_changed)
        self.workspace = ctk.CTkFrame(self.shell, fg_color='transparent')
        self.workspace.pack(fill='both', expand=True)
        self.workspace.grid_rowconfigure(0, weight=1)
        self.workspace.grid_columnconfigure(1, weight=1)
        self.nav = ResourceScrollableFrame(self.workspace, width=175, fg_color=p['CARD'], corner_radius=12)
        self.nav.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        self.content = ResourceScrollableFrame(self.workspace, fg_color=p['CARD'], corner_radius=12)
        self.content.grid(row=0, column=1, sticky='nsew')
        self.content.bind('<Configure>', self._resize_text, add='+')
        self.status_label = ResourceLabel(self.shell, text='', text_color=p['MUTED'], font=self._font(11))
        self.status_label.pack(anchor='w', pady=(8, 0))
        self._nav_buttons = {}
        self._render_nav(query)

    def _query_changed(self, *_):
        self._schedule('search', 150, self._apply_search)

    def _apply_search(self):
        query = self.query_var.get()
        results = self._render_nav(query)
        if not results:
            self._remember_position()
            self._clear_content()
            self._showing_empty = True
            self._label('没有找到相关说明', size=18, bold=True)
            self._label('请尝试更短的关键词，例如“扫描”“头像”“日志”。', tone='muted')
        elif self.current_key not in {s.key for s in results}:
            self.open_section(results[0].key)
        else:
            self.open_section(self.current_key)

    def _render_nav(self, query):
        for widget in self.nav.winfo_children():
            widget.destroy()
        self._nav_buttons.clear()
        results = search_sections(query)
        for section in results:
            active = section.key == self.current_key
            button = ResourceButton(self.nav, text=section.title, anchor='w', height=38,
                font=self._font(13), fg_color=self.palette['ACCENT'] if active else 'transparent',
                hover_color=self.palette['ACCENT_HOVER'] if active else self.palette['CARD_HOVER'],
                text_color=self.palette['ON_ACCENT'] if active else self.palette['TEXT'],
                text_color_disabled=self.palette['MUTED'],
                command=lambda key=section.key: self.open_section(key))
            button.pack(fill='x', padx=5, pady=3)
            self._nav_buttons[section.key] = button
        self.status_label.configure(text=('共%d个章节' % len(results)) if not query.strip()
                                    else ('找到%d个相关章节' % len(results))) if hasattr(self, 'status_label') else None
        return results

    def _remember_position(self):
        if hasattr(self, 'content') and self.current_key and not self._showing_empty:
            self.positions[self.current_key] = self.content._parent_canvas.yview()[0]

    def _clear_content(self):
        self._cancel('position')
        for widget in self.content.winfo_children():
            widget.destroy()
        self.content._parent_canvas.yview_moveto(0)

    def open_section(self, key, *, remember=True):
        if self._closed or key not in SECTION_BY_KEY:
            return False
        if remember:
            self._remember_position()
        self.current_key = key
        self._showing_empty = False
        self._clear_content()
        section = SECTION_BY_KEY[key]
        self._label(section.title, size=21, bold=True, pady=(18, 12))
        for block in section.blocks:
            if block.kind == 'action':
                self._action_button(block.text, block.detail)
            elif block.kind == 'link':
                self._action_button(block.text, 'open_url', block.detail)
            elif block.kind == 'qa':
                self._label(block.text, tone='accent', bold=True, pady=(14, 4))
                self._label(block.detail)
            else:
                heading = block.kind in ('heading', 'subheading')
                text = block.text.replace('{version}', str(self.metadata.get('version', '见关于本软件')))
                self._label(text, size=15 if heading else 13, bold=heading,
                            tone='accent' if heading else block.tone,
                            pady=(16, 6) if heading else (0, 9))
        for name, button in self._nav_buttons.items():
            active = name == key
            button.configure(fg_color=self.palette['ACCENT'] if active else 'transparent',
                hover_color=self.palette['ACCENT_HOVER'] if active else self.palette['CARD_HOVER'],
                text_color=self.palette['ON_ACCENT'] if active else self.palette['TEXT'])
        self._resize_text()
        position = self.positions.get(key, 0)
        self._schedule('position', 60, lambda: self.content._parent_canvas.yview_moveto(position))
        return True

    def _label(self, text, *, size=13, bold=False, tone='normal', pady=(0, 9)):
        color = self.palette.get({'muted':'MUTED','danger':'DANGER_TEXT','accent':'ACCENT'}.get(tone, 'TEXT'))
        label = ResourceLabel(self.content, text=text, text_color=color, anchor='w', justify='left',
                         wraplength=500, font=self._font(size, 'bold' if bold else 'normal'))
        label.pack(fill='x', padx=16, pady=pady)
        return label

    def _action_button(self, label, action, argument=None):
        callback = self.callbacks.get(action)
        if callback is None:
            return
        def invoke():
            if not self._closed:
                callback(argument) if argument is not None else callback()
        button = ResourceButton(self.content, text=label, height=38, anchor='w',
            fg_color=self.palette['CHIP'], hover_color=self.palette['CARD_HOVER'],
            text_color=self.palette['TEXT'], text_color_disabled=self.palette['MUTED'],
            font=self._font(13), command=invoke)
        button.pack(fill='x', padx=16, pady=(5, 8))

    def _resize_text(self, event=None):
        if self._closed:
            return
        scale = ctk.ScalingTracker.get_widget_scaling(self.content)
        width = max(100, int(self.content.winfo_width() / scale) - 40)
        for child in self.content.winfo_children():
            if isinstance(child, ctk.CTkLabel):
                child.configure(wraplength=width)

    def refresh_theme(self, palette, font=None):
        if self._closed:
            return
        self._remember_position()
        query = self.query_var.get()
        self._generation += 1
        for name in tuple(self._jobs):
            self._cancel(name)
        self.query_var.trace_remove('write', self._trace)
        self.shell.destroy()
        self.palette = self._palette(palette)
        if font is not None:
            self.font_factory = font
        self.configure(fg_color=self.palette['BG'])
        self._build(query)
        if search_sections(query):
            self.open_section(self.current_key, remember=False)
        else:
            self._apply_search()

    def close(self):
        self.destroy()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        for name in tuple(self._jobs):
            self._cancel(name)
        if hasattr(self, 'query_var'):
            self.query_var.trace_remove('write', self._trace)
        self.callbacks.clear()
        root = self._root()
        for sequence, fid in self._owned_global_bindings:
            try:
                root._unbind(('bind', 'all', sequence), fid)
                script = root.tk.call('bind', 'all', sequence)
                lines = [line for line in str(script).splitlines() if line.strip()]
                root.tk.call('bind', 'all', sequence, '\n'.join(lines) + ('\n' if lines else ''))
            except tk.TclError:
                pass
        self._owned_global_bindings.clear()
        super().destroy()
