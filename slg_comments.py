"""Cloud comments backed by the 0.23 account and moderation service."""

import json
import urllib.parse

import slg_account
import slg_scrape
from slg_sync_server import SERVER_BASE

_COMMENTS = SERVER_BASE + "/comments"
_TIMEOUT = 8
MIN_COMMENT_CONTENT_CHARS = 20
MAX_COMMENT_CONTENT_CHARS = 500
MAX_COMMENT_REQUEST_BYTES = 2048


def validate_public_comment(game_slug, content, nickname=None, device=None):
    """Check the server's content and request limits before uploading."""
    content = (content or "").strip()
    if len(content) < MIN_COMMENT_CONTENT_CHARS:
        return "公开评论至少需要 20 个字符，请写些具体体验。"
    if len(content) > MAX_COMMENT_CONTENT_CHARS:
        return "公开评论最多 500 个字符，请缩短后发布。"
    payload = {"game": game_slug, "content": content}
    if nickname:
        payload["nickname"] = nickname
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_COMMENT_REQUEST_BYTES:
        return "公开评论超过 2048 字节，请缩短正文或昵称。"
    return None


def fetch_comments_page(game_slug, page=1, limit=20, sort="latest"):
    """Fetch one public comments page with server pagination metadata.

    Returns a dict containing ``comments``, ``page``, ``page_size``,
    ``total_count``, ``total_pages``, and ``sort``; returns ``None`` on a
    network or response error. The list is already limited by SQL on server.
    """
    try:
        page = max(1, int(page))
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError, OverflowError):
        page, limit = 1, 20
    sort = sort if sort in ("latest", "popular") else "latest"
    url = _COMMENTS + "?" + urllib.parse.urlencode({
        "game": game_slug, "page": page, "limit": limit, "sort": sort})
    try:
        raw = slg_scrape.http_get(url, timeout=_TIMEOUT, direct=True)
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict):
            comments = data.get("comments")
            if not isinstance(comments, list):
                return None
            # Older servers only returned {"comments": [...]}.
            total_count = data.get("total_count", len(comments))
            total_pages = data.get("total_pages", (total_count + limit - 1) // limit)
            return {
                "comments": comments,
                "page": data.get("page", page),
                "page_size": data.get("page_size", limit),
                "total_count": total_count,
                "total_pages": total_pages,
                "sort": data.get("sort", sort),
                "has_previous": data.get("has_previous", page > 1),
                "has_next": data.get("has_next", page < total_pages),
            }
        # A very old server returned the list itself. Keep this compatibility
        # as well, although the documented legacy shape is the comments key.
        if isinstance(data, list):
            return {"comments": data, "page": page, "page_size": limit,
                    "total_count": len(data),
                    "total_pages": 1 if data else 0, "sort": sort,
                    "has_previous": False, "has_next": False}
        return None
    except Exception:  # noqa: BLE001 - offline read must not crash the GUI
        return None


def fetch_comments(game_slug, limit=30, sort="latest"):
    """Legacy list-only API; compatible with existing GUI callers."""
    page = fetch_comments_page(game_slug, page=1, limit=limit, sort=sort)
    return page.get("comments", []) if page is not None else None


def fetch_my_comments(game_slug):
    data = slg_account.authenticated("/account/comments?" + urllib.parse.urlencode(
        {"game": game_slug}))
    return data.get("comments", [])


def submit_comment(game_slug, content, nickname=None):
    """Post a public review, preserving its moderation and reward result."""
    error = validate_public_comment(game_slug, content, nickname)
    if error:
        raise slg_account.AccountError(error)
    payload = {"game": game_slug, "content": content.strip()}
    if nickname:
        payload["nickname"] = nickname
    return slg_account.authenticated("/comments", method="POST", payload=payload)


def upload_comment(game_slug, content, nickname=None, device=None):
    """Legacy API: return the new comment id, or None when upload fails."""
    try:
        return submit_comment(game_slug, content, nickname).get("id")
    except slg_account.AccountError:
        return None


def delete_comment(object_id):
    if not object_id:
        return False
    try:
        slg_account.authenticated(
            "/comments/" + urllib.parse.quote(str(object_id), safe=""), method="DELETE")
        return True
    except slg_account.AccountError:
        return False


def report_comment(object_id):
    if not object_id:
        return False
    try:
        slg_account.authenticated(
            "/comments/" + urllib.parse.quote(str(object_id), safe="") + "/report",
            method="POST", payload={})
        return True
    except slg_account.AccountError:
        return False


def vote_comment(object_id, value):
    if value not in (-1, 0, 1):
        raise ValueError("value must be -1, 0 or 1")
    return slg_account.authenticated(
        "/comments/" + urllib.parse.quote(str(object_id), safe="") + "/vote",
        method="POST", payload={"value": value})


def my_public_count(device=None):
    """Account based count for the existing connoisseur title check."""
    try:
        rows = slg_account.authenticated("/account/comments").get("comments", [])
        return sum(1 for row in rows if row.get("status") in ("public", "approved"))
    except slg_account.AccountError:
        return None
