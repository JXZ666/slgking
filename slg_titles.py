"""头衔、积分商城与兑换码的目录和业务规则。

纯本地批次的「归属感」数据源：头衔稀有度分级（对用户隐藏，只通过颜色/特效体现）、
积分经济数值、商城货架、以及靠兑换码解锁的特殊头衔。这里只放目录和高层业务，
持久化原语在 `slg_db`。
"""

import base64
import hashlib
import hmac
from datetime import date, timedelta

import slg_db

# 稀有度分五档，规则不对外公开 —— 用户只看到颜色/特效，看不到「稀有/史诗」字样。
RARITY_ORDER = ("普通", "稀有", "史诗", "传说", "至臻")

RARITY_COLORS = {
    "普通": "#9a9aa2",   # 灰
    "稀有": "#2f6fd0",   # 蓝
    "史诗": "#8b5cf6",   # 紫
    "传说": "#e0a800",   # 金
    "至臻": "#e84393",   # 品红
}

DAILY_SIGNIN_POINTS = 10
DEFAULT_TITLE_ID = "normal_user"

# obtain: default = 人人都有（未装备头衔时的兜底）；code = 兑换码；shop = 积分兑换。
TITLES = [
    {"id": "normal_user",   "name": "普通用户", "rarity": "普通", "obtain": "default"},
    {"id": "group_friend",  "name": "群友",     "rarity": "稀有", "obtain": "code"},
    {"id": "senior_user",   "name": "资深用户", "rarity": "稀有", "obtain": "shop", "cost": 300},
    {"id": "first_release", "name": "首发用户", "rarity": "史诗", "obtain": "shop",
     "cost": 30, "limited_until": "2026-10-30"},
]

# 商城货架。rename_card 本地阶段锁定（昵称仍免费改，改名卡等云端版再开放）。
SHOP_ITEMS = [
    {"id": "rename_card",   "name": "改名卡",   "kind": "rename", "cost": 100,
     "locked": True, "note": "即将开放"},
    {"id": "senior_user",   "name": "资深用户", "kind": "title",  "cost": 300},
    {"id": "first_release", "name": "首发用户", "kind": "title",  "cost": 30,
     "limited_until": "2026-10-30"},
]

# 固定兑换码（暂无）。「群友」头衔改用每日轮换码（见 group_code），不再用固定字符串，
# 免得某个码泄露后被长期复用。
REDEMPTION_CODES = {}

# 群友每日码的 HMAC 密钥。仓库是 public，此值会随源码公开，属「荣誉系统」级防护：
# 每日轮换只挡「已泄露码被长期复用」，挡不住「读源码自己生成当天码」。
QUNYOU_SECRET = b"fe7cb88e1db055268671a8dce5c0666f"


def group_code(day=None):
    """某一天的「群友」兑换码。同日确定、跨日变化，看起来随机但可离线校验。"""
    day = day or today_str()
    digest = hmac.new(QUNYOU_SECRET, ("qunyou:" + day).encode("utf-8"),
                      hashlib.sha256).digest()
    code = base64.b32encode(digest).decode("ascii").rstrip("=")
    return "QUNYOU-" + code[:6]


def title_by_id(title_id):
    for t in TITLES:
        if t["id"] == title_id:
            return t
    return None


def shop_item_by_id(item_id):
    for i in SHOP_ITEMS:
        if i["id"] == item_id:
            return i
    return None


def today_str():
    return date.today().isoformat()


def signin(conn, day=None):
    """签到一次。返回 (already, day, gained)。"""
    return slg_db.record_signin(conn, day or today_str(), DAILY_SIGNIN_POINTS)


def buy(conn, item):
    """用积分购买一件货架商品。locked 商品直接拒绝。返回是否成功。"""
    if not item or item.get("locked"):
        return False
    return slg_db.buy_title(conn, item["id"], item["cost"])


def _valid_group_codes():
    """当天 + 昨天的群友码（昨天的宽限跨零点）。"""
    today = date.today()
    return {group_code(today.isoformat()),
            group_code((today - timedelta(days=1)).isoformat())}


def redeem(conn, code):
    """兑换一个头衔兑换码。返回 (ok, msg)。"""
    code = (code or "").strip().upper()
    title_id = REDEMPTION_CODES.get(code)
    if title_id is None and code in _valid_group_codes():
        title_id = "group_friend"
    if not title_id:
        return (False, "兑换码无效")
    if slg_db.redeem_title(conn, title_id):
        return (True, "已获得头衔「%s」" % title_by_id(title_id)["name"])
    return (False, "你已拥有该头衔")


def available_shop_items(day=None):
    """商城货架，滤掉已过限时日的商品。"""
    day = day or today_str()
    out = []
    for item in SHOP_ITEMS:
        lu = item.get("limited_until")
        if lu and day > lu:
            continue
        out.append(item)
    return out


if __name__ == "__main__":
    import sys

    if "--code" in sys.argv:
        print(group_code())
