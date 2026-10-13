import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import slg_crash as crash


class CrashLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local"), "TEMP": str(self.root / "temp")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.old_path = crash._last_path
        crash._last_path = None
        self.addCleanup(setattr, crash, "_last_path", self.old_path)

    def record(self, message="异常测试", **kwargs):
        try:
            raise ValueError(message)
        except ValueError:
            return crash.record_exception(*sys.exc_info(), **kwargs)

    def test_two_errors_and_context(self):
        crash.set_context("0.27.0", "test")
        path = self.record("first")
        self.record("second")
        text = crash.read_log_tail(path)["text"]
        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertIn("version=0.27.0 channel=test", text)

    def test_fallback_and_both_fail(self):
        (self.root / "local").write_text("blocks directory")
        path = self.record()
        self.assertEqual(path, str(self.root / "temp" / "slgking" / "crash.log"))
        with patch.object(crash, "_write", side_effect=PermissionError("denied")):
            self.assertIsNone(self.record())

    def test_rotation_and_legacy_preservation(self):
        path = self.root / "local" / "slgking" / "crash.log"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"old" * 100)
        with patch.object(crash, "MAX_LOG_BYTES", 200):
            self.record("x", preferred_path=path)
        legacy = list(path.parent.glob("crash.log.legacy-*"))
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].read_bytes(), b"old" * 100)
        with patch.object(crash, "MAX_LOG_BYTES", 400):
            for index in range(12):
                self.record("r" * 30 + str(index), preferred_path=path)
        self.assertTrue(path.with_name("crash.log.1").exists())
        self.assertFalse(path.with_name("crash.log.4").exists())

    def test_long_unicode_entry_is_bounded(self):
        path = Path(self.record("中文🙂" * 40000))
        self.assertLessEqual(path.stat().st_size, crash.MAX_ENTRY_BYTES)
        text = path.read_text(encoding="utf-8")
        self.assertIn("entry truncated", text)

    def test_concurrent_entries(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda index: self.record(f"error-{index:03}"), range(50)))
        self.assertTrue(all(paths))
        text = crash.read_log_tail()["text"]
        for index in range(50):
            self.assertEqual(text.count(f"ValueError: error-{index:03}"), 1)

    def test_credentials_and_user_directory_redacted(self):
        private = "Bearer secretXYZ token='tok123' password=p123 api_key=k456 https://example.org/?token=urlSECRET&x=1 C:\\Users\\Alice\\data.py /home/Bob/code.py https://name:pass@example.org"
        path = self.record(private)
        raw = Path(path).read_text(encoding="utf-8")
        for secret in ("secretXYZ", "tok123", "p123", "k456", "urlSECRET", "Alice", "Bob", "name:pass"):
            self.assertNotIn(secret, raw)
        self.assertIn("data.py", raw)

    def test_legacy_view_redacts_and_tail_is_bounded(self):
        path = self.root / "old.log"
        path.write_text("z" * 300000 + "\nBearer oldsecret", encoding="utf-8")
        result = crash.read_log_tail(path, 999999)
        self.assertNotIn("oldsecret", result["text"])
        self.assertLess(len(result["text"]), crash.MAX_TAIL_BYTES + 100)
        self.assertIn("showing log tail", result["text"])

    def test_missing_empty_and_read_failure(self):
        self.assertEqual(crash.read_log_tail(self.root / "missing")["text"], "")
        self.assertIsNone(crash.read_log_tail(self.root / "missing")["error"])
        empty = self.root / "empty"
        empty.touch()
        self.assertEqual(crash.read_log_tail(empty)["text"], "")
        self.assertTrue(crash.read_log_tail(self.root)["error"])

    def test_tail_drops_partial_secret_line(self):
        path = self.root / "partial.log"
        path.write_text("token=" + "secret" * 100 + "\nnext safe line\n")
        result = crash.read_log_tail(path, 64)
        self.assertNotIn("secret", result["text"])
        self.assertIn("next safe line", result["text"])

    def test_startup_unknown_and_escaped_home(self):
        with patch.dict(crash._context, {"version": "unknown (startup)", "channel": "unknown (startup)"}):
            path = self.record(r'C:\\Users\\Alice\\file.txt')
        text = crash.read_log_tail(path)["text"]
        self.assertIn("unknown (startup)", text)
        self.assertNotIn("Alice", text)

    def test_available_logs_recent_first(self):
        primary = Path(self.record())
        primary.with_name("crash.log.1").write_text("backup")
        fallback = self.root / "temp" / "slgking" / "crash.log"
        self.record(preferred_path=fallback)
        logs = crash.available_logs()
        self.assertEqual(logs[0], str(fallback))
        self.assertIn(str(primary.with_name("crash.log.1")), logs)

    def test_hooks_idempotent_and_no_console(self):
        old_main, old_thread = sys.excepthook, threading.excepthook
        self.addCleanup(setattr, sys, "excepthook", old_main)
        self.addCleanup(setattr, threading, "excepthook", old_thread)
        with patch.object(sys, "stderr", None):
            crash.install_hooks()
            hook = sys.excepthook
            crash.install_hooks()
            self.assertIs(sys.excepthook, hook)
            sys.excepthook(RuntimeError, RuntimeError("main failure"), None)
            def fail():
                raise RuntimeError("thread failure")
            thread = threading.Thread(target=fail)
            thread.start()
            thread.join()
        text = crash.read_log_tail()["text"]
        self.assertIn("main failure", text)
        self.assertIn("thread failure", text)
        self.assertIn("source=background_thread", text)


if __name__ == "__main__":
    unittest.main()
