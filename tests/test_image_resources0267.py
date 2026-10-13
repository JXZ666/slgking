import gc
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from queue import Queue
import tkinter as tk
import customtkinter as ctk
from PIL import Image
from slg_image_resources import ImageLRU, ResourceLabel, ResourceButton, CoverConsumers


class CacheTests(unittest.TestCase):
    def test_lru_byte_limit_snapshot_and_visible_owner(self):
        cache = ImageLRU(2, 10, estimator=lambda value: value[0])
        visible = [4]
        cache['a'] = visible
        cache['b'] = [4]
        self.assertIs(cache.get('a'), visible)
        cache['c'] = [4]
        self.assertNotIn('b', cache)
        self.assertEqual(len(cache.items()), 2)
        cache['large'] = [11]
        self.assertEqual(len(cache), 0)
        self.assertEqual(visible, [4])

    def test_lazy_growth_remeasured(self):
        cache = ImageLRU(3, 10, estimator=lambda value: value[0])
        growing = [4]
        cache['a'] = growing
        cache['b'] = [4]
        growing[0] = 9
        cache.trim()
        self.assertNotIn('a', cache)


class Consumer:
    def __init__(self, key): self._slg_cover_key = key
    def winfo_exists(self): return True


class ConsumersTests(unittest.TestCase):
    def test_prune_cancels_only_queued_orphan_tasks(self):
        consumers, results, unblock, started = CoverConsumers(2), Queue(), Event(), Event()
        first, second = Consumer('a'), Consumer('b')
        def blocked():
            started.set()
            unblock.wait(3)
            return 'pixels'
        with ThreadPoolExecutor(1) as executor:
            consumers.request('a', first, 1, executor, blocked, lambda *a: results.put(a))
            self.assertTrue(started.wait(1))
            consumers.request('b', second, 1, executor, lambda: 'queued', lambda *a: results.put(a))
            first._slg_cover_key = 'new-a'
            second._slg_cover_key = 'new-b'
            self.assertEqual(consumers.prune(), 1)
            self.assertEqual(consumers.pending_count, 1)
            unblock.set()
        self.assertEqual(results.qsize(), 1)
        self.assertEqual(consumers.pending_count, 1)
        self.assertEqual(consumers.complete('a'), [])
        self.assertEqual(consumers.pending_count, 0)

    def test_backpressure_includes_unconsumed_results_and_deduplicates(self):
        consumers, results = CoverConsumers(2), Queue()
        with ThreadPoolExecutor(1) as executor:
            widget = Consumer('a')
            for _ in range(20):
                self.assertTrue(consumers.request('a', widget, 1, executor,
                                                lambda: 'pixels', lambda *a: results.put(a)))
            second = Consumer('b')
            self.assertTrue(consumers.request('b', second, 1, executor,
                                             lambda: 'pixels', lambda *a: results.put(a)))
        self.assertEqual(results.qsize(), 2)
        self.assertEqual(consumers.consumer_count, 2)
        self.assertFalse(consumers.request('c', Consumer('c'), 1, None, None, None))
        self.assertEqual(consumers.complete('a', 1), [(widget, 1)])
        self.assertEqual(consumers.pending_count, 1)
        self.assertEqual(consumers.complete('b', 2), [])

    def test_weak_consumers_and_reused_slot(self):
        class DeferredExecutor:
            def submit(self, work): self.work = work
        consumers, executor = CoverConsumers(), DeferredExecutor()
        widget = Consumer('a')
        ref = weakref.ref(widget)
        consumers.request('a', widget, 1, executor, lambda: None, lambda *a: None)
        del widget
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(consumers.consumer_count, 0)
        self.assertEqual(consumers.complete('a'), [])
        widget = Consumer('b')
        consumers.request('b', widget, 1, executor, lambda: None, lambda *a: None)
        widget._slg_cover_key = 'c'
        self.assertEqual(consumers.complete('b'), [])


class TkResourcesTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()

    def tearDown(self):
        if hasattr(self, 'root'):
            for token in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
                self.root.after_cancel(token)
            self.root.destroy()

    def test_destroy_and_switch_release_bound_callbacks(self):
        for cls in (ResourceLabel, ResourceButton):
            first = ctk.CTkImage(Image.new('RGB', (40, 40)))
            second = ctk.CTkImage(Image.new('RGB', (40, 40)))
            widget = cls(self.root, image=first, text='')
            self.assertEqual(len(first._configure_callback_list), 1)
            widget.configure(image=second)
            self.assertEqual(first._configure_callback_list, [])
            ref = weakref.ref(widget)
            widget.destroy()
            del widget
            gc.collect()
            self.assertEqual(second._configure_callback_list, [])
            self.assertIsNone(ref())

    def test_eviction_keeps_visible_image(self):
        cache = ImageLRU(1)
        image = ctk.CTkImage(Image.new('RGB', (40, 40)))
        cache['a'] = image
        widget = ResourceLabel(self.root, image=image, text='')
        widget.pack()
        self.root.update_idletasks()
        cache['b'] = ctk.CTkImage(Image.new('RGB', (40, 40)))
        self.assertIs(widget.cget('image'), image)
        self.assertTrue(widget._label.cget('image'))
        widget.destroy()


if __name__ == '__main__': unittest.main()
