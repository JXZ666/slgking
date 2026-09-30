"""The alternate origin route must retain TLS hostname validation."""

import ssl
import unittest
import urllib.error
import urllib.request
from unittest import mock

import slg_server_transport as transport


class VerifiedOriginTransport(unittest.TestCase):
    def test_origin_handler_requires_certificate_and_fixed_hostname(self):
        handler = next(item for item in transport.ORIGIN_OPENER.handlers
                       if isinstance(item, transport._OriginHTTPSHandler))
        self.assertTrue(handler._context.check_hostname)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        for url in ("https://example.com/healthz",
                    "http://slg-king.com/healthz",
                    "https://slg-king.com:8443/healthz"):
            with self.subTest(url=url), self.assertRaises(urllib.error.URLError):
                handler.https_open(urllib.request.Request(url))
        redirect = next(item for item in transport.ORIGIN_OPENER.handlers
                        if isinstance(item, transport._NoOriginRedirect))
        with self.assertRaises(urllib.error.HTTPError):
            redirect.redirect_request(
                urllib.request.Request("https://slg-king.com/account/me"),
                None, 302, "Found", {}, "http://example.com/")

    def test_connection_uses_origin_ip_but_keeps_hostname_for_tls(self):
        context = ssl.create_default_context(cadata=transport._ORIGIN_CA_PEM)
        conn = transport._OriginHTTPSConnection(
            transport.SERVER_HOST, context=context)
        sentinel = object()
        with mock.patch.object(transport.socket, "create_connection",
                               return_value=sentinel) as connect:
            self.assertIs(conn._connect_to_origin(
                (transport.SERVER_HOST, 443), 7, None), sentinel)
        connect.assert_called_once_with((transport.ORIGIN_IP, 443), 7, None)
        self.assertEqual(conn.host, transport.SERVER_HOST)


if __name__ == "__main__":
    unittest.main()
