"""Photograph the window so card changes can be looked at, not guessed.

    python tools/shot_cards.py [--demo] [--title NAME] [outdir]

Writes one PNG per theme into `outdir` (default: the temp dir) and prints the
paths. Four views: the card list, the detail panel, the help document, and the
translation settings dialog - the last three are what the README illustrates.

Reads the real library so real covers show up, but every pref write is stubbed
out: looking at the app must not change it. The settings dialog gets the same
treatment for the API key, which is blanked before the shot rather than trusted
to the mask. The window is up for a few seconds and then closes itself.

`--demo` swaps the real library for an invented one, because the real covers and
titles are not something a public README should carry. See build_demo_library.

`--title NAME` photographs the sidebar header under a different name than the
source declares, for promo material that must not carry the app's own wording.
It patches the global; slg_gui.py is untouched.
"""

import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402

import customtkinter as ctk  # noqa: E402
from PIL import Image, ImageDraw, ImageGrab  # noqa: E402

import slg_db  # noqa: E402
import slg_engines  # noqa: E402
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


def dialogs(app):
    """The open CTkToplevels, in creation order."""
    return [w for w in app.winfo_children()
            if isinstance(w, ctk.CTkToplevel) and w.winfo_exists()]


# --- the demo library ------------------------------------------------------------
#
# The README needs pictures of the window and the real library cannot supply
# them: dikgames catalogues adult games, so its covers, titles and tag chips are
# all explicit, and these PNGs land in a public repository. So the demo builds
# its own - same schema, same code paths, invented games and drawn covers - and
# points LOCALAPPDATA at it before anything opens the db. The real library is
# not read, not copied, and not touched.

DEMO_TAG_ZH = [
    ("slice of life", "日常"), ("romance", "恋爱"), ("adventure", "冒险"),
    ("fantasy", "奇幻"), ("mystery", "悬疑"), ("drama", "剧情"),
    ("pixel art", "像素风"), ("2DCG", "2DCG"), ("sandbox", "沙盒"),
    ("puzzle", "解谜"),
]

DEMO_GAMES = [
    dict(slug="lantern-and-ledger", title="Lantern & Ledger", zh="灯与账本",
         developer="Paper Street", version="0.8.2", rating=9.1, complete=0,
         status="playing", tags=("slice of life", "romance", "2DCG"),
         overview="A clerk inherits a harbour-town bookshop along with its debts. "
                  "Balance the ledgers by day, hear the regulars out by night, and "
                  "decide which promises are worth keeping.",
         zh_overview="继承了一间海港小镇的旧书店，也继承了它欠下的账。白天理账，"
                     "晚上听熟客说话，然后决定哪些承诺值得兑现。"),
    dict(slug="the-quiet-repair-shop", title="The Quiet Repair Shop", zh="安静的修理铺",
         developer="Kettleworks", version="1.2.0", rating=8.7, complete=1,
         status="downloaded", tags=("slice of life", "romance", "pixel art"),
         overview="Everything in the shop can be mended except the thing you brought "
                  "in. Six weeks, one counter, and a customer who keeps coming back.",
         zh_overview="铺子里什么都能修，除了你带来的那件。六个星期，一个柜台，"
                     "和一位总是回头的客人。"),
    dict(slug="paper-cranes", title="Paper Cranes", zh="千纸鹤",
         developer="Studio Fold", version="0.5.1", rating=8.9, complete=0,
         status="want", tags=("drama", "mystery", "2DCG"),
         overview="A folded crane appears on your desk every morning. Following them "
                  "leads back through a year you thought you had finished with.",
         zh_overview="每天早上桌上都会多一只纸鹤。顺着它们往回走，"
                     "会走到你以为早已翻篇的那一年。"),
    dict(slug="midnight-bakery", title="Midnight Bakery", zh="深夜面包房",
         developer="Crumb & Co.", version="1.0.0", rating=9.3, complete=1,
         status="downloaded", tags=("slice of life", "romance", "adventure"),
         overview="The ovens only work after midnight and the queue is never quite "
                  "human. Learn the recipes, keep the lights on, and don't ask "
                  "about the flour.",
         zh_overview="烤炉只在午夜之后才热，排队的客人也不太像人。学会配方，"
                     "让灯一直亮着，别问面粉的事。"),
    dict(slug="salt-and-cider", title="Salt & Cider", zh="海盐与苹果酒",
         developer="Harbourline", version="0.9.4", rating=8.2, complete=0,
         status="want", tags=("slice of life", "drama", "pixel art"),
         overview="Two weeks on a fading coast, an orchard that will not sell, and a "
                  "cousin you have not spoken to since the funeral.",
         zh_overview="在海边小镇待两周：卖不掉的果园，"
                     "和葬礼之后就没再说过话的表亲。"),
    dict(slug="the-cartographers-daughter", title="The Cartographer's Daughter",
         zh="制图师的女儿", developer="Compass Rose", version="2.1.0", rating=9.0,
         complete=0, status="playing", tags=("adventure", "fantasy", "mystery"),
         overview="Every map your father drew has one blank corner. Charting them "
                  "all is the only way to find the place he stopped writing about.",
         zh_overview="父亲画的每一张地图都留着一个空白角。把那些角都走一遍，"
                     "才能找到他停笔的地方。"),
    dict(slug="wool-and-thunder", title="Wool & Thunder", zh="羊毛与雷霆",
         developer="Highland Ink", version="0.6.7", rating=7.8, complete=1,
         status="downloaded", tags=("fantasy", "sandbox", "2DCG"),
         overview="A storm god has taken up sheep farming. You have been hired to "
                  "keep the accounts, which would be simpler if the flock stopped "
                  "predicting weather.",
         zh_overview="一位风暴之神改行养羊了。你被雇来记账——"
                     "如果羊群不再预报天气，这活儿会简单很多。"),
    dict(slug="marmalade-skies", title="Marmalade Skies", zh="橘子酱色的天空",
         developer="Sunroom", version="1.4.2", rating=8.5, complete=1,
         status="want", tags=("romance", "slice of life", "drama"),
         overview="One rooftop, one summer, and a neighbour who climbs up every "
                  "evening to argue about the correct way to make breakfast.",
         zh_overview="一个天台，一个夏天，和一位每晚爬上来跟你争论早餐该怎么做的邻居。"),
    dict(slug="the-last-tram", title="The Last Tram", zh="末班电车",
         developer="Nightline", version="0.7.0", rating=8.8, complete=0,
         status="playing", tags=("mystery", "drama", "puzzle"),
         overview="The last tram runs a route that is not on any timetable. Ride it "
                  "to the end often enough and the passengers start recognising you.",
         zh_overview="末班电车走的是一条不在时刻表上的线路。坐到终点坐得够多次，"
                     "乘客就会开始认得你。"),
]

# Muted pairs, so a wall of them still reads as "placeholder" rather than "art".
DEMO_COVER_PAIRS = [
    ((58, 76, 112), (24, 32, 54)), ((122, 78, 62), (56, 32, 28)),
    ((64, 104, 96), (26, 46, 44)), ((118, 96, 58), (56, 44, 24)),
    ((96, 66, 108), (42, 28, 50)), ((74, 108, 132), (30, 48, 62)),
    ((124, 106, 84), (58, 48, 36)), ((88, 118, 72), (38, 54, 32)),
    ((108, 84, 96), (48, 34, 44)),
]

# The settings dialog decides between "AI 翻译" and "免费机翻" from the stored
# engine, not from whether a key is present - so an engine pref alone is enough
# to photograph the AI half, with the key left genuinely blank.
DEMO_PREFS = [
    ("translate_engine", slg_engines.ENGINE_OPENAI),
    ("translate_provider", slg_engines.DEFAULT_PROVIDER),
    ("translate_base_url", "https://api.deepseek.com"),
    ("translate_model", "deepseek-flash"),
    ("theme", "light"),
]


def demo_cover(path, index):
    """A gradient with a ring on it. Synthetic by construction, not by blur."""
    top, bottom = DEMO_COVER_PAIRS[index % len(DEMO_COVER_PAIRS)]
    width, height = 576, 356
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / (height - 1)
        draw.line([(0, y), (width, y)],
                  fill=tuple(int(top[c] + (bottom[c] - top[c]) * t) for c in range(3)))
    draw.ellipse([width * 0.30, height * 0.13, width * 0.70, height * 0.87],
                 outline=(236, 236, 240), width=5)
    img.save(path)


def build_demo_library():
    """A throwaway library of games that do not exist. Returns its root dir.

    LOCALAPPDATA is redirected before the first app_dir() call, so this both
    seeds the demo and guarantees the real db is never opened.
    """
    root = tempfile.mkdtemp(prefix="slgdemo-")
    os.environ["LOCALAPPDATA"] = root
    conn = slg_db.connect()
    covers = slg_db.covers_dir()
    for index, game in enumerate(DEMO_GAMES):
        slug = game["slug"]
        game_id, _ = slg_db.upsert_game(
            conn, slug, "https://example.invalid/games/" + slug, game["title"],
            version=game["version"], developer=game["developer"], engine="Ren'Py",
            rating=game["rating"], last_updated="2026-%02d-%02d" % (1 + index, 3 + index * 3),
            tags=game["tags"], complete=game["complete"])
        slg_db.upsert_detail(conn, game_id, overview=game["overview"])
        name = slug + ".png"
        demo_cover(os.path.join(covers, name), index)
        slg_db.set_cover(conn, game_id, name)
        slg_db.set_state(conn, game_id, status=game["status"], my_rating=index % 5 + 1)
        if game["status"] != "want":
            conn.execute(
                "INSERT OR REPLACE INTO local (game_id, folder_path, folder_version)"
                " VALUES (?,?,?)",
                (game_id, "D:/Games/" + slug, game["version"]))
        # Both halves of the 中文 view are seeded, so the detail shot exercises
        # the cached path rather than reaching for an engine the demo has no key
        # for - see the early return in slg_gui._set_overview_lang.
        slg_db.set_auto_translation(conn, "title", game_id, game["title"],
                                    game["zh"], engine="deepseek-flash")
        slg_db.set_auto_translation(conn, "overview", game_id, game["overview"],
                                    game["zh_overview"], engine="deepseek-flash")
    for slug, zh in DEMO_TAG_ZH:
        slg_db.set_auto_translation(conn, "tag", slug, slug, zh,
                                    engine="deepseek-flash")
    for key, value in DEMO_PREFS:
        slg_db.set_pref(conn, key, value)
    conn.commit()
    return root


def scroll_to(frame, fraction):
    """CTkScrollableFrame keeps its canvas private, same as slg_gui reads it."""
    canvas = getattr(frame, "_parent_canvas", None)
    if canvas is not None:
        canvas.yview_moveto(fraction)


def main():
    args = sys.argv[1:]
    title = args[args.index("--title") + 1] if "--title" in args else None
    rest = [a for i, a in enumerate(args)
            if a not in ("--demo", "--title")
            and not (i > 0 and args[i - 1] == "--title")]
    outdir = rest[0] if rest else tempfile.mkdtemp(prefix="slgshot-")
    os.makedirs(outdir, exist_ok=True)
    if "--demo" in args:
        print("demo library:", build_demo_library())
    # The sidebar header is the app's own label, so the only way to photograph
    # it under the name the promo uses is to swap the global before the window
    # is built. The source keeps its name; the screenshot wears the promo's.
    titled = (mock.patch.object(slg_gui, "APP_TITLE", title) if title
              else contextlib.nullcontext())
    # Nothing here may touch the user's preferences, the theme poll included.
    with mock.patch.object(slg_db, "set_pref"), titled:
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

        # Detail panel. Picks a game that actually has a description, so the tag
        # cloud and the description are in frame instead of an empty panel, and
        # switches to 中文 - the card list beside it is already showing Chinese
        # names, and a panel left on 原文 would read as a half-translated bug.
        # Against the real library that switch costs an API call; against the
        # demo the answer is already cached, so nothing leaves the machine.
        # Scrolled down, because the cover and the status buttons fill the top.
        for mode in ("light", "dark"):
            app._apply_theme(mode)
            app.update()
            if app.rows:
                described = next((g for g in app.rows if g["overview"]), app.rows[0])
                app.select(described)
                app.update()
                app._set_overview_lang(described, "中文")
                app.update()
                # To the bottom. The panel's content (995px) is taller than the
                # frame (768px), so something is always off frame; the cover is
                # the least useful thing to lose, and scrolling all the way is
                # the only position that gets the tag cloud and the description
                # in frame together. yview_moveto clamps, so 1.0 means "as far
                # as this viewport goes", not "bottom of the content is at top".
                scroll_to(app.detail, 1.0)
                app.update()
                shoot(app, os.path.join(outdir, "detail-%s.png" % mode))

        for mode in ("light", "dark"):
            app._apply_theme(mode)
            app.update()
            app.open_help()
            app.update()
            for win in dialogs(app):
                place(win, 80, 80)
                shoot(win, os.path.join(outdir, "help-%s.png" % mode))
                win.destroy()
                app.update()

        # Settings dialog. Every pref write is already stubbed, but the key
        # entry is prefilled from the real config - masked, yet its length still
        # shows, and these files are meant for a public README. So the config is
        # stubbed to carry no key: the shot shows the "sk-..." placeholder, which
        # is what a fresh install shows anyway.
        real_resolve = slg_engines.resolve_config

        def keyless(conn, explicit_key=None):
            real = real_resolve(conn, explicit_key)
            return slg_engines.Config(real.engine, real.provider, real.base_url,
                                      "", real.model)

        for mode in ("light", "dark"):
            app._apply_theme(mode)
            app.update()
            with mock.patch.object(slg_engines, "resolve_config", keyless):
                app.open_translate_settings()
                app.update()
                for win in dialogs(app):
                    place(win, 80, 80)
                    win.lift()
                    app.update()
                    shoot(win, os.path.join(outdir, "settings-%s.png" % mode))
                    win.destroy()
                    app.update()

        app.destroy()
    print("done")


if __name__ == "__main__":
    main()
