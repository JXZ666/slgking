"""Bounded public catalogue adapters for server-side source aggregation.

Only public listing/detail metadata is read. This module does not fetch images,
attachments, download links, or whole-page archives, and it never executes site
scripts. Each adapter checks robots.txt, caps responses, rate-limits per host,
and records its own cursor/error state in the server database.
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
from html.parser import HTMLParser
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import slg_db
import slg_source_covers

F95_HOST = "f95zone.to.it"
F95_BASE = "https://f95zone.to.it"
F95_LISTING = F95_BASE + "/tag/strategy/"
# These two tags were linked from the site's configured /genres/ directory
# and their listing pages returned successfully during the bounded probe.
F95_EXTRA_CATEGORIES = (
    ("adventure", F95_BASE + "/tag/adventure/", "Adventure"),
    ("dating-sim", F95_BASE + "/tag/dating-sim/", "Dating Sim"),
)
F95_CATEGORY_STATE_PREFIX = "f95-category-cursors:v1:"
F95_CATEGORY_PAGE_PATH = re.compile(
    r"^/tag/([a-z0-9-]+)/page/(\d+)/?$", re.I)
ZY_HOST = "zyoudao.net"
ZY_BASE = "https://zyoudao.net"
ZY_API = ZY_BASE + "/wp-json/wp/v2/posts"
ZY_FIELDS = "id,slug,link,title,excerpt,content,featured_media,modified,modified_gmt,categories,tags,_links,_embedded"
USER_AGENT = "SLGKing public catalogue indexer/0.24"
MAX_RESPONSE_BYTES = 512 * 1024
TIMEOUT_SECONDS = 22
MIN_HOST_INTERVAL = 1.0
MAX_ZY_PAGES_PER_RUN = 4
ZY_PER_PAGE = 25
MAX_F95_DETAILS_PER_RUN = 24
MAX_F95_ITEMS_PER_LISTING = 12
# Before adding extra listings, a full Strategy pass could use one robots
# request, two Strategy listings, and 24 details. Keep that total ceiling.
MAX_F95_TOTAL_REQUESTS_PER_RUN = 27
TEXT_MAX = 1600
ZY_OVERVIEW_MAX = 1200
ZY_CONTENT_SCAN_MAX = 12000
GAME_PATH = re.compile(r"^/([a-z0-9][a-z0-9-]{4,})/?$", re.I)
PAGE_PATH = re.compile(r"^/tag/strategy/page/(\d+)/?$", re.I)
IGNORED_SLUGS = {"hot-games", "genres", "tag", "category", "author",
                 "page", "wp-admin", "wp-json", "search"}


class SourceError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HTML(HTMLParser):
    """Extract a small set of visible labels without retaining source HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.h1 = []
        self.anchors = []
        self.text = []
        self.meta = {}
        self._hidden = 0
        self._in_title = False
        self._in_h1 = 0
        self._href = None
        self._anchor_text = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "template"):
            self._hidden += 1
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 += 1
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            content = values.get("content") or ""
            if key in ("description", "og:title", "og:description"):
                self.meta[key] = content[:1000]
        if tag == "a":
            self._href = values.get("href")
            self._anchor_text = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template") and self._hidden:
            self._hidden -= 1
        if tag == "title":
            self._in_title = False
        if tag == "h1" and self._in_h1:
            self._in_h1 -= 1
        if tag == "a" and self._href is not None:
            label = " ".join(" ".join(self._anchor_text).split())[:180]
            self.anchors.append((label, self._href))
            self._href = None
            self._anchor_text = []

    def handle_data(self, value):
        if self._hidden:
            return
        value = " ".join(value.split())
        if not value:
            return
        self.text.append(value[:400])
        if self._in_title:
            self.title.append(value)
        if self._in_h1:
            self.h1.append(value)
        if self._href is not None:
            self._anchor_text.append(value)


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            value = " ".join(value.split())
            if value:
                self.parts.append(value)


class _ZyoudaoText(HTMLParser):
    """Extract bounded article paragraphs while skipping unsafe/noisy blocks."""

    _BLOCK_TAGS = {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
        "hr", "li", "ol", "p", "pre", "section", "table", "td", "th",
        "tr", "ul",
    }
    _HIDDEN_TAGS = {
        "button", "form", "iframe", "noscript", "script", "style", "svg",
        "template", "video", "audio",
    }
    _HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _DOWNLOAD_LINK = re.compile(
        r"(?:download|attachment|\.zip(?:$|[?#])|\.rar(?:$|[?#])|\.7z(?:$|[?#])|"
        r"\.exe(?:$|[?#])|\.apk(?:$|[?#])|\.torrent(?:$|[?#])|magnet:|"
        r"/wp-content/uploads/|/files?/|baidu\.com|pan\.quark|aliyundrive|alipan|"
        r"mega\.nz|mediafire\.com|"
        r"pixeldrain\.com|drive\.google|1drv\.ms|telegram\.me|t\.me/)", re.I)
    _SECTION_END = re.compile(
        r"^(?:download|downloads?|download\s+links?|links?|mirrors?|"
        r"下载(?:地址|链接|资源|内容)?|网盘(?:地址|链接)?|资源下载|游戏资源|"
        r"解压密码|提取码|文件列表|补丁下载|赞助|捐赠|广告|推广|购买方式|"
        r"购买地址|加群|官方群|会员专享)"
        r"(?:\s*[:：].*)?$", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._current = []
        self._hidden_depth = 0
        self._drop_section = False
        self._heading = None
        self._links = []
        self._bad_link_depth = 0

    def _text(self):
        return " ".join(" ".join(self._current).split())

    def _flush(self):
        value = self._text()
        if value and not self._drop_section and not self._bad_link_depth:
            value = re.sub(r"https?://\S+|www\.\S+", "", value, flags=re.I)
            value = " ".join(value.split()).strip(" \t\r\n-—|·")
            if value and not self._SECTION_END.match(value):
                lowered = value.casefold()
                if not re.match(
                    r"^(?:download|下载|网盘|解压密码|提取码|密码|广告|推广|赞助|购买|加群|"
                        r"百度网盘|夸克网盘|阿里云盘|迅雷|magnet:|torrent)",
                        lowered, re.I):
                    self.blocks.append(value)
        self._current = []

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        if self._hidden_depth:
            return
        if tag in self._HEADING_TAGS:
            self._flush()
            self._drop_section = False
            self._heading = tag
            self._current = []
        elif tag in self._BLOCK_TAGS:
            self._flush()
        if tag == "a":
            values = dict(attrs)
            href = str(values.get("href") or "")
            bad = bool(self._DOWNLOAD_LINK.search(href))
            self._links.append(bad)
            if bad:
                self._bad_link_depth += 1

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            if self._hidden_depth:
                self._hidden_depth -= 1
            return
        if self._hidden_depth:
            return
        if tag == "a" and self._links:
            if self._links.pop():
                self._bad_link_depth = max(0, self._bad_link_depth - 1)
        if tag in self._HEADING_TAGS and self._heading == tag:
            heading = self._text()
            if heading and not self._SECTION_END.match(heading):
                self._flush()
            else:
                self._current = []
                self._drop_section = True
            self._heading = None
        elif tag in self._BLOCK_TAGS:
            self._flush()

    def handle_data(self, value):
        if self._hidden_depth or self._drop_section or self._bad_link_depth:
            return
        value = " ".join(str(value).split())
        if value:
            self._current.append(value)


def _zy_blocks(value):
    parser = _ZyoudaoText()
    try:
        parser.feed(str(value or "")[:MAX_RESPONSE_BYTES])
        parser.close()
    except Exception:
        return []
    parser._flush()
    return parser.blocks


_ZY_VERSION = re.compile(
    r"(?<![A-Za-z0-9])(?:version\s*[:：]?\s*|ver\.?\s*|v\s*)"
    r"([0-9]+(?:\.[0-9A-Za-z]+){1,4}(?:[-+][A-Za-z0-9.-]+)?)", re.I)
_ZY_VERSION_LABEL = re.compile(
    r"(?:版本|version|ver\.?)\s*[:：]?\s*(?:v\s*)?"
    r"([0-9]+(?:\.[0-9A-Za-z]+){1,4}(?:[-+][A-Za-z0-9.-]+)?)", re.I)
_ZY_VERSION_BARE = re.compile(
    r"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-+][A-Za-z0-9.-]+)?)(?![A-Za-z0-9])", re.I)
_ZY_VERSION_BRACKET = re.compile(
    r"[\[【（(]\s*v?\s*([0-9]+(?:\.[0-9]+){1,4}(?:[-+][A-Za-z0-9.-]+)?)"
    r"\s*[\]】）)]", re.I)
_ZY_DEVELOPER_LABEL = re.compile(
    r"(?:开发商|开发团队|开发组|制作组|制作社团|社团|作者|developer|studio|team|creator)"
        r"\s*[:：]\s*([^\n|；;\]】）)]{1,160})", re.I)
_ZY_ENGINE_LABEL = re.compile(
    r"(?:引擎|engine)\s*[:：]\s*([^\n|；;]{1,100})", re.I)
_ZY_NOISE_BRACKET = re.compile(
    r"^(?:slg|rpg|adv|act|模拟|策略|汉化(?:版)?|官方汉化(?:版)?|官方中文版|官方中文|"
    r"机翻|ai汉化|官中|简中|繁中|简体中文|繁体中文|中文汉化|"
    r"(?:official\s+)?chinese(?:\s+(?:version|translation|patch|localization))?|"
    r"(?:pc|windows|安卓|android)(?:\s*[+/,&]\s*(?:pc|windows|安卓|android))*|steam|"
    r"最新|更新|完结|\d+(?:\.\d+)?\s*(?:mb|gb|tb|g|m))$",
    re.I)
_ZY_FORMAT_SUFFIX = re.compile(
    r"(?:\s+|[-|—–·]\s*|(?<=】))(?:HTML\s*汉化(?:版)?|精翻汉化|步兵版|骑兵版|"
    r"(?:Steam\s*)?官方汉化(?:版)?|(?:Steam\s*)?官方中文版|"
    r"(?:Steam\s*)?官方中文|官方汉化(?:版)?|官方中文版|官方中文|汉化(?:版)?|"
    r"简体中文|繁体中文|简中|繁中|官中|机翻|ai汉化|"
    r"(?:official\s+)?chinese(?:\s+(?:version|translation|patch|localization))?|"
    r"(?:pc|windows|安卓|android)(?:\s*[+/,&]\s*(?:pc|windows|安卓|android))?|"
    r"steam|最新版|完结)\s*$|(?:\s+\d+(?:\.\d+)?\s*(?:mb|gb|tb|g|m))\s*$",
    re.I)
_ZY_STUDIO_SUFFIX = re.compile(
    r"(?:studio|game\s+studio|games?|team|developer|dev|"
    r"工作室|制作组|制作社团|开发商|开发团队|开发组|社团)$", re.I)
_ZY_BY_SUFFIX = re.compile(
    r"\s+(?:[-|—–·]\s*)?(?i:by)\s+([A-Z][A-Za-z0-9&'.-]*"
    r"(?:\s+[A-Z][A-Za-z0-9&'.-]*){0,4})\s*$")


def _zy_value(match, maximum):
    if not match:
        return None
    value = " ".join(match.group(1).split()).strip(" \t\r\n:：,，。;；|·-—")
    if not value or re.match(r"^(?:暂无|未知|无|待定|none|n/?a)$", value, re.I):
        return None
    # A metadata field copied as one long paragraph must not absorb the next
    # labelled field or a promotional/download tail.
    value = re.split(
        r"\s+(?=(?:版本|version|ver\.?|开发商|开发团队|制作组|社团|作者|"
        r"developer|studio|team|引擎|engine)\s*[:：])", value, maxsplit=1,
        flags=re.I)[0]
    return value[:maximum] or None


def _zy_title_developer(title):
    """Extract explicit credits or organization-marked title suffixes only."""
    bracket = re.compile(r"[\[【（(]([^\]】）)]{1,100})[\]】）)]")
    for match in reversed(list(bracket.finditer(title))):
        inner = " ".join(match.group(1).split())
        labelled = _ZY_DEVELOPER_LABEL.search(inner)
        if labelled:
            value = _zy_value(labelled, 160)
            if value:
                return title[:match.start()] + " " + title[match.end():], value
        by_credit = re.match(
            r"^(?i:by)\s+([A-Z][A-Za-z0-9&'.-]*(?:\s+[A-Z][A-Za-z0-9&'.-]*){0,4})$",
            inner)
        if by_credit:
            return title[:match.start()] + " " + title[match.end():], by_credit.group(1)[:160]
        candidate = re.sub(r"^by\s+", "", inner, flags=re.I).strip()
        if _ZY_STUDIO_SUFFIX.search(candidate):
            return title[:match.start()] + " " + title[match.end():], candidate[:160]

    # A plain “(Alice)” is ambiguous and stays in the title. “by Alice” is an
    # explicit credit; a separator followed by “Blue Studio” is also clear.
    by_match = _ZY_BY_SUFFIX.search(title)
    if by_match:
        return title[:by_match.start()].rstrip(" -|—–·"), by_match.group(1)[:160]
    suffix = re.search(
        r"\s*[-|—–·]\s*((?:[A-Z][A-Za-z0-9&'.-]*\s+){0,4}"
        r"[A-Z][A-Za-z0-9&'.-]*(?:\s+(?:studio|games?|team|developer|dev))|"
        r"[\u3400-\u9fffA-Za-z0-9·_-]{2,80}(?:工作室|制作组|开发商|开发团队|社团))\s*$",
        title, re.I)
    if suffix:
        return title[:suffix.start()].rstrip(" -|—–·"), suffix.group(1)[:160]
    return title, None


def _zy_clean_format_suffix(title):
    """Strip only recognizable trailing localization/platform/size labels."""
    value = title
    for _ in range(5):
        cleaned = _ZY_FORMAT_SUFFIX.sub("", value).rstrip(" -|—–·")
        if cleaned == value:
            break
        value = cleaned
    return value


def _zy_strip_genre_prefix(title):
    """Return the bracketed Zyoudao game name, dropping catalogue fields.

    Zyoudao prefixes are an open-ended set (genre, style, platform, etc.), so
    maintaining a vocabulary of every possible label is brittle. In this
    source's title convention, a full-width ``【...】`` group marks the game
    name; any text before that group is catalogue metadata. Limit the prefix
    length and require a balanced, non-empty name group so a sentence or a
    malformed title is not mistaken for a catalogue prefix. Ordinary titles
    without this explicit marker are left untouched.
    """
    title = (title or "").strip()

    # Some feeds place a known category in its own leading bracket field,
    # e.g. ``【SLG】【Game Name】``. Skip only fields already classified as
    # release noise; a leading bracket containing a real name remains intact.
    while True:
        leading = re.match(r"^【([^【】\r\n]{1,80})】\s*", title)
        if not leading or not _ZY_NOISE_BRACKET.fullmatch(leading.group(1).strip()):
            break
        title = title[leading.end():]

    game_name = re.search(r"【([^【】\r\n]{1,240})】", title)
    if not game_name or game_name.start() == 0:
        return title

    prefix = title[:game_name.start()].strip()
    # A catalogue label is short and field-like. Keep long prose or malformed
    # source titles for manual review instead of chopping them speculatively.
    if (not prefix or len(prefix) > 80 or
            re.search(r"[。！？!?；;\r\n]", prefix)):
        return title
    return title[game_name.start():].lstrip()


def _zy_metadata(title, excerpt, content):
    """Return conservative game-name metadata and a bounded plain synopsis."""
    raw_title = _plain(title, 300)
    excerpt_blocks = _zy_blocks(excerpt)
    body_blocks = _zy_blocks(content)
    source_text = "\n".join((excerpt_blocks + body_blocks)[:80])[:ZY_CONTENT_SCAN_MAX]

    version_match = (_ZY_VERSION.search(raw_title) or
                     _ZY_VERSION_LABEL.search(raw_title) or
                     _ZY_VERSION_BRACKET.search(raw_title) or
                     _ZY_VERSION_BARE.search(raw_title))
    if not version_match:
        version_match = _ZY_VERSION_LABEL.search(source_text)
    version = _zy_value(version_match, 100)

    title_without_format = _zy_clean_format_suffix(raw_title)
    developer_match = _ZY_DEVELOPER_LABEL.search(raw_title + "\n" + source_text)
    developer = _zy_value(developer_match, 160)
    title_without_developer, suffix_developer = _zy_title_developer(title_without_format)
    if not developer:
        developer = suffix_developer
    engine_match = _ZY_ENGINE_LABEL.search(source_text)
    engine = _zy_value(engine_match, 100)

    game_title = _zy_strip_genre_prefix(title_without_developer)
    # Remove only explicit version labels / versions with a v or ver prefix.
    if _ZY_VERSION.search(game_title):
        game_title = _ZY_VERSION.sub(" ", game_title, count=1)
    elif _ZY_VERSION_LABEL.search(game_title):
        game_title = _ZY_VERSION_LABEL.sub(" ", game_title, count=1)
    else:
        game_title = _ZY_VERSION_BARE.sub(" ", game_title, count=1)
    game_title = _ZY_VERSION_BRACKET.sub(" ", game_title)
    # A bracket containing only a recognised release/platform/translation tag
    # is metadata; unknown bracket text is retained as part of the title.
    bracket = re.compile(r"[\[【（(]([^\]】）)]{1,100})[\]】）)]")
    def clean_bracket(match):
        inner = " ".join(match.group(1).split())
        if _ZY_VERSION.fullmatch(inner) or _ZY_NOISE_BRACKET.fullmatch(inner):
            return " "
        return match.group(0)
    game_title = bracket.sub(clean_bracket, game_title)
    # Strip explicitly labelled developer suffixes from the canonical title;
    # preserve the untouched rendered title separately for admin review.
    game_title = _ZY_DEVELOPER_LABEL.sub(" ", game_title)
    game_title = re.sub(r"[\[【（(]\s*[\]】）)]", " ", game_title)
    game_title = re.sub(r"\s*[-|—–]\s*(?:zyoudao(?:\.net)?|dikgames|f95zone(?:\.to\.it)?)\s*$",
                        "", game_title, flags=re.I)
    # Release status is commonly attached directly to the version with no
    # separator (for example ``v1.0.1完结``); keep chapter/season numbers but
    # drop that final status word after the version has been removed.
    game_title = re.sub(r"\s*完结\s*$", "", game_title)
    game_title = re.sub(r"^[\s:：|·-]+|[\s:：|·-]+$", "", game_title)
    game_title = " ".join(game_title.split())[:300]
    if not game_title:
        game_title = raw_title

    summary_blocks = []
    seen = set()
    for block in excerpt_blocks + body_blocks:
        value = " ".join(block.split())
        if not value or len(value) < 12:
            continue
        if value.casefold() == raw_title.casefold() or value.casefold() == game_title.casefold():
            continue
        if (_ZY_DEVELOPER_LABEL.match(value) or _ZY_VERSION_LABEL.match(value) or
                _ZY_ENGINE_LABEL.match(value)):
            continue
        key = re.sub(r"[\W_]+", "", value.casefold())
        if key and key not in seen:
            seen.add(key)
            summary_blocks.append(value)
    overview = " ".join(summary_blocks).strip()[:ZY_OVERVIEW_MAX] or None
    return game_title, version, developer, engine, overview


def _plain(value, maximum=TEXT_MAX):
    parser = _TextOnly()
    try:
        parser.feed(str(value or ""))
    except Exception:
        return ""
    return " ".join(" ".join(parser.parts).split())[:maximum]


def _host(url):
    value = (urlparse(url).hostname or "").casefold().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def _safe_https(url, host, *, keep_query=False):
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    candidate_host = (parsed.hostname or "").casefold().rstrip(".")
    if candidate_host == "www." + host:
        candidate_host = host
        parsed = parsed._replace(netloc=host)
    if (parsed.scheme != "https" or candidate_host != host
            or parsed.username or parsed.password or port not in (None, 443)):
        return None
    return parsed._replace(query=parsed.query if keep_query else "",
                           fragment="").geturl()


def _safe_log_url(url):
    try:
        parsed = urlparse(url)
        return parsed._replace(query="", fragment="").geturl()
    except (TypeError, ValueError):
        return "external source URL"


class PublicSourceClient:
    def __init__(self):
        self.opener = build_opener(_NoRedirect())
        self.last_request = {}
        self.robots = {}
        self.host_delays = {}
        self.last_total_pages = None
        self.requests = 0

    def get(self, url, host, accept="text/html,application/json,text/plain",
            *, keep_query=False):
        # Source links lose tracking parameters. The WordPress API below is the
        # one caller whose own bounded page/filter parameters must survive.
        if keep_query and (host != ZY_HOST or
                           urlparse(url).path != "/wp-json/wp/v2/posts"):
            raise SourceError("query parameters are only allowed on the WordPress feed")
        url = _safe_https(url, host, keep_query=keep_query)
        if not url or _host(url) != host or not url.startswith("https://"):
            raise SourceError("refused URL outside source host")
        interval = self.host_delays.get(host, MIN_HOST_INTERVAL)
        delay = interval - (time.monotonic() - self.last_request.get(host, 0))
        if host in self.last_request and delay > 0:
            time.sleep(delay)
        req = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Encoding": "identity",
        })
        self.requests += 1
        self.last_request[host] = time.monotonic()
        try:
            with self.opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
                self.last_total_pages = None
                total_pages = resp.headers.get("X-WP-TotalPages")
                if total_pages:
                    try:
                        self.last_total_pages = max(0, int(total_pages))
                    except ValueError:
                        pass
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > MAX_RESPONSE_BYTES:
                    raise SourceError("response exceeds 512 KiB limit")
                payload = resp.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise SourceError("HTTP %s for %s" %
                              (exc.code, _safe_log_url(url))) from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise SourceError("request failed: %s" % type(exc).__name__) from None
        if status != 200:
            raise SourceError("HTTP %s for %s" %
                              (status, _safe_log_url(url)))
        if len(payload) > MAX_RESPONSE_BYTES:
            raise SourceError("response exceeds 512 KiB limit")
        return content_type.casefold(), payload

    def check_robots(self, base, path, host):
        if host not in self.robots:
            content_type, raw = self.get(base + "/robots.txt", host,
                                         "text/plain,*/*")
            if "text" not in content_type:
                raise SourceError("robots.txt is not plain text")
            text = raw.decode("utf-8", "replace")
            groups = []
            agents, rules, delay = [], [], MIN_HOST_INTERVAL
            has_directives = False
            for raw_line in text.splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                key, value = (part.strip() for part in line.split(":", 1))
                key = key.casefold()
                if key == "user-agent":
                    if agents and has_directives:
                        groups.append((agents, rules, delay))
                        agents, rules, delay = [], [], MIN_HOST_INTERVAL
                        has_directives = False
                    agents.append(value.casefold())
                elif agents and key in ("allow", "disallow") and value:
                    rules.append((key, value))
                    has_directives = True
                elif agents and key == "crawl-delay":
                    has_directives = True
                    try:
                        delay = max(delay, min(30.0, float(value)))
                    except ValueError:
                        pass
            if agents:
                groups.append((agents, rules, delay))
            named = [(group_rules, group_delay) for group_agents, group_rules,
                     group_delay in groups if any(
                         agent != "*" and agent in USER_AGENT.casefold()
                         for agent in group_agents)]
            selected = named or [(group_rules, group_delay) for group_agents,
                                 group_rules, group_delay in groups
                                 if "*" in group_agents]
            rules = [rule for group_rules, _ in selected for rule in group_rules]
            delay = max((group_delay for _, group_delay in selected),
                        default=MIN_HOST_INTERVAL)
            self.robots[host] = (rules, delay)
        rules, delay = self.robots[host]
        self.host_delays[host] = max(MIN_HOST_INTERVAL, delay)
        best = None
        for action, pattern in rules:
            regex = "^" + re.escape(pattern).replace(r"\*", ".*")
            if pattern.endswith("$"):
                regex = regex[:-2] + "$"
            if re.match(regex, path):
                candidate = (len(pattern.rstrip("$")), action == "allow")
                if best is None or candidate > best:
                    best = candidate
        if best and not best[1]:
            raise SourceError("robots.txt disallows %s" % path)
        return delay

    def html(self, url, host):
        content_type, raw = self.get(url, host, "text/html,application/xhtml+xml")
        if "html" not in content_type:
            raise SourceError("expected an HTML listing/detail page")
        parser = _HTML()
        parser.feed(raw.decode("utf-8", "replace"))
        return parser


_UNSET = object()


def _update_state(conn, source_id, *, mode=_UNSET, cursor_page=_UNSET,
                  total_pages=_UNSET, window_since=_UNSET, last_success=_UNSET,
                  last_error=_UNSET, adapter_version=_UNSET):
    old = conn.execute("SELECT * FROM source_sync_state WHERE source_id=?",
                       (source_id,)).fetchone()
    fields = {
        "mode": mode if mode is not _UNSET else (old["mode"] if old else "incremental"),
        "cursor_page": cursor_page if cursor_page is not _UNSET else (old["cursor_page"] if old else 1),
        "total_pages": total_pages if total_pages is not _UNSET else (old["total_pages"] if old else None),
        "window_since": window_since if window_since is not _UNSET else (old["window_since"] if old else None),
        "last_success": last_success if last_success is not _UNSET else (old["last_success"] if old else None),
        "last_error": last_error if last_error is not _UNSET else (old["last_error"] if old else None),
        "adapter_version": adapter_version if adapter_version is not _UNSET else (
            old["adapter_version"] if old else 0),
        "updated_at": slg_db._now(),
    }
    conn.execute(
        "INSERT INTO source_sync_state(source_id,mode,cursor_page,total_pages,"
        "window_since,last_success,last_error,adapter_version,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source_id) DO UPDATE SET mode=excluded.mode,"
        " cursor_page=excluded.cursor_page,total_pages=excluded.total_pages,"
        " window_since=excluded.window_since,last_success=excluded.last_success,"
        " last_error=excluded.last_error,"
        " adapter_version=excluded.adapter_version,updated_at=excluded.updated_at",
        (source_id, fields["mode"], fields["cursor_page"], fields["total_pages"],
         fields["window_since"], fields["last_success"], fields["last_error"],
         fields["adapter_version"], fields["updated_at"]))


def _normalize(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"\s*[-|–—]\s*(?:dikgames|f95zone(?:\.to\.it)?|f95 zone)\s*$",
                   "", value, flags=re.I)
    # Keep bracket contents and digits. They often distinguish editions and
    # versions, so punctuation-only cleanup must not erase them.
    return "".join(ch for ch in value if ch.isalnum())


def _candidate_matches(conn, title, developer):
    title_key = _normalize(title)
    dev_key = _normalize(developer)
    if not title_key:
        return None, "new", 0.0
    games = conn.execute(
        "SELECT g.id,g.title,g.developer,g.engine,gs.source_title,"
        " gs.developer AS source_developer,gs.engine AS source_engine"
        " FROM games g LEFT JOIN game_sources gs ON gs.game_id=g.id"
        " WHERE g.origin='site' ORDER BY g.id").fetchall()
    grouped = {}
    for row in games:
        item = grouped.setdefault(row["id"], {
            "title": row["title"], "developer": row["developer"],
            "engine": row["engine"], "source_titles": [],
            "source_developers": [], "source_engines": []})
        if row["source_title"]:
            item["source_titles"].append(row["source_title"])
        if row["source_developer"]:
            item["source_developers"].append(row["source_developer"])
        if row["source_engine"]:
            item["source_engines"].append(row["source_engine"])
    candidates = []
    for game_id, game in grouped.items():
        titles = [game["title"]] + game["source_titles"]
        developers = ([game["developer"]] if game["developer"] else []) + game["source_developers"]
        engines = ([game["engine"]] if game["engine"] else []) + game["source_engines"]
        title_scores = [SequenceMatcher(None, title_key, _normalize(value)).ratio()
                        for value in titles if _normalize(value)]
        best_title = max(title_scores or [0.0])
        exact_title = title_key in {_normalize(value) for value in titles if value}
        dev_keys = {_normalize(value) for value in developers if value}
        if exact_title and dev_key and dev_key in dev_keys:
            candidates.append((game_id, 1.0, True))
        elif exact_title:
            # Same name with missing or conflicting developer is a review
            # candidate, never an automatic link.
            candidates.append((game_id, 0.84, False))
        elif best_title >= 0.91 and dev_key and dev_key in dev_keys:
            candidates.append((game_id, best_title, False))
        elif best_title >= 0.91:
            # Fuzzy similarity without exact title+developer proof is only a
            # suggestion; it never causes an automatic merge.
            candidates.append((game_id, best_title, False))
    if not candidates:
        return None, "new", 0.0
    exact = [c for c in candidates if c[2]]
    if len(exact) == 1:
        return exact[0][0], "strong_match", 1.0
    best = max(candidates, key=lambda c: c[1])
    return best[0], "review_candidate", best[1]


def _target_for_new_game(source_id, source_url):
    # Older clients hard-code a Dikgames label but open games.url. Give F95-only
    # entries a valid public detail destination. Internal Zyoudao-only entries
    # use an empty string so old clients hide the misleading external button.
    if source_id == "f95zone":
        return source_url
    return ""


def _ingest_record(conn, record):
    source_id = record["source_id"]
    external_id = str(record["external_id"])[:128]
    refreshed_overview = record.get("overview")
    existing_source = conn.execute(
        "SELECT game_id,match_status,match_confidence,suggested_game_id,overview"
        " FROM game_sources WHERE source_id=? AND external_id=?",
        (source_id, external_id)).fetchone()
    if existing_source:
        game_id = existing_source["game_id"]
        status = existing_source["match_status"]
        confidence = existing_source["match_confidence"]
        suggested = existing_source["suggested_game_id"]
        # A richer new article replaces the old short excerpt, while a later
        # incomplete/shorter fetch cannot truncate a useful synopsis already
        # stored in either catalogue table.
        old_overview = existing_source["overview"] or ""
        if old_overview and (not refreshed_overview or
                             len(old_overview) > len(refreshed_overview)):
            refreshed_overview = old_overview
    else:
        game_id, status, confidence = _candidate_matches(
            conn, record.get("title"), record.get("developer"))
        suggested = game_id if status == "review_candidate" else None
        # A low-confidence title match is only a suggestion. Keep it as a
        # separate namespaced game until an explicit future review action links
        # the two records.
        if status == "review_candidate":
            game_id = None
        if game_id is None:
            safe_id = re.sub(r"[^a-z0-9-]+", "-", external_id.casefold()).strip("-")[:80]
            slug = "%s-%s" % (source_id, safe_id or hashlib.sha256(
                external_id.encode("utf-8")).hexdigest()[:20])
            if conn.execute("SELECT 1 FROM games WHERE slug=?", (slug,)).fetchone():
                slug += "-" + hashlib.sha256(
                    (source_id + ":" + external_id).encode("utf-8")).hexdigest()[:10]
            game_id, _created = slg_db.upsert_game(
                conn, slug=slug, url=_target_for_new_game(source_id, record["url"]),
                title=record.get("title") or slug,
                version=record.get("version"), developer=record.get("developer"),
                engine=record.get("engine"), last_updated=record.get("modified"),
                tags=record.get("tags") or (), complete=0)
            if refreshed_overview:
                conn.execute("UPDATE games SET overview=COALESCE(overview,?) WHERE id=?",
                             (refreshed_overview[:1600], game_id))
            if status == "new":
                confidence = 0.0
                suggested = None

    # Refresh canonical fields only when this is the original, sole source for
    # that game. A newly linked second source never wins a field conflict.
    if existing_source:
        count = conn.execute(
            "SELECT COUNT(*) FROM game_sources WHERE game_id=?", (game_id,)).fetchone()[0]
        if count == 1:
            conn.execute(
                "UPDATE games SET title=?,version=COALESCE(?,version),"
                "developer=COALESCE(?,developer),engine=COALESCE(?,engine),"
                "overview=CASE WHEN ? IS NOT NULL AND"
                " length(COALESCE(overview,''))<length(?) THEN ? ELSE overview END"
                " WHERE id=? AND origin='site'",
                (record.get("title") or "", record.get("version"),
                 record.get("developer"), record.get("engine"),
                 refreshed_overview, refreshed_overview, refreshed_overview,
                 game_id))

    # Keep the legacy one-URL field useful without exposing an internal source.
    # A public source arriving later can replace the empty/home placeholder;
    # a Zyoudao-only mapping clears the old homepage placeholder on re-sync.
    current = conn.execute("SELECT url FROM games WHERE id=?", (game_id,)).fetchone()
    if current and source_id in ("dikgames", "f95zone"):
        if current["url"] in ("", "https://slg-king.com/"):
            conn.execute("UPDATE games SET url=? WHERE id=?",
                         (record["url"], game_id))
    elif current and source_id == "zyoudao" and current["url"] == "https://slg-king.com/":
        has_public = conn.execute(
            "SELECT 1 FROM game_sources WHERE game_id=?"
            " AND source_id IN ('dikgames','f95zone') LIMIT 1", (game_id,)
        ).fetchone()
        if not has_public:
            conn.execute("UPDATE games SET url='' WHERE id=?", (game_id,))

    slg_db.upsert_game_source(
        conn, source_id, external_id, game_id, source_url=record["url"],
        source_title=record.get("source_title") or record.get("title"),
        source_version=record.get("version"),
        developer=record.get("developer"), engine=record.get("engine"),
        overview=refreshed_overview, tags=record.get("tags") or (),
        source_modified=record.get("modified"), content_hash=record.get("hash"),
        match_status=status, match_confidence=confidence,
        suggested_game_id=suggested)
    return game_id, status


def _store_zyoudao_cover(filename, payload):
    """Atomically cache one validated, source-neutral cover file."""
    if (not isinstance(filename, str) or
            not re.fullmatch(r"cover-[0-9a-f]{24}\.(?:png|jpg|webp)", filename) or
            not isinstance(payload, bytes) or
            not payload or len(payload) > slg_source_covers.MAX_COVER_BYTES):
        return None
    try:
        directory = slg_db.covers_dir()
        target = os.path.join(directory, filename)
        if os.path.isfile(target) and 0 < os.path.getsize(target) <= slg_source_covers.MAX_COVER_BYTES:
            return filename
        fd, temporary = tempfile.mkstemp(prefix=".cover-download-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return None
        return filename
    except (OSError, ValueError):
        return None


def _ingest_zy_record(conn, client, record):
    """Ingest a Zyoudao game and fill a missing cover without replacing one."""
    game_id, status = _ingest_record(conn, record)
    media_id = record.get("featured_media_id")
    if not media_id:
        return game_id, status, False
    row = conn.execute("SELECT cover_file FROM games WHERE id=?", (game_id,)).fetchone()
    cover_file = row["cover_file"] if row else None
    if cover_file and not str(cover_file).startswith("pending:"):
        return game_id, status, False
    fetched = slg_source_covers.fetch_featured_cover(client, media_id)
    if not fetched:
        return game_id, status, False
    filename, payload = fetched
    stored = _store_zyoudao_cover(filename, payload)
    if not stored:
        return game_id, status, False
    conn.execute(
        "UPDATE games SET cover_file=? WHERE id=? AND"
        " (cover_file IS NULL OR cover_file='' OR cover_file LIKE 'pending:%')",
        (stored, game_id))
    return game_id, status, True


def _record_hash(*values):
    joined = "\n".join(str(value or "") for value in values)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()


def _labelled(text, patterns, maximum):
    for line in re.split(r"[\n|•·]+", text or ""):
        for pattern in patterns:
            match = re.search(pattern, line, re.I)
            if match:
                value = " ".join(match.group(1).split())
                if value:
                    return value[:maximum]
    return None


def _f95_record(client, url, label, tags):
    path = urlparse(url).path
    match = GAME_PATH.match(path)
    if not match:
        return None
    client.check_robots(F95_BASE, path, F95_HOST)
    page = client.html(url, F95_HOST)
    title = " ".join(page.h1).strip() or page.meta.get("og:title") or label
    title = _plain(title, 300)
    visible = "\n".join(page.text)[:12000]
    version = _labelled(visible, [r"\bVersion\s*[:：]\s*(.{1,100})",
                                  r"\b版本\s*[:：]\s*(.{1,100})"], 100)
    developer = _labelled(visible, [r"\bDeveloper\s*[:：]\s*(.{1,160})",
                                     r"\bStudio\s*[:：]\s*(.{1,160})",
                                     r"\b开发(?:者|商)?\s*[:：]\s*(.{1,160})",
                                     r"\b制作组\s*[:：]\s*(.{1,160})"], 160)
    engine = _labelled(visible, [r"\bEngine\s*[:：]\s*(.{1,100})",
                                  r"\b引擎\s*[:：]\s*(.{1,100})"], 100)
    overview = _plain(page.meta.get("og:description") or
                      page.meta.get("description"), 1200)
    if not overview:
        # Use only a short visible-text excerpt; do not persist page markup.
        overview = " ".join(page.text[:8])[:600]
    return {
        "source_id": "f95zone", "external_id": match.group(1).casefold(),
        "url": url, "title": title, "version": version,
        "developer": developer, "engine": engine, "overview": overview,
        "tags": tags[:24], "modified": None,
        "hash": _record_hash(title, version, developer, engine, overview, tags),
    }


def _f95_listing_candidates(page, listing_url):
    candidates = []
    seen = set()
    for label, href in page.anchors:
        absolute = urljoin(listing_url, href)
        if _host(absolute) != F95_HOST:
            continue
        parsed = urlparse(absolute)
        game = GAME_PATH.match(parsed.path)
        if not game or game.group(1).casefold() in IGNORED_SLUGS:
            continue
        clean = _safe_https(F95_BASE + parsed.path, F95_HOST)
        if clean and clean not in seen:
            seen.add(clean)
            candidates.append((label or game.group(1), clean))
        if len(candidates) >= MAX_F95_ITEMS_PER_LISTING:
            break
    return candidates


def _f95_add_category_tag(record, category_label):
    if category_label.casefold() not in {
            value.casefold() for value in record["tags"]}:
        record["tags"].append(category_label)
        record["hash"] = _record_hash(
            record["title"], record["version"], record["developer"],
            record["engine"], record["overview"], record["tags"])


def _f95_category_cursor_state(state):
    """Read extra-category cursors without reinterpreting legacy F95 state.

    ``cursor_page`` remains the Strategy cursor for old and new adapters.
    ``window_since`` is unused by F95, so a versioned JSON payload there keeps
    the other category cursors separate without changing the table schema.
    """
    if not state:
        return {}
    raw = state["window_since"] or ""
    if not raw.startswith(F95_CATEGORY_STATE_PREFIX):
        return {}
    try:
        payload = json.loads(raw[len(F95_CATEGORY_STATE_PREFIX):])
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    cursors = payload.get("cursors")
    if not isinstance(cursors, dict):
        return {}
    allowed = {slug for slug, _url, _label in F95_EXTRA_CATEGORIES}
    result = {}
    for slug, path in cursors.items():
        if slug not in allowed or not isinstance(path, str):
            continue
        match = F95_CATEGORY_PAGE_PATH.match(path)
        if not match or match.group(1).casefold() != slug or int(match.group(2)) < 2:
            continue
        result[slug] = path
    return result


def _f95_category_cursor_value(cursors, next_priority="strategy"):
    payload = {"version": 1, "cursors": cursors,
               "next_priority": next_priority}
    return F95_CATEGORY_STATE_PREFIX + json.dumps(
        payload, sort_keys=True, separators=(",", ":"))


def _f95_next_priority(state):
    raw = (state["window_since"] or "") if state else ""
    if raw.startswith(F95_CATEGORY_STATE_PREFIX):
        try:
            priority = json.loads(raw[len(F95_CATEGORY_STATE_PREFIX):]).get(
                "next_priority")
            if priority in ("strategy",) + tuple(
                    slug for slug, _url, _label in F95_EXTRA_CATEGORIES):
                return priority
        except (ValueError, TypeError, AttributeError):
            pass
    return "strategy"


def _f95_next_category_page(page, listing_url, slug, page_no):
    later = []
    for _label, href in page.anchors:
        absolute = urljoin(listing_url, href)
        if _host(absolute) != F95_HOST:
            continue
        parsed = urlparse(absolute)
        match = F95_CATEGORY_PAGE_PATH.match(parsed.path)
        if match and match.group(1).casefold() == slug:
            number = int(match.group(2))
            clean = _safe_https(F95_BASE + parsed.path, F95_HOST)
            if number > page_no and clean:
                later.append((number, parsed.path))
    return min(later)[1] if later else None


def sync_f95(conn, client):
    state = conn.execute("SELECT * FROM source_sync_state"
                         " WHERE source_id='f95zone'").fetchone()
    cursor_page = max(1, int(state["cursor_page"] or 1)) if state else 1
    pages_to_fetch = [1]
    if cursor_page > 1:
        pages_to_fetch.append(cursor_page)

    records = {}
    records_by_url = {}
    details_fetched = 0
    next_cursor = cursor_page

    def collect_records(page, category_url, category_label):
        nonlocal details_fetched
        complete = True
        for label, game_url in _f95_listing_candidates(page, category_url):
            game_path = urlparse(game_url).path
            client.check_robots(F95_BASE, game_path, F95_HOST)
            if game_url in records_by_url and category_label != "Strategy":
                record = records_by_url[game_url]
                _f95_add_category_tag(record, category_label)
                continue
            if (details_fetched >= MAX_F95_DETAILS_PER_RUN or
                    client.requests >= MAX_F95_TOTAL_REQUESTS_PER_RUN):
                complete = False
                break
            details_fetched += 1
            record = _f95_record(client, game_url, label, [category_label])
            if record:
                existing = records.get(record["external_id"])
                if existing:
                    for existing_tag in existing["tags"]:
                        _f95_add_category_tag(record, existing_tag)
                records[record["external_id"]] = record
                records_by_url[game_url] = record
        return complete

    category_cursors = _f95_category_cursor_state(state)
    next_category_cursors = dict(category_cursors)
    category_errors = []

    def run_strategy():
        nonlocal next_cursor
        client.check_robots(F95_BASE, "/tag/strategy/", F95_HOST)
        if client.requests >= MAX_F95_TOTAL_REQUESTS_PER_RUN:
            return
        listing = client.html(F95_LISTING, F95_HOST)
        for page_no in pages_to_fetch:
            category_url = (F95_LISTING if page_no == 1 else
                            F95_BASE + "/tag/strategy/page/%d/" % page_no)
            path = urlparse(category_url).path
            client.check_robots(F95_BASE, path, F95_HOST)
            if page_no == 1:
                page = listing
            else:
                if client.requests >= MAX_F95_TOTAL_REQUESTS_PER_RUN:
                    break
                try:
                    page = client.html(category_url, F95_HOST)
                except SourceError as exc:
                    if "HTTP 404" in str(exc):
                        next_cursor = 1
                        break
                    raise
            pagination = []
            for _pagination_label, href in page.anchors:
                absolute = urljoin(category_url, href)
                if _host(absolute) != F95_HOST:
                    continue
                page_match = PAGE_PATH.match(urlparse(absolute).path)
                if page_match:
                    pagination.append(int(page_match.group(1)))
            later_pages = sorted({number for number in pagination if number > page_no})
            next_cursor = later_pages[0] if later_pages else 1
            if not collect_records(page, category_url, "Strategy"):
                # A partial cursor page must be retried, not skipped.
                next_cursor = page_no if page_no > 1 else cursor_page
                break
            if details_fetched >= MAX_F95_DETAILS_PER_RUN:
                break

    def run_extra(slug, category_url, category_label):
        if (details_fetched >= MAX_F95_DETAILS_PER_RUN or
                client.requests >= MAX_F95_TOTAL_REQUESTS_PER_RUN):
            return
        prior_cursor = category_cursors.get(slug)
        category_next_cursor = prior_cursor
        try:
            category_path = urlparse(category_url).path
            client.check_robots(F95_BASE, category_path, F95_HOST)
            first_page = client.html(category_url, F95_HOST)
            pages = [(1, category_url, first_page)]
            cursor_missing = False
            cursor_deferred = False
            if prior_cursor:
                cursor_match = F95_CATEGORY_PAGE_PATH.match(prior_cursor)
                cursor_page_no = int(cursor_match.group(2))
                cursor_url = F95_BASE + prior_cursor
                client.check_robots(F95_BASE, prior_cursor, F95_HOST)
                if client.requests >= MAX_F95_TOTAL_REQUESTS_PER_RUN:
                    cursor_deferred = True
                else:
                    try:
                        cursor_listing = client.html(cursor_url, F95_HOST)
                    except SourceError as exc:
                        if "HTTP 404" in str(exc):
                            category_next_cursor = None
                            cursor_missing = True
                        else:
                            raise
                    else:
                        pages.append((cursor_page_no, cursor_url, cursor_listing))

            category_complete = not cursor_deferred
            for page_no, page_url, page in pages:
                next_path = _f95_next_category_page(page, page_url, slug, page_no)
                category_next_cursor = next_path
                if not collect_records(page, page_url, category_label):
                    category_complete = False
                    if page_no > 1:
                        category_next_cursor = urlparse(page_url).path
                    break
            if cursor_missing:
                category_next_cursor = None
            if category_complete:
                if category_next_cursor:
                    next_category_cursors[slug] = category_next_cursor
                else:
                    next_category_cursors.pop(slug, None)
            else:
                # Resume at the partial cursor page. A partial first page is
                # retried on the next turn, without losing an older cursor.
                if prior_cursor or category_next_cursor:
                    next_category_cursors[slug] = (
                        prior_cursor or category_next_cursor)
                else:
                    next_category_cursors.pop(slug, None)
        except SourceError as exc:
            category_errors.append("%s: %s" %
                                   (slug, " ".join(str(exc).split())[:180]))
            if prior_cursor:
                next_category_cursors[slug] = prior_cursor
            else:
                next_category_cursors.pop(slug, None)

    # Rotate which category gets the first claim on the bounded request budget.
    # In particular, a backlogged Strategy cursor must not starve Dating Sim.
    categories = [("strategy", F95_LISTING, "Strategy")] + list(F95_EXTRA_CATEGORIES)
    priority = _f95_next_priority(state)
    start = next(index for index, item in enumerate(categories)
                 if item[0] == priority)
    for slug, category_url, category_label in categories[start:] + categories[:start]:
        if (details_fetched >= MAX_F95_DETAILS_PER_RUN or
                client.requests >= MAX_F95_TOTAL_REQUESTS_PER_RUN):
            break
        if slug == "strategy":
            run_strategy()
        else:
            run_extra(slug, category_url, category_label)

    count = 0
    for record in records.values():
        _ingest_record(conn, record)
        count += 1
    _update_state(conn, "f95zone", mode="incremental", cursor_page=next_cursor,
                  total_pages=None,
                  window_since=_f95_category_cursor_value(
                      next_category_cursors,
                      categories[(start + 1) % len(categories)][0]),
                  last_success=slg_db._now())
    print("[f95zone] processed %d public detail records (%d requests)" %
          (count, client.requests))
    if category_errors:
        return ("F95 extra categories failed: " + "; ".join(category_errors))[:400]
    return None


def _wp_page(client, params):
    allowed = {"per_page", "page", "orderby", "order", "_fields", "_embed",
               "modified_after"}
    if (set(params) - allowed or params.get("per_page") != ZY_PER_PAGE or
            not isinstance(params.get("page"), int) or params["page"] < 1 or
            params["page"] > 1000000 or
            params.get("_fields") != ZY_FIELDS or
            params.get("_embed") != "wp:term" or
            params.get("orderby", "id") != "id" or
            params.get("order", "desc") != "desc"):
        raise SourceError("invalid WordPress feed parameters")
    query = urlencode(params)
    url = ZY_API + "?" + query
    client.check_robots(ZY_BASE, "/wp-json/wp/v2/posts", ZY_HOST)
    content_type, raw = client.get(url, ZY_HOST, "application/json",
                                   keep_query=True)
    if "json" not in content_type:
        raise SourceError("WordPress endpoint did not return JSON")
    try:
        # This WordPress install prefixes JSON with a UTF-8 BOM. It remains
        # valid JSON after BOM removal; decode it explicitly rather than
        # treating the public feed as a malformed response.
        rows = json.loads(raw.decode("utf-8-sig", "replace"))
    except (ValueError, TypeError):
        raise SourceError("WordPress response was invalid JSON") from None
    if not isinstance(rows, list):
        raise SourceError("WordPress response was not a post list")
    return rows


def _zy_record(item):
    if not isinstance(item, dict):
        return None
    try:
        post_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None
    title_field = item.get("title")
    excerpt_field = item.get("excerpt")
    content_field = item.get("content")
    source_title = _plain(title_field.get("rendered") if isinstance(title_field, dict)
                          else title_field, 300)
    excerpt_html = (excerpt_field.get("rendered")
                    if isinstance(excerpt_field, dict) else excerpt_field)
    content_html = (content_field.get("rendered")
                    if isinstance(content_field, dict) else content_field)
    excerpt_html = str(excerpt_html or "")[:MAX_RESPONSE_BYTES]
    content_html = str(content_html or "")[:MAX_RESPONSE_BYTES]
    if not source_title:
        return None
    title, version, developer, engine, overview = _zy_metadata(
        source_title, excerpt_html, content_html)
    slug = str(item.get("slug") or "").casefold()
    # This feed also contains site announcements and monetization posts. Exclude
    # clear non-game items; ambiguous entries remain available for later review.
    excluded_markers = ("佣金", "推广", "广告", "赞助", "充值", "捐赠",
                        "网站公告", "站点公告", "新地址", "最新地址", "会员公告")
    if any(marker in title for marker in excluded_markers) or any(
            marker in slug for marker in ("promotion", "sponsor", "announcement", "notice")):
        return None
    embedded = item.get("_embedded") or {}
    if not isinstance(embedded, dict):
        embedded = {}
    categories = embedded.get("wp:term") or []
    term_names = []
    if isinstance(categories, list):
        for group in categories:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict):
                    continue
                name = _plain(term.get("name"), 48)
                if name and name not in term_names:
                    term_names.append(name)
    excluded_terms = ("公告", "推广", "新闻", "站点支持", "网站支持", "赞助")
    if any(marker in name for marker in excluded_terms for name in term_names):
        return None
    # Construct the internal source identity URL on the known host. Never
    # follow a post's outbound links or copy an arbitrary URL from its content.
    url = "%s/archives/%d/" % (ZY_BASE, post_id)
    modified = item.get("modified_gmt") or item.get("modified")
    try:
        featured_media_id = int(item.get("featured_media") or 0)
    except (TypeError, ValueError):
        featured_media_id = 0
    if featured_media_id <= 0:
        featured_media_id = None
    return {
        "source_id": "zyoudao", "external_id": str(post_id), "url": url,
        "title": title, "source_title": source_title,
        "overview": overview,
        "developer": developer, "version": version, "engine": engine,
        "featured_media_id": featured_media_id,
        "tags": term_names[:24], "modified": modified,
        "hash": _record_hash(source_title, title, excerpt_html, content_html,
                             version, developer, engine, featured_media_id,
                             term_names, modified),
    }


def sync_zyoudao(conn, client):
    client.check_robots(ZY_BASE, "/wp-json/wp/v2/posts", ZY_HOST)
    state = conn.execute("SELECT * FROM source_sync_state WHERE source_id='zyoudao'").fetchone()
    now = slg_db._now()
    mode = state["mode"] if state else "backfill"
    cursor = max(1, int(state["cursor_page"] or 1)) if state else 1
    total_pages = state["total_pages"] if state else None
    window_since = state["window_since"] if state else None
    last_success = state["last_success"] if state else None
    if state and state["adapter_version"] < 4:
        # Earlier adapters stored short excerpts, omitted featured image IDs,
        # and retained Zyoudao catalogue prefixes in canonical titles. Rewalk
        # the bounded feed once and refresh existing mappings in place.
        mode, cursor, total_pages, window_since, last_success = (
            "backfill", 1, None, None, None)
    processed = 0
    covers_added = 0

    if mode == "backfill":
        # Remember when the multi-run walk began. After reaching the oldest
        # page, the incremental pass rechecks changes made during that walk.
        window_since = window_since or now
        for page_no in range(cursor, cursor + MAX_ZY_PAGES_PER_RUN):
            rows = _wp_page(client, {
                "per_page": ZY_PER_PAGE, "page": page_no,
                "orderby": "id", "order": "desc",
                "_fields": ZY_FIELDS,
                "_embed": "wp:term",
            })
            header_total = getattr(client, "last_total_pages", None)
            if header_total:
                total_pages = header_total
            if not rows:
                total_pages = page_no - 1
                break
            for item in rows:
                record = _zy_record(item)
                if record:
                    _game_id, _status, cover_added = _ingest_zy_record(
                        conn, client, record)
                    covers_added += int(cover_added)
                    processed += 1
            cursor = page_no + 1
            if len(rows) < ZY_PER_PAGE:
                total_pages = page_no
                break
            if total_pages is not None and page_no >= total_pages:
                break
        if total_pages is not None and cursor > total_pages:
            mode = "incremental"
            cursor = 1
            last_success = window_since
            window_since = None
        _update_state(conn, "zyoudao", mode=mode, cursor_page=cursor,
                      total_pages=total_pages, window_since=window_since,
                      last_success=last_success, adapter_version=4)
    else:
        if not window_since:
            if last_success:
                try:
                    prior = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
                    window_since = (prior - timedelta(days=2)).strftime(
                        "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    window_since = last_success[:19]
            else:
                window_since = "2000-01-01T00:00:00"
        complete = False
        for page_no in range(cursor, cursor + MAX_ZY_PAGES_PER_RUN):
            rows = _wp_page(client, {
                "per_page": ZY_PER_PAGE, "page": page_no,
                "modified_after": window_since,
                "_fields": ZY_FIELDS,
                "_embed": "wp:term",
            })
            header_total = getattr(client, "last_total_pages", None)
            if header_total is not None:
                total_pages = header_total
            for item in rows:
                record = _zy_record(item)
                if record:
                    _game_id, _status, cover_added = _ingest_zy_record(
                        conn, client, record)
                    covers_added += int(cover_added)
                    processed += 1
            cursor = page_no + 1
            if not rows or (total_pages is not None and page_no >= total_pages):
                complete = True
                break
        if complete:
            mode, cursor, window_since, last_success = "incremental", 1, None, now
            total_pages = None
        _update_state(conn, "zyoudao", mode=mode, cursor_page=cursor,
                      total_pages=total_pages, window_since=window_since,
                      last_success=last_success, adapter_version=4)
    print("[zyoudao] processed %d public REST posts; added %d covers"
          " (%d requests; mode=%s)" %
          (processed, covers_added, client.requests, mode))


def _run_one(conn, source_id):
    client = PublicSourceClient()
    savepoint = "source_" + source_id
    conn.execute("SAVEPOINT " + savepoint)
    try:
        source_warning = None
        if source_id == "zyoudao":
            sync_zyoudao(conn, client)
        elif source_id == "f95zone":
            source_warning = sync_f95(conn, client)
        else:
            raise SourceError("unknown source")
        # Zyoudao manages last_success itself because its multi-run backfill
        # needs the original start time for the catch-up window.
        if source_id == "zyoudao":
            _update_state(conn, source_id, last_error=None)
        else:
            _update_state(conn, source_id, last_success=slg_db._now(),
                          last_error=source_warning)
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        conn.commit()
        return True
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        message = " ".join(str(exc).split())[:400] or type(exc).__name__
        _update_state(conn, source_id, last_error=message)
        conn.commit()
        print("[%s] failed; previous records retained: %s" % (source_id, message))
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="server catalogue SQLite path")
    parser.add_argument("--source", choices=("all", "zyoudao", "f95zone"), default="all")
    args = parser.parse_args(argv)
    conn = slg_db.connect(args.db)
    try:
        selected = ["zyoudao", "f95zone"] if args.source == "all" else [args.source]
        results = [_run_one(conn, source_id) for source_id in selected]
        print("source sync complete: %d/%d succeeded" %
              (sum(bool(item) for item in results), len(results)))
        # A bad third-party source is not allowed to block the Dikgames cron or
        # its following public snapshot publish.
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
