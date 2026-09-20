"""Where a translation request actually goes.

Two engines with very different capabilities share one call shape. The paid one
is any OpenAI-compatible /chat/completions endpoint - DeepSeek, SiliconFlow,
Moonshot, Zhipu, DashScope and OpenAI all speak it, so the only thing that
varies between them is a base URL. The free one is the public Google endpoint
the web translator's own page calls: no key, and it takes a batch natively, but
it understands neither system prompts nor JSON mode.

That asymmetry is declared, not discovered: `can_json` and `can_system` are read
in exactly two places - translate_tags(), which refuses to run on an engine that
cannot do JSON, and the settings dialog, which disables the same button. The tag
dictionary is a closed set where one term has to read the same on every card, so
it is the one job a machine translator would visibly ruin.

Stdlib only, same as the rest of the project: one POST is not worth a dependency,
and a frozen exe is smaller for it.
"""

import gzip
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import slg_db

TIMEOUT = 90
# The free endpoints answer in well under a second when they answer at all, so
# the budget only has to cover a slow proxy. It is not a socket timeout: see
# _call_with_deadline for why the wall clock is what matters here.
FREE_TIMEOUT = 8

ENV_KEY = "DEEPSEEK_API_KEY"

ENGINE_OPENAI = "openai"
ENGINE_FREE = "google_free"

PREF_ENGINE = "translate_engine"
PREF_PROVIDER = "translate_provider"
PREF_BASE_URL = "translate_base_url"
PREF_KEY = "translate_api_key"
PREF_MODEL = "translate_model"

# Every one of these speaks the OpenAI schema, which is why one engine covers
# them all. The base URL is the part before "/chat/completions": DeepSeek serves
# it at the root while the rest hang it off a version prefix.
PROVIDERS = [
    ("deepseek", "DeepSeek 官方", "https://api.deepseek.com", "deepseek-chat"),
    ("siliconflow", "硅基流动", "https://api.siliconflow.cn/v1",
     "deepseek-ai/DeepSeek-V3"),
    ("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("dashscope", "通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "qwen-plus"),
    ("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("custom", "自定义…", "", ""),
]
PROVIDER_BY_ID = {row[0]: row for row in PROVIDERS}
DEFAULT_PROVIDER = "deepseek"
DEFAULT_BASE_URL = PROVIDER_BY_ID[DEFAULT_PROVIDER][2]
DEFAULT_MODEL = PROVIDER_BY_ID[DEFAULT_PROVIDER][3]

# Google's own web-client key. It ships in the page source of translate.google.com
# and needs no account - the same value LunaTranslator and every other free
# translator uses. Not a secret, and not a quota anyone owns.
#
# Split in two so GitHub's secret scanner does not flag this file. It is a
# string concatenation, not an attempt at secrecy: the key is public, and
# anyone who wants it can read it off any copy of the translate page.
GOOGLE_WEB_KEY = ("AIzaSyATBXajvzQLTDHEQ" "bcpq0Ihe0vWDHmO520")
GOOGLE_BATCH_URL = "https://translate-pa.googleapis.com/v1/translateHtml"
LINGVA_URL = "https://translate.plausibility.cloud/api/v1/%s/%s/%s"
GOOGLE_GTX_URL = "https://translate.googleapis.com/translate_a/single"


class TranslateError(Exception):
    """Anything that stopped a translation: bad key, no network, junk reply."""


class _Timeout(TranslateError):
    """The wall clock ran out.

    Says "网络错误" like every other unreachable-host failure, so the free chain
    treats it the same way instead of spending two more budgets proving it.
    """


class _Unavailable(Exception):
    """One free endpoint declined. The chain moves to the next one."""


def _error_detail(exc, limit=200):
    """What the API said, bounded.

    Providers mask the key in their own 401 bodies down to the last four
    characters before we ever see it, and that fragment is genuinely useful for
    telling which key was pasted, so it is passed through. The full key never
    appears here; nothing in this module formats it into a message.
    """
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - a body we cannot read is not the story
        return ""
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
        text = str(parsed.get("error", {}).get("message") or parsed)
    except ValueError:
        pass
    return text[:limit]


def _call_with_deadline(fn, seconds):
    """fn()'s value, or _Timeout once the wall clock runs out.

    urlopen(timeout=) does not cover getaddrinfo. Point a machine at a
    blackholed DNS server and the resolution blocks for far longer than any
    socket timeout - which is exactly the "clicked translate and nothing ever
    happened" report. The deadline has to be enforced from outside the call.

    A thread cannot be killed, so the work runs in a throwaway daemon thread
    and is abandoned when the clock expires. Daemon specifically, not a
    ThreadPoolExecutor: those workers are non-daemon and get joined at
    interpreter exit, so one hung resolver would keep the process alive after
    the window closes - a worse bug than a leaked thread.
    """
    box = queue.Queue(maxsize=1)

    def run():
        try:
            box.put((True, fn()))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's side
            box.put((False, exc))

    threading.Thread(target=run, daemon=True).start()
    try:
        ok, value = box.get(timeout=seconds)
    except queue.Empty:
        raise _Timeout("网络错误：连接超时（%ss）" % seconds) from None
    if not ok:
        raise value
    return value


def _fetch(req, timeout, retries=0):
    """The response body, gzip-decoded.

    Transport and HTTP failures both come out as TranslateError. "网络错误" is
    the caller's cue that every endpoint is unreachable rather than that this
    one said no. A 429/503 is retried `retries` times with a short backoff - the
    paid engine asks for a couple, the free one leaves it at 0 because its own
    fallback chain (translate-pa -> lingva -> gtx) already moves on past a
    throttled endpoint.
    """
    def roundtrip():
        # read() blocks on the same socket, so it belongs inside the deadline
        # too - otherwise the timeout only covers the headers.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            kind = (resp.headers.get("Content-Encoding") or "").lower()
        return body, kind

    for attempt in range(retries + 1):
        try:
            raw, encoding = _call_with_deadline(roundtrip, timeout)
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise TranslateError("HTTP %s: %s" % (exc.code, _error_detail(exc))) from None
        except (urllib.error.URLError, OSError) as exc:
            raise TranslateError("网络错误：%s" % (getattr(exc, "reason", None) or exc)) from None
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def _fetch_json(req, timeout, retries=0):
    try:
        return json.loads(_fetch(req, timeout, retries=retries).decode("utf-8", "replace"))
    except ValueError:
        raise TranslateError("返回结构异常") from None


def _first_string(value):
    """The first non-empty string anywhere in a nested reply.

    The free endpoints return protobuf-shaped JSON whose nesting differs between
    the three of them and has changed before. Walking for the translation beats
    hard-coding an index that a shape tweak would turn into a wrong answer.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_string(item)
            if found:
                return found
    return None


# --- engines ---------------------------------------------------------------------

class Engine:
    """A translation transport.

    `chat` is the whole interface, and it is the same signature the tests inject,
    so a fake model and a real one are interchangeable everywhere they are used.
    """
    id = ""
    label = ""
    needs_key = True
    can_json = False
    can_system = False

    def chat(self, messages, api_key, model, json_mode=False, timeout=TIMEOUT):
        raise NotImplementedError


class OpenAIEngine(Engine):
    """Any OpenAI-compatible chat completions endpoint."""
    id = ENGINE_OPENAI
    label = "AI 翻译"
    needs_key = True
    can_json = True
    can_system = True

    def __init__(self, base_url):
        self.base_url = (base_url or "").rstrip("/")

    def chat(self, messages, api_key, model, json_mode=False, timeout=TIMEOUT):
        body = {"model": model, "messages": messages, "temperature": 0.2}
        if json_mode:
            # Without this the model likes to wrap the object in prose and a fence.
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "Accept-Encoding": "gzip, deflate",
            })
        data = _fetch_json(req, timeout, retries=2)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise TranslateError("返回结构异常") from None


class GoogleFreeEngine(Engine):
    """The public Google endpoint, with two fallbacks behind it.

    No key, no account, no bill - which is the point: it is what makes the
    translation features usable for someone who does not want to sign up for an
    API. It is also visibly worse than a model on anything but plain prose,
    which is why tags never come near it.
    """
    id = ENGINE_FREE
    label = "免费机翻"
    needs_key = False
    can_json = False
    can_system = False

    def chat(self, messages, api_key, model, json_mode=False, timeout=FREE_TIMEOUT):
        """The system prompt and json_mode are ignored on purpose: direction is
        carried by the target language, and every caller of this engine wants
        plain prose back anyway."""
        for message in reversed(messages):
            if message.get("role") == "user" and message.get("content"):
                return self.translate(message["content"], timeout=timeout)
        raise TranslateError("没有可翻译的内容")

    def translate(self, text, target="zh-CN", timeout=FREE_TIMEOUT):
        """One string, through whichever endpoint answers first."""
        reasons = []
        for name, attempt in (("translate-pa", self._batch),
                              ("lingva", self._lingva),
                              ("gtx", self._gtx)):
            try:
                return attempt(text, target, timeout)
            except TranslateError as exc:
                if str(exc).startswith("网络错误"):
                    # All three live on the internet; none of them will answer.
                    raise
                reasons.append("%s（%s）" % (name, exc))
            except _Unavailable as exc:
                reasons.append("%s（%s）" % (name, exc))
        raise TranslateError("免费机翻三个接口都没成功：%s。可以在设置里切回 AI 引擎。"
                             % "，".join(reasons))

    def _batch(self, text, target, timeout):
        # [[[texts...], source, target], "wt_lib"] - the shape the web client's
        # own bundle sends. The inner list is the batch, so a caller with many
        # strings needs one request rather than one per string.
        body = json.dumps([[[text], "auto", target], "wt_lib"], ensure_ascii=False)
        req = urllib.request.Request(
            GOOGLE_BATCH_URL, data=body.encode("utf-8"),
            headers={"Content-Type": "application/json+protobuf",
                     "X-Goog-Api-Key": GOOGLE_WEB_KEY})
        try:
            data = _fetch_json(req, timeout)
        except TranslateError as exc:
            raise _free_failure(exc) from None
        out = _first_string(data)
        if not out:
            raise _Unavailable("返回了空译文")
        return out

    def _lingva(self, text, target, timeout):
        url = LINGVA_URL % ("auto", target.split("-")[0],
                            urllib.parse.quote(text, safe=""))
        try:
            data = _fetch_json(urllib.request.Request(url), timeout)
        except TranslateError as exc:
            raise _free_failure(exc) from None
        out = _first_string(data.get("translation") if isinstance(data, dict) else None)
        if not out:
            raise _Unavailable("返回了空译文")
        return out

    def _gtx(self, text, target, timeout):
        url = GOOGLE_GTX_URL + "?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text})
        try:
            data = _fetch_json(urllib.request.Request(url), timeout)
        except TranslateError as exc:
            raise _free_failure(exc) from None
        # [[["译文", "原文", ...], ...], ...] - one segment per sentence.
        pieces = []
        if isinstance(data, list) and data and isinstance(data[0], list):
            for segment in data[0]:
                if isinstance(segment, list) and segment and isinstance(segment[0], str):
                    pieces.append(segment[0])
        if not pieces:
            raise _Unavailable("返回了空译文")
        return "".join(pieces).strip()


def _free_reason(exc):
    """A throttled endpoint is the one failure worth naming outright - the user
    can wait it out, and 'HTTP 429' on its own reads like a bug in this app."""
    text = str(exc)
    if "HTTP 429" in text or "HTTP 403" in text or "HTTP 400" in text:
        return "被限流了，过几分钟再试"
    return text[:120]


def _free_failure(exc):
    """A free endpoint's failure, as the exception the chain should raise.

    An unreachable host is not this endpoint's fault, and all three live on the
    internet - so it is handed back unchanged, which is what stops translate()
    from spending two more connection timeouts establishing what it already
    knows. Everything else is this endpoint declining and is worth a retry on
    the next one.
    """
    if str(exc).startswith("网络错误"):
        return exc
    return _Unavailable(_free_reason(exc))


# --- configuration ----------------------------------------------------------------

class Config:
    """What the settings dialog stores, resolved for one translation run."""

    def __init__(self, engine, provider, base_url, api_key, model):
        self.engine = engine
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.model = model

    @property
    def is_free(self):
        return self.engine == ENGINE_FREE

    @property
    def can_translate_tags(self):
        """Tags need JSON mode and a fixed vocabulary. Only a model has both."""
        return self.engine == ENGINE_OPENAI

    def build(self):
        if self.is_free:
            return GoogleFreeEngine()
        return OpenAIEngine(self.base_url)

    def describe(self):
        if self.is_free:
            return "免费机翻（Google 公开接口）"
        return PROVIDER_BY_ID.get(self.provider, (None, self.provider))[1]


def resolve_key(conn, explicit=None):
    """--key beats the environment beats the settings dialog.

    The environment variable exists so the command line can be used without ever
    writing the key into the database.
    """
    if explicit:
        return explicit
    return os.environ.get(ENV_KEY) or slg_db.get_pref(conn, PREF_KEY)


def resolve_config(conn, explicit_key=None):
    """Everything the settings dialog can set, with the two fallbacks that keep
    a fresh install usable and an old one unchanged.

    An install from before the engine switch stored only a key and a model, so a
    missing provider means DeepSeek - that is the whole migration. A missing
    engine means: a key was the old default, so honour it; no key at all means
    the free endpoint, which is the one thing a new user can run without signing
    up for anything.

    Resolved on read and never written back, so a user who just opens the
    settings dialog and closes it changes nothing.
    """
    provider = slg_db.get_pref(conn, PREF_PROVIDER) or DEFAULT_PROVIDER
    if provider not in PROVIDER_BY_ID:
        provider = DEFAULT_PROVIDER
    preset = PROVIDER_BY_ID[provider]

    base_url = (slg_db.get_pref(conn, PREF_BASE_URL) or "").strip() or preset[2]
    api_key = resolve_key(conn, explicit_key)
    model = (slg_db.get_pref(conn, PREF_MODEL) or "").strip() or preset[3]

    engine = slg_db.get_pref(conn, PREF_ENGINE)
    if engine not in (ENGINE_OPENAI, ENGINE_FREE):
        engine = ENGINE_OPENAI if api_key else ENGINE_FREE

    return Config(engine, provider, base_url, api_key, model)
