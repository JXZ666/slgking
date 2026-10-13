"""Library identification must never silently overwrite user bindings."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import slg_db
import slg_scan


class LibraryScanTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def game(self, slug, title):
        gid, _ = slg_db.upsert_game(self.conn, slug, "https://example.invalid/" + slug, title)
        self.conn.commit()
        return gid

    def scan(self, **kw):
        return slg_scan.scan(self.conn, roots=[str(self.root)], log=lambda _: None, **kw)

    def test_namesake_requires_confirmation(self):
        western = self.game("western", "Shared Title")
        dlsite = self.game("dlsite-rj123456", "Shared Title")
        (self.root / "Shared Title").mkdir()
        result = self.scan()
        item = result["items"][0]
        self.assertIsNone(item["game_id"])
        self.assertEqual({x["game_id"] for x in item["candidates"]}, {western, dlsite})
        self.assertEqual(item["status"], "ambiguous")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)
        self.assertIsNone(slg_scan.match_game(self.conn, "Shared Title"))

    def test_existing_path_beats_title_and_keeps_executable(self):
        original = self.game("original", "Different Title")
        self.game("new", "Folder Title")
        folder = self.root / "Folder Title"
        folder.mkdir()
        self.conn.execute("INSERT INTO local(game_id, folder_path, exe_path) VALUES(?,?,?)",
                          (original, str(folder / ".." / "Folder Title"), "chosen.exe"))
        self.conn.commit()
        result = self.scan()
        self.assertEqual(result["items"][0]["game_id"], original)
        self.assertEqual(result["items"][0]["reason"], "existing_path")
        self.assertEqual(self.conn.execute("SELECT exe_path FROM local").fetchone()[0], "chosen.exe")

    def test_other_existing_path_is_protected(self):
        gid = self.game("game", "Folder Title")
        (self.root / "Folder Title").mkdir()
        old = str(self.root / "Manual Folder")
        self.conn.execute("INSERT INTO local(game_id, folder_path) VALUES(?,?)", (gid, old))
        self.conn.commit()
        result = self.scan()
        self.assertEqual(result["items"][0]["status"], "conflict")
        self.assertEqual(self.conn.execute("SELECT folder_path FROM local").fetchone()[0], old)

    def test_preview_single_full_path_and_multiple_executables(self):
        self.game("game", self.root.name)
        for name in ("game.exe", "Launcher.EXE", "unins000.exe", "UnityCrashHandler64.exe"):
            (self.root / name).touch()
        result = self.scan(mode="single", apply=False)
        item = result["items"][0]
        self.assertEqual(item["path"], str(self.root))
        self.assertEqual({Path(x).name for x in item["executables"]}, {"game.exe", "Launcher.EXE"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_repeat_roots_and_repeat_scan_do_not_duplicate(self):
        self.game("game", "Unique Title")
        folder = self.root / "Unique Title"
        folder.mkdir()
        result = slg_scan.scan(self.conn, roots=[str(self.root), str(self.root / ".")], log=lambda _: None)
        self.assertEqual(len(result["items"]), 1)
        self.scan()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 1)

    def test_cancel_after_progress_has_no_partial_writes(self):
        self.game("one", "Unique One")
        self.game("two", "Unique Two")
        for name in ("Unique One", "Unique Two"):
            (self.root / name).mkdir()
        stopped = [False]
        result = self.scan(on_progress=lambda *_: stopped.__setitem__(0, True),
                           should_stop=lambda: stopped[0])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["matched"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_permission_and_missing_root_report_failures(self):
        with mock.patch.object(slg_scan.os, "scandir", side_effect=PermissionError("locked")):
            result = self.scan()
        self.assertEqual(result["errors"][0]["kind"], "PermissionError")
        self.assertEqual(result["errors"][0]["path"], str(self.root))
        result = slg_scan.scan(self.conn, roots=[str(self.root / "missing")], log=lambda _: None)
        self.assertEqual(result["errors"][0]["kind"], "FileNotFoundError")

    def test_cancellation_during_apply_rolls_back_all_bindings(self):
        self.game("game", "Unique Title")
        (self.root / "Unique Title").mkdir()
        stopped = [False]

        def trace(sql):
            if sql.startswith("INSERT INTO local"):
                stopped[0] = True

        self.conn.set_trace_callback(trace)
        result = self.scan(should_stop=lambda: stopped[0])
        self.conn.set_trace_callback(None)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM state").fetchone()[0], 0)

    def test_aliases_deduplicate_and_personal_status_survives(self):
        gid = self.game("game", "Unique Title")
        self.conn.execute("INSERT INTO game_aliases(game_id, alias) VALUES(?,?)",
                          (gid, "UniqueTitle"))
        self.conn.execute("INSERT INTO state(game_id, status) VALUES(?,?)", (gid, "completed"))
        self.conn.commit()
        (self.root / "Unique Title").mkdir()
        result = self.scan()
        self.assertEqual(len(result["items"][0]["candidates"]), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM state").fetchone()[0], "completed")


if __name__ == "__main__":
    unittest.main()
