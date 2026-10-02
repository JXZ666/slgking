"""Focused tests for client announcement page construction and navigation."""

import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="slg-announcement-test-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_gui  # noqa: E402


class ReleaseChannelNotesTests(unittest.TestCase):
    def test_stable_and_test_notes_use_channel_when_versions_match(self):
        with mock.patch.object(slg_gui.sys, "frozen", True, create=True), \
                mock.patch.object(slg_gui, "TEST_APP_VERSION", slg_gui.APP_VERSION):
            for executable, channel in (("slgking.exe", "稳定版"),
                                        ("slgking_test.exe", "测试版")):
                with self.subTest(executable=executable), \
                        mock.patch.object(slg_gui.sys, "executable", executable):
                    notes = slg_gui.release_notes_text()
                    self.assertTrue(notes.startswith("0.24.5 " + channel))
                    self.assertNotIn("管理员身份", notes)

    def test_actual_channels_keep_stable_and_test_versions_separate(self):
        with mock.patch.object(slg_gui.sys, "frozen", True, create=True):
            for executable, version in (("slgking.exe", "0.24.5"),
                                        ("slgking_test.exe", "0.25.0")):
                with self.subTest(executable=executable), mock.patch.object(
                        slg_gui.sys, "executable", executable):
                    self.assertTrue(slg_gui.release_notes_text().startswith(version + " "))
                    self.assertNotIn("网页版", slg_gui.release_notes_text())


class AnnouncementPageModelTests(unittest.TestCase):
    def test_current_and_history_are_separate_from_version_notes(self):
        pages = slg_gui._build_announcement_pages({
            "announcement": {"id": "new", "title": "当前公告",
                             "body": "当前正文"},
            "announcement_history": [
                {"id": "old", "title": "旧公告", "body": "旧正文"},
            ],
        }, "版本说明正文")

        self.assertEqual([page["kind"] for page in pages], [
            "current", "history", "release_notes"])
        current_text, _, claimable = slg_gui._announcement_page_view(
            pages[0], 0, len(pages))
        notes_text, notes_indicator, notes_claimable = (
            slg_gui._announcement_page_view(pages[-1], 2, len(pages)))
        self.assertIn("当前正文", current_text)
        self.assertNotIn("版本说明正文", current_text)
        self.assertEqual(notes_text, "版本说明正文")
        self.assertIn("版本说明", notes_indicator)
        self.assertTrue(claimable)
        self.assertFalse(notes_claimable)

    def test_history_is_newest_first_after_current(self):
        pages = slg_gui._build_announcement_pages({
            "announcement": {"id": "active", "title": "现在"},
            "announcement_history": [
                {"id": "older", "title": "较旧"},
                {"id": "newer", "title": "较新"},
            ],
        }, "notes")

        self.assertEqual([page["item"].get("id") for page in pages[:-1]],
                         ["active", "newer", "older"])
        _, indicator, claimable = slg_gui._announcement_page_view(
            pages[1], 1, len(pages))
        self.assertIn("2 / 4", indicator)
        self.assertFalse(claimable)

    def test_deduplicates_ids_and_content_without_ids(self):
        pages = slg_gui._build_announcement_pages({
            "announcement": {"id": "same-id", "title": "当前"},
            "announcement_history": [
                {"id": "same-id", "title": "重复 ID"},
                {"title": "无 ID", "body": "相同正文"},
                {"title": "无 ID", "body": "相同正文"},
            ],
        }, "notes")

        self.assertEqual([page["item"].get("title") for page in pages[:-1]],
                         ["当前", "无 ID"])

    def test_legacy_config_and_empty_config_are_safe(self):
        legacy = slg_gui._build_announcement_pages({
            "announcement": {
                "id": "active", "title": "当前",
                "announcement_history": [{"id": "old", "title": "旧"}],
            },
        }, "notes")
        empty = slg_gui._build_announcement_pages({}, "notes")

        self.assertEqual([page["kind"] for page in legacy], [
            "current", "history", "release_notes"])
        self.assertEqual([page["kind"] for page in empty], ["release_notes"])
        self.assertEqual(slg_gui._announcement_page_view(
            empty[0], 0, len(empty)), ("notes", "版本说明 · 1 / 1", False))


class _FakeWidget:
    created = []

    def __init__(self, master=None, **kwargs):
        self.master = master
        self.values = dict(kwargs)
        self.visible = False
        self.inserted_text = ""
        self.__class__.created.append(self)

    def pack(self, **kwargs):
        self.visible = True
        self.pack_options = kwargs

    def pack_forget(self):
        self.visible = False

    def configure(self, **kwargs):
        self.values.update(kwargs)

    def insert(self, _index, value):
        self.inserted_text = value

    def delete(self, *_args):
        self.inserted_text = ""

    def destroy(self):
        self.destroyed = True


class AnnouncementNavigationTests(unittest.TestCase):
    def setUp(self):
        _FakeWidget.created = []

    def test_next_and_previous_buttons_switch_pages_and_hide_claim_controls(self):
        window = _FakeWidget()
        app = type("AppStub", (), {})()
        app.conn = None
        app._remote_config = {
            "announcement": {"id": "active", "title": "当前", "body": "正文"},
            "announcement_history": [{"id": "old", "title": "历史"}],
        }
        app._has_usable_cloud_session = lambda: False
        app._new_dialog = lambda *_args: window
        app._render_announcement_reward = lambda: None
        app._refresh_announcement_badge = lambda: None
        app._claim_maintenance_reward = lambda: None

        with mock.patch.object(slg_gui.ctk, "CTkLabel", _FakeWidget), \
                mock.patch.object(slg_gui.ctk, "CTkFrame", _FakeWidget), \
                mock.patch.object(slg_gui.ctk, "CTkButton", _FakeWidget), \
                mock.patch.object(slg_gui.ctk, "CTkTextbox", _FakeWidget), \
                mock.patch.object(slg_gui, "ui_font", return_value=None), \
                mock.patch.object(slg_gui.slg_db, "set_pref"):
            slg_gui.App.open_announcement(app)

        buttons = [widget for widget in _FakeWidget.created
                   if "command" in widget.values or "text" in widget.values]
        previous = next(widget for widget in buttons
                        if widget.values.get("text") == "上一页")
        next_page = next(widget for widget in buttons
                         if widget.values.get("text") == "下一页")
        indicator = next(widget for widget in _FakeWidget.created
                         if "当前公告 · 1 / 3" == widget.values.get("text"))
        notes = next(widget for widget in _FakeWidget.created
                     if hasattr(widget, "inserted_text")
                     and widget is not window and widget.values.get("height") == 245)
        claim_label = app._announcement_reward_label
        claim_button = app._announcement_claim_button

        self.assertTrue(claim_label.visible)
        self.assertTrue(claim_button.visible)
        self.assertIn("当前", notes.inserted_text)
        next_page.values["command"]()
        self.assertIn("历史公告 · 2 / 3", indicator.values["text"])
        self.assertIn("历史", notes.inserted_text)
        self.assertFalse(claim_label.visible)
        self.assertFalse(claim_button.visible)
        next_page.values["command"]()
        self.assertIn("版本说明 · 3 / 3", indicator.values["text"])
        self.assertFalse(claim_button.visible)
        previous.values["command"]()
        self.assertIn("历史公告 · 2 / 3", indicator.values["text"])


if __name__ == "__main__":
    unittest.main()
