"""Guard only delayed Tk focus restoration; never suppress unrelated errors."""
import weakref
import tkinter as tk


class SafeFocusMixin:
    """Put before CTk/CTkToplevel in the MRO. No toolkit patch is needed."""

    def invalidate_focus_callbacks(self):
        root = self._root()
        root._slg_focus_generation = getattr(root, '_slg_focus_generation', 0) + 1

    def after(self, ms, func=None, *args):
        target = getattr(func, '__self__', None)
        if (func is not None and isinstance(target, tk.Misc)
                and getattr(func, '__name__', '') in ('focus', 'focus_set', 'focus_force')):
            root = self._root()
            generation = getattr(root, '_slg_focus_generation', 0)
            target_ref = weakref.ref(target)
            method = func.__name__

            def guarded_focus():
                widget = target_ref()
                if (widget is None or generation != getattr(root, '_slg_focus_generation', 0)):
                    return
                try:
                    exists = widget.winfo_exists()
                except tk.TclError:
                    return
                if exists:
                    getattr(widget, method)(*args)

            return super().after(ms, guarded_focus)
        return super().after(ms, func, *args)
