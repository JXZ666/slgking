"""The GitHub release check.

Version parsing, the once-a-day gate, and every way the request can fail -
which is the part that matters, because this runs unattended three seconds
after startup on machines that may be offline. http_get is patched; nothing
here touches the network. Run with:

    python -m unittest discover tests
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402
import slg_scrape  # noqa: E402
import slg_update  # noqa: E402

PAGE = "https://github.com/JXZ666/slgking/releases/tag/v0.14.0"


def reply(tag, url=PAGE):
    return ('{"tag_name": "%s", "html_url": "%s"}' % (tag, url)).encode("utf-8")


class ParseVersion(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(slg_update.parse_version("0.14.0"), (0, 14, 0))

    def test_leading_v(self):
        self.assertEqual(slg_update.parse_version("v0.14.0"), (0, 14, 0))

    def test_a_missing_component_reads_as_zero(self):
        # So "v1" and "v1.0.0" compare equal instead of one failing to parse.
        self.assertEqual(slg_update.parse_version("v1.2"), (1, 2, 0))
        self.assertEqual(slg_update.parse_version("v1"), (1, 0, 0))

    def test_trailing_junk_is_dropped(self):
        # A pre-release tag still answers "am I behind"; refusing to parse it
        # would mean the user never hears about that release at all.
        self.assertEqual(slg_update.parse_version("v0.14.0-beta.1"), (0, 14, 0))

    def test_garbage_is_none(self):
        for text in ("", None, "latest", "v", "release-notes"):
            self.assertIsNone(slg_update.parse_version(text), text)


class IsNewer(unittest.TestCase):
    def test_ahead(self):
        self.assertTrue(slg_update.is_newer("v0.14.0", "0.13.0"))
        self.assertTrue(slg_update.is_newer("v0.14.1", "0.14.0"))
        self.assertTrue(slg_update.is_newer("v1.0.0", "0.99.99"))

    def test_same_or_behind(self):
        self.assertFalse(slg_update.is_newer("v0.13.0", "0.13.0"))
        self.assertFalse(slg_update.is_newer("v0.12.9", "0.13.0"))

    def test_unparseable_never_nags(self):
        # A repo whose tags are not versions must not produce a permanent
        # "new version available" that no release can clear.
        self.assertFalse(slg_update.is_newer("latest", "0.13.0"))
        self.assertFalse(slg_update.is_newer(None, "0.13.0"))
        self.assertFalse(slg_update.is_newer("v0.14.0", None))


class LatestRelease(unittest.TestCase):
    def test_reads_the_tag_and_the_page(self):
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=reply("v0.14.0")) as get:
            got = slg_update.latest_release()
        self.assertEqual(got["version"], "v0.14.0")
        self.assertEqual(got["url"], PAGE)
        self.assertIn(slg_update.REPO, get.call_args.args[0])

    def test_missing_html_url_falls_back_to_the_releases_page(self):
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=b'{"tag_name": "v0.14.0"}'):
            got = slg_update.latest_release()
        self.assertEqual(got["url"], slg_update.RELEASES_URL)

    def test_network_failure_is_none(self):
        with mock.patch.object(slg_scrape, "http_get",
                               side_effect=OSError("no route to host")):
            self.assertIsNone(slg_update.latest_release())

    def test_rate_limit_body_is_none(self):
        # GitHub answers 403 with JSON that has a message and no tag_name.
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=b'{"message": "API rate limit exceeded"}'):
            self.assertIsNone(slg_update.latest_release())

    def test_html_body_is_none(self):
        # A captive portal or a proxy returning its own page.
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=b"<html>sign in to wifi</html>"):
            self.assertIsNone(slg_update.latest_release())

    def test_a_non_version_tag_is_none(self):
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=reply("latest")):
            self.assertIsNone(slg_update.latest_release())


class CheckGate(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.moment = datetime(2026, 9, 20, 10, 0, 0)

    def tearDown(self):
        self.conn.close()

    def _stamp(self):
        return slg_db.get_pref(self.conn, slg_update.PREF_CHECKED_AT)

    def test_reports_a_newer_release(self):
        with mock.patch.object(slg_scrape, "http_get", return_value=reply("v0.14.0")):
            got = slg_update.check(self.conn, "0.13.0", now=self.moment)
        self.assertEqual(got["version"], "v0.14.0")

    def test_being_current_is_none(self):
        with mock.patch.object(slg_scrape, "http_get", return_value=reply("v0.13.0")):
            self.assertIsNone(slg_update.check(self.conn, "0.13.0", now=self.moment))

    def test_the_second_check_of_the_day_makes_no_request(self):
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=reply("v0.14.0")) as first:
            slg_update.check(self.conn, "0.13.0", now=self.moment)
        self.assertEqual(first.call_count, 1)

        with mock.patch.object(slg_scrape, "http_get",
                               return_value=reply("v0.14.0")) as second:
            got = slg_update.check(self.conn, "0.13.0",
                                   now=self.moment + timedelta(hours=1))
        self.assertEqual(second.call_count, 0)
        self.assertIsNone(got)

    def test_a_day_later_it_asks_again(self):
        with mock.patch.object(slg_scrape, "http_get", return_value=reply("v0.14.0")):
            slg_update.check(self.conn, "0.13.0", now=self.moment)
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=reply("v0.14.0")) as later:
            got = slg_update.check(self.conn, "0.13.0",
                                   now=self.moment + timedelta(hours=25))
        self.assertEqual(later.call_count, 1)
        self.assertIsNotNone(got)

    def test_a_failed_check_still_stamps_the_clock(self):
        # The stamp is written before the request on purpose. Without it an
        # offline laptop, a blocked github.com, or a rate limit would retry on
        # every single launch, which is the one way this becomes a nuisance.
        with mock.patch.object(slg_scrape, "http_get",
                               side_effect=OSError("offline")):
            self.assertIsNone(slg_update.check(self.conn, "0.13.0", now=self.moment))
        self.assertEqual(self._stamp(),
                         self.moment.isoformat(timespec="seconds"))

    def test_an_unreadable_stamp_does_not_block_the_check(self):
        slg_db.set_pref(self.conn, slg_update.PREF_CHECKED_AT, "not-a-date")
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=reply("v0.14.0")) as get:
            got = slg_update.check(self.conn, "0.13.0", now=self.moment)
        self.assertEqual(get.call_count, 1)
        self.assertIsNotNone(got)

    def test_force_ignores_the_gate(self):
        with mock.patch.object(slg_scrape, "http_get", return_value=reply("v0.14.0")):
            slg_update.check(self.conn, "0.13.0", now=self.moment)
        with mock.patch.object(slg_scrape, "http_get",
                               return_value=reply("v0.14.0")) as forced:
            got = slg_update.check(self.conn, "0.13.0",
                                   now=self.moment + timedelta(minutes=1),
                                   force=True)
        self.assertEqual(forced.call_count, 1)
        self.assertIsNotNone(got)


if __name__ == "__main__":
    unittest.main()
