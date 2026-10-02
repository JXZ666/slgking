"""Fixed local categories: filtering, upgrades, and atomic backup restore."""

import copy
import sqlite3
import unittest

import slg_db
from slg_game_categories import normalize_categories, format_categories


class GameCategories(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_normalization_and_display(self):
        self.assertEqual(normalize_categories(["slg", "galgame", "slg"]),
                         ("galgame", "slg"))
        self.assertEqual(format_categories(("slg", "galgame")), "Galgame · SLG")
        self.assertEqual(format_categories(()), "未分类")
        for invalid in (None, "slg", ["unknown"], [1], [{}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_categories(invalid)

    def test_edit_preserves_or_clears_categories(self):
        gid = slg_db.add_user_game(self.conn, "Game", engine="Unity", tags=["tag"],
                                   categories=("slg", "galgame"))
        slg_db.update_user_game(self.conn, gid, "Edited", engine="RenPy", tags=["tag"])
        self.assertEqual(slg_db.game_categories(self.conn, gid), ("galgame", "slg"))
        slg_db.update_user_game(self.conn, gid, "Edited", categories=("rpg",))
        self.assertEqual(slg_db.game_categories(self.conn, gid), ("rpg",))
        slg_db.update_user_game(self.conn, gid, "Edited", categories=())
        self.assertEqual(slg_db.game_categories(self.conn, gid), ())

    def test_invalid_add_and_edit_leave_no_changes(self):
        with self.assertRaises(ValueError):
            slg_db.add_user_game(self.conn, "Invalid", categories=("bad",))
        self.assertEqual(slg_db.find_games(self.conn, origin="user"), [])
        gid = slg_db.add_user_game(self.conn, "Original", categories=("slg",))
        with self.assertRaises(ValueError):
            slg_db.update_user_game(self.conn, gid, "Invalid", categories=("bad",))
        self.assertEqual(slg_db.get_game(self.conn, gid)["title"], "Original")
        self.assertEqual(slg_db.game_categories(self.conn, gid), ("slg",))

    def test_filter_union_uncategorized_and_tag_intersection(self):
        both = slg_db.add_user_game(self.conn, "Both", categories=("galgame", "slg"),
                                    tags=["a", "b"])
        only = slg_db.add_user_game(self.conn, "Only", categories=("slg",), tags=["a"])
        none = slg_db.add_user_game(self.conn, "None")
        other = slg_db.add_user_game(self.conn, "Other", categories=("rpg",))
        rows = slg_db.find_games(self.conn, origin="user", category_ids=("slg", "galgame"))
        self.assertEqual({row["id"] for row in rows}, {both, only})
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(row for row in rows if row["id"] == both)["categories"],
                         ("galgame", "slg"))
        rows = slg_db.find_games(self.conn, origin="user", category_ids=("slg",),
                                 include=("a", "b"))
        self.assertEqual([row["id"] for row in rows], [both])
        rows = slg_db.find_games(self.conn, origin="user", uncategorized=True)
        self.assertEqual([row["id"] for row in rows], [none])
        rows = slg_db.find_games(self.conn, origin="user", category_ids=("slg",),
                                 uncategorized=True)
        self.assertEqual({row["id"] for row in rows}, {both, only, none})
        # Stale local-view filters must not hide main-catalogue/promoted games.
        slg_db.promote_game(self.conn, other, True)
        self.assertEqual([row["id"] for row in slg_db.find_games(
            self.conn, category_ids=("galgame",), uncategorized=True)], [other])

    def test_categories_bulk_uses_one_query(self):
        ids = [slg_db.add_user_game(self.conn, str(i), categories=("slg",))
               for i in range(12)]
        queries = []
        self.conn.set_trace_callback(queries.append)
        result = slg_db.game_categories_bulk(self.conn, ids)
        self.conn.set_trace_callback(None)
        self.assertEqual(len(queries), 1)
        self.assertEqual(result, {gid: ("slg",) for gid in ids})

    def test_site_results_skip_category_read(self):
        slg_db.upsert_game(self.conn, "site", "https://example.org/game", "Site")
        queries = []
        self.conn.set_trace_callback(queries.append)
        rows = slg_db.find_games(self.conn)
        self.conn.set_trace_callback(None)
        self.assertEqual(rows[0]["categories"], ())
        self.assertFalse(any("FROM user_game_categories" in query for query in queries))

    def test_score_sort_preserves_direction_ties_and_categories_do_not_affect_it(self):
        first = slg_db.add_user_game(self.conn, "A", categories=("galgame",))
        second = slg_db.add_user_game(self.conn, "B", categories=("slg",))
        best = slg_db.add_user_game(self.conn, "Best", categories=())
        self.conn.execute("UPDATE games SET rating=5 WHERE id=?", (best,))
        for desc, expected in ((True, [best, first, second]),
                               (False, [first, second, best])):
            rows = slg_db.find_games(self.conn, origin="user", sort="score", desc=desc)
            self.assertEqual([row["id"] for row in rows], expected)
            self.assertTrue(all(row["score"] is not None for row in rows))
            self.assertEqual(next(row["score"] for row in rows if row["id"] == first),
                             next(row["score"] for row in rows if row["id"] == second))

    def test_category_requires_user_and_cascades_on_delete(self):
        site, _ = slg_db.upsert_game(self.conn, "site", "https://example.org/game", "Site")
        with self.assertRaises(ValueError):
            slg_db.set_game_categories(self.conn, site, ("slg",))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO user_game_categories VALUES (?,?)", (site, "slg"))
        gid = slg_db.add_user_game(self.conn, "User", categories=("slg", "rpg"))
        slg_db.delete_game(self.conn, gid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM user_game_categories")
                         .fetchone()[0], 0)

    def test_new_backup_round_trip_and_slug_remapping(self):
        gid = slg_db.add_user_game(self.conn, "Source", categories=("slg", "rpg"))
        data = slg_db.export_user_data(self.conn)
        with slg_db.connect(":memory:") as target:
            slg_db.add_user_game(target, "Existing", categories=("other",))
            slg_db.import_user_data(target, data)
            rows = slg_db.find_games(target, origin="user")
            restored = next(row for row in rows if row["title"] == "Source")
            self.assertNotEqual(restored["id"], gid)
            self.assertEqual(restored["categories"], ("slg", "rpg"))
            self.assertEqual(next(row for row in rows if row["title"] == "Existing")
                             ["categories"], ("other",))

    def test_old_backup_clears_only_restored_games(self):
        gid = slg_db.add_user_game(self.conn, "Restored", categories=("slg",))
        data = slg_db.export_user_data(self.conn)
        del data["user_game_categories"]
        keep = slg_db.add_user_game(self.conn, "Keep", categories=("other",))
        slg_db.import_user_data(self.conn, data)
        self.assertEqual(slg_db.game_categories(self.conn, gid), ())
        self.assertEqual(slg_db.game_categories(self.conn, keep), ("other",))

    def test_invalid_backup_rejected_before_mutations(self):
        slg_db.add_user_game(self.conn, "Keep", categories=("slg",))
        before = slg_db.export_user_data(self.conn)
        for invalid in ("bad", 1, None):
            data = copy.deepcopy(before)
            data["user_game_categories"][0]["category"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                slg_db.import_user_data(self.conn, data)
            self.assertEqual(slg_db.export_user_data(self.conn), before)

    def test_sql_error_rolls_back_categories_and_game_edit(self):
        slg_db.add_user_game(self.conn, "Keep", categories=("slg",))
        self.conn.commit()
        before = slg_db.export_user_data(self.conn)
        data = copy.deepcopy(before)
        data["user_games"][0]["title"] = "Changed"
        data["user_game_categories"][0]["category"] = "rpg"
        self.conn.execute(
            "CREATE TRIGGER fail_categories BEFORE INSERT ON user_game_categories"
            " WHEN NEW.category='rpg' BEGIN SELECT RAISE(ABORT, 'forced failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            slg_db.import_user_data(self.conn, data)
        self.assertEqual(slg_db.export_user_data(self.conn), before)

    def test_existing_database_upgrade_needs_no_data_rewrite(self):
        gid = slg_db.add_user_game(self.conn, "Old", engine="RenPy", tags=["tag"])
        before = dict(slg_db.get_game(self.conn, gid))
        self.conn.execute("DROP TABLE user_game_categories")
        self.conn.executescript(slg_db.SCHEMA)
        slg_db._migrate(self.conn)
        self.assertEqual(dict(slg_db.get_game(self.conn, gid)), before)
        self.assertEqual(slg_db.game_categories(self.conn, gid), ())


if __name__ == "__main__":
    unittest.main()
