"""Real-Tk confirmation boundaries; independent process, all providers offline."""
from pathlib import Path
import threading
import io
import unittest
from unittest.mock import patch
import customtkinter as ctk
from PIL import Image
import test_client_0256_ui as fixture
import slg_library_providers as providers
import slg_library_metadata as metadata
from slg_collection_menu import ResourceCollectionMenu


class LibraryQueriesUI(unittest.TestCase):
    # Reuse setup helpers, not the original TestCase or its unrelated test methods.
    setUpClass = classmethod(fixture.Client0256UI.setUpClass.__func__)
    wait = classmethod(fixture.Client0256UI.wait.__func__)
    setUp = fixture.Client0256UI.setUp
    tearDown = fixture.Client0256UI.tearDown
    widgets = staticmethod(fixture.Client0256UI.widgets)
    button = fixture.Client0256UI.button

    def settle(self):
        self.wait(lambda: not getattr(self.app, "_card_render_pending", False)
                  and not getattr(self.app, "_library_query_pending", False))

    def facts(self, external="v270001", source="vndb", **extra):
        url = ("https://vndb.org/" + external if source == "vndb" else
               "https://www.dlsite.com/maniax/work/=/product_id/" + external + ".html")
        return {"source": source, "external_id": external, "title": "Query Fixture " + external,
                "source_url": url, **extra}

    def count_games(self):
        return self.app.conn.execute("SELECT count(*) FROM games").fetchone()[0]

    def open_query(self, on_select=None):
        self.app.open_metadata_query({"name": "Safe Query", "path": r"D:\private\game"}, on_select)
        return self.app._metadata_query_dialog

    def test_local_paths_never_reach_provider(self):
        dialog = self.open_query()
        with patch.object(providers, "search") as search:
            for term in (r"D:\private\game", "D:/private/game", "file:///D:/private/game", "/private/game"):
                dialog["query"].delete(0,"end")
                dialog["query"].insert(0,term)
                dialog["search"]()
            self.assertFalse(search.called)
            self.assertIn("本机路径",dialog["status"].cget("text"))

    def test_import_while_library_open_refreshes_and_focuses_new_game(self):
        self.app.set_library_view()
        self.settle()
        facts = self.facts(external="v270002")
        dialog = self.open_query()
        self.complete_pick(dialog,facts)
        dialog["confirm"]()
        self.settle()
        gid = metadata.find_existing(self.app.conn,"vndb",facts["external_id"])
        self.assertIn(gid,{row["id"] for row in self.app.rows})
        self.assertEqual(self.app.selected["id"],gid)

    def complete_pick(self, dialog, facts):
        with patch.object(providers, "details", return_value=facts):
            dialog["pick"](facts)
            self.wait(lambda: not dialog["state"]["busy"])
        self.assertIsNotNone(dialog["state"]["candidate"])

    def test_explicit_search_selection_confirmation_and_duplicate_identity(self):
        before = self.count_games()
        facts = self.facts()
        with patch.object(providers, "search", return_value=[facts]) as search, \
             patch.object(providers, "details", return_value=facts) as details, \
             patch.object(providers, "download_cover") as download:
            dialog = self.open_query()
            dialog["source"].set("VNDB")
            self.assertFalse(search.called)
            self.assertEqual(dialog["query"].get(), "Safe Query")
            dialog["search"]()
            self.wait(lambda: not dialog["state"]["busy"])
            self.assertEqual(search.call_args.args, ("vndb", "Safe Query"))
            self.assertIsNone(dialog["state"]["candidate"])
            self.assertFalse(details.called)
            dialog["confirm"]()
            self.assertEqual(self.count_games(), before)
            dialog["pick"](facts)
            self.wait(lambda: not dialog["state"]["busy"])
            self.assertEqual(self.count_games(), before)
            self.assertFalse(download.called)
            dialog["confirm"]()
            self.settle()
        gid = metadata.find_existing(self.app.conn, "vndb", facts["external_id"])
        self.assertIsNotNone(gid)
        self.assertEqual(self.app.conn.execute("SELECT origin FROM games WHERE id=?", (gid,)).fetchone()[0], "user")
        self.assertEqual(self.app.origin, "local")
        self.assertIn(gid, {row["id"] for row in self.app.rows})
        self.assertEqual(self.count_games(), before + 1)
        again = self.open_query()
        self.complete_pick(again, facts)
        again["confirm"]()
        self.settle()
        self.assertEqual(self.count_games(), before + 1)

    def test_failure_keeps_personal_data_and_allows_retry(self):
        before = self.count_games()
        local_before = [tuple(row) for row in self.app.conn.execute("SELECT * FROM local ORDER BY game_id")]
        dialog = self.open_query()
        with patch.object(providers, "search", side_effect=providers.ProviderError("access_denied", "fixture blocked")):
            dialog["search"]()
            self.wait(lambda: not dialog["state"]["busy"])
        self.assertIn("查询失败", dialog["status"].cget("text"))
        self.assertEqual(self.count_games(), before)
        self.assertEqual([tuple(row) for row in self.app.conn.execute("SELECT * FROM local ORDER BY game_id")], local_before)
        with patch.object(providers, "search", return_value=[]):
            dialog["search"]()
            self.wait(lambda: not dialog["state"]["busy"])
        self.assertIn("未找到", dialog["status"].cget("text"))

    def test_dlsite_identifier_query_is_user_triggered(self):
        before = self.count_games()
        self.app.open_metadata_query({"name": "Fixture", "path": r"D:\private\RJ02700008",
                                      "product_ids": ["RJ02700008"]})
        dialog = self.app._metadata_query_dialog
        with patch.object(providers, "search", return_value=[self.facts("RJ02700008", "dlsite")]) as search:
            self.assertEqual(dialog["source"].get(), "DLsite")
            self.assertEqual(dialog["query"].get(), "RJ02700008")
            self.assertFalse(search.called)
            dialog["search"]()
            self.wait(lambda: not dialog["state"]["busy"])
            self.assertEqual(search.call_args.args, ("dlsite", "RJ02700008"))
        self.assertIsNone(dialog["state"]["candidate"])
        self.assertEqual(self.count_games(), before)

    def test_cancel_discards_late_network_results(self):
        before = self.count_games()
        entered, release = threading.Event(), threading.Event()
        observed = []
        def delayed(source, term, should_stop):
            observed.append(should_stop)
            entered.set()
            release.wait(3)
            return [self.facts("v270002")]
        with patch.object(providers, "search", side_effect=delayed):
            dialog = self.open_query()
            dialog["search"]()
            self.assertTrue(entered.wait(2))
            dialog["win"].destroy()
            self.assertTrue(observed[0]())
            release.set()
            self.wait(lambda: not dialog["state"]["busy"])
        self.assertIsNone(dialog["state"]["candidate"])
        self.assertEqual(self.count_games(), before)

    def test_account_context_change_discards_late_results(self):
        before = self.count_games()
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return [self.facts("v270005")]
        with patch.object(providers, "search", side_effect=delayed):
            dialog = self.open_query()
            dialog["search"]()
            self.assertTrue(entered.wait(2))
            with patch.object(self.app, "_library_identity", return_value="different-account-context"):
                release.set()
                self.wait(lambda: not dialog["state"]["busy"])
                dialog["confirm"]()
        self.assertIsNone(dialog["state"]["candidate"])
        self.assertEqual(self.count_games(), before)

    def test_cover_failure_keeps_candidate_without_writing(self):
        before = self.count_games()
        facts = self.facts("RJ02700006", "dlsite", cover_url="https://img.dlsite.jp/fixture.jpg")
        dialog = self.open_query()
        with patch.object(providers, "details", return_value=facts), \
             patch.object(providers, "download_cover", side_effect=RuntimeError("fixture bad image")):
            dialog["pick"](facts)
            self.wait(lambda: not dialog["state"]["busy"])
        self.assertIn("封面未取得", dialog["status"].cget("text"))
        self.assertIsNotNone(dialog["state"]["candidate"])
        self.assertIsNone(dialog["state"]["cover_file"])
        self.assertEqual(self.count_games(), before)

    def image_bytes(self):
        output = io.BytesIO()
        Image.new("RGB", (48, 64), "purple").save(output, "JPEG")
        return output.getvalue()

    def test_details_identity_mismatch_cannot_be_confirmed(self):
        before = self.count_games()
        selected = self.facts("v270010")
        dialog = self.open_query()
        with patch.object(providers, "details", return_value=self.facts("v270011")):
            dialog["pick"](selected)
            self.wait(lambda: not dialog["state"]["busy"])
        self.assertIn("不一致", dialog["status"].cget("text"))
        self.assertIsNone(dialog["state"]["candidate"])
        dialog["confirm"]()
        self.assertEqual(self.count_games(), before)

    def test_cancel_cleans_new_preview_cover_and_preserves_old_cache(self):
        import slg_db
        folder = Path(slg_db.covers_dir())
        folder.mkdir(exist_ok=True)
        old = folder / "user_preexisting.jpg"
        old.write_bytes(self.image_bytes())
        facts = self.facts("v270012", cover_url="https://t.vndb.org/fixture.jpg")
        dialog = self.open_query()
        with patch.object(providers, "details", return_value=facts), \
             patch.object(providers, "download_cover", return_value=self.image_bytes()):
            dialog["pick"](facts)
            self.wait(lambda: not dialog["state"]["busy"])
        created = folder / dialog["state"]["cover_file"]
        self.assertTrue(created.is_file())
        dialog["win"].destroy()
        self.assertFalse(created.exists())
        self.assertTrue(old.is_file())

    def test_late_cover_is_removed_after_cancel(self):
        import slg_db
        folder = Path(slg_db.covers_dir())
        folder.mkdir(exist_ok=True)
        before = set(folder.iterdir())
        entered, release = threading.Event(), threading.Event()
        facts = self.facts("v270013", cover_url="https://t.vndb.org/fixture.jpg")
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return self.image_bytes()
        with patch.object(providers, "details", return_value=facts), \
             patch.object(providers, "download_cover", side_effect=delayed):
            dialog = self.open_query()
            dialog["pick"](facts)
            self.assertTrue(entered.wait(2))
            dialog["win"].destroy()
            release.set()
            self.wait(lambda: not dialog["state"]["busy"])
        self.assertEqual(set(folder.iterdir()), before)

    def test_adopted_scan_preview_cover_remains_for_final_transaction(self):
        import slg_db
        accepted = []
        facts = self.facts("v270014", cover_url="https://t.vndb.org/fixture.jpg")
        dialog = self.open_query(on_select=lambda candidate: accepted.append(candidate))
        with patch.object(providers, "details", return_value=facts), \
             patch.object(providers, "download_cover", return_value=self.image_bytes()):
            dialog["pick"](facts)
            self.wait(lambda: not dialog["state"]["busy"])
        checkbox = next(w for w in self.widgets(dialog["win"]) if isinstance(w, ctk.CTkCheckBox))
        checkbox.select()
        created = Path(slg_db.covers_dir()) / dialog["state"]["cover_file"]
        dialog["confirm"]()
        self.assertTrue(created.is_file())
        self.assertEqual(accepted[0]["personal_cover_file"], created.name)

    def test_preexisting_reused_cover_is_not_deleted(self):
        import slg_db
        import slg_local_library as library
        folder = Path(slg_db.covers_dir())
        folder.mkdir(exist_ok=True)
        old = folder / "user_reused.jpg"
        old.write_bytes(self.image_bytes())
        facts = self.facts("v270015", cover_url="https://t.vndb.org/fixture.jpg")
        dialog = self.open_query()
        with patch.object(providers, "details", return_value=facts), \
             patch.object(providers, "download_cover", return_value=self.image_bytes()), \
             patch.object(library, "save_cover", return_value=old.name):
            dialog["pick"](facts)
            self.wait(lambda: not dialog["state"]["busy"])
        dialog["win"].destroy()
        self.assertTrue(old.is_file())

    def test_closed_scan_rejects_query_confirmation(self):
        before = self.count_games()
        result = self.scan_result("closed")
        self.app.open_library_scan_results(result)
        scan = self.app._library_scan_dialog
        self.button(scan["win"], "查询资料…").invoke()
        query = self.app._metadata_query_dialog
        self.complete_pick(query, self.facts("v270003"))
        scan["win"].destroy()
        query["confirm"]()
        self.assertFalse(query["state"]["applied"])
        self.assertIn("原扫描窗口已关闭", query["status"].cget("text"))
        self.assertEqual(self.count_games(), before)

    def scan_result(self, suffix):
        folder = Path(self.tmp.name) / ("import-" + suffix)
        folder.mkdir(exist_ok=True)
        exe = folder / "Game.exe"
        exe.write_bytes(b"not executable; never launched")
        return {"items": [{"name": folder.name, "path": str(folder), "game_id": None,
            "status": "unmatched", "reason": "unmatched", "engine": "RPG Maker",
            "executables": [str(exe)]}], "errors": []}

    def test_scan_candidate_writes_only_at_final_apply(self):
        before = self.count_games()
        result = self.scan_result("confirmed")
        self.app.open_library_scan_results(result)
        scan = self.app._library_scan_dialog
        self.button(scan["win"], "查询资料…").invoke()
        query = self.app._metadata_query_dialog
        facts = self.facts("RJ02700004", "dlsite")
        self.complete_pick(query, facts)
        query["confirm"]()
        self.assertTrue(scan["states"][0]["var"].get())
        self.assertEqual(self.count_games(), before)
        self.assertIsNone(metadata.find_existing(self.app.conn, "dlsite", facts["external_id"]))
        scan["apply"]()
        self.settle()
        self.assertEqual(self.count_games(), before + 1)
        gid = metadata.find_existing(self.app.conn, "dlsite", facts["external_id"])
        local = self.app.conn.execute("SELECT folder_path, exe_path FROM local WHERE game_id=?", (gid,)).fetchone()
        self.assertEqual(local["folder_path"], result["items"][0]["path"])
        self.assertEqual(local["exe_path"], result["items"][0]["executables"][0])

    def test_same_source_identity_cannot_bind_two_directories(self):
        before = self.count_games()
        result = self.scan_result("duplicate-first")
        result["items"].extend(self.scan_result("duplicate-second")["items"])
        self.app.open_library_scan_results(result)
        scan = self.app._library_scan_dialog
        facts = self.facts("v270007")
        self.wait(lambda: len(scan["states"]) == 2)
        buttons = [w for w in self.widgets(scan["win"])
                   if isinstance(w, ctk.CTkButton) and w.cget("text") == "查询资料…"]
        self.assertEqual(len(buttons), 2)
        for button in buttons:
            button.invoke()
            query = self.app._metadata_query_dialog
            self.complete_pick(query, facts)
            query["confirm"]()
        scan["apply"]()
        self.assertIn("同一来源作品", scan["status"].cget("text"))
        self.assertEqual(self.count_games(), before)

    def test_integrated_collection_menu_height_and_value(self):
        menu = self.app.collection_menu
        self.assertIsInstance(menu, ResourceCollectionMenu)
        old_value = menu.get()
        try:
            long = "长收藏夹中文及 English Title " * 8
            for height in (32, 38, 44):
                menu.configure(height=height)
                menu.set(long)
                self.app.update_idletasks()
                right = menu._text_label.winfo_x() + menu._text_label.winfo_width()
                self.assertLessEqual(right, menu._apply_widget_scaling(menu._current_width - menu._current_height))
                self.assertEqual(menu.get(), long)
        finally:
            menu.set(old_value)


if __name__ == "__main__":
    unittest.main()
