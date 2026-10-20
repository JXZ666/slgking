"""Finite, bounded Tk effects. Business results never depend on animation."""
from collections import OrderedDict
import time
import tkinter as tk
import weakref

DURATIONS = {'button': 120, 'nav': 160, 'popover': 140, 'success': 300}


def ease_out(progress):
    return 1 - (1 - progress) ** 3


class MicroInteractions:
    """One coordinator per App; at most two effects and one 30 fps timer.

    play(key, widget, update, kind='button') calls update(eased_progress).
    Replace/cancel/background/reduced-motion settle at 1.0 while widget is live.
    Call set_reduced immediately when the setting changes, close on app teardown.
    """
    def __init__(self, owner, reduced=False, clock=time.perf_counter):
        self.owner, self.reduced, self.clock = owner, bool(reduced), clock
        self._effects = OrderedDict()
        self._job = None
        self._closed = False
        self._bindings = [(weakref.ref(owner), '<FocusOut>', owner.bind('<FocusOut>', self._unfocused, add='+')),
                          (weakref.ref(owner), '<Unmap>', owner.bind('<Unmap>', self._unmapped, add='+')),
                          (weakref.ref(owner), '<Destroy>', owner.bind('<Destroy>', self._destroyed, add='+'))]

    def attach_button(self, widget, paint, kind='button'):
        """paint(progress) paints resting=0/hover=1; return the unique effect key.

        Paint only a small decorative property (for example a border), leaving
        toolkit input states and command invocation unchanged.
        """
        key = ('hover', id(widget))
        current = [0.0]
        # CTkButton.bind does not return handles and its unbind forbids funcid.
        # Bind its native surfaces explicitly so cleanup preserves CTk handlers.
        targets = [getattr(widget, name, None) for name in ('_canvas', '_text_label', '_image_label')]
        targets = [target for target in targets if target is not None] or [widget]
        for target in targets:
            for sequence, reverse in (('<Enter>', False), ('<Leave>', True), ('<ButtonPress-1>', False)):
                def changed(_event, reverse=reverse):
                    self.cancel(key, settle=False)
                    start, end = current[0], 0.0 if reverse else 1.0
                    def update(progress):
                        current[0] = start + (end - start) * progress
                        paint(current[0])
                    self.play(key, widget, update, kind)
                handle = target.bind(sequence, changed, add='+')
                self._bindings.append((weakref.ref(target), sequence, handle))
        return key

    @property
    def active_count(self):
        return len(self._effects)

    def _live(self, widget):
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def play(self, key, widget, update, kind='button'):
        duration = DURATIONS[kind] / 1000
        self.cancel(key)
        if self._closed or not self._live(widget):
            return
        if self.reduced or not self.owner.winfo_ismapped():
            update(1.0)
            return
        while len(self._effects) >= 2:
            self.cancel(next(iter(self._effects)))
        self._effects[key] = (widget, update, self.clock(), duration)
        update(0.0)
        self._schedule()

    def cancel(self, key, settle=True):
        item = self._effects.pop(key, None)
        if item and settle and self._live(item[0]):
            item[1](1.0)
        if not self._effects:
            self._cancel_job()

    def _cancel_job(self):
        job, self._job = self._job, None
        if job is not None:
            try:
                self.owner.after_cancel(job)
            except (tk.TclError, ValueError):
                pass

    def _schedule(self):
        if self._job is None and self._effects and not self._closed:
            self._job = self.owner.after(34, self._tick)

    def _tick(self):
        self._job = None
        try:
            for key, item in tuple(self._effects.items()):
                if self._effects.get(key) is not item:
                    continue
                widget, update, start, duration = item
                if not self._live(widget):
                    self._effects.pop(key, None)
                    continue
                progress = min(1.0, max(0.0, (self.clock() - start) / duration))
                if progress >= 1:
                    self._effects.pop(key, None)
                update(ease_out(progress))
        finally:
            self._schedule()

    def set_reduced(self, reduced):
        self.reduced = bool(reduced)
        if self.reduced:
            self.cancel_all()

    def cancel_all(self, settle=True):
        for key in tuple(self._effects):
            self.cancel(key, settle)
        self._cancel_job()

    def _unfocused(self, event):
        if self._closed: return
        if self.owner.focus_displayof() is None:
            self.cancel_all()

    def _unmapped(self, event):
        if event.widget is self.owner:
            self.cancel_all()

    def _destroyed(self, event):
        if event.widget is self.owner:
            self.close()

    def close(self):
        self._closed = True
        self.cancel_all(settle=False)
        for ref, sequence, handle in self._bindings:
            widget = ref()
            if widget is not None and self._live(widget) and handle:
                widget.unbind(sequence, handle)
        self._bindings.clear()
