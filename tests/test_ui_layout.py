"""The list/detail rendering fixes.

Two things got slow or broken enough to be worth pinning down: the tag wall in
the detail panel, and the per-card tag query that ran once for every card on
screen. Run with:

    python -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

# SidebarFit below builds a real App, and App.__init__ opens the database at
# slg_gui.py:408. Point LOCALAPPDATA at a throwaway directory first, the same
# way tools/smoke_detail.py does, or the suite migrates the user's own library.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="slgtest-")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_gui  # noqa: E402
import slg_translate  # noqa: E402


class FlowRows(unittest.TestCase):
    def test_wraps_at_the_available_width(self):
        # 100 + 4 + 100 = 204 fits in 250; a third chip would need 308.
        self.assertEqual(slg_gui.flow_rows([100, 100, 100], 250), [0, 0, 1])

    def test_wide_chip_gets_a_row_to_itself(self):
        self.assertEqual(slg_gui.flow_rows([400, 50], 250), [0, 1])

    def test_everything_on_one_row_when_it_fits(self):
        self.assertEqual(slg_gui.flow_rows([50, 50, 50], 1000), [0, 0, 0])

    def test_empty(self):
        self.assertEqual(slg_gui.flow_rows([], 250), [])

    def test_fifty_tags_never_exceed_the_panel(self):
        # The real case: 50 tags at ~90px in a 340px panel used to run off the
        # right edge with no way to scroll to them.
        widths = [90] * 50
        rows = slg_gui.flow_rows(widths, 304, gap=4)
        self.assertEqual(rows[0], 0)
        self.assertGreater(rows[-1], 10)
        # Rows only ever step up by one, so the placement loop cannot skip a row.
        self.assertEqual(rows, sorted(rows))


class GameTagsBulk(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.ids = []
        for slug, tags in (("a", ["x", "y"]), ("b", ["y"]), ("c", [])):
            gid, _ = slg_db.upsert_game(self.conn, slug, "u/" + slug, slug, tags=tags)
            self.ids.append(gid)

    def tearDown(self):
        self.conn.close()

    def test_matches_the_single_game_query(self):
        got = slg_db.game_tags_bulk(self.conn, self.ids)
        for gid in self.ids:
            self.assertEqual(got[gid], slg_db.game_tags(self.conn, gid))

    def test_tagless_game_is_still_a_key(self):
        # _render_card indexes the result blindly, so a missing key would be a
        # KeyError rather than a blank tag line.
        got = slg_db.game_tags_bulk(self.conn, self.ids)
        self.assertEqual(sorted(got), sorted(self.ids))
        self.assertEqual(got[self.ids[2]], [])

    def test_empty_input(self):
        self.assertEqual(slg_db.game_tags_bulk(self.conn, []), {})

    def test_batches_past_the_sqlite_variable_cap(self):
        # Old SQLite caps a statement at 999 bound variables; a whole catalogue
        # is bigger, so game_tags_bulk chunks.
        ids = []
        for i in range(1200):
            gid, _ = slg_db.upsert_game(self.conn, "bulk-%d" % i, "u/%d" % i,
                                        "g%d" % i, tags=["filler"])
            ids.append(gid)
        got = slg_db.game_tags_bulk(self.conn, ids)
        self.assertEqual(len(got), 1200)
        self.assertTrue(all(v == ["filler"] for v in got.values()))


class CardMetaLine(unittest.TestCase):
    """The status line under a card's title.

    It is written twice now - once when the card is built, once when a status
    or rating button rewrites it in place - so the two had better agree on what
    it says. That is the whole reason it is a function instead of an inline
    expression in _render_card.
    """

    def _game(self, **overrides):
        game = {"rating": None, "complete": 1, "last_updated": "2026-03-01",
                "folder_path": None, "status": ""}
        game.update(overrides)
        return game

    def test_complete_without_a_rating(self):
        self.assertEqual(slg_gui.meta_text(self._game()),
                         "完结 · 2026-03-01")

    def test_rating_leads(self):
        self.assertTrue(slg_gui.meta_text(self._game(rating=4.5)).startswith("★ 4.5"))

    def test_ongoing_when_not_complete(self):
        self.assertEqual(slg_gui.meta_text(self._game(complete=0))[:3], "连载中")

    def test_status_uses_its_label_not_its_key(self):
        line = slg_gui.meta_text(self._game(status="want"))
        self.assertEqual(line, "完结 · 2026-03-01 · 想玩")

    def test_local_folder_shows_up(self):
        self.assertIn("已在本地", slg_gui.meta_text(self._game(folder_path="D:/g")))

    def test_everything_empty_still_renders(self):
        # A dash, not an empty label: an empty one collapses the row and the
        # card's three lines stop lining up between cards.
        self.assertEqual(
            slg_gui.meta_text({"rating": None, "complete": 0, "last_updated": None,
                               "folder_path": None, "status": ""}),
            "连载中")


class ThemePalette(unittest.TestCase):
    """The rebinding trick only holds if both palettes name the same colours.

    A name missing from _DARK keeps whatever the light pass left behind - which
    shows up as black text on a black card, not as an exception.
    """

    def tearDown(self):
        slg_gui.apply_palette("light")

    def test_both_palettes_define_the_same_names(self):
        self.assertEqual(sorted(slg_gui._LIGHT), sorted(slg_gui._DARK))

    def test_apply_palette_rebinds_the_module_names(self):
        slg_gui.apply_palette("light")
        light_card = slg_gui.CARD
        slg_gui.apply_palette("dark")
        self.assertEqual(slg_gui.CARD, slg_gui._DARK["CARD"])
        self.assertNotEqual(slg_gui.CARD, light_card)
        slg_gui.apply_palette("light")
        self.assertEqual(slg_gui.CARD, light_card)

    def test_every_name_is_readable_after_each_palette(self):
        for mode in ("light", "dark"):
            slg_gui.apply_palette(mode)
            for name in slg_gui._LIGHT:
                self.assertTrue(getattr(slg_gui, name), "%s unset in %s" % (name, mode))

    def test_theme_preference_defaults_to_following_the_system(self):
        conn = slg_db.connect(":memory:")
        try:
            self.assertEqual(slg_db.get_pref(conn, slg_gui.PREF_THEME, "system"), "system")
        finally:
            conn.close()

    def test_resolved_theme_maps_an_explicit_choice(self):
        self.assertEqual(slg_gui.resolved_theme("dark"), "dark")
        self.assertEqual(slg_gui.resolved_theme("light"), "light")
        self.assertIn(slg_gui.resolved_theme("system"), ("light", "dark"))


def ctk_label(parent, text):
    """A throwaway label, for the handlers that write into one.

    An unparented CTkLabel needs a root to exist, which the window class's
    setUpClass has already made; nothing here is ever packed, so it costs
    nothing.
    """
    return slg_gui.ctk.CTkLabel(parent, text=text)


class TitleErrorText(unittest.TestCase):
    """Which name-translation failures are worth showing the user."""

    def test_a_refused_name_says_nothing(self):
        # The guard rejected a mangled name, so the English original is the
        # correct answer - a warning under it would be noise.
        self.assertEqual(slg_gui.title_error_text("keep:模型把原名原样退了回来"), "")

    def test_no_error_says_nothing(self):
        self.assertEqual(slg_gui.title_error_text(None), "")
        self.assertEqual(slg_gui.title_error_text(""), "")

    def test_a_real_failure_is_named(self):
        text = slg_gui.title_error_text("网络错误：连接超时（8s）")
        self.assertIn("名称翻译失败", text)
        self.assertIn("连接超时", text)


class SidebarFit(unittest.TestCase):
    """Window-level regressions, at the smallest window the app allows.

    Tk's pack() hands out space in call order, so whatever is packed last is
    what gets squeezed to zero first. The GitHub link sat at the end of the
    bottom group, and at the 940x600 minimum the top content plus the action
    buttons already added up to more than the window - so it rendered at zero
    height and looked like it had never been added. It is in its own footer,
    packed first, now; this is the assertion that keeps it there.

    The background-message tests live here too rather than in a file of their
    own. customtkinter keeps theme images on the interpreter it started with,
    so a second Tk root in the same process makes it look for images in an
    interpreter that is already gone - one window for the whole suite is the
    rule, and this class owns it.
    """

    @classmethod
    def setUpClass(cls):
        try:
            cls.app = slg_gui.App(notify=False)
        except Exception as exc:  # noqa: BLE001 - a headless box has no Tk
            raise unittest.SkipTest("需要图形界面：%s" % exc)
        cls.app.geometry("940x600")
        # Pinned off "跟随系统" on purpose. The app polls the OS appearance every
        # five seconds and rebuilds the whole window when it changes, which can
        # land in the middle of a test and hand it a screenful of dead widgets -
        # a flake that only shows up on a slow machine, or when the desktop
        # theme changes while the suite is running.
        with mock.patch.object(slg_db, "set_pref"):
            cls.app._apply_theme("light")
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()

    def _find(self, needle):
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    text = child.cget("text")
                except Exception:  # noqa: BLE001 - most widgets have no text
                    text = None
                if isinstance(text, str) and needle in text:
                    found.append(child)
                walk(child)

        walk(self.app)
        return found

    def _assert_has_height(self, needle, what):
        hits = self._find(needle)
        self.assertTrue(hits, "%s没有出现在窗口里" % what)
        deadline = time.time() + 2
        for hit in hits:
            # Mapping is the window manager's job and lands a moment after the
            # geometry pass that asked for it, so this waits rather than
            # asserting on whatever the first look happens to see.
            while time.time() < deadline and hit.winfo_height() <= 1:
                self.app.update()
                time.sleep(0.01)
            self.assertTrue(hit.winfo_ismapped(), "%s没被映射" % what)
            self.assertGreater(hit.winfo_height(), 1, "%s被压成了 0 高度" % what)

    def test_the_github_link_is_visible_at_the_minimum_size(self):
        self._assert_has_height("GitHub", "GitHub 链接")

    def test_the_free_disclaimer_survives_too(self):
        self._assert_has_height("完全免费", "免费声明")

    def test_the_version_stamp_is_visible(self):
        # The point of the stamp is telling a stale exe apart from a fresh one,
        # so it is useless if the layout can hide it.
        self._assert_has_height("v" + slg_gui.APP_VERSION, "版本戳")

    def test_the_vpn_notice_is_visible_at_the_minimum_size(self):
        self._assert_has_height("建议开启梯子", "梯子提示")

    def test_the_not_using_a_vpn_disclaimer_is_visible(self):
        self._assert_has_height("与作者无关", "免责声明")

    def test_the_game_site_link_is_visible(self):
        self._assert_has_height(slg_gui.SITE_LABEL, "游戏官网按钮")

    def test_the_vpn_notice_points_at_the_scrapers_own_base_url(self):
        # Two copies of the URL is one copy that goes stale.
        self.assertEqual(slg_gui.SITE_URL, "https://dikgames.com")

    # --- theme ---------------------------------------------------------------

    def test_switching_theme_repaints_the_root_window_too(self):
        # Every fg_color="transparent" resolves by walking up the parent chain
        # to the nearest opaque ancestor, and that chain ends at the root. A
        # root left at its launch colour kept the whole toolbar row - the search
        # box included - painted in the old palette while the sidebar and cards
        # switched correctly, which is exactly what got reported.
        original = self.app.theme_mode
        try:
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme("dark")
                self.assertEqual(self.app.cget("fg_color"), slg_gui._DARK["BG"])
                self.app._apply_theme("light")
                self.assertEqual(self.app.cget("fg_color"), slg_gui._LIGHT["BG"])
        finally:
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme(original)
            # The rebuild only takes effect on the next geometry pass, and the
            # layout assertions that run after this one need real heights.
            self.app.update()

    # --- the background message pump -----------------------------------------

    def test_a_handler_that_raises_does_not_kill_the_pump(self):
        # The reported bug. One exception out of a handler used to escape
        # _drain entirely, so the after() that re-arms it never ran again and
        # every later background update was dropped for the rest of the
        # session. A failure then looked exactly like a click that did nothing.
        self.app.queue.put(("overview", (1, "translated", None)))
        self.app.queue.put(("note", "sentinel"))
        with mock.patch.object(self.app, "_overview_result",
                               side_effect=tk.TclError("widget is gone")) as dead, \
                mock.patch.object(self.app, "_set_settings_status") as sink, \
                mock.patch.object(self.app, "after") as after, \
                mock.patch.object(slg_gui.traceback, "print_exc") as reported:
            self.app._drain()
        dead.assert_called_once()
        # The message behind the dead handler still landed, in the same pass.
        sink.assert_called_once_with("sentinel")
        after.assert_called_once_with(150, self.app._drain)
        # Swallowed is not the same as handled: it is still reported.
        reported.assert_called_once()

    # --- covers arriving after the cards are already up ----------------------

    def test_a_finished_cover_job_asks_for_a_refill(self):
        # Downloading covers changes a column, never a game id, so the ordinary
        # refresh path skips every card and the new thumbnails stay invisible
        # until a restart. The job has to be routed through the refill path.
        with mock.patch.object(self.app, "_end_job") as end:
            self.app._dispatch("covers_done", "封面下载 3 张")
        end.assert_called_once_with("封面下载 3 张", refill=True)

    def test_an_ordinary_job_does_not_refill_every_card(self):
        # The refill is not free - it reconfigures every visible card - so it
        # must stay off the jobs that do not need it.
        with mock.patch.object(self.app, "_invalidate_cards") as invalidate, \
                mock.patch.object(self.app, "refresh"):
            self.app._end_job("同步完成")
            invalidate.assert_not_called()

    def test_the_refill_lands_before_the_refresh(self):
        # Order matters: refresh() reads the flag, so invalidating afterwards
        # would redraw the list with the flag set and only take effect a full
        # refresh later.
        with mock.patch.object(self.app, "_invalidate_cards"), \
                mock.patch.object(self.app, "refresh") as refresh:
            self.app._end_job("封面下载 3 张", refill=True)
            refresh.assert_called_once()
        self.assertFalse(self.app.busy)

    def test_the_detail_panel_notices_a_cover_that_arrived(self):
        # Same bug, one panel over: the signature the panel skips redraws on
        # has to carry cover_file or the big cover stays a grey block too.
        was = self.app.selected
        try:
            self.app.selected = {"id": 1, "status": "wanted", "my_rating": None,
                                 "note": "", "cover_file": "pending:http://x/a.jpg"}
            before = self.app._detail_signature()
            self.app.selected["cover_file"] = "a.jpg"
            self.assertNotEqual(before, self.app._detail_signature())
        finally:
            self.app.selected = was

    def test_a_failure_after_the_dialog_closed_lands_in_the_sidebar(self):
        self.app._settings_status = None
        self.app._set_settings_status("连接失败：网络错误：连接超时（8s）")
        self.assertTrue(self.app.progress_label.cget("text").startswith("连接失败"))

    def test_a_failed_overview_reaches_the_panel_and_clears_the_flag(self):
        game = {"id": -1, "overview": "Original text."}
        label = ctk_label(self.app, "翻译中…")
        seg = mock.Mock()
        seg.get.return_value = "中文"
        seg.winfo_exists.return_value = True
        self.app._ov_inflight.add(-1)
        try:
            with mock.patch.object(self.app, "_ov_label", label), \
                    mock.patch.object(self.app, "_ov_seg", seg), \
                    mock.patch.object(slg_db, "get_pref", return_value=None), \
                    mock.patch.object(slg_translate, "translate_overview",
                                      side_effect=slg_translate.TranslateError(
                                          "网络错误：连接超时（8s）")):
                self.app.selected = dict(game)
                self.app._overview_worker(game)
                self.app._drain()
            self.assertIn("翻译失败", label.cget("text"))
            self.assertIn("Original text.", label.cget("text"))
            self.assertNotIn(-1, self.app._ov_inflight)
        finally:
            self.app.selected = None
            self.app._ov_inflight.discard(-1)

    def test_a_failed_name_translation_says_why_under_the_title(self):
        note = ctk_label(self.app, "")
        with mock.patch.object(self.app, "_title_note", note):
            self.app._title_result(-1, None, None, "网络错误：连接超时（8s）")
        self.assertIn("名称翻译失败", note.cget("text"))

    def test_a_refused_name_leaves_the_note_empty(self):
        note = ctk_label(self.app, "")
        with mock.patch.object(self.app, "_title_note", note):
            self.app._title_result(-1, None, None, "keep:模型把原名原样退了回来")
        self.assertEqual(note.cget("text"), "")

    def test_a_failed_name_does_not_leave_the_game_in_flight(self):
        # Otherwise the switch could never ask for that name again.
        note = ctk_label(self.app, "")
        self.app._title_inflight.add(-1)
        try:
            with mock.patch.object(self.app, "_title_note", note):
                self.app._title_result(-1, None, None, "网络错误：连接超时（8s）")
            self.assertNotIn(-1, self.app._title_inflight)
        finally:
            self.app._title_inflight.discard(-1)

    def test_one_switch_starts_both_workers(self):
        # The name used to run first on the overview's thread, so a blocked name
        # request meant the overview never started - and with it went the only
        # message that could have told the user anything had failed.
        game = {"id": -1, "title": "Some Game v1.0", "overview": "A description."}
        label = ctk_label(self.app, "")
        seg = mock.Mock()
        seg.get.return_value = "中文"
        seg.winfo_exists.return_value = True
        try:
            with mock.patch.object(self.app, "_ov_label", label), \
                    mock.patch.object(self.app, "_ov_seg", seg), \
                    mock.patch.object(self.app, "_overview_worker") as overview, \
                    mock.patch.object(self.app, "_title_worker") as name:
                self.app._set_overview_lang(dict(game), "中文")
                deadline = time.time() + 2
                while time.time() < deadline and not (overview.called and name.called):
                    time.sleep(0.01)
            self.assertTrue(overview.called, "简介请求没有发起")
            self.assertTrue(name.called, "名称请求没有发起")
        finally:
            self.app._ov_inflight.discard(-1)
            self.app._title_inflight.discard(-1)

    def test_a_cached_overview_asks_for_nothing(self):
        # The name is cached too, so the switch is a pure cache hit and must
        # not start a request for either half.
        game = {"id": -1, "title": "Some Game", "overview": "A description."}
        label = ctk_label(self.app, "")
        seg = mock.Mock()
        seg.get.return_value = "中文"
        seg.winfo_exists.return_value = True
        with mock.patch.object(self.app, "_ov_label", label), \
                mock.patch.object(self.app, "_ov_seg", seg), \
                mock.patch.object(slg_db, "get_translation", return_value="已翻译"), \
                mock.patch.object(slg_db, "get_translation_row",
                                  return_value=("某游戏", "model")), \
                mock.patch.object(self.app, "_overview_worker") as overview, \
                mock.patch.object(self.app, "_title_worker") as name:
            self.app._set_overview_lang(dict(game), "中文")
            time.sleep(0.15)
        self.assertFalse(overview.called)
        self.assertFalse(name.called)
        self.assertEqual(label.cget("text"), "已翻译")

    # --- the card pool -------------------------------------------------------
    #
    # Cards are built once and then pointed at different games, which is what
    # took a filter change from seconds to milliseconds. Two things about that
    # fail *silently* rather than raising, so they are pinned here: a click
    # binding that captured its game, and the pool-index invariant that pack()
    # order depends on. Both produce a list that looks right and opens the
    # wrong game.

    def _fake_game(self, i):
        return {"id": 9000 + i, "slug": "pool-%d" % i, "title": "Pool Game %d" % i,
                "version": None, "rating": None, "complete": 1,
                "last_updated": "2026-01-01", "folder_path": None, "status": "",
                "cover_file": None, "note": "", "my_rating": 0}

    def _pool_reset(self):
        """Start from an empty list.

        The app builds a full page of cards at startup, and every assertion
        below counts slots, so the count has to start somewhere known.
        """
        for child in self.app.list.winfo_children():
            child.destroy()
        self.app._card_pool = []
        self.app._pool_gid = []
        self.app._widget_gid = {}
        self.app._cards = {}
        self.app._card_meta = {}
        self.app._card_title = {}
        self.app._card_slot = {}
        self.app._more_btn = None
        self.app._empty_label = None
        self.app._rendered_ids = []
        self.app.selected = None

    def _render(self, games):
        """refresh() against a made-up catalogue, showing all of it.

        find_games is stubbed rather than seeded into the real database: the
        suite shares one window with the user's own data, and a test that
        writes a hundred rows into it is not one you can run twice.
        """
        with mock.patch.object(slg_db, "find_games", return_value=list(games)), \
                mock.patch.object(slg_db, "game_tags_bulk",
                                  return_value={g["id"]: ["netorare"] for g in games}), \
                mock.patch.object(self.app, "_render_filterbar"), \
                mock.patch.object(self.app, "_render_stats"), \
                mock.patch.object(self.app, "_render_detail_if_stale"):
            self.app.shown = len(games)
            self.app.refresh()
        # Tk only delivers a synthesised click to a widget it has already
        # mapped, and a freshly built card is not mapped until the geometry
        # pass that follows the render.
        self.app.update()

    def _finish(self):
        # Leave the list showing the real catalogue: the pool keeps every row
        # it has ever built, and a later test measuring the sidebar would be
        # measuring these fake cards instead.
        with mock.patch.object(self.app, "_render_stats"), \
                mock.patch.object(self.app, "_render_detail_if_stale"):
            self.app.shown = slg_gui.PAGE
            self.app.refresh()

    def _click(self, card):
        """Fire a card's click binding.

        CTk forwards bind() to the canvas inside the frame, so the event has to
        be delivered there; generating it on the CTkFrame itself reaches a
        widget with nothing bound to it.
        """
        target = getattr(card, "_canvas", card)
        target.event_generate("<Button-1>", x=2, y=2)

    def test_a_click_opens_the_game_the_card_shows_now(self):
        # The bug this replaced: the binding was built as
        # `lambda e, g=game: self.select(g)`, which captured the game at build
        # time. Reused cards outlive their row, so slot 5 opened whoever used to
        # be fifth - a list that looks right and opens the wrong game.
        self._pool_reset()
        try:
            self._render([self._fake_game(i) for i in range(8)])
            second = [self._fake_game(100 + i) for i in range(8)]
            self._render(second)

            with mock.patch.object(self.app, "select") as select:
                self._click(self.app._card_pool[5]["frame"])
            self.assertTrue(select.called, "点击卡片没有打开任何游戏")
            self.assertEqual(select.call_args[0][0]["id"], second[5]["id"])
        finally:
            self._finish()

    def test_pool_index_i_always_holds_games_i(self):
        # pack() appends, so showing a card again puts it at the end. The list
        # stays in order only because the slots being re-shown are always a
        # suffix, in ascending index order - which holds exactly as long as
        # this invariant does.
        self._pool_reset()
        try:
            games = [self._fake_game(i) for i in range(6)]
            self._render(games)
            self.assertEqual(len(self.app._card_pool), 6)
            self._render(games[:3])
            # The tail is hidden, not dropped: dropping it would shift the
            # index of everything behind it and scramble the next render.
            self.assertEqual([self.app._card_pool[i]["frame"].winfo_manager()
                              for i in range(3, 6)], [""] * 3)
            self._render(games)
            for i, game in enumerate(games):
                self.assertEqual(self.app._pool_gid[i], game["id"],
                                 "第 %d 位拿的不是第 %d 款游戏" % (i, i))
            self.assertEqual([self.app._card_pool[i]["frame"].winfo_manager()
                              for i in range(6)], ["pack"] * 6)
        finally:
            self._finish()

    def test_an_unchanged_card_is_not_touched(self):
        # This is the whole win: a narrowing filter leaves the games that
        # survive at the same index, so their cards are never reconfigured.
        self._pool_reset()
        try:
            games = [self._fake_game(i) for i in range(6)]
            self._render(games)
            with mock.patch.object(self.app, "_fill_card",
                                   wraps=self.app._fill_card) as fill:
                self._render(games)
                self.assertEqual(fill.call_count, 0, "没变过的卡片被重画了")
                swapped = [games[0], self._fake_game(200)] + games[2:]
                self._render(swapped)
                self.assertEqual(fill.call_count, 1, "只换了一款游戏，却重画了多张卡")
                # Narrowing hides a suffix; a hidden slot is refilled when it
                # comes back, so the count is measured before that happens.
                self.assertEqual(fill.call_count, 1)
                self._render(swapped[:4])
                self.assertEqual(fill.call_count, 1, "筛掉尾部不该重画任何卡片")
        finally:
            self._finish()

    def test_repeated_empty_and_nonempty_renders_do_not_leak_widgets(self):
        # Both the "no matches" label and the 显示更多 button used to be built
        # fresh on every render and left behind on the ones that did not need
        # them, so a few searches grew the list's child count without bound.
        self._pool_reset()
        try:
            self._render([self._fake_game(i) for i in range(4)])
            self._render([])
            settled = len(self.app.list.winfo_children())
            for _ in range(5):
                self._render([self._fake_game(i) for i in range(4)])
                self._render([])
            self.assertEqual(len(self.app.list.winfo_children()), settled)
            self.assertTrue(self.app._empty_label.winfo_manager())
        finally:
            self._finish()

    # --- the detail panel ----------------------------------------------------

    def _select(self, game):
        """Put `game` in the panel with the fake catalogue behind it."""
        self.app.rows = [game]
        self.app.shown = 1
        self.app.select(game)

    def _panel_game(self, gid, **overrides):
        game = self._fake_game(gid)
        game.update({"overview": "An English description.", "url": "https://x/1",
                     "developer": "Studio", "status": ""})
        game.update(overrides)
        return game

    def test_the_panel_reuses_its_widgets_across_games(self):
        # The panel used to be destroyed and rebuilt on every selection, which
        # is a visible blank where the user is already looking.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            first = self.app._detail_parts
            self.app.select(self._panel_game(1))
            self.assertIs(self.app._detail_parts, first)
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             "Pool Game 1")
        finally:
            self.app.selected = None
            self._finish()

    def test_a_game_without_a_description_puts_the_block_away(self):
        # The two conditional blocks have to come and go *and* come back in the
        # same order - pack() appends, so a returned block would land last.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            self.assertIn("ov_box", self.app._detail_shown)
            self.assertIn("url", self.app._detail_shown)
            self.app.select(self._panel_game(1, overview="", url=""))
            self.assertNotIn("ov_box", self.app._detail_shown)
            self.assertNotIn("url", self.app._detail_shown)
            self.app.select(self._panel_game(0))
            order = [w[0] for w in self.app._detail_order
                     if w[0] in self.app._detail_shown]
            self.assertEqual(order, self.app._detail_shown)
            self.assertLess(order.index("url"), order.index("status"))
            self.assertLess(order.index("status"), order.index("ov_box"))
        finally:
            self.app.selected = None
            self._finish()

    def test_the_editor_opens_on_the_chinese_not_the_original(self):
        # The box used to open on the English name, which is how editing a name
        # ended up storing the original as its own translation.
        self._pool_reset()
        try:
            game = self._panel_game(0)
            self._select(game)
            slg_db.set_auto_translation(self.app.conn, "title", game["id"],
                                        game["title"], "自动译名", engine="m")
            self.app._begin_title_edit()
            entry = self.app._detail_parts["title_entry"]
            self.assertEqual(entry.get(), "自动译名")
            entry.delete(0, "end")
            entry.insert(0, "手改的名字")
            self.app._save_title_edit()
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             "手改的名字")
        finally:
            slg_db.delete_translation(self.app.conn, "title", game["id"])
            slg_db.delete_manual_translation(self.app.conn, "title", game["id"])
            slg_gui.load_title_translations(self.app.conn)
            self.app.selected = None
            self._finish()

    def test_the_box_is_empty_when_nothing_has_been_translated_yet(self):
        self._pool_reset()
        try:
            game = self._panel_game(0)
            self._select(game)
            self.app._begin_title_edit()
            self.assertEqual(self.app._detail_parts["title_entry"].get(), "")
        finally:
            self.app.selected = None
            self._finish()

    def test_a_hand_typed_name_survives_a_translation_and_the_switch(self):
        # A hand-typed name is the user's correction to the Chinese, so an
        # automatic run may not take it - and 原文 stays the site's own text,
        # because a correction to the translation is not the original.
        self._pool_reset()
        try:
            game = self._panel_game(0)
            self._select(game)
            self.app._begin_title_edit()
            entry = self.app._detail_parts["title_entry"]
            entry.delete(0, "end")
            entry.insert(0, "手改的名字")
            self.app._save_title_edit()
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             "手改的名字")

            wrote = slg_db.set_auto_translation(self.app.conn, "title", game["id"],
                                                game["title"], "自动译名", engine="m")
            self.assertFalse(wrote, "自动翻译覆盖了手改的名字")

            self.app._set_overview_lang(game, "原文")
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             game["title"])
            self.app._set_overview_lang(game, "中文")
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             "手改的名字")

            # Emptying the box hands the field back
            self.app._begin_title_edit()
            entry.delete(0, "end")
            self.app._save_title_edit()
            self.assertIsNone(
                slg_db.get_manual_translation(self.app.conn, "title", game["id"]))
        finally:
            slg_gui.load_title_translations(self.app.conn)
            self.app.selected = None
            self._finish()

    def test_clearing_a_hand_typed_name_brings_the_translation_back(self):
        # End to end through the panel, because the whole point of the fix is
        # that this is undoable: an edit must not cost the game its Chinese.
        self._pool_reset()
        try:
            game = self._panel_game(0)
            self._select(game)
            slg_db.set_auto_translation(self.app.conn, "title", game["id"],
                                        game["title"], "自动译名", engine="m")
            slg_gui.load_title_translations(self.app.conn)
            self.app._begin_title_edit()
            entry = self.app._detail_parts["title_entry"]
            entry.delete(0, "end")
            entry.insert(0, "手改的名字")
            self.app._save_title_edit()
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             "手改的名字")

            self.app._begin_title_edit()
            entry.delete(0, "end")
            self.app._save_title_edit()
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             "自动译名")
        finally:
            slg_db.delete_translation(self.app.conn, "title", game["id"])
            slg_db.delete_manual_translation(self.app.conn, "title", game["id"])
            slg_gui.load_title_translations(self.app.conn)
            self.app.selected = None
            self._finish()

    def test_a_hand_typed_description_survives_the_switch(self):
        self._pool_reset()
        try:
            game = self._panel_game(0)
            self._select(game)
            self.app._begin_overview_edit()
            box = self.app._detail_parts["ov_text"]
            box.delete("1.0", "end")
            box.insert("1.0", "手写的简介")
            self.app._save_overview_edit()
            self.assertEqual(self.app._detail_parts["ov_label"].cget("text"),
                             "手写的简介")
            # 原文 is the site's text, so the correction steps aside for it.
            self.app._set_overview_lang(game, "原文")
            self.assertEqual(self.app._detail_parts["ov_label"].cget("text"),
                             game["overview"])
            self.app._set_overview_lang(game, "中文")
            self.assertEqual(self.app._detail_parts["ov_label"].cget("text"),
                             "手写的简介")
        finally:
            slg_db.delete_manual_translation(self.app.conn, "overview", game["id"])
            self.app.selected = None
            self._finish()

    def test_clearing_the_tag_editor_leaves_automatic_tags_alone(self):
        # The clear button takes the hand-typed rows and nothing else, so the
        # hundred tags nobody touched keep their translations.
        self._pool_reset()
        try:
            slg_db.set_auto_translation(self.app.conn, "tag", "story", "story",
                                        "剧情", engine="m")
            slg_db.set_manual_translation(self.app.conn, "tag", "netorare", "NTR")
            slg_gui.load_tag_translations(self.app.conn)
            self.app.open_tag_editor()
            self.app._clear_tag_edits()
            self.assertEqual(slg_gui.display_tag("netorare"), "netorare")
            self.assertEqual(slg_gui.display_tag("story"), "剧情")
            self.app._tag_editor["win"].destroy()
            self.app._tag_editor = None
        finally:
            slg_db.delete_manual_translation(self.app.conn, "tag", "netorare")
            slg_db.delete_translation(self.app.conn, "tag", "story")
            slg_gui.load_tag_translations(self.app.conn)
            self.app.selected = None
            self._finish()

    def test_a_renamed_tag_reaches_the_cards_that_are_already_up(self):
        # The pool's skip check compares game ids, and a tag rename leaves
        # every id where it was - so without an explicit invalidation the
        # cards would keep the old name.
        self._pool_reset()
        try:
            self._render([self._fake_game(i) for i in range(2)])
            slg_db.set_manual_translation(self.app.conn, "tag", "netorare", "NTR")
            slg_gui.load_tag_translations(self.app.conn)
            self.app._invalidate_cards()
            self._render([self._fake_game(i) for i in range(2)])
            self.assertIn("NTR", self.app._card_pool[0]["tagline"].cget("text"))
        finally:
            slg_db.delete_manual_translation(self.app.conn, "tag", "netorare")
            slg_gui.load_tag_translations(self.app.conn)
            self._finish()

    def test_the_card_text_follows_the_card_that_holds_it(self):
        # The three text lines are plain tk.Labels, and a tk.Label is opaque
        # where the CTkLabel it replaced was not: it does not pick up its
        # parent's background. So selecting a card has to recolour them too,
        # or the tinted frame comes with three white rectangles on it.
        self._pool_reset()
        try:
            games = [self._fake_game(i) for i in range(3)]
            self._render(games)
            slot = self.app._card_pool[0]
            text_widgets = (slot["title"], slot["meta"], slot["tagline"])
            for widget in text_widgets:
                self.assertEqual(widget.cget("bg"), slg_gui.CARD)
            self.assertEqual(slot["title"].cget("fg"), slg_gui.TEXT)
            self.assertEqual(slot["tagline"].cget("fg"), slg_gui.MUTED)

            with mock.patch.object(self.app, "_render_detail"):
                self.app.select(dict(games[0]))
            for widget in text_widgets:
                self.assertEqual(widget.cget("bg"), slg_gui.CARD_HOVER,
                                 "选中的卡片上文字没换底色")

            # Moving the selection has to put the first card back.
            with mock.patch.object(self.app, "_render_detail"):
                self.app.select(dict(games[1]))
            for widget in text_widgets:
                self.assertEqual(widget.cget("bg"), slg_gui.CARD)
        finally:
            self.app.selected = None
            self._finish()

    def test_a_theme_switch_leaves_no_card_behind(self):
        # _apply_theme destroys the scroll frame and builds a new one. A pool
        # surviving that would hand _sync_cards frames from a dead parent, and
        # the next refresh would raise TclError.
        original = self.app.theme_mode
        games = [self._fake_game(i) for i in range(4)]
        self._pool_reset()
        try:
            self._render(games)
            before = {str(slot["frame"]) for slot in self.app._card_pool}
            # _apply_theme ends in refresh(), so the catalogue has to stay
            # stubbed across it: left to read the database it would render
            # whatever is really there, and the cards this test is about would
            # be gone by the time it looks.
            with mock.patch.object(slg_db, "find_games", return_value=list(games)), \
                    mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme("dark" if original != "dark" else "light")
            after = {str(slot["frame"]) for slot in self.app._card_pool}
            self.assertTrue(after, "换主题后列表空了")
            self.assertEqual(before & after, set(), "复用到了已销毁的卡片")
            self.assertTrue(self.app._card_pool)
            for slot in self.app._card_pool:
                self.assertTrue(slot["frame"].winfo_exists())
                self.assertEqual(slot["frame"].winfo_parent(), str(self.app.list))
                # The colours are read from the module globals at build time, so
                # a rebuilt card carries the new palette. A hardcoded one would
                # survive the switch and glare through the dark theme.
                for key in ("title", "meta", "tagline"):
                    self.assertEqual(slot[key].cget("bg"), slg_gui.CARD,
                                     "换主题后卡片文字底色没跟上")
            # The singletons were blanked by the teardown and rebuilt by the
            # refresh that follows it, so what matters is that the one in hand
            # is alive rather than a frame from the old window.
            self.assertTrue(self.app._empty_label.winfo_exists())
            self.assertTrue(self.app._more_btn is None
                            or self.app._more_btn.winfo_exists())
        finally:
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme(original)
            self.app.update()
            self._finish()


if __name__ == "__main__":
    unittest.main()
