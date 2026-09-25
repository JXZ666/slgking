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


class DeveloperIdentityClientTests(unittest.TestCase):
    def tearDown(self):
        slg_account.set_developer_mode(False)

    def test_developer_login_is_blocked_outside_developer_mode(self):
        slg_account.set_developer_mode(False)
        with mock.patch.object(slg_account, "request") as send:
            with self.assertRaises(slg_account.AccountError):
                slg_account.developer_login("owner-key")
        send.assert_not_called()

    def test_developer_login_uses_owner_header_and_fixed_identity(self):
        slg_account.set_developer_mode(True)
        reply = {"account_id": "developer", "device_token": "owner-session"}
        with mock.patch.object(slg_account, "session", return_value=None), \
                mock.patch.object(slg_account, "request", return_value=reply) as send, \
                mock.patch.object(slg_account, "save_session"), \
                mock.patch.object(slg_account, "_attach_legacy_migration"):
            result = slg_account.developer_login(" owner-key ")
        self.assertEqual(result["account_id"], "developer")
        self.assertEqual(send.call_args.args[0], "/account/admin-login")
        self.assertEqual(send.call_args.kwargs["extra_headers"], {
            "X-SLG-Developer-Key": "owner-key"})

    def test_grant_all_titles_requires_developer_mode_and_uses_developer_route(self):
        slg_account.set_developer_mode(False)
        with mock.patch.object(slg_account, "request") as send:
            with self.assertRaises(slg_account.AccountError):
                slg_account.developer_grant_all_titles("owner-key")
        send.assert_not_called()

        slg_account.set_developer_mode(True)
        reply = {"ok": True, "account_id": "developer",
                 "granted": ["title_a"], "titles": ["title_a"],
                 "equipped_title": "title_a"}
        with mock.patch.object(slg_account, "request", return_value=reply) as send:
            self.assertEqual(slg_account.developer_grant_all_titles(" owner-key "),
                             reply)
        self.assertEqual(send.call_args.args[0],
                         "/account/developer/titles/grant-all")
        self.assertEqual(send.call_args.kwargs["method"], "POST")
        self.assertEqual(send.call_args.kwargs["extra_headers"], {
            "X-SLG-Developer-Key": "owner-key"})
        with mock.patch.object(slg_account, "request", return_value=[]):
            with self.assertRaises(slg_account.AccountError):
                slg_account.developer_grant_all_titles("owner-key")


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
