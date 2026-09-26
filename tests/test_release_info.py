import unittest
from unittest import mock
from urllib.error import URLError

import slg_release_info


class StableReleaseInfoTests(unittest.TestCase):
    def setUp(self):
        slg_release_info._reset_cache_for_tests()

    def tearDown(self):
        slg_release_info._reset_cache_for_tests()

    def test_latest_stable_release_is_normalized_and_cached(self):
        value = {"version": "0.23.2", "checked_at": "2026-09-25T00:00:00Z",
                 "source": "github", "fallback": False}
        with mock.patch.object(slg_release_info, "_fetch_latest", return_value=value) as fetch:
            first = slg_release_info.get_stable_release()
            second = slg_release_info.get_stable_release()
        self.assertEqual(first["version"], "0.23.2")
        self.assertFalse(first["fallback"])
        self.assertEqual(second, first)
        fetch.assert_called_once_with()

    def test_network_failure_uses_configured_fallback(self):
        with mock.patch.object(slg_release_info, "_fetch_latest",
                               side_effect=URLError("offline")):
            result = slg_release_info.get_stable_release("0.23.0")
        self.assertEqual(result["version"], "0.23.0")
        self.assertEqual(result["source"], "configured-fallback")
        self.assertTrue(result["fallback"])

    def test_api_failure_falls_back_to_github_latest_redirect(self):
        value = {"version": "0.23.0", "checked_at": "2026-09-25T00:00:00Z",
                 "source": "github-release-page", "fallback": False}
        with mock.patch.object(slg_release_info, "_fetch_latest_api",
                               side_effect=URLError("API rate limited")), \
             mock.patch.object(slg_release_info, "_fetch_latest_page",
                               return_value=value) as page:
            result = slg_release_info._fetch_latest()
        self.assertEqual(result, value)
        page.assert_called_once_with()

    def test_network_failure_keeps_last_known_good(self):
        value = {"version": "0.23.2", "checked_at": "2026-09-25T00:00:00Z",
                 "source": "github", "fallback": False}
        with mock.patch.object(slg_release_info.time, "monotonic", side_effect=[
                              10, 10 + slg_release_info.CACHE_SECONDS + 1]), \
             mock.patch.object(slg_release_info, "_fetch_latest",
                               side_effect=[value, URLError("offline")]):
            first = slg_release_info.get_stable_release()
            second = slg_release_info.get_stable_release()
        self.assertEqual(first["version"], "0.23.2")
        self.assertEqual(second["version"], "0.23.2")
        self.assertEqual(second["source"], "last-known-good")
        self.assertTrue(second["fallback"])

    def test_release_tag_must_be_a_stable_semantic_version(self):
        self.assertEqual(slg_release_info._normalize_tag("v0.23.2"), "0.23.2")
        self.assertIsNone(slg_release_info._normalize_tag("0.23.2-beta"))
        self.assertIsNone(slg_release_info._normalize_tag("latest"))


if __name__ == "__main__":
    unittest.main()
