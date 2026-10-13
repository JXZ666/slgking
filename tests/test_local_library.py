"""Personal overrides, non-destructive bindings, portable backup and image safety."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
import slg_db
import slg_local_library as library
import slg_sync_server


class LocalLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = self.tmp.name
        self.conn = slg_db.connect(":memory:")
        library.ensure_schema(self.conn)
        self.public, _ = slg_db.upsert_game(self.conn, "public", "https://example.invalid/public", "Public")
        self.custom = slg_db.add_user_game(self.conn, "Custom")
        slg_db.set_cover(self.conn, self.public, "catalogue.jpg")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def picture(self, name="source.png", **kwargs):
        path = os.path.join(self.directory, name)
        image = Image.new("RGBA", kwargs.pop("size", (120, 200)), (255, 0, 0, 100))
        image.save(path, **kwargs)
        image.close()
        return path

    def rows(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM games ORDER BY id")]

    def test_public_custom_override_reset_and_missing_fallback(self):
        name = library.import_cover(self.picture(), self.directory)
        for gid in (self.public, self.custom):
            library.set_cover_override(self.conn, gid, name)
        rows = library.apply_cover_overrides(self.conn, self.rows(), self.directory)
        self.assertEqual([row["cover_file"] for row in rows], [name, name])
        self.assertTrue(all(row["has_cover_override"] for row in rows))
        self.assertEqual(rows[0]["catalogue_cover_file"], "catalogue.jpg")
        slg_db.set_cover(self.conn, self.public, "new-directory.jpg")
        self.assertEqual(library.apply_cover_overrides(self.conn, self.rows(), self.directory)[0]["cover_file"], name)
        library.clear_cover_override(self.conn, self.public)
        self.assertEqual(library.apply_cover_overrides(self.conn, self.rows(), self.directory)[0]["cover_file"], "new-directory.jpg")
        os.unlink(os.path.join(self.directory, name))
        row = library.apply_cover_overrides(self.conn, self.rows(), self.directory)[1]
        self.assertFalse(row["has_cover_override"])
        self.assertTrue(row["cover_override_missing"])

    def test_bad_image_never_changes_old_override(self):
        old = library.import_cover(self.picture(), self.directory)
        library.set_cover_override(self.conn, self.public, old)
        broken = os.path.join(self.directory, "broken.png")
        with open(broken, "wb") as output:
            output.write(b"not an image")
        with self.assertRaises(ValueError):
            library.import_cover(broken, self.directory)
        self.assertEqual(self.conn.execute("SELECT cover_file FROM game_cover_overrides").fetchone()[0], old)

    def test_limits_formats_and_exif_orientation(self):
        with patch.object(library, "MAX_COVER_PIXELS", 100):
            with self.assertRaises(ValueError):
                library.prepare_cover(self.picture())
        with patch.object(library, "MAX_COVER_BYTES", 10):
            with self.assertRaises(ValueError):
                library.prepare_cover(self.picture())
        with self.assertRaises(ValueError):
            library.prepare_cover(self.picture("gif.gif"))
        path = os.path.join(self.directory, "rotated.jpg")
        image = Image.new("RGB", (20, 40))
        exif = image.getexif()
        exif[274] = 6
        image.save(path, exif=exif)
        prepared = library.prepare_cover(path)
        self.assertEqual(prepared.size, (40, 20))
        prepared.close()

    def test_atomic_write_failure_cleans_temporary_file(self):
        image = library.prepare_cover(self.picture())
        with patch.object(library.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                library.save_cover(image, self.directory)
        image.close()
        self.assertFalse(any(name.startswith(".cover-") for name in os.listdir(self.directory)))

    def test_clear_registration_preserves_disk_and_other_fields(self):
        path = self.picture()
        slg_db.set_local_folder(self.conn, self.public, self.directory, "v1")
        slg_db.set_local_exe(self.conn, self.public, path)
        library.clear_local_binding(self.conn, self.public, clear_folder=False)
        row = self.conn.execute("SELECT * FROM local WHERE game_id=?", (self.public,)).fetchone()
        self.assertEqual(row["folder_path"], self.directory)
        self.assertIsNone(row["exe_path"])
        library.clear_local_binding(self.conn, self.public)
        self.assertIsNone(self.conn.execute("SELECT * FROM local WHERE game_id=?", (self.public,)).fetchone())
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 2)

    def test_backup_roundtrip_rekeys_and_warns_missing_images(self):
        name = library.import_cover(self.picture(), self.directory)
        library.set_cover_override(self.conn, self.public, name)
        slg_db.set_local_folder(self.conn, self.public, self.directory)
        data = library.export_sections(self.conn)
        library.clear_cover_override(self.conn, self.public)
        library.clear_local_binding(self.conn, self.public)
        warnings = library.import_sections(self.conn, data, self.directory)
        self.assertEqual(warnings, [])
        self.assertEqual(self.conn.execute("SELECT game_id FROM local").fetchone()[0], self.public)
        os.unlink(os.path.join(self.directory, name))
        warnings = library.import_sections(self.conn, data, self.directory)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM game_cover_overrides").fetchone()[0], 0)

    def test_invalid_backup_rejected_before_mutation(self):
        slg_db.set_local_folder(self.conn, self.public, self.directory)
        for data in (
            {"local_bindings": [{"slug": "missing"}]},
            {"local_bindings": [{"slug": "public", "folder_path": []}]},
            {"cover_overrides": [{"slug": "public", "cover_file": "../private.png"}]},
            {"cover_overrides": [{"slug": "public", "cover_file": "C:\\private.png"}]},
            {"local_bindings": [{"slug": "public"}, {"slug": "public"}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                library.import_sections(self.conn, data, self.directory)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM local").fetchone()[0], 1)

    def test_old_backup_preserves_existing_public_bindings(self):
        slg_db.set_local_folder(self.conn, self.public, self.directory)
        library.import_sections(self.conn, {}, self.directory)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM local").fetchone()[0], 1)

    def test_redirect_conflict_and_safe_move(self):
        library.set_cover_override(self.conn, self.public, "one.jpg")
        library.set_cover_override(self.conn, self.custom, "two.jpg")
        self.assertTrue(library.cover_override_conflict(self.conn, self.public, self.custom))
        with self.assertRaises(ValueError):
            library.merge_cover_override(self.conn, self.public, self.custom)
        library.clear_cover_override(self.conn, self.custom)
        library.merge_cover_override(self.conn, self.public, self.custom)
        self.assertEqual(tuple(self.conn.execute("SELECT game_id,cover_file FROM game_cover_overrides").fetchone()), (self.custom, "one.jpg"))

    def test_delete_game_cascades_override(self):
        library.set_cover_override(self.conn, self.custom, "one.jpg")
        slg_db.delete_game(self.conn, self.custom)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM game_cover_overrides").fetchone()[0], 0)

    def test_library_includes_unpromoted_custom_and_bound_public(self):
        query = "SELECT g.id FROM games g LEFT JOIN local l ON l.game_id=g.id WHERE " + library.LOCAL_LIBRARY_SQL
        self.assertEqual([row[0] for row in self.conn.execute(query)], [self.custom])
        slg_db.set_local_exe(self.conn, self.public, "missing.exe")
        self.assertEqual(set(row[0] for row in self.conn.execute(query)), {self.public, self.custom})

    def test_file_backed_persistence(self):
        database = os.path.join(self.directory, "library.db")
        conn = slg_db.connect(database)
        gid = slg_db.add_user_game(conn, "Persistent")
        library.set_cover_override(conn, gid, "persistent.jpg")
        conn.commit()
        conn.close()
        reopened = slg_db.connect(database)
        self.assertEqual(reopened.execute("SELECT game_id,cover_file FROM game_cover_overrides").fetchone()[0], gid)
        reopened.close()

    def test_real_backup_validator_accepts_old_and_current_exports(self):
        self.assertEqual(slg_db.validate_user_data({})["total_records"], 0)
        data = slg_db.export_user_data(self.conn)
        self.assertIn("cover_overrides", data)
        self.assertIn("local_bindings", data)
        slg_db.validate_user_data(data)

    def test_real_backup_validator_rejects_invalid_new_sections(self):
        for data in (
            {"cover_overrides": [{"slug": "public", "cover_file": "../private.png"}]},
            {"cover_overrides": [{"slug": "public"}]},
            {"local_bindings": [{"slug": "public", "exe_path": 1}]},
            {"local_bindings": [{"slug": "public"}, {"slug": "public"}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                slg_db.validate_user_data(data)

    def test_real_restore_keeps_public_id_bindings_and_override(self):
        name = library.import_cover(self.picture(), self.directory)
        library.set_cover_override(self.conn, self.public, name)
        slg_db.set_local_folder(self.conn, self.public, self.directory)
        data = slg_db.export_user_data(self.conn)
        library.clear_cover_override(self.conn, self.public)
        library.clear_local_binding(self.conn, self.public)
        with patch.object(slg_db, "covers_dir", return_value=self.directory):
            self.assertEqual(slg_db.import_user_data(self.conn, data), [])
            self.assertEqual(slg_db.get_game(self.conn, self.public)["cover_file"], name)
        self.assertEqual(self.conn.execute("SELECT game_id FROM local WHERE folder_path=?", (self.directory,)).fetchone()[0], self.public)

    def test_real_restore_rolls_back_all_sections_on_late_failure(self):
        slg_db.set_state(self.conn, self.public, status="downloaded", my_rating=4)
        slg_db.set_local_folder(self.conn, self.public, self.directory)
        data = slg_db.export_user_data(self.conn)
        data["state"] = []
        data["local_bindings"] = []
        original = library.import_sections

        def failing_import(conn, backup):
            original(conn, backup)
            raise OSError("late failure")

        with patch.object(library, "import_sections", side_effect=failing_import):
            with self.assertRaises(OSError):
                slg_db.import_user_data(self.conn, data)
        self.assertEqual(self.conn.execute("SELECT my_rating FROM state WHERE game_id=?", (self.public,)).fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT folder_path FROM local WHERE game_id=?", (self.public,)).fetchone()[0], self.directory)

    def test_public_snapshot_strips_personal_cover_references(self):
        database = os.path.join(self.directory, "private.db")
        conn = slg_db.connect(database)
        gid, _ = slg_db.upsert_game(conn, "public", "https://example.invalid/public", "Public")
        library.set_cover_override(conn, gid, "private-user-cover.jpg")
        conn.commit()
        conn.close()
        slg_db.strip_to_site_catalogue(database)
        import sqlite3
        snapshot = sqlite3.connect(database)
        try:
            self.assertIsNone(snapshot.execute("SELECT name FROM sqlite_master WHERE name='game_cover_overrides'").fetchone())
        finally:
            snapshot.close()

    def test_real_redirect_preflight_and_merge_preserve_personal_cover(self):
        canonical, _ = slg_db.upsert_game(self.conn, "canonical", "https://example.invalid/canonical", "Canonical")
        library.set_cover_override(self.conn, self.public, "old.jpg")
        self.assertTrue(slg_sync_server._redirect_has_user_references(self.conn, self.public, "public"))
        self.assertFalse(slg_sync_server._redirect_pair_conflict(self.conn, self.public, canonical, "public"))
        slg_sync_server._merge_redirect_pair(self.conn, self.public, canonical, "public", "canonical")
        self.assertEqual(self.conn.execute("SELECT game_id FROM game_cover_overrides").fetchone()[0], canonical)
        library.set_cover_override(self.conn, self.public, "different.jpg")
        self.assertTrue(slg_sync_server._redirect_pair_conflict(self.conn, self.public, canonical, "public"))

    def test_old_readonly_snapshot_queries_never_create_override_table(self):
        import sqlite3
        from pathlib import Path
        database = os.path.join(self.directory, "old-snapshot.db")
        conn = slg_db.connect(database)
        gid, _ = slg_db.upsert_game(conn, "old", "https://example.invalid/old", "Old")
        slg_db.set_cover(conn, gid, "catalogue.jpg")
        conn.execute("DROP TABLE game_cover_overrides")
        conn.commit()
        conn.close()
        readonly = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)
        readonly.row_factory = sqlite3.Row
        try:
            readonly.execute("PRAGMA query_only=ON")
            row = slg_db.get_game(readonly, gid)
            self.assertEqual(row["cover_file"], "catalogue.jpg")
            self.assertFalse(row["has_cover_override"])
            rows = library.apply_cover_overrides(readonly, readonly.execute("SELECT * FROM games").fetchall())
            self.assertEqual(rows[0]["catalogue_cover_file"], "catalogue.jpg")
            self.assertIsNone(readonly.execute("SELECT name FROM sqlite_master WHERE name='game_cover_overrides'").fetchone())
        finally:
            readonly.close()

    def test_readonly_snapshot_can_read_existing_overrides(self):
        import sqlite3
        from pathlib import Path
        database = os.path.join(self.directory, "new-snapshot.db")
        conn = slg_db.connect(database)
        gid = slg_db.add_user_game(conn, "Personal")
        name = library.import_cover(self.picture(), self.directory)
        library.set_cover_override(conn, gid, name)
        conn.commit()
        conn.close()
        readonly = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)
        readonly.row_factory = sqlite3.Row
        try:
            readonly.execute("PRAGMA query_only=ON")
            with patch.object(slg_db, "covers_dir", return_value=self.directory):
                self.assertEqual(slg_db.get_game(readonly, gid)["cover_file"], name)
        finally:
            readonly.close()

    def test_cover_decoration_default_copies_but_in_place_reuses_rows(self):
        rows = self.rows()
        copied = library.apply_cover_overrides(self.conn, rows)
        self.assertIsNot(copied, rows)
        self.assertNotIn("has_cover_override", rows[0])
        decorated = library.apply_cover_overrides(self.conn, rows, in_place=True)
        self.assertIs(decorated, rows)
        self.assertFalse(rows[0]["has_cover_override"])
        self.assertEqual(rows[0]["catalogue_cover_file"], "catalogue.jpg")


if __name__ == "__main__":
    unittest.main()
