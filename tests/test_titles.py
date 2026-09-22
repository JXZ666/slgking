"""头衔 / 积分 / 签到 / 兑换码 的目录与持久化测试。

Run with: `python -m unittest discover tests`
"""

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
        already, day, gained = slg_titles.signin(self.conn, "2026-09-22")
        self.assertFalse(already)
        self.assertEqual(gained, slg_titles.DAILY_SIGNIN_POINTS)
        self.assertEqual(slg_db.points_balance(self.conn), slg_titles.DAILY_SIGNIN_POINTS)

        already, day, gained = slg_titles.signin(self.conn, "2026-09-22")
        self.assertTrue(already)
        self.assertEqual(gained, 0)
        self.assertEqual(slg_db.points_balance(self.conn), slg_titles.DAILY_SIGNIN_POINTS)

    def test_signin_on_a_new_day_awards_again(self):
        slg_titles.signin(self.conn, "2026-09-22")
        already, _day, gained = slg_titles.signin(self.conn, "2026-09-23")
        self.assertFalse(already)
        self.assertEqual(gained, slg_titles.DAILY_SIGNIN_POINTS)
        self.assertEqual(slg_db.points_balance(self.conn), 2 * slg_titles.DAILY_SIGNIN_POINTS)

    def test_last_signin_day_tracks_latest(self):
        self.assertIsNone(slg_db.last_signin_day(self.conn))
        slg_titles.signin(self.conn, "2026-09-22")
        slg_titles.signin(self.conn, "2026-09-23")
        self.assertEqual(slg_db.last_signin_day(self.conn), "2026-09-23")


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


if __name__ == "__main__":
    unittest.main()
