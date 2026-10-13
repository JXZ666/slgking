"""Real-Tk callback ownership; isolated synthetic widgets, no app or data."""
import gc
import sys
import unittest
import weakref
from unittest.mock import patch
import customtkinter as ctk
from slg_image_resources import ResourceScrollableFrame


class ScrollableResourcesTests(unittest.TestCase):
    def setUp(self):
        self.root = ctk.CTk()
        self.root.geometry('600x400')
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.root.update()

    def tearDown(self):
        for token in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.tk.call('after', 'cancel', token)
        self.root.destroy()
        gc.collect()

    def test_upstream_destroy_retains_frame_and_root_handlers(self):
        before = set(self.root._tclCommands)
        widget = ctk.CTkScrollableFrame(self.root)
        ref = weakref.ref(widget)
        widget.destroy()
        del widget
        gc.collect()
        self.assertIsNotNone(ref())
        self.assertEqual(len(set(self.root._tclCommands) - before),
                         6 if sys.platform.startswith('linux') else 5)

    def test_destroy_keeps_sibling_wheel_shift_and_external_handlers(self):
        external = []
        wheel = '<Button-5>' if sys.platform.startswith('linux') else '<MouseWheel>'
        wheel_id = self.root.bind_all(wheel, lambda event: external.append('wheel'), add='+')
        shift_id = self.root.bind_all('<KeyPress-Shift_L>', lambda event: external.append('shift'), add='+')
        first = ResourceScrollableFrame(self.root, width=180, height=180)
        second = ResourceScrollableFrame(self.root, width=180, height=180)
        first.pack(side='left')
        second.pack(side='left')
        second_ids = tuple(second._slg_global_bindings)
        first_ids = tuple(first._slg_global_bindings)
        ref = weakref.ref(first)
        first.destroy()
        first.destroy()
        del first
        gc.collect()
        self.assertIsNone(ref())
        for sequence, funcid in first_ids:
            self.assertNotIn(funcid, self.root.bind_all(sequence))
            self.assertNotIn(funcid, self.root._tclCommands)
        for sequence, funcid in second_ids:
            self.assertIn(funcid, self.root.bind_all(sequence))
        self.assertIn(wheel_id, self.root.bind_all(wheel))
        self.assertIn(shift_id, self.root.bind_all('<KeyPress-Shift_L>'))
        self.root.update()
        second._parent_canvas.configure(scrollregion=(0, 0, 1000, 1000))
        before = second._parent_canvas.yview()
        event = {} if sys.platform.startswith('linux') else {'delta': -120}
        with patch.object(second, '_check_if_valid_scroll', return_value=True):
            second.event_generate(wheel, **event)
            self.root.update()
        self.assertNotEqual(second._parent_canvas.yview(), before)
        second.focus_force()
        self.root.update()
        second.event_generate('<KeyPress-Shift_L>')
        self.root.update()
        self.assertTrue(second._shift_pressed)
        second.event_generate('<KeyRelease-Shift_L>')
        self.root.update()
        self.assertFalse(second._shift_pressed)
        self.assertIn('wheel', external)
        self.assertIn('shift', external)
        self.assertEqual(self.errors, [])
        second.destroy()
        self.assertIn(wheel_id, self.root.bind_all(wheel))

    def test_100_cycles_bound_root_registration_and_collect_frames(self):
        before = set(self.root._tclCommands)
        refs, owned = [], []
        sequences = ('<MouseWheel>', '<Button-4>', '<Button-5>',
                     '<KeyPress-Shift_L>', '<KeyPress-Shift_R>',
                     '<KeyRelease-Shift_L>', '<KeyRelease-Shift_R>')
        global_before = {sequence: self.root.bind_all(sequence) for sequence in sequences}
        for _ in range(100):
            widget = ResourceScrollableFrame(self.root)
            refs.append(weakref.ref(widget))
            owned.extend(funcid for sequence, funcid in widget._slg_global_bindings)
            widget.destroy()
            del widget
            self.assertLessEqual(len(self.root._tclCommands), len(before) + 2)
        gc.collect()
        self.root.update()
        # CTk's two periodic appearance/DPI commands rotate ids while update
        # runs, and its initial titlebar timer disappears. Own registrations
        # must nevertheless be absent and all shared binding scripts unchanged.
        self.assertTrue(set(owned).isdisjoint(self.root._tclCommands))
        self.assertLessEqual(len(self.root._tclCommands), len(before) + 2)
        self.assertEqual({sequence: self.root.bind_all(sequence) for sequence in sequences}, global_before)
        self.assertTrue(all(ref() is None for ref in refs))
        self.assertEqual(self.errors, [])


if __name__ == '__main__': unittest.main()
