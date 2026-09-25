"""Small client for the cloud account service.

The device session is the only credential kept on this computer. Windows
DPAPI encrypts it for the current Windows user; login and recovery secrets are
shown once and must be kept by the account owner.
"""

import base64
import binascii
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import sqlite3
import uuid
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import slg_db
from slg_sync_server import SERVER_BASE


class AccountError(Exception):
    """An account operation that can be reported directly to the user."""


class ServerNotReady(AccountError):
    """The installed server has not received the 0.23 account endpoints."""


class SessionExpired(AccountError):
    """The device session expired or was revoked."""


_BASE = SERVER_BASE.rstrip("/")
_TIMEOUT = 10
_USER_AGENT = "SLGKing/0.23.3 (+https://slg-king.com; desktop client)"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_DEVELOPER_MODE = False


def set_developer_mode(enabled):
    """Select the owner identity for this running client process."""
    global _DEVELOPER_MODE
    _DEVELOPER_MODE = bool(enabled)


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
    return os.path.join(slg_db.app_dir(), "cloud_session.json")


def save_session(account_id, device_token):
    if not account_id or not device_token:
        raise AccountError("服务器未返回完整的账号会话")
    encrypted = base64.b64encode(_dpapi(device_token.encode("utf-8"), True)).decode("ascii")
    parent = os.path.dirname(_session_path())
    try:
        fd, tmp = tempfile.mkstemp(prefix="cloud_session-", suffix=".tmp", dir=parent)
    except OSError as exc:
        raise AccountError("本机设备会话保存失败，请妥善保存登录密钥和恢复码") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"account_id": str(account_id), "token": encrypted}, out)
        os.replace(tmp, _session_path())
    except OSError as exc:
        raise AccountError("本机设备会话保存失败，请妥善保存登录密钥和恢复码") from exc
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def session():
    try:
        with open(_session_path(), encoding="utf-8") as inp:
            data = json.load(inp)
        account_id = str(data["account_id"])
        if _DEVELOPER_MODE and account_id != "developer":
            # An enabled developer identity supersedes the ordinary account on
            # this installation; never let a stale user session silently act
            # as the developer while the owner-key session is being connected.
            return None
        return {"account_id": data["account_id"],
                "device_token": _dpapi(base64.b64decode(data["token"]), False).decode("utf-8")}
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, OSError, UnicodeError, binascii.Error) as exc:
        raise AccountError("本机云端设备凭证无法读取，请重新登录") from exc


def forget_session():
    try:
        os.unlink(_session_path())
    except FileNotFoundError:
        pass


def request(path, method="GET", payload=None, token=None, extra_headers=None):
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
        with _OPENER.open(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = (detail.get("error") or detail.get("message")
                   or detail.get("detail") or detail.get("title"))
            if detail.get("cloudflare_error") and detail.get("error_code"):
                msg = "%s (Cloudflare %s)" % (msg or "请求被边缘防护拦截",
                                                detail["error_code"])
        except (ValueError, OSError):
            msg = None
        if exc.code == 404 and not msg:
            raise ServerNotReady("服务器尚未部署 0.23 账号与评论接口") from exc
        if exc.code == 401:
            if msg == "recovery required":
                raise AccountError("新设备登录需要恢复码；请检查两串密钥") from exc
            if path == "/account/login":
                raise AccountError("登录密钥或恢复码不正确") from exc
            raise SessionExpired("设备会话已过期或被撤销，请重新输入登录密钥") from exc
        if exc.code == 404:
            raise AccountError("云端记录不存在或已删除") from exc
        raise AccountError(str(msg or "服务器拒绝请求（HTTP %d）" % exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AccountError("无法连接云端服务器，请检查网络后重试") from exc
    except (ValueError, UnicodeError) as exc:
        raise AccountError("服务器返回了无法读取的数据") from exc


def create():
    result = request("/account/create", method="POST", payload={})
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
    result = request("/account/login", method="POST", payload=payload)
    save_session(result.get("account_id"), result.get("device_token"))
    _attach_legacy_migration(result)
    return result


def developer_login(developer_key):
    """Use the owner key as the account identity; no ordinary recovery code."""
    if not _DEVELOPER_MODE:
        raise AccountError("开发者身份未在本机启用")
    if not isinstance(developer_key, str) or not developer_key.strip():
        raise AccountError("未找到本机开发者密钥")
    try:
        old = session()
    except AccountError:
        old = None
    payload = {"device_label": "SLGking 管理员设备"}
    if old:
        payload["device_token"] = old["device_token"]
    result = request(
        "/account/admin-login", method="POST", payload=payload,
        extra_headers={"X-SLG-Developer-Key": developer_key.strip()})
    if (not isinstance(result, dict)
            or result.get("account_id") != "developer"
            or not result.get("device_token")):
        raise AccountError("服务器未返回开发者云端身份")
    save_session(result["account_id"], result["device_token"])
    _attach_legacy_migration(result)
    return result


def developer_grant_all_titles(developer_key):
    """Grant all titles to the fixed owner account using its owner key."""
    if not _DEVELOPER_MODE:
        raise AccountError("仅开发者身份可以解锁全部头衔")
    if not isinstance(developer_key, str) or not developer_key.strip():
        raise AccountError("未找到本机开发者密钥")
    result = request(
        "/account/developer/titles/grant-all", method="POST", payload={},
        extra_headers={"X-SLG-Developer-Key": developer_key.strip()})
    if (not isinstance(result, dict)
            or result.get("account_id") != "developer"
            or not isinstance(result.get("titles"), list)):
        raise AccountError("服务器未返回完整的开发者头衔状态")
    return result


_LEGACY_SOURCE_PREF = "cloud.legacy_migration_source"
_LEGACY_DONE_PREF = "cloud.legacy_migration_done."
_LEGACY_RESULT_PREF = "cloud.legacy_migration_result."


def migrate_legacy():
    """Move this installation's pre-cloud balance and titles once per account."""
    current = session()
    if not current:
        raise AccountError("请先创建或登录云端账号")
    aid = str(current["account_id"])
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
    try:
        result["legacy_migration"] = migrate_legacy()
    except AccountError as exc:
        result["legacy_migration_error"] = str(exc)


def authenticated(path, method="GET", payload=None):
    current = session()
    if not current:
        raise AccountError("请先在个人中心创建或登录云端账号")
    return request(path, method=method, payload=payload,
                   token=current["device_token"])


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


def lottery_draw():
    return authenticated("/account/lottery/draw", method="POST", payload={})


def equip_title(title_id):
    return authenticated("/account/equip", method="POST", payload={"title_id": title_id})


def equip_cosmetic(cosmetic_id):
    return authenticated("/account/equip", method="POST",
                         payload={"cosmetic_id": cosmetic_id})


def update_profile(nickname):
    return authenticated("/account/profile", method="POST", payload={
        "nickname": nickname.strip()})


def maintenance_reward_status():
    """Read the one-time maintenance campaign status for the signed-in account."""
    return authenticated("/account/rewards/maintenance")


def claim_maintenance_reward(campaign_id):
    """Claim an active maintenance campaign; the server enforces idempotency."""
    return authenticated("/account/rewards/maintenance/claim", method="POST",
                         payload={"campaign_id": str(campaign_id)})
