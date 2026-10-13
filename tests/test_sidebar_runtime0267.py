"""Offline real-Tk functional acceptance; no timing/performance assertions."""
import contextlib
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import slg_account
import slg_db
import slg_gui
from slg_sidebar_layout import COMPACT, NORMAL, LARGE, sidebar_layout


class SidebarRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": temp, "APPDATA": temp}))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        session = {"account_id": "runtime-fixture", "display_name": "Fixture"}
        for method in ("session", "session_for_mode"):
            self.stack.enter_context(patch.object(slg_account, method, return_value=session))
        self.stack.enter_context(patch.object(slg_account, "set_developer_mode"))
        for method in ("_run_cloud_action", "_fetch_quests"):
            self.stack.enter_context(patch.object(slg_gui.App, method))
        self.app = slg_gui.App(notify=False)
        self.addCleanup(self.close_app)
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        self.app.minsize(0, 0)
        self.app._cloud_balance = 200
        self.app._cloud_me = {"account_id": "runtime-fixture", "balance": 200}
        self.assertTrue(Path(slg_db.db_path()).is_relative_to(Path(temp)))
        self.pump()

    def close_app(self):
        self.app.destroy()
        self.app.conn.close()
        slg_gui.set_font_scale("normal")
        slg_gui.ctk.set_window_scaling(1.0)
        slg_gui.ctk.set_widget_scaling(1.0)

    def pump(self):
        self.app.update_idletasks()
        self.app.update()

    def resize(self, width, height):
        self.app.state("normal")
        self.app.geometry(f"{width}x{height}")
        self.pump()
        # CTk locks native min/max for one second after scaling, even when
        # setting the same scale. Wait for the real WM resize, not layout speed.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            scale = slg_gui.ctk.ScalingTracker.get_window_scaling(self.app)
            if (abs(self.app.winfo_width() / scale - width) <= 2 and
                    abs(self.app.winfo_height() / scale - height) <= 2):
                break
            wake = slg_gui.tk.BooleanVar(master=self.app, value=False)
            self.app.after(20, lambda v=wake:v.set(True))
            self.app.wait_variable(wake)
            self.app.geometry(f"{width}x{height}")
            self.pump()
        job = getattr(self.app, "_sidebar_resize_job", None)
        if job is not None:
            self.app.after_cancel(job)
        self.app._apply_sidebar_resize()
        self.pump()

    def assert_metrics(self, layout, factor=1.0):
        app = self.app
        self.assertEqual(app._sidebar_layout, layout)
        self.assertEqual(app._sidebar_bar.cget("width"), layout.width)
        self.assertGreater(len(app._sidebar_nav_buttons), 5)
        for button in app._sidebar_nav_buttons:
            self.assertEqual(button.cget("height"), layout.row_height)
            self.assertEqual(button.cget("font").cget("size"), round(layout.font_size * factor))
            self.assertTrue(button.winfo_exists())
        self.assertEqual(app.collection_menu.cget("height"), layout.row_height)

    def test_actual_geometry_profiles_scaling_fonts_and_scroll(self):
        app = self.app
        sidebar = app._sidebar_bar
        buttons = tuple(app._sidebar_nav_buttons)
        with patch.object(app, "_build_sidebar", wraps=app._build_sidebar) as rebuild:
            for scale in (1.0, 1.25):
                slg_gui.ctk.set_window_scaling(scale)
                slg_gui.ctk.set_widget_scaling(scale)
                for width, height, expected in ((1040, 720, COMPACT), (1200, 850, NORMAL), (1700, 1000, LARGE)):
                    with self.subTest(scale=scale, width=width, height=height):
                        self.resize(width, height)
                        effective = slg_gui.ctk.ScalingTracker.get_window_scaling(app)
                        self.assertAlmostEqual(app.winfo_width() / effective, width, delta=2)
                        self.assertAlmostEqual(app.winfo_height() / effective, height, delta=2)
                        self.assert_metrics(expected)
                self.assertIs(app._sidebar_bar, sidebar)
                self.assertEqual(tuple(app._sidebar_nav_buttons), buttons)
            # Exercise the actual OS maximized state as well as explicit sizes.
            slg_gui.ctk.set_window_scaling(1.0)
            slg_gui.ctk.set_widget_scaling(1.0)
            app.state("zoomed")
            self.pump()
            self.assertEqual(app.state(), "zoomed")
            effective = slg_gui.ctk.ScalingTracker.get_window_scaling(app)
            expected = sidebar_layout(app.winfo_width() / effective, app.winfo_height() / effective)
            if app._sidebar_resize_job is not None:
                app.after_cancel(app._sidebar_resize_job)
            app._sidebar_layout = None
            app._apply_sidebar_resize()
            self.assert_metrics(expected)
            self.resize(1700, 1000)
            slg_gui.set_font_scale("xl")
            self.assert_metrics(LARGE, 1.4)
            self.resize(1040, 600)
            self.assert_metrics(COMPACT, 1.4)
            nav = buttons[0].master
            canvas = nav._parent_canvas
            self.assertGreater(canvas.winfo_height(), 20)
            self.assertLess(canvas.yview()[1] - canvas.yview()[0], 1)
            canvas.yview_moveto(0)
            self.pump()
            first = buttons[0]
            self.assertGreaterEqual(first.winfo_rooty(), canvas.winfo_rooty())
            canvas.yview_moveto(1)
            self.pump()
            last = buttons[-1]
            self.assertEqual(last.cget("text"), "帮助文档")
            self.assertGreaterEqual(last.winfo_rooty(), canvas.winfo_rooty())
            self.assertLessEqual(last.winfo_rooty() + last.winfo_height(), canvas.winfo_rooty() + canvas.winfo_height() + 2)
            self.assertEqual(rebuild.call_count, 0)
        self.assertFalse(self.errors)

    def test_resize_events_merge_and_use_final_geometry(self):
        app = self.app
        self.resize(1040, 720)
        old_buttons = tuple(app._sidebar_nav_buttons)
        with patch.object(app, "after", wraps=app.after) as after:
            for _ in range(40):
                app._queue_sidebar_resize()
            jobs = [call for call in after.call_args_list if call.args[0] == 100]
            self.assertEqual(len(jobs), 1)
            job = app._sidebar_resize_job
            app.geometry("1200x850")
            app.update_idletasks()
            self.assertEqual(app._sidebar_resize_job, job)
        # CTk scaling temporarily locks the native min/max window dimensions
        # for 1 second. Wait for real geometry and the scheduled resize callback
        # instead of treating a 180ms sleep as functional completion.
        def settled():
            scale = slg_gui.ctk.ScalingTracker.get_window_scaling(app)
            if (abs(app.winfo_width()/scale - 1200) < 2
                    and abs(app.winfo_height()/scale - 850) < 2
                    and app._sidebar_resize_job is None):
                app.quit()
            else:
                app.after(20, settled)
        timeout = app.after(3000, app.quit)
        app.after(20, settled)
        app.mainloop()
        app.after_cancel(timeout)
        self.pump()
        scale = slg_gui.ctk.ScalingTracker.get_window_scaling(app)
        self.assertAlmostEqual(app.winfo_width()/scale, 1200, delta=2)
        self.assertAlmostEqual(app.winfo_height()/scale, 850, delta=2)
        self.assert_metrics(NORMAL)
        self.assertEqual(tuple(app._sidebar_nav_buttons), old_buttons)
        self.assertFalse(self.errors)

    def test_theme_rebuild_preserves_single_card_key_registration(self):
        app = self.app
        bindings = dict(app._card_key_bindings)
        before = {name for name in app._tclCommands if name.endswith('_on_card_key')}
        self.assertEqual(len(before), 5)
        for theme in ('light', 'dark', 'light', 'dark'):
            app._apply_theme(theme)
            self.pump()
            self.assertEqual(app._card_key_bindings, bindings)
            self.assertEqual({name for name in app._tclCommands if name.endswith('_on_card_key')}, before)
            for sequence, funcid in bindings.items():
                self.assertIn(funcid, app.bind(sequence))
        self.assertFalse(self.errors)

    def test_stale_shop_resize_keeps_new_page_job(self):
        app = self.app
        app.open_shop()
        self.pump()
        old_page = app._shop_coord().capture_page()
        app._close_shop_page()
        app.open_shop()
        self.pump()
        new_page = app._shop_coord().capture_page()
        self.assertNotEqual(old_page, new_page)
        app._on_shop_grid_configure()
        job = app._shop_resize_job
        app._shop_grid_resized(old_page)
        self.assertEqual(app._shop_resize_job, job)
        self.assertIn(job, app.tk.call("after", "info"))
        app._close_shop_page()
        self.assertNotIn(job, app.tk.call("after", "info"))
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
