"""Bounded, atomic on-demand downloads for legacy SLG catalogue covers."""
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

from slg_dlsite_online import atomic_bytes, validate_image


class PublicCoverCache:
    def __init__(self, directory, fetch, clock=time.monotonic):
        self.directory, self.fetch, self.clock = Path(directory), fetch, clock
        self._lock = threading.Lock()
        self._failure = OrderedDict()

    def clear_retry(self):
        with self._lock:
            self._failure.clear()

    def get(self, filename):
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}\.(?:jpg|jpeg|png|webp)", filename, re.I):
            raise ValueError("Invalid public cover filename")
        path = self.directory / filename
        if path.is_symlink():
            raise ValueError("Cover cache cannot be a symbolic link")
        if path.is_file():
            try:
                if path.stat().st_size <= 5 * 1024 * 1024:
                    validate_image(path.read_bytes())
                    return str(path)
            except (OSError, ValueError):
                pass
        with self._lock:
            retry_at, attempts = self._failure.get(filename, (0, 0))
            if retry_at > self.clock():
                raise RuntimeError("Cover retry delayed")
        try:
            raw = self.fetch("/covers/" + filename)
            if not isinstance(raw, bytes) or len(raw) > 5 * 1024 * 1024:
                raise ValueError("Cover too large")
            validate_image(raw)
            atomic_bytes(path, raw)
            with self._lock:
                self._failure.pop(filename, None)
            return str(path)
        except Exception:
            with self._lock:
                self._failure[filename] = (self.clock() + min(3600, 30 * 2 ** min(attempts, 7)), attempts + 1)
                self._failure.move_to_end(filename)
                while len(self._failure) > 192:
                    self._failure.popitem(last=False)
            raise
