"""Small client for the cloud account service.

The device session is the only credential kept on this computer. Windows
DPAPI encrypts it for the current Windows user; login and recovery secrets are
shown once and must be kept by the account owner.
"""

import base64
import binascii
import ctypes
from contextlib import contextmanager
from ctypes import wintypes
import errno
import hashlib
import http.client
import json
import os
import queue
import sqlite3
import socket
import ssl
import threading
import time
import uuid
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import slg_db
import slg_server_transport
from slg_sync_server import SERVER_BASE


class AccountError(Exception):
    """An account operation that can be reported directly to the user."""


class ServerNotReady(AccountError):
    """The installed server has not received the 0.23 account endpoints."""


class SessionExpired(AccountError):
    """The device session expired or was revoked."""


class OwnerAccountAlreadyBound(AccountError):
    """The installation already has its one bound owner account."""

    def __init__(self, message, account_id=None):
        super().__init__(message)
        self.account_id = str(account_id or "")


_BASE = SERVER_BASE.rstrip("/")
_TIMEOUT = 10
_PREFLIGHT_TIMEOUT = 5
_USER_AGENT = "SLGKing/0.23.8 (+https://slg-king.com; desktop client)"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError,
                   http.client.HTTPException)
_DEVELOPER_MODE = False
_SESSION_CONTEXT = threading.local()
_ROUTE_OPENER = None
_ROUTE_UNTIL = 0.0
_CONNECTIVITY_TIMEOUT = 2.0
_CONNECTIVITY_MAX_BODY = 65536


def _connectivity_error_code(exc):
    """Map a network exception to a safe, stable diagnostic code.

    Exception text can contain URLs, proxy details, or server response data, so
    it is deliberately never returned by the diagnostics API.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return "http_%d" % int(exc.code)
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "certificate_invalid"
    if isinstance(reason, socket.gaierror):
        return "dns_failed"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ssl.SSLError):
        return "tls_failed"
    if isinstance(reason, OSError):
        if reason.errno == errno.ECONNREFUSED:
            return "connection_refused"
        if reason.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            return "network_unreachable"
        return "connection_failed"
    return "request_failed"


def _connectivity_resolve(host, timeout):
    """Resolve DNS in a daemon worker so a stuck system resolver is bounded."""
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(
                host, 443, type=socket.SOCK_STREAM)))
        except Exception as exc:  # worker must report resolver failures
            result.put((False, exc))

    worker = threading.Thread(target=resolve, name="slg-dns-diagnostic",
                              daemon=True)
    worker.start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("DNS resolver timed out") from exc
    if not ok:
        raise value
    if not value:
        raise socket.gaierror("no addresses returned")
    return value


def _connectivity_probe_tcp(addresses, timeout):
    """Try one resolved address at a time; return after the first TCP success."""
    last_error = None
    for family, socktype, proto, _canonname, sockaddr in addresses[:4]:
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            connection.settimeout(timeout)
            connection.connect(sockaddr)
            return
        except OSError as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("no usable address")


def _connectivity_probe_tls(host, addresses, timeout):
    """Check the default system trust chain and hostname for the HTTPS host."""
    last_error = None
    context = ssl.create_default_context()
    for family, socktype, proto, _canonname, sockaddr in addresses[:4]:
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            connection.settimeout(timeout)
            connection.connect(sockaddr)
            with context.wrap_socket(connection, server_hostname=host):
                connection = None  # wrapped socket was closed by the context
            return
        except OSError as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("no usable address")


def _connectivity_routes():
    """Return the same safe GET routes used by account preflight checks."""
    routes = [("direct", _OPENER)]
    health_url = _BASE + "/healthz"
    try:
        if _has_system_proxy(health_url):
            routes.append(("system_proxy", urllib.request.build_opener()))
    except (OSError, TypeError, ValueError):
        # Diagnostics should still cover the direct and pinned-origin routes.
        pass
    routes.append(("verified_origin", slg_server_transport.ORIGIN_OPENER))
    return routes


def _connectivity_http_attempt(opener, path, timeout, expect_manifest=False):
    """Issue a credential-free GET and return only a sanitized result tuple."""
    req = urllib.request.Request(_BASE + path, headers={
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }, method="GET")
    with opener.open(req, timeout=timeout) as response:
        body = response.read(_CONNECTIVITY_MAX_BODY + 1)
    if not expect_manifest:
        return "ok", "response_ok"
    if len(body) > _CONNECTIVITY_MAX_BODY:
        return "failed", "response_too_large"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        return "failed", "invalid_manifest"
    if not isinstance(payload, dict):
        return "failed", "invalid_manifest"
    return "ok", "valid_manifest"


def _connectivity_probe_http(path, routes, timeout, expect_manifest=False):
    attempts = []
    for route_name, opener in routes:
        try:
            status, code = _connectivity_http_attempt(
                opener, path, timeout, expect_manifest=expect_manifest)
            attempts.append({"route": route_name, "status": status,
                             "code": code})
            if status == "ok":
                return {"status": "ok", "code": code, "route": route_name,
                        "attempts": attempts}
        except urllib.error.HTTPError as exc:
            code = "endpoint_missing" if exc.code == 404 else _connectivity_error_code(exc)
            status = "warning" if exc.code == 404 else "failed"
            attempts.append({"route": route_name, "status": status,
                             "code": code})
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as exc:
            attempts.append({"route": route_name, "status": "failed",
                             "code": _connectivity_error_code(exc)})

    missing = next((item for item in attempts
                    if item["code"] == "endpoint_missing"), None)
    if missing:
        return {"status": "warning", "code": "endpoint_missing",
                "route": missing["route"], "attempts": attempts}
    if attempts:
        last = attempts[-1]
        return {"status": "failed", "code": last["code"],
                "route": last["route"], "attempts": attempts}
    return {"status": "failed", "code": "no_route", "route": None,
            "attempts": attempts}


_CONNECTIVITY_COPY = {
    "dns_unavailable": "DNS \u672a\u901a\u8fc7\uff0c\u5df2\u8df3\u8fc7\u6b64\u68c0\u67e5",
    "tcp_unavailable": "TCP \u672a\u8fde\u63a5\uff0c\u5df2\u8df3\u8fc7 TLS \u68c0\u67e5",
    "resolved": "DNS 解析成功",
    "dns_failed": "DNS 解析失败",
    "timeout": "连接超时",
    "connected": "TCP 连接成功",
    "connection_refused": "服务器拒绝连接",
    "network_unreachable": "当前网络无法到达服务器",
    "connection_failed": "无法建立网络连接",
    "verified": "TLS 证书验证成功",
    "certificate_invalid": "TLS 证书验证失败",
    "tls_failed": "TLS 握手失败",
    "response_ok": "接口可访问",
    "valid_manifest": "目录版本接口正常",
    "invalid_manifest": "目录版本接口返回格式异常",
    "response_too_large": "目录版本响应超出诊断上限",
    "endpoint_missing": "服务器可连接，但该接口不存在",
    "no_route": "没有可用的 HTTPS 路由",
    "request_failed": "HTTPS 请求失败",
}


def _connectivity_display(check):
    code = check.get("code", "request_failed")
    label = _CONNECTIVITY_COPY.get(code, "HTTPS 请求失败（%s）" % code)
    route = check.get("route")
    if route:
        route_label = {"direct": "直连", "system_proxy": "系统代理",
                       "verified_origin": "验证源站"}.get(route, "其他路由")
        return "%s（%s）" % (label, route_label)
    return label


def diagnose_connectivity(timeout=_CONNECTIVITY_TIMEOUT):
    """Run bounded, read-only connectivity checks without account credentials.

    The result contains only fixed status codes and route labels. It never
    reads the local account session, sends account requests, or returns server
    response text. The timeout applies to each network operation and is
    clamped to a short finite range.
    """
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = _CONNECTIVITY_TIMEOUT
    timeout = min(5.0, max(0.25, timeout))

    checks = {}
    host = urllib.parse.urlsplit(_BASE).hostname or "slg-king.com"
    try:
        addresses = _connectivity_resolve(host, timeout)
        checks["dns"] = {"status": "ok", "code": "resolved"}
    except (OSError, TimeoutError, socket.gaierror) as exc:
        addresses = None
        checks["dns"] = {"status": "failed",
                         "code": _connectivity_error_code(exc)}

    if addresses:
        try:
            _connectivity_probe_tcp(addresses, timeout)
            checks["tcp"] = {"status": "ok", "code": "connected"}
            try:
                _connectivity_probe_tls(host, addresses, timeout)
                checks["tls"] = {"status": "ok", "code": "verified"}
            except (OSError, TimeoutError, ssl.SSLError) as exc:
                checks["tls"] = {"status": "failed",
                                 "code": _connectivity_error_code(exc)}
        except (OSError, TimeoutError) as exc:
            checks["tcp"] = {"status": "failed",
                             "code": _connectivity_error_code(exc)}
            checks["tls"] = {"status": "skipped", "code": "tcp_unavailable"}
    else:
        checks["tcp"] = {"status": "skipped", "code": "dns_unavailable"}
        checks["tls"] = {"status": "skipped", "code": "dns_unavailable"}

    try:
        routes = _connectivity_routes()
    except Exception:
        routes = []
    checks["healthz"] = _connectivity_probe_http(
        "/healthz", routes, timeout)
    checks["manifest"] = _connectivity_probe_http(
        "/manifest.json", routes, timeout, expect_manifest=True)

    copy_lines = ["SLGking 连接诊断（只读）"]
    copy_lines.extend((
        "DNS：%s" % _connectivity_display(checks["dns"]),
        "TCP：%s" % _connectivity_display(checks["tcp"]),
        "TLS：%s" % _connectivity_display(checks["tls"]),
        "服务状态：%s" % _connectivity_display(checks["healthz"]),
        "目录版本：%s" % _connectivity_display(checks["manifest"]),
    ))
    return {"schema": "slgking-connectivity-v1", "checks": checks,
            "share_text": "\n".join(copy_lines)}


def set_client_version(version):
    """Identify the version of the executable that is actually running."""
    global _USER_AGENT
    _USER_AGENT = "SLGKing/%s (+https://slg-king.com; desktop client)" % version


def set_developer_mode(enabled):
    """Select the owner identity for this running client process."""
    global _DEVELOPER_MODE
    _DEVELOPER_MODE = bool(enabled)
    if _DEVELOPER_MODE:
        _move_legacy_developer_session()


def developer_mode():
    """Return the active client identity, not the local capability flag."""
    return bool(_DEVELOPER_MODE)


def _is_preconnect_error(exc):
    """Return true only for failures that prove no HTTP request was sent."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.gaierror):
        return True
    # These errors come from resolving or establishing a socket. Do not treat
    # resets, timeouts, TLS failures, or arbitrary URLError reasons as safe to
    # replay: the server may already have received a non-idempotent request.
    return (isinstance(reason, OSError)
            and reason.errno in {
                errno.ECONNREFUSED,
                errno.EHOSTUNREACH,
                errno.ENETUNREACH,
                errno.EADDRNOTAVAIL,
            })


def _has_system_proxy(url):
    """Check for a configured proxy for this fixed HTTPS server origin."""
    try:
        target = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(_BASE)
        target_port = target.port or (443 if target.scheme.lower() == "https" else None)
        base_port = base.port or (443 if base.scheme.lower() == "https" else None)
        if (target.scheme.lower() != "https"
                or base.scheme.lower() != "https"
                or target.hostname != base.hostname
                or target_port != base_port):
            return False
        proxies = urllib.request.getproxies()
        if not (proxies.get("https") or proxies.get("all")):
            return False
        return not urllib.request.proxy_bypass(target.hostname or "")
    except (TypeError, ValueError, OSError):
        # If proxy configuration cannot be checked, preserve the original
        # direct failure instead of guessing and issuing another request.
        return False


def _open_with_proxy_fallback(req, url, timeout, opener=None,
                              allow_proxy_fallback=True):
    """Prefer direct HTTPS, then use the system proxy only after pre-connect failure."""
    opener = opener or _OPENER
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError:
        # HTTP responses, including proxy/auth/server errors, are authoritative
        # responses and must never cause an automatic second attempt.
        raise
    except _NETWORK_ERRORS as direct_error:
        if (not allow_proxy_fallback or opener is not _OPENER
                or not _is_preconnect_error(direct_error)
                or not _has_system_proxy(url)):
            raise
        # This opener uses urllib's configured system proxy. The fallback is
        # performed at most once and only while the direct TCP connection is
        # known not to have been established.
        return urllib.request.build_opener().open(req, timeout=timeout)


def _probe_create_route(opener):
    """Probe the fixed account origin without sending an account operation."""
    url = _BASE + "/healthz"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }, method="GET")
    try:
        with opener.open(req, timeout=_PREFLIGHT_TIMEOUT) as resp:
            # Consume the small health response so read failures are included.
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        # Earlier account servers may lack /healthz. Their public manifest is
        # enough to verify the route before one account POST is sent.
        manifest = urllib.request.Request(_BASE + "/manifest.json", headers={
            "Accept": "application/json", "User-Agent": _USER_AGENT,
        }, method="GET")
        with opener.open(manifest, timeout=_PREFLIGHT_TIMEOUT) as resp:
            body = resp.read(65537)
        if len(body) > 65536 or not isinstance(json.loads(body), dict):
            raise ValueError("invalid catalogue manifest during route check")


def _create_request_opener(action="注册"):
    """Choose a working HTTPS route before a single account operation."""
    health_url = _BASE + "/healthz"
    routes = [("直连", lambda: _OPENER)]
    if _has_system_proxy(health_url):
        routes.append(("系统代理", urllib.request.build_opener))
    routes.append(("源站 HTTPS", lambda: slg_server_transport.ORIGIN_OPENER))
    last_error = None
    for _name, make_opener in routes:
        opener = make_opener()
        try:
            _probe_create_route(opener)
            return opener
        except (urllib.error.HTTPError, *_NETWORK_ERRORS, ValueError) as exc:
            # All probes are GETs. A failed route has not sent a credential or
            # created an account, so trying the next verified route is safe.
            last_error = exc
    raise AccountError(
        "%s前的云端连接检查失败，%s请求尚未发送。请检查网络或代理设置后重试。"
        % (action, action)) from last_error


def _selected_request_opener(action, refresh=False):
    """Reuse a recently verified route, but never retry an uncertain POST."""
    global _ROUTE_OPENER, _ROUTE_UNTIL
    if not refresh and _ROUTE_OPENER is not None and time.monotonic() < _ROUTE_UNTIL:
        return _ROUTE_OPENER
    opener = _create_request_opener(action)
    _ROUTE_OPENER, _ROUTE_UNTIL = opener, time.monotonic() + 120
    return opener


def _network_error_message(path, method, exc):
    if _is_preconnect_error(exc):
        return "无法建立与云端服务器的连接，请检查网络或系统代理设置后重试。"
    if method.upper() == "POST" and path == "/account/create":
        return ("注册请求超时或连接中断，云端可能已经处理了注册。"
                "请勿立即再次创建账号；先联系支持核查，以免留下无法找回的重复账号。")
    if method.upper() == "POST":
        return ("请求超时或连接中断，云端可能已经处理本次操作。"
                "为避免重复提交，请先核对结果后再决定是否重试。")
    return "请求超时或连接中断，请检查网络后重试。"


def _blob(data):
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi(data, protect):
    if os.name != "nt":
        raise AccountError("云端设备凭证只能保存在 Windows 上")
    input_blob, _keepalive = _blob(data)
    output_blob, _ = _blob(b"")
    crypt32 = ctypes.windll.crypt32
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.restype = wintypes.BOOL
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    # No optional entropy; Windows user scope is the relevant trust boundary.
    if not func(ctypes.byref(input_blob), None, None, None, None, 0,
                ctypes.byref(output_blob)):
        raise AccountError("Windows 无法%s设备凭证" % ("保存" if protect else "读取"))
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        local_free(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _session_path():
    """The ordinary account slot, retaining the historical filename."""
    return os.path.join(slg_db.app_dir(), "cloud_session.json")


def _developer_session_path():
    return os.path.join(slg_db.app_dir(), "cloud_admin_session.json")


def _write_session(path, account_id, device_token):
    if not account_id or not device_token:
        raise AccountError("服务器未返回完整的账号会话")
    encrypted = base64.b64encode(_dpapi(device_token.encode("utf-8"), True)).decode("ascii")
    parent = os.path.dirname(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix="cloud_session-", suffix=".tmp", dir=parent)
    except OSError as exc:
        raise AccountError("本机设备会话保存失败，请妥善保存登录密钥和恢复码") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"account_id": str(account_id), "token": encrypted}, out)
        os.replace(tmp, path)
    except OSError as exc:
        raise AccountError("本机设备会话保存失败，请妥善保存登录密钥和恢复码") from exc
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_session(path):
    try:
        with open(path, encoding="utf-8") as inp:
            data = json.load(inp)
        account_id = str(data["account_id"])
        return {"account_id": data["account_id"],
                "device_token": _dpapi(base64.b64decode(data["token"]), False).decode("utf-8")}
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, OSError, UnicodeError, binascii.Error) as exc:
        raise AccountError("本机云端设备凭证无法读取，请重新登录") from exc


def _move_legacy_developer_session():
    """Move a pre-split developer session out of cloud_session.json safely."""
    legacy_path = _session_path()
    admin_path = _developer_session_path()
    try:
        with open(legacy_path, encoding="utf-8") as inp:
            data = json.load(inp)
        if str(data.get("account_id") or "") != "developer":
            return False
        if os.path.exists(admin_path):
            # Keep the already separated slot as authoritative, then make the
            # legacy filename available for an ordinary account.
            if _read_session(admin_path) is None:
                return False
            os.unlink(legacy_path)
            return True
        os.replace(legacy_path, admin_path)
        return True
    except FileNotFoundError:
        return False
    except (ValueError, AttributeError, OSError, UnicodeError):
        # A malformed legacy file remains in place for the normal corruption
        # handling path; never delete it during an attempted migration.
        return False


def save_session(account_id, device_token):
    """Save the ordinary and fixed developer identities to separate slots."""
    if str(account_id or "") == "developer":
        _write_session(_developer_session_path(), account_id, device_token)
        return
    _move_legacy_developer_session()
    _write_session(_session_path(), account_id, device_token)


def session_for_mode(developer_mode):
    """Read a session without changing the active request identity."""
    if developer_mode:
        current = _read_session(_developer_session_path())
        if current:
            return current if current.get("account_id") == "developer" else None
        if _move_legacy_developer_session():
            current = _read_session(_developer_session_path())
            if current and current.get("account_id") == "developer":
                return current
        # Preserve old files that could not be moved, while refusing to treat a
        # regular account as the developer identity.
        legacy = _read_session(_session_path())
        return legacy if legacy and legacy.get("account_id") == "developer" else None
    current = _read_session(_session_path())
    if current and current.get("account_id") != "developer":
        return current
    if current and current.get("account_id") == "developer":
        _move_legacy_developer_session()
    return None


def session():
    override = getattr(_SESSION_CONTEXT, "snapshot", _SESSION_CONTEXT)
    if override is not _SESSION_CONTEXT:
        return dict(override) if isinstance(override, dict) else None
    return session_for_mode(_DEVELOPER_MODE)


@contextmanager
def use_session_snapshot(snapshot):
    """Pin account API calls in a worker to the identity captured at enqueue."""
    previous = getattr(_SESSION_CONTEXT, "snapshot", _SESSION_CONTEXT)
    _SESSION_CONTEXT.snapshot = dict(snapshot) if isinstance(snapshot, dict) else None
    try:
        yield
    finally:
        if previous is _SESSION_CONTEXT:
            try:
                del _SESSION_CONTEXT.snapshot
            except AttributeError:
                pass
        else:
            _SESSION_CONTEXT.snapshot = previous


def forget_session():
    path = (_developer_session_path() if _DEVELOPER_MODE else _session_path())
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def request(path, method="GET", payload=None, token=None, extra_headers=None,
            _opener=None, _allow_proxy_fallback=True, _select_route=False):
    global _ROUTE_OPENER, _ROUTE_UNTIL
    route_selected = False
    if _select_route and _opener is None:
        action = "登录" if path in ("/account/login", "/account/admin-login") else "操作"
        _opener = _selected_request_opener(
            action, refresh=path in ("/account/login", "/account/admin-login"))
        _allow_proxy_fallback = False
        route_selected = True
    # Cloudflare blocks urllib's default Python-urllib signature at the edge.
    # Identify the actual desktop client instead of relying on that default.
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(_BASE + path, data=body, headers=headers,
                                 method=method)
    try:
        with _open_with_proxy_fallback(
                req, req.full_url, _TIMEOUT, opener=_opener,
                allow_proxy_fallback=_allow_proxy_fallback) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except urllib.error.HTTPError as exc:
        detail = {}
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
            detail = parsed if isinstance(parsed, dict) else {}
            msg = (detail.get("error") or detail.get("message")
                   or detail.get("detail") or detail.get("title"))
            if detail.get("cloudflare_error") and detail.get("error_code"):
                msg = "%s (Cloudflare %s)" % (msg or "请求被边缘防护拦截",
                                                detail["error_code"])
        except (ValueError, OSError):
            msg = None
        if exc.code == 404:
            if path in ("/admin/owner-account",
                        "/admin/owner-account/reissue"):
                raise ServerNotReady(
                    "\u670d\u52a1\u5668\u5c1a\u672a\u542f\u7528\u7ba1\u7406\u5458\u8d26\u53f7\u7ed1\u5b9a") from exc
            if (path == "/account/referrals/redeem" and method == "POST"
                    and msg == "unknown referral code"):
                raise AccountError("邀请码无效，请检查后重试") from exc
            if path.startswith("/account/referrals/"):
                raise ServerNotReady("当前服务器尚未启用邀请码功能") from exc
            if path.startswith("/account/quests"):
                # Old servers may send a JSON {"error": "not found"} body.
                # Keep the explicit compatibility signal so the client hides
                # its optional task panel instead of showing a broken one.
                raise ServerNotReady("每日任务接口尚未部署") from exc
            if not msg:
                raise ServerNotReady("服务器尚未部署 0.23 账号与评论接口") from exc
        if exc.code == 401:
            if path in (
                    "/account/admin-login", "/admin/owner-account",
                    "/admin/owner-account/reissue",
                    "/account/developer/titles/grant-all",
                    "/account/developer/appearances/grant-all"):
                raise AccountError(
                    "\u7ba1\u7406\u5458\u5bc6\u94a5\u65e0\u6548\u6216\u5df2\u5931\u6548") from exc
            if msg == "recovery required":
                raise AccountError("新设备登录需要恢复码；请检查两串密钥") from exc
            if path == "/account/login":
                raise AccountError("登录密钥或恢复码不正确") from exc
            raise SessionExpired("设备会话已过期或被撤销，请重新输入登录密钥") from exc
        if (exc.code == 409 and method == "POST"
                and path == "/admin/owner-account"):
            raise OwnerAccountAlreadyBound(
                "这台客户端已经绑定过专属账号；如需更换，请明确选择重新签发",
                account_id=detail.get("account_id")) from exc
        if exc.code == 404:
            raise AccountError("云端记录不存在或已删除") from exc
        raise AccountError(str(msg or "服务器拒绝请求（HTTP %d）" % exc.code)) from exc
    except _NETWORK_ERRORS as exc:
        if route_selected and _ROUTE_OPENER is _opener:
            _ROUTE_OPENER, _ROUTE_UNTIL = None, 0.0
        raise AccountError(_network_error_message(path, method, exc)) from exc
    except (ValueError, UnicodeError) as exc:
        raise AccountError("服务器返回了无法读取的数据") from exc


def create():
    opener = _create_request_opener()
    result = request("/account/create", method="POST", payload={},
                     _opener=opener, _allow_proxy_fallback=False)
    required = ("account_id", "login_key", "recovery_code", "device_token")
    if any(not result.get(name) for name in required):
        raise AccountError("服务器创建账号成功但未返回完整密钥，请联系管理员")
    try:
        save_session(result["account_id"], result["device_token"])
    except AccountError as exc:
        # The account already exists: always give the owner both credentials.
        result["session_error"] = str(exc)
    if not result.get("session_error"):
        _attach_legacy_migration(result)
    return result


def login(login_key, recovery_code=None, account_id=None):
    try:
        old = session()
    except AccountError:
        if not recovery_code:
            raise AccountError("本机设备凭证损坏，请使用登录密钥和恢复码重新登录")
        old = None
    payload = {"login_key": login_key.strip()}
    known_id = account_id or ((old or {}).get("account_id") if not recovery_code else None)
    if known_id:
        payload["account_id"] = known_id
    if recovery_code:
        payload["recovery_code"] = recovery_code.strip()
    elif old:
        payload["device_token"] = old["device_token"]
    result = request("/account/login", method="POST", payload=payload,
                     _select_route=True)
    save_session(result.get("account_id"), result.get("device_token"))
    _attach_legacy_migration(result)
    return result


def developer_login(developer_key, allow_inactive=False):
    """Use the owner key as the account identity; no ordinary recovery code."""
    if not _DEVELOPER_MODE and not allow_inactive:
        raise AccountError("开发者身份未在本机启用")
    if not isinstance(developer_key, str) or not developer_key.strip():
        raise AccountError("未找到本机开发者密钥")
    try:
        old = (session_for_mode(True) if allow_inactive else session())
    except AccountError:
        old = None
    payload = {"device_label": "SLGking 管理员设备"}
    if old:
        payload["device_token"] = old["device_token"]
    result = request(
        "/account/admin-login", method="POST", payload=payload,
        extra_headers={"X-SLG-Developer-Key": developer_key.strip()},
        _select_route=True)
    if (not isinstance(result, dict)
            or result.get("account_id") != "developer"
            or not result.get("device_token")):
        raise AccountError("服务器未返回开发者云端身份")
    save_session(result["account_id"], result["device_token"])
    _attach_legacy_migration(result)
    return result


def _owner_account_response(result):
    required = ("account_id", "login_key", "recovery_code", "device_id",
                "device_token")
    if (not isinstance(result, dict)
            or any(not result.get(name) for name in required)):
        raise AccountError("服务器未返回完整的绑定账号凭据")
    if str(result.get("account_id")) == "developer":
        raise AccountError("绑定账号不能与固定管理员身份共用账号 ID")
    try:
        save_session(result["account_id"], result["device_token"])
    except AccountError as exc:
        # Secrets remain available to the caller for one-time display even if
        # the local device token could not be saved.
        result["session_error"] = str(exc)
    return result


def create_owner_account(developer_key, operation_id=None, device_label=None):
    """Create and bind the one ordinary account associated with this owner.

    The login and recovery secrets are returned only to the caller; this
    function never writes them to local storage. If the response is uncertain,
    retain the operation_id and verify the binding before resubmitting. The
    server stores credential hashes only; after a successful commit, another
    create request returns OwnerAccountAlreadyBound and cannot replay the
    one-time secrets. Reissue is separate and revokes existing sessions.
    """
    if not isinstance(developer_key, str) or not developer_key.strip():
        raise AccountError("未找到本机开发者密钥")
    operation_id = str(operation_id or uuid.uuid4().hex).strip().lower()
    if (len(operation_id) != 32
            or any(ch not in "0123456789abcdef" for ch in operation_id)):
        raise AccountError("账号申请编号格式不正确")
    payload = {"operation_id": operation_id}
    if device_label:
        payload["device_label"] = str(device_label)[:120]
    result = request(
        "/admin/owner-account", method="POST", payload=payload,
        extra_headers={"X-SLG-Developer-Key": developer_key.strip()},
        _select_route=True)
    return _owner_account_response(result)


def reissue_owner_account(developer_key):
    """Explicitly reissue bound-account credentials and revoke old sessions."""
    if not isinstance(developer_key, str) or not developer_key.strip():
        raise AccountError("未找到本机开发者密钥")
    result = request(
        "/admin/owner-account/reissue", method="POST", payload={},
        extra_headers={"X-SLG-Developer-Key": developer_key.strip()},
        _select_route=True)
    return _owner_account_response(result)


def developer_grant_all_appearances(developer_key):
    """Grant all collectible appearances to the fixed owner account."""
    if not _DEVELOPER_MODE:
        raise AccountError("仅开发者身份可以解锁全部装扮")
    if not isinstance(developer_key, str) or not developer_key.strip():
        raise AccountError("未找到本机开发者密钥")
    result = request(
        "/account/developer/appearances/grant-all", method="POST", payload={},
        extra_headers={"X-SLG-Developer-Key": developer_key.strip()})
    if (not isinstance(result, dict)
            or result.get("account_id") != "developer"
            or not isinstance(result.get("titles"), list)):
        raise AccountError("服务器未返回完整的开发者装扮状态")
    if not isinstance(result.get("items"), list):
        # Older server responses exposed the full owned-ID list as `titles`.
        result["items"] = result["titles"]
    return result


def developer_grant_all_titles(developer_key):
    """Backward-compatible client alias for all appearance unlocks."""
    return developer_grant_all_appearances(developer_key)


_LEGACY_SOURCE_PREF = "cloud.legacy_migration_source"
_LEGACY_DONE_PREF = "cloud.legacy_migration_done."
_LEGACY_RESULT_PREF = "cloud.legacy_migration_result."


def migrate_legacy():
    """Move this installation's pre-cloud balance and titles once per account."""
    current = session()
    if not current:
        raise AccountError("请先创建或登录云端账号")
    aid = str(current["account_id"])
    if aid == "developer":
        return {"ok": True, "status": "skipped_admin_identity",
                "points_added": 0, "titles_added": 0}
    done_key = _LEGACY_DONE_PREF + aid
    with slg_db.session() as conn:
        if slg_db.get_pref(conn, done_key) == "1":
            try:
                saved = json.loads(slg_db.get_pref(conn, _LEGACY_RESULT_PREF + aid, "{}"))
                if isinstance(saved, dict):
                    return saved
            except (TypeError, ValueError):
                pass
            return {"ok": True, "status": "already_completed_local"}
        if slg_db.get_pref(conn, "cloud.legacy_migration_local_source"):
            return {"ok": True, "status": "already_completed_local"}
        if slg_db.get_pref(conn, slg_db.LEGACY_IMPORTED_ASSETS_PREF) == "1":
            raise AccountError(
                "本机旧积分或头衔来自导入备份，已保留在本地，暂不自动迁移；"
                "请联系管理员核对旧资产后办理迁移")
        if slg_db.tamper_report(conn) is not None:
            raise AccountError("本机存档完整性校验未通过，旧资产暂不自动迁移；请联系管理员核对")
        points = max(0, int(slg_db.points_balance(conn)))
        titles = sorted(slg_db.owned_title_ids(conn))
        if points == 0 and not titles:
            result = {"ok": True, "status": "nothing_to_migrate",
                      "points_added": 0, "titles_added": 0}
            slg_db.set_pref(conn, done_key, "1")
            slg_db.set_pref(conn, _LEGACY_RESULT_PREF + aid,
                            json.dumps(result, ensure_ascii=False))
            return result
        source_id = slg_db.get_pref(conn, _LEGACY_SOURCE_PREF)
        if not source_id:
            source_id = uuid.uuid4().hex
            slg_db.set_pref(conn, _LEGACY_SOURCE_PREF, source_id)
        if slg_db.get_pref(conn, "cloud.legacy_migration_conflict_source") == source_id:
            return {"ok": False, "status": "source_conflict"}
    try:
        result = authenticated("/account/legacy-migrate", method="POST", payload={
            "source_id": source_id, "points": points, "titles": titles})
    except AccountError as exc:
        reason = str(exc).lower()
        if "legacy source already used" in reason or "account already migrated from another local source" in reason:
            with slg_db.session() as conn:
                slg_db.set_pref(conn, "cloud.legacy_migration_conflict_source", source_id)
            return {"ok": False, "status": "source_conflict"}
        raise
    status = result.get("status")
    if status in ("migrated", "already_migrated", "manual_approved"):
        # Use the server's original snapshot for retries; the request's local
        # values may already have been debited by a prior successful response.
        credited_points = result.get("points")
        credited_titles = result.get("titles")
        if not isinstance(credited_points, int) or not isinstance(credited_titles, list):
            raise AccountError("服务器返回的迁移结果不完整；请重新登录重试")
        if status == "manual_approved":
            result["points_added"] = credited_points
            result["titles_added"] = len(credited_titles)
        try:
            with slg_db.session() as conn:
                slg_db.apply_cloud_legacy_migration(
                    conn, source_id, credited_points, credited_titles)
                result["local_assets_moved"] = True
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise AccountError("云端已记录迁移，但本机资产尚未扣除；请保持原存档并重新登录重试（%s）" % exc) from exc
    if result.get("status") in {
            "migrated", "already_migrated", "manual_approved",
            "nothing_to_migrate", "manual_rejected"}:
        with slg_db.session() as conn:
            slg_db.set_pref(conn, done_key, "1")
            slg_db.set_pref(conn, _LEGACY_RESULT_PREF + aid,
                            json.dumps(result, ensure_ascii=False))
    return result


def _attach_legacy_migration(result):
    """Keep account creation/login successful if migration needs a retry."""
    if isinstance(result, dict) and result.get("account_id") == "developer":
        return
    try:
        # Account actions run with the session captured when their worker was
        # queued. After login/create saves a different account, that snapshot
        # still points at the previous account (or no account at all). Pin the
        # migration to the credentials returned by this successful action.
        new_session = {"account_id": result["account_id"],
                       "device_token": result["device_token"]}
        with use_session_snapshot(new_session):
            result["legacy_migration"] = migrate_legacy()
    except AccountError as exc:
        result["legacy_migration_error"] = str(exc)


def authenticated(path, method="GET", payload=None):
    current = session()
    if not current:
        raise AccountError("请先在个人中心创建或登录云端账号")
    return request(path, method=method, payload=payload,
                   token=current["device_token"], _select_route=True)


def me():
    migration = None
    migration_error = None
    try:
        migration = migrate_legacy()
    except AccountError as exc:
        migration_error = str(exc)
    result = authenticated("/account/me")
    if migration is not None:
        result["legacy_migration"] = migration
    if migration_error:
        result["legacy_migration_error"] = migration_error
    return result


def referrals_me():
    """Fetch the authenticated account's referral code and award progress."""
    result = authenticated("/account/referrals/me")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise AccountError("服务器返回的邀请码资料格式不正确")
    return result


def redeem_referral(code):
    """Bind this new account to one inviter. The server makes this idempotent."""
    code = str(code or "").strip()
    if not code:
        raise AccountError("请输入邀请码")
    # This POST is deliberately sent once. A timeout remains an unknown result;
    # the caller refreshes referrals_me() before allowing another submission.
    result = authenticated(
        "/account/referrals/redeem", method="POST", payload={"code": code})
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise AccountError("服务器未确认邀请码绑定结果")
    return result


def devices():
    return authenticated("/account/devices")


def revoke_device(device_id):
    result = authenticated("/account/devices/" + urllib.parse.quote(str(device_id), safe=""),
                           method="DELETE")
    return result


def rotate(login_key, recovery_code=None):
    payload = {"login_key": login_key}
    if recovery_code:
        payload["recovery_code"] = recovery_code
    result = authenticated("/account/rotate", method="POST", payload=payload)
    try:
        save_session(result.get("account_id"), result.get("device_token"))
    except AccountError as exc:
        result["session_error"] = str(exc)
    return result


def request_legacy(points, titles):
    clean = {"points": int(points), "titles": sorted(str(item) for item in titles)}
    digest = hashlib.sha256(json.dumps(clean, ensure_ascii=False,
                                      sort_keys=True).encode("utf-8")).hexdigest()
    return authenticated("/account/legacy-request", method="POST", payload={
        **clean, "source_digest": digest})


def redeem_group(code):
    return authenticated("/rewards/group", method="POST", payload={"code": code.strip()})


def signin():
    return authenticated("/account/signin", method="POST", payload={})


def buy(item_id):
    return authenticated("/account/shop/buy", method="POST", payload={"item_id": item_id})


def lottery_draw(request_id=None):
    """Commit one draw, reusing request_id when retrying an uncertain request."""
    request_id = str(request_id or uuid.uuid4().hex).strip().lower()
    if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
        raise AccountError("invalid lottery request")
    return authenticated("/account/lottery/draw", method="POST",
                         payload={"request_id": request_id})


def equip_title(title_id):
    return authenticated("/account/equip", method="POST", payload={"title_id": title_id})


def equip_cosmetic(cosmetic_id):
    return authenticated("/account/equip", method="POST",
                         payload={"cosmetic_id": cosmetic_id})


def equip_appearance(slot, item_id):
    """Equip or clear one cloud appearance slot (empty item_id clears it)."""
    if slot not in ("avatar_frame", "comment_frame"):
        raise AccountError("装扮部位无效")
    if not isinstance(item_id, str) or len(item_id) > 80:
        raise AccountError("装扮编号无效")
    return authenticated("/account/equip", method="POST", payload={
        "appearance_slot": slot, "item_id": item_id})


def leaderboard(board="points", limit=30):
    """Read a public, bounded leaderboard without requiring an account session."""
    if board not in ("points", "titles", "wardrobe"):
        raise AccountError("排行榜类型无效")
    if isinstance(limit, bool):
        raise AccountError("排行榜数量无效")
    if isinstance(limit, int):
        limit_text = str(limit)
    elif isinstance(limit, str) and limit and all(ch in "0123456789" for ch in limit):
        limit_text = limit
    else:
        raise AccountError("排行榜数量无效")
    limit_value = int(limit_text)
    if limit_value < 1 or limit_value > 30:
        raise AccountError("排行榜数量必须在 1 到 30 之间")
    query = urllib.parse.urlencode({"board": board, "limit": limit_value})
    return request("/community/leaderboard?" + query)


def update_profile(nickname=None, profile_message=None):
    payload = {}
    if nickname is not None:
        if not isinstance(nickname, str):
            raise AccountError("nickname must be text")
        payload["nickname"] = nickname.strip()
    if profile_message is not None:
        if not isinstance(profile_message, str):
            raise AccountError("profile message must be text")
        payload["profile_message"] = profile_message
    if not payload:
        raise AccountError("no profile fields to update")
    return authenticated("/account/profile", method="POST", payload=payload)


def update_profile_message(profile_message):
    """Set or clear the server-stored public namecard message."""
    return update_profile(profile_message=profile_message)


def submit_feedback(category, content):
    """Submit a private, authenticated note for the developer to review."""
    if category not in ("bug", "suggestion", "data_correction", "other"):
        raise AccountError("反馈类型无效")
    if not isinstance(content, str):
        raise AccountError("反馈内容必须是文字")
    content = content.strip()
    if not 20 <= len(content) <= 500:
        raise AccountError("反馈内容需要填写 20–500 个字")
    return authenticated("/account/feedback", method="POST", payload={
        "category": category, "content": content})


def maintenance_reward_status():
    """Read the one-time maintenance campaign status for the signed-in account."""
    return authenticated("/account/rewards/maintenance")


def claim_maintenance_reward(campaign_id):
    """Claim an active maintenance campaign; the server enforces idempotency."""
    return authenticated("/account/rewards/maintenance/claim", method="POST",
                         payload={"campaign_id": str(campaign_id)})


def quests():
    """Read the signed-in account's daily and weekly quest progress."""
    return authenticated("/account/quests")


def get_quests():
    """Explicitly named alias for callers that prefer a getter."""
    return quests()


def claim_quest(quest_id):
    """Ask the server to claim one completed quest."""
    if not isinstance(quest_id, str) or not quest_id.strip() or len(quest_id) > 100:
        raise AccountError("任务编号无效")
    return authenticated("/account/quests/claim", method="POST",
                         payload={"quest_id": quest_id.strip()})


def report_rating_event(game_id, rating):
    """Report a locally saved rating so the server can credit its quest."""
    try:
        game_id = int(game_id)
        rating = int(rating)
    except (TypeError, ValueError):
        raise AccountError("评分记录无效")
    if game_id <= 0 or rating < 1 or rating > 5:
        raise AccountError("评分记录无效")
    return authenticated("/account/quests/rating", method="POST",
                         payload={"game_id": game_id, "rating": rating})
