"""Isolated real-Tk library checks. Run separately from other Tk suites."""
import contextlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

import customtkinter as ctk
from PIL import Image
import slg_account
import slg_db
import slg_gui
import slg_library_ui
import slg_local_library as library
import slg_remote
import slg_scan
import slg_translate


class Client0256UI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="slg-0256-ui-", ignore_cleanup_errors=True)
        cls.stack = contextlib.ExitStack()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.addClassCleanup(cls.stack.close)
        cls.stack.enter_context(patch.dict(os.environ, {key: cls.tmp.name for key in
            ("LOCALAPPDATA", "APPDATA", "HOME", "USERPROFILE")}))

        def offline(*args, **kwargs):
            raise RuntimeError("network forbidden in 0.25.6 UI tests")

        for obj, name, kwargs in (
            (urllib.request, "urlopen", {"side_effect": offline}),
            (urllib.request.OpenerDirector, "open", {"side_effect": offline}),
            (slg_account, "request", {"side_effect": offline}),
            (slg_account, "session", {"return_value": None}),
            (slg_remote, "report", {"return_value": None}),
            (slg_remote, "fetch_config", {"return_value": None}),
            (slg_translate, "translate_title", {"side_effect": offline}),
            (slg_translate, "translate_overview", {"side_effect": offline}),
            (slg_gui.App, "_load_dlsite_pilot", {"return_value": None}),
            (slg_gui.App, "_require_personal_access", {"return_value": True}),
        ):
            cls.stack.enter_context(patch.object(obj, name, **kwargs))
        conn = slg_db.connect()
        cls.ids = {}
        cls.folders = {}
        for catalogue in ("western_slg", "dlsite"):
            slug = "dlsite-rj00000001" if catalogue == "dlsite" else "western-fixture"
            gid, _ = slg_db.upsert_game(conn, slug,
                "https://example.invalid/" + catalogue, catalogue + " Fixture", tags=["common"])
            slg_db.set_game_catalogue(conn, gid, catalogue)
            if catalogue == "dlsite":
                slg_db.set_dlsite_metadata(conn, gid, {"work_type": "game", "languages": ["ja"]})
            folder = Path(cls.tmp.name) / (catalogue + " game")
            folder.mkdir()
            slg_db.set_local_folder(conn, gid, str(folder))
            cls.ids[catalogue], cls.folders[catalogue] = gid, folder
        cls.ids["user"] = slg_db.add_user_game(conn, "Custom Fixture", categories=["galgame"])
        conn.commit()
        conn.close()
        cls.app = slg_gui.App(notify=False)
        def close_app():
            try:
                cls.app.destroy()
            finally:
                cls.app.conn.close()
        cls.addClassCleanup(close_app)
        cls.app.geometry("1280x820")
        cls.callback_errors = []
        cls.app.report_callback_exception = lambda *args: cls.callback_errors.append(args)
        cls.wait(lambda: not getattr(cls.app, "_card_render_pending", False))

    @classmethod
    def wait(cls, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        error = []
        def poll():
            try:
                if predicate():
                    cls.app.quit()
                    return
                if time.monotonic() > deadline:
                    raise AssertionError("UI worker/render timeout")
            except BaseException as exc:
                error.append(exc)
                cls.app.quit()
                return
            cls.app.after(5, poll)
        cls.app.after(0, poll)
        cls.app.tk.mainloop()
        if error:
            raise error[0]

    def settle(self):
        self.wait(lambda: not getattr(self.app, "_card_render_pending", False))

    def setUp(self):
        app = self.app
        for child in list(app.winfo_children()):
            if isinstance(child, ctk.CTkToplevel):
                child.destroy()
        if app._search_after is not None:
            app.after_cancel(app._search_after)
            app._search_after = None
        app._catalogue_states.clear()
        app._local_view_state = None
        app._library_view_state = None
        app.catalogue_id, app.origin = "western_slg", "main"
        app.search, app.include, app.exclude = "", [], []
        app.view, app.collection_id, app.selected, app.page = None, None, None, 1
        app.category_ids, app.category_uncategorized = [], False
        app.dlsite_languages, app.dlsite_work_types = [], []
        library.clear_cover_override(app.conn, self.ids["western_slg"])
        app.conn.commit()
        app.refresh()
        self.settle()

    def tearDown(self):
        self.assertEqual(self.callback_errors, [], "Tk callback raised")

    def game(self, gid=None):
        gid = gid or self.ids["western_slg"]
        return next(row for row in self.app.rows if row["id"] == gid)

    @staticmethod
    def widgets(parent):
        for child in parent.winfo_children():
            yield child
            yield from Client0256UI.widgets(child)

    def button(self, parent, text):
        return next(w for w in self.widgets(parent)
                    if isinstance(w, ctk.CTkButton) and w.cget("text") == text)

    def cover_record(self):
        return self.app.conn.execute("SELECT cover_file FROM game_cover_overrides WHERE game_id=?",
                                     (self.ids["western_slg"],)).fetchone()

    def test_brand_and_local_union_independent_from_catalogue(self):
        self.assertEqual(self.app.title(), "黄油之王")
        self.app.set_catalogue("dlsite")
        self.app.set_library_view()
        self.settle()
        self.assertEqual({r["id"] for r in self.app.rows}, set(self.ids.values()))
        self.assertEqual(self.app.catalogue_switch.cget("state"), "disabled")

    def test_library_public_custom_search_state_round_trip(self):
        app = self.app
        app.search = "western"
        app.refresh()
        app.set_library_view()
        app.search = "Custom"
        app.refresh()
        app.set_user_view()
        self.settle()
        self.assertEqual({r["id"] for r in app.rows}, {self.ids["user"]})
        app.set_view(None)
        self.settle()
        self.assertEqual(app.search, "western")
        app.set_library_view()
        self.settle()
        self.assertEqual(app.search, "Custom")
        self.assertEqual({r["id"] for r in app.rows}, {self.ids["user"]})

    def test_reused_card_actions_show_only_in_local_view(self):
        app = self.app
        slot = app._card_pool[0]
        self.assertTrue(slot["actions"] is None or slot["actions"].winfo_manager() == "")
        app.set_library_view()
        self.settle()
        self.assertIs(slot, app._card_pool[0])
        self.assertEqual(slot["actions"].winfo_manager(), "grid")
        self.assertEqual(slot["frame"]._library_game["id"], app._page_slice()[0]["id"])
        self.button(slot["actions"], "换封面").invoke()
        self.assertTrue(app._cover_editor["win"].winfo_exists())
        app._cover_editor["win"].destroy()
        app.set_view(None)
        self.settle()
        self.assertEqual(slot["actions"].winfo_manager(), "")

    def result(self, name="Unmatched Fixture", game_id=None):
        folder = Path(self.tmp.name) / name
        folder.mkdir(exist_ok=True)
        return {"cancelled": False, "errors": [], "items": [{
            "name": name, "path": str(folder), "version": None, "game_id": game_id,
            "candidates": [], "executables": [], "reason": "ambiguous" if game_id is None else "exact",
            "status": "ambiguous" if game_id is None else "matched"}]}

    def test_scan_preview_cancel_and_cancelled_result_do_not_write(self):
        before = self.app.conn.total_changes
        self.app.open_library_scan_results(self.result())
        self.app._library_scan_dialog["win"].destroy()
        self.assertEqual(self.app.conn.total_changes, before)
        self.app.open_library_scan_results({"cancelled": True})
        self.assertEqual(self.app.conn.total_changes, before)

    def test_scan_ambiguous_unresolved_rejects_then_manual_binding_and_exe(self):
        result = self.result("Ambiguous Import")
        folder = Path(result["items"][0]["path"])
        exe = folder / "Game.exe"
        exe.touch()
        other = folder / "Launcher.exe"
        other.touch()
        result["items"][0]["executables"] = [str(exe), str(other)]
        self.app.open_library_scan_results(result)
        dialog = self.app._library_scan_dialog
        entry = dialog["states"][0]
        entry["var"].set(True)
        before = self.app.conn.total_changes
        dialog["apply"]()
        self.assertEqual(self.app.conn.total_changes, before)
        with patch.object(self.app, "_pick_game", return_value=self.game()):
            self.button(dialog["win"], "选择绑定游戏").invoke()
        menu = next(w for w in self.widgets(dialog["win"]) if isinstance(w, ctk.CTkOptionMenu))
        menu._command(str(exe))
        dialog["apply"]()
        row = self.app.conn.execute("SELECT folder_path,exe_path FROM local WHERE game_id=?",
                                     (self.ids["western_slg"],)).fetchone()
        self.assertEqual(row["folder_path"], str(folder))
        self.assertEqual(row["exe_path"], str(exe))
        # Restore the fixture binding for later independent tests.
        slg_db.set_local_folder(self.app.conn, self.ids["western_slg"], str(self.folders["western_slg"]))
        library.clear_local_binding(self.app.conn, self.ids["western_slg"], clear_folder=False, clear_exe=True)
        self.app.conn.commit()

    def test_scan_unknown_can_be_added_as_custom_game(self):
        self.app.open_library_scan_results(self.result("New Custom Import"))
        dialog = self.app._library_scan_dialog
        self.button(dialog["win"], "添加为自建游戏").invoke()
        dialog["apply"]()
        row = self.app.conn.execute("SELECT id,origin FROM games WHERE title='New Custom Import'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["origin"], "user")
        slg_db.delete_game(self.app.conn, row["id"])
        self.app.conn.commit()

    def test_scan_duplicate_ids_and_occupied_path_are_rejected(self):
        first = self.result("Duplicate One", self.ids["western_slg"])
        second = self.result("Duplicate Two", self.ids["western_slg"])
        first["items"].extend(second["items"])
        self.app.open_library_scan_results(first)
        dialog = self.app._library_scan_dialog
        self.wait(lambda: len(dialog["states"]) == 2)
        before = self.app.conn.total_changes
        dialog["apply"]()
        self.assertTrue(dialog["win"].winfo_exists())
        self.assertIn("同一游戏", dialog["status"].cget("text"))
        self.assertEqual(self.app.conn.total_changes, before)
        dialog["win"].destroy()
        occupied = self.result("Occupied Directory", self.ids["western_slg"])
        occupied["items"][0]["path"] = str(self.folders["dlsite"])
        self.app.open_library_scan_results(occupied)
        dialog = self.app._library_scan_dialog
        dialog["apply"]()
        self.assertTrue(dialog["win"].winfo_exists())
        self.assertIn("其他游戏", dialog["status"].cget("text"))
        self.assertEqual(self.app.conn.total_changes, before)

    def test_cover_file_dialog_cancel_and_bad_image_preserve_old(self):
        library.set_cover_override(self.app.conn, self.ids["western_slg"], "old-fixture.jpg")
        self.app.conn.commit()
        self.app.open_cover_editor(self.game())
        editor = self.app._cover_editor
        with patch.object(slg_library_ui.filedialog, "askopenfilename", return_value=""):
            editor["choose"]()
        self.assertFalse(editor["state"]["ready"])
        bad = Path(self.tmp.name) / "broken.jpg"
        bad.write_bytes(b"not an image")
        with patch.object(slg_library_ui.filedialog, "askopenfilename", return_value=str(bad)):
            editor["choose"]()
        self.wait(lambda: any(isinstance(w, ctk.CTkLabel) and "处理失败" in w.cget("text")
                             for w in self.widgets(editor["win"])))
        self.assertFalse(editor["state"]["ready"])
        self.assertEqual(self.cover_record()[0], "old-fixture.jpg")
        editor["win"].destroy()

    def test_cover_reset_removes_only_override(self):
        library.set_cover_override(self.app.conn, self.ids["western_slg"], "old-fixture.jpg")
        self.app.conn.commit()
        self.app.open_cover_editor(self.game())
        self.app._cover_editor["reset"]()
        self.assertIsNone(self.cover_record())
        self.assertTrue(self.folders["western_slg"].is_dir())

    def test_cover_async_preview_save_is_persisted(self):
        image = Path(self.tmp.name) / "valid.png"
        Image.new("RGB", (80, 120), "blue").save(image)
        self.app.open_cover_editor(self.game())
        editor = self.app._cover_editor
        with patch.object(slg_library_ui.filedialog, "askopenfilename", return_value=str(image)):
            editor["choose"]()
        self.wait(lambda: editor["state"]["ready"])
        self.assertIsNone(self.cover_record())
        editor["save"]()
        self.wait(lambda: not editor["win"].winfo_exists())
        record = self.cover_record()
        self.assertIsNotNone(record)
        self.assertTrue((Path(slg_db.covers_dir()) / record[0]).is_file())

    def test_closed_cover_dialog_discards_late_preparation(self):
        started, release, completed = threading.Event(), threading.Event(), threading.Event()

        def slow_prepare(_path):
            started.set()
            release.wait(3)
            completed.set()
            return Image.new("RGB", (20, 20), "red")

        self.app.open_cover_editor(self.game())
        editor = self.app._cover_editor
        try:
            with patch.object(library, "prepare_cover", side_effect=slow_prepare), \
                 patch.object(slg_library_ui.filedialog, "askopenfilename", return_value="late.png"):
                editor["choose"]()
                self.wait(started.is_set)
                editor["win"].destroy()
                release.set()
                self.wait(completed.is_set)
                self.wait(lambda: self.app.queue.empty())
            self.assertFalse(editor["state"]["ready"])
            self.assertIsNone(self.cover_record())
        finally:
            release.set()

    def test_cover_disk_error_can_retry_without_losing_old_override(self):
        library.set_cover_override(self.app.conn, self.ids["western_slg"], "old-fixture.jpg")
        self.app.conn.commit()
        self.app.open_cover_editor(self.game())
        editor = self.app._cover_editor
        editor["state"].update(image=Image.new("RGB", (20, 20), "blue"), ready=True)
        with patch.object(library, "save_cover", side_effect=OSError("disk full")):
            editor["save"]()
            self.wait(lambda: editor["state"]["ready"])
        self.assertFalse(editor["state"]["saving"])
        self.assertEqual(self.cover_record()[0], "old-fixture.jpg")
        editor["save"]()
        self.wait(lambda: not editor["win"].winfo_exists())
        self.assertNotEqual(self.cover_record()[0], "old-fixture.jpg")

    def test_cover_account_generation_change_discards_late_save(self):
        started, release, dispatched = threading.Event(), threading.Event(), threading.Event()
        original_generation = self.app._identity_generation
        original_dispatch = self.app._dispatch

        def slow_save(_image):
            started.set()
            release.wait(3)
            return "late-fixture.jpg"

        def observe(kind, payload):
            original_dispatch(kind, payload)
            if kind == "library_callback":
                dispatched.set()

        self.app.open_cover_editor(self.game())
        editor = self.app._cover_editor
        editor["state"].update(image=Image.new("RGB", (20, 20), "red"), ready=True)
        try:
            with patch.object(library, "save_cover", side_effect=slow_save), \
                 patch.object(self.app, "_dispatch", side_effect=observe):
                editor["save"]()
                self.wait(started.is_set)
                self.app._identity_generation += 1
                release.set()
                self.wait(dispatched.is_set)
            self.assertIsNone(self.cover_record())
            self.assertTrue(editor["win"].winfo_exists())
        finally:
            release.set()
            self.app._identity_generation = original_generation
            editor["win"].destroy()


if __name__ == "__main__":
    unittest.main()
