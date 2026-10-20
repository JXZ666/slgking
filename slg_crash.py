"""Bounded, local-only exception logs. This module has no GUI dependencies."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import traceback
from uuid import uuid4

MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_ENTRY_BYTES = 32 * 1024
MAX_TAIL_BYTES = 256 * 1024
BACKUP_COUNT = 3
_lock = threading.RLock()
_context = {"version": "unknown (startup)", "channel": "unknown (startup)"}
_last_path: str | None = None


def _paths() -> list[Path]:
    primary = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) / "slgking" / "crash.log"
    fallback = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "slgking" / "crash.log"
    return list(dict.fromkeys((primary, fallback)))


def redact(text: str) -> str:
    """Remove common credentials and personal home names, including legacy logs."""
    text = str(text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;'\"<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)([?&](?:access_token|refresh_token|token|api_key|apikey|key|password|passwd|pwd|secret|authorization|auth|code|signature|session|session_id|cookie)=)[^\s&#'\"<>]*", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(\b(?:access_token|refresh_token|api_key|apikey|password|passwd|secret|authorization|session_id|cookie|token|key)\b[\"']?\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}]+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)([a-z]:[\\/]+Users[\\/]+)[^\\/\r\n\"']+", r"\1[USER]", text)
    text = re.sub(r"(/(?:home|Users)/)[^/\s\"']+", r"\1[USER]", text)
    return text


def set_context(version=None, channel=None) -> None:
    with _lock:
        if version is not None:
            _context["version"] = redact(str(version))[:128]
        if channel is not None:
            _context["channel"] = redact(str(channel))[:128]


def format_exception(exc_type, value, tb) -> str:
    try:
        return redact("".join(traceback.format_exception(exc_type, value, tb)))
    except Exception:
        return "Exception formatting unavailable\n"


def _bounded_entry(exc_type, value, tb, source) -> bytes:
    kind = redact(getattr(exc_type, "__name__", "unknown"))[:128]
    header = (f"\n[{datetime.now(timezone.utc).isoformat()}] source={redact(str(source))[:128]} type={kind} "
              f"version={_context['version']} channel={_context['channel']}\n")
    body = format_exception(exc_type, value, tb)
    encoded = (header + body).encode("utf-8", "replace")
    if len(encoded) > MAX_ENTRY_BYTES:
        marker = b"\n[entry truncated]\n"
        encoded = encoded[:MAX_ENTRY_BYTES - len(marker)].decode("utf-8", "ignore").encode("utf-8") + marker
    return encoded


def _write(path: Path, entry: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        # Preserve pre-existing oversized logs outside the bounded rotation set.
        archive = path.with_name(path.name + ".legacy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
        path.replace(archive)
    if path.exists() and path.stat().st_size + len(entry) > MAX_LOG_BYTES:
        oldest = path.with_name(path.name + f".{BACKUP_COUNT}")
        oldest.unlink(missing_ok=True)
        for index in range(BACKUP_COUNT - 1, 0, -1):
            previous = path.with_name(path.name + f".{index}")
            if previous.exists():
                previous.replace(path.with_name(path.name + f".{index + 1}"))
        path.replace(path.with_name(path.name + ".1"))
    with path.open("ab") as stream:
        stream.write(entry)


def record_exception(exc_type, value, tb, source="unknown", preferred_path=None) -> str | None:
    global _last_path
    try:
        with _lock:
            entry = _bounded_entry(exc_type, value, tb, source)
            candidates = ([Path(preferred_path)] if preferred_path else []) + _paths()
            for path in dict.fromkeys(candidates):
                try:
                    _write(path, entry)
                    _last_path = str(path)
                    return _last_path
                except Exception:
                    continue
    except Exception:
        pass
    return None


def get_log_path() -> str:
    with _lock:
        return _last_path or str(_paths()[0])


def available_logs() -> list[str]:
    with _lock:
        bases = ([Path(_last_path)] if _last_path else []) + _paths()
        result = []
        for path in dict.fromkeys(bases):
            for candidate in [path] + [path.with_name(path.name + f".{n}") for n in range(1, BACKUP_COUNT + 1)]:
                try:
                    if candidate.is_file() and str(candidate) not in result:
                        result.append(str(candidate))
                except OSError:
                    pass
            try:
                result.extend(str(p) for p in sorted(path.parent.glob(path.name + ".legacy-*"), reverse=True) if str(p) not in result)
            except OSError:
                pass
        return result


def read_log_tail(path=None, max_bytes=MAX_TAIL_BYTES) -> dict:
    selected = str(path or (available_logs() or [get_log_path()])[0])
    try:
        limit = min(MAX_TAIL_BYTES, max(0, int(max_bytes)))
        with open(selected, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
        if size > limit:
            # Do not expose a truncated credential whose key lies outside the tail.
            data = data.partition(b"\n")[2]
        text = redact(data.decode("utf-8", "replace"))
        if size > limit:
            text = "[仅显示日志末尾 / showing log tail]\n" + text
        return {"path": selected, "text": text, "error": None}
    except FileNotFoundError:
        return {"path": selected, "text": "", "error": None}
    except Exception as exc:
        return {"path": selected, "text": "", "error": redact(str(exc))}


def _main_hook(exc_type, value, tb):
    record_exception(exc_type, value, tb, source="main_thread")
    if sys.stderr is not None:
        try:
            sys.__excepthook__(exc_type, value, tb)
        except Exception:
            pass


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    record_exception(args.exc_type, args.exc_value, args.exc_traceback, source="background_thread")
    if sys.stderr is not None:
        try:
            threading.__excepthook__(args)
        except Exception:
            pass


def install_hooks(version=None, channel=None) -> None:
    set_context(version, channel)
    with _lock:
        sys.excepthook = _main_hook
        threading.excepthook = _thread_hook
