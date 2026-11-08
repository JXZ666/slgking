"""Verified direct HTTPS route to the catalogue/account origin.

Cloudflare's public hostname remains the normal route.  A few networks can
reach the old origin IP but not the Cloudflare edge; this opener keeps the
hostname, SNI, and certificate checks while connecting to that known IP.
Only requests for slg-king.com are accepted.  The Cloudflare Origin CA root
is intentionally scoped to this opener and is never installed system-wide.
"""

import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request


SERVER_HOST = "slg-king.com"
ORIGIN_IP = "43.130.240.89"

# Cloudflare's published Origin RSA CA root.  The installed origin certificate
# for slg-king.com chains to this root.  Revisit before its 2029 expiry.
# https://developers.cloudflare.com/ssl/static/origin_ca_rsa_root.pem
_ORIGIN_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIEADCCAuigAwIBAgIID+rOSdTGfGcwDQYJKoZIhvcNAQELBQAwgYsxCzAJBgNV
BAYTAlVTMRkwFwYDVQQKExBDbG91ZEZsYXJlLCBJbmMuMTQwMgYDVQQLEytDbG91
ZEZsYXJlIE9yaWdpbiBTU0wgQ2VydGlmaWNhdGUgQXV0aG9yaXR5MRYwFAYDVQQH
Ew1TYW4gRnJhbmNpc2NvMRMwEQYDVQQIEwpDYWxpZm9ybmlhMB4XDTE5MDgyMzIx
MDgwMFoXDTI5MDgxNTE3MDAwMFowgYsxCzAJBgNVBAYTAlVTMRkwFwYDVQQKExBD
bG91ZEZsYXJlLCBJbmMuMTQwMgYDVQQLEytDbG91ZEZsYXJlIE9yaWdpbiBTU0wg
Q2VydGlmaWNhdGUgQXV0aG9yaXR5MRYwFAYDVQQHEw1TYW4gRnJhbmNpc2NvMRMw
EQYDVQQIEwpDYWxpZm9ybmlhMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKC
AQEAwEiVZ/UoQpHmFsHvk5isBxRehukP8DG9JhFev3WZtG76WoTthvLJFRKFCHXm
V6Z5/66Z4S09mgsUuFwvJzMnE6Ej6yIsYNCb9r9QORa8BdhrkNn6kdTly3mdnykb
OomnwbUfLlExVgNdlP0XoRoeMwbQ4598foiHblO2B/LKuNfJzAMfS7oZe34b+vLB
yrP/1bgCSLdc1AxQc1AC0EsQQhgcyTJNgnG4va1c7ogPlwKyhbDyZ4e59N5lbYPJ
SmXI/cAe3jXj1FBLJZkwnoDKe0v13xeF+nF32smSH0qB7aJX2tBMW4TWtFPmzs5I
lwrFSySWAdwYdgxw180yKU0dvwIDAQABo2YwZDAOBgNVHQ8BAf8EBAMCAQYwEgYD
VR0TAQH/BAgwBgEB/wIBAjAdBgNVHQ4EFgQUJOhTV118NECHqeuU27rhFnj8KaQw
HwYDVR0jBBgwFoAUJOhTV118NECHqeuU27rhFnj8KaQwDQYJKoZIhvcNAQELBQAD
ggEBAHwOf9Ur1l0Ar5vFE6PNrZWrDfQIMyEfdgSKofCdTckbqXNTiXdgbHs+TWoQ
wAB0pfJDAHJDXOTCWRyTeXOseeOi5Btj5CnEuw3P0oXqdqevM1/+uWp0CM35zgZ8
VD4aITxity0djzE6Qnx3Syzz+ZkoBgTnNum7d9A66/V636x4vTeqbZFBr9erJzgz
hhurjcoacvRNhnjtDRM0dPeiCJ50CP3wEYuvUzDHUaowOsnLCjQIkWbR7Ni6KEIk
MOz2U0OBSif3FTkhCgZWQKOOLo1P42jHC3ssUZAtVNXrCk3fw9/E15k8NPkBazZ6
0iykLhH1trywrKRMVw67F44IE8Y=
-----END CERTIFICATE-----"""


class _OriginHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, **kwargs):
        super().__init__(host, **kwargs)
        self._create_connection = self._connect_to_origin

    def _connect_to_origin(self, address, timeout, source_address):
        if self.host != SERVER_HOST or address[0] != SERVER_HOST:
            raise OSError("origin route rejects a different hostname")
        return socket.create_connection((ORIGIN_IP, address[1]), timeout,
                                        source_address)


class _OriginHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        parsed = urllib.parse.urlsplit(req.full_url)
        if (parsed.scheme.lower() != "https" or parsed.hostname != SERVER_HOST
                or parsed.port not in (None, 443)):
            raise urllib.error.URLError("origin route rejects a different URL")
        return self.do_open(_OriginHTTPSConnection, req,
                            context=self._context)


class _NoOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A fallback request may carry a device token. Do not follow a server
        # redirect to a different destination or downgrade to plaintext HTTP.
        raise urllib.error.HTTPError(req.full_url, code,
                                     "origin fallback does not follow redirects",
                                     headers, fp)


def _build_origin_opener():
    context = ssl.create_default_context(cadata=_ORIGIN_CA_PEM)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _OriginHTTPSHandler(context=context),
        _NoOriginRedirect())


ORIGIN_OPENER = _build_origin_opener()
