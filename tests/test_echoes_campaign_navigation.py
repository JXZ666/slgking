"""Targeted campaign and scroll checks; isolated DB and all network forbidden."""
import contextlib
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
import customtkinter as ctk
import slg_account
import slg_campaign_ui as campaign
import slg_page_scroll as scroll
import slg_db
import slg_gui
import slg_titles

FAKE_CODE = "ECHOES-00000000-11111111-22222222-33333333"


class ContractTests(unittest.TestCase):
    def test_transport_and_format_are_server_only(self):
        self.assertTrue(campaign.valid_format(" \t" + FAKE_CODE.lower() + "\n"))
        self.assertFalse(campaign.valid_format(FAKE_CODE.replace("-", "")))
        self.assertFalse(campaign.valid_format("\u3000" + FAKE_CODE))
        with patch.object(slg_account, "authenticated", return_value={}) as call:
            slg_account.redeem_campaign(FAKE_CODE.lower())
            call.assert_called_once_with("/account/rewards/campaign/redeem", method="POST", payload={"code": FAKE_CODE})
        title = slg_titles.title_by_id(campaign.TITLE_ID)
        self.assertEqual(title["rarity"], "史诗")
        self.assertEqual(title["obtain"], "campaign")
        self.assertNotIn(campaign.TITLE_ID, {item["id"] for item in slg_titles.SHOP_ITEMS})
        self.assertIn("每个云端账号一次", slg_titles.obtain_title_text(title))


class UITests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))
        self.stack.enter_context(patch.dict(os.environ, {"LOCALAPPDATA": temp, "APPDATA": temp}))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.session = {"account_id": "echoes-offline-fixture", "display_name": "Fixture"}
        self.stack.enter_context(patch.object(slg_account, "session", side_effect=lambda: self.session))
        self.stack.enter_context(patch.object(slg_account, "session_for_mode", side_effect=lambda *_: self.session))
        self.actions = self.stack.enter_context(patch.object(slg_gui.App, "_run_cloud_action", return_value=True))
        self.stack.enter_context(patch.object(slg_gui.App, "_fetch_quests"))
        self.stack.enter_context(patch.object(slg_account, "set_developer_mode"))
        conn = slg_db.connect()
        for i in range(2):
            gid, _ = slg_db.upsert_game(conn, "echoes-fixture-%d" % i,
                "https://example.invalid/%d" % i, "Fixture %d" % i, tags=["fixture"])
            slg_db.upsert_detail(conn, gid, overview="Fixture text\n" * 200)
        conn.commit()
        conn.close()
        self.app = slg_gui.App(notify=False)
        self.addCleanup(self.close)
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        self.app._cloud_balance = 200
        self.app._cloud_me = {"account_id": self.session["account_id"], "balance": 200}
        self.assertTrue(Path(slg_db.db_path()).is_relative_to(Path(temp)))
        self.pump()

    def close(self):
        self.app.destroy()
        self.app.conn.close()

    def pump(self):
        deadline = time.monotonic() + 20
        while True:
            self.app.update_idletasks()
            self.app.update()
            batch = getattr(self.app, "_shop_render_batch", None)
            if batch is None or batch.job is None:
                break
            if time.monotonic() > deadline:
                self.fail("shop batch timeout")
            time.sleep(.002)

    def response(self, already=False):
        return dict(ok=True, campaign_id=campaign.CAMPAIGN_ID, title_id=campaign.TITLE_ID,
                    already_claimed=already, gained=0 if already else 100, balance=300)

    def test_claim_repeat_error_and_wallet_lock(self):
        app = self.app
        app.open_redeem()
        self.assertTrue(campaign.submit(app, FAKE_CODE))
        token = app._campaign_pending[0]
        self.assertFalse(campaign.submit(app, FAKE_CODE))
        self.assertTrue(app._shop_wallet_busy())
        app._cloud_action_result(token, self.response(), None, app._identity_generation, self.session["account_id"])
        self.assertEqual(app._cloud_balance, 300)
        self.assertIn(campaign.TITLE_ID, app._cloud_titles)
        self.assertIn("+100", app._redeem_feedback.cget("text"))
        self.assertFalse(app._shop_wallet_busy())
        self.assertTrue(campaign.submit(app, FAKE_CODE))
        token = app._campaign_pending[0]
        app._cloud_action_result(token, self.response(True), None, app._identity_generation, self.session["account_id"])
        self.assertEqual(app._cloud_balance, 300)
        self.assertIn("不会重复", app._redeem_feedback.cget("text"))
        self.assertTrue(campaign.submit(app, FAKE_CODE))
        token = app._campaign_pending[0]
        app._cloud_action_result(token, None, "offline timeout", app._identity_generation, self.session["account_id"])
        self.assertFalse(app._campaign_pending)
        self.assertFalse(app._shop_wallet_busy())
        self.assertEqual(app._redeem_submit.cget("state"), "normal")
        self.assertFalse(self.errors)

    def test_campaign_rejects_late_result_after_account_switch(self):
        app = self.app
        app.open_redeem()
        campaign.submit(app, FAKE_CODE)
        old = app._campaign_pending[0]
        generation = app._identity_generation
        self.session = {"account_id": "other-fixture"}
        app._reset_identity_state()
        app._cloud_action_result(old, self.response(), None, generation, "echoes-offline-fixture")
        self.assertNotIn(campaign.TITLE_ID, app._cloud_titles)
        self.assertIsNone(app._campaign_pending)
        self.assertNotEqual(app._cloud_balance, 300)
        self.assertFalse(self.errors)

    def test_profile_to_shop_top_then_balance_refresh_preserves(self):
        app = self.app
        app.open_profile(); self.pump()
        body = getattr(app, "_profile_tab_body", app.detail)
        # Extra fixture height exercises an actual scroll area in both versions.
        spacer = ctk.CTkFrame(body, height=4000)
        spacer.pack(fill="x")
        self.pump()
        body._parent_canvas.yview_moveto(.6)
        self.assertGreater(body._parent_canvas.yview()[0], .1)
        app.open_shop(); self.pump()
        self.assertLess(app.detail._parent_canvas.yview()[0], .01)
        app.detail._parent_canvas.yview_moveto(.4)
        before = app.detail._parent_canvas.yview()[0]
        self.assertGreater(before, .01)
        app._cloud_balance += 1
        app._mark_shop("balance", "products"); self.pump()
        self.assertAlmostEqual(app.detail._parent_canvas.yview()[0], before, delta=.02)
        app.open_profile(); self.pump()
        body = getattr(app, "_profile_tab_body", app.detail)
        self.assertLess(body._parent_canvas.yview()[0], .01)
        self.assertFalse(self.errors)

    def test_game_switch_and_profile_return(self):
        app = self.app
        rows = slg_db.find_games(app.conn)[:2]
        self.assertGreaterEqual(len(rows), 2)
        app.select(rows[0]); self.pump()
        extra = ctk.CTkFrame(app.detail, height=4000); extra.pack(fill="x")
        self.pump(); app.detail._parent_canvas.yview_moveto(.5)
        before = app.detail._parent_canvas.yview()[0]
        if hasattr(app, "_close_profile_workspace"):
            app.open_profile(); self.pump()
            app._close_profile_workspace(); self.pump()
            self.assertAlmostEqual(app.detail._parent_canvas.yview()[0], before, delta=.02)
        app.select(rows[1]); self.pump()
        self.assertLess(app.detail._parent_canvas.yview()[0], .01)
        self.assertFalse(self.errors)

    def test_scroll_epoch_rejects_stale_deferred_restore(self):
        app = self.app
        canvas = app.detail._parent_canvas
        scroll.begin(app, "old", app.detail)
        old = scroll.snapshot(app, canvas)
        scroll.begin(app, "new", app.detail)
        scroll.restore(app, canvas, old)
        self.assertNotEqual(old[0], app._scroll_page_epoch)
        self.pump()
        self.assertEqual(app._scroll_page_key, "new")
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
