import os
import tempfile
import unittest
import slg_db
import slg_hidden_games
import slg_collections_workspace as model


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "personal.db")
        self.conn = slg_db.connect(self.path)
        self.west, _ = slg_db.upsert_game(self.conn, "west", "https://example.invalid", "West")
        self.dl, _ = slg_db.upsert_game(self.conn, "dlsite-rj123456", "https://example.invalid", "DL")
        self.user = slg_db.add_user_game(self.conn, "Mine")
        self.conn.execute("INSERT OR IGNORE INTO game_catalogs VALUES (?, 'western_slg')", (self.west,))
        self.conn.execute("INSERT OR IGNORE INTO game_catalogs VALUES (?, 'dlsite')", (self.dl,))
        self.cid = model.create_collection(self.conn, " Games ")
        for gid in (self.west, self.dl, self.user):
            slg_db.set_game_collections(self.conn, gid, [self.cid])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_names(self):
        for name in ("", "   ", None, " games "):
            with self.assertRaises(ValueError): model.create_collection(self.conn, name)
        second = model.create_collection(self.conn, "Other")
        with self.assertRaises(ValueError): model.rename_collection(self.conn, second, "GAMES")
        model.rename_collection(self.conn, self.cid, " Games ")
        self.assertEqual(model.overview(self.conn)[0]["name"], "Games")
        with self.assertRaises(ValueError): model.rename_collection(self.conn, 999, "Missing")

    def test_hidden_totals_and_empty(self):
        slg_hidden_games.set_hidden(self.conn, self.dl, True)
        row = model.overview(self.conn)[0]
        self.assertEqual((row["total_count"], row["visible_count"]), (3, 2))
        empty = model.create_collection(self.conn, "Empty")
        self.assertEqual(model.overview(self.conn)[1]["visible_count"], 0)

    def test_all_sources_and_pagination(self):
        spec = dict(collection_id=self.cid, source="all", sort="title", desc=False)
        rows, result = model.read_snapshot(self.path, spec, page=1, page_size=2)
        self.assertEqual(result.total, 3)
        self.assertEqual(len(result.rows), 2)
        _, second = model.read_snapshot(self.path, spec, page=2, page_size=2)
        self.assertEqual(len(second.rows), 1)
        for source, gid in (("western_slg", self.west), ("dlsite", self.dl), ("user", self.user)):
            _, result = model.read_snapshot(self.path, dict(spec, source=source))
            self.assertEqual([row["id"] for row in result.rows], [gid])

    def test_remove_delete_safe_even_foreign_keys_disabled(self):
        model.remove_member(self.conn, self.cid, self.west)
        self.conn.execute("PRAGMA foreign_keys=OFF")
        model.delete_collection(self.conn, self.cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0], 0)

    def test_snapshot_readonly(self):
        before = self.conn.total_changes
        overview, result = model.read_snapshot(self.path, {})
        self.assertIsNone(result)
        self.assertEqual(overview[0]["total_count"], 3)
        self.assertEqual(before, self.conn.total_changes)

    def test_backup_contract_and_game_ids_unchanged(self):
        before = slg_db.export_user_data(self.conn)
        ids = [row[0] for row in self.conn.execute("SELECT id FROM games ORDER BY id")]
        model.rename_collection(self.conn, self.cid, "Renamed")
        after = slg_db.export_user_data(self.conn)
        self.assertEqual(before["collection_items"], after["collection_items"])
        self.assertEqual(ids, [row[0] for row in self.conn.execute("SELECT id FROM games ORDER BY id")])
        self.assertEqual(after["collections"][0]["name"], "Renamed")

    def test_page_has_cover_contract_and_hidden_not_leaked(self):
        slg_hidden_games.set_hidden(self.conn, self.dl, True)
        _, result = model.read_snapshot(self.path, dict(collection_id=self.cid, source="all", sort="title"))
        self.assertEqual(result.total, 2)
        self.assertNotIn(self.dl, [row["id"] for row in result.rows])
        self.assertTrue(all("catalogue_cover_file" in row for row in result.rows))


if __name__ == "__main__": unittest.main()

