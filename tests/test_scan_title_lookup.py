"""Focused regression tests for scan log-title lookup."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_scan  # noqa: E402


class ScanTitleLookupTests(unittest.TestCase):
    def test_lookup_keeps_first_title_and_missing_id_has_fallback(self):
        index = [
            ("normalized-title", 7, "Catalogue Title"),
            ("translation", 7, "Translated Title"),
            ("alias", 8, "Alias Only Title"),
        ]

        titles = slg_scan._title_index_titles(index)

        self.assertEqual(titles, {7: "Catalogue Title", 8: "Alias Only Title"})
        self.assertEqual(titles.get(404, "?"), "?")

    def test_scan_logs_titles_for_multiple_matches_using_one_map(self):
        conn = slg_db.connect(":memory:")
        self.addCleanup(conn.close)
        game_id, _ = slg_db.upsert_game(
            conn, "display-title", "https://example.invalid/game", "Display Title")
        logs = []

        with tempfile.TemporaryDirectory() as root:
            for folder_name in ("Display Title v1.0", "Display Title v2.0"):
                os.makedirs(os.path.join(root, folder_name))

            with mock.patch.object(
                    slg_scan, "_title_index_titles",
                    wraps=slg_scan._title_index_titles) as build_title_map:
                result = slg_scan.scan(conn, roots=[root], log=logs.append)

        self.assertEqual(result, {"matched": 2, "unmatched": []})
        self.assertEqual(build_title_map.call_count, 1)
        matching_logs = [line for line in logs if line.endswith("#%d Display Title" % game_id)]
        self.assertEqual(len(matching_logs), 2)


if __name__ == "__main__":
    unittest.main()
