"""Offline real-Tk checks for identity warnings and the complete help navigation."""
from unittest.mock import patch

import customtkinter as ctk
import test_client_0256_ui as fixture


class Client0270GUI(fixture.Client0256UI):
    def test_identity_conflict_is_visible_and_not_selected(self):
        result = self.result("RJ00000001 Conflicting Fixture", self.ids["western_slg"])
        result["items"][0].update(status="ambiguous", reason="existing_path",
            product_ids=["RJ00000001"], identity_warning="目录编号与已有绑定不一致，请确认")
        self.app.open_library_scan_results(result)
        dialog = self.app._library_scan_dialog
        self.assertFalse(dialog["states"][0]["var"].get())
        texts = [w.cget("text") for w in self.widgets(dialog["win"])
                 if isinstance(w, ctk.CTkLabel)]
        self.assertTrue(any("RJ00000001" in text and "已有绑定不一致" in text for text in texts))
        dialog["win"].destroy()

    def test_full_identification_option_reaches_worker(self):
        self.app.open_library_import()
        dialog = self.app._library_targets_dialog
        dialog["targets"].append(dict(path=str(self.folders["western_slg"]), mode="single"))
        dialog["force_redetect"].set(True)
        with patch.object(self.app, "_run_job") as job:
            dialog["save"](True)
        self.assertTrue(job.call_args.args[-1])

    def test_all_help_sections_at_small_window_and_large_font(self):
        self.app.open_help()
        frame = self.app._help_content
        win = frame.winfo_toplevel()
        win.geometry("720x540")
        ctk.set_widget_scaling(1.25)
        try:
            self.app.update_idletasks()
            for key, title, _ in self.app.HELP_SECTIONS:
                self.app._show_help_section(key)
                self.app.update_idletasks()
                self.assertTrue(self.app._help_frame.winfo_children(), title)
                width = int(frame.winfo_width() / ctk.ScalingTracker.get_widget_scaling(frame))
                for child in frame.winfo_children():
                    if isinstance(child, ctk.CTkLabel) and child.cget("wraplength"):
                        self.assertLessEqual(child.cget("wraplength"), max(80, width))
            self.app._show_help_section("what")
            self.assertFalse(self.callback_errors)
        finally:
            ctk.set_widget_scaling(1.0)
            win.destroy()


if __name__ == "__main__":
    import unittest
    unittest.main()
