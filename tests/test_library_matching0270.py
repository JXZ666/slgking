"""Integration regressions for local evidence, explicit identities and layers."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import slg_db
import slg_library_metadata
import slg_scan


class LibraryMatching0270Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "games"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "fixture.sqlite"
        self.conn = slg_db.connect(str(self.db))
        self.addCleanup(lambda: self.conn.close())

    def game(self, slug, title, rj=None):
        gid, _ = slg_db.upsert_game(self.conn, slug, "https://example.invalid/" + slug, title)
        if rj:
            self.conn.execute("INSERT INTO game_sources(source_id,external_id,game_id,fetched_at)"
                              " VALUES('dlsite',?,?,datetime('now'))", (rj, gid))
        self.conn.commit()
        return gid

    def folder(self, name, config=None, config_path="game/options.rpy", exe="Game.exe"):
        folder = self.root / name
        folder.mkdir(parents=True)
        (folder / exe).touch()
        if config is not None:
            path = folder / config_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(config, encoding="utf-8")
        return folder

    def scan(self, **kwargs):
        return slg_scan.scan(self.conn, roots=[str(self.root)], log=lambda _: None, **kwargs)

    def test_existing_valid_path_wins_over_different_name_evidence(self):
        original = self.game("original", "Original Adventure")
        self.game("other", "Other Adventure")
        folder = self.folder("Other Adventure", 'define config.name = "Other Adventure"')
        slg_db.set_local_folder(self.conn, original, str(folder), "original-version")
        self.conn.commit()
        item = self.scan(apply=False)["items"][0]
        self.assertEqual(item["game_id"], original)
        self.assertEqual(item["status"], "matched")
        self.assertEqual(item["match_method"], "existing_path")
        self.assertEqual(self.conn.execute("SELECT folder_version FROM local WHERE game_id=?", (original,)).fetchone()[0],
                         "original-version")

    def test_random_directory_matches_renpy_title(self):
        gid = self.game("renpy", "Exact Visual Novel")
        self.folder("Q9x-unknown", 'define config.name = _("Exact Visual Novel")')
        item = self.scan()["items"][0]
        self.assertEqual((item["game_id"], item["status"]), (gid, "matched"))
        self.assertTrue(any(ref["reason"] == "renpy_config_name" for ref in item["evidence"]["titles"]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 1)

    def test_random_directory_matches_rpgmaker_title(self):
        gid = self.game("rpg", "Magic Adventure")
        self.folder("AbC98", '{"gameTitle":"Magic Adventure"}', "www/data/System.json")
        item = self.scan(apply=False)["items"][0]
        self.assertEqual((item["game_id"], item["status"]), (gid, "matched"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_chinese_and_english_aliases_remain_valid(self):
        gid = self.game("english", "Original English Name")
        slg_db.add_alias(self.conn, gid, "中文游戏名")
        slg_db.add_alias(self.conn, gid, "Known English Alias")
        self.conn.commit()
        self.folder("Folder-A", 'define config.name = "中文游戏名"')
        first = self.scan(apply=False)["items"][0]
        self.assertEqual(first["game_id"], gid)
        (self.root / "Folder-A" / "game/options.rpy").write_text('define config.name = "Known English Alias"')
        second = self.scan(apply=False)["items"][0]
        self.assertEqual(second["game_id"], gid)

    def test_different_metadata_targets_require_confirmation(self):
        first = self.game("first", "First Product")
        second = self.game("second", "Second Product")
        folder = self.folder("Random-X", 'define config.name = "First Product"')
        (folder / "data").mkdir()
        (folder / "data/System.json").write_text('{"gameTitle":"Second Product"}')
        item = self.scan()["items"][0]
        self.assertEqual(item["status"], "ambiguous")
        self.assertEqual({c["game_id"] for c in item["candidates"]}, {first, second})
        self.assertIsNone(item["game_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_unindexed_executable_rj_blocks_title_fallback(self):
        self.game("namesake", "Shared Visual Novel")
        self.folder("Shared Visual Novel", 'define config.name = "Shared Visual Novel"', exe="RJ99887766.exe")
        item = self.scan()["items"][0]
        self.assertIsNone(item["game_id"])
        self.assertFalse(item["candidates"])
        self.assertTrue(item["identity_warning"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_bound_path_unknown_file_identity_requires_confirmation(self):
        gid = self.game("registered", "Previously Confirmed")
        folder = self.folder("changed-package", exe="RJ99887766.exe")
        slg_db.set_local_folder(self.conn, gid, str(folder), "confirmed-version")
        self.conn.commit()
        item = self.scan()["items"][0]
        self.assertEqual(item["status"], "ambiguous")
        self.assertTrue(item["identity_warning"])
        self.assertEqual(self.conn.execute("SELECT folder_version FROM local WHERE game_id=?", (gid,)).fetchone()[0],
                         "confirmed-version")

    def test_confirmed_personal_dlsite_identity_survives_reopen(self):
        gid = slg_library_metadata.confirm(self.conn, {
            "source": "dlsite", "external_id": "RJ99887766", "title": "New Unlisted Product",
            "source_url": "https://www.dlsite.com/maniax/work/=/product_id/RJ99887766.html"})
        self.conn.commit()
        self.conn.close()
        self.conn = slg_db.connect(str(self.db))
        self.folder("Renamed [RJ99887766]")
        item = self.scan(apply=False)["items"][0]
        self.assertEqual(item["game_id"], gid)
        self.assertEqual(item["status"], "matched")
        self.assertEqual(self.conn.execute("SELECT origin FROM games WHERE id=?", (gid,)).fetchone()[0], "user")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM game_sources WHERE game_id=?", (gid,)).fetchone()[0], 0)

    def test_first_and_second_layer_are_separate_scan_schemes(self):
        gid = self.game("nested", "Nested Product")
        self.folder("Category/Nested Product")
        first = self.scan(apply=False)["items"]
        self.assertEqual([item["name"] for item in first], ["Category"])
        self.assertTrue(all(item["game_id"] is None for item in first))
        second = self.scan(apply=False, targets=[{"path": str(self.root), "mode": "children", "depth": 2}])["items"]
        self.assertEqual([item["name"] for item in second], ["Nested Product"])
        self.assertEqual(second[0]["game_id"], gid)

    def test_cancellation_does_not_apply_partial_matches(self):
        self.game("one", "One Product")
        self.game("two", "Two Product")
        self.folder("One Product")
        self.folder("Two Product")
        stopped = [False]
        result = self.scan(on_progress=lambda *_: stopped.__setitem__(0, True), should_stop=lambda: stopped[0])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["matched"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_offline_scan_neither_networks_nor_executes(self):
        gid = self.game("offline", "Offline Product")
        self.folder("random-name", 'define config.name = "Offline Product"')
        with patch("subprocess.Popen", side_effect=AssertionError("game executed")), \
                patch("os.system", side_effect=AssertionError("shell executed")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            item = self.scan(apply=False)["items"][0]
        self.assertEqual(item["game_id"], gid)


if __name__ == "__main__":
    unittest.main()
