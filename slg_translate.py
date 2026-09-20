"""What gets said to a translation engine, and what is made of the answer.

Two very different jobs share one client. The tags are a closed set of ~120
strings that every card in the library renders, so they are translated in one
batch and cached forever - consistency across the whole UI matters more than
latency. The overviews and the game names are numerous and read one at a time,
so they are translated on demand and the answer is cached against a hash of the
source text.

Which endpoint a request reaches is slg_engines' business; all this module ever
holds is a `chat` callable. The key is the user's own, typed into the settings
dialog and kept in the prefs table. It rides in the Authorization header and
nothing here formats it into a message, a log line or an exception.
"""

import json
import re
import sys

import slg_db
import slg_util
from slg_engines import (  # noqa: F401 - re-exported for the callers that import them from here
    DEFAULT_BASE_URL, DEFAULT_MODEL, ENGINE_FREE, ENGINE_OPENAI, PREF_BASE_URL,
    PREF_ENGINE, PREF_KEY, PREF_MODEL, PREF_PROVIDER, PROVIDERS, PROVIDER_BY_ID,
    TIMEOUT, OpenAIEngine, TranslateError, resolve_config, resolve_key,
)

# The model handles ~40 short strings in one reply comfortably. Longer batches
# start dropping entries off the end, which costs a retry round anyway.
TAG_CHUNK = 40

# The settings dialog's round trip. It is two characters both ways, so it gets a
# shorter budget than a real translation: the point is a verdict in seconds, and
# a machine whose DNS is blackholed says so by timing out.
PROBE_TIMEOUT = 8

TAG_NEEDS_JSON = ("标签翻译要选一个 AI 服务商：标签是全库共用的固定术语，"
                  "机翻每次给的译法都不一样。请在「翻译设置…」里切换引擎。")


class TitleGuardError(TranslateError):
    """The model mangled a game name past the point where showing it is safe."""


# --- prompt --------------------------------------------------------------------

# Settled translations, handed to the model as reference. Without them the same
# tag comes back differently either side of a chunk boundary - 'big-tits' is
# 巨乳 in one reply and 大胸 in the next - and the library reads as inconsistent
# from one card to the next.
TAG_ANCHORS = {
    "netorare": "NTR",
    "corruption": "堕落",
    "big-tits": "巨乳",
    "big-ass": "巨臀",
    "animated": "动态CG",
    "male-protagonist": "男主角",
    "female-protagonist": "女主角",
    "ahegao": "阿嘿颜",
    "bdsm": "BDSM",
    "harem": "后宫",
    "voyeurism": "偷窥",
    "exhibitionism": "露出",
}

TAG_SYSTEM = (
    "你是简体中文二次元/同人游戏圈的术语译者。用户会给你一组英文游戏标签，"
    "你要把它们翻译成这个圈子里玩家实际使用、一看就懂的说法。\n\n"
    "要求：\n"
    "1. 用圈内惯用语，不要逐字直译。例如 netorare 译作 NTR，animated 译作 "
    "动态CG，不要译成「动画的」。\n"
    "2. 尽量简短，一般 2-5 个汉字；有通用缩写的（NTR、BDSM、3DCG、CG、AI）"
    "直接保留原文缩写。\n"
    "3. 每一条都要给出译文，不要遗漏，不要解释，不要加引号。\n"
    "4. 只翻译，不评价内容。\n\n"
    "参考译法（务必与这些保持一致）：\n"
    + "\n".join("  %s → %s" % pair for pair in sorted(TAG_ANCHORS.items()))
    + "\n\n只输出一个 JSON 对象，键是原始标签，值是中文译文。"
)

OVERVIEW_SYSTEM = (
    "你是简体中文游戏资料站的译者。把给你的英文游戏简介翻译成自然通顺的"
    "简体中文。\n\n"
    "要求：\n"
    "1. 忠实原意，不增删内容，不加评论。\n"
    "2. 符合中文表达习惯，不要保留英文语序。\n"
    "3. 人名、游戏名等专有名词保留原文，不要音译。\n"
    "4. 直接输出译文，不要任何前缀、解释或引号。"
)

TITLE_SYSTEM = (
    "你是简体中文游戏资料站的译者。把给你的英文游戏名翻译成简体中文名。\n\n"
    "要求：\n"
    "1. 只输出译文本身，不要解释、不要引号、不要句末标点。\n"
    "2. 版本号一律原样照抄，例如 v1.20、1.2.3、EP03、Chapter 2 都不能改动、"
    "不能译成「1.20版」或「第2章」。\n"
    "3. 方括号 [] 里的社团名、工作室名、作者名原样保留，不要翻译。\n"
    "4. 人名、系列名等其他专有名词保留原文，不要音译。\n"
    "5. 只翻译真正的游戏名部分，不要添加原文里没有的词。"
)


# --- transport -----------------------------------------------------------------

# The endpoint a call reaches with no engine and no config: DeepSeek, which is
# what every version of this program before the engine switch talked to.
_DEFAULT_ENGINE = OpenAIEngine(DEFAULT_BASE_URL)


def _chat(messages, api_key, model, json_mode=False, timeout=TIMEOUT):
    """One completion through the default engine. Returns the assistant's text."""
    return _DEFAULT_ENGINE.chat(messages, api_key, model, json_mode, timeout)


def _resolve_chat(chat, engine):
    """The callable one translation should use.

    An injected `chat` beats everything, including an engine: the whole test
    suite passes a fake model in through this seam, and a fake has to stay
    interchangeable with a real transport.
    """
    if chat is not None:
        return chat
    return (engine or _DEFAULT_ENGINE).chat


# --- pure helpers ---------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _parse_tag_json(raw, wanted):
    """Pull {slug: translation} out of a model reply.

    Only the slugs that were asked for survive. A model that volunteers extra
    keys, or answers with the request echoed back, must not be able to put a
    tag that does not exist into the dictionary - the tag names are database
    keys elsewhere. Every value has to be a non-empty string.
    """
    text = (raw or "").strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    picked = _pick(parsed, wanted)
    if picked:
        return picked
    # Some replies wrap the mapping: {"translations": {...}}.
    for value in parsed.values():
        if isinstance(value, dict):
            picked = _pick(value, wanted)
            if picked:
                return picked
    return {}


def _pick(parsed, wanted):
    allow = set(wanted)
    return {key: value.strip() for key, value in parsed.items()
            if key in allow and isinstance(value, str) and value.strip()}


# --- translation ------------------------------------------------------------------

def _ask_for_tags(chunk, api_key, model, chat):
    reply = chat(
        [{"role": "system", "content": TAG_SYSTEM},
         {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)}],
        api_key, model, True)
    return _parse_tag_json(reply, chunk)


def _translate_chunk(chunk, api_key, model, chat, log):
    got = _ask_for_tags(chunk, api_key, model, chat)
    missing = [slug for slug in chunk if slug not in got]
    if missing:
        # Re-asking for just the stragglers costs a few tokens; re-sending the
        # whole chunk throws away what the first call already paid for.
        log("  %d 条没返回，单独重试" % len(missing))
        got.update(_ask_for_tags(missing, api_key, model, chat))
    return got


def translate_tags(slugs, api_key, model=DEFAULT_MODEL, chat=None, engine=None,
                   log=None):
    """{slug: translation} for as many of `slugs` as the model will give.

    Whatever is still missing is simply absent from the result - the caller
    falls back to the English slug rather than losing the whole batch.

    A free engine is refused rather than silently degraded: these ~120 strings
    are rendered on every card in the library, and a machine translator gives
    'netorare' a different rendering each time it is asked.
    """
    if engine is not None and not engine.can_json:
        raise TranslateError(TAG_NEEDS_JSON)
    chat = _resolve_chat(chat, engine)
    log = log or (lambda *a: None)
    slugs = list(slugs)
    out = {}
    for start in range(0, len(slugs), TAG_CHUNK):
        out.update(_translate_chunk(slugs[start:start + TAG_CHUNK],
                                    api_key, model, chat, log))
    return out


def translate_overview(text, api_key, model=DEFAULT_MODEL, chat=None, engine=None):
    chat = _resolve_chat(chat, engine)
    out = (chat([{"role": "system", "content": OVERVIEW_SYSTEM},
                 {"role": "user", "content": text}], api_key, model) or "").strip()
    if not out:
        raise TranslateError("模型返回了空译文")
    return out


# A version the model has to hand back untouched: either a prefix (v1.20, EP03,
# Chapter 2) or a dotted number (1.2.3). A bare integer deliberately does not
# count - "Princess 3" is part of a name, and flagging it would make the guard
# reject good translations.
_VERSION_TOKEN = re.compile(
    r"(?i)(?:\b(?:v|ver|rev|r|ep|ch|chapter|part|act|season)\s*\.?\s*\d+(?:[.\-_]\d+)*\b"
    r"|\b\d+\.\d+(?:[.\-_]\d+)*\b)")
_BRACKETED = re.compile(r"\[([^\]]*)\]")


def title_fragments(text):
    """The pieces of a title a translation must reproduce verbatim.

    Versions and bracketed studio names. Both are shown next to the title and
    would read as a data error if a translation touched them.
    """
    return _VERSION_TOKEN.findall(text or "") + _BRACKETED.findall(text or "")


def preserves_title_fragments(source, translated):
    return all(fragment in (translated or "") for fragment in title_fragments(source))


def translate_title(text, api_key, model=DEFAULT_MODEL, chat=None, engine=None):
    """The game's name in Chinese, or TitleGuardError if it cannot be trusted.

    Raises instead of falling back to the English source. That fallback used to
    be returned as if it were a translation, so it was cached as one: the panel
    kept showing English, the cache looked populated, and the name was never
    asked for again. The caller now decides what an untranslatable title means -
    it writes a marker saying so, which stays honest and still costs one request
    rather than one per visit.

    Empty replies raise the plain TranslateError: that is a bad key or a dead
    API, and the caller already has a path for showing it.
    """
    chat = _resolve_chat(chat, engine)
    out = (chat([{"role": "system", "content": TITLE_SYSTEM},
                 {"role": "user", "content": text}], api_key, model) or "").strip()
    if not out:
        raise TranslateError("模型返回了空译文")
    out = out.strip().strip('"“”\'').strip()
    if not out:
        raise TitleGuardError("模型只返回了一对引号")
    if out == (text or "").strip():
        # The model handing the English back untouched is the other way this
        # guard used to be defeated: it passed the fragment check trivially.
        raise TitleGuardError("模型把原名原样退了回来")
    if not preserves_title_fragments(text, out):
        raise TitleGuardError("版本号或社团名没有原样保留")
    return out


def probe(api_key, model=DEFAULT_MODEL, chat=None, engine=None):
    """One very short round trip, so a bad key or a dead network surfaces in the
    settings dialog instead of halfway through translating the library."""
    chat = _resolve_chat(chat, engine)
    if not api_key and getattr(engine, "needs_key", True):
        raise TranslateError("还没填 API Key")
    reply = chat([{"role": "user", "content": "回复两个字：正常"}],
                 api_key, model, False, PROBE_TIMEOUT)
    return (reply or "").strip()


# --- orchestration ----------------------------------------------------------------

def run_tag_translation(conn, api_key, model=DEFAULT_MODEL, limit=0, log=None,
                        chat=None, engine=None):
    """Translate every tag that has no cached translation yet, and store them."""
    log = log or (lambda *a: None)
    pending = slg_db.missing_tags(conn)
    if not pending:
        return {"pending": 0, "translated": 0, "missing": []}
    if limit:
        pending = pending[:limit]
    log("待翻译标签 %d 个" % len(pending))
    got = translate_tags(pending, api_key, model, chat=chat, engine=engine, log=log)
    for slug, text in sorted(got.items()):
        # missing_tags already skips rows that exist, so a hand-edited tag is not
        # in `pending` to begin with. This guard covers the other ordering: the
        # user renaming a tag while this batch is still in flight.
        slg_db.set_auto_translation(conn, "tag", slug, slug, text, engine=model)
    return {"pending": len(pending), "translated": len(got),
            "missing": [s for s in pending if s not in got]}


# --- CLI -----------------------------------------------------------------------

def _main(argv=None):
    # The console is cp936 and every line below is Chinese; slg_main sets this
    # for the packaged entry point, running this file directly needs it too.
    slg_util.fix_console()

    import argparse
    parser = argparse.ArgumentParser(prog="slgking translate",
                                     description="翻译标签、简介和游戏名")
    parser.add_argument("--probe", action="store_true",
                        help="只测连通性，不写库")
    parser.add_argument("--tags", action="store_true",
                        help="翻译所有还没翻过的标签")
    parser.add_argument("--overview", type=int, metavar="ID",
                        help="翻译某一款游戏的简介并打印（调试用）")
    parser.add_argument("--title", type=int, metavar="ID",
                        help="翻译某一款游戏的名称并打印（调试用）")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="配合 --tags，最多翻 N 个")
    parser.add_argument("--key", default=None,
                        help="API Key（默认读环境变量或软件里的设置）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--engine", default=None,
                        choices=[ENGINE_OPENAI, ENGINE_FREE],
                        help="覆盖软件里选的引擎，%s 不需要 Key" % ENGINE_FREE)
    args = parser.parse_args(argv)

    conn = slg_db.connect()
    try:
        config = resolve_config(conn, args.key)
        if args.model:
            config.model = args.model
        if args.engine:
            config.engine = args.engine
        engine = config.build()
        key, model = config.api_key, config.model
        if not key and engine.needs_key:
            print("没有 API Key。用 --key 传入、设环境变量 DEEPSEEK_API_KEY，"
                  "或在软件里打开「翻译设置…」填写。免费机翻不用 Key："
                  "加 --engine %s。" % ENGINE_FREE)
            return 2
        print("引擎：%s，模型：%s" % (config.describe(), model))

        if args.probe:
            print("连接正常，返回：%s" % probe(key, model, engine=engine))
            return 0

        if args.overview or args.title:
            game_id = args.overview or args.title
            row = slg_db.get_game(conn, game_id)
            if row is None:
                print("没有 id=%d 这款游戏。" % game_id)
                return 2
            if args.title:
                print(translate_title(row["title"], key, model, engine=engine))
                return 0
            if not row["overview"]:
                print("这款游戏还没有抓到简介，先跑一次同步。")
                return 2
            print(translate_overview(row["overview"], key, model, engine=engine))
            return 0

        if args.tags:
            summary = run_tag_translation(conn, key, model, engine=engine,
                                          limit=args.limit, log=print)
            if not summary["pending"]:
                print("标签都翻译过了（共 %d 个）。"
                      % slg_db.translation_counts(conn)["tags"])
                return 0
            print("翻译 %d/%d 个标签。" % (summary["translated"], summary["pending"]))
            if summary["missing"]:
                print("  未返回：%s" % ", ".join(summary["missing"]))
            return 0
    except TitleGuardError as exc:
        # Expected on names the model will not leave the studio name in: the
        # English name is the right thing to show, so this is not a failure.
        print("这个名称不适合翻译（%s），保持原文。" % exc)
        return 0
    except TranslateError as exc:
        print("翻译失败：%s" % exc)
        return 1
    finally:
        conn.close()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
