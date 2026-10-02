"""Real desktop category controls, using isolated data and offline services."""
import os
import tempfile
import time
import unittest
from unittest import mock

import customtkinter as ctk
import slg_db
import slg_gui


class ClientCategoriesUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="slg-categories-ui-")
        cls.patches = [
            mock.patch.dict(os.environ, {"LOCALAPPDATA": cls.temp.name}),
            mock.patch.object(slg_gui.slg_account, "session", return_value=None),
            mock.patch.object(slg_gui.slg_account, "request",
                              side_effect=slg_gui.slg_account.AccountError("offline test")),
            mock.patch.object(slg_gui.slg_remote, "report"),
            mock.patch.object(slg_gui.slg_comments, "fetch_comments_page", return_value=None),
            mock.patch.object(slg_gui.slg_translate, "translate_title",
                              side_effect=slg_gui.slg_translate.TranslateError("offline test")),
            mock.patch.object(slg_gui.slg_translate, "translate_overview",
                              side_effect=slg_gui.slg_translate.TranslateError("offline test")),
        ]
        for patch in cls.patches:
            patch.start()
        cls.app = slg_gui.App(notify=False)
        cls.patches.extend([
            mock.patch.object(cls.app, "_require_personal_access", return_value=True),
            mock.patch.object(cls.app, "_has_usable_cloud_session", return_value=True),
        ])
        for patch in cls.patches[-2:]:
            patch.start()
        cls.app.theme_mode = "light"
        cls.app.geometry("1100x800")
        cls.app.deiconify()
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.app.conn.close()
        cls.app.destroy()
        for patch in reversed(cls.patches):
            patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.app.selected = None
        self.app.conn.execute("DELETE FROM games")
        self.app.conn.commit()
        self.app.include = []
        self.app.exclude = []
        self.app.search = ""
        self.app.category_ids = []
        self.app.category_uncategorized = False
        self.app.set_user_view()
        self.settle()

    def tearDown(self):
        for child in self.app.winfo_children():
            if isinstance(child, ctk.CTkToplevel):
                child.destroy()
        self.app.update()

    def settle(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.update()
            if not getattr(self.app, "_card_render_pending", False):
                self.app.update_idletasks()
                return
            time.sleep(0.005)
        self.fail("Card rendering did not finish")

    def widgets(self, root):
        for child in root.winfo_children():
            yield child
            yield from self.widgets(child)

    def control(self, root, kind, text):
        return next(widget for widget in self.widgets(root)
                    if isinstance(widget, kind) and widget.cget("text") == text)

    def dialog(self, title):
        self.app.update()
        return next(child for child in self.app.winfo_children()
                    if isinstance(child, ctk.CTkToplevel) and child.title() == title)

    def add(self, title, categories=()):
        gid = slg_db.add_user_game(self.app.conn, title, categories=categories)
        self.app.conn.commit()
        return gid

    def test_add_and_edit_multiselect_controls_persist_and_clear(self):
        self.app.open_add_game(prefill_title="Category fixture")
        win = self.dialog("添加我的游戏")
        self.control(win, ctk.CTkCheckBox, "Galgame").select()
        self.control(win, ctk.CTkCheckBox, "SLG").select()
        self.control(win, ctk.CTkButton, "保存").invoke()
        self.settle()
        gid = self.app.conn.execute("SELECT id FROM games").fetchone()["id"]
        self.assertEqual(tuple(slg_db.game_categories(self.app.conn, gid)), ("galgame", "slg"))
        self.app.open_add_game(game=dict(slg_db.get_game(self.app.conn, gid)))
        win = self.dialog("编辑我的游戏")
        gal = self.control(win, ctk.CTkCheckBox, "Galgame")
        slg = self.control(win, ctk.CTkCheckBox, "SLG")
        self.assertTrue(gal.get())
        self.assertTrue(slg.get())
        gal.deselect()
        slg.deselect()
        self.control(win, ctk.CTkCheckBox, "RPG").select()
        self.control(win, ctk.CTkButton, "保存").invoke()
        self.settle()
        self.assertEqual(tuple(slg_db.game_categories(self.app.conn, gid)), ("rpg",))

    def test_category_filter_dialog_uses_or_and_uncategorized(self):
        gal = self.add("Gal", ("galgame",))
        slg = self.add("Slg", ("slg",))
        self.add("Rpg", ("rpg",))
        empty = self.add("No category")
        self.app.refresh()
        self.settle()
        self.app.open_category_filter()
        win = self.dialog("本地游戏分类")
        self.control(win, ctk.CTkCheckBox, "Galgame").select()
        self.control(win, ctk.CTkCheckBox, "SLG").select()
        self.control(win, ctk.CTkCheckBox, "未分类").select()
        self.control(win, ctk.CTkButton, "应用").invoke()
        self.settle()
        self.assertEqual({row["id"] for row in self.app.rows}, {gal, slg, empty})
        self.app.open_category_filter()
        win = self.dialog("本地游戏分类")
        for label in ("Galgame", "SLG", "未分类"):
            self.control(win, ctk.CTkCheckBox, label).deselect()
        self.control(win, ctk.CTkButton, "应用").invoke()
        self.settle()
        self.assertEqual(len(self.app.rows), 4)

    def test_local_filter_does_not_filter_main_catalogue(self):
        self.add("Only RPG", ("rpg",))
        site, _ = slg_db.upsert_game(self.app.conn, "category-site", "https://fixture.invalid", "Site")
        self.app.conn.commit()
        self.app.category_ids = ["galgame"]
        self.app.refresh()
        self.settle()
        self.assertEqual(self.app.rows, [])
        self.app.set_view(None)
        self.settle()
        self.assertIn(site, {row["id"] for row in self.app.rows})
        self.assertEqual(self.app.category_ids, ["galgame"])

    def test_detail_shows_multi_categories_and_uncategorized(self):
        gid = self.add("Mixed", ("galgame", "slg"))
        uncategorized = self.add("Unclassified")
        self.app.refresh()
        self.settle()
        self.app.select(next(row for row in self.app.rows if row["id"] == gid))
        self.app.update()
        self.assertIn("分类：Galgame · SLG", self.app._detail_parts["sub"].cget("text"))
        self.app.select(next(row for row in self.app.rows if row["id"] == uncategorized))
        self.app.update()
        self.assertIn("分类：未分类", self.app._detail_parts["sub"].cget("text"))


if __name__ == "__main__":
    unittest.main()
