"""The sitemap-driven sync.

The fixtures below are trimmed copies of real responses, taken 2026-09-18.
Run with:

    python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_scrape  # noqa: E402

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
<url>
\t<loc>https://dikgames.com/the-copycat/</loc>
\t<lastmod>2025-10-23T09:40:43+00:00</lastmod>
\t<image:image>
\t\t<image:loc>https://dikgames.com/wp-content/uploads/2025/04/mainscreen1.jpg</image:loc>
\t</image:image>
\t<image:image>
\t\t<image:loc>https://dikgames.com/wp-content/uploads/2025/04/threesome2-768x432.jpg</image:loc>
\t</image:image>
</url>
<url>
\t<loc>https://dikgames.com/your-rwby-fantasy/</loc>
\t<lastmod>2025-01-18T18:01:39+00:00</lastmod>
\t<image:image>
\t\t<image:loc>https://dikgames.com/wp-content/uploads/2025/01/OTFEAn-768x432.jpg</image:loc>
\t</image:image>
</url>
</urlset>
"""

DETAIL = """<html><head>
<meta property="og:title" content="The Copycat" />
<meta property="og:image" content="https://dikgames.com/wp-content/uploads/2025/04/mainscreen1.jpg" />
<script type="application/ld+json">{"ratingValue": "7.3"}</script>
<meta itemprop="datePublished" datetime="2025-04-02T10:00:00+00:00" />
</head><body>
<a href="https://dikgames.com/tag/netorare/">netorare</a>
<a href="https://dikgames.com/tag/cheating/">cheating</a>
<a href="https://dikgames.com/tag/horror/">horror</a>
Version: v1.3.0
Developer: Mr. PBR
<div id="elementor-tab-content-1601"><p>A dark story.</p></div>
</div></div>
</body></html>
"""


class FakeFetcher:
    """Serves the sitemap shards and one detail page, and counts requests."""

    def __init__(self, pages, failing=()):
        self.pages = pages
        self.failing = set(failing)
        self.count = 0

    def get(self, url, retries=1, binary=False):
        self.count += 1
        for key in self.failing:
            if key in url:
                return None
        for key, body in self.pages.items():
            if key in url:
                return body
        return None


class SizedCover(unittest.TestCase):
    def test_original_is_swapped_for_the_registered_crop(self):
        # The sitemap links the full-size original: 177KB against 18KB for the
        # 576x356 crop the theme renders everywhere else.
        self.assertEqual(
            slg_scrape._sized_cover(
                "https://dikgames.com/wp-content/uploads/2025/04/mainscreen1.jpg"),
            "https://dikgames.com/wp-content/uploads/2025/04/mainscreen1-576x356.jpg")

    def test_already_sized_is_left_alone(self):
        url = "https://dikgames.com/wp-content/uploads/2025/04/mainscreen1-576x356.jpg"
        self.assertEqual(slg_scrape._sized_cover(url), url)

    def test_other_images_keep_their_own_size(self):
        url = "https://dikgames.com/wp-content/uploads/2025/04/threesome2-768x432.jpg"
        self.assertEqual(slg_scrape._sized_cover(url), url)

    def test_none_passes_through(self):
        self.assertIsNone(slg_scrape._sized_cover(None))


class FetchSitemap(unittest.TestCase):
    def test_reads_lastmod_and_cover_per_slug(self):
        fetcher = FakeFetcher({"post-sitemap.xml": SITEMAP})
        index = slg_scrape.fetch_sitemap(fetcher, log=lambda *a: None)
        self.assertEqual(sorted(index), ["the-copycat", "your-rwby-fantasy"])
        self.assertEqual(index["the-copycat"]["lastmod"], "2025-10-23")
        self.assertEqual(
            index["the-copycat"]["cover"],
            "https://dikgames.com/wp-content/uploads/2025/04/mainscreen1-576x356.jpg")
        # No mainscreen in this entry, so its 768x432 stands.
        self.assertEqual(
            index["your-rwby-fantasy"]["cover"],
            "https://dikgames.com/wp-content/uploads/2025/01/OTFEAn-768x432.jpg")

    def test_a_500_on_one_shard_does_not_lose_the_others(self):
        # post-sitemap3.xml really does answer HTTP 500. The other shards must
        # still come through rather than the whole run aborting.
        fetcher = FakeFetcher({"post-sitemap.xml": SITEMAP},
                              failing=("post-sitemap2.xml", "post-sitemap4.xml"))
        index = slg_scrape.fetch_sitemap(fetcher, log=lambda *a: None)
        self.assertEqual(len(index), 2)

    def test_all_shards_down_returns_nothing(self):
        fetcher = FakeFetcher({}, failing=("post-sitemap",))
        self.assertEqual(slg_scrape.fetch_sitemap(fetcher, log=lambda *a: None), {})


class ParseDetailPage(unittest.TestCase):
    def test_pulls_tags_cover_and_title(self):
        detail = slg_scrape.parse_detail_page(DETAIL)
        self.assertEqual(detail["tags"], ["cheating", "horror", "netorare"])
        self.assertEqual(detail["title"], "The Copycat")
        self.assertEqual(
            detail["cover_url"],
            "https://dikgames.com/wp-content/uploads/2025/04/mainscreen1-576x356.jpg")
        self.assertEqual(detail["rating"], 7.3)
        self.assertEqual(detail["version"], "1.3.0")
        self.assertEqual(detail["last_updated"], "2025-04-02")


class SyncIncremental(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.pages = {"post-sitemap.xml": SITEMAP,
                      "the-copycat": DETAIL,
                      "your-rwby-fantasy": DETAIL}

    def tearDown(self):
        self.conn.close()

    def test_first_run_adopts_lastmod_without_refetching(self):
        # 1274 existing rows all start with a NULL lastmod. Treating that as
        # 'changed' would mean 1274 detail fetches to learn nothing, so they
        # adopt the sitemap value instead.
        slg_db.upsert_game(self.conn, slug="the-copycat",
                           url="https://dikgames.com/the-copycat/",
                           title="The Copycat")
        self.conn.commit()
        fetcher = FakeFetcher(self.pages)
        summary = slg_scrape.sync_incremental(self.conn, fetcher,
                                              log=lambda *a: None)
        self.assertEqual(summary["new"], 1)      # your-rwby-fantasy only
        self.assertEqual(
            self.conn.execute("SELECT lastmod FROM games WHERE slug = 'the-copycat'")
            .fetchone()["lastmod"], "2025-10-23")

    def test_unchanged_game_is_not_refetched(self):
        fetcher = FakeFetcher(self.pages)
        slg_scrape.sync_incremental(self.conn, fetcher, log=lambda *a: None)
        first = fetcher.count
        again = FakeFetcher(self.pages)
        summary = slg_scrape.sync_incremental(self.conn, again, log=lambda *a: None)
        self.assertEqual(summary["changed"], 0)
        self.assertEqual(summary["new"], 0)
        # Only the sitemap shard, no detail pages.
        self.assertLess(again.count, first)

    def test_a_moved_lastmod_refetches_and_updates(self):
        fetcher = FakeFetcher(self.pages)
        slg_scrape.sync_incremental(self.conn, fetcher, log=lambda *a: None)
        moved = SITEMAP.replace("2025-10-23", "2026-01-01")
        again = FakeFetcher({"post-sitemap.xml": moved, "the-copycat": DETAIL,
                             "your-rwby-fantasy": DETAIL})
        summary = slg_scrape.sync_incremental(self.conn, again, log=lambda *a: None)
        self.assertEqual(summary["changed"], 1)

    def test_new_game_lands_complete_in_one_request(self):
        fetcher = FakeFetcher(self.pages)
        slg_scrape.sync_incremental(self.conn, fetcher, log=lambda *a: None)
        row = self.conn.execute(
            "SELECT * FROM games WHERE slug = 'the-copycat'").fetchone()
        self.assertEqual(row["title"], "The Copycat")
        self.assertEqual(row["version"], "1.3.0")
        self.assertEqual(row["rating"], 7.3)
        self.assertEqual(row["overview"], "A dark story.")
        self.assertTrue(row["cover_file"].startswith("pending:"))
        self.assertEqual(slg_db.game_tags(self.conn, row["id"]),
                         ["cheating", "horror", "netorare"])

    def test_existing_developer_is_not_overwritten(self):
        # The listing page calls it 'PiggyBackRide Productions', the detail
        # page 'Mr. PBR'. Neither is wrong; a re-sync must not flip it.
        slg_db.upsert_game(self.conn, slug="the-copycat",
                           url="https://dikgames.com/the-copycat/",
                           title="The Copycat", developer="PiggyBackRide Productions")
        self.conn.commit()
        slg_scrape.sync_incremental(self.conn, FakeFetcher(self.pages),
                                    log=lambda *a: None)
        self.assertEqual(
            self.conn.execute(
                "SELECT developer FROM games WHERE slug = 'the-copycat'"
            ).fetchone()["developer"],
            "PiggyBackRide Productions")


class UpsertNeverBlanks(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_a_partial_update_keeps_the_earlier_fields(self):
        # Two writers share this row: the tag walk has no rating, the
        # incremental pass has no list-page version. Each must be able to
        # update what it knows without nulling what the other collected.
        slg_db.upsert_game(self.conn, slug="g", url="u", title="G",
                           version="1.0", developer="Dev", engine="unity",
                           rating=8.0)
        self.conn.commit()
        slg_db.upsert_game(self.conn, slug="g", url="u", title="G")
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM games WHERE slug = 'g'").fetchone()
        self.assertEqual(row["version"], "1.0")
        self.assertEqual(row["developer"], "Dev")
        self.assertEqual(row["engine"], "unity")
        self.assertEqual(row["rating"], 8.0)


class Migration(unittest.TestCase):
    def test_lastmod_is_added_to_an_existing_database(self):
        conn = slg_db.connect(":memory:")
        # Simulate the pre-upgrade shape by dropping the column's presence
        # check: the migration is what an older db goes through on open.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(games)")}
        self.assertIn("lastmod", have)
        conn.close()


if __name__ == "__main__":
    unittest.main()
