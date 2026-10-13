"""Verify hidden flags at real client query, restore and sync integration seams."""
import copy
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import slg_db
import slg_hidden_games as hidden
import slg_library_query
import slg_sync_server


class HiddenIntegration0265(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = slg_db.connect(os.path.join(self.tmp.name, "client.db"))
        self.old, _ = slg_db.upsert_game(self.conn, "old", "https://example.invalid/old", "Old")
        self.new, _ = slg_db.upsert_game(self.conn, "new", "https://example.invalid/new", "New")
        self.user = slg_db.add_user_game(self.conn, "User")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ids(self, **kwargs):
        return {row["id"] for row in slg_db.find_games(self.conn, origin=None, with_scores=False, **kwargs)}

    def test_find_games_default_only_all_and_collection_search(self):
        collection = slg_db.create_collection(self.conn, "Favourite")
        slg_db.set_game_collections(self.conn, self.old, [collection])
        slg_db.set_game_collections(self.conn, self.new, [collection])
        hidden.set_hidden(self.conn, self.old, True)
        self.assertEqual(self.ids(), {self.new, self.user})
        self.assertEqual(self.ids(hidden_mode="only"), {self.old})
        self.assertEqual(self.ids(hidden_mode="all"), {self.old, self.new, self.user})
        self.assertEqual(self.ids(search="Old"), set())
        self.assertEqual(self.ids(collection_id=collection), {self.new})
        self.assertIsNotNone(slg_db.get_game(self.conn, self.old))
        with self.assertRaises(ValueError):
            self.ids(hidden_mode="invalid")

    def test_local_page_excludes_hidden_registered_and_user_games(self):
        slg_db.set_local_folder(self.conn, self.old, "D:/Games/Old")
        slg_db.set_local_exe(self.conn, self.new, "D:/Games/New/game.exe")
        hidden.set_hidden(self.conn, self.old, True)
        hidden.set_hidden(self.conn, self.user, True)
        result = slg_library_query.query_local_page(self.conn, {}, 1)
        self.assertEqual(result.total, 1)
        self.assertEqual([row["id"] for row in result.rows], [self.new])
        self.assertEqual(slg_library_query.query_local_page(self.conn, {"search": "Old"}, 1).total, 0)
        hidden.set_hidden(self.conn, self.old, False)
        self.assertEqual(slg_library_query.query_local_page(self.conn, {}, 1).total, 2)

    def test_local_page_stats_match_visible_scope(self):
        slg_db.set_local_folder(self.conn, self.old, "D:/Games/Old")
        slg_db.set_state(self.conn, self.old, my_rating=5)
        slg_db.set_tags(self.conn, self.old, ["hidden-only-tag"])
        hidden.set_hidden(self.conn, self.old, True)
        hidden.set_hidden(self.conn, self.user, True)
        result = slg_library_query.query_local_page(self.conn, {}, 1)
        self.assertEqual(result.total, 0)
        self.assertEqual(result.stats, {"games": 0, "rated": 0, "downloaded": 0, "tags": 0})

    def test_hidden_wishlist_updates_excluded_until_restored(self):
        self.conn.execute("UPDATE games SET version='1.0' WHERE id=?", (self.old,))
        slg_db.set_state(self.conn, self.old, status="want")
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), [])
        self.conn.execute("UPDATE games SET version='2.0' WHERE id=?", (self.old,))
        hidden.set_hidden(self.conn, self.old, True)
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT seen_version FROM wishlist_version_seen WHERE game_id=?",
                                           (self.old,)).fetchone()[0], "1.0")
        hidden.set_hidden(self.conn, self.old, False)
        self.assertEqual([row["id"] for row in slg_db.wishlist_version_changes(self.conn)], [self.old])

    def test_sync_redirect_hidden_only_reference_backed_up_and_unioned(self):
        for old_hidden, new_hidden in ((True, False), (False, True), (True, True)):
            with self.subTest(old_hidden=old_hidden, new_hidden=new_hidden):
                self.conn.execute("DELETE FROM catalog_redirects")
                hidden.restore_all(self.conn)
                if old_hidden:
                    hidden.set_hidden(self.conn, self.old, True)
                    self.assertTrue(slg_sync_server._redirect_has_user_references(self.conn, self.old, "old"))
                if new_hidden:
                    hidden.set_hidden(self.conn, self.new, True)
                self.assertFalse(slg_sync_server._redirect_pair_conflict(self.conn, self.old, self.new, "old"))
                with patch.object(slg_sync_server, "_create_catalog_backup", wraps=slg_sync_server._create_catalog_backup) as backup:
                    slg_sync_server._sync_catalog_redirects(self.conn, {"old": "new"})
                    self.assertEqual(backup.call_count, 1)
                self.assertEqual(hidden.hidden_ids(self.conn), {self.new})
                self.assertEqual(self.ids(), {self.user})
                self.assertEqual(self.conn.execute("SELECT active FROM catalog_redirects WHERE old_slug='old'").fetchone()[0], 1)

    def test_hidden_backup_different_ids_roundtrip_and_missing_section_keeps_flags(self):
        hidden.set_hidden(self.conn, self.old, True)
        data = slg_db.export_user_data(self.conn)
        self.assertEqual(slg_db.validate_user_data(data)["counts"]["hidden_games"], 1)
        hidden.set_hidden(self.conn, self.old, False)
        hidden.set_hidden(self.conn, self.new, True)
        old_data = copy.deepcopy(data)
        del old_data["hidden_games"]
        slg_db.import_user_data(self.conn, old_data)
        self.assertEqual(hidden.hidden_ids(self.conn), {self.new})
        slg_db.import_user_data(self.conn, data)
        self.assertEqual(hidden.hidden_ids(self.conn), {self.old})
        data["hidden_games"] = []
        slg_db.import_user_data(self.conn, data)
        self.assertEqual(hidden.hidden_ids(self.conn), set())

    def test_invalid_hidden_backup_never_changes_any_personal_data(self):
        slg_db.set_state(self.conn, self.old, my_rating=4)
        hidden.set_hidden(self.conn, self.old, True)
        data = slg_db.export_user_data(self.conn)
        baseline = copy.deepcopy(data)
        data["state"] = []
        for records in ([{"slug": "missing", "hidden_at": "2026-10-09T12:00:00"}],
                        [{"slug": "old", "hidden_at": "bad"}],
                        [{"slug": "old", "hidden_at": "2026-10-09T12:00:00", "game_id": 1}]):
            with self.subTest(records=records):
                data["hidden_games"] = records
                with self.assertRaises(ValueError):
                    slg_db.import_user_data(self.conn, data)
                self.assertEqual(slg_db.export_user_data(self.conn), baseline)

    def test_hidden_user_game_rekeys_during_full_backup_restore(self):
        hidden.set_hidden(self.conn, self.user, True)
        data = slg_db.export_user_data(self.conn)
        other = slg_db.connect(":memory:")
        try:
            slg_db.add_user_game(other, "Unrelated")
            slg_db.import_user_data(other, data)
            slug = next(row["slug"] for row in data["user_games"] if row["id"] == self.user)
            restored = other.execute("SELECT id FROM games WHERE slug=?", (slug,)).fetchone()[0]
            self.assertTrue(hidden.is_hidden(other, restored))
            self.assertEqual(hidden.export_sections(other), hidden.export_sections(self.conn))
        finally:
            other.close()

    def test_public_snapshot_strips_hidden_table_and_its_preferences(self):
        hidden.set_hidden(self.conn, self.old, True)
        self.conn.commit()
        snapshot = os.path.join(self.tmp.name, "public.db")
        disk = sqlite3.connect(snapshot)
        self.conn.backup(disk)
        disk.close()
        slg_db.strip_to_site_catalogue(snapshot)
        conn = sqlite3.connect(snapshot)
        try:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='hidden_games'").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 2)
        finally:
            conn.close()

    def test_old_readonly_catalogue_queries_do_not_recreate_hidden_table(self):
        slg_db.set_local_folder(self.conn, self.old, "D:/Games/Old")
        self.conn.execute("DROP TABLE hidden_games")
        self.conn.commit()
        conn = slg_library_query.open_readonly(Path(self.tmp.name) / "client.db")
        try:
            self.assertEqual(len(slg_db.find_games(conn, origin=None, with_scores=False)), 3)
            self.assertEqual(slg_db.find_games(conn, origin=None, hidden_mode="only", with_scores=False), [])
            self.assertEqual(slg_library_query.query_local_page(conn, {}, 1).total, 2)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='hidden_games'").fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
