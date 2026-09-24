"""Regression tests for the v0.15.0 cleanup and bug fixes.

Run with:

    python -m unittest discover tests
"""

import inspect
import os
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_comments  # noqa: E402
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


class DirectHttpTests(unittest.TestCase):
    """The sync client must reach the author's own server directly.

    v0.21 switched the catalogue pull to http://43.130.240.89:8080 but kept the
    default urllib opener, so a user's system/VPN proxy swallowed the request
    (502 / timeout) and sync reported "服务器不可用". _DIRECT_OPENER is the
    no-proxy opener those requests now go through.
    """

    def test_direct_opener_bypasses_proxy(self):
        # ProxyHandler({}) means "no proxy": build_opener skips the default
        # ProxyHandler (which reads the system proxy), and the empty one registers
        # no *_open methods, so no handler in the opener carries a proxy mapping.
        for handler in slg_scrape._DIRECT_OPENER.handlers:
            self.assertIsNone(getattr(handler, "proxies", None),
                              "直连 opener 不应携带系统代理")

    def test_http_get_exposes_the_direct_flag(self):
        params = inspect.signature(slg_scrape.http_get).parameters
        self.assertIn("direct", params)
        self.assertFalse(params["direct"].default)


class NoteMergeTests(unittest.TestCase):
    """The retired 评价 note folds into the comment list exactly once.

    The detail panel used to carry a private note and a comment list side by
    side, and users read them as two competing comment boxes.
    """

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _game_with_note(self, slug, note):
        gid, _ = slg_db.upsert_game(self.conn, slug=slug,
                                    url="https://dikgames.com/%s/" % slug,
                                    title=slug)
        self.conn.execute("INSERT OR IGNORE INTO state (game_id) VALUES (?)", (gid,))
        self.conn.execute("UPDATE state SET note = ?, updated_at = datetime('now')"
                          " WHERE game_id = ?", (note, gid))
        self.conn.commit()
        return gid

    def test_a_note_becomes_a_private_comment(self):
        self._game_with_note("merge-a", "第一行\n第二行")
        self.conn.execute("DELETE FROM prefs WHERE key = ?",
                          (slg_db.NOTES_MERGED_PREF,))
        self.conn.commit()
        self.assertEqual(slg_db.migrate_notes_to_comments(self.conn), 1)
        rows = slg_db.list_comments(self.conn, "merge-a")
        self.assertEqual([r["content"] for r in rows], ["第一行\n第二行"])
        self.assertEqual(rows[0]["visibility"], "private")
        self.assertIsNone(rows[0]["cloud_id"])

    def test_the_note_column_is_left_alone(self):
        # Rollback insurance: an older build still reads state.note, and a
        # migration that ate it would leave that build with nothing.
        gid = self._game_with_note("merge-b", "留着")
        self.conn.execute("DELETE FROM prefs WHERE key = ?",
                          (slg_db.NOTES_MERGED_PREF,))
        self.conn.commit()
        slg_db.migrate_notes_to_comments(self.conn)
        note = self.conn.execute("SELECT note FROM state WHERE game_id = ?",
                                 (gid,)).fetchone()["note"]
        self.assertEqual(note, "留着")

    def test_an_empty_note_is_not_migrated(self):
        self._game_with_note("merge-c", "   \n ")
        self.conn.execute("DELETE FROM prefs WHERE key = ?",
                          (slg_db.NOTES_MERGED_PREF,))
        self.conn.commit()
        self.assertEqual(slg_db.migrate_notes_to_comments(self.conn), 0)
        self.assertEqual(slg_db.list_comments(self.conn, "merge-c"), [])

    def test_a_deleted_comment_stays_deleted(self):
        # Guarded by a pref, not by looking for the row: the user is free to
        # throw the migrated comment away, and it must not come back.
        self._game_with_note("merge-d", "不想要了")
        self.conn.execute("DELETE FROM prefs WHERE key = ?",
                          (slg_db.NOTES_MERGED_PREF,))
        self.conn.commit()
        slg_db.migrate_notes_to_comments(self.conn)
        for row in slg_db.list_comments(self.conn, "merge-d"):
            slg_db.delete_comment(self.conn, row["id"])
        self.assertEqual(slg_db.migrate_notes_to_comments(self.conn), 0)
        self.assertEqual(slg_db.list_comments(self.conn, "merge-d"), [])

    def test_connect_runs_the_merge_on_an_old_db(self):
        path = os.path.join(tempfile.mkdtemp(), "slgking.db")
        conn = slg_db.connect(path)
        gid, _ = slg_db.upsert_game(conn, slug="merge-e",
                                    url="https://dikgames.com/merge-e/",
                                    title="merge-e")
        conn.execute("INSERT OR IGNORE INTO state (game_id) VALUES (?)", (gid,))
        conn.execute("UPDATE state SET note = ?, updated_at = datetime('now')"
                     " WHERE game_id = ?", ("升级前写的", gid))
        # An old build's comments table: no visibility column at all.
        conn.execute("DROP TABLE comments")
        conn.execute("CREATE TABLE comments (id INTEGER PRIMARY KEY,"
                     " game_slug TEXT NOT NULL, content TEXT NOT NULL,"
                     " nickname TEXT, cloud_id TEXT, created_at TEXT NOT NULL)")
        conn.execute("DELETE FROM prefs WHERE key = ?",
                     (slg_db.NOTES_MERGED_PREF,))
        conn.commit()
        conn.close()

        conn = slg_db.connect(path)
        self.addCleanup(conn.close)
        rows = slg_db.list_comments(conn, "merge-e")
        self.assertEqual([r["content"] for r in rows], ["升级前写的"])
        self.assertEqual(rows[0]["visibility"], "private")

    def test_add_comment_defaults_to_private(self):
        slg_db.add_comment(self.conn, "merge-f", "随手一条")
        self.assertEqual(slg_db.list_comments(self.conn, "merge-f")[0]["visibility"],
                         "private")

    def test_a_public_comment_keeps_its_flag(self):
        slg_db.add_comment(self.conn, "merge-g", "给别人看", visibility="public")
        self.assertEqual(slg_db.list_comments(self.conn, "merge-g")[0]["visibility"],
                         "public")


class PublicCommentLimits(unittest.TestCase):
    def test_server_character_limit_is_enforced_without_truncation(self):
        self.assertIsNone(slg_comments.validate_public_comment(
            "game", "x" * slg_comments.MAX_COMMENT_CONTENT_CHARS,
            device="device-id"))
        error = slg_comments.validate_public_comment(
            "game", "x" * (slg_comments.MAX_COMMENT_CONTENT_CHARS + 1),
            device="device-id")
        self.assertIn("500", error)

    def test_serialized_utf8_request_limit_is_enforced(self):
        error = slg_comments.validate_public_comment(
            "game", "汉" * 500, nickname="x" * 600, device="device-id")
        self.assertIn("2048", error)

    def test_oversize_upload_never_reaches_network(self):
        with mock.patch.object(slg_scrape, "http_post") as post:
            result = slg_comments.upload_comment(
                "game", "x" * (slg_comments.MAX_COMMENT_CONTENT_CHARS + 1),
                device="device-id")
        self.assertIsNone(result)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
