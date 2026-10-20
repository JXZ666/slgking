"""Regressions for the DLC/kana feedback, using only synthetic local data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import slg_db
import slg_scan


FEEDBACK_FOLDER = "[SLG] 满车率300%2+DLC1+2 DL官方中文"


class FeedbackMatchingTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def game(self, slug, title, rj=None):
        gid, _ = slg_db.upsert_game(
            self.conn, slug, "https://example.invalid/" + slug, title)
        if rj:
            self.conn.execute(
                "INSERT INTO game_sources(source_id,external_id,game_id,fetched_at) "
                "VALUES('dlsite',?,?,datetime('now'))", (rj, gid))
        self.conn.commit()
        return gid

    def scan_folder(self, name, *, configuration=None):
        folder = self.root / name
        folder.mkdir()
        (folder / "Game.exe").touch()
        if configuration:
            (folder / "game").mkdir()
            (folder / "game/options.rpy").write_text(
                'define config.name = "' + configuration + '"', encoding="utf-8")
        with patch("slg_scan_evidence.windows_executable_titles", return_value=[]), \
                patch("subprocess.Popen", side_effect=AssertionError("game executed")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            result = slg_scan.scan(self.conn, roots=[self.root], log=lambda _: None)
        return folder, result["items"][0]

    def test_original_feedback_does_not_match_unrelated_japanese_dlc(self):
        self.game("unrelated", "にゃくろのみこん! DLC")
        self.assertEqual(slg_scan.match_candidates(self.conn, FEEDBACK_FOLDER), [])
        self.assertIsNone(slg_scan.match_game(self.conn, FEEDBACK_FOLDER))

    def test_original_feedback_scan_is_unmatched_and_does_not_bind(self):
        self.game("unrelated", "にゃくろのみこん! DLC")
        _, item = self.scan_folder(FEEDBACK_FOLDER)
        self.assertIsNone(item["game_id"])
        self.assertEqual(item["candidates"], [])
        self.assertEqual(item["status"], "unmatched")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_kana_survives_and_distinct_kana_titles_do_not_collapse(self):
        first = self.game("kana-one", "あいうえお")
        self.game("kana-two", "かきくけこ")
        self.assertEqual(slg_scan._key("あいうえお"), "あいうえお")
        self.assertEqual(slg_scan.match_game(self.conn, "あいうえお"), first)

    def test_fullwidth_and_halfwidth_letters_share_a_key(self):
        gid = self.game("fullwidth", "カタカナ ＤＬＣ")
        self.assertEqual(slg_scan.match_game(self.conn, "ｶﾀｶﾅ DLC"), gid)
        self.assertEqual(slg_scan._key("Ｓｔｒａße"), "strasse")

    def test_real_dlc_is_retained_as_distinct_from_base_game(self):
        base = self.game("base", "にゃくろのみこん!")
        dlc = self.game("dlc", "にゃくろのみこん! DLC")
        self.assertEqual(slg_scan.match_game(self.conn, "にゃくろのみこん!"), base)
        self.assertEqual(slg_scan.match_game(self.conn, "にゃくろのみこん! DLC"), dlc)
        self.assertTrue(slg_scan._key("にゃくろのみこん! DLC").endswith("dlc"))

    def test_short_titles_remain_exact_matches(self):
        for n, title in enumerate(("愛", "恋愛", "AI", "DLC", "あ")):
            with self.subTest(title=title):
                gid = self.game("short-" + str(n), title)
                self.assertEqual(slg_scan.match_game(self.conn, title), gid)

    def test_short_query_cannot_contain_a_long_title(self):
        self.game("long-ascii", "DLC Adventure")
        self.assertEqual(slg_scan.match_candidates(self.conn, "DLC"), [])

    def test_short_catalogue_suffix_cannot_match_long_chinese_query(self):
        self.game("generic", "DLC")
        self.assertEqual(slg_scan.match_candidates(self.conn, FEEDBACK_FOLDER), [])

    def test_short_chinese_substring_requires_an_exact_match(self):
        self.game("short-cn", "恋愛")
        self.assertEqual(slg_scan.match_candidates(self.conn, "恋愛的世界"), [])
        self.assertEqual(slg_scan.match_candidates(self.conn, "恋愛劇"), [])

    def test_meaningful_containment_is_a_candidate_not_a_silent_binding(self):
        gid = self.game("contained", "Adventure")
        _, item = self.scan_folder("Long Adventure Package")
        self.assertEqual(item["status"], "ambiguous")
        self.assertEqual([row["game_id"] for row in item["candidates"]], [gid])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_exact_cross_source_namesakes_remain_ambiguous(self):
        first = self.game("western", "Shared Story")
        second = self.game("dlsite", "Shared Story", "RJ00123456")
        _, item = self.scan_folder("Shared Story")
        self.assertEqual(item["status"], "ambiguous")
        self.assertEqual({row["game_id"] for row in item["candidates"]}, {first, second})
        self.assertIsNone(item["game_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_known_product_id_wins_over_name(self):
        known = self.game("known", "Other Product", "RJ00123456")
        self.game("namesake", "Shared Story")
        candidates = slg_scan.match_candidates(self.conn, "Shared Story [RJ00123456]")
        self.assertEqual([row["game_id"] for row in candidates], [known])
        self.assertEqual(candidates[0]["reason"], "product_id")

    def test_unknown_product_id_does_not_fallback_to_an_exact_name(self):
        self.game("namesake", "Shared Story")
        self.assertEqual(slg_scan.match_candidates(
            self.conn, "Shared Story [RJ99887766]"), [])

    def test_alias_and_translation_are_kept(self):
        gid = self.game("source-name", "Original Story")
        slg_db.add_alias(self.conn, gid, "あいうえお")
        slg_db.set_manual_translation(self.conn, "title", gid, "多娜多娜")
        self.conn.commit()
        self.assertEqual(slg_scan.match_game(self.conn, "あいうえお"), gid)
        self.assertEqual(slg_scan.match_game(self.conn, "多娜多娜"), gid)

    def test_metadata_conflict_still_requires_confirmation(self):
        folder_title = self.game("folder-title", "First Story")
        config_title = self.game("config-title", "Second Story")
        _, item = self.scan_folder("First Story", configuration="Second Story")
        self.assertEqual(item["status"], "ambiguous")
        self.assertEqual({row["game_id"] for row in item["candidates"]},
                         {folder_title, config_title})
        self.assertIsNone(item["game_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM local").fetchone()[0], 0)

    def test_previously_confirmed_binding_is_not_rewritten(self):
        gid = self.game("historical", "にゃくろのみこん! DLC")
        folder = self.root / FEEDBACK_FOLDER
        folder.mkdir()
        (folder / "Game.exe").touch()
        slg_db.set_local_folder(self.conn, gid, str(folder), "confirmed-version")
        self.conn.execute("UPDATE state SET status=? WHERE game_id=?", ("completed", gid))
        self.conn.commit()
        with patch("slg_scan_evidence.windows_executable_titles", return_value=[]):
            item = slg_scan.scan(
                self.conn, roots=[self.root], apply=False, log=lambda _: None)["items"][0]
        self.assertEqual(item["game_id"], gid)
        self.assertEqual(item["match_method"], "existing_path")
        self.assertEqual(self.conn.execute(
            "SELECT folder_version FROM local WHERE game_id=?", (gid,)).fetchone()[0],
                         "confirmed-version")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM state WHERE game_id=?", (gid,)).fetchone()[0], "completed")


if __name__ == "__main__":
    unittest.main()
