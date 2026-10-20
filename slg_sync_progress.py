"""Truthful per-stage sync events; never an overall percentage."""
import time


class SyncCancelled(RuntimeError):
    """The caller requested stopping; a started atomic merge may have finished."""


class StageProgress:
    def __init__(self, callback, catalog_id, should_stop=None, clock=time.monotonic):
        self.callback = callback
        self.catalog_id = catalog_id
        self.should_stop = should_stop or (lambda: False)
        self.clock = clock
        self.last = None
        self.last_time = 0
        self.phase = 'manifest'

    def check_stop(self):
        if self.should_stop():
            raise SyncCancelled('目录更新已取消；已完成的原子合并保留。')

    def emit(self, phase, done=0, total=None, unit=None, status='running', **extra):
        self.phase = phase
        event = dict(catalog_id=self.catalog_id, phase=phase, done=done,
                     total=total, unit=unit, status=status, **extra)
        key = (phase, total, unit, status)
        now = self.clock()
        # Stage changes and exact completions bypass the 100 ms throttle.
        if (key == self.last and status == 'running' and done != total
                and now - self.last_time < .1):
            return
        self.last, self.last_time = key, now
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass  # UI observers must never break a catalogue transaction.
