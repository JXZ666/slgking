"""Local-folder matching: CJK, aliases, noise and version stripping.

Run with:

    python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_scan  # noqa: E402


class Key(unittest.TestCase):
    def test_cjk_survives(self):
        self.assertEqual(slg_scan._key("多娜多娜"), "多娜多娜")

    def test_cjk_punctuation_stripped(self):
        self.assertEqual(slg_scan._key("多娜多娜（官中）"), "多娜多娜官中")

    def test_ascii_still_stripped(self):
        self.assertEqual(slg_scan._key("Couples: Lustbound"), "coupleslustbound")


class FolderTitle(unittest.TestCase):
    def test_chinese_bracket_noise(self):
        self.assertEqual(slg_scan.folder_title("多娜多娜【官中】"), "多娜多娜")

    def test_chinese_suffix(self):
        self.assertEqual(slg_scan.folder_title("多娜多娜 汉化版"), "多娜多娜")

    def test_episode_marker(self):
        self.assertEqual(slg_scan.folder_title("Life in Woodchester Ep.3"),
                         "Life in Woodchester")

    def test_year_is_kept(self):
        self.assertEqual(slg_scan.folder_title("Football Manager 2024"),
                         "Football Manager 2024")

    def test_v_bare_number(self):
        self.assertEqual(slg_scan.folder_title("Game v1"), "Game")

    def test_trailing_build_number(self):
        self.assertEqual(slg_scan.folder_title("Some Game 2"), "Some Game")


class Match(unittest.TestCase):
    def _add(self, conn, title):
        slug = title.lower().replace(" ", "-")
        return slg_db.upsert_game(conn, slug, "https://x/" + slug, title)[0]

    def test_english_exact(self):
        conn = slg_db.connect(":memory:")
        gid = self._add(conn, "ParadiseCity")
        self.assertEqual(slg_scan.match_game(conn, "ParadiseCity-0.6.23-pc"), gid)

    def test_chinese_manual_translation_matches(self):
        conn = slg_db.connect(":memory:")
        gid = self._add(conn, "Donadona")
        slg_db.set_manual_translation(conn, "title", gid, "多娜多娜")
        self.assertEqual(slg_scan.match_game(conn, "多娜多娜"), gid)

    def test_chinese_translation_with_noise(self):
        conn = slg_db.connect(":memory:")
        gid = self._add(conn, "Donadona")
        slg_db.set_manual_translation(conn, "title", gid, "多娜多娜")
        self.assertEqual(slg_scan.match_game(conn, "多娜多娜【官中】"), gid)

    def test_alias_matches(self):
        conn = slg_db.connect(":memory:")
        gid = self._add(conn, "Lust Theory")
        slg_db.add_alias(conn, gid, "LustBound")
        self.assertEqual(slg_scan.match_game(conn, "LustBound"), gid)

    def test_cjk_does_not_fuzzy_match_english(self):
        conn = slg_db.connect(":memory:")
        self._add(conn, "Donadona")
        # No Chinese translation, so the Chinese folder must not match the
        # English title through a bogus cross-alphabet fuzzy ratio.
        self.assertIsNone(slg_scan.match_game(conn, "多娜多娜"))


class Autofill(unittest.TestCase):
    def test_name_version_developer_from_folder_name(self):
        # 目录不存在时引擎返回空串，但标题/版本/开发商仍可从名字猜出。
        info = slg_scan.autofill_folder(
            os.path.join("C:", "games", "[NTRMAN] ParadiseCity v0.6.23"))
        self.assertEqual(info["title"], "ParadiseCity")
        self.assertEqual(info["version"], "0.6.23")
        self.assertEqual(info["developer"], "NTRMAN")
        self.assertEqual(info["engine"], "")

    def test_translation_group_is_not_developer(self):
        info = slg_scan.autofill_folder(
            os.path.join("C:", "games", "多娜多娜【官中】 v1.0"))
        self.assertEqual(info["title"], "多娜多娜")
        self.assertEqual(info["developer"], "")

    def test_detect_renpy(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "renpy"))
            self.assertEqual(slg_scan.detect_engine(root), "Ren'Py")

    def test_detect_unity(self):
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "UnityPlayer.dll"), "w").close()
            self.assertEqual(slg_scan.detect_engine(root), "Unity")

    def test_detect_rpg_maker(self):
        with tempfile.TemporaryDirectory() as root:
            www = os.path.join(root, "www")
            os.makedirs(www)
            open(os.path.join(www, "index.html"), "w").close()
            self.assertEqual(slg_scan.detect_engine(root), "RPG Maker")

    def test_detect_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "README.txt"), "w").close()
            self.assertEqual(slg_scan.detect_engine(root), "")


if __name__ == "__main__":
    unittest.main()
