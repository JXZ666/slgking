"""Read public DLsite game facts conservatively; never fetch artwork or downloads.

No account/session API is used. Public summaries intentionally exclude work
descriptions and image URLs. Scraping access is checked separately from reuse
rights: this module does not grant permission to republish protected material.
"""

from slg_dlsite_recovery import safe_diagnostic, response_evidence
import datetime
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


BASE = "https://www.dlsite.com"
ROBOTS_URL = BASE + "/robots.txt"
LATEST_URL = BASE + "/maniax/works/type/=/work_type_category/game"
USER_AGENT = "SLGkingCataloguePilot/0.25.7 (+public game metadata and official main covers)"
MIN_INTERVAL = 10.0
_ID = re.compile(r"RJ\d{6,10}\Z")
_PRODUCT = re.compile(r"/maniax/work/=/product_id/(RJ\d{6,10})\.html/?\Z")
_GAME = re.compile(
    r"动作|アクション|action|冒险|冒險|アドベンチャー|adventure|"
    r"角色扮演|ロールプレイング|role.?playing|\brpg\b|"
    r"模拟|模擬|シミュレーション|simulation|\bslg\b|"
    r"电子小说|電子小說|デジタルノベル|digital.?novel|"
    r"解谜|解謎|パズル|puzzle|问答|問答|クイズ|quiz|"
    r"桌面|テーブル|table|打字|タイピング|typing", re.I)
_NON_GAME = re.compile(r"漫画|マンガ|コミック|comic|音声|音聲|ボイス|asmr|"
                       r"cg|イラスト|插画|插畫|音乐|音樂|music", re.I)


class AccessStopped(RuntimeError):
    """A blocked/age-gated response: never work around it or retry."""

    def __init__(self, message, retry_after=None, kind=None, diagnostic=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.kind = kind
        self.diagnostic = diagnostic


class PathDisallowed(AccessStopped):
    """Only this observed URL is forbidden; never issue its request."""


class RequestBudgetReached(RuntimeError):
    """A normal resumable pause, rather than successful full coverage."""


class RobotsPolicy:
    """Match wildcard rules by longest path, with Allow winning equal ties.

    Python's robotparser does not interpret DLsite's wildcard/page rules.
    Keeping their semantics here prevents accidentally permitting page 2.
    """

    def __init__(self, text):
        text = text.lstrip("\ufeff")
        self.groups = []
        agents, rules, delay = [], [], None
        directives = False
        for line in text.splitlines() + ["User-agent: __end__"]:
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            name = name.lower()
            if name == "user-agent":
                if directives:
                    self.groups.append((agents, rules, delay))
                    agents, rules, delay, directives = [], [], None, False
                agents.append(value.lower())
            elif agents:
                directives = True
                if name in ("allow", "disallow") and value:
                    pattern = re.escape(value).replace(r"\*", ".*")
                    if value.endswith("$"):
                        pattern = pattern[:-2] + "$"
                    rules.append((re.compile("^" + pattern), len(value.replace("*", "")), name == "allow"))
                elif name == "crawl-delay":
                    try:
                        delay = float(value)
                    except ValueError:
                        pass

    def _groups(self, agent):
        agent = agent.lower()
        matches = []
        for agents, rules, delay in self.groups:
            lengths = [0 if item == "*" else len(item)
                       for item in agents if item == "*" or item in agent]
            if lengths:
                matches.append((max(lengths), rules, delay))
        if not matches:
            return []
        longest = max(item[0] for item in matches)
        return [item for item in matches if item[0] == longest]

    def can_fetch(self, agent, url):
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        matches = [(length, allow) for _, rules, _delay in self._groups(agent)
                   for pattern, length, allow in rules if pattern.match(path)]
        return max(matches)[1] if matches else True

    def crawl_delay(self, agent):
        delays = [delay for _, _rules, delay in self._groups(agent) if delay is not None]
        return max(delays) if delays else None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _text(value):
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def validate_url(url):
    """Accept only this public source's HTTPS metadata/discovery surface."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "www.dlsite.com"
            or parsed.port is not None or parsed.username or parsed.password
            or parsed.fragment):
        raise ValueError("Not an allowed DLsite public URL")
    path = parsed.path
    if '%' in path or '..' in path or '\\' in path:
        raise ValueError('Ambiguous public URL path')
    if any(part in path.lower() for part in ("adultcheck", "mypage", "cart", "login", "download")):
        raise ValueError("Private, gated or download URL is not allowed")
    if not (path == "/robots.txt" or _PRODUCT.fullmatch(path)
            or path.startswith("/maniax/works/type/=/work_type_category/game")
            or path.startswith("/maniax/ranking")
            or path.startswith("/maniax/fsr/=/")
            or path.startswith("/maniax/new")
            or path.startswith("/modpub/sitemap-xml/")):
        raise ValueError("URL is outside public game metadata scope")
    return url


def product_url(external_id):
    if not isinstance(external_id, str) or not _ID.fullmatch(external_id):
        raise ValueError("Invalid DLsite product identifier")
    return BASE + "/maniax/work/=/product_id/" + external_id + ".html"


def product_id(url):
    validate_url(url)
    match = _PRODUCT.fullmatch(urllib.parse.urlsplit(url).path)
    if match is None:
        raise ValueError("Not a DLsite adult doujin product URL")
    return match.group(1)


class FactParser(HTMLParser):
    """Capture only headings, maker, outline rows, genre labels and rating meta."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip = 0
        self.outline_depth = None
        self.row = None
        self.cell = None
        self.captures = []
        self.title = ""
        self.developer = ""
        self.outline = {}
        self.meta = {}
        self.canonical = None
        self.links = []
        self.genres = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        void = tag in {"meta", "link", "img", "br", "hr", "input", "source", "area", "wbr"}
        if not void:
            self.depth += 1
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "meta":
            name = attrs.get("itemprop") or attrs.get("property") or ""
            if name in ("ratingValue", "ratingCount", "bestRating", "og:title"):
                self.meta[name] = attrs.get("content", "")
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
            match = re.search(r"/genre/(\d+)(?:/|$)", attrs["href"])
            if match:
                self.captures.append((self.depth, "genre:" + match.group(1), []))
        if tag == "h1" and not self.title:
            self.captures.append((self.depth, "title", []))
        if "maker_name" in attrs.get("class", "").split():
            self.captures.append((self.depth, "developer", []))
        if tag == "table" and attrs.get("id") == "work_outline":
            self.outline_depth = self.depth
        if self.outline_depth is not None:
            if tag == "tr":
                self.row = [[], []]
            elif tag in ("th", "td") and self.row is not None:
                self.cell = 0 if tag == "th" else 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.skip:
            return
        for _depth, _kind, parts in self.captures:
            parts.append(data)
        if self.row is not None and self.cell is not None:
            self.row[self.cell].append(data)

    def handle_endtag(self, tag):
        for depth, kind, parts in list(self.captures):
            if depth == self.depth:
                value = _text(" ".join(parts))
                if kind == "title":
                    self.title = value
                elif kind == "developer":
                    self.developer = value
                else:
                    self.genres[kind[6:]] = value
                self.captures.remove((depth, kind, parts))
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag in ("th", "td"):
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.outline[_text(" ".join(self.row[0]))] = _text(" ".join(self.row[1]))
            self.row = None
        if tag == "table" and self.outline_depth == self.depth:
            self.outline_depth = None
        self.depth = max(0, self.depth - 1)


def _field(outline, *labels):
    for label in labels:
        if label in outline:
            return outline[label]
    return ""


def _date(value):
    match = re.search(r"((?:19|20)\d{2})[年/\-](\d{1,2})[月/\-](\d{1,2})", value)
    if not match:
        return ""
    try:
        return datetime.date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return ""


def _number(value, integer=False):
    try:
        result = int(value) if integer else float(value)
        return result if result >= 0 and math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_product(text, url):
    """Return eligible R18 game facts, None for non-games or missing evidence."""
    external_id = product_id(url)
    parser = FactParser()
    parser.feed(text)
    if parser.canonical and product_id(parser.canonical) != external_id:
        raise ValueError("Canonical product identifier differs from requested product")
    if not parser.outline and re.search(r"(?:action|location)[^\n]{0,120}adultcheck", text, re.I):
        raise AccessStopped("Age confirmation page returned; collection stopped")
    age = _field(parser.outline, "年龄指定", "年齡指定", "年齢指定", "Age rating")
    work_type = _field(parser.outline, "作品形式", "Work format", "Work type")
    # Music/voice support is a game property too. Reject a non-game *primary*
    # type, not supporting flags such as '有配音/有音乐' after the game label.
    primary_type = re.split(r"\s+(?:有|音声|音樂|音楽|with|Included)", work_type, maxsplit=1)[0]
    if (not re.search(r"R[- ]?18|18禁|18歳|18岁|成人", age, re.I)
            or not _GAME.search(primary_type) or _NON_GAME.search(primary_type)):
        return None
    title = parser.title or parser.meta.get("og:title", "").split(" | ", 1)[0]
    if not title:
        return None
    languages = _field(parser.outline, "支持的语言", "支持的語言", "対応言語", "Supported languages")
    # Locale never supplies the language field; only explicit work metadata does.
    language_labels = re.findall(
        r"中文\s*[（(](?:简体字|簡體字|繁体字|繁體字)[）)]|简体中文|繁體中文|"
        r"日本語|日语|日語|Japanese|英語|英语|English|韓国語|韩语|韓語|Korean|"
        r"中国語\s*[（(](?:簡体字|繁体字)[）)]|Chinese[^,;]*", languages, re.I)
    if languages and not language_labels:
        language_labels = [languages]
    tag_labels = {"dlsite:genre:" + key: value for key, value in parser.genres.items() if value}
    return {
        "external_id": external_id, "slug": "dlsite-" + external_id.lower(),
        "url": product_url(external_id), "title": title,
        "developer": parser.developer,
        "release_date": _date(_field(parser.outline, "发售日", "發售日", "販売日", "Release date")),
        "updated_date": _date(_field(parser.outline, "更新信息", "更新資訊", "更新情報", "Update information")),
        "work_type": primary_type, "languages": list(dict.fromkeys(language_labels)),
        "age_rating": "R18", "rating_value": _number(parser.meta.get("ratingValue")),
        "rating_count": _number(parser.meta.get("ratingCount"), integer=True),
        "rating_scale_max": _number(parser.meta.get("bestRating")),
        "tags": list(tag_labels), "tag_labels": tag_labels,
    }


def _generic_game_ranking(url):
    parsed = urllib.parse.urlsplit(url)
    if re.fullmatch(r"/maniax/ranking/(?:day|week|month)/=/category/game(?:/page/[1-9]\d*)?/?", parsed.path):
        return not parsed.query
    query = urllib.parse.parse_qs(parsed.query)
    return bool(re.fullmatch(r"/maniax/ranking/(?:day|week|month)/?", parsed.path)
                and query.get('category') == ['game'] and set(query) <= {'category', 'page'}
                and ('page' not in query or len(query['page']) == 1 and re.fullmatch(r'[1-9]\d*', query['page'][0])))


def discover_links(text, base_url):
    if urllib.parse.urlsplit(base_url).path.startswith('/modpub/sitemap-xml/'):
        # Only locations in an actually returned sitemap are candidates.
        if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
            raise ValueError('Unsafe sitemap XML')
        root = ET.fromstring(text)
        if root.tag.rsplit('}', 1)[-1] not in ('urlset', 'sitemapindex'):
            raise ValueError('Unsupported sitemap document')
        links = [node.text.strip() for node in root.iter()
                 if node.tag.rsplit('}', 1)[-1] == 'loc' and node.text]
    else:
        parser = FactParser()
        parser.feed(text)
        links = parser.links
    products, pages = [], []
    for href in links:
        url = urllib.parse.urljoin(base_url, html.unescape(href))
        try:
            validate_url(url)
        except ValueError:
            continue
        path = urllib.parse.urlsplit(url).path
        if _PRODUCT.fullmatch(path):
            products.append(product_url(product_id(url)))
        elif _generic_game_ranking(url):
            pages.append(url)
        elif "/fsr/=/" in path and "work_type_category/game" in path:
            pages.append(url)
        elif (path.startswith('/modpub/sitemap-xml/')
              or path.startswith('/maniax/works/type/=/work_type_category/game')
              or path.startswith('/maniax/new') and 'work_type_category/game' in path):
            pages.append(url)
    return list(dict.fromkeys(products)), list(dict.fromkeys(pages))


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, client):
        self.client = client

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        self.client.redirects.append(new_url)
        try:
            self.client.check_url(new_url)
        except (ValueError, AccessStopped) as error:
            diagnostic=safe_diagnostic('verification_gate','redirect',request.full_url,code,error,self.client.redirects,self.client.clock())
            self.client.last_diagnostic=diagnostic
            atomic_write(self.client.cache_dir / 'access-diagnostic.json',diagnostic)
            raise AccessStopped("Unexpected/gated redirect; collection stopped",kind='verification_gate',diagnostic=diagnostic) from error
        if self.client.requests >= self.client.max_requests:
            raise RequestBudgetReached("Request budget reached before redirect")
        self.client.pace()
        self.client.request_kind = 'check'
        if self.client.on_request:
            self.client.on_request()
        self.client.request_kind = 'work'
        self.client.requests += 1
        return super().redirect_request(request, fp, code, msg, headers, new_url)


class PublicClient:
    def __init__(self, cache_dir, max_requests=260, clock=time.time, sleep=time.sleep, opener=None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.requests = 0
        self.on_request = None
        self.clock, self.sleep = clock, sleep
        self.interval = MIN_INTERVAL
        self.robots = None
        self.redirects = []
        self.last_diagnostic = None
        self.force_robots = False
        self.last_request = 0.0
        state = self.cache_dir / "http-state.json"
        if state.exists():
            self.last_request = float(json.loads(state.read_text(encoding="utf-8")).get("last_request", 0))
        self.opener = opener or urllib.request.build_opener(_SafeRedirect(self))

    def pace(self):
        remaining = self.interval - (self.clock() - self.last_request)
        if remaining > 0:
            self.sleep(remaining)
        self.last_request = self.clock()
        atomic_write(self.cache_dir / "http-state.json", {"last_request": self.last_request})

    def check_url(self, url):
        validate_url(url)
        if url != ROBOTS_URL and (self.robots is None or not self.robots.can_fetch(USER_AGENT, url)):
            raise PathDisallowed("URL is disallowed by current robots rules", kind="robots_path")

    def fetch(self, url):
        self.check_url(url)
        if self.requests >= self.max_requests:
            raise RequestBudgetReached("Request budget reached; collection paused")
        self.pace()
        self.request_kind = 'check' if url == ROBOTS_URL else 'work'
        if self.on_request:
            self.on_request()
        self.request_kind = 'work'
        self.requests += 1
        self.redirects = []
        stage = 'robots' if url == ROBOTS_URL else 'page'
        def stopped(kind, error=None, status=None, retry=None, response=None):
            diagnostic = safe_diagnostic(kind,stage,url,status,error,self.redirects,self.clock())
            path = urllib.parse.urlsplit(url).path
            diagnostic['request_role'] = ('robots' if url == ROBOTS_URL else 'product_detail'
                if _PRODUCT.fullmatch(path) else 'sitemap' if path.startswith('/modpub/sitemap-xml/') else 'discovery')
            if response is not None:
                diagnostic['response_evidence'] = response_evidence(response)
            self.last_diagnostic = diagnostic
            atomic_write(self.cache_dir / 'access-diagnostic.json',diagnostic)
            return AccessStopped('Access check failed: '+kind,retry,kind,diagnostic)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=getattr(self, 'request_timeout', lambda: 30)()) as response:
                self.check_url(response.geturl())
                if response.status in (403, 429):
                    raise stopped("http_"+str(response.status),status=response.status,retry=response.headers.get("Retry-After"),response=response)
                if response.status != 200:
                    raise urllib.error.HTTPError(url, response.status, 'Unexpected HTTP status', response.headers, None)
                self.last_diagnostic=safe_diagnostic('allowed',stage,url,response.status,redirects=self.redirects,now=self.clock())
                atomic_write(self.cache_dir / 'access-diagnostic.json',self.last_diagnostic)
                content = response.read(2_000_001)
                if len(content) > 2_000_000:
                    raise stopped("response_size")
                charset = response.headers.get_content_charset() or "utf-8"
                text = content.decode(charset, "replace")
                if any(x in text.lower() for x in ('cf-chl-', 'g-recaptcha', 'verify you are human')):
                    raise stopped('verification_gate')
                return text
        except urllib.error.HTTPError as error:
            if error.code in (403,429):
                raise stopped('http_'+str(error.code),error,error.code,(error.headers or {}).get('Retry-After'),response=error) from error
            if url == ROBOTS_URL and 400 <= error.code < 500:
                self.last_diagnostic=safe_diagnostic('robots_unavailable','robots',url,error.code,error,self.redirects,self.clock())
                atomic_write(self.cache_dir / 'access-diagnostic.json',self.last_diagnostic)
                return 'User-agent: *\nAllow: /\n'  # RFC9309 unavailable; refusals excluded above
            if error.code >= 500:
                raise stopped('server_error',error,error.code,(error.headers or {}).get('Retry-After'),response=error) from error
            raise
        except (urllib.error.URLError, OSError) as error:
            raise stopped('network',error) from error

    def load_robots(self):
        try:
            cached = _read_json(self.cache_dir / 'robots-policy.json', {})
        except (OSError,ValueError,TypeError):
            cached = {}
        try:
            stamp=float(cached.get('checked_epoch',0))
        except (TypeError,ValueError,AttributeError):
            cached={};stamp=0
        if stamp>self.clock() or not isinstance(cached.get('rules'),list):
            cached={};stamp=0
        if not self.force_robots and 0 <= self.clock()-stamp < 86400 and cached.get('rules'):
            text = '\n'.join(cached['rules'])
        else:
            text = self.fetch(ROBOTS_URL)
        text = text.lstrip("\ufeff")
        self.sitemaps = []
        for line in text.splitlines():
            name, separator, value = line.partition(':')
            if separator and name.strip().lower() == 'sitemap':
                try:
                    url = validate_url(value.strip())
                    if urllib.parse.urlsplit(url).path.startswith('/modpub/sitemap-xml/'):
                        self.sitemaps.append(url)
                except ValueError:
                    pass
        if not any(line.split('#',1)[0].strip() for line in text.splitlines()):
            text='User-agent: *\nAllow: /\n'
        self.robots = RobotsPolicy(text)
        if '<html' in text.lower() or '<!doctype' in text.lower() or not self.robots.groups:
            diagnostic = safe_diagnostic('robots_parse','robots',ROBOTS_URL,now=self.clock())
            self.last_diagnostic = diagnostic
            atomic_write(self.cache_dir / 'access-diagnostic.json',diagnostic)
            raise AccessStopped("Unreadable robots policy; collection stopped",kind='robots_parse',diagnostic=diagnostic)
        self.interval = max(MIN_INTERVAL, self.robots.crawl_delay(USER_AGENT) or 0)
        atomic_write(self.cache_dir / "robots-policy.json", {
            "url": ROBOTS_URL, "checked_at": utc_now(), "checked_epoch": stamp if stamp and not self.force_robots and self.clock()-stamp<86400 else self.clock(), "interval_seconds": self.interval,
            "rules": text.splitlines(),
        })


def _read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def collect(cache_dir, target=200, max_requests=260, progress=print, client=None):
    """Collect a bounded, resumable pilot. Network failures never replace its catalogue."""
    if not isinstance(target, int) or not 1 <= target <= 200 or max_requests < 2:
        raise ValueError("target must be 1..200; max_requests must be at least 2")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = cache_dir / "collector.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RuntimeError("A collector already owns this cache directory") from error
    os.close(fd)
    try:
        return _collect_locked(cache_dir, target, max_requests, progress, client)
    finally:
        lock.unlink(missing_ok=True)


def _collect_locked(cache_dir, target, max_requests, progress, client):
    client = client or PublicClient(cache_dir, max_requests=max_requests)
    facts_path = cache_dir / "facts-cache.json"
    cache = _read_json(facts_path, {})
    origins = _read_json(cache_dir / "discovery.json", {"latest": [], "popular": [], "pages": []})
    eligible = {key: entry["item"] for key, entry in cache.items() if entry.get("item")}
    fetched_pages = []
    stopped_reason = None
    try:
        client.load_robots()
        # Reuse fact cache; re-read only allowed discovery pages each run.
        text = client.fetch(LATEST_URL)
        fetched_pages.append(LATEST_URL)
        products, pages = discover_links(text, LATEST_URL)
        if not products:
            raise AccessStopped("No product links on public game list; stopped rather than replacing valid data")
        origins["latest"] = list(dict.fromkeys(origins["latest"] + products))
        origins["pages"] = list(dict.fromkeys(origins["pages"] + pages))
        atomic_write(cache_dir / "discovery.json", origins)
        # At most one latest-list link and three explicitly linked game
        # rankings (day plus week/month). Do not construct guessed routes or
        # follow pagination variants to evade a robots prohibition.
        queue = list(origins["pages"])
        ranking_count, latest_count, visited = 0, 0, set()
        while queue:
            page = queue.pop(0)
            if page in visited:
                continue
            visited.add(page)
            try:
                client.check_url(page)
            except AccessStopped:
                continue
            kind = "popular" if "/ranking" in urllib.parse.urlsplit(page).path else "latest"
            if kind == "popular" and not _generic_game_ranking(page):
                continue
            if (kind == "popular" and ranking_count >= 3) or (kind == "latest" and latest_count >= 1):
                continue
            page_products, linked_pages = discover_links(client.fetch(page), page)
            fetched_pages.append(page)
            if not page_products:
                raise AccessStopped("No product links on discovery page; possible gate or markup change")
            origins[kind] = list(dict.fromkeys(origins[kind] + page_products))
            if kind == "popular":
                ranking_count += 1
                supplements = [url for url in linked_pages
                               if "/ranking/week" in url or "/ranking/month" in url]
                queue.extend(supplements)
                origins["pages"] = list(dict.fromkeys(origins["pages"] + supplements))
            else:
                latest_count += 1
            atomic_write(cache_dir / "discovery.json", origins)
        origins["fetched_pages"] = fetched_pages
        atomic_write(cache_dir / "discovery.json", origins)
        candidates = list(dict.fromkeys(origins["latest"] + origins["popular"]))
        progress("Discovered %d public candidates; cached eligible %d" % (len(candidates), len(eligible)))
        for url in candidates:
            key = product_id(url)
            if key in cache:
                continue
            item = parse_product(client.fetch(url + "?locale=zh_CN"), url)
            cache[key] = {"checked_at": utc_now(), "item": item,
                          "status": "eligible_game" if item else "not_eligible_or_missing_evidence"}
            if item:
                eligible[key] = item
            atomic_write(facts_path, cache)
            if len(cache) % 5 == 0:
                progress("Checked %d products; eligible %d; requests %d" % (len(cache), len(eligible), client.requests))
            if len(eligible) >= target:
                break
    except RequestBudgetReached as error:
        stopped_reason = str(error)
    except Exception as error:
        atomic_write(cache_dir / "last-run.json", {
            "finished_at": utc_now(), "status": "failed", "reason": str(error),
            "eligible_cached": len(eligible), "requests": client.requests,
        })
        raise
    catalogue = _build_catalogue(cache, origins, target, client.interval,
                                 client.requests, stopped_reason, fetched_pages)
    items = catalogue["items"]
    atomic_write(cache_dir / "catalogue.json", catalogue)
    atomic_write(cache_dir / "last-run.json", {
        "finished_at": utc_now(), "status": "complete" if len(items) >= target else "partial",
        "count": len(items), "requests": client.requests,
        "reason": catalogue["provenance"]["stopped_reason"],
    })
    return catalogue


def _build_catalogue(cache, origins, target, interval, requests, stopped_reason, fetched_pages):
    """Pure assembly of verified facts; reusable without any network or personal DB."""
    eligible = {key: entry["item"] for key, entry in cache.items() if entry.get("item")}
    newest = sorted(eligible.values(), key=lambda item: (item["release_date"], item["external_id"]), reverse=True)
    latest_ids = {product_id(url) for url in origins["latest"]}
    latest = [item for item in newest if item["external_id"] in latest_ids][:min(100, target)]
    selected = {item["external_id"] for item in latest}
    popular = []
    for url in origins["popular"]:
        if len(popular) >= min(100, target - len(latest)):
            break
        item = eligible.get(product_id(url))
        if item and item["external_id"] not in selected:
            popular.append(item)
            selected.add(item["external_id"])
            if len(popular) >= min(100, target - len(latest)):
                break
    items = latest + popular
    for item in newest:
        if len(items) >= target:
            break
        if item["external_id"] not in selected:
            items.append(item)
            selected.add(item["external_id"])
    catalogue = {
        "catalog_id": "dlsite", "collected_at": utc_now(),
        "provenance": {
            "source": BASE, "robots_url": ROBOTS_URL,
            "interval_seconds": interval, "target": target, "actual_count": len(items),
            "target_reached": len(items) >= target, "requests_this_run": requests,
            "latest_selected": len(latest), "popular_selected": len(popular),
            "latest_supplement": max(0, len(items) - len(latest) - len(popular)),
            "discovery_pages": fetched_pages,
            "candidate_count": len(set(origins["latest"] + origins["popular"])),
            "checked_count": len(cache),
            "excluded_count": sum(not entry.get("item") for entry in cache.values()),
            "selection_scope": "eligible public candidates discovered from permitted lists, not a claim of exhaustive site coverage",
            "stopped_reason": stopped_reason or ("Allowed candidate pool exhausted" if len(items) < target else None),
            "descriptions_and_images": "excluded; republication permission not established",
            "rating_policy": "native only; no conversion or cross-source comparison",
        }, "items": items,
    }
    return catalogue


def rebuild_cached(cache_dir, target=200):
    """Rebuild already verified public facts offline, never make a request.

    Existing provenance is retained, so assembly does not imply a new fetch.
    Call only after the collection process has released its cache lock.
    """
    cache_dir = Path(cache_dir)
    if not 1 <= target <= 200:
        raise ValueError("target must be 1..200")
    lock = cache_dir / "collector.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RuntimeError("A collector already owns this cache directory") from error
    os.close(fd)
    try:
        prior = _read_json(cache_dir / "catalogue.json", {})
        facts = _read_json(cache_dir / "facts-cache.json", {})
        origins = _read_json(cache_dir / "discovery.json", {})
        if not prior or not facts or not origins:
            raise ValueError("No verified catalogue and fact cache available")
        provenance = prior["provenance"]
        result = _build_catalogue(facts, origins, target,
                                 provenance["interval_seconds"],
                                 provenance["requests_this_run"],
                                 provenance.get("stopped_reason"),
                                 origins.get("fetched_pages", provenance["discovery_pages"]))
        result["collected_at"] = prior["collected_at"]
        result["provenance"]["assembled_at"] = utc_now()
        atomic_write(cache_dir / "catalogue.json", result)
        return result
    finally:
        lock.unlink(missing_ok=True)
