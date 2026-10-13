"""Real Tk shop lifecycle regression, with a disposable DB and no network."""
import contextlib
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import slg_account
import slg_db
import slg_gui


class ShopThemeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": temp, "APPDATA": temp}))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        session = {"account_id": "offline-fixture", "display_name": "Fixture"}
        self.stack.enter_context(patch.object(slg_account, "session", return_value=session))
        self.stack.enter_context(patch.object(slg_account, "session_for_mode", return_value=session))
        self.actions = self.stack.enter_context(patch.object(slg_gui.App, "_run_cloud_action"))
        self.stack.enter_context(patch.object(slg_gui.App, "_fetch_quests"))
        self.stack.enter_context(patch.object(slg_account, "set_developer_mode"))
        self.app = slg_gui.App(notify=False)
        self.addCleanup(self.close_app)
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        self.app._cloud_balance = 200
        self.app._cloud_me = {"account_id": "offline-fixture", "balance": 200}
        self.assertTrue(Path(slg_db.db_path()).is_relative_to(Path(temp)))
        self.app.update()

    def close_app(self):
        self.app.destroy()
        self.app.conn.close()

    def pump(self):
        self.app.update_idletasks()
        self.app.update()

    def assert_shop_populated(self):
        app = self.app
        self.assertEqual(app._panel_mode, "shop")
        self.assertIsNotNone(app._shop_coord().capture_page())
        self.assertTrue(app._shop_grid.winfo_exists())
        self.assertTrue(app._shop_cards, "theme rebuild must recreate product cards")
        self.assertTrue(app._shop_balance_label.winfo_exists())
        self.assertTrue(app._shop_signin_button.winfo_exists())

    def test_theme_switch_reopens_shop_and_preserves_pending_wallet(self):
        app = self.app
        app.open_shop()
        self.pump()
        self.assert_shop_populated()
        token = app._begin_shop_write("buy")
        self.assertIsNotNone(token)
        for mode in ("light", "dark", "light"):
            old_page = app._shop_coord().capture_page()
            old_grid = app._shop_grid
            app._apply_theme(mode)
            self.pump()
            self.assert_shop_populated()
            self.assertFalse(app._shop_coord().page_is_current(old_page))
            self.assertIsNot(app._shop_grid, old_grid)
            self.assertEqual(app._shop_wallet_pending, token)
            self.assertTrue(app._shop_wallet_busy())
            self.assertIsNone(app._begin_shop_write("signin"))
        self.assertTrue(app._finish_shop_write(token))
        self.assertFalse(app._shop_wallet_busy())
        self.assertFalse(self.errors)

    def test_close_cancels_page_jobs_once_and_rejects_stale_callbacks(self):
        app = self.app
        app.open_shop()
        self.pump()
        coord = app._shop_coord()
        old_page = coord.capture_page()
        pending_read = coord.begin_read("fixture-page-read", page_bound=True)
        wallet_token = app._begin_shop_write("buy")
        calls = []
        app._shop_resize_job = app.after(10000, lambda: calls.append("resize"))
        day_job = app._shop_day_job
        resize_job = app._shop_resize_job
        coord.mark("products")
        flush_job = coord._job
        with patch.object(app, "after_cancel", wraps=app.after_cancel) as cancel:
            app._close_shop_page()
            first_calls = cancel.call_count
            app._close_shop_page()
            self.assertEqual(cancel.call_count, first_calls)
            canceled = [call.args[0] for call in cancel.call_args_list]
            for job in (day_job, resize_job):
                self.assertEqual(canceled.count(job), 1)
        remaining = app.tk.call("after", "info")
        for job in (day_job, resize_job, flush_job):
            self.assertNotIn(job, remaining)
        self.assertIsNone(app._shop_resize_job)
        self.assertIsNone(app._shop_day_job)
        self.assertIsNone(coord.capture_page())
        self.assertFalse(coord.finish_read(pending_read))
        self.assertEqual(app._shop_wallet_pending, wallet_token)
        app._shop_day_tick(old_page)
        app._shop_grid_resized(old_page)
        app._reopen_shop_if_showing(old_page)
        self.pump()
        self.assertIsNone(app._shop_day_job)
        self.assertFalse(calls)
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
