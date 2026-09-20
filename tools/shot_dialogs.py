"""Photograph the dialogs that shot_cards.py frames badly.

    python tools/shot_dialogs.py [--demo] [--verify]

shot_cards.py parks a dialog with `place()` and then grabs the rectangle the
dialog reports. On this display the two disagree: Tk reports the position it was
asked for while the window manager still has the dialog where it opened, so the
grab happens at the requested spot and photographs the window behind it. The
翻译设置 shot in assets/screenshots came out with its right and bottom edges cut
off that way.

This grabs the rectangle the dialog actually occupies - rootx/rooty read after
the dialog has settled, no repositioning - and nothing else. A dialog is opaque,
so its own rectangle is all of it and none of what is behind.

`--demo` builds the invented library from shot_cards and then ages the recorded
local versions, because build_demo_library writes each folder version equal to
the site version and 检查更新 would report nothing to do. Nothing here is real:
the games, the versions and the covers are all made up.

Writes `dlg-<name>-<mode>.png` into promo/shots/. `--verify` adds one contact
sheet of everything it wrote, which is how the framing gets checked.
"""

import os
import re
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont, ImageGrab  # noqa: E402

import slg_db  # noqa: E402
import slg_engines  # noqa: E402
import slg_gui  # noqa: E402

from shot_cards import build_demo_library  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "promo", "shots")

# (name, opener). All of these build a window from the local database and
# return - none of them reach the network, so the demo library is enough.
DIALOGS = [
    ("updates", lambda app: app.do_updates()),
    ("settings", lambda app: app.open_translate_settings()),
    ("weights", lambda app: app.open_weights()),
    ("tools", lambda app: app.open_tools()),
]


def age_local_versions(how_many=6):
    """Make the recorded local versions older than the site's.

    检查更新 compares the version in the folder name against the version on the
    site. build_demo_library writes them equal, so the dialog would have nothing
    to list; a demo library is for showing what a feature does.
    """
    conn = slg_db.connect()
    rows = conn.execute(
        "SELECT g.id, g.version FROM local l JOIN games g ON g.id = l.game_id"
        " ORDER BY g.id LIMIT ?", (how_many,)).fetchall()
    changed = []
    for game_id, site in rows:
        # Stored bare ("0.8.2"); the window is what puts the "v" on. Keep
        # whichever form this row used so the column stays consistent.
        m = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)$", site or "")
        if not m:
            continue
        prefix, major, minor, patch = m.group(1), *(int(x) for x in m.groups()[1:])
        older = "%s%d.%d.%d" % (prefix, major, max(0, minor - 1), patch)
        conn.execute("UPDATE local SET folder_version = ? WHERE game_id = ?",
                     (older, game_id))
        changed.append((game_id, older, site))
    conn.commit()
    print("aged %d local versions, e.g. %s" % (len(changed), changed[:2]))
    return changed


def dialogs(app):
    import customtkinter as ctk
    return [w for w in app.winfo_children()
            if isinstance(w, ctk.CTkToplevel) and w.winfo_exists()]


def contact_sheet(paths, out, scale=0.45):
    ims = []
    for p in paths:
        im = Image.open(p).convert("RGB")
        im = im.resize((int(im.width * scale), int(im.height * scale)),
                       Image.LANCZOS)
        ims.append((p, im))
    w = max(i.width for _, i in ims) + 24
    total = sum(i.height for _, i in ims) + 26 * len(ims)
    sheet = Image.new("RGB", (w, total), (16, 16, 18))
    d = ImageDraw.Draw(sheet)
    f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20, index=0)
    y = 0
    for p, i in ims:
        d.text((12, y + 4), "%s  %s" % (os.path.basename(p), i.size),
               font=f, fill=(255, 120, 120))
        y += 26
        sheet.paste(i, (12, y))
        y += i.height
    sheet.save(out)
    print(out)


def main():
    use_demo = "--demo" in sys.argv[1:]
    os.makedirs(SHOTS, exist_ok=True)
    if use_demo:
        print("demo library:", build_demo_library())
        age_local_versions()

    real_resolve = slg_engines.resolve_config

    def keyless(conn, explicit_key=None):
        real = real_resolve(conn, explicit_key)
        return slg_engines.Config(real.engine, real.provider, real.base_url,
                                  "", real.model)

    written = []
    with mock.patch.object(slg_db, "set_pref"):
        app = slg_gui.App(notify=False)
        app.geometry("1180x760+40+40")
        app.update()
        for mode in ("dark", "light"):
            app._apply_theme(mode)
            app.update()
            for name, opener in DIALOGS:
                with mock.patch.object(slg_engines, "resolve_config", keyless):
                    opener(app)
                app.update()
                wins = dialogs(app)
                if not wins:
                    print("!! %s/%s: no dialog opened" % (name, mode))
                    continue
                win = wins[-1]
                win.lift()
                # DWM composites a freshly mapped toplevel a beat late; grabbing
                # straight away photographs whatever was underneath it.
                time.sleep(0.7)
                app.update()
                x, y = win.winfo_rootx(), win.winfo_rooty()
                box = (x, y, x + win.winfo_width(), y + win.winfo_height())
                screen = ImageGrab.grab(all_screens=True).convert("RGB")
                if box[2] > screen.width or box[3] > screen.height or box[0] < 0 or box[1] < 0:
                    print("!! %s/%s: rect %s outside screen %s"
                          % (name, mode, box, screen.size))
                    win.destroy()
                    app.update()
                    continue
                path = os.path.join(SHOTS, "dlg-%s-%s.png" % (name, mode))
                crop = screen.crop(box)
                crop.save(path)
                print("%s  rect=%s  size=%s" % (path, box, crop.size))
                written.append(path)
                win.destroy()
                app.update()
        app.destroy()

    if "--verify" in sys.argv[1:] and written:
        contact_sheet(written, os.path.join(SHOTS, "verify-dialogs.png"))
    print("done")


if __name__ == "__main__":
    main()
