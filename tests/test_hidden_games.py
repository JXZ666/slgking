"""Hidden visibility is local personal data, independent from other records."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import slg_hidden_games as hidden

STAMP = "2026-10-09T12:00:00+00:00"


def database():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE games(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,title TEXT)")
    conn.executemany("INSERT INTO games VALUES (?,?,?)", [(1, "old", "Old game"), (2, "new", "New game"),
                                                           (3, "percent", "100%_game")])
    conn.commit()
    return conn


class HiddenGames(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def tearDown(self):
        self.conn.close()

    def test_old_database_read_helpers_do_not_create_schema(self):
        self.assertEqual(hidden.hidden_ids(self.conn), set())
        self.assertFalse(hidden.is_hidden(self.conn, 1))
        self.assertEqual(hidden.query_hidden(self.conn), [])
        self.assertEqual(hidden.visibility_sql(self.conn), "1")
        self.assertEqual(hidden.visibility_sql(self.conn, hidden=True), "0")
        self.assertEqual(hidden.export_sections(self.conn), {"hidden_games": []})
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='hidden_games'").fetchone())

    def test_schema_idempotent_and_foreign_key_cascade(self):
        hidden.ensure_schema(self.conn)
        hidden.ensure_schema(self.conn)
        fk = self.conn.execute("PRAGMA foreign_key_list(hidden_games)").fetchone()
        self.assertEqual((fk[2], fk[3], fk[6]), ("games", "game_id", "CASCADE"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO hidden_games VALUES(999,?)", (STAMP,))
        hidden.set_hidden(self.conn, 1, True)
        self.conn.execute("DELETE FROM games WHERE id=1")
        self.assertEqual(hidden.hidden_ids(self.conn), set())

    def test_set_restore_and_timestamp_idempotence(self):
        self.assertTrue(hidden.set_hidden(self.conn, 1, True))
        timestamp = hidden.export_sections(self.conn)["hidden_games"][0]["hidden_at"]
        self.assertFalse(hidden.set_hidden(self.conn, 1, True))
        self.assertEqual(hidden.export_sections(self.conn)["hidden_games"][0]["hidden_at"], timestamp)
        self.assertTrue(hidden.is_hidden(self.conn, 1))
        self.assertTrue(hidden.set_hidden(self.conn, 1, False))
        self.assertFalse(hidden.set_hidden(self.conn, 1, False))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 3)

    def test_bad_set_arguments_do_not_create_table(self):
        for game_id, flag in ((999, True), (True, True), (1, 1), ("1", True), (0, True)):
            with self.subTest(game_id=game_id, flag=flag), self.assertRaises(ValueError):
                hidden.set_hidden(self.conn, game_id, flag)
        self.assertEqual(hidden.hidden_ids(self.conn), set())

    def test_caller_transaction_can_rollback(self):
        hidden.ensure_schema(self.conn)
        self.conn.commit()
        hidden.set_hidden(self.conn, 1, True, commit=False)
        self.conn.rollback()
        self.assertFalse(hidden.is_hidden(self.conn, 1))

    def test_normal_predicate_management_search_and_restore_all(self):
        hidden.set_hidden(self.conn, 1, True)
        hidden.set_hidden(self.conn, 3, True)
        sql = "SELECT id FROM games g WHERE " + hidden.visibility_sql(self.conn)
        self.assertEqual([row[0] for row in self.conn.execute(sql)], [2])
        self.assertEqual([row["id"] for row in hidden.query_hidden(self.conn, "old")], [1])
        self.assertEqual([row["id"] for row in hidden.query_hidden(self.conn, "%_")], [3])
        self.assertEqual(hidden.query_hidden(self.conn, "' OR 1=1 --"), [])
        self.assertEqual(hidden.restore_all(self.conn), 2)
        self.assertEqual(hidden.restore_all(self.conn), 0)

    def test_alias_injection_rejected(self):
        with self.assertRaises(ValueError):
            hidden.visibility_sql(self.conn, "g); DROP TABLE games;--")

    def test_slug_backup_round_trip_across_different_ids(self):
        hidden.set_hidden(self.conn, 1, True)
        data = hidden.export_sections(self.conn)
        other = database()
        try:
            other.execute("UPDATE games SET id=101 WHERE slug='old'")
            hidden.import_sections(other, data)
            self.assertEqual(hidden.hidden_ids(other), {101})
            self.assertEqual(hidden.export_sections(other), data)
        finally:
            other.close()

    def test_missing_section_preserves_and_empty_section_restores(self):
        hidden.set_hidden(self.conn, 1, True)
        hidden.import_sections(self.conn, {})
        self.assertEqual(hidden.hidden_ids(self.conn), {1})
        hidden.import_sections(self.conn, {"hidden_games": []})
        self.assertEqual(hidden.hidden_ids(self.conn), set())

    def test_invalid_sections_never_clear_existing_flags(self):
        hidden.set_hidden(self.conn, 1, True)
        original = hidden.export_sections(self.conn)
        invalid = [None, [], {"hidden_games": None}, {"hidden_games": {}},
                   {"hidden_games": [None]}, {"hidden_games": [{"slug": "old"}]},
                   {"hidden_games": [{"slug": "old", "hidden_at": STAMP, "game_id": 1}]},
                   {"hidden_games": [{"slug": "old", "hidden_at": 1}]},
                   {"hidden_games": [{"slug": "old", "hidden_at": "bad"}]},
                   {"hidden_games": [{"slug": "\x00", "hidden_at": STAMP}]},
                   {"hidden_games": [{"slug": 1, "hidden_at": STAMP}]},
                   {"hidden_games": [{"slug": "missing", "hidden_at": STAMP}]},
                   {"hidden_games": [], "user_games": None},
                   {"hidden_games": [{"slug": "old", "hidden_at": STAMP}] * 2}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                hidden.import_sections(self.conn, data)
            self.assertEqual(hidden.export_sections(self.conn), original)

    def test_invalid_first_import_does_not_create_table(self):
        with self.assertRaises(ValueError):
            hidden.import_sections(self.conn, {"hidden_games": [{"slug": "missing", "hidden_at": STAMP}]})
        self.assertEqual(hidden.visibility_sql(self.conn), "1")

    def test_pending_user_games_validate_but_must_exist_before_import(self):
        data = {"user_games": [{"slug": "user-abc"}],
                "hidden_games": [{"slug": "user-abc", "hidden_at": STAMP}]}
        self.assertEqual(hidden.validate_sections(data, self.conn), {"hidden_games": 1})
        with self.assertRaises(ValueError):
            hidden.import_sections(self.conn, data)
        self.conn.execute("INSERT INTO games VALUES(4,'user-abc','User game')")
        hidden.import_sections(self.conn, data)
        self.assertEqual(hidden.hidden_ids(self.conn), {4})

    def test_redirect_union_old_new_both_and_neither(self):
        for old, new in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(old=old, new=new):
                hidden.restore_all(self.conn)
                if old:
                    hidden.set_hidden(self.conn, 1, True)
                if new:
                    hidden.set_hidden(self.conn, 2, True)
                hidden.merge_redirect(self.conn, 1, 2)
                self.assertEqual(hidden.hidden_ids(self.conn), {2} if old or new else set())
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 3)

    def test_redirect_same_id_keeps_flag_and_invalid_target_keeps_source(self):
        hidden.set_hidden(self.conn, 1, True)
        hidden.merge_redirect(self.conn, 1, 1)
        self.assertEqual(hidden.hidden_ids(self.conn), {1})
        with self.assertRaises(ValueError):
            hidden.merge_redirect(self.conn, 1, 999)
        self.assertEqual(hidden.hidden_ids(self.conn), {1})

    def test_restore_redirected_slugs_coalesces(self):
        self.conn.execute("CREATE TABLE catalog_redirects(old_slug TEXT,canonical_slug TEXT,active INTEGER)")
        self.conn.execute("INSERT INTO catalog_redirects VALUES('old','new',1)")
        hidden.import_sections(self.conn, {"hidden_games": [{"slug": slug, "hidden_at": STAMP}
                                                           for slug in ("old", "new")]})
        self.assertEqual(hidden.hidden_ids(self.conn), {2})

    def test_redirect_cycle_backup_rejected_before_writes(self):
        hidden.set_hidden(self.conn, 3, True)
        self.conn.execute("CREATE TABLE catalog_redirects(old_slug TEXT,canonical_slug TEXT,active INTEGER)")
        self.conn.executemany("INSERT INTO catalog_redirects VALUES(?,?,1)", [("old", "new"), ("new", "old")])
        with self.assertRaises(ValueError):
            hidden.import_sections(self.conn, {"hidden_games": [{"slug": "old", "hidden_at": STAMP}]})
        self.assertEqual(hidden.hidden_ids(self.conn), {3})

    def test_restore_runtime_failure_rolls_back(self):
        hidden.set_hidden(self.conn, 1, True)
        self.conn.execute("CREATE TRIGGER fail_hidden BEFORE INSERT ON hidden_games"
                          " WHEN NEW.game_id=2 BEGIN SELECT RAISE(ABORT,'failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            hidden.import_sections(self.conn, {"hidden_games": [{"slug": "new", "hidden_at": STAMP}]})
        self.assertEqual(hidden.hidden_ids(self.conn), {1})

    def test_readonly_old_catalogue_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.db")
            disk = sqlite3.connect(path)
            self.conn.backup(disk)
            disk.close()
            conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
            try:
                self.assertEqual(hidden.query_hidden(conn), [])
                self.assertEqual(hidden.hidden_ids(conn), set())
                self.assertEqual(hidden.export_sections(conn), {"hidden_games": []})
            finally:
                conn.close()

    def test_real_database_personal_records_and_rating_weights_unchanged(self):
        import slg_db
        conn = slg_db.connect(":memory:")
        try:
            gid, _ = slg_db.upsert_game(conn, "real", "https://example.invalid/real", "Real")
            slg_db.set_tags(conn, gid, ["tag"])
            slg_db.set_state(conn, gid, status="done", my_rating=5)
            slg_db.set_local_folder(conn, gid, "D:/Games/Real")
            slg_db.set_local_exe(conn, gid, "D:/Games/Real/game.exe")
            collection = slg_db.create_collection(conn, "Favourite")
            slg_db.set_game_collections(conn, gid, [collection])
            tables = ("state", "local", "collection_items", "weights", "affinities")
            before = {table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)] for table in tables}
            hidden.set_hidden(conn, gid, True)
            hidden.set_hidden(conn, gid, False)
            hidden.set_hidden(conn, gid, True)
            hidden.restore_all(conn)
            for table in tables:
                self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM " + table)], before[table])
        finally:
            conn.close()

    def test_hiding_keeps_personal_records_and_disk(self):
        self.conn.execute("CREATE TABLE state(game_id INTEGER,status TEXT,note TEXT,my_rating INTEGER)")
        self.conn.execute("INSERT INTO state VALUES(1,'done','note',5)")
        self.conn.execute("CREATE TABLE local(game_id INTEGER,folder_path TEXT,exe_path TEXT)")
        self.conn.execute("INSERT INTO local VALUES(1,'folder','launch.exe')")
        self.conn.execute("CREATE TABLE collection_items(collection_id INTEGER,game_id INTEGER)")
        self.conn.execute("INSERT INTO collection_items VALUES(5,1)")
        before = {table: self.conn.execute("SELECT * FROM " + table).fetchall()
                  for table in ("state", "local", "collection_items")}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "save.dat")
            with open(path, "wb") as output:
                output.write(b"save")
            hidden.set_hidden(self.conn, 1, True)
            hidden.set_hidden(self.conn, 1, False)
            hidden.set_hidden(self.conn, 1, True)
            hidden.restore_all(self.conn)
            with open(path, "rb") as source:
                self.assertEqual(source.read(), b"save")
        for table, records in before.items():
            self.assertEqual(self.conn.execute("SELECT * FROM " + table).fetchall(), records)


if __name__ == "__main__":
    unittest.main()
