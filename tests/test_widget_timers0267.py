import tkinter as tk
import unittest
import customtkinter as ctk
from slg_image_resources import cancel_widget_timers


class WidgetTimerTests(unittest.TestCase):
    def setUp(self):
        self.root=ctk.CTk()
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self):
        cancel_widget_timers(self.root)
        self.root.destroy()

    def test_subtree_timer_cancel_preserves_business_and_other_window(self):
        panel=ctk.CTkFrame(self.root)
        text=ctk.CTkTextbox(panel)
        popup=ctk.CTkToplevel(self.root)
        popup.withdraw()
        jobs=[text.after(500,lambda: None),panel.after_idle(lambda: None)]
        business=self.root.after(500,lambda: None)
        unrelated=popup.after(500,lambda: None)
        names=list(text._tclCommands)
        self.assertGreaterEqual(cancel_widget_timers(panel),2)
        pending=self.root.tk.splitlist(self.root.tk.call('after','info'))
        self.assertFalse(set(jobs)&set(pending))
        self.assertIn(business,pending)
        self.assertIn(unrelated,pending)
        # Cancel only the after record; destroy still owns command deletion.
        self.assertEqual(names,text._tclCommands)
        panel.destroy()
        self.root.update()

    def test_stale_timer_cannot_invoke_reused_event_command_name(self):
        panel=ctk.CTkFrame(self.root)
        label=ctk.CTkLabel(panel,text='old')
        job=label.after(40,lambda: None)
        command=self.root.tk.splitlist(self.root.tk.call('after','info',job))[0]
        cancel_widget_timers(panel)
        panel.destroy()
        calls=[]
        # Deterministic equivalent of Tcl name reuse after Python id recycling.
        self.root.tk.createcommand(command,lambda event: calls.append(event))
        self.root.after(80,self.root.quit)
        errors=[]
        self.root.report_callback_exception=lambda *args:errors.append(args)
        self.root.mainloop()
        self.assertEqual(calls,[])
        self.assertEqual(errors,[])
        self.root.tk.deletecommand(command)

    def test_detail_replacement_keeps_parent_timer(self):
        from slg_gui import App
        from types import SimpleNamespace
        panel=ctk.CTkFrame(self.root)
        child=ctk.CTkFrame(panel)
        parent_job=panel.after(500,lambda: None)
        child_job=child.after(500,lambda: None)
        owner=SimpleNamespace(detail=panel,_close_shop_page=lambda: None)
        App._destroy_detail(owner)
        pending=self.root.tk.splitlist(self.root.tk.call('after','info'))
        self.assertIn(parent_job,pending)
        self.assertNotIn(child_job,pending)
        panel.destroy()


if __name__=='__main__':unittest.main()
