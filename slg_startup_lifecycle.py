"""One-time startup handoff after the root enters its event loop.

The callback owns the existing splash/age-gate behavior.  This helper only
defers that handoff and cancels it when the application closes; it never
changes a window state, polls, or accesses toolkit-private state.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable


def run_mainloop_until_closed(owner, run: Callable[[], object], *,
                             is_closing: Callable[[], bool]):
    """Allow only CTk's known Windows-titlebar tail after explicit teardown.

    CTk's first mainloop calls update() while setting the Windows title bar.
    A close event can destroy the root in that nested loop, after which CTk
    tries `wm state`.  Ignore that exact terminal error only when the normal
    close path ran and the root Tcl command has actually disappeared.
    """
    try:
        return run()
    except tk.TclError as error:
        if (str(error).strip() != 'can\'t invoke "wm" command: application has been destroyed'
                or not is_closing()):
            raise
        try:
            gone = not owner.tk.call("info", "commands", str(owner))
        except tk.TclError:
            gone = False
        if not gone:
            raise
        return None


class StartupHandoff:
    """Schedule at most one handoff, with terminal cancellation on close."""

    def __init__(self, owner, callback: Callable[[], None], *,
                 is_closing: Callable[[], bool] | None = None):
        self._owner = owner
        self._callback = callback
        self._is_closing = is_closing or (lambda: False)
        self._job = None
        self._finished = False
        self._cancelled = False

    @property
    def pending(self) -> bool:
        return self._job is not None

    def schedule(self) -> bool:
        """Return True only when a new idle callback was registered."""
        if self._finished or self._cancelled or self._job is not None:
            return False
        if self._is_closing():
            self.cancel()
            return False
        try:
            if not self._owner.winfo_exists():
                self.cancel()
                return False
            self._job = self._owner.after_idle(self._run)
        except tk.TclError:
            self.cancel()
            return False
        return True

    def _run(self):
        self._job = None
        if self._finished or self._cancelled:
            return
        self._finished = True
        try:
            exists = bool(self._owner.winfo_exists())
        except tk.TclError:
            exists = False
        if not exists or self._is_closing():
            self._callback = None
            return
        callback, self._callback = self._callback, None
        # Callback errors belong to the application's normal Tk reporter.
        # They must not trigger a second reveal or be silently swallowed.
        callback()

    def cancel(self):
        """Permanently retire the handoff, including a late queued callback."""
        self._cancelled = True
        self._callback = None
        job, self._job = self._job, None
        if job is not None:
            try:
                self._owner.after_cancel(job)
            except tk.TclError:
                pass
