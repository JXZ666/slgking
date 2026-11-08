"""Responsive scan-target rows; resizing keeps the original input widgets."""
import tkinter as tk

import customtkinter as ctk

from slg_image_resources import ResourceLabel


def option_columns(available_width, minimum_width=140, count=4):
    """Choose four or two columns from logical pixels, never physical DPI."""
    required = 4 * max(1, minimum_width) + 3 * 8
    return 4 if count > 2 and available_width >= required else 2


class ScanTargetRow(ctk.CTkFrame):
    """Path, responsive settings and independent actions for one scan target.

    Construct OptionMenus under ``controls``, then call ``set_controls`` once.
    Put the remove action under ``actions``. Configure only repositions the
    existing controls: it never recreates them or calls business callbacks.
    """
    def __init__(self, master, *, path, text_color, font=None, **kwargs):
        super().__init__(master, **kwargs)
        self._layout_job = None
        self._controls = []
        self._columns = None
        self._minimum_width = 140
        self._path_wrap = None
        label_options = dict(text=path, text_color=text_color, anchor='w',
                             justify='left', wraplength=560)
        if font is not None:
            label_options['font'] = font
        self.path_label = ResourceLabel(self, **label_options)
        self.path_label.pack(fill='x', padx=10, pady=(8, 3))
        self.controls = ctk.CTkFrame(self, fg_color='transparent')
        self.controls.pack(fill='x', padx=10, pady=3)
        self.actions = ctk.CTkFrame(self, fg_color='transparent')
        self.actions.pack(fill='x', padx=10, pady=(3, 8))
        # CTkFrame.bind targets its drawing Canvas. Observe the frame itself so
        # its final allocated width (and its own destruction) drives layout.
        tk.Frame.bind(self, '<Configure>', self._schedule_layout, add='+')
        tk.Frame.bind(self, '<Destroy>', self._destroy_layout, add='+')

    def set_controls(self, widgets):
        self._controls = list(widgets)
        self._columns = None
        sizes = [140]
        for widget in self._controls:
            font = widget.cget('font')
            if hasattr(font, 'cget'):
                sizes.append(abs(float(font.cget('size'))) * 9)
        self._minimum_width = max(sizes)
        self._schedule_layout()

    def _schedule_layout(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._layout_job is None:
            self._layout_job = self.after_idle(self._layout_controls)

    def _layout_controls(self):
        self._layout_job = None
        if not self.winfo_exists():
            return
        scale = self._apply_widget_scaling(1.0)
        width = max(1, self.winfo_width() / scale - 20)
        wrap = max(40, int(width))
        if wrap != self._path_wrap:
            self.path_label.configure(wraplength=wrap)
            self._path_wrap = wrap
        columns = option_columns(width, self._minimum_width, len(self._controls))
        if columns == self._columns:
            return
        self._columns = columns
        for index in range(4):
            self.controls.grid_columnconfigure(index, weight=int(index < columns),
                                               uniform='scan-options' if index < columns else '')
        for index, widget in enumerate(self._controls):
            column = index % columns
            widget.grid(row=index // columns, column=column, sticky='ew',
                        padx=(0 if column == 0 else 4, 0 if column == columns - 1 else 4),
                        pady=4)

    def _destroy_layout(self, event):
        if event.widget is self and self._layout_job is not None:
            try:
                self.after_cancel(self._layout_job)
            except tk.TclError:
                pass
            self._layout_job = None
