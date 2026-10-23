"""Page-owned scroll position; deferred callbacks cannot affect another page."""
import tkinter as tk


def begin(app, key, frame=None, *, offset=0.0, force=False):
    if not force and getattr(app, "_scroll_page_key", None) == key:
        return getattr(app, "_scroll_page_epoch", 0)
    app._scroll_page_key = key
    epoch = app._scroll_page_epoch = getattr(app, "_scroll_page_epoch", 0) + 1
    app._scroll_page_initial = (epoch, offset)
    frame = frame or getattr(app, "detail", None)
    def apply():
        if (getattr(app, "_scroll_page_epoch", None) != epoch
                or getattr(app, "_scroll_page_key", None) != key):
            return
        try:
            canvas = getattr(frame, "_parent_canvas", None)
            if canvas is not None and canvas.winfo_exists():
                canvas.yview_moveto(offset)
        except tk.TclError:
            pass
    app.after_idle(apply)
    return epoch


def snapshot(app, canvas):
    epoch = getattr(app, "_scroll_page_epoch", 0)
    initial = getattr(app, "_scroll_page_initial", None)
    offset = initial[1] if initial is not None and initial[0] == epoch else canvas.yview()[0]
    return epoch, offset


def restore(app, canvas, saved):
    epoch, offset = saved
    if epoch != getattr(app, "_scroll_page_epoch", 0):
        return
    if canvas is not None and canvas.winfo_exists():
        canvas.yview_moveto(offset)
    if (getattr(app, "_scroll_page_initial", None) or (None,))[0] == epoch:
        app._scroll_page_initial = None


def finish(app):
    app._scroll_page_initial = None
