"""The window.

Master-detail on purpose: a card carries only what you scan (cover, title,
version, rating, a few tags, a status dot) and every control that writes to the
database lives in the detail panel on the right. Putting stars and buttons on
every card would mean thousands of widgets for the full catalogue.

Light, card-based, Win11-ish - the stock tkinter look reads as Windows XP and
that was the one thing about the previous tools the user actively disliked.
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import webbrowser
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageGrab, ImageTk

import slg_comments
import slg_db
import slg_engines
import slg_scrape
import slg_titles
import slg_translate
import slg_update

APP_VERSION = "0.22.0"
# The sidebar shows the number and nothing else. build_stamp() still carries
# the channel and the build time, but it belongs on the 关于 page now: a
# timestamp in the corner of every screen was answering a question the user
# asks once.
APP_VERSION_LABEL = "v" + APP_VERSION
APP_TITLE = "SLG黄游之王"
AUTHOR = "菊千代赛高"
GITHUB_URL = "https://github.com/JXZ666"
GITHUB_LABEL = "GitHub 主页 · JXZ666"
CONTACT_EMAIL = "jxzsaikou666@qq.com"
CONTACT_MAILTO = "mailto:" + CONTACT_EMAIL
# The group has no join link that works without a key, so the number is offered
# as copyable text instead of a URL. Bare digits, no dashes or spaces: whatever
# is on the clipboard has to paste straight into QQ's search box.
QQ_GROUP = "1124074040"
QQ_GROUP_LABEL = "交流群 · %s · 欢迎大家加入" % QQ_GROUP
QQ_GROUP_COPY_LABEL = "交流群 %s（点击复制）" % QQ_GROUP
# Taken from the scraper rather than typed again: this is the site the catalogue
# comes from, and two copies of that URL is one copy that goes stale.
SITE_URL = slg_scrape.BASE
SITE_LABEL = "数据来源 · dikgames.com"
# The site is English-first and most games ship untranslated; pointing users at
# LunaTranslator (real-time machine translation) plus the author's own RenPy
# tooling is the one referral that both helps and fits the "retrieval only" line.
LUNA_URL = "https://docs.lunatranslator.org/zh/"
LUNA_LABEL = "露娜翻译器 LunaTranslator"
RPYKIT_URL = "https://github.com/JXZ666/SLG-Renpy-Toolkit"
RPYKIT_LABEL = "RenPy 汉化小工具 · SLG-Renpy-Toolkit"
PREF_THEME = "theme"
PREF_FREE_NOTICE = "free_notice_seen"
# First-launch onboarding: shown once, then reachable again from 设置.
PREF_WELCOME_SEEN = "welcome_seen"
# Bundled tag seed: imported once so a user who deletes a translation on purpose
# doesn't have it silently restored on the next launch.
PREF_SEED_TAGS_IMPORTED = "seed_tags_imported_v1"
# Where the games live on this machine. Asked for once and remembered, because
# slg_scan used to ship one hard-coded path - the author's own - so the button
# did nothing on anybody else's computer.
PREF_SCAN_ROOT = "scan_root"


def asset_path(name):
    """A bundled asset, from inside the exe or from the source tree.

    One-file builds unpack the datas into a temp dir that only exists while the
    process runs, so the packaged path is sys._MEIPASS, not the exe's folder.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", name)

# Two palettes and one rebinding function, rather than a colour lookup at every
# call site: a bare name inside a function body is a fresh global lookup on each
# execution, so apply_palette() also repaints the widgets that read these during
# a redraw (_render_card, _paint_card, _render_filterbar) and not just the ones
# built at startup.
_LIGHT = {
    "BG": "#f3f3f3", "CARD": "#ffffff", "CARD_HOVER": "#eef3fb",
    "SIDEBAR": "#eaeaea", "ACCENT": "#2f6fd0", "TEXT": "#1a1a1a",
    "MUTED": "#6b6b6b", "CHIP": "#e4e9f2", "CHIP_OFF": "#f6d9d9",
    "PLACEHOLDER": "#d8dde5", "DANGER_TEXT": "#8a2b2b",
    "ON_ACCENT": "#ffffff", "STAR": "#e0a800",
}
_DARK = dict(
    _LIGHT,
    BG="#1c1c1e", CARD="#26262a", CARD_HOVER="#32323a", SIDEBAR="#141416",
    ACCENT="#4a8ee0", TEXT="#e8e8ea", MUTED="#9a9aa2", CHIP="#33333c",
    CHIP_OFF="#4a2b2b", PLACEHOLDER="#33333c", DANGER_TEXT="#e88a8a",
)

# Bound by apply_palette() below. Listed so the linter sees them defined.
BG = CARD = CARD_HOVER = SIDEBAR = ACCENT = TEXT = MUTED = ""
CHIP = CHIP_OFF = PLACEHOLDER = DANGER_TEXT = ON_ACCENT = STAR = ""


def apply_palette(mode):
    """Rebind the module-level colour names in place for "light" or "dark".

    globals() rather than a hand-written `global` list: the palette dicts are
    then the only source of truth, and adding a colour cannot leave one name
    stale (which would show up as black-on-black in dark mode).
    """
    for name, value in (_DARK if mode == "dark" else _LIGHT).items():
        globals()[name] = value


def resolved_theme(mode):
    """"light"/"dark"/"system" -> the palette that actually applies right now.

    ctk.get_appearance_mode() reports "Light"/"Dark" and re-reads the OS setting
    on every call, so "system" has to be resolved *after* the mode is set.
    """
    if mode == "system":
        return "dark" if ctk.get_appearance_mode() == "Dark" else "light"
    return "dark" if mode == "dark" else "light"


apply_palette("light")

COVER_W, COVER_H = 128, 79          # dikgames thumbs are 576x356, ~1.62:1
DETAIL_W, DETAIL_H = 300, 185
# Cards on screen at once, i.e. one page. The list was an accumulating
# "显示更多" before this: eighty cards built eagerly and four hundred on
# screen after a few clicks, which is where the scroll lag came from. The
# cost was never the query - find_games still reads the whole catalogue in
# single-digit milliseconds - it was the widget count.
#
# Eight is the measured fit of the maximized window, and that is the size this
# is designed against - a page that fills the screen with no scrolling is what
# the number is for. A card is cover-driven (79px * 1.5 widget scaling on the
# machine this was built for, plus its padding and three text lines = 142px),
# so eight is ~1140px of list.
#
# Below that the page simply does not all fit, and that is deliberate: the
# cards keep their size and the scroll area does the rest. Shrinking them to
# fit a narrow window is what makes a seven-card page unreadable, and it is the
# one thing this number must not do. A five-card page measured at the default
# 1180x760 wasted two rows on a maximized window, which was the other half of
# the complaint.
#
# A fixed number rather than a fit-to-window count, because the page numbers
# have to mean the same thing after a resize - the pager lets you type one, and
# a count that followed the window would silently move the game off the page
# you just turned to. The pager sits outside the scroll area (see _build), so
# it is reachable without scrolling whatever this is set to.
PAGE_SIZE = 8
# The card tagline's wrap width. It is a function of COVER_W - the text column
# is whatever is left of the list after the cover and its padding - so the two
# have to move together. It was a literal 430, which silently clipped the
# moment the cover grew.
CARD_WRAP = 405
REFRESH_GAP = 5.0                    # seconds between refreshes while syncing
# What that gap becomes while a job is running. A refresh rebuilds every card
# on screen, and during a sync the top of the list churns - enrich rewrites
# tags, tags move the score, the score is the default sort - so the
# skip-if-unchanged shortcut misses and the rebuild is a multi-second freeze on
# the tk thread. At 5s that was most of the time the user spent trying to click
# 停止. The progress line still updates every message; the end-of-job refresh
# still draws the final list.
REFRESH_GAP_BUSY = 20.0
DRAIN_PER_TICK = 200                 # background messages handled per pump pass

# Each field opens the way it reads: the best score, the best rating and the
# newest update first, but names from A. The arrow button flips from there.
SORT_FIELDS = {"按xp推荐": "score", "站内评分": "rating",
               "最近更新": "updated", "名称": "title", "热度": "heat"}
SORT_DEFAULT_DESC = {"score": True, "rating": True, "updated": True,
                     "title": False, "heat": True}
# Labelled: a bare ↓ in a 44px box next to the dropdown read as decoration, not
# as a control. "只有正序没有反序" was the report, and the arrow was the answer.
SORT_ARROW = {True: "↓ 降序", False: "↑ 升序"}

# Shown in the description box when there is no blurb to show. Deliberately not
# "the site has none": the library cannot yet tell "never fetched" from "the
# site never wrote one", and 968 of 1595 rows were in the first state. Claiming
# the site had no blurb for those would have been a lie the user could check.
EMPTY_OVERVIEW = ("暂无简介 — 本站没有提供，或还没抓到（点「更新游戏数据」可补齐）。\n"
                  "想自己写一段，点上面的「✎ 改简介」。")

# Nearly every game carries these, so leading with them wastes the three lines
# a card gets. Push them to the back and let the distinctive tags show.
GENERIC_TAGS = {
    "3dcg", "2dcg", "3d-game", "2d-game", "animated", "big-tits", "big-ass",
    "male-protagonist", "female-protagonist", "mobile-game", "adventure",
    "visual-novel", "vaginal-sex", "oral-sex", "handjob", "teasing",
}

VIEWS = [("全部", None), ("想玩", "want"), ("已下载", "downloaded")]

_THEME_LABELS = {"light": "浅色", "dark": "深色", "system": "跟随系统"}

# Every font in the app comes from ui_font(). customtkinter's own default family
# is Roboto, which ships no CJK glyphs: each Chinese character was reaching the
# screen through GDI font linking, and bold was falling back to a *synthesised*
# weight that smears the strokes together - the dense paragraphs in 帮助文档 were
# where that became unreadable. Chinese font family names are listed alongside
# the English ones because tkinter reports whichever the font registers.
_FONT_CANDIDATES = ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei",
                    "SimHei", "黑体", "SimSun", "宋体")
_ui_family = None


def _resolve_ui_family():
    """Pick the UI font family once, the first time a font is asked for.

    Resolved lazily rather than at import: font.families() needs a live root,
    and this module is imported before there is one. The fallback is
    TkDefaultFont's own family, which is already the system UI font.
    """
    global _ui_family
    if _ui_family is None:
        try:
            have = set(tkfont.families())
        except Exception:  # noqa: BLE001 - no root yet, or no font system
            have = set()
        _ui_family = (next((f for f in _FONT_CANDIDATES if f in have), None)
                      or tkfont.nametofont("TkDefaultFont").actual()["family"])
    return _ui_family


def ui_font(size, weight="normal"):
    """The one place a font for a widget is built."""
    return ctk.CTkFont(family=_resolve_ui_family(), size=size, weight=weight)


def ui_tkfont(size, weight="normal"):
    """A plain tkinter font, for canvas text.

    Canvas items take a font rather than a widget, so they cannot use the
    CTkFont above. Going through the same resolution anyway: it is the only
    place that knows which installed family has CJK glyphs, and splash text in
    a font of its own would be the one place that showed it.
    """
    return tkfont.Font(family=_resolve_ui_family(), size=size, weight=weight)


def _mix(color_a, color_b, t):
    """Blend two #rrggbb colours; t=0 gives a, t=1 gives b.

    Canvas items have no alpha, so every faint mark in the splash is a real
    blend against the colour behind it instead of a translucent overlay.
    """
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _section(parent, text):
    """Small caption that breaks the sidebar's flat run of buttons into groups."""
    ctk.CTkLabel(parent, text=text, text_color=MUTED, anchor="w",
                 font=ui_font(size=11)).pack(fill="x", padx=18, pady=(12, 2))


def _rule(parent):
    ctk.CTkFrame(parent, height=1, fg_color=CHIP, corner_radius=0).pack(
        fill="x", padx=12, pady=(12, 0))


def _nav_button(parent, text, command, active=False, height=34, size=13,
                **pack_kwargs):
    """A sidebar nav button: muted by default, filled when `active`.

    The view and tool rows all share this shape; the one-off buttons (同步,
    the delete-collection danger row) differ enough to stay inline.
    """
    btn = ctk.CTkButton(
        parent, text=text, anchor="w", height=height, corner_radius=8,
        fg_color=CARD if active else "transparent",
        text_color=TEXT, hover_color=CARD,
        font=ui_font(size=size), command=command)
    pack = {"fill": "x", "padx": 12, "pady": 2}
    pack.update(pack_kwargs)
    btn.pack(**pack)
    return btn


def _provider_id(label):
    """The provider id behind an option menu's display name."""
    for row in slg_engines.PROVIDERS:
        if row[1] == label:
            return row[0]
    return slg_engines.DEFAULT_PROVIDER


def _link_button(parent, text, url, **pack_kwargs):
    """A link that reads as a button rather than as a caption.

    An 11px underlined label was too easy to walk past at the foot of the
    sidebar - which is exactly where someone looks for who made this and where
    the source lives. A filled button costs nothing and gets found.
    """
    button = ctk.CTkButton(parent, text=text, height=30, corner_radius=8,
                           fg_color=CHIP, text_color=ACCENT, hover_color=CARD_HOVER,
                           font=ui_font(size=12), anchor="w",
                           command=lambda u=url: webbrowser.open(u))
    if pack_kwargs:
        button.pack(**pack_kwargs)
    return button


def _copy_button(parent, text, value, app, **pack_kwargs):
    """`_link_button`'s twin, for text that is copied rather than opened.

    Same chrome on purpose: the email link and the group number sit side by side
    in three different dialogs, and one of them looking like a plain caption
    would read as the less important of the two.

    The command is attached after construction because the flash needs the
    button being built, which the lambda cannot close over until it exists.
    """
    button = ctk.CTkButton(parent, text=text, height=30, corner_radius=8,
                           fg_color=CHIP, text_color=ACCENT, hover_color=CARD_HOVER,
                           font=ui_font(size=12), anchor="w")
    button.configure(command=lambda: app._copy_value(value, button, text))
    if pack_kwargs:
        button.pack(**pack_kwargs)
    return button


def _centred_row(parent, **grid_kwargs):
    """A full-width row whose contents sit on the window's centre axis.

    Columns 0 and 2 are equal-weight spacers, so the middle column is centred
    while there is room. When the contents outgrow the row the right spacer
    collapses first and the contents stay fully readable, rather than being
    clipped at both ends the way equal left and right padding would.

    Returns the inner frame, which is where the row's widgets go. The caller
    must grid it into the parent's column 0: `parent` gets a weighted column 0
    here, without which the row would shrink to its contents and sit at the
    left edge instead of spanning the frame it is centring inside.
    """
    parent.grid_columnconfigure(0, weight=1)
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.grid(**grid_kwargs)
    row.grid_columnconfigure(0, weight=1)
    row.grid_columnconfigure(2, weight=1)
    inner = ctk.CTkFrame(row, fg_color="transparent")
    inner.grid(row=0, column=1)
    return inner


def title_error_text(error):
    """The line shown under a game name whose translation failed, or "".

    A "keep:" error is the guard refusing a name the model mangled - the English
    original is then genuinely the right answer and a warning would be noise. A
    real failure (no key, no network) is worth naming, because otherwise the
    language switch looks like it did nothing.
    """
    if not error or error.startswith("keep:"):
        return ""
    return "名称翻译失败：%s（简介不受影响）" % error


_image_cache = {}


def load_cover(game, width=COVER_W, height=COVER_H):
    """Cover thumbnail, or a neutral placeholder when it has not downloaded yet."""
    key = (game["slug"], width, height)
    if key in _image_cache:
        return _image_cache[key]
    path = None
    name = game["cover_file"]
    if name and not name.startswith("pending:"):
        candidate = os.path.join(slg_db.covers_dir(), name)
        if os.path.exists(candidate):
            path = candidate
    try:
        if path:
            img = Image.open(path).convert("RGB")
            img.thumbnail((width * 2, height * 2), Image.LANCZOS)
        else:
            img = Image.new("RGB", (width, height), PLACEHOLDER)
    except Exception:  # noqa: BLE001 - a broken thumbnail must not kill the list
        img = Image.new("RGB", (width, height), PLACEHOLDER)
        path = None
    if path is None:
        # The grey block is drawn in PLACEHOLDER, which is a palette colour, so
        # its cache key carries that colour. A theme switch then invalidates
        # exactly the placeholders instead of every real thumbnail - those used
        # to be thrown away and re-decoded en masse, which is most of why
        # switching themes took as long as it did.
        key = ("__placeholder__", width, height, PLACEHOLDER)
        if key in _image_cache:
            return _image_cache[key]
    ctk_img = ctk.CTkImage(light_image=img, size=(width, height))
    _image_cache[key] = ctk_img
    return ctk_img


def load_avatar(size=72):
    """The author's avatar, pre-cropped to a transparent circle in assets.

    Cached like load_cover so a theme switch does not re-decode it, and the
    module-level dict keeps the CTkImage referenced (an unreferenced CTkImage
    renders blank).
    """
    key = ("__avatar__", size)
    if key in _image_cache:
        return _image_cache[key]
    try:
        img = Image.open(asset_path("avatar.png")).convert("RGBA")
        img = img.resize((size * 2, size * 2), Image.LANCZOS)
    except Exception:  # noqa: BLE001 - a missing avatar must not crash the window
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ctk_img = ctk.CTkImage(light_image=img, size=(size, size))
    _image_cache[key] = ctk_img
    return ctk_img


# Tag slug -> Chinese, filled from the translations table. Held at module level
# because display_tag() runs once per chip per card, and every one of those
# would otherwise be a query. Empty until a translation run has happened, which
# is why the fallback below has to stay exactly what it always was.
_TAG_ZH = {}
_TITLE_ZH = {}


def load_tag_translations(conn):
    """Reload the tag dictionary. Called at startup and after a translation run."""
    global _TAG_ZH
    _TAG_ZH = slg_db.tag_translations(conn)


def display_tag(slug):
    return _TAG_ZH.get(slug) or slug.replace("-", " ")


def load_title_translations(conn):
    """Reload the game-name dictionary. Called at startup and after a run.

    Same shape as the tag dictionary above and for the same reason: a card has
    to render its name from a dict lookup, not from a query, and there are
    eighty cards on screen at a time.
    """
    global _TITLE_ZH
    _TITLE_ZH = slg_db.title_translations(conn)


def display_title(game):
    """The name to show for `game` - Chinese once it has been translated."""
    text, engine = _TITLE_ZH.get(str(game["id"]), (None, None))
    if engine == slg_db.ENGINE_UNTRANSLATED:
        return game["title"]
    return text or game["title"]


def build_stamp():
    """Version, channel and build time, for the 关于 page.

    Puts "am I running the build I just made?" in front of the user instead of
    in a guess about timestamps. The exe's own mtime is the honest answer for a
    frozen build - it changes exactly when a new one is written - and the
    source file's stands in when running from the tree.

    Not on screen by default: the sidebar reads APP_VERSION_LABEL, because the
    build time is a question the user has once and then reads past forever.
    """
    if getattr(sys, "frozen", False):
        path, channel = sys.executable, "exe"
    else:
        path, channel = os.path.abspath(__file__), "源码"
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    except OSError:
        when = "?"
    return "v%s · %s · %s" % (APP_VERSION, channel, when)


def meta_text(game):
    """The one-line summary under a card's title.

    Split out of _render_card because a status or rating write now rewrites
    this line in place instead of redrawing the card - two copies of this
    expression would drift apart the first time a field is added.
    """
    parts = []
    if game["rating"]:
        parts.append("★ %.1f" % game["rating"])
    parts.append("完结" if game["complete"] else "连载中")
    if game["last_updated"]:
        parts.append(game["last_updated"])
    if game["folder_path"]:
        parts.append("已在本地")
    if game["status"]:
        parts.append(slg_db.STATUS_LABELS.get(game["status"], game["status"]))
    return " · ".join(parts) or "—"


CARD_TAG_LIMIT = 6


def card_tags(tags):
    """Which of a game's tags a card shows.

    The near-universal ones are pushed to the back so the distinctive tags get
    the three lines a card has - leading with the common ones made every card
    read the same. Kept separate from _fill_card so the ordering is testable
    without a window.
    """
    return sorted(tags, key=lambda t: (t in GENERIC_TAGS, t))[:CARD_TAG_LIMIT]


def flow_rows(widths, available, gap=4):
    """Row index for each item, wrapping at `available` pixels.

    Split out from FlowFrame so the wrapping maths can be tested without
    opening a window.
    """
    out, row, used = [], 0, 0
    for width in widths:
        if used and used + gap + width > available:
            row += 1
            used = 0
        if used:
            used += gap
        used += width
        out.append(row)
    return out


class FlowFrame(ctk.CTkFrame):
    """A wrapping run of chips.

    CTk has no flow layout. The detail panel used to pack its tag buttons
    side="left", which runs them off the right edge - and 488 games carry 25 or
    more tags, so most of them were unreachable even maximised. This measures
    the chips once and re-places them whenever the width changes.
    """

    def __init__(self, master, gap_x=4, gap_y=4, **kwargs):
        super().__init__(master, **kwargs)
        self._gap_x = gap_x
        self._gap_y = gap_y
        self._items = []
        self._width = -1
        self.bind("<Configure>", self._on_configure)

    def set_items(self, widgets):
        self._items = list(widgets)
        self._layout()

    def add_item(self, widget):
        self._items.append(widget)
        self._layout()

    def _on_configure(self, event):
        if abs(event.width - self._width) < 2:
            return  # height-only configure, or the panel did not really move
        self._layout(event.width)

    def _layout(self, width=None):
        if not self._items:
            return
        actual = width or self.winfo_width()
        if actual <= 1:
            return  # not mapped yet; the first <Configure> will come back
        self._width = actual

        # CTk multiplies both the constructor geometry and the arguments of
        # place() by the display scaling (1.5x here), while winfo_* reports the
        # finished pixels. So the flow maths has to run in the same unscaled
        # units the chips were built in, or every chip lands 1.5x too far out.
        available = self._reverse_widget_scaling(actual)
        widths = [self._reverse_widget_scaling(w.winfo_reqwidth()) for w in self._items]
        heights = [self._reverse_widget_scaling(w.winfo_reqheight()) for w in self._items]
        rows = flow_rows(widths, available, self._gap_x)

        x = y = bottom = 0
        current = -1
        for widget, w, h, r in zip(self._items, widths, heights, rows):
            if r != current:
                if current >= 0:
                    y += bottom + self._gap_y
                current, x, bottom = r, 0, 0
            widget.place(x=x, y=y)
            x += w + self._gap_x
            bottom = max(bottom, h)
        self.configure(height=y + bottom)


class _Splash(ctk.CTkToplevel):
    """A borderless loading card shown while the window builds itself.

    The heavy startup - seed copy, db connect, ~480 card widgets - is all
    synchronous, so a threaded progress bar would not paint until it was over.
    Everything here is drawn on one canvas for the same reason: a frame costs a
    few itemconfig()/coords() calls, where rebuilding widgets would cost more
    than the window it is covering for.

    The animation is the catalogue's own verb - a scan. A line sweeps the card,
    the logo sits inside a ring that fills with real progress, the title
    flickers through junk glyphs as if it were still being read, and the bar is
    a row of cells rather than a smooth fill. Static marks (rules, corner
    brackets) are drawn once; only the moving parts are touched per frame.
    """

    # The design is laid out in fractions of the card, not pixels: customtkinter
    # multiplies the window's size by the display scaling while tk.Canvas
    # coordinates stay real pixels, so a hard-coded layout is correct at 100%
    # and cramped at 150%. The fonts get the same scaling factor for the same
    # reason - a raw canvas font is the one thing CTk does not scale for us.
    _CELLS = 20
    _GLYPHS = "01#><*"
    _LOGO_F = 0.286      # logo centre
    _TITLE_F = 0.563
    _VERSION_F = 0.683
    _BAR_F = 0.825
    _STATUS_F = 0.913
    _RING_F = 0.159      # ring radius
    _MARGIN_F = 0.073    # left/right inset for the bar
    _ARM_F = 0.039       # corner bracket arm

    def __init__(self, master):
        super().__init__(master)
        self.overrideredirect(True)
        self.geometry("420x260")
        self.configure(fg_color=BG)
        self.attributes("-topmost", True)

        # Neon rim: an accent frame just inside the edge. Tk has no drop shadow,
        # so a 2px accent border is the closest cheap thing to a neon glow.
        rim = ctk.CTkFrame(self, fg_color=ACCENT, corner_radius=14)
        rim.pack(fill="both", expand=True, padx=2, pady=2)
        card = ctk.CTkFrame(rim, fg_color=BG, corner_radius=12)
        card.pack(fill="both", expand=True, padx=2, pady=2)

        self.canvas = tk.Canvas(card, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        try:
            self._logo_src = Image.open(asset_path("avatar.png"))
        except Exception:  # noqa: BLE001 - a missing logo must not block startup
            self._logo_src = None
        # Held on self: a PhotoImage the canvas points at is collected the
        # moment nothing else references it, and the item then draws nothing.
        self._logo_img = None

        self._frame = 0
        self._shown = 0.0            # eased toward _target, so a step never snaps
        self._target = 0.0
        self._status_text = "正在加载…"
        self._built = False
        self._centred = False
        # Canvas metrics, not self._w/self._h: tkinter keeps its widget path in
        # _w, and overwriting it makes every later call on this window fail with
        # "bad window path name".
        self._cw = self._ch = 0
        self._ticks = []
        self._title_chars = []
        self._make_fonts()
        self.canvas.bind("<Configure>", self._on_configure)
        self.after(40, self._tick)

    def _make_fonts(self):
        """Build the canvas fonts at the display's scaling.

        A raw tk.Canvas gets none of customtkinter's scaling for free, so a
        22pt title here would come out two thirds the size of the same text in
        the window behind it on a 150% display. Rebuilt on the first layout call
        because the scaling factor is not known until the window is mapped.
        """
        scale = self._apply_window_scaling(1.0) or 1.0
        self._title_font = ui_tkfont(int(round(22 * scale)), "bold")
        self._small_font = ui_tkfont(int(round(12 * scale)))
        self._pct_font = ui_tkfont(int(round(11 * scale)))

    def _centre(self):
        """Park the card in the middle of the screen, once it has a size.

        Measured rather than computed from the requested geometry: geometry()
        takes real pixels for the position but multiplies the size by the display
        scaling, so (screen - 420) / 2 would land the card half that growth off
        centre. The window is mapped by now, so winfo_width() is the real size.
        """
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:      # not mapped yet; the next configure retries
            return
        self._centred = True
        self.geometry("+%d+%d" % ((self.winfo_screenwidth() - w) // 2,
                                  (self.winfo_screenheight() - h) // 2 - 40))

    # --- drawing -------------------------------------------------------------

    def _on_configure(self, event):
        # Only ever fires once or twice: the window is borderless with a fixed
        # geometry, so there is nothing to reflow after the first real size.
        if event.width <= 1 or event.height <= 1:
            return
        if not self._centred:
            # The display scaling is not knowable until the window is mapped,
            # so the fonts are built for real here and the card is parked.
            self._make_fonts()
            self._centre()
        self._build_scene(event.width, event.height)

    def _build_scene(self, w, h):
        c = self.canvas
        c.delete("all")
        self._cw, self._ch = w, h
        self._bar_y = h * self._BAR_F
        self._status_y = h * self._STATUS_F
        cx = w / 2
        logo_y = h * self._LOGO_F
        inset = w * self._MARGIN_F

        # The catalogue's rows, before there is a catalogue to draw. Faint on
        # purpose: this is backdrop, not content.
        rule = _mix(BG, ACCENT, 0.10)
        step = max(6, int(h * 0.040))
        for y in range(step, int(h) - step, step):
            c.create_line(w * 0.02, y, w * 0.98, y, fill=rule)

        # HUD corner brackets - the one static mark that reads as game UI
        # rather than as a loading dialog.
        bracket = _mix(BG, ACCENT, 0.55)
        arm = w * self._ARM_F
        pad = w * 0.015
        for bx, by, dx, dy in ((pad, pad, 1, 1), (w - pad, pad, -1, 1),
                               (pad, h - pad, 1, -1), (w - pad, h - pad, -1, -1)):
            c.create_line(bx, by, bx + dx * arm, by, fill=bracket, width=2)
            c.create_line(bx, by, bx, by + dy * arm, fill=bracket, width=2)

        # The sweep. Created before the logo so it passes behind it.
        self._sweep_echo = c.create_line(0, 0, 0, 0, fill=_mix(BG, ACCENT, 0.28))
        self._sweep = c.create_line(0, 0, 0, 0, fill=ACCENT, width=2)

        # Rings: a track, an arc that fills with real progress, and a highlight
        # that keeps turning while the work behind it has nothing to report.
        r = h * self._RING_F
        box = (cx - r, logo_y - r, cx + r, logo_y + r)
        c.create_arc(*box, start=0, extent=359, style="arc",
                     outline=CHIP, width=2)
        self._ring_spin = c.create_arc(
            *box, start=0, extent=40, style="arc",
            outline=_mix(BG, ACCENT, 0.45), width=2)
        self._ring_fill = c.create_arc(*box, start=90, extent=0, style="arc",
                                       outline=ACCENT, width=3)

        if self._logo_src is not None:
            side = int(2 * (r * 0.78))
            img = self._logo_src.copy()
            img.thumbnail((side, side))
            self._logo_img = ImageTk.PhotoImage(img)
            c.create_image(cx, logo_y, image=self._logo_img)

        # One text item per character, so the glitch can swap a single glyph
        # without re-measuring the run every frame.
        tx = cx - sum(self._title_font.measure(ch) for ch in APP_TITLE) / 2
        self._title_chars = []
        for ch in APP_TITLE:
            item = c.create_text(tx, h * self._TITLE_F, text=ch, anchor="w",
                                 fill=TEXT, font=self._title_font)
            self._title_chars.append((item, ch, tx))
            tx += self._title_font.measure(ch)

        c.create_text(cx, h * self._VERSION_F, text=APP_VERSION_LABEL,
                      fill=MUTED, font=self._small_font)

        # Cells, not a smooth fill: 20 of them, evenly spread across the bar.
        pct_w = self._pct_font.measure("100%") + w * 0.015
        right = w - inset - pct_w
        gap = w * 0.010
        cell = ((right - inset) - gap * (self._CELLS - 1)) / self._CELLS
        half = h * 0.017
        self._pct = c.create_text(w - inset, self._bar_y, text="0%", anchor="e",
                                  fill=MUTED, font=self._pct_font)
        self._ticks = []
        for i in range(self._CELLS):
            x0 = inset + i * (cell + gap)
            self._ticks.append(c.create_rectangle(
                x0, self._bar_y - half, x0 + cell, self._bar_y + half,
                fill=CHIP, outline=""))

        self._status = c.create_text(inset, self._status_y,
                                     text=self._status_text, anchor="w",
                                     fill=MUTED, font=self._small_font)
        self._caret = c.create_rectangle(0, 0, 0, 0, fill=ACCENT, outline="")

        self._built = True
        self._advance()

    # --- animation -----------------------------------------------------------

    def _advance(self):
        """One frame. Touches only the items that move."""
        if not self._built:
            return
        self._frame += 1
        f = self._frame
        c = self.canvas

        x0, x1 = self._cw * 0.02, self._cw * 0.98
        trail = self._ch * 0.024
        y = self._ch * 0.03 + (f * self._ch * 0.016) % max(1.0, self._ch * 0.94)
        c.coords(self._sweep, x0, y, x1, y)
        c.coords(self._sweep_echo, x0, y + trail, x1, y + trail)

        c.itemconfig(self._ring_spin, start=(f * 7) % 360)
        c.itemconfig(self._ring_fill, start=90, extent=-359 * self._shown)

        # The glitch walks the title one character at a time. Driven by the
        # frame count rather than random: a startup card that renders
        # differently on every launch is one nothing can be asserted about.
        for item, ch, _x in self._title_chars:
            c.itemconfig(item, text=ch, fill=TEXT)
        slot = (f // 5) % len(self._title_chars)
        item, _ch, _x = self._title_chars[slot]
        c.itemconfig(item, text=self._GLYPHS[(f // 5) % len(self._GLYPHS)],
                     fill=ACCENT)

        self._shown += (self._target - self._shown) * 0.25
        filled = int(round(self._shown * self._CELLS))
        for i, item in enumerate(self._ticks):
            c.itemconfig(item, fill=ACCENT if i < filled else CHIP)
        c.itemconfig(self._pct, text="%d%%" % round(self._shown * 100))

        c.itemconfig(self._status, text=self._status_text)
        sx = (self._cw * self._MARGIN_F + self._small_font.measure(
            self._status_text) + self._cw * 0.012)
        if (f // 6) % 2:
            # A block, not a bar: it has to read as a cursor, and Tk has no
            # way to blink one for us.
            cy, cw = self._status_y, self._ch * 0.024
            c.coords(self._caret, sx, cy - cw, sx + cw, cy + cw)
            c.itemconfig(self._caret, state="normal")
        else:
            c.itemconfig(self._caret, state="hidden")

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._advance()
        self.after(40, self._tick)

    def step(self, text, progress):
        """Label the current stage and move the bar to `progress` (0..1).

        The window is built synchronously from here, so the event loop is
        blocked and after() callbacks never fire: the frames have to be pumped
        from this loop, which is why it calls update() itself.
        """
        self._status_text = text
        self._target = progress
        for _ in range(10):
            self._advance()
            self.update()
            time.sleep(0.012)


class App(ctk.CTk):
    def __init__(self, notify=True):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(1020, 600)
        # Without this the window and the taskbar entry keep tkinter's feather.
        try:
            self.iconbitmap(asset_path("slgking.ico"))
        except Exception:  # noqa: BLE001 - a missing icon is not worth a crash
            pass

        self._splash = None
        # Hidden until the splash has run its course: the window is built
        # synchronously below, so without a withdraw it would pop in fully
        # formed and leave the splash nothing to cross-fade into.
        if notify:
            self.withdraw()

        # A fresh install has an empty library until the first sync; copy the
        # bundled seed (a few hundred games + covers) in so there is something
        # to browse immediately. Gated on the db file not existing yet, so an
        # existing library is never touched.
        if notify and not os.path.exists(slg_db.db_path()):
            seed_db = asset_path("seed/slgking.db")
            if os.path.exists(seed_db):
                slg_db.install_seed_db(seed_db, asset_path("seed/covers"))
        self.conn = slg_db.connect()
        # notify is off for the packaging smoke test and the test harness, which
        # both expect an empty library; a real session imports the shipped tag
        # seed once so users without a translation API still see Chinese tags.
        if notify and not slg_db.get_pref(self.conn, PREF_SEED_TAGS_IMPORTED):
            seed = asset_path("tag_zh.json")
            if os.path.exists(seed):
                slg_db.import_seed_tag_translations(self.conn, seed)
                # Inside the exists() branch on purpose. Marking the import done
                # when the file was missing latched the flag: every later launch
                # skipped the import for good and left the user on English tag
                # names with no way back short of editing the db.
                slg_db.set_pref(self.conn, PREF_SEED_TAGS_IMPORTED, "1")
        load_tag_translations(self.conn)
        load_title_translations(self.conn)
        # Resolved before the first widget exists, so the opening frame is
        # already in the right palette instead of flashing white and repainting.
        self.theme_mode = slg_db.get_pref(self.conn, PREF_THEME, "system") or "system"
        if self.theme_mode not in _THEME_LABELS:   # hand-edited db, not worth a crash
            self.theme_mode = "system"
        ctk.set_appearance_mode(self.theme_mode)
        apply_palette(resolved_theme(self.theme_mode))
        self._sys_theme = resolved_theme(self.theme_mode)
        self.configure(fg_color=BG)
        if notify:
            self._splash = _Splash(self)
            self._splash.step("正在加载数据库…", 0.2)
            self.update_idletasks()
            self.update()
        self.include, self.exclude = [], []
        self.search = ""
        self.view = None
        self.origin = "main"
        self.collection_id = None
        self.sort = "score"
        self.sort_desc = SORT_DEFAULT_DESC[self.sort]
        self.selected = None
        self.rows = []
        # 1-based, and it indexes _page_slice(). self.rows stays the whole
        # result set on purpose: four other places read it as such (re-finding
        # the selection by id, dropping a card, resolving a click, counting
        # pages), and they would all change meaning if it held one page.
        self.page = 1
        self.queue = queue.Queue()
        self.busy = False
        self._job_label = ""
        self._cards = {}          # game id -> its card frame, for repainting
        # The same two things _cards cannot answer: the meta line on a card, and
        # the detail panel's status/stars. Status writes used to repaint the
        # whole window to change three buttons and one line of text.
        self._card_meta = {}      # game id -> the card's "★ 4.5 · 想玩" label
        self._card_title = {}     # game id -> the name label, rewritten in place
        # game id -> the whole slot dict. _paint_card needs the frame *and* the
        # text labels, because a plain tk.Label does not inherit the card's
        # background the way a CTkLabel's transparent canvas did.
        self._card_slot = {}
        # The cards themselves, kept and refilled instead of destroyed and
        # rebuilt. One screenful costs ~600ms to build and ~300ms to destroy, and
        # a view switch, a sort change or a filter does both - which is the stall
        # that reads as the window lagging. Three index-aligned lists:
        # `_pool_gid[i]` names the game `_card_pool[i]` currently shows, and None
        # means the slot is hidden. See _sync_cards for the ordering invariant
        # that makes this safe with pack().
        self._card_pool = []
        self._pool_gid = []
        # Set when what changed is not the row itself - a renamed tag, say -
        # because the skip-unchanged check below compares game ids.
        self._pool_dirty = False
        # card frame -> game id. The click handler looks the id up here at click
        # time; capturing the game in the binding instead would pin every card to
        # whatever it showed when it was first built.
        self._widget_gid = {}
        self._empty_label = None
        self._status_btns = {}    # status key -> its button in the detail panel
        self._star_btns = []      # the five rating buttons, index 0 == one star
        self._pager = None        # the pager bar, built lazily on first render
        self._pager_parts = None  # its children, blanked with it on teardown
        self._search_after = None
        # Overview translations in flight, by game id. Deliberately separate
        # from self.busy: translating one description must not lock out syncing.
        self._ov_inflight = set()
        # The name has its own set because it has its own thread: sharing one
        # flag meant a still-running name request blocked the overview forever
        # after a failure.
        self._title_inflight = set()
        self._ov_label = None     # the overview text widget currently on screen
        self._ov_seg = None
        self._title_label = None  # the name at the top of the detail panel
        self._title_note = None   # why that name is still English, if it is
        # The detail panel's widgets, built once and repointed at each game.
        # None means "nothing is built", which is the state after a theme switch
        # and while no game is selected.
        self._detail_parts = None
        self._detail_shown = None  # which conditional blocks are currently up
        self._tag_chips = []
        self._title_entry = None
        self._ov_text = None
        self._tag_editor = None   # the open 标签译名 dialog, if there is one
        self._settings_status = None
        self._about_status = None
        self.tag_btn = None
        self._engine_seg = None   # the settings dialog's engine switch
        # What refresh() last put on screen, so a sync that changed nothing
        # visible can skip rebuilding ~480 widgets.
        self._rendered_ids = []
        # Ties the filter bar's contents to the filters themselves instead of to
        # "a refresh happened".
        self._filter_sig = None
        # The same trick for the detail panel: what it is showing, rather than
        # whether a refresh ran.
        self._detail_sig = None
        # Background work calls back here from a thread; the refresh itself has
        # to happen on the tk thread, and rebuilding ~480 widgets is far too
        # expensive to do once per downloaded cover.
        self._last_refresh = 0.0
        self._stop = threading.Event()
        # The newer release the last check found, kept so a theme switch can put
        # the notice back on the sidebar it just rebuilt.
        self._update_found = None
        self._update_url = slg_update.RELEASES_URL
        self._update_notes = ""
        self._update_dialog_shown = False

        if notify:
            self._splash.step("正在构建界面…", 0.55)
            self.update_idletasks()
        self._build()
        if notify:
            self._splash.step("正在渲染卡片…", 0.85)
            self.update_idletasks()
        self.refresh()
        self.after(120, self._drain)
        # Armed unconditionally: switching to "跟随系统" later has to start being
        # watched too, and the poll is a no-op while another mode is selected.
        self.after(5000, self._poll_system)
        # notify is off for the packaging smoke test: the dialog is modal and
        # would sit there blocking the mainloop it is meant to be checking.
        if notify and not slg_db.get_pref(self.conn, PREF_WELCOME_SEEN):
            self.after(300, self._show_welcome)
        if notify and not slg_db.get_pref(self.conn, PREF_FREE_NOTICE):
            self.after(600, self._show_free_notice)
        # Late enough that it never delays the window appearing, and skipped
        # entirely by the smoke test, which is not a user session.
        if notify:
            self.after(3000, self._start_update_check)

        if notify:
            # The bar is a real progress bar for three stages and then a fade;
            # landing it on 100% first is the difference between "finished" and
            # "gave up three quarters of the way".
            self._splash.step("准备就绪", 1.0)
            self._finish_splash()

    def _finish_splash(self):
        """Cross-fade the splash out and the window in, then drop the splash."""
        if self._splash is None:
            return
        self.attributes("-alpha", 0.0)
        self.deiconify()
        self._fade_splash(1)

    def _fade_splash(self, step, total=10):
        splash = self._splash
        if splash is None:
            self.attributes("-alpha", 1.0)
            return
        frac = step / total
        try:
            self.attributes("-alpha", frac)
            splash.attributes("-alpha", 1.0 - frac)
        except tk.TclError:
            return
        if step >= total:
            self._splash = None
            splash.destroy()
            self.attributes("-alpha", 1.0)
            return
        self.after(24, self._fade_splash, step + 1, total)

    # --- theme ----------------------------------------------------------------

    def _apply_theme(self, mode):
        """Switch palette and repaint the whole window.

        Everything is rebuilt rather than recoloured in place: CTk bakes colours
        into the internals it creates for a widget (the scrollable canvas, an
        entry's placeholder, an option menu's popup, the scrollbars), so walking
        the tree with configure() misses more than it catches.
        """
        slg_db.set_pref(self.conn, PREF_THEME, mode)
        self.theme_mode = mode
        ctk.set_appearance_mode(mode)
        apply_palette(resolved_theme(mode))
        self._sys_theme = resolved_theme(mode)
        # Not just tidying: every fg_color="transparent" in the window resolves
        # by walking up the parent chain to the nearest opaque ancestor, and the
        # chain ends here. Leaving the root at its launch colour kept the whole
        # toolbar row - search box included - in the old palette while the
        # opaque sidebar and cards switched correctly.
        self.configure(fg_color=BG)

        selected_id = self.selected["id"] if self.selected else None
        if self._search_after is not None:
            self.after_cancel(self._search_after)
            self._search_after = None

        self._teardown_ui()
        # _image_cache is left alone on purpose. Only the placeholder covers are
        # palette-dependent, and their cache key carries the colour, so they
        # invalidate themselves; clearing everything here re-decoded every
        # downloaded thumbnail, which is the bulk of the work of a repaint.
        self._build()
        self._repaint_update_notice()
        if self.busy:
            self._start_job(self._job_label)
        self.selected = None
        if selected_id is not None:
            fresh = next((g for g in self.rows if g["id"] == selected_id), None)
            self.selected = dict(fresh) if fresh is not None else None
        self.refresh()

    def _teardown_ui(self):
        for child in self.winfo_children():
            # An open dialog is a child of the root as well, so destroying
            # everything closed the help document, the settings dialog and the
            # tag editor out from under the user - silently, and automatically
            # every time the OS flipped theme in 跟随系统 mode. They keep the
            # palette they were built with, which is a shade out of date and far
            # better than gone.
            if isinstance(child, ctk.CTkToplevel):
                continue
            child.destroy()
        # Every one of these pointed at a widget that no longer exists. Leaving
        # them set is how a background repaint turns into a TclError on a dead
        # frame, so blank them all before anything can run.
        self._cards = {}
        self._card_meta = {}
        self._card_title = {}
        self._card_slot = {}
        # The pool held widgets under the old root, so it dies with them. Left
        # set, the next _sync_cards would hand back dead frames.
        self._card_pool = []
        self._pool_gid = []
        self._pool_dirty = False
        self._widget_gid = {}
        self._empty_label = None
        # The bar itself is gone, so the signature no longer describes what is
        # on screen: leaving it set would skip drawing the chips entirely.
        self._filter_sig = None
        self._detail_sig = None
        self._rendered_ids = []
        self._pager = self._pager_parts = None
        self.filterbar = self.filterbar_inner = None
        self.list = self.detail = None
        self._pager_holder = None
        self._ov_label = self._ov_seg = self._title_label = None
        self._title_note = None
        self._status_btns = {}
        self._star_btns = []
        # The skeleton held widgets under the old root; the next _render_detail
        # has to build a new one rather than configure these.
        self._detail_parts = None
        self._detail_shown = None
        self._tag_chips = []
        self._title_entry = self._ov_text = None
        # _tag_editor / _settings_status / tag_btn are deliberately left alone:
        # their widgets are inside dialogs, and those now survive the rebuild.
        # Blanking them here would leave a live window whose buttons had nothing
        # to write through.
        self.view_buttons = {}
        self.stat_label = self.progress_label = self.progress_bar = None
        self.search_entry = self.settings_btn = None
        self.sync_btn = self.maintenance_btn = None
        self.update_label = None
        self.qq_btn = None
        self.profile_btn = None

    def _poll_system(self):
        """"system" has no callback to hang off, so sample the OS setting.

        get_appearance_mode() re-reads it every call; the delay is up to one
        tick, and a real change costs a full rebuild (~480 widgets).
        """
        if self.theme_mode == "system":
            now = resolved_theme("system")
            if now != self._sys_theme:
                self._apply_theme("system")
        self.after(5000, self._poll_system)

    def _show_free_notice(self):
        messagebox.showinfo(
            "完全免费",
            "本软件完全免费。\n\n"
            "没有收费版、没有付费激活、没有隐藏收费入口。\n"
            "如果你是通过付费渠道拿到它的，请立即举报。",
            parent=self)
        slg_db.set_pref(self.conn, PREF_FREE_NOTICE, "1")

    def _show_welcome(self):
        """First-launch onboarding, reachable again from 设置.

        The one thing a brand-new user cannot guess is *why* the app is worth
        three clicks - that it learns their xp from the star ratings and pushes
        matching games. This says so up front, and the pref gate above only lets
        it pop on the first run; opening it from 设置 never re-arms the gate.
        """
        slg_db.set_pref(self.conn, PREF_WELCOME_SEEN, "1")
        win = self._new_dialog("欢迎使用 SLG黄游大王", "540x600")
        ctk.CTkLabel(win, text="欢迎使用 SLG黄游大王", text_color=TEXT,
                     font=ui_font(size=18, weight="bold")).pack(pady=(18, 2))
        ctk.CTkLabel(
            win, text="免费 · 帮你整理检索 dikgames 游戏，并学习你的 xp 口味、推送对口游戏。",
            text_color=MUTED, font=ui_font(size=12), justify="left", anchor="w",
            wraplength=480).pack(fill="x", padx=24, pady=(0, 12))

        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        def _section(title, text):
            ctk.CTkLabel(body, text=title, text_color=ACCENT, anchor="w",
                         font=ui_font(size=14, weight="bold")).pack(
                fill="x", padx=8, pady=(10, 2))
            ctk.CTkLabel(body, text=text, text_color=TEXT, anchor="w",
                         justify="left", wraplength=460,
                         font=ui_font(size=12)).pack(fill="x", padx=8, pady=(0, 4))

        _section("它是什么",
                 "把 dikgames 的全部游戏抓进本地，可搜索、可按标签筛选、可标记想玩/已下载。")
        _section("它会学习你的 xp",
                 "给玩过的游戏打 1–5 星，软件会从你喜欢/不喜欢的标签、开发商和引擎里，"
                 "学出你的口味——这是它和普通游戏列表最大的区别。")
        _section("按 xp 推荐",
                 "顶栏「按xp推荐」排序会把最对你胃口的游戏排到最前，越用越准。")

        ctk.CTkLabel(body, text="三步上手", text_color=ACCENT, anchor="w",
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=8, pady=(14, 2))
        steps = (
            "第 1 步　点「更新游戏数据」从服务器拉取游戏目录。",
            "第 2 步　点开任意游戏，在右侧点星星打分。",
            "第 3 步　用标签筛选 + 「按xp推荐」找新游戏。",
        )
        for s in steps:
            ctk.CTkLabel(body, text=s, text_color=TEXT, anchor="w", justify="left",
                         wraplength=460, font=ui_font(size=12)).pack(
                fill="x", padx=8, pady=1)
        ctk.CTkLabel(body, text="以后想再看这份说明，点右上角 ⚙ → 查看新手引导。",
                     text_color=MUTED, anchor="w", justify="left", wraplength=460,
                     font=ui_font(size=11)).pack(fill="x", padx=8, pady=(12, 6))

        ctk.CTkButton(win, text="开始使用", height=38, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD,
                      font=ui_font(size=14), command=win.destroy).pack(
            fill="x", padx=24, pady=(0, 16))

    def _set_progress(self, text, frac=None):
        """progress_label is recreated on a theme switch, so guard the write.

        The drain loop and the worker callbacks both land here, and they can
        fire between the teardown and the rebuild. frac, when given, drives the
        progress bar so it tracks the real per-game count instead of a fake
        sweep.
        """
        label = self.progress_label
        if label is not None and label.winfo_exists():
            label.configure(text=text[:60])
        if frac is not None:
            bar = self.progress_bar
            if bar is not None and bar.winfo_exists():
                bar.set(frac)

    def _ui(self, widget, **kwargs):
        """configure() a widget that may no longer exist.

        Same reason as _set_progress, for the sidebar's buttons: a theme switch
        rebuilds them and a job started before the switch finishes after it, so
        the finish has to reach whichever button is there now - or none.
        """
        if widget is not None and widget.winfo_exists():
            widget.configure(**kwargs)

    # --- layout ---------------------------------------------------------------

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_sidebar()

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(0, 14), pady=14)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        self._build_toolbar(main)
        self._build_filterbar()

        body = ctk.CTkFrame(main, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        self.list = ctk.CTkScrollableFrame(body, fg_color="transparent")
        self.list.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.list.grid_columnconfigure(0, weight=1)

        # The pager is a sibling of the list, not a child, so it stays pinned to
        # the bottom of the column. Packed inside the list it would sit after
        # the cards in the scroll region, and with the covers at their current
        # size the sixth row plus the bar is already past the fold - the user
        # would have to scroll to turn the page, which is the one thing
        # pagination is for. Row 1 has weight 0, so the list keeps everything
        # the pager does not use.
        self._pager_holder = ctk.CTkFrame(body, fg_color="transparent")
        self._pager_holder.grid(row=1, column=0, sticky="ew", padx=(0, 12),
                                pady=(8, 0))
        self._pager_holder.grid_columnconfigure(0, weight=1)

        self.detail = ctk.CTkScrollableFrame(body, fg_color=CARD, corner_radius=12)
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.detail.grid_columnconfigure(0, weight=1)

        # Below the panel, not inside it. The panel is a description of one game
        # and scrolls; the group is about the app and must not scroll away with
        # it. Row 1 column 1 is the mirror of the pager's cell, so the two share
        # a top edge and the bottom strip reads as one line across the window.
        self._build_qq_footer(body)

    def _build_qq_footer(self, body):
        """The 交流群 copy button, level with the pager under the panel.

        Built in _build rather than _build_detail so it exists once for the
        session instead of once per selected game - and so a theme switch, which
        rebuilds the whole window, leaves self.qq_btn pointing at the new button
        rather than a deleted one.
        """
        self.qq_holder = ctk.CTkFrame(body, fg_color="transparent")
        self.qq_holder.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        self.qq_holder.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(self.qq_holder, fg_color=CARD, corner_radius=8)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.qq_btn = ctk.CTkButton(
            bar, text=QQ_GROUP_COPY_LABEL, height=30, corner_radius=6,
            anchor="w", fg_color="transparent", text_color=ACCENT,
            hover_color=CARD_HOVER, font=ui_font(size=12),
            command=lambda: self._copy_value(
                QQ_GROUP, self.qq_btn, QQ_GROUP_COPY_LABEL))
        self.qq_btn.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

    def _build_sidebar(self):
        """Four bands, packed in priority order rather than reading order.

        pack() hands out space in call order, so the first band packed is the
        one that survives a short window. What that means here: the header and
        the footer - the two things that have nowhere else to go - are packed
        before the navigation list, and the list absorbs the slack and scrolls
        when there is none. The footer used to be packed last of everything,
        which made the GitHub link (the last widget in it) the very first thing
        squeezed to zero height at the window's default size.
        """
        bar = ctk.CTkFrame(self, fg_color=SIDEBAR, corner_radius=0, width=190)
        bar.grid(row=0, column=0, rowspan=2, sticky="nsw")
        bar.grid_propagate(False)

        header = ctk.CTkFrame(bar, fg_color="transparent")
        header.pack(side="top", fill="x")
        ctk.CTkLabel(header, text="", image=load_avatar(64)).pack(pady=(18, 6))
        ctk.CTkLabel(header, text=APP_TITLE, text_color=TEXT,
                     font=ui_font(size=19, weight="bold")).pack(pady=(0, 2),
                                                                   padx=18)
        ctk.CTkLabel(header, text="作者 · %s" % AUTHOR, text_color=MUTED,
                     font=ui_font(size=11)).pack(padx=18)
        ctk.CTkLabel(header, text=APP_VERSION_LABEL, text_color=MUTED,
                     font=ui_font(size=10)).pack(padx=18, pady=(1, 0))
        # Where a person looks for who to tell about a bug. A button here would
        # outweigh the two lines around it, so it is a label with the same
        # hand cursor and click binding update_label uses.
        mail = ctk.CTkLabel(header, text="反馈：%s" % CONTACT_EMAIL, text_color=MUTED,
                            font=ui_font(size=10), wraplength=166, cursor="hand2")
        mail.pack(padx=18, pady=(4, 0))
        mail.bind("<Button-1>", lambda e: webbrowser.open(CONTACT_MAILTO))
        ctk.CTkLabel(header, text="求个 GitHub star，也欢迎推荐给朋友",
                     text_color=MUTED, font=ui_font(size=10),
                     wraplength=166).pack(padx=18)

        self.stat_label = ctk.CTkLabel(header, text="", text_color=MUTED,
                                       font=ui_font(size=12))
        self.stat_label.pack(pady=(6, 4))
        # Separate from stat_label because refresh() rewrites that one with the
        # catalogue counts - and refresh() now runs *during* a sync, which
        # would otherwise wipe the progress text every couple of seconds.
        self.progress_label = ctk.CTkLabel(header, text="", text_color=ACCENT,
                                           font=ui_font(size=11),
                                           wraplength=166, justify="left")
        self.progress_label.pack(pady=(0, 10))

        # A thin indeterminate bar, animated only while a job runs. set(0) is
        # invisible, so it sits here idle without adding a flickering empty row.
        self.progress_bar = ctk.CTkProgressBar(
            header, height=4, corner_radius=2, fg_color=CHIP,
            progress_color=ACCENT)
        self.progress_bar.pack(fill="x", padx=18, pady=(0, 10))
        self.progress_bar.set(0)

        # Built here, packed only when a check finds something. The sidebar is
        # the one column that has already been squeezed to zero once, so this
        # goes in with an explicit after= rather than by re-packing the header.
        self.update_label = ctk.CTkLabel(
            header, text="", text_color=ACCENT, font=ui_font(size=11),
            wraplength=166, justify="left", cursor="hand2")
        self.update_label.bind("<Button-1>", self._open_update_page)

        # Identity and the free-software warning, pinned to the bottom before
        # anything that can grow gets a say.
        footer = ctk.CTkFrame(bar, fg_color="transparent")
        footer.pack(side="bottom", fill="x")
        ctk.CTkFrame(footer, height=1, fg_color=CHIP, corner_radius=0).pack(
            fill="x", padx=12, pady=(0, 8))
        _link_button(footer, GITHUB_LABEL, GITHUB_URL).pack(
            fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(footer, text="本软件完全免费\n如遇收费请立即举报",
                     text_color=DANGER_TEXT, font=ui_font(size=11),
                     wraplength=166, justify="left").pack(
            fill="x", padx=18, pady=(0, 10))

        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.pack(side="bottom", fill="x")
        # Two buttons, not four. The chores behind 更多… all reach the
        # same games as the sitemap sync - the cover download at a hundred
        # times the cost - and as a column of four differently-sized buttons
        # they gave a new user no way to tell which one they wanted. Sync stays
        # out here because it is the one that is actually routine.
        self.sync_btn = ctk.CTkButton(actions, text="更新游戏数据", height=38,
                                      corner_radius=8, fg_color=ACCENT,
                                      command=self.do_sync)
        self.sync_btn.pack(fill="x", padx=12, pady=(0, 6))
        # Same shape and height as 同步, transparent instead of filled: the
        # difference between the two is meant to be the only thing that reads
        # as a difference.
        self.maintenance_btn = ctk.CTkButton(
            actions, text="更多…", height=38, corner_radius=8,
            fg_color="transparent", text_color=TEXT, hover_color=CARD,
            command=self.open_maintenance)
        self.maintenance_btn.pack(fill="x", padx=12)

        nav = ctk.CTkScrollableFrame(bar, fg_color="transparent")
        nav.pack(side="top", fill="both", expand=True)

        _section(nav, "浏览")
        self.view_buttons = {}
        for label, status in VIEWS:
            self.view_buttons[status] = _nav_button(
                nav, label, lambda s=status: self.set_view(s),
                # CARD for the active view, so a theme rebuild does not come back
                # with the highlight missing.
                active=(status == self.view), height=36, size=14)
        self.user_view_btn = _nav_button(
            nav, "我添加的游戏", self.set_user_view, height=36, size=14)

        colbox = ctk.CTkFrame(nav, fg_color="transparent")
        colbox.pack(fill="x", padx=12, pady=(6, 2))
        self.collection_menu = ctk.CTkOptionMenu(
            colbox, values=["收藏夹"], height=34, corner_radius=8,
            fg_color=SIDEBAR, text_color=TEXT, button_color=CHIP,
            button_hover_color=CARD_HOVER, font=ui_font(size=13),
            command=self._on_collection)
        self.collection_menu.pack(fill="x")
        # Hidden until a specific collection is chosen: it deletes the one the
        # dropdown is currently on, which is meaningless for the 收藏夹 (all).
        self.delete_collection_btn = ctk.CTkButton(
            colbox, text="删除此收藏夹", height=28, corner_radius=8, anchor="w",
            fg_color="transparent", text_color=DANGER_TEXT, hover_color=CHIP,
            font=ui_font(size=12), command=self._delete_selected_collection)
        self._refresh_collection_menu()

        _rule(nav)
        _section(nav, "工具")
        # 标签库 stays out here as the one routine filter; 添加我的游戏… maps
        # straight onto the sidebar's 我添加的游戏 view. The rest (检查更新 and
        # the set-once-then-forget ones) live behind 更多工具…, and the toolbar
        # gear opens 设置.
        for text, command in (("标签库…", self.open_tag_picker),
                              ("添加我的游戏…", self.open_add_game),
                              ("更多工具…", self.open_tools)):
            _nav_button(nav, text, command)
        _nav_button(nav, "帮助文档", self.open_help, pady=(2, 10))

    def _copy_value(self, value, button=None, restore=None):
        """Put `value` on the clipboard, and say so on the button that did it.

        The flash rather than a toast: every caller is already a button, and a
        label appearing somewhere else in the window is easy to miss when the
        click itself produced no other visible change.

        `winfo_exists` guards the restore - a theme switch rebuilds the toolbar
        and the dialogs wholesale, so the callback can outlive its own button.
        """
        self.clipboard_clear()
        self.clipboard_append(value)
        if button is None or not button.winfo_exists():
            return
        # √, not ✓: the check mark is an empty box in 雅黑 and 宋体 alike.
        button.configure(text="已复制 √")

        def restore_text():
            if button.winfo_exists():
                button.configure(text=restore)

        self.after(1200, restore_text)

    def _build_toolbar(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.toolbar = bar

        # The "搜索" caption is gone and the text is centred: the caption only
        # pushed the placeholder into the left edge, and with the whole row to
        # itself the box says what it is without it.
        #
        # 38 is the sort controls' own height: the box no longer stretches to
        # fill the row, so it reads as a search field rather than a banner, and
        # the gap above the notice band below keeps the two from touching.
        self.search_entry = ctk.CTkEntry(
            bar, placeholder_text="搜索游戏名…", height=38, corner_radius=8,
            fg_color=CARD, text_color=TEXT, placeholder_text_color=MUTED,
            border_width=1, border_color=CHIP, font=ui_font(size=13),
            justify="center")
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_entry.bind("<KeyRelease>", self._on_search)

        # Sort field, sort direction and 设置, all on one line and all the same
        # height. The "排序" caption that used to open this row is gone: the
        # dropdown reads as a sort control without a label, and the room the
        # label took is where the gear sits now that it no longer shares a row
        # with a theme switch.
        #
        # sticky="e" without n/s is load-bearing: it centres the three on the
        # search box's height instead of stretching them with it.
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=1, padx=(20, 0), sticky="e")

        self.sort_menu = ctk.CTkOptionMenu(
            right, values=["按xp推荐", "站内评分", "最近更新", "名称", "热度"], width=116,
            height=38, corner_radius=8, fg_color=CARD, text_color=TEXT,
            button_color=CHIP, button_hover_color=CARD_HOVER,
            command=self._on_sort)
        self.sort_menu.pack(side="left")
        self.sort_dir_btn = ctk.CTkButton(
            right, text=SORT_ARROW[self.sort_desc], width=78, height=38,
            corner_radius=8, fg_color=CARD, text_color=TEXT,
            hover_color=CARD_HOVER, font=ui_font(size=15),
            command=self._toggle_sort_dir)
        self.sort_dir_btn.pack(side="left", padx=(6, 0))
        # 个人 sits in the reserved empty cell directly below the sort cluster
        # (bar row 1, col 1), vertically centred with the VPN notice band on the
        # left. Kept out of `right` so the sort/gear row stays one clean line.
        self.profile_btn = ctk.CTkButton(
            bar, text="个人", width=64, height=38, corner_radius=8,
            fg_color=CARD, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=13), command=self.open_profile)
        self.profile_btn.grid(row=1, column=1, sticky="e",
                              padx=(20, 0), pady=(9, 0))
        # The gear lands on 设置, not 关于: the theme switch lives in there now,
        # and 关于 is the first row inside it. Same width as the button it
        # replaced, taller to match the two controls beside it.
        self.settings_btn = ctk.CTkButton(
            right, text="⚙", width=38, height=38, corner_radius=8,
            fg_color=CARD, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=14), command=self.open_settings)
        self.settings_btn.pack(side="left", padx=(6, 0))

        # Permanent and deliberately not dismissible. It lives inside the
        # toolbar rather than in a row of its own on `main` so that a theme
        # switch rebuilds it along with everything else, and so the filter bar
        # and body keep their row numbers.
        #
        # One line, not two: the second sentence was the same warning said twice
        # in a box the width of the window, and it now sits with the other
        # disclaimers at the foot of the detail panel - the one place a reader
        # goes looking for "what is this site not promising me".
        # The tinted band still spans the window - a card that shrank to the
        # text would jump sideways every time the wording changed - but the two
        # things inside it sit together on the centre axis instead of being
        # pushed to opposite ends. The stretch of empty blue between them was
        # the whole reason this row looked wrong.
        # A small top gap separates this band from the search box above: with the
        # box back at the sort row's height, the band no longer has to sit flush
        # against it to read as the toolbar's own bottom edge.
        self.vpn_notice = ctk.CTkFrame(bar, fg_color=CHIP, corner_radius=8)
        self.vpn_notice.grid(row=1, column=0, columnspan=1, sticky="ew",
                             pady=(8, 0))
        inner = _centred_row(self.vpn_notice, row=0, column=0, sticky="ew")
        ctk.CTkLabel(inner, text="建议开启梯子（VPN / 代理）后使用本软件",
                     text_color=TEXT, font=ui_font(size=12)
                     ).pack(side="left", padx=(0, 10), pady=6)
        ctk.CTkButton(inner, text=SITE_LABEL, height=28, corner_radius=6,
                      fg_color="transparent", text_color=ACCENT,
                      hover_color=CARD_HOVER, font=ui_font(size=12),
                      command=lambda: webbrowser.open(SITE_URL)
                      ).pack(side="left", pady=6)

    def _on_theme_pick(self, label):
        mode = next(m for m, text in _THEME_LABELS.items() if text == label)
        # Deferred: this runs from inside the segmented button's own press
        # handler, and the switch destroys that button.
        self.after(1, lambda: self._apply_theme(mode))

    def _build_filterbar(self):
        # Lives inside the toolbar's column 0 (the search box column), not on
        # `main`: that way `_centred_row` centres the 筛选 row on the same axis
        # as the search box and the notice strip, which sit in that same column
        # while the sort controls take the column to their right.
        self.filterbar = ctk.CTkFrame(self.toolbar, fg_color="transparent")
        self.filterbar.grid(row=2, column=0, columnspan=1, sticky="ew",
                            pady=(12, 10))
        self.filterbar_inner = _centred_row(self.filterbar, row=0, column=0,
                                           sticky="ew")

    def _render_filterbar(self):
        # The chips are a function of the filters and nothing else, so a refresh
        # for an unrelated reason (a sync tick, a status write) rebuilding them
        # was pure flicker.
        signature = (tuple(self.include), tuple(self.exclude))
        if signature == self._filter_sig:
            return
        self._filter_sig = signature
        bar = self.filterbar_inner
        for child in bar.winfo_children():
            child.destroy()
        ctk.CTkLabel(bar, text="筛选", text_color=MUTED,
                     font=ui_font(size=13)).pack(side="left", padx=(0, 6))
        if not self.include and not self.exclude:
            # A button, not a label: it was telling the user about 标签库 while
            # being the one thing in the row that could not open it.
            ctk.CTkButton(bar, text="未设置 — 点这里挑标签",
                          height=24, corner_radius=12, fg_color="transparent",
                          text_color=ACCENT, hover_color=CHIP,
                          font=ui_font(size=12),
                          command=self.open_tag_picker).pack(side="left")

        def chip(slug, excluded):
            label = ("× " if excluded else "") + display_tag(slug)
            btn = ctk.CTkButton(
                bar, text=label, height=26, corner_radius=13,
                fg_color=CHIP_OFF if excluded else CHIP,
                text_color=DANGER_TEXT if excluded else TEXT,
                hover_color=CARD_HOVER,
                font=ui_font(size=12),
                command=lambda s=slug, e=excluded: self._drop_chip(s, e))
            btn.pack(side="left", padx=3)

        for slug in self.include:
            chip(slug, False)
        for slug in self.exclude:
            chip(slug, True)
        if self.include or self.exclude:
            ctk.CTkButton(bar, text="清空", width=54, height=26,
                          corner_radius=13, fg_color="transparent", text_color=ACCENT,
                          hover_color=CHIP, font=ui_font(size=12),
                          command=self.clear_filters).pack(side="left", padx=(10, 0))

    # --- state changes --------------------------------------------------------

    def set_view(self, status):
        self.view = status
        self.origin = "main"
        self.page = 1
        for key, btn in self.view_buttons.items():
            btn.configure(fg_color=CARD if key == status else "transparent")
        self._paint_user_view()
        self.refresh()

    def set_user_view(self):
        """Show only the games the user added themselves."""
        self.view = None
        self.origin = "user"
        self.collection_id = None
        self.page = 1
        for key, btn in self.view_buttons.items():
            btn.configure(fg_color="transparent")
        self._refresh_collection_menu()
        self._sync_delete_collection_btn()
        self._paint_user_view()
        self.refresh()

    def _paint_user_view(self):
        btn = getattr(self, "user_view_btn", None)
        if btn is not None and btn.winfo_exists():
            btn.configure(fg_color=CARD if self.origin == "user" else "transparent")

    def _on_collection(self, name):
        cols = slg_db.list_collections(self.conn)
        self.collection_id = next((c["id"] for c in cols if c["name"] == name), None)
        self.origin = "main"
        self.page = 1
        self._sync_delete_collection_btn()
        self._paint_user_view()
        self.refresh()

    def _refresh_collection_menu(self):
        """Repopulate the 收藏夹 dropdown, keeping the current choice."""
        menu = getattr(self, "collection_menu", None)
        if menu is None or not menu.winfo_exists():
            return
        cols = slg_db.list_collections(self.conn)
        names = ["收藏夹"] + [c["name"] for c in cols]
        by_id = {c["id"]: c["name"] for c in cols}
        menu.configure(values=names)
        menu.set(by_id.get(self.collection_id, "收藏夹"))
        self._sync_delete_collection_btn()

    def _sync_delete_collection_btn(self):
        """Show the 删除此收藏夹 row only when a specific collection is on."""
        btn = getattr(self, "delete_collection_btn", None)
        if btn is None or not btn.winfo_exists():
            return
        if self.collection_id is not None:
            btn.pack(fill="x", pady=(4, 0))
        else:
            btn.pack_forget()

    def _delete_selected_collection(self):
        if self.collection_id is not None:
            self._delete_collection(self.collection_id)

    def _on_search(self, event):
        # Rebuilding the list on every keystroke is what made typing feel like
        # dragging. Read the box now, but only re-query once the typing pauses.
        self.search = event.widget.get().strip()
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(300, self._apply_search)

    def _apply_search(self):
        self._search_after = None
        self.page = 1
        self.refresh()

    def _on_sort(self, label):
        self.sort = SORT_FIELDS[label]
        # A new field opens in its own natural direction rather than inheriting
        # the previous field's flip.
        self.sort_desc = SORT_DEFAULT_DESC[self.sort]
        self._paint_sort_dir()
        self.page = 1
        self.refresh()

    def _toggle_sort_dir(self):
        self.sort_desc = not self.sort_desc
        self._paint_sort_dir()
        self.page = 1
        self.refresh()

    def _paint_sort_dir(self):
        self.sort_dir_btn.configure(text=SORT_ARROW[self.sort_desc])

    def _drop_chip(self, slug, excluded):
        (self.exclude if excluded else self.include).remove(slug)
        self.page = 1
        self.refresh()

    def clear_filters(self):
        self.include, self.exclude = [], []
        self.page = 1
        self.refresh()

    def toggle_tag(self, slug, exclude=False):
        target, other = ((self.exclude, self.include) if exclude
                         else (self.include, self.exclude))
        if slug in target:
            target.remove(slug)
        else:
            target.append(slug)
            if slug in other:
                other.remove(slug)
        self.page = 1
        self.refresh()

    # --- rendering ------------------------------------------------------------

    def refresh(self, preserve_scroll=False, skip_if_same=False):
        """Rebuild the whole list. For filter/sort/data changes only.

        Selecting a card does NOT come through here - rebuilding a screenful of
        CTk widgets to recolour two of them was the multi-second stall. The two
        flags exist for the one caller that is neither: the sync's periodic
        tick, which fires every few seconds whether or not anything the user
        can see has changed.

        find_games returns the whole result set and the page is sliced out of
        it below, which is O(the catalogue) per refresh - single-digit
        milliseconds at the few thousand rows this holds, and fine an order of
        magnitude past that. Keeping self.rows whole is what lets the four
        other readers of it (re-finding the selection by id, dropping a card,
        resolving a click, counting pages) go on meaning what they meant.
        """
        self._render_filterbar()
        self.rows = slg_db.find_games(
            self.conn, include=self.include, exclude=self.exclude,
            search=self.search or None,
            statuses=[self.view] if self.view else None,
            downloaded_only=False, collection_id=self.collection_id,
            origin=self.origin, sort=self.sort, desc=self.sort_desc)
        # A filter or a sort handler sets page 1 already; this is for the other
        # way the set can shrink - the tick after a sync emptied the tail, or a
        # drop that took the last row off the last page.
        self._clamp_page()

        # The row objects are replaced wholesale, so the old selection would
        # keep showing pre-edit values (a status button that never lights up).
        # Copied into a dict: find_games hands back sqlite3.Row, which is
        # read-only, and the in-place status/rating writes need to put the new
        # value back into this object.
        if self.selected is not None:
            sid = self.selected["id"]
            fresh = next((r for r in self.rows if r["id"] == sid), None)
            if fresh is not None:
                self.selected = dict(fresh)

        visible = [g["id"] for g in self._page_slice()]
        if skip_if_same and visible == self._rendered_ids:
            # A sync added rows on another page, or only touched columns nothing
            # on screen reads. Destroying and redrawing every card to arrive at
            # the same picture is what made the list stutter. The detail panel is
            # left alone too - rebuilding it would throw away the user's place in
            # it (and reset the 原文/中文 switch) every tick.
            #
            # The pager is not left alone: this branch is reached precisely when
            # rows were appended past the fold, which can add a whole page, and a
            # 第 1 / 3 页 that never moves is the one thing on screen that is now
            # wrong. It is configure-only, so it does not undo the point of the
            # shortcut.
            self._render_pager()
            self._render_stats()
            return

        offset = self._scroll_offset() if preserve_scroll else None
        self._sync_cards(self._page_slice())
        self._render_pager()
        self._set_empty_label(not self.rows)
        self._rendered_ids = visible
        if offset is not None:
            self._restore_scroll(offset)

        self._render_stats()
        self._render_detail_if_stale()

    def _render_stats(self):
        stats = slg_db.stats(self.conn)
        gaps = slg_db.data_gaps(self.conn)
        text = ("%d 款 · %d 标签\n评分 %d 款 · 已下载 %d 款"
                % (stats["games"], stats["tags"], stats["rated"],
                   stats["downloaded"]))
        # Only while there is something to report. "简介缺 0 款" forever would be
        # noise in the one column with no room to spare, and the number is the
        # only thing that tells the user why a panel is showing the placeholder
        # and roughly how many syncs are left.
        if gaps["overview"]:
            text += "\n简介缺 %d 款（同步补齐）" % gaps["overview"]
        self.stat_label.configure(text=text)
        # The count rides on the door rather than on the button behind it: the
        # dialog is rebuilt on every open, so a number cached in a closed window
        # would be the one place the user cannot read it.
        self.maintenance_btn.configure(
            text="更多…  ·  封面待下 %d" % gaps["covers"] if gaps["covers"]
            else "更多…")

    def _scroll_offset(self):
        """Where the list is scrolled to, or None if that cannot be read.

        CTkScrollableFrame keeps its canvas private; the attribute is read
        defensively so a customtkinter release that renames it degrades to
        "scroll jumps to the top" instead of taking the refresh down.
        """
        canvas = getattr(self.list, "_parent_canvas", None)
        if canvas is None:
            return None
        try:
            return canvas.yview()[0]
        except Exception:  # noqa: BLE001 - see above
            return None

    def _restore_scroll(self, offset):
        # after_idle: the scrollregion only covers the new cards once the
        # geometry pass that follows this render has run.
        self.after_idle(lambda: self._apply_scroll(offset))

    def _apply_scroll(self, offset):
        canvas = getattr(self.list, "_parent_canvas", None)
        if canvas is not None:
            try:
                canvas.yview_moveto(offset)
            except Exception:  # noqa: BLE001 - a stale canvas must not crash
                pass

    def _invalidate_cards(self):
        """Make the next sync refill every card, not just the changed ones.

        The skip is keyed on the game id, which is the right test for a filter
        or a sort. It is the wrong test for anything stored per *tag* rather
        than per game: renaming a tag leaves every row's id exactly where it
        was, so without this the cards would keep the old name on screen.
        """
        self._pool_dirty = True

    def _reset_card_index(self):
        # Rebuilt from the pool rather than maintained incrementally: eighty
        # dict entries cost microseconds, and an incrementally-updated index is
        # how a hidden card ends up still answering for its game id.
        self._cards = {}
        self._card_meta = {}
        self._card_title = {}
        self._card_slot = {}
        self._widget_gid = {}
        for gid, slot in zip(self._pool_gid, self._card_pool):
            if gid is None:
                continue
            self._cards[gid] = slot["frame"]
            self._card_meta[gid] = slot["meta"]
            self._card_title[gid] = slot["title"]
            self._card_slot[gid] = slot
            self._widget_gid[slot["frame"]] = gid

    def _sync_cards(self, games):
        """Show exactly `games`, reusing the cards already on screen.

        The invariant that keeps pack() honest: pool index i always holds
        games[i]. So the slots to hide are always a *suffix*, and slots coming
        back are re-packed in ascending index order - which is the order pack()
        appends in, so a re-shown card lands in the right place without any
        repacking. Breaking that invariant (rendering a slice, or dropping a card
        out of the middle and shifting everything behind it down) is what would
        silently scramble the list, which is why _drop_card re-syncs instead.

        A slot whose game is unchanged is skipped outright. That is what makes a
        narrowing filter cheap: the games that survive keep their index, so most
        of the list is left completely alone.
        """
        tags = slg_db.game_tags_bulk(self.conn, [g["id"] for g in games])
        dirty, self._pool_dirty = self._pool_dirty, False
        for i, game in enumerate(games):
            gid = game["id"]
            if i < len(self._card_pool):
                slot, was_hidden = self._card_pool[i], self._pool_gid[i] is None
                if dirty or self._pool_gid[i] != gid:
                    self._fill_card(slot, game, tags.get(gid, ()))
                    self._pool_gid[i] = gid
                if was_hidden:
                    slot["frame"].pack(fill="x", pady=2)
            else:
                slot = self._new_card(game, tags.get(gid, ()))
                self._card_pool.append(slot)
                self._pool_gid.append(gid)
        for i in range(len(games), len(self._card_pool)):
            if self._pool_gid[i] is not None:
                self._card_pool[i]["frame"].pack_forget()
                self._pool_gid[i] = None
        self._reset_card_index()

    def _page_count(self):
        # PAGE_SIZE is read here rather than bound as a default argument: a
        # default is evaluated at import, and the tests patch the module
        # global to widen a page to their fake catalogue.
        return max(1, -(-len(self.rows) // PAGE_SIZE))

    def _page_slice(self):
        start = (self.page - 1) * PAGE_SIZE
        return self.rows[start:start + PAGE_SIZE]

    def _clamp_page(self):
        self.page = min(max(1, self.page), self._page_count())

    def _goto_page(self, page):
        """Turn to a page. A no-op when it is the one already showing, so the
        pager's Entry does not redraw the list for a jump to where it is."""
        before = self.page
        self.page = page
        self._clamp_page()
        if self.page == before:
            self._render_pager()
            return
        self.refresh()
        # Not preserve_scroll: the canvas keeps its yview across a card swap,
        # so without this the new page opens wherever the old one was scrolled
        # to. _restore_scroll defers through after_idle, which is what it needs
        # - the scrollregion only covers the new cards after the next geometry
        # pass.
        self._restore_scroll(0.0)

    def _prev_page(self):
        self._goto_page(self.page - 1)

    def _next_page(self):
        self._goto_page(self.page + 1)

    def _jump_to_typed(self):
        """Read the page box and go there. Anything that is not a number is
        ignored outright - _goto_page would clamp a garbage parse to page 1,
        which silently throws the user to the top of the list."""
        text = self._pager_parts["entry"].get().strip()
        if not text.isdigit():
            return
        self._goto_page(int(text))

    def _render_pager(self):
        """Draw the page bar: 上一页 / 第 X / Y 页 / 下一页, plus a box to type
        a page into.

        Configure-only once built. This runs from refresh(), including the
        skip_if_same branch and every sync tick, and the whole reason that
        branch exists is to avoid re-doing geometry - so the bar is never
        re-packed and its widgets are only touched when the value differs.
        """
        pages = self._page_count()
        if self._pager is None:
            self._pager = ctk.CTkFrame(self._pager_holder, fg_color=CARD,
                                       corner_radius=8)
            self._pager.grid(row=0, column=0, sticky="ew")
            self._pager.grid_columnconfigure(1, weight=1)
            prev = ctk.CTkButton(self._pager, text="‹ 上一页", width=84, height=30,
                                 corner_radius=8, fg_color=CHIP, text_color=TEXT,
                                 hover_color=CARD_HOVER, font=ui_font(size=12),
                                 command=self._prev_page)
            prev.grid(row=0, column=0, padx=(10, 6), pady=8)
            label = ctk.CTkLabel(self._pager, text="", text_color=MUTED,
                                 font=ui_font(size=12))
            label.grid(row=0, column=1)
            nxt = ctk.CTkButton(self._pager, text="下一页 ›", width=84, height=30,
                                corner_radius=8, fg_color=CHIP, text_color=TEXT,
                                hover_color=CARD_HOVER, font=ui_font(size=12),
                                command=self._next_page)
            nxt.grid(row=0, column=2, padx=6, pady=8)
            entry = ctk.CTkEntry(self._pager, width=56, height=30, corner_radius=8,
                                 fg_color=BG, text_color=TEXT, border_color=CHIP,
                                 justify="center", font=ui_font(size=12))
            entry.bind("<Return>", lambda e: self._jump_to_typed())
            entry.grid(row=0, column=3, padx=(6, 4), pady=8)
            jump = ctk.CTkButton(self._pager, text="跳转", width=56, height=30,
                                 corner_radius=8, fg_color=CHIP, text_color=ACCENT,
                                 hover_color=CARD_HOVER, font=ui_font(size=12),
                                 command=self._jump_to_typed)
            jump.grid(row=0, column=4, padx=(0, 10), pady=8)
            self._pager_parts = {"prev": prev, "label": label, "next": nxt,
                                 "entry": entry, "jump": jump}
        if len(self.rows) <= PAGE_SIZE:
            # One page or none: there is nothing to turn to, and a bar reading
            # 第 1 / 1 页 is furniture.
            if self._pager.winfo_manager():
                self._pager.grid_forget()
            return
        if not self._pager.winfo_manager():
            self._pager.grid(row=0, column=0, sticky="ew")
        p = self._pager_parts
        p["label"].configure(text="第 %d / %d 页 · 共 %d 款"
                             % (self.page, pages, len(self.rows)))
        p["prev"].configure(state="disabled" if self.page <= 1 else "normal")
        p["next"].configure(state="disabled" if self.page >= pages else "normal")
        # Never rewrite a box the user is typing into - the sync tick comes
        # through here every few seconds and would eat a half-entered number.
        # Same guard as the 评价 field's, for the same reason.
        want = str(self.page)
        if p["entry"].get() != want and self.focus_get() is not p["entry"]:
            p["entry"].delete(0, "end")
            p["entry"].insert(0, want)

    def _set_empty_label(self, show):
        """One label, shown or hidden. It used to be built fresh on every empty
        render and leaked into the list on every non-empty one."""
        if self._empty_label is None:
            self._empty_label = ctk.CTkLabel(
                self.list, text="没有匹配的游戏。\n左侧点「更新游戏数据」先把站点数据拉下来。",
                text_color=MUTED, font=ui_font(size=13), justify="left")
        shown = bool(self._empty_label.winfo_manager())
        if show and not shown:
            self._empty_label.pack(pady=40)
        elif shown and not show:
            self._empty_label.pack_forget()

    def _drop_card(self, game_id):
        """Take one card off the list without rebuilding the rest of the page.

        A status write made from inside one of the filtered views always means
        the game just left that view, so the list loses a row and gains nothing.
        The card itself is not destroyed here either - the row goes, and
        _sync_cards shifts the cards behind it down by one slot, which is the
        only way to keep the index invariant intact.
        """
        self.rows = [g for g in self.rows if g["id"] != game_id]
        # Removed and then recomputed, not just filtered: dropping the only row
        # of the last page decrements the page, and the on-screen set changes
        # wholesale in a way that a one-id filter cannot describe.
        self._clamp_page()
        self._rendered_ids = [g["id"] for g in self._page_slice()]
        self._sync_cards(self._page_slice())
        self._render_pager()

    def _detail_signature(self):
        """What the detail panel's contents depend on.

        refresh() ends by redrawing that panel, and every widget in it is
        destroyed and rebuilt when it does - a visible blank while the user is
        looking straight at it. Skipping the redraw when the game and its three
        editable fields are unchanged is the difference between a status button
        that lights up and one that makes the panel flash.
        """
        game = self.selected
        if game is None:
            return ("empty",)
        # cover_file belongs here for the same reason the three editable fields
        # do: a cover that lands while the panel is open has to show up in it,
        # and downloading covers changes nothing else about the row.
        # collection_id rides along so the 移出此收藏夹 button appears and
        # disappears when the user switches collections on the same game.
        return (game["id"], game["status"], game["my_rating"], game["note"],
                game["cover_file"], self.collection_id,
                game.get("origin"), game.get("promoted"))

    def _render_detail_if_stale(self):
        if self._detail_signature() == self._detail_sig:
            return
        self._render_detail()

    def _on_card_click(self, card):
        """Open whatever game this card is showing *now*.

        The binding deliberately does not capture the game: cards outlive the row
        they were built for, so a closure holding `game` would open the previous
        occupant of that slot.
        """
        gid = self._widget_gid.get(card)
        if gid is None:
            return
        game = next((g for g in self.rows if g["id"] == gid), None)
        if game is not None:
            self.select(game)

    def _new_card(self, game, tags):
        """Build one card's widgets. Everything that does not change with the
        game - geometry, fonts, wrap width - is set here and never again.

        The three text lines are plain tk.Labels, not CTkLabels. A CTkLabel is a
        Frame plus a Canvas plus a Label, so eighty cards meant 240 extra widgets
        and 240 extra rounded-rectangle draws; measured against the CTk version
        this is roughly 40% off a cold build. The price is that nothing is
        transparent any more: each label carries its own `bg`, which is why
        _paint_card has to recolour them along with the frame.
        """
        # Row height is cover-driven: COVER_H plus the image's vertical padding
        # plus the card's. At six rows a page those two paddings are the whole
        # difference between the last card sitting above the fold and needing a
        # scroll to see, which is why they are quoted off the line they were
        # written on.
        card = ctk.CTkFrame(self.list, corner_radius=10)
        card.pack(fill="x", pady=2)
        card.grid_columnconfigure(1, weight=1)

        img = ctk.CTkLabel(card, text="")
        img.grid(row=0, column=0, rowspan=3, padx=10, pady=8)

        # tk.Label takes a raw pixel wraplength where CTkLabel scaled it for us.
        wrap = card._apply_widget_scaling(CARD_WRAP)

        title = tk.Label(card, text="", bg=CARD, fg=TEXT, anchor="w",
                         font=ui_font(size=15, weight="bold"))
        title.grid(row=0, column=1, sticky="ew", pady=(10, 0))

        meta = tk.Label(card, text="", bg=CARD, fg=MUTED, anchor="w",
                        font=ui_font(size=12))
        meta.grid(row=1, column=1, sticky="ew")

        tagline = tk.Label(card, text="", bg=CARD, fg=MUTED, anchor="w",
                           wraplength=wrap, justify="left",
                           font=ui_font(size=12))
        tagline.grid(row=2, column=1, sticky="ew", pady=(0, 10))

        # The three text lines are bound too, and that is not belt and braces:
        # a plain tk.Label does not pass its clicks up to the frame, so binding
        # only the frame left the 80% of the card that is text dead to the
        # mouse - clicking a game's name did nothing at all.
        for widget in (card, img, title, meta, tagline):
            widget.bind("<Button-1>", lambda e, c=card: self._on_card_click(c))
        slot = {"frame": card, "img": img, "title": title, "meta": meta,
                "tagline": tagline}
        self._fill_card(slot, game, tags)
        return slot

    def _fill_card(self, slot, game, tags):
        """Point an existing card at `game`. Five configures, no new widgets.

        No re-packing is needed when the text changes length: the card fills the
        list horizontally, the labels stick east-west in a weight-1 column, and
        the tag line's wrapping is fixed by its wraplength.
        """
        selected = self.selected is not None and game["id"] == self.selected["id"]
        self._paint_slot(slot, selected)
        slot["img"].configure(image=load_cover(game))
        title = display_title(game)
        if game["version"]:
            title += "  v%s" % game["version"]
        slot["title"].configure(text=title)
        slot["meta"].configure(text=meta_text(game),
                               fg=ACCENT if game["complete"] else MUTED)
        slot["tagline"].configure(text="  ".join(
            display_tag(t) for t in card_tags(tags)))

    def _paint_slot(self, slot, selected):
        """Colour a card for its selected state.

        tk.Label does not follow its parent's background, so the labels have to
        be repainted by hand. The `cget` guard is what keeps a selection change
        from being four needless Tcl round trips per card on the list.
        """
        bg = CARD_HOVER if selected else CARD
        if slot["frame"].cget("fg_color") != bg:
            slot["frame"].configure(fg_color=bg)
        for key in ("title", "meta", "tagline"):
            if slot[key].cget("bg") != bg:
                slot[key].configure(bg=bg)

    def _repaint_card_title(self, game):
        """Rewrite one card's name after its translation landed.

        In place, like _repaint_card_meta: the alternative is relaunching the
        whole list, and the translation always arrives while the user is
        looking at the card it belongs to.
        """
        label = self._card_title.get(game["id"])
        if label is None or not label.winfo_exists():
            return
        title = display_title(game)
        if game["version"]:
            title += "  v%s" % game["version"]
        label.configure(text=title)

    def _paint_card(self, game_id, selected):
        slot = self._card_slot.get(game_id)
        if slot is not None:
            self._paint_slot(slot, selected)

    def _repaint_card_meta(self, game):
        """Rewrite one card's meta line. The star rating and the status badge
        are both in there, and both are things the detail panel writes."""
        label = self._card_meta.get(game["id"])
        if label is not None and label.winfo_exists():
            label.configure(text=meta_text(game),
                            fg=ACCENT if game["complete"] else MUTED)

    def select(self, game):
        old = self.selected
        if old is not None and old["id"] == game["id"]:
            return  # already open; rebuilding the panel would just flicker
        # dict(), not the row itself: sqlite3.Row is read-only and the status
        # and rating buttons write their new value back into this object.
        self.selected = dict(game)
        if old is not None:
            self._paint_card(old["id"], False)
        self._paint_card(game["id"], True)
        self._render_detail()

    def _render_detail(self):
        """Point the detail panel at whatever is selected.

        The widgets are built once and reused. Rebuilding the panel was what
        made selecting a game flash white - the whole right-hand side went
        blank for the length of a build - and it reset the 原文/中文 switch as
        a side effect, which is now done deliberately in _fill_detail.
        """
        game = self.selected
        if game is None:
            self._destroy_detail()
            ctk.CTkLabel(self.detail, text="左边选一款游戏", text_color=MUTED,
                         font=ui_font(size=14)).pack(pady=60)
            self._detail_sig = self._detail_signature()
            return
        if self._detail_parts is None:
            self._build_detail_skeleton()
        self._fill_detail(game)
        # Recorded last, once every widget above exists: this is what lets
        # refresh() tell "the panel still describes this game" from "rebuild it".
        self._detail_sig = self._detail_signature()

    def _destroy_detail(self):
        for child in self.detail.winfo_children():
            child.destroy()
        self._detail_parts = None
        self._detail_shown = None
        self._tag_chips = []
        self._ov_label = None
        self._ov_seg = None
        self._title_label = None
        self._title_note = None
        self._title_entry = self._ov_text = None
        self._status_btns = {}
        self._star_btns = []

    def _build_detail_skeleton(self):
        """Every widget the panel can ever show, built once.

        The two conditional blocks - the site link and the description - are
        built here too and shown or hidden by _layout_detail, so going from a
        game with a description to one without costs nothing. The same goes for
        the two in-place editors: they exist from the start, one keystroke away.
        """
        self._destroy_detail()
        d = self.detail
        parts = {}
        order = []

        def add(key, widget, **pack):
            parts[key] = widget
            order.append((key, widget, pack))
            return widget

        add("cover", ctk.CTkLabel(d, text=""), pady=(20, 10))

        # The name and the editor that replaces it share a box, so swapping
        # between them cannot disturb the order of what is below.
        box = ctk.CTkFrame(d, fg_color="transparent")
        title = ctk.CTkLabel(box, text="", text_color=TEXT,
                             font=ui_font(size=18, weight="bold"),
                             wraplength=340, justify="left")
        title.pack()
        entry = ctk.CTkEntry(box, height=30, corner_radius=8, fg_color=BG,
                             text_color=TEXT, border_color=CHIP,
                             placeholder_text_color=PLACEHOLDER,
                             placeholder_text="还没译文，可直接输入中文名",
                             font=ui_font(size=15))
        entry.bind("<Return>", lambda e: self._save_title_edit())
        entry.bind("<Escape>", lambda e: self._cancel_title_edit())
        parts["title_box"] = box
        parts["title"] = title
        parts["title_entry"] = entry
        self._title_label = title
        self._title_entry = entry
        add("title_box", box, fill="x", padx=18, pady=(0, 2))

        subrow = ctk.CTkFrame(d, fg_color="transparent")
        sub = ctk.CTkLabel(subrow, text="", text_color=MUTED, font=ui_font(size=12))
        sub.pack(side="left", expand=True)
        edit = ctk.CTkButton(subrow, text="✎ 改名", width=64, height=24,
                             corner_radius=6, fg_color="transparent",
                             text_color=ACCENT, hover_color=CHIP,
                             font=ui_font(size=11), command=self._begin_title_edit)
        edit.pack(side="right")
        parts["sub"] = sub
        add("subrow", subrow, fill="x", padx=18)

        # Empty until a name translation actually fails. It exists from the
        # start so the message has somewhere to land that is not a popup, and so
        # the failure is visible rather than only the English name quietly
        # staying put.
        note = ctk.CTkLabel(d, text="", text_color=DANGER_TEXT,
                            font=ui_font(size=11), wraplength=340,
                            justify="left", anchor="w")
        self._title_note = note
        add("title_note", note, fill="x", padx=18, pady=(2, 0))

        order.extend(self._detail_action(
            d, parts, "url", "在浏览器打开 dikgames 页面", pady=(10, 4)))

        order.extend(self._build_detail_share(d, parts))
        order.extend(self._build_detail_open_folder(d, parts))
        order.extend(self._build_detail_collect(d, parts))
        order.extend(self._build_detail_remove_collection(d, parts))
        order.extend(self._build_detail_promote(d, parts))
        order.extend(self._build_detail_delete(d, parts))

        order.extend(self._build_detail_status(d, parts))
        order.extend(self._build_detail_stars(d, parts))
        order.extend(self._build_detail_heat(d, parts))
        order.extend(self._build_detail_note(d, parts))
        order.extend(self._build_detail_tags(d, parts))
        order.extend(self._build_detail_overview(d, parts))
        order.extend(self._build_detail_comments(d, parts))

        # Static, so _detail_signature stays as it is, and _layout_detail shows
        # it unconditionally: only url/ov_* are keyed off a flag there.
        #
        # The VPN sentence came down here from the toolbar. Both halves are the
        # same kind of statement - what this app does not promise to deliver -
        # and the foot of the panel is where someone reads them together
        # instead of glancing past a box under the search field.
        add("disclaimer", ctk.CTkLabel(
                d, text="本站只做游戏检索，不提供下载。想玩请去官网或自寻下载地址。\n"
                        "未使用梯子导致的一切问题与作者无关。",
                text_color=MUTED, font=ui_font(size=11), wraplength=340,
                justify="left", anchor="w"),
            fill="x", padx=18, pady=(12, 2))

        # Last thing in the panel on every game, for the same reason the
        # disclaimer is: it is addressed to whoever is reading, and there is no
        # other screen they are guaranteed to see.
        #
        # Only the mail door lives in here now. The group number used to sit
        # next to it, but the panel is about one game and the group is about the
        # app, so it moved down to the strip under the panel (_build_qq_footer)
        # where it keeps company with the pager instead of scrolling away with
        # a game's blurb.
        feedback = ctk.CTkFrame(d, fg_color="transparent")
        ctk.CTkLabel(feedback,
                     text="用得还行的话，欢迎在 GitHub 点个 star，"
                          "也帮忙推荐给周围的朋友。",
                     text_color=MUTED, font=ui_font(size=11), wraplength=340,
                     justify="left", anchor="w").pack(fill="x")
        mail = ctk.CTkButton(feedback, text="反馈 / 建议：%s" % CONTACT_EMAIL,
                             height=22, corner_radius=6, fg_color="transparent",
                             text_color=ACCENT, hover_color=CHIP,
                             font=ui_font(size=11), anchor="w",
                             command=lambda: webbrowser.open(CONTACT_MAILTO))
        mail.pack(anchor="w", pady=(2, 0))
        add("feedback", feedback, fill="x", padx=18, pady=(0, 20))

        self._detail_parts = parts
        self._detail_order = order
        self._detail_shown = None

    def _layout_detail(self, show_url, show_folder=False, show_remove=False,
                       show_user=False):
        """Show, hide and order the panel's blocks.

        pack() appends, so a block that comes back lands at the bottom.
        Re-packing the whole visible run in order is a dozen calls and removes
        the entire class of "the description is below the tags now" bug. It is
        skipped when the same set of blocks is already up, which is every
        game change but one.

        The description block is unconditional. It used to be dropped when the
        overview was empty, which hid 968 of 1595 games' 简介 section entirely:
        the user could not tell "the site has no blurb for this one" from "the
        app is broken", and there was nowhere to click 改简介 on a game whose
        translation they wanted to write by hand.
        """
        wanted = {"url": show_url, "open_folder": show_folder,
                  "remove_collection": show_remove,
                  "promote": show_user, "delete_game": show_user}
        keys = [key for key, _w, _p in self._detail_order if wanted.get(key, True)]
        if keys == self._detail_shown:
            return
        for _key, widget, _pack in self._detail_order:
            widget.pack_forget()
        for key, widget, pack in self._detail_order:
            if key in keys:
                widget.pack(**pack)
        self._detail_shown = keys

    def _fill_detail(self, game):
        p = self._detail_parts
        p["cover"].configure(image=load_cover(game, DETAIL_W, DETAIL_H))
        p["title"].configure(text=self._title_to_show(game))
        p["sub"].configure(text="v%s · %s" % (game["version"] or "?",
                                              game["developer"] or "未知作者"))
        p["title_note"].configure(text="")
        is_user = game.get("origin") == "user"
        if game["url"] and not is_user:
            p["url"].configure(command=lambda u=game["url"]: webbrowser.open(u))
        folder = game.get("folder_path")
        if folder:
            p["open_folder"].configure(
                command=lambda f=folder: self._open_local_folder(f))
        if is_user:
            if game.get("promoted"):
                p["promote"].configure(text="移出主列表",
                                       command=lambda: self._set_promote(game["id"], False))
            else:
                p["promote"].configure(text="加入主列表",
                                       command=lambda: self._check_promote(game))
            p["delete_game"].configure(
                command=lambda: self._delete_user_game(game))
        self._sync_status_btns(game)
        self._sync_star_btns(game)
        self._fill_heat(game)
        # Only rewritten when it differs: the sync tick comes through here too,
        # and a delete/insert drops the cursor out of a note being typed.
        # get('1.0','end') would append a newline, so the comparison would
        # never come out equal and this guard would fire - and drop the caret -
        # on every sync tick, which is the exact stall it is here to prevent.
        if p["note_entry"].get("1.0", "end-1c") != (game["note"] or ""):
            p["note_entry"].delete("1.0", "end")
            if game["note"]:
                p["note_entry"].insert("1.0", game["note"])
        self._fill_detail_tags(game)
        self._fill_detail_comments(game)
        # The switch remembers what the last game was read in. It used to be
        # forced back to 原文 on every fill, which threw the choice away each
        # time the user picked a new game, and it opened English even for games
        # whose Chinese was already in the cache - so a card that read 中文 in
        # the grid came up English in the panel and looked untranslated.
        # request=False: restoring a cached language costs no API call, and
        # spending one per click on the list is not something a passive fill
        # should do.
        p["ov_seg"].set("原文")
        self._apply_lang(game, "原文", request=False)
        self._layout_detail(show_url=(not is_user) and bool(game["url"]),
                            show_folder=bool(folder),
                            show_remove=self.collection_id is not None,
                            show_user=is_user)

    def _show_title(self, text):
        label = self._title_label
        if label is not None and label.winfo_exists():
            label.configure(text=text)

    def _set_ov_text(self, text):
        """The description box, saying so when the site has none.

        A blank label reads as a broken panel, and the block used to be hidden
        outright - so a game with no blurb looked like a game with no
        description section at all. The placeholder also tells the user the
        box next to it is theirs to fill.
        """
        label = self._ov_label
        if label is None or not label.winfo_exists():
            return
        if text:
            label.configure(text=text, text_color=TEXT)
        else:
            label.configure(text=EMPTY_OVERVIEW, text_color=MUTED)

    def _set_overview_lang(self, game, value):
        """The user picked a language for this panel."""
        seg = self._ov_seg
        if seg is not None and seg.winfo_exists() and seg.get() != value:
            seg.set(value)
        self._apply_lang(game, value, request=True)

    def _apply_lang(self, game, value, request):
        """Put `value`'s text in the panel, from the cache.

        `request` decides what happens when nothing is cached: True kicks off
        the translation, False leaves whatever is on screen alone. Both callers
        go through here so the title and the description can never disagree
        about which language the panel is in.
        """
        label = self._ov_label
        if label is None or not label.winfo_exists():
            return
        if value == "原文":
            self._show_title(self._title_to_show(game))
            self._show_title_note("")
            self._set_ov_text(self._overview_to_show(game))
            return
        # get_translation_row, not get_translation: a row marked
        # ENGINE_UNTRANSLATED means "asked already, the model will not do it",
        # and the English name is what belongs on screen for it.
        title_row = slg_db.get_translation_row(self.conn, "title", game["id"],
                                               game["title"])
        cached_title = None
        if title_row is not None and title_row[1] != slg_db.ENGINE_UNTRANSLATED:
            cached_title = title_row[0]
        cached_ov = slg_db.get_translation(self.conn, "overview", game["id"],
                                           game["overview"])
        if cached_title is not None:
            self._show_title(cached_title)
        elif not request:
            # Nothing cached for the remembered language. Falling back to the
            # site's own name keeps the panel readable instead of half-empty.
            self._show_title(self._title_to_show(game))
        if cached_ov is not None:
            self._set_ov_text(cached_ov)
        elif not game["overview"]:
            # Nothing to translate. Asking anyway spends a request to come back
            # with an error the user can do nothing about, over a game the site
            # never wrote a blurb for. The placeholder stays; the name below
            # still gets its translation.
            self._set_ov_text("")
        elif not request:
            self._set_ov_text(self._overview_to_show(game))
        else:
            label.configure(text="翻译中…")
        if not request:
            return
        # The name rides along on the same switch rather than getting a button
        # of its own: it is one short string, and half the panel turning Chinese
        # while the title stays English reads as a bug.
        if cached_title is not None and cached_ov is not None:
            return
        if (cached_ov is None and game["overview"]
                and game["id"] not in self._ov_inflight):
            self._ov_inflight.add(game["id"])
            threading.Thread(target=self._overview_worker,
                             args=(dict(game),), daemon=True).start()
        if title_row is not None or cached_title is not None:
            return  # the name is settled, translated or marked untranslatable
        if game["id"] in self._title_inflight:
            return
        self._title_inflight.add(game["id"])
        threading.Thread(target=self._title_worker,
                         args=(dict(game),), daemon=True).start()

    def _overview_worker(self, game):
        conn = None
        try:
            conn = slg_db.connect()
            config = slg_engines.resolve_config(conn)
            engine = config.build()
            if not config.api_key and engine.needs_key:
                raise slg_translate.TranslateError(
                    "还没填 API Key。点左侧栏的「更多工具…」→「翻译设置…」填一下。")
            text = slg_translate.translate_overview(game["overview"], config.api_key,
                                                   config.model, engine=engine)
            slg_db.set_auto_translation(conn, "overview", game["id"],
                                        game["overview"], text, engine=config.model)
        except Exception as exc:  # noqa: BLE001 - the user needs the reason
            self.queue.put(("overview", (game["id"], None, str(exc)[:120])))
        else:
            self.queue.put(("overview", (game["id"], text, None)))
        finally:
            if conn is not None:
                conn.close()

    def _title_worker(self, game):
        """The name, on a thread of its own.

        It used to run first on the overview's thread. A name request against a
        blackholed DNS blocks for far longer than any socket timeout, so the
        overview behind it never started - and along with it went the only
        message that would have told the user anything had failed at all.
        """
        conn = None
        try:
            conn = slg_db.connect()
            config = slg_engines.resolve_config(conn)
            engine = config.build()
            if not config.api_key and engine.needs_key:
                raise slg_translate.TranslateError(
                    "还没填 API Key。点左侧栏的「更多工具…」→「翻译设置…」填一下。")
            title = slg_translate.translate_title(game["title"], config.api_key,
                                                 config.model, engine=engine)
        except slg_translate.TitleGuardError as exc:
            # Recorded rather than dropped: without the marker every later visit
            # to this game pays for the same hopeless request. The English name
            # is the answer here, not a failure - the "keep:" prefix says so.
            if conn is not None:
                slg_db.set_auto_translation(conn, "title", game["id"], game["title"],
                                            game["title"],
                                            engine=slg_db.ENGINE_UNTRANSLATED)
            self.queue.put(("title", (game["id"], None, None, "keep:%s" % exc)))
        except Exception as exc:  # noqa: BLE001 - the user needs the reason
            # Not cached: a dead key or a dropped connection is worth retrying,
            # unlike a model that refuses to leave v1.2 alone.
            self.queue.put(("title", (game["id"], None, None, str(exc)[:120])))
        else:
            slg_db.set_auto_translation(conn, "title", game["id"], game["title"],
                                        title, engine=config.model)
            self.queue.put(("title", (game["id"], title, config.model, None)))
        finally:
            if conn is not None:
                conn.close()

    def _show_title_note(self, text):
        label = self._title_note
        if label is not None and label.winfo_exists():
            label.configure(text=text)

    def _title_result(self, game_id, text, engine, error):
        """A translated game name landed.

        The list card is repainted too: a name that just got translated is on
        screen in two places at once.
        """
        self._title_inflight.discard(game_id)
        if error:
            # This used to return silently. The English name is still the right
            # thing on screen, but a switch that quietly does nothing is not
            # something the user can tell apart from a broken build - so the
            # reason goes under the title. title_error_text() decides which
            # failures are worth naming at all.
            self._show_title_note(title_error_text(error))
            return
        row = next((g for g in self.rows if g["id"] == game_id), None)
        _TITLE_ZH[str(game_id)] = (text, engine)
        if row is not None:
            self._repaint_card_title(row)
        game = self.selected
        if game is None or game["id"] != game_id:
            return
        if self._ov_seg is not None and self._ov_seg.winfo_exists():
            if self._ov_seg.get() != "中文":
                return
        self._show_title_note("")
        self._show_title(text)

    def _overview_result(self, game_id, text, error):
        """A translation came back. It is only shown if the panel still belongs
        to that game - if the user moved on, the row is already in the cache and
        will be there when they come back."""
        self._ov_inflight.discard(game_id)
        game = self.selected
        if game is None or game["id"] != game_id:
            return
        label = self._ov_label
        if label is None or not label.winfo_exists():
            return
        # The user may have switched back to 原文 while this was in flight.
        # Showing either the translation or its failure would contradict the
        # switch, and the translation is cached either way - so it is simply
        # there the next time they ask.
        if self._ov_seg is not None and self._ov_seg.winfo_exists():
            if self._ov_seg.get() != "中文":
                return
        if error:
            label.configure(text="翻译失败：%s\n\n%s"
                                 % (error, game["overview"] or ""))
            self._ov_seg.set("原文")
            return
        self._set_ov_text(text)

    def _build_detail_status(self, d, parts):
        row = ctk.CTkFrame(d, fg_color="transparent")
        buttons = {}
        for label, status in (("想玩", "want"), ("已下载", "downloaded")):
            btn = ctk.CTkButton(row, text=label, height=30, corner_radius=8,
                                fg_color=CHIP, text_color=TEXT,
                                hover_color=CARD_HOVER,
                                command=lambda s=status: self._set_status(s))
            # fill+expand so the two share the row's width evenly: left-packed
            # they stayed the same width in fullscreen and left the right half
            # empty.
            btn.pack(side="left", fill="x", expand=True, padx=3)
            buttons[status] = btn
        self._status_btns = buttons
        parts["status"] = row
        return [("status", row, {"fill": "x", "padx": 18, "pady": (8, 0)})]

    def _sync_status_btns(self, game):
        """Colour the two status buttons for `game` without rebuilding them.

        A status write used to end in a full refresh, which destroyed and
        recreated the very button the user had just pressed - the highlight
        blinked off and came back, and the list behind it collapsed and
        rebuilt. Same expression, applied to widgets that stay put.
        """
        for status, btn in self._status_btns.items():
            if not btn.winfo_exists():
                continue
            on = game["status"] == status
            btn.configure(fg_color=ACCENT if on else CHIP,
                          text_color=ON_ACCENT if on else TEXT)

    def _build_detail_stars(self, d, parts):
        row = ctk.CTkFrame(d, fg_color="transparent")
        ctk.CTkLabel(row, text="我的评分", text_color=MUTED,
                     font=ui_font(size=12)).pack(side="left", padx=(0, 8))
        stars = []
        for star in range(1, 6):
            btn = ctk.CTkButton(row, text="☆", width=30, height=30, corner_radius=6,
                                fg_color="transparent", text_color=MUTED,
                                hover_color=CHIP, font=ui_font(size=17),
                                command=lambda s=star: self._set_rating(s))
            btn.pack(side="left")
            stars.append(btn)
        self._star_btns = stars
        parts["stars"] = row
        return [("stars", row, {"fill": "x", "padx": 18, "pady": (12, 0)})]

    def _sync_star_btns(self, game):
        mine = game["my_rating"] or 0
        for index, btn in enumerate(self._star_btns, start=1):
            if not btn.winfo_exists():
                continue
            btn.configure(text="★" if index <= mine else "☆",
                          text_color=STAR if index <= mine else MUTED)

    def _fill_heat(self, game):
        heat = game.get("heat")
        if heat is None:
            self._detail_parts["heat"].configure(text="热度 —（同步后显示）")
            return
        parts = ["热度 %.1f" % heat]
        if game.get("site_views") is not None:
            parts.append("浏览 %s" % f"{game['site_views']:,}")
        if game.get("site_likes") is not None:
            parts.append("点赞 %s" % f"{game['site_likes']:,}")
        if game.get("site_comments") is not None:
            parts.append("评论 %s" % f"{game['site_comments']:,}")
        self._detail_parts["heat"].configure(text=" · ".join(parts))

    def _build_detail_heat(self, d, parts):
        label = ctk.CTkLabel(d, text="", text_color=MUTED, font=ui_font(size=12))
        parts["heat"] = label
        return [("heat", label, {"anchor": "w", "padx": 18, "pady": (10, 0)})]

    def _detail_action(self, d, parts, key, text, command=None, accent=False,
                       pady=(0, 4), text_color=None):
        """A filled detail-panel action button, registered under `key`.

        share/open_folder/collect/url/remove_collection are one widget restyled
        five times; the constructor's seven kwargs are the thing that drifted.
        """
        btn = ctk.CTkButton(d, text=text, height=30, corner_radius=8,
                            fg_color=CHIP,
                            text_color=text_color or (ACCENT if accent else TEXT),
                            hover_color=CARD_HOVER, command=command)
        parts[key] = btn
        return [(key, btn, {"fill": "x", "padx": 18, "pady": pady})]

    def _build_detail_share(self, d, parts):
        return self._detail_action(d, parts, "share", "分享截图",
                                   self._share_screenshot, accent=True)

    def _build_detail_open_folder(self, d, parts):
        return self._detail_action(d, parts, "open_folder", "打开本地目录",
                                   accent=True)

    def _build_detail_collect(self, d, parts):
        return self._detail_action(d, parts, "collect", "收藏夹…",
                                   self._open_collect_dialog)

    def _build_detail_remove_collection(self, d, parts):
        return self._detail_action(d, parts, "remove_collection",
                                   "移出此收藏夹", self._remove_from_collection)

    def _build_detail_promote(self, d, parts):
        return self._detail_action(d, parts, "promote", "加入主列表", accent=True)

    def _build_detail_delete(self, d, parts):
        return self._detail_action(d, parts, "delete_game", "删除此游戏",
                                   text_color=DANGER_TEXT)

    def _edit_tags_current(self):
        if self.selected is not None:
            self.open_edit_tags(self.selected)

    def _set_promote(self, game_id, on):
        with slg_db.session() as conn:
            slg_db.promote_game(conn, game_id, on)
        self.refresh()

    def _check_promote(self, game):
        missing = []
        if not game["title"]:
            missing.append("标题")
        if not game["developer"]:
            missing.append("开发商")
        if not game["engine"]:
            missing.append("引擎")
        if not game["version"]:
            missing.append("版本")
        if not slg_db.game_tags(self.conn, game["id"]):
            missing.append("至少1个标签")
        if missing:
            messagebox.showinfo("还不能加入主列表",
                                "还缺：%s。\n先在右侧补全条目再试。" % "、".join(missing),
                                parent=self)
            return
        self._set_promote(game["id"], True)

    def _delete_user_game(self, game):
        if game.get("origin") != "user":
            return
        if not messagebox.askyesno("删除游戏",
                                   "确定删除「%s」吗？\n这个操作无法撤销。" % game["title"],
                                   parent=self):
            return
        with slg_db.session() as conn:
            slg_db.delete_game(conn, game["id"])
        self.selected = None
        self.refresh()

    def _open_local_folder(self, folder):
        if os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo("找不到目录", "本地目录已不存在：\n%s" % folder)

    def _build_detail_comments(self, d, parts):
        """The comment list for the selected game, rebuilt per game.

        Local comments come from SQLite and always show; remote comments are
        fetched from LeanCloud in the background and merged in when they land.
        """
        head = ctk.CTkFrame(d, fg_color="transparent")
        ctk.CTkLabel(head, text="评论", text_color=MUTED,
                     font=ui_font(size=12)).pack(side="left")
        ctk.CTkButton(head, text="写评论", height=24, width=76, corner_radius=6,
                      fg_color="transparent", text_color=ACCENT, hover_color=CHIP,
                      font=ui_font(size=11),
                      command=self._open_comment_dialog).pack(side="right")
        box = ctk.CTkFrame(d, fg_color="transparent")
        parts["comments_head"] = head
        parts["comments_box"] = box
        return [("comments_head", head, {"fill": "x", "padx": 18, "pady": (12, 4)}),
                ("comments_box", box, {"fill": "x", "padx": 18})]

    def _fill_detail_comments(self, game):
        box = self._detail_parts["comments_box"]
        for child in box.winfo_children():
            child.destroy()
        local = slg_db.list_comments(self.conn, game["slug"])
        self._render_comments(game, local, [], loading=slg_comments.configured())
        if slg_comments.configured():
            slug = game["slug"]
            threading.Thread(target=self._fetch_comments_worker,
                             args=(slug,), daemon=True).start()

    def _render_comments(self, game, local, remote, loading=False):
        box = self._detail_parts["comments_box"]
        uploaded_ids = {c["cloud_id"] for c in local if c["cloud_id"]}
        rows = []
        for c in local:
            rows.append({"author": c["nickname"] or "匿名", "content": c["content"],
                         "time": c["created_at"], "own": True,
                         "local_id": c["id"], "cloud_id": c["cloud_id"]})
        for c in remote:
            oid = c.get("objectId")
            if oid and oid in uploaded_ids:
                continue
            rows.append({"author": c.get("nickname") or "匿名",
                         "content": c.get("content") or "",
                         "time": c.get("createdAt") or "", "own": False,
                         "local_id": None, "cloud_id": None})
        if not rows and not loading:
            ctk.CTkLabel(box, text="暂无评论，来写第一条吧", text_color=MUTED,
                         font=ui_font(size=11)).pack(anchor="w", pady=(0, 4))
        for r in rows:
            self._comment_row(box, r)
        if loading:
            ctk.CTkLabel(box, text="正在加载云端评论…", text_color=MUTED,
                         font=ui_font(size=11)).pack(anchor="w", pady=(4, 0))

    def _comment_row(self, box, r):
        frame = ctk.CTkFrame(box, fg_color="transparent")
        top = ctk.CTkFrame(frame, fg_color="transparent")
        ctk.CTkLabel(top, text=r["author"], text_color=TEXT,
                     font=ui_font(size=12, weight="bold")).pack(side="left")
        t = (r["time"] or "").replace("T", " ")[:16]
        if t:
            ctk.CTkLabel(top, text="  " + t, text_color=MUTED,
                         font=ui_font(size=10)).pack(side="left")
        if r["own"]:
            tag = "已上传" if r["cloud_id"] else "仅自己可见"
            ctk.CTkLabel(top, text=" · " + tag, text_color=MUTED,
                         font=ui_font(size=10)).pack(side="left")
            ctk.CTkButton(top, text="删除", width=40, height=18, corner_radius=6,
                          fg_color="transparent", text_color=MUTED,
                          hover_color=CHIP, font=ui_font(size=10),
                          command=lambda lid=r["local_id"], cid=r["cloud_id"]:
                              self._delete_own_comment(lid, cid)).pack(side="right")
        else:
            ctk.CTkLabel(top, text=" · 云端", text_color=MUTED,
                         font=ui_font(size=10)).pack(side="left")
        top.pack(fill="x")
        ctk.CTkLabel(frame, text=r["content"], text_color=TEXT,
                     font=ui_font(size=12), wraplength=340, justify="left",
                     anchor="w").pack(fill="x")
        frame.pack(fill="x", pady=(0, 8))

    def _fetch_comments_worker(self, slug):
        remote = slg_comments.fetch_comments(slug)
        self.queue.put(("comments", (slug, remote)))

    def _comments_result(self, slug, remote):
        game = self.selected
        if game is None or game["slug"] != slug:
            return
        box = self._detail_parts.get("comments_box")
        if box is None or not box.winfo_exists():
            return
        local = slg_db.list_comments(self.conn, slug)
        if remote is None:
            # An upload just finished; fetch fresh remote comments.
            self._render_comments(game, local, [], loading=slg_comments.configured())
            if slg_comments.configured():
                threading.Thread(target=self._fetch_comments_worker,
                                 args=(slug,), daemon=True).start()
        else:
            self._render_comments(game, local, remote, loading=False)

    def _delete_own_comment(self, local_id, cloud_id):
        if cloud_id:
            threading.Thread(target=slg_comments.delete_comment,
                             args=(cloud_id,), daemon=True).start()
        slg_db.delete_comment(self.conn, local_id)
        if self.selected is not None:
            self._fill_detail_comments(self.selected)

    def _open_comment_dialog(self):
        game = self.selected
        if game is None:
            return
        win = self._new_dialog("写评论", "400x420")
        ctk.CTkLabel(win, text="「%s」" % self._title_to_show(game),
                     text_color=TEXT, font=ui_font(size=13, weight="bold"),
                     wraplength=340, justify="left").pack(
            fill="x", padx=16, pady=(14, 8))
        box = ctk.CTkTextbox(win, height=180, corner_radius=8, fg_color=BG,
                             text_color=TEXT, border_color=CHIP, border_width=1,
                             font=ui_font(size=12), wrap="word")
        box.pack(fill="x", padx=16)
        entry = ctk.CTkEntry(win, placeholder_text="昵称（可选）", height=30,
                             corner_radius=8, fg_color=CARD, text_color=TEXT,
                             placeholder_text_color=MUTED, border_width=1,
                             border_color=CHIP, font=ui_font(size=12))
        entry.pack(fill="x", padx=16, pady=(8, 0))
        nickname = slg_db.get_pref(self.conn, "profile.nickname", "") or ""
        if nickname:
            entry.insert(0, nickname)
        upload = tk.BooleanVar(value=slg_comments.configured())
        if slg_comments.configured():
            ctk.CTkCheckBox(win, text="上传到云端，让其他用户也能看到",
                            variable=upload, font=ui_font(size=12)).pack(
                anchor="w", padx=16, pady=(10, 4))
        else:
            ctk.CTkLabel(win, text="云端评论未配置，评论仅保存在本机。",
                         text_color=MUTED, font=ui_font(size=11), wraplength=340,
                         justify="left").pack(anchor="w", padx=16, pady=(10, 4))
        row = ctk.CTkFrame(win, fg_color="transparent")
        ctk.CTkButton(row, text="取消", height=32, width=90, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      font=ui_font(size=12), command=win.destroy).pack(side="left")
        ctk.CTkButton(row, text="发布", height=32, width=90, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=12),
                      command=lambda: self._submit_comment(
                          win, box.get("1.0", "end-1c"), entry.get(),
                          upload.get())).pack(side="left", padx=(8, 0))
        row.pack(pady=(8, 12))

    def _submit_comment(self, win, content, nickname, upload):
        content = (content or "").strip()
        game = self.selected
        if not content or game is None:
            return
        nickname = (nickname or "").strip() or None
        local_id = slg_db.add_comment(self.conn, game["slug"], content, nickname)
        win.destroy()
        self._fill_detail_comments(game)
        if upload and slg_comments.configured():
            slug = game["slug"]

            def worker():
                cid = slg_comments.upload_comment(slug, content, nickname)
                if cid:
                    with slg_db.session() as conn:
                        slg_db.mark_comment_uploaded(conn, local_id, cid)
                self.queue.put(("comments", (slug, None)))
            threading.Thread(target=worker, daemon=True).start()

    def _build_detail_note(self, d, parts):
        """A Textbox, not an Entry. CTkEntry wraps tkinter.Entry, which has no
        wrapping at any setting - a review longer than the panel just scrolled
        sideways out of view, which is what "只能在同一行里不断延伸" was. Same
        widget and same key bindings as the 简介 editor below, so the panel has
        one way of editing a long text rather than two.
        """
        head = ctk.CTkLabel(d, text="评价", text_color=MUTED, font=ui_font(size=12))
        box = ctk.CTkTextbox(d, height=90, corner_radius=8, fg_color=BG,
                             text_color=TEXT, border_color=CHIP, border_width=1,
                             font=ui_font(size=12), wrap="word")
        box.bind("<Control-Return>", lambda e: self._set_note())
        box.bind("<Escape>", lambda e: self._cancel_note_edit())
        row = ctk.CTkFrame(d, fg_color="transparent")
        # The keys are not guessable - an empty box with no hint reads as the
        # app having lost the text, and Enter inserting a newline instead of
        # saving is exactly the surprise a hint is for.
        ctk.CTkLabel(row, text="Ctrl+Enter 保存 · Esc 还原", text_color=MUTED,
                     font=ui_font(size=11)).pack(side="left")
        ctk.CTkButton(row, text="保存", height=28, width=80, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=CARD_HOVER, font=ui_font(size=12),
                      command=self._set_note).pack(side="right")
        parts["note_head"] = head
        parts["note_entry"] = box
        parts["note_row"] = row
        return [("note_head", head, {"anchor": "w", "padx": 18, "pady": (12, 2)}),
                ("note_entry", box, {"fill": "x", "padx": 18}),
                ("note_row", row, {"fill": "x", "padx": 18, "pady": (4, 0)})]

    def _build_detail_tags(self, d, parts):
        head = ctk.CTkFrame(d, fg_color="transparent")
        ctk.CTkLabel(head, text="标签（左键加入筛选 / 右键排除）", text_color=MUTED,
                     font=ui_font(size=12)).pack(side="left")
        ctk.CTkButton(head, text="✎ 编辑", width=64, height=24, corner_radius=6,
                      fg_color="transparent", text_color=ACCENT, hover_color=CHIP,
                      font=ui_font(size=11), command=self._edit_tags_current
                      ).pack(side="right")
        ctk.CTkButton(head, text="✎ 译名", width=64, height=24, corner_radius=6,
                      fg_color="transparent", text_color=ACCENT, hover_color=CHIP,
                      font=ui_font(size=11), command=self.open_tag_editor
                      ).pack(side="right")
        flow = FlowFrame(d, fg_color="transparent", gap_x=4, gap_y=4)
        parts["tags_head"] = head
        parts["tags_flow"] = flow
        return [("tags_head", head, {"fill": "x", "padx": 18, "pady": (12, 4)}),
                ("tags_flow", flow, {"fill": "x", "padx": 18})]

    def _fill_detail_tags(self, game):
        """Rebuild the chip row for `game`.

        The one block that is still rebuilt rather than repointed: chip count
        and width differ per game, and ChipFrame measures reqwidth, so
        configuring text in place would re-place them against stale geometry.
        It is a strip inside the panel, not the panel.
        """
        flow = self._detail_parts["tags_flow"]
        for chip in self._tag_chips:
            chip.destroy()
        font = ui_font(size=11)
        chips = []
        for slug in slg_db.game_tags(self.conn, game["id"]):
            label = display_tag(slug)
            active = slug in self.include or slug in self.exclude
            # CTkButton defaults to 140px wide whatever the text is, which would
            # waste more room than the wrapping saves.
            btn = ctk.CTkButton(
                flow, text=label, height=24, width=font.measure(label) + 22,
                corner_radius=12, fg_color=ACCENT if active else CHIP,
                text_color=ON_ACCENT if active else TEXT, hover_color=CARD_HOVER,
                font=font,
                command=lambda s=slug: self.toggle_tag(s))
            btn.bind("<Button-3>", lambda e, s=slug: self.toggle_tag(s, exclude=True))
            chips.append(btn)
        self._tag_chips = chips
        flow.set_items(chips)

    def _build_detail_overview(self, d, parts):
        """The description, with the original and a translation on a switch.

        Nothing is translated until the user picks 中文. There are 1474 games
        and most are never opened, so paying per game only for the ones that
        are read is the whole point; once paid for, the result is cached and
        the switch is instant forever after.

        The label and the editor share a box, like the name above: only one of
        them is ever packed, so the blocks below never move.
        """
        head = ctk.CTkFrame(d, fg_color="transparent")
        ctk.CTkLabel(head, text="简介", text_color=MUTED,
                     font=ui_font(size=12)).pack(side="left")
        seg = ctk.CTkSegmentedButton(
            head, values=["原文", "中文"], height=24, width=104,
            font=ui_font(size=11), selected_color=ACCENT,
            selected_hover_color=ACCENT, command=self._on_overview_seg)
        seg.set("原文")
        seg.pack(side="right")
        ctk.CTkButton(head, text="✎ 改简介", width=84, height=24, corner_radius=6,
                      fg_color="transparent", text_color=ACCENT, hover_color=CHIP,
                      font=ui_font(size=11), command=self._begin_overview_edit
                      ).pack(side="right", padx=(0, 6))
        self._ov_seg = seg

        box = ctk.CTkFrame(d, fg_color="transparent")
        label = ctk.CTkLabel(box, text="", text_color=TEXT, wraplength=340,
                             justify="left", anchor="w", font=ui_font(size=12))
        label.pack(anchor="w")
        text = ctk.CTkTextbox(box, height=160, corner_radius=8, fg_color=BG,
                              text_color=TEXT, border_color=CHIP,
                              border_width=1, font=ui_font(size=12), wrap="word")
        text.bind("<Control-Return>", lambda e: self._save_overview_edit())
        text.bind("<Escape>", lambda e: self._cancel_overview_edit())
        save = ctk.CTkButton(box, text="保存", height=28, width=80, corner_radius=8,
                             fg_color=ACCENT, text_color=ON_ACCENT,
                             hover_color=CARD_HOVER, font=ui_font(size=12),
                             command=self._save_overview_edit)
        # The name's editor says this through its placeholder, and it is only
        # ever seen when the box is empty. A textbox has no placeholder, and an
        # empty one with no explanation reads as the app having lost the text.
        hint = ctk.CTkLabel(box, text="", text_color=MUTED, font=ui_font(size=11),
                            anchor="w", wraplength=340, justify="left")
        self._ov_label = label
        self._ov_text = text
        parts["ov_head"] = head
        parts["ov_seg"] = seg
        parts["ov_box"] = box
        parts["ov_label"] = label
        parts["ov_text"] = text
        parts["ov_save"] = save
        parts["ov_hint"] = hint
        return [("ov_head", head, {"fill": "x", "padx": 18, "pady": (12, 2)}),
                ("ov_box", box, {"fill": "x", "padx": 18, "pady": (0, 20)})]

    # --- in-place editing -----------------------------------------------------

    # Both editors rewrite the Chinese. 原文 is the site's own text and stays
    # read-only: a hand-typed name is a correction to the translation, so it
    # belongs on the 中文 side, and one field with one meaning is what makes the
    # switch predictable.
    #
    # What a correction must not do is destroy the thing it corrects - that is
    # the whole reason it goes to slg_db.manual_translations instead of over the
    # top of the cached row. Clearing the box puts the automatic translation
    # back exactly as it was.

    def _title_to_show(self, game):
        """The name above the description: the site's own, always."""
        return game["title"]

    def _overview_to_show(self, game):
        """The description: the site's own, always."""
        return game["overview"] or ""

    def _zh_title(self, game):
        """What 中文 shows for the name, or "" when there is nothing to show.

        Read through the same call the panel uses, so the box cannot open on
        text the panel is not showing - which it did: an untranslated marker
        stores the English name, and prefilling the box with it invited the user
        to "edit" the original.
        """
        return self._zh_of("title", game, game["title"])

    def _zh_overview(self, game):
        return self._zh_of("overview", game, game["overview"])

    def _zh_of(self, kind, game, src_text):
        row = slg_db.get_translation_row(self.conn, kind, game["id"], src_text)
        if row is None or row[1] == slg_db.ENGINE_UNTRANSLATED:
            return ""
        return row[0]

    def _begin_title_edit(self):
        game = self.selected
        if game is None:
            return
        entry = self._title_entry
        self._detail_parts["title"].pack_forget()
        entry.delete(0, "end")
        entry.insert(0, self._zh_title(game))
        entry.pack(fill="x")
        entry.focus_set()

    def _cancel_title_edit(self):
        self._detail_parts["title_entry"].pack_forget()
        self._detail_parts["title"].pack()

    def _save_title_edit(self):
        game = self.selected
        if game is None:
            return
        text = self._title_entry.get().strip()
        if text:
            slg_db.set_manual_translation(self.conn, "title", game["id"], text)
        else:
            # Empty means "put the translation back". Deleting the hand-typed
            # row - not the cached one - is what makes that true: the machine
            # translation was never touched, so it simply shows through again.
            slg_db.delete_manual_translation(self.conn, "title", game["id"])
        self._cancel_title_edit()
        # The cards read a module-level dict, and both the new hand-typed name
        # and the machine name returning have to reach it before the repaint.
        load_title_translations(self.conn)
        row = next((g for g in self.rows if g["id"] == game["id"]), None)
        if row is not None:
            self._repaint_card_title(row)
        self._show_zh(game)

    def _on_overview_seg(self, value):
        game = self.selected
        if game is not None:
            self._set_overview_lang(game, value)

    def _begin_overview_edit(self):
        game = self.selected
        if game is None:
            return
        p = self._detail_parts
        p["ov_label"].pack_forget()
        p["ov_save"].pack_forget()
        text = self._zh_overview(game)
        self._ov_text.delete("1.0", "end")
        self._ov_text.insert("1.0", text)
        p["ov_hint"].configure(
            text="留空 = 恢复自动翻译" if text else "还没译文，可直接输入中文简介")
        p["ov_text"].pack(fill="x")
        p["ov_save"].pack(anchor="e", pady=(4, 0))
        p["ov_hint"].pack(anchor="w")
        self._ov_text.focus_set()

    def _cancel_overview_edit(self):
        p = self._detail_parts
        p["ov_text"].pack_forget()
        p["ov_save"].pack_forget()
        p["ov_hint"].pack_forget()
        p["ov_label"].pack(anchor="w")

    def _save_overview_edit(self):
        game = self.selected
        if game is None:
            return
        text = self._ov_text.get("1.0", "end").strip()
        if text:
            slg_db.set_manual_translation(self.conn, "overview", game["id"], text)
        else:
            slg_db.delete_manual_translation(self.conn, "overview", game["id"])
        self._cancel_overview_edit()
        self._show_zh(game)

    def _show_zh(self, game):
        """Leave the panel on 中文 after an edit.

        The text just saved is the Chinese one, and staying on 原文 would hide
        it behind the original - which reads as the edit having failed.

        Not routed through _set_overview_lang, which is the switch's handler:
        that one starts a translation for whatever is missing, and hitting Enter
        in the name box should not quietly spend a request on the description
        beside it. Whatever is cached is shown, through the same lookup the box
        was filled from, so the two cannot disagree.
        """
        self._ov_seg.set("中文")
        self._show_title(self._zh_title(game) or game["title"])
        self._show_title_note("")
        self._set_ov_text(self._zh_overview(game)
                          or self._overview_to_show(game))

    # --- writes (everything lands in sqlite immediately) ----------------------

    def _set_status(self, status):
        game = self.selected
        new = None if game["status"] == status else status
        slg_db.set_state(self.conn, game["id"], status=new or "")
        # Written back into the row object too: refresh() replaces self.selected
        # with a freshly queried row, and a stale dict here would repaint the
        # old value over the new one.
        game["status"] = new or ""
        self._sync_status_btns(game)
        self._repaint_card_meta(game)
        if self.view:
            # Only a status-filtered view can gain or lose a member, and here it
            # always loses one: the game was in this view and just stopped being
            # so. Under 全部 the card stays on screen and needs no touching.
            self._drop_card(game["id"])
            self._render_stats()

    def _remove_from_collection(self):
        """Take the selected game out of the collection being browsed.

        The card is dropped in place rather than via a full refresh: under a
        collection filter the game just left the only view that shows it, which
        is the same remove-on-status-write case _set_status handles.
        """
        game = self.selected
        if game is None or self.collection_id is None:
            return
        slg_db.remove_from_collection(self.conn, game["id"], self.collection_id)
        self._drop_card(game["id"])
        self._refresh_collection_menu()
        self._render_stats()

    def _set_rating(self, value):
        game = self.selected
        new = 0 if game["my_rating"] == value else value
        slg_db.set_state(self.conn, game["id"], my_rating=new or "")
        slg_db.recompute_weights(self.conn)
        game["my_rating"] = new or None
        self._sync_star_btns(game)
        # Deliberately no refresh. recompute_weights() does move games around
        # under 按xp推荐, but reordering the list out from under the cursor is
        # worse than a sort that settles on the next redraw - and the rating
        # itself is not on the card, so there is nothing stale on screen.

    def _set_note(self):
        """Read the box and store it.

        Both callers - Ctrl+Enter and the 保存 button - read from the widget
        here rather than passing a value in, so there is one definition of what
        "the text" is. end-1c because a Textbox's get() ends with a newline of
        its own making, and storing that would put a blank last line into every
        saved review.
        """
        text = self._detail_parts["note_entry"].get("1.0", "end-1c").strip()
        self.selected["note"] = text
        slg_db.set_state(self.conn, self.selected["id"], note=text)
        self._set_progress("评价已保存" if text else "评价已清空")

    def _cancel_note_edit(self):
        """Put the stored value back, from the in-memory game rather than the
        db: _set_note always writes through to self.selected, so the two cannot
        disagree and this costs no query."""
        note = self.selected.get("note") or ""
        box = self._detail_parts["note_entry"]
        box.delete("1.0", "end")
        if note:
            box.insert("1.0", note)
        self._set_progress("评价已还原")

    def _share_screenshot(self):
        if not self.selected:
            return
        self.update()
        d = self.detail
        x, y = d.winfo_rootx(), d.winfo_rooty()
        box = (x, y, x + d.winfo_width(), y + d.winfo_height())
        img = ImageGrab.grab(bbox=box, all_screens=True)
        outdir = os.path.join(slg_db.app_dir(), "分享")
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, "%s_%s.png" % (
            self.selected.get("slug", "game"),
            time.strftime("%Y%m%d_%H%M%S")))
        img.save(path)
        self._set_progress("截图已保存：%s" % path)
        messagebox.showinfo("分享", "详情截图已保存到：\n%s" % path)

    # --- dialogs --------------------------------------------------------------

    def _new_dialog(self, title, geometry=None):
        """A dialog window with the two things every one of them needs.

        Escape closes it - Tk hands a bare Toplevel no bindings at all - and
        re-opening the same dialog reuses the window instead of stacking a
        second copy, which is what clicking 帮助文档 twice used to do.
        """
        for child in self.winfo_children():
            if isinstance(child, ctk.CTkToplevel) and child.title() == title:
                child.destroy()
        win = ctk.CTkToplevel(self)
        win.title(title)
        if geometry:
            win.geometry(geometry)
        win.transient(self)
        win.bind("<Escape>", lambda e: win.destroy())
        return win

    def _dialog_rows(self, win, entries, state="normal", parent=None):
        """A dialog body of rows: one button over one blurb, identically styled.

        同步与维护 and 更多工具 are the same shape and only differ in their text
        and one optional right-click - so the loop lives here instead of twice.

        parent separates the two jobs win used to do at once: the rows are built
        into parent when one is given (更多工具 puts them in a scrollable frame),
        while the row command still destroys the window - not the frame - so the
        whole dialog still closes before the next one opens.
        """
        body = parent if parent is not None else win
        for text, blurb, command, on_right in entries:
            btn = ctk.CTkButton(
                body, text=text, height=36, corner_radius=8, anchor="w",
                fg_color="transparent", text_color=TEXT, hover_color=CARD,
                font=ui_font(size=13), state=state,
                command=lambda c=command, w=win: (w.destroy(), c()))
            if on_right is not None:
                btn.bind("<Button-3>",
                         lambda e, c=on_right, w=win: (w.destroy(), c()))
            btn.pack(fill="x", padx=16, pady=(10, 0))
            ctk.CTkLabel(body, text=blurb, text_color=MUTED,
                         font=ui_font(size=11), justify="left",
                         wraplength=380).pack(fill="x", padx=22, pady=(2, 0))

    # --- profile / points / titles ---------------------------------------------

    def _title_color(self, title_id):
        t = slg_titles.title_by_id(title_id)
        return slg_titles.RARITY_COLORS.get(t["rarity"], MUTED) if t else MUTED

    def _obtain_hint(self, t):
        if t["obtain"] == "default":
            return "默认"
        if t["obtain"] == "code":
            return "兑换码获取"
        if t["obtain"] == "shop":
            hint = "积分兑换（%d 分）" % t["cost"]
            if t.get("limited_until"):
                hint += " · 限时"
            return hint
        return ""

    def _title_badge(self, parent, title_id, animate=True):
        """A rarity badge drawn on a canvas, so the equipped title can glow.

        普通/稀有 are static; 史诗 breathes, 传说 gets a sweeping shine and 至臻
        cycles through a rainbow ramp. The rarity names never show - the colour
        and the motion are the whole signal.
        """
        t = slg_titles.title_by_id(title_id) or {"name": "普通用户", "rarity": "普通"}
        rarity = t["rarity"]
        color = slg_titles.RARITY_COLORS.get(rarity, MUTED)
        name = t["name"]
        font = ui_tkfont(size=12, weight="bold")
        w = max(64, font.measure(name) + 40)
        h = 34
        c = tk.Canvas(parent, width=w, height=h, highlightthickness=0, bg=BG)
        tint = _mix(BG, color, 0.16)
        r = h // 2
        c.create_oval(0, 0, h, h, fill=tint, outline="")
        c.create_oval(w - h, 0, w, h, fill=tint, outline="")
        c.create_rectangle(r, 0, w - r, h, fill=tint, outline="")
        text_item = c.create_text(w // 2, h // 2, text=name, fill=color, font=font)
        shine = c.create_rectangle(-44, h * 0.12, -22, h * 0.88,
                                   fill=_mix(color, "#ffffff", 0.75), outline="")
        if not animate or rarity in ("普通", "稀有"):
            c.itemconfig(shine, state="hidden")
            return c

        frame = {"n": 0}

        def tick():
            try:
                if not c.winfo_exists():
                    return
            except tk.TclError:
                return
            frame["n"] += 1
            n = frame["n"]
            if rarity == "史诗":
                p = 1 - abs(2 * ((n % 40) / 39.0) - 1)
                c.itemconfig(text_item, fill=_mix(color, "#ffffff", 0.35 * p))
            elif rarity == "传说":
                x = -44 + (n * 4) % (w + 66)
                c.coords(shine, x, h * 0.12, x + 22, h * 0.88)
                c.itemconfig(shine, state="normal")
            elif rarity == "至臻":
                ramp = ("#e84393", "#e06a3f", "#e0a800", "#3fae5a", "#2f9bd0", "#8b5cf6")
                c.itemconfig(text_item, fill=ramp[n % len(ramp)])
            c.after(40, tick)

        c.after(40, tick)
        return c

    def _equip_title(self, win, title_id):
        if title_id == slg_titles.DEFAULT_TITLE_ID:
            slg_db.set_equipped_title(self.conn, "")
        else:
            slg_db.set_equipped_title(self.conn, title_id)
        win.destroy()
        self.open_titles()

    def _do_signin(self, win, sign_btn, bal_label):
        already, _day, gained = slg_titles.signin(self.conn)
        if already:
            return
        sign_btn.configure(text="今日已签到", state="disabled")
        bal_label.configure(text="积分：%d" % slg_db.points_balance(self.conn))

    def _edit_nickname(self, parent):
        win = self._new_dialog("修改昵称", "340x180")
        cur = slg_db.get_pref(self.conn, "profile.nickname", "") or ""
        ctk.CTkLabel(win, text="设置你的昵称（本地保存，随时可改）", text_color=TEXT,
                     font=ui_font(size=13)).pack(fill="x", padx=16, pady=(16, 8))
        entry = ctk.CTkEntry(win, placeholder_text="昵称…", height=32, corner_radius=8,
                             fg_color=CARD, text_color=TEXT,
                             placeholder_text_color=MUTED, border_width=1,
                             border_color=CHIP, font=ui_font(size=13))
        entry.insert(0, cur)
        entry.pack(fill="x", padx=16)

        def save():
            name = entry.get().strip()
            if not name:
                return
            slg_db.set_pref(self.conn, "profile.nickname", name)
            if not slg_db.get_pref(self.conn, "profile.registered_at"):
                slg_db.set_pref(self.conn, "profile.registered_at",
                                time.strftime("%Y-%m-%d %H:%M:%S"))
            win.destroy()
            parent.destroy()
            self.open_profile()

        ctk.CTkButton(win, text="保存", height=32, width=90, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=12), command=save).pack(pady=(12, 0))

    def open_profile(self):
        win = self._new_dialog("个人中心", "420x560")
        nickname = slg_db.get_pref(self.conn, "profile.nickname", "") or ""
        equipped = slg_db.get_equipped_title(self.conn) or slg_titles.DEFAULT_TITLE_ID
        day = slg_titles.today_str()
        signed = slg_db.last_signin_day(self.conn) == day

        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(16, 8))
        ctk.CTkLabel(head, text=(nickname[:1] or "游"), width=48, height=48,
                     corner_radius=24, fg_color=ACCENT, text_color=ON_ACCENT,
                     font=ui_font(size=20, weight="bold")).pack(side="left")
        info = ctk.CTkFrame(head, fg_color="transparent")
        info.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(info, text=nickname or "未设置昵称", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(anchor="w")
        self._title_badge(info, equipped).pack(anchor="w", pady=(6, 0))

        bal_label = ctk.CTkLabel(win, text="积分：%d" % slg_db.points_balance(self.conn),
                                 text_color=TEXT, font=ui_font(size=13))
        bal_label.pack(anchor="w", padx=16, pady=(14, 0))
        sign_btn = ctk.CTkButton(
            win, text="今日已签到" if signed else "签到（+%d 分）" % slg_titles.DAILY_SIGNIN_POINTS,
            height=36, corner_radius=8, fg_color=ACCENT, text_color=ON_ACCENT,
            hover_color=CARD_HOVER, font=ui_font(size=13),
            state="disabled" if signed else "normal",
            command=lambda: self._do_signin(win, sign_btn, bal_label))
        sign_btn.pack(fill="x", padx=16, pady=(6, 0))

        ctk.CTkButton(win, text="修改昵称", height=32, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      font=ui_font(size=12),
                      command=lambda: self._edit_nickname(win)).pack(
            fill="x", padx=16, pady=(10, 0))

        ctk.CTkLabel(win, text="——————", text_color=MUTED,
                     font=ui_font(size=11)).pack(pady=(16, 4))
        for text, fn in (("查看头衔", self.open_titles),
                         ("积分商城", self.open_shop),
                         ("兑换码", self.open_redeem)):
            ctk.CTkButton(win, text=text, height=36, corner_radius=8, anchor="w",
                          fg_color="transparent", text_color=TEXT,
                          hover_color=CARD, font=ui_font(size=13),
                          command=lambda f=fn: (win.destroy(), f())).pack(
                fill="x", padx=16, pady=(6, 0))

    def open_titles(self):
        win = self._new_dialog("我的头衔", "360x520")
        ctk.CTkLabel(win, text="我的头衔", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=16, pady=(14, 8))
        owned = slg_db.owned_title_ids(self.conn) | {slg_titles.DEFAULT_TITLE_ID}
        equipped = slg_db.get_equipped_title(self.conn) or slg_titles.DEFAULT_TITLE_ID
        box = ctk.CTkScrollableFrame(win, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        for t in slg_titles.TITLES:
            tid = t["id"]
            is_owned = tid in owned
            is_equipped = tid == equipped
            color = slg_titles.RARITY_COLORS.get(t["rarity"], MUTED)
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=4)
            label = t["name"] + ("（使用中）" if is_equipped else "")
            ctk.CTkLabel(row, text=label, text_color=color if is_owned else MUTED,
                         font=ui_font(size=13, weight="bold")).pack(side="left")
            if is_owned:
                ctk.CTkButton(row, text="取消" if is_equipped else "装备", width=64,
                              height=28, corner_radius=8, fg_color=CHIP,
                              text_color=TEXT, hover_color=CARD_HOVER,
                              font=ui_font(size=12),
                              command=lambda tid=tid: self._equip_title(win, tid)
                              ).pack(side="right")
            else:
                ctk.CTkLabel(row, text=self._obtain_hint(t), text_color=MUTED,
                             font=ui_font(size=11)).pack(side="right")

    def open_shop(self):
        win = self._new_dialog("积分商城", "380x520")
        ctk.CTkLabel(win, text="积分商城", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(win, text="当前积分：%d" % slg_db.points_balance(self.conn),
                     text_color=TEXT, font=ui_font(size=13)).pack(
            anchor="w", padx=16, pady=(0, 8))
        owned = slg_db.owned_title_ids(self.conn)
        box = ctk.CTkScrollableFrame(win, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        for item in slg_titles.available_shop_items():
            row = ctk.CTkFrame(box, fg_color=CARD, corner_radius=8)
            row.pack(fill="x", padx=4, pady=6)
            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            ctk.CTkLabel(left, text=item["name"],
                         text_color=self._title_color(item["id"]),
                         font=ui_font(size=13, weight="bold")).pack(anchor="w")
            locked = item.get("locked")
            cost_text = "%d 分" % item["cost"]
            if locked:
                cost_text = item.get("note") or "即将开放"
            ctk.CTkLabel(left, text=cost_text, text_color=MUTED,
                         font=ui_font(size=11)).pack(anchor="w")
            if locked:
                ctk.CTkLabel(row, text="即将开放", text_color=MUTED,
                             font=ui_font(size=11)).pack(side="right", padx=12)
            elif item["id"] in owned:
                ctk.CTkLabel(row, text="已拥有", text_color=MUTED,
                             font=ui_font(size=11)).pack(side="right", padx=12)
            else:
                ctk.CTkButton(row, text="兑换", width=64, height=28, corner_radius=8,
                              fg_color=ACCENT, text_color=ON_ACCENT,
                              hover_color=CARD_HOVER, font=ui_font(size=12),
                              command=lambda i=item: self._buy_item(win, i)
                              ).pack(side="right", padx=12)

    def _buy_item(self, win, item):
        if slg_titles.buy(self.conn, item):
            messagebox.showinfo("兑换成功", "已获得「%s」" % item["name"], parent=win)
            win.destroy()
            self.open_shop()
        else:
            messagebox.showwarning("积分不足", "积分不足，无法兑换", parent=win)

    def open_redeem(self):
        win = self._new_dialog("兑换码", "360x240")
        ctk.CTkLabel(win, text="输入头衔兑换码", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=16, pady=(16, 4))
        ctk.CTkLabel(win, text="部分头衔需通过兑换码解锁", text_color=MUTED,
                     font=ui_font(size=11)).pack(anchor="w", padx=16, pady=(0, 10))
        entry = ctk.CTkEntry(win, placeholder_text="兑换码…", height=34, corner_radius=8,
                             fg_color=CARD, text_color=TEXT,
                             placeholder_text_color=MUTED, border_width=1,
                             border_color=CHIP, font=ui_font(size=13))
        entry.pack(fill="x", padx=16)
        feedback = ctk.CTkLabel(win, text="", text_color=MUTED, font=ui_font(size=12))
        feedback.pack(anchor="w", padx=16, pady=(8, 0))

        def do():
            ok, msg = slg_titles.redeem(self.conn, entry.get())
            feedback.configure(text=msg, text_color=ACCENT if ok else DANGER_TEXT)

        ctk.CTkButton(win, text="兑换", height=34, corner_radius=8, fg_color=ACCENT,
                      text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=13), command=do).pack(fill="x", padx=16, pady=(12, 0))

    def _open_collect_dialog(self):
        """Check off which collections the selected game belongs to."""
        game = self.selected
        if game is None:
            return
        mine = set(slg_db.collections_for_game(self.conn, game["id"]))
        cols = slg_db.list_collections(self.conn)

        win = self._new_dialog("收藏夹", "360x480")
        ctk.CTkLabel(win, text="把「%s」加入收藏夹" % self._title_to_show(game),
                     text_color=TEXT, font=ui_font(size=13, weight="bold"),
                     wraplength=320, justify="left").pack(
            fill="x", padx=16, pady=(14, 8))

        box = ctk.CTkScrollableFrame(win, fg_color="transparent", height=250)
        box.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        checks = {}
        if cols:
            for c in cols:
                var = tk.BooleanVar(value=(c["id"] in mine))
                ctk.CTkCheckBox(box, text="%s（%d 款）" % (c["name"], c["count"]),
                                variable=var, font=ui_font(size=13)
                                ).pack(fill="x", padx=8, pady=3)
                checks[c["id"]] = var
        else:
            ctk.CTkLabel(box, text="还没有收藏夹。下面输入名字新建一个。",
                         text_color=MUTED, font=ui_font(size=12)).pack(pady=12)

        entry = ctk.CTkEntry(win, placeholder_text="新建收藏夹名字…", height=32,
                             corner_radius=8, fg_color=CARD, text_color=TEXT,
                             placeholder_text_color=MUTED, border_width=1,
                             border_color=CHIP, font=ui_font(size=13))
        entry.pack(fill="x", padx=16, pady=(4, 4))

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4, 14))

        def add():
            name = entry.get().strip()
            if name and slg_db.create_collection(self.conn, name):
                entry.delete(0, "end")
                self._refresh_collection_menu()
                win.destroy()
                self._open_collect_dialog()

        def save():
            chosen = [cid for cid, var in checks.items() if var.get()]
            slg_db.set_game_collections(self.conn, game["id"], chosen)
            self._refresh_collection_menu()
            win.destroy()
            self.refresh()

        ctk.CTkButton(row, text="新建", width=90, height=32, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      command=add).pack(side="left")
        ctk.CTkButton(row, text="保存", width=90, height=32, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      command=save).pack(side="right")

    def open_collection_manager(self):
        """Create and delete collections, from 更多工具."""
        win = self._new_dialog("管理收藏夹", "360x460")
        ctk.CTkLabel(win, text="新建或删除收藏夹", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=16, pady=(14, 8))
        entry = ctk.CTkEntry(win, placeholder_text="新建收藏夹名字…", height=32,
                             corner_radius=8, fg_color=CARD, text_color=TEXT,
                             placeholder_text_color=MUTED, border_width=1,
                             border_color=CHIP, font=ui_font(size=13))
        entry.pack(fill="x", padx=16, pady=(0, 4))

        def add():
            name = entry.get().strip()
            if name and slg_db.create_collection(self.conn, name):
                entry.delete(0, "end")
                self._refresh_collection_menu()
                win.destroy()
                self.open_collection_manager()

        ctk.CTkButton(win, text="新建", height=32, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=CARD_HOVER, command=add).pack(
            fill="x", padx=16, pady=(0, 8))

        box = ctk.CTkScrollableFrame(win, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        cols = slg_db.list_collections(self.conn)
        if not cols:
            ctk.CTkLabel(box, text="还没有收藏夹。", text_color=MUTED,
                         font=ui_font(size=12)).pack(pady=12)
        for c in cols:
            row = ctk.CTkFrame(box, fg_color=CARD, corner_radius=8)
            row.pack(fill="x", padx=6, pady=3)
            ctk.CTkLabel(row, text="%s · %d 款" % (c["name"], c["count"]),
                         text_color=TEXT, font=ui_font(size=13)).pack(
                side="left", padx=12, pady=8)
            ctk.CTkButton(row, text="删除", width=60, height=26, corner_radius=6,
                          fg_color="transparent", text_color=DANGER_TEXT,
                          hover_color=CHIP, font=ui_font(size=12),
                          command=lambda cid=c["id"]: self._delete_collection_from_manager(
                              cid, win)).pack(side="right", padx=8)

    def _delete_collection(self, cid):
        """Delete one collection after confirming; True if it actually went."""
        if not messagebox.askyesno("删除收藏夹", "确定删除这个收藏夹？其中的游戏不受影响。"):
            return False
        slg_db.delete_collection(self.conn, cid)
        if self.collection_id == cid:
            self.collection_id = None
        self._refresh_collection_menu()
        self.refresh()
        return True

    def _delete_collection_from_manager(self, cid, win):
        """The manager's delete: run the shared delete, then refresh its list."""
        if self._delete_collection(cid):
            win.destroy()
            self.open_collection_manager()

    def open_backup(self):
        """Export or import the user's own data, from 更多工具."""
        win = self._new_dialog("备份与恢复", "420x360")
        ctk.CTkLabel(win, text="备份与恢复", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=20, pady=(18, 4))
        ctk.CTkLabel(win, text="导出把评分、备注、状态、收藏夹和标签排除存成 json；\n"
                               "导入用文件覆盖这些数据。不含密钥与机器翻译缓存。",
                     text_color=MUTED, font=ui_font(size=12), justify="left",
                     anchor="w").pack(fill="x", padx=20, pady=(0, 12))

        def do_export():
            path = filedialog.asksaveasfilename(
                parent=win, title="导出备份", defaultextension=".json",
                initialfile="slgking-备份-%s.json" % time.strftime("%Y%m%d"),
                filetypes=[("JSON 文件", "*.json")])
            if not path:
                return
            try:
                data = slg_db.export_user_data(self.conn)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"app": "slgking", "version": 1, "data": data},
                              f, ensure_ascii=False, indent=2)
                win.destroy()
                self._set_progress("已导出到 %s" % path)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("导出失败", str(exc))

        def do_import():
            path = filedialog.askopenfilename(
                parent=win, title="导入备份",
                filetypes=[("JSON 文件", "*.json")])
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                slg_db.import_user_data(self.conn, payload.get("data", payload))
                win.destroy()
                self._refresh_collection_menu()
                self.refresh()
                self._set_progress("已从备份恢复")
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("导入失败", str(exc))

        ctk.CTkButton(win, text="导出数据", height=38, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=CARD_HOVER, command=do_export).pack(
            fill="x", padx=20, pady=(0, 8))
        ctk.CTkButton(win, text="导入数据", height=38, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT,
                      hover_color=CARD_HOVER, command=do_import).pack(
            fill="x", padx=20, pady=(0, 12))

    def open_tag_picker(self):
        """Browse and select tags without losing the window on every click.

        Picking a tag used to destroy the dialog, so choosing four meant four
        round trips. Now the selection applies live and the window stays open.
        The search box and the two-column grid are for the other half of the
        complaint: the vocabulary passed 130 tags and a single column of them
        was a long scroll to a tag whose name the user already knew.
        """
        win = self._new_dialog("标签库", "580x620")
        head = ctk.CTkLabel(win, text="", text_color=MUTED, font=ui_font(size=12))
        head.pack(pady=(10, 6))
        entry = ctk.CTkEntry(win, placeholder_text="搜索标签…", height=32,
                             corner_radius=8, fg_color=CARD, text_color=TEXT,
                             placeholder_text_color=MUTED, border_width=1,
                             border_color=CHIP, font=ui_font(size=13))
        entry.pack(fill="x", padx=12)
        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(8, 6))
        footer = ctk.CTkFrame(win, fg_color="transparent")
        footer.pack(fill="x", padx=12, pady=(0, 10))

        # Read once: the counts cannot change while this window is open, and
        # re-querying on every keystroke would put a GROUP BY behind the search
        # box. Matching is case-folded against both the translated name and the
        # slug, because the user may know either.
        rows = slg_db.tag_counts(self.conn)

        def redraw(*_):
            for child in frame.winfo_children():
                child.destroy()
            needle = entry.get().strip().lower()
            shown = [row for row in rows
                     if not needle
                     or needle in display_tag(row["name"]).lower()
                     or needle in row["name"].lower()]
            for i, row in enumerate(shown):
                slug = row["name"]
                state = (" √" if slug in self.include
                         else " ×" if slug in self.exclude else "")
                btn = ctk.CTkButton(
                    frame, text="%s    %d%s" % (display_tag(slug), row["n"], state),
                    anchor="w", height=30, corner_radius=6, fg_color="transparent",
                    text_color=TEXT, hover_color=CHIP, font=ui_font(size=13),
                    command=lambda s=slug: pick(s))
                btn.bind("<Button-3>", lambda e, s=slug: pick(s, exclude=True))
                btn.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=1)
            if not shown:
                ctk.CTkLabel(frame, text="没有匹配的标签", text_color=MUTED,
                             font=ui_font(size=12)).grid(row=0, column=0,
                                                         padx=8, pady=14)
            head.configure(text="左键加入筛选 · 右键排除　|　已选 %d · 排除 %d"
                                % (len(self.include), len(self.exclude)))

        def pick(slug, exclude=False):
            self.toggle_tag(slug, exclude=exclude)
            redraw()

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        entry.bind("<KeyRelease>", redraw)
        ctk.CTkButton(footer, text="清空", height=34, corner_radius=8,
                      fg_color="transparent", text_color=TEXT, hover_color=CARD,
                      font=ui_font(size=13),
                      command=lambda: (self.clear_filters(), redraw())
                      ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(footer, text="完成", height=34, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD,
                      font=ui_font(size=13),
                      command=win.destroy).pack(side="right", fill="x",
                                                expand=True, padx=(6, 0))
        redraw()

    def open_maintenance(self):
        """The two chores that are not the routine sync.

        One dialog rather than two sidebar buttons: none of them is what a
        new user is looking for, and each is only safe to click with a sentence
        of explanation attached. Identical styling on both, so what tells
        them apart is the text and nothing else.
        """
        win = self._new_dialog("同步与维护", "440x560")
        gaps = slg_db.data_gaps(self.conn)
        entries = (
            ("下载封面（%d）" % gaps["covers"] if gaps["covers"] else "封面已齐",
             "从服务器补下缺失的封面缩略图。可随时停止，下次接着下。",
             self.do_covers, None),
            ("补齐热度（%d）" % gaps["heat"] if gaps["heat"] else "热度已齐",
             "本地重算热度，不联网。",
             self.do_heat, None),
        )
        # The dialog cannot normally be opened mid-job - _start_job disables the
        # button that leads here - but the job may have started from the sync
        # button while this was already on screen.
        state = "disabled" if self.busy else "normal"
        self._dialog_rows(win, entries, state=state)

    def open_tools(self):
        """The set-once tools, behind one door.

        Same shape as 同步与维护 and for the same reason: none of these is what
        the window is for, and as extra rows in the sidebar's 工具 group
        they buried the routine controls. The toolbar gear no longer lands here
        - it opens 设置, which holds the theme switch and a way into 关于.

        Each row closes this window before opening its own, so the two dialogs
        cannot stack - and the next visit rebuilds the list, which is what keeps
        the numbers in the blurbs below honest.
        """
        root = self.scan_root()
        scan_blurb = ("把本地游戏库对上号，记录版本号。右键换文件夹。\n当前：%s"
                      % os.path.basename(root)) if root else \
                     "把本地游戏库对上号，记录版本号。点它或右键先选文件夹。"
        win = self._new_dialog("更多工具", "440x580")
        ctk.CTkLabel(win, text="不常用的和设一次就够的都在这里。\n"
                               "同步与维护在左侧栏的「更多…」里。",
                     text_color=MUTED, font=ui_font(size=12), justify="left",
                     anchor="w").pack(fill="x", padx=16, pady=(12, 0))
        # The intro stays outside the scroll: it is the paragraph that says what
        # this window is, so it should not scroll away. The rows go in a
        # scrollable frame instead of straight into the window - the height above
        # is a literal, and at 150% scaling seven rows need more than it holds,
        # which is what clipped 扫描本地目录 off the bottom. Adding an eighth row
        # now costs nothing but a scrollbar.
        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=0, pady=(4, 10))
        entries = (
            ("检查更新",
             "看看本地哪些游戏落后于站点新版。",
             self.do_updates, None),
            ("管理收藏夹…",
             "新建或删除收藏夹，整理你的个人游戏库。",
             self.open_collection_manager, None),
            ("备份与恢复…",
             "把评分、备注、收藏、标签排除导出成文件，或从文件恢复。",
             self.open_backup, None),
            ("游戏汉化工具…",
             "游戏是英文的？这里有搭配使用的翻译工具。",
             self.open_translation_tools, None),
            ("标签译名…",
             "给标签写中文名。改完列表和筛选条立刻跟着变。",
             self.open_tag_editor, None),
            ("偏好权重…",
             "从你的五星评分里算出来的标签倾向，正数是你喜欢的。",
             self.open_weights, None),
            ("翻译设置…",
             "配置名称和简介用的翻译引擎与密钥。",
             self.open_translate_settings, None),
            ("扫描本地目录…",
             scan_blurb,
             self.do_scan, self.pick_scan_root),
        )
        state = "disabled" if self.busy else "normal"
        self._dialog_rows(win, entries, state=state, parent=body)

    def open_translation_tools(self):
        """The 游戏汉化工具 door: the answer to "my game is in English".

        The two links are the same ones 帮助文档 carries, but here they are the
        whole point instead of a paragraph buried at the foot of a long page.
        They are not two alternatives: the toolkit needs Luna, so the dialog
        walks the user through them in order.
        """
        win = self._new_dialog("游戏汉化工具", "480x400")
        ctk.CTkLabel(win, text="游戏是英文的？按顺序装这两个工具",
                     text_color=TEXT, anchor="w",
                     font=ui_font(size=16, weight="bold")).pack(
            fill="x", padx=20, pady=(18, 0))
        ctk.CTkLabel(win, text="dikgames 是英文流站点，绝大多数游戏没有官方中文，"
                               "下载到英文版是正常的，不是文件坏了。",
                     text_color=MUTED, font=ui_font(size=12), justify="left",
                     anchor="w", wraplength=420).pack(
            fill="x", padx=20, pady=(8, 0))
        ctk.CTkLabel(win, text="第 1 步 · 下载露娜翻译器（必须先装）",
                     text_color=TEXT, font=ui_font(size=12, weight="bold"),
                     justify="left", anchor="w").pack(
            fill="x", padx=20, pady=(14, 4))
        _link_button(win, LUNA_LABEL, LUNA_URL).pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(win, text="开源免费，边玩边实时机翻游戏文本。",
                     text_color=MUTED, font=ui_font(size=11), justify="left",
                     anchor="w").pack(fill="x", padx=20, pady=(0, 12))
        ctk.CTkLabel(win, text="第 2 步 · 下载作者的汉化小工具（搭配露娜使用）",
                     text_color=TEXT, font=ui_font(size=12, weight="bold"),
                     justify="left", anchor="w").pack(
            fill="x", padx=20, pady=(0, 4))
        _link_button(win, RPYKIT_LABEL, RPYKIT_URL).pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(win, text="配合露娜翻译器，把 RenPy 游戏做成离线汉化。",
                     text_color=MUTED, font=ui_font(size=11), justify="left",
                     anchor="w").pack(fill="x", padx=20, pady=(0, 18))

    def open_settings(self):
        """The gear's door: the theme switch, with 关于 one row inside it.

        The theme switch used to sit in the toolbar, in the row the sort
        controls now occupy. It is a choice you make once and forget, and the
        three buttons it needed were the widest thing in the header - so it
        moved in here and the sort controls moved up into the space, which also
        left room for the gear beside them.
        """
        win = self._new_dialog("设置", "420x360")
        ctk.CTkLabel(win, text="主题", text_color=TEXT, anchor="w",
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=16, pady=(16, 0))
        switch = ctk.CTkSegmentedButton(
            win, values=[_THEME_LABELS[m] for m in ("light", "dark", "system")],
            height=34, corner_radius=8, fg_color=CARD, selected_color=ACCENT,
            selected_hover_color=ACCENT, unselected_color=CARD,
            unselected_hover_color=CARD_HOVER, text_color=TEXT,
            font=ui_font(size=13),
            command=lambda label: self._pick_theme_from(win, label))
        switch.pack(fill="x", padx=16, pady=(8, 0))
        switch.set(_THEME_LABELS[self.theme_mode])
        ctk.CTkLabel(win, text="「跟随系统」每 5 秒采样一次 Windows 的浅色/深色设置，"
                               "所以会有一小段延迟；手动选浅色或深色则会被记住。",
                     text_color=MUTED, font=ui_font(size=11), justify="left",
                     anchor="w", wraplength=380).pack(fill="x", padx=16, pady=(6, 14))
        self._dialog_rows(win, (
            ("查看新手引导…",
             "重新打开首次启动时的那份功能简介和使用说明。",
             self._show_welcome, None),
            ("关于本软件…",
             "版本信息、检查软件更新、GitHub 主页、反馈邮箱、数据目录。",
             self.open_about, None),
        ))

    def _pick_theme_from(self, win, label):
        """Close the settings dialog, then change the theme.

        Order matters: _apply_theme tears the whole window down and builds it
        again, so a dialog left open across that call would be the one thing on
        screen the new palette never reached.
        """
        win.destroy()
        self._on_theme_pick(label)

    def open_about(self):
        """The system-level door: version, update check, links, and the data dir.

        Distinct from 更多工具… on purpose - that dialog holds the set-once
        translation/tag/scan tools, while this one is about the app itself.
        """
        # 520 rather than 480: the group row pushed the 完全免费 line past the
        # bottom edge, and a disclaimer nobody can scroll to is not a disclaimer.
        win = self._new_dialog("关于", "440x620")
        ctk.CTkLabel(win, text="", image=load_avatar(84)).pack(pady=(18, 6))
        ctk.CTkLabel(win, text=APP_TITLE, text_color=TEXT,
                     font=ui_font(size=18, weight="bold")).pack(pady=(0, 0))
        ctk.CTkLabel(win, text="作者 · %s" % AUTHOR, text_color=MUTED,
                     font=ui_font(size=12)).pack()
        ctk.CTkLabel(win, text="版本 " + build_stamp(), text_color=MUTED,
                     font=ui_font(size=12)).pack(pady=(2, 12))

        self._about_status = ctk.CTkLabel(win, text="", text_color=ACCENT,
                                          font=ui_font(size=12), wraplength=380,
                                          justify="left")
        self._about_status.pack(fill="x", padx=24, pady=(0, 8))
        ctk.CTkButton(win, text="检查软件更新", height=36, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      font=ui_font(size=13),
                      command=lambda: self._start_update_check(force=True)
                      ).pack(fill="x", padx=24, pady=(0, 4))

        _link_button(win, GITHUB_LABEL, GITHUB_URL).pack(
            fill="x", padx=24, pady=(10, 4))
        _link_button(win, LUNA_LABEL, LUNA_URL).pack(
            fill="x", padx=24, pady=4)
        _link_button(win, RPYKIT_LABEL, RPYKIT_URL).pack(
            fill="x", padx=24, pady=4)
        _link_button(win, "反馈 / 建议：%s" % CONTACT_EMAIL, CONTACT_MAILTO).pack(
            fill="x", padx=24, pady=4)
        _copy_button(win, QQ_GROUP_LABEL, QQ_GROUP, self).pack(
            fill="x", padx=24, pady=4)
        ctk.CTkButton(win, text="打开数据目录", height=36, corner_radius=8,
                      fg_color="transparent", text_color=TEXT, hover_color=CARD,
                      font=ui_font(size=13),
                      command=self._open_data_dir).pack(
            fill="x", padx=24, pady=(4, 0))

        ctk.CTkLabel(win, text="本软件完全免费。没有收费版、没有付费激活、没有隐藏收费入口。\n"
                               "如果你是通过付费渠道拿到它的，请立即举报。",
                     text_color=DANGER_TEXT, font=ui_font(size=11), wraplength=380,
                     justify="left").pack(fill="x", padx=24, pady=(16, 16))

    @staticmethod
    def _open_data_dir():
        try:
            os.startfile(slg_db.app_dir())
        except OSError:
            pass

    def open_weights(self):
        win = self._new_dialog("偏好权重", "420x560")
        ctk.CTkLabel(win, text="从你的五星评分里算出来的标签倾向（已按标签稀缺度加权）\n"
                               "正数 = 你喜欢，负数 = 你不喜欢",
                     text_color=MUTED, font=ui_font(size=12),
                     justify="left").pack(pady=10, anchor="w", padx=16)
        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        rows = slg_db.weight_table(self.conn)
        if not rows:
            ctk.CTkLabel(frame, text="还没评过分。在右侧点星星，权重就会出来。",
                         text_color=MUTED).pack(pady=30)
        for row in rows:
            sign = "+" if row["weight"] >= 0 else ""
            ctk.CTkLabel(
                frame, anchor="w", text_color=TEXT, font=ui_font(size=13),
                text="%-28s %s%.2f   (%d 款)" % (display_tag(row["name"]), sign,
                                                 row["weight"], row["sample_count"])
            ).pack(fill="x", pady=2, padx=6)

    def open_tag_editor(self):
        """Hand-edit the Chinese of any tag in the library.

        Machine translation gets most tags right and a handful wrong, and the
        wrong ones are the tags the user sees on every card. Editing them here
        is one pass over the whole vocabulary; the alternative is living with
        "big-tits → 大胸" forever because one chunk boundary landed badly.

        A hand-typed tag is stored as ENGINE_MANUAL, so a later translation run
        skips it while leaving every other tag alone.
        """
        win = self._new_dialog("标签译名", "520x640")
        ctk.CTkLabel(
            win, justify="left", text_color=MUTED, font=ui_font(size=12),
            text="左边的英文是标签原文，右边是显示在卡片上的中文。\n"
                 "清空一格 = 这一条退回英文；改完点「保存全部」。"
            ).pack(pady=10, anchor="w", padx=16)

        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        frame.grid_columnconfigure(1, weight=1)

        entries = {}
        slugs = slg_db.all_tags(self.conn)
        if not slugs:
            ctk.CTkLabel(frame, text="库里还没有标签，先同步一次。",
                         text_color=MUTED).pack(pady=30)
        for i, slug in enumerate(slugs):
            ctk.CTkLabel(frame, text=slug, text_color=MUTED, anchor="w",
                         font=ui_font(size=12)).grid(row=i, column=0, sticky="w",
                                                     padx=(6, 10), pady=2)
            entry = ctk.CTkEntry(frame, height=28, corner_radius=6, fg_color=BG,
                                 text_color=TEXT, border_color=CHIP,
                                 font=ui_font(size=12))
            entry.insert(0, _TAG_ZH.get(slug, ""))
            entry.grid(row=i, column=1, sticky="ew", pady=2)
            entries[slug] = entry

        status = ctk.CTkLabel(win, text="", text_color=MUTED, font=ui_font(size=12))
        status.pack(anchor="w", padx=16)
        # Held on the window rather than closed over: the two buttons below are
        # the only callers, and this way the dialog can be driven without one.
        self._tag_editor = {"win": win, "entries": entries, "status": status}

        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkButton(buttons, text="保存全部", height=32, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      command=self._save_tag_edits).pack(side="left")
        ctk.CTkButton(buttons, text="清除手工译名", height=32, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT,
                      command=self._clear_tag_edits).pack(side="left", padx=8)
        ctk.CTkButton(buttons, text="关闭", height=32, corner_radius=8,
                      fg_color="transparent", text_color=TEXT, hover_color=CHIP,
                      command=win.destroy).pack(side="right")

    def _save_tag_edits(self):
        editor = self._tag_editor
        if editor is None:
            return
        entries = editor["entries"]
        changed = 0
        for slug, entry in entries.items():
            if not entry.winfo_exists():
                continue
            text = entry.get().strip()
            if text == (_TAG_ZH.get(slug) or ""):
                continue  # untouched; rewriting it would only churn the table
            if text:
                slg_db.set_manual_translation(self.conn, "tag", slug, text)
            elif not slg_db.delete_manual_translation(self.conn, "tag", slug):
                # Nothing hand-typed to remove, so what the box was showing is
                # the machine translation - and emptying it is the only way to
                # say "drop that too".
                slg_db.delete_translation(self.conn, "tag", slug)
            changed += 1
        self._reload_tags(editor["status"],
                          "已保存 %d 条。" % changed if changed else "没有改动。")

    def _clear_tag_edits(self):
        editor = self._tag_editor
        if editor is None:
            return
        gone = slg_db.delete_manual_translations(self.conn, "tag")
        self._reload_tags(editor["status"],
                          "已清除 %d 条手工译名，其余自动翻译没动。" % gone)

    def _reload_tags(self, status, message):
        """Reload the dictionaries and repaint both places a tag is rendered."""
        load_tag_translations(self.conn)
        if status is not None and status.winfo_exists():
            status.configure(text=message)
        for slug, entry in (self._tag_editor or {}).get("entries", {}).items():
            if entry.winfo_exists():
                entry.delete(0, "end")
                entry.insert(0, _TAG_ZH.get(slug, ""))
        # The filter bar's signature is the include/exclude lists and nothing
        # else, so a rename - which changes the chips' text without touching
        # either list - would leave the bar showing the old Chinese until the
        # next time the filters changed. Dropping the signature forces the
        # repaint this call is already here to do.
        self._filter_sig = None
        # preserve_scroll: the chips under the cursor change text, not order, so
        # throwing the scroll position away here would just be disorienting.
        self._invalidate_cards()
        self.refresh(preserve_scroll=True)
        # refresh()'s staleness check only looks at the game's own fields, and
        # the tag names are not among them - the chips have to be redrawn here.
        self._render_detail()

    def _tag_checkbox(self, parent, text, variable):
        """A checkbox in the shared tag-editing style, returned unpacked so the
        caller can choose its own pack options."""
        return ctk.CTkCheckBox(parent, text=text, variable=variable, height=24,
                               corner_radius=4, border_width=1,
                               fg_color=ACCENT, hover_color=CARD_HOVER,
                               text_color=TEXT, font=ui_font(size=12))

    def _tag_checklist(self, parent, current):
        """A wrapping run of tag checkboxes. Returns (flow, {name: var})."""
        flow = FlowFrame(parent, fg_color="transparent", gap_x=4, gap_y=4)
        flow.pack(fill="x")
        vars_ = {}
        widgets = []
        for name in sorted(slg_db.all_tags(self.conn), key=display_tag):
            var = ctk.BooleanVar(value=(name in current))
            vars_[name] = var
            widgets.append(self._tag_checkbox(flow, display_tag(name), var))
        flow.set_items(widgets)
        return flow, vars_

    def _tag_new_entry(self, parent, flow, vars_):
        """An entry that turns whatever is typed into a fresh checked tag."""
        entry = ctk.CTkEntry(parent, placeholder_text="新标签（回车添加）",
                             height=30, corner_radius=8, fg_color=BG,
                             text_color=TEXT, border_color=CHIP,
                             placeholder_text_color=MUTED, font=ui_font(size=12))
        entry.pack(fill="x", pady=(4, 0))

        def add(_e=None):
            text = entry.get().strip()
            if not text:
                return
            if text not in vars_:
                var = ctk.BooleanVar(value=True)
                vars_[text] = var
                flow.add_item(self._tag_checkbox(flow, display_tag(text), var))
            else:
                vars_[text].set(True)
            entry.delete(0, "end")
        entry.bind("<Return>", add)
        return entry

    def open_edit_tags(self, game):
        """Edit which tags a game carries (not their display names)."""
        win = self._new_dialog("编辑标签", "420x600")
        ctk.CTkLabel(win, text="给「%s」打标签" % self._title_to_show(game),
                     text_color=TEXT, font=ui_font(size=14, weight="bold"),
                     anchor="w", wraplength=380).pack(fill="x", padx=18, pady=(14, 2))
        ctk.CTkLabel(win, text="勾选已有标签，或在下面输入自己的标签名后回车。",
                     text_color=MUTED, font=ui_font(size=11), anchor="w",
                     justify="left").pack(fill="x", padx=18)

        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(8, 0))
        current = set(slg_db.game_tags(self.conn, game["id"]))
        flow, vars_ = self._tag_checklist(body, current)
        self._tag_new_entry(body, flow, vars_)

        def save():
            chosen = [n for n, v in vars_.items() if v.get()]
            with slg_db.session() as conn:
                slg_db.set_tags(conn, game["id"], chosen, clear=True)
            win.destroy()
            self._reload_tags(None, None)

        ctk.CTkButton(win, text="保存", height=34, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=CARD_HOVER, command=save
                      ).pack(fill="x", padx=18, pady=(8, 16))

    def open_add_game(self, prefill_title="", prefill_folder=""):
        """Add a game the user owns that is not in the dikgames catalogue."""
        win = self._new_dialog("添加我的游戏", "460x760")
        ctk.CTkLabel(win, text="把自己本地的游戏加进库",
                     text_color=TEXT, font=ui_font(size=16, weight="bold"),
                     anchor="w").pack(fill="x", padx=18, pady=(14, 2))
        ctk.CTkLabel(win, text="默认只出现在左侧「我添加的游戏」。\n"
                               "填齐标题/开发商/引擎/版本/标签后，可勾选「加入主列表」。",
                     text_color=MUTED, font=ui_font(size=11), justify="left",
                     anchor="w", wraplength=420).pack(fill="x", padx=18, pady=(0, 6))

        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(4, 0))

        def field(label, placeholder=""):
            ctk.CTkLabel(body, text=label, text_color=MUTED,
                         font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
            e = ctk.CTkEntry(body, height=30, corner_radius=8, fg_color=BG,
                             text_color=TEXT, border_color=CHIP,
                             placeholder_text_color=MUTED,
                             placeholder_text=placeholder, font=ui_font(size=13))
            e.pack(fill="x")
            return e

        title_e = field("标题（必填）")
        title_e.insert(0, prefill_title)
        dev_e = field("开发商")
        engine_e = field("引擎")
        ver_e = field("版本")

        ctk.CTkLabel(body, text="简介", text_color=MUTED,
                     font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
        ov_e = ctk.CTkTextbox(body, height=70, corner_radius=8, fg_color=BG,
                              text_color=TEXT, border_color=CHIP, border_width=1,
                              font=ui_font(size=12), wrap="word")
        ov_e.pack(fill="x")

        cover = {"path": None}
        ctk.CTkLabel(body, text="封面（可选）", text_color=MUTED,
                     font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
        cover_row = ctk.CTkFrame(body, fg_color="transparent")
        cover_row.pack(fill="x")
        cover_label = ctk.CTkLabel(cover_row, text="未选择", text_color=MUTED,
                                   font=ui_font(size=11), anchor="w")
        cover_label.pack(side="left", fill="x", expand=True)

        def pick_cover():
            path = filedialog.askopenfilename(
                title="选择封面图片", parent=win,
                filetypes=[("图片", "*.png *.jpg *.jpeg")])
            if path:
                cover["path"] = path
                cover_label.configure(text=os.path.basename(path))

        ctk.CTkButton(cover_row, text="选择…", width=72, height=26,
                      corner_radius=6, fg_color=CHIP, text_color=TEXT,
                      hover_color=CARD_HOVER, font=ui_font(size=11),
                      command=pick_cover).pack(side="right")

        folder = {"path": prefill_folder}
        ctk.CTkLabel(body, text="本地目录（可选）", text_color=MUTED,
                     font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
        folder_row = ctk.CTkFrame(body, fg_color="transparent")
        folder_row.pack(fill="x")
        folder_label = ctk.CTkLabel(folder_row,
                                    text=os.path.basename(prefill_folder) if prefill_folder else "未选择",
                                    text_color=MUTED, font=ui_font(size=11), anchor="w")
        folder_label.pack(side="left", fill="x", expand=True)

        def pick_folder():
            path = filedialog.askdirectory(title="选择游戏所在文件夹", parent=win)
            if path:
                folder["path"] = path
                folder_label.configure(text=os.path.basename(path))

        ctk.CTkButton(folder_row, text="选择…", width=72, height=26,
                      corner_radius=6, fg_color=CHIP, text_color=TEXT,
                      hover_color=CARD_HOVER, font=ui_font(size=11),
                      command=pick_folder).pack(side="right")

        ctk.CTkLabel(body, text="标签", text_color=MUTED,
                     font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
        flow, vars_ = self._tag_checklist(body, set())
        self._tag_new_entry(body, flow, vars_)

        promoted = ctk.BooleanVar(value=False)
        self._tag_checkbox(body, "加入主列表（需填齐标题/开发商/引擎/版本/至少1个标签）",
                           promoted).pack(anchor="w", pady=(12, 0))

        def save():
            title = title_e.get().strip()
            if not title:
                messagebox.showwarning("还差标题", "标题是必填的。", parent=win)
                return
            chosen = [n for n, v in vars_.items() if v.get()]
            want_promote = promoted.get()
            if want_promote:
                missing = []
                if not dev_e.get().strip():
                    missing.append("开发商")
                if not engine_e.get().strip():
                    missing.append("引擎")
                if not ver_e.get().strip():
                    missing.append("版本")
                if not chosen:
                    missing.append("至少1个标签")
                if missing:
                    messagebox.showwarning(
                        "还不能加入主列表",
                        "还缺：%s。\n补齐后再勾选「加入主列表」。" % "、".join(missing),
                        parent=win)
                    return
            cover_file = None
            if cover["path"]:
                try:
                    cover_file = slg_db.import_cover(cover["path"])
                except Exception:  # noqa: BLE001 - a bad image must not block saving
                    cover_file = None
            with slg_db.session() as conn:
                slg_db.add_user_game(
                    conn, title,
                    developer=dev_e.get().strip() or None,
                    engine=engine_e.get().strip() or None,
                    version=ver_e.get().strip() or None,
                    overview=ov_e.get("1.0", "end-1c").strip() or None,
                    cover_file=cover_file, tags=chosen,
                    folder_path=folder["path"] or None,
                    promoted=want_promote)
            win.destroy()
            self.refresh()

        ctk.CTkButton(win, text="保存", height=34, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=CARD_HOVER, command=save
                      ).pack(fill="x", padx=18, pady=(8, 16))

    def open_bind_dialog(self, unmatched):
        """Offer to hand-match folders the scan could not place."""
        if not unmatched:
            return
        import slg_scan
        root = self.scan_root()
        win = self._new_dialog("未匹配的游戏", "560x560")
        ctk.CTkLabel(win, text="这些文件夹没匹配上，手动处理一下：",
                     text_color=TEXT, font=ui_font(size=14, weight="bold"),
                     anchor="w").pack(fill="x", padx=18, pady=(14, 2))
        ctk.CTkLabel(win, text="绑定后下次扫描会自动对上号；也可以当新游戏加进库。",
                     text_color=MUTED, font=ui_font(size=11), anchor="w",
                     justify="left").pack(fill="x", padx=18)

        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(8, 6))
        for name in sorted(unmatched):
            row = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8)
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=name, text_color=TEXT, anchor="w",
                         font=ui_font(size=12, weight="bold"),
                         wraplength=240, justify="left").pack(
                side="left", padx=12, pady=8, fill="x", expand=True)
            ctk.CTkButton(row, text="绑定", width=56, height=26, corner_radius=6,
                          fg_color=ACCENT, text_color=ON_ACCENT,
                          hover_color=CARD_HOVER, font=ui_font(size=11),
                          command=lambda n=name, w=win: self._bind_folder(n, w)
                          ).pack(side="right", padx=(4, 6))
            ctk.CTkButton(row, text="加为游戏", width=72, height=26, corner_radius=6,
                          fg_color=CHIP, text_color=TEXT,
                          hover_color=CARD_HOVER, font=ui_font(size=11),
                          command=lambda n=name, w=win, r=root: (
                              w.destroy(),
                              self.open_add_game(
                                  prefill_title=slg_scan.folder_title(n),
                                  prefill_folder=os.path.join(r, n) if r else ""))
                          ).pack(side="right", padx=(0, 12))

    def _bind_folder(self, name, win):
        import slg_scan
        game = self._pick_game()
        if game is None:
            return
        root = self.scan_root()
        with slg_db.session() as conn:
            slg_db.add_alias(conn, game["id"], slg_scan.folder_title(name))
            slg_db.set_local_folder(
                conn, game["id"],
                os.path.join(root, name) if root else name,
                slg_scan.parse_folder_version(name))
            # Binding a folder means the game is on disk; without this it stays
            # out of the 已下载 view, unlike a game found by scan().
            slg_db.set_state(conn, game["id"], status="downloaded")
        win.destroy()
        self.refresh()

    def _pick_game(self):
        """A search dialog that returns a games row, or None when cancelled."""
        win = self._new_dialog("选择游戏", "460x560")
        entry = ctk.CTkEntry(win, placeholder_text="搜索游戏名…", height=30,
                             corner_radius=8, fg_color=BG, text_color=TEXT,
                             border_color=CHIP, placeholder_text_color=MUTED,
                             font=ui_font(size=13))
        entry.pack(fill="x", padx=14, pady=(12, 4))
        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        result = [None]

        def rebuild():
            for w in frame.winfo_children():
                w.destroy()
            text = entry.get().strip()
            rows = slg_db.find_games(self.conn, search=text or None, origin=None,
                                     sort="title", desc=False)
            for g in rows[:200]:
                ctk.CTkButton(
                    frame, text=g["title"], height=30, corner_radius=8, anchor="w",
                    fg_color="transparent", text_color=TEXT, hover_color=CARD,
                    font=ui_font(size=12),
                    command=lambda gid=g["id"], w=win: (result.__setitem__(0, gid),
                                                        w.destroy())
                    ).pack(fill="x")

        entry.bind("<KeyRelease>", lambda e: rebuild())
        rebuild()
        self.wait_window(win)
        if result[0] is None:
            return None
        return slg_db.get_game(self.conn, result[0])

    def open_translate_settings(self):
        """Engine, then provider, then a key - in that order.

        The AI half is a grid so the whole block can be hidden in one call when
        the free engine is picked: five fields that have no meaning without a
        key would otherwise sit there greyed out and read as a broken dialog.
        """
        win = self._new_dialog("翻译设置", "500x580")

        frame = ctk.CTkFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=18, pady=16)
        frame.grid_columnconfigure(0, weight=1)

        intro = ctk.CTkLabel(frame, justify="left", text_color=MUTED,
                             font=ui_font(size=12), wraplength=450)
        intro.grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(frame, text="引擎", text_color=TEXT, anchor="w",
                     font=ui_font(size=13)).grid(row=1, column=0, sticky="ew",
                                                     pady=(14, 4))
        engine_seg = ctk.CTkSegmentedButton(
            frame, values=["AI 翻译", "免费机翻"], height=32,
            selected_color=ACCENT, selected_hover_color=ACCENT,
            font=ui_font(size=13))
        engine_seg.grid(row=2, column=0, sticky="ew")

        # Everything that only an API-backed engine has a use for.
        ai = ctk.CTkFrame(frame, fg_color="transparent")
        ai.grid(row=3, column=0, sticky="ew")
        ai.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ai, text="服务商", text_color=TEXT, anchor="w",
                     font=ui_font(size=13)).grid(row=0, column=0, sticky="ew",
                                                     pady=(14, 4))
        provider_menu = ctk.CTkOptionMenu(
            ai, values=[row[1] for row in slg_engines.PROVIDERS], height=32,
            corner_radius=8, fg_color=CARD, text_color=TEXT,
            button_color=CHIP, button_hover_color=CARD_HOVER,
            font=ui_font(size=13))
        provider_menu.grid(row=1, column=0, sticky="ew")

        ctk.CTkLabel(ai, text="接口地址（以 /chat/completions 结尾的上一级）",
                     text_color=TEXT, anchor="w",
                     font=ui_font(size=13)).grid(row=2, column=0, sticky="ew",
                                                     pady=(12, 4))
        base_entry = ctk.CTkEntry(ai, height=32, corner_radius=8)
        base_entry.grid(row=3, column=0, sticky="ew")

        ctk.CTkLabel(ai, text="API Key", text_color=TEXT, anchor="w",
                     font=ui_font(size=13)).grid(row=4, column=0, sticky="ew",
                                                     pady=(12, 4))
        key_entry = ctk.CTkEntry(ai, height=32, corner_radius=8, show="•",
                                 placeholder_text="sk-…")
        key_entry.grid(row=5, column=0, sticky="ew")

        ctk.CTkLabel(ai, text="模型", text_color=TEXT, anchor="w",
                     font=ui_font(size=13)).grid(row=6, column=0, sticky="ew",
                                                     pady=(12, 4))
        model_entry = ctk.CTkEntry(ai, height=32, corner_radius=8)
        model_entry.grid(row=7, column=0, sticky="ew")

        def widgets():
            """What the dialog currently says, as one dict."""
            return {
                "engine": (slg_engines.ENGINE_FREE if engine_seg.get() == "免费机翻"
                           else slg_engines.ENGINE_OPENAI),
                "provider": _provider_id(provider_menu.get()),
                "base_url": base_entry.get().strip(),
                "key": key_entry.get().strip(),
                "model": model_entry.get().strip(),
            }

        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        ctk.CTkButton(row, text="保存", height=34, corner_radius=8, width=90,
                      fg_color=ACCENT,
                      command=lambda: self._save_translate_prefs(**widgets())
                      ).pack(side="left")
        ctk.CTkButton(row, text="测试连接", height=34, corner_radius=8, width=110,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      command=lambda: self._test_translate_key(**widgets())
                      ).pack(side="left", padx=8)

        self.tag_btn = ctk.CTkButton(
            frame, text="", height=38, corner_radius=8,
            command=lambda: self.do_translate_tags(**widgets()))
        self.tag_btn.grid(row=5, column=0, sticky="ew", pady=(16, 0))

        self._settings_status = ctk.CTkLabel(frame, text="", text_color=ACCENT,
                                             font=ui_font(size=12),
                                             wraplength=450, justify="left")
        self._settings_status.grid(row=6, column=0, sticky="ew", pady=(10, 0))

        def paint():
            """Re-apply everything that depends on the engine choice."""
            free = engine_seg.get() == "免费机翻"
            intro.configure(
                text="免费机翻用 Google 的公开接口，不用注册、不花钱，适合只想看懂简介"
                     "的人；质量一般，偶尔会被限流。\n"
                     "AI 翻译要自己的 API Key，质量明显更好，标签只能用这个。\n"
                     "Key 只存在这台电脑的本地数据库里，不会发到别的地方。"
                     if free else
                     "AI 翻译需要一个 OpenAI 兼容接口的 Key：DeepSeek、硅基流动、"
                     "Kimi、智谱、通义、OpenAI 都可以。\n"
                     "Key 只存在这台电脑的本地数据库里，不会发到别的地方。\n"
                     "标签全库共用，翻一次就一直用；简介和游戏名只翻你点开过的。")
            if free:
                ai.grid_remove()
            else:
                ai.grid()
            self._refresh_tag_button()

        def on_engine(_label):
            # Deferred: this fires from inside the segmented button's own press
            # handler, and paint() reconfigures that button's neighbours.
            self.after(1, paint)

        def on_provider(choice):
            preset = slg_engines.PROVIDER_BY_ID.get(_provider_id(choice))
            if preset is None:
                return
            # custom is the one entry that means "type it yourself", so it
            # clears both fields instead of filling them with a wrong guess.
            base_entry.delete(0, "end")
            base_entry.insert(0, preset[2])
            model_entry.delete(0, "end")
            model_entry.insert(0, preset[3])

        engine_seg.configure(command=on_engine)
        provider_menu.configure(command=on_provider)
        self._engine_seg = engine_seg

        config = slg_engines.resolve_config(self.conn)
        engine_seg.set(slg_engines.ENGINE_FREE if config.is_free else "AI 翻译")
        provider_menu.set(slg_engines.PROVIDER_BY_ID[config.provider][1])
        base_entry.insert(0, config.base_url)
        if config.api_key:
            key_entry.insert(0, config.api_key)
        model_entry.insert(0, config.model)
        paint()
        self._refresh_tag_button()

        # The five AI fields wrap to different heights depending on display
        # scaling, so the 500x580 literal above clips the last row (measured at
        # 150%: the content needs 624 logical px). Ask the widgets what they
        # need instead of guessing a bigger number that goes stale again.
        win.update_idletasks()
        win.geometry("500x%d" % max(
            580, int(round(win.winfo_reqheight()
                           / ctk.ScalingTracker.get_window_scaling(win)))))

    def open_help(self):
        win = self._new_dialog("帮助文档", "600x680")
        win.after(120, win.lift)

        ctk.CTkLabel(win, text="帮助文档", text_color=TEXT,
                     font=ui_font(size=17, weight="bold")).pack(pady=(16, 0), padx=22,
                                                              anchor="w")
        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(6, 12))

        def head(text):
            ctk.CTkLabel(frame, text=text, text_color=ACCENT,
                         font=ui_font(size=14, weight="bold")).pack(
                anchor="w", padx=8, pady=(18, 6))

        def sub(text):
            ctk.CTkLabel(frame, text=text, text_color=MUTED,
                         font=ui_font(size=13, weight="bold")).pack(
                anchor="w", padx=8, pady=(12, 4))

        def body(text, color=None):
            ctk.CTkLabel(frame, text=text, text_color=color or TEXT,
                         font=ui_font(size=13), wraplength=520,
                         justify="left").pack(anchor="w", padx=8, pady=(0, 6))

        def qa(question, answer):
            """A question and its answer rendered as one visual unit.

            The question carries the emphasis: it is what someone scrolls the
            page looking for, so it gets the bold accent treatment while the
            answer stays plain. Loose gap above the question and a tight one
            under the answer is what keeps one pair from running into the next.
            """
            ctk.CTkLabel(frame, text=question, text_color=ACCENT,
                         font=ui_font(size=13, weight="bold"), wraplength=520,
                         justify="left").pack(anchor="w", padx=8, pady=(10, 2))
            ctk.CTkLabel(frame, text=answer, text_color=TEXT,
                         font=ui_font(size=13), wraplength=520,
                         justify="left").pack(anchor="w", padx=8, pady=(0, 8))

        head("三步上手")
        body("第 1 步　点左下角的「更新游戏数据」，从服务器拉取游戏目录。")
        body("第 2 步　点开任意一款游戏，在右侧用星星给它打分。")
        body("第 3 步　用标签筛选，再把顶栏排序切成「按xp推荐」，挑下一款要玩的。")
        body("打分越多，推荐越准——这是它和普通游戏列表最大的区别。", color=MUTED)

        head("这个软件是什么")
        body("一个 dikgames 站点游戏的本地资料库。游戏目录由作者的服务器从站点整理好，"
             "同步时下载进本地数据库，再按标签、评分、下载状态去挑你想玩的那些。"
             "浏览、搜索、筛选都不联网；封面图跟着目录一起下载，下完一款存一款。")
        body("数据库和封面不在程序旁边，在 %LOCALAPPDATA%\\slgking\\ 下面："
             "slgking.db 和 covers 文件夹。想备份或换电脑，把那个目录整个带走。"
             "exe 删了数据还在，换台机器数据留在原处。")

        head("网络与梯子")
        body("游戏目录不再直连 dikgames，改从作者的服务器下载（国内可直连，不用梯子）。"
             "翻译用的是 Google 和各家大模型的公开接口，这些接口在墙外，"
             "需要先开梯子（VPN / 代理），否则翻译会连接失败。")
        body("没开梯子时，翻译会在 8 秒内明确报「网络错误：连接超时」，而不是一直卡着不动。",
             color=MUTED)
        body("未使用梯子导致的翻译失败等问题与作者无关。",
             color=DANGER_TEXT)
        _link_button(frame, SITE_LABEL, SITE_URL).pack(fill="x", padx=8, pady=(0, 6))

        head("常见问题")

        sub("同步与数据")
        qa("Q：同步失败了怎么办？",
           "A：同步从服务器下载目录，中断了再点一次「更新游戏数据」就行，"
           "已经下好的不会重复下。服务器偶尔抖动，等一会儿再试。")

        qa("Q：同步跑太久，能停吗？",
           "A：能。任务跑起来之后，左下角那颗按钮会变成「停止」，点一下就停；"
           "已经抓到的部分会保存，下次接着来。正在飞行中的那一个网页请求要等它"
           "返回，通常不到一秒。")

        qa("Q：左下角「更多…」里面那几个是干什么的？",
           "A：都是不常用的维护动作，点开每个下面都有一句说明。\n"
           "「下载封面」：补下缺的封面缩略图。正常「同步」会从服务器把封面一起"
           "拉下来，这个只是用来补漏下的，或者哪张图当时没抓到。\n"
           "「补齐热度」：在本地重算热度，不联网。")

        qa("Q：封面显示灰色方块？",
           "A：说明这张封面还没下载。正常「同步」会把封面一起拉下来，所以先再点一次"
           "同步；还是灰的就点左下角「更多…」→「下载封面」补，让它慢慢跑完。")

        qa("Q：「扫描本地目录」扫哪里？",
           "A：在左侧栏「更多工具…」→「扫描本地目录…」里，第一次点它会让你选一个"
           "文件夹，选完就记住了。想换一个，在那行上点右键重新选。\n"
           "选中文件夹之后，库里同名（或近似同名）的游戏会被标成「已下载」，"
           "并记下本地版本号，方便和站点上的最新版对比。")

        sub("翻译")
        qa("Q：翻译要怎么开？",
           "A：点左侧栏的「更多工具…」→「翻译设置…」，有两条路。\n"
           "· 免费机翻：什么都不用填，选上就能用，简介和游戏名都翻。质量一般，"
           "偶尔会被 Google 限流，过几分钟再试。\n"
           "· AI 翻译：填一个 OpenAI 兼容接口的 Key（DeepSeek、硅基流动、Kimi、"
           "智谱、通义、OpenAI 都行），质量明显更好。\n"
           "译文存在本地数据库里，翻一次就一直有效，不会重复花钱。")

        qa("Q：点了翻译，等很久什么都没有？",
           "A：先看有没有开梯子——翻译接口都在墙外，没开梯子必然连不上。"
           "现在这种情况会在 8 秒内报「网络错误：连接超时」；如果超过 8 秒还没有任何"
           "提示，那是 bug，请到 GitHub 上反馈。")

        qa("Q：为什么标签只能用 AI 翻？",
           "A：标签是全库共用的固定术语，一百多张卡片都显示同一份。机翻每次给的"
           "译法都不一样（netorare 这轮叫「寝取」下轮叫「NTR」），整个库会读起来"
           "前后矛盾。所以标签翻译需要 AI 引擎——在「更多工具…」→「翻译设置…」里选一个服务商，"
           "填好 Key，然后点「翻译标签」就行，一趟大概花 1 分钱。")

        qa("Q：为什么有些游戏名还是英文？",
           "A：名字里的版本号（v1.20、EP03）和方括号里的社团名，AI 经常忍不住去改。"
           "改过的名字会被丢掉，改用原文——这类名字会记一笔「不适合翻译」，"
           "之后不会再重复请求。简介不受影响，照常翻。")

        sub("界面与设置")
        qa("Q：搜索、筛选、标签库之间的区别？",
           "A：搜索栏按游戏名找；卡片上的标签或「标签库…」里的左键加入筛选、右键排除；"
           "「更多工具…」→「偏好权重…」会按你打过的五星评分算出你倾向的标签。")

        qa("Q：右上角那颗齿轮是干什么的？",
           "A：打开「设置」——浅色/深色/跟随系统在这里切，"
           "「关于本软件…」也在里面（版本信息、检查软件更新、GitHub 主页、"
           "反馈邮箱、数据目录）。设一次的工具在左侧栏的「更多工具…」里，"
           "和这个是两个不同的门。")

        qa("Q：深色主题里的「跟随系统」是怎么工作的？",
           "A：程序每 5 秒采样一次 Windows 的浅色/深色设置，变了就跟着换，"
           "所以会有一小段延迟。手动选「浅色」或「深色」则会记住，下次打开还是它。")

        head("下载的游戏是英文的怎么办")
        body("dikgames 是英文流站点，站上绝大多数游戏都没有官方中文，下载到"
             "英文版本是正常的，不是文件坏了。想看懂，按顺序装这两个工具：\n"
             "1. 露娜翻译器（LunaTranslator）：开源免费，边玩边实时机翻游戏文本；\n"
             "2. 作者的 RenPy 汉化小工具：搭配露娜翻译器，把 RenPy 游戏做成离线汉化。")
        _link_button(frame, LUNA_LABEL, LUNA_URL).pack(fill="x", padx=8, pady=(0, 4))
        _link_button(frame, RPYKIT_LABEL, RPYKIT_URL).pack(fill="x", padx=8, pady=(0, 6))

        head("声明与关于")
        body("本软件只是一个游戏资料检索库，里面没有任何游戏文件，也不提供"
             "任何下载。想下载游戏请前往游戏官网，或者自己去找下载地址。\n"
             "检索到的信息和游戏的版权都归原站点与作者所有。", color=DANGER_TEXT)
        body("作者 · %s" % AUTHOR, color=MUTED)
        # The button rather than a bare link: someone who opens 帮助文档 looking
        # for the source should not have to spot an 11px underlined label.
        _link_button(frame, GITHUB_LABEL, GITHUB_URL).pack(
            fill="x", padx=8, pady=(0, 6))
        body("版本 " + build_stamp(), color=MUTED)
        body("本软件完全免费。没有收费版、没有付费激活、没有隐藏收费入口。\n"
             "如果你是通过付费渠道拿到它的，请立即举报。", color=DANGER_TEXT)
        body("用得还行的话，欢迎在 GitHub 点个 star，也帮忙推荐给周围的朋友。"
             "有想法、有 bug、想要什么功能，发邮件到 %s，"
             "或者加交流群 %s。" % (CONTACT_EMAIL, QQ_GROUP),
             color=MUTED)
        _link_button(frame, "发邮件给作者", CONTACT_MAILTO).pack(
            fill="x", padx=8, pady=(0, 6))
        _copy_button(frame, "复制交流群号：%s" % QQ_GROUP, QQ_GROUP, self).pack(
            fill="x", padx=8, pady=(0, 6))

    @staticmethod
    def _tag_btn_text(pending, allowed=True):
        if not allowed:
            return "标签翻译需要 AI 引擎（机翻术语不一致）"
        return "翻译标签（还剩 %d 个）" % pending if pending else "标签已全部翻译"

    def _refresh_tag_button(self):
        """The tag button's state, from the engine the settings now describe.

        Read straight off the dialog's own widgets rather than from the database:
        the user may have switched to the free engine without saving yet, and a
        button that stays enabled through that would only fail on the click.
        """
        btn = self.tag_btn
        if btn is None or not btn.winfo_exists():
            return
        allowed = self._settings_engine() == slg_engines.ENGINE_OPENAI
        counts = slg_db.translation_counts(self.conn)
        pending = counts["tag_total"] - counts["tags"]
        btn.configure(text=self._tag_btn_text(pending, allowed),
                      state="normal" if allowed and pending else "disabled")

    def _settings_engine(self):
        seg = getattr(self, "_engine_seg", None)
        if seg is None or not seg.winfo_exists():
            return slg_engines.ENGINE_OPENAI
        if seg.get() == "免费机翻":
            return slg_engines.ENGINE_FREE
        return slg_engines.ENGINE_OPENAI

    def _set_settings_status(self, text):
        label = self._settings_status
        if label is not None and label.winfo_exists():
            label.configure(text=text)
            return
        # The dialog was closed - or a theme switch tore it down - while the
        # request was still in flight. The sidebar's status line is the other
        # always-there one-liner, so a failure lands somewhere the user can
        # still read instead of vanishing.
        self._set_progress(text)

    def _save_translate_prefs(self, engine, provider, base_url, key, model):
        for name, value in ((slg_engines.PREF_ENGINE, engine),
                            (slg_engines.PREF_PROVIDER, provider),
                            (slg_engines.PREF_BASE_URL, (base_url or "").strip()),
                            (slg_engines.PREF_KEY, (key or "").strip()),
                            (slg_engines.PREF_MODEL, (model or "").strip())):
            slg_db.set_pref(self.conn, name, value)
        self._set_settings_status("已保存")
        self._refresh_tag_button()

    def _test_translate_key(self, engine, provider, base_url, key, model):
        self._set_settings_status("测试中…")
        config = slg_engines.Config(engine, provider, base_url, key, model)
        if config.engine == slg_engines.ENGINE_OPENAI and not config.api_key:
            self._set_settings_status("先填 API Key，或者切到免费机翻")
            return

        def worker():
            try:
                engine_obj = config.build()
                reply = slg_translate.probe(config.api_key, config.model,
                                            engine=engine_obj)
                self.queue.put(("note", "%s 连接正常，返回：%s"
                                % (config.describe(), reply[:20])))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("note", "连接失败：%s" % str(exc)[:150]))

        threading.Thread(target=worker, daemon=True).start()

    def do_translate_tags(self, engine, provider, base_url, key, model):
        if self.busy:
            return
        if engine == slg_engines.ENGINE_FREE:
            self._set_settings_status("标签翻译需要 AI 引擎，免费机翻做不了")
            return
        if not (key or "").strip():
            self._set_settings_status("先填 API Key 再翻译")
            return
        counts = slg_db.translation_counts(self.conn)
        pending = counts["tag_total"] - counts["tags"]
        if not pending:
            self._set_settings_status("标签都翻译过了")
            self._refresh_tag_button()
            return
        if not messagebox.askyesno(
                "翻译标签",
                "把剩下 %d 个标签交给 %s 翻译。\n\n"
                "标签是全库共用的，翻一次就一直用，这一趟大概花 1 分钱。\n"
                "确定开始吗？" % (pending, slg_engines.
                                  PROVIDER_BY_ID.get(provider, (None, provider))[1])):
            return
        self._save_translate_prefs(engine, provider, base_url, key, model)
        self._set_settings_status("翻译中…")
        self._run_job("翻标签…", self._tag_worker,
                      engine, provider, base_url, key, model)

    def _tag_worker(self, engine, provider, base_url, key, model):
        try:
            with slg_db.session() as conn:
                config = slg_engines.Config(engine, provider, base_url, key, model)
                summary = slg_translate.run_tag_translation(
                    conn, config.api_key, config.model or slg_translate.DEFAULT_MODEL,
                    engine=config.build(),
                    log=self._log)
                # Both dictionaries are module level, so the refresh at the end
                # of the job picks the new translations up for every card at once.
                load_tag_translations(conn)
                load_title_translations(conn)
            text = "标签翻译完成：%d/%d 个" % (summary["translated"],
                                              summary["pending"])
            if summary["missing"]:
                text += "，%d 个没翻出来" % len(summary["missing"])
            self.queue.put(("done", text))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("done", "标签翻译失败：%s" % str(exc)[:150]))

    # --- software updates ------------------------------------------------------

    def _start_update_check(self, force=False):
        """Ask GitHub whether there is a newer slgking, off the tk thread.

        slg_update.check reads and writes the pref table, so the worker opens
        the connection it uses - the same rule the sync workers follow, because
        self.conn belongs to the tk thread and sqlite connections do not cross
        threads.
        """
        threading.Thread(target=self._update_worker, args=(force,),
                         daemon=True).start()

    def _update_worker(self, force):
        found = None
        try:
            with slg_db.session() as conn:
                found = slg_update.check(conn, APP_VERSION, force=force)
        except Exception as exc:  # noqa: BLE001 - a version check is never fatal
            self.queue.put(("note", "检查更新失败：%s" % str(exc)[:100]))
            return
        if found:
            self.queue.put(("update", found))
        elif force:
            # Only on a manual click. The startup check saying "已是最新" every
            # launch is noise in the one line that also carries progress.
            self.queue.put(("note", "已是最新版本 %s" % APP_VERSION))

    def _show_update(self, release):
        """The sidebar notice, the settings line, and a modal changelog dialog."""
        self._update_found = release
        self._update_url = release["url"]
        self._update_notes = release.get("body") or ""
        label = self.update_label
        if label is not None and label.winfo_exists():
            label.configure(text="有新版本 %s，点击查看" % release["version"])
            if not label.winfo_ismapped():
                # after=, not a plain pack: the sidebar's pack order is load
                # bearing, and appending would put this below the footer.
                label.pack(after=self.progress_label, pady=(0, 10))
        # The settings dialog's own line only, not _set_settings_status: that
        # helper falls back to the sidebar's progress line, which this check
        # runs in the background of and which the notice above already covers.
        status = self._settings_status
        if status is not None and status.winfo_exists():
            status.configure(text="发现新版本 %s" % release["version"])
        about = self._about_status
        if about is not None and about.winfo_exists():
            about.configure(text="发现新版本 %s" % release["version"])
        # The dialog fires once per session; _repaint_update_notice calls back
        # into here on every theme switch and must not re-open a dialog the user
        # already dismissed.
        if not self._update_dialog_shown:
            self._update_dialog_shown = True
            self._show_update_dialog(release)

    def _show_update_dialog(self, release):
        """A modal changelog with a download button, shown once per new version.

        The sidebar label alone used to be the whole announcement, which a user
        glued to the grid would not notice until they happened to read the left
        rail. The dialog is what actually tells them an update exists.
        """
        body = (release.get("body") or "").strip()
        win = self._new_dialog("发现新版本", "460x520")
        ctk.CTkLabel(win, text="发现新版本 %s" % release["version"],
                     text_color=TEXT, font=ui_font(size=16, weight="bold")
                     ).pack(fill="x", padx=20, pady=(18, 4))
        if release.get("name"):
            ctk.CTkLabel(win, text=release["name"], text_color=MUTED,
                         font=ui_font(size=12), wraplength=400,
                         justify="left").pack(fill="x", padx=20, pady=(0, 6))
        if body:
            notes = ctk.CTkTextbox(win, height=300, corner_radius=8, fg_color=BG,
                                   text_color=TEXT, border_color=CHIP,
                                   border_width=1, font=ui_font(size=12),
                                   wrap="word")
            notes.insert("1.0", body[:4000])
            notes.configure(state="disabled")
            notes.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        else:
            ctk.CTkLabel(win, text="本次更新没有附带说明。", text_color=MUTED,
                         font=ui_font(size=12)).pack(fill="x", padx=20, pady=8)
        row = ctk.CTkFrame(win, fg_color="transparent")
        ctk.CTkButton(row, text="稍后", height=32, width=90, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      font=ui_font(size=12), command=win.destroy).pack(side="left")
        ctk.CTkButton(row, text="前往下载", height=32, width=110, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=12),
                      command=lambda: (webbrowser.open(self._update_url),
                                       win.destroy())).pack(side="left", padx=(8, 0))
        row.pack(pady=(0, 16))

    def _repaint_update_notice(self):
        """Put the notice back on a sidebar that was just rebuilt."""
        if self._update_found is not None:
            self._show_update(self._update_found)

    def _open_update_page(self, event=None):
        webbrowser.open(self._update_url)

    # --- background work ------------------------------------------------------

    def _log(self, message):
        self.queue.put(("log", message))

    def _fail(self, what, exc):
        return "%s失败：%s: %s" % (what, type(exc).__name__, exc)

    def _run_job(self, label, worker, *args):
        """Start `worker` in a daemon thread under the usual job discipline.

        Every long job used to repeat the same _start_job + Thread(daemon=True)
        preamble; the only thing that differed was the worker and its args.
        """
        self._start_job(label)
        threading.Thread(target=worker, args=args, daemon=True).start()

    def _start_job(self, label):
        self.busy = True
        # Remembered so a theme switch can replay the disabled/relabelled state
        # on the buttons it just rebuilt.
        self._job_label = label
        self._stop.clear()
        # The sync button doubles as the stop button for the duration. _stop has
        # been threaded into every worker's should_stop since they were written
        # and nothing ever set it: a job that ran long - 400 detail pages, or a
        # stalled cover queue - had no way out except killing the app. The label
        # moves to the sidebar's status line, which is where progress already
        # goes.
        self._ui(self.sync_btn, text="停止", state="normal",
                 command=self._cancel_job)
        # 更多工具… and 翻译设置… are behind dialogs now, so these reach whichever
        # window is open - usually none, and that is fine: the entry point the
        # user could click to start a second job is the sync button, which is
        # disabled above.
        self._ui(self.maintenance_btn, state="disabled")
        self._ui(self.settings_btn, state="disabled")
        self._set_progress(label)
        bar = self.progress_bar
        if bar is not None and bar.winfo_exists():
            bar.set(0)

    def _cancel_job(self):
        """Ask the running worker to stop. It stops at its next checkpoint."""
        self._stop.set()
        self._ui(self.sync_btn, text="正在停止…", state="disabled")

    def _end_job(self, message, refill=False):
        """Finish a background job. `refill` forces every card to be redrawn.

        The dirty check in _sync_cards is keyed on the game id, which cannot see
        a job that only changes a column. Cover downloads are exactly that: every
        row keeps its id and only cover_file moves, so without this the cards on
        screen would keep showing the grey placeholder until the next restart.
        """
        self.busy = False
        self._job_label = ""
        self._ui(self.sync_btn, text="更新游戏数据", state="normal",
                 command=self.do_sync)
        self._ui(self.maintenance_btn, state="normal")
        self._ui(self.settings_btn, state="normal")
        self._set_progress(message)
        bar = self.progress_bar
        if bar is not None and bar.winfo_exists():
            bar.set(0)
        self._refresh_tag_button()
        if refill:
            self._invalidate_cards()
        self.refresh()

    def do_sync(self):
        """Pull the pre-built catalogue from the server - no direct scraping."""
        if self.busy:
            return
        self._run_job("同步中…", self._sync_worker)

    def _sync_worker(self):
        import slg_sync_server
        try:
            with slg_db.session() as conn:
                summary = slg_sync_server.pull(
                    conn, log=self._log, should_stop=self._stop.is_set,
                    on_progress=lambda d, total: self.queue.put(
                        ("progress", ("封面 %d/%d" % (d, total), d, total))))
            if self._stop.is_set():
                self.queue.put(("done", "已停止 · 新增 %d · 封面 %d"
                                % (summary["new"], summary["covers"])))
                return
            if summary["unchanged"]:
                said = "目录已是最新 · 全站 %d 款" % summary["catalogue"]
            else:
                said = "同步完成 · 全站 %d 款 · 新增 %d · 封面 %d" % (
                    summary["catalogue"], summary["new"], summary["covers"])
            self.queue.put(("done", said))
        except Exception as exc:  # noqa: BLE001 - the user needs the message
            self.queue.put(("done", self._fail("同步", exc)))

    def do_covers(self):
        if self.busy:
            return
        if not slg_db.missing_cover_files(self.conn):
            self._set_progress("封面都下载好了")
            return
        self._run_job("下封面…", self._covers_worker)

    def _covers_worker(self):
        import slg_sync_server
        try:
            with slg_db.session() as conn:
                done = slg_sync_server.download_covers(
                    conn, log=self._log, should_stop=self._stop.is_set,
                    on_progress=lambda d, total: self.queue.put(
                        ("progress", ("封面 %d/%d" % (d, total), d, total))))
            self.queue.put(("covers_done", "封面下载 %d 张" % done))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("covers_done", self._fail("封面下载", exc)))

    def do_heat(self):
        """Local recompute of the heat column - no network."""
        if self.busy:
            return
        if not slg_db.data_gaps(self.conn)["heat"]:
            self._set_progress("热度都补齐了")
            return
        self._run_job("补热度…", self._heat_worker)

    def _heat_worker(self):
        try:
            with slg_db.session() as conn:
                recomputed = slg_db.backfill_heat(
                    conn, log=self._log,
                    on_progress=lambda d, total: self.queue.put(
                        ("progress", ("重算热度 %d/%d" % (d, total), d, total))))
            self.queue.put(("done", "补齐热度 · 重算 %d 款" % recomputed))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("done", self._fail("补齐热度", exc)))

    def scan_root(self):
        """The remembered game folder, or "" when there is not one yet."""
        root = slg_db.get_pref(self.conn, PREF_SCAN_ROOT) or ""
        return root if root and os.path.isdir(root) else ""

    def pick_scan_root(self):
        """Ask for the folder the games live in. Returns it, or "" if cancelled."""
        chosen = filedialog.askdirectory(
            title="选择放游戏的文件夹（里面每个子文件夹是一款游戏）",
            initialdir=self.scan_root() or os.path.expanduser("~"), parent=self)
        if chosen:
            slg_db.set_pref(self.conn, PREF_SCAN_ROOT, os.path.normpath(chosen))
        return self.scan_root()

    def do_scan(self):
        if self.busy:
            return
        root = self.scan_root() or self.pick_scan_root()
        if not root:
            return
        # _start_job, not a hand-rolled busy flag: the hand-rolled version
        # disabled only the sync button, so 下载封面 and 全量重建 stayed
        # clickable and then did nothing at all when clicked.
        self._run_job("扫描本地目录…", self._scan_worker, root)

    def _scan_worker(self, root):
        import slg_scan
        try:
            with slg_db.session() as conn:
                result = slg_scan.scan(
                    conn, roots=[root],
                    log=self._log,
                    on_progress=lambda name, gid: self.queue.put(
                        ("progress", "扫描 %s" % name)),
                    should_stop=self._stop.is_set)
            self.queue.put(("done", "扫描完成：匹配 %d 个，未匹配 %d 个"
                            % (result["matched"], len(result["unmatched"]))))
            if result["unmatched"]:
                self.queue.put(("bind", list(result["unmatched"])))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("done", self._fail("扫描", exc)))

    def do_updates(self):
        import slg_scan
        report = slg_scan.check_updates(self.conn)
        behind = report["behind"]

        win = self._new_dialog("检查更新", "660x540")
        ctk.CTkLabel(win, text="%d 款有新版 · 已最新 %d 款 · 无法比较 %d 款"
                     % (len(behind), len(report["same"]), len(report["unknown"])),
                     text_color=MUTED, font=ui_font(size=13)).pack(pady=10)

        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        if not behind:
            ctk.CTkLabel(frame, text="没有发现更新。\n"
                                     "（没数据的话先点左侧「更多工具…」→「扫描本地目录…」建立台账）",
                         text_color=MUTED, justify="left").pack(pady=40)
        for row in behind:
            card = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=8)
            card.pack(fill="x", pady=3)
            ctk.CTkLabel(card, text=row["title"], text_color=TEXT, anchor="w",
                         font=ui_font(size=13, weight="bold")).pack(
                anchor="w", padx=12, pady=(8, 0))
            ctk.CTkLabel(card, text="本地 %s  →  站点 %s   (%s)"
                         % (row["folder_version"], row["version"],
                            row["last_updated"] or "?"),
                         text_color=MUTED, anchor="w",
                         font=ui_font(size=12)).pack(anchor="w", padx=12, pady=(0, 8))

    def _maybe_refresh(self, gap=None):
        """Redraw at most once per `gap` seconds.

        The sync used to leave the list frozen for its whole run because only
        ("done", ...) triggered a refresh. But a refresh rebuilds every visible
        card, so firing one per downloaded cover would trade a frozen list for
        a stuttering one. Hence both guards: the gap caps how often it can run,
        and skip_if_same drops the runs that would redraw an identical list.

        The gap widens while a job is running: those redraws are exactly the
        ones the user is clicking through, and the end-of-job refresh draws the
        same final list a moment later.
        """
        if gap is None:
            gap = REFRESH_GAP_BUSY if self.busy else REFRESH_GAP
        now = time.time()
        if now - self._last_refresh < gap:
            return
        self._last_refresh = now
        self.refresh(preserve_scroll=True, skip_if_same=True)

    def _drain(self):
        """Every background message, on the tk thread.

        The re-arm lives in a finally on purpose. A handler that raised - and
        they touch widgets a theme switch may have just destroyed - used to
        escape this method entirely, so after() was never called again and every
        later background update was dropped for the rest of the session. The
        user sees that as nothing happening at all, which is exactly what it is.
        """
        # Bounded per tick: a job that queues a thousand progress lines used to
        # be drained in one go, and every click that landed mid-drain waited for
        # the whole backlog. The remainder is a millisecond away.
        pending = DRAIN_PER_TICK
        try:
            while pending > 0:
                try:
                    message = self.queue.get_nowait()
                except queue.Empty:
                    break
                pending -= 1
                # Malformed messages are dropped rather than unpacked: one bad
                # put() must not cost the user every future update.
                if not isinstance(message, tuple) or len(message) != 2:
                    continue
                try:
                    self._dispatch(message[0], message[1])
                except Exception:  # noqa: BLE001 - a dead handler is not a dead pump
                    traceback.print_exc()
        finally:
            try:
                # Nothing left in the queue means the usual idle poll; a
                # truncated pass comes straight back for the rest.
                delay = 1 if pending == 0 else 150
                self.after(delay, self._drain)
            except tk.TclError:
                pass  # the window is on its way out

    def _dispatch(self, kind, payload):
        if kind == "log":
            self._set_progress(payload)
        elif kind == "progress":
            if isinstance(payload, tuple) and len(payload) == 3:
                text, done, total = payload
                self._set_progress(text, done / total if total else 0.0)
            else:
                self._set_progress(payload)
            self._maybe_refresh()
        elif kind == "done":
            self._end_job(payload)
        elif kind == "covers_done":
            # Separate from "done" only for the refill: covers move a column,
            # not a row, so the list has to be rebuilt rather than re-synced.
            self._end_job(payload, refill=True)
        elif kind == "update":
            self._show_update(payload)
        elif kind == "note":
            self._set_settings_status(payload)
        elif kind == "overview":
            self._overview_result(*payload)
        elif kind == "title":
            self._title_result(*payload)
        elif kind == "comments":
            self._comments_result(*payload)
        elif kind == "bind":
            self.open_bind_dialog(payload)


def build(root, smoke=False):
    """Kept for the packaging smoke test."""
    if smoke:
        root.after(1800, root.destroy)


def main(smoke=False):
    # Must be here rather than in App(): it has to be set before the first
    # widget exists. App resolves the saved light/dark/system preference itself.
    ctk.set_default_color_theme("blue")
    app = App(notify=not smoke)
    if smoke:
        app.after(2200, app.destroy)
        app.update()
        print("smoke: window built ok, rows=%d" % len(app.rows))
    app.mainloop()
    return 0
