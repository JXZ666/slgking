"""Photograph the window so card changes can be looked at, not guessed.

    python tools/shot_cards.py [outdir]

Writes one PNG per theme into `outdir` (default: the temp dir) and prints the
paths. Reads the real library so real covers show up, but every pref write is
stubbed out: looking at the app must not change it. The window is up for a few
seconds and then closes itself.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

import customtkinter as ctk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import slg_db  # noqa: E402
import slg_gui  # noqa: E402


def shoot(win, path):
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    box = (x, y, x + win.winfo_width(), y + win.winfo_height())
    ImageGrab.grab(bbox=box, all_screens=True).save(path)
    print(path)


def place(win, x, y):
    """Park a window at a known spot.

    A toplevel the window manager has not positioned yet reports the geometry
    it was asked for rather than the one it has, and grabbing then photographs
    whatever happens to be at those coordinates - a corner of the desktop, in
    practice.
    """
    win.geometry("+%d+%d" % (x, y))
    win.update()


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="slgshot-")
    os.makedirs(outdir, exist_ok=True)
    # Nothing here may touch the user's preferences, the theme poll included.
    with mock.patch.object(slg_db, "set_pref"):
        app = slg_gui.App(notify=False)
        app.geometry("1180x760+40+40")
        app.update()
        # _apply_theme pins the mode off "跟随系统", which is what has to happen
        # before the five-second poll fires: it rebuilds the whole window, and a
        # rebuild mid-shoot deletes the widgets being photographed.
        for mode in ("light", "dark"):
            app._apply_theme(mode)
            app.update()
            # A selection, so the hover colour on the chosen card is in frame.
            if app.rows:
                app.select(app.rows[0])
                app.update()
            shoot(app, os.path.join(outdir, "cards-%s.png" % mode))
            if len(app._card_pool) > 2:
                app.select(app.rows[min(2, len(app.rows) - 1)])
                app.update()
                shoot(app, os.path.join(outdir, "cards-%s-selected.png" % mode))
        for mode in ("light", "dark"):
            app._apply_theme(mode)
            app.update()
            app.open_help()
            app.update()
            for win in [w for w in app.winfo_children()
                        if isinstance(w, ctk.CTkToplevel) and w.winfo_exists()]:
                place(win, 80, 80)
                shoot(win, os.path.join(outdir, "help-%s.png" % mode))
                win.destroy()
                app.update()
        app.destroy()
    print("done")


if __name__ == "__main__":
    main()
