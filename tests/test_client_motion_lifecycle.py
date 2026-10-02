"""Real Canvas lifecycle checks without cloud requests or a full application."""
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Do not redirect an existing suite's imported database paths; these tests never
# connect to SQLite or construct App. Only App's drawing methods are exercised.
import slg_gui

class ConfettiLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=tk.Tk(); cls.root.geometry('300x200')
        cls.root.update()
    @classmethod
    def tearDownClass(cls): cls.root.destroy()
    def setUp(self):
        self.app=SimpleNamespace(reduced_motion=False)
        self.canvas=tk.Canvas(self.root,width=240,height=100)
        self.canvas.pack(); self.root.update()
    def tearDown(self):
        if self.canvas.winfo_exists(): self.canvas.destroy()
        self.root.update()
    def pump_timer(self):
        time.sleep(.04); self.root.update()
    def text(self):
        return [self.canvas.itemcget(i,'text') for i in self.canvas.find_all()
                if self.canvas.type(i)=='text']
    def test_confetti_finishes_at_monotonic_deadline_and_preserves_amount(self):
        clock=[100.0]
        with patch.object(slg_gui.time,'perf_counter',side_effect=lambda: clock[0]):
            slg_gui.App._confetti(self.app,self.canvas,240,100,23)
            scheduler=self.canvas._slg_confetti_scheduler
            self.assertEqual(scheduler.pending_count,1)
            clock[0]=101.6; self.pump_timer()
            self.assertEqual(scheduler.pending_count,0)
            self.assertEqual(self.text(),['+23 积分'])
            self.assertEqual(len(self.canvas.find_all()),1)
            self.pump_timer(); self.assertEqual(scheduler.pending_count,0)
    def test_reduced_motion_has_final_state_and_no_animation_jobs(self):
        self.app.reduced_motion=True
        slg_gui.App._confetti(self.app,self.canvas,240,100,7)
        self.assertEqual(self.canvas._slg_confetti_scheduler.pending_count,0)
        self.assertEqual(self.text(),['+7 积分'])
        self.assertEqual(len(self.canvas.find_all()),1)
    def test_hide_cancels_and_remap_resumes_then_finishes(self):
        clock=[100.0]
        with patch.object(slg_gui.time,'perf_counter',side_effect=lambda: clock[0]):
            slg_gui.App._confetti(self.app,self.canvas,240,100,8)
            scheduler=self.canvas._slg_confetti_scheduler
            self.canvas.pack_forget(); self.root.update()
            self.assertEqual(scheduler.pending_count,0)
            clock[0]=102.0
            self.canvas.pack(); self.root.update(); self.pump_timer()
            self.assertEqual(scheduler.pending_count,0)
            self.assertEqual(self.text(),['+8 积分'])
    def test_destroy_removes_pending_tcl_callbacks(self):
        slg_gui.App._confetti(self.app,self.canvas,240,100,3)
        scheduler=self.canvas._slg_confetti_scheduler
        handles=set(scheduler._jobs)
        self.canvas.destroy(); self.root.update()
        remaining=set(self.root.tk.splitlist(self.root.tk.call('after','info')))
        self.assertFalse(handles & remaining); self.assertEqual(scheduler.pending_count,0)
    def test_changing_to_reduced_motion_finishes_existing_animation(self):
        slg_gui.App._confetti(self.app,self.canvas,240,100,11)
        self.app.reduced_motion=True; self.pump_timer()
        self.assertEqual(self.canvas._slg_confetti_scheduler.pending_count,0)
        self.assertEqual(self.text(),['+11 积分'])
    def test_title_reward_without_counter_leaves_no_particles(self):
        self.app.reduced_motion=True
        slg_gui.App._confetti(self.app,self.canvas,240,100,0,label=None)
        self.assertEqual(self.canvas.find_all(),())
        self.assertEqual(self.canvas._slg_confetti_scheduler.pending_count,0)

if __name__=='__main__': unittest.main()
