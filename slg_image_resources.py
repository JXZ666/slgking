"""Tk-owned image resources and bounded, weak cover consumers.

Cache and consumer methods run on the Tk thread. Decode callables never touch Tk.
Eviction drops only cache ownership: a visible widget still owns its CTkImage.
"""
from collections import OrderedDict
from collections.abc import MutableMapping
import weakref
import tkinter as tk
import customtkinter as ctk
from slg_safe_focus import SafeFocusMixin


def cancel_widget_timers(widget):
    """Cancel subtree-owned timers without deleting their Tcl command registry.

    Destroy deletes the widget's registered commands, but Tcl after records can
    survive. A later Python callback can reuse that command name and receive
    an old timer's empty argument list. Root-owned business jobs are excluded.
    """
    commands = set()
    pending = [widget]
    while pending:
        current = pending.pop()
        commands.update(getattr(current, '_tclCommands', None) or ())
        try:
            pending.extend(current.winfo_children())
        except Exception:
            pass
    if not commands:
        return 0
    cancelled = 0
    for handle in widget.tk.splitlist(widget.tk.call('after', 'info')):
        try:
            script, _kind = widget.tk.splitlist(widget.tk.call('after', 'info', handle))
            parts = widget.tk.splitlist(script)
            if parts and parts[0] in commands:
                widget.tk.call('after', 'cancel', handle)
                cancelled += 1
        except tk.TclError:
            pass
    return cancelled


def estimate_image_bytes(image):
    """Conservative RGBA estimate for PIL sources and scaled Tk images."""
    sources = {id(value): value for value in
               (getattr(image, "_light_image", None), getattr(image, "_dark_image", None))
               if value is not None}
    total = sum(value.width * value.height * 4 for value in sources.values())
    for name in ("_scaled_light_photo_images", "_scaled_dark_photo_images"):
        for photo in getattr(image, name, {}).values():
            total += photo.width() * photo.height() * 4
    return max(total, 1)


class ImageLRU(MutableMapping):
    """Dictionary-compatible LRU with count and estimated byte limits."""
    def __init__(self, max_entries=192, max_bytes=96 * 1024 * 1024, estimator=None):
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("image cache limits must be positive")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._estimate = estimator or estimate_image_bytes
        self._data = OrderedDict()
        self.evictions = 0

    @property
    def estimated_bytes(self):
        # CTkImage creates scaled PhotoImages lazily after insertion.
        return sum(self._estimate(value) for value in self._data.values())

    def trim(self):
        while self._data and (len(self._data) > self.max_entries or
                              self.estimated_bytes > self.max_bytes):
            self._data.popitem(last=False)
            self.evictions += 1

    def __getitem__(self, key):
        value = self._data[key]
        self._data.move_to_end(key)
        self.trim()
        return value

    def __setitem__(self, key, value):
        self._data[key] = value
        self._data.move_to_end(key)
        self.trim()

    def __delitem__(self, key):
        del self._data[key]

    def __iter__(self):
        return iter(tuple(self._data))

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data

    def items(self):
        # Snapshot avoids access-driven reordering during iteration.
        return tuple(self._data.items())

    def clear(self):
        self._data.clear()


class _ImageCallbackCleanup:
    """Own the CTk 6 image callback lifecycle without patching the package."""
    def _detach_image(self):
        image = getattr(self, "_image", None)
        if isinstance(image, ctk.CTkImage):
            callbacks = image._configure_callback_list
            while self._update_image in callbacks:
                image.remove_configure_callback(self._update_image)

    def configure(self, *args, **kwargs):
        # CTk already removes the old image callback when changing images.
        # Keep that behavior; destroy is the missing lifecycle operation.
        return super().configure(*args, **kwargs)

    def destroy(self):
        cancel_widget_timers(self)
        self._detach_image()
        super().destroy()
        self._image = None


class ResourceLabel(_ImageCallbackCleanup, ctk.CTkLabel):
    """Release owned images on destroy and clear the native label on reset."""

    def configure(self, *args, **kwargs):
        # CTkLabel skips _update_image when its image becomes None, leaving
        # a native Tk image name behind after the CTkImage is released.
        if "image" in kwargs and kwargs["image"] is None:
            self._label.configure(image="")
        return super().configure(*args, **kwargs)



class ResourceButton(_ImageCallbackCleanup, ctk.CTkButton):
    """CTkButton has the same CTk 6 destroy omission as CTkLabel."""


class ResourceScrollbar(ctk.CTkScrollbar):
    """Bound CTk's nested update_idletasks during DPI/scroll-region changes."""
    def __init__(self, *args, **kwargs):
        self._slg_drawing = False
        self._slg_redraw_job = None
        super().__init__(*args, **kwargs)

    def set(self, start_value, end_value):
        start, end = float(start_value), float(end_value)
        if (abs(start-self._start_value) < 1e-9 and abs(end-self._end_value) < 1e-9):
            return
        super().set(start, end)

    def _draw(self, no_color_updates=False):
        if self._slg_drawing:
            return
        self._slg_drawing = True
        before = (self._start_value, self._end_value)
        try:
            super()._draw(no_color_updates)
        finally:
            self._slg_drawing = False
        if before != (self._start_value, self._end_value) and self._slg_redraw_job is None:
            def redraw():
                self._slg_redraw_job = None
                self._draw()
            self._slg_redraw_job = self.after(1, redraw)

    def destroy(self):
        if self._slg_redraw_job is not None:
            self.after_cancel(self._slg_redraw_job)
            self._slg_redraw_job = None
        super().destroy()


class ResourceScrollableFrame(ctk.CTkScrollableFrame):
    """Release this frame's root wheel/Shift commands, preserving siblings."""
    def __init__(self, *args, **kwargs):
        self._slg_global_bindings = []
        self._slg_resource_destroyed = False
        super().__init__(*args, **kwargs)
        old = self._scrollbar
        placement = old.grid_info()
        self._scrollbar = ResourceScrollbar(self._parent_frame, orientation=self._orientation,
            command=self._parent_canvas.xview if self._orientation == "horizontal" else self._parent_canvas.yview,
            fg_color=old.cget("fg_color"), button_color=old.cget("button_color"),
            button_hover_color=old.cget("button_hover_color"))
        self._parent_canvas.configure(**{
            "xscrollcommand" if self._orientation == "horizontal" else "yscrollcommand": self._scrollbar.set})
        old.destroy()
        self._scrollbar.grid(**placement)

    def bind_all(self, sequence=None, func=None, add=None):
        funcid = super().bind_all(sequence, func, add)
        if func is not None and funcid:
            self._slg_global_bindings.append((sequence, funcid))
        return funcid

    def destroy(self):
        if self._slg_resource_destroyed:
            return
        self._slg_resource_destroyed = True
        root = self._root()
        for sequence, funcid in self._slg_global_bindings:
            try:
                # bind_all registers on root. Filter only this function's Tcl
                # script and delete its command; unbind_all would break peers.
                root._unbind(('bind', 'all', sequence), funcid)
            except tk.TclError:
                if root._tclCommands and funcid in root._tclCommands:
                    root._tclCommands.remove(funcid)
        self._slg_global_bindings.clear()
        cancel_widget_timers(self._parent_frame)
        super().destroy()


class ResourceToplevel(SafeFocusMixin, ctk.CTkToplevel):
    """Cancel toolkit/preview timers before releasing a dialog's Tcl commands."""
    def destroy(self):
        before = getattr(self, "_slg_before_destroy", None)
        self._slg_before_destroy = None
        if before is not None:
            before()
        cancel_widget_timers(self)
        super().destroy()


class CoverConsumers:
    """Deduplicate consumers and cap submitted AND delivered decode results.

    request returns False under pressure, allowing a later visible-row retry.
    complete must be called for every delivered result, even obsolete/error ones.
    No business message is dropped or removed from the application's queue.
    """
    def __init__(self, max_pending=32):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.max_pending = max_pending
        self._pending = {}
        self._futures = {}
        self.rejected = 0

    @property
    def pending_count(self):
        return len(self._pending)

    @property
    def consumer_count(self):
        return sum(len(consumers) for consumers in self._pending.values())

    def request(self, key, widget, generation, executor, decode, publish):
        """decode() -> PIL/result; publish(key, result) -> queue.put on worker."""
        if key in self._pending:
            self._pending[key][widget] = generation
            return True
        if len(self._pending) >= self.max_pending:
            self.rejected += 1
            return False
        consumers = weakref.WeakKeyDictionary()
        consumers[widget] = generation
        self._pending[key] = consumers
        def work():
            try:
                result = decode()
            except Exception as exc:
                result = exc
            publish(key, result)
        try:
            self._futures[key] = executor.submit(work)
        except Exception:
            self._pending.pop(key, None)
            raise
        return True

    def complete(self, key, current_generation=None):
        """Release admission and return only live, still-bound consumers.

        Optional current_generation filters page-generation results. Key checks
        also protect card slots reused for a different game.
        """
        consumers = self._pending.pop(key, {})
        self._futures.pop(key, None)
        result = []
        for widget, generation in list(consumers.items()):
            if current_generation is not None and generation != current_generation:
                continue
            if getattr(widget, "_slg_cover_key", None) != key:
                continue
            try:
                if widget.winfo_exists():
                    result.append((widget, generation))
            except Exception:
                continue
        return result

    def prune(self, current_generation=None):
        """Cancel queued work only when no current consumer needs its result.

        Running/delivered work retains its key and admission until complete,
        preventing an old result from being mistaken for a new same-key request.
        Returns the number of successfully cancelled tasks.
        """
        cancelled = 0
        for key, consumers in list(self._pending.items()):
            for widget, generation in list(consumers.items()):
                valid = (getattr(widget, '_slg_cover_key', None) == key and
                         (current_generation is None or generation == current_generation))
                try:
                    valid = valid and bool(widget.winfo_exists())
                except Exception:
                    valid = False
                if not valid:
                    consumers.pop(widget, None)
            future = self._futures.get(key)
            if not consumers and future is not None and future.cancel():
                self._pending.pop(key, None)
                self._futures.pop(key, None)
                cancelled += 1
        return cancelled

    def clear(self):
        """Clear on shutdown after stopping executor submissions."""
        self._pending.clear()
        self._futures.clear()
