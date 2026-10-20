"""Project-owned OptionMenu with stable arrow space and full-value semantics."""
import tkinter.font
import customtkinter as ctk


class ResourceCollectionMenu(ctk.CTkOptionMenu):
    def __init__(self, master, **kwargs):
        self._layout_ready = False
        self._ellipsis_after = None
        kwargs["dynamic_resizing"] = False
        kwargs.setdefault("text_color", ("#1a1a1a", "#f5edf3"))
        kwargs.setdefault("text_color_disabled", ("#6b6b6b", "#9e929c"))
        kwargs.setdefault("dropdown_text_color", kwargs["text_color"])
        super().__init__(master, **kwargs)
        self._layout_ready = True
        self.bind("<Configure>", self._schedule_fit)
        self._create_grid()
        self._fit_text()

    def _create_grid(self):
        super()._create_grid()
        if getattr(self, "_layout_ready", False):
            self._fit_text()

    def _set_dimensions(self, width=None, height=None):
        super()._set_dimensions(width, height)
        if hasattr(self, "_text_label"):
            self._create_grid()
            self._schedule_fit()

    def _update_dimensions_event(self, event):
        super()._update_dimensions_event(event)
        if getattr(self, "_layout_ready", False):
            self._create_grid()
            self._schedule_fit()

    def _update_font(self):
        super()._update_font()
        if getattr(self, "_layout_ready", False):
            self._create_grid()
            self._schedule_fit()

    def _set_scaling(self, *args, **kwargs):
        super()._set_scaling(*args, **kwargs)
        if getattr(self, "_layout_ready", False):
            self._fit_text()
            self._schedule_fit()

    def _schedule_fit(self, event=None):
        if not self._layout_ready or self._ellipsis_after is not None:
            return
        self._ellipsis_after = self.after_idle(self._finish_fit)

    def _finish_fit(self):
        self._ellipsis_after = None
        if self.winfo_exists():
            self._fit_text()

    def _fit_text(self):
        value = str(self._current_value)
        left = max(self._corner_radius, 3)
        available = max(0, self._apply_widget_scaling(
            self._current_width - self._current_height - 3 - left) - 4)
        font = tkinter.font.Font(master=self, font=self._text_label.cget("font"))
        if font.measure(value) > available:
            lo, hi = 0, len(value)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(value[:mid] + "…") <= available:
                    lo = mid
                else:
                    hi = mid - 1
            value = value[:lo] + "…" if font.measure("…") <= available else ""
        self._text_label.configure(text=value)

    def configure(self, require_redraw=False, **kwargs):
        if "dynamic_resizing" in kwargs:
            kwargs["dynamic_resizing"] = False
        super().configure(require_redraw=require_redraw, **kwargs)
        if getattr(self, "_layout_ready", False):
            self._create_grid()
            self._schedule_fit()

    def set(self, value):
        super().set(value)
        self._fit_text()

    def _variable_callback(self, *args):
        super()._variable_callback(*args)
        if getattr(self, "_layout_ready", False):
            self._fit_text()

    def _dropdown_callback(self, value):
        super()._dropdown_callback(value)
        if self.winfo_exists():
            self._fit_text()

    def destroy(self):
        self._layout_ready = False
        if self._ellipsis_after is not None:
            self.after_cancel(self._ellipsis_after)
            self._ellipsis_after = None
        super().destroy()
