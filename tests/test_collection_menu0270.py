import unittest
from unittest import mock
import tkinter
import customtkinter as ctk
from slg_collection_menu import ResourceCollectionMenu


class CollectionMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ctk.CTk()
        cls.root.geometry("600x300")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.addCleanup(ctk.set_widget_scaling, 1)
        self.addCleanup(ctk.set_appearance_mode, "Dark")

    def test_real_bounds_and_full_value_for_all_sizes(self):
        value = "收藏夹非常长的名称 ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 2
        font = ctk.CTkFont(size=14)
        chosen = []
        menu = ResourceCollectionMenu(self.root, width=224, height=32, font=font,
            fg_color=("#eeeeee", "#151015"), button_color=("#dddddd", "#322333"),
            values=[value, "第二项"], command=chosen.append)
        self.addCleanup(menu.destroy)
        menu.pack(padx=10, pady=10)
        for scale in (1, 1.5, 2):
            ctk.set_widget_scaling(scale)
            for height in (32, 38, 44):
                for size in (13, 24):
                    font.configure(size=size)
                    menu.configure(height=height)
                    menu.set(value)
                    for mode in ("Light", "Dark"):
                        ctk.set_appearance_mode(mode)
                        self.root.update()
                        right = menu._text_label.winfo_x() + menu._text_label.winfo_width()
                        arrow_start = menu._apply_widget_scaling(menu._current_width - menu._current_height)
                        self.assertLessEqual(right, arrow_start)
                        self.assertEqual(menu.get(), value)
                        self.assertTrue(menu._text_label.cget("text").endswith("…"))
                        self.assertFalse(menu.cget("dynamic_resizing"))
        menu._dropdown_callback(value)
        self.assertEqual(chosen, [value])
        self.assertEqual(menu.cget("values")[0], value)
        with mock.patch.object(menu, "_open_dropdown_menu") as opened:
            menu._clicked()
            opened.assert_called_once()

    def test_value_variable_refresh_and_destroy_cleanup(self):
        variable = tkinter.StringVar(value="initial")
        font = ctk.CTkFont(size=14)
        menu = ResourceCollectionMenu(self.root, width=190, font=font, variable=variable,
            values=["initial", "other"])
        menu.pack()
        self.root.update()
        variable.set("外部修改的超长收藏夹名称" * 10)
        self.root.update()
        self.assertEqual(menu.get(), variable.get())
        self.assertTrue(menu._text_label.cget("text").endswith("…"))
        menu.configure(values=["完整新名称", "别的"])
        menu.set("完整新名称")
        self.root.update()
        self.assertEqual(menu.get(), "完整新名称")
        self.assertEqual(menu._dropdown_menu._values, ["完整新名称", "别的"])
        menu.destroy()
        font.configure(size=28)
        variable.set("closed")
        self.root.update()


if __name__ == "__main__":
    unittest.main()
