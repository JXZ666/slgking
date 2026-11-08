"""Small Tk scheduling and monotonic timing helpers; no UI or business policy."""

import time
import tkinter as tk


class MotionScheduler:
    """Own delayed callbacks for one Tk widget, cancelling them on destruction.

    Call from the Tk thread. Visibility policy belongs to the caller: use
    cancel_all on Unmap and resume on Map when that effect needs pausing.
    """

    def __init__(self, owner):
        self.owner = owner
        self._jobs = set()
        self._closed = False
        owner.bind("<Destroy>", self._destroyed, add="+")

    @property
    def pending_count(self):
        return len(self._jobs)

    def call_later(self, delay, callback):
        """Return a Tk after handle, or None if the owner has been destroyed."""
        if self._closed:
            return None
        handle = None

        def run():
            self._jobs.discard(handle)
            if not self._closed:
                callback()

        try:
            handle = self.owner.after(max(0, int(delay)), run)
        except tk.TclError:
            self._closed = True
            self.cancel_all()
            return None
        self._jobs.add(handle)
        return handle

    def cancel_all(self):
        """Cancel queued work; safe to call repeatedly or after owner destruction."""
        jobs, self._jobs = self._jobs, set()
        for handle in jobs:
            try:
                self.owner.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass

    def _destroyed(self, event):
        if event.widget is self.owner:
            self._closed = True
            self.cancel_all()


class Elapsed:
    """Monotonic elapsed time and clamped progress, with an injectable clock."""

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.started = clock()

    def reset(self):
        self.started = self.clock()

    @property
    def seconds(self):
        return max(0.0, self.clock() - self.started)

    def progress(self, duration):
        return 1.0 if duration <= 0 else min(1.0, self.seconds / duration)


class TimeBudget:
    """Bound a batch by time and count. Check before processing each item.

    The first item is always allowed: a slow item cannot starve all progress.
    This cannot preempt a running handler; keep handlers individually short.
    """

    def __init__(self, milliseconds=8, max_items=200, clock=time.perf_counter):
        self.clock = clock
        self.started = clock()
        self.seconds = max(0.0, milliseconds / 1000.0)
        self.max_items = max(1, int(max_items))
        self.count = 0

    @property
    def available(self):
        return self.count < self.max_items and (
            self.count == 0 or self.clock() - self.started < self.seconds)

    def consumed(self):
        self.count += 1
