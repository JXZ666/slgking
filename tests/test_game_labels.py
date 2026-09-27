"""Tests for shared game-name formatting."""

import sqlite3
import unittest

from slg_game_labels import format_game_label


class FormatGameLabel(unittest.TestCase):
    def test_formats_title_version_and_developer(self):
        self.assertEqual(
            format_game_label({
                "title": "Night Adventure",
                "version": "1.2",
                "developer": "Moon Studio",
            }),
            "Night Adventure · v1.2 · Moon Studio",
        )

    def test_omits_missing_version_or_developer(self):
        self.assertEqual(format_game_label({"title": "Game", "version": "1.0"}),
                         "Game · v1.0")
        self.assertEqual(format_game_label({"title": "Game", "developer": "Studio"}),
                         "Game · Studio")
        self.assertEqual(format_game_label({"title": "Game"}), "Game")

    def test_none_or_empty_title_has_safe_fallback(self):
        self.assertEqual(format_game_label(None), "未命名游戏")
        self.assertEqual(format_game_label({"title": "  ", "version": "1.0"}),
                         "未命名游戏 · v1.0")

    def test_does_not_repeat_version_already_in_title(self):
        self.assertEqual(
            format_game_label({"title": "Night Adventure v1.2", "version": "1.2"}),
            "Night Adventure v1.2",
        )
        self.assertEqual(
            format_game_label({"title": "Night Adventure (V1.2)", "version": "v1.2"}),
            "Night Adventure (V1.2)",
        )

    def test_does_not_repeat_developer_already_in_title(self):
        self.assertEqual(
            format_game_label({"title": "Night Adventure · Moon Studio",
                               "developer": "Moon Studio"}),
            "Night Adventure · Moon Studio",
        )
        self.assertEqual(
            format_game_label({"title": "夜之物语（星月制作组）", "developer": "星月制作组"}),
            "夜之物语（星月制作组）",
        )

    def test_chinese_title_with_metadata(self):
        self.assertEqual(
            format_game_label({"title": "夏日回忆", "version": "1.05", "developer": "月光社"}),
            "夏日回忆 · v1.05 · 月光社",
        )

    def test_accepts_sqlite_row(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ? AS title, ? AS version, ? AS developer",
            ("Game", "v0.9", "Studio"),
        ).fetchone()
        self.assertEqual(format_game_label(row), "Game · v0.9 · Studio")

    def test_accepts_translated_title_with_sqlite_row(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ? AS title, ? AS version, ? AS developer",
            ("Original title", "1.1", "Studio"),
        ).fetchone()
        self.assertEqual(
            format_game_label(row, title="Translated title"),
            "Translated title · v1.1 · Studio",
        )

    def test_accepts_title_string(self):
        self.assertEqual(format_game_label("Game"), "Game")


if __name__ == "__main__":
    unittest.main()
