"""Multi-target configuration and one-run identification regressions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import slg_db
import slg_scan
import slg_scan_targets as targets


class ScanTargetsTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def game(self, slug, title):
        gid, _ = slg_db.upsert_game(self.conn, slug, "https://example.invalid/" + slug, title)
        self.conn.commit()
        return gid

    def scan(self, configured, **kwargs):
        return slg_scan.scan(self.conn, targets=configured, log=lambda _: None, **kwargs)

    def test_missing_new_preference_migrates_legacy_once(self):
        legacy = str(self.root / "offline")
        slg_db.set_pref(self.conn, "scan_root", legacy)
        expected = [{"path": legacy, "mode": "children"}]
        self.assertEqual(targets.load_targets(self.conn), expected)
        self.assertEqual(json.loads(slg_db.get_pref(self.conn, targets.PREF_TARGETS)), expected)
        slg_db.set_pref(self.conn, "scan_root", str(self.root / "different"))
        self.assertEqual(targets.load_targets(self.conn), expected)

    def test_empty_configuration_never_resurrects_legacy(self):
        slg_db.set_pref(self.conn, "scan_root", str(self.root))
        targets.save_targets(self.conn, [])
        self.assertEqual(targets.load_targets(self.conn), [])
        self.assertEqual(self.scan([])["items"], [])

    def test_missing_roots_survive_then_work_when_connected(self):
        path = self.root / "offline"
        configured = targets.save_targets(self.conn, [{"path": str(path)}])
        self.assertEqual(self.scan(configured)["errors"][0]["kind"], "FileNotFoundError")
        path.mkdir()
        (path / "Unique Title").mkdir()
        self.game("unique", "Unique Title")
        self.assertEqual(self.scan(targets.load_targets(self.conn))["matched"], 1)

    def test_configuration_duplicates_first_mode_wins(self):
        got = targets.normalize_targets([
            {"path": str(self.root), "mode": "single"},
            {"path": str(self.root / "."), "mode": "children"}])
        self.assertEqual(got, [{"path": str(self.root), "mode": "single"}])

    def test_bad_config_is_not_overwritten_or_migrated(self):
        slg_db.set_pref(self.conn, "scan_root", str(self.root))
        slg_db.set_pref(self.conn, targets.PREF_TARGETS, "not json")
        with self.assertRaises(ValueError):
            targets.load_targets(self.conn)
        self.assertEqual(slg_db.get_pref(self.conn, targets.PREF_TARGETS), "not json")
        for value in (None, "path", [{}], [{"path": ""}], [{"path": "x", "mode": "recursive"}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                targets.save_targets(self.conn, value)

    def test_mixed_modes_use_one_index_and_do_not_recurse(self):
        many = self.root / "many"
        many.mkdir()
        (many / "First Title").mkdir()
        (many / "First Title" / "Ignored Nested").mkdir()
        single = self.root / "Second Title"
        single.mkdir()
        (single / "Also Ignored").mkdir()
        self.game("first", "First Title")
        self.game("second", "Second Title")
        with mock.patch.object(slg_scan, "_title_index", wraps=slg_scan._title_index) as index:
            result = self.scan([{"path": str(many)}, {"path": str(single), "mode": "single"}], apply=False)
        self.assertEqual(index.call_count, 1)
        self.assertEqual(result["matched"], 2)
        self.assertEqual({x["name"] for x in result["items"]}, {"First Title", "Second Title"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_parent_children_and_single_child_identify_once(self):
        child = self.root / "Unique Title"
        child.mkdir()
        self.game("unique", "Unique Title")
        result = self.scan([{"path": str(self.root)}, {"path": str(child), "mode": "single"}])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["matched"], 1)

    def test_duplicate_game_across_targets_is_conflict(self):
        gid = self.game("unique", "Unique Title")
        first = self.root / "one" / "Unique Title"
        second = self.root / "two" / "Unique Title"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        result = self.scan([{"path": str(first), "mode": "single"},
                            {"path": str(second), "mode": "single"}])
        self.assertEqual([x["status"] for x in result["items"]], ["matched", "conflict"])
        self.assertEqual(result["items"][1]["reason"], "existing_other_path")
        row = self.conn.execute("SELECT * FROM local WHERE game_id=?", (gid,)).fetchone()
        self.assertEqual(row["folder_path"], str(first))

    def test_namesakes_stay_ambiguous_in_mixed_targets(self):
        self.game("western", "Shared Title")
        self.game("dlsite-rj123456", "Shared Title")
        folder = self.root / "Shared Title"
        folder.mkdir()
        result = self.scan([{"path": str(folder), "mode": "single"}])
        self.assertEqual(result["items"][0]["status"], "ambiguous")
        self.assertEqual(len(result["items"][0]["candidates"]), 2)
        self.assertEqual(result["matched"], 0)

    def test_failed_target_does_not_abort_successful_target(self):
        locked = self.root / "locked"
        locked.mkdir()
        good = self.root / "Unique Title"
        good.mkdir()
        self.game("unique", "Unique Title")
        real_listdir = os.scandir

        def listing(path):
            if Path(path) == locked:
                raise PermissionError("locked")
            return real_listdir(path)

        with mock.patch.object(slg_scan.os, "scandir", side_effect=listing):
            result = self.scan([{"path": str(locked)}, {"path": str(good), "mode": "single"}])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["errors"][0]["kind"], "PermissionError")

    def test_cancel_between_targets_never_applies_first_match(self):
        first, second = self.root / "First Title", self.root / "Second Title"
        first.mkdir()
        second.mkdir()
        self.game("first", "First Title")
        self.game("second", "Second Title")
        stopped = [False]
        result = self.scan([{"path": str(first), "mode": "single"},
                            {"path": str(second), "mode": "single"}],
                           on_progress=lambda *_: stopped.__setitem__(0, True),
                           should_stop=lambda: stopped[0])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["matched"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_invalid_target_fails_before_database_mutation(self):
        with self.assertRaises(ValueError):
            self.scan([{"path": str(self.root), "mode": "recursive"}])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_explicit_targets_override_legacy_arguments(self):
        folder = self.root / "Unique Title"
        folder.mkdir()
        self.game("unique", "Unique Title")
        result = self.scan([{"path": str(folder), "mode": "single"}],
                           roots=["missing-legacy-root"], mode="unused", apply=False)
        self.assertEqual(result["matched"], 1)

    def test_multiple_executables_are_candidates_only(self):
        folder = self.root / "Unique Title"
        folder.mkdir()
        self.game("unique", "Unique Title")
        for name in ("Main.exe", "Launcher.exe", "unins000.exe"):
            (folder / name).write_bytes(b"not a real executable")
        result = self.scan([{"path": str(folder), "mode": "single"}], apply=False)
        self.assertEqual({Path(x).name for x in result["items"][0]["executables"]},
                         {"Main.exe", "Launcher.exe"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
