import json
import os
import tempfile
import unittest
import slg_db
import slg_scan_targets as targets
import slg_scan_review as review


class ScanPlanTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = [{"path": self.tmp.name, "mode": "children"}]

    def test_legacy_defaults_and_expanded_roundtrip(self):
        targets.save_targets(self.conn, self.plan)
        self.assertEqual(targets.load_targets(self.conn), self.plan)
        full = targets.normalize_plans(self.plan)[0]
        self.assertEqual((full["depth"], full["online_sources"], full["target_collection_id"]), (1, [], None))
        full.update(depth=4, online_sources=["vndb", "dlsite", "vndb"], target_collection_id=3)
        stored = targets.save_targets(self.conn, [full])
        self.assertEqual(stored[0]["online_sources"], ["vndb", "dlsite"])
        self.assertEqual(targets.load_targets(self.conn), stored)

    def test_bad_preferences_do_not_overwrite(self):
        targets.save_targets(self.conn, self.plan)
        before = slg_db.get_pref(self.conn, targets.PREF_TARGETS)
        for change in ({"depth": 0}, {"depth": 5}, {"depth": True}, {"online_sources": "dlsite"},
                       {"online_sources": ["steam"]}, {"target_collection_id": True},
                       {"target_collection_id": -1}, {"surprise": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                targets.save_targets(self.conn, [{**self.plan[0], **change}])
        self.assertEqual(slg_db.get_pref(self.conn, targets.PREF_TARGETS), before)
        with self.assertRaises(ValueError):
            targets.normalize_targets([{"path": self.tmp.name, "mode": "single", "depth": 1}])

    def test_cancel_preserves_completed_results(self):
        path = os.path.join(self.tmp.name, "game")
        review.save_recent(self.conn, self.plan, {"items": [{"path": path, "status": "unmatched"}]})
        before = review.get_recent(self.conn, self.plan)
        self.assertFalse(review.save_recent(self.conn, self.plan, {"cancelled": True, "items": []}))
        self.assertEqual(review.get_recent(self.conn, self.plan), before)

    def test_selective_retry_ignore_and_scope_isolation(self):
        paths = [os.path.join(self.tmp.name, str(i)) for i in range(5)]
        states = ["matched", "unmatched", "ambiguous", "query_failed"]
        review.save_recent(self.conn, self.plan, {
            "items": [{"path": p, "status": s} for p, s in zip(paths, states)],
            "errors": [{"path": paths[4], "kind": "PermissionError", "message": "locked"}]})
        self.assertEqual(review.get_recent(self.conn, self.plan)[-1]["status"], "read_failed")
        self.assertEqual(len(review.retry_selection(self.conn, self.plan, paths)), 4)
        review.ignore(self.conn, self.plan, paths[1:3])
        self.assertEqual(len(review.retry_selection(self.conn, self.plan, paths)), 2)
        self.assertEqual(review.unignore(self.conn, self.plan, paths[1:3]), 2)
        self.assertEqual(review.get_recent(self.conn, [{"path": os.path.join(self.tmp.name, "other")}]), [])
        self.assertEqual(review.scope_key(self.plan), review.scope_key([{**self.plan[0], "depth": 1,
            "online_sources": [], "target_collection_id": None}]))
        self.assertEqual(self.conn.execute("select count(*) from local").fetchone()[0], 0)

    def test_storage_bound_across_scopes(self):
        for scope in ("a", "b"):
            review.save_recent(self.conn, os.path.join(self.tmp.name, scope), {"items": [
                {"path": os.path.join(self.tmp.name, scope, str(i)), "status": "unmatched"}
                for i in range(1500)]})
        data = json.loads(slg_db.get_pref(self.conn, review.PREF_REVIEW))
        self.assertEqual(sum(len(s["items"]) for s in data["scopes"].values()), 2000)


if __name__ == "__main__":
    unittest.main()
