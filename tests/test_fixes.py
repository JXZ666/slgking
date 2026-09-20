"""Regression tests for the v0.15.0 cleanup and bug fixes.

Run with:

    python -m unittest discover tests
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_scan  # noqa: E402
import slg_scrape  # noqa: E402


class SetTagsTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _game(self, slug):
        gid, _ = slg_db.upsert_game(
            self.conn, slug=slug, url="https://dikgames.com/%s/" % slug, title=slug)
        return gid

    def _names(self, gid):
        rows = self.conn.execute(
            "SELECT name FROM tags JOIN game_tags ON tags.id = game_tags.tag_id"
            " WHERE game_id = ? ORDER BY name", (gid,)).fetchall()
        return [r["name"] for r in rows]

    def test_empty_tag_list_does_not_wipe(self):
        gid = self._game("a")
        slg_db.set_tags(self.conn, gid, ["netorare", "corruption"])
        slg_db.set_tags(self.conn, gid, [])
        self.assertEqual(self._names(gid), ["corruption", "netorare"])

    def test_none_tag_list_does_not_wipe(self):
        gid = self._game("a")
        slg_db.set_tags(self.conn, gid, ["cheating"])
        slg_db.set_tags(self.conn, gid, None)
        self.assertEqual(self._names(gid), ["cheating"])

    def test_set_tags_replaces(self):
        gid = self._game("a")
        slg_db.set_tags(self.conn, gid, ["a", "b"])
        slg_db.set_tags(self.conn, gid, ["c"])
        self.assertEqual(self._names(gid), ["c"])

    def test_set_tags_deduplicates(self):
        gid = self._game("a")
        slg_db.set_tags(self.conn, gid, ["x", "x", "y"])
        self.assertEqual(self._names(gid), ["x", "y"])


class VersionTests(unittest.TestCase):
    def test_version_gt_accepts_non_string(self):
        # A future writer that stores an int must not crash the comparison.
        self.assertTrue(slg_db._version_gt(10, "0.5"))
        self.assertTrue(slg_db._version_gt(2, 1))
        self.assertFalse(slg_db._version_gt(1, 2))

    def test_version_gt_unparseable_is_false(self):
        self.assertFalse(slg_db._version_gt("x", "y"))
        self.assertFalse(slg_db._version_gt(None, "0.1"))


class SessionTests(unittest.TestCase):
    def test_session_commits_and_closes(self):
        with slg_db.session(":memory:") as conn:
            conn.execute(
                "INSERT INTO games (slug, url, title, first_seen)"
                " VALUES ('s', 'u', 't', 'now')")
        # Closed after the with-block: a fresh query raises ProgrammingError.
        with self.assertRaises(Exception):
            conn.execute("SELECT 1")


class FetchFailureTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    class _DeadFetcher:
        log = staticmethod(lambda *a: None)

        def get(self, url, **kwargs):
            return None

    def test_note_and_clear(self):
        gid, _ = slg_db.upsert_game(
            self.conn, slug="d", url="https://dikgames.com/d/", title="D")
        slg_db.note_fetch_failure(self.conn, gid)
        slg_db.note_fetch_failure(self.conn, gid)
        value = self.conn.execute(
            "SELECT fetch_failures FROM games WHERE id = ?", (gid,)).fetchone()[0]
        self.assertEqual(value, 2)
        slg_db.clear_fetch_failures(self.conn, gid)
        value = self.conn.execute(
            "SELECT fetch_failures FROM games WHERE id = ?", (gid,)).fetchone()[0]
        self.assertEqual(value, 0)

    def test_enrich_bumps_failure(self):
        gid, _ = slg_db.upsert_game(
            self.conn, slug="d", url="https://dikgames.com/d/", title="D")
        slg_scrape.enrich(self.conn, self._DeadFetcher(), limit=5)
        value = self.conn.execute(
            "SELECT fetch_failures FROM games WHERE id = ?", (gid,)).fetchone()[0]
        self.assertEqual(value, 1)

    def test_enrich_skips_three_failures(self):
        gid, _ = slg_db.upsert_game(
            self.conn, slug="d", url="https://dikgames.com/d/", title="D")
        self.conn.execute("UPDATE games SET fetch_failures = 3 WHERE id = ?", (gid,))

        class Counting(self._DeadFetcher):
            def __init__(self):
                self.calls = 0

            def get(self, url, **kwargs):
                self.calls += 1
                return None

        fetcher = Counting()
        slg_scrape.enrich(self.conn, fetcher, limit=5)
        self.assertEqual(fetcher.calls, 0)


class ScanTests(unittest.TestCase):
    def test_listdir_bad_path_is_empty(self):
        self.assertEqual(slg_scan._listdir("Z:/definitely/not/here"), [])

    def test_scan_missing_root_is_graceful(self):
        conn = slg_db.connect(":memory:")
        self.addCleanup(conn.close)
        result = slg_scan.scan(conn, roots=["Z:/no/such/root"])
        self.assertEqual(result, {"matched": 0, "unmatched": []})

    def test_cli_help_without_default_roots(self):
        # On a machine without the author's hard-coded library folder,
        # DEFAULT_ROOTS is empty and the old help string indexed it, crashing
        # `slgking scan --help`. It must raise SystemExit (argparse --help),
        # never IndexError.
        with mock.patch.object(slg_scan, "DEFAULT_ROOTS", []):
            with self.assertRaises(SystemExit):
                slg_scan._main(["--help"])


class ScrapeTests(unittest.TestCase):
    def test_ext_from_url(self):
        self.assertEqual(
            slg_scrape._ext_from_url("https://x/a.png?w=100"), ".png")
        self.assertEqual(slg_scrape._ext_from_url("https://x/noext"), ".jpg")

    def test_parse_list_page_logs_dropped_cards(self):
        page = '<section class="gp-post-item"><div>no title link here</div></section>'
        logged = []
        out = slg_scrape.parse_list_page(page, log=logged.append)
        self.assertEqual(out, [])
        self.assertEqual(len(logged), 1)


if __name__ == "__main__":
    unittest.main()
