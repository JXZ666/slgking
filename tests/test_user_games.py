"""User-added games: origin, tags, promotion, deletion and backup.

Run with:

    python -m unittest discover tests
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402


class UserGames(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_add_and_view(self):
        gid = slg_db.add_user_game(self.conn, "My Game", developer="dev",
                                   engine="renpy", version="1.0",
                                   tags=["custom", "tag"], folder_path="/x")
        row = slg_db.get_game(self.conn, gid)
        self.assertEqual(row["origin"], "user")
        self.assertEqual(row["promoted"], 0)
        self.assertTrue(row["slug"].startswith("user-"))
        self.assertEqual(slg_db.game_tags(self.conn, gid), ["custom", "tag"])
        # Unpromoted user games are hidden from the main list, shown in their own.
        self.assertEqual(len(slg_db.find_games(self.conn)), 0)
        self.assertEqual(len(slg_db.find_games(self.conn, origin="user")), 1)

    def test_promote_into_main(self):
        gid = slg_db.add_user_game(self.conn, "My Game", developer="d",
                                   engine="e", version="1", tags=["t"])
        slg_db.promote_game(self.conn, gid, True)
        self.assertEqual(len(slg_db.find_games(self.conn)), 1)

    def test_delete_cleans_children(self):
        gid = slg_db.add_user_game(self.conn, "My Game", tags=["t"],
                                   folder_path="/x")
        slg_db.add_alias(self.conn, gid, "Alias")
        slg_db.delete_game(self.conn, gid)
        self.assertIsNone(slg_db.get_game(self.conn, gid))
        self.assertEqual(slg_db.game_tags(self.conn, gid), [])
        self.assertEqual(
            [r["alias"] for r in self.conn.execute(
                "SELECT alias FROM game_aliases WHERE game_id = ? ORDER BY alias",
                (gid,))], [])

    def test_set_tags_clear(self):
        gid = slg_db.add_user_game(self.conn, "My Game", tags=["a", "b"])
        slg_db.set_tags(self.conn, gid, ["c"], clear=True)
        self.assertEqual(slg_db.game_tags(self.conn, gid), ["c"])
        slg_db.set_tags(self.conn, gid, [], clear=True)
        self.assertEqual(slg_db.game_tags(self.conn, gid), [])

    def test_set_tags_empty_does_not_clear_by_default(self):
        gid = slg_db.add_user_game(self.conn, "My Game", tags=["a"])
        slg_db.set_tags(self.conn, gid, [])
        self.assertEqual(slg_db.game_tags(self.conn, gid), ["a"])


class BackupRoundTrip(unittest.TestCase):
    def test_user_games_survive(self):
        conn = slg_db.connect(":memory:")
        gid = slg_db.add_user_game(conn, "My Game", developer="dev",
                                   engine="renpy", version="1.0",
                                   overview="blurb", tags=["custom"],
                                   folder_path="/x", promoted=1)
        slg_db.add_alias(conn, gid, "My Game Alias")
        data = slg_db.export_user_data(conn)

        conn2 = slg_db.connect(":memory:")
        slg_db.import_user_data(conn2, data)

        rows = slg_db.find_games(conn2, origin="user")
        self.assertEqual(len(rows), 1)
        g = rows[0]
        self.assertEqual(g["title"], "My Game")
        self.assertEqual(g["developer"], "dev")
        self.assertEqual(g["promoted"], 1)
        self.assertEqual(slg_db.game_tags(conn2, g["id"]), ["custom"])
        self.assertEqual(
            [r["alias"] for r in conn2.execute(
                "SELECT alias FROM game_aliases WHERE game_id = ? ORDER BY alias",
                (g["id"],))], ["My Game Alias"])
        conn.close()
        conn2.close()

    def test_rated_user_game_state_remaps_by_slug(self):
        """A user game's rating/status must survive a restore, re-keyed by slug.

        The raw game_id is a local rowid that means nothing on the target
        machine; the restore used to bulk-load `state` by that id before the
        user game existed, tripping the foreign key.
        """
        conn = slg_db.connect(":memory:")
        gid = slg_db.add_user_game(conn, "My Game", developer="dev",
                                   engine="renpy", version="1.0", tags=["t"])
        slg_db.set_state(conn, gid, status="downloaded", my_rating=5)
        data = slg_db.export_user_data(conn)

        conn2 = slg_db.connect(":memory:")
        slg_db.import_user_data(conn2, data)

        g = slg_db.find_games(conn2, origin="user")[0]
        self.assertEqual(g["status"], "downloaded")
        self.assertEqual(g["my_rating"], 5)
        conn.close()
        conn2.close()

    def test_backup_validation_returns_preview_counts_without_paths(self):
        conn = slg_db.connect(":memory:")
        gid = slg_db.add_user_game(conn, "My Game", folder_path="C:/games/my-game",
                                   tags=["t"])
        slg_db.add_alias(conn, gid, "Alias")
        data = slg_db.export_user_data(conn)

        summary = slg_db.validate_user_data(data)

        self.assertEqual(summary["counts"]["user_games"], 1)
        self.assertEqual(summary["counts"]["user_local"], 1)
        self.assertEqual(summary["local_path_count"], 1)
        self.assertEqual(summary["total_records"],
                         sum(summary["counts"].values()))
        self.assertNotIn("C:/games/my-game", repr(summary))
        conn.close()

    def test_malformed_backup_is_rejected_before_existing_data_is_changed(self):
        conn = slg_db.connect(":memory:")
        gid = slg_db.add_user_game(conn, "Existing Game", tags=["old"])
        slg_db.set_state(conn, gid, status="downloaded", my_rating=5)
        slg_db.create_collection(conn, "Existing collection")
        before = slg_db.export_user_data(conn)

        malformed = {
            "collections": [{"name": {"unexpected": "object"}}],
            "user_games": "not a list",
        }
        with self.assertRaises(ValueError):
            slg_db.import_user_data(conn, malformed)

        self.assertEqual(slg_db.export_user_data(conn), before)
        conn.close()

    def test_database_failure_during_restore_rolls_back_all_changes(self):
        conn = slg_db.connect(":memory:")
        gid = slg_db.add_user_game(conn, "Existing Game", tags=["old"])
        slg_db.set_state(conn, gid, status="downloaded", my_rating=4)
        slg_db.create_collection(conn, "Existing collection")
        conn.execute("INSERT INTO points_log (delta, reason, at)"
                     " VALUES (5, 'old', '2026-01-01T00:00:00')")
        conn.execute("INSERT INTO signin (day, at)"
                     " VALUES ('2026-01-01', '2026-01-01T00:00:00')")
        conn.commit()
        before = slg_db.export_user_data(conn)
        conn.execute(
            "CREATE TRIGGER fail_backup_restore BEFORE INSERT ON signin"
            " BEGIN SELECT RAISE(ABORT, 'forced restore failure'); END")
        conn.commit()

        replacement = {
            "collections": [{"id": 8, "name": "Replacement", "created_at": "2026-02-01"}],
            "points_log": [{"delta": 10, "reason": "replacement", "at": "2026-02-01"}],
            "signin": [{"day": "2026-02-01", "at": "2026-02-01"}],
        }
        with self.assertRaisesRegex(Exception, "forced restore failure"):
            slg_db.import_user_data(conn, replacement)

        self.assertEqual(slg_db.export_user_data(conn), before)
        self.assertIsNone(slg_db.get_pref(conn, slg_db.LEGACY_IMPORTED_ASSETS_PREF))
        conn.close()

    def test_missing_backup_sections_remain_backward_compatible(self):
        summary = slg_db.validate_user_data({"collections": []})
        self.assertEqual(summary["counts"]["collections"], 0)
        self.assertEqual(summary["counts"]["user_games"], 0)

    def test_restored_assets_mark_provenance_but_curation_does_not(self):
        conn = slg_db.connect(":memory:")
        self.addCleanup(conn.close)
        slg_db.import_user_data(conn, {"collections": []})
        self.assertIsNone(slg_db.get_pref(conn, slg_db.LEGACY_IMPORTED_ASSETS_PREF))
        slg_db.import_user_data(conn, {
            "owned_titles": [{"title_id": "group_friend", "source": "code",
                              "acquired_at": "2026-01-01"}],
        })
        self.assertEqual(slg_db.get_pref(conn, slg_db.LEGACY_IMPORTED_ASSETS_PREF), "1")
        slg_db.import_user_data(conn, {})
        self.assertEqual(slg_db.get_pref(conn, slg_db.LEGACY_IMPORTED_ASSETS_PREF), "1")


class StripCatalogue(unittest.TestCase):
    def test_strip_removes_user_games_and_private_tables(self):
        """The public snapshot keeps only site games; personal data is dropped."""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.db")
        try:
            conn = slg_db.connect(path)
            slg_db.add_user_game(conn, "My Game", tags=["t"])
            slg_db.upsert_game(conn, slug="site-1", url="http://x", title="Site",
                               version="1", developer="d", engine="e",
                               tags=["tag"], complete=0, lastmod="2026-01-01")
            slg_db.set_pref(conn, "k", "v")
            conn.close()

            slg_db.strip_to_site_catalogue(path)

            # Inspect with plain sqlite3, not slg_db.connect: connect() runs the
            # schema, which would re-create the tables the strip just dropped.
            import sqlite3
            conn = sqlite3.connect(path)
            try:
                slugs = {r[0] for r in conn.execute("SELECT slug FROM games")}
                self.assertEqual(slugs, {"site-1"})
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for private in ("prefs", "state", "collections", "comments"):
                    self.assertNotIn(private, tables)
            finally:
                conn.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
