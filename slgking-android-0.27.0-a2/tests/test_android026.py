"""Android 0.26 protocol and cooperative cancellation regressions, no cloud writes."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import slg_android_api as api
import slg_android_bootstrap as bootstrap
import slg_db
import slg_sync_server as sync


class Android026(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        api._close_conn()
        bootstrap.init(self.tmp.name, crypto="stub")

    def tearDown(self):
        api._close_conn()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, method, **params):
        return json.loads(api.call(json.dumps(dict(params, method=method))))

    def test_cloud_comment_ids_are_opaque_strings_and_ownership_is_current(self):
        cid = "a123bcdef456789abcdef"
        account = mock.Mock()
        account.session.return_value = {"account_id": "owner"}
        account.authenticated.return_value = {"comments": [{"id": cid}]}
        with mock.patch("slg_comments.fetch_comments_page", return_value={
                "comments": [{"id": cid, "nickname": "User"},
                             {"id": "abcdef23456", "account_id": "other"}], "page": 1}), \
                mock.patch.object(api, "_account_module", return_value=account):
            result = self.call("comments.page", slug="game")
        self.assertTrue(result["ok"])
        self.assertEqual(result["comments"][0]["id"], cid)
        self.assertEqual([x["own"] for x in result["comments"]], [True, False])
        with mock.patch("slg_comments.vote_comment", return_value={"votes_up": 7}) as vote, \
                mock.patch("slg_comments.delete_comment", return_value=True) as delete, \
                mock.patch("slg_comments.report_comment", return_value=True) as report:
            self.assertEqual(self.call("comments.vote", id=cid, value=1)["vote"]["votes_up"], 7)
            self.assertTrue(self.call("comments.cloud_delete", id=cid)["ok"])
            self.assertTrue(self.call("comments.report", id=cid)["ok"])
            vote.assert_called_once_with(cid, 1)
            delete.assert_called_once_with(cid)
            report.assert_called_once_with(cid)
            self.assertFalse(self.call("comments.report", id=0)["ok"])

    def test_cancel_request_idempotent_and_finished_job_not_modified(self):
        job = {"id": "test-cancel", "running": True, "stop": threading.Event()}
        with mock.patch.dict(api._JOBS, {job["id"]: job}):
            self.assertTrue(self.call("sync.cancel", job=job["id"])["requested"])
            self.assertTrue(self.call("sync.cancel", job=job["id"])["requested"])
            self.assertTrue(job["cancel_requested"])
            job["running"] = False
            self.assertFalse(self.call("sync.cancel", job=job["id"])["requested"])
            self.assertNotIn("cancelled", job)

    def test_cancel_before_manifest_does_not_contact_server(self):
        with mock.patch.object(sync, "fetch_manifest") as fetch:
            result = sync.pull(api._conn(), should_stop=lambda: True, log=lambda *_: None)
        fetch.assert_not_called()
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["catalog_updated"])

    def run_pull(self, event, download=None, merge=None, covers=None):
        with mock.patch.object(sync, "fetch_manifest", return_value={"lastmod": "new", "count": 2}), \
                mock.patch.object(sync, "_server_http_get", side_effect=download or (lambda *a, **k: b"mock")), \
                mock.patch.object(sync, "merge_catalog", side_effect=merge or (lambda *a: (1, 2))) as merged, \
                mock.patch.object(sync, "_missing_covers", return_value=[]), \
                mock.patch.object(sync, "_download_covers", side_effect=covers or (lambda *a: 0)) as covered:
            result = sync.pull(api._conn(), should_stop=event.is_set, log=lambda *_: None)
        return result, merged.call_count, covered.call_count

    def test_cancel_after_download_keeps_catalog_and_personal_state(self):
        event = threading.Event()
        slg_db.set_pref(api._conn(), "personal.marker", "keep")
        def download(*args, **kwargs):
            event.set()
            return b"mock"
        result, merged, covered = self.run_pull(event, download=download)
        self.assertTrue(result["cancelled"])
        self.assertEqual((merged, covered), (0, 0))
        self.assertIsNone(slg_db.get_pref(api._conn(), sync.PREF_LAST_PULL))
        self.assertEqual(slg_db.get_pref(api._conn(), "personal.marker"), "keep")

    def test_cancel_during_merge_finishes_commit_and_skips_covers(self):
        event = threading.Event()
        def merge(conn, path):
            slg_db.set_pref(conn, "merge.marker", "complete")
            event.set()
            return 1, 2
        result, merged, covered = self.run_pull(event, merge=merge)
        api._conn().commit()
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["catalog_updated"])
        self.assertEqual((merged, covered), (1, 0))
        self.assertEqual(slg_db.get_pref(api._conn(), sync.PREF_LAST_PULL), "new")
        self.assertEqual(slg_db.get_pref(api._conn(), "merge.marker"), "complete")

    def test_cover_cancellation_observed_but_late_cancel_does_not_change_success(self):
        event = threading.Event()
        def covers(rows, log, progress, stopped):
            event.set()
            stopped()
            return 2
        result, _, _ = self.run_pull(event, covers=covers)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["covers"], 2)
        event.clear()
        result, _, _ = self.run_pull(event)
        event.set()
        self.assertFalse(result["cancelled"])

    def test_cancelled_cover_workers_drain_without_writing(self):
        event = threading.Event()
        def fetch(*args, **kwargs):
            event.set()
            return b"late bytes"
        with mock.patch.object(sync, "_server_http_get", side_effect=fetch):
            done = sync._download_covers([{"cover_file": "late.jpg"}], lambda *_: None, None, event.is_set)
        self.assertEqual(done, 0)
        self.assertFalse(os.path.exists(os.path.join(slg_db.covers_dir(), "late.jpg")))


if __name__ == "__main__":
    unittest.main()
