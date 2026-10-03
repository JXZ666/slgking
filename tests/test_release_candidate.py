"""Stable release identity and additive shipped labels."""
from pathlib import Path
import unittest
from unittest.mock import patch

import slg_db
import slg_gui
import slg_update


class ReleaseCandidate(unittest.TestCase):
    def test_stable_and_test_channels_share_version_but_keep_identity(self):
        for executable, test in (("slgking.exe", False), ("slgking_test.exe", True)):
            with self.subTest(executable=executable), \
                    patch.object(slg_gui.sys, "frozen", True, create=True), \
                    patch.object(slg_gui.sys, "executable", executable):
                self.assertEqual(slg_gui.display_app_version(), "0.25.0")
                self.assertEqual(slg_gui.is_test_build(), test)
                notes = slg_gui.release_notes_text()
                self.assertIn("测试版" if test else "稳定版", notes)
                self.assertIn("多选分类", notes)
                self.assertIn("不会随账号同步", notes)

    def test_formal_update_asset_matches_and_test_asset_stays_separate(self):
        assets = [{"name": "slgking.exe", "browser_download_url": "https://example.com/slgking.exe"}]
        self.assertTrue(slg_update.is_newer("v0.25.0", "0.24.5"))
        with patch.object(slg_update.sys, "executable", "slgking.exe"):
            self.assertEqual(slg_update._match_asset(assets), assets[0]["browser_download_url"])
        with patch.object(slg_update.sys, "executable", "slgking_test.exe"):
            self.assertIsNone(slg_update._match_asset(assets))

    def test_new_labels_fill_gaps_without_overwriting_user_translation(self):
        conn = slg_db.connect(":memory:")
        self.addCleanup(conn.close)
        slg_db.set_manual_translation(conn, "tag", "Dating Sim", "我的译名")
        seed = Path(__file__).resolve().parents[1] / "assets" / "tag_zh.json"
        self.assertGreater(slg_db.import_seed_tag_translations(conn, seed), 0)
        labels = slg_db.tag_translations(conn)
        self.assertEqual(labels["Dating Sim"], "我的译名")
        self.assertEqual(labels["animated-big-ass"], "动态CG·巨臀")
        self.assertEqual(slg_db.import_seed_tag_translations(conn, seed), 0)
