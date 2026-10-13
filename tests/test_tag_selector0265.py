import unittest
import customtkinter as ctk
from slg_tag_selector import TagSelector


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.root = ctk.CTk()
        self.root.geometry('600x800')
        self.errors = []
        self.root.report_callback_exception = lambda *e: self.errors.append(e)
        self.picker = TagSelector(self.root, ['tag-%04d'%i for i in range(1000)],
            {'tag-0999'}, str, lambda p,n,v: ctk.CTkCheckBox(p,text=n,variable=v))
        self.picker.pack(fill='both',expand=True)

    def tearDown(self):
        self.picker.destroy() if self.picker.winfo_exists() else None
        for job in self.root.tk.call('after','info'):
            self.root.after_cancel(job)
        self.root.destroy()

    def wait(self):
        def poll():
            if not self.picker.rendering:
                self.root.quit()
            else:
                self.root.after(10, poll)
        timeout = self.root.after(10000, self.root.quit)
        self.root.after(0,poll)
        self.root.mainloop()
        self.root.after_cancel(timeout)
        self.assertFalse(self.picker.rendering)
        self.assertFalse(self.errors)

    def test_pages_bound_widgets_and_keep_offscreen_selection(self):
        self.wait()
        self.assertEqual(len(self.picker.options.winfo_children()),50)
        self.assertLessEqual(len(self.picker.variables),51)
        self.picker.variables['tag-0000'].set(True)
        self.picker.turn(1)
        self.wait()
        self.assertEqual(len(self.picker.options.winfo_children()),50)
        self.assertTrue(self.picker.variables['tag-0999'].get())
        self.assertTrue(self.picker.variables['tag-0000'].get())

    def test_search_and_new_tag_preserve_checked_values(self):
        self.wait()
        self.picker.search.insert(0,'0999')
        self.picker.render()
        self.wait()
        self.assertEqual(len(self.picker.options.winfo_children()),1)
        self.picker.add_tag('custom-tag')
        self.wait()
        self.assertTrue(self.picker.variables['custom-tag'].get())
        self.assertTrue(self.picker.variables['tag-0999'].get())

    def test_destroy_cancels_initial_callback(self):
        self.picker.destroy()
        self.root.after(150,self.root.quit)
        self.root.mainloop()
        self.assertFalse(self.errors)


if __name__=='__main__': unittest.main()
