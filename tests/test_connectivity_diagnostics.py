"""Credential-free connection diagnostics for the desktop cloud client."""

import errno
import io
import json
import socket
import ssl
import unittest
import urllib.error
from unittest import mock

import slg_account


class ConnectivityDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("127.0.0.1", 443)),
        ]

    def _patch_ready_transport(self, opener):
        return (
            mock.patch.object(slg_account, "_connectivity_resolve",
                              return_value=self.addresses),
            mock.patch.object(slg_account, "_connectivity_probe_tcp"),
            mock.patch.object(slg_account, "_connectivity_probe_tls"),
            mock.patch.object(slg_account, "_connectivity_routes",
                              return_value=[("direct", opener)]),
        )

    def test_success_uses_only_anonymous_gets_and_discards_manifest_body(self):
        calls = []
        secret = "do-not-copy-server-private-data"

        def open_request(req, timeout):
            calls.append((req, timeout))
            self.assertIsNone(req.data)
            self.assertIsNone(req.get_header("Authorization"))
            self.assertEqual(req.get_method(), "GET")
            if req.full_url.endswith("/healthz"):
                return io.BytesIO(b"ok")
            return io.BytesIO(json.dumps({
                "version": "0.24.3", "private_field": secret,
            }).encode("utf-8"))

        opener = mock.Mock()
        opener.open.side_effect = open_request
        patches = self._patch_ready_transport(opener)
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(slg_account, "session",
                                  side_effect=AssertionError("session read")):
            result = slg_account.diagnose_connectivity(timeout=0.5)

        self.assertEqual(result["schema"], "slgking-connectivity-v1")
        self.assertEqual(result["checks"]["dns"]["code"], "resolved")
        self.assertEqual(result["checks"]["tcp"]["code"], "connected")
        self.assertEqual(result["checks"]["tls"]["code"], "verified")
        self.assertEqual(result["checks"]["healthz"]["status"], "ok")
        self.assertEqual(result["checks"]["manifest"]["code"], "valid_manifest")
        self.assertEqual([req.full_url.rsplit("/", 1)[-1]
                          for req, _timeout in calls], ["healthz", "manifest.json"])
        self.assertTrue(all(timeout <= 5.0 for _req, timeout in calls))
        self.assertNotIn(secret, repr(result))

    def test_dns_failure_is_distinct_and_skips_raw_socket_stages(self):
        with mock.patch.object(slg_account, "_connectivity_resolve",
                               side_effect=socket.gaierror(
                                   socket.EAI_NONAME, "private resolver detail")), \
                mock.patch.object(slg_account, "_connectivity_probe_tcp") as tcp, \
                mock.patch.object(slg_account, "_connectivity_probe_tls") as tls, \
                mock.patch.object(slg_account, "_connectivity_routes",
                                  return_value=[]):
            result = slg_account.diagnose_connectivity()

        self.assertEqual(result["checks"]["dns"], {
            "status": "failed", "code": "dns_failed"})
        self.assertEqual(result["checks"]["tcp"]["code"], "dns_unavailable")
        self.assertEqual(result["checks"]["tls"]["code"], "dns_unavailable")
        tcp.assert_not_called()
        tls.assert_not_called()
        self.assertNotIn("private resolver detail", result["share_text"])

    def test_refused_tcp_and_bad_tls_certificate_have_separate_codes(self):
        with mock.patch.object(slg_account, "_connectivity_resolve",
                               return_value=self.addresses), \
                mock.patch.object(slg_account, "_connectivity_probe_tcp",
                                  side_effect=ConnectionRefusedError(
                                      errno.ECONNREFUSED, "private endpoint")), \
                mock.patch.object(slg_account, "_connectivity_probe_tls") as tls, \
                mock.patch.object(slg_account, "_connectivity_routes",
                                  return_value=[]):
            result = slg_account.diagnose_connectivity()
        self.assertEqual(result["checks"]["tcp"]["code"], "connection_refused")
        self.assertEqual(result["checks"]["tls"]["code"], "tcp_unavailable")
        tls.assert_not_called()

        certificate_error = ssl.SSLCertVerificationError(
            "private certificate detail")
        with mock.patch.object(slg_account, "_connectivity_resolve",
                               return_value=self.addresses), \
                mock.patch.object(slg_account, "_connectivity_probe_tcp"), \
                mock.patch.object(slg_account, "_connectivity_probe_tls",
                                  side_effect=certificate_error), \
                mock.patch.object(slg_account, "_connectivity_routes",
                                  return_value=[]):
            result = slg_account.diagnose_connectivity()
        self.assertEqual(result["checks"]["tls"]["code"], "certificate_invalid")
        self.assertNotIn("private certificate detail", repr(result))

    def test_old_server_health_404_is_distinguished_from_manifest_success(self):
        opener = mock.Mock()

        def open_request(req, timeout):
            if req.full_url.endswith("/healthz"):
                raise urllib.error.HTTPError(
                    req.full_url, 404, "not found", {}, io.BytesIO(b"private body"))
            return io.BytesIO(b'{"version":"0.23.0"}')

        opener.open.side_effect = open_request
        patches = self._patch_ready_transport(opener)
        with patches[0], patches[1], patches[2], patches[3]:
            result = slg_account.diagnose_connectivity()

        self.assertEqual(result["checks"]["healthz"]["status"], "warning")
        self.assertEqual(result["checks"]["healthz"]["code"], "endpoint_missing")
        self.assertEqual(result["checks"]["manifest"]["status"], "ok")
        self.assertNotIn("private body", repr(result))

    def test_http_failure_reports_status_code_without_reason_or_body(self):
        secret = "sensitive-response-material"
        opener = mock.Mock()

        def fail(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 503, secret, {},
                                         io.BytesIO(secret.encode("utf-8")))

        opener.open.side_effect = fail
        patches = self._patch_ready_transport(opener)
        with patches[0], patches[1], patches[2], patches[3]:
            result = slg_account.diagnose_connectivity()

        self.assertEqual(result["checks"]["healthz"]["code"], "http_503")
        self.assertEqual(result["checks"]["manifest"]["code"], "http_503")
        self.assertNotIn(secret, repr(result))


if __name__ == "__main__":
    unittest.main()
