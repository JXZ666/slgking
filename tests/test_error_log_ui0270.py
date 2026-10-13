"""Offline real-Tk error-log checks; run separately from other Tk suites."""
import contextlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import customtkinter as ctk
import slg_crash
import slg_gui

import test_client_0256_ui as fixture


class ErrorLog0270UI(unittest.TestCase):
    # Reuse only the isolated fixture, not its historical test methods.
    @classmethod
    def setUpClass(cls):
        fixture.Client0256UI.setUpClass.__func__(cls)

    @classmethod
    def wait(cls, predicate, timeout=5):
        fixture.Client0256UI.wait.__func__(cls, predicate, timeout)

    def settle(self):
        self.wait(lambda: not getattr(self.app, "_card_render_pending", False))

    def setUp(self):
        fixture.Client0256UI.setUp(self)
        self.callback_errors.clear()
        self.logs = tempfile.TemporaryDirectory(prefix="error-log-ui-", ignore_cleanup_errors=True)
        self.addCleanup(self.logs.cleanup)
        self.path = Path(self.logs.name) / "crash.log"
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(slg_crash, "_paths", return_value=[self.path]))
        self.patches.enter_context(patch.object(slg_crash, "_last_path", None))
        self.app._crash_reported = False

    def tearDown(self):
        self.assertEqual(self.callback_errors, [], "Unexpected Tk callback error")

    @staticmethod
    def widgets(parent):
        return fixture.Client0256UI.widgets(parent)

    def button(self, parent, text):
        return fixture.Client0256UI.button(self, parent, text)

    def open_viewer(self):
        self.app.open_error_log()
        dialog = self.app._error_log_dialog
        self.wait(lambda: not dialog["path"]["pending"])
        return dialog

    def test_continuous_tk_exceptions_are_logged_but_warn_once(self):
        with patch.object(slg_gui.messagebox, "showwarning") as warning:
            for kind, marker in ((ValueError, "first-tk-failure"), (RuntimeError, "second-tk-failure")):
                try:
                    raise kind(marker)
                except Exception:
                    self.app._report_callback_exception(*sys.exc_info())
            self.assertEqual(warning.call_count, 1)
        recorded = self.path.read_text(encoding="utf-8")
        self.assertIn("first-tk-failure", recorded)
        self.assertIn("second-tk-failure", recorded)
        self.assertIn("ValueError", recorded)
        self.assertIn("RuntimeError", recorded)

    def test_read_only_manual_refresh_and_no_polling(self):
        self.path.write_text("initial-ui-record\n", encoding="utf-8")
        original = slg_crash.read_log_tail
        with patch.object(slg_crash, "read_log_tail", wraps=original) as reader:
            dialog = self.open_viewer()
            self.assertEqual(dialog["text"].cget("state"), "disabled")
            self.assertIn("initial-ui-record", dialog["text"].get("1.0", "end"))
            dialog["text"].insert("end", "forbidden-edit")
            self.assertNotIn("forbidden-edit", dialog["text"].get("1.0", "end"))
            self.path.write_text("new-ui-record\n", encoding="utf-8")
            # A real Tk interval with no refresh must not read the file again.
            passed = []
            self.app.after(180, lambda: passed.append(True))
            self.wait(lambda: bool(passed))
            self.assertEqual(reader.call_count, 1)
            self.assertNotIn("new-ui-record", dialog["text"].get("1.0", "end"))
            self.button(dialog["win"], "刷新").invoke()
            self.wait(lambda: not dialog["path"]["pending"])
            self.assertEqual(reader.call_count, 2)
            self.assertIn("new-ui-record", dialog["text"].get("1.0", "end"))

    def test_empty_log_and_folder_action(self):
        dialog = self.open_viewer()
        self.assertIn("尚无错误记录", dialog["text"].get("1.0", "end"))
        self.path.write_text("folder-ui-record\n", encoding="utf-8")
        dialog["refresh"]()
        self.wait(lambda: not dialog["path"]["pending"])
        with patch("slg_log_ui.os.startfile") as startfile:
            self.button(dialog["win"], "打开所在目录").invoke()
        startfile.assert_called_once_with(str(self.path.parent))

    def test_viewer_survives_theme_changes(self):
        self.path.write_text("theme-ui-record\n", encoding="utf-8")
        dialog = self.open_viewer()
        initial = self.app.theme_mode
        try:
            for mode in ("Light", "Dark", "System"):
                self.app._apply_theme(mode)
                self.settle()
                self.assertTrue(dialog["win"].winfo_exists())
                self.assertEqual(dialog["text"].cget("state"), "disabled")
                self.assertIn("theme-ui-record", dialog["text"].get("1.0", "end"))
        finally:
            self.app._apply_theme(initial)
            self.settle()

    def test_close_before_background_result_does_not_touch_dead_window(self):
        started, release, enqueued = threading.Event(), threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            started.set()
            release.wait(5)
            return {"path": str(self.path), "text": "late-ui-record", "error": None}
        original_put = self.app.queue.put
        def observe_put(message, *args, **kwargs):
            original_put(message, *args, **kwargs)
            if (message[0] == "library_callback" and isinstance(message[1][1], dict)
                    and message[1][1].get("text") == "late-ui-record"):
                enqueued.set()
        with patch.object(slg_crash, "read_log_tail", side_effect=delayed), \
                patch.object(self.app.queue, "put", side_effect=observe_put):
            self.app.open_error_log()
            dialog = self.app._error_log_dialog
            try:
                self.wait(started.is_set)
                self.assertTrue(dialog["path"]["pending"])
                dialog["win"].destroy()
                release.set()
                self.wait(lambda: enqueued.is_set() and self.app.queue.empty())
                self.assertFalse(dialog["win"].winfo_exists())
                self.assertEqual(self.callback_errors, [])
            finally:
                release.set()

    def test_settings_exposes_error_log_entry(self):
        self.app.open_settings()
        dialogs = [w for w in self.app.winfo_children() if isinstance(w, ctk.CTkToplevel)]
        buttons = [w for win in dialogs for w in self.widgets(win)
                   if isinstance(w, ctk.CTkButton) and "查看本地错误日志" in w.cget("text")]
        self.assertEqual(len(buttons), 1)
        buttons[0].invoke()
        dialog = self.app._error_log_dialog
        self.wait(lambda: not dialog["path"]["pending"])
        self.assertTrue(dialog["win"].winfo_exists())


if __name__ == "__main__":
    unittest.main()

