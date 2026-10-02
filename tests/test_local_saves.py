"""The local save inventory: the table, and the scan that fills it.

Both halves run without a window on purpose - the scan walks the filesystem and
writes rows, and nothing about it needs Tk. The inventory window itself is
covered in test_ui_layout.py, where the suite's single App already exists.

Run with:

    python -m unittest tests.test_local_saves
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

# Point the app at a throwaway directory before anything reads db_path(), the
# same precaution test_ui_layout.py takes.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="slgsaves-")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_scan  # noqa: E402


def _touch(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)


class SaveTable(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_upsert_replaces_the_row_for_the_same_path(self):
        slg_db.set_local_save(self.conn, r"C:\g\saves", save_count=1,
                              total_size=10)
        slg_db.set_local_save(self.conn, r"C:\g\saves", save_count=7,
                              total_size=70, engine="Ren'Py")
        rows = slg_db.list_local_saves(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["save_count"], 7)
        self.assertEqual(rows[0]["total_size"], 70)
        self.assertEqual(rows[0]["engine"], "Ren'Py")

    def test_two_paths_stay_separate(self):
        slg_db.set_local_save(self.conn, r"C:\a", total_size=1)
        slg_db.set_local_save(self.conn, r"C:\b", total_size=2)
        self.assertEqual(len(slg_db.list_local_saves(self.conn)), 2)

    def test_list_is_largest_first(self):
        slg_db.set_local_save(self.conn, r"C:\small", total_size=10)
        slg_db.set_local_save(self.conn, r"C:\big", total_size=900)
        rows = slg_db.list_local_saves(self.conn)
        self.assertEqual([row["path"] for row in rows], [r"C:\big", r"C:\small"])

    def test_catalogue_title_beats_the_scan_label(self):
        self.conn.execute(
            "INSERT INTO games (slug,url,title,first_seen)"
            " VALUES ('sluggy','http://x','Catalogue Name','2026-01-01')")
        slg_db.set_local_save(self.conn, r"C:\g", game_slug="sluggy",
                              display_title="Folder Name")
        self.assertEqual(slg_db.list_local_saves(self.conn)[0]["title"],
                         "Catalogue Name")

    def test_display_title_is_the_fallback_for_an_unmatched_folder(self):
        slg_db.set_local_save(self.conn, r"C:\g", display_title="Folder Name")
        self.assertEqual(slg_db.list_local_saves(self.conn)[0]["title"],
                         "Folder Name")

    def test_clear_empties_the_table(self):
        slg_db.set_local_save(self.conn, r"C:\a")
        slg_db.set_local_save(self.conn, r"C:\b")
        slg_db.clear_local_saves(self.conn)
        self.assertEqual(slg_db.list_local_saves(self.conn), [])

    def test_the_table_is_not_part_of_the_portable_export(self):
        # A save path is machine-specific and a re-scan rebuilds it for free,
        # so it must stay out of the export the way `local` is kept out of it.
        self.assertNotIn("local_saves", slg_db._BACKUP_TABLES)


class SaveScan(unittest.TestCase):
    """scan_saves against a fabricated machine.

    APPDATA and USERPROFILE are redirected into a temp tree so the assertions
    describe the fixture rather than whatever save files happen to exist on the
    machine running the suite.
    """

    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix="slgscan-")
        self.appdata = os.path.join(self.tree, "AppData", "Roaming")
        self.profile = os.path.join(self.tree, "Home")
        os.makedirs(self.appdata)
        os.makedirs(self.profile)
        env = {"APPDATA": self.appdata, "USERPROFILE": self.profile}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _install(self, name, saves, size=100):
        folder = os.path.join(self.tree, name)
        os.makedirs(os.path.join(folder, "renpy"))
        save_dir = os.path.join(folder, "game", "saves")
        for index in range(saves):
            _touch(os.path.join(save_dir, "slot%d.save" % index), size)
        return folder

    def _bind(self, folder, slug="bound", title="Bound Game"):
        self.conn.execute(
            "INSERT INTO games (slug,url,title,first_seen) VALUES (?,?,?,?)",
            (slug, "http://x", title, "2026-01-01"))
        game_id = self.conn.execute(
            "SELECT id FROM games WHERE slug = ?", (slug,)).fetchone()["id"]
        self.conn.execute(
            "INSERT INTO local (game_id, folder_path) VALUES (?,?)",
            (game_id, folder))
        self.conn.commit()

    def test_counts_size_and_match_for_a_bound_folder(self):
        folder = self._install("Bound-v1.0", saves=3, size=100)
        self._bind(folder)
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["bytes"], 300)
        row = slg_db.list_local_saves(self.conn)[0]
        self.assertEqual(row["save_count"], 3)
        self.assertEqual(row["total_size"], 300)
        self.assertEqual(row["title"], "Bound Game")
        self.assertEqual(row["engine"], "Ren'Py")
        self.assertEqual(row["source_kind"], "install")
        self.assertIsNotNone(row["last_modified"])

    def test_sweeps_a_system_location_the_user_never_bound(self):
        product = os.path.join(self.profile, "AppData", "LocalLow",
                               "SomeCo", "SomeProduct")
        _touch(os.path.join(product, "save1.dat"), 50)
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(result["count"], 1)
        row = slg_db.list_local_saves(self.conn)[0]
        self.assertEqual(row["engine"], "Unity")
        self.assertEqual(row["source_kind"], "system")
        self.assertEqual(row["total_size"], 50)

    def test_a_rescan_replaces_rather_than_accumulates(self):
        folder = self._install("Bound-v1.0", saves=2)
        self._bind(folder)
        slg_scan.scan_saves(self.conn, log=lambda _m: None)
        slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(len(slg_db.list_local_saves(self.conn)), 1)

    def test_an_empty_save_folder_is_skipped(self):
        folder = os.path.join(self.tree, "Empty-v1.0")
        os.makedirs(os.path.join(folder, "renpy"))
        os.makedirs(os.path.join(folder, "game", "saves"))
        self._bind(folder)
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(result["count"], 0)
        self.assertEqual(slg_db.list_local_saves(self.conn), [])

    def test_renpy_housekeeping_folders_are_not_games(self):
        renpy = os.path.join(self.appdata, "RenPy")
        _touch(os.path.join(renpy, "backups", "old.save"), 10)
        _touch(os.path.join(renpy, "tokens", "token"), 10)
        _touch(os.path.join(renpy, "RealGame-123", "save.save"), 10)
        slg_scan.scan_saves(self.conn, log=lambda _m: None)
        titles = [row["path"] for row in slg_db.list_local_saves(self.conn)]
        self.assertEqual(len(titles), 1)
        self.assertIn("RealGame-123", titles[0])

    def test_progress_reports_each_bound_folder(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        seen = []
        slg_scan.scan_saves(self.conn, log=lambda _m: None,
                            on_progress=seen.append)
        self.assertEqual(seen, ["Bound Game"])

    def test_should_stop_halts_before_anything_is_written(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None,
                                     should_stop=lambda: True)
        self.assertEqual(result["count"], 0)
        self.assertEqual(slg_db.list_local_saves(self.conn), [])

    def test_removed_and_empty_directories_do_not_leave_stale_rows(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        slg_scan.scan_saves(self.conn, log=lambda _m: None)
        os.remove(os.path.join(folder, "game", "saves", "slot0.save"))
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(result["count"], 0)
        self.assertEqual(slg_db.list_local_saves(self.conn), [])
        slg_db.set_local_save(self.conn, os.path.join(folder, "gone"), total_size=33)
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(result["bytes"], 0)
        self.assertEqual(slg_db.list_local_saves(self.conn), [])

    def test_moved_save_directory_replaces_previous_path(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        slg_scan.scan_saves(self.conn, log=lambda _m: None)
        os.rename(os.path.join(folder, "game", "saves"), os.path.join(folder, "saves"))
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        rows = slg_db.list_local_saves(self.conn)
        self.assertEqual(result["count"], 1)
        self.assertEqual(rows[0]["path"], os.path.join(folder, "saves"))

    def test_deep_file_cancellation_keeps_previous_complete_inventory(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        slg_db.set_local_save(self.conn, "previous", total_size=99)
        self.conn.commit()
        _touch(os.path.join(folder, "game", "saves", "deep", "nested", "stop.save"), 40)
        stopped = [False]
        real_stat = os.stat

        def cancelling_stat(path, *args, **kwargs):
            value = real_stat(path, *args, **kwargs)
            if str(path).endswith("stop.save"):
                stopped[0] = True
            return value

        messages = []
        with mock.patch.object(slg_scan.os, "stat", side_effect=cancelling_stat):
            result = slg_scan.scan_saves(self.conn, log=messages.append,
                                         should_stop=lambda: stopped[0])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["bytes"], 99)
        self.assertEqual(slg_db.list_local_saves(self.conn)[0]["path"], "previous")
        self.assertFalse(any("完成" in message for message in messages))

    def test_cancel_during_system_enumeration_keeps_inventory(self):
        _touch(os.path.join(self.appdata, "RenPy", "StopGame", "s.save"), 10)
        slg_db.set_local_save(self.conn, "previous", total_size=99)
        stopped = [False]
        real_listdir = os.listdir

        def cancelling_listing(path):
            result = real_listdir(path)
            if str(path).endswith("RenPy"):
                stopped[0] = True
            return result

        with mock.patch.object(slg_scan.os, "listdir", side_effect=cancelling_listing):
            result = slg_scan.scan_saves(self.conn, log=lambda _m: None,
                                         should_stop=lambda: stopped[0])
        self.assertTrue(result["cancelled"])
        self.assertEqual(slg_db.list_local_saves(self.conn)[0]["path"], "previous")

    def test_unreadable_directory_retains_old_row_and_updates_successful_scope(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        save_path = os.path.join(folder, "game", "saves")
        slg_db.set_local_save(self.conn, save_path, total_size=77, save_count=2)
        product = os.path.join(self.profile, "AppData", "LocalLow", "Co", "Product")
        _touch(os.path.join(product, "cache.log"), 30)
        real_stats = slg_scan._dir_stats

        def denied(path, should_stop=None):
            if path == save_path:
                raise PermissionError("access denied")
            return real_stats(path, should_stop)

        with mock.patch.object(slg_scan, "_dir_stats", side_effect=denied):
            result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["preserved_count"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["bytes"], 107)
        self.assertEqual(result["count"], len(slg_db.list_local_saves(self.conn)))
        self.assertEqual(result["game_data_count"], 1)

    def test_unreadable_system_parent_retains_rows_within_scope_only(self):
        renpy = os.path.join(self.appdata, "RenPy")
        old_path = os.path.join(renpy, "OldGame")
        slg_db.set_local_save(self.conn, old_path, total_size=77)
        slg_db.set_local_save(self.conn, os.path.join(self.tree, "gone"), total_size=66)
        real_listing = os.listdir

        def denied(path):
            if path == renpy:
                raise PermissionError("access denied")
            return real_listing(path)

        with mock.patch.object(slg_scan.os, "listdir", side_effect=denied):
            result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(result["preserved_count"], 1)
        self.assertEqual(result["bytes"], 77)
        self.assertEqual(slg_db.list_local_saves(self.conn)[0]["path"], old_path)

    def test_database_write_failure_restores_previous_inventory(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        slg_db.set_local_save(self.conn, "previous", total_size=99)
        self.conn.commit()
        with mock.patch.object(slg_db, "set_local_save", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(slg_db.list_local_saves(self.conn)[0]["path"], "previous")

    def test_unexpected_collection_error_does_not_publish_partial_rows(self):
        folder = self._install("Bound-v1.0", saves=1)
        self._bind(folder)
        slg_db.set_local_save(self.conn, "previous", total_size=99)
        with mock.patch.object(slg_scan, "_system_save_dirs", side_effect=RuntimeError("unexpected")):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(slg_db.list_local_saves(self.conn)[0]["path"], "previous")

    def test_unity_product_counts_game_data_including_cache(self):
        product = os.path.join(self.profile, "AppData", "LocalLow", "Co", "Product")
        _touch(os.path.join(product, "save.dat"), 20)
        _touch(os.path.join(product, "Cache", "log.txt"), 30)
        result = slg_scan.scan_saves(self.conn, log=lambda _m: None)
        self.assertEqual(result["game_data_count"], 1)
        self.assertEqual(result["bytes"], 50)
        row = slg_db.list_local_saves(self.conn)[0]
        self.assertEqual(row["source_kind"], "system")
        self.assertEqual(row["engine"], "Unity")
        self.assertEqual(row["save_count"], 2)


class HumanSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(slg_scan.human_size(0), "0 B")
        self.assertEqual(slg_scan.human_size(512), "512 B")
        self.assertEqual(slg_scan.human_size(1024), "1.0 KB")
        self.assertEqual(slg_scan.human_size(5 * 1024 ** 3), "5.0 GB")


if __name__ == "__main__":
    unittest.main()
