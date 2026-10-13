"""Sequential real-Tk review, collection and staged-cover transaction checks."""
from pathlib import Path
import unittest
from unittest.mock import patch
import customtkinter as ctk
import test_library_queries0270_ui as fixture
import slg_db
import slg_library_metadata as metadata
import slg_library_providers as providers
import slg_scan_review as review
import slg_scan_targets as targets


class LibraryReviewUI(unittest.TestCase):
    setUpClass = classmethod(fixture.LibraryQueriesUI.setUpClass.__func__)
    wait = classmethod(fixture.LibraryQueriesUI.wait.__func__)
    tearDown = fixture.LibraryQueriesUI.tearDown
    settle = fixture.LibraryQueriesUI.settle
    widgets = staticmethod(fixture.LibraryQueriesUI.widgets)
    button = fixture.LibraryQueriesUI.button
    facts = fixture.LibraryQueriesUI.facts
    count_games = fixture.LibraryQueriesUI.count_games
    scan_result = fixture.LibraryQueriesUI.scan_result
    image_bytes = fixture.LibraryQueriesUI.image_bytes

    def setUp(self):
        fixture.LibraryQueriesUI.setUp(self)
        self.plans = [{"path": self.tmp.name, "mode": "children"}]
        targets.save_targets(self.app.conn, self.plans)
        slg_db.set_pref(self.app.conn, review.PREF_REVIEW, '{"version":1,"scopes":{}}')

    def local_rows(self):
        return [tuple(row) for row in self.app.conn.execute("SELECT * FROM local ORDER BY game_id")]

    def test_review_empty_does_not_start_scan(self):
        before = self.local_rows()
        self.app.open_scan_review()
        dialog = self.app._scan_review_dialog
        self.assertEqual(dialog["states"], [])
        self.assertIn("暂无待处理", dialog["status"].cget("text"))
        with patch.object(self.app, "_run_job") as job:
            dialog["action"]("retry")
            job.assert_not_called()
        self.assertEqual(self.local_rows(), before)

    def test_review_failure_ignore_undo_and_selected_retry(self):
        before = self.local_rows()
        paths = [str(Path(self.tmp.name) / ("review-" + str(i))) for i in range(4)]
        review.save_recent(self.app.conn, self.plans, {"items": [
            {"path": paths[0], "status": "unmatched"},
            {"path": paths[1], "status": "ambiguous"},
            {"path": paths[2], "status": "query_failed", "message": "fixture access denied"}],
            "errors": [{"path": paths[3], "kind": "PermissionError", "message": "fixture locked"}]})
        self.app.open_scan_review()
        dialog = self.app._scan_review_dialog
        self.wait(lambda: len(dialog["states"]) == 4)
        self.assertEqual({row["status"] for row, var in dialog["states"]},
                         {"unmatched", "ambiguous", "query_failed", "read_failed"})
        next(var for row, var in dialog["states"] if row["path"] == paths[2]).set(True)
        dialog["action"]("ignore")
        self.wait(lambda: len(dialog["states"]) == 4 and any(
            row["path"] == paths[2] and row["ignored"] for row, var in dialog["states"]))
        next(var for row, var in dialog["states"] if row["path"] == paths[2]).set(True)
        with patch.object(self.app, "_run_job") as job:
            dialog["action"]("retry")
            job.assert_not_called()
        self.assertIn("先撤销忽略", dialog["status"].cget("text"))
        dialog["action"]("unignore")
        self.wait(lambda: len(dialog["states"]) == 4 and not any(
            row["path"] == paths[2] and row["ignored"] for row, var in dialog["states"]))
        next(var for row, var in dialog["states"] if row["path"] == paths[0]).set(True)
        with patch.object(self.app, "_run_job") as job:
            dialog["action"]("retry")
            job.assert_called_once()
            retried = job.call_args.args[2]
            self.assertEqual([t["path"] for t in retried], [paths[0]])
            self.assertEqual(retried[0]["mode"], "single")
        self.assertEqual(self.local_rows(), before)

    def choose_manual_new(self, dialog):
        buttons = [w for w in self.widgets(dialog["win"])
                   if isinstance(w, ctk.CTkButton) and w.cget("text") == "添加为自建游戏"]
        for button in buttons:
            button.invoke()

    def test_scan_plan_collection_added_after_confirmation(self):
        cid = slg_db.create_collection(self.app.conn, "review fixture collection")
        result = self.scan_result("review-collection")
        result["items"][0]["target_collection_id"] = cid
        before = self.count_games()
        self.app.open_library_scan_results(result)
        dialog = self.app._library_scan_dialog
        self.choose_manual_new(dialog)
        self.assertEqual(self.count_games(), before)
        dialog["apply"]()
        self.settle()
        local = self.app.conn.execute("SELECT game_id FROM local WHERE folder_path=?",
                                      (result["items"][0]["path"],)).fetchone()
        self.assertIsNotNone(local)
        self.assertIn(cid, slg_db.collections_for_game(self.app.conn, local[0]))

    def test_later_invalid_path_rolls_back_first_game_and_collection(self):
        cid = slg_db.create_collection(self.app.conn, "rollback fixture collection")
        result = self.scan_result("review-rollback-first")
        result["items"][0]["target_collection_id"] = cid
        second = self.scan_result("review-rollback-second")["items"][0]
        result["items"].append(second)
        before, local_before = self.count_games(), self.local_rows()
        self.app.open_library_scan_results(result)
        dialog = self.app._library_scan_dialog
        self.wait(lambda: len(dialog["states"]) == 2)
        self.choose_manual_new(dialog)
        Path(second["executables"][0]).unlink()
        Path(second["path"]).rmdir()
        dialog["apply"]()
        self.assertIn("导入失败", dialog["status"].cget("text"))
        self.assertEqual(self.count_games(), before)
        self.assertEqual(self.local_rows(), local_before)
        self.assertEqual(self.app.conn.execute("SELECT count(*) FROM collection_items WHERE collection_id=?", (cid,)).fetchone()[0], 0)

    def adopt_cover(self, suffix, external):
        result = self.scan_result(suffix)
        self.app.open_library_scan_results(result)
        scan = self.app._library_scan_dialog
        self.button(scan["win"], "查询资料…").invoke()
        query = self.app._metadata_query_dialog
        facts = self.facts(external, cover_url="https://t.vndb.org/fixture.jpg")
        with patch.object(providers, "details", return_value=facts), \
             patch.object(providers, "download_cover", return_value=self.image_bytes()):
            query["pick"](facts)
            self.wait(lambda: not query["state"]["busy"])
        checkbox = next(w for w in self.widgets(query["win"]) if isinstance(w, ctk.CTkCheckBox))
        checkbox.select()
        path = Path(slg_db.covers_dir()) / query["state"]["cover_file"]
        query["confirm"]()
        self.assertTrue(scan["states"][0]["item"]["provider_metadata"]["personal_cover_owned"])
        self.assertTrue(path.is_file())
        return scan, path

    def test_scan_cancel_removes_adopted_owned_cover_without_import(self):
        before = self.count_games()
        scan, path = self.adopt_cover("review-cover-cancel", "v270101")
        scan["win"].destroy()
        self.assertFalse(path.exists())
        self.assertEqual(self.count_games(), before)

    def test_scan_commit_preserves_adopted_cover(self):
        scan, path = self.adopt_cover("review-cover-commit", "v270102")
        scan["apply"]()
        self.settle()
        gid = metadata.find_existing(self.app.conn, "vndb", "v270102")
        self.assertIsNotNone(gid)
        self.assertTrue(path.is_file())
        row = self.app.conn.execute("SELECT cover_file FROM game_cover_overrides WHERE game_id=?", (gid,)).fetchone()
        self.assertEqual(row[0], path.name)


if __name__ == "__main__":
    unittest.main()
