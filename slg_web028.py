"""Cookie-only adapters for the existing shared discussion business."""

import re
from urllib.parse import parse_qs, urlparse

from slg_cloud_store import CloudError


def serve_web028(handler, method, path):
    root = re.fullmatch(r"/web/comments/([A-Za-z0-9_-]{1,128})/replies", path)
    item = re.fullmatch(r"/web/comment-replies/([A-Za-z0-9_-]{1,128})(?:/(report))?", path)
    private = path in ("/web/account/comment-replies", "/web/account/notifications",
                       "/web/account/notifications/read")
    if not (root or item or private):
        return False
    if not handler._is_secure_transport():
        raise CloudError("HTTPS required", 403)
    if method != "GET" and not handler._same_origin_request():
        raise CloudError("cross-origin request blocked", 403)
    aid = handler._web_session()["account_id"]
    store = handler._cloud()
    query = parse_qs(urlparse(handler.path).query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        raise CloudError("bad query")
    if method != "GET" and query:
        raise CloudError("bad query")

    def fields(allowed):
        if set(query) - set(allowed):
            raise CloudError("bad query")
        return {name: query[name][0] for name in allowed if name in query}

    if root and method == "GET":
        args = fields(("page", "limit", "focus_id"))
        handler._catalogue_request(lambda: handler._send_json(
            store.comment_replies(root[1], aid=aid, **args)))
    elif root and method == "POST":
        obj = handler._json_body(maximum=4096)
        if set(obj) - {"content", "reply_to_id", "operation_id"}:
            raise CloudError("bad body")
        from slg_moderation import classify_discussion
        handler._send_json(store.post_comment_reply(
            aid, root[1], obj.get("content"), classify_discussion,
            reply_to_id=obj.get("reply_to_id"), operation_id=obj.get("operation_id")))
    elif item and method == "DELETE" and item[2] is None:
        handler._send_json(store.delete_comment_reply(aid, item[1]))
    elif item and method == "POST" and item[2] == "report":
        if handler._json_body(maximum=256):
            raise CloudError("bad body")
        handler._send_json(store.report_comment_reply(aid, item[1]))
    elif path == "/web/account/comment-replies" and method == "GET":
        args = fields(("root_id", "page", "limit"))
        handler._catalogue_request(lambda: handler._send_json(
            store.account_comment_replies(aid, **args)))
    elif path == "/web/account/notifications" and method == "GET":
        args = fields(("page", "limit"))
        handler._catalogue_request(lambda: handler._send_json(
            store.account_notifications(aid, **args)))
    elif path == "/web/account/notifications/read" and method == "POST":
        obj = handler._json_body(maximum=16384)
        if set(obj) not in ({"ids"}, {"all"}) or ("all" in obj and obj["all"] is not True):
            raise CloudError("bad body")
        handler._send_json(store.read_account_notifications(aid, **obj))
    else:
        raise CloudError("method not allowed", 405)
    return True
