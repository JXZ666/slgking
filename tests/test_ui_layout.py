"""The list/detail rendering fixes.

Two things got slow or broken enough to be worth pinning down: the tag wall in
the detail panel, and the per-card tag query that ran once for every card on
screen. Run with:

    python -m unittest discover tests
"""

import itertools
import os
import sys
import tempfile
import time
import tkinter as tk
import traceback
import unittest
from datetime import date
from unittest import mock

# SidebarFit below builds a real App, and App.__init__ opens the database at
# slg_gui.py:408. Point LOCALAPPDATA at a throwaway directory first, the same
# way tools/smoke_detail.py does, or the suite migrates the user's own library.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="slgtest-")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_gui  # noqa: E402
import slg_remote  # noqa: E402
import slg_titles  # noqa: E402
import slg_translate  # noqa: E402

import customtkinter as ctk  # noqa: E402


class _InlineThread:
    """Runs a threading.Thread's target on the caller's stack.

    The stats panel fills itself from a background worker, so a test that races
    that worker would be the flakiest thing in the suite.
    """

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


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


class ShopColumns(unittest.TestCase):
    """商城货架的列数/卡宽。纯函数，因为这段算法才是「商品不见了」的根因。

    背景：右侧面板是窗口的 40%。默认窗口 1180 时网格只有 192 逻辑像素，而两张
    104 的卡 + 8 间距 = 216 放不下，FlowFrame 就把 4 件商品拉成 4 行，底下的
    被挤出视野 —— 看起来像商品消失了。所以宽度得按实测反推，不能再写死。
    """

    MAX = slg_gui.App.SHOP_TILE_W
    MIN = slg_gui.App.SHOP_TILE_W_MIN
    GAP = slg_gui.App.SHOP_TILE_GAP

    def _cols(self, avail):
        return slg_gui.shop_columns(avail, self.MAX, self.MIN, self.GAP)

    def test_default_window_fits_two_columns(self):
        # 192 = 默认 1180 窗口下实测的网格宽度。这条是用户报的那个 bug 的守卫。
        cols, tile_w = self._cols(192)
        self.assertEqual(cols, 2, "默认窗口还是只放得下一列")
        self.assertGreaterEqual(tile_w, self.MIN)
        self.assertLessEqual(tile_w * 2 + self.GAP, 192, "两张卡放不下")

    def test_narrow_panel_falls_back_to_one_wider_column(self):
        cols, tile_w = self._cols(128)
        self.assertEqual(cols, 1)
        self.assertEqual(tile_w, self.MAX, "单列时该用最宽的卡")

    def test_roomy_panel_uses_the_widest_tiles(self):
        cols, tile_w = self._cols(280)
        self.assertEqual((cols, tile_w), (2, self.MAX))

    def test_very_roomy_panel_adds_a_column_rather_than_stretching(self):
        cols, tile_w = self._cols(400)
        self.assertEqual(cols, 3)
        self.assertEqual(tile_w, self.MAX)

    def test_tiles_never_overflow_the_available_width(self):
        for avail in range(80, 900, 7):
            cols, tile_w = self._cols(avail)
            self.assertGreaterEqual(cols, 1)
            self.assertGreaterEqual(tile_w, self.MIN)
            self.assertLessEqual(tile_w, self.MAX)
            if cols > 1:
                self.assertLessEqual(tile_w * cols + self.GAP * (cols - 1), avail,
                                     "%dpx 里塞了 %d 列 %dpx" % (avail, cols, tile_w))


class LotterySpinSchedule(unittest.TestCase):
    """The reel's deceleration curve, which is the whole animation.

    Pure maths on purpose: the curve is what makes the spin read as a real reel
    slowing down, and it is the part that would be painful to eyeball in a
    window that is on screen for two seconds.
    """

    def test_steps_sum_to_exactly_one_revolution(self):
        # Land a hair short and the strip stops between two symbols, which
        # looks like a rendering bug rather than a result.
        for slots in (12, 18, 24):
            with self.subTest(slots=slots):
                spun = sum(step for _d, step
                           in slg_gui.lottery_spin_schedule(slots=slots))
                self.assertAlmostEqual(spun, slots, places=6)

    def test_it_starts_fast_and_ends_slow(self):
        steps = [s for _d, s in slg_gui.lottery_spin_schedule()]
        self.assertGreater(steps[0], steps[-3] * 3,
                           "第一步和最后一步该差一个数量级")

    def test_the_delays_grow_monotonically(self):
        delays = [d for d, _s in slg_gui.lottery_spin_schedule()]
        body = delays[:-2]  # the last two are the rebound
        self.assertEqual(body, sorted(body), "间隔必须一直变长，否则会一顿一顿的")
        self.assertLess(body[0], 40)
        self.assertGreater(body[-1], 100)
        self.assertLess(max(delays), 400, "最后一跳等太久就不像有个轮子在转了")

    def test_the_rebound_returns_to_start(self):
        sched = slg_gui.lottery_spin_schedule(bounce=0.06)
        self.assertLess(sched[-2][1], 0, "倒数第二步应该是往回弹一点")
        self.assertGreater(sched[-1][1], 0)
        self.assertAlmostEqual(sched[-2][1] + sched[-1][1], 0,
                               msg="过冲和回弹不对称，停下时就不在原位了")

    def test_a_degenerate_tick_count_still_works(self):
        # ticks=1 collapses the span to zero, which is a division by zero in
        # the delay ramp unless it is clamped.
        sched = slg_gui.lottery_spin_schedule(ticks=1)
        self.assertAlmostEqual(sum(s for _d, s in sched), 18, places=6)


class ReelTapeCoverage(unittest.TestCase):
    """「三个空框」的回归测试。

    卷带只铺一圈（slots 格）时，位移到一圈末尾就把整条带子推出画布，剩下三个
    空框。这条测试锁住 reel_geometry 给出的格数：任意相位下都要有符号压在可视区
    上，包括 travel 落在 11.999999999999998（浮点误差差一点点到 12）这种边界。
    """

    CELL, ROWS, PAD, SLOTS = 52, 3, 4, 14

    def _covered(self, travel, slots=None):
        """落在画布可视区上的贴纸下标。"""
        slots = slots or self.SLOTS
        height, tiles = slg_gui.reel_geometry(self.CELL, self.ROWS, self.PAD,
                                              slots)
        hits = []
        for k in range(tiles):
            y = self.PAD + k * self.CELL + self.CELL / 2 - travel * self.CELL
            if y + self.CELL / 2 > 0 and y - self.CELL / 2 < height:
                hits.append(k)
        return hits

    def test_the_window_stays_covered_at_every_phase(self):
        for slots in (12, 14, 18):
            with self.subTest(slots=slots):
                for i in range(2001):
                    travel = slots * i / 2001.0
                    self.assertTrue(
                        self._covered(travel, slots),
                        "travel=%.12f 时画布上一个符号都没有（就是那个空框 bug）"
                        % travel)

    def test_the_boundary_phases_that_caused_the_bug(self):
        for travel in (0.0, self.SLOTS - 1e-15, self.SLOTS - 1e-9,
                       self.SLOTS - 1e-12):
            with self.subTest(travel=travel):
                self.assertTrue(self._covered(travel))

    def test_a_bare_loop_of_slots_tiles_would_leave_the_window_empty(self):
        """留下反例：只用 slots 格就是老写法，它在末尾会露空。

        没有这条，把 reel_geometry 的容差改回去也不会有人发现。
        """
        height, tiles = slg_gui.reel_geometry(self.CELL, self.ROWS, self.PAD,
                                              self.SLOTS)
        self.assertGreater(tiles, self.SLOTS)
        self.assertEqual(tiles - self.SLOTS, -(-height // self.CELL))


class ReelLanding(unittest.TestCase):
    """停稳之后，预定符号必须正好压在判定线上。

    判定线是中间那一行，中心 = pad + 1*cell + cell/2 = 82 = 画布中心。
    """

    CELL, PAD, SLOTS = 52, 4, 14

    def test_index_one_is_centred_on_the_payline(self):
        height, _tiles = slg_gui.reel_geometry(self.CELL, 3, self.PAD,
                                               self.SLOTS)
        self.assertEqual(height, 164)
        self.assertAlmostEqual(self.PAD + self.CELL + self.CELL / 2,
                               height / 2)

    def test_the_planted_symbol_is_what_the_payline_shows_at_rest(self):
        """停稳时压在判定线上的是带子上的 index 1 = 预定的那一格。

        卷带按 `cycle[k % slots]` 铺、且 tick 数多铺了几格，所以「travel 差一点点
        到一整圈」时压在判定线上的是 `cycle[(1 + slots) % slots]`，还是 cycle[1]
        —— 这正是只补容差不补格数会漏掉的那一半。
        """
        slots = self.SLOTS
        _height, tiles = slg_gui.reel_geometry(self.CELL, 3, self.PAD, slots)
        cycle = list(range(slots))  # 拿下标当「符号」，方便比对
        tape = [cycle[k % slots] for k in range(tiles)]
        for k in range(tiles):
            y = self.PAD + k * self.CELL + self.CELL / 2
            if abs(y - 82) < self.CELL / 2:
                self.assertEqual(tape[k], cycle[1])


class ReelWrap(unittest.TestCase):
    """换行的浮点容差：减速曲线会把 travel 停在 11.999999999999998。"""

    def test_a_hair_short_of_a_revolution_still_wraps(self):
        slots = 14
        travel = slots - 1e-15
        # 旧写法 `while travel >= slots` 在这里为 False，卷带不换行 -> 空框。
        self.assertFalse(travel >= slots)
        while travel >= slots - 1e-9:
            travel -= slots
        self.assertLess(travel, 0.0)  # 折回后是个绝对值极小的负数（-1e-15）


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


class FindGamesSort(unittest.TestCase):
    """Sort direction, which has to live in the SQL rather than a reversed().

    The list is paged, so reversing after the fetch would show the wrong 80.
    """

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        for slug, rating, updated in (("a", 4.0, "2026-01-01"),
                                      ("b", 9.0, "2026-03-01"),
                                      ("c", None, "2026-02-01")):
            slg_db.upsert_game(self.conn, slug, "u/" + slug, slug,
                               last_updated=updated)
        for slug, rating in (("a", 4.0), ("b", 9.0)):
            gid = self.conn.execute("SELECT id FROM games WHERE slug = ?",
                                    (slug,)).fetchone()["id"]
            slg_db.upsert_detail(self.conn, gid, rating=rating)

    def tearDown(self):
        self.conn.close()

    def _titles(self, **kw):
        return [r["title"] for r in slg_db.find_games(self.conn, **kw)]

    def test_rating_desc_puts_the_best_first(self):
        self.assertEqual(self._titles(sort="rating", desc=True)[0], "b")

    def test_rating_asc_puts_the_worst_first(self):
        self.assertEqual(self._titles(sort="rating", desc=False)[0], "a")

    def test_unrated_rows_sink_in_both_directions(self):
        # 985 rows in the real db have no rating. Plain ASC would open the
        # list with every one of them, which reads as a broken sort.
        for desc in (True, False):
            with self.subTest(desc=desc):
                self.assertEqual(self._titles(sort="rating", desc=desc)[-1], "c")

    def test_updated_direction(self):
        self.assertEqual(self._titles(sort="updated", desc=True)[0], "b")
        self.assertEqual(self._titles(sort="updated", desc=False)[0], "a")

    def test_title_direction(self):
        self.assertEqual(self._titles(sort="title", desc=False), ["a", "b", "c"])
        self.assertEqual(self._titles(sort="title", desc=True), ["c", "b", "a"])

    def test_an_unknown_field_still_sorts(self):
        self.assertEqual(len(slg_db.find_games(self.conn, sort="nonsense")), 3)


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
        # Selecting a game starts a translation worker, and with no engine
        # configured those workers reached the real translator over the
        # network: two title calls and two overview calls on every run, each
        # landing in the shared database a second or two later. That is what
        # made test_the_box_is_empty_when_nothing_has_been_translated_yet fail
        # about one run in six - the fake game's Chinese name arrived somewhere
        # mid-suite. Nothing here needs a real translation, so they only get as
        # far as an error.
        try:
            cls.app = slg_gui.App(notify=False)
        except Exception as exc:  # noqa: BLE001 - a headless box has no Tk
            raise unittest.SkipTest("需要图形界面：%s" % exc)
        cls._offline = [
            mock.patch.object(slg_translate, "translate_title",
                              side_effect=slg_translate.TranslateError("测试不联网")),
            mock.patch.object(slg_translate, "translate_overview",
                              side_effect=slg_translate.TranslateError("测试不联网")),
        ]
        for patch in cls._offline:
            patch.start()
        cls.app.report_callback_exception = cls._ignore_dead_widget_focus
        cls.app.geometry("1020x600")
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
        for patch in cls._offline:
            patch.stop()

    @staticmethod
    def _ignore_dead_widget_focus(exc, value, tb):
        """Drop the one Tk error customtkinter hands us on the way out.

        CTkToplevel schedules a focus restore a few milliseconds after it is
        shown. A dialog that is focused and then closed inside that window -
        which is what a test does, immediately - lands the callback on a widget
        path Tcl has already deleted, and Tk prints a full traceback for it in
        the middle of an otherwise green run. Every other exception still comes
        through untouched.
        """
        if isinstance(value, tk.TclError) and "bad window path name" in str(value):
            return
        traceback.print_exception(exc, value, tb)

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

    def test_the_vpn_disclaimer_lives_in_the_detail_panel(self):
        # Moved down here with the "本站只做检索" line when the notice strip was
        # compressed to a single row. It only exists on screen once a game is
        # selected, so this picks one first - the panel is empty until then.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            self._assert_has_height("与作者无关", "免责声明")
            self.assertIn("未使用梯子", self.app._detail_parts["disclaimer"].cget("text"))
        finally:
            self.app.selected = None
            self._finish()

    def test_the_notice_strip_no_longer_carries_the_disclaimer(self):
        # The strip is one row now; a second line there would push it back to
        # the two-row height this change existed to remove.
        for hit in self._find("与作者无关"):
            self.assertFalse(self._inside(hit, self.app.vpn_notice),
                             "免责声明还在工具栏的说明条里")

    def _inside(self, widget, ancestor):
        """True when `widget` is `ancestor` or sits somewhere below it.

        customtkinter wraps every CTkLabel in a real tkinter.Label, so a text
        match turns up twice per widget and the outer one is what the layout
        tests care about.
        """
        while widget is not None:
            if widget is ancestor:
                return True
            widget = widget.master
        return False

    def test_the_game_site_link_is_visible(self):
        self._assert_has_height(slg_gui.SITE_LABEL, "数据来源按钮")

    def _content_bounds(self, frame):
        """Left and right pixel edges of the labelled widgets inside `frame`."""
        edges = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    text = child.cget("text")
                except Exception:  # noqa: BLE001 - not every widget has text
                    text = None
                if (isinstance(text, str) and text.strip()
                        and child.winfo_width() > 1):
                    edges.append(child.winfo_rootx())
                    edges.append(child.winfo_rootx() + child.winfo_width())
                walk(child)

        walk(frame)
        if not edges:
            return None, None
        return min(edges), max(edges)

    def test_the_two_top_rows_share_the_windows_centre_axis(self):
        # Both rows used to run edge to edge: the notice pinned its text left
        # and its link right with a stretch of empty blue between them, and the
        # filter row hugged the left edge under it. They are one centred column
        # now. The centring frame only works if the frame it sits in has a
        # weighted column - without one it silently shrinks to its contents and
        # goes back to the left edge, which is exactly what this catches.
        self.app.update()
        for frame, what in ((self.app.vpn_notice, "梯子提示条"),
                            (self.app.filterbar, "筛选条")):
            left, right = self._content_bounds(frame)
            self.assertIsNotNone(left, "%s里没有可测量的内容" % what)
            drift = ((left + right) / 2.0
                     - (frame.winfo_rootx() + frame.winfo_width() / 2.0))
            self.assertLess(abs(drift), 2,
                            "%s没居中，偏了 %.1f px" % (what, drift))

    # --- sort direction ------------------------------------------------------

    def test_the_sort_direction_button_is_visible(self):
        # The sort menu used to be the only control, and every field had one
        # hardcoded direction; there was no way back up the list.
        self._assert_has_height(slg_gui.SORT_ARROW[True], "反序按钮")
        self.assertIsNotNone(self.app.sort_dir_btn)

    def test_the_profile_buttons_fill_the_reserved_row_below_sort(self):
        # 个人/每日签到/积分商城 sit side by side in the toolbar's reserved
        # row=1 column=1, right-aligned below the sort cluster (not in `right`).
        self.assertIsNotNone(self.app.profile_btn)
        self.assertIsNotNone(self.app.signin_btn)
        self.assertIsNotNone(self.app.shop_btn)
        row_frame = self.app.profile_btn.master
        self.assertIs(self.app.signin_btn.master, row_frame)
        self.assertIs(self.app.shop_btn.master, row_frame)
        self.assertIs(row_frame.master, self.app.toolbar)
        info = row_frame.grid_info()
        self.assertEqual(info.get("row"), 1)
        self.assertEqual(info.get("column"), 1)
        self._assert_has_height("个人", "个人按钮")
        self._assert_has_height("每日签到", "签到按钮")
        self._assert_has_height("积分商城", "商城按钮")

    def _panel_texts(self):
        """Every label text in the detail panel, wherever it sits."""
        out = []

        def walk(widget):
            for child in widget.winfo_children():
                if type(child).__module__.startswith("customtkinter"):
                    try:
                        text = child.cget("text")
                    except Exception:  # noqa: BLE001 - most widgets have no text
                        text = None
                    if isinstance(text, str):
                        out.append(text)
                    walk(child)

        walk(self.app.detail)
        return out

    def _texts_in(self, widget):
        """Every label text inside one widget, wherever it sits."""
        out = []

        def walk(node):
            for child in node.winfo_children():
                try:
                    text = child.cget("text")
                except Exception:  # noqa: BLE001 - most widgets have no text
                    text = None
                if isinstance(text, str):
                    out.append(text)
                walk(child)

        walk(widget)
        return out

    def _calendar_cells(self):
        """签到日历里每个日期格子，键是日号。

        格子是 gridded 的 CTkLabel，文字就是日号 —— 面板上别处没有这种纯数字
        标签，所以按「有 grid_info」筛出来就够，不必记住它挂在哪个 frame 下。
        """
        cells = {}

        def walk(node):
            for child in node.winfo_children():
                if isinstance(child, ctk.CTkLabel):
                    try:
                        text = child.cget("text")
                        gridded = bool(child.grid_info())
                    except Exception:  # noqa: BLE001
                        text, gridded = "", False
                    if gridded and text.isdigit():
                        cells[int(text)] = child
                walk(child)

        walk(self.app.detail)
        return cells

    def _close_profile(self):
        self.app._panel_mode = None
        self.app._destroy_detail()
        self._finish()

    def _shop_tiles(self):
        """The product cards currently drawn in the shop grid."""
        grid = getattr(self.app, "_shop_grid", None)
        self.assertIsNotNone(grid, "商城没有网格容器")
        tiles = []
        for child in grid.winfo_children():
            tiles.extend(getattr(child, "_items", []))
        return tiles

    def _close_shop(self):
        self.app.selected = None
        self.app._panel_mode = None
        self.app._destroy_detail()
        self._finish()

    def _settled_shop_tiles(self):
        """The tiles once the grid has really laid them out.

        FlowFrame._layout() returns early while it is unmapped, so every card
        sits at (0, 0) and any assertion about rows or columns silently
        describes a layout that was never computed. Waiting for mapping is what
        turns that into a real measurement.
        """
        tiles = []
        deadline = time.time() + 2
        while time.time() < deadline:
            self.app.update()
            tiles = self._shop_tiles()
            if tiles and len({t.winfo_x() for t in tiles}) > 1:
                return tiles
            time.sleep(0.01)
        return tiles

    def test_the_shop_is_a_wrapping_grid_not_one_card_per_row(self):
        # 之前是「分组标题 + 每件商品一个横条」一层层往下 pack，三件商品就得
        # 滚动；商品涨到几十件就是一条不见底的列表。
        self.app.open_shop()
        try:
            tiles = self._settled_shop_tiles()
            self.assertGreaterEqual(len(tiles), 3, "商城里没有商品")
            for tile in tiles:
                # The tile, not the app: CTk scales frame geometry by the 1.5x
                # display factor, and the App itself is on window scaling.
                h = tile._reverse_widget_scaling(tile.winfo_reqheight())
                self.assertLessEqual(h, self.app.SHOP_TILE_H + 2,
                                     "商品卡 %dpx 高，固定尺寸没生效" % h)
                # The card is a fixed box with pack_propagate off, so content
                # taller than it gets squeezed in silence - which is how the
                # 兑换 button once came out 12px tall instead of 26.
                for child in tile.winfo_children():
                    self.assertLessEqual(
                        child.winfo_y() + child.winfo_height(),
                        tile.winfo_height(),
                        "商品卡里的内容被裁掉了")
            # 列数必须等于按这个宽度算出来的列数。卡片全停在 x=0（网格没 map，
            # _layout 直接 return）时这里就是 1 ≠ cols，会失败 —— 旧断言读
            # 「第一行 ≥ 2 张」，而没布局时所有卡都在 y=0、全被判成第一行，
            # 恒过，每行一张反而守不住。
            cols, tile_w = self.app._shop_cols
            xs = {t.winfo_x() for t in tiles}
            self.assertEqual(len(xs), cols,
                             "卡片没排成 %d 列，实际 x=%s" % (cols, sorted(xs)))
            # 「默认窗口放得下两列」由 test_the_default_window_fits_two_shop_columns
            # 按测得的网格宽度守着；这里不管本机窗口多宽，只管排出来的列数和
            # _shop_cols 说的一致。

            # 同一坐标系里比：winfo_x()/winfo_width() 都是缩放后的物理像素，
            # 跟 FlowFrame 自己的宽度比才对（反缩放一半会差 1.5 倍）。
            flows = [c for c in self.app._shop_grid.winfo_children()
                     if isinstance(c, slg_gui.FlowFrame)]
            flow_w = flows[0].winfo_width() if flows else 0
            for tile in tiles:
                right = tile.winfo_x() + tile.winfo_width()
                self.assertLessEqual(right, flow_w + 1, "商品卡越出了网格右边界")
        finally:
            self._close_shop()

    def test_the_shop_filters_by_category_and_subcategory(self):
        self.app.open_shop()
        try:
            self.app.update()
            chips = self._panel_texts()
            for want in ("全部", "头衔类", "物品类", "稀有", "史诗", "消耗品"):
                self.assertIn(want, chips, "筛选条上少了「%s」" % want)
            total = len(self._shop_tiles())

            self.app._pick_shop_filter("cat", "头衔类")
            self.app.update()
            titles = self._shop_tiles()
            self.assertTrue(titles, "头衔类筛完空了")
            self.assertLess(len(titles), total, "切到「头衔类」商品数没变")

            self.app._pick_shop_filter("cat", "物品类")
            self.app.update()
            self.assertTrue(self._shop_tiles(), "物品类筛完空了")

            # A subcategory only means something under the category it belongs
            # to; switching category has to drop back to 全部 rather than keep a
            # filter that shows nothing.
            self.app._pick_shop_filter("sub", "消耗品")
            self.app._pick_shop_filter("cat", "头衔类")
            self.app.update()
            self.assertEqual(self.app._shop_sub, "全部")
            self.assertTrue(self._shop_tiles(), "换了分类之后筛选残留，网格空了")
        finally:
            self._close_shop()

    def test_the_titles_rail_lists_every_rarity_even_the_empty_ones(self):
        # 用户报的 bug：全部里还看得到「史诗」，切到「头衔类」就没了。细分栏当时
        # 只列真的存在的细分，于是档次凭空消失 —— 现在五档列全，没货的那档点进去
        # 如实说没货。
        self.app.open_shop()
        try:
            self.app._pick_shop_filter("cat", "头衔类")
            self.app.update()
            texts = self._panel_texts()
            for rarity in slg_titles.RARITY_ORDER:
                self.assertIn(rarity, texts, "头衔类细分栏少了「%s」" % rarity)
            self.app._pick_shop_filter("sub", "至臻")
            self.app.update()
            self.assertFalse(self._shop_tiles(), "至臻档本来就没货")
            self.assertIn("该分类暂无商品，敬请期待", self._panel_texts())
        finally:
            self._close_shop()

    def test_owned_shop_items_sink_below_the_ones_you_still_lack(self):
        drawn = []
        real = self.app._shop_tile

        def spy(parent, item, owned, equipped, points, width=None):
            drawn.append(item["id"])
            return real(parent, item, owned, equipped, points, width=width)

        self.app.open_shop()
        try:
            with mock.patch.object(slg_db, "owned_title_ids",
                                   return_value={"senior_user"}), \
                    mock.patch.object(self.app, "_shop_tile", side_effect=spy):
                drawn.clear()
                self.app._render_shop()
            self.assertIn("senior_user", drawn)
            self.assertEqual(drawn[-1], "senior_user",
                             "已拥有的商品没沉到底部：%s" % drawn)
            self.assertEqual(drawn.count("senior_user"), 1, "商品重复了")
        finally:
            self._close_shop()

    def test_every_title_offers_a_how_to_obtain_dialog(self):
        self.app.open_titles()
        try:
            self.app.update()
            win = self._dialog("我的头衔")
            self.assertIsNotNone(win, "头衔弹窗没打开")
            how_to = [b for b in self._buttons_in(win)
                      if b.cget("text") == "获得方式"]
            self.assertEqual(len(how_to), len(slg_titles.TITLES),
                             "不是每枚头衔都有「获得方式」按钮")

            t = slg_titles.TITLES[0]
            how_to[0].invoke()
            self.app.update()
            info = self._dialog(t["name"])
            self.assertIsNotNone(info, "「获得方式」没开出简介弹窗")
            texts = self._texts_in(info)
            self.assertIn(t["desc"], texts, "弹窗里没有简介")
            self.assertIn(slg_titles.obtain_title_text(t), texts,
                          "弹窗里没有获取路径")
        finally:
            for child in list(self.app.winfo_children()):
                if isinstance(child, ctk.CTkToplevel):
                    child.destroy()
            self.app.update()

    def test_the_signin_calendar_only_offers_days_you_can_backfill(self):
        signed = {1, 2, 3}
        today = date.today()
        with mock.patch.object(slg_db, "signin_month_days",
                               return_value=set(signed)):
            self.app.open_profile()
            self.app.update()
            cells = self._calendar_cells()
        try:
            self.assertTrue(cells, "签到日历没画出来")
            missed = [d for d in range(1, today.day) if d not in signed]
            if not missed:
                self.skipTest("本月还没有漏签日，无从验证补签入口")
            for day, cell in cells.items():
                clickable = bool(cell._label.bind("<Button-1>"))
                should = day in missed
                self.assertEqual(
                    clickable, should,
                    "%d 号该%s可点" % (day, "" if should else "不"))
                if should:
                    self.assertEqual(cell.cget("cursor"), "hand2",
                                     "%d 号没给手型光标" % day)
        finally:
            self._close_profile()

    def test_the_lottery_stays_a_banner_above_the_grid(self):
        # 抽奖是每天一次的特殊项，混进商品网格里会和普通商品抢注意力，也不该
        # 被分类筛选藏起来。
        self.app.open_shop()
        try:
            self.app.update()
            texts = self._panel_texts()
            self.assertTrue(any(t in ("抽一次", "今日已抽") for t in texts),
                            "商城顶上没有抽奖入口：%s" % texts)
            self.assertIn("每日抽奖", texts)
        finally:
            self._close_shop()

    def test_dev_mode_unlocks_the_daily_lottery_limit(self):
        slg_db.set_pref(self.app.conn, "dev.unlocked", "1")
        try:
            self.app.open_shop()
            self.app.update()
            texts = self._panel_texts()
            self.assertTrue(any("不限次数" in t for t in texts),
                            "开发者模式下抽奖副标题没变：%s" % texts)
            self.assertIn("抽一次", texts, "开发者模式下抽奖入口被当日限制挡掉了")
        finally:
            self._close_shop()
            slg_db.set_pref(self.app.conn, "dev.unlocked", "")

    def test_the_lottery_result_states_the_prize_only(self):
        # 抽奖结果只报「拿到了什么」：盈亏/期望属于内部数值，不该出现在用户眼前。
        for pts in (2, 5, 20):
            text = self.app._lottery_label({"kind": "points", "value": pts})
            self.assertEqual(text, "获得 %d 积分" % pts)
        grand = self.app._lottery_label({"kind": "title", "value": "lucky_star"})
        self.assertIn("幸运星", grand)

    def test_the_lottery_says_so_when_nothing_was_won(self):
        # 0 分占三成。既然对照表公开了「图案不搭 = 谢谢参与」，落空就得如实说，
        # 而不是报一句「获得 0 积分」让用户以为那也算中了点什么。
        text = self.app._lottery_label({"kind": "points", "value": 0})
        self.assertIn("谢谢参与", text)

    def test_makeup_card_has_a_use_button(self):
        self.app.open_shop()
        try:
            self.app.update()
            self.app._pick_shop_filter("cat", "物品类")
            self.app.update()
            texts = []
            for tile in self._shop_tiles():
                stack = list(tile.winfo_children())
                while stack:
                    w = stack.pop()
                    stack.extend(w.winfo_children())
                    try:
                        t = w.cget("text")
                    except Exception:  # noqa: BLE001 - most widgets have no text
                        t = None
                    if isinstance(t, str):
                        texts.append(t)
            self.assertIn("补签卡", texts, "商城没有补签卡：%s" % texts)
            self.assertIn("补签", texts, "补签卡没有「补签」按钮：%s" % texts)
        finally:
            self._close_shop()

    def test_lottery_finals_grand_is_three_sevens(self):
        # 用户拍板：摇到 777 才是头衔大奖。
        finals = self.app._lottery_finals({"kind": "title", "value": "lucky_star"})
        self.assertEqual(finals, ("seven", "seven", "seven"))

    def test_lottery_finals_points_uses_known_symbols(self):
        finals = self.app._lottery_finals({"kind": "points", "value": 3})
        self.assertEqual(len(finals), 3)
        for name in finals:
            self.assertIn(name, slg_titles.SLOT_SYMBOLS)

    def test_every_prize_tier_lands_on_its_published_symbols(self):
        """「抽奖概率」写的那组符号 = 滚轮实际停的那组。

        这两者曾经是两个独立的映射（说明写星星、演出却能冒出一组表上没有的
        组合），所以这条断言是这次改动真正要锁住的东西。两边都走
        symbols_for_prize()，所以这里比的是「演出」而不是「表本身」。
        """
        for _weight, kind, value in slg_titles.LOTTERY_PRIZES:
            if not value:
                continue
            prize = {"kind": kind, "value": value}
            declared = slg_titles.symbols_for_prize(prize)
            self.assertIsNotNone(declared, "第 %s 档没有对照的符号组合" % (value,))
            for _ in range(12):  # 展开 "*" 通配，多跑几次覆盖到每一种取值
                for got, want in zip(self.app._lottery_finals(prize), declared):
                    if want != "*":
                        self.assertEqual(got, want)

    def test_a_miss_never_looks_like_a_winning_combination(self):
        """落空时抽到的图案必须构不成任何中奖组合，否则等于骗人。"""
        winning = set()
        for symbols in slg_titles.LOTTERY_SYMBOLS.values():
            for combo in itertools.product(slg_titles.SLOT_SYMBOLS, repeat=3):
                if all(s == "*" or s == c for s, c in zip(symbols, combo)):
                    winning.add(combo)
        self.assertTrue(winning, "中奖组合集合是空的，这条测试就没意义了")
        for _ in range(40):
            got = self.app._lottery_finals({"kind": "points", "value": 0})
            self.assertIn(got, slg_titles.LOTTERY_MISSES)
            self.assertNotIn(got, winning,
                             "落空却摆出了一个中奖图案：%s" % (got,))

    def test_the_lottery_sound_is_on_by_default(self):
        # 默认开（用户要求的效果），但读的是 pref，所以设置里能关掉；一个写死的
        # True 会让那颗开关看起来能用其实没用。
        with mock.patch.object(slg_db, "get_pref", return_value="") as gp:
            self.app._lottery_sound_on()
        self.assertEqual(gp.call_args.args[1], "sound.lottery")
        self.assertEqual(gp.call_args.args[2], "1", "默认值不是开")

    # --- 抽奖弹窗：脚必须留在窗口里 ------------------------------------------

    def _close_lottery(self):
        for child in list(self.app.winfo_children()):
            if isinstance(child, ctk.CTkToplevel) and child.title() in (
                    "每日抽奖", "抽奖概率"):
                child.destroy()
        self.app.update()

    def _open_lottery(self, prize=None):
        self.app._open_slot_machine(
            prize or {"kind": "title", "value": "king_of_luck"})
        self.app.update()
        win = self._dialog("每日抽奖")
        self.assertIsNotNone(win, "抽奖窗口没开出来")
        return win

    def test_the_lottery_disclaimer_is_not_clipped_by_the_window(self):
        # 免责声明是箱底最后一行。窗口高度以前写死 600，内容比它高，这一行就落在
        # 窗口之外 —— 用户只看得到半行。现在头脚固定、中间滚动，这条断言钉的就是
        # 「整段都在窗口内」。
        win = self._open_lottery()
        try:
            hits = [w for w in self._find("无货币价值") if w.winfo_ismapped()]
            self.assertTrue(hits, "免责声明没出现在抽奖窗口里")
            label = hits[0]
            self.assertLessEqual(
                label.winfo_rooty() + label.winfo_height(),
                win.winfo_rooty() + win.winfo_height(),
                "免责声明被窗口底边切掉了")
            # 一行只有 20 上下；这里该有两三行，压成一行高说明后面几行还在窗口外。
            self.assertGreaterEqual(label.winfo_height(), 30,
                                    "免责声明只排了一行，后面被截了")
        finally:
            self._close_lottery()

    def test_the_paytable_lists_every_tier_with_two_decimals(self):
        win = self._open_lottery({"kind": "points", "value": 1})
        try:
            buttons = [b for b in self._buttons_in(win)
                       if b.cget("text") == "抽奖概率"]
            self.assertTrue(buttons, "抽奖弹窗里没有「抽奖概率」按钮")
            self.assertFalse(self._find("奖项说明"), "旧名字「奖项说明」还在")
            buttons[0].invoke()
            self.app.update()
            table = self._dialog("抽奖概率")
            self.assertIsNotNone(table, "点了按钮没开出概率表")
            texts = self._texts_in(table)
            for _symbols, name, percent in slg_titles.lottery_paytable():
                self.assertIn(name, texts, "概率表少了「%s」" % name)
                self.assertIn("%.2f%%" % percent, texts,
                              "「%s」的概率没显示成两位小数" % name)
            self.assertIn("0.01%", texts, "最高一档显示成了 0%")
        finally:
            self._close_lottery()

    def test_the_title_popup_keeps_its_longest_text_inside_the_scroller(self):
        # 「获得方式」文案最长的就是幸运之王那句。滚动区窗口只有 ~464 物理像素，
        # 换行宽度写大了最后几个字会被右边缘吃掉 —— 这里量的是文字实际占的宽度。
        # CTk 6.0 把滚动区挂在自己的 Canvas 下，所以要整棵子树找，不能只看一层。
        t = slg_titles.title_by_id(slg_titles.LOTTERY_JACKPOT_TITLE)
        want = slg_titles.obtain_title_text(t)
        self.app._title_info(t)
        self.app.update()
        try:
            win = self._dialog(t["name"])
            self.assertIsNotNone(win, "头衔弹窗没开出来")
            body = None
            stack = list(win.winfo_children())
            while stack:
                node = stack.pop()
                if isinstance(node, ctk.CTkScrollableFrame):
                    body = node
                    break
                stack.extend(node.winfo_children())
            self.assertIsNotNone(body, "头衔弹窗没有滚动区")
            self.assertIn(want, self._texts_in(body), "获取方式不是完整的一句")
            viewport = body._parent_canvas.winfo_width()
            self.assertGreater(viewport, 1, "滚动区还没排出来")
            labels = [w for w in self._find(want) if w.winfo_ismapped()]
            self.assertTrue(labels, "获取方式那条标签没排出来")
            for label in labels:
                self.assertLessEqual(label.winfo_width(), viewport,
                                     "这段文字比滚动区还宽，右边会被切掉")
        finally:
            if win.winfo_exists():
                win.destroy()
            self.app.update()

    def test_toggling_reverses_the_arrow_and_the_query(self):
        original = self.app.sort_desc
        try:
            with mock.patch.object(slg_db, "find_games", return_value=[]) as fg:
                self.app._toggle_sort_dir()
                self.assertNotEqual(self.app.sort_desc, original)
                self.assertEqual(self.app.sort_dir_btn.cget("text"),
                                 slg_gui.SORT_ARROW[self.app.sort_desc])
                self.assertEqual(fg.call_args.kwargs["desc"], self.app.sort_desc)
        finally:
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.sort_desc = original
                self.app.refresh()

    def test_switching_field_returns_to_that_fields_natural_direction(self):
        # Flipping 名称 to Z→A and then picking 站内评分 should not open the
        # list on the worst-rated games.
        original = self.app.sort
        try:
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app._on_sort("名称")
                self.assertFalse(self.app.sort_desc)   # names open A→Z
                self.app._toggle_sort_dir()
                self.assertTrue(self.app.sort_desc)
                self.app._on_sort("站内评分")
                self.assertEqual(self.app.sort, "rating")
                self.assertTrue(self.app.sort_desc)
        finally:
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.sort = original
                self.app.sort_desc = slg_gui.SORT_DEFAULT_DESC[original]
                self.app.refresh()

    def test_changing_sort_resets_the_page_cursor(self):
        # Every other state-change handler resets the page; _on_sort was the one
        # that did not, so a sort change left the user deep in a list whose
        # order had just changed under them.
        try:
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.page = 3
                self.app._on_sort("名称")
                self.assertEqual(self.app.page, 1)
        finally:
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.sort = "score"
                self.app.sort_desc = slg_gui.SORT_DEFAULT_DESC["score"]
                self.app.refresh()

    def test_turning_the_page_clamps_to_the_catalogue(self):
        # A page number past the end is a click on 下一页 from the last page, or
        # a typed jump. Either way it lands on a real page rather than an empty
        # one - _page_slice() of an out-of-range page is [] and the list would
        # go blank with no way back.
        try:
            games = [self._fake_game(i) for i in range(20)]
            with mock.patch.object(slg_db, "find_games", return_value=games):
                with mock.patch.object(self.app, "_render_filterbar"), \
                        mock.patch.object(self.app, "_render_stats"), \
                        mock.patch.object(self.app, "_render_detail_if_stale"):
                    pages = self.app._page_count()
                    self.app.page = pages + 5
                    self.app._clamp_page()
                    self.assertEqual(self.app.page, pages)
                    self.app.page = 0
                    self.app._clamp_page()
                    self.assertEqual(self.app.page, 1)
        finally:
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.page = 1
                self.app.refresh()

    def test_a_page_holds_exactly_the_page_size(self):
        # The slice and the pool have to agree: _sync_cards indexes the pool by
        # position in the list it is handed, so a slice that disagreed with
        # PAGE_SIZE would either leave cards hidden or build more than a page.
        try:
            games = [self._fake_game(i) for i in range(slg_gui.PAGE_SIZE * 3)]
            with mock.patch.object(slg_db, "find_games", return_value=games):
                with mock.patch.object(self.app, "_render_filterbar"), \
                        mock.patch.object(self.app, "_render_stats"), \
                        mock.patch.object(self.app, "_render_detail_if_stale"):
                    self.app.page = 2
                    self.app.refresh()
                    page = self.app._page_slice()
                    self.assertEqual(len(page), slg_gui.PAGE_SIZE)
                    self.assertEqual(len(self.app._card_pool), slg_gui.PAGE_SIZE)
                    self.assertEqual(page[0]["id"], games[slg_gui.PAGE_SIZE]["id"])
        finally:
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.page = 1
                self.app.refresh()

    def test_the_detail_panel_carries_the_no_download_notice(self):
        # Built into the skeleton and shown unconditionally by _layout_detail,
        # so it is there for every game rather than the ones with a blurb.
        self.assertIn("disclaimer", self.app._detail_parts)
        self.assertIn("不提供下载",
                      self.app._detail_parts["disclaimer"].cget("text"))

    def test_the_vpn_notice_points_at_the_scrapers_own_base_url(self):
        # Two copies of the URL is one copy that goes stale.
        self.assertEqual(slg_gui.SITE_URL, "https://dikgames.com")

    # --- the feedback group --------------------------------------------------

    def test_the_group_number_is_plain_digits(self):
        # It gets pasted straight into QQ's search box, so a dash or a space
        # copied along with it is a bug the user has to clean up by hand.
        self.assertTrue(slg_gui.QQ_GROUP.isdigit(),
                        "群号里有非数字字符：%r" % slg_gui.QQ_GROUP)
        self.assertIn(slg_gui.QQ_GROUP, slg_gui.QQ_GROUP_LABEL)

    def test_copying_the_group_number_reaches_the_clipboard(self):
        self.app._copy_value(slg_gui.QQ_GROUP)
        self.app.update()
        self.assertEqual(self.app.clipboard_get(), slg_gui.QQ_GROUP)

    def test_the_copy_flashes_and_then_puts_the_button_back(self):
        # A silent copy leaves the user wondering whether the click landed. Both
        # halves matter: the check mark is √ rather than ✓ because the latter is
        # an empty box in 雅黑, and the button must not keep saying 已复制.
        #
        # `after` is captured rather than waited on: the real delay is 1.2s, and
        # a test that sleeps through it is a test nobody runs.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            button = self.app.qq_btn
            scheduled = []
            with mock.patch.object(self.app, "after",
                                   side_effect=lambda ms, fn: scheduled.append(fn)):
                self.app._copy_value(slg_gui.QQ_GROUP, button,
                                     slg_gui.QQ_GROUP_COPY_LABEL)
            self.app.update()
            self.assertEqual(button.cget("text"), "已复制 √")
            self.assertEqual(self.app.clipboard_get(), slg_gui.QQ_GROUP)

            self.assertTrue(scheduled, "没有安排还原回调")
            for callback in scheduled:
                callback()
            self.app.update()
            self.assertEqual(button.cget("text"), slg_gui.QQ_GROUP_COPY_LABEL)
        finally:
            self.app.selected = None
            self._finish()

    def test_a_stale_flash_callback_survives_a_theme_switch(self):
        # The restore lands 1.2s later, and a theme switch in between rebuilds
        # the window - so the callback runs against a button Tcl has already
        # deleted. The winfo_exists guard is the only thing between that and a
        # TclError traceback with the user's name on it.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            button = self.app.qq_btn
            scheduled = []
            with mock.patch.object(self.app, "after",
                                   side_effect=lambda ms, fn: scheduled.append(fn)):
                self.app._copy_value(slg_gui.QQ_GROUP, button,
                                     slg_gui.QQ_GROUP_COPY_LABEL)
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme("dark")
            self.app.update()
            self.assertFalse(button.winfo_exists(), "按钮没被主题切换重建")

            for callback in scheduled:
                callback()  # must not raise
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme("light")
            self.app.update()
        finally:
            self.app.selected = None
            self._finish()

    def test_the_group_number_is_in_the_about_dialog(self):
        self.app.open_about()
        try:
            self.app.update()
            win = self._dialog("关于")
            self.assertIsNotNone(win, "关于弹窗没打开")
            hits = [hit for hit in self._find(slg_gui.QQ_GROUP)
                    if self._inside(hit, win)]
            self.assertTrue(hits, "关于弹窗里没有群号")
        finally:
            self._close("关于")

    def test_the_group_number_is_in_the_help_document(self):
        self.app.open_help()
        try:
            self.app.update()
            win = self._dialog("帮助文档")
            self.assertIsNotNone(win, "帮助文档没打开")
            self._help_click("声明与关于")
            hits = [hit for hit in self._find(slg_gui.QQ_GROUP)
                    if self._inside(hit, win)]
            self.assertTrue(hits, "帮助文档里没有群号")
        finally:
            self._close("帮助文档")

    def test_the_group_number_sits_under_the_panel_not_inside_it(self):
        # The panel is a description of one game and scrolls with it; the group
        # is about the app. Nested in the panel it scrolled out of sight behind
        # a long blurb, which is exactly what moved it to the strip below.
        hits = self._find(slg_gui.QQ_GROUP)
        self.assertTrue(hits, "窗口里找不到群号")
        self.assertTrue([hit for hit in hits
                         if self._inside(hit, self.app.qq_holder)],
                        "群号不在详情面板下方的那一条里")
        self.assertFalse([hit for hit in hits
                          if self._inside(hit, self.app.detail)],
                         "群号还嵌在详情面板里")

    def test_the_group_strip_is_level_with_the_pager(self):
        # Same row and same top padding as the pager, so the two read as one
        # line running across the bottom of the window.
        group = self.app.qq_holder.grid_info()
        pager = self.app._pager_holder.grid_info()
        self.assertEqual(int(group["row"]), int(pager["row"]), "没和翻页栏同一行")
        self.assertEqual(group["pady"], pager["pady"], "顶边没和翻页栏对齐")
        self.assertEqual(int(group["column"]), 1, "没在详情面板那一列下面")

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
        self.app._pager = self.app._pager_parts = None
        self.app._empty_label = None
        self.app._rendered_ids = []
        self.app.selected = None

    def _filterbar_texts(self):
        """Every chip label currently in the filter bar.

        Walks the whole subtree, not just the bar's direct children: the row's
        widgets live inside the centring frame that keeps it on the window's
        centre axis, one level below `self.app.filterbar`.
        """
        texts = []

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    texts.append(str(child.cget("text")))
                except Exception:  # noqa: BLE001 - not every widget has text
                    pass
                walk(child)

        walk(self.app.filterbar)
        return texts

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
                mock.patch.object(self.app, "_render_detail_if_stale"), \
                mock.patch.object(slg_gui, "PAGE_SIZE",
                                  max(slg_gui.PAGE_SIZE, len(games))):
            # Page size is widened to the fixture rather than the fixture cut
            # down to the page: these tests reach for _card_pool[5] and
            # _card_pool[7], and a page of six would leave those slots
            # unbuilt. The app never renders more than a page - that is the
            # whole change - so the mock is the only way to say "show all of
            # this" here.
            self.app.page = 1
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
            self.app.page = 1
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
        # Both the "no matches" label and its counterpart used to be built
        # fresh on every render and left behind on the ones that did not need
        # them, so a few searches grew the list's child count without bound.
        # The pager is outside the list for this reason too: it is a sibling in
        # the body grid, not a child that comes and goes with the page.
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
        self.app.page = 1
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

    def test_a_game_without_a_description_keeps_the_block_and_says_so(self):
        # The description block stays put and pleads ignorance instead of
        # vanishing. Hiding it made 968 of 1595 games read as "the app lost a
        # section", with nothing on screen to click to write one by hand.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            self.assertIn("ov_box", self.app._detail_shown)
            self.assertIn("url", self.app._detail_shown)
            self.app.select(self._panel_game(1, overview="", url=""))
            self.assertIn("ov_box", self.app._detail_shown, "简介块被藏起来了")
            self.assertNotIn("url", self.app._detail_shown)
            self.assertEqual(self.app._detail_parts["ov_label"].cget("text"),
                             slg_gui.EMPTY_OVERVIEW)
            self.app.select(self._panel_game(0))
            self.assertNotEqual(self.app._detail_parts["ov_label"].cget("text"),
                                slg_gui.EMPTY_OVERVIEW)
            order = [w[0] for w in self.app._detail_order
                     if w[0] in self.app._detail_shown]
            self.assertEqual(order, self.app._detail_shown)
            self.assertLess(order.index("status"), order.index("ov_box"))
        finally:
            self.app.selected = None
            self._finish()

    def test_a_game_without_a_description_is_not_sent_to_the_translator(self):
        # Switching to 中文 on a blurb that does not exist used to fire a
        # request anyway: one wasted call per visit, reported back as a
        # translation failure over a game the site simply has no blurb for.
        self._pool_reset()
        try:
            bare = self._panel_game(1, overview="", url="")
            self.app.rows = [bare]
            self.app.page = 1
            with mock.patch.object(slg_gui.threading, "Thread") as thread:
                self.app.select(bare)
                self.app._set_overview_lang(bare, "中文")
            started = [c.kwargs.get("target") for c in thread.call_args_list]
            self.assertNotIn(self.app._overview_worker, started, "空简介仍发了翻译请求")
            self.assertEqual(self.app._detail_parts["ov_label"].cget("text"),
                             slg_gui.EMPTY_OVERVIEW)
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
            # Switching to 中文 with nothing cached would start a real
            # translation thread, and its failure lands in app.queue whenever
            # it feels like it - usually after this test has torn down, where
            # it shows up as an extra _overview_result in whichever test runs
            # next. Nothing here is about the request, so it does not run.
            with mock.patch.object(slg_gui.threading, "Thread"):
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

    def test_a_multi_line_note_round_trips(self):
        # The 评价 field is a Textbox now. It was a CTkEntry, which wraps
        # tkinter.Entry and so could not wrap at all - a review longer than
        # the panel just scrolled sideways out of view.
        self._pool_reset()
        gid = None
        try:
            # _set_note writes through slg_db.set_state, and state.game_id is a
            # foreign key to games(id) - the made-up 9000 the other panel tests
            # use is refused outright. The suite points LOCALAPPDATA at a temp
            # directory, so this row is not the user's catalogue, and it is
            # deleted below so a later test counting the list does not see it.
            gid, _ = slg_db.upsert_game(self.app.conn, "note-roundtrip",
                                        "https://x/note-roundtrip", "Note Roundtrip")
            self.app.conn.commit()
            game = self._panel_game(0, note="第一行\n第二行")
            game["id"] = gid
            self._select(game)
            box = self.app._detail_parts["note_entry"]
            self.assertEqual(box.get("1.0", "end-1c"), "第一行\n第二行")
            box.delete("1.0", "end")
            box.insert("1.0", "改过的\n两行\n三行")
            self.app._set_note()
            # On self.selected, not `game`: select() keeps a dict() copy so the
            # status and rating buttons can write back into a mutable object.
            self.assertEqual(self.app.selected["note"], "改过的\n两行\n三行")
            stored = slg_db.get_state(self.app.conn, gid)["note"]
            self.assertEqual(stored, "改过的\n两行\n三行")
            # No trailing newline: a Textbox's get() ends with one it made
            # itself, and storing it puts a blank last line in every review.
            self.assertFalse(stored.endswith("\n"))
        finally:
            if gid is not None:
                self.app.conn.execute("DELETE FROM games WHERE id = ?", (gid,))
                self.app.conn.commit()
            self._finish()

    def test_escape_puts_the_stored_note_back(self):
        self._pool_reset()
        try:
            game = self._panel_game(0, note="原来的")
            self._select(game)
            box = self.app._detail_parts["note_entry"]
            box.delete("1.0", "end")
            box.insert("1.0", "打了一半又反悔")
            self.app._cancel_note_edit()
            self.assertEqual(box.get("1.0", "end-1c"), "原来的")
        finally:
            self._finish()

    def test_the_note_field_wraps(self):
        # What the bug report was actually about. CTkEntry has no wrap
        # setting to give; the Textbox has one, and word wrapping is it.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            box = self.app._detail_parts["note_entry"]
            self.assertEqual(str(box.cget("wrap")), "word")
        finally:
            self._finish()

    def test_the_note_block_says_how_to_save_it(self):
        # Asked for by name: tell the user that this is how it saves. The key
        # is not guessable - Enter inserts a newline in a Textbox where it used
        # to submit an Entry - so without the hint a written review looks
        # unsavable and the box reads as broken.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            row = self.app._detail_parts["note_row"]
            texts = []
            for child in row.winfo_children():
                try:
                    texts.append(child.cget("text"))
                except Exception:  # noqa: BLE001 - most widgets have no text
                    pass
            self.assertTrue(any("Ctrl+Enter" in t for t in texts if t),
                            "没有任何地方告诉用户怎么保存：%s" % texts)
            self.assertIn("保存", texts, "没有保存按钮：%s" % texts)
            # And the block is actually on screen, not just built.
            self.assertIn("note_row", self.app._detail_shown)
        finally:
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

    def test_a_renamed_tag_reaches_the_filter_chips(self):
        # The bar's redraw check compares the include/exclude lists, and a
        # rename changes the chips' text without touching either list - so the
        # bar kept showing the old Chinese under the new one.
        self._pool_reset()
        include = list(self.app.include)
        try:
            self.app.include = ["netorare"]
            self.app._render_filterbar()
            self.app.update()
            self.assertTrue(any("netorare" in text
                                for text in self._filterbar_texts()),
                            "筛选条上没有这个标签")
            slg_db.set_manual_translation(self.app.conn, "tag", "netorare", "NTR")
            self.app._reload_tags(None, "")
            self.app.update()
            self.assertTrue(any("NTR" in text for text in self._filterbar_texts()),
                            "改完译名筛选条还是旧中文：%s" % self._filterbar_texts())
        finally:
            self.app.include = include
            slg_db.delete_manual_translation(self.app.conn, "tag", "netorare")
            slg_gui.load_tag_translations(self.app.conn)
            self.app._filter_sig = None
            self.app._render_filterbar()
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
            self.assertTrue(self.app._pager is None
                            or self.app._pager.winfo_exists())
        finally:
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme(original)
            self.app.update()
            self._finish()

    # --- the mouse, on things that are not frames ----------------------------

    def test_a_click_on_the_title_opens_the_game(self):
        # A plain tk.Label does not pass its clicks up to the frame that holds
        # it, and the title, the version line and the tagline are all plain
        # labels - so binding only the card frame left most of the card's
        # surface dead. Clicking a game's name did nothing at all, and only the
        # thin border around it worked.
        self._pool_reset()
        try:
            games = [self._fake_game(i) for i in range(4)]
            self._render(games)
            for key in ("title", "meta", "tagline"):
                with mock.patch.object(self.app, "select") as select:
                    self.app._card_pool[1][key].event_generate("<Button-1>")
                    self.app.update()
                self.assertTrue(select.called, "点卡片上的 %s 没有打开游戏" % key)
                self.assertEqual(select.call_args[0][0]["id"], games[1]["id"])
        finally:
            self._finish()

    # --- dialogs -------------------------------------------------------------

    def test_a_dialog_binds_escape(self):
        # Tk hands a bare Toplevel no bindings, so every dialog in the app could
        # only be closed with the window manager's X.
        #
        # The binding is asserted rather than exercised: Tk delivers a key event
        # to the focused widget, and with no window manager in the loop nothing
        # inside a fresh Toplevel ever takes focus - event_generate("<Escape>")
        # on an unfocused window is silently dropped, so a keystroke-based test
        # here would pass no matter what the app does.
        win = self.app._new_dialog("测试对话框", "300x200")
        try:
            self.app.update()
            self.assertTrue(win.bind("<Escape>"),
                            "对话框没有绑定 Escape，只能靠窗口的 X 关掉")
        finally:
            if win.winfo_exists():
                win.destroy()

    def test_opening_the_same_dialog_twice_reuses_the_window(self):
        # Two clicks on 帮助文档 used to leave two identical windows stacked.
        first = self.app._new_dialog("测试对话框", "300x200")
        self.app.update()
        try:
            second = self.app._new_dialog("测试对话框", "300x200")
            self.app.update()
            self.assertFalse(first.winfo_exists(), "旧对话框没有被关掉")
            self.assertTrue(second.winfo_exists())
            open_windows = [w for w in self.app.winfo_children()
                            if isinstance(w, ctk.CTkToplevel)
                            and w.title() == "测试对话框"]
            self.assertEqual(len(open_windows), 1, "同一个对话框开了两个窗口")
        finally:
            for child in list(self.app.winfo_children()):
                if isinstance(child, ctk.CTkToplevel) and child.title() == "测试对话框":
                    child.destroy()

    # --- which language the panel opens in -----------------------------------

    def test_the_panel_opens_on_the_original_even_when_a_translation_exists(self):
        # A cached Chinese title must not pre-select 中文: the panel always
        # opens on 原文.
        self._pool_reset()
        try:
            game = self._panel_game(0)
            slg_db.set_auto_translation(self.app.conn, "title", game["id"],
                                        game["title"], "缓存的中文名", engine="m")
            slg_gui.load_title_translations(self.app.conn)
            self._select(game)
            self.assertEqual(self.app._detail_parts["title"].cget("text"),
                             game["title"])
        finally:
            slg_db.delete_translation(self.app.conn, "title", game["id"])
            slg_gui.load_title_translations(self.app.conn)
            self.app.selected = None
            self._finish()

    def test_the_language_resets_to_original_on_the_next_game(self):
        # Flipping to 中文 is per-panel only: the next game reopens on 原文
        # instead of inheriting the last choice.
        self._pool_reset()
        try:
            self._select(self._panel_game(0, overview="One."))
            with mock.patch.object(slg_gui.threading, "Thread"):
                self.app._set_overview_lang(self.app.selected, "中文")
            self._select(self._panel_game(1, overview="Two."))
            self.assertEqual(self.app._detail_parts["ov_seg"].get(), "原文")
        finally:
            self.app.selected = None
            self._finish()

    def test_opening_a_game_spends_no_request(self):
        # _fill_detail runs on every click in the list and opens on 原文, so
        # browsing must never fire a translation request.
        self._pool_reset()
        try:
            with mock.patch.object(slg_gui.threading, "Thread") as thread:
                self._select(self._panel_game(0, overview="Nothing cached."))
            self.assertFalse(thread.called, "被动填充发出了翻译请求")
        finally:
            self.app.selected = None
            self._finish()


    # --- the sidebar's action buttons ----------------------------------------

    def _dialog(self, title):
        for child in self.app.winfo_children():
            if isinstance(child, ctk.CTkToplevel) and child.title() == title:
                return child
        return None

    def _buttons_in(self, widget):
        found = []

        def walk(node):
            for child in node.winfo_children():
                if isinstance(child, ctk.CTkButton):
                    found.append(child)
                walk(child)

        walk(widget)
        return found

    def _tag_buttons(self, win):
        """The tag cells only, not the 清空/完成 pair below them.

        The scrollable frame is not a direct child of the Toplevel - it lives
        inside a Canvas the CTk wrapper builds - so this has to walk the whole
        subtree. Geometry is what separates the two groups: cells are gridded
        (two per row is the point), the footer pair is packed.
        """
        cells = []
        for button in self._buttons_in(win):
            try:
                if button.grid_info():
                    cells.append(button)
            except tk.TclError:
                pass
        return cells

    def _close(self, title):
        """Destroy a dialog and let Tk finish the teardown.

        The update() is not optional. customtkinter's appearance-mode tracker
        holds a callback per widget, and destroying a Toplevel leaves the
        actual window teardown queued. A theme switch that lands before the
        queue drains walks those half-dead widgets and takes the whole process
        down with a segfault - which is what this class's theme tests did until
        this line was added.
        """
        win = self._dialog(title)
        if win is not None:
            win.destroy()
            self.app.update()

    def _help_nav_titles(self, win):
        """The left-hand chapter list, top to bottom.

        Read off the live buttons rather than off HELP_SECTIONS: the point is to
        catch a chapter that is declared but never built, or built but left
        unmapped behind a zero-height frame.
        """
        self.app.update()  # a freshly built Toplevel is unmapped until Tk runs
        nav = getattr(self.app, "_help_nav", None)
        self.assertTrue(nav, "帮助文档没有左栏目录")
        for btn in nav.values():
            self.assertTrue(btn.winfo_ismapped(), "目录里有按钮没显示出来")
        return [btn.cget("text") for btn in nav.values()]

    def _help_click(self, title):
        """Click a chapter in the left nav; raises if it is not there."""
        for btn in self.app._help_nav.values():
            if btn.cget("text") == title:
                btn.invoke()
                self.app.update()
                return
        raise AssertionError("左栏里没有「%s」" % title)

    def _help_pane_lines(self, win):
        """Label texts in the right-hand pane, in pack order.

        Only the customtkinter widgets are read: every CTkLabel wraps a plain
        tkinter.Label carrying the same text, so walking raw children reports
        each line twice - and the inner one is not the widget that was packed.
        """
        lines = []

        def walk(widget):
            for child in widget.winfo_children():
                if type(child).__module__.startswith("customtkinter"):
                    try:
                        text = child.cget("text")
                    except Exception:  # noqa: BLE001 - most widgets have no text
                        text = None
                    if isinstance(text, str):
                        lines.append(text)
                    walk(child)

        walk(self.app._help_content)
        return lines

    def test_the_help_nav_lists_every_chapter(self):
        # One scrolling column stopped being usable the moment the growth
        # system landed: seventeen chapters, findable only by scrolling.
        self.app.open_help()
        try:
            win = self._dialog("帮助文档")
            self.assertIsNotNone(win, "帮助文档没打开")
            got = self._help_nav_titles(win)
        finally:
            self._close("帮助文档")
        want = [title for _key, title, _builder in self.app.HELP_SECTIONS]
        self.assertEqual(got, want, "左栏和声明的章节对不上")
        self.assertGreaterEqual(len(got), 15)

    def test_the_help_doc_opens_with_the_three_step_ladder(self):
        # The three clicks that make the app worth using lived only in the
        # first-run welcome, which is gone by the time anyone needs reminding.
        self.app.open_help()
        try:
            win = self._dialog("帮助文档")
            self.assertIsNotNone(win, "帮助文档没打开")
            self.assertEqual(self._help_nav_titles(win)[0], "三步上手",
                             "三步上手 应该是第一节，且开箱就在右栏")
            lines = self._help_pane_lines(win)
        finally:
            self._close("帮助文档")
        steps = [t for t in lines if t.startswith(("第 1 步", "第 2 步", "第 3 步"))]
        self.assertEqual(len(steps), 3, lines)
        index = [lines.index(s) for s in steps]
        self.assertEqual(index, sorted(index), "三步的顺序乱了")

    def test_the_help_faq_is_grouped(self):
        # Twelve answers in a flat run meant the one you wanted was found by
        # scrolling, not by looking.
        self.app.open_help()
        try:
            win = self._dialog("帮助文档")
            self.assertIsNotNone(win, "帮助文档没打开")
            self._help_click("常见问题")
            lines = self._help_pane_lines(win)
        finally:
            self._close("帮助文档")
        groups = ("界面与设置", "同步与数据", "抽奖与积分", "翻译")
        for group in groups:
            self.assertIn(group, lines, lines)
        index = [lines.index(g) for g in groups]
        self.assertEqual(index, sorted(index), "问答分组乱了")
        self.assertGreaterEqual(sum(1 for t in lines if t.startswith("Q：")), 8,
                                "问答删太多了")

    def test_the_help_disclaimers_come_last(self):
        # The no-download notice used to sit between 常见问题 and the
        # English-game instructions, interrupting the one part of the doc that
        # tells someone what to actually do next.
        self.app.open_help()
        try:
            win = self._dialog("帮助文档")
            self.assertIsNotNone(win, "帮助文档没打开")
            self.assertEqual(self._help_nav_titles(win)[-1], "声明与关于")
            self._help_click("声明与关于")
            lines = self._help_pane_lines(win)
        finally:
            self._close("帮助文档")
        no_download = next(i for i, t in enumerate(lines)
                           if "不提供任何下载" in t)
        free = next(i for i, t in enumerate(lines) if "完全免费" in t)
        self.assertLess(no_download, free, "免费声明应该在最后")
        self.assertTrue(any("GitHub" in t for t in lines), lines)

    def test_the_help_doc_covers_the_growth_system(self):
        # v0.22 加的收藏夹、个人中心/签到/积分/头衔、商城抽奖、添加我的游戏、
        # 备份恢复、更新推送，帮助文档里一个都没有——这就是「跟不上版本」。
        self.app.open_help()
        try:
            win = self._dialog("帮助文档")
            self.assertIsNotNone(win, "帮助文档没打开")
            titles = self._help_nav_titles(win)
        finally:
            self._close("帮助文档")
        for want in ("收藏夹", "个人中心与积分", "每日签到", "头衔",
                     "商城与每日抽奖", "添加我的游戏", "扫描本地目录",
                     "备份与恢复", "更新推送"):
            self.assertIn(want, titles)

    def test_the_sidebar_offers_sync_and_one_more_button(self):
        # Four buttons in a column - 28/38/34/28 px tall, two muted and one
        # filled, two left-aligned and one centred - gave a new user no way to
        # tell which one they wanted. Sync is the routine one and stays out;
        # the other three are behind 更多….
        self.assertIsNotNone(self.app.sync_btn)
        self.assertIsNotNone(self.app.maintenance_btn)
        self.assertIsNotNone(self.app.settings_btn)
        self._assert_has_height("更新游戏数据", "同步按钮")
        self._assert_has_height("更多", "更多按钮")
        for gone in ("covers_btn", "rebuild_btn", "backfill_btn"):
            self.assertFalse(hasattr(self.app, gone), gone)

    def test_the_gear_is_visible_and_leads_to_the_about_page(self):
        # The toolbar gear opens 关于 (version/update/links); the set-once tools
        # live behind 更多工具… in the sidebar - two different doors now.
        self._assert_has_height("⚙", "关于齿轮")
        try:
            self.app.open_about()
            self.assertIsNotNone(self._dialog("关于"), "关于弹窗没打开")
        finally:
            self._close("关于")

    def test_the_tools_dialog_holds_the_set_once_entries(self):
        # These are set-once tools, out of the sidebar's routine column.
        try:
            self.app.open_tools()
            win = self._dialog("更多工具")
            self.assertIsNotNone(win, "更多工具弹窗没打开")
            texts = [b.cget("text") for b in self._buttons_in(win)]
            for label in ("标签译名…", "偏好权重…", "翻译设置…", "扫描本地目录…", "检查更新"):
                self.assertIn(label, texts)
        finally:
            self._close("更多工具")

    def test_the_tool_group_no_longer_lists_the_set_once_entries(self):
        # 标签库 is the one routine filter that stayed in the sidebar; 检查更新
        # and the set-once four (including the local scan) moved into 更多工具….
        self.assertTrue(self._find("标签库…"), "标签库 被一起搬走了")
        for label in ("标签译名…", "偏好权重…", "翻译设置…", "扫描本地目录", "检查更新"):
            self.assertEqual(self._find(label), [], "%s 还留在左侧栏" % label)

    def test_the_version_label_is_just_the_version(self):
        # The build time is a question the user has once. It pushed the version
        # itself - the part that tells a stale exe from a fresh one - into a
        # long line nobody finishes reading.
        hits = self._find("v" + slg_gui.APP_VERSION)
        self.assertTrue(hits, "版本号没了")
        for hit in hits:
            text = hit.cget("text")
            self.assertNotIn("·", text, text)
            self.assertNotIn("源码", text, text)

    def test_the_sidebar_asks_for_feedback_and_a_star(self):
        # Two lines under the version, and the header is packed side="top" in a
        # column that is already tight at 940x600 - so they have to be checked
        # for height, not just for being constructed.
        self._assert_has_height(slg_gui.CONTACT_EMAIL, "侧栏反馈邮箱")
        self._assert_has_height("求个 GitHub star", "侧栏求 star 文案")

    def test_the_detail_footer_carries_the_same_ask(self):
        # Same two sentences, at the foot of the panel where someone who just
        # finished a game is actually looking.
        self._pool_reset()
        try:
            self._select(self._panel_game(0))
            self._assert_has_height("点个 star", "详情页求 star 文案")
            self._assert_has_height("反馈 / 建议", "详情页反馈按钮")
        finally:
            self.app.selected = None
            self._finish()

    def test_the_maintenance_dialog_lists_both_chores(self):
        try:
            with mock.patch.object(slg_db, "data_gaps",
                                   return_value={"covers": 7, "overview": 3,
                                                 "heat": 5, "metrics": 5}):
                self.app.open_maintenance()
                win = self._dialog("同步与维护")
                self.assertIsNotNone(win, "维护弹窗没打开")
                texts = [b.cget("text") for b in self._buttons_in(win)]
            for label in ("下载封面", "补齐热度"):
                self.assertTrue(any(t.startswith(label) for t in texts), texts)
            # Computed when the dialog opens, so a stale number in a window the
            # user cannot see is not possible.
            self.assertIn("下载封面（7）", texts)
            self.assertIn("补齐热度（5）", texts)
        finally:
            self._close("同步与维护")

    def test_picking_a_tag_leaves_the_picker_open(self):
        # Choosing a tag used to destroy the dialog, so picking four tags meant
        # four round trips through 标签库.
        rows = [{"name": "netorare", "n": 40}, {"name": "cheating", "n": 12}]
        was = (list(self.app.include), list(self.app.exclude))
        try:
            with mock.patch.object(slg_db, "tag_counts", return_value=rows), \
                    mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.open_tag_picker()
                win = self._dialog("标签库")
                self.assertIsNotNone(win, "标签库没打开")
                target = next(b for b in self._tag_buttons(win)
                              if b.cget("text").startswith("netorare"))
                target.invoke()
                self.app.update()
                self.assertIn("netorare", self.app.include)
                self.assertTrue(win.winfo_exists(), "选完一个标签后窗口被关掉了")
                # Still selectable: a second click on the same row toggles off.
                target = next(b for b in self._tag_buttons(win)
                              if b.cget("text").startswith("netorare"))
                target.invoke()
                self.assertNotIn("netorare", self.app.include)
                self.assertTrue(win.winfo_exists())
        finally:
            self.app.include, self.app.exclude = was
            self._close("标签库")
            with mock.patch.object(slg_db, "find_games", return_value=[]):
                self.app.refresh()

    def test_the_tag_search_box_narrows_the_grid(self):
        # 130 tags in one column was a long scroll, and the tag the user wants
        # is usually one whose name they already know.
        rows = [{"name": "netorare", "n": 40}, {"name": "cheating", "n": 12},
                {"name": "big-tits", "n": 9}]
        try:
            with mock.patch.object(slg_db, "tag_counts", return_value=rows):
                self.app.open_tag_picker()
                win = self._dialog("标签库")
                self.assertEqual(len(self._tag_buttons(win)), 3)
                entry = next(c for c in self._walk(win)
                             if isinstance(c, ctk.CTkEntry))
                self._type(entry, "net")
                self.assertEqual(len(self._tag_buttons(win)), 1)
                # Matched on the slug as well as the translated name.
                self._type(entry, "cheat")
                self.assertEqual(len(self._tag_buttons(win)), 1)
        finally:
            self._close("标签库")

    def _type(self, entry, text):
        """Replace the search box's contents and fire the keystroke handler.

        Two things make this fiddly. CTkEntry.bind forwards to an inner entry
        widget, so the event has to be generated there - on the CTkEntry
        wrapper it dispatches to nothing. And Tk silently drops a generated key
        event on an unfocused widget, so the whole chain has to be focused
        first: the app, then the dialog, then the inner entry.
        """
        dialog = entry.winfo_toplevel()
        self.app.focus_set()
        dialog.lift()
        dialog.focus_set()
        self.app.update()
        entry._entry.focus_set()
        entry.delete(0, "end")
        entry.insert(0, text)
        self.app.update()
        entry._entry.event_generate("<KeyRelease>", when="now")
        self.app.update()

    def _walk(self, widget):
        for child in widget.winfo_children():
            yield child
            for deeper in self._walk(child):
                yield deeper

    def test_the_tag_grid_is_two_columns(self):
        rows = [{"name": "a", "n": 3}, {"name": "b", "n": 2}, {"name": "c", "n": 1}]
        try:
            with mock.patch.object(slg_db, "tag_counts", return_value=rows):
                self.app.open_tag_picker()
                win = self._dialog("标签库")
                cells = self._tag_buttons(win)
                self.assertEqual(len(cells), 3)
                # grid_info on the cell's container frame is what pack() would
                # not give us: two per row is the whole point of the grid.
                info = [int(c.grid_info()["column"]) for c in cells]
                self.assertEqual(info, [0, 1, 0])
        finally:
            self._close("标签库")

    # --- the update notice ---------------------------------------------------

    def test_the_update_notice_is_hidden_until_there_is_one(self):
        self.assertFalse(self.app.update_label.winfo_ismapped())

    def test_a_found_release_shows_a_clickable_sidebar_notice(self):
        try:
            self.app._show_update({"version": "v99.0.0", "url": "https://x/y"})
            self.app.update()
            self.assertEqual(self.app._update_url, "https://x/y")
            self.assertIn("v99.0.0", self.app.update_label.cget("text"))
            self.assertTrue(self.app.update_label.winfo_ismapped(),
                            "新版本提示没有出现在侧栏")
        finally:
            self.app._update_found = None
            self.app.update_label.pack_forget()
            self.app.update()

    def test_the_notice_survives_a_theme_switch(self):
        # The sidebar is destroyed and rebuilt on a theme change, and the
        # notice lives in it.
        original = self.app.theme_mode
        try:
            self.app._show_update({"version": "v99.0.0", "url": "https://x/y"})
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme("dark")
                self.app.update()
            self.assertIn("v99.0.0", self.app.update_label.cget("text"))
            self.assertTrue(self.app.update_label.winfo_ismapped())
        finally:
            with mock.patch.object(slg_db, "set_pref"):
                self.app._apply_theme(original)
            self.app.update()
            self.app._update_found = None
            self.app.update_label.pack_forget()
            self.app.update()


    # --- 数据统计面板 -------------------------------------------------------
    #
    # Every one of the four states used to render as the same empty panel, and
    # three of them are not "no data yet" - they are "the server was never
    # deployed", "your key is wrong" and "the network is down". These pin that
    # the panel says which one it is, in the words that name the fix.

    def _open_stats(self, result):
        with mock.patch.object(slg_remote, "fetch_stats", return_value=result), \
                mock.patch.object(slg_gui.threading, "Thread", _InlineThread):
            self.app._show_stats_panel()
        self.app._drain()
        self.app.update()
        self.addCleanup(self._close_window, self.app._stats_win)
        return self.app._stats_body

    @staticmethod
    def _close_window(win):
        try:
            if win.winfo_exists():
                win.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def _texts(widget):
        out = []

        def walk(node):
            for child in node.winfo_children():
                try:
                    text = child.cget("text")
                except Exception:  # noqa: BLE001 - most widgets have no text
                    text = None
                if isinstance(text, str):
                    out.append(text)
                walk(child)

        walk(widget)
        return out

    def test_the_stats_panel_translates_event_names(self):
        body = self._open_stats(("ok", {
            "events": {"launch": 5, "signin": 2}, "devices": 3,
            "last_report": "2026-09-22T10:00:00"}))
        texts = self._texts(body)
        self.assertIn("启动", texts)
        self.assertIn("签到", texts, "事件名没翻成中文，面板像给开发看的原始日志")

    def test_the_stats_panel_shows_devices_and_a_short_timestamp(self):
        body = self._open_stats(("ok", {
            "events": {"launch": 5}, "devices": 7,
            "last_report": "2026-09-22T10:00:00"}))
        texts = self._texts(body)
        self.assertIn("7", texts)
        self.assertIn("09-22 10:00:00", texts)

    def test_an_unknown_event_is_still_listed(self):
        # A new event type ships before its label does; dropping it silently
        # would make the panel look like the event never fired.
        body = self._open_stats(("ok", {
            "events": {"brand_new": 1}, "devices": 1, "last_report": None}))
        self.assertIn("brand_new", self._texts(body))

    def test_an_undeployed_server_names_the_command_that_fixes_it(self):
        body = self._open_stats(("missing", None))
        texts = self._texts(body)
        self.assertTrue(any("deploy_server.py --user ubuntu" in t for t in texts),
                        "面板没给出修复命令，用户只能看到一片空白")

    def test_a_key_mismatch_points_at_the_local_key_file(self):
        body = self._open_stats(("locked", None))
        texts = self._texts(body)
        self.assertTrue(any("stats_key.txt" in t for t in texts),
                        "没告诉用户是哪个文件里的密钥对不上")

    def test_the_panel_survives_an_empty_but_deployed_server(self):
        body = self._open_stats(("ok", {"events": {}, "devices": 0,
                                        "last_report": None}))
        self.assertTrue(self._texts(body))


if __name__ == "__main__":
    unittest.main()
