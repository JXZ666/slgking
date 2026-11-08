"""0.28 reply and notification transport. No avatar or reward payloads."""
import json
import urllib.parse
import uuid
import slg_account


def _id(value):
    value = str(value or '')
    if not value or len(value) > 160 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        raise ValueError('无效的评论编号')
    return value


def _page(value):
    return max(1, int(value))


def _result(value):
    if not isinstance(value, dict) or value.get('ok') is not True:
        raise slg_account.AccountError('资料服务未返回有效结果，请稍后重试')
    return value


def fetch_replies(root_id, page=1, focus_id=None):
    args = {'page': _page(page), 'limit': 20}
    if focus_id is not None: args['focus_id'] = _id(focus_id)
    return _result(slg_account.request('/comments/' + _id(root_id) + '/replies?' + urllib.parse.urlencode(args)))


def _require_ordinary_writer():
    current = slg_account.session()
    if isinstance(current, dict) and current.get('account_id') == 'developer':
        raise slg_account.AccountError('开发者模式仅支持阅读讨论，请切换普通账号后操作')


def submit_reply(root_id, content, reply_to_id=None, operation_id=None):
    _require_ordinary_writer()
    content = str(content or '').strip()
    if not 2 <= len(content) <= 500:
        raise ValueError('追评需要 2 至 500 个字符')
    operation_id = str(operation_id or uuid.uuid4())
    uuid.UUID(operation_id)
    payload = dict(content=content, reply_to_id=_id(reply_to_id) if reply_to_id is not None else None, operation_id=operation_id)
    if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > 2048:
        raise ValueError('追评请求超过 2048 字节，请缩短正文')
    return _result(slg_account.authenticated('/comments/' + _id(root_id) + '/replies', method='POST', payload=payload))


def fetch_my_replies(root_id, page=1):
    return _result(slg_account.authenticated('/account/comment-replies?' + urllib.parse.urlencode({'root_id': _id(root_id), 'page': _page(page), 'limit': 20})))


def delete_reply(reply_id):
    return _result(slg_account.authenticated('/comment-replies/' + _id(reply_id), method='DELETE'))


def report_reply(reply_id):
    _require_ordinary_writer()
    return _result(slg_account.authenticated('/comment-replies/' + _id(reply_id) + '/report', method='POST', payload={}))


def fetch_context(reply_id):
    return _result(slg_account.request('/comment-replies/' + _id(reply_id) + '/context'))


def fetch_notifications(page=1):
    return _result(slg_account.authenticated('/account/notifications?' + urllib.parse.urlencode({'page': _page(page), 'limit': 20})))


def mark_notifications_read(ids=None, all=False):
    payload = {'all': True} if all else {'ids': [_id(value) for value in (ids or [])]}
    if not all and not 1 <= len(payload['ids']) <= 100:
        raise ValueError('请选择 1 至 100 条消息')
    return _result(slg_account.authenticated('/account/notifications/read', method='POST', payload=payload))


def safe_reply(row):
    """Public presentation never trusts cached decorations or local image paths."""
    result = {key: row.get(key) for key in ('id', 'root_id', 'game', 'content', 'created', 'status', 'nickname', 'author_public_id', 'reply_to_id', 'reply_to_nickname', 'own')}
    visible = row.get('author_profile_visible') is True
    result['author_profile_visible'] = visible
    if visible:
        result.update({key: row.get(key) for key in ('equipped_title', 'equipped_appearances', 'profile_message')})
    return result
