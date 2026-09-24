"""Cloud comments, self-hosted on the author's server.

Comments are the one feature that needs shared storage. They live as an
append-only JSONL on slg-king.com (see serve_catalog.py's /comments routes),
keyed by the same anonymous device id slg_remote uses for telemetry. There is
no account system, so "your own comment" is a device-id match - honour-system
strength, enough to stop casual misuse but not a determined editor.

Every call is best-effort: an offline comment reads as [] / None and never
raises into the GUI.
"""

import json
import urllib.parse
import urllib.request

import slg_scrape
from slg_sync_server import SERVER_BASE

_COMMENTS = SERVER_BASE + "/comments"
_TIMEOUT = 8
MAX_COMMENT_CONTENT_CHARS = 2000
MAX_COMMENT_REQUEST_BYTES = 4096

# Same no-proxy opener as slg_scrape._DIRECT_OPENER, for the one DELETE route
# http_get/http_post don't cover.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def configured():
    """The self-hosted backend is always on - there is no 'not configured' state."""
    return True


def _delete(url, headers=None):
    req = urllib.request.Request(url, method="DELETE", headers=headers or {})
    with _DIRECT_OPENER.open(req, timeout=_TIMEOUT) as resp:
        resp.read()
    return True


def validate_public_comment(game_slug, content, nickname=None, device=None):
    """Return a user-facing validation error, or None when the server accepts it.

    Keep both server limits here: it silently stores only 2000 content
    characters, and rejects JSON request bodies larger than 4096 UTF-8 bytes.
    Measuring the serialized body matters for CJK text, where a modest-looking
    textbox can exceed the byte limit well before it reaches 2000 characters.
    """
    if len(content) > MAX_COMMENT_CONTENT_CHARS:
        return "公开评论最多 2000 个字符，请缩短后再发布。"
    payload = {"game": game_slug, "content": content, "device": device or ""}
    if nickname:
        payload["nickname"] = nickname
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_COMMENT_REQUEST_BYTES:
        return "公开评论请求超过服务器大小限制，请缩短正文或昵称。"
    return None


def fetch_comments(game_slug, limit=30):
    """Recent public comments, or None if the cloud could not be reached."""
    url = _COMMENTS + "?game=%s&limit=%d" % (urllib.parse.quote(game_slug), limit)
    try:
        raw = slg_scrape.http_get(url, timeout=_TIMEOUT, direct=True)
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict):
            return data.get("comments") or []
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 - offline fetch must not raise
        return None


def upload_comment(game_slug, content, nickname=None, device=None):
    """Post a public comment; returns its id, or None on failure."""
    if validate_public_comment(game_slug, content, nickname, device):
        return None
    payload = {"game": game_slug, "content": content, "device": device or ""}
    if nickname:
        payload["nickname"] = nickname
    try:
        raw = slg_scrape.http_post(_COMMENTS, payload, timeout=_TIMEOUT, direct=True)
        data = json.loads(raw.decode("utf-8", "replace"))
        return data.get("id") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - offline upload must not raise
        return None


def delete_comment(object_id, device=None):
    """Best-effort removal of a comment the caller owns. Returns True on success."""
    if not object_id:
        return False
    url = _COMMENTS + "/" + urllib.parse.quote(str(object_id))
    try:
        headers = {"X-SLG-Device": str(device)} if device else {}
        return _delete(url, headers=headers)
    except Exception:  # noqa: BLE001 - offline delete must not raise
        return False


def report_comment(object_id, device=None):
    """Flag a comment for the admin. Returns True on success."""
    if not object_id:
        return False
    url = _COMMENTS + "/" + urllib.parse.quote(str(object_id)) + "/report"
    try:
        slg_scrape.http_post(
            url, {"device": device or ""}, timeout=_TIMEOUT, direct=True)
        return True
    except Exception:  # noqa: BLE001 - offline report must not raise
        return False


def my_public_count(device):
    """How many public comments this device has posted. None when unreachable."""
    if not device:
        return None
    url = _COMMENTS + "/count"
    try:
        raw = slg_scrape.http_get(
            url, timeout=_TIMEOUT, direct=True,
            headers={"X-SLG-Device": str(device)})
        data = json.loads(raw.decode("utf-8", "replace"))
        return data.get("count") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - the achievement just won't fire
        return None
