"""远程配置、匿名上报与个人页计数器的测试。

远程侧全是「失败静默」的：config 拉不到返回 {}，上报失败被吞掉。这些测试锁定
那两条契约，以及上报 payload 绝不带任何可识别字段。个人页计数器则锁连续签到
天数和积分流这两个最容易算错的地方。

Run with: `python -m unittest discover tests`
"""

import threading
import unittest
import urllib.error
from datetime import date, timedelta
from unittest import mock

import slg_db
import slg_remote


class DeviceId(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_device_id_is_stable_and_persisted(self):
        a = slg_remote.device_id(self.conn)
        b = slg_remote.device_id(self.conn)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 32, "uuid4().hex 应为 32 位")
        self.assertEqual(slg_db.get_pref(self.conn, slg_remote.PREF_DEVICE_ID), a)


class FetchConfig(unittest.TestCase):
    def test_returns_dict_on_success(self):
        with mock.patch.object(slg_remote.slg_scrape, "http_get",
                               return_value=b'{"maintenance": true}'):
            self.assertEqual(slg_remote.fetch_config(), {"maintenance": True})

    def test_returns_empty_on_failure(self):
        with mock.patch.object(slg_remote.slg_scrape, "http_get",
                               side_effect=OSError("down")):
            self.assertEqual(slg_remote.fetch_config(), {})

    def test_rejects_non_dict_body(self):
        with mock.patch.object(slg_remote.slg_scrape, "http_get",
                               return_value=b'[1, 2, 3]'):
            self.assertEqual(slg_remote.fetch_config(), {})


class FetchStats(unittest.TestCase):
    """The panel shows one of four states, and each one has a different fix.

    Flattening them to None is what made an undeployed server look exactly like
    a server that simply had no users yet.
    """

    def _get(self, value=None, error=None):
        patcher = mock.patch.object(slg_remote.slg_scrape, "http_get")
        get = patcher.start()
        self.addCleanup(patcher.stop)
        if error is not None:
            get.side_effect = error
        else:
            get.return_value = value
        return slg_remote.fetch_stats()

    def test_ok_carries_the_aggregate(self):
        body = b'{"events": {"launch": 3}, "devices": 2, "last_report": "x"}'
        status, stats = self._get(body)
        self.assertEqual(status, "ok")
        self.assertEqual(stats["devices"], 2)

    def test_404_means_the_box_was_never_deployed(self):
        err = urllib.error.HTTPError("u", 404, "not found", {}, None)
        self.assertEqual(self._get(error=err), ("missing", None))

    def test_locked_means_the_key_does_not_match(self):
        self.assertEqual(self._get(b'{"locked": true}'), ("locked", None))

    def test_network_failure_is_offline(self):
        self.assertEqual(self._get(error=OSError("down")), ("offline", None))

    def test_non_json_body_is_offline(self):
        self.assertEqual(self._get(b"<html>404</html>"), ("offline", None))

    def test_other_http_errors_are_not_missing(self):
        """Only 404 means 'no such endpoint'; a 500 must not read as undeployed."""
        err = urllib.error.HTTPError("u", 500, "boom", {}, None)
        self.assertEqual(self._get(error=err), ("offline", None))


class Report(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _sync_threads(self):
        """Run report()'s sender inline instead of on a daemon thread."""
        class _Sync:
            def __init__(self, target, daemon=False):
                self._target = target

            def start(self):
                self._target()
        return mock.patch.object(slg_remote.threading, "Thread", _Sync)

    def test_payload_has_no_personal_fields(self):
        with self._sync_threads(), \
                mock.patch.object(slg_remote.slg_scrape, "http_post") as post:
            slg_remote.report(self.conn, "launch", {"games": 5})
        self.assertTrue(post.called)
        payload = post.call_args.args[1]
        self.assertEqual(payload["event"], "launch")
        self.assertEqual(payload["data"], {"games": 5})
        self.assertIn("device", payload)
        for key in ("nickname", "game", "title", "cover", "comment"):
            self.assertNotIn(key, payload, "上报 payload 泄漏了可识别字段")


class ProfileCounters(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_signin_streak_counts_back_from_today(self):
        today = date.today()
        for delta in (0, 1, 2):
            slg_db.record_signin(self.conn, (today - timedelta(days=delta)).isoformat(), 10)
        s = slg_db.signin_days(self.conn)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["streak"], 3)

    def test_signin_streak_breaks_on_gap(self):
        today = date.today()
        slg_db.record_signin(self.conn, today.isoformat(), 10)
        slg_db.record_signin(self.conn, (today - timedelta(days=2)).isoformat(), 10)
        s = slg_db.signin_days(self.conn)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["streak"], 1)

    def test_signin_month_days_returns_day_numbers(self):
        today = date.today()
        slg_db.record_signin(self.conn, today.isoformat(), 10)
        days = slg_db.signin_month_days(self.conn, today.year, today.month)
        self.assertIn(today.day, days)

    def test_lottery_count_and_points_flow(self):
        slg_db.add_points(self.conn, 100, "签到")
        slg_db.add_points(self.conn, -5, "每日抽奖")
        slg_db.add_points(self.conn, 3, "抽奖奖励")
        self.assertEqual(slg_db.lottery_count(self.conn), 1)
        flow = slg_db.points_flow(self.conn)
        self.assertEqual(flow["earned"], 103)
        self.assertEqual(flow["spent"], 5)

    def test_empty_profile_counts_are_zero(self):
        self.assertEqual(slg_db.collection_count(self.conn), 0)
        self.assertEqual(slg_db.rating_count(self.conn), 0)
        self.assertEqual(slg_db.local_count(self.conn), 0)
        self.assertEqual(slg_db.lottery_count(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
