"""Focused checks for the 0.23 cloud client contract and credential storage."""

import os
import secrets
import tempfile
import unittest
from unittest import mock

import slg_account
import slg_comments
import slg_titles


class SessionStorageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
    def test_windows_dpapi_session_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "cloud_session.json")
            token = secrets.token_urlsafe(32)
            with mock.patch.object(slg_account, "_session_path", return_value=path):
                slg_account.save_session("acct-example", token)
                with open(path, "rb") as inp:
                    self.assertNotIn(token.encode("utf-8"), inp.read())
                self.assertEqual(slg_account.session(), {
                    "account_id": "acct-example", "device_token": token})
                slg_account.forget_session()
                self.assertIsNone(slg_account.session())

    def test_new_account_returns_keys_even_if_local_save_fails(self):
        credentials = {"account_id": "acct", "login_key": "login",
                       "recovery_code": "recovery", "device_token": "session"}
        with mock.patch.object(slg_account, "request", return_value=credentials), \
                mock.patch.object(slg_account, "save_session",
                                  side_effect=slg_account.AccountError("disk failed")):
            self.assertEqual(slg_account.create()["session_error"], "disk failed")

    def test_damaged_old_session_can_be_recovered_with_both_keys(self):
        reply = {"account_id": "acct", "device_token": "new-session"}
        with mock.patch.object(slg_account, "session",
                               side_effect=slg_account.AccountError("damaged")), \
                mock.patch.object(slg_account, "request", return_value=reply) as send, \
                mock.patch.object(slg_account, "save_session"):
            slg_account.login("login-key", "recovery-code")
        self.assertEqual(send.call_args.kwargs["payload"], {
            "login_key": "login-key", "recovery_code": "recovery-code"})


class CommentValidationTests(unittest.TestCase):
    def test_character_and_utf8_limits(self):
        self.assertIsNotNone(slg_comments.validate_public_comment("g", "短评"))
        self.assertIsNone(slg_comments.validate_public_comment("g", "写得很具体" * 8))
        self.assertIsNotNone(slg_comments.validate_public_comment("g", "x" * 501))
        self.assertIsNotNone(slg_comments.validate_public_comment(
            "g" * 200, "𠮷" * 500))

    def test_new_group_code_uses_cloud_route(self):
        self.assertTrue(slg_titles.is_cloud_group_code_format("qunyou-ABCDEFG234"))
        self.assertFalse(slg_titles.is_cloud_group_code_format("QUNYOU-ABCDEF"))
        self.assertFalse(slg_titles.is_cloud_group_code_format("QUNYOU-ABCDEF!234"))


if __name__ == "__main__":
    unittest.main()
