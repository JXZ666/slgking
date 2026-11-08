"""Responsive settings categories, without rebuilding page state or actions."""
import math
import tkinter as tk


def settings_navigation_mode(logical_width, previous=None):
    """Choose the initial layout at 520px, then use a 12px dead band."""
    width = float(logical_width)
    if not math.isfinite(width) or width < 0:
        raise ValueError('settings width must be finite and nonnegative')
    if previous not in (None, 'compact', 'wide'):
        raise ValueError('unknown settings navigation mode')
    if previous == 'compact':
        return 'wide' if width >= 532 else 'compact'
    if previous == 'wide':
        return 'compact' if width < 508 else 'wide'
    return 'compact' if width < 520 else 'wide'


def settings_navigation_height(logical_font_size):
    """Reserve enough logical height for enlarged fonts and a 44px target."""
    size = abs(float(logical_font_size))
    if not math.isfinite(size):
        raise ValueError('font size must be finite')
    return max(44, math.ceil(size * 1.5) + 16)


class SettingsNavigationLayout:
    """Reposition existing category buttons while keeping pages and commands.

    ``font_factory`` uses the application's ``ui_font(size=...)`` contract.
    Call ``request_layout`` after changing the shared font-size setting; native
    Configure events already cover window and DPI changes. ``close`` is safe
    both explicitly and when the owning body is destroyed.
    """
    def __init__(self, body, nav, content, buttons, page_names, font_factory):
        self.body, self.nav, self.content = body, nav, content
        self.buttons = dict(buttons)
        self.page_names = tuple(page_names)
        if any(key not in self.buttons for key, *_ in self.page_names):
            raise ValueError('each settings category requires an existing button')
        self.font_factory = font_factory
        self.mode = None
        self._signature = None
        self._layout_job = None
        self._closed = False
        self._bindings = []
        # CTkFrame.bind delegates to its drawing Canvas. Listen to the owning
        # frame, whose allocated width and destruction govern this layout.
        self._bindings.append(('<Configure>', tk.Misc.bind(
            body, '<Configure>', self._configured, add='+')))
        self._bindings.append(('<Destroy>', tk.Misc.bind(
            body, '<Destroy>', self._destroyed, add='+')))
        self.request_layout()

    def _configured(self, event):
        if event.widget is self.body:
            self.request_layout()

    def request_layout(self):
        if self._closed or self._layout_job is not None:
            return
        self._layout_job = self.body.after_idle(self._apply_layout)

    def _apply_layout(self):
        self._layout_job = None
        if self._closed:
            return
        width = self.body.winfo_width()
        if width <= 1:
            return  # A mapped Configure will supply the real viewport.
        scale = float(self.body._apply_widget_scaling(1.0))
        mode = settings_navigation_mode(width / scale, self.mode)
        font = self.font_factory(size=14 if mode == 'compact' else 15)
        font_size = abs(float(font.cget('size')))
        height = settings_navigation_height(font_size)
        signature = (mode, id(font), font_size, height)
        if signature == self._signature:
            return
        changed_mode = mode != self.mode
        if changed_mode:
            for button in self.buttons.values():
                button.pack_forget()
                button.grid_forget()
            self.nav.pack_forget()
            self.content.pack_forget()
        for key, full_label, *_ in self.page_names:
            self.buttons[key].configure(text=full_label, font=font, height=height,
                                        width=1 if mode == 'compact' else 156)
        if changed_mode and mode == 'compact':
            self.nav.pack_propagate(True)
            self.nav.grid_propagate(True)
            self.nav.configure(width=1)
            self.nav.pack(side='top', fill='x', pady=(0, 8))
            self.nav.grid_columnconfigure((0, 1), weight=1, uniform='settings-categories')
            for index, (key, *_labels) in enumerate(self.page_names):
                column = index % 2
                self.buttons[key].grid(row=index // 2, column=column, sticky='ew',
                    padx=(0 if column == 0 else 4, 4 if column == 0 else 0), pady=4)
            self.content.pack(side='top', fill='both', expand=True)
        elif changed_mode:
            self.nav.pack_propagate(False)
            self.nav.configure(width=156, height=1)
            self.nav.pack(side='left', fill='y', padx=(0, 12))
            for key, *_labels in self.page_names:
                self.buttons[key].pack(fill='x', pady=4)
            self.content.pack(side='left', fill='both', expand=True)
        self.mode = mode
        self._signature = signature

    def _destroyed(self, event):
        if event.widget is self.body:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._layout_job is not None:
            try:
                self.body.after_cancel(self._layout_job)
            except tk.TclError:
                pass
            self._layout_job = None
        for sequence, function_id in self._bindings:
            try:
                tk.Misc.unbind(self.body, sequence, function_id)
            except tk.TclError:
                pass
        self._bindings.clear()
