"""Bounded right-panel entry feedback; no persistence or network access.

The App supplies its existing MicroInteractions coordinator, palette/font and
local preference callbacks.  Call deactivate on navigation/identity changes,
and close on root or theme teardown.  Never call activate for background work.
"""
import math
import tkinter as tk

import customtkinter as ctk
from slg_image_resources import ResourceLabel


PANEL_KEYS = frozenset(("profile", "settings", "leaderboard", "shop", "tools"))
HINT_TEXT = "已在右侧打开"
HINT_MS = 2500


def _color(value):
    if isinstance(value, (tuple, list)):
        return value[1 if ctk.get_appearance_mode().lower() == "dark" else 0]
    return value


def _blend(base, accent, amount):
    try:
        a, b = base.lstrip("#"), accent.lstrip("#")
        if len(a) == 3:
            a = "".join(c * 2 for c in a)
        if len(b) == 3:
            b = "".join(c * 2 for c in b)
        return "#" + "".join("%02x" % round(int(a[i:i + 2], 16) * (1 - amount)
                                               + int(b[i:i + 2], 16) * amount)
                              for i in (0, 2, 4))
    except (ValueError, TypeError, AttributeError):
        return accent


class FeedbackHeader(ctk.CTkFrame):
    """A stable title area; callers manage this Frame with pack or grid.

    title_row contains title_label and an optional trailing Frame for existing
    controls/balance.  The hint always reserves a single line, including after
    dismissal, so showing it never moves the panel content.
    """
    def __init__(self, parent, key, title, *, palette, font_factory,
                 title_size=18, title_anchor="center", on_map=None,
                 on_unmap=None, on_destroy=None):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.key = key
        self._palette = palette
        self._font_factory = font_factory
        self._title_size = title_size
        self._line_progress = 1.0
        self._glow_progress = 0.0
        self._events = []
        self.title_row = ctk.CTkFrame(self, width=1, height=1,
                                    fg_color="transparent", corner_radius=0)
        self.title_row.pack(fill="x")
        self.title_row.grid_columnconfigure(0, weight=1)
        colors = palette()
        self.title_label = ResourceLabel(self.title_row, text=title, width=1,
            anchor=title_anchor, text_color=colors["TEXT"],
            font=font_factory(size=title_size, weight="bold"))
        self.title_label.grid(row=0, column=0, sticky="ew")
        # Empty CTkFrames default to 200px high and wide. An optional trailing
        # holder must request only its real children, rather than a blank block.
        self.trailing = ctk.CTkFrame(self.title_row, width=1, height=1,
                                   fg_color="transparent", corner_radius=0)
        self.trailing.grid(row=0, column=1, sticky="e")
        self.hint_label = ResourceLabel(self, text="", width=1, height=20,
            anchor="center", text_color=colors["MUTED"], font=font_factory(size=11))
        self.hint_label.pack(fill="x", pady=(2, 0))
        self.line_canvas = tk.Canvas(self, height=max(4, round(6 * self._get_widget_scaling())),
                                     highlightthickness=0, borderwidth=0)
        self.line_canvas.pack(fill="x", pady=(0, 2))
        self._band = self.line_canvas.create_rectangle(0, 0, 1, 1, outline="")
        self._line = self.line_canvas.create_rectangle(0, 0, 1, 1, outline="")
        self.line_canvas.bind("<Configure>", lambda _event: self._paint(), add="+")
        # CTkFrame.bind delegates to its canvas and does not return a function
        # handle. Bind the actual Frame's public Tk surface for lifecycle events.
        for event, callback in (("<Map>", on_map), ("<Unmap>", on_unmap),
                                ("<Destroy>", on_destroy)):
            if callback is not None:
                handle = tk.Misc.bind(self, event,
                    lambda e, cb=callback: cb(self) if e.widget is self else None, add="+")
                self._events.append((event, handle))
        self.refresh()

    def _surface(self, colors):
        current = self.master
        while current is not None:
            for option in ("fg_color", "background"):
                try:
                    value = _color(current.cget(option))
                except (tk.TclError, ValueError):
                    continue
                if value and value != "transparent":
                    return value
            current = getattr(current, "master", None)
        return _color(colors.get("CARD", colors["BG"]))

    def refresh(self):
        colors = self._palette()
        self.title_label.configure(text_color=colors["TEXT"],
            font=self._font_factory(size=self._title_size, weight="bold"))
        self.hint_label.configure(text_color=colors["MUTED"],
                                  font=self._font_factory(size=11))
        self._paint()

    def _set_scaling(self, new_widget_scaling, new_window_scaling):
        super()._set_scaling(new_widget_scaling, new_window_scaling)
        if getattr(self, "line_canvas", None) is not None:
            self.line_canvas.configure(height=max(4, round(6 * new_widget_scaling)))
            self._paint()

    def _paint(self):
        canvas = getattr(self, "line_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        colors = self._palette()
        base, accent = self._surface(colors), _color(colors["ACCENT"])
        width, height = max(1, canvas.winfo_width()), max(4, canvas.winfo_height())
        canvas.configure(background=base)
        canvas.coords(self._band, 0, 0, width, height)
        canvas.itemconfigure(self._band, fill=_blend(base, accent, .16 * self._glow_progress))
        length = width * self._line_progress
        thickness = max(1, round(2 * self._get_widget_scaling()))
        canvas.coords(self._line, (width - length) / 2, height - thickness,
                      (width + length) / 2, height)
        canvas.itemconfigure(self._line, fill=_blend(base, accent, .75),
                             state="normal" if length > 0 else "hidden")

    def set_line(self, progress):
        self._line_progress = min(1.0, max(0.0, progress))
        self._paint()

    def set_glow(self, progress):
        self._glow_progress = math.sin(math.pi * min(1.0, max(0.0, progress))) ** 2
        self._paint()

    def reset(self):
        self._line_progress, self._glow_progress = 1.0, 0.0
        self.show_hint("")
        self._paint()

    def show_hint(self, text):
        self.hint_label.configure(text=text)

    def hint_is_viewable(self):
        label = self.hint_label
        if not label.cget("text") or not label.winfo_viewable():
            return False
        # A canvas keeps its scrolled-out children mapped. Intersect every
        # ancestor viewport so that offscreen text cannot consume the hint.
        x, y = label.winfo_rootx(), label.winfo_rooty()
        right, bottom = x + label.winfo_width(), y + label.winfo_height()
        ancestor = label.master
        while ancestor is not None:
            left, top = ancestor.winfo_rootx(), ancestor.winfo_rooty()
            x, y = max(x, left), max(y, top)
            right = min(right, left + ancestor.winfo_width())
            bottom = min(bottom, top + ancestor.winfo_height())
            if right <= x or bottom <= y:
                return False
            ancestor = getattr(ancestor, "master", None)
        return True


class PanelFeedback:
    def __init__(self, owner, motion, *, seen, mark_seen, palette, font_factory):
        self.owner, self.motion = owner, motion
        self.seen, self.mark_seen = seen, mark_seen
        self.palette, self.font_factory = palette, font_factory
        self.generation = 0
        self.active_key = None
        self._header = None
        self._started = False
        self._closed = False
        self._jobs = set()
        self._start_job = None
        self._line_key = ("panel-feedback-line", id(self))
        self._glow_key = ("panel-feedback-glow", id(self))
        self._bindings = []
        for event, callback in (("<Unmap>", self._owner_unmap),
                                ("<FocusOut>", self._owner_unfocused),
                                ("<Destroy>", self._owner_destroy)):
            handle = owner.bind(event, callback, add="+")
            self._bindings.append((event, handle))

    def attach(self, key, parent, title, *, title_size=18, title_anchor="center"):
        if self._closed:
            raise RuntimeError("panel feedback is closed")
        if key not in PANEL_KEYS:
            raise ValueError("unknown right-panel key")
        return FeedbackHeader(parent, key, title, palette=self.palette,
            font_factory=self.font_factory, title_size=title_size, title_anchor=title_anchor,
            on_map=self._header_map, on_unmap=self._header_unmap,
            on_destroy=self._header_destroy)

    def _live(self, header):
        try:
            return bool(header is not None and header.winfo_exists())
        except tk.TclError:
            return False

    def _visible(self, header):
        try:
            return self._live(header) and bool(self.owner.winfo_viewable() and header.winfo_viewable())
        except tk.TclError:
            return False

    def _current(self, generation, header):
        return (not self._closed and generation == self.generation
                and self._header is header and self.active_key == header.key
                and self._live(header))

    def _later(self, delay, callback):
        handle = None
        def run():
            self._jobs.discard(handle)
            callback()
        try:
            handle = (self.owner.after_idle(run) if delay is None
                      else self.owner.after(delay, run))
        except tk.TclError:
            return None
        self._jobs.add(handle)
        return handle

    def activate(self, key, header, user_initiated=True):
        if self._closed or key not in PANEL_KEYS or header.key != key or not self._live(header):
            return False
        if not user_initiated and self.active_key == key and self._header is header:
            return True
        self.deactivate()
        self.active_key, self._header = key, header
        header.refresh()
        header.reset()
        if not user_initiated:
            self._started = True
            return True
        generation = self.generation
        if self._visible(header):
            self._start(generation, header)
        else:
            # At most one idle confirmation. If still hidden, only a real Map
            # event can resume this request; there is no visibility polling.
            self._queue_start(generation, header)
        return True

    def _queue_start(self, generation, header):
        if self._start_job is not None or self._started:
            return
        handle = None
        def confirm():
            if self._start_job == handle:
                self._start_job = None
            self._start(generation, header)
        handle = self._later(None, confirm)
        self._start_job = handle

    def _start(self, generation, header):
        if self._started or not self._current(generation, header) or not self._visible(header):
            return
        self._started = True
        def line(progress):
            if self._current(generation, header):
                header.set_line(progress)
        def glow(progress):
            if self._current(generation, header):
                header.set_glow(progress)
        self.motion.play(self._line_key, header, line, "panel_line")
        self.motion.play(self._glow_key, header, glow, "panel_glow")
        if not self.seen(header.key):
            header.show_hint(HINT_TEXT)
            self._later(None, lambda: self._confirm_hint(generation, header))

    def _confirm_hint(self, generation, header):
        if not self._current(generation, header) or not self._visible(header):
            return
        try:
            visible = header.hint_is_viewable()
        except tk.TclError:
            return
        if not visible:
            return
        if self.seen(header.key):
            header.show_hint("")
            return
        self.mark_seen(header.key)
        self._later(HINT_MS, lambda: self._clear_hint(generation, header))

    def _clear_hint(self, generation, header):
        if self._current(generation, header):
            header.show_hint("")

    def _header_map(self, header):
        if self._header is header:
            # A child's Map can arrive before the final ancestor is viewable.
            # Confirm once after that Map batch, coalescing repeated events.
            self._queue_start(self.generation, header)

    def _header_unmap(self, header):
        # Cached page hosts can unmap/remap within one Tk turn. An old Unmap
        # event must not cancel a new activation that is already visible.
        if self._header is header and not self._visible(header):
            self.deactivate()

    def _header_destroy(self, header):
        if self._header is header:
            self.deactivate()

    def _owner_unmap(self, event):
        if event.widget is self.owner:
            self.deactivate()

    def _owner_unfocused(self, _event):
        if self._closed:
            return
        try:
            background = self.owner.focus_displayof() is None
        except tk.TclError:
            return
        if background:
            self.deactivate()

    def _owner_destroy(self, event):
        if event.widget is self.owner:
            self.close()

    def deactivate(self):
        self.generation += 1
        self.motion.cancel(self._line_key, settle=False)
        self.motion.cancel(self._glow_key, settle=False)
        for job in tuple(self._jobs):
            try:
                self.owner.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        self._jobs.clear()
        self._start_job = None
        header = self._header
        self.active_key, self._header, self._started = None, None, False
        if self._live(header):
            header.reset()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.deactivate()
        for event, handle in self._bindings:
            if handle:
                try:
                    self.owner.unbind(event, handle)
                except tk.TclError:
                    pass
        self._bindings.clear()
