"""Focused checks for the 0.23 cloud client contract and credential storage."""

import os
import contextlib
import io
import errno
import json
import secrets
import socket
import tempfile
import unittest
import urllib.error
from unittest import mock

import slg_account
import slg_db
import slg_comments
import slg_titles


class AccountRequestNetworkTests(unittest.TestCase):
    def setUp(self):
        slg_account._ROUTE_OPENER = None
        slg_account._ROUTE_UNTIL = 0.0

    def test_http_error_is_mapped_without_proxy_retry(self):
        failure = urllib.error.HTTPError(
            slg_account._BASE + "/account/create", 503, "Unavailable", {},
            io.BytesIO(b"not json"))
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=failure) as direct, \
                mock.patch.object(slg_account.urllib.request, "build_opener") as proxy:
            with self.assertRaisesRegex(slg_account.AccountError, "HTTP 503"):
                slg_account.request("/account/create", method="POST", payload={})
        direct.assert_called_once()
        proxy.assert_not_called()

    def test_referral_404_distinguishes_invalid_code_from_missing_endpoint(self):
        invalid_code = urllib.error.HTTPError(
            slg_account._BASE + "/account/referrals/redeem", 404,
            "Not Found", {}, io.BytesIO(b'{"error":"unknown referral code"}'))
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=invalid_code):
            with self.assertRaisesRegex(slg_account.AccountError, "邀请码无效"):
                slg_account.request("/account/referrals/redeem", method="POST",
                                    payload={"code": "BAD"})

        missing_endpoint = urllib.error.HTTPError(
            slg_account._BASE + "/account/referrals/me", 404,
            "Not Found", {}, io.BytesIO(b'{"error":"not found"}'))
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=missing_endpoint):
            with self.assertRaisesRegex(
                    slg_account.ServerNotReady, "服务器尚未启用邀请码功能"):
                slg_account.request("/account/referrals/me")

    def test_explicit_preconnect_failure_can_fall_back_to_system_proxy(self):
        direct_error = urllib.error.URLError(
            socket.gaierror(socket.EAI_AGAIN, "temporary name resolution failure"))
        proxy = mock.Mock()
        proxy.open.return_value = io.BytesIO(b'{"ok":true}')
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=direct_error) as direct, \
                mock.patch.object(slg_account, "_has_system_proxy", return_value=True), \
                mock.patch.object(slg_account.urllib.request, "build_opener",
                                  return_value=proxy) as build:
            result = slg_account.request(
                "/account/create", method="POST", payload={})
        self.assertEqual(result, {"ok": True})
        direct.assert_called_once()
        build.assert_called_once_with()
        proxy.open.assert_called_once()
        direct_request = direct.call_args.args[0]
        proxy_request = proxy.open.call_args.args[0]
        self.assertIs(direct_request, proxy_request)
        self.assertEqual(direct_request.get_header("User-agent"),
                         slg_account._USER_AGENT)

    def test_ambiguous_timeout_does_not_replay_post_and_warns_for_registration(self):
        failure = urllib.error.URLError(socket.timeout("timed out"))
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=failure) as direct, \
                mock.patch.object(slg_account, "_has_system_proxy",
                                  return_value=True), \
                mock.patch.object(slg_account.urllib.request, "build_opener") as proxy:
            with self.assertRaisesRegex(
                    slg_account.AccountError, "云端可能已经处理.*请勿立即再次创建"):
                slg_account.request("/account/create", method="POST", payload={})
        direct.assert_called_once()
        proxy.assert_not_called()

    def test_refused_connection_gets_connection_establishment_message(self):
        failure = urllib.error.URLError(
            ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"))
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=failure), \
                mock.patch.object(slg_account, "_has_system_proxy", return_value=False):
            with self.assertRaisesRegex(
                    slg_account.AccountError, "无法建立与云端服务器的连接"):
                slg_account.request("/account/create", method="POST", payload={})

    def test_create_submits_only_one_post_after_ambiguous_timeout(self):
        failure = urllib.error.URLError(socket.timeout("timed out"))
        def direct_open(req, timeout):
            if req.get_method() == "GET":
                return io.BytesIO(b'{"ok":true}')
            raise failure

        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=direct_open) as send, \
                mock.patch.object(slg_account, "_has_system_proxy",
                                  return_value=True), \
                mock.patch.object(slg_account.urllib.request, "build_opener") as proxy:
            with self.assertRaisesRegex(
                    slg_account.AccountError, "云端可能已经处理.*请勿立即再次创建"):
                slg_account.create()
        posts = [call for call in send.call_args_list
                 if call.args[0].get_method() == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].args[0].full_url.endswith("/account/create"))
        proxy.assert_not_called()

    def test_create_uses_proxy_after_direct_health_timeout_and_sends_one_post(self):
        credentials = {"account_id": "acct", "login_key": "login",
                       "recovery_code": "recovery", "device_token": "session"}

        def direct_open(req, timeout):
            self.assertEqual(req.get_method(), "GET")
            self.assertTrue(req.full_url.endswith("/healthz"))
            raise urllib.error.URLError(socket.timeout("direct timed out"))

        proxy = mock.Mock()

        def proxy_open(req, timeout):
            if req.full_url.endswith("/healthz"):
                return io.BytesIO(b'{"ok":true}')
            if req.full_url.endswith("/account/create"):
                return io.BytesIO(json.dumps(credentials).encode("utf-8"))
            self.fail("unexpected proxy request: %s" % req.full_url)

        proxy.open.side_effect = proxy_open
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=direct_open) as direct, \
                mock.patch.object(slg_account, "_has_system_proxy",
                                  return_value=True), \
                mock.patch.object(slg_account.urllib.request, "build_opener",
                                  return_value=proxy) as build, \
                mock.patch.object(slg_account, "save_session"), \
                mock.patch.object(slg_account, "_attach_legacy_migration"):
            result = slg_account.create()

        self.assertEqual(result, credentials)
        direct.assert_called_once()
        build.assert_called_once_with()
        self.assertEqual(
            [call.args[0].full_url.rsplit("/", 1)[-1]
             for call in proxy.open.call_args_list],
            ["healthz", "create"])
        create_posts = [call for call in proxy.open.call_args_list
                        if call.args[0].get_method() == "POST"]
        self.assertEqual(len(create_posts), 1)
        for call in proxy.open.call_args_list:
            req = call.args[0]
            self.assertEqual(req.get_header("Accept"), "application/json")
            self.assertEqual(req.get_header("User-agent"), slg_account._USER_AGENT)

    def test_create_does_not_post_if_every_health_route_fails(self):
        direct_failure = urllib.error.URLError(socket.timeout("direct timed out"))
        proxy_failure = urllib.error.URLError(socket.timeout("proxy timed out"))
        proxy = mock.Mock()
        proxy.open.side_effect = proxy_failure
        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=direct_failure) as direct, \
                mock.patch.object(slg_account, "_has_system_proxy",
                                  return_value=True), \
                mock.patch.object(slg_account.urllib.request, "build_opener",
                                  return_value=proxy), \
                mock.patch.object(slg_account.slg_server_transport.ORIGIN_OPENER,
                                  "open", side_effect=proxy_failure) as origin:
            with self.assertRaisesRegex(slg_account.AccountError, "注册请求尚未发送"):
                slg_account.create()

        direct.assert_called_once()
        proxy.open.assert_called_once()
        self.assertTrue(proxy.open.call_args.args[0].full_url.endswith("/healthz"))
        self.assertEqual(proxy.open.call_args.args[0].get_method(), "GET")
        origin.assert_called_once()
        self.assertEqual(origin.call_args.args[0].get_method(), "GET")

    def test_create_health_http_error_can_use_verified_origin_once(self):
        failure = urllib.error.HTTPError(
            slg_account._BASE + "/healthz", 503, "Unavailable", {},
            io.BytesIO(b"unavailable"))
        credentials = {"account_id": "acct", "login_key": "login",
                       "recovery_code": "recovery", "device_token": "session"}

        def origin_open(req, timeout):
            if req.get_method() == "GET":
                return io.BytesIO(b'{"ok":true}')
            self.assertEqual(req.full_url, slg_account._BASE + "/account/create")
            return io.BytesIO(json.dumps(credentials).encode("utf-8"))

        with mock.patch.object(slg_account._OPENER, "open",
                               side_effect=failure) as direct, \
                mock.patch.object(slg_account, "_has_system_proxy", return_value=False), \
                mock.patch.object(slg_account.urllib.request, "build_opener") as proxy, \
                mock.patch.object(slg_account.slg_server_transport.ORIGIN_OPENER,
                                  "open", side_effect=origin_open) as origin, \
                mock.patch.object(slg_account, "save_session"), \
                mock.patch.object(slg_account, "_attach_legacy_migration"):
            self.assertEqual(slg_account.create(), credentials)
        direct.assert_called_once()
        proxy.assert_not_called()
        self.assertEqual([call.args[0].get_method()
                          for call in origin.call_args_list], ["GET", "POST"])

    def test_login_uses_origin_after_direct_probe_failure(self):
        failure = urllib.error.URLError(socket.timeout("direct timed out"))
        reply = {"account_id": "acct", "device_token": "session"}

        def origin_open(req, timeout):
            if req.get_method() == "GET":
                return io.BytesIO(b'{"ok":true}')
            self.assertTrue(req.full_url.endswith("/account/login"))
            return io.BytesIO(json.dumps(reply).encode("utf-8"))

        with mock.patch.object(slg_account, "session", return_value=None), \
                mock.patch.object(slg_account._OPENER, "open",
                                  side_effect=failure) as direct, \
                mock.patch.object(slg_account, "_has_system_proxy", return_value=False), \
                mock.patch.object(slg_account.slg_server_transport.ORIGIN_OPENER,
                                  "open", side_effect=origin_open) as origin, \
                mock.patch.object(slg_account, "save_session"), \
                mock.patch.object(slg_account, "_attach_legacy_migration"):
            self.assertEqual(slg_account.login("key", "recovery"), reply)
        direct.assert_called_once()
        self.assertEqual([call.args[0].get_method()
                          for call in origin.call_args_list], ["GET", "POST"])


class SessionStorageTests(unittest.TestCase):
    def setUp(self):
        slg_account._ROUTE_OPENER = None
        slg_account._ROUTE_UNTIL = 0.0

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

    def test_malformed_session_shape_uses_recoverable_account_error(self):
        samples = [[], None, 123, "text", {},
                   {"account_id": [], "token": "ZHVtbXk="},
                   {"account_id": True, "token": "ZHVtbXk="},
                   {"account_id": " ", "token": "ZHVtbXk="},
                   {"account_id": "acct", "token": []},
                   {"account_id": "acct", "token": ""},
                   {"account_id": "acct", "token": "!!!"}]
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "session.json")
            for sample in samples:
                with self.subTest(sample_type=type(sample).__name__):
                    with open(path, "w", encoding="utf-8") as out:
                        json.dump(sample, out)
                    with mock.patch.object(slg_account, "_dpapi") as decrypt:
                        with self.assertRaisesRegex(slg_account.AccountError,
                                                    "设备凭证无法读取.*重新登录"):
                            slg_account._read_session(path)
                    decrypt.assert_not_called()

    def test_empty_decrypted_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "session.json")
            with open(path, "w", encoding="utf-8") as out:
                json.dump({"account_id": "acct", "token": "ZHVtbXk="}, out)
            with mock.patch.object(slg_account, "_dpapi", return_value=b""):
                with self.assertRaises(slg_account.AccountError):
                    slg_account._read_session(path)

    def test_missing_session_stays_a_guest(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(slg_account._read_session(os.path.join(root, "missing.json")))

    def test_new_account_returns_keys_even_if_local_save_fails(self):
        credentials = {"account_id": "acct", "login_key": "login",
                       "recovery_code": "recovery", "device_token": "session"}
        with mock.patch.object(slg_account, "_create_request_opener",
                               return_value=slg_account._OPENER), \
                mock.patch.object(slg_account, "request", return_value=credentials), \
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

    def test_login_migrates_with_new_account_session_not_queued_old_session(self):
        old = {"account_id": "old-account", "device_token": "old-device"}
        reply = {"account_id": "new-account", "device_token": "new-device"}
        observed = []

        def migration():
            observed.append(slg_account.session())
            return {"status": "nothing_to_migrate"}

        with mock.patch.object(slg_account, "request", return_value=reply), \
                mock.patch.object(slg_account, "save_session"), \
                mock.patch.object(slg_account, "migrate_legacy",
                                  side_effect=migration):
            with slg_account.use_session_snapshot(old):
                result = slg_account.login("login-key", "recovery-code")
                self.assertEqual(slg_account.session(), old)
        self.assertEqual(observed, [{"account_id": "new-account",
                                     "device_token": "new-device"}])
        self.assertEqual(result["legacy_migration"]["status"],
                         "nothing_to_migrate")

    def test_create_migrates_with_created_account_after_empty_queue_session(self):
        reply = {"account_id": "new-account", "login_key": "login-key",
                 "recovery_code": "recovery-code", "device_token": "new-device"}
        observed = []

        def migration():
            observed.append(slg_account.session())
            return {"status": "nothing_to_migrate"}

        with mock.patch.object(slg_account, "_create_request_opener"), \
                mock.patch.object(slg_account, "request", return_value=reply), \
                mock.patch.object(slg_account, "save_session"), \
                mock.patch.object(slg_account, "migrate_legacy",
                                  side_effect=migration):
            with slg_account.use_session_snapshot(None):
                result = slg_account.create()
                self.assertIsNone(slg_account.session())
        self.assertEqual(observed, [{"account_id": "new-account",
                                     "device_token": "new-device"}])
        self.assertEqual(result["legacy_migration"]["status"],
                         "nothing_to_migrate")

    def test_old_server_json_404_hides_optional_quests(self):
        response = urllib.error.HTTPError(
            "https://slg-king.com/account/quests", 404, "Not Found", {},
            io.BytesIO(b'{"error":"not found"}'))
        with mock.patch.object(slg_account, "session", return_value={
                "account_id": "account", "device_token": "session"}), \
                mock.patch.object(slg_account, "_selected_request_opener",
                                  return_value=slg_account._OPENER), \
                mock.patch.object(slg_account._OPENER, "open",
                                  side_effect=response):
            with self.assertRaisesRegex(slg_account.ServerNotReady,
                                        "每日任务接口尚未部署"):
                slg_account.quests()


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

    def test_grant_all_appearances_requires_developer_mode_and_uses_current_route(self):
        slg_account.set_developer_mode(False)
        with mock.patch.object(slg_account, "request") as send:
            with self.assertRaisesRegex(slg_account.AccountError, "解锁全部装扮"):
                slg_account.developer_grant_all_appearances("owner-key")
        send.assert_not_called()

        slg_account.set_developer_mode(True)
        reply = {"ok": True, "account_id": "developer",
                 "granted": ["title_a"], "titles": ["title_a"],
                 "equipped_title": "title_a"}
        with mock.patch.object(slg_account, "request", return_value=reply) as send:
            self.assertEqual(slg_account.developer_grant_all_appearances(" owner-key "),
                             reply)
        self.assertEqual(send.call_args.args[0],
                         "/account/developer/appearances/grant-all")
        self.assertEqual(send.call_args.kwargs["method"], "POST")
        self.assertEqual(send.call_args.kwargs["extra_headers"], {
            "X-SLG-Developer-Key": "owner-key"})
        with mock.patch.object(slg_account, "request", return_value=[]):
            with self.assertRaisesRegex(slg_account.AccountError, "开发者装扮状态"):
                slg_account.developer_grant_all_appearances("owner-key")


class LegacyBackupProvenanceTests(unittest.TestCase):
    def setUp(self):
        key = mock.patch.object(slg_db, "integrity_key", return_value="test-key")
        key.start()
        self.addCleanup(key.stop)
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        db = mock.patch.object(slg_db, "session",
                               side_effect=lambda: contextlib.nullcontext(self.conn))
        db.start()
        self.addCleanup(db.stop)
        account = mock.patch.object(slg_account, "session", return_value={
            "account_id": "test-account", "device_token": "test-token"})
        account.start()
        self.addCleanup(account.stop)

    def test_imported_999_points_are_retained_without_migration_post(self):
        slg_db.import_user_data(self.conn, {"points_log": [
            {"delta": 999, "reason": "backup", "at": "2026-01-01"}]})
        with mock.patch.object(slg_account, "authenticated") as send:
            with self.assertRaisesRegex(slg_account.AccountError, "导入备份.*管理员核对"):
                slg_account.migrate_legacy()
        send.assert_not_called()
        self.assertEqual(slg_db.points_balance(self.conn), 999)
        self.assertIsNone(slg_db.get_pref(self.conn, slg_account._LEGACY_SOURCE_PREF))

    def test_native_unsealed_and_sealed_assets_still_migrate(self):
        for sealed in (False, True):
            with self.subTest(sealed=sealed):
                self.conn.execute("DELETE FROM points_log")
                if sealed:
                    slg_db.add_points(self.conn, 70, "native")
                else:
                    self.conn.execute("INSERT INTO points_log (delta,reason,at)"
                                      " VALUES (70,'native','2026-01-01')")
                slg_db.invalidate_ledger(self.conn)
                with mock.patch.object(slg_account, "authenticated", return_value={
                        "ok": True, "status": "pending_manual_review"}) as send:
                    result = slg_account.migrate_legacy()
                self.assertEqual(result["status"], "pending_manual_review")
                send.assert_called_once()
                self.assertEqual(send.call_args.kwargs["payload"]["points"], 70)
                self.assertEqual(slg_db.points_balance(self.conn), 70)

    def test_completed_migration_cache_precedes_import_marker(self):
        cached = {"ok": True, "status": "migrated", "points": 70, "titles": []}
        slg_db.set_pref(self.conn, slg_db.LEGACY_IMPORTED_ASSETS_PREF, "1")
        slg_db.set_pref(self.conn, slg_account._LEGACY_DONE_PREF + "test-account", "1")
        slg_db.set_pref(self.conn, slg_account._LEGACY_RESULT_PREF + "test-account",
                        json.dumps(cached))
        with mock.patch.object(slg_account, "authenticated") as send:
            self.assertEqual(slg_account.migrate_legacy(), cached)
        send.assert_not_called()

    def test_completed_local_debit_precedes_import_marker(self):
        slg_db.set_pref(self.conn, slg_db.LEGACY_IMPORTED_ASSETS_PREF, "1")
        slg_db.set_pref(self.conn, "cloud.legacy_migration_local_source", "a" * 32)
        with mock.patch.object(slg_account, "authenticated") as send:
            self.assertEqual(slg_account.migrate_legacy()["status"],
                             "already_completed_local")
        send.assert_not_called()


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
