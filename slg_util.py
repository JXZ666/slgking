"""Shared helpers that used to be copy-pasted into every module.

Kept tiny on purpose: the moment a helper needs its own dependency it is
probably specific enough to live beside its caller instead of here.
"""

import sys


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
