#!/usr/bin/env python
"""SLG黄游之王 - dikgames 游戏库管理。

  double-click the exe -> a window
  slgking --sync       -> refresh the catalogue from dikgames and exit

The window title and the desktop shortcut are Chinese; everything internal
(exe name, module names, the repo) stays ASCII on purpose. A Chinese module
name and a Chinese exe name both come out mangled through cp936 - rpykit-luna
lives with that and its build_exe.bat is ASCII-only for the same reason.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402


def _attach_console():
    """Borrow the console this process was launched from, if there is one.

    The exe is built windowless: double-clicking it must not produce a black
    window, and a console=False build leaves sys.stdout as None. Launched from
    cmd or PowerShell there IS a parent console worth talking to, so the CLI
    flags keep printing where the user is looking. AttachConsole fails on the
    double-click path and we simply stay windowless - never AllocConsole, which
    would put the stray black window back.
    """
    # From source the terminal is already ours, so only the frozen build has
    # anything to attach.
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            if not ctypes.windll.kernel32.AttachConsole(-1):
                return
            for name, mode in (("stdout", "w"), ("stderr", "w"), ("stdin", "r")):
                dev = "CONIN$" if name == "stdin" else "CONOUT$"
                setattr(sys, name, open(dev, mode, encoding="utf-8",
                                        errors="replace", buffering=1))
        except Exception:  # noqa: BLE001 - no console is cosmetic, not fatal
            return

    # Game titles and paths are routinely CJK; the Windows console defaults to
    # cp936 and would mangle them. Under a windowless build these are None.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _pause_on_error(code):
    """Keeps the console open long enough to read a failure.

    Explorer closes the console the moment this process exits, so without this
    an error would flash past unread.
    """
    if code == 0 or not getattr(sys, "frozen", False):
        return
    try:
        input("\n失败（退出码 %d）。按回车关闭..." % code)
    except (EOFError, OSError, RuntimeError):
        pass


def main(argv=None):
    _attach_console()
    parser = argparse.ArgumentParser(
        prog="slgking",
        description="SLG黄游之王 - dikgames 游戏库管理")
    parser.add_argument("--smoke", action="store_true",
                        help="open the window, render once, exit (build check)")
    parser.add_argument("--version", action="store_true",
                        help="print the version and build time and exit")
    sub = parser.add_subparsers(dest="command")
    # add_help=False on every subcommand: otherwise the stub parser swallows
    # --help and shows its own empty option list instead of the real one, which
    # lives in the module's own parser and is passed the leftovers.
    for name, blurb in (("scrape", "抓取 dikgames 站点数据"),
                        ("scan", "扫描本地游戏目录"),
                        ("updates", "列出本地有新版游戏"),
                        ("translate", "用 AI 翻译标签和简介")):
        sub.add_parser(name, help=blurb, add_help=False)
    args, rest = parser.parse_known_args(argv)

    if args.version:
        import slg_gui
        print("%s %s" % (slg_gui.APP_TITLE, slg_gui.build_stamp()))
        return 0

    if args.command == "scrape":
        import slg_scrape
        return slg_scrape._main(rest) or 0
    if args.command == "scan":
        import slg_scan
        code = slg_scan._main(rest) or 0
        _pause_on_error(code)
        return code
    if args.command == "updates":
        import slg_scan
        code = slg_scan._main(["--updates"] + rest) or 0
        _pause_on_error(code)
        return code
    if args.command == "translate":
        import slg_translate
        code = slg_translate._main(rest) or 0
        _pause_on_error(code)
        return code

    import slg_gui
    code = slg_gui.main(smoke=args.smoke)
    _pause_on_error(code or 0)
    return code or 0


if __name__ == "__main__":
    sys.exit(main())
