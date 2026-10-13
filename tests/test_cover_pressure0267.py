"""Real-Tk integration of GUI cover methods with deterministic slow decodes."""
import gc
from concurrent.futures import Future
from contextlib import ExitStack
import os
from pathlib import Path
from queue import Queue
import tempfile
import threading
import unittest
from unittest.mock import patch
import weakref

import customtkinter as ctk
from PIL import Image
import slg_db
import slg_gui
from slg_image_resources import CoverConsumers, ResourceLabel


class ControlledExecutor:
    def __init__(self): self.tasks = []
    def submit(self, work):
        future = Future()
        self.tasks.append((future, work))
        return future
    def run_next(self):
        while self.tasks:
            future, work = self.tasks.pop(0)
            if future.set_running_or_notify_cancel():
                work()
                future.set_result(None)
                return True
        return False
    def shutdown(self, wait=False, cancel_futures=False):
        if cancel_futures:
            for future, work in self.tasks: future.cancel()
        self.tasks.clear()


class CoverRoot(slg_gui.App):
    _bind_cover = slg_gui.App._bind_cover
    _schedule_cover_retry = slg_gui.App._schedule_cover_retry
    _retry_visible_covers = slg_gui.App._retry_visible_covers
    _cover_decoded = slg_gui.App._cover_decoded
    destroy = slg_gui.App.destroy
    def __init__(self):
        ctk.CTk.__init__(self)
        self.withdraw()
        self._closing_event = threading.Event()
        self._network_enabled = False
        self._dlsite_cover_index = {}
        self._cover_consumers = CoverConsumers(32)
        self._cover_waiters = self._cover_consumers._pending
        self._cover_retry = weakref.WeakKeyDictionary()
        self._cover_retry_job = None
        self._cover_executor = ControlledExecutor()
        self._download_executor = ControlledExecutor()
        self.queue = Queue()


class CoverPressureTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.data = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='slg-cover-pressure-'))
        self.stack.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': self.data}))
        slg_gui._image_cache.clear()
        self.root = CoverRoot()
        self.labels = []

    def tearDown(self):
        if not self.root._closing_event.is_set(): self.root.destroy()
        self.labels.clear()
        slg_gui._image_cache.clear()
        gc.collect()
        self.stack.close()

    def add(self, index):
        name = '%d.png' % index
        Image.new('RGB', (80, 120), (index % 256, 30, 70)).save(Path(slg_db.covers_dir()) / name)
        game = {'slug': 'pressure-%d' % index, 'cover_file': name}
        widget = ResourceLabel(self.root, text='')
        self.labels.append(widget)
        self.root._bind_cover(widget, game, 40, 60)
        return widget

    def drain_one(self):
        ran = self.root._cover_executor.run_next()
        while not self.root.queue.empty():
            kind, payload = self.root.queue.get_nowait()
            self.assertEqual(kind, 'cover_decoded')
            self.root._cover_decoded(*payload)
        return ran

    def test_32_backpressure_eventually_paints_latest_waiting_image(self):
        for index in range(40): self.add(index)
        latest = self.labels[-1]
        placeholder = latest.cget('image')
        self.assertEqual(self.root._cover_consumers.pending_count, 32)
        self.assertEqual(len(self.root._cover_retry), 8)
        for _ in range(80):
            self.drain_one()
            self.root._retry_visible_covers()
            self.assertLessEqual(self.root._cover_consumers.pending_count, 32)
            if not self.root._cover_waiters and not self.root._cover_retry: break
        self.assertEqual(self.root._cover_consumers.pending_count, 0)
        self.assertFalse(self.root._cover_retry)
        self.assertIsNot(latest.cget('image'), placeholder)
        self.assertEqual(latest.cget('text'), '')

    def test_pending_and_retry_do_not_retain_destroyed_widget(self):
        for index in range(33): self.add(index)
        queued, waiting = self.labels[0], self.labels[-1]
        queued_ref, waiting_ref = weakref.ref(queued), weakref.ref(waiting)
        queued.destroy()
        waiting.destroy()
        self.labels.pop()
        self.labels.pop(0)
        del queued, waiting
        gc.collect()
        self.assertIsNone(queued_ref())
        self.assertIsNone(waiting_ref())
        self.assertEqual(self.root._cover_consumers.prune(), 1)
        self.assertFalse(self.root._cover_retry)

    def test_late_result_cannot_paint_reused_widget(self):
        widget = self.add(0)
        old_key = widget._slg_cover_key
        self.root._cover_executor.run_next()
        name = 'new.png'
        Image.new('RGB', (80, 120), (240, 100, 10)).save(Path(slg_db.covers_dir()) / name)
        self.root._bind_cover(widget, {'slug': 'new', 'cover_file': name}, 40, 60)
        placeholder = widget.cget('image')
        kind, payload = self.root.queue.get_nowait()
        self.root._cover_decoded(*payload)
        self.assertIs(widget.cget('image'), placeholder)
        self.assertNotIn(old_key, slg_gui._image_cache)
        self.drain_one()
        self.assertIsNot(widget.cget('image'), placeholder)

    def test_window_destroy_cancels_queued_tasks_and_releases_widget(self):
        widget = self.add(0)
        future = self.root._cover_consumers._futures[widget._slg_cover_key]
        ref = weakref.ref(widget)
        self.root.destroy()
        self.assertTrue(future.cancelled())
        self.labels.clear()
        del widget
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(self.root._cover_executor.tasks, [])


if __name__ == '__main__': unittest.main()
