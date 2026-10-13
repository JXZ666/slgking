"""Queue ordering and stale asynchronous cover results, without network."""
import os
import queue
import sys
import tempfile
import unittest
import weakref
from types import SimpleNamespace
from unittest import mock
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="slg-dispatch-")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import slg_gui
from slg_motion import TimeBudget
from slg_image_resources import CoverConsumers

class DispatchTests(unittest.TestCase):
    def test_inflight_library_wait_is_short_and_idle_pump_remains_single(self):
        app = SimpleNamespace(queue=queue.Queue(), _dispatch=mock.Mock(),
                              after=mock.Mock(), _drain=lambda: None,
                              _library_query_pending=True)
        slg_gui.App._drain(app)
        app.after.assert_called_once_with(16,app._drain)
        app.after.reset_mock()
        app._library_query_pending = False
        slg_gui.App._drain(app)
        app.after.assert_called_once_with(50,app._drain)
        app._dispatch.assert_not_called()

    def test_progress_coalesces_but_terminal_and_account_results_remain_ordered(self):
        messages = [("progress", 1), ("progress", 2), ("done", "done"),
                    ("cloud_action", "account"), ("progress", 3), ("progress", 4),
                    ("lottery", "result")]
        delivered = []
        app = SimpleNamespace(queue=queue.Queue(), _dispatch=lambda *m: delivered.append(m),
                              after=mock.Mock(), _drain=lambda: None)
        for message in messages:
            app.queue.put(message)
        slg_gui.App._drain(app)
        self.assertEqual(delivered, [("progress", 2), ("done", "done"),
            ("cloud_action", "account"), ("progress", 4), ("lottery", "result")])

    def test_slow_handler_yields_without_losing_remaining_messages(self):
        now = [0.0]
        delivered = []
        def dispatch(*message):
            delivered.append(message)
            now[0] += .007
        app = SimpleNamespace(queue=queue.Queue(), _dispatch=dispatch,
                              after=mock.Mock(), _drain=lambda: None)
        app.queue.put(("done", "first")); app.queue.put(("done", "second"))
        with mock.patch.object(slg_gui, "TimeBudget", side_effect=lambda **k:
                               TimeBudget(clock=lambda: now[0], **k)):
            slg_gui.App._drain(app)
        self.assertEqual(delivered, [("done", "first")])
        self.assertEqual(app.queue.qsize(), 1)
        self.assertEqual(app.after.call_args.args[0], 1)

    def test_late_cover_cannot_repaint_reused_widget(self):
        old_key = ("old", "old.jpg", 72, 96)
        widget = mock.Mock()
        widget._slg_cover_key = ("new", "new.jpg", 72, 96)
        consumers = CoverConsumers()
        consumers._pending[old_key] = weakref.WeakKeyDictionary({widget: 1})
        app = SimpleNamespace(_cover_consumers=consumers,
                              _cover_waiters=consumers._pending)
        with mock.patch.object(slg_gui.ctk, "CTkImage", return_value=object()):
            slg_gui.App._cover_decoded(app, old_key, object())
        widget.configure.assert_not_called()
        slg_gui._image_cache.pop(old_key, None)
