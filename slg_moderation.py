"""Small, deterministic first-pass filter for public game comments.

The server owns final publication and reward decisions.  This module does not
store comments or call an external moderation service, keeping each request
cheap and independent of the size of the comment log.
"""

from __future__ import annotations

import re
import unicodedata


MIN_CHARS = 20
MAX_CHARS = 500
MAX_UTF8_BYTES = 2048

# Keep these expressions narrow. Political context that a lexical rule cannot
# distinguish reliably goes to review; ordinary plot/character talk passes.
_POLITICAL_REJECT = re.compile(
    r"(?:习近平|中国共产党|中共(?:中央)?|中共中央|中央政治局|国务院|人大常委会|"
    r"六四(?:事件|运动|屠杀)?|天安门事件|文化大革命|法轮功|"
    r"台湾独立|港独|疆独|藏独|推翻(?:现实|中央|国家)?政府|颠覆政权|"
    r"打倒(?:政府|共产党)|涉政(?:内容|话题|宣传)|时政|政治新闻|政治评论|"
    r"现实(?:社会)?政治(?:话题|讨论|宣传|立场)?|政治宣传|"
    r"(?:现实|当今).{0,6}(?:政治|政府|政党|总统|选举|示威|游行|领导人|国家政策)|"
    r"(?:政治|政府|政党|总统|选举|示威|游行|领导人|国家政策).{0,6}(?:现实|当今))",
    re.I,
)
_POLITICAL_REVIEW = re.compile(
    r"(?:政治立场|政治观点|政府政策|总统选举|国家主权|外交立场|"
    r"(?:(?:支持|反对|讨论|宣传|主张|批判|评价|投票).{0,8}"
    r"(?:政治|政党|共产党|政府|总统|选举|示威|游行|革命|意识形态|领导人|主权|独立|统一|候选人|外交)|"
    r"(?:政治|政党|共产党|政府|总统|选举|示威|游行|革命|意识形态|领导人|主权|独立|统一|候选人|外交)"
    r".{0,8}(?:支持|反对|讨论|宣传|主张|批判|评价|投票)))",
    re.I,
)
_RESOURCE = re.compile(
    r"(?:magnet:\?|ed2k://|thunder://|(?:pan\.)?baidu\.com/s/|"
    r"pan\.quark\.cn|aliyundrive\.com|123pan\.com|"
    r"(?:网盘|磁力|种子|下载链接|资源链接|求资源|求链接|求种子).{0,12}"
    r"(?:分享|下载|获取|地址|链接|提取码|解压码|私聊|回复)|"
    r"(?:分享|下载|获取|提供|求|私聊).{0,12}"
    r"(?:游戏资源|网盘|磁力|种子|提取码|解压码)|"
    r"(?:提取码|解压码)\s*[:：=])",
    re.I,
)
_URL = re.compile(r"(?:https?://|www\.|[a-z0-9-]+\.(?:com|cn|net|org|io|app)(?:/|\b))", re.I)
_ADVERTISEMENT = re.compile(
    r"(?:加(?:我|微|v|V|扣|Q|q)(?:信|群|好友|号)?|扫码(?:购买|进群|加群)|"
    r"(?:代充|低价出售|私聊购买|付费解锁|包月会员|广告合作))",
    re.I,
)
_MINOR = re.compile(r"(?:未成年|未满\s*十八岁|幼女|幼童|儿童|小学生|初中生)")
_AMBIGUOUS_AGE = re.compile(r"(?:萝莉|正太)")
_SEXUAL = re.compile(
    r"(?:性(?:交|侵|爱|行为|剥削)|做爱|性爱|强奸|性交|猥亵|色情|淫|裸|调教)"
)
_REPEATED_CHAR = re.compile(r"(.)\1{9,}", re.S)
_REPEATED_PHRASE = re.compile(r"(.{4,80})\1{3,}", re.S)


def _normalise(content: str) -> str:
    text = unicodedata.normalize("NFKC", content)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def classify(content: str) -> dict[str, str]:
    """Return ``decision`` (approve/review/reject) and an admin-only reason.

    A review result is deliberately conservative: rules alone cannot resolve
    whether a vague political or external-link mention is safe in context.
    """
    if not isinstance(content, str):
        return {"decision": "reject", "reason": "评论内容不是文本"}
    text = _normalise(content).strip()
    compact = re.sub(r"\s+", "", text)
    if len(text) < MIN_CHARS or len(text) > MAX_CHARS or len(text.encode("utf-8")) > MAX_UTF8_BYTES:
        return {"decision": "reject", "reason": "评论长度须为20至500字且不超过2048字节"}
    # Remove separators only for the high-confidence deny list, so inserting
    # punctuation cannot trivially bypass explicit political-name rules.
    political_compact = re.sub(r"[\W_]+", "", compact, flags=re.UNICODE)
    if _POLITICAL_REJECT.search(political_compact):
        return {"decision": "reject", "reason": "明确涉及现实政治"}
    if _RESOURCE.search(compact):
        return {"decision": "reject", "reason": "包含网盘、磁力或资源获取引导"}
    if _ADVERTISEMENT.search(compact):
        return {"decision": "reject", "reason": "明显广告或站外引流"}
    if (_REPEATED_CHAR.search(compact) or _REPEATED_PHRASE.search(compact)
            or (len(compact) > 30 and len(set(compact)) < 5)):
        return {"decision": "reject", "reason": "重复字符刷屏"}
    if _MINOR.search(text) and _SEXUAL.search(text):
        return {"decision": "reject", "reason": "明确涉及未成年人性内容"}
    if _AMBIGUOUS_AGE.search(text) and _SEXUAL.search(text):
        return {"decision": "review", "reason": "角色年龄表述含糊，须人工核查"}
    if _POLITICAL_REVIEW.search(text):
        return {"decision": "review", "reason": "可能涉及政治语境，须人工核查"}
    if _URL.search(text):
        return {"decision": "review", "reason": "包含站外链接，须人工核查"}
    return {"decision": "approve", "reason": "未命中风险规则"}


COMMENT_RULE_VERSION = "comments-2026-10-03-v1"
_DIRECT_INSULT = re.compile(
    r"(?:你(?:们)?|楼主|层主|管理员|作者|开发者|玩家|评论者).{0,8}"
    r"(?:傻[逼比]|煞笔|蠢货|废物|脑残|狗东西|去死|滚蛋|死全家|操你|草你|艹你)|"
    r"(?:傻[逼比]|煞笔|脑残|狗东西).{0,5}(?:楼主|层主|管理员|作者|开发者|玩家)|"
    r"(?:操|草|艹)你(?:妈|娘)|你妈.{0,3}(?:死|逼)", re.I)
_FICTIONAL_QUOTE = re.compile(
    r"(?:角色|主角|男主|女主|反派|NPC|剧情).{0,24}"
    r"(?:说|喊|骂|台词|对白|对话).{0,12}"
    r"[\"“‘「『](?P<dialogue>[^\"”’」』\n]{1,160})[\"”’」』]", re.I)


def classify_comment(content: str) -> dict[str, str]:
    """Validate comment format before applying shared discussion risk rules."""
    if not isinstance(content, str):
        return {"decision": "reject", "reason": "评论内容不是文本",
                "source": "automatic", "rule_version": COMMENT_RULE_VERSION}
    text = _normalise(content).strip()
    if not MIN_CHARS <= len(text) <= MAX_CHARS or len(text.encode("utf-8")) > MAX_UTF8_BYTES:
        return {"decision": "reject", "reason": "评论长度须为20至500字且不超过2048字节",
                "source": "automatic", "rule_version": COMMENT_RULE_VERSION}
    return classify_discussion(content)


def classify_discussion(content: str) -> dict[str, str]:
    """Classify complete discussion text without imposing comment size limits."""
    def verdict(decision, reason):
        return {"decision": decision, "reason": reason,
                "source": "automatic", "rule_version": COMMENT_RULE_VERSION}

    if not isinstance(content, str):
        return verdict("reject", "评论内容不是文本")
    text = _normalise(content).strip()
    compact = re.sub(r"\s+", "", text)
    political_compact = re.sub(r"[\W_]+", "", compact, flags=re.UNICODE)
    if _POLITICAL_REJECT.search(political_compact):
        return verdict("reject", "明确涉及现实政治")
    if (_REPEATED_CHAR.search(compact) or _REPEATED_PHRASE.search(compact)
            or (len(compact) > 30 and len(set(compact)) < 5)):
        return verdict("reject", "重复字符或低信息内容刷屏")
    insults = list(_DIRECT_INSULT.finditer(compact))
    if insults:
        dialogue_ranges = [match.span("dialogue") for match in _FICTIONAL_QUOTE.finditer(compact)]
        # A fictional quote does not excuse an additional attack outside it.
        if all(any(start <= match.start() and match.end() <= end
                   for start, end in dialogue_ranges) for match in insults):
            return verdict("review", "剧情台词含辱骂表述，须人工核查")
        return verdict("reject", "直接针对他人辱骂")
    if (_MINOR.search(text) or _AMBIGUOUS_AGE.search(text)) and _SEXUAL.search(text):
        return verdict("review", "角色年龄或性内容风险，须人工核查")
    if _RESOURCE.search(compact):
        return verdict("review", "资源获取引导，须人工核查")
    if _ADVERTISEMENT.search(compact):
        return verdict("review", "疑似广告或引流，须人工核查")
    if _POLITICAL_REVIEW.search(text):
        return verdict("review", "政治语境不明确，须人工核查")
    if _URL.search(text):
        return verdict("review", "包含站外链接，须人工核查")
    return verdict("approve", "正常游戏讨论，未命中风险规则")
