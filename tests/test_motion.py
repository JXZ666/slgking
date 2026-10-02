import unittest
import tkinter as tk
from types import SimpleNamespace
from slg_motion import MotionScheduler, Elapsed, TimeBudget

class Owner:
    def __init__(self): self.jobs={}; self.next=0; self.handlers={}
    def bind(self,event,fn,add=None): self.handlers[event]=fn
    def after(self,delay,fn):
        self.next+=1; key=str(self.next); self.jobs[key]=fn; return key
    def after_cancel(self,key): self.jobs.pop(key,None)
    def fire(self,key): self.jobs.pop(key)()
    def destroy(self): self.handlers['<Destroy>'](SimpleNamespace(widget=self))

class SchedulerTests(unittest.TestCase):
    def test_completed_callbacks_are_removed_before_reschedule(self):
        owner=Owner(); scheduler=MotionScheduler(owner); seen=[]
        def callback():
            seen.append(scheduler.pending_count)
            scheduler.call_later(10,lambda: seen.append('second'))
        handle=scheduler.call_later(10,callback)
        self.assertEqual(scheduler.pending_count,1)
        owner.fire(handle)
        self.assertEqual(seen,[0]); self.assertEqual(scheduler.pending_count,1)
        owner.fire(next(iter(owner.jobs)))
        self.assertEqual(seen,[0,'second']); self.assertEqual(scheduler.pending_count,0)
    def test_cancel_is_idempotent_and_allows_resume(self):
        owner=Owner(); s=MotionScheduler(owner)
        s.call_later(5,lambda: self.fail('cancelled')); s.cancel_all(); s.cancel_all()
        self.assertEqual(owner.jobs,{}); self.assertEqual(s.pending_count,0)
        self.assertIsNotNone(s.call_later(5,lambda: None))
    def test_destroy_prevents_future_work(self):
        owner=Owner(); s=MotionScheduler(owner); s.call_later(1,lambda: None)
        owner.destroy(); self.assertEqual(owner.jobs,{})
        self.assertIsNone(s.call_later(1,lambda: None)); self.assertEqual(s.pending_count,0)
    def test_child_destroy_does_not_close_owner(self):
        owner=Owner(); s=MotionScheduler(owner)
        s._destroyed(SimpleNamespace(widget=object()))
        self.assertIsNotNone(s.call_later(1,lambda: None))
    def test_exception_does_not_leave_pending_handle(self):
        owner=Owner(); s=MotionScheduler(owner)
        def fail(): raise ValueError('boom')
        handle=s.call_later(1,fail)
        with self.assertRaises(ValueError): owner.fire(handle)
        self.assertEqual(s.pending_count,0)

class TimingTests(unittest.TestCase):
    def test_elapsed_uses_injected_monotonic_clock(self):
        now=[10.0]; e=Elapsed(lambda: now[0]); now[0]=10.5
        self.assertEqual(e.seconds,.5); self.assertEqual(e.progress(2),.25)
        now[0]=15; self.assertEqual(e.progress(2),1)
        e.reset(); self.assertEqual(e.seconds,0)
        self.assertEqual(e.progress(0),1)
    def test_budget_honors_time_count_and_first_item(self):
        now=[0.0]; budget=TimeBudget(8,2,lambda: now[0]); now[0]=1
        self.assertTrue(budget.available); budget.consumed(); self.assertFalse(budget.available)
        now[0]=2; budget=TimeBudget(8,2,lambda: now[0])
        budget.consumed(); self.assertTrue(budget.available)
        budget.consumed(); self.assertFalse(budget.available)

class RealTkLifecycleTests(unittest.TestCase):
    def test_destroy_cancels_real_tcl_after_handles(self):
        root=tk.Tk(); root.withdraw()
        try:
            frame=tk.Frame(root); s=MotionScheduler(frame); seen=[]
            handles=[s.call_later(1000,lambda: seen.append('late')) for _ in range(3)]
            self.assertEqual(s.pending_count,3)
            frame.destroy(); root.update()
            pending=root.tk.splitlist(root.tk.call('after','info'))
            self.assertTrue(all(h not in pending for h in handles))
            self.assertEqual(s.pending_count,0); self.assertEqual(seen,[])
            self.assertIsNone(s.call_later(1,lambda: None))
        finally: root.destroy()

if __name__=='__main__': unittest.main()
