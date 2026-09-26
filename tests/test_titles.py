"""头衔 / 积分 / 签到 / 兑换码 的目录与持久化测试。

Run with: `python -m unittest discover tests`
"""

import os
import unittest

import slg_db
import slg_titles


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_every_title_has_a_known_rarity_and_colour(self):
        for t in slg_titles.TITLES:
            self.assertIn(t["rarity"], slg_titles.RARITY_ORDER, t["id"])
            self.assertIn(t["rarity"], slg_titles.RARITY_COLORS, t["id"])

    def test_rarity_order_is_normal_through_mythic(self):
        self.assertEqual(slg_titles.RARITY_ORDER, ("普通", "稀有", "史诗", "传说", "至臻"))

    def test_default_title_is_the_only_normal_obtain(self):
        default = slg_titles.title_by_id(slg_titles.DEFAULT_TITLE_ID)
        self.assertEqual(default["rarity"], "普通")
        self.assertEqual(default["obtain"], "default")

    def test_shop_items_reference_known_titles(self):
        for item in slg_titles.SHOP_ITEMS:
            if item["kind"] == "title":
                self.assertIsNotNone(slg_titles.title_by_id(item["id"]), item["id"])

    def test_fixed_redemption_codes_map_to_known_titles(self):
        for code, title_id in slg_titles.REDEMPTION_CODES.items():
            self.assertIsNotNone(slg_titles.title_by_id(title_id), code)

    def test_group_code_is_stable_within_a_day_and_changes_across_days(self):
        a = slg_titles.group_code("2026-09-22")
        b = slg_titles.group_code("2026-09-22")
        c = slg_titles.group_code("2026-09-23")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_group_code_format(self):
        code = slg_titles.group_code("2026-09-22")
        self.assertTrue(code.startswith("QUNYOU-"), code)
        self.assertEqual(len(code), 13, code)

    def test_available_shop_items_hide_expired_limited_items(self):
        active = slg_titles.available_shop_items("2026-10-30")
        self.assertIn("first_release", [i["id"] for i in active])
        expired = slg_titles.available_shop_items("2026-10-31")
        self.assertNotIn("first_release", [i["id"] for i in expired])

    def test_holiday_titles_are_available_through_october_eighth(self):
        holiday_ids = {"mid_autumn_happy", "national_day_happy"}
        items = {i["id"]: i for i in slg_titles.SHOP_ITEMS}
        for item_id in holiday_ids:
            with self.subTest(item_id=item_id):
                title = slg_titles.title_by_id(item_id)
                self.assertIsNotNone(title)
                self.assertEqual(title["name"], items[item_id]["name"])
                self.assertEqual(items[item_id]["limited_until"], "2026-10-08")
                self.assertTrue(items[item_id]["cloud_only"])
        on_sale = {i["id"] for i in slg_titles.available_shop_items("2026-10-08")}
        after_sale = {i["id"] for i in slg_titles.available_shop_items("2026-10-09")}
        self.assertTrue(holiday_ids <= on_sale)
        self.assertTrue(holiday_ids.isdisjoint(after_sale))

    def test_neon_comment_frame_is_a_fixed_cloud_decoration(self):
        item = slg_titles.shop_item_by_id("neon_comment_frame")
        self.assertIsNotNone(item)
        self.assertEqual(item["kind"], "decoration")
        self.assertEqual(item["category"], "名片框")
        self.assertEqual(item["cost"], 120)
        self.assertTrue(item["cloud_only"])
        self.assertEqual(item["appearance"], "comment_frame")
        self.assertEqual(item["asset"], "neon_comment_frame")
        self.assertEqual(item["asset_source"], "builtin")
        self.assertIn("不支持上传", item["description"])

    def test_lucky_star_is_epic_and_lottery_obtain(self):
        t = slg_titles.title_by_id("lucky_star")
        self.assertIsNotNone(t)
        self.assertEqual(t["rarity"], "史诗")
        self.assertEqual(t["obtain"], "lottery")

    def test_shop_items_have_category_and_subcategory(self):
        for item in slg_titles.SHOP_ITEMS:
            self.assertIn(item.get("category"),
                          ("头衔", "头像框", "名片框", "功能道具"), item["id"])
            self.assertTrue(item.get("subcategory"), item["id"])


class PointsLedger(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_balance_starts_at_zero(self):
        self.assertEqual(slg_db.points_balance(self.conn), 0)

    def test_add_points_accumulates(self):
        slg_db.add_points(self.conn, 10, "签到")
        slg_db.add_points(self.conn, 5, "签到")
        self.assertEqual(slg_db.points_balance(self.conn), 15)

    def test_negative_delta_reduces_balance(self):
        slg_db.add_points(self.conn, 30, "签到")
        slg_db.add_points(self.conn, -30, "兑换:first_release")
        self.assertEqual(slg_db.points_balance(self.conn), 0)


class Signin(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_signin_awards_points_once_per_day(self):
        already, day, gained, bonus = slg_titles.signin(self.conn, "2026-09-22")
        self.assertFalse(already)
        self.assertEqual(gained, slg_titles.DAILY_SIGNIN_POINTS)
        self.assertEqual(bonus, 0, "第一天不该触发累签奖励")
        self.assertEqual(slg_db.points_balance(self.conn), slg_titles.DAILY_SIGNIN_POINTS)

        already, day, gained, bonus = slg_titles.signin(self.conn, "2026-09-22")
        self.assertTrue(already)
        self.assertEqual(gained, 0)
        self.assertEqual(slg_db.points_balance(self.conn), slg_titles.DAILY_SIGNIN_POINTS)

    def test_signin_on_a_new_day_awards_again(self):
        slg_titles.signin(self.conn, "2026-09-22")
        already, _day, gained, bonus = slg_titles.signin(self.conn, "2026-09-23")
        self.assertFalse(already)
        self.assertEqual(gained, slg_titles.DAILY_SIGNIN_POINTS)
        self.assertEqual(bonus, 0)
        self.assertEqual(slg_db.points_balance(self.conn), 2 * slg_titles.DAILY_SIGNIN_POINTS)

    def test_last_signin_day_tracks_latest(self):
        self.assertIsNone(slg_db.last_signin_day(self.conn))
        slg_titles.signin(self.conn, "2026-09-22")
        slg_titles.signin(self.conn, "2026-09-23")
        self.assertEqual(slg_db.last_signin_day(self.conn), "2026-09-23")


class Milestones(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_milestone_awards_15_at_5_days(self):
        bonus_total = 0
        for d in range(1, 6):
            already, _day, _gained, bonus = slg_titles.signin(
                self.conn, "2026-09-%02d" % d)
            self.assertFalse(already)
            bonus_total += bonus
        self.assertEqual(bonus_total, 15)
        self.assertEqual(slg_titles.claimed_milestone(self.conn, "2026-09-30"), 5)

    def test_milestone_tiers_award_once_each(self):
        for d in range(1, 21):
            slg_titles.signin(self.conn, "2026-09-%02d" % d)
        self.assertEqual(slg_titles.claimed_milestone(self.conn, "2026-09-30"), 20)
        # 20 天签到 = 200 分 + 累签 15+30+50 = 95 分。
        self.assertEqual(slg_db.points_balance(self.conn), 295)
        # 重复签同一日不再补发任何奖励。
        already, _d, gained, bonus = slg_titles.signin(self.conn, "2026-09-20")
        self.assertTrue(already)
        self.assertEqual(bonus, 0)


class MakeupCard(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_makeup_is_removed_from_shelf_but_direct_calendar_makeup_remains(self):
        item = slg_titles.shop_item_by_id("makeup_card")
        self.assertIsNone(item)
        self.assertNotIn("makeup_card", {
            i["id"] for i in slg_titles.available_shop_items("2026-09-22")})
        self.assertEqual(slg_titles.MAKEUP_CARD_COST, 5)

    def test_backfills_most_recent_missed_day(self):
        for d in (1, 2, 3):
            slg_titles.signin(self.conn, "2026-09-%02d" % d)
        slg_db.add_points(self.conn, 100, "测试")
        ok, msg, bonus = slg_titles.buy_makeup_card(self.conn, "2026-09-06")
        self.assertTrue(ok, msg)
        self.assertEqual(bonus, 0, "只补 1 天，未到 5 天档")
        self.assertIn(5, slg_db.signin_month_days(self.conn, 2026, 9),
                      "应补签最近的漏签日 5")
        self.assertEqual(slg_db.points_balance(self.conn), 125, "100 + 30 - 5")

    def test_rejected_when_no_missed_day(self):
        for d in range(1, 5):
            slg_titles.signin(self.conn, "2026-09-%02d" % d)
        slg_db.add_points(self.conn, 100, "测试")
        ok, _msg, _bonus = slg_titles.buy_makeup_card(self.conn, "2026-09-05")
        self.assertFalse(ok)
        self.assertEqual(slg_db.points_balance(self.conn), 140, "没漏签不该扣分")

    def test_makeup_can_trigger_milestone(self):
        for d in (1, 2, 3, 4):
            slg_titles.signin(self.conn, "2026-09-%02d" % d)
        self.assertEqual(slg_titles.claimed_milestone(self.conn, "2026-09-06"), 0)
        slg_db.add_points(self.conn, 100, "测试")
        ok, msg, bonus = slg_titles.buy_makeup_card(self.conn, "2026-09-06")
        self.assertTrue(ok, msg)
        self.assertEqual(bonus, 15, "补签凑满 5 天应触发累签奖励")
        self.assertEqual(slg_titles.claimed_milestone(self.conn, "2026-09-06"), 5)


class Buying(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_buy_title_deducts_and_owns(self):
        slg_db.add_points(self.conn, 300, "签到")
        self.assertTrue(slg_db.buy_title(self.conn, "senior_user", 300))
        self.assertEqual(slg_db.points_balance(self.conn), 0)
        self.assertIn("senior_user", slg_db.owned_title_ids(self.conn))

    def test_buy_title_insufficient_balance(self):
        slg_db.add_points(self.conn, 10, "签到")
        self.assertFalse(slg_db.buy_title(self.conn, "senior_user", 300))
        self.assertEqual(slg_db.points_balance(self.conn), 10)
        self.assertNotIn("senior_user", slg_db.owned_title_ids(self.conn))

    def test_locked_shop_item_cannot_be_bought(self):
        rename_card = slg_titles.shop_item_by_id("rename_card")
        self.assertTrue(rename_card["locked"])
        slg_db.add_points(self.conn, 1000, "签到")
        self.assertFalse(slg_titles.buy(self.conn, rename_card))
        self.assertEqual(slg_db.points_balance(self.conn), 1000)

    def test_cloud_only_decoration_cannot_be_bought_into_local_inventory(self):
        item = slg_titles.shop_item_by_id("neon_comment_frame")
        slg_db.add_points(self.conn, item["cost"], "签到")
        self.assertFalse(slg_titles.buy(self.conn, item))
        self.assertEqual(slg_db.points_balance(self.conn), item["cost"])
        self.assertNotIn(item["id"], slg_db.owned_title_ids(self.conn))


class Redemption(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_todays_group_code_owns_title(self):
        ok, _msg = slg_titles.redeem(self.conn, slg_titles.group_code())
        self.assertTrue(ok)
        self.assertIn("group_friend", slg_db.owned_title_ids(self.conn))

    def test_yesterdays_group_code_is_gracefully_accepted(self):
        from datetime import date, timedelta
        yesterday = slg_titles.group_code(
            (date.today() - timedelta(days=1)).isoformat())
        ok, _msg = slg_titles.redeem(self.conn, yesterday)
        self.assertTrue(ok)

    def test_group_code_is_case_insensitive(self):
        ok, _msg = slg_titles.redeem(self.conn, slg_titles.group_code().lower())
        self.assertTrue(ok)

    def test_invalid_code_rejected(self):
        ok, msg = slg_titles.redeem(self.conn, "NOPE")
        self.assertFalse(ok)
        self.assertEqual(slg_db.owned_title_ids(self.conn), set())

    def test_redeem_twice_is_not_newly_owned(self):
        code = slg_titles.group_code()
        slg_titles.redeem(self.conn, code)
        ok, _msg = slg_titles.redeem(self.conn, code)
        self.assertFalse(ok)


class Equip(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_equipped_title_round_trip(self):
        self.assertEqual(slg_db.get_equipped_title(self.conn), "")
        slg_db.set_equipped_title(self.conn, "senior_user")
        self.assertEqual(slg_db.get_equipped_title(self.conn), "senior_user")
        slg_db.set_equipped_title(self.conn, "")
        self.assertEqual(slg_db.get_equipped_title(self.conn), "")


class PrivateTables(unittest.TestCase):
    def test_new_tables_are_marked_private(self):
        for table in ("points_log", "signin", "owned_titles"):
            self.assertIn(table, slg_db._PRIVATE_TABLES, table)

    def test_new_tables_are_backed_up(self):
        backed = [name for name, _cols in slg_db._BACKUP_TABLES]
        for table in ("points_log", "signin", "owned_titles"):
            self.assertIn(table, backed, table)


class DevPrivileges(unittest.TestCase):
    """开发者特权：密钥只从本机文件/环境变量读取，测试注入假密钥，不落真实密钥。

    真实密钥绝不写入本文件或任何会被 git 跟踪的文件。
    """

    FAKE = "FAKEDEVKEY"

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self._old = os.environ.get("SLGKING_DEV_SECRET")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SLGKING_DEV_SECRET", None)
        else:
            os.environ["SLGKING_DEV_SECRET"] = self._old

    def test_butter_king_is_zhizhen_and_code_obtain(self):
        t = slg_titles.title_by_id("butter_king")
        self.assertIsNotNone(t)
        self.assertEqual(t["rarity"], "至臻")
        self.assertEqual(t["obtain"], "code")

    def test_dev_code_owns_butter_king_and_unlocks_dev(self):
        os.environ["SLGKING_DEV_SECRET"] = self.FAKE
        ok, _msg = slg_titles.redeem(self.conn, self.FAKE)
        self.assertTrue(ok)
        self.assertIn("butter_king", slg_db.owned_title_ids(self.conn))
        self.assertTrue(slg_titles.dev_unlocked(self.conn))

    def test_dev_signin_is_unlimited(self):
        os.environ["SLGKING_DEV_SECRET"] = self.FAKE
        slg_titles.redeem(self.conn, self.FAKE)
        for _ in range(3):
            already, _day, gained, bonus = slg_titles.signin(self.conn, "2026-09-22")
            self.assertFalse(already)
            self.assertEqual(gained, slg_titles.DAILY_SIGNIN_POINTS)
            self.assertEqual(bonus, 0, "开发者签到不走累签奖励")
        self.assertEqual(slg_db.points_balance(self.conn),
                         3 * slg_titles.DAILY_SIGNIN_POINTS)

    def test_dev_unlocked_is_false_by_default(self):
        self.assertFalse(slg_titles.dev_unlocked(self.conn))

    def test_dev_lottery_is_unlimited_but_still_charges_each_draw(self):
        os.environ["SLGKING_DEV_SECRET"] = self.FAKE
        slg_titles.redeem(self.conn, self.FAKE)
        slg_db.add_points(self.conn, 100, "签到")
        for _ in range(3):
            ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
            self.assertTrue(ok, "开发者抽第二次被当日限制挡住了")
            self.assertEqual(prize["kind"], "points")
        # 不限次数不等于免费：每次扣 5 成本、回 1 分，净 -4。
        self.assertEqual(slg_db.points_balance(self.conn), 100 - 3 * 4)

    def test_shop_items_all_have_a_category(self):
        for item in slg_titles.SHOP_ITEMS:
            self.assertTrue(item.get("category"), item["id"])

    def test_unlock_all_titles_owns_every_non_default_title(self):
        gained = slg_titles.unlock_all_titles(self.conn)
        owned = slg_db.owned_title_ids(self.conn)
        expected = {t["id"] for t in slg_titles.TITLES
                    if t["obtain"] != "default"}
        self.assertEqual(owned, expected)
        self.assertEqual(gained, len(expected))

    def test_unlock_all_titles_is_idempotent(self):
        slg_titles.unlock_all_titles(self.conn)
        self.assertEqual(slg_titles.unlock_all_titles(self.conn), 0)


class _FakeRng:
    """固定摇出的点数。randint(1, LOTTERY_WEIGHT_TOTAL) 的结果就是它。

    刻度是万分之一，所以下面这些 roll 值各落在哪一档，写死成名字比在用例里
    散落一堆魔数好读 —— 改权重表时只需要动这里。
    """

    def __init__(self, n):
        self.n = n

    def randint(self, a, b):
        return self.n


ROLL_JACKPOT = 1       # 幸运之王 0.01%
ROLL_GRAND = 2         # 幸运星 1%
ROLL_ZERO = 102        # 谢谢参与
ROLL_ONE = 3101        # 1 积分
ROLL_TWO = 5101        # 2 积分


class Lottery(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _fund(self, points=100):
        slg_db.add_points(self.conn, points, "签到")

    def test_draw_deducts_cost_and_records_day(self):
        self._fund()
        ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        self.assertTrue(ok)
        self.assertEqual(prize["kind"], "points")
        self.assertEqual(slg_db.points_balance(self.conn), 100 - 5 + prize["value"])
        self.assertEqual(slg_titles.last_lottery_day(self.conn),
                         slg_titles.today_str())

    def test_draw_limit_is_three_per_day(self):
        self._fund()
        for _ in range(slg_titles.LOTTERY_DAILY_LIMIT):
            ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
            self.assertTrue(ok)
            self.assertEqual(prize["kind"], "points")
        ok, msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        self.assertFalse(ok)
        self.assertIsNone(prize)

    def test_draw_insufficient_points(self):
        slg_db.add_points(self.conn, 3, "签到")
        ok, msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        self.assertFalse(ok)
        self.assertIsNone(prize)
        self.assertEqual(slg_db.points_balance(self.conn), 3)

    def test_grand_prize_owns_lucky_star(self):
        self._fund()
        ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        self.assertTrue(ok)
        self.assertEqual(prize, {"kind": "title", "value": "lucky_star"})
        self.assertIn("lucky_star", slg_db.owned_title_ids(self.conn))
        # 大奖不返积分：只扣了 5 分成本。
        self.assertEqual(slg_db.points_balance(self.conn), 95)

    def test_prize_weights_sum_to_the_declared_total(self):
        total = sum(w for w, _k, _v in slg_titles.LOTTERY_PRIZES)
        self.assertEqual(total, slg_titles.LOTTERY_WEIGHT_TOTAL)

    def test_points_tiers_stay_below_the_draw_cost(self):
        low = sum(w for w, k, v in slg_titles.LOTTERY_PRIZES
                  if k == "points" and v < slg_titles.LOTTERY_COST)
        self.assertGreater(low, slg_titles.LOTTERY_WEIGHT_TOTAL * 0.8)


class JackpotTier(unittest.TestCase):
    """0.01% 的幸运之王：权重表里有它、保底够不着它、滚轮和概率表都认得它。

    开发者权限在这里只是为了让同一天能连着抽 —— 当日限一次会把第二抽挡掉。
    """

    FAKE = "FAKEDEVKEY"

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self._old = os.environ.get("SLGKING_DEV_SECRET")
        os.environ["SLGKING_DEV_SECRET"] = self.FAKE
        self.addCleanup(self._restore_env)
        slg_titles.redeem(self.conn, self.FAKE)
        slg_db.add_points(self.conn, 1000, "测试")

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("SLGKING_DEV_SECRET", None)
        else:
            os.environ["SLGKING_DEV_SECRET"] = self._old

    def test_the_odds_are_one_part_in_ten_thousand(self):
        weights = {v: w for w, _k, v in slg_titles.LOTTERY_PRIZES}
        self.assertEqual(slg_titles.LOTTERY_WEIGHT_TOTAL, 10000)
        self.assertEqual(weights[slg_titles.LOTTERY_JACKPOT_TITLE], 1)

    def test_it_is_legendary_and_lottery_only(self):
        t = slg_titles.title_by_id(slg_titles.LOTTERY_JACKPOT_TITLE)
        self.assertIsNotNone(t)
        self.assertEqual(t["rarity"], "传说")
        self.assertEqual(t["obtain"], "lottery")

    def test_the_lowest_roll_wins_it(self):
        ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_JACKPOT))
        self.assertTrue(ok)
        self.assertEqual(prize, {"kind": "title",
                                 "value": slg_titles.LOTTERY_JACKPOT_TITLE})
        self.assertIn(slg_titles.LOTTERY_JACKPOT_TITLE,
                      slg_db.owned_title_ids(self.conn))

    def test_the_pity_does_not_hand_it_out(self):
        # 保底只保 777：不然 0.01% 就是一张「一百抽必得」的假彩票。
        slg_db.set_pref(self.conn, "lottery.pity",
                        str(slg_titles.LOTTERY_PITY - 1))
        _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        self.assertEqual(prize["value"], slg_titles.LOTTERY_GRAND_TITLE)

    def test_a_second_crown_pays_the_special_rate(self):
        slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_JACKPOT))
        _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_JACKPOT))
        self.assertTrue(prize.get("duplicate"))
        self.assertEqual(prize["points"],
                         slg_titles.LOTTERY_DUPLICATE_BY_TITLE[
                             slg_titles.LOTTERY_JACKPOT_TITLE])
        self.assertGreater(prize["points"], slg_titles.LOTTERY_DUPLICATE_POINTS,
                           "皇冠的折算不该跟普通大奖一个价")

    def test_the_reels_and_the_paytable_agree_on_the_crown(self):
        self.assertEqual(
            slg_titles.symbols_for_prize(
                {"kind": "title", "value": slg_titles.LOTTERY_JACKPOT_TITLE}),
            ("crown", "crown", "crown"))
        rows = [r for r in slg_titles.lottery_paytable()
                if r[0] == ("crown", "crown", "crown")]
        self.assertEqual(len(rows), 1)
        self.assertIn("幸运之王", rows[0][1])
        self.assertAlmostEqual(rows[0][2], 0.01, places=6)

    def test_the_crown_never_spins_by_chance(self):
        # 皇冠只在中奖时落定。混进随机符号表 = 白送一个出场率，也毁掉「转出来
        # 一样、奖不一样」这条线。
        self.assertNotIn("crown", slg_titles.SLOT_SYMBOLS)

    def test_every_symbol_in_the_tables_has_an_image(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in slg_titles.SYMBOL_NAMES:
            path = os.path.join(root, "assets", "slot", "%s.png" % name)
            self.assertTrue(os.path.exists(path), path)


class MakeupByDay(unittest.TestCase):
    """点名补签：补的是你点的那一天，不是「最近一个漏签日」。

    以前只能盲补最近一天 —— 日历上看到中间漏了哪天也够不着。
    """

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _sign(self, days, day="2026-09-10"):
        for d in days:
            slg_titles.signin(self.conn, "2026-09-%02d" % d)
        slg_db.add_points(self.conn, 100, "测试")
        return day

    def test_backfills_the_named_day_not_the_latest_miss(self):
        day = self._sign((1, 2, 3, 5, 6, 7))
        ok, msg, _bonus = slg_titles.buy_makeup_card(self.conn, day,
                                                     target="2026-09-08")
        self.assertTrue(ok, msg)
        signed = slg_db.signin_month_days(self.conn, 2026, 9)
        self.assertIn(8, signed)
        self.assertNotIn(9, signed, "补的是最近漏签日，不是点名的那天")

    def test_a_named_day_can_still_trigger_the_milestone(self):
        day = self._sign((1, 2, 3, 5))
        ok, msg, bonus = slg_titles.buy_makeup_card(self.conn, day,
                                                    target="2026-09-04")
        self.assertTrue(ok, msg)
        self.assertEqual(bonus, 15, "点名补签凑满 5 天没触发累签奖励")

    def test_rejects_a_day_outside_this_month(self):
        day = self._sign(())
        ok, msg, _bonus = slg_titles.buy_makeup_card(self.conn, day,
                                                     target="2026-08-03")
        self.assertFalse(ok)
        self.assertIn("本月", msg)
        self.assertEqual(slg_db.points_balance(self.conn), 100, "被拒还扣了分")

    def test_rejects_today_and_the_future(self):
        day = self._sign(())
        for target in ("2026-09-10", "2026-09-11"):
            ok, _msg, _bonus = slg_titles.buy_makeup_card(self.conn, day,
                                                          target=target)
            self.assertFalse(ok, target)
        self.assertEqual(slg_db.points_balance(self.conn), 100)

    def test_rejects_a_day_that_is_already_signed(self):
        day = self._sign((1, 2, 3))
        before = slg_db.points_balance(self.conn)
        ok, msg, _bonus = slg_titles.buy_makeup_card(self.conn, day,
                                                     target="2026-09-02")
        self.assertFalse(ok)
        self.assertIn("已经签过", msg)
        self.assertEqual(slg_db.points_balance(self.conn), before)

    def test_rejects_a_malformed_date(self):
        day = self._sign(())
        ok, _msg, _bonus = slg_titles.buy_makeup_card(self.conn, day,
                                                      target="NOPE")
        self.assertFalse(ok)
        self.assertEqual(slg_db.points_balance(self.conn), 100)

    def test_makeup_problem_is_none_for_a_day_you_can_still_fill(self):
        self.assertIsNone(
            slg_titles.makeup_problem(self.conn, "2026-09-05", "2026-09-06"))

    def test_makeup_problem_explains_a_refusal(self):
        self.assertEqual(
            slg_titles.makeup_problem(self.conn, "2026-09-06", "2026-09-06"),
            "只能补今天之前的日子")


class LotteryPity(unittest.TestCase):
    """保底 100 抽 + 重复大奖折算积分 + 抽奖流水。

    开发模式在这里只是为了让同一天能连着抽（当日限一次会把第二次挡掉）。
    """

    FAKE = "FAKEDEVKEY"

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self._old = os.environ.get("SLGKING_DEV_SECRET")
        os.environ["SLGKING_DEV_SECRET"] = self.FAKE
        self.addCleanup(self._restore_env)
        slg_titles.redeem(self.conn, self.FAKE)
        slg_db.add_points(self.conn, 1000, "测试")

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("SLGKING_DEV_SECRET", None)
        else:
            os.environ["SLGKING_DEV_SECRET"] = self._old

    def test_the_pity_and_the_duplicate_payout_are_what_was_asked_for(self):
        self.assertEqual(slg_titles.LOTTERY_PITY, 100)
        self.assertEqual(slg_titles.LOTTERY_DUPLICATE_POINTS, 100)

    def test_the_hundredth_draw_forces_the_grand_title(self):
        # 保底路径不看点数，ROLL_ONE 只是提醒「这一抽本来不中头衔」。
        slg_db.set_pref(self.conn, "lottery.pity",
                        str(slg_titles.LOTTERY_PITY - 1))
        ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        self.assertTrue(ok)
        self.assertEqual(prize["kind"], "title")
        self.assertEqual(prize["value"], slg_titles.LOTTERY_GRAND_TITLE)
        self.assertIn(slg_titles.LOTTERY_GRAND_TITLE,
                      slg_db.owned_title_ids(self.conn))

    def test_one_draw_short_of_the_pity_does_not_force_anything(self):
        slg_db.set_pref(self.conn, "lottery.pity",
                        str(slg_titles.LOTTERY_PITY - 2))
        _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        self.assertEqual(prize["kind"], "points")
        self.assertEqual(slg_titles.lottery_pity(self.conn),
                         slg_titles.LOTTERY_PITY - 1)

    def test_a_title_win_clears_the_pity(self):
        slg_db.set_pref(self.conn, "lottery.pity", "42")
        _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        self.assertEqual(prize["kind"], "title")
        self.assertEqual(slg_titles.lottery_pity(self.conn), 0)

    def test_a_points_win_advances_the_pity(self):
        slg_db.set_pref(self.conn, "lottery.pity", "7")
        slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        self.assertEqual(slg_titles.lottery_pity(self.conn), 8)

    def test_pity_starts_at_zero(self):
        self.assertEqual(slg_titles.lottery_pity(self.conn), 0)

    def test_a_second_grand_title_pays_points_instead_of_reowning_it(self):
        slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        self.assertIn(slg_titles.LOTTERY_GRAND_TITLE,
                      slg_db.owned_title_ids(self.conn))
        before = slg_db.points_balance(self.conn)
        ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        self.assertTrue(ok)
        self.assertEqual(prize["kind"], "title")
        self.assertTrue(prize.get("duplicate"), "重复大奖没标成 duplicate")
        self.assertEqual(prize["points"], slg_titles.LOTTERY_DUPLICATE_POINTS)
        self.assertEqual(
            slg_db.points_balance(self.conn),
            before - slg_titles.LOTTERY_COST + slg_titles.LOTTERY_DUPLICATE_POINTS)
        # 重复掉落不重复入库，也不该把保底吞掉。
        self.assertEqual(slg_titles.lottery_pity(self.conn), 0)

    def test_the_duplicate_prize_reads_as_a_payout(self):
        slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        text = slg_titles.lottery_prize_text(prize)
        self.assertIn("重复获得", text)
        self.assertIn(str(slg_titles.LOTTERY_DUPLICATE_POINTS), text)

    def test_the_duplicate_keeps_the_kind_so_the_reels_still_land_on_777(self):
        # 滚轮站位查的是奖品的 value（头衔是 id）；重复奖品若把 value 抹掉，
        # _lottery_finals 就查不到符号表，只能摆出一组落空图案。
        slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        self.assertEqual(prize["kind"], "title")
        self.assertEqual(
            slg_titles.symbols_for_prize(prize), ("seven", "seven", "seven"))

    def test_history_records_draws_newest_first(self):
        values = []
        for roll in (ROLL_JACKPOT, ROLL_GRAND, ROLL_ONE):
            _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(roll))
            values.append(prize["value"])
        rows = slg_titles.lottery_history(self.conn)
        self.assertEqual([r["value"] for r in rows], list(reversed(values)))
        self.assertEqual({r["day"] for r in rows}, {slg_titles.today_str()})

    def test_history_records_the_points_each_draw_actually_paid(self):
        _ok, _msg, prize = slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        row = slg_titles.lottery_history(self.conn)[0]
        self.assertEqual(row["points"], prize["value"])

    def test_a_duplicate_history_row_carries_the_payout(self):
        slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_GRAND))
        row = slg_titles.lottery_history(self.conn)[0]
        self.assertEqual(row["points"], slg_titles.LOTTERY_DUPLICATE_POINTS)
        self.assertIn("重复获得", slg_titles.lottery_prize_text(row))

    def test_history_keeps_only_the_newest_batch(self):
        for _ in range(slg_titles.LOTTERY_HISTORY_KEEP + 5):
            slg_titles.draw_lottery(self.conn, _FakeRng(ROLL_ONE))
        rows = slg_titles.lottery_history(self.conn)
        self.assertEqual(len(rows), slg_titles.LOTTERY_HISTORY_KEEP)

    def test_history_survives_a_corrupt_pref(self):
        slg_db.set_pref(self.conn, "lottery.history", "{not json")
        self.assertEqual(slg_titles.lottery_history(self.conn), [])

    def test_history_defaults_to_empty(self):
        self.assertEqual(slg_titles.lottery_history(self.conn), [])


class Compensation(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_first_launch_grants_major(self):
        amount = slg_titles.apply_update_compensation(self.conn, "0.22.0")
        self.assertEqual(amount, slg_titles.COMPENSATION_MAJOR)
        self.assertEqual(slg_db.points_balance(self.conn),
                         slg_titles.COMPENSATION_MAJOR)
        self.assertEqual(
            slg_db.get_pref(self.conn, slg_titles.COMPENSATION_PREF), "0.22.0")

    def test_same_version_grants_nothing(self):
        slg_titles.apply_update_compensation(self.conn, "0.22.0")
        self.assertEqual(
            slg_titles.apply_update_compensation(self.conn, "0.22.0"), 0)
        self.assertEqual(slg_db.points_balance(self.conn),
                         slg_titles.COMPENSATION_MAJOR)

    def test_patch_bump_grants_minor(self):
        slg_titles.apply_update_compensation(self.conn, "0.22.0")
        self.assertEqual(
            slg_titles.apply_update_compensation(self.conn, "0.22.1"),
            slg_titles.COMPENSATION_MINOR)

    def test_older_test_build_grants_nothing_and_keeps_rewarded_version(self):
        slg_titles.apply_update_compensation(self.conn, "0.23.5")
        before = slg_db.points_balance(self.conn)
        self.assertEqual(
            slg_titles.apply_update_compensation(self.conn, "0.22.6"), 0)
        self.assertEqual(slg_db.points_balance(self.conn), before)
        self.assertEqual(
            slg_db.get_pref(self.conn, slg_titles.COMPENSATION_PREF), "0.23.5")

    def test_minor_bump_grants_major(self):
        slg_titles.apply_update_compensation(self.conn, "0.22.0")
        self.assertEqual(
            slg_titles.apply_update_compensation(self.conn, "0.23.0"),
            slg_titles.COMPENSATION_MAJOR)

    def test_major_bump_grants_major(self):
        slg_titles.apply_update_compensation(self.conn, "0.22.0")
        self.assertEqual(
            slg_titles.apply_update_compensation(self.conn, "1.0.0"),
            slg_titles.COMPENSATION_MAJOR)


class Achievements(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _seed_collection(self, n):
        cid = slg_db.create_collection(self.conn, "test")
        for i in range(n):
            gid, _ = slg_db.upsert_game(self.conn, slug="g%d" % i,
                                        url="https://x/g%d" % i, title="g%d" % i)
            self.conn.execute(
                "INSERT OR IGNORE INTO collection_items"
                " (collection_id, game_id, added_at) VALUES (?,?,datetime('now'))",
                (cid, gid))
        self.conn.commit()

    def test_collector_unlocks_at_threshold(self):
        self._seed_collection(slg_titles.COLLECTOR_NEED)
        self.assertEqual(slg_titles.grant_achievements(self.conn), ["collector"])
        self.assertIn("collector", slg_db.owned_title_ids(self.conn))

    def test_collector_not_before_threshold(self):
        self._seed_collection(slg_titles.COLLECTOR_NEED - 1)
        self.assertEqual(slg_titles.grant_achievements(self.conn), [])
        self.assertNotIn("collector", slg_db.owned_title_ids(self.conn))

    def test_collector_is_idempotent(self):
        self._seed_collection(slg_titles.COLLECTOR_NEED)
        slg_titles.grant_achievements(self.conn)
        self.assertEqual(slg_titles.grant_achievements(self.conn), [])
        self.assertIn("collector", slg_db.owned_title_ids(self.conn))

    def test_connoisseur_unlocks_at_public_comment_threshold(self):
        self.assertEqual(
            slg_titles.grant_achievements(self.conn, slg_titles.CONNOISSEUR_NEED),
            ["connoisseur"])
        self.assertIn("connoisseur", slg_db.owned_title_ids(self.conn))

    def test_connoisseur_not_below_threshold(self):
        self.assertEqual(
            slg_titles.grant_achievements(
                self.conn, slg_titles.CONNOISSEUR_NEED - 1), [])
        self.assertNotIn("connoisseur", slg_db.owned_title_ids(self.conn))

    def test_connoisseur_skipped_when_count_unknown(self):
        self.assertEqual(slg_titles.grant_achievements(self.conn, None), [])
        self.assertNotIn("connoisseur", slg_db.owned_title_ids(self.conn))

    def test_achievement_titles_listed_in_catalogue(self):
        self.assertEqual(slg_titles.title_by_id("collector")["rarity"], "稀有")
        self.assertEqual(slg_titles.title_by_id("connoisseur")["rarity"], "史诗")
        names = {t["name"] for t in slg_titles.TITLES}
        self.assertIn("收藏家", names)
        self.assertIn("鉴赏家", names)


if __name__ == "__main__":
    unittest.main()
