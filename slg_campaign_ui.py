"""Cloud campaign UI; codes are validated by the server, never bundled."""
import re
import uuid
import slg_account

TITLE_ID = "echoes_of_abyss"
CAMPAIGN_ID = "echoes-launch"


def normalize_code(code):
    return str(code or "").strip(" \t\r\n\v\f").translate(str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def handles(code):
    return normalize_code(code).startswith("ECHOES-")


def valid_format(code):
    return re.fullmatch(r"ECHOES-(?:[0-9A-F]{8}-){3}[0-9A-F]{8}", normalize_code(code)) is not None


def _feedback(app, text, error=False):
    import slg_gui as g
    label = getattr(app, "_redeem_feedback", None)
    if label is not None and label.winfo_exists():
        label.configure(text=text, text_color=g.DANGER_TEXT if error else g.ACCENT)


def submit(app, code):
    code = normalize_code(code)
    if not valid_format(code):
        _feedback(app, "联动兑换码格式不完整，请保留全部字符和连字符。", True)
        return False
    if getattr(app, "_campaign_pending", None):
        _feedback(app, "正在领取联动奖励，请稍候。")
        return False
    if not app._has_usable_cloud_session():
        _feedback(app, "请先在个人中心创建或登录云端账号。", True)
        return False
    token = "campaign:" + uuid.uuid4().hex
    wallet = app._begin_shop_write("campaign")
    if wallet is None:
        _feedback(app, "另一项积分操作正在处理中，请稍后再领取。", True)
        return False
    app._campaign_pending = (token, app._capture_identity_context(), wallet)
    button = getattr(app, "_redeem_submit", None)
    if button is not None and button.winfo_exists():
        button.configure(state="disabled")
    _feedback(app, "正在云端领取联动头衔及积分…")
    if app._run_cloud_action(token, lambda value=code: slg_account.redeem_campaign(value)) is False:
        complete(app, token, None, "领取未开始，请重试。")
        return False
    return True


def complete(app, token, result, error):
    pending = getattr(app, "_campaign_pending", None)
    if not pending or pending[:2] != (token, app._capture_identity_context()):
        return
    app._campaign_pending = None
    app._finish_shop_write(pending[2])
    button = getattr(app, "_redeem_submit", None)
    if button is not None and button.winfo_exists():
        button.configure(state="normal")
    if not error:
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("title_id") != TITLE_ID or result.get("campaign_id") != CAMPAIGN_ID
                or not isinstance(result.get("already_claimed"), bool)
                or isinstance(result.get("balance"), bool) or not isinstance(result.get("balance"), int)
                or result.get("balance") < 0
                or isinstance(result.get("gained"), bool)
                or result.get("gained") != (0 if result.get("already_claimed") else 100)):
            error = "服务器返回的奖励状态不完整，请重试确认；同账号不会重复发奖。"
    if error:
        _feedback(app, str(error), True)
        app._set_progress("联动奖励领取失败：" + str(error))
        return
    app._shop_coord().invalidate_reads("me")
    app._store_cloud_balance(result["balance"])
    app._cloud_titles = set(getattr(app, "_cloud_titles", ()) or ()) | {TITLE_ID}
    if isinstance(getattr(app, "_cloud_me", None), dict):
        app._cloud_me.update(balance=result["balance"], titles=sorted(app._cloud_titles))
    message = ("此账号已领取过「深渊回响」和100积分，不会重复发放。" if result["already_claimed"]
               else "已获得史诗头衔「深渊回响」，积分 +100。")
    _feedback(app, message)
    app._set_progress(message)
    app._refresh_open_wardrobe()
    app._mark_shop("balance", "products")
    if getattr(app, "_panel_mode", None) == "profile":
        app.open_profile()
