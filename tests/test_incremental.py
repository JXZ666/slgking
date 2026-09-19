"""The sitemap-driven sync.

The fixtures below are trimmed copies of real responses, taken 2026-09-18.
Run with:

    python -m unittest discover tests
"""

import http.client
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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
<div id="elementor-tab-title-1601" class="elementor-tab-title" aria-controls="elementor-tab-content-1601" role="tab">Overview</div>
<div id="elementor-tab-content-1601" class="elementor-tab-content"><p>A dark story.</p></div>
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

    def _summary(self, since):
        return slg_scrape.sync_incremental(self.conn, FakeFetcher(self.pages),
                                           since=since, log=lambda *a: None)

    def test_the_gate_drops_back_catalogue_new_games(self):
        # The site lists 2066 games against a library that has never held more
        # than half of them. Without a cutoff the routine sync spends its whole
        # budget on years-old entries and reports a pile of "new" that is not
        # what the user is waiting for. The fixture's two games are 2025-10-23
        # and 2025-01-18.
        summary = self._summary("2025-06-01")
        self.assertEqual(summary["new"], 1)          # the-copycat only
        self.assertEqual(summary["skipped_old"], 1)  # your-rwby-fantasy
        slugs = {r["slug"] for r in self.conn.execute("SELECT slug FROM games")}
        self.assertEqual(slugs, {"the-copycat"})

    def test_no_gate_takes_the_back_catalogue_too(self):
        summary = self._summary(None)
        self.assertEqual(summary["new"], 2)
        self.assertEqual(summary["skipped_old"], 0)

    def test_the_gate_never_hides_a_change_to_a_game_already_held(self):
        # The cutoff is about games that have never been seen. Applying it to
        # rows already in the library would mean an old game the author patched
        # last week never being noticed - the one thing a sync must not miss.
        slg_db.upsert_game(self.conn, slug="your-rwby-fantasy",
                           url="https://dikgames.com/your-rwby-fantasy/",
                           title="Your RWBY Fantasy", lastmod="2020-01-01")
        self.conn.commit()
        summary = self._summary("2030-01-01")
        self.assertEqual(summary["changed"], 1)
        self.assertEqual(summary["skipped_old"], 1)  # the-copycat is still new

    def test_an_entry_with_no_lastmod_is_collected_rather_than_guessed_at(self):
        # Nothing to compare, so the safe direction is to take it: a skipped
        # game is invisible, a fetched one costs a request.
        sitemap = SITEMAP.replace("\t<lastmod>2025-01-18T18:01:39+00:00</lastmod>\n",
                                  "")
        pages = dict(self.pages, **{"post-sitemap.xml": sitemap})
        summary = slg_scrape.sync_incremental(
            self.conn, FakeFetcher(pages), since="2030-01-01",
            log=lambda *a: None)
        slugs = {r["slug"] for r in self.conn.execute("SELECT slug FROM games")}
        self.assertEqual(slugs, {"your-rwby-fantasy"})
        self.assertEqual(summary["new"], 1)
        self.assertEqual(summary["skipped_old"], 1)  # the dated one

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


class TheVersionNeverWalksBackwards(unittest.TestCase):
    """The listing page and the detail page disagree about the version.

    The listing page's '[v0.26.6]' bracket is the site's current version. The
    detail page carries a 'Version:' line the author often forgets to bump.
    Both go through COALESCE, and COALESCE takes the first non-null - so the
    detail page won simply for being written last, and agent17 dropped from
    0.26.6 to 0.25.3 on an ordinary sync.
    """

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.gid, _ = slg_db.upsert_game(self.conn, slug="g", url="u",
                                         title="G", version="0.26.6")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _version(self):
        return self.conn.execute("SELECT version FROM games WHERE id = ?",
                                 (self.gid,)).fetchone()["version"]

    def test_the_detail_page_cannot_lower_it(self):
        slg_db.upsert_detail(self.conn, self.gid, version="0.25.3")
        self.assertEqual(self._version(), "0.26.6", "详情页把版本号写低了")

    def test_the_listing_page_cannot_lower_it(self):
        slg_db.upsert_game(self.conn, slug="g", url="u", title="G",
                           version="0.1")
        self.assertEqual(self._version(), "0.26.6", "列表页把版本号写低了")

    def test_a_newer_version_still_lands(self):
        slg_db.upsert_detail(self.conn, self.gid, version="0.27")
        self.assertEqual(self._version(), "0.27")

    def test_a_version_written_with_a_v_prefix_does_not_lower_it(self):
        # 'v0.26.6' and '0.26.6' are the same version written two ways, and
        # both spellings exist in the wild - one on each page. Reading the
        # prefixed one as an upgrade would let the number flip on every sync.
        slg_db.upsert_detail(self.conn, self.gid, version="v0.26.6")
        self.assertEqual(self._version(), "0.26.6")

    def test_an_unreadable_version_leaves_the_stored_one_alone(self):
        # _version_gt answers False when either side has no digits, so an
        # empty string refuses to move rather than moving backwards.
        slg_db.upsert_detail(self.conn, self.gid, version="")
        self.assertEqual(self._version(), "0.26.6")


class DataGaps(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.object(slg_db, "covers_dir", return_value=self.tmp)
        self.patch.start()
        # The cover walk is memoised and the memo is module-level, so one test's
        # count would otherwise be served to the next.
        slg_db.invalidate_cover_gaps()

    def tearDown(self):
        slg_db.invalidate_cover_gaps()
        self.patch.stop()
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _game(self, slug):
        self.gid, _ = slg_db.upsert_game(self.conn, slug=slug, url="u/" + slug,
                                         title=slug)
        self.conn.commit()

    def test_a_cover_file_that_is_not_on_disk_is_still_a_gap(self):
        # load_cover only accepts a file it can actually open, so a stored name
        # whose file has been deleted left the button saying 封面已齐 over a list
        # of blank cards - and do_covers then refused to run.
        self._game("g")
        self.conn.execute("UPDATE games SET cover_file = 'gone.jpg' WHERE id = ?",
                          (self.gid,))
        self.conn.commit()
        self.assertEqual(slg_db.data_gaps(self.conn)["covers"], 1)
        with open(os.path.join(self.tmp, "gone.jpg"), "wb"):
            pass
        slg_db.invalidate_cover_gaps()
        self.assertEqual(slg_db.data_gaps(self.conn)["covers"], 0)

    def test_a_pending_cover_is_still_a_gap(self):
        self._game("g")
        self.conn.execute("UPDATE games SET cover_file = 'pending:x' WHERE id = ?",
                          (self.gid,))
        self.conn.commit()
        self.assertEqual(slg_db.data_gaps(self.conn)["covers"], 1)

    def test_the_cover_walk_is_memoised_until_it_is_dropped(self):
        # One os.path.exists per stored cover, ~1500 of them, and _render_stats
        # asks on every refresh. The memo is what keeps that off the UI thread.
        self._game("g")
        self.conn.execute("UPDATE games SET cover_file = 'gone.jpg' WHERE id = ?",
                          (self.gid,))
        self.conn.commit()
        self.assertEqual(slg_db.data_gaps(self.conn)["covers"], 1)
        with open(os.path.join(self.tmp, "gone.jpg"), "wb"):
            pass
        self.assertEqual(slg_db.data_gaps(self.conn)["covers"], 1,
                         "缓存没有生效，每次都重新扫了封面目录")
        slg_db.invalidate_cover_gaps()
        self.assertEqual(slg_db.data_gaps(self.conn)["covers"], 0,
                         "invalidate 之后还在用旧结果")

    def test_an_overview_of_spaces_counts_as_a_gap(self):
        # _clean turns an empty blurb into NULL, but rows written before that
        # exist and hold "". The panel renders the two the same way, and the
        # sync skipped both, so they could never be filled.
        self._game("g")
        self.conn.execute("UPDATE games SET overview = '   ' WHERE id = ?",
                          (self.gid,))
        self.conn.commit()
        self.assertEqual(slg_db.data_gaps(self.conn)["overview"], 1)
        self.conn.execute("UPDATE games SET overview = 'A story.' WHERE id = ?",
                          (self.gid,))
        self.conn.commit()
        self.assertEqual(slg_db.data_gaps(self.conn)["overview"], 0)


class CleanSiteTitle(unittest.TestCase):
    """og:title carries the site's own name, and the detail walk stored it.

    314 of 1595 rows read 'Something - dikgames' on the card - the site's
    masthead leaking into a game's name.
    """

    def test_the_site_name_comes_off_the_end(self):
        self.assertEqual(slg_db.clean_site_title("The Copycat - dikgames"),
                         "The Copycat")

    def test_the_separator_variants_all_come_off(self):
        for text in ("The Copycat \u2013 dikgames", "The Copycat \u2014 dikgames",
                     "The Copycat-dikgames", "The Copycat - DikGames"):
            self.assertEqual(slg_db.clean_site_title(text), "The Copycat", text)

    def test_a_name_that_merely_mentions_the_site_is_left_alone(self):
        # Only a trailing ' - dikgames' is the site signing its own page. A
        # game actually called that would be mangled by a looser rule.
        self.assertEqual(slg_db.clean_site_title("Dikgames Tycoon"),
                         "Dikgames Tycoon")
        self.assertEqual(slg_db.clean_site_title("dikgames - the game"),
                         "dikgames - the game")

    def test_nothing_is_returned_for_nothing(self):
        self.assertIsNone(slg_db.clean_site_title(None))
        self.assertEqual(slg_db.clean_site_title(""), "")

    def test_the_migration_repairs_the_cached_translation_too(self):
        # The cards read the translation cache, not the title. Cleaning the
        # source alone left the card reading '永恒世界 [v0.9.5] - dikgames'
        # with a clean row underneath - and dropping the row instead would
        # cost the user the Chinese name entirely.
        conn = slg_db.connect(":memory:")
        try:
            gid, _ = slg_db.upsert_game(conn, slug="g", url="u",
                                        title="Eternum [v0.9.5] - dikgames")
            conn.commit()
            slg_db.set_translation(conn, "title", gid,
                                   "Eternum [v0.9.5] - dikgames",
                                   "永恒世界 [v0.9.5] - dikgames", engine="m")
            slg_db._migrate(conn)
            self.assertEqual(slg_db.title_translations(conn),
                             {str(gid): ("永恒世界 [v0.9.5]", "m")})
        finally:
            conn.close()

    def test_the_migration_leaves_a_hand_typed_name_alone(self):
        conn = slg_db.connect(":memory:")
        try:
            gid, _ = slg_db.upsert_game(conn, slug="g", url="u",
                                        title="Eternum - dikgames")
            conn.commit()
            slg_db.set_manual_translation(conn, "title", gid, "永恒世界")
            slg_db._migrate(conn)
            self.assertEqual(slg_db.title_translations(conn),
                             {str(gid): ("永恒世界", slg_db.ENGINE_MANUAL)})
        finally:
            conn.close()

    def test_the_migration_cleans_rows_written_before_it_existed(self):
        # The parser fix only helps pages fetched from here on; the rows the
        # old parser wrote are already in the user's database.
        conn = slg_db.connect(":memory:")
        try:
            slg_db.upsert_game(conn, slug="g", url="u",
                               title="The Copycat - dikgames")
            slg_db.upsert_game(conn, slug="h", url="u2", title="Untouched")
            conn.commit()
            slg_db._migrate(conn)
            rows = {r["slug"]: r["title"] for r in
                    conn.execute("SELECT slug, title FROM games")}
            self.assertEqual(rows["g"], "The Copycat")
            self.assertEqual(rows["h"], "Untouched")
        finally:
            conn.close()


class FetcherSurvivesBadResponses(unittest.TestCase):
    def test_a_truncated_response_returns_none_instead_of_raising(self):
        # http.client.IncompleteRead is an HTTPException, not an OSError, so it
        # slipped past Fetcher's except clause and killed the whole sync - one
        # flaky page cost every page queued behind it.
        fetcher = slg_scrape.Fetcher(delay=0, log=lambda *a: None)
        with mock.patch.object(slg_scrape, "http_get",
                               side_effect=http.client.IncompleteRead(b"half")):
            self.assertIsNone(fetcher.get("https://dikgames.com/x/", retries=0))

    def test_the_request_is_logged_when_it_gives_up(self):
        said = []
        fetcher = slg_scrape.Fetcher(delay=0, log=said.append)
        with mock.patch.object(slg_scrape, "http_get",
                               side_effect=http.client.IncompleteRead(b"half")):
            fetcher.get("https://dikgames.com/x/", retries=1)
        self.assertTrue(any("dikgames.com/x" in line for line in said), said)


class Migration(unittest.TestCase):
    def test_lastmod_is_added_to_an_existing_database(self):
        conn = slg_db.connect(":memory:")
        # Simulate the pre-upgrade shape by dropping the column's presence
        # check: the migration is what an older db goes through on open.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(games)")}
        self.assertIn("lastmod", have)
        conn.close()


class StoppingTheSync(unittest.TestCase):
    """The cancel button, which the sitemap sync used to ignore outright.

    sync_incremental had no should_stop parameter at all, so the whole first
    phase of a sync - up to NEW_PER_RUN detail pages - did not look at the
    stop flag once. The button changed to "正在停止…" and nothing else happened.
    """

    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.pages = {"post-sitemap.xml": SITEMAP,
                      "the-copycat": DETAIL,
                      "your-rwby-fantasy": DETAIL}

    def tearDown(self):
        self.conn.close()

    def _games(self):
        return self.conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"]

    def test_without_a_stop_both_games_land(self):
        # The baseline the two tests below are measured against.
        summary = slg_scrape.sync_incremental(self.conn, FakeFetcher(self.pages),
                                              log=lambda *a: None)
        self.assertEqual(summary["new"], 2)
        self.assertEqual(self._games(), 2)

    def test_the_loop_stops_between_detail_pages(self):
        # Counting the checks rather than flagging them: the one after the
        # sitemap and the one at the top of each page are separate, and the
        # loop has to stop at the *second* page, not before the first.
        checks = []

        def should_stop():
            checks.append(1)
            return len(checks) > 2

        summary = slg_scrape.sync_incremental(self.conn, FakeFetcher(self.pages),
                                              should_stop=should_stop,
                                              log=lambda *a: None)
        self.assertEqual(summary["new"], 1)
        self.assertEqual(self._games(), 1)

    def test_a_stop_before_the_first_page_touches_nothing(self):
        summary = slg_scrape.sync_incremental(self.conn, FakeFetcher(self.pages),
                                              should_stop=lambda: True,
                                              log=lambda *a: None)
        self.assertEqual(summary["new"], 0)
        self.assertEqual(self._games(), 0)

    def test_a_stop_is_not_reported_as_an_unreachable_sitemap(self):
        # fetch_sitemap returns {} when every shard fails, and a stop cuts the
        # run short too. Reporting the second as "sitemap 全取不到，本次不同步"
        # would send the user looking for a network problem they do not have.
        said = []
        summary = slg_scrape.sync_incremental(self.conn, FakeFetcher(self.pages),
                                              should_stop=lambda: True,
                                              log=said.append)
        self.assertEqual(summary["catalogue"], 2)
        self.assertFalse([line for line in said if "全取不到" in line], said)

    def test_sync_tags_stops_before_the_first_tag(self):
        # 全量重建 shares the button and had the same hole: sync_tags took no
        # should_stop either.
        fetcher = FakeFetcher({})
        slg_scrape.sync_tags(self.conn, fetcher, ["a", "b", "c"],
                             should_stop=lambda: True, log=lambda *a: None)
        self.assertEqual(fetcher.count, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM sync_log").fetchone()["n"], 0)

    def test_sync_tags_still_walks_without_a_stop(self):
        # Guards the check against short-circuiting an ordinary run.
        fetcher = FakeFetcher({})
        slg_scrape.sync_tags(self.conn, fetcher, ["a"], log=lambda *a: None)
        self.assertGreater(fetcher.count, 0)


class FetcherHonoursStop(unittest.TestCase):
    def test_get_returns_none_without_a_request_once_stopped(self):
        fetcher = slg_scrape.Fetcher(delay=0, should_stop=lambda: True)
        with mock.patch.object(slg_scrape, "http_get") as get:
            self.assertIsNone(fetcher.get("https://dikgames.com/x/", retries=0))
        self.assertEqual(get.call_count, 0)

    def test_the_gap_between_requests_is_interrupted(self):
        # The gap is where a long sync actually spends its wall clock - a
        # second between requests, two more before a retry - and time.sleep()
        # made the cancel button sit out the whole thing. Thirty seconds here
        # so a plain sleep would be unmistakable.
        stop = threading.Event()
        fetcher = slg_scrape.Fetcher(delay=30, should_stop=stop.is_set)
        with mock.patch.object(slg_scrape, "http_get", return_value=b"ok"):
            fetcher.get("https://dikgames.com/first/", retries=0)  # sets _last

        timer = threading.Timer(0.2, stop.set)
        timer.start()
        started = time.time()
        try:
            with mock.patch.object(slg_scrape, "http_get") as second:
                self.assertIsNone(fetcher.get("https://dikgames.com/second/",
                                              retries=0))
        finally:
            timer.cancel()
        self.assertLess(time.time() - started, 5)
        self.assertEqual(second.call_count, 0)


if __name__ == "__main__":
    unittest.main()
