"""Shared helpers that used to be copy-pasted into every module.

Kept tiny on purpose: the moment a helper needs its own dependency it is
probably specific enough to live beside its caller instead of here.
"""

import os
import sys
import traceback
from datetime import datetime


def fix_console():
    """Reopen stdout/stderr as UTF-8 so CJK survives the cp936 console.

    Windows terminals default to cp936 and would mangle game titles and the
    Chinese log lines. Under a windowless frozen build the streams are None,
    and the reconfigure is skipped (AttributeError / OSError).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def log_crash(exc, val, tb, path):
    """Append a traceback to `path`. Returns the text, or "" if nothing stuck.

    A windowless build (`console=False`) has `sys.stderr is None`, so tkinter's
    default `report_callback_exception` blows up inside its own error report and
    the real traceback is lost - the user sees a button that "does nothing" or a
    dialog that flashes, and there is nothing to act on. Writing the traceback to
    a file is the only thing that survives that, so the next report is a stack
    trace instead of a description.
    """
    text = "".join(traceback.format_exception(exc, val, tb))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n===== %s =====\n"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            fh.write(text)
    except OSError:
        return ""
    return text
