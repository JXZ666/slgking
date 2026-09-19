"""The window.

Master-detail on purpose: a card carries only what you scan (cover, title,
version, rating, a few tags, a status dot) and every control that writes to the
database lives in the detail panel on the right. Putting stars and buttons on
every card would mean thousands of widgets for the full catalogue.

Light, card-based, Win11-ish - the stock tkinter look reads as Windows XP and
that was the one thing about the previous tools the user actively disliked.
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
import webbrowser
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

import slg_db
import slg_engines
import slg_scrape
import slg_translate

APP_VERSION = "0.12.0"
APP_TITLE = "SLG黄游之王"
AUTHOR = "菊千代赛高"
GITHUB_URL = "https://github.com/JXZ666"
GITHUB_LABEL = "GitHub 主页 · JXZ666"
# Taken from the scraper rather than typed again: this is the site the catalogue
# comes from, and two copies of that URL is one copy that goes stale.
SITE_URL = slg_scrape.BASE
SITE_LABEL = "游戏官网 · dikgames.com"
PREF_THEME = "theme"
PREF_FREE_NOTICE = "free_notice_seen"


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

COVER_W, COVER_H = 112, 69          # dikgames thumbs are 576x356, ~1.62:1
DETAIL_W, DETAIL_H = 300, 185
PAGE = 80                            # cards rendered per "load more"
REFRESH_GAP = 5.0                    # seconds between refreshes while syncing
ENRICH_PER_SYNC = 150                # detail pages a single sync will backfill

# Nearly every game carries these, so leading with them wastes the three lines
# a card gets. Push them to the back and let the distinctive tags show.
GENERIC_TAGS = {
    "3dcg", "2dcg", "3d-game", "2d-game", "animated", "big-tits", "big-ass",
    "male-protagonist", "female-protagonist", "mobile-game", "adventure",
    "visual-novel", "vaginal-sex", "oral-sex", "handjob", "teasing",
}

VIEWS = [("全部", None), ("想玩", "want"), ("已下载", "downloaded"), ("正在玩", "playing")]

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


def ui_font(size, weight="normal"):
    """The one place a font is built.

    The family is resolved on first call rather than at import: font.families()
    needs a live root, and this module is imported before there is one. The
    fallback is TkDefaultFont's own family, which is already the system UI font.
    """
    global _ui_family
    if _ui_family is None:
        try:
            have = set(tkfont.families())
        except Exception:  # noqa: BLE001 - no root yet, or no font system
            have = set()
        _ui_family = (next((f for f in _FONT_CANDIDATES if f in have), None)
                      or tkfont.nametofont("TkDefaultFont").actual()["family"])
    return ctk.CTkFont(family=_ui_family, size=size, weight=weight)


def _section(parent, text):
    """Small caption that breaks the sidebar's flat run of buttons into groups."""
    ctk.CTkLabel(parent, text=text, text_color=MUTED, anchor="w",
                 font=ui_font(size=11)).pack(fill="x", padx=18, pady=(12, 2))


def _rule(parent):
    ctk.CTkFrame(parent, height=1, fg_color=CHIP, corner_radius=0).pack(
        fill="x", padx=12, pady=(12, 0))


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
    """Version, channel and build time. Puts "am I running the build I just
    made?" in the corner of the window instead of in a guess about timestamps.

    The exe's own mtime is the honest answer for a frozen build - it changes
    exactly when a new one is written - and the source file's stands in when
    running from the tree.
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


class App(ctk.CTk):
    def __init__(self, notify=True):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(940, 600)
        # Without this the window and the taskbar entry keep tkinter's feather.
        try:
            self.iconbitmap(asset_path("slgking.ico"))
        except Exception:  # noqa: BLE001 - a missing icon is not worth a crash
            pass

        self.conn = slg_db.connect()
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
        self.include, self.exclude = [], []
        self.search = ""
        self.view = None
        self.sort = "score"
        self.selected = None
        self.rows = []
        self.shown = PAGE
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
        self._more_btn = None
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

        self._build()
        self.refresh()
        self.after(120, self._drain)
        # Armed unconditionally: switching to "跟随系统" later has to start being
        # watched too, and the poll is a no-op while another mode is selected.
        self.after(5000, self._poll_system)
        # notify is off for the packaging smoke test: the dialog is modal and
        # would sit there blocking the mainloop it is meant to be checking.
        if notify and not slg_db.get_pref(self.conn, PREF_FREE_NOTICE):
            self.after(300, self._show_free_notice)

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
        if self.busy:
            self._start_job(self._job_label)
        self.selected = None
        if selected_id is not None:
            fresh = next((g for g in self.rows if g["id"] == selected_id), None)
            self.selected = dict(fresh) if fresh is not None else None
        self.refresh()

    def _teardown_ui(self):
        for child in self.winfo_children():
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
        self._more_btn = None
        self.filterbar = self.list = self.detail = None
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
        self._tag_editor = None
        self._settings_status = self.tag_btn = None
        self.view_buttons = {}
        self.stat_label = self.progress_label = None
        self.update_btn = self.translate_btn = self.covers_btn = None
        self.sync_btn = self.rebuild_btn = self.search_entry = None

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

    def _set_progress(self, text):
        """progress_label is recreated on a theme switch, so guard the write.

        The drain loop and the worker callbacks both land here, and they can
        fire between the teardown and the rebuild.
        """
        label = self.progress_label
        if label is not None and label.winfo_exists():
            label.configure(text=text[:60])

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
        self._build_filterbar(main)

        body = ctk.CTkFrame(main, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        self.list = ctk.CTkScrollableFrame(body, fg_color="transparent")
        self.list.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.list.grid_columnconfigure(0, weight=1)

        self.detail = ctk.CTkScrollableFrame(body, fg_color=CARD, corner_radius=12)
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.detail.grid_columnconfigure(0, weight=1)

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
        ctk.CTkLabel(header, text=APP_TITLE, text_color=TEXT,
                     font=ui_font(size=19, weight="bold")).pack(pady=(22, 2),
                                                                   padx=18)
        ctk.CTkLabel(header, text="作者 · %s" % AUTHOR, text_color=MUTED,
                     font=ui_font(size=11)).pack(padx=18)
        ctk.CTkLabel(header, text=build_stamp(), text_color=MUTED,
                     font=ui_font(size=10)).pack(padx=18, pady=(1, 0))

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
        # Demoted to a manual, confirmed action: the tag walk is ~100 requests
        # and cannot see anything the sitemap pass has not already seen. It
        # stays because it is the fallback if the sitemaps ever change shape.
        self.rebuild_btn = ctk.CTkButton(
            actions, text="全量重建…", height=28, corner_radius=8,
            fg_color="transparent", text_color=MUTED, hover_color=CARD,
            font=ui_font(size=12), anchor="w", command=self.do_rebuild)
        self.rebuild_btn.pack(fill="x", padx=12, pady=(0, 2))
        self.sync_btn = ctk.CTkButton(actions, text="同步 dikgames", height=38,
                                      corner_radius=8, fg_color=ACCENT,
                                      command=self.do_sync)
        self.sync_btn.pack(fill="x", padx=12, pady=(0, 6))
        self.covers_btn = ctk.CTkButton(
            actions, text="下载封面", height=34, corner_radius=8,
            fg_color="transparent", text_color=TEXT, hover_color=CARD,
            anchor="w", command=self.do_covers)
        self.covers_btn.pack(fill="x", padx=12, pady=(0, 8))

        nav = ctk.CTkScrollableFrame(bar, fg_color="transparent")
        nav.pack(side="top", fill="both", expand=True)

        _section(nav, "浏览")
        self.view_buttons = {}
        for label, status in VIEWS:
            btn = ctk.CTkButton(
                nav, text=label, anchor="w", height=36, corner_radius=8,
                # CARD for the active view, so a theme rebuild does not come back
                # with the highlight missing.
                fg_color=CARD if status == self.view else "transparent",
                text_color=TEXT, hover_color=CARD,
                font=ui_font(size=14),
                command=lambda s=status: self.set_view(s))
            btn.pack(fill="x", padx=12, pady=2)
            self.view_buttons[status] = btn

        _rule(nav)
        _section(nav, "工具")
        for text, command in (("标签库…", self.open_tag_picker),
                              ("标签译名…", self.open_tag_editor),
                              ("偏好权重…", self.open_weights),
                              ("扫描本地目录", self.do_scan)):
            ctk.CTkButton(nav, text=text, height=34, corner_radius=8,
                          fg_color="transparent", text_color=TEXT, hover_color=CARD,
                          anchor="w", command=command).pack(fill="x", padx=12, pady=2)
        self.update_btn = ctk.CTkButton(
            nav, text="检查更新", height=34, corner_radius=8, fg_color="transparent",
            text_color=TEXT, hover_color=CARD, anchor="w", command=self.do_updates)
        self.update_btn.pack(fill="x", padx=12, pady=2)
        self.translate_btn = ctk.CTkButton(
            nav, text="翻译设置…", height=34, corner_radius=8,
            fg_color="transparent", text_color=TEXT, hover_color=CARD,
            anchor="w", command=self.open_translate_settings)
        self.translate_btn.pack(fill="x", padx=12, pady=2)
        ctk.CTkButton(nav, text="帮助文档", height=34, corner_radius=8,
                      fg_color="transparent", text_color=TEXT, hover_color=CARD,
                      anchor="w", command=self.open_help).pack(fill="x", padx=12,
                                                                pady=(2, 10))

    def _build_toolbar(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        bar.grid_columnconfigure(0, weight=1)

        # The "搜索" caption is gone and the text is centred: the caption only
        # pushed the placeholder into the left edge, and with the whole row to
        # itself the box says what it is without it.
        self.search_entry = ctk.CTkEntry(
            bar, placeholder_text="搜索游戏名…", height=36, corner_radius=8,
            fg_color=CARD, text_color=TEXT, placeholder_text_color=MUTED,
            border_width=1, border_color=CHIP, font=ui_font(size=13),
            justify="center")
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_entry.bind("<KeyRelease>", self._on_search)

        # Sort and the theme switch share the right-hand column, stacked, so the
        # search box keeps the whole row to itself and stops looking cramped.
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=1, padx=(20, 0), sticky="e")

        sort_row = ctk.CTkFrame(right, fg_color="transparent")
        sort_row.pack(fill="x")
        ctk.CTkLabel(sort_row, text="排序", text_color=MUTED,
                     font=ui_font(size=13)).pack(side="left", padx=(0, 8))
        self.sort_menu = ctk.CTkOptionMenu(
            sort_row, values=["推荐分", "站内评分", "最近更新", "名称"], width=110,
            height=34, corner_radius=8, fg_color=CARD, text_color=TEXT,
            button_color=CHIP, button_hover_color=CARD_HOVER,
            command=self._on_sort)
        self.sort_menu.pack(side="left")

        self.theme_switch = ctk.CTkSegmentedButton(
            right, values=[_THEME_LABELS[m] for m in ("light", "dark", "system")],
            height=30, corner_radius=8, fg_color=CARD, selected_color=ACCENT,
            selected_hover_color=ACCENT, unselected_color=CARD,
            unselected_hover_color=CARD_HOVER, text_color=TEXT,
            font=ui_font(size=12), command=self._on_theme_pick)
        self.theme_switch.pack(anchor="e", pady=(6, 0))
        self.theme_switch.set(_THEME_LABELS[self.theme_mode])

        # Permanent and deliberately not dismissible - the disclaimer is the
        # point. It lives inside the toolbar rather than in a row of its own on
        # `main` so that a theme switch rebuilds it along with everything else,
        # and so the filter bar and body keep their row numbers.
        self.vpn_notice = ctk.CTkFrame(bar, fg_color=CHIP, corner_radius=8)
        self.vpn_notice.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ctk.CTkButton(self.vpn_notice, text=SITE_LABEL, height=28, corner_radius=6,
                      fg_color="transparent", text_color=ACCENT,
                      hover_color=CARD_HOVER, font=ui_font(size=12),
                      command=lambda: webbrowser.open(SITE_URL)
                      ).pack(side="right", padx=(8, 6), pady=6)
        note = ctk.CTkFrame(self.vpn_notice, fg_color="transparent")
        note.pack(side="left", fill="x", expand=True, padx=(12, 0), pady=6)
        ctk.CTkLabel(note, text="建议开启梯子（VPN / 代理）后使用本软件",
                     text_color=TEXT, font=ui_font(size=12),
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(note, text="未使用梯子导致的一切问题与作者无关",
                     text_color=DANGER_TEXT, font=ui_font(size=11),
                     anchor="w").pack(fill="x")

    def _on_theme_pick(self, label):
        mode = next(m for m, text in _THEME_LABELS.items() if text == label)
        # Deferred: this runs from inside the segmented button's own press
        # handler, and the switch destroys that button.
        self.after(1, lambda: self._apply_theme(mode))

    def _build_filterbar(self, parent):
        self.filterbar = ctk.CTkFrame(parent, fg_color="transparent")
        self.filterbar.grid(row=1, column=0, sticky="ew", pady=(0, 10))

    def _render_filterbar(self):
        # The chips are a function of the filters and nothing else, so a refresh
        # for an unrelated reason (a sync tick, a status write) rebuilding them
        # was pure flicker.
        signature = (tuple(self.include), tuple(self.exclude))
        if signature == self._filter_sig:
            return
        self._filter_sig = signature
        for child in self.filterbar.winfo_children():
            child.destroy()
        ctk.CTkLabel(self.filterbar, text="筛选", text_color=MUTED,
                     font=ui_font(size=13)).pack(side="left", padx=(0, 6))
        if not self.include and not self.exclude:
            ctk.CTkLabel(self.filterbar, text="未设置 — 左侧「标签库…」可以挑",
                         text_color=MUTED, font=ui_font(size=12)).pack(side="left")

        def chip(slug, excluded):
            label = ("× " if excluded else "") + display_tag(slug)
            btn = ctk.CTkButton(
                self.filterbar, text=label, height=26, corner_radius=13,
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
            ctk.CTkButton(self.filterbar, text="清空", width=54, height=26,
                          corner_radius=13, fg_color="transparent", text_color=ACCENT,
                          hover_color=CHIP, font=ui_font(size=12),
                          command=self.clear_filters).pack(side="left", padx=(10, 0))

    # --- state changes --------------------------------------------------------

    def set_view(self, status):
        self.view = status
        self.shown = PAGE
        for key, btn in self.view_buttons.items():
            btn.configure(fg_color=CARD if key == status else "transparent")
        self.refresh()

    def _on_search(self, event):
        # Rebuilding the list on every keystroke is what made typing feel like
        # dragging. Read the box now, but only re-query once the typing pauses.
        self.search = event.widget.get().strip()
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(300, self._apply_search)

    def _apply_search(self):
        self._search_after = None
        self.shown = PAGE
        self.refresh()

    def _on_sort(self, label):
        self.sort = {"推荐分": "score", "站内评分": "rating",
                     "最近更新": "updated", "名称": "title"}[label]
        self.refresh()

    def _drop_chip(self, slug, excluded):
        (self.exclude if excluded else self.include).remove(slug)
        self.shown = PAGE
        self.refresh()

    def clear_filters(self):
        self.include, self.exclude = [], []
        self.shown = PAGE
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
        self.shown = PAGE
        self.refresh()

    # --- rendering ------------------------------------------------------------

    def refresh(self, preserve_scroll=False, skip_if_same=False):
        """Rebuild the whole list. For filter/sort/data changes only.

        Selecting a card does NOT come through here - rebuilding ~480 CTk
        widgets to recolour two of them was the multi-second stall. The two
        flags exist for the one caller that is neither: the sync's periodic
        tick, which fires every few seconds whether or not anything the user
        can see has changed.
        """
        self._render_filterbar()
        self.rows = slg_db.find_games(
            self.conn, include=self.include, exclude=self.exclude,
            search=self.search or None,
            statuses=[self.view] if self.view else None,
            downloaded_only=False, sort=self.sort)

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

        visible = [g["id"] for g in self.rows[:self.shown]]
        if skip_if_same and visible == self._rendered_ids:
            # A sync added rows past the fold, or only touched columns nothing
            # on screen reads. Destroying and redrawing 480 widgets to arrive
            # at the same picture is what made the list stutter. The detail
            # panel is left alone too - rebuilding it would throw away the
            # user's place in it (and reset the 原文/中文 switch) every tick.
            self._render_stats()
            return

        offset = self._scroll_offset() if preserve_scroll else None
        self._sync_cards(self.rows[:self.shown])
        self._render_more()
        self._set_empty_label(not self.rows)
        self._rendered_ids = visible
        if offset is not None:
            self._restore_scroll(offset)

        self._render_stats()
        self._render_detail_if_stale()

    def _render_stats(self):
        stats = slg_db.stats(self.conn)
        self.stat_label.configure(
            text="%d 款 · %d 标签\n评分 %d 款 · 已下载 %d 款"
                 % (stats["games"], stats["tags"], stats["rated"], stats["downloaded"]))
        gaps = slg_db.data_gaps(self.conn)
        self.covers_btn.configure(
            text="下载封面（%d）" % gaps["covers"] if gaps["covers"] else "封面已齐")

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
                    slot["frame"].pack(fill="x", pady=3)
            else:
                slot = self._new_card(game, tags.get(gid, ()))
                self._card_pool.append(slot)
                self._pool_gid.append(gid)
        for i in range(len(games), len(self._card_pool)):
            if self._pool_gid[i] is not None:
                self._card_pool[i]["frame"].pack_forget()
                self._pool_gid[i] = None
        self._reset_card_index()

    def _more(self):
        self.shown += PAGE
        self._sync_cards(self.rows[:self.shown])
        self._render_more()

    def _is_last_slave(self, widget):
        """pack() records its own order, and only pack_slaves() reports it -
        winfo_children() hands back creation order instead."""
        slaves = self.list.pack_slaves()
        return bool(slaves) and slaves[-1] is widget

    def _render_more(self):
        """Keep one button, moved to the end.

        pack() appends, so a re-packed widget lands after everything currently in
        the list - which is where this one belongs, behind the cards _sync_cards
        just re-shuffled. Skipped when it is already last: a needless
        forget/re-pack invalidates the whole scroll region's geometry, and this
        runs on every single refresh.
        """
        remaining = len(self.rows) - self.shown
        if self._more_btn is None:
            self._more_btn = ctk.CTkButton(
                self.list, text="", height=36, corner_radius=8, fg_color=CARD,
                text_color=ACCENT, hover_color=CARD_HOVER, command=self._more)
        if remaining <= 0:
            if self._more_btn.winfo_manager():
                self._more_btn.pack_forget()
            return
        self._more_btn.configure(text="显示更多（还有 %d 款）" % remaining)
        if not self._is_last_slave(self._more_btn):
            self._more_btn.pack_forget()
            self._more_btn.pack(fill="x", pady=8)

    def _set_empty_label(self, show):
        """One label, shown or hidden. It used to be built fresh on every empty
        render and leaked into the list on every non-empty one."""
        if self._empty_label is None:
            self._empty_label = ctk.CTkLabel(
                self.list, text="没有匹配的游戏。\n左侧点「同步 dikgames」先把站点数据拉下来。",
                text_color=MUTED, font=ui_font(size=13), justify="left")
        shown = bool(self._empty_label.winfo_manager())
        if show and not shown:
            self._empty_label.pack(pady=40)
        elif shown and not show:
            self._empty_label.pack_forget()

    def _drop_card(self, game_id):
        """Take one card off the list without rebuilding the other four hundred.

        A status write made from inside one of the filtered views always means
        the game just left that view, so the list loses a row and gains nothing.
        The card itself is not destroyed here either - the row goes, and
        _sync_cards shifts the cards behind it down by one slot, which is the
        only way to keep the index invariant intact.
        """
        self.rows = [g for g in self.rows if g["id"] != game_id]
        self._rendered_ids = [i for i in self._rendered_ids if i != game_id]
        self._sync_cards(self.rows[:self.shown])
        self._render_more()

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
        return (game["id"], game["status"], game["my_rating"], game["note"],
                game["cover_file"])

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
        card = ctk.CTkFrame(self.list, corner_radius=10)
        card.pack(fill="x", pady=3)
        card.grid_columnconfigure(1, weight=1)

        img = ctk.CTkLabel(card, text="")
        img.grid(row=0, column=0, rowspan=3, padx=10, pady=10)

        # tk.Label takes a raw pixel wraplength where CTkLabel scaled it for us.
        wrap = card._apply_widget_scaling(430)

        title = tk.Label(card, text="", bg=CARD, fg=TEXT, anchor="w",
                         font=ui_font(size=14, weight="bold"))
        title.grid(row=0, column=1, sticky="ew", pady=(12, 0))

        meta = tk.Label(card, text="", bg=CARD, fg=MUTED, anchor="w",
                        font=ui_font(size=12))
        meta.grid(row=1, column=1, sticky="ew")

        tagline = tk.Label(card, text="", bg=CARD, fg=MUTED, anchor="w",
                           wraplength=wrap, justify="left",
                           font=ui_font(size=11))
        tagline.grid(row=2, column=1, sticky="ew", pady=(0, 12))

        for widget in (card, img):
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

        add("url", ctk.CTkButton(d, text="在浏览器打开 dikgames 页面", height=30,
                                 corner_radius=8, fg_color=CHIP, text_color=TEXT,
                                 hover_color=CARD_HOVER),
            fill="x", padx=18, pady=(10, 4))

        order.extend(self._build_detail_status(d, parts))
        order.extend(self._build_detail_stars(d, parts))
        order.extend(self._build_detail_note(d, parts))
        order.extend(self._build_detail_tags(d, parts))
        order.extend(self._build_detail_overview(d, parts))

        self._detail_parts = parts
        self._detail_order = order
        self._detail_shown = None

    def _layout_detail(self, show_url, show_overview):
        """Show, hide and order the panel's blocks.

        pack() appends, so a block that comes back lands at the bottom.
        Re-packing the whole visible run in order is a dozen calls and removes
        the entire class of "the description is below the tags now" bug. It is
        skipped when the same set of blocks is already up, which is every
        game change but one.
        """
        wanted = {"url": show_url, "ov_head": show_overview, "ov_box": show_overview}
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
        if game["url"]:
            p["url"].configure(command=lambda u=game["url"]: webbrowser.open(u))
        self._sync_status_btns(game)
        self._sync_star_btns(game)
        # Only rewritten when it differs: the sync tick comes through here too,
        # and a delete/insert drops the cursor out of a note being typed.
        if p["note_entry"].get() != (game["note"] or ""):
            p["note_entry"].delete(0, "end")
            if game["note"]:
                p["note_entry"].insert(0, game["note"])
        self._fill_detail_tags(game)
        # Explicit, every time: the switch used to be reset by the rebuild, and
        # without this the panel would show 中文 over the previous game's text.
        p["ov_seg"].set("原文")
        p["ov_label"].configure(text=self._overview_to_show(game))
        self._layout_detail(bool(game["url"]), bool(game["overview"]))

    def _show_title(self, text):
        label = self._title_label
        if label is not None and label.winfo_exists():
            label.configure(text=text)

    def _set_overview_lang(self, game, value):
        label = self._ov_label
        if label is None or not label.winfo_exists():
            return
        if value == "原文":
            self._show_title(self._title_to_show(game))
            self._show_title_note("")
            label.configure(text=self._overview_to_show(game))
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
        if cached_ov is not None:
            label.configure(text=cached_ov)
        else:
            label.configure(text="翻译中…")
        # The name rides along on the same switch rather than getting a button
        # of its own: it is one short string, and half the panel turning Chinese
        # while the title stays English reads as a bug.
        if cached_title is not None and cached_ov is not None:
            return
        if cached_ov is None and game["id"] not in self._ov_inflight:
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
                    "还没填 API Key。点左边的「翻译设置…」填一下。")
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
                    "还没填 API Key。点左边的「翻译设置…」填一下。")
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
            label.configure(text="翻译失败：%s\n\n%s" % (error, game["overview"]))
            self._ov_seg.set("原文")
            return
        label.configure(text=text)

    def _build_detail_status(self, d, parts):
        row = ctk.CTkFrame(d, fg_color="transparent")
        buttons = {}
        for label, status in (("想玩", "want"), ("已下载", "downloaded"), ("正在玩", "playing")):
            btn = ctk.CTkButton(row, text=label, height=30, corner_radius=8,
                                fg_color=CHIP, text_color=TEXT,
                                hover_color=CARD_HOVER,
                                command=lambda s=status: self._set_status(s))
            btn.pack(side="left", padx=3)
            buttons[status] = btn
        self._status_btns = buttons
        parts["status"] = row
        return [("status", row, {"fill": "x", "padx": 18, "pady": (8, 0)})]

    def _sync_status_btns(self, game):
        """Colour the three status buttons for `game` without rebuilding them.

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

    def _build_detail_note(self, d, parts):
        head = ctk.CTkLabel(d, text="备注", text_color=MUTED, font=ui_font(size=12))
        entry = ctk.CTkEntry(d, height=32, corner_radius=8, fg_color=BG,
                             placeholder_text="玩到哪了、等更新…")
        entry.bind("<Return>", lambda e: self._set_note(e.widget.get()))
        parts["note_head"] = head
        parts["note_entry"] = entry
        return [("note_head", head, {"anchor": "w", "padx": 18, "pady": (12, 2)}),
                ("note_entry", entry, {"fill": "x", "padx": 18})]

    def _build_detail_tags(self, d, parts):
        head = ctk.CTkFrame(d, fg_color="transparent")
        ctk.CTkLabel(head, text="标签（左键加入筛选 / 右键排除）", text_color=MUTED,
                     font=ui_font(size=12)).pack(side="left")
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
        self._ov_label.configure(text=self._zh_overview(game)
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

    def _set_rating(self, value):
        game = self.selected
        new = 0 if game["my_rating"] == value else value
        slg_db.set_state(self.conn, game["id"], my_rating=new or "")
        slg_db.recompute_weights(self.conn)
        game["my_rating"] = new or None
        self._sync_star_btns(game)
        # Deliberately no refresh. recompute_weights() does move games around
        # under 推荐分, but reordering the list out from under the cursor is
        # worse than a sort that settles on the next redraw - and the rating
        # itself is not on the card, so there is nothing stale on screen.

    def _set_note(self, text):
        self.selected["note"] = text
        slg_db.set_state(self.conn, self.selected["id"], note=text)

    # --- dialogs --------------------------------------------------------------

    def open_tag_picker(self):
        win = ctk.CTkToplevel(self)
        win.title("标签库")
        win.geometry("420x600")
        win.transient(self)
        ctk.CTkLabel(win, text="左键加入筛选 · 右键排除",
                     text_color=MUTED, font=ui_font(size=12)).pack(pady=10)
        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for row in slg_db.tag_counts(self.conn):
            slug = row["name"]
            state = " √" if slug in self.include else (" ×" if slug in self.exclude else "")
            btn = ctk.CTkButton(
                frame, text="%s    %d%s" % (display_tag(slug), row["n"], state),
                anchor="w", height=30, corner_radius=6, fg_color="transparent",
                text_color=TEXT, hover_color=CHIP, font=ui_font(size=13),
                command=lambda s=slug: (self.toggle_tag(s), win.destroy()))
            btn.pack(fill="x", pady=1)
            btn.bind("<Button-3>",
                     lambda e, s=slug: (self.toggle_tag(s, exclude=True), win.destroy()))

    def open_weights(self):
        win = ctk.CTkToplevel(self)
        win.title("偏好权重")
        win.geometry("420x560")
        win.transient(self)
        ctk.CTkLabel(win, text="从你的五星评分里算出来的标签倾向\n正数 = 你喜欢，负数 = 你不喜欢",
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
        win = ctk.CTkToplevel(self)
        win.title("标签译名")
        win.geometry("520x640")
        win.transient(self)
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
        # preserve_scroll: the chips under the cursor change text, not order, so
        # throwing the scroll position away here would just be disorienting.
        self._invalidate_cards()
        self.refresh(preserve_scroll=True)
        # refresh()'s staleness check only looks at the game's own fields, and
        # the tag names are not among them - the chips have to be redrawn here.
        self._render_detail()

    def open_translate_settings(self):
        """Engine, then provider, then a key - in that order.

        The AI half is a grid so the whole block can be hidden in one call when
        the free engine is picked: five fields that have no meaning without a
        key would otherwise sit there greyed out and read as a broken dialog.
        """
        win = ctk.CTkToplevel(self)
        win.title("翻译设置")
        win.geometry("500x580")
        win.transient(self)

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
        win = ctk.CTkToplevel(self)
        win.title("帮助文档")
        win.geometry("560x640")
        win.transient(self)
        win.after(120, win.lift)

        ctk.CTkLabel(win, text="帮助文档", text_color=TEXT,
                     font=ui_font(size=17, weight="bold")).pack(pady=(16, 0), padx=22,
                                                              anchor="w")
        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(6, 12))

        def head(text):
            ctk.CTkLabel(frame, text=text, text_color=ACCENT,
                         font=ui_font(size=14, weight="bold")).pack(
                anchor="w", padx=8, pady=(14, 4))

        def body(text, color=None):
            ctk.CTkLabel(frame, text=text, text_color=color or TEXT,
                         font=ui_font(size=13), wraplength=480,
                         justify="left").pack(anchor="w", padx=8, pady=(0, 6))

        head("这个软件是什么")
        body("一个 dikgames 站点游戏的本地资料库。把站上的游戏抓下来存进本地数据库，"
             "再按标签、评分、下载状态去挑你想玩的那些。")
        body("数据来自站点的 sitemap，抓完就存在本地，浏览、搜索、筛选都不联网。"
             "封面图会单独下载到本地缓存。")
        body("数据库和封面不在程序旁边，在 %LOCALAPPDATA%\\slgking\\ 下面："
             "slgking.db 和 covers 文件夹。想备份或换电脑，把那个目录整个带走。"
             "exe 删了数据还在，换台机器数据留在原处。")

        head("网络与梯子")
        body("游戏数据来自 dikgames 站点，翻译用的是 Google 和各家大模型的公开接口，"
             "这些站点和接口都在墙外。在国内使用请先开梯子（VPN / 代理），"
             "否则同步、下载封面、翻译都会连接失败。")
        body("没开梯子时，翻译会在 8 秒内明确报「网络错误：连接超时」，而不是一直卡着不动。",
             color=MUTED)
        body("未使用梯子导致的一切问题（抓不到数据、翻译失败、封面空白等）与作者无关。",
             color=DANGER_TEXT)
        _link_button(frame, SITE_LABEL, SITE_URL).pack(fill="x", padx=8, pady=(0, 6))

        head("常见问题")

        body("Q：翻译要怎么开？", color=MUTED)
        body("A：点左侧「翻译设置…」，有两条路。\n"
             "· 免费机翻：什么都不用填，选上就能用，简介和游戏名都翻。质量一般，"
             "偶尔会被 Google 限流，过几分钟再试。\n"
             "· AI 翻译：填一个 OpenAI 兼容接口的 Key（DeepSeek、硅基流动、Kimi、"
             "智谱、通义、OpenAI 都行），质量明显更好。\n"
             "译文存在本地数据库里，翻一次就一直有效，不会重复花钱。")

        body("Q：点了翻译，等很久什么都没有？", color=MUTED)
        body("A：先看有没有开梯子——翻译接口都在墙外，没开梯子必然连不上。"
             "现在这种情况会在 8 秒内报「网络错误：连接超时」；如果超过 8 秒还没有任何"
             "提示，那是 bug，请到 GitHub 上反馈。")

        body("Q：为什么标签只能用 AI 翻？", color=MUTED)
        body("A：标签是全库共用的固定术语，一百多张卡片都显示同一份。机翻每次给的"
             "译法都不一样（netorare 这轮叫「寝取」下轮叫「NTR」），整个库会读起来"
             "前后矛盾。所以标签翻译需要 AI 引擎——在「翻译设置…」里选一个服务商，"
             "填好 Key，然后点「翻译标签」就行，一趟大概花 1 分钱。")

        body("Q：为什么有些游戏名还是英文？", color=MUTED)
        body("A：名字里的版本号（v1.20、EP03）和方括号里的社团名，AI 经常忍不住去改。"
             "改过的名字会被丢掉，改用原文——这类名字会记一笔「不适合翻译」，"
             "之后不会再重复请求。简介不受影响，照常翻。")

        body("Q：同步失败了怎么办？", color=MUTED)
        body("A：同步是增量抓取，中断了再点一次「同步 dikgames」就行，"
             "已经抓到的不会重复抓。站点偶尔抖动，等一会儿再试。")

        body("Q：封面显示灰色方块？", color=MUTED)
        body("A：说明这张封面还没下载。点左下角「下载封面」，让它慢慢跑完。")

        body("Q：搜索、筛选、标签库之间的区别？", color=MUTED)
        body("A：搜索栏按游戏名找；卡片上的标签或「标签库…」里的左键加入筛选、右键排除；"
             "「偏好权重…」会按你打过的五星评分算出你倾向的标签。")

        body("Q：深色主题里的「跟随系统」是怎么工作的？", color=MUTED)
        body("A：程序每 5 秒采样一次 Windows 的浅色/深色设置，变了就跟着换，"
             "所以会有一小段延迟。手动选「浅色」或「深色」则会记住，下次打开还是它。")

        head("关于")
        body("作者 · %s" % AUTHOR, color=MUTED)
        # The button rather than a bare link: someone who opens 帮助文档 looking
        # for the source should not have to spot an 11px underlined label.
        _link_button(frame, GITHUB_LABEL, GITHUB_URL).pack(
            fill="x", padx=8, pady=(0, 6))
        body("版本 " + build_stamp(), color=MUTED)
        body("本软件完全免费。没有收费版、没有付费激活、没有隐藏收费入口。\n"
             "如果你是通过付费渠道拿到它的，请立即举报。", color=DANGER_TEXT)

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
        self._start_job("翻标签…")
        threading.Thread(target=self._tag_worker,
                         args=(engine, provider, base_url, key, model),
                         daemon=True).start()

    def _tag_worker(self, engine, provider, base_url, key, model):
        conn = None
        try:
            conn = slg_db.connect()
            config = slg_engines.Config(engine, provider, base_url, key, model)
            summary = slg_translate.run_tag_translation(
                conn, config.api_key, config.model or slg_translate.DEFAULT_MODEL,
                engine=config.build(),
                log=lambda m: self.queue.put(("log", m)))
            # Both dictionaries are module level, so the refresh at the end of
            # the job picks the new translations up for every card at once.
            load_tag_translations(conn)
            load_title_translations(conn)
            text = "标签翻译完成：%d/%d 个" % (summary["translated"],
                                              summary["pending"])
            if summary["missing"]:
                text += "，%d 个没翻出来" % len(summary["missing"])
            self.queue.put(("done", text))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("done", "标签翻译失败：%s" % str(exc)[:150]))
        finally:
            if conn is not None:
                conn.close()

    # --- background work ------------------------------------------------------

    def _start_job(self, label):
        self.busy = True
        # Remembered so a theme switch can replay the disabled/relabelled state
        # on the buttons it just rebuilt.
        self._job_label = label
        self._stop.clear()
        self.sync_btn.configure(text=label, state="disabled")
        self.covers_btn.configure(state="disabled")
        self.rebuild_btn.configure(state="disabled")
        self.translate_btn.configure(state="disabled")

    def _end_job(self, message, refill=False):
        """Finish a background job. `refill` forces every card to be redrawn.

        The dirty check in _sync_cards is keyed on the game id, which cannot see
        a job that only changes a column. Cover downloads are exactly that: every
        row keeps its id and only cover_file moves, so without this the cards on
        screen would keep showing the grey placeholder until the next restart.
        """
        self.busy = False
        self.sync_btn.configure(text="同步 dikgames", state="normal")
        self.covers_btn.configure(state="normal")
        self.rebuild_btn.configure(state="normal")
        self.translate_btn.configure(state="normal")
        self._set_progress(message)
        self._refresh_tag_button()
        if refill:
            self._invalidate_cards()
        self.refresh()

    def do_sync(self):
        """Incremental. Three sitemap requests, then only what actually moved."""
        if self.busy:
            return
        self._start_job("同步中…")
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        import slg_scrape
        fetcher = slg_scrape.Fetcher(log=lambda m: self.queue.put(("log", m)))
        post = lambda m: self.queue.put(("log", m))  # noqa: E731
        try:
            conn = slg_db.connect()
            summary = slg_scrape.sync_incremental(
                conn, fetcher, log=post,
                on_progress=lambda i, n, title: self.queue.put(
                    ("progress", "抓详情 %d/%d · %s" % (i, n, title))))
            # Games that predate the rating/overview columns get topped up
            # here rather than in a separate chore. Capped and resumable, so a
            # sync stays minutes and the next one continues where this stopped.
            filled = slg_scrape.enrich(
                conn, fetcher, limit=ENRICH_PER_SYNC, log=post,
                should_stop=self._stop.is_set,
                on_progress=lambda d, total, url: self.queue.put(
                    ("progress", "补全详情 %d/%d" % (d, total))))
            conn.close()
            tail = ("，还有 %d 款下次接着来" % summary["deferred"]
                    if summary["deferred"] else "")
            self.queue.put(("done", "同步完成 · 全站 %d 款 · 新增 %d · 变动 %d · 补全 %d%s"
                            % (summary["catalogue"], summary["new"],
                               summary["changed"], filled, tail)))
        except Exception as exc:  # noqa: BLE001 - the user needs the message
            self.queue.put(("done", "同步失败：%s: %s" % (type(exc).__name__, exc)))

    def do_covers(self):
        if self.busy:
            return
        gaps = slg_db.data_gaps(self.conn)
        if not gaps["covers"]:
            self._set_progress("封面都下载好了")
            return
        self._start_job("下封面…")
        threading.Thread(target=self._covers_worker, daemon=True).start()

    def _covers_worker(self):
        import slg_scrape
        try:
            conn = slg_db.connect()
            done = slg_scrape.download_covers(
                conn, workers=3, rate=3.0, log=lambda m: self.queue.put(("log", m)),
                should_stop=self._stop.is_set,
                on_progress=lambda d, total: self.queue.put(
                    ("progress", "封面 %d/%d" % (d, total))))
            conn.close()
            self.queue.put(("covers_done", "封面下载 %d 张" % done))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("covers_done", "封面下载失败：%s: %s"
                            % (type(exc).__name__, exc)))

    def do_rebuild(self):
        """The original tag walk, kept only as a fallback.

        It reaches the same games and the same fields as the sitemap pass, at
        roughly a hundred requests and ten minutes instead of three and three
        seconds, so it asks first.
        """
        if self.busy:
            return
        if not messagebox.askyesno(
                "全量重建",
                "全量重建会按标签把 dikgames 重爬一遍：约 100 个请求、10 分钟。\n\n"
                "它拿到的数据和「同步 dikgames」完全一样，只是慢得多。\n"
                "只有增量同步明显出问题时才需要跑。确定继续吗？"):
            return
        self._start_job("重建中…")
        threading.Thread(target=self._rebuild_worker, daemon=True).start()

    def _rebuild_worker(self):
        import slg_scrape
        fetcher = slg_scrape.Fetcher(log=lambda m: self.queue.put(("log", m)))
        try:
            conn = slg_db.connect()
            tags = slg_db.get_pref(conn, "watched_tags")
            tags = tags.split(",") if tags else ["netorare", "corruption", "cheating"]
            summary = slg_scrape.sync_tags(
                conn, fetcher, tags,
                on_progress=lambda t, p, n: self.queue.put(
                    ("progress", "%s 第 %d 页 · %d 款" % (t, p, n))))
            conn.close()
            self.queue.put(("done", "全量重建完成：%d 款（新增 %d）"
                            % (summary["games"], summary["new"])))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("done", "全量重建失败：%s: %s" % (type(exc).__name__, exc)))

    def do_scan(self):
        if self.busy:
            return
        self.busy = True
        self.sync_btn.configure(state="disabled")
        self._set_progress("扫描本地目录…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        import slg_scan
        try:
            conn = slg_db.connect()
            result = slg_scan.scan(conn, log=lambda m: self.queue.put(("log", m)))
            conn.close()
            self.queue.put(("done", "扫描完成：匹配 %d 个，未匹配 %d 个"
                            % (result["matched"], len(result["unmatched"]))))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("done", "扫描失败：%s: %s" % (type(exc).__name__, exc)))

    def do_updates(self):
        import slg_scan
        report = slg_scan.check_updates(self.conn)
        behind = report["behind"]

        win = ctk.CTkToplevel(self)
        win.title("检查更新")
        win.geometry("660x540")
        win.transient(self)
        ctk.CTkLabel(win, text="%d 款有新版 · 已最新 %d 款 · 无法比较 %d 款"
                     % (len(behind), len(report["same"]), len(report["unknown"])),
                     text_color=MUTED, font=ui_font(size=13)).pack(pady=10)

        frame = ctk.CTkScrollableFrame(win, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        if not behind:
            ctk.CTkLabel(frame, text="没有发现更新。\n"
                                     "（没数据的话先在左侧点「扫描本地目录」建立台账）",
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

    def _maybe_refresh(self, gap=REFRESH_GAP):
        """Redraw at most once per `gap` seconds.

        The sync used to leave the list frozen for its whole run because only
        ("done", ...) triggered a refresh. But a refresh rebuilds every visible
        card, so firing one per downloaded cover would trade a frozen list for
        a stuttering one. Hence both guards: the gap caps how often it can run,
        and skip_if_same drops the runs that would redraw an identical list.
        """
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
        try:
            while True:
                try:
                    message = self.queue.get_nowait()
                except queue.Empty:
                    break
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
                self.after(150, self._drain)
            except tk.TclError:
                pass  # the window is on its way out

    def _dispatch(self, kind, payload):
        if kind == "log":
            self._set_progress(payload)
        elif kind == "progress":
            self._set_progress(payload)
            self._maybe_refresh()
        elif kind == "done":
            self._end_job(payload)
        elif kind == "covers_done":
            # Separate from "done" only for the refill: covers move a column,
            # not a row, so the list has to be rebuilt rather than re-synced.
            self._end_job(payload, refill=True)
        elif kind == "note":
            self._set_settings_status(payload)
        elif kind == "overview":
            self._overview_result(*payload)
        elif kind == "title":
            self._title_result(*payload)


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
