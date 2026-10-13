"""Conservative, restartable mirror collector; its private ledger is never exported.

All network calls, including robots and future cover requests, use BudgetClient.
The production adapter remains the sole authority for matching catalogue identity.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import json
from html.parser import HTMLParser
from io import BytesIO
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request

import slg_sources as sources
import slg_db
from slg_collector_policy import DEFAULTS, validate_limits

MIRROR_LABEL = "F95zone 镜像（f95zone.to.it）"
EXIT_CODES = {"completed": 0, "partial": 3, "paused": 2, "error": 1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS streams(url TEXT PRIMARY KEY,label TEXT NOT NULL,cursor TEXT NOT NULL,last_attempt REAL DEFAULT 0,next_due REAL DEFAULT 0,attempts INTEGER DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS candidates(identity TEXT PRIMARY KEY,url TEXT UNIQUE NOT NULL,label TEXT,tags TEXT NOT NULL,kind TEXT DEFAULT 'new',attempts INTEGER DEFAULT 0,last_attempt REAL DEFAULT 0,next_due REAL DEFAULT 0,last_success REAL,error TEXT);
CREATE TABLE IF NOT EXISTS cover_tasks(identity TEXT PRIMARY KEY,url TEXT NOT NULL,next_due REAL DEFAULT 0,failed INTEGER DEFAULT 0,last_attempt REAL DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS aliases(url TEXT PRIMARY KEY,identity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,day TEXT NOT NULL,at REAL NOT NULL,url TEXT NOT NULL,kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,started REAL NOT NULL,heartbeat REAL NOT NULL,status TEXT NOT NULL,finished REAL);
CREATE TABLE IF NOT EXISTS run_metrics(run_id INTEGER PRIMARY KEY,metrics TEXT NOT NULL);
"""


class Stop(RuntimeError):
    pass


class FetchError(sources.SourceError):
    def __init__(self, reason, *, pause=False, retry_after=0):
        super().__init__(reason)
        self.reason, self.pause, self.retry_after = reason, pause, retry_after


@contextmanager
def file_lock(path):
    """OS-owned nonblocking lock, released by process death on Linux/Windows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


class Ledger:
    def __init__(self, path, now=time.time):
        self.now = now
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(requests)")}
        if "category" not in columns:
            self.conn.execute("ALTER TABLE requests ADD COLUMN category TEXT")
        self.conn.execute("UPDATE requests SET category=CASE WHEN kind='listing' THEN 'discovery' WHEN kind='cover' THEN 'cover' ELSE 'legacy' END WHERE category IS NULL AND kind!='legacy_daily_reservation'")
        self.conn.execute("CREATE TABLE IF NOT EXISTS request_groups(day TEXT,group_name TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,group_name))")
        self.conn.execute("CREATE TABLE IF NOT EXISTS discovery_origins(identity TEXT PRIMARY KEY,priority TEXT NOT NULL)")
        self.conn.commit()

    def get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def day(self):
        return datetime.fromtimestamp(self.now(), timezone(timedelta(hours=8))).date().isoformat()

    def used(self):
        return self.conn.execute("SELECT count(*) FROM requests WHERE day=?", (self.day(),)).fetchone()[0]

    def adopt_legacy_budget(self, catalogue):
        """Charge the transition day conservatively after the legacy cron ran.

        These are explicitly reservation receipts, not fabricated HTTP events.
        Adopt once; the next Beijing day has its ordinary independent allowance.
        """
        if self.get('legacy_budget_adopted'):
            return
        row = catalogue.execute("SELECT last_success FROM source_sync_state WHERE source_id='f95zone'").fetchone()
        stamp = datetime.fromisoformat(row[0]) if row and row[0] else None
        if stamp and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone(timedelta(hours=8)))
        ran_today = stamp and stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat() == self.day()
        with self.conn:
            if ran_today:
                for _ in range(max(0, 27 - self.used())):
                    self.conn.execute("INSERT INTO requests(day,at,url,kind) VALUES(?,?,?,?)",
                                      (self.day(),self.now(),'','legacy_daily_reservation'))
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES('legacy_budget_adopted','1')")

    def reserve(self, url, kind, budget, category=None):
        # Commit before issuing a request: a killed request still consumes budget.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.used() >= budget:
                raise Stop("daily_budget")
            group = 'check' if kind == 'robots' else getattr(self, 'active_group', 'update')
            if (group == 'update' and getattr(self, 'protect_history', False)
                    and budget-self.used() <= self.history_remaining()):
                raise Stop('history_turn')
            self.conn.execute("INSERT INTO request_groups VALUES(?,?,1) ON CONFLICT(day,group_name) DO UPDATE SET used=used+1", (self.day(),group))
            self.conn.execute("INSERT INTO requests(day,at,url,kind,category) VALUES(?,?,?,?,?)", (self.day(), self.now(), url, kind, category or {"listing": "discovery", "cover": "cover", "refresh": "refresh"}.get(kind, "new")))
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_request_at',?)", (str(self.now()),))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def history_remaining(self):
        target = json.loads(self.get('history_target', '{}'))
        used = self.conn.execute("SELECT used FROM request_groups WHERE day=? AND group_name='history'", (self.day(),)).fetchone()
        return max(0,target.get('requests',0)-(used[0] if used else 0)) if target.get('day') == self.day() else 0

    def run_metrics(self, run_id=None):
        row = self.conn.execute("SELECT metrics FROM run_metrics " +
                                ("ORDER BY run_id DESC LIMIT 1" if run_id is None else "WHERE run_id=?"),
                                () if run_id is None else (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def counts(self):
        return dict(self.conn.execute("SELECT category,count(*) FROM requests WHERE day=? AND kind!='legacy_daily_reservation' GROUP BY category", (self.day(),)))

    def kind_counts(self):
        return dict(self.conn.execute("SELECT kind,count(*) FROM requests WHERE day=? AND kind!='legacy_daily_reservation' GROUP BY kind", (self.day(),)))

    def queue_cover(self, identity, url):
        # Keep retry history for the same URL; a changed publisher URL is new work.
        with self.conn:
            self.conn.execute("INSERT INTO cover_tasks(identity,url) VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET url=excluded.url,next_due=CASE WHEN url=excluded.url THEN next_due ELSE 0 END,failed=CASE WHEN url=excluded.url THEN failed ELSE 0 END", (identity, url))

    def enqueue(self, identity, url, label, tags, kind="new"):
        old = self.conn.execute("SELECT tags FROM candidates WHERE identity=?", (identity,)).fetchone()
        merged = sorted(set(tags) | set(json.loads(old[0]) if old else []))
        self.conn.execute("INSERT INTO candidates(identity,url,label,tags,kind) VALUES(?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET tags=excluded.tags", (identity, url, label, json.dumps(merged), kind))
        self.conn.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (url, identity))

    def page(self, stream, candidates, next_url):
        # No truncated discovery: queue and cursor commit together.
        with self.conn:
            for label, url in candidates:
                identity = urlparse(url).path.strip("/").casefold()
                self.enqueue(identity, url, label, [stream["label"]])
            self.conn.execute("UPDATE streams SET cursor=?,last_attempt=?,next_due=? WHERE url=?", (next_url or stream["url"], self.now(), self.now() + (0 if next_url else 86400), stream["url"]))


class BudgetClient(sources.PublicSourceClient):
    def __init__(self, ledger, budget=None, deadline=None, clock=time.monotonic, sleep=time.sleep, daily_limit=None):
        super().__init__()
        self.max_requests = validate_limits("f95zone", budget)[0]
        self.budget = validate_limits("f95zone", daily_limit if daily_limit is not None else max(DEFAULTS["f95zone"][0],self.max_requests))[0]
        self.ledger, self.deadline = ledger, deadline
        self.clock, self.sleep = clock, sleep
        self.kind = "detail"
        self.category = "new"
        self.category_limit = None
        self.cover_urls = {}
        self.publisher_delay = 10

    def html(self, url, host):
        self.cover_urls.pop(url, None)
        content_type, raw = self.get(url, host, "text/html,application/xhtml+xml")
        if "html" not in content_type:
            raise FetchError("invalid_content_type")
        class Page(sources._HTML):
            def handle_starttag(self, tag, attrs):
                super().handle_starttag(tag, attrs)
                values = dict(attrs)
                if tag == "meta" and values.get("property", "").casefold() == "og:image":
                    self.meta["og:image"] = values.get("content", "")
        page = Page()
        page.feed(raw.decode("utf-8", "replace"))
        image = sources._safe_https(urljoin(url, page.meta.get("og:image", "")), host) if page.meta.get("og:image") else None
        if image:
            self.cover_urls[url] = image
        return page

    def get(self, url, host, accept="text/html,application/json,text/plain", *, keep_query=False, _redirects=0):
        url = sources._safe_https(url, sources.F95_HOST)
        if not url or host != sources.F95_HOST or keep_query:
            raise FetchError("unsafe_url")
        if self.requests >= self.max_requests:
            raise Stop("run_budget")
        last = float(self.ledger.get("last_request_at", "0"))
        delay = max(10, self.host_delays.get(host, 10)) - (self.ledger.now() - last)
        if self.deadline is not None and self.clock() + max(0, delay) + sources.TIMEOUT_SECONDS >= self.deadline:
            raise Stop("deadline")
        if delay > 0:
            self.sleep(delay)
        if self.category_limit is not None and self.ledger.counts().get(self.category, 0) >= self.category_limit:
            raise Stop("category_budget")
        self.ledger.reserve(url, "robots" if url.endswith("/robots.txt") else self.kind, self.budget, self.category)
        self.requests += 1
        try:
            with self.opener.open(Request(url, headers={"User-Agent": sources.USER_AGENT, "Accept": accept, "Accept-Encoding": "identity"}), timeout=sources.TIMEOUT_SECONDS) as resp:
                limit = 5 * 1024 * 1024 if accept.startswith("image/") else sources.MAX_RESPONSE_BYTES
                raw = resp.read(limit + 1)
                content_type = resp.headers.get("Content-Type", "").casefold()
                if resp.status != 200:
                    raise FetchError("http_%s" % resp.status, pause=resp.status in (403, 429))
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                target = sources._safe_https(urljoin(url, exc.headers.get("Location", "")), host)
                if not target or _redirects >= 5 or url.endswith("/robots.txt"):
                    raise FetchError("redirect_policy", pause=url.endswith("/robots.txt")) from None
                self.check_robots(sources.F95_BASE, urlparse(target).path, host)
                return self.get(target, host, accept, _redirects=_redirects + 1)
            retry = 0
            value = exc.headers.get("Retry-After", "")
            try:
                retry = max(0, int(value))
            except ValueError:
                try:
                    retry = max(0, parsedate_to_datetime(value).timestamp() - self.ledger.now())
                except (ValueError, TypeError):
                    pass
            raise FetchError("http_%s" % exc.code, pause=exc.code in (403, 429), retry_after=retry) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise FetchError("network_" + type(exc).__name__) from None
        if len(raw) > limit:
            raise FetchError("response_limit")
        text = raw.decode("utf-8", "replace").casefold()
        if any(token in text for token in ("cf-chl-", "verify you are human", "checking your browser", "just a moment...", "captcha")):
            raise FetchError("verification", pause=True)
        if url.endswith("/robots.txt"):
            for value in re.findall(r"(?im)^crawl-delay\s*:\s*([0-9.]+)", text):
                try:
                    self.publisher_delay = max(self.publisher_delay, float(value))
                except ValueError:
                    pass
        return content_type, raw

    def check_robots(self, base, path, host):
        try:
            delay = super().check_robots(base, path, host)
            self.host_delays[host] = max(10, delay, self.publisher_delay)
            # Do not cap publisher crawl delay at the adapter's legacy 30s.
            return self.host_delays[host]
        except FetchError:
            raise
        except sources.SourceError as exc:
            raise FetchError("robots_" + str(exc), pause=True) from None


def listing_candidates(page, url):
    blocked = sources.IGNORED_SLUGS | {"login", "register", "contact", "about", "privacy-policy", "terms-of-service", "dmca", "disclaimer", "sitemap", "games", "home", "faq"}
    result = {}
    for label, href in page.anchors:
        clean = sources._safe_https(urljoin(url, href), sources.F95_HOST)
        if not clean:
            continue
        match = sources.GAME_PATH.fullmatch(urlparse(clean).path)
        if match and match[1].casefold() not in blocked and label.strip():
            result[clean] = label
    return [(label, url) for url, label in result.items()]


def linked_categories(page, url):
    """Only observed tag links in the publisher's genres navigation."""
    found = {}
    for label, href in page.anchors:
        clean = sources._safe_https(urljoin(url, href), sources.F95_HOST)
        if clean and re.fullmatch(r"/tag/[a-z0-9-]+/", urlparse(clean).path, re.I) and label.strip():
            found[clean] = sources._plain(label, 100)
    return found


def next_page(page, current):
    path = urlparse(current).path
    match = re.fullmatch(r"(/tag/[a-z0-9-]+/)(?:page/(\d+)/)?", path, re.I)
    if not match:
        return None
    choices = []
    for _, href in page.anchors:
        clean = sources._safe_https(urljoin(current, href), sources.F95_HOST)
        linked = re.fullmatch(re.escape(match[1]) + r"page/(\d+)/", urlparse(clean).path) if clean else None
        if linked and int(linked[1]) > int(match[2] or 1):
            choices.append((int(linked[1]), clean))
    return min(choices)[1] if choices else None


def fail(ledger, candidate, exc):
    attempts = candidate["attempts"] + 1
    reason = getattr(exc, "reason", "parse_" + type(exc).__name__)
    delay = [3600, 21600, 86400, 604800][min(attempts - 1, 3)]
    if reason == "http_404":
        delay = max(delay, 604800)
    delay = max(delay, getattr(exc, "retry_after", 0))
    with ledger.conn:
        ledger.conn.execute("UPDATE candidates SET attempts=?,last_attempt=?,next_due=?,error=? WHERE identity=?", (attempts, ledger.now(), ledger.now() + delay, reason, candidate["identity"]))
    consecutive = int(ledger.get("consecutive_5xx", "0")) + 1 if re.fullmatch(r"http_5\d\d", reason) or reason.startswith("network_") else 0
    ledger.set("consecutive_5xx", consecutive)
    exc.failure_counted = True
    if getattr(exc, "pause", False) or consecutive >= 3:
        ledger.set("paused", reason)
        ledger.set("pause_until", ledger.now() + delay)
        raise FetchError(reason, pause=True)


def fail_cover(ledger, task, exc):
    attempts = task["failed"] + 1
    reason = getattr(exc, "reason", "cover_" + type(exc).__name__)
    delay = max([3600, 21600, 86400, 604800][min(attempts - 1, 3)], getattr(exc, "retry_after", 0))
    with ledger.conn:
        ledger.conn.execute("UPDATE cover_tasks SET failed=?,last_attempt=?,next_due=?,error=? WHERE identity=?", (attempts, ledger.now(), ledger.now() + delay, reason, task["identity"]))
    consecutive = int(ledger.get("consecutive_5xx", "0")) + 1 if re.fullmatch(r"http_5\d\d", reason) or reason.startswith("network_") else 0
    ledger.set("consecutive_5xx", consecutive)
    if getattr(exc, "pause", False) or consecutive >= 3:
        ledger.set("paused", reason)
        ledger.set("pause_until", ledger.now() + delay)
        raised = FetchError(reason, pause=True)
        raised.failure_counted = True
        raise raised


def valid_cover(data, content_type):
    """F95-only 5 MiB ceiling; keep shared source limits unchanged."""
    mime = (content_type or '').split(';', 1)[0].strip().lower()
    extensions = {'image/png': ('png','PNG'), 'image/jpeg': ('jpg','JPEG'), 'image/webp': ('webp','WEBP')}
    if mime not in extensions or not data or len(data) > 5 * 1024 * 1024:
        return None
    dimensions = sources.slg_source_covers._dimensions_from_header(data, mime)
    if not dimensions or not all(0 < n <= 4096 for n in dimensions) or math.prod(dimensions) > 16777216:
        return None
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            if image.format != extensions[mime][1] or image.size != dimensions or getattr(image, 'n_frames', 1) != 1:
                return None
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
    except Exception:
        return None
    return (extensions[mime][0], *dimensions)


def store_cover(filename, payload):
    """Atomically store an already decoded F95 image, independent of legacy caps."""
    if (not isinstance(payload, bytes) or not 0 < len(payload) <= 5*1024*1024
            or not re.fullmatch(r'cover-[0-9a-f]{24}\.(png|jpg|webp)', filename)):
        return None
    directory = Path(slg_db.covers_dir())
    target = directory/filename
    temporary = None
    try:
        if directory.is_symlink() or target.is_symlink():
            return None
        if target.exists():
            if target.stat().st_size != len(payload):
                return None
            return filename if hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(payload).digest() else None
        fd, temporary = tempfile.mkstemp(prefix='.f95-cover-',dir=directory)
        with os.fdopen(fd,'wb') as output:
            output.write(payload);output.flush();os.fsync(output.fileno())
        os.replace(temporary,target)
        return filename
    except (OSError, ValueError):
        return None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def supplement_cover(ledger, catalogue, client, task, catalogue_lock):
    old = catalogue.execute("SELECT g.cover_file,gs.game_id FROM games g JOIN game_sources gs ON gs.game_id=g.id WHERE gs.source_id='f95zone' AND gs.external_id=?", (task["identity"],)).fetchone()
    if old and old[0]:
        with ledger.conn:
            ledger.conn.execute("DELETE FROM cover_tasks WHERE identity=?", (task["identity"],))
        return 0
    if not old:
        raise FetchError("cover_fact_missing")
    client.check_robots(sources.F95_BASE, urlparse(task["url"]).path, sources.F95_HOST)
    mime, payload = client.get(task["url"], sources.F95_HOST, "image/png,image/jpeg,image/webp")
    valid = valid_cover(payload, mime)
    if not valid:
        raise FetchError("invalid_cover")
    filename = "cover-" + hashlib.sha256(payload).hexdigest()[:24] + "." + valid[0]
    stored = store_cover(filename, payload)
    if not stored:
        raise FetchError("cover_store_failed")
    def save():
        ledger.set("pending_publish", "1")
        row = catalogue.execute("SELECT game_id FROM game_sources WHERE source_id='f95zone' AND external_id=?", (task["identity"],)).fetchone()
        if not row:
            raise FetchError("cover_fact_missing")
        cursor = catalogue.execute("UPDATE games SET cover_file=? WHERE id=? AND (cover_file IS NULL OR cover_file='')", (stored, row[0]))
        return cursor.rowcount
    if catalogue_lock:
        with file_lock(catalogue_lock), catalogue:
            changed = save()
    else:
        with catalogue:
            changed = save()
    with ledger.conn:
        ledger.conn.execute("DELETE FROM cover_tasks WHERE identity=?", (task["identity"],))
    ledger.set("consecutive_5xx", 0)
    return changed


def needs_ingest(catalogue, record):
    """Avoid refreshing last_synced/export timestamps for equivalent facts."""
    old = catalogue.execute("SELECT * FROM game_sources WHERE source_id=? AND external_id=?", (record["source_id"], record["external_id"])).fetchone()
    if not old:
        return True
    for field, key in (("source_url", "url"), ("source_title", "title"), ("source_version", "version"), ("developer", "developer"), ("engine", "engine"), ("source_modified", "modified")):
        if record.get(key) is not None and record[key] != old[field]:
            return True
    if (record.get("overview") and len(record["overview"]) >= len(old["overview"] or "") and record["overview"] != old["overview"]):
        return True
    return set(record.get("tags") or ()) != set(json.loads(old["tags_json"] or "[]"))


def run(ledger, catalogue, client, catalogue_lock=None):
    started = ledger.now()
    with ledger.conn:
        run_id = ledger.conn.execute("INSERT INTO runs(started,heartbeat,status) VALUES(?,?,'running')", (started, started)).lastrowid
        # A killed process cannot hold the collector lock; mark its stale run.
        ledger.conn.execute("UPDATE runs SET status='interrupted',finished=? WHERE status='running' AND id<>?", (started, run_id))
    metrics = dict(new_verified=0, facts_updated=0, covers_added=0, failed=0)
    def save_metrics():
        with ledger.conn:
            ledger.conn.execute("INSERT OR REPLACE INTO run_metrics VALUES(?,?)", (run_id, json.dumps(metrics)))
    def fact_signature(identity):
        row = catalogue.execute("SELECT source_url,source_title,source_version,developer,engine,source_modified,overview,tags_json FROM game_sources WHERE source_id='f95zone' AND external_id=?", (identity,)).fetchone()
        return tuple(row) if row else None
    save_metrics()
    status, reason, processed, failed, changed = "completed", None, 0, 0, 0
    try:
        if ledger.get("paused"):
            raise FetchError(ledger.get("paused"), pause=True)
        with ledger.conn:
            for slug, url, label in (("strategy", sources.F95_LISTING, "Strategy"),) + sources.F95_EXTRA_CATEGORIES:
                ledger.conn.execute("INSERT OR IGNORE INTO streams(url,label,cursor) VALUES(?,?,?)", (url, label, url))
            for row in catalogue.execute("SELECT external_id,source_url,source_title,tags_json FROM game_sources WHERE source_id='f95zone'"):
                if sources._safe_https(row["source_url"], sources.F95_HOST):
                    ledger.enqueue(row["external_id"], row["source_url"], row["source_title"], json.loads(row["tags_json"] or "[]"), "refresh")
        client.kind, client.category, client.category_limit = 'listing', 'check', None
        client.check_robots(sources.F95_BASE, urlparse(sources.F95_LISTING).path, sources.F95_HOST)
        target = json.loads(ledger.get('history_target','{}'))
        if target.get('day') != ledger.day():
            prior = ledger.conn.execute("SELECT used FROM request_groups WHERE day=? AND group_name='history'", (ledger.day(),)).fetchone()
            ledger.set('history_target',json.dumps({'day':ledger.day(),'requests':(prior[0] if prior else 0)+math.ceil(max(0,client.budget-ledger.used())*.2)}))
        # Refresh the observed newest category head once each day without moving
        # its independent historical pagination cursor backwards.
        if client.budget >= 10 and ledger.get('latest_head_day') != ledger.day() and ledger.counts().get('discovery',0) < 6:
            ledger.active_group = 'update'
            client.kind, client.category = 'listing', 'discovery'
            page = client.html(sources.F95_LISTING,sources.F95_HOST)
            with ledger.conn:
                for label,url in listing_candidates(page,sources.F95_LISTING):
                    identity = urlparse(url).path.strip("/").casefold()
                    ledger.enqueue(identity,url,label,["Strategy"])
                    ledger.conn.execute("INSERT INTO discovery_origins VALUES(?,'update') ON CONFLICT(identity) DO UPDATE SET priority='update'",(identity,))
                ledger.conn.execute("INSERT OR REPLACE INTO meta VALUES('latest_head_day',?)",(ledger.day(),))
        refresh_quota = math.ceil(client.budget / 3)
        # Daily discovery receipts survive restart; tiny fixture/acceptance budgets
        # retain one-page opportunistic discovery instead of consuming six calls.
        discovery_quota = max(6 if client.budget < 10 else min(6,max(2,client.budget//9)),ledger.history_remaining()+ledger.counts().get("discovery",0))
        discovered = ledger.counts().get("discovery", 0) >= discovery_quota
        prefer_cover = True
        while True:
            ledger.conn.execute("UPDATE runs SET heartbeat=? WHERE id=?", (ledger.now(), run_id))
            ledger.conn.commit()
            pending = ledger.conn.execute("SELECT * FROM candidates WHERE next_due<=? ORDER BY last_attempt,next_due,identity", (ledger.now(),)).fetchall()
            refresh = [c for c in pending if c['kind'] == 'refresh']
            origins = dict(ledger.conn.execute('SELECT identity,priority FROM discovery_origins'))
            history = [c for c in pending if c['kind']=='new' and origins.get(c['identity'],'history')=='history']
            updates = [c for c in pending if c['kind']=='new' and origins.get(c['identity'])=='update']
            refresh_used = ledger.counts().get('refresh',0)
            cover = ledger.conn.execute('SELECT * FROM cover_tasks WHERE next_due<=? ORDER BY last_attempt,next_due,identity LIMIT 1',(ledger.now(),)).fetchone()
            stream_due = ledger.conn.execute('SELECT 1 FROM streams WHERE next_due<=? LIMIT 1',(ledger.now(),)).fetchone()
            ledger.protect_history = client.budget >= 10 and bool(history or (not discovered and stream_due))
            history_turn = ledger.protect_history and client.budget-ledger.used() <= ledger.history_remaining()
            # Updates take the main share; history gets its protected remainder.
            candidate = history[0] if history_turn and history else (updates[0] if updates else (refresh[0] if refresh and refresh_used<refresh_quota else None))
            if candidate is None and discovered:
                candidate = history[0] if history else (refresh[0] if refresh else None)
            if client.budget < 10:
                candidate = refresh[0] if refresh and refresh_used < refresh_quota else next((c for c in pending if c['kind']=='new'),None)
                if candidate is None and discovered and pending:
                    candidate = pending[0]
            discovery_turn = not discovered and (not candidate or (history_turn and client.budget >= 10))
            if discovery_turn:
                candidate = None
            ledger.active_group = 'history' if candidate in history else 'update'
            if cover and not history_turn and (discovered or client.budget < 10) and (prefer_cover or candidate is None):
                ledger.active_group = "update"
                client.kind, client.category, client.category_limit = "cover", "cover", None
                try:
                    added = supplement_cover(ledger, catalogue, client, cover, catalogue_lock)
                    changed += added
                    metrics['covers_added'] += added
                    save_metrics()
                except Stop as exc:
                    if str(exc) == "history_turn":
                        continue
                    raise
                except (sources.SourceError, ValueError, OSError) as exc:
                    failed += 1
                    metrics["failed"] += 1
                    save_metrics()
                    fail_cover(ledger, cover, exc)
                prefer_cover = False
                continue
            if candidate is not None and candidate["kind"] == "refresh" and refresh_used >= refresh_quota and not discovered:
                candidate = None
            if candidate is None and not discovered:
                ledger.active_group = "history"
                client.kind, client.category, client.category_limit = "listing", "discovery", discovery_quota
                # Periodically expand from real publisher navigation, never
                # generate guessed tag names or pagination URLs.
                if float(ledger.get("genres_due", "0")) <= ledger.now() and client.budget - ledger.used() >= 3:
                    nav = sources.F95_BASE + "/genres/"
                    try:
                        client.check_robots(sources.F95_BASE, "/genres/", sources.F95_HOST)
                        page = client.html(nav, sources.F95_HOST)
                    except Stop as exc:
                        if str(exc) != "category_budget":
                            raise
                        discovered = True
                        continue
                    except FetchError as exc:
                        attempts = int(ledger.get("genres_attempts", "0")) + 1
                        delay = max([3600, 21600, 86400, 604800][min(attempts - 1, 3)], exc.retry_after)
                        ledger.set("genres_attempts", attempts)
                        ledger.set("genres_error", exc.reason)
                        ledger.set("genres_due", ledger.now() + delay)
                        raise
                    with ledger.conn:
                        for url, label in linked_categories(page, nav).items():
                            ledger.conn.execute("INSERT OR IGNORE INTO streams(url,label,cursor) VALUES(?,?,?)", (url, label, url))
                    ledger.set("genres_due", ledger.now() + 604800)
                    ledger.set("genres_attempts", 0)
                stream = ledger.conn.execute("SELECT * FROM streams WHERE next_due<=? ORDER BY last_attempt,url LIMIT 1", (ledger.now(),)).fetchone()
                if stream:
                    try:
                        client.check_robots(sources.F95_BASE, urlparse(stream["cursor"]).path, sources.F95_HOST)
                        page = client.html(stream["cursor"], sources.F95_HOST)
                        ledger.page(stream, listing_candidates(page, stream["cursor"]), next_page(page, stream["cursor"]))
                        ledger.set("consecutive_5xx", 0)
                    except Stop as exc:
                        if str(exc) != "category_budget":
                            raise
                        discovered = True
                        continue
                    except FetchError as exc:
                        attempts = stream["attempts"] + 1
                        delay = max([3600, 21600, 86400, 604800][min(attempts - 1, 3)], exc.retry_after)
                        with ledger.conn:
                            ledger.conn.execute("UPDATE streams SET attempts=?,error=?,last_attempt=?,next_due=? WHERE url=?", (attempts, exc.reason, ledger.now(), ledger.now() + delay, stream["url"]))
                        consecutive = int(ledger.get("consecutive_5xx", "0")) + 1 if re.fullmatch(r"http_5\d\d", exc.reason) or exc.reason.startswith("network_") else 0
                        ledger.set("consecutive_5xx", consecutive)
                        exc.failure_counted = True
                        if consecutive >= 3:
                            exc.pause = True
                        if exc.pause:
                            ledger.set("pause_until", ledger.now() + delay)
                        raise
                discovered = (not stream or client.budget < 10
                              or ledger.counts().get("discovery", 0) >= discovery_quota)
                continue
            if candidate is None:
                break
            client.kind, client.category = "detail", candidate["kind"]
            client.category_limit = refresh_quota if candidate["kind"] == "refresh" and not discovered else None
            prefer_cover = True
            try:
                record = sources._f95_record(client, candidate["url"], candidate["label"], json.loads(candidate["tags"]))
                if not record or not record.get("title"):
                    raise FetchError("invalid_detail")
                image = getattr(client, "cover_urls", {}).get(candidate["url"])
                existing_cover = catalogue.execute("SELECT g.cover_file FROM games g JOIN game_sources gs ON gs.game_id=g.id WHERE gs.source_id='f95zone' AND gs.external_id=?", (record["external_id"],)).fetchone()
                if image and not (existing_cover and existing_cover[0]):
                    ledger.queue_cover(record["external_id"], image)
                before_facts = fact_signature(record["external_id"])
                # Network finishes before shared catalogue lock/transaction.
                if catalogue_lock:
                    with file_lock(catalogue_lock), catalogue:
                        if needs_ingest(catalogue, record):
                            ledger.set("pending_publish", "1")
                            sources._ingest_record(catalogue, record)
                            changed += 1
                else:
                    with catalogue:
                        if needs_ingest(catalogue, record):
                            ledger.set("pending_publish", "1")
                            sources._ingest_record(catalogue, record)
                            changed += 1
                after_facts = fact_signature(record["external_id"])
                if before_facts is None and after_facts is not None:
                    metrics['new_verified'] += 1
                elif before_facts != after_facts:
                    metrics['facts_updated'] += 1
                save_metrics()
                with ledger.conn:
                    ledger.conn.execute("UPDATE candidates SET kind='refresh',attempts=0,last_attempt=?,last_success=?,next_due=?,error=NULL WHERE identity=?", (ledger.now(), ledger.now(), ledger.now() + 604800, candidate["identity"]))
                ledger.set("consecutive_5xx", 0)
                processed += 1
            except Stop as exc:
                if str(exc) in ("category_budget", "history_turn"):
                    continue
                raise
            except (sources.SourceError, ValueError, OSError) as exc:
                failed += 1
                metrics["failed"] += 1
                save_metrics()
                fail(ledger, candidate, exc)
    except Stop as exc:
        status, reason = "partial", str(exc)
    except FetchError as exc:
        if not getattr(exc, "failure_counted", False):
            consecutive = int(ledger.get("consecutive_5xx", "0")) + 1 if re.fullmatch(r"http_5\d\d", exc.reason) or exc.reason.startswith("network_") else 0
            ledger.set("consecutive_5xx", consecutive)
            exc.pause = exc.pause or consecutive >= 3
        status, reason = ("paused" if exc.pause else "error"), exc.reason
        if exc.pause:
            ledger.set("paused", reason)
            ledger.set("pause_until", max(float(ledger.get("pause_until", "0")), ledger.now() + max(3600, exc.retry_after)))
    except Exception as exc:
        status, reason = "error", type(exc).__name__
    if failed and status == "completed":
        status, reason = "partial", "item_failures"
    with ledger.conn:
        ledger.conn.execute("UPDATE runs SET status=?,finished=?,heartbeat=? WHERE id=?", (status, ledger.now(), ledger.now(), run_id))
    save_metrics()
    return {"metrics": dict(metrics), "source_id": "f95zone", "source_label": MIRROR_LABEL, "status": status, "reason": reason, "processed": processed, "changed": changed, "failed": failed, "requests_today": ledger.used(), "daily_limit": client.budget, "request_counts": ledger.counts(), "request_kind_counts": ledger.kind_counts(), "actual_requests_today": sum(ledger.counts().values()), "run_id": run_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--catalogue-db", required=True)
    parser.add_argument("--catalogue-lock")
    parser.add_argument("--budget", type=int, default=None)
    parser.add_argument("--max-requests", type=int, dest="budget")
    parser.add_argument("--daily-limit",type=int,help="Persistent daily ceiling independent of the invocation limit")
    parser.add_argument("--status-file")
    parser.add_argument("--dry-run", action="store_true", help="zero-network database and lock validation")
    parser.add_argument("--deadline-seconds", type=int, default=None)
    parser.add_argument("--max-seconds", type=int, dest="deadline_seconds")
    parser.add_argument("--resume", action="store_true", help="explicit operator recheck after source pause; Retry-After still enforced")
    args = parser.parse_args(argv)
    try:
        args.budget, args.deadline_seconds = validate_limits("f95zone", args.budget, args.deadline_seconds)
        daily_limit = args.daily_limit if args.daily_limit is not None else max(DEFAULTS["f95zone"][0],args.budget)
        validate_limits("f95zone",daily_limit,args.deadline_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(state / "collector.lock"):
            ledger = Ledger(state / "ledger.sqlite3")
            if args.resume and ledger.now() >= float(ledger.get("pause_until", "0")):
                ledger.set("paused", "")
            catalogue = slg_db.connect(args.catalogue_db)
            try:
                ledger.adopt_legacy_budget(catalogue)
                result = {"source_id": "f95zone", "status": "completed", "reason": "dry_run", "processed": 0, "requests_today": ledger.used()} if args.dry_run else run(ledger, catalogue, BudgetClient(ledger, args.budget, time.monotonic() + args.deadline_seconds, daily_limit=daily_limit), args.catalogue_lock)
                if result.get("processed"):
                    ledger.set("last_success", ledger.now())
                due = ledger.conn.execute("SELECT count(*),min(NULLIF(next_due,0)) FROM (SELECT next_due FROM candidates UNION ALL SELECT next_due FROM cover_tasks) WHERE next_due<=?", (ledger.now(),)).fetchone()
                latest = ledger.conn.execute("SELECT started,finished FROM runs ORDER BY id DESC LIMIT 1").fetchone()
                result.update(state="complete" if result["status"] == "completed" else result["status"], reason_code=result.get("reason"), started_at=float(latest[0] if latest else ledger.now()), finished_at=float(ledger.now()), last_success=float(ledger.get("last_success")) if ledger.get("last_success") else None, last_publish=float(ledger.get("last_publish")) if ledger.get("last_publish") else None, requests_used=ledger.used(), pending_due=due[0], oldest_pending_at=float(due[1]) if due[1] is not None else None)
                status_path = Path(args.status_file) if args.status_file else state / "status.json"
                status_path.parent.mkdir(parents=True, exist_ok=True)
                # The service wrapper owns snapshot publication and records its
                # timestamp here. Preserve it across subsequent collector runs.
                if result["last_publish"] is None and status_path.exists():
                    try:
                        previous = json.loads(status_path.read_text(encoding="utf-8"))
                        previous_stamp = previous.get("last_publish")
                        result["last_publish"] = float(previous_stamp) if previous_stamp is not None else None
                    except (OSError, ValueError, TypeError):
                        pass
                temporary = status_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, status_path)
            finally:
                catalogue.close()
                ledger.conn.close()
    except OSError as exc:
        result = {"source_id": "f95zone", "status": "error", "reason": "lock_or_io_" + type(exc).__name__}
    if "state" not in result:
        result.update(state="error", reason_code=result.get("reason"), started_at=None, finished_at=time.time(), last_success=None, last_publish=None, requests_used=0, pending_due=0, oldest_pending_at=None)
    print(json.dumps(result, ensure_ascii=False))
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
