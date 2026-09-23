"""头衔、积分商城与兑换码的目录和业务规则。

纯本地批次的「归属感」数据源：头衔稀有度分级（对用户隐藏，只通过颜色/特效体现）、
积分经济数值、商城货架、以及靠兑换码解锁的特殊头衔。这里只放目录和高层业务，
持久化原语在 `slg_db`。
"""

import base64
import hashlib
import hmac
import json
import os
import random
from datetime import date, timedelta

import slg_db
import slg_update

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

# 每月累计签到达标奖励：签到满 N 天发一次积分。按档位升序补发，每月各档只领一次。
SIGNIN_MILESTONES = {5: 15, 10: 30, 20: 50}

# 补签卡价格（积分）。补签只补天数、不再给当日签到分。
MAKEUP_CARD_COST = 5

# obtain: default = 人人都有（未装备头衔时的兜底）；code = 兑换码；shop = 积分兑换；
#         lottery = 每日抽奖大奖。
# desc 是给「查看头衔」里那颗「获得方式」按钮用的一句话简介；获取路径由
# obtain_title_text() 从 obtain/cost/limited_until 推出来，不另写一份。
TITLES = [
    {"id": "normal_user",   "name": "普通用户", "rarity": "普通", "obtain": "default",
     "desc": "默认头衔。没有名头的时候就是它，安安静静挂在昵称旁边。"},
    {"id": "group_friend",  "name": "群友",     "rarity": "稀有", "obtain": "code",
     "desc": "交流群的常驻证明。群公告里每天换一次的兑换码换来的。"},
    {"id": "senior_user",   "name": "资深用户", "rarity": "稀有", "obtain": "shop",
     "cost": 300,
     "desc": "攒得住的玩家才有。没有捷径，就是每天签到把积分攒够。"},
    {"id": "first_release", "name": "首发用户", "rarity": "史诗", "obtain": "shop",
     "cost": 30, "limited_until": "2026-10-30",
     "desc": "限时纪念头衔。软件早期就在的那批人，凭证。"},
    {"id": "lucky_star",    "name": "幸运星",   "rarity": "史诗", "obtain": "lottery",
     "desc": "每日抽奖的大奖。滚轮摇出三个 7 才出，一百抽之内必定到手。"},
    {"id": "king_of_luck",  "name": "幸运之王", "rarity": "传说", "obtain": "lottery",
     "desc": "抽奖最高一档。滚轮摇出三个皇冠才出，没有任何保底，全靠运气。"},
    {"id": "butter_king", "name": "黄油之王", "rarity": "至臻", "obtain": "code",
     "desc": "唯一一档至臻。作者持有，只通过开发者密钥解锁。"},
]

# 每条头衔的获取路径，一句话。obtain_hint() 是同一个来源的短标签版本。
_OBTAIN_TEXT = {
    "default": "默认头衔，人人都有，无需获取。",
    "code": "加入交流群，用群公告里每天更新的当日兑换码兑换。",
    "lottery": "每日抽奖摇出「777」获得；累计 100 抽必定出一次。",
    "dev": "输入开发者密钥解锁。",
}

# 幸运之王跟幸运星同为 lottery，但获取路径不同 —— 一句通用的「摇出 777」会把
# 最高一档说得跟史诗一档一样，所以它单独有一句，写明概率和「没有保底」。
LOTTERY_JACKPOT_TEXT = ("每日抽奖摇出「皇冠 ×3」获得，概率 0.01%，而且没有保底。"
                        "一百抽的保底只保「幸运星」—— 皇冠只能靠运气。")


def obtain_title_text(t):
    """一条头衔的获取路径（完整句），跟 obtain_hint 同源。"""
    if t["id"] == "butter_king":
        return _OBTAIN_TEXT["dev"]
    if t["id"] == LOTTERY_JACKPOT_TITLE:
        return LOTTERY_JACKPOT_TEXT
    kind = t.get("obtain") or ""
    if kind == "shop":
        text = "积分商城用 %d 分兑换。" % t.get("cost", 0)
        if t.get("limited_until"):
            text += "限时商品，%s 之后下架。" % t["limited_until"]
        return text
    return _OBTAIN_TEXT.get(kind, "")


# --- 版本更新维护补偿 -------------------------------------------------------------

# 版本号比对只用来定「发多少」：major/minor 变 = 大版本 30 分，仅 patch 变 = 小版本 10 分。
# pref 记「上次发过的版本号」，升级时和当前 APP_VERSION 不一致就补发一次，幂等。
COMPENSATION_PREF = "points.compensation_version"
COMPENSATION_MAJOR = 30
COMPENSATION_MINOR = 10


def _compensation_amount(prev, cur):
    """prev -> cur 该发多少分。首测（prev 解析不出）或 major/minor 变给 30，仅 patch 变给 10。"""
    p, c = slg_update.parse_version(prev), slg_update.parse_version(cur)
    if p is None or c is None:
        return COMPENSATION_MAJOR
    if p[0] != c[0] or p[1] != c[1]:
        return COMPENSATION_MAJOR
    if p[2] != c[2]:
        return COMPENSATION_MINOR
    return 0


def apply_update_compensation(conn, current_version):
    """版本变了就发一次维护补偿积分，返回本次实发数量（0 = 没发）。"""
    prev = slg_db.get_pref(conn, COMPENSATION_PREF, "") or ""
    if prev == current_version:
        return 0
    amount = COMPENSATION_MAJOR if not prev else _compensation_amount(prev, current_version)
    if amount:
        slg_db.add_points(conn, amount, "版本更新补偿")
    slg_db.set_pref(conn, COMPENSATION_PREF, current_version)
    return amount

# 商城货架。rename_card 本地阶段锁定（昵称仍免费改，改名卡等云端版再开放）。
# category 是顶层分组（头衔类 / 物品类），subcategory 在其下按属性细分（稀有度 / 类型）。
# daily_lottery 是特殊项：kind="lottery"，不走普通 buy，点「抽一次」触发老虎机。
SHOP_ITEMS = [
    {"id": "rename_card",   "name": "改名卡",   "kind": "rename",  "category": "物品类",
     "subcategory": "消耗品", "cost": 100, "locked": True, "note": "即将开放",
     "description": "用于修改昵称。云端版上线后开放，届时昵称唯一、改名消耗一张。"},
    {"id": "daily_lottery", "name": "每日抽奖", "kind": "lottery", "category": "物品类",
     "subcategory": "抽奖", "cost": 5, "note": "每日 3 次",
     "description": "花 5 积分抽一次，有机会赢取史诗头衔「幸运星」或若干积分。每日限 3 次。"},
    {"id": "makeup_card",   "name": "补签卡",   "kind": "makeup",  "category": "物品类",
     "subcategory": "消耗品", "cost": 5, "note": "补签一次",
     "description": "补上本月最近一个漏签日，接续累计签到，不发放当日签到分。"},
    {"id": "senior_user",   "name": "资深用户", "kind": "title",   "category": "头衔类",
     "subcategory": "稀有", "cost": 300,
     "description": "稀有头衔，资深玩家的身份象征，评论上线后展示在昵称旁。"},
    {"id": "first_release", "name": "首发用户", "kind": "title",   "category": "头衔类",
     "subcategory": "史诗", "cost": 30, "limited_until": "2026-10-30",
     "description": "史诗头衔，首发用户的限时纪念，到期后下架。"},
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


def dev_secret():
    """The developer key, from a machine-local file, or None.

    只从本机文件读取，绝不写进源码或任何会被 git 跟踪的文件。测试可用环境变量
    SLGKING_DEV_SECRET 注入一个假密钥来走同一条代码路径，而不把真实密钥落进仓库。
    """
    override = os.environ.get("SLGKING_DEV_SECRET")
    if override:
        return override.strip().upper()
    path = os.path.join(slg_db.app_dir(), "dev_secret.txt")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            return value.upper() if value else None
    except OSError:
        return None


def dev_unlocked(conn):
    """True once the developer key has been redeemed on this machine."""
    return slg_db.get_pref(conn, "dev.unlocked", "") == "1"


def signin(conn, day=None):
    """签到一次。返回 (already, day, gained, bonus)。开发者模式下不限次数。

    bonus 是当月累计签到达标补发的积分，签到时一并结算，弹窗可一次展示。
    """
    day = day or today_str()
    if dev_unlocked(conn):
        slg_db.add_points(conn, DAILY_SIGNIN_POINTS, "签到（开发者）")
        return (False, day, DAILY_SIGNIN_POINTS, 0)
    already, d, gained = slg_db.record_signin(conn, day, DAILY_SIGNIN_POINTS)
    bonus = grant_signin_milestones(conn, day) if not already else 0
    return (already, d, gained, bonus)


def _milestone_key(day):
    year, month, _d = (int(x) for x in day.split("-"))
    return "signin.milestone.%04d-%02d" % (year, month)


def claimed_milestone(conn, day=None):
    """当月已领到的最高累签档位（天数），没领过返回 0。"""
    day = day or today_str()
    claimed = slg_db.get_pref(conn, _milestone_key(day), "") or ""
    return int(claimed) if claimed else 0


def grant_signin_milestones(conn, day=None):
    """补发当月累计签到达标的奖励。返回本次额外加分（可能一次跨多档）。

    进度存 prefs：`signin.milestone.YYYY-MM` = 已领到的最高天数档。按档位升序补发，
    跨档（如直接补签跳过 5 天）也能一次把 5/10/20 三档都领齐。
    """
    day = day or today_str()
    year, month, _d = (int(x) for x in day.split("-"))
    key = _milestone_key(day)
    old_tier = claimed_milestone(conn, day)
    tier = old_tier
    count = len(slg_db.signin_month_days(conn, year, month))
    bonus = 0
    for need, pts in sorted(SIGNIN_MILESTONES.items()):
        if count >= need and tier < need:
            slg_db.add_points(conn, pts, "累签奖励 %d 天" % need)
            tier = need
            bonus += pts
    if tier != old_tier:
        slg_db.set_pref(conn, key, str(tier))
    return bonus


def _last_missed_day(conn, day):
    """本月、今天之前、最近的一个未签到日（ISO 日期），没有则 None。"""
    today = date.fromisoformat(day)
    signed = slg_db.signin_month_days(conn, today.year, today.month)
    for d in range(today.day - 1, 0, -1):
        if d not in signed:
            return "%04d-%02d-%02d" % (today.year, today.month, d)
    return None


def makeup_problem(conn, target, day=None):
    """`target` 能不能作为补签日。能就返回 None，否则返回给用户看的原因。

    日历上「哪天可以点」和真正下单前的校验读同一个函数，免得点得亮却补不了。
    """
    day = day or today_str()
    if not target:
        return "没有可补签的日子"
    today = date.fromisoformat(day)
    try:
        want = date.fromisoformat(target)
    except ValueError:
        return "日期无效"
    if (want.year, want.month) != (today.year, today.month):
        return "只能补本月的漏签"
    if want >= today:
        return "只能补今天之前的日子"
    if want.day in slg_db.signin_month_days(conn, today.year, today.month):
        return "%s 已经签过了" % target
    return None


def buy_makeup_card(conn, day=None, target=None):
    """用积分买一张补签卡。返回 (ok, msg, bonus)。

    `target` 给了就补那一天（日历点名补签，先过 makeup_problem），没给就补本月
    最近的一个漏签日（商城那颗「补签」按钮的老行为）。
    补签只插入一条 signin 行（不再给当日 +10），随后结算累签达标奖励。
    """
    day = day or today_str()
    if slg_db.points_balance(conn) < MAKEUP_CARD_COST:
        return (False, "积分不足（补签卡需 %d 分）" % MAKEUP_CARD_COST, 0)
    if target is None:
        target = _last_missed_day(conn, day)
        if target is None:
            return (False, "本月没有漏签", 0)
    else:
        problem = makeup_problem(conn, target, day)
        if problem:
            return (False, problem, 0)
    slg_db.add_points(conn, -MAKEUP_CARD_COST, "补签卡")
    slg_db.makeup_signin(conn, target)
    bonus = grant_signin_milestones(conn, day)
    return (True, "已补签 %s" % target, bonus)


def buy(conn, item):
    """用积分购买一件货架商品。locked 商品直接拒绝。返回是否成功。"""
    if not item or item.get("locked"):
        return False
    return slg_db.buy_title(conn, item["id"], item["cost"])


# --- 每日抽奖 --------------------------------------------------------------------

LOTTERY_COST = 5
# 每日抽奖次数上限。开发者特权（dev_unlocked）不受此限。
LOTTERY_DAILY_LIMIT = 3
LOTTERY_GRAND_TITLE = "lucky_star"
# 抽奖最高一档：0.01%，只走权重表，不走保底。
LOTTERY_JACKPOT_TITLE = "king_of_luck"

# 保底：连着这么多抽没出头衔，下一抽直接给。概率表不动，只是把「一直不出」封了顶。
# 保底只保 LOTTERY_GRAND_TITLE；幸运之王不进这条路径，否则 0.01% 就是假的。
LOTTERY_PITY = 100
# 大奖头衔已经拥有时再抽到，折算成这个数目的积分 —— 否则「又抽到了」等于什么都没发生。
LOTTERY_DUPLICATE_POINTS = 100
# 稀有档的折算单独给，否则重复抽到 0.01% 的皇冠只赔 100 分，比中奖还难受。
LOTTERY_DUPLICATE_BY_TITLE = {LOTTERY_JACKPOT_TITLE: 2000}
# 抽奖记录保留条数。
LOTTERY_HISTORY_KEEP = 20

# (weight, kind, value)。权重合计 LOTTERY_WEIGHT_TOTAL；kind: "title"|"points"。
# 放大到万分之一是为了给幸运之王留出 0.01% —— 100 的刻度上它只能是 0。
# 那 1 份从「谢谢参与」里出，其余档位与 0.22.0 逐档一致（1%/30%/20%/...）。
LOTTERY_WEIGHT_TOTAL = 10000
LOTTERY_PRIZES = [
    (1,    "title",  LOTTERY_JACKPOT_TITLE),  # 0.01% 幸运之王（无保底）
    (100,  "title",  LOTTERY_GRAND_TITLE),    # 1%    幸运星（保底会兜）
    (2999, "points", 0),                      # 29.99% 谢谢参与
    (2000, "points", 1),
    (1500, "points", 2),
    (1200, "points", 3),
    (1000, "points", 4),
    (700,  "points", 5),
    (400,  "points", 10),
    (100,  "points", 20),
]

# --- 抽奖奖项对照表 ---------------------------------------------------------------
# 每个奖档独占一组符号，且是唯一真源：滚轮落定的组合（slg_gui._lottery_finals）
# 和「抽奖概率」弹窗里的表格都从这里读，所以规则和演出不可能各说各话。

SLOT_SYMBOLS = ("seven", "bar", "bell", "cherry", "diamond", "star")

# key: 奖品的 value（头衔奖品是头衔 id，积分奖品是分数）。value: 三个符号；
# "*" 表示任意符号。
# 皇冠是 2026-09-23 为幸运之王新加的一枚（见 tools/make_slot_symbols.py）——
# 它必须跟金色的「7」长得不一样，否则「转出来一样、奖不一样」就是当年那个假擦边。
LOTTERY_SYMBOLS = {
    LOTTERY_JACKPOT_TITLE: ("crown", "crown", "crown"),   # 0.01%
    LOTTERY_GRAND_TITLE:   ("seven", "seven", "seven"),   # 1%  用户拍板：777 就是大奖
    20:      ("diamond", "diamond", "diamond"),
    10:      ("bar", "bar", "bar"),
    5:       ("bell", "bell", "bell"),
    4:       ("cherry", "cherry", "cherry"),
    3:       ("star", "star", "star"),
    2:       ("star", "star", "*"),
    1:       ("cherry", "cherry", "*"),
}

# 0 分那一档的「谢谢参与」图案。三种互不相同，且构不成上表任何中奖组合
# ——公开了对照表，落空就得长得像落空（现在还要多加一条：不许凑出三个皇冠）。
LOTTERY_MISSES = [
    ("bell", "cherry", "seven"),
    ("star", "bar", "bell"),
    ("diamond", "cherry", "bar"),
]


def symbols_for_prize(prize):
    """一次抽奖落定的三个符号，查不到就是「谢谢参与」档（None）。

    滚轮和概率表都走这里，只要奖品的 value 对得上，演出就不可能跟说明不符。
    """
    return LOTTERY_SYMBOLS.get(prize["value"])


# 有图的那几枚：上面三张表里出现过的符号全都算上。
#
# 皇冠不在 SLOT_SYMBOLS 里（转的时候不会随机出现，只在中奖时落定），但**必须**有图，
# 否则 `_slot_symbol_images` 载不到它就跳过那一格 —— 中奖的那一格反而是空的，正是
# 当年「三个空盒子」那个 bug 换了个面孔。GUI 按这份名单加载，别再手写一份。
SYMBOL_NAMES = tuple(sorted(
    set(SLOT_SYMBOLS)
    | {name for combo in LOTTERY_SYMBOLS.values() for name in combo if name != "*"}
    | {name for combo in LOTTERY_MISSES for name in combo}))


def duplicate_points(title_id):
    """重复抽到某个头衔时折算的积分。"""
    return LOTTERY_DUPLICATE_BY_TITLE.get(title_id, LOTTERY_DUPLICATE_POINTS)


def _title_prize_text(title_id):
    t = title_by_id(title_id) or {}
    return "%s头衔「%s」" % (t.get("rarity", ""), t.get("name", title_id))


def lottery_paytable():
    """(symbols|None, 奖项文案, 概率百分数) 列表，头衔档在前、积分档按权重降序。

    从 LOTTERY_PRIZES 推导而不是另写一份，避免表里的概率和实际抽奖漂移。
    symbols 为 None 表示「任意不中奖组合」，弹窗据此渲染成「谢谢参与」。
    概率是浮点百分数（万分之一刻度），弹窗用两位小数才显示得出 0.01%。
    """
    rows = []
    for weight, kind, value in LOTTERY_PRIZES:
        percent = weight * 100.0 / LOTTERY_WEIGHT_TOTAL
        if kind == "title":
            rows.append((symbols_for_prize({"kind": kind, "value": value}),
                         _title_prize_text(value), percent))
        elif not value:
            rows.append((None, "谢谢参与", percent))
        else:
            rows.append((LOTTERY_SYMBOLS[value], "%d 积分" % value, percent))
    return rows


def last_lottery_day(conn):
    return slg_db.get_pref(conn, "lottery.last_day", "") or ""


def lottery_draws_today(conn):
    """今天已经抽了几次（0 .. LOTTERY_DAILY_LIMIT）。跨天自动归零。"""
    if last_lottery_day(conn) != today_str():
        return 0
    raw = slg_db.get_pref(conn, "lottery.count", "") or ""
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def lottery_pity(conn):
    """距离上一次抽到头衔已经抽了多少次（0 .. LOTTERY_PITY）。"""
    raw = slg_db.get_pref(conn, "lottery.pity", "") or ""
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def lottery_history(conn):
    """最近几次抽奖，新的在前：[{"day", "kind", "value", "points"}]。

    只是展示用的流水，坏了就当没有 —— 抽奖本身不依赖它。
    """
    raw = slg_db.get_pref(conn, "lottery.history", "") or ""
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def lottery_prize_text(prize):
    """一次抽奖结果的一句话文案。弹窗、历史记录、测试都读这里，只有一份措辞。"""
    if prize.get("kind") == "title":
        t = title_by_id(prize.get("value")) or {}
        name = t.get("name", "头衔")
        if prize.get("duplicate"):
            return "重复获得「%s」，折算 %d 积分" % (
                name, prize.get("points") or LOTTERY_DUPLICATE_POINTS)
        # 稀有度从头衔本身读：「史诗」在这里是写死过的，遇上传说档就会说错。
        return "恭喜获得%s头衔「%s」！" % (t.get("rarity", ""), name)
    if not prize.get("value"):
        return "谢谢参与，下次再来"
    return "获得 %d 积分" % prize["value"]


def _record_lottery(conn, day, prize):
    """追加一条抽奖流水。points 记的是这次真正入账的积分：头衔 0，重复折算 N。

    duplicate 必须一起存：历史卡片是拿流水行再调 lottery_prize_text 的，缺了这个
    标记，一次「重复大奖折算 100 分」会被写成「恭喜获得史诗头衔」，等于把记录
    说成了没发生的事。
    """
    points = prize.get("points", 0) if prize["kind"] == "title" else prize["value"]
    row = {"day": day, "kind": prize["kind"], "value": prize["value"],
           "points": points}
    if prize.get("duplicate"):
        row["duplicate"] = True
    rows = lottery_history(conn)
    rows.insert(0, row)
    slg_db.set_pref(conn, "lottery.history",
                    json.dumps(rows[:LOTTERY_HISTORY_KEEP], ensure_ascii=False))


def draw_lottery(conn, rng=None):
    """一次每日抽奖。返回 (ok, msg, prize)；prize = {"kind", "value"}。

    先扣 5 分并记录当天，再结算奖品（title 直接拥有、points 入账）。已抽过或积分不足
    返回 ok=False 且不动账本。开发者特权不限当日次数。rng 可注入随机源（测试用
    `random.Random(seed)`）。

    连续 LOTTERY_PITY 抽没出头衔时，第 LOTTERY_PITY 抽直接给大奖；出了就归零。
    已拥有大奖头衔再抽到，不重复入库，改按 LOTTERY_DUPLICATE_POINTS 折算积分，
    prize 保持 kind="title" 并带 duplicate=True —— 滚轮照落 777 才算诚实。
    """
    day = today_str()
    dev = dev_unlocked(conn)
    if not dev and lottery_draws_today(conn) >= LOTTERY_DAILY_LIMIT:
        return (False, "今日已抽 %d 次，明天再来" % LOTTERY_DAILY_LIMIT, None)
    if slg_db.points_balance(conn) < LOTTERY_COST:
        return (False, "积分不足（抽奖需 %d 分）" % LOTTERY_COST, None)
    rng = rng or random
    pity = lottery_pity(conn)
    if pity + 1 >= LOTTERY_PITY:
        prize = {"kind": "title", "value": LOTTERY_GRAND_TITLE}
    else:
        total = sum(w for w, _k, _v in LOTTERY_PRIZES)
        roll = rng.randint(1, total)
        acc = 0
        prize = None
        for weight, kind, value in LOTTERY_PRIZES:
            acc += weight
            if roll <= acc:
                prize = {"kind": kind, "value": value}
                break
        if prize is None:
            prize = {"kind": "points", "value": 0}
    slg_db.add_points(conn, -LOTTERY_COST, "每日抽奖")
    today_count = lottery_draws_today(conn)
    slg_db.set_pref(conn, "lottery.last_day", day)
    slg_db.set_pref(conn, "lottery.count", str(today_count + 1))
    if prize["kind"] == "title":
        if prize["value"] in slg_db.owned_title_ids(conn):
            points = duplicate_points(prize["value"])
            prize = {"kind": "title", "value": prize["value"], "duplicate": True,
                     "points": points}
            slg_db.add_points(conn, points, "抽奖重复头衔折算")
        else:
            slg_db.own_title(conn, prize["value"], "lottery")
        slg_db.set_pref(conn, "lottery.pity", "0")
    else:
        slg_db.set_pref(conn, "lottery.pity", str(pity + 1))
        slg_db.add_points(conn, prize["value"], "抽奖奖励")
    _record_lottery(conn, day, prize)
    return (True, "抽奖完成", prize)


def unlock_all_titles(conn):
    """开发者特权：一键拥有全部头衔（默认头衔除外）。返回新获得数量。"""
    gained = 0
    for t in TITLES:
        if t["obtain"] == "default":
            continue
        if t["id"] not in slg_db.owned_title_ids(conn):
            slg_db.own_title(conn, t["id"], "dev")
            gained += 1
    return gained


def _valid_group_codes():
    """当天 + 昨天的群友码（昨天的宽限跨零点）。"""
    today = date.today()
    return {group_code(today.isoformat()),
            group_code((today - timedelta(days=1)).isoformat())}


def redeem(conn, code):
    """兑换一个头衔兑换码。返回 (ok, msg)。"""
    code = (code or "").strip().upper()
    secret = dev_secret()
    if secret and code == secret:
        slg_db.set_pref(conn, "dev.unlocked", "1")
        if slg_db.redeem_title(conn, "butter_king"):
            return (True, "已解锁开发者特权，获得头衔「黄油之王」")
        return (True, "已解锁开发者特权")
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
