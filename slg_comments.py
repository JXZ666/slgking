"""User comments backed by LeanCloud.

The app is otherwise a pure-local desktop tool; comments are the one feature
that needs shared storage, and LeanCloud's free tier is the lightest way to get
it without standing up a server. AppId/Key are baked in here - they are a
master key, so this is only safe for low-stakes comment data; delete abuse from
the LeanCloud console.
"""

import json
import urllib.parse
import urllib.request

# TODO(user): fill these in from your LeanCloud console. Use a dedicated app
# created just for comments. While they are blank the feature degrades quietly
# to local-only - comments still save and show on this machine, just never sync.
LC_APP_ID = ""
LC_APP_KEY = ""
# The REST host from the console's 数据存储 -> 安全中心 -> API 地址, without the
# trailing "/1.1". International apps use *.api.lncldglobal.com, China apps
# (leancloud.cn) use *.api.lncld.net. "/1.1/classes" is appended below.
LC_HOST = ""

CLASS = "Comment"


def configured():
    return bool(LC_APP_ID and LC_APP_KEY and LC_HOST)


def _http_json(url, data=None, headers=None, timeout=8):
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def _headers():
    return {"X-LC-Id": LC_APP_ID, "X-LC-Key": LC_APP_KEY}


def _endpoint():
    return LC_HOST.rstrip("/") + "/1.1/classes/" + CLASS


def upload_comment(game_slug, content, nickname=None):
    """Create a comment object; returns its objectId, or None on failure."""
    if not configured():
        return None
    payload = {"gameSlug": game_slug, "content": content}
    if nickname:
        payload["nickname"] = nickname
    try:
        data = _http_json(_endpoint(), data=payload, headers=_headers())
        return data.get("objectId")
    except Exception:  # noqa: BLE001 - offline upload must not raise
        return None


def fetch_comments(game_slug, limit=30):
    """Recent comments for a game, newest first, as a list of dicts."""
    if not configured():
        return []
    where = urllib.parse.quote(json.dumps({"gameSlug": game_slug}))
    url = _endpoint() + "?where=%s&order=-createdAt&limit=%d" % (where, limit)
    try:
        data = _http_json(url, headers=_headers())
        return data.get("results") or []
    except Exception:  # noqa: BLE001 - offline fetch must not raise
        return []


def delete_comment(object_id):
    """Best-effort removal of an uploaded comment. Returns True on success."""
    if not configured() or not object_id:
        return False
    url = _endpoint() + "/" + urllib.parse.quote(str(object_id))
    try:
        req = urllib.request.Request(url, headers=_headers(), method="DELETE")
        with urllib.request.urlopen(req, timeout=8):
            return True
    except Exception:  # noqa: BLE001 - offline delete must not raise
        return False
