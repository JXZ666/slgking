"""The window.

Master-detail on purpose: a card carries only what you scan (cover, title,
version, rating, a few tags, a status dot) and every control that writes to the
database lives in the detail panel on the right. Putting stars and buttons on
every card would mean thousands of widgets for the full catalogue.

Light, card-based, Win11-ish - the stock tkinter look reads as Windows XP and
that was the one thing about the previous tools the user actively disliked.
"""

import calendar
import json
import math
import os
import queue
import random
import sqlite3
import sys
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import uuid
import webbrowser
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox
from urllib.parse import urlsplit

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

try:  # Windows-only, and the reel sounds are a nicety rather than a feature
    import winsound
except ImportError:  # pragma: no cover - the build is Windows-only in practice
    winsound = None

import slg_comments
import slg_account
import slg_db
import slg_engines
from slg_game_categories import CATEGORIES, format_categories
from slg_motion import MotionScheduler, TimeBudget
from slg_game_labels import format_game_label
import slg_remote
import slg_scrape
import slg_titles
import slg_translate
import slg_update
import slg_util

APP_VERSION = "0.25.0"
TEST_APP_VERSION = "0.25.0"


def _build_announcement_pages(remote_config, release_notes):
    """Build newest-first notice pages followed by a separate version page."""
    if not isinstance(remote_config, dict):
        remote_config = {}
    current = remote_config.get("announcement")
    if not isinstance(current, dict):
        current = {}

    history = remote_config.get("announcement_history")
    if not isinstance(history, list):
        history = current.get("announcement_history")
    if not isinstance(history, list):
        history = []

    def has_content(item):
        return isinstance(item, dict) and any(
            str(item.get(key) or "").strip() for key in ("id", "title", "body"))

    pages = []
    seen_ids = set()
    seen_content = set()

    def append_notice(item, kind):
        if not has_content(item):
            return
        item = dict(item)
        item_id = str(item.get("id") or "").strip()
        content_key = (str(item.get("title") or "").strip(),
                       str(item.get("body") or "").strip())
        if item_id and item_id in seen_ids:
            return
        if not item_id and content_key in seen_content:
            return
        pages.append({"kind": kind, "item": item})
        if item_id:
            seen_ids.add(item_id)
        seen_content.add(content_key)

    append_notice(current, "current")
    # The server stores history oldest-first; show the latest archived notice
    # immediately after the active one.
    for item in reversed(history):
        append_notice(item, "history")

    pages.append({"kind": "release_notes", "item": {
        "title": "版本说明", "body": str(release_notes or "")}})
    return pages


def _announcement_page_view(page, index, total):
    """Return visible text, a page indicator, and active-notice claimability."""
    page = page if isinstance(page, dict) else {}
    item = page.get("item")
    item = item if isinstance(item, dict) else {}
    kind = page.get("kind")
    page_number = max(1, int(index) + 1)
    total = max(1, int(total))
    if kind == "release_notes":
        return (str(item.get("body") or ""),
                "版本说明 · %d / %d" % (page_number, total), False)

    title = str(item.get("title") or "未命名公告").strip()
    body = str(item.get("body") or "").strip()
    text = "服务器公告 · " + title
    if body:
        text += "\n\n" + body
    label = "当前公告" if kind == "current" else "历史公告"
    indicator = "%s · %d / %d" % (label, page_number, total)
    created_at = str(item.get("created_at") or "").strip()
    if created_at:
        indicator += " · " + created_at
    return text, indicator, kind == "current"


def display_app_version():
    """Keep the experimental exe distinguishable without changing stable behavior."""
    if (getattr(sys, "frozen", False)
            and "_test" in os.path.basename(sys.executable).lower()):
        return TEST_APP_VERSION
    return APP_VERSION


# The sidebar shows the number and nothing else. build_stamp() still carries
# the channel and the build time, but it belongs on the 关于 page now: a
# timestamp in the corner of every screen was answering a question the user
# asks once.
APP_VERSION_LABEL = "v" + display_app_version()
slg_account.set_client_version(display_app_version())
APP_TITLE = "SLG黄游之王"
AUTHOR = "菊千代赛高"
GITHUB_URL = "https://github.com/JXZ666"
GITHUB_LABEL = "GitHub 主页 · JXZ666"
# The group has no join link that works without a key, so the number is offered
# as copyable text instead of a URL. Bare digits, no dashes or spaces: whatever
# is on the clipboard has to paste straight into QQ's search box.
QQ_GROUP = "1124074040"
QQ_GROUP_LABEL = "交流群 · %s · 欢迎大家加入" % QQ_GROUP
QQ_GROUP_COPY_LABEL = "交流群 %s · 群内每日码兑 +10积分" % QQ_GROUP
# Taken from the scraper rather than typed again: this is the site the catalogue
# comes from, and two copies of that URL is one copy that goes stale.
SITE_URL = slg_scrape.BASE
ADMIN_URL = "https://slg-king.com/admin/"
SITE_LABEL = "数据来源"
F95_SOURCE_URL = "https://f95zone.to.it/genres/"
# The site is English-first and most games ship untranslated; pointing users at
# LunaTranslator (real-time machine translation) plus the author's own RenPy
# tooling is the one referral that both helps and fits the "retrieval only" line.
LUNA_URL = "https://docs.lunatranslator.org/zh/"
LUNA_LABEL = "露娜翻译器 LunaTranslator"
RPYKIT_URL = "https://github.com/JXZ666/SLG-Renpy-Toolkit"
RPYKIT_LABEL = "RenPy 汉化小工具 · SLG-Renpy-Toolkit"
PREF_THEME = "theme"
PREF_UI_SCALE = "ui.scale_profile"
PREF_ACTIVE_IDENTITY_MODE = "identity.active_mode"
UI_SCALE_PROFILES = {
    "auto": {"label": "自动（跟随系统）", "factor": 1.0},
    "1366x768": {"label": "1366×768（紧凑，90%）", "factor": 0.90},
    "1920x1080": {"label": "1920×1080（标准，100%）", "factor": 1.0},
    "2560x1440": {"label": "2560×1440（宽屏，115%）", "factor": 1.15},
    "3840x2160": {"label": "3840×2160（4K，130%）", "factor": 1.30},
}
UI_SCALE_LABEL_TO_PROFILE = {
    entry["label"]: key for key, entry in UI_SCALE_PROFILES.items()}

# A second, independent multiplier. The profiles above resize the whole window
# - covers, padding, row heights - which is what a 4K desktop needs. This one
# moves the glyphs and nothing else, for the smaller complaint that the layout
# is already right but the text on it is too small to read.
PREF_FONT_SCALE = "ui.font_scale"
FONT_SCALE_PROFILES = {
    "small": {"label": "小（90%）", "factor": 0.90},
    "normal": {"label": "标准（100%）", "factor": 1.00},
    "large": {"label": "大（120%）", "factor": 1.20},
    "xl": {"label": "特大（140%）", "factor": 1.40},
}
FONT_SCALE_LABEL_TO_PROFILE = {
    entry["label"]: key for key, entry in FONT_SCALE_PROFILES.items()}

_font_scale_profile = "normal"
# These two caches are what make the setting work at all. A widget refers to a
# font by name once it has been configured with one, so re-configuring the
# shared object is what reaches everything already on screen; and holding the
# reference is also what stops tkinter's Font.__del__ from deleting the
# underlying named font out from under those widgets.
_FONT_CACHE = {}
_TKFONT_CACHE = {}
_font_cache_root = None


def normalize_ui_scale_profile(profile):
    """Return a known UI scale profile, defaulting safely to system DPI."""
    profile = str(profile or "auto").strip().lower()
    return profile if profile in UI_SCALE_PROFILES else "auto"


def ui_scale_factor(profile):
    """Manual multiplier on top of CustomTkinter's detected system DPI.

    Fonts use ui_font() at their normal logical size; CustomTkinter applies the
    resulting widget scale once. This factor is not baked into ui_font sizes.
    """
    return float(UI_SCALE_PROFILES[normalize_ui_scale_profile(profile)]["factor"])


def ui_scale_multipliers(profile, system_window_dpi, system_widget_dpi):
    """Compute CTk manual factors that reach the profile's effective scale.

    CustomTkinter reports effective scaling as detected monitor DPI multiplied
    by its global manual factor. Divide by the detected DPI here so selecting a
    profile on a 150%/200% desktop does not multiply the OS scale a second time.
    """
    profile = normalize_ui_scale_profile(profile)
    if profile == "auto":
        return 1.0, 1.0

    def safe_dpi(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 1.0
        return value if math.isfinite(value) and value > 0 else 1.0

    target = ui_scale_factor(profile)
    return (max(0.4, target / safe_dpi(system_widget_dpi)),
            max(0.4, target / safe_dpi(system_window_dpi)))


def load_ui_scale_profile(conn):
    return normalize_ui_scale_profile(
        slg_db.get_pref(conn, PREF_UI_SCALE, "auto"))


def normalize_font_scale(profile):
    profile = str(profile or "normal").strip().lower()
    return profile if profile in FONT_SCALE_PROFILES else "normal"


def font_scale_factor():
    return float(FONT_SCALE_PROFILES[_font_scale_profile]["factor"])


def load_font_scale(conn):
    return normalize_font_scale(
        slg_db.get_pref(conn, PREF_FONT_SCALE, "normal"))


def _scaled_font_size(size, extra=1.0):
    return max(1, round(size * font_scale_factor() * extra))


def _drop_fonts_from_dead_root():
    """Empty both caches when the Tk interpreter they were built on is gone.

    A font object belongs to the interpreter that created it: configuring one
    whose root has been destroyed raises TclError. The app starts a single
    root and never notices, but a test that builds a second App does - and it
    fails on the very first widget, which is a confusing place to land.
    """
    global _font_cache_root
    root = getattr(tk, "_default_root", None)
    if root is not _font_cache_root:
        _FONT_CACHE.clear()
        _TKFONT_CACHE.clear()
        _font_cache_root = root


def set_font_scale(profile):
    """Switch the glyph multiplier and re-size every font already handed out.

    Nothing is rebuilt. customtkinter widgets registered a callback on the font
    they were given (CTkFont.add_size_configure_callback), and a canvas item or
    a plain tk.Label follows the named font it was configured with, so one
    configure() per cached font reaches all of them at once.
    """
    global _font_scale_profile
    _font_scale_profile = normalize_font_scale(profile)
    _drop_fonts_from_dead_root()
    for (size, _weight), font in _FONT_CACHE.items():
        font.configure(size=_scaled_font_size(size))
    for (size, _weight, extra), font in _TKFONT_CACHE.items():
        font.configure(size=_scaled_font_size(size, extra))


def clamp_scaled_size(width, height, scale, screen_width, screen_height,
                      margin=48, min_width=700, min_height=480):
    """Clamp logical window dimensions to a physical usable-screen rectangle."""
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    usable_width = max(320, int(screen_width) - int(margin))
    usable_height = max(240, int(screen_height) - int(margin))
    max_width = max(1, int(usable_width / scale))
    max_height = max(1, int(usable_height / scale))
    min_width = min(max_width, max(1, int(min_width)))
    min_height = min(max_height, max(1, int(min_height)))
    width = min(max(int(width), min_width), max_width)
    height = min(max(int(height), min_height), max_height)
    return width, height


PREF_FREE_NOTICE = "free_notice_seen"
# First-launch onboarding: shown once, then reachable again from 设置.
PREF_WELCOME_SEEN = "welcome_seen"
PREF_AGE_CONFIRMED = "age_confirmed_v1"
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
# A midnight-plum base with restrained wine / rose neon and champagne gold:
# it keeps the HUD/cyberpunk cues while giving the adult catalogue a more
# intimate, editorial feel. ACCENT is also used for small text, so keep it
# bright against the dark surfaces; ON_ACCENT is a deep plum to stay readable
# on rose buttons and selected chips.
_DARK = dict(
    _LIGHT,
    BG="#110d15", CARD="#1d1520", CARD_HOVER="#2b1d2b", SIDEBAR="#100b13",
    ACCENT="#ef6a9b", TEXT="#f5edf3", MUTED="#b8a8b5", CHIP="#382632",
    CHIP_OFF="#512637", PLACEHOLDER="#342a36", DANGER_TEXT="#ff9aa8",
    ON_ACCENT="#28131e", STAR="#f2ca77",
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
    """The one place a font for a widget is built.

    Cached rather than freshly built so set_font_scale() has something to
    reach: the widget keeps the font object it was configured with, and a
    customtkinter widget registers a callback on it, so re-configuring the
    shared object updates every widget built from it.
    """
    _drop_fonts_from_dead_root()
    key = (size, weight)
    font = _FONT_CACHE.get(key)
    if font is None:
        font = ctk.CTkFont(family=_resolve_ui_family(),
                           size=_scaled_font_size(size), weight=weight)
        _FONT_CACHE[key] = font
    return font


def ui_tkfont(size, weight="normal", dpi_scale=1.0):
    """A plain tkinter font, for canvas text and for a plain tk.Label.

    Canvas items take a font rather than a widget, and a plain tk.Label is
    never asked to scale itself, so neither gets customtkinter's automatic
    treatment - the multiplier is baked in here instead. Going through the
    same family resolution anyway: it is the only place that knows which
    installed family has CJK glyphs.

    `dpi_scale` is the caller's own widget scaling. It exists for the card
    list, whose three labels have to follow the monitor DPI by hand.
    """
    _drop_fonts_from_dead_root()
    key = (size, weight, round(dpi_scale, 4))
    font = _TKFONT_CACHE.get(key)
    if font is None:
        font = tkfont.Font(family=_resolve_ui_family(),
                           size=_scaled_font_size(size, dpi_scale),
                           weight=weight)
        _TKFONT_CACHE[key] = font
    return font


def _mix(color_a, color_b, t):
    """Blend two #rrggbb colours; t=0 gives a, t=1 gives b.

    Canvas items have no alpha, so every faint mark in the splash is a real
    blend against the colour behind it instead of a translucent overlay.
    """
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        int(round(x + (y - x) * t)) for x, y in zip(a, b))


# 至臻档的色相环。以前是把文字在下面这 6 个色值之间直接换，一帧一跳 —— 那正是
# 「看着像劣质传说」的来源：跳变读起来是闪烁，不是流光。
RAINBOW_STOPS = ("#e84393", "#e06a3f", "#e0a800", "#3fae5a", "#2f9bd0", "#8b5cf6")


def _rainbow(t):
    """色相环上的一点：t 取模到 [0,1)，在相邻色标之间线性插值，帧间连续。"""
    t = t % 1.0
    span = len(RAINBOW_STOPS)
    pos = t * span
    index = int(pos)
    return _mix(RAINBOW_STOPS[index],
                RAINBOW_STOPS[(index + 1) % span], pos - index)


def _rounded_rect(canvas, x0, y0, x1, y1, r, fill):
    """Fill a rounded rectangle on a canvas.

    Tk has no rounded-rectangle item and canvas items carry no alpha, so every
    corner in this file is four ovals with two slabs across them.
    """
    for cx, cy in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r),
                   (x1 - 2 * r, y1 - 2 * r)):
        canvas.create_oval(cx, cy, cx + 2 * r, cy + 2 * r, fill=fill, outline="")
    canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline="")
    canvas.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline="")


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

    Same chrome across the app's group-feedback and copy actions, so copied
    identifiers read as deliberate controls rather than incidental captions.

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
    key = (game["slug"], game.get("cover_file"), width, height)
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


def circle_avatar(img, size):
    """Centre-crop to a square, round it off, and scale to `size`.

    The bundled avatar is already a transparent circle; a picture the user picks
    is whatever shape their file was, and pasting a square into a round slot
    leaves four corners sticking out. One crop and one mask make the two
    indistinguishable.
    """
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w - side) // 2 + side, (h - side) // 2 + side))
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def avatar_source(own=True):
    """(path, mtime) of the avatar to draw.

    own=True prefers a picture the user set (see slg_db.avatar_path) and falls
    back to the bundled one. own=False forces the bundled one, because 关于 and
    the splash are the *author's* card: they must not start showing whichever
    picture the user put next to their own nickname.

    The mtime is part of the cache key below so a freshly picked avatar takes
    effect on the next repaint rather than the next restart.
    """
    if own:
        try:
            path = slg_db.avatar_path()
            return path, os.path.getmtime(path)
        except OSError:
            pass
    path = asset_path("avatar.png")
    try:
        return path, os.path.getmtime(path)
    except OSError:
        return None, 0


def load_avatar(size=72, own=True):
    """The avatar image. Cached so a theme switch does not re-decode it, and the
    module-level dict keeps the CTkImage referenced (an unreferenced CTkImage
    renders blank).
    """
    path, mtime = avatar_source(own)
    key = ("__avatar__", size, path, mtime)
    if key in _image_cache:
        return _image_cache[key]
    try:
        img = Image.open(path).convert("RGBA")
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


def display_game_label(game, title=None):
    """Show a translated or original title with its available metadata."""
    title = display_title(game) if title is None else title
    # Catalogue rows are sqlite3.Row objects: they support keyed access but
    # do not implement dict.get(). Let the shared formatter read them directly.
    return format_game_label(game, title=title)


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
        path = sys.executable
        channel = ("测试版" if "_test" in os.path.basename(path) else "exe")
    else:
        path, channel = os.path.abspath(__file__), "源码"
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    except OSError:
        when = "?"
    return "v%s · %s · %s" % (display_app_version(), channel, when)


def is_test_build():
    """Keep source runs and the explicitly named test exe out of live rewards."""
    if not getattr(sys, "frozen", False):
        return True
    return "_test" in os.path.basename(sys.executable).lower()


def release_notes_text():
    """Select the release wording by build channel, even when versions match."""
    channel = "测试版" if is_test_build() else "稳定版"
    return (
        "%s %s\n\n" % (display_app_version(), channel)
        + "网页端支持中文标签检索、共享账号和可折叠个人中心；游戏评论与自由论坛分开，公开内容自动审核。\n"
        "本机评分、收藏、游戏路径、私人记录和自建游戏分类不会随账号同步。\n"
        "自建游戏新增 Galgame、SLG、RPG、ACT、模拟经营、休闲/解谜、其他多选分类，支持编辑、筛选和备份。\n"
        "卡片与列表分批加载，封面后台解码；新增减少动效设置。\n"
        "改进存档重扫和取消，修复过期记录与损坏会话恢复。\n"
        "保留字体大小设置与键盘导航，补齐中文标签译名。\n\n"
        "首次使用需要确认年龄并阅读软件说明。\n"
        "工具与设置入口重新整理，公告和版本说明分别显示。\n"
        "兑换码与邀请码合并到同一窗口，可查看邀请码和邀请进度。\n"
        "个性装扮窗口集中展示已拥有头衔和装扮收集进度。\n"
        "抽奖动画或云端结果确认期间可以关闭窗口；迟到的结果不会重新弹窗，未确认请求不会重复提交。\n"
        "账号窗口新增只读服务器连接诊断；备份导入增加格式预检和覆盖确认。\n"
        "从备份恢复的旧积分与头衔保留在本机，迁移需联系管理员核对。\n\n"
        "云端功能是否可用及账号、积分状态以服务器返回为准。"
    )


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


def shop_columns(available, tile_max, tile_min, gap=8):
    """商城货架的 (列数, 卡宽)，按网格的实际可用宽度算。

    卡宽写死过一个 104，因为「最小窗口下面板有 322px」——那是个过期前提：面板宽度
    是窗口的 40%，默认窗口 1180 只有 288px，减掉货架栏和内边距后网格只剩 192px，
    两张 104 的卡放不下，于是每行只排一张、四件商品被拉成四行、底下的被挤出视野。
    现在按实际宽度回推：优先放满 `tile_max` 的卡，放不下两张就压到 `tile_min` 硬塞
    两张，实在塞不下才退回一张。

    纯函数，和 FlowFrame 的 flow_rows 一样可以脱离窗口测。
    """
    count = max(1, (available + gap) // (tile_max + gap))
    if count == 1 and available >= 2 * tile_min + gap:
        count = 2
    width = int((available - gap * (count - 1)) // count)
    return count, max(tile_min, min(tile_max, width))


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


def detail_overview_wraplength(actual_width, window_scale=1.0):
    """Fit description wrapping to the detail box in CustomTkinter units.

    Configure events report device pixels; wraplength is scaled by
    CustomTkinter. Convert with the window's Windows/display scaling factor so
    4K at 200% behaves like its 1920px logical desktop.
    """
    try:
        scale = float(window_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    try:
        width = max(0, int(actual_width))
    except (TypeError, ValueError):
        width = 0
    return max(120, int(width / scale) - 24)


def reel_geometry(cell=52, rows=3, pad=4, slots=14):
    """One reel's (canvas_height, tape_tiles) - the numbers that keep it covered.

    A reel is a tape of tile-high symbols scrolled behind a window. The tape
    loops every `slots` tiles, so its offset only ever lands in [0, slots), and
    the tiles have to reach past the bottom of the window by the largest offset:

        tiles * cell - slots * cell >= rows * cell + pad

    Get this wrong and the tape scrolls clean off the canvas, which is exactly
    what "three empty boxes" was: the tape had `slots` tiles and no spare, so
    the last sliver of travel left nothing inside the window.

    Pure arithmetic so the coverage can be proven in a test without a window.
    """
    height = rows * cell + 2 * pad
    extra = -(-height // cell)  # ceil: whole tiles needed to cross the window
    return height, slots + extra


# 抽奖弹窗底部常驻的免责声明。奖品全部是软件内的积分与虚拟头衔，没有任何货币
# 价值，也不涉及任何形式的真实下注——这话得写在用户看得见的地方，而不是只留在
# 开发者的脑子里。
SLOT_DISCLAIMER = ("本玩法为纯娱乐：奖品仅为软件内积分与虚拟头衔，无货币价值，"
                   "不可兑换现金或实物，与真实赌博无关。")


def lottery_spin_schedule(slots=18, ticks=26, start_delay=16, end_delay=200,
                          bounce=0.09):
    """(delay_ms, step_in_slots) per tick, for one lottery reel.

    Fast on the first tick and decelerating to a crawl, so the reel reads as
    losing momentum rather than stopping dead. The steps always sum to exactly
    `slots`, which is what lands the strip centred on a symbol instead of
    between two - and the last two entries are a small rebound, the little
    settle a real reel does.

    Pure maths, so the shape can be tested without opening a window.
    """
    span = max(1, ticks - 1)
    weights = [max(1.0 - (i / span) ** 2.2, 0.05) for i in range(ticks)]
    total = sum(weights)
    steps = [slots * w / total for w in weights]
    steps[-1] += slots - sum(steps)  # absorb the rounding drift
    out = []
    for i, step in enumerate(steps):
        frac = i / span
        delay = int(round(start_delay + (end_delay - start_delay) * frac ** 1.5))
        out.append((delay, step))
    out.append((80, -bounce))
    out.append((70, bounce))
    return out


# --- 抽奖音效 ---------------------------------------------------------------------
# winsound.Beep blocks its caller for the length of the tone, so it can never run
# on the reel's after() tick - that would stutter the animation. One worker plays
# a short queue in order, and a full queue drops the tone: falling behind the
# animation sounds worse than missing a tick.

_sound_queue = queue.Queue(maxsize=4)
_sound_worker = None


def _sound_loop():
    while True:
        freq, dur_ms = _sound_queue.get()
        try:
            winsound.Beep(int(freq), int(dur_ms))
        except Exception:  # noqa: BLE001 - no sound device must not break the draw
            pass


def _play_tone(freq, dur_ms):
    global _sound_worker
    if winsound is None:
        return
    if _sound_worker is None or not _sound_worker.is_alive():
        _sound_worker = threading.Thread(target=_sound_loop, daemon=True)
        _sound_worker.start()
    try:
        _sound_queue.put_nowait((freq, dur_ms))
    except queue.Full:
        pass


def lottery_jingle(grand, jackpot=False):
    """The notes played when the reels stop. Queued tones play end to end.

    jackpot is the 0.01% 幸运之王 fanfare - one note longer and a fifth higher at
    the end, so the player who actually hits it can hear that this was not the
    ordinary title win even before reading the result line.
    """
    if jackpot:
        return ((784, 90), (988, 90), (1175, 90), (1568, 120), (1976, 120),
                (2349, 380))
    if grand:
        return ((784, 90), (988, 90), (1175, 90), (1568, 240))
    return ((880, 80), (1175, 150))


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
        self._motion = MotionScheduler(self)
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
        self._motion.call_later(40, self._tick)

    def _make_fonts(self):
        """Build the canvas fonts at the display's scaling.

        A raw tk.Canvas gets none of customtkinter's scaling for free, so a
        22pt title here would come out two thirds the size of the same text in
        the window behind it on a 150% display. Rebuilt on the first layout call
        because the scaling factor is not known until the window is mapped.
        """
        # _get_window_scaling() rather than _apply_window_scaling(1.0): the
        # latter returns int(value * scaling), so the single point it was scaled
        # by was truncated to 1 and the splash stayed at 100% type on every
        # display above it.
        scale = self._get_window_scaling()
        self._title_font = ui_tkfont(int(round(22 * scale)), "bold")
        self._small_font = ui_tkfont(int(round(12 * scale)))
        self._pct_font = ui_tkfont(int(round(11 * scale)))

    def _centre(self):
        """Park the card in the middle of the screen, once it has a size.

        Both numbers have to be in the same unit. CTk multiplies every size it
        is handed by the display scaling, so a window asked for at 420x260 is
        really 630x390 on a 150% display and winfo_width() reports that grown
        value - while winfo_screenwidth() stays in the virtualised (unscaled)
        units the process sees. Subtracting one from the other used to land the
        card at 538,298 on a 2560x1600 screen whose centre is 965,605. Scaling
        the screen up by that same factor puts the two back in one space.
        """
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:      # not mapped yet; the next configure retries
            return
        self._centred = True
        scale = self._get_window_scaling()
        sw = int(self.winfo_screenwidth() * scale)
        sh = int(self.winfo_screenheight() * scale)
        self.geometry("+%d+%d" % ((sw - w) // 2, (sh - h) // 2))

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
        self._motion.call_later(40, self._tick)

    def step(self, text, progress):
        """Set stage targets; the scheduled animation advances without reentry."""
        self._status_text = text
        self._target = progress
        self._advance()



class App(ctk.CTk):
    def __init__(self, notify=True):
        super().__init__()
        # CTkImage caches PhotoImages tied to one Tcl interpreter. A new app
        # must not reuse images from a destroyed root (including smoke tests).
        _image_cache.clear()
        self.protocol("WM_DELETE_WINDOW", self._request_close)
        # Before anything else: a callback that raises must leave a trace.
        self.report_callback_exception = self._report_callback_exception
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
        self._age_gate_required = bool(
            notify and not slg_db.get_pref(self.conn, PREF_AGE_CONFIRMED))
        saved_identity_mode = slg_db.get_pref(
            self.conn, PREF_ACTIVE_IDENTITY_MODE, "") or ""
        self._startup_admin_login = (
            bool(not saved_identity_mode and slg_titles.dev_unlocked(self.conn))
            or saved_identity_mode == "admin")
        if saved_identity_mode not in ("user", "admin"):
            slg_db.set_pref(self.conn, PREF_ACTIVE_IDENTITY_MODE,
                            "admin" if self._startup_admin_login else "user")
        # Local possession of the owner key only makes the switch available.
        # The process enters administrator mode after the server authenticates it.
        slg_account.set_developer_mode(False)
        # notify is off for the packaging smoke test and the test harness, which
        # both expect an empty library; a real session imports the shipped tag
        # seed once so users without a translation API still see Chinese tags.
        if notify and (not slg_db.get_pref(self.conn, PREF_SEED_TAGS_IMPORTED)
                       or slg_db.get_pref(self.conn, "tag_seed.version") != APP_VERSION):
            seed = asset_path("tag_zh.json")
            if os.path.exists(seed):
                slg_db.import_seed_tag_translations(self.conn, seed)
                # Inside the exists() branch on purpose. Marking the import done
                # when the file was missing latched the flag: every later launch
                # skipped the import for good and left the user on English tag
                # names with no way back short of editing the db.
                slg_db.set_pref(self.conn, PREF_SEED_TAGS_IMPORTED, "1")
                slg_db.set_pref(self.conn, "tag_seed.version", APP_VERSION)
        load_tag_translations(self.conn)
        load_title_translations(self.conn)
        # Resolved before the first widget exists, so the opening frame is
        # already in the right palette instead of flashing white and repainting.
        self.theme_mode = slg_db.get_pref(self.conn, PREF_THEME, "system") or "system"
        if self.theme_mode not in _THEME_LABELS:   # hand-edited db, not worth a crash
            self.theme_mode = "system"
        # Both multipliers are resolved before the first widget is built: a font
        # size chosen after the fact would have to re-lay-out every frame.
        self.reduced_motion = slg_db.get_pref(self.conn, "ui.reduced_motion", "0") == "1"
        self.font_scale_profile = load_font_scale(self.conn)
        set_font_scale(self.font_scale_profile)
        self.ui_scale_profile = load_ui_scale_profile(self.conn)
        self._apply_ui_scale_profile(
            self.ui_scale_profile, persist=False, resize_main=False)
        ctk.set_appearance_mode(self.theme_mode)
        apply_palette(resolved_theme(self.theme_mode))
        self._sys_theme = resolved_theme(self.theme_mode)
        self.configure(fg_color=BG)
        self._resize_main_for_ui_scale()
        if notify:
            self._splash = _Splash(self)
            self._splash.step("正在加载数据库…", 0.2)
            self.update_idletasks()
        self.include, self.exclude = [], []
        self.category_ids = []
        self.category_uncategorized = False
        self.search = ""
        self.view = None
        self._wishlist_updates = []
        self.origin = "main"
        self.collection_id = None
        self.sort = "score"
        self.sort_desc = SORT_DEFAULT_DESC[self.sort]
        self.selected = None
        self._panel_mode = "game"
        self.rows = []
        # 1-based, and it indexes _page_slice(). self.rows stays the whole
        # result set on purpose: four other places read it as such (re-finding
        # the selection by id, dropping a card, resolving a click, counting
        # pages), and they would all change meaning if it held one page.
        self.page = 1
        self.queue = queue.Queue()
        self._cover_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cover")
        self._cover_waiters = {}
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
        # Remote config (feature flags + announcement) and the flags extracted
        # from it; both start empty and fill in a few seconds after launch.
        self._remote_config = {}
        self._remote_flags = {}
        self._remote_config_loaded = False
        self._announcement_reward_prompted = False
        self._maintenance_reward_status = None
        self._maintenance_reward_fetching = False
        self._announcement_reward_label = None
        self._announcement_claim_button = None
        self._quest_generation = 0
        self._quests_supported = None
        self._quests_fetching = False
        self._quests_data = None
        self._quests_error = None
        self._quest_claiming = None
        self._quest_panel = None
        self._quest_status_label = None
        self._quest_retry_button = None
        self._quest_rows = None
        self._quest_claim_buttons = {}
        self._cloud_create_pending = False
        self._cloud_login_pending = False
        self._rotate_pending = False
        self._identity_generation = 0
        self._identity_switch_pending = False
        self._owner_account_pending = False
        self._owner_secret_dialog_pending = False
        self._owner_account_operation_id = (
            slg_db.get_pref(self.conn, "admin.owner_account.pending_operation_id", "")
            or None)
        self._owner_account_bound_id = (
            slg_db.get_pref(self.conn, "admin.owner_account.account_id", "") or "")
        self._cloud_create_button = None
        self._cloud_login_button = None
        self._cloud_login_key_entry = None
        self._cloud_recovery_entry = None
        self._developer_cloud_login_pending = False
        self._developer_title_unlock_pending = False

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
        if notify and (
                self._age_gate_required
                or not slg_db.get_pref(self.conn, PREF_WELCOME_SEEN)):
            self.after(300, self._show_welcome)
        if (notify and not self._age_gate_required
                and not slg_db.get_pref(self.conn, PREF_FREE_NOTICE)):
            self.after(600, self._show_free_notice)
        # Late enough that it never delays the window appearing, and skipped
        # entirely by the smoke test, which is not a user session.
        if notify:
            self.after(3000, self._start_update_check)
            self.after(3100, self._apply_startup_compensation)
            # Only local achievements are checked automatically. Looking up a
            # public-comment count sends the persistent device ID to the server,
            # so do that only after the user explicitly publishes a comment.
            self.after(3150, self._apply_achievements)
            self.after(3200, self._start_remote_check)
            if (slg_titles.dev_unlocked(self.conn)
                    and self._startup_admin_login):
                self.after(3250, lambda: self._connect_developer_identity(
                    startup=True))
            elif self._has_usable_cloud_session():
                self.after(3250, self._fetch_maintenance_reward_status)

        if notify:
            # The bar is a real progress bar for three stages and then a fade;
            # landing it on 100% first is the difference between "finished" and
            # "gave up three quarters of the way".
            self._splash.step("准备就绪", 1.0)
            self._finish_splash()

    def _report_callback_exception(self, exc, val, tb):
        """Turn a Tk callback explosion into a file on disk plus one dialog.

        The packaged build is windowless, so `sys.stderr is None` and tkinter's
        own reporter raises while printing the traceback - the exception vanishes
        and the user is left with a button that "does nothing" or a dialog that
        flashes. Appending to crash.log is the only record that survives, and it
        is what turns the next bug report into a stack trace. The dialog is shown
        once per session: a callback that keeps failing would otherwise bury the
        window under identical popups.
        """
        if getattr(self, "_crash_reported", False):
            return
        self._crash_reported = True

        # Prefer the persistent app-data directory. If that folder is not
        # writable (or resolving it itself fails), keep a second copy in the
        # operating system's temp directory and tell the user which path worked.
        path = None
        try:
            candidate = os.path.join(slg_db.app_dir(), "crash.log")
            if slg_util.log_crash(exc, val, tb, candidate):
                path = candidate
        except Exception:  # noqa: BLE001 - a reporter must not mask the callback error
            pass
        if path is None:
            try:
                fallback_dir = os.path.join(tempfile.gettempdir(), "slgking")
                candidate = os.path.join(fallback_dir, "crash.log")
                if slg_util.log_crash(exc, val, tb, candidate):
                    path = candidate
            except Exception:  # noqa: BLE001 - keep the original Tk error visible
                pass

        try:
            if path:
                detail = "详细信息已记录到：\n%s\n\n程序还能继续用，但这一步没完成。" % path
            else:
                detail = ("错误详情无法写入日志文件，请联系作者并附上错误时间。\n\n"
                          "程序还能继续用，但这一步没完成。")
            messagebox.showwarning(
                "程序内部出错了", "刚才的操作出了点问题。\n" + detail,
                parent=self)
        except Exception:  # noqa: BLE001 - the reporter must never explode too
            pass

    def _finish_splash(self):
        """Cross-fade the splash out and the window in, then drop the splash."""
        splash = self._splash
        if splash is None:
            self._show_main_window()
            return
        if self.reduced_motion:
            self._show_main_window(splash)
            return
        try:
            self.attributes("-alpha", 0.0)
            self.deiconify()
            self._fade_splash(1)
        except tk.TclError:
            # Some window managers do not implement per-window transparency.
            # In that case, do a plain handoff instead of leaving the app at
            # alpha 0 with the 100% splash covering it forever.
            self._show_main_window(splash)

    def _show_main_window(self, splash=None):
        """Reveal the app and remove the splash without relying on alpha support."""
        if splash is None:
            splash = self._splash
        self._splash = None
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        if getattr(self, "_age_gate_required", False):
            # Keep the application itself inaccessible until the first-run
            # confirmation dialog has been accepted.
            try:
                self.withdraw()
            except tk.TclError:
                pass
            if splash is not None:
                try:
                    splash.destroy()
                except tk.TclError:
                    pass
            return
        try:
            self.deiconify()
            self.lift()
        except tk.TclError:
            pass
        if splash is not None:
            try:
                splash.destroy()
            except tk.TclError:
                pass

    def _fade_splash(self, step, total=10):
        splash = self._splash
        if splash is None:
            self._show_main_window()
            return
        frac = step / total
        try:
            self.attributes("-alpha", frac)
            splash.attributes("-alpha", 1.0 - frac)
        except tk.TclError:
            self._show_main_window(splash)
            return
        if step >= total:
            self._show_main_window(splash)
            return
        try:
            self.after(24, self._fade_splash, step + 1, total)
        except tk.TclError:
            self._show_main_window(splash)

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
        self._card_render_generation = getattr(self, "_card_render_generation", 0) + 1
        job = getattr(self, "_card_render_job", None)
        if job:
            self.after_cancel(job)
        self._card_render_job = None
        self._card_render_pending = False
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
        self.profile_btn = self.leaderboard_btn = self.shop_btn = None
        self.signin_btn = self._shop_signin_button = None

    def _poll_system(self):
        """"system" has no callback to hang off, so sample the OS setting.

        get_appearance_mode() re-reads it every call; the delay is up to one
        tick, and a real change costs a full rebuild (~480 widgets).
        """
        if self.theme_mode == "system":
            now = resolved_theme("system")
            if now != self._sys_theme:
                self._apply_theme("system")
        cloud_day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        previous_day = getattr(self, "_shop_signin_day", cloud_day)
        if previous_day != cloud_day:
            self._update_shop_signin_button()
            if (getattr(self, "_panel_mode", None) == "shop"
                    and self._has_cloud_account()):
                self._run_cloud_action("me", slg_account.me)
        self._shop_signin_day = cloud_day
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
        age_required = not slg_db.get_pref(self.conn, PREF_AGE_CONFIRMED)
        if not age_required:
            # Preserve the existing behavior for opening the guide from 设置.
            slg_db.set_pref(self.conn, PREF_WELCOME_SEEN, "1")

        def _exit_without_confirmation():
            # Closing the first-run gate, including Escape, exits without
            # recording either confirmation preference.
            self.destroy()

        win = self._new_dialog(
            "欢迎使用 SLG黄游大王", "540x680",
            close_guard=_exit_without_confirmation if age_required else None)
        if age_required:
            # The main window stays withdrawn during the gate. _new_dialog()
            # makes every dialog transient to the main window by default; on
            # Windows that can hide this dialog along with its withdrawn owner.
            # Passing an empty master explicitly clears WM_TRANSIENT_FOR.
            win.transient("")
            win.deiconify()
            win.update_idletasks()
            win.lift()
            try:
                win.focus_force()
            except tk.TclError:
                pass
            # Grab only after the toplevel has been made visible. The root is
            # still withdrawn, so the age confirmation remains the only UI.
            win.grab_set()
        ctk.CTkLabel(win, text="欢迎使用 SLG黄游大王", text_color=TEXT,
                     font=ui_font(size=18, weight="bold")).pack(pady=(18, 2))
        ctk.CTkLabel(
            win, text="免费 · 帮你整理检索 dikgames 游戏，并学习你的 xp 口味、推送对口游戏。",
            text_color=MUTED, font=ui_font(size=12), justify="left", anchor="w",
            wraplength=480).pack(fill="x", padx=24, pady=(0, 12))

        if age_required:
            ctk.CTkLabel(
                win,
                text=("年龄提示与免责声明：本客户端收录的游戏可能包含成人题材或相关内容。"
                      "仅限年满18周岁的用户使用，请在使用前自行判断内容是否适合。"),
                text_color=TEXT, font=ui_font(size=12), justify="left", anchor="w",
                wraplength=480).pack(fill="x", padx=24, pady=(0, 10))

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
        _section("云端账号与评论（0.23）",
                 "云端账号用于跨设备保存昵称、积分、头衔、签到和公开评论。新建账号后请妥善保存登录密钥与恢复码；"
                 "本机游戏库、评分、收藏夹和私人评论仍保存在当前电脑。")
        _section("查看和发布评论",
                 "打开游戏详情，点「写评论&查看评论区」进入独立评论页，可翻页、按最新或热门排序，也能查看自己的本机记录。"
                 "公开评论会经过自动审核；符合规则并通过审核后才展示和发放奖励。")

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
        ctk.CTkLabel(body, text="以后想再看这份说明，点右上角 ⚙ → 查看新手引导；问题反馈请加入交流群 1124074040。",
                     text_color=MUTED, anchor="w", justify="left", wraplength=460,
                     font=ui_font(size=11)).pack(fill="x", padx=8, pady=(12, 6))

        age_confirmed = tk.BooleanVar(master=win, value=False)
        if age_required:
            ctk.CTkCheckBox(
                win, text="我已年满18周岁", variable=age_confirmed,
                text_color=TEXT, fg_color=ACCENT, hover_color=CARD,
                font=ui_font(size=13)).pack(
                    fill="x", padx=24, pady=(0, 8))

            actions = ctk.CTkFrame(win, fg_color="transparent")
            actions.pack(fill="x", padx=24, pady=(0, 16))
            actions.grid_columnconfigure((0, 1), weight=1, uniform="age-actions")

            ctk.CTkButton(
                actions, text="不同意，退出", height=38, corner_radius=8,
                fg_color=CARD, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=13), command=_exit_without_confirmation
            ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

            def _update_confirm_state():
                confirm_button.configure(
                    state="normal" if age_confirmed.get() else "disabled")

            def _confirm_age_and_continue():
                if not age_confirmed.get():
                    return
                slg_db.set_pref(self.conn, PREF_AGE_CONFIRMED, "1")
                slg_db.set_pref(self.conn, PREF_WELCOME_SEEN, "1")
                self._age_gate_required = False
                win.destroy()
                self.deiconify()
                self.lift()
                if not slg_db.get_pref(self.conn, PREF_FREE_NOTICE):
                    self.after(600, self._show_free_notice)

            confirm_button = ctk.CTkButton(
                actions, text="确认并进入", height=38, corner_radius=8,
                fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD,
                font=ui_font(size=13), state="disabled",
                command=_confirm_age_and_continue)
            confirm_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            age_confirmed.trace_add("write", lambda *_: _update_confirm_state())
        else:
            actions = ctk.CTkFrame(win, fg_color="transparent")
            actions.pack(fill="x", padx=24, pady=(0, 16))
            ctk.CTkButton(
                actions, text="开始使用", height=38, corner_radius=8,
                fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD,
                font=ui_font(size=14), command=win.destroy).pack(fill="x")

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
        self._content_body = body
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        self.list = ctk.CTkScrollableFrame(body, fg_color="transparent")
        self.list.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.list.grid_columnconfigure(0, weight=1)

        # Card navigation is bound to the window, not to the cards - see
        # _card_key_owns_keystroke for why. Bound here rather than in __init__
        # because _build is what makes self.list exist.
        for sequence in ("<Up>", "<Down>", "<Prior>", "<Next>", "<Return>"):
            self.bind(sequence, self._on_card_key)
        if not getattr(self, "_dpi_hook_registered", False):
            self._dpi_hook_registered = True
            ctk.ScalingTracker.add_widget(self._on_dpi_scaling_changed, self)

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
        brand = ctk.CTkFrame(header, fg_color=CARD, corner_radius=12,
                             border_width=1, border_color=CHIP)
        brand.pack(fill="x", padx=10, pady=(14, 6))
        ctk.CTkLabel(brand, text="", image=load_avatar(54, own=False)).pack(
            pady=(10, 4))
        ctk.CTkLabel(brand, text=APP_TITLE, text_color=TEXT,
                     font=ui_font(size=17, weight="bold")).pack(
            fill="x", padx=6, pady=(0, 1))
        ctk.CTkLabel(brand, text=AUTHOR, text_color=ACCENT,
                     font=ui_font(size=12, weight="bold")).pack(pady=(0, 8))
        ctk.CTkLabel(header, text=APP_VERSION_LABEL, text_color=MUTED,
                     font=ui_font(size=10)).pack(padx=18, pady=(1, 0))
        _copy_button(header, "问题反馈 · QQ 群 1124074040", QQ_GROUP, self).pack(
            fill="x", padx=12, pady=(5, 0))
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
                nav, label, lambda s=status: self._open_view(s),
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
        # 个人 / 排行榜 / 积分商城 sit side by side in the reserved empty cell
        # directly below the sort cluster (bar row 1, col 1), vertically centred
        # with the VPN notice band on the left and filling the row's right edge.
        # Daily sign-in lives in the shop, next to the wallet and lottery.
        prof_row = ctk.CTkFrame(bar, fg_color="transparent")
        prof_row.grid(row=1, column=1, sticky="e", padx=(20, 0), pady=(9, 0))
        self.profile_btn = ctk.CTkButton(
            prof_row, text="个人", width=64, height=38, corner_radius=8,
            fg_color=CARD, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=13), command=self.open_profile)
        self.profile_btn.pack(side="left")
        self.leaderboard_btn = ctk.CTkButton(
            prof_row, text="排行榜", width=78, height=38,
            corner_radius=8, fg_color=CARD, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=13), command=self.open_leaderboard)
        self.leaderboard_btn.pack(side="left", padx=(6, 0))
        self.shop_btn = ctk.CTkButton(
            prof_row, text="积分商城", width=88, height=38, corner_radius=8,
            fg_color=CARD, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=13), command=self.open_shop)
        self.shop_btn.pack(side="left", padx=(6, 0))
        # The gear lands on 设置, not 关于: the theme switch lives in there now,
        # and 关于 is the first row inside it. Same width as the button it
        # replaced, taller to match the two controls beside it.
        self.settings_btn = ctk.CTkButton(
            right, text="⚙", width=38, height=38, corner_radius=8,
            fg_color=CARD, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=14), command=self.open_settings)
        self.settings_btn.pack(side="left", padx=(6, 0))
        self._refresh_announcement_badge()

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
                      command=self.open_data_sources
                      ).pack(side="left", pady=6)

    def _on_theme_pick(self, label):
        mode = next(m for m, text in _THEME_LABELS.items() if text == label)
        # Deferred: this runs from inside the segmented button's own press
        # handler, and the switch destroys that button.
        self.after(1, lambda: self._apply_theme(mode))

    def _build_filterbar(self):
        # Lives inside the toolbar's search column. Keep it full width so tag
        # chips can wrap when the list column gets narrow instead of running
        # underneath the sort controls or being clipped at the window edge.
        self.filterbar = ctk.CTkFrame(self.toolbar, fg_color="transparent")
        self.filterbar.grid(row=2, column=0, columnspan=1, sticky="ew",
                            pady=(12, 10))
        self.filterbar.grid_columnconfigure(0, weight=1)
        self.filterbar_inner = ctk.CTkFrame(self.filterbar, fg_color="transparent")
        self.filterbar_inner.grid(row=0, column=0, sticky="ew")
        self.filterbar_inner.bind("<Configure>", self._queue_filterbar_layout)
        self._filterbar_widgets = []
        self._filterbar_layout_pending = False

    def _queue_filterbar_layout(self, _event=None):
        """Debounce responsive chip layout until Tk has settled widget sizes."""
        if self._filterbar_layout_pending:
            return
        self._filterbar_layout_pending = True
        try:
            self.after_idle(self._layout_filterbar)
        except tk.TclError:
            self._filterbar_layout_pending = False

    def _layout_filterbar(self):
        """Wrap filter controls onto another row when the list column is narrow."""
        self._filterbar_layout_pending = False
        bar = self.filterbar_inner
        try:
            if not bar.winfo_exists():
                return
            available = bar.winfo_width() - 8
        except tk.TclError:
            return
        if available < 80:
            return  # the first Configure event can arrive before geometry settles

        rows = []
        current = []
        current_width = 0
        gap = 6
        for widget in self._filterbar_widgets:
            try:
                if not widget.winfo_exists():
                    continue
                width = widget.winfo_reqwidth()
                next_width = current_width + (gap if current else 0) + width
                if current and next_width > available:
                    rows.append(current)
                    current = []
                    current_width = 0
                current.append((widget, width))
                current_width += (gap if len(current) > 1 else 0) + width
            except tk.TclError:
                # A filter change can destroy an old chip while a resize layout
                # is queued. The next render lays out the replacement controls.
                continue

        if current:
            rows.append(current)

        # Each wrapped row is centered independently. The toolbar's notice is
        # centered too; leaving a short filter row against the left edge made
        # the controls look detached from that band on wide windows.
        column_widths = []
        for row_items in rows:
            for index, (_widget, width) in enumerate(row_items):
                if index == len(column_widths):
                    column_widths.append(width)
                else:
                    column_widths[index] = max(column_widths[index], width)
        try:
            scale = float(ctk.ScalingTracker.get_window_scaling(
                self.winfo_toplevel()))
        except (AttributeError, tk.TclError, TypeError, ValueError):
            scale = 1.0
        for row_index, row_items in enumerate(rows):
            grid_width = (sum(column_widths[:len(row_items)])
                          + gap * scale * max(0, len(row_items) - 1))
            # CustomTkinter scales geometry-manager padding, while winfo widths
            # are already physical pixels. Convert the centering offset back
            # before passing it to grid().
            centered_width = bar.winfo_width()
            left_pad = max(0, int((centered_width - grid_width) / (2 * scale)))
            for column, (widget, _width) in enumerate(row_items):
                try:
                    widget.grid(
                        row=row_index, column=column, sticky="w",
                        padx=(left_pad if column == 0 else 0,
                              gap if column < len(row_items) - 1 else 0),
                        pady=(0, 4))
                except tk.TclError:
                    continue

    def _render_filterbar(self):
        # The chips are a function of the filters and nothing else, so a refresh
        # for an unrelated reason (a sync tick, a status write) rebuilding them
        # was pure flicker.
        signature = (tuple(self.include), tuple(self.exclude), self.origin,
                     tuple(self.category_ids), self.category_uncategorized)
        if signature == self._filter_sig:
            return
        self._filter_sig = signature
        bar = self.filterbar_inner
        for child in bar.winfo_children():
            child.destroy()
        self._filterbar_widgets = []

        def add(widget):
            self._filterbar_widgets.append(widget)
            return widget

        add(ctk.CTkLabel(bar, text="筛选", text_color=MUTED,
                         font=ui_font(size=13)))
        if not self.include and not self.exclude:
            # A button, not a label: it was telling the user about 标签库 while
            # being the one thing in the row that could not open it.
            add(ctk.CTkButton(bar, text="未设置 — 点这里挑标签",
                              height=24, corner_radius=12, fg_color="transparent",
                              text_color=ACCENT, hover_color=CHIP,
                              font=ui_font(size=12),
                              command=self.open_tag_picker))

        def chip(slug, excluded):
            label = ("× " if excluded else "") + display_tag(slug)
            return add(ctk.CTkButton(
                bar, text=label, height=26, corner_radius=13,
                fg_color=CHIP_OFF if excluded else CHIP,
                text_color=DANGER_TEXT if excluded else TEXT,
                hover_color=CARD_HOVER,
                font=ui_font(size=12),
                command=lambda s=slug, e=excluded: self._drop_chip(s, e)))

        for slug in self.include:
            chip(slug, False)
        for slug in self.exclude:
            chip(slug, True)
        if self.include or self.exclude:
            add(ctk.CTkButton(bar, text="＋ 添加标签", height=26,
                              corner_radius=13, fg_color="transparent",
                              text_color=ACCENT, hover_color=CHIP,
                              font=ui_font(size=12),
                              command=self.open_tag_picker))
            add(ctk.CTkButton(bar, text="清空", width=54, height=26,
                              corner_radius=13, fg_color="transparent",
                              text_color=ACCENT, hover_color=CHIP,
                              font=ui_font(size=12),
                              command=self.clear_filters))
        if self.origin == "user":
            names = [label for cid, label in CATEGORIES if cid in self.category_ids]
            if self.category_uncategorized:
                names.append("未分类")
            add(ctk.CTkButton(bar, text="分类：" + (" / ".join(names) or "全部"),
                height=26, corner_radius=13, fg_color=CHIP, text_color=TEXT,
                hover_color=CARD_HOVER, font=ui_font(size=12),
                command=self.open_category_filter))
        self._queue_filterbar_layout()

    def _category_checklist(self, parent, selected=()):
        flow = FlowFrame(parent, fg_color="transparent", gap_x=8, gap_y=6)
        flow.pack(fill="x", pady=(4, 8))
        variables = {cid: ctk.BooleanVar(value=cid in selected) for cid, _ in CATEGORIES}
        flow.set_items([self._tag_checkbox(flow, label, variables[cid])
                        for cid, label in CATEGORIES])
        return variables

    def open_category_filter(self):
        win = self._new_dialog("本地游戏分类", "420x300")
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=18)
        ctk.CTkLabel(body, text="多项分类满足任一项；不选表示全部", text_color=MUTED,
                     font=ui_font(size=12)).pack(anchor="w")
        variables = self._category_checklist(body, self.category_ids)
        uncategorized = ctk.BooleanVar(value=self.category_uncategorized)
        self._tag_checkbox(body, "未分类", uncategorized).pack(anchor="w", pady=6)
        def apply():
            self.category_ids = [cid for cid, var in variables.items() if var.get()]
            self.category_uncategorized = bool(uncategorized.get())
            self.page = 1
            win.destroy()
            self.refresh()
        ctk.CTkButton(body, text="应用", command=apply, fg_color=ACCENT,
                     text_color=ON_ACCENT).pack(fill="x", pady=10)

    # --- state changes --------------------------------------------------------

    def set_view(self, status):
        self.view = status
        self.origin = "main"
        self.page = 1
        for key, btn in self.view_buttons.items():
            btn.configure(fg_color=CARD if key == status else "transparent")
        self._paint_user_view()
        self.refresh()

    def _open_view(self, status):
        """Open a status view; entering the wishlist can show pending versions."""
        self.set_view(status)
        if status == "want":
            self.after_idle(self._show_wishlist_updates)

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
        if not self._require_personal_access("管理收藏夹"):
            return
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
        if not self._require_personal_access("删除收藏夹"):
            return
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
        translation_ids = (slg_db.title_translation_game_ids(self.conn, self.search)
                           if self.search else None)
        self.rows = slg_db.find_games(
            self.conn, include=self.include, exclude=self.exclude,
            search=self.search or None,
            statuses=[self.view] if self.view else None,
            downloaded_only=False, collection_id=self.collection_id,
            origin=self.origin, sort=self.sort, desc=self.sort_desc,
            translation_ids=translation_ids,
            category_ids=self.category_ids if self.origin == "user" else (),
            uncategorized=self.category_uncategorized if self.origin == "user" else False)
        self._refresh_wishlist_notice()
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

    def _refresh_wishlist_notice(self):
        """Refresh the local-only count of new versions in the want view."""
        try:
            updates = slg_db.wishlist_version_changes(self.conn)
        except Exception:  # noqa: BLE001 - a reminder must not interrupt browsing
            return
        self._wishlist_updates = list(updates)
        button = getattr(self, "view_buttons", {}).get("want")
        if button is not None and button.winfo_exists():
            label = "想玩"
            if updates:
                label += " · 更新 %d" % len(updates)
            button.configure(text=label)

    def _show_wishlist_updates(self):
        """Show site-version changes only when the user opens their wishlist."""
        self._refresh_wishlist_notice()
        rows = list(self._wishlist_updates)
        if not rows:
            return
        win = self._new_dialog("想玩清单更新", "500x540")
        ctk.CTkLabel(
            win, text="想玩但尚未安装的游戏中，有 %d 款出现了新版本。" % len(rows),
            text_color=MUTED, font=ui_font(size=12), wraplength=440,
            justify="left").pack(anchor="w", padx=16, pady=(14, 8))
        box = ctk.CTkScrollableFrame(win, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        for row in rows:
            card = ctk.CTkFrame(box, fg_color=CARD, corner_radius=8)
            card.pack(fill="x", padx=4, pady=4)
            ctk.CTkLabel(card, text=row["title"], text_color=TEXT,
                         anchor="w", font=ui_font(size=13, weight="bold"))\
                .pack(fill="x", padx=12, pady=(8, 2))
            before = row.get("seen_version") or "未知"
            after = row.get("version") or "未知"
            changed = "版本 %s → %s" % (before, after)
            if row.get("last_updated"):
                changed += " · 更新于 " + str(row["last_updated"])
            ctk.CTkLabel(card, text=changed, text_color=MUTED,
                         anchor="w", font=ui_font(size=11)).pack(
                fill="x", padx=12, pady=(0, 5))
            ctk.CTkButton(
                card, text="标记已读", width=86, height=25, corner_radius=7,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=10),
                command=lambda r=dict(row), w=win:
                    self._ack_wishlist_update(r, w)).pack(
                        anchor="e", padx=10, pady=(0, 8))

        actions = ctk.CTkFrame(win, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(
            actions, text="全部标记已读", height=32, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
            font=ui_font(size=12),
            command=lambda rs=rows, w=win: self._ack_all_wishlist_updates(rs, w)
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(
            actions, text="稍后", height=32, corner_radius=8,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=12), command=win.destroy
        ).pack(side="left", fill="x", expand=True, padx=(6, 0))

    def _ack_wishlist_update(self, row, win):
        slg_db.mark_wishlist_version_seen(
            self.conn, row["id"], row.get("version"))
        if win.winfo_exists():
            win.destroy()
        self._refresh_wishlist_notice()
        if self._wishlist_updates:
            self.after_idle(self._show_wishlist_updates)

    def _ack_all_wishlist_updates(self, rows, win):
        for row in rows:
            slg_db.mark_wishlist_version_seen(
                self.conn, row["id"], row.get("version"))
        if win.winfo_exists():
            win.destroy()
        self._refresh_wishlist_notice()

    def _render_stats(self):
        stats = slg_db.stats(self.conn)
        gaps = slg_db.data_gaps(self.conn)
        text = ("%d 款 · %d 标签\n评分 %d 款 · 已下载 %d 款"
                % (stats["games"], stats["tags"], stats["rated"],
                   stats["downloaded"]))
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
        self._card_render_generation = getattr(self, "_card_render_generation", 0) + 1
        generation = self._card_render_generation
        old_job = getattr(self, "_card_render_job", None)
        if old_job:
            try:
                self.after_cancel(old_job)
            except tk.TclError:
                pass
        self._card_render_job = None
        self._card_render_pending = True
        tags = slg_db.game_tags_bulk(self.conn, [g["id"] for g in games])
        dirty, self._pool_dirty = self._pool_dirty, False
        for i in range(len(games), len(self._card_pool)):
            if self._pool_gid[i] is not None:
                self._card_pool[i]["frame"].pack_forget()
                self._pool_gid[i] = None
        index = [0]
        def batch():
            if generation != self._card_render_generation:
                return
            budget = TimeBudget(milliseconds=6, max_items=80)
            while index[0] < len(games) and budget.available:
                i = index[0]
                game = games[i]
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
                index[0] += 1
                budget.consumed()
            self._reset_card_index()
            if index[0] < len(games):
                self._card_render_job = self.after(1, batch)
            else:
                self._card_render_job = None
                self._card_render_pending = False
        batch()

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
        self._sync_cards(self._page_slice())
        self._rendered_ids = [g["id"] for g in self._page_slice()]
        self._render_pager()
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

    def _card_key_owns_keystroke(self):
        """True when the keystroke belongs to a text box, not to the card list.

        The card bindings live on the window rather than on the cards, because
        giving eighty tk.Labels a tab stop each would put eighty worthless
        stops between the search box and the detail panel - which is why there
        is no takefocus anywhere in this file. The price is that these bindings
        also see keys aimed at the search box and the pager's page box, and an
        Entry wants its arrows and its Return for itself.
        """
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            return True
        return isinstance(focused, (tk.Entry, tk.Text))

    def _move_selection(self, delta):
        """Move the card selection by one row, turning the page at either end."""
        page = self._page_slice()
        if not page:
            return "break"
        index = next((i for i, g in enumerate(page)
                      if self.selected is not None
                      and g["id"] == self.selected["id"]), None)
        if index is None:
            # Nothing selected yet: the first press picks the end nearest the
            # key, so Down enters at the top and Up enters at the bottom.
            target = 0 if delta > 0 else len(page) - 1
        else:
            target = index + delta
        if target < 0 or target >= len(page):
            before = self.page
            self._goto_page(self.page + (1 if delta > 0 else -1))
            if self.page == before:
                return "break"          # already on the first or last page
            page = self._page_slice()
            if not page:
                return "break"
            target = 0 if delta > 0 else len(page) - 1
        self.select(page[target])
        self._scroll_card_into_view(page[target]["id"])
        return "break"

    def _activate_selected_card(self):
        """Enter does exactly what a click does - open the detail panel.

        Deliberately not "launch the game": the mouse never launched anything
        from this list, and a keystroke that starts a process is the one
        mistake here that cannot be undone with another keystroke.
        """
        page = self._page_slice()
        if not page:
            return "break"
        gid = self.selected["id"] if self.selected is not None else None
        game = next((g for g in page if g["id"] == gid), None)
        self.select(game if game is not None else page[0])
        return "break"

    def _scroll_card_into_view(self, gid):
        """Bring the selected row into the viewport after a keyboard move.

        CTkScrollableFrame has no per-widget scrolling, so the row's y inside
        the scroll region is converted to a canvas fraction. Deferred through
        after_idle because a page turn repaints the cards first.
        """
        def apply():
            canvas = getattr(self.list, "_parent_canvas", None)
            if canvas is None:
                return
            slot = next((s for row_gid, s in zip(self._pool_gid, self._card_pool)
                         if row_gid == gid), None)
            if slot is None:
                return
            try:
                region = canvas.bbox("all")
                if not region or region[3] <= 0:
                    return
                top = slot["frame"].winfo_y()
                canvas.yview_moveto(max(0.0, min(1.0, top / region[3])))
            except Exception:  # noqa: BLE001 - scrolling must never crash
                pass
        self.after_idle(apply)

    def _on_card_key(self, event):
        if self._card_key_owns_keystroke():
            return None
        keysym = event.keysym
        if keysym == "Up":
            return self._move_selection(-1)
        if keysym == "Down":
            return self._move_selection(1)
        if keysym == "Prior":
            self._prev_page()
            return "break"
        if keysym == "Next":
            self._next_page()
            return "break"
        if keysym in ("Return", "KP_Enter"):
            return self._activate_selected_card()
        return None

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
        # Same guard as the comment composer's, for the same reason.
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
        return (game["id"], game["status"], game["my_rating"],
                game["cover_file"], self.collection_id,
                game.get("origin"), game.get("promoted"),
                tuple(game.get("categories", ())))

    def _render_detail_if_stale(self):
        if self._panel_mode == "profile":
            self.open_profile()
            return
        if self._panel_mode == "shop":
            self.open_shop()
            return
        if self._panel_mode == "comments":
            return
        if self._detail_signature() == self._detail_sig:
            return
        self._render_detail()

    def _on_card_click(self, card):
        """Open whatever game this card is showing *now*.

        The binding deliberately does not capture the game: cards outlive the row
        they were built for, so a closure holding `game` would open the previous
        occupant of that slot.
        """
        # Clicking a card is also how a keyboard user gets back out of the
        # search box: focus parked in an Entry swallows the arrow keys, so the
        # list has to take focus back or the arrows stay dead after a search.
        try:
            self.list.focus_set()
        except tk.TclError:
            pass
        gid = self._widget_gid.get(card)
        if gid is None:
            return
        game = next((g for g in self.rows if g["id"] == gid), None)
        if game is not None:
            self.select(game)

    def _card_text_fonts(self, card):
        """The three card fonts, at this card's own widget scaling.

        ui_tkfont bakes in the font-size setting; `dpi` is the half that
        customtkinter would have applied for us had these been CTkLabels
        rather than the plain tk.Labels the cold-build note below explains.
        """
        dpi = card._apply_widget_scaling(1.0)
        return (ui_tkfont(15, "bold", dpi_scale=dpi),
                ui_tkfont(12, dpi_scale=dpi),
                ui_tkfont(12, dpi_scale=dpi))

    def _new_card(self, game, tags):
        """Build one card's widgets. Everything that does not change with the
        game - geometry, fonts, wrap width - is set here and never again.

        "Never again" holds except for the three fonts and the wrap width,
        which _refresh_card_text_scaling re-fits if the font size or the
        monitor DPI changes while the card is on screen.

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

        # tk.Label takes raw pixels where CTkLabel scaled for us, so the wrap
        # width and the three fonts both have to be scaled by hand. Leaving the
        # fonts unscaled was why the card text stayed the same physical size on
        # every monitor: CTkFont holds a pixel size and never scales itself.
        wrap = card._apply_widget_scaling(CARD_WRAP)
        title_font, meta_font, tagline_font = self._card_text_fonts(card)

        title = tk.Label(card, text="", bg=CARD, fg=TEXT, anchor="w",
                         font=title_font)
        title.grid(row=0, column=1, sticky="ew", pady=(10, 0))

        meta = tk.Label(card, text="", bg=CARD, fg=MUTED, anchor="w",
                        font=meta_font)
        meta.grid(row=1, column=1, sticky="ew")

        tagline = tk.Label(card, text="", bg=CARD, fg=MUTED, anchor="w",
                           wraplength=wrap, justify="left",
                           font=tagline_font)
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
        self._bind_cover(slot["img"], game)
        title = display_game_label(game)
        slot["title"].configure(text=title)
        slot["meta"].configure(text=meta_text(game),
                               fg=ACCENT if game["complete"] else MUTED)
        slot["tagline"].configure(text="  ".join(
            display_tag(t) for t in card_tags(tags)))

    def _bind_cover(self, widget, game, width=COVER_W, height=COVER_H):
        key = (game["slug"], game.get("cover_file"), width, height)
        widget._slg_cover_key = key
        if key in _image_cache:
            widget.configure(image=_image_cache[key])
            return
        widget.configure(image=load_cover({"slug": "__empty__", "cover_file": None}, width, height))
        name = game.get("cover_file")
        if not name or name.startswith("pending:"):
            return
        # Readers only decode PIL images. CTkImage creation/binding stays on Tk.
        path = os.path.join(slg_db.covers_dir(), name)
        generation = getattr(self, "_card_render_generation", 0)
        if key in self._cover_waiters:
            self._cover_waiters[key].append((widget, generation))
            return
        self._cover_waiters[key] = [(widget, generation)]
        def decode():
            try:
                with Image.open(path) as source:
                    image = source.convert("RGB")
                    image.thumbnail((width * 2, height * 2), Image.LANCZOS)
            except Exception:  # a malformed image must still release pending readers
                image = None
            self.queue.put(("cover_decoded", (key, image)))
        self._cover_executor.submit(decode)

    def _cover_decoded(self, key, image):
        waiters = self._cover_waiters.pop(key, ())
        if image is None:
            return
        ctk_image = ctk.CTkImage(light_image=image, size=key[-2:])
        _image_cache[key] = ctk_image
        for widget, generation in waiters:
            try:
                if widget.winfo_exists() and getattr(widget, "_slg_cover_key", None) == key:
                    # A reused card carries a new key; obsolete results never repaint it.
                    widget.configure(image=ctk_image)
            except tk.TclError:
                pass

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
        title = display_game_label(game)
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
        # In profile mode the panel is showing 个人中心, not this game - so even
        # a re-click on the same game must switch the panel back to game view.
        if old is not None and old["id"] == game["id"] and self._panel_mode == "game":
            return  # already open; rebuilding the panel would just flicker
        self._panel_mode = "game"
        self._set_shop_wide_layout(False)
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
        self._active_comments_box = None
        self._comment_entry = None
        self._composer_slug = None
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
            d, parts, "url", "跳转到外部站点", pady=(10, 4)))

        order.extend(self._build_detail_back_profile(d, parts))
        order.extend(self._build_detail_launch(d, parts))
        order.extend(self._build_detail_open_folder(d, parts))
        order.extend(self._build_detail_collect(d, parts))
        order.extend(self._build_detail_remove_collection(d, parts))
        order.extend(self._build_detail_promote(d, parts))
        order.extend(self._build_detail_edit(d, parts))
        order.extend(self._build_detail_delete(d, parts))

        order.extend(self._build_detail_status(d, parts))
        order.extend(self._build_detail_stars(d, parts))
        order.extend(self._build_detail_heat(d, parts))
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
        feedback = ctk.CTkFrame(d, fg_color="transparent")
        ctk.CTkLabel(feedback, text="问题或功能建议？使用窗口底部的交流群入口。",
                     text_color=MUTED, font=ui_font(size=11), wraplength=340,
                     justify="left", anchor="w").pack(fill="x")
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
                  "launch": show_folder,
                  "remove_collection": show_remove,
                  "promote": show_user, "edit_game": show_user,
                  "delete_game": show_user}
        keys = [key for key, _w, _p in self._detail_order if wanted.get(key, True)]
        if keys == self._detail_shown:
            return
        for _key, widget, _pack in self._detail_order:
            widget.pack_forget()
        for key, widget, pack in self._detail_order:
            if key in keys:
                widget.pack(**pack)
        self._detail_shown = keys

    @staticmethod
    def _public_game_source_url(url):
        """Return a safe public catalogue URL, or None for any other host."""
        if not isinstance(url, str) or not url.strip():
            return None
        try:
            parsed = urlsplit(url.strip())
            host = (parsed.hostname or "").lower().rstrip(".")
            if (parsed.scheme.lower() != "https" or not parsed.netloc
                    or parsed.username or parsed.password
                    or parsed.port not in (None, 443)):
                return None
        except (TypeError, ValueError):
            return None
        if host not in {
                "dikgames.com", "www.dikgames.com",
                "f95zone.to.it", "www.f95zone.to.it"}:
            return None
        return url.strip()

    def _external_game_sources(self, game):
        """Read public source rows; keep old catalogs working via games.url.

        Zyoudao is intentionally excluded from user-facing links. The source
        API is additive, so an older local database can safely fall back to its
        original Dikgames URL.
        """
        sources = []
        seen_hosts = set()
        loader = getattr(slg_db, "list_game_sources", None)
        if callable(loader):
            try:
                rows = loader(self.conn, game.get("id"), public_only=True) or []
            except Exception:
                rows = []
            for row in rows:
                try:
                    item = dict(row)
                    source_id = str(item.get("source_id", "")).strip().lower()
                    if "zyoudao" in source_id or "zyoudao.net" in str(
                            item.get("url", "")).lower():
                        continue
                    target = self._public_game_source_url(item.get("url"))
                    if not target:
                        continue
                    host = urlsplit(target).hostname.lower().rstrip(".")
                    label = ("Dikgames" if host.endswith("dikgames.com")
                             else "F95zone.to.it")
                    # The chooser represents sites. Multiple records from one
                    # site must not turn into several indistinguishable buttons.
                    site_key = "dikgames" if host.endswith("dikgames.com") else "f95zone"
                    if site_key not in seen_hosts:
                        seen_hosts.add(site_key)
                        sources.append({"source_id": source_id,
                                        "label": label, "url": target})
                except (TypeError, ValueError, AttributeError):
                    continue

        # Existing installs and older catalog snapshots contain only games.url.
        # Add that target only when no valid source mapping was available.
        if not sources and game.get("url"):
            target = self._public_game_source_url(game.get("url"))
            if target:
                host = urlsplit(target).hostname.lower().rstrip(".")
                sources.append({
                    "source_id": "dikgames" if host.endswith("dikgames.com") else "f95zone",
                    "label": "Dikgames" if host.endswith("dikgames.com") else "F95zone.to.it",
                    "url": target,
                })
        return sources

    def _open_game_source(self, source):
        # Re-validate at the point of use in case a widget retained stale data.
        target = self._public_game_source_url(source.get("url"))
        if target:
            webbrowser.open(target)

    def _choose_game_source(self, game, sources):
        sources = [dict(item) for item in sources
                   if self._public_game_source_url(item.get("url"))]
        if not sources:
            return
        if len(sources) == 1:
            self._open_game_source(sources[0])
            return
        win = self._new_dialog("选择外部站点", "340x250")
        ctk.CTkLabel(
            win, text=display_game_label(game, self._title_to_show(game)), text_color=TEXT,
            font=ui_font(size=14, weight="bold"), wraplength=300,
            justify="center").pack(fill="x", padx=16, pady=(16, 6))
        ctk.CTkLabel(
            win, text="这款游戏有多个可用来源，选择要打开的站点。",
            text_color=MUTED, font=ui_font(size=11), wraplength=300,
            justify="center").pack(fill="x", padx=16, pady=(0, 10))
        for source in sources:
            ctk.CTkButton(
                win, text=source["label"], height=34, corner_radius=8,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=12),
                command=lambda item=source, dialog=win: (
                    dialog.destroy(), self._open_game_source(item))
            ).pack(fill="x", padx=18, pady=4)
        ctk.CTkButton(
            win, text="取消", height=30, corner_radius=8,
            fg_color="transparent", text_color=MUTED,
            hover_color=CHIP, font=ui_font(size=11),
            command=win.destroy).pack(fill="x", padx=18, pady=(8, 14))

    def _fill_detail(self, game):
        p = self._detail_parts
        self._bind_cover(p["cover"], game, DETAIL_W, DETAIL_H)
        p["title"].configure(text=self._title_to_show(game))
        p["sub"].configure(text="v%s · %s" % (game["version"] or "?",
                                              game["developer"] or "未知作者"))
        p["title_note"].configure(text="")
        if game.get("origin") == "user":
            p["sub"].configure(text=p["sub"].cget("text") + "\n分类：" +
                               format_categories(game.get("categories", ())))
        is_user = game.get("origin") == "user"
        public_sources = self._external_game_sources(game) if not is_user else []
        if public_sources:
            p["url"].configure(command=lambda g=game, s=public_sources:
                                self._choose_game_source(g, s))
        folder = game.get("folder_path")
        if folder:
            p["open_folder"].configure(
                command=lambda f=folder: self._open_local_folder(f))
            p["launch"].configure(command=lambda g=game: self._launch_game(g))
            p["launch"].bind("<Button-3>",
                             lambda e, g=game: self._re_pick_exe(g))
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
        self._layout_detail(show_url=bool(public_sources),
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
            # 官网评论, not 评论: this is the count scraped off dikgames. It sat
            # in the same panel as the app's own 评论区, and the two read as the
            # same thing while counting different people's comments.
            parts.append("官网评论 %s" % f"{game['site_comments']:,}")
        self._detail_parts["heat"].configure(text=" · ".join(parts))

    def _build_detail_heat(self, d, parts):
        label = ctk.CTkLabel(d, text="", text_color=MUTED, font=ui_font(size=12))
        parts["heat"] = label
        return [("heat", label, {"anchor": "w", "padx": 18, "pady": (10, 0)})]

    def _detail_action(self, d, parts, key, text, command=None, accent=False,
                       pady=(0, 4), text_color=None, filled=False):
        """A filled detail-panel action button, registered under `key`.

        One widget restyled across the panel's action buttons; the constructor's
        seven kwargs are the thing that drifted between them.
        """
        btn = ctk.CTkButton(
            d, text=text, height=30, corner_radius=8,
            fg_color=ACCENT if filled else CHIP,
            text_color=(ON_ACCENT if filled
                        else text_color or (ACCENT if accent else TEXT)),
            hover_color=CARD_HOVER, command=command)
        parts[key] = btn
        return [(key, btn, {"fill": "x", "padx": 18, "pady": pady})]

    def _build_detail_back_profile(self, d, parts):
        return self._detail_action(d, parts, "back_profile", "返回个人",
                                   self.open_profile)

    def _build_detail_launch(self, d, parts):
        return self._detail_action(d, parts, "launch", "启动游戏",
                                   accent=True, filled=True)

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

    def _build_detail_edit(self, d, parts):
        return self._detail_action(d, parts, "edit_game", "编辑此游戏",
                                   self._edit_user_game)

    def _build_detail_delete(self, d, parts):
        return self._detail_action(d, parts, "delete_game", "删除此游戏",
                                   text_color=DANGER_TEXT)

    def _edit_tags_current(self):
        if not self._require_personal_access("编辑游戏标签"):
            return
        if self.selected is not None:
            self.open_edit_tags(self.selected)

    def _set_promote(self, game_id, on):
        if not self._require_personal_access("管理游戏条目"):
            return
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
        if not self._require_personal_access("删除自定义游戏"):
            return
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

    def _edit_user_game(self):
        if not self._require_personal_access("编辑自定义游戏"):
            return
        game = self.selected
        if game is not None and game.get("origin") == "user":
            self.open_add_game(game=game)

    def _open_local_folder(self, folder):
        if os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo("找不到目录", "本地目录已不存在：\n%s" % folder)

    def _launch_game(self, game):
        exe = game.get("exe_path")
        if not exe or not os.path.isfile(exe):
            exe = self._pick_game_exe(game["id"], game.get("folder_path"))
            if not exe:
                return
            game["exe_path"] = exe
        self._start_game_exe(exe)

    def _re_pick_exe(self, game):
        exe = self._pick_game_exe(game["id"], game.get("folder_path"))
        if exe:
            game["exe_path"] = exe
            self._start_game_exe(exe)

    def _pick_game_exe(self, game_id, folder):
        if not self._require_personal_access("设置本地游戏启动程序"):
            return None
        initial = folder if folder and os.path.isdir(folder) else os.path.expanduser("~")
        path = filedialog.askopenfilename(
            title="选择游戏启动程序（.exe）", parent=self, initialdir=initial,
            filetypes=[("程序", "*.exe"), ("所有文件", "*.*")])
        if not path:
            return None
        path = os.path.normpath(path)
        with slg_db.session() as conn:
            slg_db.set_local_exe(conn, game_id, path)
        return path

    def _start_game_exe(self, exe):
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except OSError as exc:
            messagebox.showerror("启动失败",
                                 "无法启动：\n%s\n%s" % (exe, exc), parent=self)

    def _build_detail_comments(self, d, parts):
        """Keep game details compact; the full discussion has its own view."""
        card = ctk.CTkFrame(d, fg_color=CARD, corner_radius=10)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(head, text="玩家评论", text_color=TEXT,
                     font=ui_font(size=13, weight="bold")).pack(side="left")
        button = ctk.CTkButton(
            head, text="写评论&查看评论区", width=168, height=30, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
            font=ui_font(size=12), command=self.open_comments)
        button.pack(side="right")
        hint = ctk.CTkLabel(card, text="", text_color=MUTED,
                            font=ui_font(size=11), anchor="w")
        hint.pack(fill="x", padx=12, pady=(0, 10))
        parts["comments_preview"] = hint
        parts["comments_button"] = button
        return [("comments_preview_card", card,
                 {"fill": "x", "padx": 18, "pady": (12, 4)})]

    def _fill_detail_comments(self, game):
        local_count = len(slg_db.list_comments(self.conn, game["slug"]))
        meta = getattr(self, "_comments_meta", {}).get(game["slug"], {})
        public_count = meta.get("total_count")
        summary = "本机记录 %d 条" % local_count
        if public_count is not None:
            summary += " · 云端公开评论 %d 条" % public_count
        else:
            summary += " · 点开可查看云端评论与分页"
        self._detail_parts["comments_preview"].configure(text=summary)

    def _refresh_comment_view(self, game):
        if (getattr(self, "_panel_mode", None) == "comments"
                and getattr(self, "_comment_view_game", {}).get("slug") == game["slug"]):
            if getattr(self, "_comment_active_tab", "public") == "mine":
                self._render_comment_response()
            self._load_comment_page(getattr(self, "_comment_page", 1))
        elif self.selected is not None and self.selected["slug"] == game["slug"]:
            self._fill_detail_comments(self.selected)

    def open_comments(self, game=None):
        """Open a dedicated, paginated comment view for the selected game."""
        game = game or self.selected
        if game is None:
            return
        self._panel_mode = "comments"
        self._set_shop_wide_layout(True)
        self._comment_view_game = dict(game)
        self._comment_page = 1
        self._comment_active_tab = "public"
        self._comment_response = None
        self._comment_mine = []
        self._comment_error = False
        self._comment_request_id = 0
        self._comment_sort = tk.StringVar(value="latest")
        self._comment_public = tk.BooleanVar(value=False)
        self._comment_scope = tk.StringVar(value="本机（仅自己可见）")
        self._comment_post_button = None
        self._comment_composer_scope_note = None
        self._destroy_detail()
        d = self.detail

        header = ctk.CTkFrame(d, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 8))
        ctk.CTkButton(header, text="‹ 返回游戏详情", width=132, height=30,
                      corner_radius=8, fg_color=CHIP, text_color=TEXT,
                      hover_color=CARD_HOVER, font=ui_font(size=12),
                      command=self._close_comments_view).pack(side="left")
        ctk.CTkLabel(header, text="游戏评论", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(side="left", padx=12)

        summary = ctk.CTkFrame(d, fg_color=CARD, corner_radius=10)
        summary.pack(fill="x", padx=16, pady=(0, 10))
        summary_cover = ctk.CTkLabel(summary, text="")
        summary_cover.pack(side="left", padx=12, pady=9)
        self._bind_cover(summary_cover, game, 64, 84)
        meta = ctk.CTkFrame(summary, fg_color="transparent")
        meta.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=8)
        ctk.CTkLabel(meta, text=self._title_to_show(game), text_color=TEXT,
                     font=ui_font(size=15, weight="bold"), anchor="w",
                     wraplength=650, justify="left").pack(fill="x", anchor="w")
        details = "v%s · %s" % (game.get("version") or "?",
                                game.get("developer") or "未知作者")
        if game.get("site_rating") is not None:
            details += " · 官网评分 %s" % game.get("site_rating")
        if game.get("site_comments") is not None:
            details += " · 官网评论 %s" % game.get("site_comments")
        ctk.CTkLabel(meta, text=details, text_color=MUTED,
                     font=ui_font(size=11), anchor="w",
                     wraplength=650, justify="left").pack(fill="x", pady=(5, 0))
        tags = slg_db.game_tags(self.conn, game["id"])
        if tags:
            ctk.CTkLabel(meta, text="标签：" + " · ".join(tags[:6]),
                         text_color=ACCENT, font=ui_font(size=10), anchor="w",
                         wraplength=650, justify="left").pack(fill="x", pady=(4, 0))
        overview = (game.get("overview") or "").strip()
        if overview:
            if len(overview) > 140:
                overview = overview[:140].rstrip() + "…"
            ctk.CTkLabel(meta, text=overview, text_color=MUTED,
                         font=ui_font(size=10), anchor="w",
                         wraplength=650, justify="left").pack(fill="x", pady=(4, 0))

        composer_card = ctk.CTkFrame(d, fg_color=CARD, corner_radius=10)
        composer_card.pack(fill="x", padx=16, pady=(0, 10))
        composer_head = ctk.CTkFrame(composer_card, fg_color="transparent")
        composer_head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(composer_head, text="写评论", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(side="left")
        self._comment_counter = ctk.CTkLabel(composer_head, text="0 / 500 字",
                                             text_color=MUTED,
                                             font=ui_font(size=10))
        self._comment_counter.pack(side="right")

        scope_row = ctk.CTkFrame(composer_card, fg_color="transparent")
        scope_row.pack(fill="x", padx=12, pady=(0, 5))
        scope = ctk.CTkSegmentedButton(
            scope_row, values=["本机（仅自己可见）", "公开发表（所有用户可见）"],
            variable=self._comment_scope, height=30, corner_radius=8,
            selected_color=ACCENT, selected_hover_color=ACCENT,
            unselected_color=CHIP, unselected_hover_color=CARD_HOVER,
            text_color=TEXT, font=ui_font(size=11),
            command=self._comment_scope_changed)
        scope.pack(side="left", fill="x", expand=True)
        self._comment_composer_scope_note = ctk.CTkLabel(
            composer_card, text="私人评论只保存在本机；公开发表会自动提交云端审核。",
            text_color=MUTED, font=ui_font(size=10), anchor="w",
            wraplength=820, justify="left")
        self._comment_composer_scope_note.pack(fill="x", padx=12, pady=(5, 6))

        self._comment_entry = ctk.CTkTextbox(
            composer_card, height=78, corner_radius=8, fg_color=BG, text_color=TEXT,
            border_color=CHIP, border_width=1, font=ui_font(size=12), wrap="word")
        self._comment_entry.bind("<Control-Return>", lambda e: self._post_comment())
        self._comment_entry.bind("<Escape>", lambda e: self._clear_composer())
        self._comment_entry.bind("<KeyRelease>", self._update_comment_counter)
        self._comment_entry.pack(fill="x", padx=12, pady=(0, 5))
        self._composer_slug = game["slug"]

        composer_actions = ctk.CTkFrame(composer_card, fg_color="transparent")
        composer_actions.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkLabel(composer_actions, text="Ctrl+Enter 发布 · Esc 清空",
                     text_color=MUTED, font=ui_font(size=10)).pack(side="left")
        self._comment_post_button = ctk.CTkButton(
            composer_actions, text="保存到本机", width=120, height=30,
            corner_radius=8, fg_color=ACCENT, text_color=ON_ACCENT,
            hover_color=CARD_HOVER, font=ui_font(size=12),
            command=self._post_comment)
        self._comment_post_button.pack(side="right")

        if not self._has_usable_cloud_session() and not self._is_admin_mode():
            account_row = ctk.CTkFrame(d, fg_color="transparent")
            account_row.pack(fill="x", padx=18, pady=(0, 8))
            ctk.CTkLabel(account_row, text="登录后才能发表评论；公开评论也会自动上传并进入审核。",
                         text_color=MUTED, font=ui_font(size=10)).pack(side="left")
            ctk.CTkButton(account_row, text="创建 / 登录", width=96, height=26,
                          corner_radius=7, fg_color=CHIP, text_color=ACCENT,
                          hover_color=CARD_HOVER, font=ui_font(size=11),
                          command=self.open_profile).pack(side="right")
            self._comment_entry.configure(state="disabled")
            scope.configure(state="disabled")
            self._comment_post_button.configure(text="登录后发表评论", state="disabled")
        self._comment_scope_changed(self._comment_scope.get())

        comments_card = ctk.CTkFrame(d, fg_color=CARD, corner_radius=10)
        comments_card.pack(fill="x", padx=16, pady=(0, 14))
        list_head = ctk.CTkFrame(comments_card, fg_color="transparent")
        list_head.pack(fill="x", padx=12, pady=(10, 6))
        ctk.CTkLabel(list_head, text="评论区", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(side="left")
        self._comment_total_label = ctk.CTkLabel(
            list_head, text="正在读取…", text_color=MUTED,
            font=ui_font(size=10))
        self._comment_total_label.pack(side="right")

        tabrow = ctk.CTkFrame(comments_card, fg_color="transparent")
        tabrow.pack(fill="x", padx=12, pady=(0, 6))
        self._comment_public_tab = ctk.CTkButton(
            tabrow, text="大家的评论", width=106, height=28, corner_radius=7,
            fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
            font=ui_font(size=11), command=lambda: self._switch_comment_tab("public"))
        self._comment_public_tab.pack(side="left", padx=(0, 6))
        self._comment_mine_tab = ctk.CTkButton(
            tabrow, text="我的记录", width=100, height=28, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=11), command=lambda: self._switch_comment_tab("mine"))
        self._comment_mine_tab.pack(side="left")
        sort_box = ctk.CTkFrame(tabrow, fg_color="transparent")
        sort_box.pack(side="right")
        self._comment_sort_label = ctk.CTkLabel(
            sort_box, text="排序", text_color=MUTED,
            font=ui_font(size=10))
        self._comment_sort_label.pack(side="left", padx=(0, 5))
        self._comment_sort_menu = ctk.CTkOptionMenu(
            sort_box, values=["最新", "热门"], width=84, height=26,
            variable=tk.StringVar(value="最新"),
            command=self._change_comment_sort)
        self._comment_sort_menu.pack(side="right")

        self._comment_scope_note = ctk.CTkLabel(
            comments_card, text="云端公开评论会显示在这里；按最新或热门排序。",
            text_color=MUTED, font=ui_font(size=10), anchor="w")
        self._comment_scope_note.pack(fill="x", padx=12, pady=(0, 5))
        self._active_comments_box = ctk.CTkFrame(
            comments_card, fg_color=BG, corner_radius=8)
        self._active_comments_box.pack(fill="x", padx=12, pady=(0, 4))
        pager = ctk.CTkFrame(comments_card, fg_color="transparent")
        pager.pack(fill="x", padx=12, pady=(4, 10))
        self._comment_prev = ctk.CTkButton(
            pager, text="上一页", width=72, height=26, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=11), command=lambda: self._load_comment_page(self._comment_page - 1))
        self._comment_prev.pack(side="left")
        self._comment_page_label = ctk.CTkLabel(pager, text="第 1 页",
                                                text_color=MUTED,
                                                font=ui_font(size=11))
        self._comment_page_label.pack(side="left", expand=True)
        self._comment_next = ctk.CTkButton(
            pager, text="下一页", width=72, height=26, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=11), command=lambda: self._load_comment_page(self._comment_page + 1))
        self._comment_next.pack(side="right")

        self._load_comment_page(1)

    def _close_comments_view(self):
        self._panel_mode = "game"
        self._set_shop_wide_layout(False)
        self._active_comments_box = None
        self._render_detail()

    def _switch_comment_tab(self, tab):
        self._comment_active_tab = tab
        self._comment_public_tab.configure(
            fg_color=ACCENT if tab == "public" else CHIP,
            text_color=ON_ACCENT if tab == "public" else TEXT)
        self._comment_mine_tab.configure(
            fg_color=ACCENT if tab == "mine" else CHIP,
            text_color=ON_ACCENT if tab == "mine" else TEXT)
        self._comment_sort_menu.configure(state="normal" if tab == "public" else "disabled")
        self._comment_sort_label.configure(
            text_color=MUTED if tab == "public" else PLACEHOLDER)
        self._comment_scope_note.configure(
            text=("公开评论审核通过后会显示在这里；可按最新或热门排序。"
                  if tab == "public" else
                  "这里汇总本机私人记录和自己的云端评论；本机记录不会同步到其他设备。"))
        self._render_comment_response()

    def _comment_scope_changed(self, value):
        public = str(value).startswith("公开发表")
        if public and not self._has_usable_cloud_session():
            messagebox.showinfo("需要云端账号", "公开发表需要先创建或登录云端账号。",
                                parent=self)
            self._comment_scope.set("本机（仅自己可见）")
            public = False
        self._comment_public.set(public)
        button = getattr(self, "_comment_post_button", None)
        if button is not None and button.winfo_exists():
            button.configure(text="公开发表" if public else "保存到本机")
        note = getattr(self, "_comment_composer_scope_note", None)
        if note is not None and note.winfo_exists():
            note.configure(text=(
                "公开发表会自动提交云端审核；公开评论需 20–500 字，审核通过后展示，有效评论奖励 20 积分。"
                if public else
                "本机评论只保存在这台电脑；最多 500 字，不会上传给其他用户。"))

    def _update_comment_counter(self, _event=None):
        entry = getattr(self, "_comment_entry", None)
        label = getattr(self, "_comment_counter", None)
        if entry is None or label is None:
            return
        count = len(entry.get("1.0", "end-1c").strip())
        label.configure(text="%d / 500 字" % count,
                        text_color=DANGER_TEXT if count > 500 else MUTED)

    def _change_comment_sort(self, value):
        self._comment_sort.set("popular" if value == "热门" else "latest")
        if getattr(self, "_panel_mode", None) == "comments":
            self._load_comment_page(1)

    def _render_comments(self, game, local, remote, loading=False,
                         cloud_error=False, own_cloud=None,
                         include_own_cloud=True):
        box = getattr(self, "_active_comments_box", None)
        if box is None or not box.winfo_exists():
            return
        for child in box.winfo_children():
            child.destroy()
        own_by_id = {str(c.get("id")): c for c in (own_cloud or [])}
        uploaded_ids = {str(c["cloud_id"]) for c in local if c["cloud_id"]}
        rows = []
        if not include_own_cloud:
            local_by_cloud = {str(c["cloud_id"]): c for c in local if c["cloud_id"]}
            for c in remote or []:
                oid = c.get("id")
                own = own_by_id.get(str(oid)) if oid else None
                local_row = local_by_cloud.get(str(oid)) if oid else None
                rows.append({"author": c.get("nickname") or "匿名",
                             "content": c.get("content") or "",
                             "time": c.get("ts") or "", "own": bool(own),
                             "local_id": local_row["id"] if local_row else None,
                             "cloud_id": oid, "visibility": "public",
                             "status": (own or {}).get("status", "approved"),
                             "votes_up": c.get("votes_up", c.get("likes", 0)),
                             "votes_down": c.get("votes_down", c.get("dislikes", 0)),
                             "author_cosmetic": c.get("author_cosmetic") or c.get("cosmetic_id"),
                             "author_appearances": c.get("author_appearances") or {},
                             "author_title_id": c.get("author_title_id") or c.get("title_id"),
                             "profile_message": c.get("profile_message") or ""})
        else:
            for c in local:
                rows.append({"author": c["nickname"] or "匿名", "content": c["content"],
                             "time": c["created_at"], "own": True,
                             "local_id": c["id"], "cloud_id": c["cloud_id"],
                             "visibility": c["visibility"],
                             "status": own_by_id.get(str(c["cloud_id"]), {}).get("status")})
            for c in own_cloud or []:
                if str(c.get("id")) in uploaded_ids:
                    continue
                rows.append({"author": c.get("nickname") or "我", "content": c.get("content") or "",
                             "time": c.get("ts") or "", "own": True,
                             "local_id": None, "cloud_id": c.get("id"),
                             "visibility": "public", "status": c.get("status"),
                             "author_cosmetic": c.get("author_cosmetic") or c.get("cosmetic_id"),
                             "author_appearances": c.get("author_appearances") or {},
                             "author_title_id": c.get("author_title_id") or c.get("title_id"),
                             "profile_message": c.get("profile_message") or ""})
        for r in rows:
            self._comment_row(box, r)
        if cloud_error and not rows:
            state = ctk.CTkFrame(box, fg_color=CARD, corner_radius=8)
            ctk.CTkLabel(
                state, text="评论暂时没有加载出来。检查网络后可以重试。",
                text_color=MUTED, font=ui_font(size=11), wraplength=540,
                justify="left", anchor="w").pack(side="left", padx=10, pady=9)
            ctk.CTkButton(
                state, text="重试", width=64, height=26, corner_radius=6,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=10), command=self._retry_comments_fetch
            ).pack(side="right", padx=8, pady=6)
            state.pack(fill="x", padx=7, pady=7)
        elif loading and not rows:
            ctk.CTkLabel(box, text="正在加载评论…", text_color=MUTED,
                         font=ui_font(size=11)).pack(anchor="w", padx=10, pady=12)
        elif not rows:
            empty = ctk.CTkFrame(box, fg_color=CARD, corner_radius=8)
            ctk.CTkLabel(
                empty,
                text=("这款游戏还没有公开评论，来写第一条吧。"
                      if not include_own_cloud else "还没有评论记录。"),
                text_color=MUTED, font=ui_font(size=11), anchor="w"
            ).pack(fill="x", padx=10, pady=12)
            empty.pack(fill="x", padx=7, pady=7)
        elif loading:
            ctk.CTkLabel(box, text="正在更新评论…", text_color=MUTED,
                         font=ui_font(size=10)).pack(anchor="w", padx=10, pady=(2, 6))
        elif cloud_error:
            state = ctk.CTkFrame(box, fg_color=CARD, corner_radius=8)
            ctk.CTkLabel(
                state, text="云端暂时无法更新，当前保留已加载的评论。",
                text_color=MUTED, font=ui_font(size=10), wraplength=480,
                justify="left", anchor="w").pack(side="left", padx=10, pady=7)
            ctk.CTkButton(
                state, text="重试", width=64, height=24, corner_radius=6,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=10), command=self._retry_comments_fetch
            ).pack(side="right", padx=8, pady=5)
            state.pack(fill="x", padx=7, pady=(2, 6))

    def _retry_comments_fetch(self):
        if getattr(self, "_panel_mode", None) == "comments":
            self._load_comment_page(self._comment_page)

    def _load_comment_page(self, page=None):
        game = getattr(self, "_comment_view_game", None)
        if game is None or getattr(self, "_panel_mode", None) != "comments":
            return
        if page is not None:
            self._comment_page = max(1, int(page))
        self._comment_error = False
        self._comment_response = None
        self._render_comment_response(loading=True)
        self._comment_request_id += 1
        request_id = self._comment_request_id
        threading.Thread(target=self._fetch_comments_worker,
                         args=(game["slug"], self._comment_page,
                               self._comment_sort.get(), request_id),
                         daemon=True).start()

    def _switch_comment_sort(self, value):
        self._change_comment_sort(value)

    def _render_comment_response(self, loading=False):
        game = getattr(self, "_comment_view_game", None)
        box = getattr(self, "_active_comments_box", None)
        if game is None or box is None or not box.winfo_exists():
            return
        if self._comment_active_tab == "public":
            response = getattr(self, "_comment_response", None) or {}
            comments = response.get("comments") or []
            self._render_comments(
                game, slg_db.list_comments(self.conn, game["slug"]), comments,
                loading=loading, cloud_error=self._comment_error,
                own_cloud=getattr(self, "_comment_mine", []),
                include_own_cloud=False)
            total = int(response.get("total_count") or 0)
            pages = int(response.get("total_pages") or (1 if total else 0))
            if loading and not response:
                self._comment_total_label.configure(text="正在读取公开评论…")
                self._comment_page_label.configure(text="等待加载")
            elif self._comment_error and not response:
                self._comment_total_label.configure(text="公开评论加载失败")
                self._comment_page_label.configure(text="未加载")
            else:
                self._comment_total_label.configure(text="%d 条公开评论" % total)
                self._comment_page_label.configure(
                    text=("第 %d / %d 页" % (self._comment_page, max(1, pages))))
            self._comment_prev.configure(
                state="normal" if not loading and self._comment_page > 1 else "disabled")
            self._comment_next.configure(
                state=("normal" if not loading and self._comment_page < pages
                       else "disabled"))
        else:
            local = slg_db.list_comments(self.conn, game["slug"])
            mine = getattr(self, "_comment_mine", [])
            self._render_comments(game, local, [], loading=loading,
                                  cloud_error=self._comment_error, own_cloud=mine)
            local_cloud_ids = {str(c["cloud_id"]) for c in local if c["cloud_id"]}
            extra_cloud = sum(1 for c in mine
                              if str(c.get("id")) not in local_cloud_ids)
            self._comment_total_label.configure(
                text="%d 条个人记录" % (len(local) + extra_cloud))
            self._comment_page_label.configure(text="个人记录不分页")
            self._comment_prev.configure(state="disabled")
            self._comment_next.configure(state="disabled")

    def _comment_row(self, box, r):
        frame = ctk.CTkFrame(box, fg_color=CARD, corner_radius=8)
        top = ctk.CTkFrame(frame, fg_color="transparent")
        identity = ctk.CTkFrame(top, fg_color="transparent")
        identity.pack(side="left", fill="x", expand=True)
        appearances = r.get("author_appearances")
        appearances = dict(appearances) if isinstance(appearances, dict) else {}
        legacy_cosmetic = (r.get("author_cosmetic") or r.get("cosmetic_id")
                           or r.get("decoration_id"))
        if legacy_cosmetic and not appearances.get("comment_frame"):
            appearances["comment_frame"] = legacy_cosmetic
        avatar_frame = appearances.get("avatar_frame", "")
        comment_frame = appearances.get("comment_frame", "")
        public_cloud = (r.get("visibility") == "public" and bool(r.get("cloud_id"))
                        and r.get("status", "approved") in ("approved", "public"))
        author_details = {
            "nickname": r.get("author", "匿名"),
            "profile_message": r.get("profile_message", "") if public_cloud else "",
            "title_id": r.get("author_title_id") or r.get("title_id") or "",
            "appearances": appearances,
        }
        identity_line = ctk.CTkFrame(identity, fg_color="transparent")
        identity_line.pack(fill="x")
        avatar_widget = None
        if avatar_frame:
            avatar_widget = self._avatar_frame_canvas(
                identity_line, avatar_frame, r.get("author", ""),
                size=40, actual_avatar=False)
            avatar_widget.pack(side="left", padx=(0, 5))
        name_widget = self._comment_nameplate(
            identity_line, r.get("author", "匿名"), comment_frame)
        title_id = author_details["title_id"]
        title_widget = None
        title = slg_titles.title_by_id(title_id) if title_id else None
        if title and title_id != slg_titles.DEFAULT_TITLE_ID:
            title_widget = ctk.CTkLabel(
                identity_line, text=" · " + title["name"],
                text_color=self._title_color(title_id),
                font=ui_font(size=10, weight="bold"))
            title_widget.pack(side="left")
        message_widget = None
        if public_cloud and author_details["profile_message"]:
            message_widget = ctk.CTkLabel(
                identity, text=author_details["profile_message"][:20],
                text_color=MUTED, font=ui_font(size=10),
                justify="left", anchor="w")
            message_widget.pack(anchor="w", padx=(5 if avatar_frame else 0, 0),
                               pady=(1, 0))
        t = (r["time"] or "").replace("T", " ")[:16]
        if t:
            ctk.CTkLabel(top, text=t, text_color=MUTED,
                         font=ui_font(size=10)).pack(side="right")
        top.pack(fill="x", padx=10, pady=(8, 3))
        if public_cloud:
            for widget in (identity, identity_line, avatar_widget, name_widget,
                           title_widget, message_widget):
                if widget is not None:
                    self._bind_public_author_info(widget, author_details)

        width = max(220, min(900, self.detail.winfo_width() - 90))
        ctk.CTkLabel(frame, text=r["content"], text_color=TEXT,
                     font=ui_font(size=12), wraplength=width, justify="left",
                     anchor="w").pack(fill="x", padx=10, pady=(0, 7))
        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=(0, 6))
        if r["own"]:
            status_labels = {"public": "已公开", "approved": "已公开",
                             "pending": "待审核", "rejected": "未通过审核",
                             "hidden": "已隐藏"}
            tag = (status_labels.get(r.get("status"), "已上传·待同步状态") if r["cloud_id"] else
                   "待公开" if r.get("visibility") == "public" else "仅自己可见")
            ctk.CTkLabel(actions, text=tag, text_color=MUTED,
                         font=ui_font(size=10)).pack(side="left")
            if r.get("visibility") == "public" and not r["cloud_id"]:
                ctk.CTkButton(
                    actions, text="重试公开", width=66, height=20, corner_radius=6,
                    fg_color="transparent", text_color=ACCENT,
                    hover_color=CHIP, font=ui_font(size=10),
                    command=lambda row=dict(r): self._retry_public_comment(row)
                ).pack(side="right", padx=(3, 0))
            if r["local_id"] is not None or r.get("cloud_id"):
                ctk.CTkButton(actions, text="删除", width=44, height=20, corner_radius=6,
                              fg_color="transparent", text_color=MUTED,
                              hover_color=CHIP, font=ui_font(size=10),
                              command=lambda lid=r["local_id"], cid=r["cloud_id"]:
                                  self._delete_own_comment(lid, cid)).pack(side="right", padx=(3, 0))
        else:
            ctk.CTkLabel(actions, text="云端公开评论", text_color=MUTED,
                         font=ui_font(size=10)).pack(side="left")
            ctk.CTkButton(actions, text="举报", width=48, height=20, corner_radius=6,
                          fg_color="transparent", text_color=MUTED,
                          hover_color=CHIP, font=ui_font(size=10),
                          command=lambda cid=r["cloud_id"]:
                              self._report_comment(cid)).pack(side="right", padx=(3, 0))
            for value, label in ((0, "撤销"), (1, "赞"), (-1, "踩")):
                count = r.get("votes_up" if value == 1 else "votes_down", 0)
                button_text = "%s %s" % (label, count) if value else label
                ctk.CTkButton(actions, text=button_text, width=48,
                              height=20, corner_radius=6, fg_color="transparent",
                              text_color=MUTED, hover_color=CHIP, font=ui_font(size=10),
                              command=lambda cid=r["cloud_id"], v=value:
                                  self._vote_comment(cid, v)).pack(side="right", padx=(3, 0))
        frame.pack(fill="x", padx=7, pady=(7, 0))

    def _bind_public_author_info(self, widget, details):
        try:
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>",
                        lambda _event, data=dict(details):
                            self._show_public_author_card(data), add="+")
        except (tk.TclError, AttributeError):
            return

    def _show_public_author_card(self, details):
        """Show public-facing profile fields only; never expose account IDs or keys."""
        nickname = str(details.get("nickname") or "匿名用户")[:40]
        appearances = details.get("appearances")
        appearances = dict(appearances) if isinstance(appearances, dict) else {}
        avatar_frame = appearances.get("avatar_frame") or ""
        comment_frame = appearances.get("comment_frame") or ""
        message = str(details.get("profile_message") or "")[:20].strip()
        title_id = details.get("title_id")
        title = slg_titles.title_by_id(title_id) if title_id else None
        has_appearance = bool(avatar_frame or comment_frame)

        # Grow the dialog only for fields that are present. The footer is a
        # separate packed region, so its close action stays visible when text
        # wraps at larger Windows display scaling.
        height = 350 + (68 if message else 0) + (96 if has_appearance else 0)
        win = self._new_dialog("公开名片", "460x%d" % min(height, 510))
        win.minsize(400, 340)

        body = ctk.CTkFrame(win, fg_color=CARD, corner_radius=12)
        body.pack(side="top", fill="both", expand=True, padx=14, pady=(14, 0))
        footer = ctk.CTkFrame(win, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=14, pady=14)

        identity = ctk.CTkFrame(body, fg_color=BG, corner_radius=10)
        identity.pack(fill="x", padx=12, pady=12)
        initial = next((char for char in nickname if not char.isspace()), "?")
        avatar = ctk.CTkLabel(
            identity, text=initial, width=62, height=62,
            corner_radius=31, fg_color=CHIP, text_color=TEXT,
            font=ui_font(size=22, weight="bold"))
        avatar.pack(side="left", padx=(12, 10), pady=12)

        identity_text = ctk.CTkFrame(identity, fg_color="transparent")
        identity_text.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=10)
        ctk.CTkLabel(
            identity_text, text=nickname, text_color=TEXT,
            font=ui_font(size=15, weight="bold"), wraplength=315,
            justify="left", anchor="w").pack(fill="x", anchor="w")
        if title:
            self._title_badge(identity_text, title_id, max_width=240).pack(
                anchor="w", pady=(7, 0))
            ctk.CTkLabel(
                identity_text, text="头衔 · %s" % (title.get("rarity") or "普通"),
                text_color=MUTED, font=ui_font(size=10), anchor="w").pack(
                    anchor="w", pady=(3, 0))

        if message:
            message_card = ctk.CTkFrame(
                body, fg_color=BG, corner_radius=9, border_width=1,
                border_color=CHIP)
            message_card.pack(fill="x", padx=12, pady=(0, 8))
            ctk.CTkLabel(
                message_card, text="公开寄语", text_color=MUTED,
                font=ui_font(size=10, weight="bold")).pack(
                    anchor="w", padx=10, pady=(8, 2))
            ctk.CTkLabel(
                message_card, text=message, text_color=TEXT,
                font=ui_font(size=12), wraplength=390,
                justify="left", anchor="w").pack(
                    fill="x", padx=10, pady=(0, 9))

        if has_appearance:
            outfits = ctk.CTkFrame(body, fg_color=BG, corner_radius=9)
            outfits.pack(fill="x", padx=12, pady=(0, 12))
            ctk.CTkLabel(
                outfits, text="装扮预览", text_color=MUTED,
                font=ui_font(size=10, weight="bold")).pack(
                    anchor="w", padx=10, pady=(8, 3))
            preview_row = ctk.CTkFrame(outfits, fg_color="transparent")
            preview_row.pack(fill="x", padx=10, pady=(0, 9))
            if avatar_frame:
                avatar_item = self._appearance_item(avatar_frame)
                preview = ctk.CTkFrame(preview_row, fg_color="transparent")
                preview.pack(fill="x", pady=(0, 5))
                self._avatar_frame_canvas(
                    preview, avatar_frame, nickname, size=48,
                    actual_avatar=False).pack(side="left", padx=(0, 7))
                ctk.CTkLabel(
                    preview,
                    text="头像框 · %s · %s" % (
                        avatar_item.get("name", "已装备"),
                        avatar_item.get("rarity", "普通")),
                    text_color=self._appearance_color(avatar_frame),
                    font=ui_font(size=10), wraplength=300,
                    justify="left", anchor="w").pack(
                        side="left", fill="x", expand=True)
            if comment_frame:
                comment_item = self._appearance_item(comment_frame)
                preview = ctk.CTkFrame(preview_row, fg_color="transparent")
                preview.pack(fill="x")
                # A short sample shows the frame treatment without repeating
                # the user's nickname from the identity section.
                self._comment_nameplate(preview, "名片预览", comment_frame)
                ctk.CTkLabel(
                    preview,
                    text="名片框 · %s · %s" % (
                        comment_item.get("name", "已装备"),
                        comment_item.get("rarity", "普通")),
                    text_color=self._appearance_color(comment_frame),
                    font=ui_font(size=10), wraplength=260,
                    justify="left", anchor="w").pack(
                        side="left", fill="x", expand=True, padx=(7, 0))

        ctk.CTkButton(
            footer, text="关闭", height=34, fg_color=CHIP,
            text_color=TEXT, hover_color=CARD_HOVER,
            command=win.destroy).pack(fill="x")

    @staticmethod
    def _appearance_item(item_id):
        """Return the built-in appearance definition for a stored item ID."""
        for item in slg_titles.SHOP_ITEMS:
            if item.get("id") == item_id and item.get("kind") == "decoration":
                return item
        if item_id == "neon_comment_frame":
            return {"id": item_id, "appearance": "comment_frame",
                    "rarity": "稀有", "effect_style": "neon"}
        # The catalogue remains the price/ownership source. These local style
        # descriptors let an older client still render a newly equipped ID.
        fallbacks = {
            "comment_frame_black_gold_member":
                ("comment_frame", "black_gold", "传说"),
            "comment_frame_sakura": ("comment_frame", "sakura", "稀有"),
            "comment_frame_mint": ("comment_frame", "mint", "普通"),
            "comment_frame_deep_red_velvet":
                ("comment_frame", "deep_red_velvet", "史诗"),
            "avatar_frame_gilded_laurel":
                ("avatar_frame", "gilded_laurel", "传说"),
            "avatar_frame_glitch": ("avatar_frame", "glitch", "史诗"),
            "avatar_frame_startrail": ("avatar_frame", "startrail", "稀有"),
            "avatar_frame_pixel_8bit":
                ("avatar_frame", "pixel_8bit", "稀有"),
            "avatar_frame_blood_moon":
                ("avatar_frame", "blood_moon", "传说"),
        }
        fallback = fallbacks.get(item_id)
        if fallback:
            appearance, style, rarity = fallback
            return {"id": item_id, "appearance": appearance,
                    "effect_style": style, "rarity": rarity}
        return {}

    @classmethod
    def _appearance_rarity(cls, item_id):
        item = cls._appearance_item(item_id)
        return item.get("rarity") or "普通"

    @classmethod
    def _appearance_color(cls, item_id):
        rarity = cls._appearance_rarity(item_id)
        return slg_titles.RARITY_COLORS.get(rarity, MUTED)

    def _avatar_frame_canvas(self, parent, item_id, nickname="", size=68,
                             actual_avatar=False):
        """Draw a fixed built-in avatar frame; user pictures never leave this PC."""
        if not item_id:
            return None
        item = self._appearance_item(item_id)
        rarity = item.get("rarity") or "普通"
        style = item.get("effect_style") or ""
        bg = CARD
        canvas = tk.Canvas(parent, width=size, height=size, bg=bg,
                           highlightthickness=0, bd=0)
        cx = cy = size / 2
        rank = {name: index for index, name in enumerate(slg_titles.RARITY_ORDER)}
        tier = rank.get(rarity, 0)
        accent = self._appearance_color(item_id)

        # The user image stays local. Other people's comment cards get a
        # neutral initial, while the same server-side item ID draws the frame.
        diameter = max(16, int(size * 0.57))
        avatar = None
        if actual_avatar:
            # ``avatar_source(own=True)`` deliberately falls back to the
            # bundled author portrait when this user has no custom picture.
            # On a user's profile that would show the author's face under the
            # user's frame, so only load the local profile image here; the
            # initial below is the correct fallback.
            path = slg_db.avatar_path()
            if os.path.isfile(path):
                try:
                    with Image.open(path) as source:
                        avatar = ImageTk.PhotoImage(
                            circle_avatar(source.convert("RGBA"), diameter))
                except (OSError, ValueError, tk.TclError):
                    avatar = None
        if avatar is not None:
            canvas.create_image(cx, cy, image=avatar)
            canvas._appearance_photo = avatar
        else:
            r = diameter / 2
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                               fill=_mix(BG, accent, 0.12), outline="")
            canvas.create_text(cx, cy, text=(nickname[:1] or "?").upper(),
                               fill=TEXT, font=ui_tkfont(
                                   size=max(9, int(diameter * 0.43)), weight="bold"))

        # Five rarity tiers affect the number and weight of visible metal rims.
        # Most pixels remain static; only the preview gets a rare, short pulse.
        base = {"普通": "#777480", "稀有": "#52b9e8", "史诗": "#b77afa",
                "传说": "#efb963", "至臻": "#f27daf"}.get(rarity, accent)
        # Canvas coordinates below use a radius around (cx, cy).  Subtracting
        # this from cx put the decorative rim *inside* the avatar, where a
        # user's picture obscured it.
        outer = diameter * 0.62
        rim_width = 1 if tier <= 1 else 2 if tier <= 3 else 3
        outer_ring = canvas.create_oval(
            cx - outer, cy - outer, cx + outer, cy + outer,
            outline=_mix(base, "#ffffff", 0.10 if tier < 3 else 0.32),
            width=rim_width)
        inner = diameter * 0.53
        canvas.create_oval(cx - inner, cy - inner, cx + inner, cy + inner,
                           outline=_mix(base, BG, 0.28), width=1)

        if style in ("cyber_neon", "neon") or item_id == "neon_comment_frame":
            cyan, magenta = "#49edf3", "#ff4fb8"
            canvas.create_arc(cx - outer, cy - outer, cx + outer, cy + outer,
                              start=18, extent=118, style="arc", outline=cyan,
                              width=2 if tier < 4 else 3)
            canvas.create_arc(cx - outer, cy - outer, cx + outer, cy + outer,
                              start=202, extent=104, style="arc", outline=magenta,
                              width=2)
            for angle in (38, 132, 218, 310):
                rad = math.radians(angle)
                x, y = cx + outer * math.cos(rad), cy + outer * math.sin(rad)
                canvas.create_oval(x - 2.2, y - 2.2, x + 2.2, y + 2.2,
                                   fill=cyan if angle % 2 else magenta, outline="")
            # A fine scan line gives the product its electronic identity.
            scan_y = cy - diameter * 0.12
            canvas.create_line(cx - diameter * 0.28, scan_y,
                               cx + diameter * 0.28, scan_y,
                               fill=_mix(cyan, BG, 0.35), width=1)
        elif style == "dark_rose":
            # Six dark petals form a small rose motif around the avatar; the
            # wine-red highlight reads clearly without a bright neon halo.
            petal_r = diameter * 0.075
            for angle in range(0, 360, 60):
                rad = math.radians(angle)
                px = cx + (outer - petal_r * 0.25) * math.cos(rad)
                py = cy + (outer - petal_r * 0.25) * math.sin(rad)
                canvas.create_oval(px - petal_r, py - petal_r,
                                   px + petal_r, py + petal_r,
                                   fill="#35121f", outline="#9b294c", width=1)
                canvas.create_oval(px - petal_r * 0.35, py - petal_r * 0.6,
                                   px + petal_r * 0.25, py - petal_r * 0.05,
                                   fill="#d64b70", outline="")
            canvas.create_arc(cx - outer, cy - outer, cx + outer, cy + outer,
                              start=18, extent=100, style="arc",
                              outline="#d34c70", width=2)
            canvas.create_arc(cx - outer, cy - outer, cx + outer, cy + outer,
                              start=198, extent=110, style="arc",
                              outline="#631b35", width=3)
        elif style == "gilded_laurel":
            gold, leaf, shade = "#f4d37c", "#77c995", "#286d55"
            canvas.create_oval(cx - outer, cy - outer, cx + outer, cy + outer,
                               outline=gold, width=3)
            for side in (-1, 1):
                for angle in (115, 138, 161, 184, 207, 230, 253):
                    rad = math.radians(angle if side < 0 else 360 - angle)
                    px = cx + (outer * 0.87) * math.cos(rad)
                    py = cy + (outer * 0.87) * math.sin(rad)
                    tangent = rad + math.pi / 2
                    dx, dy = math.cos(tangent), math.sin(tangent)
                    nx, ny = math.cos(rad), math.sin(rad)
                    length, width = size * 0.095, size * 0.045
                    points = [px - dx * length / 2, py - dy * length / 2,
                              px + nx * width, py + ny * width,
                              px + dx * length / 2, py + dy * length / 2,
                              px - nx * width, py - ny * width]
                    canvas.create_polygon(*points, fill=leaf, outline=shade,
                                           width=1)
            canvas.create_polygon(cx, cy - outer - 2,
                                  cx - size * 0.09, cy - outer * 0.68,
                                  cx + size * 0.09, cy - outer * 0.68,
                                  fill=gold, outline="#fff0bb", width=1)
        elif style == "glitch":
            # Three offset RGB arcs create a visible chromatic aberration ring.
            red, green, blue = "#ff315d", "#39f5c5", "#438cff"
            for offset, color, start in ((-1.7, red, 8), (0, green, 128),
                                         (1.7, blue, 248)):
                radius = outer + offset
                canvas.create_arc(cx - radius, cy - radius,
                                  cx + radius, cy + radius,
                                  start=start, extent=104, style="arc",
                                  outline=color, width=2)
            for angle in (42, 88, 222, 294):
                rad = math.radians(angle)
                px, py = cx + outer * math.cos(rad), cy + outer * math.sin(rad)
                canvas.create_line(px - 3, py, px + 3, py + 1,
                                   fill=red, width=1)
                canvas.create_line(px, py - 3, px - 1, py + 3,
                                   fill=blue, width=1)
        elif style == "startrail":
            indigo, cyan, gold = "#5966d8", "#8ce9ff", "#fff0a1"
            canvas.create_arc(cx - outer, cy - outer * 0.74,
                              cx + outer, cy + outer * 0.74,
                              start=198, extent=145, style="arc",
                              outline=indigo, width=2)
            canvas.create_arc(cx - outer * 0.92, cy - outer,
                              cx + outer * 0.92, cy + outer,
                              start=18, extent=126, style="arc",
                              outline=cyan, width=1)
            for angle, color in ((20, gold), (98, cyan), (196, gold), (286, cyan)):
                rad = math.radians(angle)
                px, py = cx + outer * math.cos(rad), cy + outer * math.sin(rad)
                r = max(1.4, size * 0.035)
                canvas.create_polygon(px, py - r * 1.8, px + r * 0.55, py - r * 0.5,
                                      px + r * 1.8, py, px + r * 0.5, py + r * 0.5,
                                      px, py + r * 1.8, px - r * 0.5, py + r * 0.5,
                                      px - r * 1.8, py, px - r * 0.5, py - r * 0.5,
                                      fill=color, outline="")
        elif style == "pixel_8bit":
            # Chunky square tiles deliberately contrast with the circular avatar.
            cell = max(3, int(size * 0.085))
            tile_colors = ("#4ce9f5", "#a8fbff", "#ff59b8", "#45a8ff")
            perimeter = [(-3, y) for y in range(-3, 4)]
            perimeter += [(3, y) for y in range(-3, 4)]
            perimeter += [(x, -3) for x in range(-2, 3)]
            perimeter += [(x, 3) for x in range(-2, 3)]
            for index, (gx, gy) in enumerate(perimeter):
                x0, y0 = cx + gx * cell - cell / 2, cy + gy * cell - cell / 2
                canvas.create_rectangle(
                    x0, y0, x0 + cell - 1, y0 + cell - 1,
                    fill=tile_colors[index % len(tile_colors)],
                    outline="#161528", width=1)
        elif style == "blood_moon":
            crimson, moon, shadow = "#b9294c", "#ed5769", "#420d22"
            canvas.create_oval(cx - outer, cy - outer, cx + outer, cy + outer,
                               outline=crimson, width=3)
            moon_r = size * 0.075
            canvas.create_oval(cx - moon_r, cy - outer - moon_r * 0.35,
                               cx + moon_r, cy - outer + moon_r * 1.65,
                               fill=moon, outline="#ffc0b2", width=1)
            canvas.create_oval(cx - moon_r * 0.15, cy - outer - moon_r * 0.4,
                               cx + moon_r * 1.25, cy - outer + moon_r * 1.25,
                               fill=shadow, outline="")
            canvas.create_arc(cx - outer * 0.9, cy - outer * 0.9,
                              cx + outer * 0.9, cy + outer * 0.9,
                              start=207, extent=126, style="arc",
                              outline="#ff8a84", width=1)
            for angle in (40, 148, 267):
                rad = math.radians(angle)
                px, py = cx + outer * math.cos(rad), cy + outer * math.sin(rad)
                canvas.create_oval(px - 1.5, py - 1.5, px + 1.5, py + 1.5,
                                   fill="#ffaea0", outline="")

        if actual_avatar and tier >= 2:
            pulse_item = outer_ring
            base_color = canvas.itemcget(pulse_item, "outline")

            def draw_frame(index):
                p = 1 - abs(2 * index / 10.0 - 1)
                color = _mix(base_color, "#ffffff", 0.12 + p * 0.38)
                canvas.itemconfigure(pulse_item, outline=color,
                                     width=rim_width + (1 if p > 0.55 else 0))

            def reset():
                canvas.itemconfigure(pulse_item, outline=base_color, width=rim_width)

            self._sparse_widget_animation(
                canvas, frame_count=11, frame_ms=100,
                rest_ms=7200 if tier >= 4 else 9200,
                draw_frame=draw_frame, reset=reset, start_ms=900)
        return canvas

    def _comment_nameplate(self, parent, nickname, item_id):
        if not item_id:
            label = ctk.CTkLabel(parent, text=nickname, text_color=TEXT,
                                 font=ui_font(size=12, weight="bold"))
            label.pack(side="left")
            return label
        item = self._appearance_item(item_id)
        rarity = item.get("rarity") or "普通"
        style = item.get("effect_style") or ""
        palettes = {
            "cyber_neon": ("#111a24", "#4de8ef", "#9ffaff", "⌁"),
            "neon": ("#111a24", "#4de8ef", "#9ffaff", "⌁"),
            "dark_rose": ("#1c1119", "#9b294c", "#f4d7df", "✿"),
            "black_gold": ("#100d08", "#d5ac54", "#ffe6a0", "◆"),
            "sakura": ("#321827", "#f385ba", "#ffd4e8", "✿"),
            "mint": ("#14332b", "#55d6a6", "#caffea", "❧"),
            "deep_red_velvet": ("#310d19", "#c44661", "#ffd5dd", "❖"),
        }
        tint, edge, name_color, ornament = palettes.get(
            style, (CARD, self._appearance_color(item_id), TEXT, ""))
        width = 2 if rarity in ("史诗", "传说", "至臻") else 1
        badge = ctk.CTkFrame(parent, fg_color=tint, corner_radius=6,
                             border_width=width, border_color=edge)
        if ornament:
            ctk.CTkLabel(
                badge, text=ornament, text_color=edge,
                font=ui_font(size=9, weight="bold")).pack(
                    side="left", padx=(7, 2), pady=2)
        ctk.CTkLabel(badge, text=nickname, text_color=name_color,
                     font=ui_font(size=12, weight="bold")).pack(
                         side="left", padx=(2, 2) if ornament else 7, pady=2)
        if ornament:
            ctk.CTkLabel(
                badge, text=ornament, text_color=edge,
                font=ui_font(size=9, weight="bold")).pack(
                    side="left", padx=(2, 7), pady=2)
        badge.pack(side="left", padx=(0, 4))
        return badge

    def _fetch_comments_worker(self, slug, page=1, sort="latest", request_id=None):
        remote = slg_comments.fetch_comments_page(slug, page=page, limit=20,
                                                 sort=sort)
        try:
            mine = slg_comments.fetch_my_comments(slug) if slg_account.session() else []
        except slg_account.AccountError:
            mine = []
        self.queue.put(("comments", (slug, page, sort, request_id, remote, mine)))

    def _comments_result(self, slug, page, sort, request_id, remote, mine=None):
        game = getattr(self, "_comment_view_game", None)
        if (getattr(self, "_panel_mode", None) != "comments" or game is None
                or game["slug"] != slug or page != self._comment_page
                or sort != self._comment_sort.get()
                or request_id != self._comment_request_id):
            return
        self._comment_error = remote is None
        self._comment_mine = mine or []
        self._comment_response = remote or {"comments": [], "page": page,
                                            "total_count": 0, "total_pages": 0}
        if remote is not None:
            self._comments_meta = getattr(self, "_comments_meta", {})
            self._comments_meta[slug] = remote
        self._render_comment_response()

    def _delete_own_comment(self, local_id, cloud_id):
        if cloud_id and not self._require_regular_user_mode("\u4e91\u7aef\u8bc4\u8bba\u7ba1\u7406"):
            return
        if not self._require_personal_access("删除评论", cloud_only=bool(cloud_id)):
            return
        if cloud_id:
            game = self.selected
            slug = game["slug"] if game is not None else ""
            try:
                session_snapshot = slg_account.session()
            except slg_account.AccountError as exc:
                self._set_progress(str(exc))
                return
            self._set_progress("正在删除云端评论…")

            def worker():
                with slg_account.use_session_snapshot(session_snapshot):
                    success = slg_comments.delete_comment(cloud_id)
                if success and local_id is not None:
                    with slg_db.session() as conn:
                        slg_db.delete_comment(conn, local_id)
                self.queue.put(("comment_delete", (slug, local_id, success)))

            threading.Thread(target=worker, daemon=True).start()
            return
        slg_db.delete_comment(self.conn, local_id)
        if self.selected is not None:
            self._refresh_comment_view(self.selected)

    def _comment_delete_result(self, slug, local_id, success):
        if success:
            self._set_progress("云端评论和本机记录都已删除")
        else:
            self._set_progress("云端删除失败；评论仍保留在本机，可稍后重试")
        game = self.selected
        if game is not None and game["slug"] == slug:
            self._refresh_comment_view(game)

    def _clear_composer(self):
        entry = getattr(self, "_comment_entry", None)
        if entry is None:
            entry = (self._detail_parts or {}).get("comments_entry")
        if entry is not None and entry.winfo_exists():
            entry.delete("1.0", "end")

    def _post_comment(self):
        """Read the composer, store it, and start the upload if it is public.

        The nickname is read from the profile rather than asked for here: the
        old 写评论 dialog made the user retype it on every comment, and every
        comment on this machine belongs to the same person anyway.
        """
        entry = getattr(self, "_comment_entry", None)
        if entry is None:
            entry = self._detail_parts["comments_entry"]
        content = entry.get("1.0", "end-1c").strip()
        game = (getattr(self, "_comment_view_game", None)
                if getattr(self, "_panel_mode", None) == "comments" else self.selected)
        if not content or game is None:
            return
        public = bool(getattr(self, "_comment_public", None)
                      and self._comment_public.get())
        if public and not self._require_regular_user_mode("\u53d1\u5e03\u516c\u5f00\u8bc4\u8bba"):
            return
        if not self._require_personal_access("发表评论"):
            return
        if len(content) > 500:
            self._set_progress("评论最多 500 字，请缩短后再发布")
            return
        nickname = (getattr(self, "_cloud_me", {}).get("nickname", "")
                    if self._has_cloud_account() else
                    slg_db.get_pref(self.conn, "profile.nickname", ""))
        nickname = (nickname or "").strip() or None
        public = bool(getattr(self, "_comment_public", None)
                      and self._comment_public.get())
        slug = game["slug"]
        if public:
            try:
                signed_in = bool(slg_account.session())
            except slg_account.AccountError as exc:
                self._set_progress(str(exc))
                return
            if not signed_in:
                self._set_progress("公开评论需要云端账号，请先到个人中心创建或登录")
                return
            error = slg_comments.validate_public_comment(
                slug, content, nickname)
            if error:
                self._set_progress(error)
                return
        created = slg_db.add_comment(
            self.conn, slug, content, nickname,
            visibility="public" if public else "private")
        entry.delete("1.0", "end")
        if not public and getattr(self, "_panel_mode", None) == "comments":
            self._switch_comment_tab("mine")
        self._refresh_comment_view(game)
        self._set_progress("评论已保存，正在提交云端审核…" if public else "私人评论已保存在本机")
        if public:
            self._upload_comment(slug, created, content, nickname)

    def _upload_comment(self, slug, local_id, content, nickname):
        try:
            session_snapshot = slg_account.session()
        except slg_account.AccountError as exc:
            self._set_progress(str(exc))
            return
        if (not session_snapshot
                or session_snapshot.get("account_id") == "developer"):
            self._require_regular_user_mode("\u53d1\u5e03\u516c\u5f00\u8bc4\u8bba")
            return

        def worker():
            with slg_account.use_session_snapshot(session_snapshot):
                try:
                    result = slg_comments.submit_comment(slug, content, nickname)
                    error = None
                except slg_account.AccountError as exc:
                    result, error = {}, str(exc)
                cid = result.get("id")
                if cid:
                    with slg_db.session() as conn:
                        slg_db.mark_comment_uploaded(conn, local_id, cid)
                public_count = None
                if cid and result.get("status") in ("public", "approved"):
                    try:
                        public_count = slg_comments.my_public_count()
                    except slg_account.AccountError:
                        pass
            self.queue.put(("comment_upload", (slug, local_id, result, error)))
            if public_count is not None:
                self.queue.put(("achievement", public_count))
        threading.Thread(target=worker, daemon=True).start()

    def _retry_public_comment(self, row):
        """Retry a public comment that is saved locally but not on the server."""
        if not self._require_regular_user_mode("\u91cd\u8bd5\u516c\u5f00\u8bc4\u8bba"):
            return
        if not self._require_personal_access("重新发布评论", cloud_only=True):
            return
        game = self.selected
        if (not row or not game or row.get("cloud_id")
                or row.get("visibility") != "public"):
            return
        self._set_progress("正在重试公开发布…")
        error = slg_comments.validate_public_comment(
            game["slug"], row["content"], row.get("author"))
        if error:
            self._set_progress(error)
            return
        self._upload_comment(game["slug"], row["local_id"],
                             row["content"], row.get("author"))

    def _comment_upload_result(self, slug, local_id, result, error=None):
        if isinstance(result, bool):
            result = {"id": local_id, "status": "public"} if result else {}
        if not isinstance(result, dict):
            result = {}
        if result.get("id"):
            status = result.get("status")
            if status in ("public", "approved"):
                gained = int(result.get("gained") or 0)
                message = "评论已公开" + ("，云端积分 +%d" % gained if gained else "")
                # Public-review quest progress is authoritative on the server;
                # pending or rejected submissions never guess local completion.
                self._fetch_quests(force=True)
            elif status == "pending":
                message = "评论已提交，等待审核；通过后才会公开并结算积分"
            elif status == "rejected":
                message = "评论未通过审核，请检查内容后修改"
            else:
                message = "评论已上传，状态：%s" % (status or "待同步")
            self._set_progress(message)
        else:
            self._set_progress((error or "云端发布失败") + "；评论仍保存在本机，可重试公开")
        game = self.selected
        if game is not None and game["slug"] == slug:
            self._refresh_comment_view(game)

    def _report_comment(self, cloud_id):
        if not self._require_regular_user_mode("\u4e3e\u62a5\u8bc4\u8bba"):
            return
        if not self._require_personal_access("\u4e3e\u62a5\u8bc4\u8bba", cloud_only=True):
            return
        if not cloud_id:
            return
        try:
            session_snapshot = slg_account.session()
        except slg_account.AccountError as exc:
            self._set_progress(str(exc))
            return
        self._set_progress("\u6b63\u5728\u63d0\u4ea4\u4e3e\u62a5\u2026")

        def worker():
            with slg_account.use_session_snapshot(session_snapshot):
                try:
                    success = slg_comments.report_comment(cloud_id)
                except slg_account.AccountError:
                    success = False
            self.queue.put(("comment_report", bool(success)))

        threading.Thread(target=worker, daemon=True).start()

    def _vote_comment(self, cloud_id, value):
        if not self._require_regular_user_mode("\u8bc4\u4ef7\u8bc4\u8bba"):
            return
        if not self._require_personal_access("\u8bc4\u4ef7\u8bc4\u8bba", cloud_only=True):
            return
        if not cloud_id:
            return
        try:
            session_snapshot = slg_account.session()
        except slg_account.AccountError as exc:
            self._set_progress(str(exc))
            return

        def worker():
            with slg_account.use_session_snapshot(session_snapshot):
                try:
                    slg_comments.vote_comment(cloud_id, value)
                    error = None
                except slg_account.AccountError as exc:
                    error = str(exc)
            self.queue.put(("comment_vote", error))

        threading.Thread(target=worker, daemon=True).start()

    def _comment_vote_result(self, error):
        self._set_progress(error or "投票已记录")
        if not error:
            self._retry_comments_fetch()

    def _comment_report_result(self, success):
        self._set_progress("举报已提交，管理员会处理" if success
                           else "举报提交失败，请检查网络后重试")

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
                             justify="left", anchor="w", font=ui_font(size=14))
        label.pack(fill="x", anchor="w")
        text = ctk.CTkTextbox(box, height=190, corner_radius=8, fg_color=BG,
                              text_color=TEXT, border_color=CHIP,
                              border_width=1, font=ui_font(size=14), wrap="word")
        text.bind("<Control-Return>", lambda e: self._save_overview_edit())
        text.bind("<Escape>", lambda e: self._cancel_overview_edit())
        save = ctk.CTkButton(box, text="保存", height=28, width=80, corner_radius=8,
                             fg_color=ACCENT, text_color=ON_ACCENT,
                             hover_color=CARD_HOVER, font=ui_font(size=12),
                             command=self._save_overview_edit)
        # The name's editor says this through its placeholder, and it is only
        # ever seen when the box is empty. A textbox has no placeholder, and an
        # empty one with no explanation reads as the app having lost the text.
        hint = ctk.CTkLabel(box, text="", text_color=MUTED, font=ui_font(size=12),
                            anchor="w", wraplength=340, justify="left")
        box.bind("<Configure>", self._detail_overview_resized, add="+")
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

    def _detail_overview_resized(self, event):
        """Use the real detail content width and the active DPI scale."""
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:  # noqa: BLE001 - keep initial layout usable during map
            scale = 1.0
        wrap = detail_overview_wraplength(getattr(event, "width", 0), scale)
        for name in ("_ov_label", "_ov_hint"):
            widget = getattr(self, name, None)
            try:
                if widget is not None and widget.winfo_exists():
                    widget.configure(wraplength=wrap)
            except tk.TclError:
                pass

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
        if not self._require_personal_access("修改游戏译名"):
            return
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
        if not self._require_personal_access("修改游戏译名"):
            return
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
        if not self._require_personal_access("编辑游戏简介"):
            return
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
        p["ov_hint"].pack(fill="x", anchor="w")
        self._ov_text.focus_set()

    def _cancel_overview_edit(self):
        p = self._detail_parts
        p["ov_text"].pack_forget()
        p["ov_save"].pack_forget()
        p["ov_hint"].pack_forget()
        p["ov_label"].pack(anchor="w")

    def _save_overview_edit(self):
        if not self._require_personal_access("编辑游戏简介"):
            return
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
        if not self._require_personal_access("修改游戏状态"):
            return
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
        if not self._require_personal_access("修改收藏夹"):
            return
        game = self.selected
        if game is None or self.collection_id is None:
            return
        slg_db.remove_from_collection(self.conn, game["id"], self.collection_id)
        self._drop_card(game["id"])
        self._refresh_collection_menu()
        self._render_stats()

    def _set_rating(self, value):
        if not self._require_personal_access("提交游戏评分"):
            return
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

    # --- dialogs --------------------------------------------------------------

    def _new_dialog(self, title, geometry=None, parent=None, close_guard=None):
        """A dialog window with the two things every one of them needs.

        Escape closes it - Tk hands a bare Toplevel no bindings at all - and
        re-opening the same dialog reuses the window instead of stacking a
        second copy, which is what clicking 帮助文档 twice used to do.

        parent is where it lands: a dialog opened from another dialog (头衔简介
        over 我的头衔) belongs over that one, not over the main window.
        """
        for child in self.winfo_children():
            if isinstance(child, ctk.CTkToplevel) and child.winfo_exists() \
                    and child.title() == title:
                guarded_close = getattr(child, "_slg_close_guard", None)
                if guarded_close is not None:
                    guarded_close()
                    if child.winfo_exists():
                        # The caller may be showing newly rotated credentials.
                        # Preserve the old window if the user declines to close
                        # it, then create a new one so the new secret is not lost.
                        child.lift()
                else:
                    child.destroy()
        win = ctk.CTkToplevel(self)
        win._slg_identity_context = self._capture_identity_context()
        win.title(title)
        win.transient(parent if parent is not None else self)
        if close_guard is None:
            win.bind("<Escape>", lambda e: win.destroy())
        else:
            win._slg_close_guard = close_guard
            win.protocol("WM_DELETE_WINDOW", close_guard)
            win.bind("<Escape>", lambda e: (close_guard(), "break")[1])
        win.lift()

        def _relift():
            if win.winfo_exists():
                win.lift()
        win.after(120, _relift)
        if geometry:
            w, h = geometry.split("x")
            self._place(win, int(w), int(h), parent)
        else:
            self._center_on_parent(win, parent)
        return win

    def destroy(self):
        job = getattr(self, "_card_render_job", None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._card_render_pending = False
        self._card_render_generation = getattr(self, "_card_render_generation", 0) + 1
        executor = getattr(self, "_cover_executor", None)
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        # All timers in this interpreter belong to the root being destroyed.
        # Cancel before deleting Tcl commands, including toolkit polling timers.
        try:
            for handle in self.tk.call("after", "info"):
                self.after_cancel(handle)
        except tk.TclError:
            pass
        super().destroy()

    def _request_close(self):
        """Give a visible one-time credential dialog its final save check."""
        if getattr(self, "_rotate_pending", False):
            messagebox.showinfo(
                "\u5bc6\u94a5\u8f6e\u6362\u8fdb\u884c\u4e2d",
                "\u8bf7\u7b49\u8f6e\u6362\u7ed3\u679c\u5e76\u4fdd\u5b58\u65b0\u5bc6\u94a5\u548c\u6062\u590d\u7801\u540e\u518d\u5173\u95ed\u5ba2\u6237\u7aef\u3002",
                parent=self)
            return
        for child in self.winfo_children():
            if not isinstance(child, ctk.CTkToplevel) or not child.winfo_exists():
                continue
            guard = getattr(child, "_slg_close_guard", None)
            if guard is not None:
                guard()
                if child.winfo_exists():
                    return
        self.destroy()

    def _place(self, win, width, height, parent=None):
        """Size and centre a dialog in one geometry() call.

        Tk does not report a Toplevel's size back until it is mapped - long after
        the caller needs the position - so centring on winfo_width() measured the
        placeholder CTk hands out (200x200) and parked the dialog wherever that
        happened to land. The size the caller asked for is the only trustworthy
        number here. Real pixels for the position, unscaled units for the size:
        CTk multiplies the width and height by the display scale and passes the
        +x+y through untouched.
        """
        base = self if parent is None else parent
        win.update_idletasks()
        base.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(win)
        left, top, right, bottom = self._usable_screen_bounds(win)
        width, height = clamp_scaled_size(
            width, height, scale, right - left, bottom - top,
            margin=24, min_width=320, min_height=240)
        pixel_width, pixel_height = width * scale, height * scale
        x = base.winfo_rootx() + (base.winfo_width() - pixel_width) // 2
        y = base.winfo_rooty() + (base.winfo_height() - pixel_height) // 2
        x = min(max(int(x), left), max(left, int(right - pixel_width)))
        y = min(max(int(y), top), max(top, int(bottom - pixel_height)))
        win.geometry("%dx%d+%d+%d" % (width, height, x, y))

    @staticmethod
    def _usable_screen_bounds(widget):
        """Use the current monitor work area, including the taskbar boundary."""
        try:
            import ctypes
            from ctypes import wintypes

            class _MonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            user32 = ctypes.windll.user32
            monitor = user32.MonitorFromWindow(wintypes.HWND(widget.winfo_id()), 2)
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcWork
                if rect.right > rect.left and rect.bottom > rect.top:
                    return (int(rect.left), int(rect.top),
                            int(rect.right), int(rect.bottom))
        except (AttributeError, OSError, tk.TclError, TypeError, ValueError):
            pass
        try:
            left, top = int(widget.winfo_vrootx()), int(widget.winfo_vrooty())
            width, height = int(widget.winfo_vrootwidth()), int(widget.winfo_vrootheight())
            if width > 1 and height > 1:
                return left, top, left + width, top + height
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass
        return (0, 0, int(widget.winfo_screenwidth()),
                int(widget.winfo_screenheight()))

    def _apply_ui_scale_profile(self, profile, persist=True, resize_main=True):
        """Apply a target effective scale while retaining the monitor's DPI."""
        profile = normalize_ui_scale_profile(profile)
        tracker = ctk.ScalingTracker
        try:
            current_widget = float(tracker.get_widget_scaling(self))
            current_window = float(tracker.get_window_scaling(self))
            global_widget = float(tracker.widget_scaling)
            global_window = float(tracker.window_scaling)
            widget_dpi = current_widget / global_widget if global_widget else 1.0
            window_dpi = current_window / global_window if global_window else 1.0
        except (AttributeError, TypeError, ValueError, ZeroDivisionError, tk.TclError):
            widget_dpi = window_dpi = 1.0
        widget_factor, window_factor = ui_scale_multipliers(
            profile, window_dpi, widget_dpi)
        self.ui_scale_profile = profile
        ctk.set_widget_scaling(widget_factor)
        ctk.set_window_scaling(window_factor)
        if persist and getattr(self, "conn", None) is not None:
            slg_db.set_pref(self.conn, PREF_UI_SCALE, profile)
        if resize_main:
            try:
                self.after_idle(self._resize_main_for_ui_scale)
                self.after_idle(self._resize_settings_for_ui_scale)
                self.after_idle(self._refresh_detail_overview_wrap)
                self.after_idle(self._refresh_card_text_scaling)
            except tk.TclError:
                pass

    def _resize_main_for_ui_scale(self):
        try:
            self.update_idletasks()
            scale = float(ctk.ScalingTracker.get_window_scaling(self))
            left, top, right, bottom = self._usable_screen_bounds(self)
            screen_width, screen_height = right - left, bottom - top
            min_width, min_height = clamp_scaled_size(
                1020, 600, scale, screen_width, screen_height,
                margin=48, min_width=700, min_height=480)
            width, height = clamp_scaled_size(
                getattr(self, "_current_width", 1180),
                getattr(self, "_current_height", 760), scale,
                screen_width, screen_height, margin=48,
                min_width=min_width, min_height=min_height)
            self.minsize(min_width, min_height)
            x = left + max(0, (screen_width - width * scale) // 2)
            y = top + max(0, (screen_height - height * scale) // 2)
            self.geometry("%dx%d+%d+%d" % (width, height, int(x), int(y)))
        except (AttributeError, tk.TclError, TypeError, ValueError):
            return

    def _resize_settings_for_ui_scale(self):
        for child in self.winfo_children():
            try:
                if (isinstance(child, ctk.CTkToplevel) and child.winfo_exists()
                        and child.title() == "设置"):
                    self._place(child, 500, 650)
                    return
            except tk.TclError:
                continue

    def _refresh_detail_overview_wrap(self):
        label = getattr(self, "_ov_label", None)
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
            box = label.master
            width = max(0, box.winfo_width())
            if width <= 1:
                width = max(0, self.detail.winfo_width() - 36)
            self._detail_overview_resized(type("ConfigureEvent", (), {"width": width})())
        except (AttributeError, tk.TclError):
            return

    def _refresh_card_text_scaling(self):
        """Re-fit the card text after the font size or the monitor DPI changed.

        The three card labels are plain tk.Labels, so neither the font-size
        setting nor customtkinter's DPI tracking recalculates them: DPI
        tracking only ever rescales CTk widgets. The tagline's wrap width was
        measured against the old scaling too, so it is re-measured here rather
        than being left to wrap at a width the card no longer has.
        """
        for slot in getattr(self, "_card_pool", []):
            frame = slot["frame"]
            try:
                if not frame.winfo_exists():
                    continue
                title_font, meta_font, tagline_font = self._card_text_fonts(frame)
                slot["title"].configure(font=title_font)
                slot["meta"].configure(font=meta_font)
                slot["tagline"].configure(
                    font=tagline_font,
                    wraplength=frame._apply_widget_scaling(CARD_WRAP))
            except tk.TclError:
                continue

    def _on_dpi_scaling_changed(self, _widget_scaling, _window_scaling):
        """Re-fit the hand-scaled text when the window changes monitor.

        customtkinter calls this for every registered widget when the detected
        monitor DPI changes, but it only rescales CTk widgets. The card's three
        tk.Labels and the tagline's pixel wrap width are ours to redo.
        """
        self._refresh_card_text_scaling()

    def _center_on_parent(self, win, parent=None):
        """Park a dialog over the middle of its parent window.

        Tk defaults a Toplevel to a cascade position near the top-left of the
        screen; that is why every dialog used to open up there. geometry() takes
        real pixels for the position, so the offset is measured from winfo_root*
        rather than computed from the requested size.
        """
        base = self if parent is None else parent
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        if w <= 1 or h <= 1:
            return
        x = base.winfo_rootx() + (base.winfo_width() - w) // 2
        y = base.winfo_rooty() + (base.winfo_height() - h) // 2
        left, top, right, bottom = self._usable_screen_bounds(win)
        x = min(max(x, left), max(left, right - w))
        y = min(max(y, top), max(top, bottom - h))
        win.geometry("+%d+%d" % (x, y))

    def _fit_dialog(self, win, width, parent=None):
        """Height a dialog from its actual content, clamped to the screen.

        Every dialog here used to hard-code a geometry, and CTk multiplies that
        number by the display scale (1.5 on the machine this was written on): a
        "340x432" is really 510x648, and the moment the content grew past it the
        last widget was simply cut off - which is exactly how 幸运之王的获得方式
        and the lottery's disclaimer disappeared. reqheight() is in real pixels
        while geometry() is in unscaled units, hence the division by the scale;
        that mismatch is the whole trick.

        Callers with text of unpredictable length keep the middle in a scrollable
        frame so that this clamp is never the thing that hides a control.
        """
        win.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(win)
        limit = int(win.winfo_screenheight() * 0.9)
        height = int(min(win.winfo_reqheight() / float(scale), limit))
        self._place(win, width, height, parent)

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

    def _title_badge(self, parent, title_id, animate=True, max_width=None,
                     background=None):
        """Draw a restrained rarity badge; high tiers get brief, separated accents.

        Ordinary and rare badges stay still. Higher tiers use a single short
        animation followed by a long pause, and only while the canvas is mapped.
        The canvas keeps a fixed set of shapes, so animation never rebuilds it.
        """
        t = slg_titles.title_by_id(title_id) or {"name": "普通用户", "rarity": "普通"}
        rarity = t["rarity"]
        color = slg_titles.RARITY_COLORS.get(rarity, MUTED)
        name = t["name"]
        bg = background or BG
        h = 36
        font_size = 12
        font = ui_tkfont(size=font_size, weight="bold")
        if max_width:
            while font_size > 8 and font.measure(name) + 52 > max_width:
                font_size -= 1
                font = ui_tkfont(size=font_size, weight="bold")
        w = max(72, font.measure(name) + 52)
        if max_width:
            w = min(w, max_width)
        c = tk.Canvas(parent, width=w, height=h, highlightthickness=0,
                      bg=bg, bd=0, relief="flat")

        # The rim and inset use the Canvas oval/rectangle primitives to form a
        # pill without relying on transparency or unsupported rounded shapes.
        rim = _mix(bg, color, 0.62 if rarity in ("史诗", "传说", "至臻") else 0.34)
        face = _mix(bg, color, 0.20 if rarity in ("史诗", "传说", "至臻") else 0.13)
        inner = _mix(face, "#ffffff", 0.055)
        r = h // 2
        outer_items = [
            c.create_oval(0, 0, h, h, fill=rim, outline=""),
            c.create_oval(w - h, 0, w, h, fill=rim, outline=""),
            c.create_rectangle(r, 0, w - r, h, fill=rim, outline=""),
        ]
        inner_items = [
            c.create_oval(1, 1, h - 1, h - 1, fill=inner, outline=""),
            c.create_oval(w - h + 1, 1, w - 1, h - 1, fill=inner, outline=""),
            c.create_rectangle(r, 1, w - r, h - 1, fill=inner, outline=""),
        ]
        # A fine upper glint adds a metal edge while remaining legible in both
        # themes. The band is narrow and stays inside the pill silhouette.
        top_rule = c.create_line(r + 5, 2, w - r - 5, 2,
                                 fill=_mix(inner, "#ffffff", 0.20), width=1)
        text_item = c.create_text(w // 2, h // 2, text=name, fill=color,
                                  font=font)
        sweep = c.create_polygon(-30, 4, -23, 4, -10, h - 4, -17, h - 4,
                                 fill=_mix(color, "#ffffff", 0.62),
                                 outline="", state="hidden")
        # Raise the sweep over the surface but under the title text.
        c.tag_raise(sweep, text_item)
        c.tag_raise(text_item)

        if not animate or self.reduced_motion or rarity in ("普通", "稀有"):
            return c

        state = {"alive": True, "mapped": False, "after": None,
                 "frame": 0, "phase": "idle"}

        def cancel_pending():
            job = state["after"]
            state["after"] = None
            if job is not None:
                try:
                    c.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass

        def schedule(delay):
            if not state["alive"] or not state["mapped"]:
                return
            try:
                state["after"] = c.after(delay, tick)
            except tk.TclError:
                state["alive"] = False

        def reset_visuals():
            try:
                for item in outer_items:
                    c.itemconfig(item, fill=rim)
                for item in inner_items:
                    c.itemconfig(item, fill=inner)
                c.itemconfig(text_item, fill=color)
                c.itemconfig(sweep, state="hidden")
            except tk.TclError:
                state["alive"] = False

        def tick():
            state["after"] = None
            if not state["alive"] or not state["mapped"]:
                return
            try:
                if not c.winfo_exists() or not c.winfo_ismapped():
                    state["mapped"] = False
                    return
            except tk.TclError:
                state["alive"] = False
                return

            if self.reduced_motion:
                reset_visuals()
                state["frame"] = 0
                schedule(6200)
                return

            frame = state["frame"]
            state["frame"] += 1
            if rarity == "史诗":
                # A low-amplitude champagne breath: one gentle rise and fall.
                p = 1 - abs(2 * (frame / 10.0) - 1)
                c.itemconfig(outer_items[0], fill=_mix(rim, "#f4d7ad", 0.10 * p))
                c.itemconfig(outer_items[1], fill=_mix(rim, "#f4d7ad", 0.10 * p))
                c.itemconfig(outer_items[2], fill=_mix(rim, "#f4d7ad", 0.10 * p))
                if state["frame"] < 11:
                    schedule(150)
                else:
                    reset_visuals()
                    state["frame"] = 0
                    schedule(6200)
            elif rarity == "传说":
                # One fine gold reflection passes through, then rests for seconds.
                x = -28 + (w + 56) * frame / 12.0
                c.coords(sweep, x, 4, x + 7, 4, x + 20, h - 4, x + 13, h - 4)
                c.itemconfig(sweep, fill=_mix(color, "#fff2c9", 0.46), state="normal")
                if state["frame"] < 13:
                    schedule(85)
                else:
                    c.itemconfig(sweep, state="hidden")
                    state["frame"] = 0
                    schedule(6800)
            elif rarity == "至臻":
                # A muted rose/champagne gleam; no rainbow cycling or flashing.
                p = 1 - abs(2 * (frame / 10.0) - 1)
                c.itemconfig(text_item, fill=_mix(color, "#fff1dc", 0.10 * p))
                x = -24 + (w + 48) * frame / 10.0
                c.coords(sweep, x, 4, x + 5, 4, x + 15, h - 4, x + 10, h - 4)
                c.itemconfig(sweep, fill=_mix(color, "#fff1dc", 0.32), state="normal")
                if state["frame"] < 11:
                    schedule(100)
                else:
                    c.itemconfig(sweep, state="hidden")
                    c.itemconfig(text_item, fill=color)
                    state["frame"] = 0
                    schedule(8200)

        def on_map(_event=None):
            if not state["alive"]:
                return
            state["mapped"] = True
            if state["after"] is None:
                state["frame"] = 0
                schedule(700 if rarity == "史诗" else 950)

        def on_unmap(_event=None):
            state["mapped"] = False
            cancel_pending()
            state["frame"] = 0
            reset_visuals()

        def on_destroy(event):
            if event.widget is c:
                state["alive"] = False
                state["mapped"] = False
                cancel_pending()

        c.bind("<Map>", on_map, add="+")
        c.bind("<Unmap>", on_unmap, add="+")
        c.bind("<Destroy>", on_destroy, add="+")
        return c

    def _equip_title(self, win, title_id):
        if not self._require_identity_context(
                getattr(win, "_slg_identity_context", None),
                "\u8bbe\u7f6e\u4e2a\u4eba\u88c5\u626e", require_regular=True):
            return
        if not self._require_personal_access("设置个人装扮"):
            return
        if self._has_cloud_account():
            desired = (slg_titles.DEFAULT_TITLE_ID
                       if title_id == getattr(self, "_cloud_equipped", "")
                       else title_id)
            self._wardrobe_reopen_after_equip = True
            self._run_cloud_action("equip", lambda: slg_account.equip_title(desired))
            win.destroy()
            return
        if title_id == slg_titles.DEFAULT_TITLE_ID:
            slg_db.set_equipped_title(self.conn, "")
        else:
            slg_db.set_equipped_title(self.conn, title_id)
        win.destroy()
        self.open_titles()

    def _equip_appearance(self, slot, item_id, return_to_wardrobe=False,
                          identity_context=None):
        if not self._require_identity_context(
                identity_context, "\u88c5\u5907\u5916\u89c2", require_regular=True):
            return
        if not self._has_cloud_account():
            self.open_profile()
            return
        if slot not in ("avatar_frame", "comment_frame"):
            self._set_progress("未知的装扮部位")
            return
        self._wardrobe_pending_slot = slot
        self._wardrobe_pending_item = item_id or ""
        self._wardrobe_reopen_after_equip = bool(return_to_wardrobe)
        self._run_cloud_action(
            "equip_appearance",
            lambda s=slot, i=item_id or "": slg_account.equip_appearance(s, i))

    # Report event names to the words the panel shows. The wire names stay
    # stable so the aggregate keeps working across client versions; only the
    # display changes here. An unknown name falls through untranslated rather
    # than disappearing, so a new event is visible before it is labelled.
    def _open_admin_console(self):
        """Open the server console for administrative analytics."""
        webbrowser.open(ADMIN_URL)

    def _unlock_all_appearances(self):
        if not self._is_admin_mode():
            self._set_progress("只有启用开发者身份后才能解锁全部装扮")
            return
        if not self._has_usable_cloud_session():
            self._ensure_developer_cloud_identity()
            self._set_progress("正在关联开发者云端身份，连接成功后请再点击一次")
            return
        if self._developer_title_unlock_pending:
            return
        developer_key = slg_titles.dev_secret()
        if not developer_key:
            self._set_progress("本机未找到开发者密钥，无法解锁云端装扮")
            return
        self._developer_title_unlock_pending = True
        self._run_cloud_action(
            "developer_unlock_appearances",
            lambda: slg_account.developer_grant_all_appearances(developer_key))

    def _unlock_all_titles(self):
        """Compatibility alias for older callbacks; now grants all appearances."""
        self._unlock_all_appearances()

    def _on_signin_click(self):
        if not self._require_regular_user_mode("\u7b7e\u5230\u5956\u52b1"):
            return
        if not self._require_personal_access("每日签到"):
            return
        if self._remote_flags.get("disable_signin"):
            messagebox.showinfo("签到", "签到功能维护中，稍后再试", parent=self)
            return
        if self._has_cloud_account():
            button = getattr(self, "_shop_signin_button", None)
            if button is not None and button.winfo_exists():
                button.configure(text="正在签到…", state="disabled",
                                 fg_color=CHIP, text_color=MUTED)
            self._run_cloud_action("signin", slg_account.signin)
            self._set_progress("正在提交云端签到…")
            return
        dev = self._is_admin_mode()
        already, _day, gained, bonus = slg_titles.signin(self.conn)
        if already and not dev:
            self._show_signin_result(gained=0, already=True, dev=False)
            return
        slg_remote.report(self.conn, "signin", {"dev": dev})
        if not dev and self.signin_btn is not None and self.signin_btn.winfo_exists():
            self.signin_btn.configure(text="今日已签到", state="disabled")
        self._update_shop_signin_button()
        self._show_signin_result(gained=gained, bonus=bonus, already=False, dev=dev)
        if self._panel_mode == "profile":
            self.open_profile()
        elif self._panel_mode == "shop":
            self.open_shop()

    def _show_signin_result(self, gained, already, dev, bonus=0):
        """签到结果弹窗，成功时带撒花动画。开发者模式下可反复签到。"""
        win = self._new_dialog("每日签到", "360x320")
        canvas = tk.Canvas(win, width=360, height=150, highlightthickness=0, bg=BG)
        canvas.pack(fill="x")
        if already:
            ctk.CTkLabel(win, text="今日已签到", text_color=TEXT,
                         font=ui_font(size=16, weight="bold")).pack(pady=(10, 2))
            ctk.CTkLabel(win, text="明天再来吧", text_color=MUTED,
                         font=ui_font(size=12)).pack()
        else:
            self._confetti(canvas, 360, 150, gained + bonus)
            title = "无限签到" if dev else "签到成功"
            ctk.CTkLabel(win, text=title, text_color=ACCENT,
                         font=ui_font(size=16, weight="bold")).pack(pady=(6, 2))
            if dev:
                sub = "开发者模式 · 积分已到账"
            elif bonus:
                sub = "签到 +%d · 累签奖励 +%d" % (gained, bonus)
            else:
                sub = "签到 +%d 积分" % gained
            ctk.CTkLabel(win, text=sub, text_color=MUTED,
                         font=ui_font(size=12)).pack()
        ctk.CTkButton(win, text="知道了", height=34, width=120, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=13), command=win.destroy).pack(pady=(14, 0))

    def _confetti(self, canvas, w, h, gained, label="+%d 积分", ramp=None):
        """Falling confetti plus a counting-up +N 积分, run on one after loop.

        `label=None` drops the counter and `ramp` swaps the palette, which is
        how the lottery reuses this for a gold shower over a title win - a
        second particle system for the same effect would be two things to keep
        in step.
        """
        previous = getattr(canvas, "_slg_confetti_scheduler", None)
        if previous:
            previous.cancel_all()
            canvas.delete("slg_confetti")
        ramp = ramp or ("#e84393", "#e06a3f", "#e0a800", "#3fae5a", "#2f9bd0",
                        "#8b5cf6")
        parts = []
        rects = []
        for _ in range(64):
            size = random.randint(3, 7)
            p = {"x": random.uniform(0, w), "y": random.uniform(-h, 0),
                 "vy": random.uniform(1.6, 3.6), "dx": random.uniform(-1.2, 1.2),
                 "color": random.choice(ramp)}
            parts.append(p)
            rects.append(canvas.create_rectangle(
                p["x"], p["y"], p["x"] + size, p["y"] + size,
                fill=p["color"], outline="", tags="slg_confetti"))
        text_item = None
        if label:
            text_item = canvas.create_text(w // 2, h // 2 - 8, text="+0 积分",
                                          fill=ACCENT, font=ui_tkfont(22, "bold"), tags="slg_confetti")
        start = time.perf_counter()
        scheduler = MotionScheduler(canvas)
        canvas._slg_confetti_scheduler = scheduler
        finished = [False]
        def finish():
            scheduler.cancel_all()
            for item in rects:
                canvas.delete(item)
            if text_item is not None:
                canvas.itemconfig(text_item, text=label % gained, fill=ACCENT)
            finished[0] = True

        def tick():
            try:
                if not canvas.winfo_exists():
                    return
            except tk.TclError:
                return
            if self.reduced_motion or time.perf_counter() - start >= 1.5:
                finish()
                return
            if not canvas.winfo_ismapped():
                return
            for i, p in enumerate(parts):
                p["y"] += p["vy"]
                p["x"] += p["dx"]
                if p["y"] > h + 8:
                    p["y"] = -8
                    p["x"] = random.uniform(0, w)
                canvas.coords(rects[i], p["x"], p["y"],
                              p["x"] + 5, p["y"] + 5)
            if text_item is not None:
                progress = min(1.0, (time.perf_counter() - start) / 0.6)
                shown = int(gained * progress)
                pulse = 1 - abs(2 * (progress % 0.5) / 0.5 - 1) if progress < 1 else 0
                color = _mix(ACCENT, "#ffffff", 0.35 * pulse) if pulse else ACCENT
                canvas.itemconfig(text_item, text=label % shown, fill=color)
            scheduler.call_later(30, tick)

        canvas.bind("<Unmap>", lambda e: scheduler.cancel_all(), add="+")
        def resume(e):
            if e.widget is canvas and not finished[0] and not scheduler.pending_count:
                scheduler.call_later(30, tick)
        canvas.bind("<Map>", resume, add="+")
        if self.reduced_motion:
            finish()
        else:
            scheduler.call_later(30, tick)

    def _edit_nickname(self):
        if not self._require_personal_access("修改个人资料"):
            return
        win = self._new_dialog("修改昵称", "340x180")
        identity_context = win._slg_identity_context
        cloud_mode = self._has_cloud_account()
        cur = (getattr(self, "_cloud_me", {}).get("nickname", "") if cloud_mode
               else slg_db.get_pref(self.conn, "profile.nickname", "")) or ""
        ctk.CTkLabel(win, text=("设置云端昵称，跨设备同步" if cloud_mode
                                else "设置你的昵称（本地保存，随时可改）"), text_color=TEXT,
                     font=ui_font(size=13)).pack(fill="x", padx=16, pady=(16, 8))
        entry = ctk.CTkEntry(win, placeholder_text="昵称…", height=32, corner_radius=8,
                             fg_color=CARD, text_color=TEXT,
                             placeholder_text_color=MUTED, border_width=1,
                             border_color=CHIP, font=ui_font(size=13))
        entry.insert(0, cur)
        entry.pack(fill="x", padx=16)

        def save():
            if not self._require_identity_context(
                    identity_context, "\u4fee\u6539\u6635\u79f0", require_regular=True):
                return
            if not self._require_personal_access("修改个人资料"):
                return
            name = entry.get().strip()
            if not name and not cloud_mode:
                return
            if cloud_mode:
                self._run_cloud_action("profile", lambda: slg_account.update_profile(name))
                win.destroy()
                return
            slg_db.set_pref(self.conn, "profile.nickname", name)
            if not slg_db.get_pref(self.conn, "profile.registered_at"):
                slg_db.set_pref(self.conn, "profile.registered_at",
                                time.strftime("%Y-%m-%d %H:%M:%S"))
            win.destroy()
            self.open_profile()

        ctk.CTkButton(win, text="保存", height=32, width=90, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=12), command=save).pack(pady=(12, 0))

    def _is_admin_mode(self):
        return slg_titles.admin_mode(self.conn)

    def _require_regular_user_mode(self, feature):
        """Keep ordinary account rewards and community writes out of admin mode."""
        if not self._is_admin_mode():
            return True
        self._set_progress(
            "\u7ba1\u7406\u5458\u8eab\u4efd\u4e0d\u53c2\u4e0e%s\uff1b"
            "\u8bf7\u5728\u4e2a\u4eba\u9875\u5207\u6362\u5230\u666e\u901a\u7528\u6237\u540e\u64cd\u4f5c" % feature)
        return False

    def _has_local_admin_capability(self):
        return (slg_titles.dev_unlocked(self.conn)
                and bool(slg_titles.dev_secret()))

    @staticmethod
    def _current_session_account_id():
        try:
            current = slg_account.session()
        except slg_account.AccountError:
            return None
        return current.get("account_id") if isinstance(current, dict) else None

    def _capture_identity_context(self):
        """Snapshot the active account for callbacks owned by a dialog."""
        return {
            "generation": getattr(self, "_identity_generation", 0),
            "admin": bool(self._is_admin_mode()),
            "account_id": self._current_session_account_id(),
        }

    def _require_identity_context(self, context, feature, require_regular=False):
        """Reject delayed dialog actions after the active identity has changed."""
        context = context or self._capture_identity_context()
        current = self._capture_identity_context()
        if (context.get("generation") != current.get("generation")
                or context.get("admin") != current.get("admin")
                or context.get("account_id") != current.get("account_id")):
            self._set_progress(
                "\u8eab\u4efd\u5df2\u5207\u6362\uff1b\u8bf7\u91cd\u65b0\u6253\u5f00%s" % feature)
            return False
        if require_regular and current.get("admin"):
            return self._require_regular_user_mode(feature)
        return True

    def _identity_switch_blocked(self):
        return bool(
            getattr(self, "_identity_switch_pending", False)
            or getattr(self, "_developer_cloud_login_pending", False)
            or getattr(self, "_owner_account_pending", False)
            or getattr(self, "_cloud_create_pending", False)
            or getattr(self, "_cloud_login_pending", False)
            or getattr(self, "_rotate_pending", False)
            or getattr(self, "_owner_secret_dialog_pending", False))

    def _close_identity_scoped_dialogs(self):
        """Close old account dialogs before rendering the newly active identity."""
        for child in self.winfo_children():
            try:
                if (isinstance(child, ctk.CTkToplevel)
                        and child.winfo_exists()
                        and hasattr(child, "_slg_identity_context")
                        and not getattr(child, "_slg_credential_dialog", False)):
                    child.destroy()
            except tk.TclError:
                continue

    def _profile_identity_switch(self, parent, admin):
        """Show an explicit identity switch only on locally owner-enabled installs."""
        if not self._has_local_admin_capability():
            return
        box = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10,
                           border_width=1, border_color=ACCENT)
        box.pack(fill="x", padx=16, pady=(10, 0))
        current = "管理员" if admin else "普通用户"
        target = "普通用户" if admin else "管理员"
        blocked = self._identity_switch_blocked()
        ctk.CTkLabel(
            box, text="当前身份：%s" % current, text_color=ACCENT,
            font=ui_font(size=12, weight="bold")).pack(
                side="left", padx=12, pady=10)
        ctk.CTkButton(
            box, text=("\u8bf7\u7b49\u5f53\u524d\u8d26\u53f7\u64cd\u4f5c" if blocked
                      else "\u5207\u6362\u5230%s\u8eab\u4efd" % target),
            state="disabled" if blocked else "normal",
            height=30, width=150, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT,
            hover_color=CARD_HOVER, font=ui_font(size=11, weight="bold"),
            command=self._switch_identity_mode).pack(
                side="right", padx=10, pady=8)

    def _render_admin_profile(self, parent):
        """Render a separate owner workspace without ordinary account progress."""
        self._profile_balance_label = None
        self._cloud_account_label = None
        self._profile_identity_switch(parent, admin=True)
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        card.pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(card, text="管理员工作台", text_color=TEXT,
                     font=ui_font(size=17, weight="bold")).pack(
            anchor="w", padx=14, pady=(13, 3))
        ctk.CTkLabel(
            card,
            text="此页面使用固定管理员身份。普通用户的积分、任务、邀请和头衔进度在普通用户模式查看。",
            text_color=MUTED, font=ui_font(size=11), wraplength=430,
            justify="left").pack(anchor="w", padx=14, pady=(0, 10))
        try:
            current = slg_account.session()
        except slg_account.AccountError:
            current = None
        connected = bool(current and current.get("account_id") == "developer")
        ctk.CTkLabel(
            card,
            text=("固定管理员云端身份已连接。" if connected else
                  "正在连接固定管理员云端身份；未验证成功前不会开放管理员模式。"),
            text_color=ACCENT if connected else MUTED,
            font=ui_font(size=11), wraplength=430, justify="left").pack(
                anchor="w", padx=14, pady=(0, 12))

        owner = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        owner.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(owner, text="管理员绑定的普通账号", text_color=ACCENT,
                     font=ui_font(size=13, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 3))
        account_text = ("已绑定账号：%s" % self._owner_account_bound_id
                        if self._owner_account_bound_id else
                        "账号密钥仅在创建或明确重新签发成功时显示一次。")
        ctk.CTkLabel(owner, text=account_text, text_color=MUTED,
                     font=ui_font(size=11), wraplength=430,
                     justify="left").pack(anchor="w", padx=14, pady=(0, 8))
        ctk.CTkButton(
            owner, text="申请绑定普通账号", height=32,
            state="disabled" if self._owner_account_pending else "normal",
            fg_color=ACCENT, text_color=ON_ACCENT,
            command=self._create_owner_account).pack(
                fill="x", padx=14, pady=(0, 7))
        ctk.CTkButton(
            owner, text="重新签发绑定账号密钥（撤销旧会话）", height=30,
            state="disabled" if self._owner_account_pending else "normal",
            fg_color=CHIP, text_color=TEXT,
            command=self._reissue_owner_account).pack(
                fill="x", padx=14, pady=(0, 12))

        tools = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        tools.pack(fill="x", padx=16, pady=(12, 14))
        ctk.CTkLabel(tools, text="管理员工具", text_color=ACCENT,
                     font=ui_font(size=13, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 7))
        for title, callback in (
                ("打开管理台", self._open_admin_console),
                ("解锁全部装扮", self._unlock_all_appearances),
                ("更换管理员头像", self.pick_avatar)):
            ctk.CTkButton(
                tools, text=title, height=31, fg_color=CHIP,
                text_color=TEXT, hover_color=CARD_HOVER,
                command=callback).pack(fill="x", padx=14, pady=(0, 6))
        if os.path.exists(slg_db.avatar_path()):
            ctk.CTkButton(
                tools, text="恢复默认头像", height=29, fg_color="transparent",
                text_color=MUTED, hover_color=CARD_HOVER,
                command=self.clear_avatar).pack(
                    fill="x", padx=14, pady=(0, 10))
        ctk.CTkLabel(
            parent,
            text="游戏目录、收藏夹和评分保存在这台电脑上，两个身份共用这些本机数据。",
            text_color=MUTED, font=ui_font(size=10), wraplength=440,
            justify="left").pack(anchor="w", padx=18, pady=(0, 16))

    def open_profile(self):
        # 个人中心 shares the detail panel: the first click swaps the panel over
        # (temporarily covering any game shown), and the actions inside it -
        # 修改昵称/个性装扮/兑换码 - still open dialogs. The content is grouped
        # into labelled sections so the flat pile of numbers reads as a dashboard.
        self._panel_mode = "profile"
        if self._is_admin_mode():
            self._ensure_developer_cloud_identity()
        self._set_shop_wide_layout(False)
        self._destroy_detail()
        d = self.detail
        if self._is_admin_mode():
            self._render_admin_profile(d)
            return
        if not self._has_usable_cloud_session():
            self._profile_identity_switch(d, admin=False)
            self._render_guest_profile(d)
            return
        nickname = (getattr(self, "_cloud_me", {}).get("nickname", "")
                    if self._has_cloud_account() else
                    slg_db.get_pref(self.conn, "profile.nickname", "")) or ""
        equipped = (getattr(self, "_cloud_equipped", slg_titles.DEFAULT_TITLE_ID)
                    if self._has_cloud_account() else
                    slg_db.get_equipped_title(self.conn) or slg_titles.DEFAULT_TITLE_ID)
        dev = self._is_admin_mode()

        # 身份卡：头像 + 昵称 + 头衔，右侧积分。换过头像的人显示那张图，没换过的
        # 仍是昵称首字 —— 默认头像属于作者，挂到别人名下会认错人。
        appearances = (self._equipped_appearance_map()
                       if self._has_cloud_account() else {})
        avatar_frame = appearances.get("avatar_frame", "")
        cosmetic = appearances.get("comment_frame", "")
        head = ctk.CTkFrame(
            d, fg_color=CARD, corner_radius=10,
            border_width=1 if avatar_frame or cosmetic else 0,
            border_color=(self._appearance_color(avatar_frame or cosmetic)
                          if avatar_frame or cosmetic else CARD))
        head.pack(fill="x", padx=16, pady=(10, 0))
        if avatar_frame:
            avatar = self._avatar_frame_canvas(
                head, avatar_frame, nickname, size=64, actual_avatar=True)
        elif os.path.exists(slg_db.avatar_path()):
            avatar = ctk.CTkLabel(head, text="", image=load_avatar(48))
        else:
            avatar = ctk.CTkLabel(head, text=(nickname[:1] or "游"), width=48,
                                  height=48, corner_radius=24, fg_color=ACCENT,
                                  text_color=ON_ACCENT,
                                  font=ui_font(size=20, weight="bold"))
        avatar.pack(side="left", padx=14, pady=10)
        if dev:
            avatar.configure(cursor="hand2")
            avatar.bind("<Button-1>", lambda e: self.pick_avatar())
        info = ctk.CTkFrame(head, fg_color="transparent")
        info.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(info, text=nickname or "未设置昵称", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(anchor="w")
        self._title_badge(info, equipped).pack(anchor="w", pady=(6, 0))
        balance_text, balance_color = self._profile_balance_display()
        self._profile_balance_label = ctk.CTkLabel(
            head, text=balance_text, text_color=balance_color,
            font=ui_font(size=15, weight="bold"), justify="right")
        self._profile_balance_label.pack(side="right", padx=(8, 14), pady=8)
        self._profile_identity_switch(d, admin=False)
        self._profile_cloud_account(d)

        # 改档警告：账本封印对不上才出现。上面那个余额已经是「只算到断链为止」
        # 的数字，这里把被排掉的部分明说，免得用户以为积分凭空少了。
        report = slg_db.tamper_report(self.conn)
        if report:
            self._tamper_notice(d, report)

        # 我的数据：两行统计卡。库存、行为、收支本来分散在三个小节里，各自只有
        # 一两行字，滚动一屏全是标题 —— 合成一块才读得出「我这台机器攒了什么」。
        flow = slg_db.points_flow(self.conn)
        self._profile_section(d, "我的数据")
        self._profile_cells(d, (
            ("本机共用收藏", slg_db.collection_count(self.conn),
             self._open_profile_collections),
            ("本机共用评分", slg_db.rating_count(self.conn)),
            ("本机共用游戏目录", slg_db.local_count(self.conn)),
        ))
        self._profile_cells(d, (
            ("本机抽奖次数", slg_db.lottery_count(self.conn)),
            ("本机积分收入", flow["earned"]),
            ("本机积分支出", flow["spent"]),
        ))

        # 操作：三个入口横排。放在「我的数据」正下方、签到日历之前，是为了让
        # 「兑换码」这类高频入口首屏可见 —— 原先压在面板最底部，小窗下必须滚到
        # 底才看得到，等于"找不到入口"。
        self._profile_section(d, "操作")
        acts = ctk.CTkFrame(d, fg_color="transparent")
        acts.pack(fill="x", padx=16)
        for i, (text, fn) in enumerate((("修改昵称", self._edit_nickname),
                                        ("个性装扮", self.open_wardrobe),
                                        ("兑换码&邀请码", self.open_redeem))):
            # width=1 matters: with expand=True the packer satisfies every
            # button's request first and splits what is left. Without it each
            # one asks for CTkButton's default 140px, three of those are wider
            # than this panel, and the third ends up with no width at all -
            # invisible, and unreachable because a vertical CTkScrollableFrame
            # clips overflow instead of scrolling it.
            ctk.CTkButton(acts, text=text, width=1, height=34, corner_radius=8,
                          fg_color=CHIP, text_color=TEXT,
                          hover_color=CARD_HOVER, font=ui_font(size=12),
                          command=fn).pack(side="left", expand=True, fill="x",
                                           padx=(0, 0 if i == 2 else 6))

        # 本月签到：日历 + 连续/累计 + 累签奖励。
        cloud_mode = self._has_cloud_account()
        cloud_me = getattr(self, "_cloud_me", {}) if cloud_mode else {}
        sign = ({"streak": cloud_me.get("signin_streak", 0),
                 "total": cloud_me.get("signin_month_count", 0)}
                if cloud_mode else slg_db.signin_days(self.conn))
        today = date.today()
        first_wd, days_in_month = calendar.monthrange(today.year, today.month)
        signed = ({int(iso[-2:]) for iso in cloud_me.get("signin_month_days", [])
                   if isinstance(iso, str) and iso.startswith(today.strftime("%Y-%m-"))}
                  if cloud_mode else slg_db.signin_month_days(
                      self.conn, today.year, today.month))
        self._profile_section(d, "云端本月签到" if cloud_mode else "本月签到",
                              "连续 %d 天 · %s %d 天" % (
                                  sign["streak"], "本月" if cloud_mode else "累计",
                                  sign["total"]))
        cal = ctk.CTkFrame(d, fg_color="transparent")
        cal.pack(fill="x", padx=16)
        for wd, name in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            cal.grid_columnconfigure(wd, weight=1, uniform="signin-week")
            ctk.CTkLabel(cal, text=name, text_color=MUTED,
                         font=ui_font(size=10)).grid(row=0, column=wd, padx=1,
                                                     sticky="ew")
        col, row = first_wd, 1
        for day in range(1, days_in_month + 1):
            is_signed = day in signed
            is_today = day == today.day
            # 漏签 = 本月、今天之前、没签过。这些格子可以点，点了就补这一天。
            missed = (not cloud_mode and not is_signed and day < today.day
                      and slg_titles.makeup_problem(
                          self.conn, "%04d-%02d-%02d"
                          % (today.year, today.month, day)) is None)
            cell = ctk.CTkLabel(
                cal, text=str(day), height=26, corner_radius=6,
                fg_color=(ACCENT if is_signed
                          else (CHIP if is_today else "transparent")),
                text_color=(ON_ACCENT if is_signed else TEXT),
                font=ui_font(size=10))
            if missed:
                # 淡红描边：一眼看清哪天漏了，而不是要去数哪几格没有颜色。
                cell.configure(text_color=DANGER_TEXT, border_width=1,
                               border_color=_mix(BG, DANGER_TEXT, 0.45),
                               cursor="hand2")
                cell.bind("<Button-1>",
                          lambda e, d=day: self._makeup_prompt(d))
            cell.grid(row=row, column=col, padx=2, pady=2, sticky="ew")
            col += 1
            if col > 6:
                col = 0
                row += 1

        claimed = (max(cloud_me.get("signin_milestones", []) or [0])
                   if cloud_mode else slg_titles.claimed_milestone(self.conn))
        ms_parts = []
        for need in sorted(slg_titles.SIGNIN_MILESTONES):
            mark = "已领" if claimed >= need else "+%d" % slg_titles.SIGNIN_MILESTONES[need]
            ms_parts.append("%d天%s" % (need, mark))
        ctk.CTkLabel(d, text="本月已签 %d 天 · 累签奖励 %s"
                     % (len(signed), "  ".join(ms_parts)),
                     text_color=MUTED, font=ui_font(size=11)).pack(
            anchor="w", padx=16, pady=(4, 0))
        if not cloud_mode and any(1 <= day < today.day and day not in signed
               for day in range(1, days_in_month + 1)):
            ctk.CTkLabel(d, text="红色日期是漏签，点它就能用 %d 积分补上"
                         % slg_titles.MAKEUP_CARD_COST,
                         text_color=MUTED, font=ui_font(size=10)).pack(
                anchor="w", padx=16, pady=(2, 0))

        self._profile_lottery_log(d)

    def _render_guest_profile(self, parent):
        """Explain which features need an account without exposing edit controls."""
        panel = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12)
        panel.pack(fill="x", padx=18, pady=(28, 12))
        ctk.CTkLabel(panel, text="访客模式", text_color=TEXT,
                     font=ui_font(size=18, weight="bold")).pack(
            anchor="w", padx=18, pady=(18, 6))
        ctk.CTkLabel(
            panel,
            text="未登录时可以浏览游戏资料并同步目录。创建或登录云端账号后，才能使用积分商城、签到、评论、个人资料与收藏管理；账号数据可在其他设备继续使用。",
            text_color=MUTED, font=ui_font(size=12), wraplength=650,
            justify="left", anchor="w").pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkButton(
            panel, text="创建账号 / 登录", height=36, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
            font=ui_font(size=13), command=self.open_cloud_account).pack(
            fill="x", padx=18, pady=(0, 18))
    def open_leaderboard(self):
        """Open the two public, privacy-limited cloud leaderboards."""
        existing = getattr(self, "_leaderboard_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except tk.TclError:
                pass
        win = self._new_dialog("社区排行榜", "440x590")
        self._leaderboard_window = win
        ctk.CTkLabel(
            win, text="每榜仅显示前 30 名（不含管理员）；只展示昵称和榜单数值。",
            text_color=MUTED, font=ui_font(size=11), wraplength=390,
            justify="left").pack(anchor="w", padx=16, pady=(12, 7))
        tabs = ctk.CTkTabview(win)
        tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._leaderboard_tabs = tabs
        self._leaderboard_states = {
            "points": {"loading": True, "error": "", "entries": []},
            "wardrobe": {"loading": True, "error": "", "entries": []},
        }
        self._leaderboard_lists = {}
        for board, title in (("points", "云端积分榜"), ("wardrobe", "装扮榜")):
            tab = tabs.add(title)
            header = ctk.CTkFrame(tab, fg_color="transparent")
            header.pack(fill="x", padx=4, pady=(4, 2))
            ctk.CTkLabel(
                header,
                text=("按当前云端积分排序" if board == "points"
                      else "按已拥有装扮总数排序（头衔、头像框、名片框）"),
                text_color=MUTED, font=ui_font(size=10)).pack(side="left")
            ctk.CTkButton(
                header, text="刷新", width=62, height=25, corner_radius=7,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=10),
                command=lambda b=board: self._load_leaderboard(b)
            ).pack(side="right")
            listing = ctk.CTkScrollableFrame(tab, fg_color="transparent")
            listing.pack(fill="both", expand=True, padx=2, pady=(0, 4))
            self._leaderboard_lists[board] = listing
            self._render_leaderboard_board(board)
        self._load_leaderboard("points")
        self._load_leaderboard("wardrobe")

    def _load_leaderboard(self, board):
        if board not in ("points", "wardrobe"):
            return
        states = getattr(self, "_leaderboard_states", {})
        if board not in states:
            return
        states[board].update(loading=True, error="")
        self._render_leaderboard_board(board)
        self._run_cloud_action(
            "leaderboard_" + board,
            lambda b=board: slg_account.leaderboard(b, limit=30))

    def _render_leaderboard_board(self, board):
        listing = getattr(self, "_leaderboard_lists", {}).get(board)
        state = getattr(self, "_leaderboard_states", {}).get(board)
        if listing is None or state is None:
            return
        try:
            if not listing.winfo_exists():
                return
            for child in listing.winfo_children():
                child.destroy()
        except tk.TclError:
            return
        if state.get("loading") and not state.get("entries"):
            ctk.CTkLabel(listing, text="正在读取排行榜…", text_color=MUTED,
                         font=ui_font(size=12)).pack(anchor="w", padx=8, pady=12)
            return
        if state.get("error") and not state.get("entries"):
            ctk.CTkLabel(listing, text="排行榜暂时无法加载\n" + state["error"],
                         text_color=DANGER_TEXT, font=ui_font(size=11),
                         wraplength=330, justify="left").pack(
                anchor="w", padx=8, pady=(14, 6))
            ctk.CTkButton(
                listing, text="重试", height=28, fg_color=CHIP,
                text_color=TEXT, hover_color=CARD_HOVER,
                command=lambda b=board: self._load_leaderboard(b)).pack(
                fill="x", padx=8, pady=(0, 8))
            return
        entries = state.get("entries") or []
        if not entries:
            ctk.CTkLabel(listing, text="暂时还没有上榜数据。",
                         text_color=MUTED, font=ui_font(size=11)).pack(
                anchor="w", padx=8, pady=12)
            return
        for entry in entries:
            row = ctk.CTkFrame(listing, fg_color=CARD, corner_radius=8)
            row.pack(fill="x", padx=4, pady=3)
            rank = int(entry.get("rank") or 0)
            rank_color = {1: "#f0c56a", 2: "#c3cbd5", 3: "#c98663"}.get(rank, MUTED)
            ctk.CTkLabel(row, text=("%02d" % rank if rank > 0 else "—"),
                         width=34, text_color=rank_color,
                         font=ui_font(size=13, weight="bold")).pack(
                side="left", padx=(9, 4), pady=8)
            nickname = str(entry.get("nickname") or "未设置昵称")[:32]
            ctk.CTkLabel(row, text=nickname, text_color=TEXT,
                         font=ui_font(size=12), anchor="w").pack(
                side="left", fill="x", expand=True, padx=4, pady=8)
            score = int(entry.get("score") or 0)
            score_text = ("%s 积分" % score if board == "points"
                          else "%s 件装扮" % score)
            ctk.CTkLabel(row, text=score_text, text_color=ACCENT,
                         font=ui_font(size=11, weight="bold")).pack(
                side="right", padx=(4, 10), pady=8)
        if state.get("error"):
            ctk.CTkLabel(listing, text="刷新失败，显示上次已加载的数据。",
                         text_color=MUTED, font=ui_font(size=10)).pack(
                anchor="w", padx=8, pady=5)

    def _leaderboard_action_result(self, board, result, error):
        states = getattr(self, "_leaderboard_states", {})
        state = states.get(board)
        if state is None:
            return
        state["loading"] = False
        if error:
            state["error"] = str(error)
        elif isinstance(result, dict):
            entries = result.get("entries")
            if result.get("board") != board or not isinstance(entries, list):
                state["error"] = "服务器返回的排行榜格式不正确"
            else:
                cleaned = []
                for entry in entries[:30]:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        cleaned.append({
                            "rank": int(entry.get("rank") or len(cleaned) + 1),
                            "nickname": str(entry.get("nickname") or "未设置昵称"),
                            "score": max(0, int(entry.get("score") or 0)),
                        })
                    except (TypeError, ValueError):
                        continue
                state["entries"] = cleaned
                state["error"] = ""
        else:
            state["error"] = "服务器未返回排行榜数据"
        self._render_leaderboard_board(board)

    @staticmethod
    def _legacy_migration_status(cache):
        """Describe the old local balance without displaying it as spendable."""
        cache = cache if isinstance(cache, dict) else {}
        if cache.get("legacy_migration_error"):
            return "旧资产迁移暂未完成，请保留本机存档"
        migration = cache.get("legacy_migration")
        if not isinstance(migration, dict):
            return "正在确认本机旧资产的迁移状态"
        status = migration.get("status")
        if status in ("migrated", "already_migrated", "manual_approved",
                      "nothing_to_migrate", "already_completed_local"):
            return "本机旧资产已处理完成（迁移完成或无需迁移）"
        if status == "pending_manual_review":
            return "旧资产等待管理员核对，请保留本机存档"
        if status == "source_conflict":
            return "旧资产迁移来源待核对，本机存档尚未扣除"
        if status == "manual_rejected":
            return "旧资产迁移未通过，本机账本仍保留"
        return "本机旧资产迁移状态暂不可用，请保留本机存档"

    def _equipped_appearance_map(self):
        """Read both the new per-slot map and the old single namecard field."""
        me = getattr(self, "_cloud_me", {})
        values = getattr(self, "_cloud_appearances", None)
        if not isinstance(values, dict) and isinstance(me, dict):
            values = me.get("equipped_appearances")
        values = dict(values) if isinstance(values, dict) else {}
        legacy = (getattr(self, "_cloud_equipped_cosmetic", "")
                  or (me.get("equipped_cosmetic", "") if isinstance(me, dict) else ""))
        if legacy and not values.get("comment_frame"):
            values["comment_frame"] = legacy
        return values

    def _profile_balance_display(self):
        """Return cloud wallet UI only; never substitute the obsolete local balance."""
        try:
            current = slg_account.session()
        except slg_account.AccountError:
            return "云端积分\n读取失败", DANGER_TEXT
        if not current:
            return "云端积分\n登录后同步", MUTED
        live_balance = getattr(self, "_cloud_balance", None)
        if (getattr(self, "_cloud_balance_account_id", None)
                == current.get("account_id")
                and isinstance(live_balance, (int, float))):
            return "云端积分\n%d" % int(live_balance), ACCENT
        cache = getattr(self, "_cloud_me", {})
        if (isinstance(cache, dict)
                and cache.get("account_id") == current.get("account_id")
                and isinstance(cache.get("balance"), (int, float))):
            return "云端积分\n%d" % int(cache["balance"]), ACCENT
        return "云端积分\n正在读取…", MUTED

    def _store_cloud_balance(self, value):
        """Cache a fresh server wallet value against the current account only."""
        if not isinstance(value, (int, float)):
            return
        self._cloud_balance = int(value)
        try:
            current = slg_account.session()
        except slg_account.AccountError:
            return
        if not current:
            return
        account_id = current.get("account_id")
        self._cloud_balance_account_id = account_id
        cache = getattr(self, "_cloud_me", None)
        if isinstance(cache, dict) and cache.get("account_id") == account_id:
            cache["balance"] = self._cloud_balance
        label = getattr(self, "_profile_balance_label", None)
        if label is not None and label.winfo_exists():
            label.configure(text="云端积分\n%d" % self._cloud_balance,
                            text_color=ACCENT)

    def _profile_cloud_account(self, parent):
        """Keep the server wallet visibly separate from legacy local points."""
        box = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        box.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(box, text="云端账号", text_color=ACCENT,
                     font=ui_font(size=13, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 2))
        try:
            current = slg_account.session()
            cache = getattr(self, "_cloud_me", {})
            if current and cache.get("account_id") == current["account_id"]:
                is_developer = current["account_id"] == "developer"
                if is_developer:
                    status = ("开发者身份\n本月云端签到 %s 天 · 连续 %s 天 · %s\n%s" % (
                                  cache.get("signin_month_count", 0),
                                  cache.get("signin_streak", 0),
                                  "今天已签" if cache.get("signed_today") else "今天未签",
                                  self._legacy_migration_status(cache)))
                else:
                    status = ("账号 %s\n本月云端签到 %s 天 · 连续 %s 天 · %s\n%s" % (
                                  current["account_id"],
                                  cache.get("signin_month_count", 0),
                                  cache.get("signin_streak", 0),
                                  "今天已签" if cache.get("signed_today") else "今天未签",
                                  self._legacy_migration_status(cache)))
            else:
                status = ("正在关联开发者云端身份…" if self._is_admin_mode()
                          else "账号 %s · 正在读取账户资料…" % current["account_id"]
                          if current else "尚未创建云端账号")
        except slg_account.AccountError as exc:
            current, status = None, str(exc)
        self._cloud_account_label = ctk.CTkLabel(
            box, text=status, text_color=MUTED, font=ui_font(size=11),
            wraplength=330, justify="left")
        self._cloud_account_label.pack(anchor="w", padx=12, pady=(0, 8))
        actions = ctk.CTkFrame(box, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(0, 10))
        account_action = ("开发者身份" if self._is_admin_mode()
                          else "创建/登录", self.open_cloud_account)
        for name, callback in (account_action,
                               ("设备管理", self.open_cloud_devices)):
            ctk.CTkButton(actions, text=name, width=1, height=28, corner_radius=7,
                          fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                          font=ui_font(size=10), command=callback).pack(
                side="left", expand=True, fill="x", padx=(0, 5))
        if current and getattr(self, "_skip_profile_cloud_fetch_once", False):
            self._skip_profile_cloud_fetch_once = False
        elif current:
            self._run_cloud_action("me", slg_account.me)

    def _copy_referral_code(self, code):
        try:
            self.clipboard_clear()
            self.clipboard_append(str(code))
            self._set_progress("邀请码已复制")
        except tk.TclError:
            self._set_progress("复制失败，请手动复制邀请码")

    def _ensure_referral_context(self):
        """Load invite status independently of whether the profile is open."""
        try:
            current = slg_account.session()
        except slg_account.AccountError:
            current = None
        if not isinstance(current, dict) or not current.get("account_id"):
            return False
        account_id = str(current["account_id"])
        if account_id != getattr(self, "_profile_referral_account_id", None):
            self._profile_referral_state = None
            self._profile_referral_error = None
            self._profile_referral_feedback = ""
            self._profile_referral_input_value = ""
            self._profile_referral_submit_pending = False
            self._profile_referral_after_submit = False
            self._profile_referral_submit_error = None
            self._profile_referral_post_confirmed = False
            self._profile_referral_latest_fetch_id = None
            self._profile_referral_loading = False
            self._profile_referral_requests = {}
        self._profile_referral_account_id = account_id
        if account_id == "developer":
            self._profile_referral_unavailable = True
            self._profile_referral_error = "开发者账号不参与邀请码活动"
        elif not (getattr(self, "_profile_referral_loading", False)
                  or getattr(self, "_profile_referral_submit_pending", False)
                  or getattr(self, "_profile_referral_after_submit", False)):
            # Refresh cached invite totals each time the invite page is entered.
            # A request already in flight, including post-submit confirmation,
            # owns its own refresh and must not be repeated here.
            self._profile_referral_unavailable = False
            self._fetch_profile_referrals()
        return True

    def _render_invite_dialog(self):
        content = getattr(self, "_invite_dialog_content", None)
        expected_account = getattr(self, "_invite_dialog_account_id", None)
        if (content is None or expected_account !=
                getattr(self, "_profile_referral_account_id", None)):
            return
        try:
            if not content.winfo_exists():
                return
            entry = getattr(self, "_invite_referral_entry", None)
            if (entry is not None and entry.winfo_exists()
                    and not getattr(self, "_profile_referral_submit_pending", False)
                    and not getattr(self, "_profile_referral_after_submit", False)
                    and not getattr(self, "_profile_referral_post_confirmed", False)):
                self._profile_referral_input_value = entry.get()
            for child in content.winfo_children():
                child.destroy()
        except tk.TclError:
            return
        self._invite_referral_entry = None

        if expected_account == "developer":
            ctk.CTkLabel(content, text="开发者账号不参与邀请码活动。",
                         text_color=MUTED, font=ui_font(size=12),
                         wraplength=350, justify="left").pack(
                anchor="w", fill="x", padx=12, pady=10)
            return

        if getattr(self, "_profile_referral_unavailable", False):
            ctk.CTkLabel(
                content, text="当前服务器尚未启用邀请码功能；兑换码仍可照常使用。",
                text_color=MUTED, font=ui_font(size=12), wraplength=350,
                justify="left").pack(anchor="w", fill="x", padx=12, pady=10)
            return

        error = getattr(self, "_profile_referral_error", None)
        state = getattr(self, "_profile_referral_state", None)
        if error:
            ctk.CTkLabel(content, text=str(error), text_color=DANGER_TEXT,
                         font=ui_font(size=11), wraplength=350,
                         justify="left").pack(anchor="w", fill="x", padx=12,
                                               pady=(10, 4))
            ctk.CTkButton(
                content, text="刷新邀请状态", height=30, corner_radius=7,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=11), command=self._fetch_profile_referrals
            ).pack(anchor="w", padx=12, pady=(4, 10))
            return
        if not isinstance(state, dict):
            message = ("正在读取邀请状态…"
                       if getattr(self, "_profile_referral_loading", False)
                       else "暂时无法读取邀请状态")
            ctk.CTkLabel(content, text=message, text_color=MUTED,
                         font=ui_font(size=12), wraplength=350,
                         justify="left").pack(anchor="w", fill="x", padx=12,
                                               pady=(10, 4))
            if not getattr(self, "_profile_referral_loading", False):
                ctk.CTkButton(
                    content, text="重试", height=30, corner_radius=7,
                    fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                    font=ui_font(size=11), command=self._fetch_profile_referrals
                ).pack(anchor="w", padx=12, pady=(4, 10))
            return

        invite_code = str(state.get("invite_code") or "").strip()
        code_row = ctk.CTkFrame(content, fg_color=CARD, corner_radius=8)
        code_row.pack(fill="x", padx=10, pady=(10, 6))
        ctk.CTkLabel(code_row, text=invite_code or "邀请码暂不可用",
                     text_color=TEXT, font=ui_font(size=15, weight="bold"),
                     anchor="w", wraplength=230).pack(
            side="left", fill="x", expand=True, padx=10, pady=9)
        if invite_code:
            ctk.CTkButton(
                code_row, text="复制", width=58, height=27, corner_radius=7,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=10),
                command=lambda value=invite_code: self._copy_referral_code(value)
            ).pack(side="right", padx=7)

        try:
            invite_count = max(0, int(state.get("invite_count", 0)))
        except (TypeError, ValueError):
            invite_count = 0
        ctk.CTkLabel(
            content,
            text="有效邀请 %d 人；每次成功绑定后，邀请双方各得 100 积分。" % invite_count,
            text_color=MUTED, font=ui_font(size=11), wraplength=360,
            justify="left").pack(anchor="w", fill="x", padx=12, pady=(2, 6))
        earned = state.get("earned_titles")
        earned_ids = ({item for item in earned if isinstance(item, str)}
                      if isinstance(earned, list) else set())
        for need, title_id in slg_titles.REFERRAL_TITLE_STEPS:
            title = slg_titles.title_by_id(title_id) or {"name": title_id}
            if title_id in earned_ids:
                status, color = "已解锁", ACCENT
            elif invite_count >= need:
                status, color = "等待服务器同步", MUTED
            else:
                status, color = "还差 %d 人" % (need - invite_count), MUTED
            row = ctk.CTkFrame(content, fg_color=CHIP, corner_radius=7)
            row.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(row, text="%d 人 · %s" % (need, title["name"]),
                         text_color=TEXT, font=ui_font(size=11, weight="bold"),
                         anchor="w").pack(side="left", fill="x", expand=True,
                                           padx=9, pady=6)
            ctk.CTkLabel(row, text=status, text_color=color,
                         font=ui_font(size=10)).pack(side="right", padx=8)

        redeemed = bool(state.get("redeemed"))
        eligible = bool(state.get("eligible_to_redeem"))
        busy = (getattr(self, "_profile_referral_submit_pending", False)
                or getattr(self, "_profile_referral_after_submit", False)
                or getattr(self, "_profile_referral_loading", False))
        if redeemed:
            note = "此账号已绑定邀请码，不能再次更改。"
            color = ACCENT
        elif eligible:
            note = "仅新创建且尚未绑定的账号可输入一次邀请码。"
            color = MUTED
        else:
            note = "此账号不能绑定邀请码；只有新创建且未绑定的账号符合条件。"
            color = MUTED
        ctk.CTkLabel(content, text=note, text_color=color,
                     font=ui_font(size=11), wraplength=350,
                     justify="left").pack(anchor="w", fill="x", padx=12,
                                           pady=(7, 3))
        feedback = getattr(self, "_profile_referral_feedback", "")
        if feedback:
            ctk.CTkLabel(
                content, text=feedback,
                text_color=(DANGER_TEXT if getattr(
                    self, "_profile_referral_submit_error", None) else MUTED),
                font=ui_font(size=10), wraplength=350,
                justify="left").pack(anchor="w", fill="x", padx=12, pady=2)
        if eligible and not redeemed:
            form = ctk.CTkFrame(content, fg_color="transparent")
            form.pack(fill="x", padx=10, pady=(4, 10))
            self._invite_referral_entry = ctk.CTkEntry(
                form, height=34, placeholder_text="输入好友的邀请码",
                font=ui_font(size=11))
            self._invite_referral_entry.pack(side="left", fill="x",
                                              expand=True, padx=(0, 7))
            value = getattr(self, "_profile_referral_input_value", "")
            if value:
                self._invite_referral_entry.insert(0, value)
            self._invite_referral_entry.configure(
                state="disabled" if busy else "normal")
            ctk.CTkButton(
                form, text=("正在确认" if busy else "绑定一次"), width=82,
                height=34, corner_radius=7, fg_color=ACCENT,
                text_color=ON_ACCENT, hover_color=CARD_HOVER,
                font=ui_font(size=11, weight="bold"),
                state="disabled" if busy else "normal",
                command=lambda entry=self._invite_referral_entry:
                    self._submit_profile_referral(entry)).pack(side="right")

    def _fetch_profile_referrals(self):
        account_id = getattr(self, "_profile_referral_account_id", None)
        if not account_id or account_id == "developer":
            return
        serial = int(getattr(self, "_profile_referral_request_serial", 0)) + 1
        self._profile_referral_request_serial = serial
        self._profile_referral_requests = getattr(
            self, "_profile_referral_requests", {})
        self._profile_referral_requests[serial] = {
            "action": "me", "account_id": account_id}
        self._profile_referral_latest_fetch_id = serial
        self._profile_referral_error = None
        self._profile_referral_loading = True
        self._render_invite_dialog()
        self._run_cloud_action("referral_me:%d" % serial,
                               slg_account.referrals_me)

    def _submit_profile_referral(self, entry=None):
        if not self._require_regular_user_mode("\u7ed1\u5b9a\u9080\u8bf7\u7801"):
            return
        account_id = getattr(self, "_profile_referral_account_id", None)
        entry = entry or getattr(self, "_invite_referral_entry", None)
        if not account_id or entry is None:
            return
        code = str(entry.get() or "").strip()
        if not code:
            self._profile_referral_feedback = "请输入邀请码。"
            self._profile_referral_submit_error = "empty"
            self._render_invite_dialog()
            return
        if (getattr(self, "_profile_referral_submit_pending", False)
                or getattr(self, "_profile_referral_after_submit", False)
                or getattr(self, "_profile_referral_post_confirmed", False)):
            return
        serial = int(getattr(self, "_profile_referral_request_serial", 0)) + 1
        self._profile_referral_request_serial = serial
        self._profile_referral_requests = getattr(
            self, "_profile_referral_requests", {})
        self._profile_referral_requests[serial] = {
            "action": "redeem", "account_id": account_id}
        self._profile_referral_submit_pending = True
        self._profile_referral_after_submit = False
        self._profile_referral_submit_error = None
        self._profile_referral_feedback = "正在向服务器提交邀请码…"
        self._profile_referral_input_value = code
        self._render_invite_dialog()
        self._run_cloud_action(
            "referral_redeem:%d" % serial,
            lambda value=code: slg_account.redeem_referral(value))

    def _switch_identity_mode(self):
        if self._identity_switch_blocked():
            self._set_progress(
                "\u8bf7\u7b49\u5f53\u524d\u8d26\u53f7\u64cd\u4f5c\u5b8c\u6210\u540e\u518d\u5207\u6362\u8eab\u4efd")
            return
        if self._is_admin_mode():
            slg_account.set_developer_mode(False)
            slg_db.set_pref(self.conn, PREF_ACTIVE_IDENTITY_MODE, "user")
            self._reset_identity_state()
            self._set_progress("已切换到普通用户身份")
            self._redraw_identity_view()
            return
        self._connect_developer_identity(startup=False)

    def _connect_developer_identity(self, startup=False, force=False):
        if not self._has_local_admin_capability():
            if not startup:
                self._set_progress("本机没有管理员切换权限")
            return False
        if self._is_admin_mode() and not force:
            return True
        if self._identity_switch_pending or self._developer_cloud_login_pending:
            return False
        developer_key = slg_titles.dev_secret()
        if not developer_key:
            if startup:
                slg_db.set_pref(self.conn, PREF_ACTIVE_IDENTITY_MODE, "user")
            self._set_progress("本机未找到开发者密钥，保持当前身份")
            if not startup:
                messagebox.showwarning("管理员身份", "本机未找到开发者密钥，保持当前身份。",
                                       parent=self)
            return False
        self._identity_switch_pending = True
        self._developer_cloud_login_pending = True
        if not startup:
            self._redraw_identity_view()
        self._run_cloud_action(
            "identity_startup_admin" if startup else "identity_switch_admin",
            lambda key=developer_key: slg_account.developer_login(
                key, allow_inactive=True), identity_sensitive=False)
        return True

    def _reset_identity_state(self):
        """Invalidate user/account data and every in-flight response generation."""
        self._close_identity_scoped_dialogs()
        self._identity_generation += 1
        self._cloud_me = {}
        self._cloud_balance = None
        self._cloud_balance_account_id = None
        self._maintenance_reward_status = None
        self._maintenance_reward_fetching = False
        self._announcement_reward_prompted = False
        self._cloud_titles = set()
        self._cloud_equipped = slg_titles.DEFAULT_TITLE_ID
        self._cloud_equipped_cosmetic = ""
        self._cloud_appearances = {}
        self._profile_message_pending = False
        self._reset_quests_for_account()
        self._profile_referral_request_serial = (
            int(getattr(self, "_profile_referral_request_serial", 0)) + 1)
        self._profile_referral_requests = {}
        self._profile_referral_state = None
        self._profile_referral_error = None
        self._profile_referral_account_id = None
        self._profile_referral_loading = False
        self._profile_referral_unavailable = False
        self._refresh_announcement_badge()

    def _redraw_identity_view(self):
        mode = getattr(self, "_panel_mode", "game")
        if mode == "profile":
            self.open_profile()
        elif mode == "shop":
            self.open_shop()
        elif mode == "comments" and getattr(self, "selected", None):
            self.open_comments(self.selected)

    def _ensure_developer_cloud_identity(self, force=False):
        if not self._is_admin_mode():
            return False
        try:
            current = slg_account.session()
        except slg_account.AccountError:
            current = None
        if (not force and current
                and current.get("account_id") == "developer"):
            return True
        if self._developer_cloud_login_pending:
            return False
        developer_key = slg_titles.dev_secret()
        if not developer_key:
            self._set_progress("本机未找到开发者密钥，无法关联云端身份")
            return False
        self._developer_cloud_login_pending = True
        self._run_cloud_action(
            "admin_login", lambda: slg_account.developer_login(
                developer_key, allow_inactive=True), identity_sensitive=False)
        return False

    def _create_owner_account(self):
        if (not self._is_admin_mode() or self._owner_account_pending
                or self._owner_secret_dialog_pending):
            return
        developer_key = slg_titles.dev_secret()
        if not developer_key:
            self._set_progress("本机未找到开发者密钥，无法申请绑定账号")
            return
        try:
            ordinary = slg_account.session_for_mode(False)
        except slg_account.AccountError as exc:
            messagebox.showwarning("管理员绑定账号", str(exc), parent=self)
            return
        if ordinary and ordinary.get("account_id") != self._owner_account_bound_id:
            if not messagebox.askyesno(
                    "切换本机普通账号",
                    "这台电脑已保存另一个普通账号的设备会话。创建绑定账号后，普通用户模式会改用新账号；"
                    "旧账号仍可用登录密钥和恢复码重新登录。继续吗？", parent=self):
                return
        if not self._owner_account_operation_id:
            self._owner_account_operation_id = uuid.uuid4().hex
            slg_db.set_pref(self.conn, "admin.owner_account.pending_operation_id",
                            self._owner_account_operation_id)
        operation_id = self._owner_account_operation_id
        self._owner_account_pending = True
        self._run_cloud_action(
            "owner_account_create",
            lambda key=developer_key, op=operation_id:
                slg_account.create_owner_account(
                    key, operation_id=op,
                    device_label="SLGking 管理员工作站"),
            )

    def _reissue_owner_account(self):
        if (not self._is_admin_mode() or self._owner_account_pending
                or self._owner_secret_dialog_pending):
            return
        if not messagebox.askyesno(
                "重新签发绑定账号",
                "重新签发会立即撤销绑定账号原有的密钥和全部设备会话。\n\n"
                "只有明确需要更换凭据时才继续。", parent=self):
            return
        try:
            ordinary = slg_account.session_for_mode(False)
        except slg_account.AccountError as exc:
            messagebox.showwarning("管理员绑定账号", str(exc), parent=self)
            return
        if ordinary and ordinary.get("account_id") != self._owner_account_bound_id:
            if not messagebox.askyesno(
                    "切换本机普通账号",
                    "重新签发后，普通用户模式会改用管理员绑定账号；当前普通账号仍可用其登录密钥和恢复码登录。继续吗？",
                    parent=self):
                return
        developer_key = slg_titles.dev_secret()
        if not developer_key:
            self._set_progress("本机未找到开发者密钥，无法重新签发")
            return
        self._owner_account_pending = True
        self._run_cloud_action(
            "owner_account_reissue",
            lambda key=developer_key:
                slg_account.reissue_owner_account(key),
            )

    def _has_cloud_account(self):
        try:
            current = slg_account.session()
            return bool(current)
        except slg_account.AccountError as exc:
            self._set_progress(str(exc))
            # A damaged session file must never trigger a local balance debit.
            filename = ("cloud_admin_session.json" if self._is_admin_mode()
                        else "cloud_session.json")
            return os.path.exists(os.path.join(slg_db.app_dir(), filename))

    def _has_usable_cloud_session(self):
        try:
            current = slg_account.session()
            return bool(current)
        except slg_account.AccountError:
            return False

    def _require_personal_access(self, feature, cloud_only=False):
        """Gate personal writes for guests while keeping catalogue reads open."""
        if self._has_usable_cloud_session():
            return True
        if self._is_admin_mode():
            self._ensure_developer_cloud_identity()
            self._set_progress("正在重新验证管理员云端身份，请连接成功后重试")
            return False
        prompt = ("%s需要登录云端账号。访客仍可浏览游戏资料和同步目录。\n\n现在打开账号窗口吗？"
                  % feature)
        if messagebox.askyesno("需要云端账号", prompt, parent=self):
            self.open_cloud_account()
        return False

    def _run_cloud_action(self, kind, func, identity_sensitive=True):
        identity_generation = (self._identity_generation
                              if identity_sensitive else None)
        session_snapshot = None
        if identity_sensitive:
            try:
                session_snapshot = slg_account.session()
            except slg_account.AccountError:
                session_snapshot = None
        identity_account_id = (session_snapshot.get("account_id")
                               if isinstance(session_snapshot, dict) else None)

        def worker():
            with slg_account.use_session_snapshot(session_snapshot):
                try:
                    result, error = func(), None
                except slg_account.OwnerAccountAlreadyBound as exc:
                    result = {"already_bound": True,
                              "account_id": exc.account_id}
                    error = str(exc)
                except slg_account.AccountError as exc:
                    result, error = None, str(exc)
                except Exception as exc:  # noqa: BLE001 - always unblock the UI
                    # Keep the traceback locally; never expose request data.
                    try:
                        slg_util.log_crash(
                            type(exc), exc, exc.__traceback__,
                            os.path.join(slg_db.app_dir(), "crash.log"))
                    except Exception:  # noqa: BLE001 - logging may also fail
                        pass
                    result, error = None, "程序内部出错，请重试；若持续发生，请反馈错误日志"
            self.queue.put(("cloud_action", (
                kind, result, error, identity_generation, identity_account_id)))
        threading.Thread(target=worker, daemon=True).start()

    def _cloud_action_result(self, kind, result, error,
                             identity_generation=None, identity_account_id=None):
        if kind in ("identity_startup_admin", "identity_switch_admin"):
            startup = kind == "identity_startup_admin"
            self._identity_switch_pending = False
            self._developer_cloud_login_pending = False
            if error or not isinstance(result, dict) or result.get("account_id") != "developer":
                detail = error or "服务器未确认管理员身份"
                if startup:
                    slg_account.set_developer_mode(False)
                    slg_db.set_pref(self.conn, PREF_ACTIVE_IDENTITY_MODE, "user")
                self._set_progress("管理员登录失败，仍使用普通用户身份：" + detail)
                if not startup:
                    messagebox.showwarning(
                        "管理员身份", "无法验证管理员身份，已保持普通用户模式。\n\n" + detail,
                        parent=self)
                self._redraw_identity_view()
                return
            slg_account.set_developer_mode(True)
            slg_db.set_pref(self.conn, PREF_ACTIVE_IDENTITY_MODE, "admin")
            self._reset_identity_state()
            self._set_progress("已切换到管理员身份")
            self._redraw_identity_view()
            if getattr(self, "_panel_mode", None) not in ("profile", "shop"):
                self._run_cloud_action("me", slg_account.me)
            return

        if kind in ("owner_account_create", "owner_account_reissue"):
            self._owner_account_pending = False
            if isinstance(result, dict) and result.get("account_id"):
                self._owner_account_bound_id = str(result["account_id"])
                slg_db.set_pref(self.conn, "admin.owner_account.account_id",
                                self._owner_account_bound_id)
            if kind == "owner_account_create" and isinstance(result, dict) \
                    and result.get("already_bound"):
                self._owner_account_operation_id = None
                slg_db.set_pref(self.conn,
                                "admin.owner_account.pending_operation_id", "")
                self._set_progress("绑定普通账号已存在；没有自动重新签发")
                messagebox.showinfo(
                    "管理员绑定账号",
                    "服务器已存在绑定账号（%s）。如需更换密钥，请明确点击“重新签发”。"
                    % (self._owner_account_bound_id or "账号 ID 未返回"), parent=self)
                self._redraw_identity_view()
                return
            if error:
                self._set_progress("绑定账号操作失败：" + str(error))
                messagebox.showwarning("管理员绑定账号", str(error), parent=self)
                self._redraw_identity_view()
                return
            self._owner_account_operation_id = None
            slg_db.set_pref(self.conn,
                            "admin.owner_account.pending_operation_id", "")
            self._set_progress("管理员绑定的普通账号凭据已生成，请妥善保存")
            self._show_new_account_keys(
                result, rotated=(kind == "owner_account_reissue"),
                owner_bound=True)
            self._redraw_identity_view()
            return

        if kind == "login":
            self._cloud_login_pending = False
            login_button = getattr(self, "_cloud_login_button", None)
            if login_button is not None and login_button.winfo_exists():
                login_button.configure(state="normal")
            if error:
                self._set_progress(str(error))
                messagebox.showwarning(
                    "\u4e91\u7aef\u8d26\u53f7", str(error), parent=self)
                return
        if kind == "rotate":
            self._rotate_pending = False
            submit_button = getattr(self, "_rotate_submit_button", None)
            if submit_button is not None and submit_button.winfo_exists():
                submit_button.configure(state="normal")
            self._rotate_submit_button = None
            if error:
                self._set_progress(str(error))
                messagebox.showwarning(
                    "\u5bc6\u94a5\u8f6e\u6362", str(error), parent=self)
                return
            rotate_window = getattr(self, "_rotate_keys_window", None)
            self._rotate_keys_window = None
            if rotate_window is not None and rotate_window.winfo_exists():
                rotate_window.destroy()
            self._show_new_account_keys(result, rotated=True)
            return
        current_account_id = self._current_session_account_id()
        stale_identity = (
            identity_generation is not None
            and identity_generation != self._identity_generation
            and not (identity_account_id is not None
                     and identity_account_id == current_account_id))
        stale_account = (identity_account_id is not None
                         and identity_account_id != current_account_id)
        if stale_identity or stale_account:
            if kind in ("create", "login"):
                if error:
                    self._cloud_create_pending = False
                    self._set_progress("普通账号操作失败：" + str(error))
                    messagebox.showwarning("云端账号", str(error), parent=self)
                elif (not self._is_admin_mode()
                      and isinstance(result, dict)
                      and result.get("account_id") == current_account_id):
                    # The standard session slot matches the response again, so
                    # it is safe to apply this result after switching back.
                    self._cloud_action_result(
                        kind, result, error, self._identity_generation,
                        current_account_id)
                elif kind == "create" and isinstance(result, dict):
                    self._cloud_create_pending = False
                    self._show_new_account_keys(result)
                    self._set_progress(
                        "普通账号已创建；当前身份未切换，密钥仅显示这一次")
                else:
                    self._set_progress(
                        "普通账号操作已完成；当前仍保持另一身份，普通会话已单独保存")
                return
            if kind == "admin_login":
                self._developer_cloud_login_pending = False
            elif kind in ("developer_unlock_titles", "developer_unlock_appearances"):
                self._developer_title_unlock_pending = False
            elif kind == "lottery":
                self._lottery_request_active = False
                complete = getattr(self, "_lottery_request_complete", None)
                self._lottery_request_complete = None
                if callable(complete):
                    complete(error=("身份已切换；原账号抽奖仍属于原账号，请切回后查看"))
            return
        if (kind == "me" and isinstance(result, dict)
                and identity_account_id is not None
                and result.get("account_id")
                and result.get("account_id") != identity_account_id):
            return
        if kind.startswith("quest_"):
            try:
                action, generation = kind[len("quest_"):].rsplit("_", 1)
                if int(generation) != self._quest_generation:
                    return
            except (TypeError, ValueError):
                return
            if action == "status":
                self._quests_fetching = False
                if not self._has_quest_session():
                    self._quests_data = None
                    self._quests_error = None
                    self._render_quest_panel()
                    return
                if error:
                    if self._quest_endpoint_missing(error):
                        self._quests_supported = False
                        self._quests_data = None
                        self._quests_error = None
                    else:
                        self._quests_error = str(error)
                elif (isinstance(result, dict)
                      and isinstance(result.get("tasks"), list)):
                    self._quests_supported = True
                    self._quests_data = result
                    self._quests_error = None
                    self._quest_claiming = None
                else:
                    self._quests_error = "invalid quest response"
                self._render_quest_panel()
                return
            if action == "claim":
                if error:
                    self._quest_claiming = None
                    self._quests_error = str(error)
                    self._set_progress("\u4efb\u52a1\u9886\u53d6\u5931\u8d25\uff1a" + str(error))
                self._fetch_quests()
                return
            if action == "rating":
                if not error:
                    self._fetch_quests()
                return
        if kind == "admin_login":
            self._developer_cloud_login_pending = False
            if not self._is_admin_mode():
                return
            if error:
                label = getattr(self, "_developer_cloud_status_label", None)
                if label is not None and label.winfo_exists():
                    label.configure(text="云端连接失败：" + error,
                                    text_color=DANGER_TEXT)
                reward_label = getattr(self, "_announcement_reward_label", None)
                claim_button = getattr(self, "_announcement_claim_button", None)
                if reward_label is not None and reward_label.winfo_exists():
                    reward_label.configure(text="开发者云端身份连接失败，请重试后再领取活动积分。")
                if claim_button is not None and claim_button.winfo_exists():
                    claim_button.configure(
                        text="重试连接", state="normal",
                        command=lambda: self._ensure_developer_cloud_identity(force=True))
                self._set_progress("开发者云端身份连接失败：" + error)
                return
            self._reset_identity_state()
            self._set_progress("开发者密钥已关联固定云端身份，无需普通登录或恢复码")
            account_window = getattr(self, "_cloud_account_window", None)
            if account_window is not None and account_window.winfo_exists():
                account_window.destroy()
            mode = getattr(self, "_panel_mode", None)
            if mode in ("profile", "shop"):
                self._redraw_identity_view()
            else:
                self._run_cloud_action("me", slg_account.me)
            return
        if kind in ("developer_unlock_titles", "developer_unlock_appearances"):
            self._developer_title_unlock_pending = False
            if error:
                self._set_progress("云端解锁装扮失败：" + error)
                messagebox.showwarning("开发者装扮", error, parent=self)
                return
            if not self._is_admin_mode():
                self._set_progress("开发者身份已关闭，未应用云端头衔状态")
                return
            # `items` is the new complete inventory list; keep `titles` as a
            # compatibility fallback for older service deployments.
            self._cloud_titles = set(result.get("items") or result.get("titles") or [])
            self._cloud_titles.update(result.get("titles") or [])
            self._cloud_titles.add(slg_titles.DEFAULT_TITLE_ID)
            self._cloud_equipped = (result.get("equipped_title")
                                    or getattr(self, "_cloud_equipped", "")
                                    or slg_titles.DEFAULT_TITLE_ID)
            if isinstance(getattr(self, "_cloud_me", None), dict):
                self._cloud_me["titles"] = list(self._cloud_titles)
                self._cloud_me["owned_titles"] = list(self._cloud_titles)
                self._cloud_me["items"] = list(self._cloud_titles)
                self._cloud_me["equipped_title"] = self._cloud_equipped
            granted = result.get("granted")
            gained = len(granted) if isinstance(granted, list) else 0
            self._set_progress("开发者云端装扮已全部解锁（本次新增 %d 件）" % gained)
            messagebox.showinfo(
                "开发者特权", "已解锁全部云端装扮（本次新增 %d 件）" % gained,
                parent=self)
            if getattr(self, "_panel_mode", None) == "shop":
                self.open_shop()
            elif getattr(self, "_panel_mode", None) == "profile":
                self._skip_profile_cloud_fetch_once = True
                self.open_profile()
            self._refresh_open_wardrobe()
            return
        if kind == "feedback_submit":
            self._feedback_submission_pending = False
            form = getattr(self, "_feedback_form", None)
            if isinstance(form, dict):
                form["pending"] = False
                submit_button = form.get("submit")
                status = form.get("status")
                body = form.get("body")
                if submit_button is not None and submit_button.winfo_exists():
                    submit_button.configure(state="normal", text="提交反馈")
                if error:
                    if status is not None and status.winfo_exists():
                        status.configure(text=error, text_color=DANGER_TEXT)
                    self._set_progress("反馈提交失败：" + error)
                else:
                    feedback_id = result.get("id") if isinstance(result, dict) else None
                    receipt = ("反馈已收到（编号 %s），等待管理员审核。"
                               "奖励以审核结果为准。" % feedback_id
                               if feedback_id else
                               "反馈已收到，等待管理员审核。奖励以审核结果为准。")
                    if status is not None and status.winfo_exists():
                        status.configure(text=receipt, text_color=ACCENT)
                    if body is not None and body.winfo_exists():
                        body.delete("1.0", "end")
                    counter = form.get("counter")
                    if counter is not None and counter.winfo_exists():
                        counter.configure(text="0 / 500 字", text_color=MUTED)
                    self._set_progress("意见反馈已提交，等待管理员审核")
            return
        if kind.startswith(("referral_me:", "referral_redeem:")):
            action, _, serial_text = kind.partition(":")
            try:
                serial = int(serial_text)
            except (TypeError, ValueError):
                return
            requests = getattr(self, "_profile_referral_requests", {})
            request_info = requests.pop(serial, None)
            if not isinstance(request_info, dict):
                return
            if request_info.get("account_id") != getattr(
                    self, "_profile_referral_account_id", None):
                return
            if action == "referral_me":
                if serial != getattr(self, "_profile_referral_latest_fetch_id", None):
                    return
                self._profile_referral_loading = False
                if error:
                    self._profile_referral_error = str(error)
                    self._profile_referral_unavailable = (
                        "当前服务器尚未启用邀请码功能" in str(error))
                    self._render_invite_dialog()
                    return
                required = ("invite_code", "eligible_to_redeem", "redeemed",
                            "invite_count", "earned_titles")
                if (not isinstance(result, dict)
                        or any(key not in result for key in required)
                        or not isinstance(result.get("invite_code"), str)
                        or not result.get("invite_code", "").strip()
                        or not isinstance(result.get("eligible_to_redeem"), bool)
                        or not isinstance(result.get("redeemed"), bool)
                        or not isinstance(result.get("invite_count"), int)
                        or not isinstance(result.get("earned_titles"), list)
                        or not all(isinstance(title_id, str)
                                   for title_id in result.get("earned_titles", []))):
                    self._profile_referral_error = "服务器返回的邀请码状态格式不正确"
                    self._render_invite_dialog()
                    return
                self._profile_referral_unavailable = False
                self._profile_referral_error = None
                self._profile_referral_state = result
                if result.get("redeemed"):
                    self._profile_referral_post_confirmed = True
                    self._profile_referral_input_value = ""
                    if getattr(self, "_profile_referral_after_submit", False):
                        self._profile_referral_feedback = "服务器已确认绑定成功，奖励状态已同步。"
                        self._profile_referral_submit_error = None
                elif getattr(self, "_profile_referral_after_submit", False):
                    submit_error = getattr(
                        self, "_profile_referral_submit_error", None)
                    self._profile_referral_post_confirmed = False
                    if submit_error:
                        self._profile_referral_feedback = "绑定失败：%s" % submit_error
                    else:
                        self._profile_referral_feedback = "服务器未确认绑定，请核对邀请码后重试。"
                        self._profile_referral_submit_error = "unconfirmed"
                elif result.get("eligible_to_redeem") and not result.get("redeemed"):
                    self._profile_referral_post_confirmed = False
                self._profile_referral_after_submit = False
                self._render_invite_dialog()
                return

            self._profile_referral_submit_pending = False
            self._profile_referral_after_submit = True
            if error:
                self._profile_referral_submit_error = str(error)
                self._profile_referral_feedback = "绑定结果待确认，正在刷新服务器状态…"
            elif isinstance(result, dict) and result.get("redeemed") is True:
                self._profile_referral_post_confirmed = True
                self._profile_referral_input_value = ""
                self._profile_referral_submit_error = None
                self._profile_referral_feedback = "绑定已由服务器确认，正在刷新邀请状态…"
                self._run_cloud_action("me", slg_account.me)
            else:
                self._profile_referral_post_confirmed = False
                self._profile_referral_submit_error = "服务器未确认邀请码绑定结果"
                self._profile_referral_feedback = "服务器未确认绑定，正在刷新状态…"
            self._fetch_profile_referrals()
            return
        if kind == "me":
            if not error:
                self._cloud_balance = result.get("balance", 0)
                self._cloud_titles = set(result.get("titles") or [])
                self._cloud_equipped = result.get("equipped_title") or slg_titles.DEFAULT_TITLE_ID
                self._cloud_equipped_cosmetic = result.get("equipped_cosmetic") or ""
                self._cloud_appearances = result.get("equipped_appearances") or {}
                self._cloud_me = result
                self._cloud_me_day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
                self._store_cloud_balance(result.get("balance"))
                self._update_shop_signin_button()
                migration = result.get("legacy_migration") or {}
                if result.get("legacy_migration_error"):
                    self._set_progress("\u65e7\u8d44\u4ea7\u81ea\u52a8\u8fc1\u79fb\u6682\u672a\u5b8c\u6210\uff1b\u8bf7\u4fdd\u7559\u672c\u673a\u5b58\u6863\uff0c\u4e4b\u540e\u518d\u6b21\u6253\u5f00\u4e2a\u4eba\u4e2d\u5fc3\u4f1a\u91cd\u8bd5")
                elif migration.get("status") == "migrated":
                    self._set_progress("\u65e7\u8d44\u4ea7\u5df2\u8fc1\u79fb\uff1a\u4e91\u7aef\u589e\u52a0 %s \u79ef\u5206\uff0c\u8fc1\u5165 %s \u4e2a\u5934\u8854" % (
                        migration.get("points_added", 0), migration.get("titles_added", 0)))
                elif migration.get("status") == "pending_manual_review":
                    self._set_progress("\u65e7\u8d44\u4ea7\u5df2\u8f6c\u4ea4\u7ba1\u7406\u5458\u6838\u5bf9\uff1b\u4e91\u7aef\u5165\u8d26\u524d\u8bf7\u4fdd\u7559\u672c\u673a\u5b58\u6863")
                elif migration.get("status") == "source_conflict":
                    self._set_progress("\u65e7\u8d44\u4ea7\u8fc1\u79fb\u6765\u6e90\u51b2\u7a81\uff0c\u672c\u673a\u8d44\u4ea7\u672a\u6263\u9664\uff1b\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u6838\u5bf9")
                elif migration.get("status") == "manual_approved":
                    self._set_progress("\u7ba1\u7406\u5458\u5df2\u901a\u8fc7\u8fc1\u79fb\uff1a\u4e91\u7aef\u5165\u8d26 %s \u79ef\u5206\uff0c\u672c\u673a\u5df2\u6263\u9664\u65e7\u8d44\u4ea7" % migration.get("points_added", 0))
                elif migration.get("status") == "manual_rejected":
                    self._set_progress("\u65e7\u8d44\u4ea7\u8fc1\u79fb\u672a\u901a\u8fc7\uff0c\u672c\u673a\u8d26\u76ee\u4fdd\u7559")
                elif migration.get("status") in ("already_migrated", "already_completed_local"):
                    self._set_progress("\u8fd9\u4efd\u672c\u673a\u65e7\u8d44\u4ea7\u5df2\u5b8c\u6210\u8fc1\u79fb")
            label = getattr(self, "_cloud_account_label", None)
            if label is not None and label.winfo_exists():
                if error:
                    label.configure(text=error)
                else:
                    if result.get("account_id") == "developer":
                        label_text = (
                            "开发者身份\n本月云端签到 %s 天 · 连续 %s 天 · %s\n%s" % (
                                result.get("signin_month_count", 0),
                                result.get("signin_streak", 0),
                                "今天已签" if result.get("signed_today") else "今天未签",
                                self._legacy_migration_status(result)))
                    else:
                        label_text = (
                            "账号 %s\n本月云端签到 %s 天 · 连续 %s 天 · %s\n%s" % (
                                result.get("account_id", ""),
                                result.get("signin_month_count", 0),
                                result.get("signin_streak", 0),
                                "今天已签" if result.get("signed_today") else "今天未签",
                                self._legacy_migration_status(result)))
                    label.configure(text=label_text)
            balance_label = getattr(self, "_profile_balance_label", None)
            if balance_label is not None and balance_label.winfo_exists():
                if error:
                    balance_label.configure(text="云端积分\n读取失败",
                                            text_color=DANGER_TEXT)
                elif isinstance(result, dict):
                    balance = result.get("balance")
                    if isinstance(balance, (int, float)):
                        balance_label.configure(text="云端积分\n%d" % int(balance),
                                                text_color=ACCENT)
                    else:
                        balance_label.configure(text="云端积分\n数据异常",
                                                text_color=DANGER_TEXT)
            shop_label = getattr(self, "_shop_balance_label", None)
            if (not error and shop_label is not None and shop_label.winfo_exists()
                    and getattr(self, "_panel_mode", None) == "shop"):
                shop_label.configure(text="云端 %s 积分" % result.get("balance", 0))
                self._render_shop()
            if not error and getattr(self, "_panel_mode", None) == "profile":
                self._skip_profile_cloud_fetch_once = True
                self.open_profile()
            if not error:
                self._refresh_open_wardrobe()
            return
        if kind == "create":
            self._cloud_create_pending = False
            create_button = getattr(self, "_cloud_create_button", None)
            if create_button is not None and create_button.winfo_exists():
                active = self._has_usable_cloud_session()
                create_button.configure(
                    state="disabled" if active else "normal",
                    text="此设备已有账号" if active else "创建新账号")
            if error:
                for widget in (getattr(self, "_cloud_login_key_entry", None),
                               getattr(self, "_cloud_recovery_entry", None),
                               getattr(self, "_cloud_login_button", None)):
                    if widget is not None and widget.winfo_exists():
                        widget.configure(state="normal")
                self._set_progress(error)
                messagebox.showwarning("云端账号", error, parent=self)
                return
            account_window = getattr(self, "_cloud_account_window", None)
            if account_window is not None and account_window.winfo_exists():
                account_window.destroy()
        if kind == "create" and not error:
            self._reset_identity_state()
            self._cloud_me = {}
            self._maintenance_reward_status = None
            self._reset_quests_for_account()
            self._cloud_titles = set()
            self._cloud_equipped = slg_titles.DEFAULT_TITLE_ID
            self._cloud_equipped_cosmetic = ""
            self._cloud_appearances = {}
            self._cloud_balance_account_id = None
            self._show_new_account_keys(result)
            self.open_profile()
            self._fetch_maintenance_reward_status()
            return
        if kind == "login" and not error:
            self._reset_identity_state()
            self._cloud_me = {}
            self._maintenance_reward_status = None
            self._reset_quests_for_account()
            self._cloud_titles = set()
            self._cloud_equipped = slg_titles.DEFAULT_TITLE_ID
            self._cloud_equipped_cosmetic = ""
            self._cloud_appearances = {}
            self._cloud_balance_account_id = None
            self._set_progress("云端账号登录成功")
            self.open_profile()
            self._fetch_maintenance_reward_status()
            return
        if kind == "maintenance_reward_status":
            self._maintenance_reward_fetching = False
            self._maintenance_reward_status = (
                {"error": error} if error else result)
            self._render_announcement_reward()
            self._refresh_announcement_badge()
            self._maybe_prompt_announcement_reward()
            return
        if kind == "maintenance_reward_claim":
            if not error and isinstance(result, dict):
                status = getattr(self, "_maintenance_reward_status", None) or {}
                status = dict(status)
                status["claimed"] = bool(result.get("claimed", True))
                status["balance"] = result.get("balance", status.get("balance", 0))
                self._maintenance_reward_status = status
                self._store_cloud_balance(result.get(
                    "balance", getattr(self, "_cloud_balance", 0)))
                self._set_progress("全服活动奖励已领取，云端积分 +%d" %
                                   int(result.get("gained") or 0))
            elif error:
                self._set_progress(error)
            self._render_announcement_reward()
            self._refresh_announcement_badge()
            return
        if kind == "devices" and not error:
            self._show_cloud_devices(result)
            return
        if kind == "revoke" and not error:
            self._set_progress("设备已撤销")
            self.open_cloud_devices()
            return
        if kind in ("leaderboard_points", "leaderboard_wardrobe"):
            board = kind.rsplit("_", 1)[-1]
            self._leaderboard_action_result(board, result, error)
            return
        if kind == "connectivity_diagnostics":
            self._connectivity_diagnostics_pending = False
            widgets = getattr(self, "_connectivity_diagnostics_widgets", {})
            if error or not isinstance(result, dict):
                summary = "诊断未完成，请重试。"
                self._connectivity_diagnostics_share_text = ""
                status_widget = widgets.get("status")
                try:
                    if status_widget is not None and status_widget.winfo_exists():
                        status_widget.configure(text=summary, text_color=DANGER_TEXT)
                    run_button = widgets.get("run")
                    if run_button is not None and run_button.winfo_exists():
                        run_button.configure(text="重试诊断", state="normal")
                except tk.TclError:
                    pass
                return
            self._connectivity_diagnostics_share_text = str(
                result.get("share_text") or "")
            status_names = {"ok": "正常", "failed": "失败",
                            "warning": "兼容提示", "skipped": "未检查"}
            code_names = {
                "resolved": "DNS 解析成功", "dns_failed": "DNS 解析失败",
                "connected": "TCP 连接成功",
                "connection_refused": "连接被拒绝",
                "network_unreachable": "网络不可达", "timeout": "连接超时",
                "verified": "TLS 证书验证成功",
                "certificate_invalid": "TLS 证书验证失败",
                "tls_failed": "TLS 握手失败", "response_ok": "接口可访问",
                "valid_manifest": "目录接口正常",
                "invalid_manifest": "目录接口响应异常",
                "endpoint_missing": "旧服务器未提供此接口",
                "no_route": "没有可用连接路线",
            }
            route_names = {"direct": "直连", "system_proxy": "系统代理",
                           "verified_origin": "验证源站"}
            row_widgets = widgets.get("rows", {})
            checks = result.get("checks", {})
            for key, label in row_widgets.items():
                check = checks.get(key, {}) if isinstance(checks, dict) else {}
                status_text = status_names.get(check.get("status"), "未知")
                code_text = code_names.get(check.get("code"), "")
                route_text = route_names.get(check.get("route"), "")
                detail = " · ".join(part for part in (status_text, code_text,
                                                       route_text) if part)
                try:
                    if label.winfo_exists():
                        label.configure(text=detail, text_color=(
                            ACCENT if check.get("status") == "ok" else
                            DANGER_TEXT if check.get("status") == "failed" else MUTED))
                except tk.TclError:
                    pass
            status_widget = widgets.get("status")
            output = widgets.get("output")
            run_button = widgets.get("run")
            copy_button = widgets.get("copy")
            try:
                if status_widget is not None and status_widget.winfo_exists():
                    status_widget.configure(
                        text="诊断完成。摘要不包含账号凭据或服务器响应正文。",
                        text_color=ACCENT)
                if output is not None and output.winfo_exists():
                    output.configure(state="normal")
                    output.delete("1.0", "end")
                    output.insert("1.0", self._connectivity_diagnostics_share_text)
                    output.configure(state="disabled")
                if run_button is not None and run_button.winfo_exists():
                    run_button.configure(text="重新诊断", state="normal")
                if (copy_button is not None and copy_button.winfo_exists()
                        and self._connectivity_diagnostics_share_text):
                    copy_button.configure(state="normal")
            except tk.TclError:
                pass
            return
        if kind == "group":
            if not error:
                gained = int(result.get("gained") or 0)
                self._store_cloud_balance(
                    result.get("balance", getattr(self, "_cloud_balance", 0)))
                self._cloud_titles = set(result.get("titles") or [])
                if isinstance(getattr(self, "_cloud_me", None), dict):
                    self._cloud_me["balance"] = self._cloud_balance
                    self._cloud_me["titles"] = list(self._cloud_titles)
                message = ("群内每日码云端积分 +%d" % gained if gained else
                           "今天的群码积分已经领取过")
            else:
                message = error
            self._set_progress(message)
            label = getattr(self, "_redeem_feedback", None)
            if label is not None and label.winfo_exists():
                label.configure(text=message, text_color=ACCENT if not error else DANGER_TEXT)
            return
        if kind == "signin":
            if error:
                self._set_progress("签到失败：" + str(error))
                self._update_shop_signin_button()
                messagebox.showwarning("签到失败", str(error), parent=self)
                return
            gained = int(result.get("gained") or 0)
            bonus = int(result.get("bonus") or 0)
            self._store_cloud_balance(
                result.get("balance", getattr(self, "_cloud_balance", 0)))
            if isinstance(getattr(self, "_cloud_me", None), dict):
                self._cloud_me["signed_today"] = True
                for key in ("signin_month_count", "signin_streak", "signin_month_days"):
                    if key in result:
                        self._cloud_me[key] = result[key]
                signed_day = result.get("day")
                days = self._cloud_me.get("signin_month_days")
                if (isinstance(days, list) and isinstance(signed_day, str)
                        and signed_day[:7] == datetime.now(
                            timezone(timedelta(hours=8))).date().isoformat()[:7]
                        and signed_day not in days):
                    days.append(signed_day)
                self._cloud_me_day = signed_day
            self._update_shop_signin_button()
            self._fetch_quests(force=True)
            self._show_signin_result(gained=gained,
                                     already=bool(result.get("already")), dev=False,
                                     bonus=bonus)
            if self._panel_mode == "profile":
                self.open_profile()
            return
        if kind == "buy" and not error:
            self._store_cloud_balance(
                result.get("balance", getattr(self, "_cloud_balance", 0)))
            self._cloud_titles = set(result.get("titles") or [])
            self._set_progress("云端兑换成功")
            if self._panel_mode == "shop":
                self.open_shop()
            return
        if kind == "equip" and not error:
            self._cloud_equipped = result.get("equipped_title") or slg_titles.DEFAULT_TITLE_ID
            self._fetch_quests()
            reopen_wardrobe = bool(getattr(self, "_wardrobe_reopen_after_equip", False))
            self._wardrobe_reopen_after_equip = False
            if isinstance(getattr(self, "_cloud_me", None), dict):
                self._cloud_me["equipped_title"] = self._cloud_equipped
            if reopen_wardrobe:
                self.open_wardrobe()
            elif self._panel_mode == "profile":
                self.open_profile()
            return
        if kind == "equip_cosmetic" and not error:
            self._cloud_equipped_cosmetic = result.get("equipped_cosmetic") or ""
            self._fetch_quests()
            self._cloud_appearances = (result.get("equipped_appearances") or
                                       {"comment_frame": self._cloud_equipped_cosmetic})
            reopen_wardrobe = bool(getattr(self, "_wardrobe_reopen_after_equip", False))
            self._wardrobe_reopen_after_equip = False
            if isinstance(getattr(self, "_cloud_me", None), dict):
                self._cloud_me["equipped_cosmetic"] = self._cloud_equipped_cosmetic
                self._cloud_me["equipped_appearances"] = self._cloud_appearances
            if reopen_wardrobe:
                self.open_wardrobe()
            elif self._panel_mode == "shop":
                self.open_shop()
            elif self._panel_mode == "profile":
                self.open_profile()
            return
        if kind == "equip_appearance" and not error:
            self._fetch_quests()
            pending_slot = getattr(self, "_wardrobe_pending_slot", "comment_frame")
            pending_item = getattr(self, "_wardrobe_pending_item", "")
            appearances = result.get("equipped_appearances")
            if isinstance(appearances, dict):
                self._cloud_appearances = dict(appearances)
            else:
                self._cloud_appearances = self._equipped_appearance_map()
                if pending_item:
                    self._cloud_appearances[pending_slot] = pending_item
                else:
                    self._cloud_appearances.pop(pending_slot, None)
            self._cloud_equipped_cosmetic = (
                self._cloud_appearances.get("comment_frame") or "")
            reopen_wardrobe = bool(getattr(self, "_wardrobe_reopen_after_equip", False))
            self._wardrobe_reopen_after_equip = False
            if isinstance(getattr(self, "_cloud_me", None), dict):
                self._cloud_me["equipped_appearances"] = self._cloud_appearances
                self._cloud_me["equipped_cosmetic"] = self._cloud_equipped_cosmetic
            if reopen_wardrobe:
                self.open_wardrobe()
            elif self._panel_mode == "shop":
                self.open_shop()
            elif self._panel_mode == "profile":
                self.open_profile()
            return
        if kind == "profile" and not error:
            self._run_cloud_action("me", slg_account.me)
            self._set_progress("云端昵称已更新")
            return
        if kind == "profile_message":
            self._profile_message_pending = False
            save_button = getattr(self, "_wardrobe_message_save_button", None)
            if error:
                self._set_progress("名片寄语保存失败：" + str(error))
                if save_button is not None and save_button.winfo_exists():
                    save_button.configure(text="保存寄语")
                self._update_profile_message_counter()
                messagebox.showwarning("名片寄语", str(error), parent=self)
                return
            message = (result.get("profile_message", self._pending_profile_message)
                       if isinstance(result, dict) else self._pending_profile_message)
            if isinstance(getattr(self, "_cloud_me", None), dict):
                self._cloud_me["profile_message"] = message or ""
            self._set_progress("公开名片寄语已保存")
            self._refresh_open_wardrobe()
            return
        if kind == "lottery":
            complete = getattr(self, "_lottery_request_complete", None)
            self._lottery_request_active = False
            self._lottery_request_complete = None
            if error:
                self._set_progress("\u4e91\u7aef\u62bd\u5956\u7ed3\u679c\u5c1a\u672a\u786e\u8ba4\uff1a" + str(error))
                if complete is not None:
                    complete(error=error)
                else:
                    # The dialog may have been dismissed while the request was
                    # in flight. Keep its idempotency key for a safe retry and
                    # report the outcome without creating a new modal window.
                    label = getattr(self, "_lottery_status_label", None)
                    try:
                        if label is not None and label.winfo_exists():
                            label.configure(
                                text="上一笔结果尚未确认；可安全重试", text_color=MUTED)
                    except tk.TclError:
                        pass
                return
            self._store_cloud_balance(
                result.get("balance", getattr(self, "_cloud_balance", 0)))
            self._cloud_titles = set(result.get("titles") or [])
            prize = result.get("prize") or {}
            if isinstance(getattr(self, "_cloud_me", None), dict):
                self._cloud_me["draws_today"] = result.get("draws_today", 0)
                self._cloud_me["lottery_pity"] = (
                    0 if prize.get("kind") == "title" else
                    self._cloud_me.get("lottery_pity", 0) + 1)
            self._refresh_shop_balance()
            self._fetch_quests()
            if complete is not None:
                complete(prize=prize)
            else:
                # The original window was closed. Do not unexpectedly reopen
                # it when an asynchronous response arrives. Retire the saved
                # key only after the server has confirmed this exact request.
                pref_key = getattr(self, "_lottery_pending_pref_key", None)
                if pref_key:
                    try:
                        slg_db.set_pref(self.conn, pref_key, "")
                    except (sqlite3.Error, OSError):
                        pass
                    else:
                        self._lottery_pending_pref_key = None
                prize_text = self._lottery_label(prize)
                self._set_progress("云端抽奖已确认：" + prize_text)
                label = getattr(self, "_lottery_status_label", None)
                try:
                    if label is not None and label.winfo_exists():
                        label.configure(text="上一笔已确认：" + prize_text,
                                        text_color=TEXT)
                except tk.TclError:
                    pass
            return
        if kind in ("equip", "equip_cosmetic", "equip_appearance"):
            reopen_wardrobe = bool(getattr(self, "_wardrobe_reopen_after_equip", False))
            self._wardrobe_reopen_after_equip = False
            self._set_progress(error or "云端装扮设置未完成")
            messagebox.showwarning("个性装扮", error or "云端装扮设置未完成", parent=self)
            if reopen_wardrobe:
                self.open_wardrobe()
            return
        self._set_progress(error or "云端操作未完成")
        messagebox.showwarning("云端账号", error or "云端操作未完成", parent=self)

    def open_connection_diagnostics(self):
        """Show bounded, anonymous network checks for the account server."""
        existing = getattr(self, "_connection_diagnostics_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    return
            except tk.TclError:
                pass
        win = self._new_dialog("服务器连接诊断", "470x470")
        self._connection_diagnostics_window = win
        ctk.CTkLabel(
            win, text="服务器连接诊断", text_color=TEXT,
            font=ui_font(size=15, weight="bold")).pack(
                anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            win, text="只发送匿名只读请求，不会读取或上传账号密钥、恢复码和本机路径。",
            text_color=MUTED, font=ui_font(size=11), wraplength=420,
            justify="left").pack(anchor="w", padx=16, pady=(0, 10))
        checks_frame = ctk.CTkFrame(win, fg_color="transparent")
        checks_frame.pack(fill="x", padx=12)
        row_labels = {}
        for key, title in (("dns", "DNS 解析"), ("tcp", "TCP 连接"),
                           ("tls", "TLS 证书"), ("healthz", "服务状态接口"),
                           ("manifest", "目录版本接口")):
            row = ctk.CTkFrame(checks_frame, fg_color=CARD, corner_radius=7)
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=title, text_color=TEXT,
                         font=ui_font(size=11), anchor="w").pack(
                side="left", padx=9, pady=7)
            value = ctk.CTkLabel(row, text="尚未检查", text_color=MUTED,
                                 font=ui_font(size=10), anchor="e",
                                 wraplength=255, justify="right")
            value.pack(side="right", padx=9, pady=7)
            row_labels[key] = value
        status = ctk.CTkLabel(win, text="点击下方按钮开始检查。",
                              text_color=MUTED, font=ui_font(size=11),
                              wraplength=420, justify="left")
        status.pack(anchor="w", fill="x", padx=16, pady=(8, 2))
        output = ctk.CTkTextbox(win, height=92, wrap="word",
                                fg_color=CARD, text_color=TEXT,
                                font=ui_font(size=10))
        output.pack(fill="x", padx=16, pady=(4, 8))
        output.insert("1.0", "诊断摘要将在这里显示。")
        output.configure(state="disabled")

        def run():
            if getattr(self, "_connectivity_diagnostics_pending", False):
                return
            self._connectivity_diagnostics_pending = True
            status.configure(text="正在检查 DNS、网络连接和服务器接口…")
            button.configure(state="disabled", text="正在检查…")
            self._run_cloud_action(
                "connectivity_diagnostics",
                lambda: slg_account.diagnose_connectivity(timeout=2.0))

        def close():
            if getattr(self, "_connection_diagnostics_window", None) is win:
                self._connection_diagnostics_window = None
            win.destroy()

        button = ctk.CTkButton(
            win, text="开始诊断", height=32, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT,
            hover_color=CARD_HOVER, command=run)
        button.pack(fill="x", padx=16, pady=(2, 5))
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 12))
        copy_button = ctk.CTkButton(
            row, text="复制摘要", height=29, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            state="disabled", command=lambda: self._copy_value(
                getattr(self, "_connectivity_diagnostics_share_text", "")))
        copy_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            row, text="关闭", height=29, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            command=close).pack(side="right", fill="x", expand=True,
                                padx=(4, 0))
        self._connectivity_diagnostics_widgets = {
            "window": win, "rows": row_labels, "status": status,
            "output": output, "run": button, "copy": copy_button,
        }
        win._slg_close_guard = close
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _event: (close(), "break")[1])

    def open_cloud_account(self):
        if getattr(self, "_owner_secret_dialog_pending", False):
            self._set_progress("\u8bf7\u5148\u4fdd\u5b58\u5f53\u524d\u4e00\u6b21\u6027\u5bc6\u94a5\uff0c\u518d\u7ba1\u7406\u8d26\u53f7")
            return
        if self._is_admin_mode():
            win = self._new_dialog("开发者云端身份", "390x275")
            self._cloud_account_window = win
            identity_context = win._slg_identity_context
            ctk.CTkLabel(
                win,
                text="开发者密钥就是这份云端身份的唯一凭据。此身份会自动关联到固定账号，不需要创建普通账号、登录密钥或恢复码。",
                text_color=TEXT, font=ui_font(size=12), wraplength=345,
                justify="left").pack(anchor="w", padx=18, pady=(18, 10))
            try:
                current = slg_account.session()
            except slg_account.AccountError:
                current = None
            connected = bool(current and current.get("account_id") == "developer")
            self._developer_cloud_status_label = ctk.CTkLabel(
                win, text=("开发者云端身份已连接。" if connected else
                           "正在用本机开发者密钥关联云端身份。"),
                text_color=ACCENT if connected else MUTED,
                font=ui_font(size=11))
            self._developer_cloud_status_label.pack(
                anchor="w", padx=18, pady=(0, 12))
            ctk.CTkButton(
                win, text="重新连接", height=32,
                command=lambda: (self._require_identity_context(
                    identity_context, "\u8fde\u63a5\u7ba1\u7406\u5458\u8d26\u53f7")
                and self._ensure_developer_cloud_identity(force=True))
            ).pack(fill="x", padx=18)
            ctk.CTkButton(
                win, text="诊断服务器连接…", height=30, fg_color=CHIP,
                text_color=TEXT, command=self.open_connection_diagnostics
            ).pack(fill="x", padx=18, pady=(8, 0))
            ctk.CTkButton(
                win, text="关闭", height=28, fg_color=CHIP, text_color=TEXT,
                command=win.destroy).pack(fill="x", padx=18, pady=(8, 14))
            self._ensure_developer_cloud_identity()
            return
        win = self._new_dialog("云端账号", "430x470")
        self._cloud_account_window = win
        identity_context = win._slg_identity_context
        active = self._has_cloud_account()
        pending = bool(getattr(self, "_cloud_create_pending", False)
                       or getattr(self, "_cloud_login_pending", False))
        ctk.CTkLabel(win, text="创建账号或登录已有账号", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(
            anchor="w", padx=18, pady=(18, 4))
        ctk.CTkLabel(win, text="新账号会给你登录密钥和恢复码。请分别妥善保存；软件不会替你保存。",
                     text_color=MUTED, font=ui_font(size=11), wraplength=370,
                     justify="left").pack(anchor="w", padx=18, pady=(0, 12))
        def create_new_account():
            if not self._require_identity_context(
                    identity_context, "\u521b\u5efa\u8d26\u53f7", require_regular=True):
                return
            if self._cloud_create_pending or self._cloud_login_pending:
                return
            if self._has_usable_cloud_session():
                messagebox.showinfo("云端账号", "此设备已经登录账号；如需切换，请先完成登录流程。",
                                    parent=win)
                return
            if not messagebox.askyesno(
                    "确认创建独立账号",
                    "每次创建都会生成一份全新的账号身份，和你以前创建的账号互不关联，也不能合并。\n\n"
                    "如果你已经有账号，请使用下方登录。仍要创建新账号吗？",
                    parent=win):
                return
            if not self._require_identity_context(
                    identity_context, "\u521b\u5efa\u8d26\u53f7", require_regular=True):
                return
            self._cloud_create_pending = True
            create_button.configure(state="disabled", text="正在创建…")
            self._cloud_create_button = create_button
            for widget in (login_key, recovery, login_button):
                widget.configure(state="disabled")
            self._run_cloud_action("create", slg_account.create)
        create_button = ctk.CTkButton(
            win, text=("正在创建…" if pending else
                      "此设备已有账号" if active else "创建新账号"), height=34,
            state="disabled" if active or pending else "normal",
            command=create_new_account)
        self._cloud_create_button = create_button
        create_button.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkLabel(win, text="已有账号：输入登录密钥", text_color=TEXT,
                     font=ui_font(size=12)).pack(anchor="w", padx=18)
        login_key = ctk.CTkEntry(win, placeholder_text="登录密钥", show="•")
        login_key.pack(fill="x", padx=18, pady=(4, 9))
        ctk.CTkLabel(win, text="新设备需恢复码；此设备保留受信任凭证时可留空",
                     text_color=MUTED, font=ui_font(size=11)).pack(anchor="w", padx=18)
        recovery = ctk.CTkEntry(win, placeholder_text="恢复码（新设备必填）", show="•")
        recovery.pack(fill="x", padx=18, pady=(4, 12))
        if pending:
            login_key.configure(state="disabled")
            recovery.configure(state="disabled")
        def sign_in():
            if not self._require_identity_context(
                    identity_context, "\u767b\u5f55\u8d26\u53f7", require_regular=True):
                return
            if self._cloud_create_pending or self._cloud_login_pending:
                return
            key, code = login_key.get().strip(), recovery.get().strip()
            if not key:
                messagebox.showwarning("云端账号", "请输入登录密钥", parent=win)
                return
            if self._has_usable_cloud_session() and code and not messagebox.askyesno(
                    "切换账号", "使用恢复码可能切换到另一个账号，本机原账号会话将被替换。"
                    "请确认已保存原账号的登录密钥和恢复码。继续吗？", parent=win):
                return
            if not self._require_identity_context(
                    identity_context, "\u767b\u5f55\u8d26\u53f7", require_regular=True):
                return
            self._cloud_login_pending = True
            if login_button.winfo_exists():
                login_button.configure(state="disabled")
            self._run_cloud_action("login", lambda: slg_account.login(key, code or None))
        login_button = ctk.CTkButton(
            win, text="登录", height=34, command=sign_in,
            state="disabled" if pending else "normal")
        self._cloud_login_key_entry = login_key
        self._cloud_recovery_entry = recovery
        self._cloud_login_button = login_button
        login_button.pack(fill="x", padx=18)
        ctk.CTkButton(
            win, text="诊断服务器连接…", height=29, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=11), command=self.open_connection_diagnostics
        ).pack(fill="x", padx=18, pady=(8, 0))
        if active:
            def local_logout():
                if not self._require_identity_context(
                        identity_context, "\u9000\u51fa\u8d26\u53f7", require_regular=True):
                    return
                if not messagebox.askyesno(
                        "退出本机账号", "这会清除本机受信任设备凭证。再次登录需要登录密钥"
                        "和恢复码；请确认已分别保存两串密钥。继续吗？", parent=win):
                    return
                slg_account.forget_session()
                self._reset_identity_state()
                self.open_profile()
            ctk.CTkButton(win, text="退出本机账号（清除本机凭证）", height=28,
                          fg_color=CHIP, text_color=TEXT,
                          command=local_logout).pack(fill="x", padx=18, pady=(12, 0))

    def _show_new_account_keys(self, data, rotated=False, owner_bound=False):
        win = self._new_dialog("请保存云端账号密钥", "460x360")
        self._owner_secret_dialog_pending = True
        win._slg_credential_dialog = True
        def confirm_close():
            if not win.winfo_exists():
                return
            if messagebox.askyesno(
                    "关闭前确认",
                    "请确认你已经分别保存了登录密钥和恢复码。关闭后这两串密钥不会再次显示，丢失后无法找回。\n\n"
                    "确定现在关闭吗？",
                    parent=win):
                self._owner_secret_dialog_pending = False
                win.destroy()
                if getattr(self, "_panel_mode", None) == "profile":
                    self._redraw_identity_view()
        win._slg_close_guard = confirm_close
        win.protocol("WM_DELETE_WINDOW", confirm_close)
        win.bind("<Escape>", lambda e: (confirm_close(), "break")[1])
        ctk.CTkLabel(win, text="密钥只显示这一次，请分别保存", text_color=ACCENT,
                     font=ui_font(size=16, weight="bold")).pack(
            anchor="w", padx=16, pady=(16, 8))
        ctk.CTkLabel(
            win,
            text=("密钥已重新签发：之前的登录密钥、恢复码和设备会话已失效。"
                  if rotated else
                  "管理员绑定的普通账号已创建。请把两串密钥保存在安全位置。"
                  if owner_bound else
                  "新账号创建成功。请把两串密钥保存在安全位置。"),
            text_color=MUTED, font=ui_font(size=11), wraplength=420,
            justify="left").pack(anchor="w", padx=16, pady=(0, 4))
        if data.get("session_error"):
            ctk.CTkLabel(win, text=data["session_error"], text_color=DANGER_TEXT,
                         wraplength=420, justify="left", font=ui_font(size=11)).pack(
                anchor="w", padx=16)
        ctk.CTkLabel(win, text="账号 ID：%s" % data.get("account_id", ""),
                     text_color=MUTED, font=ui_font(size=11)).pack(anchor="w", padx=16)
        for title, key in (("登录密钥", "login_key"), ("恢复码", "recovery_code")):
            value = data.get(key, "")
            ctk.CTkLabel(win, text=title, text_color=TEXT,
                         font=ui_font(size=12)).pack(anchor="w", padx=16, pady=(10, 2))
            row = ctk.CTkFrame(win, fg_color="transparent")
            row.pack(fill="x", padx=16)
            field = ctk.CTkEntry(row, font=ui_font(size=11))
            field.insert(0, value)
            field.configure(state="readonly")
            field.pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="复制", width=50,
                          command=lambda v=value: self._copy_value(v)).pack(
                side="right", padx=(6, 0))
        ctk.CTkLabel(win, text="遗失登录密钥或恢复码后，其他设备可能无法登录。",
                     text_color=DANGER_TEXT, font=ui_font(size=11)).pack(
            anchor="w", padx=16, pady=(12, 0))
        ctk.CTkButton(
            win, text="关闭（确认已保存）", height=32, corner_radius=8,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=12), command=confirm_close).pack(
            fill="x", padx=16, pady=(12, 12))

    def open_cloud_devices(self):
        if not self._require_regular_user_mode("\u8bbe\u5907\u7ba1\u7406"):
            return
        if not self._require_personal_access("管理受信任设备", cloud_only=True):
            return
        self._run_cloud_action("devices", slg_account.devices)
        self._set_progress("正在读取已信任设备…")

    def _show_cloud_devices(self, data):
        win = self._new_dialog("\u5df2\u4fe1\u4efb\u8bbe\u5907", "420x400")
        identity_context = win._slg_identity_context
        ctk.CTkLabel(win, text="\u5df2\u4fe1\u4efb\u8bbe\u5907", text_color=TEXT,
                     font=ui_font(size=15, weight="bold")).pack(
            anchor="w", padx=16, pady=(14, 8))
        rows = data.get("devices", []) if isinstance(data, dict) else []
        box = ctk.CTkScrollableFrame(win, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=12)
        for row in rows:
            device_id = row.get("id") or row.get("device_id")
            item = ctk.CTkFrame(box, fg_color=CARD)
            item.pack(fill="x", pady=3)
            ctk.CTkLabel(item, text="%s  %s" % (
                row.get("label") or str(device_id)[:12],
                date.fromtimestamp(row.get("last_used")).isoformat()
                if isinstance(row.get("last_used"), (int, float)) else ""),
                text_color=TEXT, font=ui_font(size=11)).pack(side="left", padx=8)
            if device_id:
                def revoke(did=device_id):
                    if not self._require_identity_context(
                            identity_context, "\u8bbe\u5907\u7ba1\u7406", require_regular=True):
                        return
                    self._run_cloud_action(
                        "revoke", lambda: slg_account.revoke_device(did))
                ctk.CTkButton(item, text="\u64a4\u9500", width=52, height=25,
                              command=revoke).pack(side="right", padx=8, pady=5)
        ctk.CTkButton(
            win, text="\u8f6e\u6362\u5bc6\u94a5",
            command=lambda: self.open_rotate_keys(identity_context)).pack(
                fill="x", padx=12, pady=10)

    def open_rotate_keys(self, expected_context=None):
        if getattr(self, "_rotate_pending", False):
            self._set_progress("\u5bc6\u94a5\u8f6e\u6362\u5df2\u5728\u8fdb\u884c\uff0c\u8bf7\u7b49\u5f85\u7ed3\u679c")
            return
        if getattr(self, "_owner_secret_dialog_pending", False):
            self._set_progress("\u8bf7\u5148\u4fdd\u5b58\u5f53\u524d\u4e00\u6b21\u6027\u5bc6\u94a5")
            return
        if expected_context is not None and not self._require_identity_context(
                expected_context, "\u8f6e\u6362\u5bc6\u94a5", require_regular=True):
            return
        win = self._new_dialog("\u8f6e\u6362\u8d26\u53f7\u5bc6\u94a5", "420x270")
        self._rotate_keys_window = win
        if expected_context is not None:
            win._slg_identity_context = expected_context
        identity_context = win._slg_identity_context
        ctk.CTkLabel(
            win, text="\u8f93\u5165\u5f53\u524d\u767b\u5f55\u5bc6\u94a5\u4ee5\u66f4\u6362\u8d26\u53f7\u5bc6\u94a5",
            text_color=TEXT, font=ui_font(size=13)).pack(
                anchor="w", padx=16, pady=(16, 8))
        key = ctk.CTkEntry(win, placeholder_text="\u5f53\u524d\u767b\u5f55\u5bc6\u94a5", show="*")
        key.pack(fill="x", padx=16, pady=5)
        def rotate():
            if self._rotate_pending:
                return
            if not self._require_identity_context(
                    identity_context, "\u8f6e\u6362\u5bc6\u94a5", require_regular=True):
                return
            login_key = key.get()
            key.delete(0, "end")
            self._rotate_pending = True
            submit_button.configure(state="disabled")
            self._run_cloud_action(
                "rotate", lambda: slg_account.rotate(login_key))
        submit_button = ctk.CTkButton(
            win, text="\u786e\u8ba4\u8f6e\u6362", command=rotate)
        self._rotate_submit_button = submit_button
        submit_button.pack(fill="x", padx=16, pady=14)

    def pick_avatar(self):
        """开发者专用入口：挑一张图当头像。普通用户没有入口（见 open_profile）。"""
        path = filedialog.askopenfilename(
            title="选择头像", parent=self,
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp"),
                       ("所有文件", "*.*")])
        if not path:
            return
        ok, msg = self.set_avatar_from_file(path)
        if ok:
            self.open_profile()
        else:
            messagebox.showwarning("头像", msg, parent=self)

    def clear_avatar(self):
        try:
            os.remove(slg_db.avatar_path())
        except OSError:
            pass
        self.open_profile()

    def set_avatar_from_file(self, path):
        """把一张图存成头像，返回 (ok, msg)。

        目前只有开发者特权用户能走到这里（入口在 open_profile），但这个方法本身
        不检查权限 —— 将来要开放普通用户上传时，挂个按钮上来就行。
        """
        try:
            img = Image.open(path).convert("RGBA")
        except Exception as exc:  # noqa: BLE001 - any unreadable file is a message
            return (False, "这张图打不开：%s" % exc)
        try:
            # 256 是给高 DPI 留的余量：界面上最大用到 84pt，×2 也才 168。
            circle_avatar(img, 256).save(slg_db.avatar_path())
        except OSError as exc:
            return (False, "头像写不进去：%s" % exc)
        return (True, "头像已更新")

    def _profile_lottery_log(self, parent):
        """最近抽奖：一行一条流水。没有记录时整块不画，省得面板挂着一条空标题。"""
        cloud_mode = self._has_cloud_account()
        rows = (getattr(self, "_cloud_me", {}).get("recent_lottery_history", [])[:5]
                if cloud_mode else slg_titles.lottery_history(self.conn)[:5])
        if not rows:
            return
        self._profile_section(parent, "云端最近抽奖" if cloud_mode else "最近抽奖",
                              "今天 %d 次" % getattr(self, "_cloud_me", {}).get("draws_today", 0)
                              if cloud_mode else "共 %d 次" % slg_db.lottery_count(self.conn))
        box = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8)
        box.pack(fill="x", padx=16)
        for i, row in enumerate(rows):
            line = ctk.CTkFrame(box, fg_color="transparent")
            line.pack(fill="x", padx=10, pady=(8 if i == 0 else 2,
                                               8 if i == len(rows) - 1 else 2))
            ctk.CTkLabel(line, text=row.get("day", ""), text_color=MUTED,
                         font=ui_font(size=10)).pack(side="left")
            prize = dict(row)
            if cloud_mode and prize.get("kind") == "points":
                prize["value"] = int(prize.get("value") or 0)
            ctk.CTkLabel(
                line, text=slg_titles.lottery_prize_text(prize),
                text_color=(ACCENT if row.get("kind") == "title" else TEXT),
                font=ui_font(size=11)).pack(side="right")

    def _makeup_prompt(self, day):
        """日历点名补签：确认 → 下单 → 原地刷新个人面板。

        只在日历上「今天之前且未签到」的格子上挂这个回调，所以这里不再重算一遍
        能不能补 —— 但 slg_titles 会再校验一次（积分、日期都可能在面板开着的时候
        被别处改动），拒绝理由直接透给用户。
        """
        if not self._require_personal_access("补签"):
            return
        if self._has_cloud_account():
            messagebox.showinfo("云端补签", "云端补签暂未开放；本机旧档日历不计入云端账号。",
                                parent=self)
            return
        today = date.today()
        target = "%04d-%02d-%02d" % (today.year, today.month, day)
        problem = slg_titles.makeup_problem(self.conn, target)
        if problem:
            messagebox.showinfo("补签", problem, parent=self)
            return
        cost = slg_titles.MAKEUP_CARD_COST
        if not messagebox.askyesno(
                "补签", "补签 %s？\n消耗 %d 积分。" % (target, cost),
                parent=self):
            return
        ok, msg, bonus = slg_titles.buy_makeup_card(self.conn, target=target)
        if not ok:
            messagebox.showwarning("补签失败", msg, parent=self)
            return
        if bonus:
            msg += "，累签奖励 +%d 积分" % bonus
        messagebox.showinfo("补签成功", msg, parent=self)
        self.open_profile()

    @staticmethod
    def _profile_cells(parent, cells):
        """一行等宽统计卡：上面数字、下面标签。第三项可选 command，给了就让整卡可点。"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 2))
        for i, item in enumerate(cells):
            label_text, value = item[0], item[1]
            command = item[2] if len(item) > 2 else None
            cell = ctk.CTkFrame(row, fg_color=CARD, corner_radius=8)
            cell.pack(side="left", expand=True, fill="x",
                      padx=(0, 0 if i == len(cells) - 1 else 6))
            if command is not None:
                cell.configure(cursor="hand2")
                cell.bind("<Button-1>", lambda e, c=command: c())
            ctk.CTkLabel(cell, text=str(value), text_color=ACCENT,
                         font=ui_font(size=17, weight="bold")).pack(pady=(7, 0))
            ctk.CTkLabel(cell, text=label_text, text_color=MUTED,
                         font=ui_font(size=10)).pack(pady=(0, 6))

    @staticmethod
    def _profile_section(parent, title, right=""):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        # (12, 6) rather than (18, 6): the profile dashboard is taller than the
        # smallest window the app allows, and every section header's slack comes
        # out of the same budget. Six saved here is what keeps 操作 above the fold.
        head.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(head, text=title, text_color=TEXT,
                     font=ui_font(size=12, weight="bold")).pack(side="left")
        if right:
            ctk.CTkLabel(head, text=right, text_color=MUTED,
                         font=ui_font(size=11)).pack(side="right")

    @staticmethod
    def _tamper_notice(parent, report):
        """账本封印对不上时的那块警告。

        上面的余额已经只算到断链为止了，这里把被排掉的部分明说 —— 不然用户只
        会看到积分莫名变少。措辞克制：不指控，只说程序看到的事实。
        """
        box = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10,
                           border_width=1,
                           border_color=_mix(BG, DANGER_TEXT, 0.55))
        box.pack(fill="x", padx=16, pady=(12, 0))
        ctk.CTkLabel(box, text="存档被外部修改", text_color=DANGER_TEXT,
                     font=ui_font(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 2))
        lines = []
        if report["points"]:
            lines.append("多出的 %d 积分未计入余额" % report["points"])
        if report["titles"]:
            names = []
            for tid in report["titles"]:
                t = slg_titles.title_by_id(tid)
                names.append(t["name"] if t else tid)
            lines.append("已失效头衔：%s" % "、".join(names))
        ctk.CTkLabel(box, text="\n".join(lines), text_color=TEXT,
                     font=ui_font(size=11), justify="left",
                     wraplength=420).pack(anchor="w", padx=12)
        ctk.CTkLabel(box, text="这份存档的积分和头衔记录被程序之外的东西动过，"
                     "以上内容不参与计算。",
                     text_color=MUTED, font=ui_font(size=10), justify="left",
                     wraplength=420).pack(anchor="w", padx=12, pady=(4, 10))

    def open_titles(self):
        """Compatibility entry point; personal cosmetics now share one wardrobe."""
        self.open_wardrobe()

    def open_wardrobe(self):
        if not self._require_personal_access("查看和管理个性装扮"):
            return
        existing = getattr(self, "_wardrobe_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except tk.TclError:
                pass
        win = self._new_dialog("个性装扮", "480x760")
        self._wardrobe_window = win
        intro = ctk.CTkLabel(
            win, text="先看组合效果，再决定是否装备。内置素材按普通、稀有、史诗、传说、至臻分级。",
            text_color=MUTED, font=ui_font(size=11), wraplength=430,
            justify="left")
        intro.pack(anchor="w", fill="x", padx=16, pady=(12, 6))
        self._bind_wardrobe_wraplength(intro, min_width=180)
        owned_ids = (getattr(self, "_cloud_titles", set()) if self._has_cloud_account()
                     else slg_db.owned_title_ids(self.conn))
        owned = set(owned_ids or ()) | {slg_titles.DEFAULT_TITLE_ID}
        collectible_ids = set(slg_titles.collectible_appearance_ids())
        collected = len(collectible_ids & owned)
        ctk.CTkLabel(
            win, text="全部装扮收集  %d/%d · 默认头衔不计" % (
                collected, len(collectible_ids)),
            text_color=MUTED, font=ui_font(size=11)).pack(
                anchor="w", padx=16, pady=(0, 8))
        equipped_title = (getattr(self, "_cloud_equipped", slg_titles.DEFAULT_TITLE_ID)
                          if self._has_cloud_account() else
                          slg_db.get_equipped_title(self.conn) or slg_titles.DEFAULT_TITLE_ID)
        appearances = (self._equipped_appearance_map()
                       if self._has_cloud_account() else {})
        self._wardrobe_preview_state = {
            "title_id": equipped_title,
            "avatar_frame": appearances.get("avatar_frame", ""),
            "comment_frame": appearances.get("comment_frame", ""),
        }
        preview_panel = ctk.CTkFrame(win, fg_color=CARD, corner_radius=10)
        preview_panel.pack(fill="x", padx=14, pady=(0, 10))
        self._wardrobe_preview_panel = preview_panel
        self._wardrobe_preview_expanded = False
        self._render_wardrobe_preview()
        tabs = ctk.CTkTabview(win)
        self._wardrobe_tabs = tabs
        tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        title_tab = tabs.add("头衔")
        avatar_tab = tabs.add("头像框")
        comment_tab = tabs.add("名片框")
        self._wardrobe_browser(
            title_tab, list(slg_titles.TITLES), owned,
            lambda item: item.get("id") == equipped_title,
            lambda item: " ".join((item.get("name", ""), item.get("desc", ""),
                                   item.get("rarity", ""),
                                   slg_titles.obtain_title_text(item))),
            lambda parent, item: self._title_row(
                parent, item, item["id"] in owned,
                item["id"] == equipped_title, win),
            page_size=5)

        owned_items = owned
        self._wardrobe_appearance_list(avatar_tab, "avatar_frame", owned_items, win)
        owns_namecard = any(
            item.get("kind") == "decoration"
            and (item.get("appearance") or "comment_frame") == "comment_frame"
            and item.get("id") in owned_items
            for item in slg_titles.SHOP_ITEMS)
        self._wardrobe_profile_message_editor(comment_tab, owns_namecard, win)
        self._wardrobe_appearance_list(comment_tab, "comment_frame", owned_items, win)
        # _fit_dialog's screen-height cap is expressed in geometry units on
        # high-DPI displays. Clamp this dialog in logical units so it stays
        # within the physical screen while leaving the list its scroll area.
        win.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(win)
        height_limit = int(win.winfo_screenheight() * 0.9 / float(scale))
        height = int(min(win.winfo_reqheight() / float(scale), height_limit))
        self._place(win, 480, height)

    def _wardrobe_profile_message_editor(self, parent, owns_namecard, win):
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8)
        card.pack(fill="x", padx=4, pady=(4, 7))
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=9, pady=(6, 0))
        ctk.CTkLabel(header, text="公开名片寄语", text_color=ACCENT,
                     font=ui_font(size=11, weight="bold")).pack(side="left")
        editor = ctk.CTkFrame(card, fg_color="transparent")
        if not owns_namecard:
            hint = ctk.CTkLabel(
                card, text="拥有任意一款名片框后，可设置一条最多 20 字的公开寄语。",
                text_color=MUTED, font=ui_font(size=10), wraplength=390,
                justify="left", anchor="w")
            hint.pack(anchor="w", fill="x", padx=9, pady=(1, 7))
            self._bind_wardrobe_wraplength(hint, min_width=140)
            return
        if not self._has_cloud_account():
            hint = ctk.CTkLabel(
                card, text="名片寄语需要云端账号。登录后可以保存并在公开评论中展示。",
                text_color=MUTED, font=ui_font(size=10), wraplength=390,
                justify="left", anchor="w")
            hint.pack(anchor="w", fill="x", padx=9, pady=(1, 7))
            self._bind_wardrobe_wraplength(hint, min_width=140)
            return
        message = str(getattr(self, "_cloud_me", {}).get("profile_message", "") or "")
        summary = ctk.CTkLabel(
            card, text=("已设置寄语：" + message if message else "尚未设置寄语 · 最多 20 字"),
            text_color=MUTED, font=ui_font(size=10), wraplength=390,
            justify="left", anchor="w")
        summary.pack(anchor="w", fill="x", padx=9, pady=(1, 3))
        self._bind_wardrobe_wraplength(summary, min_width=140)
        toggle = ctk.CTkButton(
            header, text="编辑", width=54, height=23, corner_radius=6,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=10))
        # Keep the callback reference explicit; it avoids relying on child order
        # if the header gains another control later.
        toggle.configure(command=lambda target=editor, button=toggle:
                         self._toggle_wardrobe_message_editor(target, button))
        toggle.pack(side="right")
        self._wardrobe_message_editor_expanded = False
        message = str(getattr(self, "_cloud_me", {}).get("profile_message", "") or "")
        row = ctk.CTkFrame(editor, fg_color="transparent")
        row.pack(fill="x", padx=9, pady=(3, 2))
        entry = ctk.CTkEntry(
            row, height=30, placeholder_text="写一句简短寄语（最多 20 字）",
            font=ui_font(size=11))
        entry.insert(0, message)
        entry.pack(side="left", fill="x", expand=True)
        counter = ctk.CTkLabel(editor, text="%d / 20 字" % len(message),
                               text_color=MUTED, font=ui_font(size=9))
        counter.pack(anchor="e", padx=10, pady=(0, 1))
        actions = ctk.CTkFrame(editor, fg_color="transparent")
        actions.pack(fill="x", padx=9, pady=(0, 7))
        save = ctk.CTkButton(
            actions, text="保存寄语", width=1, height=26, corner_radius=7,
            fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
            font=ui_font(size=10), command=lambda: self._save_profile_message(
                identity_context=getattr(win, "_slg_identity_context", None)))
        save.pack(side="left", fill="x", expand=True, padx=(0, 5))
        clear = ctk.CTkButton(
            actions, text="清除", width=1, height=26, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=10), command=lambda: self._save_profile_message(
                clear=True, identity_context=getattr(
                    win, "_slg_identity_context", None)))
        clear.pack(side="left", fill="x", expand=True)
        entry.bind("<KeyRelease>", lambda _e: self._update_profile_message_counter())
        self._wardrobe_message_entry = entry
        self._wardrobe_message_counter = counter
        self._wardrobe_message_save_button = save
        self._wardrobe_message_clear_button = clear
        self._update_profile_message_counter()

    def _wardrobe_browser(self, tab, entries, owned_ids,
                          equipped_predicate, search_text, render_entry,
                          page_size):
        """Build a bounded search/filter/page view for one wardrobe category."""
        state = {"query": "", "filter": "全部", "page": 0}
        owned_ids = set(owned_ids)
        search = ctk.CTkEntry(
            tab, height=29, corner_radius=8,
            placeholder_text="搜索名称、说明或获取方式",
            font=ui_font(size=10))
        search.pack(fill="x", padx=7, pady=(3, 5))
        filter_row = ctk.CTkFrame(tab, fg_color="transparent")
        filter_row.pack(fill="x", padx=7, pady=(0, 5))
        filters = ctk.CTkSegmentedButton(
            filter_row, values=["全部", "已拥有", "未拥有", "已装备"],
            height=26, corner_radius=7, font=ui_font(size=9),
            selected_color=ACCENT, selected_hover_color=ACCENT,
            unselected_color=CHIP, unselected_hover_color=CARD_HOVER,
            text_color=TEXT)
        filters.set("全部")
        filters.pack(fill="x", expand=True)

        # Keep the page controls fixed while allowing a small scroll on short
        # displays; the frame never contains more than one bounded page.
        listing = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        listing.pack(fill="both", expand=True, padx=4, pady=(0, 3))
        footer = ctk.CTkFrame(tab, fg_color="transparent")
        footer.pack(fill="x", padx=7, pady=(0, 5))
        previous = ctk.CTkButton(
            footer, text="上一页", width=74, height=25, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=10))
        previous.pack(side="left")
        page_label = ctk.CTkLabel(
            footer, text="", text_color=MUTED, font=ui_font(size=10))
        page_label.pack(side="left", expand=True)
        following = ctk.CTkButton(
            footer, text="下一页", width=74, height=25, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=10))
        following.pack(side="right")

        def matches(item):
            owned = item.get("id") in owned_ids
            equipped = bool(equipped_predicate(item))
            selected_filter = state["filter"]
            if selected_filter == "已拥有" and not owned:
                return False
            if selected_filter == "未拥有" and owned:
                return False
            if selected_filter == "已装备" and not equipped:
                return False
            query = state["query"].casefold()
            return not query or query in str(search_text(item)).casefold()

        def change_page(delta):
            state["page"] += delta
            render()

        def render():
            filtered = [item for item in entries if matches(item)]
            page_count = max(1, (len(filtered) + page_size - 1) // page_size)
            state["page"] = min(max(0, state["page"]), page_count - 1)
            start = state["page"] * page_size
            visible = filtered[start:start + page_size]
            for child in listing.winfo_children():
                child.destroy()
            if not visible:
                ctk.CTkLabel(
                    listing, text="没有符合条件的装扮。",
                    text_color=MUTED, font=ui_font(size=11)
                ).pack(expand=True, pady=30)
            else:
                for item in visible:
                    render_entry(listing, item)
            canvas = getattr(listing, "_parent_canvas", None)
            if canvas is not None:
                try:
                    canvas.yview_moveto(0)
                except tk.TclError:
                    pass
            page_label.configure(
                text="第 %d / %d 页 · %d 件" % (state["page"] + 1,
                                               page_count, len(filtered)))
            previous.configure(
                state="normal" if state["page"] > 0 else "disabled",
                command=lambda: change_page(-1))
            following.configure(
                state="normal" if state["page"] + 1 < page_count else "disabled",
                command=lambda: change_page(1))

        def set_filter(value):
            state["filter"] = value
            state["page"] = 0
            render()

        def set_query(_event=None):
            state["query"] = search.get().strip()
            state["page"] = 0
            render()

        filters.configure(command=set_filter)
        search.bind("<KeyRelease>", set_query)
        render()

    def _toggle_wardrobe_message_editor(self, editor, button):
        """Show message controls only when requested, preserving list space by default."""
        try:
            if not editor.winfo_exists() or not button.winfo_exists():
                return
            expanded = bool(getattr(self, "_wardrobe_message_editor_expanded", False))
            if expanded:
                editor.pack_forget()
                button.configure(text="编辑")
            else:
                editor.pack(fill="x", padx=4, pady=(0, 5))
                button.configure(text="收起")
                self._update_profile_message_counter()
            self._wardrobe_message_editor_expanded = not expanded
        except tk.TclError:
            return

    def _update_profile_message_counter(self):
        entry = getattr(self, "_wardrobe_message_entry", None)
        counter = getattr(self, "_wardrobe_message_counter", None)
        save = getattr(self, "_wardrobe_message_save_button", None)
        clear = getattr(self, "_wardrobe_message_clear_button", None)
        try:
            if entry is None or not entry.winfo_exists():
                return
            value = entry.get()
            count = len(value)
            if counter is not None and counter.winfo_exists():
                counter.configure(text="%d / 20 字" % count,
                                  text_color=DANGER_TEXT if count > 20 else MUTED)
            if save is not None and save.winfo_exists():
                save.configure(state="disabled" if (count > 20 or
                               getattr(self, "_profile_message_pending", False))
                               else "normal")
            if clear is not None and clear.winfo_exists():
                clear.configure(state="disabled" if (not value or
                                getattr(self, "_profile_message_pending", False))
                                else "normal")
        except tk.TclError:
            return

    def _save_profile_message(self, clear=False, identity_context=None):
        if getattr(self, "_profile_message_pending", False):
            return
        if not self._require_identity_context(
                identity_context, "\u4fdd\u5b58\u4e2a\u4eba\u5bc4\u8bed", require_regular=True):
            return
        if not self._has_cloud_account():
            self._set_progress("名片寄语需要连接云端账号")
            return
        entry = getattr(self, "_wardrobe_message_entry", None)
        if entry is None or not entry.winfo_exists():
            return
        message = "" if clear else entry.get().strip()
        if len(message) > 20:
            messagebox.showwarning("名片寄语", "寄语最多 20 个字符。", parent=self)
            return
        self._profile_message_pending = True
        button = getattr(self, "_wardrobe_message_save_button", None)
        if button is not None and button.winfo_exists():
            button.configure(text="正在保存…")
        self._update_profile_message_counter()
        self._pending_profile_message = message
        self._run_cloud_action(
            "profile_message", lambda value=message:
                slg_account.update_profile_message(value))

    def _bind_wardrobe_wraplength(self, label, min_width=120):
        """Keep wardrobe descriptions inside their actual scaled container."""
        state = {"wrap": None}

        def resize(event=None):
            try:
                event_width = int(getattr(event, "width", 0) or 0)
                raw_width = event_width or int(label.winfo_width())
                if raw_width <= 1:
                    return
                reverse = getattr(label, "_reverse_widget_scaling", None)
                if callable(reverse):
                    width = reverse(raw_width)
                else:
                    scale = float(ctk.ScalingTracker.get_widget_scaling(label))
                    width = int(raw_width / max(scale, 0.1))
                width = max(int(min_width), int(width))
                if state["wrap"] != width and label.winfo_exists():
                    state["wrap"] = width
                    label.configure(wraplength=width)
            except (AttributeError, TypeError, ValueError, tk.TclError):
                return

        binding_id = label.bind("<Configure>", resize, add="+")
        try:
            label._wardrobe_wrap_binding = (label, binding_id)
        except (AttributeError, tk.TclError):
            pass
        try:
            label.after_idle(resize)
        except tk.TclError:
            pass

    def _toggle_wardrobe_preview(self):
        self._wardrobe_preview_expanded = not bool(
            getattr(self, "_wardrobe_preview_expanded", False))
        self._render_wardrobe_preview()

    def _render_wardrobe_preview(self):
        panel = getattr(self, "_wardrobe_preview_panel", None)
        if panel is None or not panel.winfo_exists():
            return
        for child in panel.winfo_children():
            binding = getattr(child, "_wardrobe_wrap_binding", None)
            if binding:
                try:
                    binding[0].unbind("<Configure>", binding[1])
                except tk.TclError:
                    pass
            child.destroy()
        state = getattr(self, "_wardrobe_preview_state", {})
        title_id = state.get("title_id") or slg_titles.DEFAULT_TITLE_ID
        avatar_frame = state.get("avatar_frame", "")
        comment_frame = state.get("comment_frame", "")
        equipped_title = (getattr(self, "_cloud_equipped", slg_titles.DEFAULT_TITLE_ID)
                          if self._has_cloud_account() else
                          slg_db.get_equipped_title(self.conn) or slg_titles.DEFAULT_TITLE_ID)
        equipped_appearances = (self._equipped_appearance_map()
                                if self._has_cloud_account() else {})
        is_equipped = (title_id == equipped_title
                       and avatar_frame == equipped_appearances.get("avatar_frame", "")
                       and comment_frame == equipped_appearances.get("comment_frame", ""))
        profile_name = ((getattr(self, "_cloud_me", {}).get("nickname", "")
                         if self._has_cloud_account() else
                         slg_db.get_pref(self.conn, "profile.nickname", ""))
                        or "未设置昵称")
        title_info = slg_titles.title_by_id(title_id) or {}

        def appearance_name(item_id, empty_text):
            if not item_id:
                return empty_text
            item = self._appearance_item(item_id)
            return item.get("name", item_id)

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(7, 0))
        ctk.CTkLabel(
            header, text=("当前已装备" if is_equipped else "试搭预览"),
            text_color=ACCENT, font=ui_font(size=11, weight="bold")
        ).pack(side="left", anchor="w")
        ctk.CTkButton(
            header, text="收起预览" if getattr(self, "_wardrobe_preview_expanded", False)
            else "展开预览", width=72, height=23, corner_radius=6,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=9), command=self._toggle_wardrobe_preview
        ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            header, text="恢复已装备", width=78, height=23, corner_radius=6,
            fg_color="transparent", text_color=MUTED, hover_color=CHIP,
            font=ui_font(size=9), command=self._reset_wardrobe_preview
        ).pack(side="right")

        summary = ctk.CTkLabel(
            panel,
            text="%s · %s　头像框：%s　名片框：%s" % (
                title_info.get("name", "普通用户"),
                title_info.get("rarity", "普通"),
                appearance_name(avatar_frame, "未装备"),
                appearance_name(comment_frame, "未装备")),
            text_color=MUTED, font=ui_font(size=10), justify="left", anchor="w")
        summary.pack(fill="x", padx=12, pady=(2, 7))
        self._bind_wardrobe_wraplength(summary, min_width=180)

        if not getattr(self, "_wardrobe_preview_expanded", False):
            return

        body = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0, height=190)
        body.pack(fill="x", padx=7, pady=(0, 6))
        line = ctk.CTkFrame(body, fg_color="transparent")
        line.pack(fill="x", padx=3, pady=(2, 5))
        if avatar_frame:
            self._avatar_frame_canvas(
                line, avatar_frame, profile_name,
                size=78, actual_avatar=True).pack(side="left", padx=(0, 10))
        elif os.path.exists(slg_db.avatar_path()):
            ctk.CTkLabel(line, text="", image=load_avatar(56)).pack(
                side="left", padx=(0, 10), pady=5)
        else:
            ctk.CTkLabel(
                line, text=(profile_name[:1] or "我"), width=54, height=54,
                corner_radius=27, fg_color=ACCENT, text_color=ON_ACCENT,
                font=ui_font(size=19, weight="bold")).pack(
                    side="left", padx=(0, 10), pady=5)
        details = ctk.CTkFrame(line, fg_color="transparent")
        details.pack(side="left", fill="both", expand=True)
        self._title_badge(details, title_id, max_width=230).pack(
            anchor="w", pady=(1, 4))
        title_description = (title_info.get("desc") or
                             title_info.get("description") or "暂无头衔说明")
        title_text = ctk.CTkLabel(
            details,
            text="%s · %s\n%s" % (title_info.get("name", "普通用户"),
                                  title_info.get("rarity", "普通"),
                                  title_description),
            text_color=MUTED, font=ui_font(size=10), wraplength=270,
            justify="left", anchor="w")
        title_text.pack(anchor="w", fill="x", pady=(0, 3))
        self._bind_wardrobe_wraplength(title_text, min_width=120)
        self._comment_nameplate(details, profile_name, comment_frame)
        appearance_summary = ctk.CTkLabel(
            details,
            text="头像框：%s\n名片框：%s" % (
                appearance_name(avatar_frame, "未装备"),
                appearance_name(comment_frame, "未装备")),
            text_color=MUTED, font=ui_font(size=10), justify="left", anchor="w")
        appearance_summary.pack(anchor="w", fill="x", pady=(5, 0))
        self._bind_wardrobe_wraplength(
            appearance_summary, min_width=120)

    def _set_wardrobe_preview(self, slot, item_id):
        state = getattr(self, "_wardrobe_preview_state", None)
        if not isinstance(state, dict):
            return
        if slot == "title_id":
            state[slot] = item_id or slg_titles.DEFAULT_TITLE_ID
        elif slot in ("avatar_frame", "comment_frame"):
            state[slot] = item_id or ""
        self._render_wardrobe_preview()

    def _reset_wardrobe_preview(self):
        appearances = self._equipped_appearance_map() if self._has_cloud_account() else {}
        self._wardrobe_preview_state = {
            "title_id": (getattr(self, "_cloud_equipped", slg_titles.DEFAULT_TITLE_ID)
                         if self._has_cloud_account() else
                         slg_db.get_equipped_title(self.conn) or slg_titles.DEFAULT_TITLE_ID),
            "avatar_frame": appearances.get("avatar_frame", ""),
            "comment_frame": appearances.get("comment_frame", ""),
        }
        self._render_wardrobe_preview()

    def _wardrobe_appearance_list(self, tab, slot, owned_items, win):
        items = [item for item in slg_titles.SHOP_ITEMS
                 if item.get("kind") == "decoration"
                 and (item.get("appearance") or "comment_frame") == slot]
        hint = ctk.CTkLabel(
            tab, text="点击“预览搭配”可以和当前头衔、另一装备槽组合查看；未拥有的商品也能先试搭。",
            text_color=MUTED, font=ui_font(size=10), wraplength=390,
            justify="left", anchor="w")
        hint.pack(anchor="w", fill="x", padx=8, pady=(5, 4))
        self._bind_wardrobe_wraplength(hint, min_width=140)
        current = self._equipped_appearance_map().get(slot, "")

        def render_item(listing, item):
            card = ctk.CTkFrame(listing, fg_color=CARD, corner_radius=8,
                                border_width=1,
                                border_color=self._appearance_color(item["id"]))
            card.pack(fill="x", padx=3, pady=3)
            head = ctk.CTkFrame(card, fg_color="transparent")
            head.pack(fill="x", padx=8, pady=(5, 1))
            ctk.CTkLabel(head, text=item.get("name", item["id"]),
                         text_color=TEXT, font=ui_font(size=11, weight="bold")
                         ).pack(side="left")
            ctk.CTkLabel(head, text=item.get("rarity", "普通"),
                         text_color=self._appearance_color(item["id"]),
                         font=ui_font(size=9, weight="bold")).pack(side="left", padx=6)
            owned = item["id"] in owned_items
            ctk.CTkLabel(head, text=("正在使用" if item["id"] == current else
                                     "已拥有" if owned else "未拥有"),
                         text_color=ON_ACCENT if item["id"] == current else MUTED,
                         fg_color=ACCENT if item["id"] == current else CHIP,
                         corner_radius=6, height=17,
                         font=ui_font(size=9)).pack(side="right")
            description = ctk.CTkLabel(
                card, text=item.get("description", ""),
                text_color=MUTED, font=ui_font(size=9),
                justify="left", wraplength=370, anchor="w")
            description.pack(fill="x", padx=8, pady=(1, 3))
            self._bind_wardrobe_wraplength(
                description, min_width=120)
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.pack(fill="x", padx=8, pady=(0, 5))
            buttons = [ctk.CTkButton(
                actions, text="预览搭配", width=1, height=23, corner_radius=7,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=9),
                command=lambda s=slot, i=item["id"]:
                    self._set_wardrobe_preview(s, i)
            )]
            if owned:
                active = item["id"] == current
                buttons.append(ctk.CTkButton(
                    actions, text="卸下" if active else "装备", width=1,
                    height=23, corner_radius=7,
                    fg_color=CHIP if active else ACCENT,
                    text_color=TEXT if active else ON_ACCENT,
                    hover_color=CARD_HOVER, font=ui_font(size=9),
                    command=lambda i=item["id"], s=slot, a=active: (
                        win.destroy(), self._equip_appearance(
                            s, "" if a else i, return_to_wardrobe=True,
                            identity_context=getattr(
                                win, "_slg_identity_context", None)))
                ))
            else:
                buttons.append(ctk.CTkButton(
                    actions, text="前往商城", width=1, height=23,
                    corner_radius=7, fg_color=CHIP, text_color=ACCENT,
                    hover_color=CARD_HOVER, font=ui_font(size=9),
                    command=lambda: (win.destroy(), self.open_shop())
                ))
            self._layout_wardrobe_actions(actions, buttons, compact_at=340)

        self._wardrobe_browser(
            tab, items, owned_items,
            lambda item: item.get("id") == current,
            lambda item: " ".join((item.get("name", ""),
                                   item.get("description", ""),
                                   item.get("rarity", ""))),
            render_item, page_size=4)
    def _refresh_open_wardrobe(self):
        """Rebuild the one open wardrobe after its cloud inventory arrives."""
        win = getattr(self, "_wardrobe_window", None)
        if win is None:
            return
        try:
            if not win.winfo_exists():
                return
            tabs = getattr(self, "_wardrobe_tabs", None)
            selected = tabs.get() if tabs is not None and tabs.winfo_exists() else None
            win.destroy()
        except tk.TclError:
            return
        self._wardrobe_window = None
        self._wardrobe_tabs = None
        self.open_wardrobe()
        tabs = getattr(self, "_wardrobe_tabs", None)
        if selected and tabs is not None:
            try:
                tabs.set(selected)
            except tk.TclError:
                pass

    def _layout_wardrobe_actions(self, container, buttons, compact_at=390):
        """Wrap card actions into two rows when the scaled card is narrow."""
        for column in range(max(2, len(buttons))):
            container.grid_columnconfigure(column, weight=1, uniform="wardrobe-actions")
        layout = {"compact": None}

        def resize(event=None):
            try:
                event_width = int(getattr(event, "width", 0) or 0)
                raw_width = event_width or int(container.winfo_width())
                scale = float(ctk.ScalingTracker.get_widget_scaling(container))
                compact = raw_width / max(scale, 0.1) < compact_at
                if layout["compact"] == compact:
                    return
                layout["compact"] = compact
                for button in buttons:
                    button.grid_forget()
                if compact:
                    buttons[0].grid(row=0, column=0, columnspan=2,
                                    sticky="ew", padx=1, pady=(0, 3))
                    for index, button in enumerate(buttons[1:]):
                        span = 2 if len(buttons) == 2 else 1
                        button.grid(row=1, column=index, columnspan=span,
                                    sticky="ew", padx=(0, 3) if index == 0 else (0, 0))
                else:
                    for index, button in enumerate(buttons):
                        button.grid(row=0, column=index, sticky="ew",
                                    padx=(0, 5) if index < len(buttons) - 1 else (0, 0))
            except (AttributeError, TypeError, ValueError, tk.TclError):
                return

        container.bind("<Configure>", resize, add="+")
        try:
            container.after_idle(resize)
        except tk.TclError:
            pass

    def _title_row(self, parent, t, is_owned, is_equipped, win):
        """一条头衔：名字 + 状态、一句话简介、以及「获得方式 / 装备」两颗按钮。

        简介直接摊在列表里 —— 藏进弹窗等于每次都要点一次才知道是什么。
        """
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8)
        card.pack(fill="x", padx=3, pady=2)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(4, 0))
        # Show the actual badge in the collection list, so users can compare
        # what they own without opening every detail dialog.
        name_badge = self._title_badge(head, t["id"], max_width=190)
        name_badge.pack(side="left", pady=(0, 1))
        name_badge.configure(cursor="hand2")
        name_badge.bind("<Button-1>", lambda e, t=t: self._title_info(t, win))
        if is_equipped:
            ctk.CTkLabel(head, text="使用中", text_color=ON_ACCENT,
                         fg_color=ACCENT, corner_radius=6, height=16,
                         font=ui_font(size=9)).pack(side="left", padx=(6, 0))
        elif is_owned:
            ctk.CTkLabel(head, text="已拥有", text_color=MUTED, fg_color=CHIP,
                         corner_radius=6, height=16,
                         font=ui_font(size=9)).pack(side="left", padx=(6, 0))

        desc_text = str(t.get("desc", ""))
        desc_lbl = ctk.CTkLabel(card, text=desc_text, text_color=MUTED,
                                font=ui_font(size=9), justify="left",
                                wraplength=390, anchor="w")
        desc_lbl.pack(fill="x", padx=8, pady=(1, 0))
        self._bind_wardrobe_wraplength(desc_lbl, min_width=140)
        # 名字和简介也是打开简介的入口，跟那颗按钮同一个弹窗。
        desc_lbl.bind("<Button-1>", lambda e, t=t: self._title_info(t, win))
        desc_lbl.configure(cursor="hand2")

        acts = ctk.CTkFrame(card, fg_color="transparent")
        acts.pack(fill="x", padx=8, pady=(3, 4))
        buttons = [ctk.CTkButton(
            acts, text="预览搭配", height=22, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=9),
            command=lambda tid=t["id"]: self._set_wardrobe_preview("title_id", tid)
        )]
        # 「获得方式」人人都有：没拿到的想知道怎么拿，拿到了的也可能想知道这枚是什么来头。
        buttons.append(ctk.CTkButton(
            acts, text="获得方式", height=22, corner_radius=7,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=9),
            command=lambda t=t: self._title_info(t, win)))
        if is_owned:
            buttons.append(ctk.CTkButton(
                acts, text="取消" if is_equipped else "装备", width=72,
                height=22, corner_radius=7, fg_color=CHIP,
                text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=9),
                command=lambda tid=t["id"]: self._equip_title(win, tid)))
        self._layout_wardrobe_actions(acts, buttons, compact_at=390)

    def _title_info(self, t, parent=None):
        """头衔简介 + 获取路径。列表里那句点评简化过，这里给完整的一句。

        顶部徽记和底部「知道了」都固定，中间可滚动：简介长短不由这里说了算
        （幸运之王那段就比别的长一截），固定高度迟早会把按钮顶出可视区。
        """
        color = slg_titles.RARITY_COLORS.get(t.get("rarity"), MUTED)
        owned = t.get("id") in (getattr(self, "_cloud_titles", set())
                                 if self._has_cloud_account() else
                                 slg_db.owned_title_ids(self.conn))
        win = self._new_dialog(t.get("name", "头衔"), parent=parent)

        band_color = _mix(BG, color, 0.22)
        band = ctk.CTkFrame(win, height=72, fg_color=band_color,
                            corner_radius=10)
        band.pack(fill="x", padx=16, pady=(16, 0))
        band.pack_propagate(False)
        self._title_badge(band, t.get("id"), max_width=320,
                          background=band_color).pack(expand=True)

        ctk.CTkLabel(win, text=t.get("name", "头衔"), text_color=TEXT,
                     font=ui_font(size=17, weight="bold")).pack(pady=(12, 0))
        ctk.CTkLabel(win, text=t.get("rarity", ""), text_color=color,
                     font=ui_font(size=11)).pack(pady=(2, 0))

        # 换行宽度得按**滚动区内部**算，不是按窗口：340 的滚动区去掉滚动条和左右
        # 留白只剩 ~464 物理像素，写 480 会让最后几个字被右边缘切掉（幸运之王的
        # 那句「一百抽」就被吃掉了一个字）。
        body = ctk.CTkScrollableFrame(win, fg_color="transparent",
                                      width=340, height=150)
        body.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        for heading, text in (("简介", t.get("desc", "")),
                              ("获得方式", slg_titles.obtain_title_text(t))):
            ctk.CTkLabel(body, text=heading, text_color=ACCENT, anchor="w",
                         font=ui_font(size=11, weight="bold")).pack(
                fill="x", padx=10, pady=(10, 2))
            ctk.CTkLabel(body, text=text, text_color=MUTED, justify="left",
                         wraplength=440, anchor="w",
                         font=ui_font(size=11)).pack(fill="x", padx=10)

        ctk.CTkLabel(win, text="已拥有" if owned else "尚未获得",
                     text_color=ACCENT if owned else MUTED,
                     font=ui_font(size=11, weight="bold")).pack(pady=(12, 0))
        ctk.CTkButton(win, text="知道了", height=32, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=CARD_HOVER, font=ui_font(size=12),
                      command=win.destroy).pack(fill="x", padx=16, pady=(10, 16))
        self._fit_dialog(win, 380, parent=parent)

    # 商城货架：左栏分类 + 右栏网格。一件商品一张固定尺寸的卡片，FlowFrame 负责
    # 换行。高度写死是为了让 winfo_reqheight 就是卡片高度 —— 网格的行高靠它算，
    # 让卡片随内容长高会让同一行的卡片参差不齐。
    #
    # Unscaled units, like every other geometry the FlowFrame measures: CTk
    # multiplies these by the 1.5x display factor. 卡宽不再写死：面板是窗口的 40%，
    # 默认窗口 1180 时网格只有 192px，两张 104 放不下（这正是「商品不见了」的根因），
    # 所以 _render_shop 用 shop_columns() 按实测宽度回推列数与卡宽。
    # SHOP_TILE_W 是卡宽上限（Grid 够宽时的样子），SHOP_TILE_W_MIN 是下限 ——
    # 「首发用户」加「限时」角标在 92px 内刚好放得下，再窄会截断商品名。
    SHOP_RAIL_W = 56
    SHOP_TILE_W = 104
    SHOP_TILE_W_MIN = 92
    SHOP_TILE_H = 172
    SHOP_TILE_GAP = 8
    SHOP_TITLE_GLYPHS = {"普通": "●", "稀有": "◆", "史诗": "★", "传说": "✦", "至臻": "♛"}
    SHOP_KIND_GLYPHS = {"rename": "✎", "makeup": "↺"}

    def _set_shop_wide_layout(self, enabled):
        """Expand the shop panel, then restore the usual game-list split."""
        body = getattr(self, "_content_body", None)
        if body is None or not body.winfo_exists():
            return
        # Game pages use 3:2 (list:detail). While the shop occupies the detail
        # panel, 2:3 gives its filters and product grid enough room for at least
        # two practical cards at the minimum supported window width.
        body.grid_columnconfigure(0, weight=2 if enabled else 3)
        body.grid_columnconfigure(1, weight=3 if enabled else 2)

    def open_shop(self):
        if self._is_admin_mode():
            if not self._require_personal_access("\u7ba1\u7406\u5458\u62bd\u5956\u5de5\u5177"):
                return
            self._panel_mode = "shop"
            self._set_shop_wide_layout(True)
            self._destroy_detail()
            d = self.detail
            self._shop_balance_label = None
            self._shop_signin_button = None
            self.signin_btn = None
            self._quest_panel = None
            self._quest_status_label = None
            self._quest_retry_button = None
            self._quest_rows = None
            self._quest_claim_buttons = {}
            self._shop_grid = None
            self._shop_rail = None
            self._profile_identity_switch(d, admin=True)
            ctk.CTkLabel(
                d,
                text="\u7ba1\u7406\u5458\u8eab\u4efd\u4e0d\u4f7f\u7528\u666e\u901a\u5546\u57ce\u3001\u7b7e\u5230\u3001\u4efb\u52a1\u6216\u62bd\u5956\uff1b\u8bf7\u5207\u6362\u5230\u666e\u901a\u7528\u6237\u8eab\u4efd\u4f7f\u7528\u8fd9\u4e9b\u529f\u80fd",
                text_color=MUTED, font=ui_font(size=11), wraplength=520,
                justify="left").pack(fill="x", padx=16, pady=(10, 0))
            self._shop_lottery_card(d)
            return
        if not self._require_personal_access("积分商城"):
            return
        # 积分商城 shares the detail panel exactly like 个人中心: the first click
        # swaps the panel over (temporarily covering any game shown), and buying
        # or drawing re-renders it in place.
        self._panel_mode = "shop"
        self._set_shop_wide_layout(True)
        self._destroy_detail()
        d = self.detail
        head = ctk.CTkFrame(d, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(20, 8))
        ctk.CTkLabel(head, text="积分商城", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(side="left")
        # Held on self so the lottery can update the balance in place the moment
        # points are spent, instead of waiting for the panel to rebuild.
        self._shop_balance_label = ctk.CTkLabel(
            head, text=("云端 %s 积分" % getattr(self, "_cloud_balance", "读取中")
                        if self._has_cloud_account() else
                        "本地旧档 %d 积分" % slg_db.points_balance(self.conn)),
            text_color=ACCENT, font=ui_font(size=14, weight="bold"))
        self._shop_balance_label.pack(side="right")
        if self._has_cloud_account():
            self._run_cloud_action("me", slg_account.me)

        sign_row = ctk.CTkFrame(d, fg_color="transparent")
        sign_row.pack(fill="x", padx=16, pady=(0, 2))
        if self._is_admin_mode():
            self._shop_signin_button = None
            self.signin_btn = None
            ctk.CTkLabel(
                sign_row,
                text="管理员身份不参与普通用户签到和任务；切换到普通用户后可使用这些功能。",
                text_color=MUTED, font=ui_font(size=10), anchor="w",
                wraplength=520, justify="left").pack(fill="x")
        else:
            self._shop_signin_button = ctk.CTkButton(
                sign_row, text="每日签到", width=126, height=30, corner_radius=8,
                fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                font=ui_font(size=11, weight="bold"), command=self._on_signin_click)
            self._shop_signin_button.pack(side="left")
            self.signin_btn = self._shop_signin_button
            ctk.CTkLabel(
                sign_row, text="签到积分会计入当前云端账号", text_color=MUTED,
                font=ui_font(size=10)).pack(side="left", padx=(9, 0))
            self._update_shop_signin_button()

        self._shop_lottery_card(d)
        if self._is_admin_mode():
            self._quest_panel = None
            self._quest_status_label = None
            self._quest_retry_button = None
            self._quest_rows = None
            self._quest_claim_buttons = {}
        else:
            self._build_shop_quest_panel(d)
            self._fetch_quests()

        # 货架：左栏分类、右栏商品。以前分类 tab 和子分类 chip 是上下两排圆角
        # 按钮、只差一个高度，层级几乎看不出来；竖排才像货架，顺带把纵向让给商品。
        body = ctk.CTkFrame(d, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(12, 14))
        # 货架栏不锁尺寸：以前写了 width + pack_propagate(False)，那会把高度一起冻在
        # CTk 默认的 200px 上，细分栏涨到 6 档之后，第 7 行往下的稀有度就再也画不出来
        # （史诗/传说/至臻集体消失）。让它按内容自己撑开，高度和宽度都不会再被裁。
        self._shop_rail = ctk.CTkFrame(body, fg_color="transparent")
        self._shop_rail.pack(side="left", fill="y")
        # The rail and the grid are separate holders so a filter click rebuilds
        # both without laying the whole panel out again.
        self._shop_grid = ctk.CTkFrame(body, fg_color="transparent")
        self._shop_grid.pack(side="left", fill="both", expand=True, padx=(8, 0))
        # 面板宽度跟着窗口走，拉宽后每行能放的张数会变；卡宽是烘进控件的，所以宽度
        # 变了必须整格重建。防抖 80ms，免得拖动窗口时每像素重建一次。
        self._shop_resize_job = None
        self._shop_grid.bind("<Configure>", self._on_shop_grid_configure)
        # Every open starts on 全部: the panel swaps in wholesale, so arriving
        # with last visit's filter still on reads as "the shop lost its stock".
        self._shop_cat = self._shop_sub = "全部"
        self._render_shop()
        fly = getattr(self, "_shop_fly", None)
        self._shop_fly = None
        if fly:
            self._shop_fly_in(*fly)

    def _build_shop_quest_panel(self, parent):
        """Build the compact account quest list inside the existing shop."""
        panel = ctk.CTkFrame(parent, fg_color=CHIP, corner_radius=9)
        self._quest_panel = panel
        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(7, 3))
        ctk.CTkLabel(header, text="每日 / 每周任务", text_color=TEXT,
                     font=ui_font(size=12, weight="bold")).pack(side="left")
        self._quest_status_label = ctk.CTkLabel(
            header, text="", text_color=MUTED, font=ui_font(size=10),
            anchor="e")
        self._quest_status_label.pack(side="right", padx=(6, 0))
        self._quest_retry_button = ctk.CTkButton(
            header, text="重试", width=48, height=22, corner_radius=6,
            fg_color=BG, text_color=ACCENT, hover_color=CARD_HOVER,
            font=ui_font(size=10), command=self._fetch_quests)
        self._quest_retry_button.pack(side="right")
        self._quest_retry_button.pack_forget()
        ctk.CTkLabel(
            panel,
            text="每日任务按上海时间 04:00 刷新；每周任务于周一 04:00 刷新。抽奖次数仍按自然日计算。",
            text_color=MUTED, font=ui_font(size=9), anchor="w",
            justify="left", wraplength=620).pack(
                fill="x", padx=10, pady=(0, 4))
        self._quest_rows = ctk.CTkFrame(panel, fg_color="transparent")
        self._quest_rows.pack(fill="x", padx=8, pady=(0, 7))
        self._quest_claim_buttons = {}
        self._render_quest_panel()

    def _render_quest_panel(self):
        panel = getattr(self, "_quest_panel", None)
        try:
            if panel is None or not panel.winfo_exists():
                return
        except tk.TclError:
            return
        if (not self._has_quest_session()
                or getattr(self, "_quests_supported", None) is False):
            panel.pack_forget()
            return
        if panel.winfo_manager() != "pack":
            panel.pack(fill="x", padx=16, pady=(6, 0))

        status = getattr(self, "_quest_status_label", None)
        retry = getattr(self, "_quest_retry_button", None)
        rows = getattr(self, "_quest_rows", None)
        if status is None or rows is None:
            return
        for child in rows.winfo_children():
            child.destroy()
        self._quest_claim_buttons = {}

        error = getattr(self, "_quests_error", None)
        tasks = ((getattr(self, "_quests_data", None) or {}).get("tasks")
                 if isinstance(getattr(self, "_quests_data", None), dict) else None)
        if error:
            status.configure(text="任务暂不可用")
            if retry is not None and retry.winfo_exists():
                retry.pack(side="right")
            ctk.CTkLabel(
                rows, text="连接失败，稍后可以重新查询。", text_color=MUTED,
                font=ui_font(size=11), anchor="w").pack(fill="x", padx=3, pady=2)
            return
        if retry is not None and retry.winfo_exists():
            retry.pack_forget()
        if tasks is None:
            status.configure(text="正在读取…" if self._quests_fetching else "")
            return
        if not isinstance(tasks, list):
            self._quests_error = "invalid quest response"
            self._render_quest_panel()
            return
        status.configure(text="")
        for task in tasks:
            if not isinstance(task, dict) or not task.get("id"):
                continue
            quest_id = str(task["id"])
            try:
                progress = max(0, int(task.get("progress") or 0))
                target = max(0, int(task.get("target") or 0))
                reward = max(0, int(task.get("reward") or 0))
            except (TypeError, ValueError):
                progress = target = reward = 0
            period = "每周" if str(task.get("period") or "").lower() in (
                "week", "weekly") else "每日"
            label = str(task.get("label") or quest_id)
            row = ctk.CTkFrame(rows, fg_color="transparent", height=27)
            row.pack(fill="x", padx=2, pady=1)
            ctk.CTkLabel(
                row, text="%s  %s  %d/%d  +%d" % (
                    period, label, progress, target, reward),
                text_color=TEXT, font=ui_font(size=11), anchor="w",
                justify="left").pack(side="left", fill="x", expand=True)
            claiming = getattr(self, "_quest_claiming", None) == quest_id
            if task.get("claimed"):
                button_text, enabled = "已领取", False
            elif claiming:
                button_text, enabled = "提交中…", False
            elif task.get("claimable"):
                button_text, enabled = "领取 +%d" % reward, True
            elif target and progress >= target:
                button_text, enabled = "已完成", False
            else:
                button_text, enabled = "未完成", False
            button = ctk.CTkButton(
                row, text=button_text, width=72, height=22, corner_radius=6,
                fg_color=ACCENT if enabled else BG,
                text_color=ON_ACCENT if enabled else MUTED,
                hover_color=CARD_HOVER, font=ui_font(size=10),
                state="normal" if enabled else "disabled",
                command=lambda q=quest_id: self._claim_quest(q))
            button.pack(side="right", padx=(6, 0))
            self._quest_claim_buttons[quest_id] = button

    def _reset_quests_for_account(self):
        self._quest_generation += 1
        self._quests_supported = None
        self._quests_fetching = False
        self._quests_data = None
        self._quests_error = None
        self._quest_claiming = None
        self._render_quest_panel()

    def _has_quest_session(self):
        if not self._has_usable_cloud_session():
            return False
        try:
            return bool(slg_account.session())
        except slg_account.AccountError:
            return False

    def _run_quest_action(self, action, func):
        """Tag asynchronous quest work so stale results cannot cross accounts."""
        kind = "quest_%s_%d" % (action, self._quest_generation)
        self._run_cloud_action(kind, func)

    def _fetch_quests(self, force=False):
        if not self._has_quest_session():
            self._render_quest_panel()
            return
        if self._quests_supported is False:
            return
        if self._quests_fetching:
            if not force:
                return
            # A pre-action GET could finish after a comment/signin write and
            # repaint stale progress. Invalidate it and issue a fresh snapshot.
            self._quest_generation += 1
            self._quests_fetching = False
        self._quests_fetching = True
        self._quests_error = None
        self._render_quest_panel()
        self._run_quest_action("status", slg_account.get_quests)

    @staticmethod
    def _quest_endpoint_missing(error):
        text = str(error or "").lower()
        return ("404" in text or "尚未部署" in text
                or "not deployed" in text or "endpoint not found" in text)

    def _claim_quest(self, quest_id):
        if not self._require_regular_user_mode("\u4efb\u52a1\u9886\u53d6"):
            return
        if self._quest_claiming is not None:
            return
        data = getattr(self, "_quests_data", None)
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        task = next((item for item in tasks if isinstance(item, dict)
                     and str(item.get("id")) == str(quest_id)), None)
        if not task or task.get("claimed") or not task.get("claimable"):
            return
        if not self._has_quest_session():
            self._render_quest_panel()
            return
        self._quest_claiming = str(quest_id)
        self._render_quest_panel()
        self._run_quest_action(
            "claim", lambda q=str(quest_id): slg_account.claim_quest(q))

    def _update_shop_signin_button(self):
        button = getattr(self, "_shop_signin_button", None)
        try:
            if button is None or not button.winfo_exists():
                return
        except tk.TclError:
            return
        if self._remote_flags.get("disable_signin"):
            button.configure(text="签到维护中", state="disabled",
                             fg_color=CHIP, text_color=MUTED)
            return
        if self._is_admin_mode():
            if not self._has_usable_cloud_session():
                self._ensure_developer_cloud_identity()
                button.configure(text="管理员身份未连接", state="disabled",
                                 fg_color=CHIP, text_color=MUTED)
                return
            button.configure(text="无限签到", state="normal",
                             fg_color=ACCENT, text_color=ON_ACCENT)
            return
        if self._has_cloud_account():
            cache = getattr(self, "_cloud_me", {})
            try:
                current = slg_account.session()
            except slg_account.AccountError:
                current = None
            if (current and isinstance(cache, dict)
                    and cache.get("account_id") == current.get("account_id")):
                today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
                days = cache.get("signin_month_days")
                if isinstance(days, (list, tuple, set)):
                    signed_today = today in days
                elif getattr(self, "_cloud_me_day", None) == today:
                    signed_today = cache.get("signed_today")
                else:
                    signed_today = None
                if signed_today is None:
                    button.configure(text="读取签到状态…", state="disabled",
                                     fg_color=CHIP, text_color=MUTED)
                    return
                signed = bool(signed_today)
                button.configure(
                    text="今日已签到" if signed else "每日签到",
                    state="disabled" if signed else "normal",
                    fg_color=CHIP if signed else ACCENT,
                    text_color=MUTED if signed else ON_ACCENT)
            else:
                button.configure(text="读取签到状态…", state="disabled",
                                 fg_color=CHIP, text_color=MUTED)
            return
        signed = slg_db.last_signin_day(self.conn) == slg_titles.today_str()
        button.configure(text="今日已签到" if signed else "每日签到",
                         state="disabled" if signed else "normal",
                         fg_color=CHIP if signed else ACCENT,
                         text_color=MUTED if signed else ON_ACCENT)

    def _render_shop(self):
        """Draw the filter chips and the product grid for the current filter.

        Split from open_shop because a chip click only needs this half: the
        header, the balance and the lottery banner do not change.
        """
        items = [i for i in slg_titles.available_shop_items()
                 if i["kind"] != "lottery"]
        cat = getattr(self, "_shop_cat", "全部")
        sub = getattr(self, "_shop_sub", "全部")

        discovered = list(dict.fromkeys(self._shop_category(i) for i in items))
        category_order = ("头衔", "头像框", "名片框", "功能道具")
        cats = ([value for value in category_order if value in discovered]
                + [value for value in discovered if value not in category_order])
        if cat != "全部" and cat not in cats:
            cat = "全部"
        subs = self._shop_subs(items, cat)
        if sub != "全部" and sub not in subs:
            sub = "全部"
        self._shop_cat, self._shop_sub = cat, sub

        self._shop_rail_view(items, cats, subs, cat, sub)

        for child in self._shop_grid.winfo_children():
            child.destroy()
        owned = (getattr(self, "_cloud_titles", set()) if self._has_cloud_account()
                 else slg_db.owned_title_ids(self.conn))
        shown = [i for i in items
                 if cat in ("全部", self._shop_category(i))
                 and sub in ("全部", i.get("subcategory"))]
        # 已拥有的排后面：逛商城是为了看还没拿到的，「已拥有」挡在最前面没有信息量。
        # sorted 是稳定的，同类商品之间的原顺序不变。
        shown.sort(key=lambda i: i["id"] in owned)
        if not shown:
            ctk.CTkLabel(self._shop_grid, text="该分类暂无商品，敬请期待",
                         text_color=MUTED, font=ui_font(size=12)).pack(pady=24)
            self._shop_cols = (1, self.SHOP_TILE_W)
            return
        cols, tile_w = self._shop_columns_now()
        self._shop_cols = (cols, tile_w)
        flow = FlowFrame(self._shop_grid, gap_x=self.SHOP_TILE_GAP,
                         gap_y=self.SHOP_TILE_GAP, fg_color="transparent")
        flow.pack(fill="x")
        equipped = ((getattr(self, "_cloud_equipped", slg_titles.DEFAULT_TITLE_ID),
                     getattr(self, "_cloud_equipped_cosmetic", ""),
                     self._equipped_appearance_map())
                    if self._has_cloud_account() else
                    (slg_db.get_equipped_title(self.conn) or "", ""))
        points = (getattr(self, "_cloud_balance", 0) if self._has_cloud_account()
                  else slg_db.points_balance(self.conn))
        flow.set_items([self._shop_tile(flow, item, owned, equipped, points,
                                        width=tile_w)
                        for item in shown])

    @staticmethod
    def _shop_category(item):
        """Normalize catalog category names while accepting pre-0.23.7 items."""
        value = item.get("category")
        return {"头衔类": "头衔", "物品类": "功能道具"}.get(
            value, value or "其他")

    @classmethod
    def _shop_subs(cls, items, cat):
        """细分栏的条目。

        头衔类的细分就是稀有度，而「这个档现在没货」本身就是有用信息 —— 所以列全
        RARITY_ORDER 五档，空档点进去如实显示「该分类暂无商品」，而不是让那一档
        凭空消失、让人以为软件漏了。物品类维持现状，只列真的存在的细分。
        """
        subs = list(dict.fromkeys(
            i.get("subcategory") or "全部" for i in items
            if cat in ("全部", cls._shop_category(i))))
        if cat in ("全部", "头衔", "头像框", "名片框"):
            # 稀有度按档位顺序接在后面。直接 append 漏掉的档会让顺序变成
            # 「稀有 / 史诗 / 普通 / 传说 / 至臻」—— 商品自带的排前面、补的排后面。
            others = [s for s in subs if s not in slg_titles.RARITY_ORDER]
            subs = others + [r for r in slg_titles.RARITY_ORDER if r not in others]
        return subs

    def _on_shop_grid_configure(self, _event=None):
        if self._shop_resize_job is not None:
            try:
                self.after_cancel(self._shop_resize_job)
            except (ValueError, tk.TclError):
                pass
        self._shop_resize_job = self.after(80, self._shop_grid_resized)

    def _shop_columns_now(self):
        """当前网格宽度下的 (列数, 卡宽)，未 map 时回落到默认卡宽。"""
        grid_w = self._shop_grid.winfo_width()
        if grid_w <= 1:
            return 1, self.SHOP_TILE_W
        available = self._shop_grid._reverse_widget_scaling(grid_w)
        return shop_columns(available, self.SHOP_TILE_W, self.SHOP_TILE_W_MIN,
                            self.SHOP_TILE_GAP)

    def _shop_grid_resized(self):
        """窗口拉宽/缩窄后，每行放得下的张数变了就重排一次。"""
        self._shop_resize_job = None
        if getattr(self, "_panel_mode", None) != "shop":
            return
        cols = self._shop_columns_now()
        if cols == getattr(self, "_shop_cols", None):
            return
        self._shop_cols = cols
        self._render_shop()

    def _shop_rail_view(self, items, cats, subs, cat, sub):
        """左栏：上面主分类、下面当前分类的细分。

        选中态是左侧一根竖条 + 高亮字，不是整块填色 —— 一栏里同时存在主分类和
        子分类，全填色会分不出谁套着谁。
        """
        rail = self._shop_rail
        for child in rail.winfo_children():
            child.destroy()
        ctk.CTkLabel(rail, text="分类", text_color=MUTED, anchor="w",
                     font=ui_font(size=10)).pack(fill="x", pady=(0, 2))
        for value in ["全部"] + cats:
            self._rail_entry(rail, value, value == cat, 11,
                             lambda v=value: self._pick_shop_filter("cat", v))
        ctk.CTkLabel(rail, text="细分", text_color=MUTED, anchor="w",
                     font=ui_font(size=10)).pack(fill="x", pady=(12, 2))
        for value in ["全部"] + subs:
            self._rail_entry(rail, value, value == sub, 10,
                             lambda v=value: self._pick_shop_filter("sub", v))

    def _rail_entry(self, parent, text, active, size, command):
        row = ctk.CTkFrame(parent, fg_color="transparent", height=24)
        row.pack(fill="x", pady=1)
        ctk.CTkFrame(row, width=3, height=14, corner_radius=2,
                     fg_color=ACCENT if active else "transparent").pack(
            side="left", padx=(0, 5))
        label = ctk.CTkLabel(row, text=text, anchor="w",
                             text_color=ACCENT if active else TEXT,
                             font=ui_font(size=size,
                                          weight="bold" if active else "normal"))
        label.pack(side="left", fill="x", expand=True)
        for widget in (row, label):
            widget.bind("<Button-1>", lambda e=None, c=command: c())

    def _pick_shop_filter(self, kind, value):
        if kind == "cat":
            self._shop_cat = value
            self._shop_sub = "全部"  # a subcategory only means something under its own
        else:
            self._shop_sub = value
        self._render_shop()

    def _shop_lottery_card(self, parent):
        dev = self._is_admin_mode()
        draws_today = (getattr(self, "_cloud_me", {}).get("draws_today", 0)
                       if self._has_cloud_account() else
                       slg_titles.lottery_draws_today(self.conn))
        done = not dev and draws_today >= slg_titles.LOTTERY_DAILY_LIMIT
        row = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
        row.pack(fill="x", padx=16, pady=(10, 2))
        row.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(row, width=4, height=18, fg_color=ACCENT,
                     corner_radius=2).grid(
            row=0, column=0, rowspan=2, sticky="nsw", padx=(12, 10),
            pady=8)
        ctk.CTkLabel(row, text="每日抽奖", text_color=ACCENT,
                     font=ui_font(size=13, weight="bold")).grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 0))
        blurb = ("\u7ba1\u7406\u5458\u8eab\u4efd\u4e0d\u53c2\u4e0e\u6bcf\u65e5\u62bd\u5956\uff1b\u8bf7\u5207\u6362\u5230\u666e\u901a\u7528\u6237\u8eab\u4efd\u540e\u4f7f\u7528"
                 if dev else
                 "5 \u79ef\u5206/\u6b21 \u00b7 \u6bcf\u65e5 %d \u6b21 \u00b7 \u5927\u5956\u4fdd\u5e95 %d \u62bd" % (
                     slg_titles.LOTTERY_DAILY_LIMIT, slg_titles.LOTTERY_PITY))
        blurb_label = ctk.CTkLabel(
            row, text=blurb, text_color=MUTED, anchor="w", justify="left",
            wraplength=520, font=ui_font(size=11))
        blurb_label.grid(row=1, column=1, columnspan=2, sticky="ew",
                         padx=(0, 12), pady=(2, 8))

        def _wrap_lottery_blurb(event):
            width = max(100, blurb_label._reverse_widget_scaling(event.width - 4))
            if getattr(blurb_label, "_lottery_wraplength", None) != width:
                blurb_label._lottery_wraplength = width
                blurb_label.configure(wraplength=width)

        blurb_label.bind("<Configure>", _wrap_lottery_blurb)
        if dev:
            ctk.CTkLabel(row, text="\u4ec5\u666e\u901a\u7528\u6237\u53ef\u7528",
                         text_color=MUTED, font=ui_font(size=11)).grid(
                row=0, column=2, sticky="e", padx=(8, 12), pady=(8, 0))
        elif done:
            ctk.CTkLabel(row, text="今日已抽", text_color=MUTED,
                         font=ui_font(size=11)).grid(
                row=0, column=2, sticky="e", padx=(8, 12), pady=(8, 0))
        elif self._remote_flags.get("disable_lottery"):
            ctk.CTkLabel(row, text="维护中", text_color=MUTED,
                         font=ui_font(size=11)).grid(
                row=0, column=2, sticky="e", padx=(8, 12), pady=(8, 0))
        else:
            ctk.CTkButton(row, text="打开抽奖", width=76, height=26, corner_radius=8,
                          fg_color=ACCENT, text_color=ON_ACCENT,
                          hover_color=CARD_HOVER, font=ui_font(size=12),
                          command=self._do_lottery).grid(
                row=0, column=2, sticky="e", padx=(8, 12), pady=(6, 0))

    def _do_lottery(self):
        if not self._require_regular_user_mode("\u6bcf\u65e5\u62bd\u5956"):
            return
        if not self._require_personal_access("\u6bcf\u65e5\u62bd\u5956"):
            return
        if self._remote_flags.get("disable_lottery"):
            messagebox.showinfo("\u6bcf\u65e5\u62bd\u5956", "\u62bd\u5956\u529f\u80fd\u7ef4\u62a4\u4e2d\uff0c\u7a0d\u540e\u518d\u8bd5", parent=self)
            return
        self._open_slot_machine()

    @staticmethod
    def _lottery_request_pref_name():
        """Keep an uncertain cloud draw bound to its account across restarts."""
        current = slg_account.session()
        if not current or not current.get("account_id"):
            raise slg_account.AccountError("请先登录云端账号")
        return "lottery.pending_request." + str(current["account_id"])

    def _refresh_shop_balance(self):
        lbl = getattr(self, "_shop_balance_label", None)
        if lbl is not None and lbl.winfo_exists():
            lbl.configure(text=("云端 %s 积分" % getattr(self, "_cloud_balance", "读取中")
                                if self._has_cloud_account() else
                                "%d 积分" % slg_db.points_balance(self.conn)))

    @staticmethod
    def _lottery_label(prize):
        # 文案只有一份，在 slg_titles 里 —— 弹窗、历史记录卡和测试读同一句话，
        # 「重复头衔折算积分」这种分支才不会三处各写一遍。
        return slg_titles.lottery_prize_text(prize)

    @staticmethod
    def _lottery_finals(prize):
        """The three symbols the reels land on, read straight off the paytable.

        This used to invent its own mapping and add a fake near-miss, so the
        reels could show a combination the published table said nothing about.
        Now the symbols are a lookup into the same table the 「抽奖概率」 dialog
        renders, which is the only way the two can be guaranteed to agree.
        """
        symbols = slg_titles.symbols_for_prize(prize)
        if symbols is None:  # the losing tier: any combination that pays nothing
            return random.choice(slg_titles.LOTTERY_MISSES)
        return tuple(random.choice(slg_titles.SLOT_SYMBOLS)
                     if name == "*" else name for name in symbols)

    def _open_paytable(self, parent=None):
        """抽奖概率：每档中奖组合、奖品与概率，外加免责声明。

        符号画的是 assets/slot 里那批真图（与滚轮同一份），所以「说明上写的」
        和「滚轮上转出来的」不可能不一致——这正是之前那个假擦边演出的问题。
        """
        win = self._new_dialog("抽奖概率", "440x600", parent=parent)
        ctk.CTkLabel(win, text="抽奖概率", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(
            anchor="w", padx=18, pady=(16, 2))
        ctk.CTkLabel(win, text="每次抽奖消耗 %d 积分。概率是长期期望，%d 抽保底"
                               "只保「幸运星」，不保「幸运之王」"
                     % (slg_titles.LOTTERY_COST, slg_titles.LOTTERY_PITY),
                     text_color=MUTED, font=ui_font(size=10), justify="left",
                     wraplength=400).pack(anchor="w", padx=18, pady=(0, 10))
        imgs = self._slot_symbol_images(24)
        body = ctk.CTkScrollableFrame(win, fg_color="transparent", width=410,
                                      height=330)
        body.pack(fill="both", expand=True, padx=14)
        for symbols, prize_text, percent in slg_titles.lottery_paytable():
            row = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8)
            row.pack(fill="x", pady=3)
            start = ctk.CTkFrame(row, fg_color="transparent", width=104,
                                 height=34)
            start.pack(side="left", padx=(8, 6), pady=6)
            start.pack_propagate(False)
            if symbols is None:
                ctk.CTkLabel(start, text="图案不搭", text_color=MUTED,
                             font=ui_font(size=10)).pack(expand=True)
            else:
                for name in symbols:
                    if name == "*":
                        ctk.CTkLabel(start, text="任意", text_color=MUTED,
                                     fg_color=CHIP, corner_radius=4, width=24,
                                     height=20,
                                     font=ui_font(size=9)).pack(
                            side="left", padx=1)
                    elif imgs.get(name) is not None:
                        ctk.CTkLabel(start, image=imgs[name], text="").pack(
                            side="left", padx=1)
            ctk.CTkLabel(row, text=prize_text, text_color=TEXT,
                         font=ui_font(size=12)).pack(side="left")
            # 两位小数：0.01% 用 %.0f 会显示成「0%」，等于把最高一档抹掉。
            ctk.CTkLabel(row, text="%.2f%%" % percent, text_color=ACCENT,
                         font=ui_font(size=12, weight="bold")).pack(
                side="right", padx=12)
        ctk.CTkLabel(win, text=SLOT_DISCLAIMER, text_color=MUTED,
                     font=ui_font(size=9), wraplength=400,
                     justify="left").pack(padx=18, pady=(10, 16))
        self._fit_dialog(win, 440, parent=parent)

    def _lottery_sound_on(self):
        return slg_db.get_pref(self.conn, "sound.lottery", "1") == "1"

    def _shake_window(self, win, amp=5):
        """Rattle the dialog for a beat - the thump of a reel landing."""
        try:
            size, _sep, pos = win.geometry().partition("+")
            x, y = (int(v) for v in pos.split("+"))
        except (ValueError, tk.TclError):
            return
        if self.reduced_motion:
            return
        scheduler = MotionScheduler(win)
        offsets = ((amp, -amp), (-amp, amp), (amp, -amp), (-amp, amp), (0, 0))

        def step(i=0):
            if i >= len(offsets):
                return
            try:
                if not win.winfo_exists():
                    return
                dx, dy = offsets[i]
                win.geometry("%s+%d+%d" % (size, x + dx, y + dy))
            except tk.TclError:
                return
            scheduler.call_later(35, lambda: step(i + 1))

        step()

    def _result_pulse(self, label, text, grand):
        """A few frames of glitch jitter on the result line.

        Touches only the label - never an overlay - so the result stays visible
        the whole time. This replaces the old full-window stipple flash, whose
        opaque canvas is what made the result read as a black screen.
        """
        if self.reduced_motion:
            label.configure(text=text, text_color=ACCENT if grand else TEXT)
            return
        scheduler = MotionScheduler(label)
        color = ACCENT if grand else TEXT
        glitch = "01#><*"
        frames = 10

        def step(i=0):
            try:
                if not label.winfo_exists():
                    return
            except tk.TclError:
                return
            if i >= frames:
                label.configure(text=text, text_color=color)
                return
            if i % 2:
                label.configure(text="".join(
                    random.choice(glitch) if random.random() < 0.3 else ch
                    for ch in text), text_color=_mix(color, "#ffffff", 0.5))
            else:
                label.configure(text=text, text_color=color)
            scheduler.call_later(50, lambda: step(i + 1))

        step()

    def _slot_symbol_images(self, size):
        """Every reel symbol the paytable can name, loaded once and cached per size.

        The names come from slg_titles.SYMBOL_NAMES rather than a list written out
        here: a hand-kept list is what left the crown out, so a jackpot planted
        three symbols the loader had never heard of and the winning row came up
        empty - the "three empty boxes" bug wearing a new hat.

        Kept on self so tkinter does not garbage-collect the PhotoImages mid-draw.
        """
        cache = getattr(self, "_slot_imgs", None)
        if cache is None:
            cache = self._slot_imgs = {}
        if size not in cache:
            imgs = {}
            for name in slg_titles.SYMBOL_NAMES:
                try:
                    im = Image.open(asset_path("slot/" + name + ".png")
                                    ).convert("RGBA")
                    imgs[name] = ImageTk.PhotoImage(
                        im.resize((size, size), Image.LANCZOS))
                except Exception:  # noqa: BLE001 - a missing symbol must not crash the draw
                    imgs[name] = None
            cache[size] = imgs
        return cache[size]

    def _open_slot_machine(self, prize=None):
        """Open the reel UI; the first lever pull is the draw commit point."""
        existing = getattr(self, "_lottery_machine_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    return
            except tk.TclError:
                pass
            self._lottery_machine_window = None
        initial_prize = prize
        prize = None
        grand = False
        jackpot = False
        finals = []
        label = ""
        pending = [False]

        def close_machine():
            # Keep WM_DELETE_WINDOW and the app close handler non-blocking. A
            # nested messagebox here can make both windows impossible to close.
            if pending[0]:
                self._set_progress("抽奖结果正在确认；关闭窗口不会取消请求")
            elif spinning or stopping:
                self._set_progress("开奖动画已停止显示；抽奖结果已经确认")
            if getattr(self, "_lottery_machine_window", None) is win:
                self._lottery_machine_window = None
            if getattr(self, "_lottery_request_complete", None) is complete_draw:
                self._lottery_request_complete = None
            if getattr(self, "_lottery_status_label", None) is result:
                self._lottery_status_label = None
            win.destroy()

        win = self._new_dialog("\u6bcf\u65e5\u62bd\u5956", close_guard=close_machine)
        self._lottery_machine_window = win
        cell, rows, slots, pad = 52, 3, 14, 4
        height, tiles = reel_geometry(cell, rows, pad, slots)

        rim = ctk.CTkFrame(win, fg_color=ACCENT, corner_radius=12)
        rim.pack(fill="both", expand=True, padx=1, pady=1)
        card = ctk.CTkFrame(rim, fg_color=BG, corner_radius=11)
        card.pack(fill="both", expand=True, padx=2, pady=2)

        ctk.CTkLabel(card, text="每日抽奖", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(pady=(14, 8))
        ctk.CTkLabel(card, text="拉杆后提交抽奖并扣除 5 积分；结果确认后播放开奖动画。",
                     text_color=MUTED, font=ui_font(size=10),
                     wraplength=450, justify="center").pack(padx=14, pady=(0, 7))

        # 声明高度按内容给（滚轮 + 结果行 + 彩带 + 中奖头衔徽记那一行），
        # 箱子本身是物理像素、CTk 的 height 是缩放单位，所以要除以缩放系数。
        scale = ctk.ScalingTracker.get_window_scaling(win)
        body = ctk.CTkScrollableFrame(card, fg_color="transparent", width=470,
                                      height=int((height + 230) / scale))
        body.pack(fill="both", expand=True)

        machine = ctk.CTkFrame(body, fg_color=CARD, corner_radius=12)
        machine.pack(padx=16, pady=(0, 4))
        holder = ctk.CTkFrame(machine, fg_color="transparent")
        holder.pack(side="left", padx=(10, 4), pady=10)

        imgs = self._slot_symbol_images(cell)
        reels = []
        for idx in range(3):
            box = ctk.CTkFrame(holder, fg_color=BG, corner_radius=8)
            box.pack(side="left", padx=5)
            canvas = tk.Canvas(box, width=cell + 2 * pad, height=height,
                               bg=BG, highlightthickness=0)
            canvas.pack()
            # The middle row is the payline: a faint accent frame around it.
            payline = canvas.create_rectangle(
                pad, pad + cell, pad + cell, pad + 2 * cell,
                outline=_mix(CARD, ACCENT, 0.5), width=1)
            # The tape is a loop, so slot 1 is where the reel must come to rest.
            # Filling `tiles` entries from that loop (rather than one pass of
            # `slots`) is what keeps symbols under the window at every offset.
            cycle = [slg_titles.SLOT_SYMBOLS[0] for _ in range(slots)]
            strip = []
            symbol_items = []
            for k in range(tiles):
                img = imgs.get(cycle[k % slots])
                if img is None:
                    continue
                item = canvas.create_image(
                    pad + cell / 2, pad + k * cell + cell / 2, image=img)
                strip.append(item)
                symbol_items.append((k % slots, item))
            reels.append({"canvas": canvas, "strip": strip, "cycle": cycle,
                          "symbol_items": symbol_items, "travel": 0.0,
                          "i": 0, "schedule": [], "crossed": 0,
                          "payline": payline, "speed": 0.10})

        lever = ctk.CTkButton(machine, text="拉杆", width=60, height=120,
                              corner_radius=10, fg_color=ACCENT,
                              text_color=ON_ACCENT, hover_color=CARD_HOVER,
                              font=ui_font(size=15, weight="bold"))
        lever.pack(side="left", padx=(0, 10), pady=10)

        result = ctk.CTkLabel(body, text="\u62c9\u52a8\u62c9\u6746\u5f00\u59cb\u62bd\u5956", text_color=MUTED,
                              font=ui_font(size=15, weight="bold"))
        result.pack(pady=(12, 0))
        prize_slot = ctk.CTkFrame(body, fg_color="transparent")
        prize_slot.pack()
        confetti = tk.Canvas(body, width=440, height=84, bg=BG,
                             highlightthickness=0)
        confetti.pack()
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(pady=(8, 0))
        ctk.CTkButton(actions, text="抽奖概率", height=34, width=100,
                      corner_radius=8, fg_color=CHIP, text_color=TEXT,
                      hover_color=CARD_HOVER, font=ui_font(size=13),
                      command=lambda: self._open_paytable(win)).pack(
            side="left", padx=(0, 8))
        button = ctk.CTkButton(actions, text="知道了", height=34, width=120,
                               corner_radius=8, fg_color=ACCENT,
                               text_color=ON_ACCENT, hover_color=CARD_HOVER,
                               font=ui_font(size=13), state="disabled",
                               command=lambda: (close_machine(), self.open_shop()))
        button.pack(side="left")
        # 免责声明：奖品是软件内的积分和虚拟头衔，不是钱，也不是赌博。
        # 保底进度：为什么值得接着抽，得让用户看见，不能只写在奖项说明里。
        ctk.CTkLabel(card, text="保底进度 %d / %d" % (
            getattr(self, "_cloud_me", {}).get("lottery_pity", 0)
            if self._has_cloud_account() else slg_titles.lottery_pity(self.conn),
            slg_titles.LOTTERY_PITY),
            text_color=MUTED, font=ui_font(size=10)).pack(pady=(8, 0))
        ctk.CTkLabel(card, text=SLOT_DISCLAIMER, text_color=MUTED,
                     font=ui_font(size=9), wraplength=440,
                     justify="left").pack(padx=16, pady=(10, 12))
        self._fit_dialog(win, 520)
        sound = self._lottery_sound_on()
        animation = MotionScheduler(win)
        spinning = False
        stopping = False
        settled = [0]

        def _advance(r, step):
            r["travel"] += step
            for item in r["strip"]:
                r["canvas"].move(item, 0, -step * cell)
            # The same comparison that used to be `>= slots`: a deceleration can
            # land travel on 11.999999999999998, and `>= 12` is False, so the
            # tape never wrapped and the reels came out empty. The epsilon folds
            # that last sliver of a revolution back into the loop.
            while r["travel"] >= slots - 1e-9:
                r["travel"] -= slots
                for item in r["strip"]:
                    r["canvas"].move(item, 0, slots * cell)

        def _alive(r):
            try:
                return r["canvas"].winfo_exists()
            except tk.TclError:
                return False

        def _flash_payline(r, color=ACCENT):
            """Light the payline as its reel lands, then fade it back."""
            if not _alive(r):
                return
            r["canvas"].itemconfigure(r["payline"], outline=color)
            animation.call_later(
                180, lambda: _alive(r) and r["canvas"].itemconfigure(
                    r["payline"], outline=_mix(CARD, ACCENT, 0.5)))

        def _spin_tick(r):
            if not _alive(r) or not spinning:
                return
            now = time.perf_counter()
            delta = min(0.1, max(0.0, now - r.get("last_tick", now)))
            r["last_tick"] = now
            if r["canvas"].winfo_ismapped() and not self.reduced_motion:
                r["speed"] = min(0.34, r["speed"] + delta * 3.75)
                _advance(r, r["speed"] * delta / 0.016)
            animation.call_later(16 if not self.reduced_motion else 80, lambda: _spin_tick(r))

        def start_spin(outcome, confirmed_cloud=False):
            nonlocal prize, grand, jackpot, finals, label, spinning, stopping
            if (not isinstance(outcome, dict)
                    or outcome.get("kind") not in ("title", "points")
                    or "value" not in outcome):
                raise ValueError("invalid lottery result")
            new_grand = outcome["kind"] == "title"
            new_jackpot = (new_grand and
                           outcome.get("value") == slg_titles.LOTTERY_JACKPOT_TITLE)
            new_finals = self._lottery_finals(outcome)
            new_label = self._lottery_label(outcome)
            if confirmed_cloud:
                pref_key = getattr(self, "_lottery_pending_pref_key", None)
                if pref_key is None:
                    try:
                        pref_key = self._lottery_request_pref_name()
                    except slg_account.AccountError:
                        # An already confirmed result can outlive its session.
                        # There is no account-specific pending key to clear.
                        pref_key = None
                if pref_key is not None:
                    try:
                        slg_db.set_pref(self.conn, pref_key, "")
                    except (sqlite3.Error, OSError):
                        # The server has already committed this draw. Keep its
                        # idempotency key for a safe retry, but reveal the prize.
                        pass
                    else:
                        self._lottery_pending_pref_key = None
            prize = outcome
            grand = new_grand
            jackpot = new_jackpot
            finals = new_finals
            label = new_label
            pending[0] = False
            for idx, reel in enumerate(reels):
                symbol = finals[idx]
                cycle = [random.choice(slg_titles.SLOT_SYMBOLS) for _ in range(slots)]
                cycle[1] = symbol
                reel["cycle"] = cycle
                for cycle_index, item in reel["symbol_items"]:
                    image = imgs.get(cycle[cycle_index])
                    if image is not None:
                        reel["canvas"].itemconfigure(item, image=image)
            result.configure(text="\u6b63\u5728\u63ed\u6653\u2026")
            spinning = True
            stopping = False
            lever.configure(text="\u505c\u6b62", state="normal")
            for reel in reels:
                reel["last_tick"] = time.perf_counter()
                _spin_tick(reel)

        def complete_draw(prize=None, error=None):
            pending[0] = False
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            if error:
                result.configure(text="\u7ed3\u679c\u5c1a\u672a\u786e\u8ba4\uff0c\u53ef\u5b89\u5168\u91cd\u8bd5", text_color=MUTED)
                lever.configure(text="\u91cd\u8bd5\u786e\u8ba4", state="normal")
                return
            try:
                start_spin(prize, confirmed_cloud=True)
            except (KeyError, TypeError, ValueError, sqlite3.Error,
                    slg_account.AccountError) as exc:
                result.configure(text="\u62bd\u5956\u7ed3\u679c\u65e0\u6cd5\u786e\u8ba4\uff0c\u8bf7\u91cd\u8bd5", text_color=MUTED)
                lever.configure(text="\u91cd\u8bd5\u786e\u8ba4", state="normal")
                result.configure(
                    text="\u62bd\u5956\u7ed3\u679c\u65e0\u6cd5\u786e\u8ba4\uff0c\u53ef\u4f7f\u7528\u540c\u4e00\u8bf7\u6c42\u5b89\u5168\u91cd\u8bd5\uff1a%s" % exc,
                    text_color=MUTED)

        self._lottery_status_label = result

        def _reveal():
            nonlocal stopping
            stopping = False
            result.configure(text=label, text_color=ACCENT if grand else TEXT)
            self._refresh_shop_balance()
            if sound:
                for freq, dur in lottery_jingle(grand, jackpot):
                    _play_tone(freq, dur)
            if grand:
                # 徽记按实际中的那一枚画：写死 LOTTERY_GRAND_TITLE 会把幸运之王
                # 显示成幸运星，等于把最高一档抹成下一档。
                self._title_badge(prize_slot, prize["value"]).pack()
                self._confetti(confetti, 440, 84, 0, label=None,
                               ramp=("#ffd76a", "#e0a800", "#ffffff", "#e84393"))
                if jackpot:
                    _jackpot_sweep()
                    _pulse_paylines(beats=16)
                else:
                    _pulse_paylines()
            else:
                self._confetti(confetti, 440, 84, prize["value"])
            self._result_pulse(result, label, grand)
            button.configure(state="normal")

        def _jackpot_sweep():
            """外框走一整圈色相环，再缓缓落回强调色。

            0.01% 的奖只有一次演出机会，不能跟 1% 的幸运星长得一样 —— 至臻头衔
            那条帧间连续色相环拿来用在这里，慢到看得出是流光。"""
            if self.reduced_motion:
                return
            frames = 60

            def step(i=0):
                try:
                    if not rim.winfo_exists():
                        return
                except tk.TclError:
                    return
                if i >= frames:
                    rim.configure(fg_color=ACCENT)
                    return
                rim.configure(fg_color=_rainbow(i / float(frames)))
                animation.call_later(40, lambda: step(i + 1))

            step()

        def _pulse_paylines(beats=6):
            """A gold heartbeat on all three paylines after a title win."""
            if self.reduced_motion:
                return
            gold, rest = "#ffd76a", _mix(CARD, ACCENT, 0.5)

            def step(i=0):
                for r in reels:
                    if not _alive(r):
                        return
                    r["canvas"].itemconfigure(
                        r["payline"], outline=gold if i % 2 else rest)
                if i < beats:
                    animation.call_later(140, lambda: step(i + 1))

            step()

        def _settle_tick(r):
            if not _alive(r):
                return
            now = time.perf_counter()
            while r["i"] < len(r["schedule"]) and now >= r["settle_due"]:
                delay, step = r["schedule"][r["i"]]
                r["i"] += 1
                _advance(r, step)
                r["settle_due"] += delay / 1000.0
            if sound and int(r["travel"]) > r["crossed"]:
                r["crossed"] = int(r["travel"])
                _play_tone(1500, 14)
            if r["i"] >= len(r["schedule"]):
                self._shake_window(win)
                _flash_payline(r)
                settled[0] += 1
                if settled[0] == 3:
                    _reveal()
                return
            animation.call_later(max(1, int((r["settle_due"] - now) * 1000)),
                                 lambda: _settle_tick(r))

        def stop_all():
            # Land each reel on its planted final symbol: the deceleration runs
            # for a whole cycle plus whatever phase is left, so travel ends at 0
            # (mod slots) and index 1 sits on the middle payline.
            for idx, r in enumerate(reels):
                phase = r["travel"] % slots
                dist = slots + ((-phase) % slots)
                r["schedule"] = ([(0, dist)] if self.reduced_motion
                                 else lottery_spin_schedule(slots=dist))
                r["i"] = 0
                r["crossed"] = 0
                r["settle_due"] = time.perf_counter() + idx * 0.420
                animation.call_later(idx * 420, lambda r=r: _settle_tick(r))

        def toggle():
            nonlocal spinning, stopping
            if pending[0] or stopping:
                return
            if getattr(self, "_lottery_request_active", False):
                result.configure(text="上一笔抽奖仍在确认中，请稍后再试", text_color=MUTED)
                return
            if spinning:
                spinning = False
                stopping = True
                lever.configure(text="\u505c\u6b62\u4e2d", state="disabled")
                stop_all()
                return
            if grand or finals:
                return
            if not self._require_regular_user_mode("\u6bcf\u65e5\u62bd\u5956"):
                return

            pending[0] = True
            lever.configure(text="\u6b63\u5728\u786e\u8ba4", state="disabled")
            result.configure(text="\u6b63\u5728\u786e\u8ba4\u62bd\u5956\u7ed3\u679c\u2026")
            if self._is_admin_mode() and not self._has_usable_cloud_session():
                self._ensure_developer_cloud_identity()
                pending[0] = False
                lever.configure(text="\u62c9\u6746", state="normal")
                result.configure(text="管理员云端身份未连接，请连接后重试",
                                 text_color=MUTED)
                return
            if self._has_cloud_account():
                try:
                    pref_key = self._lottery_request_pref_name()
                    request_id = slg_db.get_pref(self.conn, pref_key, "") or ""
                    if (len(request_id) != 32 or
                            any(c not in "0123456789abcdef" for c in request_id)):
                        request_id = uuid.uuid4().hex
                    # Commit before the network call. A response lost before the
                    # next launch can then be replayed without a second debit.
                    slg_db.set_pref(self.conn, pref_key, request_id)
                    self._lottery_pending_pref_key = pref_key
                except (slg_account.AccountError, OSError, ValueError,
                        sqlite3.Error) as exc:
                    complete_draw(error="本机无法保存抽奖请求，未发送到云端：%s" % exc)
                    return
                self._set_progress("\u6b63\u5728\u4e0e\u4e91\u7aef\u786e\u8ba4\u62bd\u5956")
                self._lottery_request_active = True
                self._lottery_request_complete = complete_draw
                self._run_cloud_action(
                    "lottery", lambda rid=request_id: slg_account.lottery_draw(rid))
                return

            dev = self._is_admin_mode()
            if not dev and slg_titles.lottery_draws_today(self.conn) >= slg_titles.LOTTERY_DAILY_LIMIT:
                pending[0] = False
                lever.configure(text="\u62c9\u6746", state="normal")
                message = "\u4eca\u5929\u7684\u62bd\u5956\u6b21\u6570\u5df2\u7528\u5b8c"
                result.configure(text=message)
                messagebox.showinfo("\u6bcf\u65e5\u62bd\u5956", message, parent=win)
                return
            if slg_db.points_balance(self.conn) < slg_titles.LOTTERY_COST:
                pending[0] = False
                lever.configure(text="\u62c9\u6746", state="normal")
                message = "\u79ef\u5206\u4e0d\u8db3\uff0c\u62bd\u5956\u9700\u8981 %d \u79ef\u5206" % slg_titles.LOTTERY_COST
                result.configure(text=message)
                messagebox.showwarning("\u79ef\u5206\u4e0d\u8db3", message, parent=win)
                return
            ok, message, prize = slg_titles.draw_lottery(self.conn)
            if not ok:
                pending[0] = False
                lever.configure(text="\u62c9\u6746", state="normal")
                result.configure(text=message)
                messagebox.showwarning("\u6bcf\u65e5\u62bd\u5956", message, parent=win)
                return
            if not self._is_admin_mode():
                slg_remote.report(self.conn, "lottery", {"grand": prize["kind"] == "title"})
            self._refresh_shop_balance()
            start_spin(prize)

        lever.configure(command=toggle)
        if initial_prize is not None:
            complete_draw(prize=initial_prize)

    def _shop_tile(self, parent, item, owned, equipped, points, width=None):
        """One shelf product as a fixed-size card.

        Rarity drives the whole look: border, banner and glyph all take the
        rarity colour, and 传说/至臻 keep it moving. Five tiers that differed only
        by the colour of one line of text read as one flat grey shelf - which is
        what "looks like a rush job" was describing.

        `width` comes from shop_columns() so the rack fits two cards a row at the
        default window; the fallback keeps the old fixed size for callers that
        have no grid width to measure.
        """
        width = width or self.SHOP_TILE_W
        is_title = item["kind"] == "title"
        is_decoration = item["kind"] == "decoration"
        color = (self._title_color(item["id"]) if is_title else
                 self._appearance_color(item["id"]) if is_decoration else ACCENT)
        rarity = ((slg_titles.title_by_id(item["id"]) or {}).get("rarity", "")
                  if is_title else item.get("rarity", "") if is_decoration else "")
        has = item["id"] in owned
        if isinstance(equipped, tuple):
            equipped_title = equipped[0]
            equipped_cosmetic = equipped[1] if len(equipped) > 1 else ""
            appearance_map = equipped[2] if len(equipped) > 2 else {}
        else:  # compatibility for direct callers
            equipped_title, equipped_cosmetic = equipped, ""
            appearance_map = {}
        slot = item.get("appearance") or "comment_frame"
        active_item = appearance_map.get(slot) if isinstance(appearance_map, dict) else ""
        if not active_item and slot == "comment_frame":
            active_item = equipped_cosmetic
        using = has and item["id"] == (
            active_item if item["kind"] == "decoration" else equipped_title)
        cost = item.get("cost") or 0
        short = 0 if (item.get("locked") or has) else max(0, cost - points)

        card = ctk.CTkFrame(parent, width=width,
                            height=self.SHOP_TILE_H, fg_color=CARD,
                            corner_radius=10, border_width=1,
                            border_color=self._tile_border(color, rarity, has))
        card.pack_propagate(False)

        # Bottom action row first, so the preview fills whatever is left above it.
        foot = ctk.CTkFrame(card, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        if item.get("locked"):
            self._tile_note(foot, "即将开放")
        elif item.get("cloud_only") and not self._has_cloud_account():
            ctk.CTkButton(
                foot, text="登录后购买", height=26, corner_radius=8,
                fg_color=CHIP, text_color=ACCENT, hover_color=CARD_HOVER,
                font=ui_font(size=11), command=self.open_profile).pack(fill="x")
        elif item["kind"] == "makeup" and self._has_cloud_account():
            self._tile_note(foot, "云端补签暂未开放")
        elif item["kind"] == "makeup":
            ctk.CTkButton(foot, text="补签", height=26, corner_radius=8,
                          fg_color=ACCENT, text_color=ON_ACCENT,
                          hover_color=CARD_HOVER, font=ui_font(size=12),
                          command=lambda i=item, c=card: self._use_makeup(i, c)
                          ).pack(fill="x")
        elif has:
            if item["kind"] == "decoration":
                ctk.CTkButton(
                    foot, text="卸下" if using else "装备", height=26,
                    corner_radius=8, fg_color=CHIP if using else ACCENT,
                    text_color=TEXT if using else ON_ACCENT,
                    hover_color=CARD_HOVER, font=ui_font(size=12),
                    command=lambda i=item, s=slot, active=using:
                        self._equip_appearance(s, "" if active else i["id"])
                ).pack(fill="x")
            else:
                self._tile_note(foot, "使用中" if using else "已拥有")
        else:
            ctk.CTkButton(foot, text="兑换", height=26, corner_radius=8,
                          fg_color=ACCENT, text_color=ON_ACCENT,
                          hover_color=CARD_HOVER, font=ui_font(size=12),
                          command=lambda i=item, c=card: self._buy_item(i, c)
                          ).pack(fill="x")

        preview = ctk.CTkFrame(card, fg_color="transparent")
        preview.pack(fill="both", expand=True)

        band = self._tile_band(preview, item, color, rarity)
        band.pack(fill="x", padx=6, pady=(6, 0))
        if rarity == "至臻":
            self._pulse_border(card, color)
        if has:
            ctk.CTkLabel(band, text="使用中" if using else "已拥有", height=16,
                         corner_radius=8,
                         fg_color=ACCENT if using else CHIP,
                         text_color=ON_ACCENT if using else MUTED,
                         font=ui_font(size=9)).place(relx=1.0, x=-4, y=4,
                                                     anchor="ne")

        name_row = ctk.CTkFrame(preview, fg_color="transparent")
        name_row.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(name_row, text=item["name"],
                     text_color=MUTED if has else color,
                     font=ui_font(size=12, weight="bold")).pack(side="left")
        if item.get("limited_until"):
            ctk.CTkLabel(name_row, text="限时", text_color=ON_ACCENT,
                         fg_color=ACCENT, corner_radius=6, height=14,
                         font=ui_font(size=9)).pack(side="left", padx=(4, 0))

        price = ctk.CTkFrame(preview, fg_color="transparent")
        price.pack(fill="x", padx=8, pady=(2, 0))
        if short:
            # 「还差 N 分」比一个买不起的价格有用：它是个缺口，不是一句拒绝。
            ctk.CTkLabel(price, text="还差 %d 分" % short, text_color=MUTED,
                         font=ui_font(size=10)).pack(anchor="w")
            bar = ctk.CTkProgressBar(price, height=4, corner_radius=2,
                                     fg_color=CHIP, progress_color=ACCENT)
            bar.set(min(1.0, points / cost) if cost else 0.0)
            bar.pack(fill="x", pady=(3, 0))
        else:
            ctk.CTkLabel(price, text="%d 积分" % cost, text_color=MUTED,
                         font=ui_font(size=10)).pack(anchor="w")

        # Bound last so the banner, the state chip and the price block are all
        # one hit target - binding preview earlier left the banner dead.
        self._clickable(preview, lambda e=None, i=item: self._shop_detail(i))
        return card

    @staticmethod
    def _tile_border(color, rarity, has):
        """卡面描边。已拥有的一律压成灰：货架上要一眼分出「我有的」和「还没有的」。"""
        if has:
            return CHIP
        if rarity == "传说":
            return _mix(BG, color, 0.8)
        if rarity == "史诗":
            return _mix(BG, color, 0.55)
        return _mix(BG, color, 0.3)

    @staticmethod
    def _sparse_widget_animation(widget, frame_count, frame_ms, rest_ms,
                                 draw_frame, reset, start_ms=900):
        """Run a short accent only while mapped, then leave the widget at rest."""
        state = {"alive": True, "mapped": False, "after": None, "frame": 0}

        def cancel_pending():
            job = state["after"]
            state["after"] = None
            if job is not None:
                try:
                    widget.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass

        def schedule(delay):
            if not state["alive"] or not state["mapped"]:
                return
            try:
                state["after"] = widget.after(delay, tick)
            except tk.TclError:
                state["alive"] = False

        def tick():
            state["after"] = None
            if not state["alive"] or not state["mapped"]:
                return
            try:
                root = widget.winfo_toplevel()
                app = root
                while getattr(app, "master", None) is not None:
                    app = app.master
                if getattr(app, "reduced_motion", False):
                    reset()
                    schedule(rest_ms)
                    return
                if not widget.winfo_exists() or not widget.winfo_ismapped():
                    state["mapped"] = False
                    reset()
                    return
                draw_frame(state["frame"])
            except tk.TclError:
                state["alive"] = False
                return
            state["frame"] += 1
            if state["frame"] < frame_count:
                schedule(frame_ms)
            else:
                try:
                    reset()
                except tk.TclError:
                    state["alive"] = False
                    return
                state["frame"] = 0
                schedule(rest_ms)

        def on_map(_event=None):
            if state["alive"]:
                state["mapped"] = True
                if state["after"] is None:
                    state["frame"] = 0
                    schedule(start_ms)

        def on_unmap(_event=None):
            state["mapped"] = False
            cancel_pending()
            state["frame"] = 0
            try:
                reset()
            except tk.TclError:
                state["alive"] = False

        def on_destroy(event):
            if event.widget is widget:
                state["alive"] = False
                state["mapped"] = False
                cancel_pending()

        widget.bind("<Map>", on_map, add="+")
        widget.bind("<Unmap>", on_unmap, add="+")
        widget.bind("<Destroy>", on_destroy, add="+")

    def _tile_band(self, parent, item, color, rarity):
        """卡面主视觉：固定稀有度底色，高档位偶尔掠过一条柔和反光。"""
        holder = ctk.CTkFrame(parent, height=72, fg_color="transparent")
        holder.pack_propagate(False)
        tint = _mix(CARD, color, 0.22)
        c = tk.Canvas(holder, highlightthickness=0, bg=CARD)
        c.pack(fill="both", expand=True)
        glyph = self._shop_glyph(item)
        font = ui_tkfont(size=28, weight="bold")
        paint = {"size": None, "pill": [], "text": None, "shine": None}

        def repaint():
            w, h = c.winfo_width(), c.winfo_height()
            if w <= 1 or h <= 1 or paint["size"] == (w, h):
                return
            paint["size"] = (w, h)
            c.delete("all")
            _rounded_rect(c, 0, 0, w - 1, h - 1, 8, tint)
            # 圆角底色是 _rounded_rect 拼出来的 6 个图元；抓住它们才能在至臻档
            # 整块改色，只改其中一块会让圆角跟主体脱色。
            paint["pill"] = list(c.find_all())
            paint["text"] = c.create_text(w // 2, h // 2, text=glyph,
                                          fill=color, font=font)
            paint["shine"] = c.create_rectangle(
                -40, h * 0.12, -18, h * 0.88,
                fill=_mix(color, "#ffffff", 0.7), outline="", state="hidden")

        c.bind("<Configure>", lambda e=None: repaint())

        if rarity in ("传说", "至臻"):
            steps = 13

            def draw_frame(index):
                if paint["size"] is None:
                    repaint()
                if paint["size"] is None:
                    return
                w, h = paint["size"]
                # 传说用较清晰的香槟金线；至臻的反光更细、更淡，底色和
                # 字色始终固定在本身的稀有度色阶，不再做彩虹换色。
                width = 8 if rarity == "传说" else 5
                strength = 0.45 if rarity == "传说" else 0.28
                glint = _mix(color, "#fff1d7", strength)
                x = -32 + (w + 64) * index / (steps - 1)
                c.coords(paint["shine"], x, 4, x + width, 4,
                         x + width + 12, h - 4, x + 12, h - 4)
                c.itemconfig(paint["shine"], fill=glint, state="normal")

            def reset():
                if paint["shine"] is not None:
                    c.itemconfig(paint["shine"], state="hidden")

            self._sparse_widget_animation(
                c, frame_count=steps, frame_ms=90,
                rest_ms=7600 if rarity == "传说" else 9400,
                draw_frame=draw_frame, reset=reset, start_ms=1100)
        return holder

    @staticmethod
    def _pulse_border(widget, color):
        """至臻卡片边框只偶尔轻提亮，平时保留原来的稀有度边色。"""
        try:
            base = widget.cget("border_color")
        except tk.TclError:
            base = _mix(BG, color, 0.30)

        def draw_frame(index):
            p = 1 - abs(2 * index / 10.0 - 1)
            widget.configure(border_color=_mix(base, color, 0.16 * p))

        def reset():
            widget.configure(border_color=base)

        App._sparse_widget_animation(
            widget, frame_count=11, frame_ms=125, rest_ms=8800,
            draw_frame=draw_frame, reset=reset, start_ms=1400)

    def _clickable(self, widget, handler):
        """Bind a click handler to a widget and every descendant, so an image
        card's whole preview area is one hit target."""
        widget.bind("<Button-1>", handler)
        for child in widget.winfo_children():
            self._clickable(child, handler)

    def _shop_detail(self, item):
        """Click-through detail: a large glyph, name + rarity, a description and
        a single buy action. The card's own button still buys directly."""
        is_title = item["kind"] == "title"
        color = self._title_color(item["id"]) if is_title else ACCENT
        rarity = (slg_titles.title_by_id(item["id"]) or {}).get("rarity", "") \
            if is_title else ""
        desc = item.get("description") or item.get("note") or ""
        win = self._new_dialog(item["name"], "360x460")

        band_color = _mix(BG, color, 0.22)
        band = ctk.CTkFrame(win, height=120, fg_color=band_color,
                            corner_radius=10)
        band.pack(fill="x", padx=16, pady=(16, 0))
        band.pack_propagate(False)
        if is_title:
            self._title_badge(band, item["id"], max_width=300,
                              background=band_color).pack(expand=True)
        else:
            ctk.CTkLabel(band, text=self._shop_glyph(item), text_color=color,
                         font=ui_font(size=56, weight="bold")).pack(expand=True)

        ctk.CTkLabel(win, text=item["name"], text_color=TEXT,
                     font=ui_font(size=18, weight="bold")).pack(pady=(12, 0))
        if rarity:
            ctk.CTkLabel(win, text=rarity, text_color=color,
                         font=ui_font(size=12)).pack(pady=(2, 0))
        if desc:
            ctk.CTkLabel(win, text=desc, text_color=MUTED, font=ui_font(size=12),
                         justify="left", wraplength=300).pack(
                fill="x", padx=16, pady=(12, 0))

        locked = item.get("locked")
        # 价格照常报数字，锁定状态由下面的按钮说 —— 两个地方都写「即将开放」，
        # 一张卡上就重复了两遍同一句废话。
        ctk.CTkLabel(win, text="%d 积分" % item["cost"], text_color=ACCENT,
                     font=ui_font(size=16, weight="bold")).pack(pady=(12, 0))

        if locked:
            ctk.CTkButton(win, text="即将开放", height=36, corner_radius=8,
                          state="disabled", fg_color=CHIP, text_color=MUTED,
                          font=ui_font(size=13)).pack(fill="x", padx=16, pady=(16, 0))
        elif item.get("cloud_only") and not self._has_cloud_account():
            ctk.CTkButton(
                win, text="登录云端账号后购买", height=36, corner_radius=8,
                fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                font=ui_font(size=13),
                command=lambda: (win.destroy(), self.open_profile())
            ).pack(fill="x", padx=16, pady=(16, 0))
        elif item["kind"] == "makeup" and self._has_cloud_account():
            ctk.CTkButton(win, text="云端补签暂未开放", height=36, corner_radius=8,
                          state="disabled", fg_color=CHIP, text_color=MUTED,
                          font=ui_font(size=13)).pack(fill="x", padx=16, pady=(16, 0))
        elif item["kind"] == "makeup":
            ctk.CTkButton(win, text="补签一次", height=36, corner_radius=8,
                          fg_color=ACCENT, text_color=ON_ACCENT,
                          hover_color=CARD_HOVER, font=ui_font(size=13),
                          command=lambda: (win.destroy(), self._use_makeup(item))
                          ).pack(fill="x", padx=16, pady=(16, 0))
        elif item["id"] in (getattr(self, "_cloud_titles", set())
                            if self._has_cloud_account() else slg_db.owned_title_ids(self.conn)):
            if item["kind"] == "decoration" and self._has_cloud_account():
                slot = item.get("appearance") or "comment_frame"
                using = item["id"] == self._equipped_appearance_map().get(slot, "")
                slot_label = "头像框" if slot == "avatar_frame" else "名片框"
                ctk.CTkButton(
                    win, text=("卸下" + slot_label if using else "装备" + slot_label), height=36,
                    corner_radius=8, fg_color=CHIP if using else ACCENT,
                    text_color=TEXT if using else ON_ACCENT,
                    hover_color=CARD_HOVER, font=ui_font(size=13),
                    command=lambda s=slot, active=using, i=item["id"]: (
                        win.destroy(), self._equip_appearance(
                            s, "" if active else i,
                            identity_context=getattr(
                                win, "_slg_identity_context", None)))
                ).pack(fill="x", padx=16, pady=(16, 0))
                return
            ctk.CTkButton(win, text="已拥有", height=36, corner_radius=8,
                          state="disabled", fg_color=CHIP, text_color=MUTED,
                          font=ui_font(size=13)).pack(fill="x", padx=16, pady=(16, 0))
        else:
            ctk.CTkButton(win, text="兑换", height=36, corner_radius=8,
                          fg_color=ACCENT, text_color=ON_ACCENT,
                          hover_color=CARD_HOVER, font=ui_font(size=13),
                          command=lambda: (win.destroy(), self._buy_item(item))
                          ).pack(fill="x", padx=16, pady=(16, 0))

    @staticmethod
    def _tile_note(parent, text):
        ctk.CTkLabel(parent, text=text, text_color=MUTED,
                     font=ui_font(size=11)).pack()

    def _shop_glyph(self, item):
        """The oversized mark on a tile: a rarity glyph for titles, else a kind one."""
        if item["kind"] == "title":
            t = slg_titles.title_by_id(item["id"]) or {}
            return self.SHOP_TITLE_GLYPHS.get(t.get("rarity"), "●")
        return self.SHOP_KIND_GLYPHS.get(item["kind"], "◈")

    def _buy_item(self, item, card=None):
        if not self._require_regular_user_mode("\u5546\u57ce\u8d2d\u4e70"):
            return
        if not self._require_personal_access("购买商城商品"):
            return
        if item.get("cloud_only") and not self._has_cloud_account():
            self.open_profile()
            return
        if self._has_cloud_account():
            self._run_cloud_action("buy", lambda: slg_account.buy(item["id"]))
            self._set_progress("正在云端兑换「%s」…" % item["name"])
            return
        if not slg_titles.buy(self.conn, item):
            messagebox.showwarning("积分不足", "积分不足，无法兑换", parent=self)
            return
        slg_remote.report(self.conn, "buy", {"item": item["id"]})
        self._shop_fly = (self._title_color(item["id"]),
                         "已获得「%s」" % item["name"])
        self._flash_and_reopen(card)

    def _use_makeup(self, item, card=None):
        if not self._require_regular_user_mode("\u8865\u7b7e\u5956\u52b1"):
            return
        if not self._require_personal_access("使用补签卡"):
            return
        if self._has_cloud_account():
            messagebox.showinfo("云端补签", "云端补签尚未开放，本机旧档补签不能增加云端积分。",
                                parent=self)
            return
        ok, msg, bonus = slg_titles.buy_makeup_card(self.conn)
        if not ok:
            messagebox.showwarning("补签卡", msg, parent=self)
            return
        slg_remote.report(self.conn, "buy", {"item": item["id"]})
        if bonus:
            msg += " · 累签 +%d 积分" % bonus
        self._shop_fly = (ACCENT, msg)
        self._flash_and_reopen(card)

    def _flash_and_reopen(self, card, delay=170):
        """兑换成功先让卡面闪一下，再重建货架。

        只重绘的话整块面板无声换掉，看起来像没点中 —— 而一个「兑换成功」的模态
        框又挡操作。这一下比两者都轻。
        """
        if card is not None:
            try:
                if card.winfo_exists():
                    flash = ctk.CTkFrame(card, fg_color="#ffffff",
                                         corner_radius=10)
                    flash.place(x=0, y=0, relwidth=1, relheight=1)

                    def _drop(w=flash):
                        try:
                            if w.winfo_exists():
                                w.destroy()
                        except tk.TclError:
                            pass

                    flash.after(delay, _drop)
            except tk.TclError:
                pass
        self.after(delay + 30, self._reopen_shop_if_showing)

    def _reopen_shop_if_showing(self):
        # The rebuild is scheduled, so the user may have left the shop or picked
        # a game in the meantime - only the shop panel should come back.
        if getattr(self, "_panel_mode", None) == "shop":
            self.open_shop()
        else:
            self._shop_fly = None

    def _shop_fly_in(self, color, text):
        """提货反馈：一块稀有度色的牌子从货架上方落下来。

        tk 的 canvas 没有透明度，所以只做位移不做淡出 —— 落地这个动作本身就是
        「到手了」的信号。
        """
        host = getattr(self, "_shop_grid", None)
        if host is None:
            return
        try:
            if not host.winfo_exists():
                return
        except tk.TclError:
            return
        chip = ctk.CTkLabel(host, text=text, height=26, corner_radius=13,
                            fg_color=color, text_color=ON_ACCENT,
                            font=ui_font(size=12, weight="bold"))
        target = 6
        step = {"y": -30}

        def _drop(w=chip):
            try:
                if w.winfo_exists():
                    w.destroy()
            except tk.TclError:
                pass

        def tick():
            try:
                if not chip.winfo_exists():
                    return
            except tk.TclError:
                return
            step["y"] = min(step["y"] + 6, target)
            chip.place_configure(y=step["y"])
            if step["y"] < target:
                chip.after(16, tick)
            else:
                chip.after(1500, _drop)

        chip.place(relx=0.5, y=step["y"], anchor="n")
        chip.after(16, tick)

    def open_redeem(self, initial_tab="兑换码"):
        if not self._require_regular_user_mode("\u5151\u6362\u7801\u4e0e\u9080\u8bf7\u7801"):
            return
        if not self._require_personal_access("使用兑换码和邀请码"):
            return
        existing = getattr(self, "_redeem_dialog", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    switch_tab = getattr(self, "_redeem_switch_tab", None)
                    if callable(switch_tab):
                        switch_tab(initial_tab)
                    return
            except tk.TclError:
                pass

        win = self._new_dialog("兑换码&邀请码", "420x390")
        self._redeem_dialog = win
        ctk.CTkLabel(
            win, text="兑换码与好友邀请", text_color=TEXT,
            font=ui_font(size=14, weight="bold")).pack(
                fill="x", padx=16, pady=(14, 4))
        tabs = ctk.CTkSegmentedButton(
            win, values=["兑换码", "邀请码"],
            font=ui_font(size=12))
        tabs.pack(fill="x", padx=16, pady=(4, 8))
        pages = ctk.CTkFrame(win, fg_color="transparent")
        pages.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        redeem_page = ctk.CTkFrame(pages, fg_color="transparent")
        invite_page = ctk.CTkFrame(pages, fg_color="transparent")
        invite_body = ctk.CTkScrollableFrame(
            invite_page, fg_color="transparent", corner_radius=0)
        invite_body.pack(fill="both", expand=True)
        self._invite_dialog_content = invite_body
        try:
            current = slg_account.session()
            self._invite_dialog_account_id = (
                str(current.get("account_id") or "")
                if isinstance(current, dict) else None)
        except slg_account.AccountError:
            self._invite_dialog_account_id = None

        ctk.CTkLabel(
            redeem_page, text="新版群内每日码可在云端领取 10 积分及群友头衔。",
            text_color=MUTED, font=ui_font(size=11), wraplength=360,
            justify="left").pack(anchor="w", fill="x", padx=4, pady=(5, 10))
        entry = ctk.CTkEntry(
            redeem_page, placeholder_text="输入兑换码…", height=34,
            corner_radius=8, fg_color=CARD, text_color=TEXT,
            placeholder_text_color=MUTED, border_width=1,
            border_color=CHIP, font=ui_font(size=13))
        entry.pack(fill="x", padx=4)
        feedback = ctk.CTkLabel(
            redeem_page, text="", text_color=MUTED,
            font=ui_font(size=12), wraplength=360, justify="left")
        feedback.pack(anchor="w", fill="x", padx=4, pady=(8, 0))
        self._redeem_feedback = feedback

        def do_redeem():
            if not self._require_regular_user_mode("\u5151\u6362\u7801\u4e0e\u9080\u8bf7\u7801"):
                return
            code = entry.get().strip()
            if not code:
                feedback.configure(text="请输入兑换码。", text_color=DANGER_TEXT)
                return
            if slg_titles.is_cloud_group_code_format(code):
                try:
                    current = slg_account.session()
                except slg_account.AccountError as exc:
                    feedback.configure(text=str(exc), text_color=DANGER_TEXT)
                    return
                if not current:
                    feedback.configure(text="请先在个人中心创建或登录云端账号",
                                       text_color=DANGER_TEXT)
                    return
                feedback.configure(text="正在验证群内每日码并领取云端积分…",
                                   text_color=MUTED)
                self._run_cloud_action(
                    "group", lambda: slg_account.redeem_group(code))
                return
            ok, msg = slg_titles.redeem(self.conn, code)
            if ok:
                slg_remote.report(self.conn, "redeem", {"success": True})
                secret = slg_titles.dev_secret()
                if secret and code == secret.upper():
                    msg += "\u5f00\u53d1\u8005\u6743\u9650\u5df2\u89e3\u9501\uff1b\u53ef\u5728\u4e2a\u4eba\u9875\u4e3b\u52a8\u5207\u6362\u8eab\u4efd"
            feedback.configure(text=msg, text_color=ACCENT if ok else DANGER_TEXT)
            if slg_titles.is_group_code(code):
                feedback.configure(
                    text="旧版群码仅解锁本机头衔；每日 +10 积分请使用新版群码。",
                    text_color=MUTED)

        ctk.CTkButton(
            redeem_page, text="兑换", height=34, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT,
            hover_color=CARD_HOVER, font=ui_font(size=13),
            command=do_redeem).pack(fill="x", padx=4, pady=(12, 0))

        def switch_tab(value):
            if value not in ("兑换码", "邀请码"):
                value = "兑换码"
            for page in (redeem_page, invite_page):
                page.pack_forget()
            if value == "兑换码":
                redeem_page.pack(fill="both", expand=True)
            else:
                invite_page.pack(fill="both", expand=True)
                if self._ensure_referral_context():
                    self._invite_dialog_account_id = self._profile_referral_account_id
                elif self._is_admin_mode():
                    self._profile_referral_account_id = "developer"
                    self._invite_dialog_account_id = "developer"
                    self._profile_referral_unavailable = True
                self._render_invite_dialog()
            try:
                if tabs.get() != value:
                    tabs.set(value)
            except tk.TclError:
                pass

        self._redeem_switch_tab = switch_tab
        tabs.configure(command=switch_tab)
        tabs.set("兑换码")

        def close_redeem():
            if getattr(self, "_redeem_dialog", None) is win:
                self._redeem_dialog = None
            if getattr(self, "_invite_dialog_content", None) is invite_body:
                self._invite_dialog_content = None
                self._invite_referral_entry = None
            win.destroy()

        win._slg_close_guard = close_redeem
        win.protocol("WM_DELETE_WINDOW", close_redeem)
        win.bind("<Escape>", lambda _event: (close_redeem(), "break")[1])
        if initial_tab == "邀请码":
            switch_tab("邀请码")
        else:
            switch_tab("兑换码")

    def _open_collect_dialog(self):
        """Check off which collections the selected game belongs to."""
        if not self._require_personal_access("管理收藏夹"):
            return
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
            if not self._require_personal_access("管理收藏夹"):
                return
            name = entry.get().strip()
            if name and slg_db.create_collection(self.conn, name):
                entry.delete(0, "end")
                self._refresh_collection_menu()
                win.destroy()
                self._open_collect_dialog()

        def save():
            if not self._require_personal_access("管理收藏夹"):
                return
            chosen = [cid for cid, var in checks.items() if var.get()]
            slg_db.set_game_collections(self.conn, game["id"], chosen)
            self._refresh_collection_menu()
            self._apply_achievements()
            win.destroy()
            self.refresh()

        ctk.CTkButton(row, text="新建", width=90, height=32, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      command=add).pack(side="left")
        ctk.CTkButton(row, text="保存", width=90, height=32, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      command=save).pack(side="right")

    def _open_profile_collections(self):
        """个人中心「收藏」卡：列出所有收藏夹，点一个就跳过去。"""
        if not self._require_personal_access("查看个人收藏"):
            return
        cols = slg_db.list_collections(self.conn)
        win = self._new_dialog("我的收藏夹", "360x440")
        ctk.CTkLabel(win, text="点一个收藏夹跳过去", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=16, pady=(14, 8))
        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12)
        if not cols:
            ctk.CTkLabel(body, text="还没有收藏夹。点开任意游戏，用详情页的"
                                    "「收藏夹…」新建一个。",
                         text_color=MUTED, font=ui_font(size=12), justify="left",
                         wraplength=300).pack(anchor="w", padx=6, pady=10)
        for c in cols:
            def go(name=c["name"]):
                win.destroy()
                self._on_collection(name)
                self._refresh_collection_menu()
            ctk.CTkButton(body, text="%s（%d）" % (c["name"], c["count"]),
                          height=32, corner_radius=8, fg_color=CHIP,
                          text_color=TEXT, hover_color=CARD_HOVER, anchor="w",
                          font=ui_font(size=12),
                          command=go).pack(fill="x", pady=3)
        ctk.CTkButton(win, text="关闭", height=32, corner_radius=8,
                      fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                      command=win.destroy).pack(fill="x", padx=16, pady=(10, 14))

    def open_collection_manager(self):
        """Create and delete collections, from 更多工具."""
        if not self._require_personal_access("管理收藏夹"):
            return
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
            if not self._require_personal_access("管理收藏夹"):
                return
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
        if not self._require_personal_access("删除收藏夹"):
            return False
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
        if not self._require_personal_access("备份和恢复个人数据"):
            return
        win = self._new_dialog("备份与恢复", "440x410")
        ctk.CTkLabel(win, text="备份与恢复", text_color=TEXT,
                     font=ui_font(size=14, weight="bold")).pack(
            fill="x", padx=20, pady=(18, 4))
        ctk.CTkLabel(win, text="导出包含评分、备注、状态、收藏夹、标签译名、签到与积分记录，"
                               "以及手动添加的游戏信息；其中可能有本机游戏路径。\n"
                               "文件不含账号登录密钥和恢复码，请妥善保管，不要公开分享。\n"
                               "导入会覆盖对应的本机个人资料，建议先导出现有备份。",
                     text_color=MUTED, font=ui_font(size=12), justify="left",
                     anchor="w", wraplength=390).pack(
                         fill="x", padx=20, pady=(0, 12))

        def do_export():
            if not self._require_personal_access("导出个人数据"):
                return
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
            if not self._require_personal_access("恢复个人数据"):
                return
            path = filedialog.askopenfilename(
                parent=win, title="导入备份",
                filetypes=[("JSON 文件", "*.json")])
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                data = payload.get("data", payload)
                preview = slg_db.validate_user_data(data)
                path_note = ("\n其中 %d 条手动游戏记录含有本机路径。" %
                             preview["local_path_count"]
                             if preview["local_path_count"] else "")
                prompt = ("将导入 %d 条个人数据记录，并覆盖对应的本机个人资料。"
                          "\n\n建议先导出现有数据备份。%s\n\n是否继续？" % (
                              preview["total_records"], path_note))
                if not messagebox.askyesno(
                        "确认覆盖个人数据", prompt, parent=win,
                        icon="warning", default="no"):
                    return
                slg_db.import_user_data(self.conn, data)
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
        self._report_ui_event("tool_open", "sync_maintenance")
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
        """Show compact tool cards grouped by routine, advanced, and maintenance."""
        self._report_ui_event("tools_open")
        root = self.scan_root()
        scan_blurb = ("把本地游戏库对上号，记录版本号。右键换文件夹。\n当前：%s"
                      % os.path.basename(root)) if root else \
                     "把本地游戏库对上号，记录版本号。点它或右键先选文件夹。"
        win = self._new_dialog("更多工具", "500x620")
        ctk.CTkLabel(
            win, text="常用入口在上方；进阶工具有 4 项，点击整行展开。",
            text_color=MUTED, font=ui_font(size=12), justify="left",
            anchor="w").pack(fill="x", padx=16, pady=(10, 2))
        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=0, pady=(2, 8))
        routine = (
            ("检查更新",
             "看看本地哪些游戏落后于站点新版。",
             self.do_updates, None, "check_updates"),
            ("管理收藏夹…",
             "新建或删除收藏夹，整理你的个人游戏库。",
             self.open_collection_manager, None, "collection_manager"),
            ("备份与恢复…",
             "把评分、评论、收藏、标签排除导出成文件，或从文件恢复。",
             self.open_backup, None, "backup_restore"),
        )
        advanced = (
            ("游戏汉化工具…",
             "游戏是英文的？这里有搭配使用的翻译工具。",
             self.open_translation_tools, None, "translation_tools"),
            ("标签译名…",
             "给标签写中文名。改完列表和筛选条立刻跟着变。",
             self.open_tag_editor, None, "tag_translations"),
            ("偏好权重…",
             "从你的五星评分里算出来的标签倾向，正数是你喜欢的。",
             self.open_weights, None, "preference_weights"),
            ("翻译设置…",
             "配置名称和简介用的翻译引擎与密钥。",
             self.open_translate_settings, None, "translation_settings"),
        )
        maintenance = (
            ("扫描本地目录…",
             scan_blurb,
             self.do_scan, self.pick_scan_root, "scan_local_library"),
            ("扫描本地存档…",
             "看看每个游戏在本机留了多少存档、占多大，可一键打开所在文件夹。",
             self.do_scan_saves, self.open_save_inventory, "scan_local_saves"),
            ("同步与维护…",
             "同步目录数据，或补齐封面与热度信息。",
             self.open_maintenance, None, "sync_maintenance"),
        )
        state = "disabled" if self.busy else "normal"

        def section(title, entries, parent):
            ctk.CTkLabel(
                parent, text=title, text_color=ACCENT,
                font=ui_font(size=12, weight="bold"), anchor="w").pack(
                    fill="x", padx=16, pady=(8, 1))
            for label, blurb, command, on_right, tool_id in entries:
                card = ctk.CTkFrame(
                    parent, fg_color=CARD, corner_radius=8, border_width=1,
                    border_color=CHIP)
                card.pack(fill="x", padx=16, pady=(4, 0))
                if tool_id == "sync_maintenance":
                    # open_maintenance reports itself; do not count this menu
                    # shortcut twice.
                    open_tool = command
                else:
                    open_tool = lambda c=command, tid=tool_id: (
                        self._report_ui_event("tool_open", tid), c())

                def invoke_tool(c=open_tool):
                    win.destroy()
                    c()

                button = ctk.CTkButton(
                    card, text=label, height=30, corner_radius=6, anchor="w",
                    fg_color="transparent", text_color=TEXT,
                    hover_color=CARD_HOVER, font=ui_font(size=13),
                    state=state, command=invoke_tool)
                button.pack(fill="x", padx=5, pady=(4, 0))
                description = ctk.CTkLabel(
                    card, text=blurb, text_color=MUTED,
                    font=ui_font(size=11), justify="left", anchor="w",
                    wraplength=410)
                description.pack(fill="x", padx=13, pady=(0, 6))

                if on_right is not None and state == "normal":
                    def choose_scan_root(event=None, c=on_right, tid=tool_id):
                        win.destroy()
                        self._report_ui_event("tool_open", tid)
                        c()
                        return "break"

                    for widget in (card, button, description):
                        widget.bind("<Button-3>", choose_scan_root)

        section("常用", routine, body)
        advanced_header = ctk.CTkButton(
            body, text="进阶工具　4 项　·　点击展开　⌄", height=42,
            corner_radius=8, anchor="w", fg_color=CARD, text_color=ACCENT,
            hover_color=CARD_HOVER, border_width=1, border_color=ACCENT,
            font=ui_font(size=13, weight="bold"), state=state)
        advanced_header.pack(fill="x", padx=16, pady=(10, 0))
        advanced_body = ctk.CTkFrame(body, fg_color="transparent")
        is_advanced_open = {"value": False}
        section("进阶工具", advanced, advanced_body)
        advanced_body.pack_forget()

        def toggle_advanced():
            is_advanced_open["value"] = not is_advanced_open["value"]
            if is_advanced_open["value"]:
                advanced_body.pack(fill="x", expand=False)
                advanced_header.configure(
                    text="进阶工具　4 项　·　点击收起　⌃",
                    fg_color=CHIP)
            else:
                advanced_body.pack_forget()
                advanced_header.configure(
                    text="进阶工具　4 项　·　点击展开　⌄",
                    fg_color=CARD)

        advanced_header.configure(command=toggle_advanced)
        section("维护", maintenance, body)

    def _report_ui_event(self, event, tool_id=None):
        """Send only the fixed UI event schema; telemetry never blocks UI."""
        if self._is_admin_mode():
            return
        allowed_events = {"tools_open", "settings_open", "tool_open"}
        allowed_tools = {
            "check_updates", "collection_manager", "backup_restore",
            "translation_tools", "tag_translations", "preference_weights",
            "translation_settings", "scan_local_library", "sync_maintenance",
            "scan_local_saves",
        }
        if event not in allowed_events:
            return
        if event == "tool_open":
            if tool_id not in allowed_tools:
                return
            data = {"tool_id": tool_id}
        else:
            data = {}
        try:
            slg_remote.report(self.conn, event, data)
        except Exception:  # noqa: BLE001 - telemetry must not break the UI
            pass

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
        ctk.CTkLabel(win, text="dikgames 是英文游戏资料站，绝大多数游戏没有官方中文，"
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

    def _announcement_has_unread(self):
        local_seen = slg_db.get_pref(
            self.conn, "announcement.read.launch-0.23.0", "") == "1"
        remote = self._remote_config.get("announcement") or {}
        remote_id = str(remote.get("id") or "")
        remote_seen = (not remote_id or slg_db.get_pref(
            self.conn, slg_remote.PREF_ANNOUNCE_SEEN, "") == remote_id)
        return not local_seen or not remote_seen

    def _refresh_announcement_badge(self):
        button = getattr(self, "settings_btn", None)
        if button is None or not button.winfo_exists():
            return
        reward = getattr(self, "_maintenance_reward_status", None) or {}
        campaign = reward.get("campaign") if isinstance(reward, dict) else None
        reward_pending = bool(
            campaign and campaign.get("active") and not reward.get("claimed"))
        unread = self._announcement_has_unread()
        attention = unread or reward_pending
        button.configure(text="⚙ ●" if attention else "⚙",
                         text_color=DANGER_TEXT if attention else TEXT,
                         width=46 if attention else 38)

    def _maybe_prompt_announcement_reward(self):
        """Open the announcement once this launch for a server-confirmed reward."""
        if (getattr(self, "_announcement_reward_prompted", False)
                or not self._has_usable_cloud_session()):
            return False
        status = getattr(self, "_maintenance_reward_status", None)
        if not isinstance(status, dict) or status.get("error"):
            return False
        campaign = status.get("campaign")
        if (not isinstance(campaign, dict) or not campaign.get("active")
                or status.get("claimed")):
            return False
        self._announcement_reward_prompted = True
        self.open_announcement()
        return True

    def _fetch_maintenance_reward_status(self):
        if self._maintenance_reward_fetching:
            return
        if not self._has_usable_cloud_session():
            if self._is_admin_mode():
                self._ensure_developer_cloud_identity()
                self._maintenance_reward_status = None
                self._refresh_announcement_badge()
                self._render_announcement_reward()
                return
            self._maintenance_reward_status = None
            self._refresh_announcement_badge()
            self._render_announcement_reward()
            return
        self._maintenance_reward_fetching = True
        self._run_cloud_action(
            "maintenance_reward_status", slg_account.maintenance_reward_status)

    def _render_announcement_reward(self):
        label = getattr(self, "_announcement_reward_label", None)
        button = getattr(self, "_announcement_claim_button", None)
        if label is None or not label.winfo_exists():
            return
        status = getattr(self, "_maintenance_reward_status", None)
        if not self._has_usable_cloud_session():
            if self._is_admin_mode():
                label.configure(text="正在关联开发者云端身份…")
                button.configure(text="连接中…", state="disabled")
                self._ensure_developer_cloud_identity()
                return
            label.configure(text="登录云端账号后，才能查看和领取账号维护补偿。")
            button.configure(text="登录后查看", state="disabled")
            return
        if status is None:
            label.configure(text="正在查询账号的维护补偿状态…")
            button.configure(text="查询中…", state="disabled")
            self._fetch_maintenance_reward_status()
            return
        if status.get("error"):
            label.configure(text="暂时无法读取全服积分活动；请稍后重试。")
            button.configure(text="暂不可领取", state="disabled")
            button.configure(text="\u91cd\u8bd5\u67e5\u8be2", state="normal",
                             command=self._fetch_maintenance_reward_status)
            return
        campaign = status.get("campaign")
        if not isinstance(campaign, dict) or not campaign.get("active"):
            label.configure(text="当前没有开放中的全服积分活动。")
            button.configure(text="暂无活动", state="disabled")
            return
        if status.get("claimed"):
            label.configure(text="这份账号已领取过本次活动奖励。")
            button.configure(text="已领取", state="disabled")
            return
        if (is_test_build()
                and not (status.get("claimable") or campaign.get("claimable"))):
            label.configure(text="测试包不会领取正式活动奖励。稳定版用户可领取一次 %d 云端积分。"
                            % max(0, int(campaign.get("amount") or 100)))
            button.configure(text="请使用稳定版", state="disabled")
            return
        amount = max(0, int(campaign.get("amount") or 100))
        label.configure(text=(campaign.get("body") or campaign.get("title")
                              or "全服积分活动") +
                        "\n领取后将获得 %d 云端积分。" % amount)
        button.configure(text="手动领取 %d 积分" % amount, state="normal",
                         command=self._claim_maintenance_reward)

    def _claim_maintenance_reward(self):
        if not self._require_regular_user_mode("\u7ef4\u62a4\u8865\u507f"):
            return
        status = getattr(self, "_maintenance_reward_status", None) or {}
        campaign = status.get("campaign")
        server_claimable = bool(
            status.get("claimable") or
            (isinstance(campaign, dict) and campaign.get("claimable")))
        if is_test_build() and not server_claimable:
            self._set_progress("测试版不会领取正式活动积分，请使用稳定版")
            return
        if not self._require_personal_access("领取维护补偿", cloud_only=True):
            return
        status = getattr(self, "_maintenance_reward_status", None) or {}
        campaign = status.get("campaign")
        if (not isinstance(campaign, dict) or not campaign.get("active")
                or not campaign.get("id")):
            self._render_announcement_reward()
            return
        button = getattr(self, "_announcement_claim_button", None)
        if button is not None and button.winfo_exists():
            button.configure(state="disabled", text="正在领取…")
        self._run_cloud_action(
            "maintenance_reward_claim",
            lambda: slg_account.claim_maintenance_reward(campaign["id"]))

    def open_announcement(self):
        # Manual access also satisfies this launch's prompt so a status reply
        # arriving a moment later does not reopen the same dialog.
        if self._has_usable_cloud_session():
            self._announcement_reward_prompted = True
        win = self._new_dialog("公告", "480x600")
        slg_db.set_pref(self.conn, "announcement.read.launch-0.23.0", "1")
        remote_config = (self._remote_config
                         if isinstance(self._remote_config, dict) else {})
        remote = remote_config.get("announcement") or {}
        if not isinstance(remote, dict):
            remote = {}
        if remote.get("id"):
            slg_db.set_pref(self.conn, slg_remote.PREF_ANNOUNCE_SEEN,
                            str(remote.get("id")))

        release_notes = release_notes_text()
        pages = _build_announcement_pages(remote_config, release_notes)

        ctk.CTkLabel(win, text="公告与版本说明", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(
            anchor="w", padx=18, pady=(16, 8))

        page_nav = ctk.CTkFrame(win, fg_color="transparent")
        page_nav.pack(fill="x", padx=18, pady=(0, 8))
        previous_button = ctk.CTkButton(
            page_nav, text="上一页", width=86, height=30, corner_radius=8,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER)
        previous_button.pack(side="left")
        page_indicator = ctk.CTkLabel(
            page_nav, text="", text_color=MUTED, font=ui_font(size=11))
        page_indicator.pack(side="left", expand=True)
        next_button = ctk.CTkButton(
            page_nav, text="下一页", width=86, height=30, corner_radius=8,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER)
        next_button.pack(side="right")

        notes = ctk.CTkTextbox(win, height=245, corner_radius=8, fg_color=BG,
                               text_color=TEXT, border_color=CHIP,
                               border_width=1, font=ui_font(size=12), wrap="word")
        notes.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        self._announcement_reward_label = ctk.CTkLabel(
            win, text="", text_color=MUTED, font=ui_font(size=11),
            wraplength=420, justify="left", anchor="w")
        self._announcement_claim_button = ctk.CTkButton(
            win, text="", height=34, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT,
            hover_color=CARD_HOVER, font=ui_font(size=12),
            command=self._claim_maintenance_reward)
        close_button = ctk.CTkButton(
            win, text="关闭", height=30, corner_radius=8,
            fg_color=CHIP, text_color=TEXT,
            hover_color=CARD_HOVER, command=win.destroy)
        close_button.pack(fill="x", padx=18, pady=(0, 14))

        page_state = {"index": 0}

        def _show_announcement_page(index):
            index = max(0, min(int(index), len(pages) - 1))
            page_state["index"] = index
            page = pages[index]
            text, indicator, claimable = _announcement_page_view(
                page, index, len(pages))
            page_indicator.configure(text=indicator)
            notes.configure(state="normal")
            notes.delete("1.0", "end")
            notes.insert("1.0", text)
            notes.configure(state="disabled")

            if claimable:
                self._announcement_reward_label.pack(
                    fill="x", padx=18, pady=(0, 8), before=close_button)
                self._announcement_claim_button.pack(
                    fill="x", padx=18, pady=(0, 8), before=close_button)
                self._render_announcement_reward()
            else:
                self._announcement_reward_label.pack_forget()
                self._announcement_claim_button.pack_forget()

            previous_button.configure(
                state="normal" if index > 0 else "disabled")
            next_button.configure(
                state="normal" if index < len(pages) - 1 else "disabled")

        previous_button.configure(
            command=lambda: _show_announcement_page(page_state["index"] - 1))
        next_button.configure(
            command=lambda: _show_announcement_page(page_state["index"] + 1))
        if len(pages) <= 1:
            page_nav.pack_forget()
        _show_announcement_page(0)
        self._refresh_announcement_badge()

    def open_settings(self):
        """Show settings in navigable, independently scrollable categories."""
        self._report_ui_event("settings_open")
        win = self._new_dialog("设置", "500x650")
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=8, pady=8)
        nav = ctk.CTkFrame(body, fg_color="transparent")
        content = ctk.CTkFrame(body, fg_color="transparent")
        content.pack(side="left", fill="both", expand=True)

        page_names = (
            ("appearance", "外观", "外观"),
            ("alerts", "提醒与声音", "提醒"),
            ("help", "帮助与反馈", "帮助"),
            ("about", "关于", "关于"),
        )
        pages = {}
        for key, _label, _compact_label in page_names:
            pages[key] = ctk.CTkScrollableFrame(content, fg_color="transparent")
        nav_buttons = {}
        selected_page = {"key": getattr(self, "_settings_page", "appearance")}

        def select_page(key):
            if key not in pages:
                key = "appearance"
            selected_page["key"] = key
            self._settings_page = key
            for page_key, page in pages.items():
                if page_key == key:
                    page.pack(fill="both", expand=True)
                else:
                    page.pack_forget()
            for page_key, button in nav_buttons.items():
                active = page_key == key
                button.configure(
                    fg_color=ACCENT if active else "transparent",
                    text_color=ON_ACCENT if active else TEXT,
                    hover_color=ACCENT if active else CARD)

        for key, label, compact_label in page_names:
            button = ctk.CTkButton(
                nav, text=label, height=36, width=116, corner_radius=8,
                anchor="w", fg_color="transparent", text_color=TEXT,
                hover_color=CARD, font=ui_font(size=12),
                command=lambda target=key: select_page(target))
            nav_buttons[key] = button

        # At high DPI or on a narrow display, clamp the dialog to the work area
        # and move the navigation above the content instead of squeezing both
        # columns until their labels become unreadable.
        layout = {"compact": None}

        def responsive_layout(event=None):
            width = int(getattr(event, "width", body.winfo_width()) or 0)
            try:
                scale = float(ctk.ScalingTracker.get_widget_scaling(body))
            except (AttributeError, TypeError, ValueError, tk.TclError):
                scale = 1.0
            logical_width = width / max(scale, 0.1)
            compact = logical_width < 470
            if layout["compact"] == compact:
                return
            layout["compact"] = compact
            for button in nav_buttons.values():
                button.pack_forget()
                button.grid_forget()
            nav.pack_forget()
            content.pack_forget()
            if compact:
                nav.pack(side="top", fill="x", pady=(0, 6))
                nav.pack_propagate(True)
                for index, (key, _label, compact_label) in enumerate(page_names):
                    button = nav_buttons[key]
                    button.configure(text=compact_label, width=1)
                    button.grid(row=index // 2, column=index % 2,
                                sticky="ew", padx=3, pady=2)
                nav.grid_columnconfigure(0, weight=1)
                nav.grid_columnconfigure(1, weight=1)
                content.pack(side="top", fill="both", expand=True)
            else:
                nav.pack(side="left", fill="y", padx=(0, 8))
                for key, label, _compact_label in page_names:
                    button = nav_buttons[key]
                    button.configure(text=label, width=116)
                    button.pack(fill="x", pady=3)
                content.pack(side="left", fill="both", expand=True)

        body.bind("<Configure>", responsive_layout, add="+")

        wrapped_labels = []

        def page_heading(parent, title):
            ctk.CTkLabel(
                parent, text=title, text_color=TEXT, anchor="w",
                font=ui_font(size=16, weight="bold")).pack(
                    fill="x", padx=12, pady=(16, 10))

        def setting_card(parent, title):
            card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
            card.pack(fill="x", padx=12, pady=(0, 10))
            ctk.CTkLabel(
                card, text=title, text_color=TEXT, anchor="w",
                font=ui_font(size=13, weight="bold")).pack(
                    fill="x", padx=12, pady=(11, 7))
            return card

        def info_note(parent, text):
            note = ctk.CTkFrame(parent, fg_color=CHIP, corner_radius=8)
            note.pack(fill="x", padx=10, pady=(8, 10))
            label = ctk.CTkLabel(
                note, text=text, text_color=MUTED, anchor="w",
                justify="left", font=ui_font(size=11), wraplength=280)
            label.pack(fill="x", padx=10, pady=8)
            wrapped_labels.append(label)
            return label

        def action_row(parent, title, detail, command, emphasized=False):
            card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
            card.pack(fill="x", padx=12, pady=(0, 10))
            button = ctk.CTkButton(
                card, text=title, height=32, corner_radius=7, anchor="w",
                fg_color="transparent", text_color=ACCENT if emphasized else TEXT,
                hover_color=CARD_HOVER, font=ui_font(size=13, weight="bold"),
                command=lambda: (win.destroy(), command()))
            button.pack(fill="x", padx=8, pady=(7, 0))
            label = ctk.CTkLabel(
                card, text=detail, text_color=MUTED, anchor="w",
                justify="left", font=ui_font(size=11), wraplength=280)
            label.pack(fill="x", padx=12, pady=(1, 9))
            wrapped_labels.append(label)
            return card, button

        appearance = pages["appearance"]
        page_heading(appearance, "外观")
        theme_card = setting_card(appearance, "主题")
        switch = ctk.CTkSegmentedButton(
            theme_card, values=[_THEME_LABELS[m] for m in ("light", "dark", "system")],
            height=34, corner_radius=8, fg_color=CHIP, selected_color=ACCENT,
            selected_hover_color=ACCENT, unselected_color=CHIP,
            unselected_hover_color=CARD_HOVER, text_color=TEXT,
            font=ui_font(size=13),
            command=lambda label: self._pick_theme_from(win, label))
        switch.pack(fill="x", padx=10, pady=(0, 0))
        switch.set(_THEME_LABELS[self.theme_mode])
        info_note(
            theme_card,
            "「跟随系统」每 5 秒采样一次 Windows 的浅色/深色设置，"
            "所以会有一小段延迟；手动选浅色或深色则会被记住。")

        scale_card = setting_card(appearance, "界面尺寸")
        scale_values = [entry["label"] for entry in UI_SCALE_PROFILES.values()]
        scale_menu = ctk.CTkOptionMenu(
            scale_card, values=scale_values, height=34, corner_radius=8,
            fg_color=CHIP, button_color=CHIP, button_hover_color=CARD_HOVER,
            text_color=TEXT, font=ui_font(size=12),
            command=self._pick_ui_scale_profile)
        scale_menu.pack(fill="x", padx=10, pady=(0, 0))
        scale_menu.set(UI_SCALE_PROFILES[self.ui_scale_profile]["label"])
        info_note(
            scale_card,
            "只调整本软件窗口和控件，不会修改 Windows 显示分辨率。"
            "分辨率档位是界面缩放预设；「自动」沿用系统 DPI。")

        font_card = setting_card(appearance, "字体大小")
        font_menu = ctk.CTkOptionMenu(
            font_card,
            values=[entry["label"] for entry in FONT_SCALE_PROFILES.values()],
            height=34, corner_radius=8,
            fg_color=CHIP, button_color=CHIP, button_hover_color=CARD_HOVER,
            text_color=TEXT, font=ui_font(size=12),
            command=self._pick_font_scale)
        font_menu.pack(fill="x", padx=10, pady=(0, 0))
        font_menu.set(FONT_SCALE_PROFILES[self.font_scale_profile]["label"])
        info_note(
            font_card,
            "只放大文字，窗口大小和封面尺寸不变。"
            "整体都小就调上面的界面尺寸，只有字看不清就用这一项。")

        motion_card = setting_card(appearance, "交互动效")
        motion_var = ctk.BooleanVar(value=self.reduced_motion)
        def set_motion():
            self.reduced_motion = bool(motion_var.get())
            slg_db.set_pref(self.conn, "ui.reduced_motion", "1" if self.reduced_motion else "0")
        ctk.CTkCheckBox(motion_card, text="减少动效", variable=motion_var,
                       command=set_motion, font=ui_font(size=12), text_color=TEXT,
                       fg_color=ACCENT).pack(anchor="w", padx=10, pady=8)
        info_note(motion_card, "停止持续装饰、彩带和摇窗，保留等待提示和最终结果。")

        alerts = pages["alerts"]
        page_heading(alerts, "提醒与声音")
        sound_card = setting_card(alerts, "每日抽奖音效")
        sound = ctk.CTkSwitch(
            sound_card, text="每日抽奖的转盘声与中奖声", onvalue="1", offvalue="0",
            font=ui_font(size=12), text_color=TEXT, progress_color=ACCENT,
            command=lambda: slg_db.set_pref(
                self.conn, "sound.lottery", sound.get()))
        sound.pack(fill="x", padx=10, pady=(0, 11))
        if self._lottery_sound_on():
            sound.select()
        reward_status = getattr(self, "_maintenance_reward_status", None) or {}
        reward_campaign = reward_status.get("campaign") if isinstance(
            reward_status, dict) else None
        reward_pending = bool(reward_campaign and reward_campaign.get("active")
                              and not reward_status.get("claimed"))
        ann_label = ("公告 · 补偿待领取" if reward_pending else
                     "公告 · 有新内容" if self._announcement_has_unread() else "公告")
        ann_detail = ("有一项补偿等待领取，点击查看公告详情。" if reward_pending else
                      "有尚未查看的新内容。" if self._announcement_has_unread() else
                      "查看公告与近期更新。")
        action_row(alerts, ann_label, ann_detail, self.open_announcement,
                   emphasized=reward_pending or self._announcement_has_unread())

        help_page = pages["help"]
        page_heading(help_page, "帮助与反馈")
        action_row(
            help_page, "查看新手引导…",
            "重新打开首次启动时的那份功能简介和使用说明。",
            self._show_welcome)
        action_row(
            help_page, "意见反馈…",
            "把问题、建议或资料纠错私下发给开发者；被采纳后才可能获得奖励。",
            self.open_feedback)

        about_page = pages["about"]
        page_heading(about_page, "关于")
        action_row(
            about_page, "关于本软件…",
            "版本信息、检查软件更新、GitHub 主页、交流群与数据目录。",
            self.open_about)

        def resize_setting_text(event):
            try:
                scale = float(ctk.ScalingTracker.get_widget_scaling(content))
            except (AttributeError, TypeError, ValueError, tk.TclError):
                scale = 1.0
            wrap = max(120, int(event.width / max(scale, 0.1)) - 56)
            for label in wrapped_labels:
                label.configure(wraplength=wrap)

        content.bind("<Configure>", resize_setting_text, add="+")
        responsive_layout()
        select_page(selected_page["key"])

    def _pick_ui_scale_profile(self, label):
        profile = UI_SCALE_LABEL_TO_PROFILE.get(label, "auto")
        self._apply_ui_scale_profile(profile)

    def _pick_font_scale(self, label):
        """Apply a font size and re-fit the text that is already on screen.

        set_font_scale reaches every CTk widget because they all share the
        cached fonts; the card labels are the plain ones that have to be
        repainted by hand.
        """
        profile = FONT_SCALE_LABEL_TO_PROFILE.get(label, "normal")
        self.font_scale_profile = profile
        if getattr(self, "conn", None) is not None:
            slg_db.set_pref(self.conn, PREF_FONT_SCALE, profile)
        set_font_scale(profile)
        self._refresh_card_text_scaling()

    def open_data_sources(self):
        """Show the public catalogue references in one place."""
        win = self._new_dialog("数据来源", "430x300")
        ctk.CTkLabel(
            win, text="目录资料参考来源", text_color=TEXT,
            font=ui_font(size=16, weight="bold")).pack(
                anchor="w", padx=18, pady=(18, 4))
        ctk.CTkLabel(
            win, text="可点击名称打开对应站点。", text_color=MUTED,
            font=ui_font(size=12), anchor="w").pack(
                anchor="w", padx=18, pady=(0, 8))
        for name, domain, url in (
                ("Dikgames", "dikgames.com", SITE_URL),
                ("F95zone.to.it", "f95zone.to.it", F95_SOURCE_URL)):
            row = ctk.CTkFrame(win, fg_color=CARD, corner_radius=9)
            row.pack(fill="x", padx=16, pady=5)
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            ctk.CTkLabel(text, text=name, text_color=TEXT, anchor="w",
                         font=ui_font(size=13, weight="bold")).pack(anchor="w")
            ctk.CTkLabel(text, text=domain, text_color=MUTED, anchor="w",
                         font=ui_font(size=11)).pack(anchor="w", pady=(1, 0))
            ctk.CTkButton(
                row, text="打开", width=64, height=30, corner_radius=7,
                fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
                font=ui_font(size=11), command=lambda target=url:
                    webbrowser.open(target)).pack(side="right", padx=10)
        ctk.CTkButton(
            win, text="关闭", height=32, corner_radius=8,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=12), command=win.destroy).pack(
                fill="x", padx=16, pady=(8, 14))

    def open_feedback(self):
        """Submit a short private note for the developer to review."""
        if not self._require_regular_user_mode("\u63d0\u4ea4\u666e\u901a\u7528\u6237\u53cd\u9988"):
            return
        if not self._require_personal_access("提交意见反馈", cloud_only=True):
            return
        if getattr(self, "_feedback_submission_pending", False):
            messagebox.showinfo("意见反馈", "上一条反馈正在提交，请稍候。", parent=self)
            return
        win = self._new_dialog("意见反馈", "460x570")
        ctk.CTkLabel(win, text="把遇到的问题或想法告诉开发者",
                     text_color=TEXT, anchor="w",
                     font=ui_font(size=15, weight="bold")).pack(
            fill="x", padx=18, pady=(16, 3))
        ctk.CTkLabel(
            win, text="反馈仅供管理员查看。请勿填写账号密钥等敏感信息；经审核采纳的反馈才可能获得积分或头衔。",
            text_color=MUTED, font=ui_font(size=11), anchor="w",
            justify="left", wraplength=420).pack(
                fill="x", padx=18, pady=(0, 10))

        ctk.CTkLabel(win, text="反馈类型", text_color=TEXT, anchor="w",
                     font=ui_font(size=12)).pack(fill="x", padx=18)
        category_labels = {
            "问题反馈": "bug",
            "功能建议": "suggestion",
            "资料纠错": "data_correction",
            "其他": "other",
        }
        category = tk.StringVar(master=win, value="功能建议")
        ctk.CTkOptionMenu(
            win, variable=category, values=list(category_labels), height=34,
            corner_radius=8, fg_color=CARD, button_color=CHIP,
            button_hover_color=CARD_HOVER, text_color=TEXT,
            font=ui_font(size=12)).pack(fill="x", padx=18, pady=(4, 10))

        ctk.CTkLabel(win, text="内容（20–500 字）", text_color=TEXT,
                     anchor="w", font=ui_font(size=12)).pack(
            fill="x", padx=18)
        body = ctk.CTkTextbox(
            win, height=210, corner_radius=8, fg_color=BG,
            text_color=TEXT, border_width=1, border_color=CHIP,
            font=ui_font(size=12), wrap="word")
        body.pack(fill="both", expand=True, padx=18, pady=(4, 4))
        counter = ctk.CTkLabel(win, text="0 / 500 字", text_color=MUTED,
                               anchor="e", font=ui_font(size=11))
        counter.pack(fill="x", padx=18)
        status = ctk.CTkLabel(win, text="", text_color=MUTED,
                              anchor="w", justify="left",
                              font=ui_font(size=11), wraplength=420)
        status.pack(fill="x", padx=18, pady=(4, 2))
        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(5, 14))
        submit_button = ctk.CTkButton(
            buttons, text="提交反馈", height=36, corner_radius=8,
            fg_color=ACCENT, text_color=ON_ACCENT, font=ui_font(size=12))
        submit_button.pack(side="left", expand=True, fill="x")
        ctk.CTkButton(
            buttons, text="取消", height=36, corner_radius=8,
            fg_color=CHIP, text_color=TEXT, hover_color=CARD_HOVER,
            font=ui_font(size=12), command=win.destroy).pack(
                side="left", expand=True, fill="x", padx=(8, 0))

        form = {"win": win, "body": body, "counter": counter,
                "status": status, "submit": submit_button,
                "category": category, "pending": False}
        self._feedback_form = form

        def update_counter(_event=None):
            if not win.winfo_exists():
                return
            count = len(body.get("1.0", "end-1c").strip())
            counter.configure(text="%d / 500 字" % count,
                              text_color=(DANGER_TEXT if count > 500 else MUTED))

        def submit():
            if not self._require_regular_user_mode("\u63d0\u4ea4\u666e\u901a\u7528\u6237\u53cd\u9988"):
                return
            if (form["pending"]
                    or getattr(self, "_feedback_submission_pending", False)):
                return
            content = body.get("1.0", "end-1c").strip()
            count = len(content)
            if not 20 <= count <= 500:
                status.configure(text="请将内容控制在 20–500 字。",
                                 text_color=DANGER_TEXT)
                return
            selected = category.get()
            form["pending"] = True
            self._feedback_submission_pending = True
            submit_button.configure(state="disabled", text="正在提交…")
            status.configure(text="正在安全地发送反馈…", text_color=MUTED)
            self._run_cloud_action(
                "feedback_submit",
                lambda: slg_account.submit_feedback(
                    category_labels[selected], content))

        def update_after_edit(_event=None):
            if win.winfo_exists():
                win.after_idle(update_counter)

        body.bind("<KeyRelease>", update_after_edit, add="+")
        body.bind("<<Paste>>", update_after_edit, add="+")
        body.bind("<<Cut>>", update_after_edit, add="+")
        submit_button.configure(command=submit)
        body.focus_set()

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
        ctk.CTkLabel(win, text="", image=load_avatar(84, own=False)).pack(
            pady=(18, 6))
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
        if not self._require_personal_access("编辑标签译名"):
            return
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
        if not self._require_personal_access("编辑标签译名"):
            return
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
        names = sorted(slg_db.all_tags(self.conn), key=display_tag)
        vars_.update({name: ctk.BooleanVar(value=name in current) for name in names})
        scheduler = MotionScheduler(flow)
        index = [0]
        def batch():
            budget = TimeBudget(milliseconds=6, max_items=20)
            while index[0] < len(names) and budget.available:
                name = names[index[0]]
                widget = self._tag_checkbox(flow, display_tag(name), vars_[name])
                widgets.append(widget)
                flow._items.append(widget)
                index[0] += 1
                budget.consumed()
            flow._layout()
            if index[0] < len(names):
                scheduler.call_later(1, batch)
        batch()
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
        if not self._require_personal_access("编辑游戏标签"):
            return
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
            if not self._require_personal_access("编辑游戏标签"):
                return
            chosen = [n for n, v in vars_.items() if v.get()]
            with slg_db.session() as conn:
                slg_db.set_tags(conn, game["id"], chosen, clear=True)
            win.destroy()
            self._reload_tags(None, None)

        ctk.CTkButton(win, text="保存", height=34, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT,
                      hover_color=CARD_HOVER, command=save
                      ).pack(fill="x", padx=18, pady=(8, 16))

    def open_add_game(self, prefill_title="", prefill_folder="", game=None):
        """Add a game the user owns, or edit one already added (game is set)."""
        if not self._require_personal_access("添加或编辑自定义游戏"):
            return
        editing = game is not None
        win = self._new_dialog("编辑我的游戏" if editing else "添加我的游戏",
                               "460x760")
        ctk.CTkLabel(win, text=("改完点保存即可" if editing else "把自己本地的游戏加进库"),
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
        title_e.insert(0, game["title"] if editing else prefill_title)
        dev_e = field("开发商")
        engine_e = field("引擎")
        ver_e = field("版本")
        if editing:
            if game.get("developer"):
                dev_e.insert(0, game["developer"])
            if game.get("engine"):
                engine_e.insert(0, game["engine"])
            if game.get("version"):
                ver_e.insert(0, game["version"])

        ctk.CTkLabel(body, text="游戏分类（可多选，与引擎和内容标签分开）",
                     text_color=MUTED, font=ui_font(size=11), anchor="w").pack(
                         fill="x", pady=(8, 2))
        category_vars = self._category_checklist(
            body, slg_db.game_categories(self.conn, game["id"]) if editing else ())

        ctk.CTkLabel(body, text="简介", text_color=MUTED,
                     font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
        ov_e = ctk.CTkTextbox(body, height=70, corner_radius=8, fg_color=BG,
                              text_color=TEXT, border_color=CHIP, border_width=1,
                              font=ui_font(size=12), wrap="word")
        ov_e.pack(fill="x")
        if editing and game.get("overview"):
            ov_e.insert("1.0", game["overview"])

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

        folder = {"path": (game.get("folder_path") if editing
                           else prefill_folder) or None}
        ctk.CTkLabel(body, text="本地目录（可选，选好后自动读取标题/开发商/版本/引擎）",
                     text_color=MUTED,
                     font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
        folder_row = ctk.CTkFrame(body, fg_color="transparent")
        folder_row.pack(fill="x")
        folder_label = ctk.CTkLabel(folder_row,
                                    text=(os.path.basename(folder["path"])
                                          if folder["path"] else "未选择"),
                                    text_color=MUTED, font=ui_font(size=11), anchor="w")
        folder_label.pack(side="left", fill="x", expand=True)

        def pick_folder():
            path = filedialog.askdirectory(title="选择游戏所在文件夹", parent=win)
            if not path:
                return
            folder["path"] = path
            folder_label.configure(text=os.path.basename(path))
            import slg_scan
            info = slg_scan.autofill_folder(path)
            if info["title"] and not title_e.get().strip():
                title_e.delete(0, "end")
                title_e.insert(0, info["title"])
            if info["developer"] and not dev_e.get().strip():
                dev_e.delete(0, "end")
                dev_e.insert(0, info["developer"])
            if info["engine"] and not engine_e.get().strip():
                engine_e.delete(0, "end")
                engine_e.insert(0, info["engine"])
            if info["version"] and not ver_e.get().strip():
                ver_e.delete(0, "end")
                ver_e.insert(0, info["version"])

        ctk.CTkButton(folder_row, text="选择…", width=72, height=26,
                      corner_radius=6, fg_color=CHIP, text_color=TEXT,
                      hover_color=CARD_HOVER, font=ui_font(size=11),
                      command=pick_folder).pack(side="right")

        ctk.CTkLabel(body, text="标签", text_color=MUTED,
                     font=ui_font(size=11), anchor="w").pack(fill="x", pady=(8, 2))
        current_tags = (set(slg_db.game_tags(self.conn, game["id"]))
                        if editing else set())
        flow, vars_ = self._tag_checklist(body, current_tags)
        self._tag_new_entry(body, flow, vars_)

        promoted = ctk.BooleanVar(value=bool(game.get("promoted")) if editing else False)
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
                if editing:
                    slg_db.update_user_game(
                        conn, game["id"], title,
                        developer=dev_e.get().strip() or None,
                        engine=engine_e.get().strip() or None,
                        version=ver_e.get().strip() or None,
                        overview=ov_e.get("1.0", "end-1c").strip() or None,
                        cover_file=cover_file, tags=chosen,
                        folder_path=folder["path"] or None,
                        promoted=want_promote,
                        categories=[cid for cid, v in category_vars.items() if v.get()])
                else:
                    slg_db.add_user_game(
                        conn, title,
                        developer=dev_e.get().strip() or None,
                        engine=engine_e.get().strip() or None,
                        version=ver_e.get().strip() or None,
                        overview=ov_e.get("1.0", "end-1c").strip() or None,
                        cover_file=cover_file, tags=chosen,
                        folder_path=folder["path"] or None,
                        promoted=want_promote,
                        categories=[cid for cid, v in category_vars.items() if v.get()])
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
        if not self._require_personal_access("绑定本地游戏目录"):
            return
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
            translation_ids = (slg_db.title_translation_game_ids(self.conn, text)
                               if text else None)
            rows = slg_db.find_games(self.conn, search=text or None, origin=None,
                                     sort="title", desc=False,
                                     translation_ids=translation_ids)
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

    # 帮助文档的章节表：(key, 左栏标题, 构造函数名)。挂成类属性而不是写在 open_help
    # 里面，是为了让「有哪些章节、什么顺序」能在不开窗口的情况下被断言。
    HELP_SECTIONS = (
        ("start",      "三步上手",       "_help_start"),
        ("what",       "这个软件是什么", "_help_what"),
        ("browse",     "界面与筛选",     "_help_browse"),
        ("collection", "收藏夹",         "_help_collection"),
        ("profile",    "个人中心与积分", "_help_profile"),
        ("signin",     "每日签到",       "_help_signin"),
        ("titles",     "头衔",           "_help_titles"),
        ("shop",       "商城与每日抽奖", "_help_shop"),
        ("addgame",    "添加我的游戏",   "_help_add_game"),
        ("scan",       "扫描本地目录",   "_help_scan"),
        ("translate",  "翻译与标签",     "_help_translate"),
        ("hanhua",     "汉化工具",       "_help_hanhua"),
        ("sync",       "同步与数据",     "_help_sync"),
        ("backup",     "备份与恢复",     "_help_backup"),
        ("update",     "更新推送",       "_help_update"),
        ("faq",        "常见问题",       "_help_faq"),
        ("about",      "声明与关于",     "_help_about"),
    )

    def open_help(self):
        """帮助文档：左栏目录，右栏当前章节。

        一列到底的长滚动在 v0.22 之后不够用了 —— 收藏夹、个人中心、签到、头衔、
        商城抽奖、添加我的游戏都没有位置，而想找的那一节只能靠翻。左栏是目录，
        右栏一次只画一节：多点一次的成本，换掉了一直往下滚的成本。
        """
        win = self._new_dialog("帮助文档", "880x660")
        win.after(120, win.lift)
        ctk.CTkLabel(win, text="帮助文档", text_color=TEXT,
                     font=ui_font(size=17, weight="bold")).pack(
            pady=(16, 8), padx=22, anchor="w")

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        nav = ctk.CTkScrollableFrame(body, fg_color=CARD, corner_radius=10,
                                     width=158)
        nav.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        self._help_content = ctk.CTkScrollableFrame(body, fg_color=CARD,
                                                    corner_radius=10)
        self._help_content.grid(row=0, column=1, sticky="nsew")
        self._help_nav = {}
        for key, title, _builder in self.HELP_SECTIONS:
            btn = ctk.CTkButton(
                nav, text=title, anchor="w", height=32, corner_radius=8,
                fg_color="transparent", text_color=TEXT,
                hover_color=CARD_HOVER, font=ui_font(size=13),
                command=lambda k=key: self._show_help_section(k))
            btn.pack(fill="x", padx=6, pady=2)
            self._help_nav[key] = btn
        self._show_help_section(self.HELP_SECTIONS[0][0])

    def _show_help_section(self, key):
        frame = self._help_content
        for child in frame.winfo_children():
            child.destroy()
        self._help_frame = frame
        for name, btn in self._help_nav.items():
            active = name == key
            btn.configure(fg_color=ACCENT if active else "transparent",
                          text_color=ON_ACCENT if active else TEXT)
        title = next(t for k, t, _b in self.HELP_SECTIONS if k == key)
        ctk.CTkLabel(frame, text=title, text_color=TEXT,
                     font=ui_font(size=17, weight="bold")).pack(
            anchor="w", padx=10, pady=(12, 2))
        builder = next(b for k, _t, b in self.HELP_SECTIONS if k == key)
        getattr(self, builder)()

    # --- 章节排版小件：章节构造函数用它们写内容，对应旧版的 head/sub/body/qa ----
    def _help_head(self, text):
        ctk.CTkLabel(self._help_frame, text=text, text_color=ACCENT,
                     font=ui_font(size=14, weight="bold")).pack(
            anchor="w", padx=10, pady=(18, 6))

    def _help_sub(self, text):
        ctk.CTkLabel(self._help_frame, text=text, text_color=MUTED,
                     font=ui_font(size=13, weight="bold")).pack(
            anchor="w", padx=10, pady=(12, 4))

    def _help_body(self, text, color=None):
        ctk.CTkLabel(self._help_frame, text=text, text_color=color or TEXT,
                     font=ui_font(size=13), wraplength=520,
                     justify="left").pack(anchor="w", padx=10, pady=(0, 6))

    def _help_qa(self, question, answer):
        """A question and its answer rendered as one visual unit.

        The question carries the emphasis: it is what someone scrolls the page
        looking for, so it gets the bold accent treatment while the answer stays
        plain. Loose gap above the question and a tight one under the answer is
        what keeps one pair from running into the next.
        """
        ctk.CTkLabel(self._help_frame, text=question, text_color=ACCENT,
                     font=ui_font(size=13, weight="bold"), wraplength=520,
                     justify="left").pack(anchor="w", padx=10, pady=(10, 2))
        ctk.CTkLabel(self._help_frame, text=answer, text_color=TEXT,
                     font=ui_font(size=13), wraplength=520,
                     justify="left").pack(anchor="w", padx=10, pady=(0, 8))

    def _help_link(self, label, url):
        _link_button(self._help_frame, label, url).pack(fill="x", padx=10,
                                                        pady=(0, 6))

    def _help_start(self):
        self._help_body("第 1 步　点左下角的「更新游戏数据」，从服务器拉取游戏目录。")
        self._help_body("第 2 步　点开任意一款游戏，在右侧用星星给它打分。")
        self._help_body("第 3 步　用标签筛选，再把顶栏排序切成「按xp推荐」，挑下一款要玩的。")
        self._help_body("评分会参与「按xp推荐」排序；评过的游戏越多，推荐越能反映你的偏好。",
                        color=MUTED)
        self._help_head("个人与积分")
        self._help_body("工具栏的「个人」「排行榜」「积分商城」分别用于管理账号、查看云端积分与装扮排行、兑换商品。每日签到入口在积分商城的余额附近。"
                        "左侧栏「更多工具…」放常用设置；左下角「更多…」放同步和维护操作。")

    def _help_what(self):
        self._help_body("一个 dikgames 站点游戏的本地资料库。游戏目录由作者的服务器从"
                        "站点整理好，同步时下载进本地数据库，再按标签、评分、下载状态"
                        "去挑你想玩的那些。浏览、搜索、筛选都不联网；封面图跟着目录"
                        "一起下载，下完一款存一款。")
        self._help_body("数据库和封面缓存在 %LOCALAPPDATA%\\slgking\\ 下，分别位于 slgking.db 和 covers 文件夹。"
                        "备份或换电脑时，可以复制整个数据目录；删除 exe 不会删掉这里的数据。")

    def _help_browse(self):
        self._help_head("左栏")
        self._help_body("「我添加的游戏」显示你手动添加的条目（见「添加我的游戏」）；"
                        "「收藏夹」是个下拉框，选中某个收藏夹后主列表只显示它里面的游戏。"
                        "「更多工具…」包含翻译、偏好等工具；「更多…」用于同步和维护。")
        self._help_body("左栏「想玩」是本地愿望单。目录更新后，如果尚未安装的想玩游戏有"
                        "新版本，按钮会显示数量；点开可查看并标记已读。提醒记录只保存在本机。",
                        color=MUTED)
        self._help_head("卡片与详情")
        self._help_body("主列表每张卡片显示封面、标题、版本、评分、前几个标签和状态点。"
                        "点开一款，右侧详情页是封面、简介、标签、评分、状态，"
                        "以及一排动作按钮。")
        self._help_body("游戏详情里的「写评论&查看评论区」会打开独立评论页。公开评论按每页 20 条读取，"
                        "可以按最新或热门排序，也可以翻页；「我的记录」保留本机私人评论和自己的云端评论。"
                        "热度行那个「官网评论」是站点数据，和这里的评论不是一回事。",
                        color=MUTED)
        self._help_head("筛选与排序")
        self._help_qa("Q：搜索、筛选、标签库有什么区别？",
                      "A：搜索栏按游戏名查找。左键点击游戏卡片或「标签库…」里的标签可加入筛选，"
                      "右键点击可排除；「更多工具…」→「偏好权重…」会根据你的五星评分计算标签偏好，正数代表更偏好。")
        self._help_body("顶栏排序有「按xp推荐」「热度」「更新时间」「评分」，"
                        "旁边的箭头切换升序降序。", color=MUTED)

    def _help_collection(self):
        self._help_body("收藏夹是你自己的分组，和站点标签无关。可以按「正在追」「纯爱」或其他习惯整理游戏。")
        self._help_body("添加游戏：打开详情页，点「收藏夹…」，勾选要加入的分组。已加入的游戏也可以从该分组移除。")
        self._help_body("创建或删除分组：左栏「更多工具…」→「管理收藏夹…」。删除收藏夹只会删除分组，不会删除游戏。")
        self._help_body("想只看某个收藏夹里的游戏，用左栏那个下拉框切过去；切回"
                        "「收藏夹」就是全部。", color=MUTED)

    def _help_profile(self):
        self._help_body("「个人」面板显示你的昵称、头像、当前头衔和云端积分，也可以在这里修改昵称、搭配个性装扮或打开「兑换码&邀请码」。")
        self._help_body("老带新：新账号只能绑定一次邀请码；双方各得 100 云端积分。每成功邀请 1、3、5 人时，邀请人分别解锁对应头衔；积分奖励不设上限。邀请码功能需服务器支持。",
                        color=MUTED)
        self._help_head("积分从哪里来")
        self._help_body("每日签到：每次 +%d 分。每月累计签到达到第 5、10、20 天时，还会分别获得 15、30、50 分；每档每月领取一次。"
                        % slg_titles.DAILY_SIGNIN_POINTS)
        self._help_body("维护补偿：按版本变化发给本机旧积分。首次记录版本或主、次版本变化时发 30 分，补丁版本变化时发 10 分；同一版本只发一次。云端不会单独发这笔补偿；本机积分迁入时按旧积分一并处理。")
        self._help_body("公开评论：有效评论通过审核后奖励 20 分。每个账号 7 天内最多有 3 条评论获得奖励，同一款游戏 30 天内最多奖励一条；待审核或未通过的评论暂不发分。")
        self._help_body("群每日码：兑换群公告里的当日码，可领取云端积分 +10，每个账号每天一次。每日抽奖也可能获得积分；抽奖每次消耗 5 分，重复抽到已拥有的头衔会折算成积分。",
                        color=MUTED)
        self._help_body("云端账号保存身份、昵称、云端积分、头衔、签到、名片装饰和公开评论，可在其他设备登录。首次创建或登录后，通过本机存档完整性校验的旧积分和头衔会自动迁入一次；迁入成功后不会再作为本机旧资产重复使用。登录密钥与恢复码请妥善保存，丢失后无法找回。",
                        color=MUTED)
        self._help_body("本机游戏库、评分、状态、备注、收藏夹和私人评论仍保存在当前电脑（%LOCALAPPDATA%\\slgking\\slgking.db）。公开评论会显示昵称、头衔、头像框、名片框和账号寄语；删除自己的云端评论后，对应内容也会下线。",
                        color=MUTED)

    def _help_signin(self):
        self._help_body("打开「积分商城」，在云端积分余额附近点「每日签到」。每天可签到一次，完成后按钮会显示「今日已签到」，次日恢复。")
        self._help_body("当月累计签到达到第 5、10、20 天时，会额外获得 15、30、50 分；达标后自动发放，每个档位每月一次。云端签到按北京时间结算。",
                        color=MUTED)

    def _help_titles(self):
        self._help_body("「个人」→「个性装扮」分为头衔、头像框和名片框。可以先预览当前搭配，也可以试搭尚未拥有的商品；头像框与名片框可同时装备。")
        self._help_body("装扮从普通、稀有、史诗、传说到至臻分级。边框颜色、细节与短动画体现不同档次，动画不会持续闪烁。")
        self._help_body("头衔可通过群码或活动码兑换、在积分商城购买、通过抽奖获得；少数头衔会随成就解锁。",
                        color=MUTED)

    def _help_shop(self):
        self._help_body("点击工具栏的「积分商城」。每日签到位于云端积分余额附近，商品按头衔、头像框、名片框和功能道具分类；点商品卡片可查看说明，兑换会扣除相应积分。")
        self._help_head("每日抽奖")
        self._help_body("商城中的「打开抽奖」只会打开抽奖窗口；拉杆后才提交抽奖并扣除 5 分。每天最多 3 次。奖品包括头衔和积分；抽到已拥有的头衔时会折算成积分。")

    def _help_add_game(self):
        self._help_body("站点目录里没有的游戏也可以手动添加：点击左栏「添加我的游戏…」打开添加窗口。"
                        "添加后的条目会显示在「我添加的游戏」视图中，与站点目录分开。")
        self._help_body("选择游戏文件夹后，程序会尝试读取标题、开发商、版本号和引擎（Ren'Py / Unity / RPG Maker / HTML）。"
                        "没读到的内容可以手动补充；简介、标签和封面需要你填写。")
        self._help_body("新添加的游戏默认只出现在左侧「我添加的游戏」；补齐信息后可以勾选"
                        "「加入主列表」，让它和站点目录里的游戏混在一起显示。")
        self._help_body("同步站点目录不会覆盖你手动添加的游戏；即使站点目录里删除了同名条目，本机的游戏记录仍会保留。",
                        color=MUTED)

    def _help_scan(self):
        self._help_body("「更多工具…」→「扫描本地目录…」：第一次点会让你选一个文件夹，"
                        "选完就记住了。想换一个，在那行上点右键重新选。")
        self._help_body("选中之后，库里同名（或近似同名）的游戏会被标成「已下载」，"
                        "并记下本地版本号，方便和站点上的最新版对比。")
        self._help_body("已安装游戏用「更多工具…」→「检查更新」查看落后版本；尚未安装的"
                        "「想玩」游戏则在站点目录版本变化后显示本地提醒。首次扫描只记录当前版本，"
                        "不会把此前已有的版本变化当作新提醒。", color=MUTED)

    def _help_translate(self):
        self._help_body("站点目录通过软件服务器同步，通常无需代理；翻译会连接 Google 或大模型服务商的接口，"
                        "部分接口需要 VPN 或代理才能访问。")
        self._help_body("请求超时会在 8 秒内显示「网络错误：连接超时」，不会一直停在等待状态。",
                        color=MUTED)
        ctk.CTkButton(
            self._help_frame, text=SITE_LABEL, height=32, corner_radius=7,
            fg_color=CHIP, text_color=ACCENT, hover_color=CARD_HOVER,
            font=ui_font(size=12), command=self.open_data_sources).pack(
                fill="x", padx=10, pady=(0, 6))
        self._help_qa("Q：翻译要怎么开？",
                      "A：点左侧栏的「更多工具…」→「翻译设置…」，有两条路。\n"
                      "· 免费机翻：什么都不用填，选上就能用，简介和游戏名都翻。"
                      "质量一般，偶尔会被 Google 限流，过几分钟再试。\n"
                      "· AI 翻译：填一个 OpenAI 兼容接口的 Key（DeepSeek、硅基流动、"
                      "Kimi、智谱、通义、OpenAI 都行），译文质量通常更好。\n"
                      "译文缓存在本地数据库，再次打开时会读取缓存。")
        self._help_qa("Q：点了翻译，等很久什么都没有？",
                      "A：先检查网络或代理设置。部分翻译接口需要代理才能访问。"
                      "连接超时会在 8 秒内显示提示；如果超过 8 秒仍没有反馈，请到 GitHub 反馈。")
        self._help_qa("Q：为什么标签只能用 AI 翻？",
                      "A：标签是全库共用的固定术语，如果机翻每次给出不同译法，列表就会前后不一致。"
                      "因此标签翻译统一使用 AI 引擎：在「更多工具…」→"
                      "「翻译设置…」里选一个服务商，填好 Key，然后点「翻译标签」就行，"
                      "费用按所选服务商的计费规则结算。")
        self._help_qa("Q：为什么有些游戏名还是英文？",
                      "A：程序会保留版本号（如 v1.20、EP03）和方括号中的社团名。如果翻译结果改动了这些信息，"
                      "程序会保留原名，并标记为「不适合翻译」，之后不再重复请求；简介仍会正常翻译。")
        self._help_head("标签译名")
        self._help_body("「更多工具…」→「标签译名…」可以给标签写中文名，改完列表和"
                        "筛选条立刻跟着变。标签本身还是站点原文，只有显示名被替换。")

    def _help_sync(self):
        self._help_body("左下角「更新游戏数据」从软件服务器获取游戏目录。服务器每天中午"
                        "（北京时间 12:23）同步一次站点数据；客户端只下载整理后的目录，无需代理。")
        self._help_body("要不要下载只看目录更新时间。没变就直接跳过；目录有更新时，客户端会下载新目录并"
                        "按游戏标识合并，不会覆盖你的评分、评论、收藏夹或译名。")
        self._help_qa("Q：同步失败了怎么办？",
                      "A：再点一次「更新游戏数据」即可，已经下载的内容不会重复下载。服务器暂时无响应时，稍后重试。")
        self._help_qa("Q：同步跑太久，能停吗？",
                      "A：能。任务跑起来之后，左下角那颗按钮会变成「停止」，点一下就停；"
                      "已经下载的部分会保存，下次可以继续。正在处理的请求会等它返回，通常不到一秒。")
        self._help_qa("Q：封面显示灰色方块？",
                      "A：本机还没有这张封面。封面和目录一起从服务器拉，"
                      "再点一次「更新游戏数据」通常就有了；还是灰的，点左下角"
                      "「更多…」→「下载封面」单独补，让它慢慢跑完（全量约 250 MB）。")
        self._help_qa("Q：左下角「更多…」里面那几个是干什么的？",
                      "A：都是不常用的维护动作，点开每个下面都有一句说明。\n"
                      "「下载封面」：补下缺的封面缩略图。\n"
                      "「补齐热度」：在本地重算热度，不联网。")

    def _help_faq(self):
        self._help_sub("界面与设置")
        self._help_qa("Q：右上角那颗齿轮是干什么的？",
                      "A：打开「设置」——浅色/深色/跟随系统在这里切，抽奖音效的开关"
                      "也在这里。「关于本软件…」里有版本信息、软件更新、GitHub 主页、"
                      "交流群和数据目录。左侧栏「更多工具…」则提供翻译、扫描等工具。")
        self._help_qa("Q：深色主题里的「跟随系统」是怎么工作的？",
                      "A：程序每 5 秒采样一次 Windows 的浅色/深色设置，变了就跟着换，"
                      "所以会有一小段延迟。手动选「浅色」或「深色」则会记住，"
                      "下次打开还是它。")
        self._help_qa("Q：窗口太小/字太大，排版挤了？",
                      "A：窗口可以随意拉伸，右侧面板会跟着变宽，商城的商品网格也会"
                      "自动多排一列。")
        self._help_sub("同步与数据")
        self._help_qa("Q：目录多久更新一次？",
                      "A：服务器每天中午自动同步一次站点，你这边点「更新游戏数据」"
                      "拿到的就是最新快照。")
        self._help_qa("Q：换电脑了，我的评分还在吗？",
                      "A：本机游戏库、评分、收藏夹和私人评论在 %LOCALAPPDATA%\\slgking\\slgking.db，"
                      "拷贝数据目录或用「备份与恢复」带走。云端身份、积分、头衔、签到和公开评论可在新设备登录后使用；"
                      "首次创建或登录时，通过完整性校验的本机旧积分和头衔会自动迁入一次。首次在新设备登录还需要恢复码。")
        self._help_sub("抽奖与积分")
        self._help_qa("Q：抽奖的概率是怎么定的？",
                      "A：奖池包含头衔和积分，具体结果以抽奖页面显示为准。抽到已拥有的头衔会折算成积分。")
        self._help_qa("Q：抽中头衔了，界面怎么没变？",
                      "A：头衔到手了但没自动装备。「个人」→「个性装扮」里找到它，"
                      "点「装备」才会显示在个人页。")
        self._help_qa("Q：本机旧积分和头衔怎么迁到云端？",
                      "A：升级到 0.23 后，首次成功创建或登录云端账号时，程序会校验本机存档，并自动迁入通过完整性校验的旧积分和头衔。迁入成功后不会重复迁移；校验异常时不会自动迁移。")
        self._help_qa("Q：积分能不能送人或换钱？",
                      "A：不能。云端积分绑定云端账号，不能转给其他账号或兑换现金。")
        self._help_sub("翻译")
        self._help_qa("Q：翻译相关的问题在哪？",
                      "A：见左栏的「翻译与标签」一节，那边的答案更全。")

    def _help_hanhua(self):
        self._help_body("dikgames 是英文站点，站上大多数游戏没有官方中文。下载到英文版本是正常的，"
                        "不代表文件损坏。需要翻译时，可以使用下面的工具：\n"
                        "1. 露娜翻译器（LunaTranslator）：开源免费，边玩边实时机翻游戏文本；\n"
                        "2. 作者的 RenPy 汉化小工具：搭配露娜翻译器，把 RenPy 游戏做成离线汉化。")
        self._help_link(LUNA_LABEL, LUNA_URL)
        self._help_link(RPYKIT_LABEL, RPYKIT_URL)
        self._help_body("RenPy 游戏想整包离线汉化，用第二个工具：它按脚本把文本抽出来、"
                        "翻好再塞回去。露娜翻译器对付的是运行时的实时翻译，两个可以并存。",
                        color=MUTED)

    def _help_backup(self):
        self._help_body("「更多工具…」→「备份与恢复…」可将个人数据导出为 JSON 文件，包括："
                        "评分、评论、状态、收藏夹、手动加的标签译名。导入时用文件里的内容"
                        "覆盖这几项。")
        self._help_body("文件名默认带导出日期，例如 slgking-备份-YYYYMMDD.json。换电脑后，在新设备导入这份文件即可恢复这些资料。",
                        color=MUTED)
        self._help_qa("Q：备份里包含游戏目录和封面吗？",
                      "A：不含。游戏目录和封面可以从服务器重新下载；备份保存的是你的个人资料。")
        self._help_qa("Q：导入会删掉我现在的东西吗？",
                      "A：会覆盖同一款游戏的评分、评论和状态，以及整个收藏夹和标签"
                      "译名。导入前建议先导出当前数据，方便需要时恢复。")
        self._help_qa("Q：最彻底的备份方式？",
                      "A：把 %LOCALAPPDATA%\\slgking 整个目录拷走。里面是 slgking.db"
                      "（本机数据）和 covers（封面缓存）。复制到新电脑后，可以完整恢复本机数据；目录大小取决于封面缓存。")
        self._help_qa("Q：备份里有没有密钥之类的东西？",
                      "A：不含账号登录密钥和恢复码，但会包含个人记录，部分手动添加的游戏记录还可能带本机路径。请私下保管，不要公开分享。")

    def _help_update(self):
        self._help_body("程序启动时会检查 GitHub 是否有新版本。发现更新后，左侧栏底部会显示版本提示，点击可查看更新说明。")
        self._help_body("也可以手动检查：右上角齿轮 →「关于本软件…」→「检查更新」。"
                        "手动检查会显示检查结果；自动检查只在发现新版时提示。", color=MUTED)
        self._help_qa("Q：它会不会自己装新版本？",
                      "A：不会自动安装。需要你从 GitHub 发布页下载新版 exe 并替换旧版；数据保存在独立的数据目录中。")
        self._help_qa("Q：检查更新失败要管吗？",
                      "A：不会影响软件使用。可能是网络暂时不可用，下次启动时还会再检查。")

    def _help_about(self):
        self._help_body("本软件只是一个游戏资料检索库，里面没有游戏文件，也不提供下载。\n"
                        "检索到的信息和游戏的版权都归原站点与作者所有。",
                        color=DANGER_TEXT)
        self._help_body("作者 · %s" % AUTHOR, color=MUTED)
        # The button rather than a bare link: someone who opens 帮助文档 looking
        # for the source should not have to spot an 11px underlined label.
        self._help_link(GITHUB_LABEL, GITHUB_URL)
        self._help_body("版本 " + build_stamp(), color=MUTED)
        self._help_body("本软件完全免费。没有收费版、没有付费激活、没有隐藏收费入口。\n"
                        "如果你是通过付费渠道拿到它的，请立即举报。", color=DANGER_TEXT)
        self._help_body("有 bug 或功能建议，请加入交流群反馈。",
                        color=MUTED)
        _copy_button(self._help_frame, "复制交流群号：%s" % QQ_GROUP, QQ_GROUP,
                     self).pack(fill="x", padx=10, pady=(0, 6))

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

    # --- remote config / telemetry ---------------------------------------------

    def _apply_startup_compensation(self):
        """版本升级维护补偿：版本号变了发一次积分，静默失败不阻断启动。"""
        if (self._is_admin_mode()
                or getattr(self, "_startup_admin_login", False)):
            return
        if os.path.exists(os.path.join(slg_db.app_dir(), "cloud_session.json")):
            return
        try:
            amount = slg_titles.apply_update_compensation(self.conn, APP_VERSION)
        except Exception:  # noqa: BLE001 - compensation must never break launch
            return
        if amount:
            self.queue.put(("note", "版本更新，已发放 %d 积分维护补偿" % amount))

    def _apply_achievements(self, public_count=None):
        """成就头衔：收藏家（本地收藏数）+ 鉴赏家（公开评论数）。"""
        if (self._is_admin_mode()
                or getattr(self, "_startup_admin_login", False)):
            return
        if os.path.exists(os.path.join(slg_db.app_dir(), "cloud_session.json")):
            return
        try:
            gained = slg_titles.grant_achievements(self.conn, public_count)
        except Exception:  # noqa: BLE001 - 成就不能阻断任何操作
            return
        for tid in gained:
            t = slg_titles.title_by_id(tid) or {}
            self.queue.put(("note", "达成成就，获得%s头衔「%s」！"
                            % (t.get("rarity", ""), t.get("name", tid))))

    def _start_remote_check(self):
        """Fetch the server's config.json and fire the launch events, off-thread."""
        threading.Thread(target=self._remote_worker, daemon=True).start()

    def _remote_worker(self):
        cfg = slg_remote.fetch_config()
        # Launch + snapshot events go out on their own connection, the same rule
        # the sync/update workers follow: self.conn belongs to the tk thread.
        if not getattr(self, "_startup_admin_login", False):
            try:
                with slg_db.session() as conn:
                    slg_remote.report(conn, "launch",
                                      {"platform": sys.platform,
                                       "client_version": display_app_version(),
                                       "games": slg_db.stats(conn)["games"]})
                    slg_remote.report(conn, "snapshot", {
                        "points": slg_db.points_balance(conn),
                        "titles": len(slg_db.owned_title_ids(conn)),
                        "collection": slg_db.collection_count(conn),
                    })
            except Exception:  # noqa: BLE001 - telemetry must never raise
                pass
        self.queue.put(("remote", cfg))

    def _handle_remote_config(self, cfg):
        self._remote_config = cfg or {}
        self._remote_config_loaded = True
        flags = self._remote_config.get("flags") or {}
        self._remote_flags = flags

        if flags.get("disable_signin"):
            if self.signin_btn is not None and self.signin_btn.winfo_exists():
                self.signin_btn.configure(text="签到维护中", state="disabled")

        # Keep announcements in Settings until the user opens them. This makes
        # the red dot meaningful and avoids marking a notice read just because
        # a startup dialog was dismissed.
        self._refresh_announcement_badge()

        # Forced update beats the soft GitHub notice.
        minv = self._remote_config.get("min_version")
        if minv and slg_update.is_newer(minv, display_app_version()):
            self._show_forced_update(minv)

        # Maintenance: disable the sync button and warn once.
        if self._remote_config.get("maintenance"):
            self._show_maintenance(self._remote_config.get("maintenance_msg"))
        self._maybe_prompt_announcement_reward()

    def _show_announcement(self, ann):
        title = ann.get("title") or "公告"
        body = ann.get("body") or ""
        win = self._new_dialog("公告", "420x340")
        ctk.CTkLabel(win, text=title, text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(
            fill="x", padx=20, pady=(18, 8))
        ctk.CTkLabel(win, text=body, text_color=TEXT, font=ui_font(size=13),
                     justify="left", wraplength=360).pack(
            fill="x", padx=20, pady=(0, 12))
        ctk.CTkButton(win, text="知道了", height=34, width=120, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=13), command=win.destroy).pack(pady=(8, 0))

    def _show_forced_update(self, min_version):
        win = self._new_dialog("需要更新", "420x300")
        ctk.CTkLabel(win, text="请更新到最新版本", text_color=TEXT,
                     font=ui_font(size=16, weight="bold")).pack(
            fill="x", padx=20, pady=(18, 4))
        ctk.CTkLabel(win, text="当前版本 %s 已不再支持，需要 %s 及以上版本。"
                      % (display_app_version(), min_version), text_color=MUTED,
                     font=ui_font(size=13), justify="left", wraplength=360).pack(
            fill="x", padx=20, pady=(0, 12))
        ctk.CTkButton(win, text="前往下载", height=34, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=13),
                      command=lambda: (webbrowser.open(slg_update.RELEASES_URL),
                                       win.destroy())).pack(fill="x", padx=20, pady=(8, 0))

    def _show_maintenance(self, msg):
        text = msg or "服务器维护中，同步功能暂时不可用，请稍后再试。"
        if self.sync_btn is not None and self.sync_btn.winfo_exists():
            self.sync_btn.configure(state="disabled")
        messagebox.showinfo("维护中", text, parent=self)

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
                found = slg_update.check(conn, display_app_version(), force=force)
        except Exception as exc:  # noqa: BLE001 - a version check is never fatal
            self.queue.put(("note", "检查更新失败：%s" % str(exc)[:100]))
            return
        if found:
            self.queue.put(("update", found))
        elif force:
            # Only on a manual click. The startup check saying "已是最新" every
            # launch is noise in the one line that also carries progress.
            self.queue.put(("note", "已是最新版本 %s" % display_app_version()))

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
        ctk.CTkButton(row, text="立即更新", height=32, width=110, corner_radius=8,
                      fg_color=ACCENT, text_color=ON_ACCENT, hover_color=CARD_HOVER,
                      font=ui_font(size=12),
                      command=lambda: (win.destroy(),
                                       self._start_self_update(release))
                      ).pack(side="left", padx=(8, 0))
        if not release.get("asset_url"):
            ctk.CTkButton(row, text="前往下载", height=32, width=110,
                          corner_radius=8, fg_color=CHIP, text_color=TEXT,
                          hover_color=CARD_HOVER, font=ui_font(size=12),
                          command=lambda: (webbrowser.open(self._update_url),
                                           win.destroy())
                          ).pack(side="left", padx=(8, 0))
        row.pack(pady=(0, 16))

    def _repaint_update_notice(self):
        """Put the notice back on a sidebar that was just rebuilt."""
        if self._update_found is not None:
            self._show_update(self._update_found)

    def _open_update_page(self, event=None):
        webbrowser.open(self._update_url)

    def _start_self_update(self, release):
        """Download the matching release asset and hand off to the batch updater.

        Only the formal build self-updates: the test build has no GitHub asset,
        so `asset_url` is None there and this falls back to the release page.
        """
        if not getattr(sys, "frozen", False):
            webbrowser.open(release["url"])
            return
        url = release.get("asset_url")
        if not url:
            webbrowser.open(release["url"])
            return
        if not messagebox.askyesno(
                "更新", "下载并安装 %s？\n程序会自动重启。" % release["version"],
                parent=self):
            return
        dest = os.path.join(tempfile.gettempdir(), "slgking_update",
                            os.path.basename(sys.executable))
        self._set_progress("正在下载 %s …" % release["version"])
        threading.Thread(target=self._update_download_worker,
                         args=(release, dest), daemon=True).start()

    def _update_download_worker(self, release, dest):
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        except OSError:
            pass
        if slg_update.download_asset(release, dest):
            self.queue.put(("update_ready", dest))
        else:
            self.queue.put(("note", "下载更新失败，请前往 GitHub 手动下载"))

    def _install_update(self, dest):
        """Run the batch that swaps the running exe for the downloaded one.

        Windows locks a running exe, so the swap happens in a detached cmd that
        waits for this process to exit, then deletes the old file, moves the new
        one into its place, and restarts it. `%~1`/`%~2` keep paths with spaces
        intact.
        """
        if not os.path.isfile(dest):
            self._set_settings_status("更新文件缺失，请重新检查更新")
            return
        old = sys.executable
        bat = os.path.join(os.path.dirname(dest), "update.bat")
        script = (
            "@echo off\r\n"
            "setlocal\r\n"
            ":loop\r\n"
            "ping 127.0.0.1 -n 2 >nul\r\n"
            "del /f /q \"%~1\" >nul 2>&1\r\n"
            "if exist \"%~1\" goto loop\r\n"
            "move /y \"%~2\" \"%~1\" >nul\r\n"
            "start \"\" \"%~1\"\r\n"
            "del /f /q \"%~f0\"\r\n"
        )
        try:
            with open(bat, "w") as fh:
                fh.write(script)
            subprocess.Popen(
                ["cmd", "/c", bat, old, dest],
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.DETACHED_PROCESS),
                close_fds=True)
        except OSError:
            self._set_settings_status("无法启动更新脚本，请前往 GitHub 手动下载")
            return
        self.destroy()

    # --- background work ------------------------------------------------------

    def _log(self, message):
        self.queue.put(("log", message))

    def _fail(self, what, exc):
        detail = str(exc).strip() or "出现未知错误，请稍后重试"
        return "%s失败：%s" % (what, detail)

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
        if self._remote_config.get("maintenance"):
            messagebox.showwarning(
                "维护中",
                self._remote_config.get("maintenance_msg")
                or "服务器维护中，同步功能暂时不可用", parent=self)
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
        if not self._require_personal_access("管理本地游戏目录"):
            return ""
        chosen = filedialog.askdirectory(
            title="选择放游戏的文件夹（里面每个子文件夹是一款游戏）",
            initialdir=self.scan_root() or os.path.expanduser("~"), parent=self)
        if chosen:
            slg_db.set_pref(self.conn, PREF_SCAN_ROOT, os.path.normpath(chosen))
        return self.scan_root()

    def do_scan(self):
        if not self._require_personal_access("扫描本地游戏目录"):
            return
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

    def do_scan_saves(self):
        """Inventory the save folders on this machine.

        Deliberately not folded into 扫描本地目录: that scan reconciles the
        catalogue against folders the user chose, this one only reads what the
        games already wrote. Kept manual for the same reason - it walks the
        whole LocalLow tree, which is not something to run on every launch.
        """
        if self.busy:
            return
        self._run_job("扫描本地存档…", self._saves_worker)

    def _saves_worker(self):
        import slg_scan
        try:
            with slg_db.session() as conn:
                result = slg_scan.scan_saves(
                    conn, log=self._log,
                    on_progress=lambda title: self.queue.put(
                        ("progress", "查看 %s" % title)),
                    should_stop=self._stop.is_set)
            if result.get("cancelled"):
                self.queue.put(("saves_done", "已取消，保留上次完整台账"))
                return
            message = "扫描完成：%d 个目录，共 %s" % (
                result["count"], slg_scan.human_size(result["bytes"]))
            if result.get("errors"):
                message += "；%d 个范围无法读取，保留对应旧记录" % len(result["errors"])
            self.queue.put(("saves_done", message))
            self.queue.put(("saves_inventory", None))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("saves_done", self._fail("扫描存档", exc)))

    def open_save_inventory(self):
        """Show what the last save scan found. Read-only on purpose.

        There is no delete or restore here: a save folder is the one thing in
        this app the user cannot get back from the catalogue, so the first
        version of this window only reports and opens the folder.
        """
        window = self._new_dialog("本地存档", "780x560")
        body = ctk.CTkFrame(window, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=14)
        self._render_save_inventory(body)

    def _render_save_inventory(self, body):
        import slg_scan

        for child in body.winfo_children():
            child.destroy()
        rows = slg_db.list_local_saves(self.conn)
        if not rows:
            ctk.CTkLabel(
                body, text="还没有扫描过本地存档。\n"
                           "点左侧「更多工具…」→「扫描本地存档…」建立台账。",
                text_color=MUTED, justify="left",
                font=ui_font(size=13)).pack(pady=48)
            return

        total = sum(int(row["total_size"] or 0) for row in rows)
        ctk.CTkLabel(
            body, text="%d 个存档 / 游戏数据目录 · 共 %s"
                       % (len(rows), slg_scan.human_size(total)),
            text_color=MUTED, anchor="w",
            font=ui_font(size=12)).pack(fill="x", pady=(0, 8))

        frame = ctk.CTkScrollableFrame(body, fg_color="transparent")
        frame.pack(fill="both", expand=True)
        def make_row(row):
            card = ctk.CTkFrame(frame, fg_color=CARD, corner_radius=8)
            card.pack(fill="x", pady=3)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                card, text=row["title"] or row["path"], text_color=TEXT,
                anchor="w", font=ui_font(size=13, weight="bold")).grid(
                    row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
            ctk.CTkLabel(
                card, text=("游戏数据目录（可能含缓存和日志） · %d 个文件 · %s · 最后修改 %s"
                           if row["engine"] == "Unity" and row["source_kind"] == "system"
                           else "%d 个存档 · %s · 最后修改 %s")
                           % (int(row["save_count"] or 0),
                              slg_scan.human_size(int(row["total_size"] or 0)),
                              row["last_modified"] or "—"),
                text_color=MUTED, anchor="w",
                font=ui_font(size=11)).grid(
                    row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
            ctk.CTkButton(
                card, text="打开目录", width=88, height=28, corner_radius=6,
                fg_color=CHIP, hover_color=CARD_HOVER, text_color=TEXT,
                font=ui_font(size=12),
                command=lambda p=row["path"]: self._open_save_folder(p)).grid(
                    row=0, column=1, rowspan=2, padx=(0, 10), pady=10)

        scheduler = MotionScheduler(frame)
        index = [0]
        def batch():
            budget = TimeBudget(milliseconds=6, max_items=10)
            while index[0] < len(rows) and budget.available:
                make_row(rows[index[0]])
                index[0] += 1
                budget.consumed()
            if index[0] < len(rows):
                scheduler.call_later(1, batch)
        batch()

    def _open_save_folder(self, path):
        """Hand a save folder to the OS file manager. Read-only."""
        try:
            if os.path.isdir(path):
                os.startfile(path)
            else:
                messagebox.showinfo("目录不存在",
                                    "这个存档目录已经不在了：\n%s" % path, parent=self)
        except (OSError, AttributeError) as exc:  # noqa: BLE001
            # AttributeError: os.startfile is Windows-only and is simply absent
            # elsewhere. A missing file manager must not take the window down.
            messagebox.showwarning("打不开目录", str(exc), parent=self)

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
        budget = TimeBudget(milliseconds=6, max_items=DRAIN_PER_TICK)
        latest_progress = None
        def dispatch(message):
            try:
                self._dispatch(message[0], message[1])
            except Exception:  # a failed handler must not kill the pump
                traceback.print_exc()
        try:
            while budget.available:
                try:
                    message = self.queue.get_nowait()
                except queue.Empty:
                    break
                budget.consumed()
                if not isinstance(message, tuple) or len(message) != 2:
                    continue
                if message[0] == "progress":
                    latest_progress = message
                    continue
                if latest_progress is not None:
                    dispatch(latest_progress)
                    latest_progress = None
                dispatch(message)
            if latest_progress is not None:
                dispatch(latest_progress)
        finally:
            try:
                self.after(1 if not self.queue.empty() else 50, self._drain)
            except tk.TclError:
                pass

    def _dispatch(self, kind, payload):
        if kind == "cover_decoded":
            self._cover_decoded(*payload)
        elif kind == "log":
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
        elif kind == "saves_done":
            self._end_job(payload)
        elif kind == "saves_inventory":
            # _new_dialog replaces a same-titled window, so re-scanning while
            # the inventory is open rebuilds it with the fresh rows.
            self.open_save_inventory()
        elif kind == "covers_done":
            # Separate from "done" only for the refill: covers move a column,
            # not a row, so the list has to be rebuilt rather than re-synced.
            self._end_job(payload, refill=True)
        elif kind == "update":
            self._show_update(payload)
        elif kind == "update_ready":
            self._install_update(payload)
        elif kind == "remote":
            self._handle_remote_config(payload)
        elif kind == "note":
            self._set_settings_status(payload)
        elif kind == "overview":
            self._overview_result(*payload)
        elif kind == "title":
            self._title_result(*payload)
        elif kind == "comments":
            self._comments_result(*payload)
        elif kind == "comment_upload":
            self._comment_upload_result(*payload)
        elif kind == "comment_delete":
            self._comment_delete_result(*payload)
        elif kind == "comment_report":
            self._comment_report_result(payload)
        elif kind == "comment_vote":
            self._comment_vote_result(payload)
        elif kind == "cloud_action":
            self._cloud_action_result(*payload)
        elif kind == "achievement":
            self._apply_achievements(payload)
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
