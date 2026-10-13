"""Resumable independent DLsite ledger and immutable publication assembly.

Run through tools/run_dlsite_worker.py. This never writes the SLG catalogue or
cloud database. Only positively observed public candidates are requested.
"""
from contextlib import contextmanager
import hashlib
from slg_dlsite_recovery import pause_decision, retry_delay, safe_diagnostic
import datetime
import email.utils
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import slg_dlsite
from slg_collector_policy import DEFAULTS, validate_limits
from slg_dlsite_online import atomic_bytes, normalise_image, validate_cover_index

DETAIL_TTL = 7 * 24 * 3600
COVER_TTL = 30 * 24 * 3600
_IMAGE_HOST = 'img.dlsite.jp'


def validate_artwork_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != _IMAGE_HOST or parsed.port
            or parsed.username or parsed.password or parsed.fragment or parsed.query
            or not parsed.path.startswith('/modpub/images2/work/')
            or not parsed.path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
            or '..' in parsed.path or '%' in parsed.path):
        raise ValueError('Not an official main artwork URL')
    return url


class _ArtworkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('property') == 'og:image':
            self.urls.append(attrs.get('content', ''))
        if tag in ('img', 'source'):
            candidates = [attrs.get(key, '') for key in ('data-src', 'src')]
            for key in ('srcset', 'data-srcset'):
                candidates.extend(part.strip().split()[0] for part in attrs.get(key, '').split(',') if part.strip())
            # Every candidate is subsequently checked against the requested RJ.
            self.urls.extend(candidates)


def parse_main_artwork(text, external_id):
    """Extract one identity-bound official main image; no synthesized URL."""
    parser = _ArtworkParser()
    parser.feed(text)
    for url in parser.urls:
        if url.startswith('//'):
            url = 'https:' + url
        try:
            validate_artwork_url(url)
        except ValueError:
            continue
        if re.search(r'/' + re.escape(external_id) + r'_img_main\.(jpg|jpeg|png|webp)$', url, re.I):
            return url
    return None


class _ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never switch hosts, even when the destination looks like an image.
        validate_artwork_url(newurl)
        raise slg_dlsite.AccessStopped('Artwork redirect refused; inspect source changes')


class WorkerClient(slg_dlsite.PublicClient):
    def __init__(self, cache_dir, max_requests=None, max_seconds=None, **kwargs):
        max_requests, max_seconds = validate_limits('dlsite', max_requests, max_seconds)
        monotonic = kwargs.pop('monotonic', time.monotonic)
        super().__init__(cache_dir, max_requests=max_requests, **kwargs)
        self.monotonic = monotonic
        self.deadline = self.monotonic() + max_seconds
        self.image_robots = None
        self.image_opener = urllib.request.build_opener(_ImageRedirect())

    def pace(self):
        remaining = self.interval - (self.clock() - self.last_request)
        if self.monotonic() + max(remaining, 0) >= self.deadline:
            raise slg_dlsite.RequestBudgetReached('Time budget reached')
        super().pace()
        if self.monotonic() >= self.deadline:
            raise slg_dlsite.RequestBudgetReached('Time budget reached')

    def request_timeout(self):
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise slg_dlsite.RequestBudgetReached('Time budget reached')
        return min(30, remaining)

    def _image_request(self, url, limit):
        if self.requests >= self.max_requests:
            raise slg_dlsite.RequestBudgetReached('Request budget reached')
        self.pace()
        self.request_kind = 'check' if url.endswith('/robots.txt') else 'work'
        if self.on_request:
            self.on_request()
        self.request_kind = 'work'
        self.requests += 1
        try:
            request = urllib.request.Request(url, headers={'User-Agent': slg_dlsite.USER_AGENT})
            with self.image_opener.open(request, timeout=self.request_timeout()) as response:
                if response.geturl() != url:
                    raise self._image_stopped('verification_gate',url,status=response.status)
                if response.status != 200:
                    raise urllib.error.HTTPError(url, response.status, 'Artwork response failure', response.headers, None)
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise ValueError('Oversized artwork response')
                if 'text/html' in response.headers.get('Content-Type', '').lower():
                    raise self._image_stopped('verification_gate',url,status=response.status)
                return raw
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                raise self._image_stopped('http_'+str(error.code),url,error,error.code,(error.headers or {}).get('Retry-After')) from error
            if error.code>=500:
                raise self._image_stopped('server_error',url,error,error.code,(error.headers or {}).get('Retry-After')) from error
            raise
        except (urllib.error.URLError,OSError) as error:
            raise self._image_stopped('network',url,error) from error

    def _image_stopped(self,kind,url,error=None,status=None,retry=None):
        self.last_diagnostic=safe_diagnostic(kind,'artwork_robots' if url.endswith('/robots.txt') else 'artwork',url,status,error,now=self.clock())
        slg_dlsite.atomic_write(self.cache_dir / 'access-diagnostic.json',self.last_diagnostic)
        return slg_dlsite.AccessStopped('Artwork access check: '+kind,retry,kind,self.last_diagnostic)

    def fetch_image(self, url):
        validate_artwork_url(url)
        if self.image_robots is None:
            robots_url = 'https://' + _IMAGE_HOST + '/robots.txt'
            try:
                raw = self._image_request(robots_url, 512 * 1024)
                policy = slg_dlsite.RobotsPolicy(raw.decode('utf-8', 'replace'))
                status = 'available'
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise self._image_stopped('http_'+str(error.code),robots_url,error,error.code) from error
                # RFC 9309 2.3.1.3: an unavailable robots file may be treated
                # as no rules. Only the observed 404 qualifies; access refusal
                # (403/429), 5xx and network failure remain fail-closed.
                policy = slg_dlsite.RobotsPolicy('User-agent: *\nDisallow:\n')
                status = 'unavailable_404_no_rules'
            except urllib.error.URLError as error:
                raise self._image_stopped('network',robots_url,error) from error
            self.image_robots = policy
            if not self.image_robots.groups:
                raise self._image_stopped('robots_parse',robots_url)
            self.interval = max(self.interval, self.image_robots.crawl_delay(slg_dlsite.USER_AGENT) or 0)
            slg_dlsite.atomic_write(self.cache_dir / 'artwork-robots-policy.json', {
                'url': robots_url, 'checked_at': slg_dlsite.utc_now(), 'status': status,
                'http_status': 404 if status.startswith('unavailable') else 200,
                'interval_seconds': self.interval,
                'policy_reference': 'https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3',
            })
        if not self.image_robots.can_fetch(slg_dlsite.USER_AGENT, url):
            raise slg_dlsite.PathDisallowed('Artwork path disallowed by robots',kind='robots_path')
        return self._image_request(url, 5 * 1024 * 1024)


@contextmanager
def worker_lock(directory, lock_path=None):
    """OS advisory lock automatically released after process failure."""
    path = Path(lock_path) if lock_path else Path(directory) / 'collector.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, 'a+b')
    handle.seek(0)
    if path.stat().st_size == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise RuntimeError('DLsite worker already running') from error
    try:
        yield
    finally:
        handle.close()


class HistoryTurn(slg_dlsite.RequestBudgetReached):
    """Switch to the protected history queue without ending this run."""


class ProtocolChecked(Exception):
    """Internal control flow: acquisition has not run; only protocol passed."""


class Ledger:
    def __init__(self, directory, clock=time.time, max_requests=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.max_requests = validate_limits('dlsite', max_requests, None)[0]
        self.conn = sqlite3.connect(self.directory / 'ledger.db')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS works(
              external_id TEXT PRIMARY KEY,url TEXT NOT NULL,discovered_at REAL NOT NULL,
              checked_at REAL NOT NULL DEFAULT 0,item TEXT,image_url TEXT,
              cover TEXT,cover_checked REAL NOT NULL DEFAULT 0,attempted_at REAL NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0,last_error TEXT);
            CREATE TABLE IF NOT EXISTS pages(url TEXT PRIMARY KEY,checked_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS runs(started_at REAL,finished_at REAL,requests INTEGER,status TEXT,reason TEXT);
        ''')
        self._migrate()
        self.conn.execute('CREATE TABLE IF NOT EXISTS run_metrics(run_id INTEGER PRIMARY KEY,started_at REAL,finished_at REAL,status TEXT,metrics TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS page_schedule(url TEXT PRIMARY KEY,next_due REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS request_lanes(day TEXT,lane TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,lane))')
        self.conn.execute('CREATE TABLE IF NOT EXISTS request_groups(day TEXT,group_name TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,group_name))')
        self.conn.execute('CREATE TABLE IF NOT EXISTS discovery_origins(kind TEXT,identity TEXT,priority TEXT NOT NULL,PRIMARY KEY(kind,identity))')
        self.conn.execute('INSERT OR IGNORE INTO page_schedule(url) SELECT url FROM pages')
        self.conn.commit()

    def _migrate(self):
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if version > 2:
            raise ValueError('Unsupported ledger schema')
        if version == 2:
            return
        backup = sqlite3.connect(self.directory / ('ledger-schema-%s.bak' % version))
        try:
            self.conn.backup(backup)
        finally:
            backup.close()
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in self.conn.execute('PRAGMA table_info(works)')}
            for name, kind in [('next_fact_due', 'REAL NOT NULL DEFAULT 0'),
                               ('next_cover_due', 'REAL NOT NULL DEFAULT 0'),
                               ('fact_failures', 'INTEGER NOT NULL DEFAULT 0'),
                               ('cover_failures', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    self.conn.execute('ALTER TABLE works ADD COLUMN ' + name + ' ' + kind)
            self.conn.execute('CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.conn.execute('CREATE TABLE IF NOT EXISTS budget(day TEXT PRIMARY KEY,used INTEGER NOT NULL)')
            self.conn.execute('CREATE TABLE IF NOT EXISTS events(at REAL,kind TEXT,collection TEXT,publish TEXT)')
            # Carry over actual legacy runs: upgrading must not reset today's budget.
            legacy_days = {}
            for started, used in self.conn.execute('SELECT started_at,requests FROM runs'):
                if type(used) is not int or used < 0:
                    raise ValueError('Invalid legacy request accounting')
                day = datetime.datetime.fromtimestamp(started, datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                legacy_days[day] = legacy_days.get(day, 0) + used
            for day, used in legacy_days.items():
                self.conn.execute('INSERT INTO budget(day,used) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET used=MAX(budget.used,excluded.used)', (day,used))
            self.conn.execute('UPDATE works SET next_fact_due=CASE WHEN checked_at=0 THEN 0 ELSE checked_at+? END, next_cover_due=CASE WHEN cover_checked=0 THEN 0 ELSE cover_checked+? END', (DETAIL_TTL, DETAIL_TTL))
            self.conn.execute('PRAGMA user_version=2')
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _get(self, key, default=None):
        row = self.conn.execute('SELECT value FROM control WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.conn.execute('INSERT OR REPLACE INTO control VALUES(?,?)', (key, json.dumps(value)))

    def event(self, kind, collection, publication='pending'):
        self.conn.execute('INSERT INTO events VALUES(?,?,?,?)', (self.clock(), kind, collection, publication))
        self._set('state', collection)
        self.conn.commit()

    def _day(self):
        return datetime.datetime.fromtimestamp(self.clock(), datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()

    def daily_new_count(self):
        day_start=datetime.datetime.fromisoformat(self._day()).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
        return sum(int(json.loads(row[0]).get('new_verified',0)) for row in self.conn.execute('SELECT metrics FROM run_metrics WHERE started_at>=? AND started_at<?',(day_start,day_start+86400)))

    def reserve_request(self):
        day = self._day()
        self.conn.execute('BEGIN IMMEDIATE')
        row = self.conn.execute('SELECT used FROM budget WHERE day=?', (day,)).fetchone()
        if row and row[0] >= self.max_requests:
            self.conn.rollback()
            raise slg_dlsite.RequestBudgetReached('Beijing daily request budget reached')
        group = getattr(self, '_active_group', 'update')
        if (getattr(self, '_protect_history', False) and group == 'update'
                and getattr(getattr(self, '_client', None), 'request_kind', '') != 'check'
                and self.max_requests - (row[0] if row else 0) <= self.history_remaining()):
            self.conn.rollback()
            raise HistoryTurn('Protected history turn')
        self.conn.execute('INSERT INTO request_groups VALUES(?,?,1) ON CONFLICT(day,group_name) DO UPDATE SET used=used+1', (day,group))
        self.conn.execute('INSERT INTO budget VALUES(?,1) ON CONFLICT(day) DO UPDATE SET used=used+1', (day,))
        lane = 'check' if getattr(getattr(self, '_client', None), 'request_kind', '') == 'check' else getattr(self, '_active_lane', 'check')
        self.conn.execute('INSERT INTO request_lanes VALUES(?,?,1) ON CONFLICT(day,lane) DO UPDATE SET used=used+1', (day, lane))
        self.conn.commit()
        self.event('heartbeat', 'running')

    def status(self):
        used = self.conn.execute('SELECT used FROM budget WHERE day=?', (self._day(),)).fetchone()
        pending = self.conn.execute('SELECT count(*),min(NULLIF(due,0)) FROM (SELECT next_fact_due AS due FROM works WHERE next_fact_due<=? UNION ALL SELECT next_cover_due AS due FROM works WHERE item IS NOT NULL AND image_url IS NOT NULL AND next_cover_due<=?)', (self.clock(), self.clock())).fetchone()
        return {key: self._get(key) for key in ('last_success', 'last_publish', 'state', 'reason', 'active_hash')} | {'request_used': used[0] if used else 0, 'pending': pending[0], 'oldest_due': pending[1]}

    def run_metrics(self, run_id=None):
        row = self.conn.execute('SELECT metrics FROM run_metrics ' +
                                ('ORDER BY run_id DESC LIMIT 1' if run_id is None else 'WHERE run_id=?'),
                                () if run_id is None else (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def lane_used(self, lane):
        row = self.conn.execute('SELECT used FROM request_lanes WHERE day=? AND lane=?', (self._day(), lane)).fetchone()
        return row[0] if row else 0

    def history_remaining(self):
        target = self._get('history_target', {})
        if target.get('day') != self._day():
            return 0
        used = self.conn.execute("SELECT used FROM request_groups WHERE day=? AND group_name='history'", (self._day(),)).fetchone()
        return max(0, target['requests'] - (used[0] if used else 0))

    def close(self):
        self.conn.close()

    def discover(self, urls, pages=(), priority="history"):
        if priority not in ('history', 'update'):
            raise ValueError('Invalid discovery priority')
        for url in urls:
            external = slg_dlsite.product_id(url)
            self.conn.execute("INSERT INTO discovery_origins VALUES(?,?,?) ON CONFLICT(kind,identity) DO UPDATE SET priority=CASE WHEN excluded.priority='update' THEN excluded.priority ELSE priority END", ('work',external,priority))
            self.conn.execute('INSERT OR IGNORE INTO works(external_id,url,discovered_at) VALUES(?,?,?)',
                              (external, slg_dlsite.product_url(external), self.clock()))
        for url in pages:
            try:
                slg_dlsite.validate_url(url)
            except ValueError:
                continue
            self.conn.execute('INSERT OR IGNORE INTO pages(url) VALUES(?)', (url,))
            self.conn.execute('INSERT OR IGNORE INTO page_schedule(url) VALUES(?)', (url,))
        self.conn.commit()

    def seed(self, facts_path):
        catalogue = json.loads(Path(facts_path).read_text(encoding='utf-8'))
        if catalogue.get('catalog_id') != 'dlsite':
            raise ValueError('Wrong seed catalogue')
        for item in catalogue['items']:
            external = slg_dlsite.product_id(item['url'])
            if item['external_id'] != external or item['slug'] != 'dlsite-' + external.lower():
                raise ValueError('Invalid seed identity')
            # Existing online facts are authoritative; a repeated seed never overwrites them.
            self.conn.execute('INSERT OR IGNORE INTO works(external_id,url,discovered_at,item) VALUES(?,?,?,?)',
                (external, item['url'], self.clock(), json.dumps(item, ensure_ascii=False)))
        self.conn.commit()

    def items(self):
        return [json.loads(row[0]) for row in self.conn.execute('SELECT item FROM works WHERE item IS NOT NULL ORDER BY external_id')]

    def covers(self):
        result = {}
        for row in self.conn.execute('SELECT external_id,cover FROM works WHERE item IS NOT NULL AND cover IS NOT NULL'):
            item = json.loads(row['cover'])
            path = self.directory / 'covers' / item['filename']
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']:
                result['dlsite-' + row['external_id'].lower()] = item
        return validate_cover_index(result)

    def adopt_publication(self, output):
        """Restore missing public facts/covers only after validating all active files.

        Existing ledger facts and valid artwork win. Corrupt activity aborts
        before collection/publication, preventing a reduced replacement set.
        """
        from slg_dlsite_online import validate_image, validate_manifest, validate_snapshot
        output = Path(output)
        manifest_path = output / 'catalogues/dlsite/manifest.json'
        if not manifest_path.is_file():
            return {'adopted': 0, 'skipped': 0}
        manifest = validate_manifest(json.loads(manifest_path.read_text(encoding='utf-8')))
        snapshot = output / manifest['db_path'].lstrip('/')
        index_path = output / manifest['cover_index_path'].lstrip('/')
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != manifest['db_sha256']:
            raise ValueError('Bootstrap publication database hash mismatch')
        raw_index = index_path.read_bytes()
        if hashlib.sha256(raw_index).hexdigest() != manifest['cover_index_sha256']:
            raise ValueError('Bootstrap publication index hash mismatch')
        index = validate_cover_index(json.loads(raw_index.decode('utf-8')))
        validate_snapshot(snapshot, manifest, index)
        # Validate the whole active bundle before any ledger mutation.
        from slg_db import get_game_metadata_bulk
        source = sqlite3.connect(snapshot)
        source.row_factory = sqlite3.Row
        try:
            games = source.execute('SELECT * FROM games ORDER BY slug').fetchall()
            metadata = get_game_metadata_bulk(source, [game['id'] for game in games])
            restored = []
            for game in games:
                external = slg_dlsite.product_id(game['url'])
                tags = [row[0] for row in source.execute('SELECT t.name FROM tags t JOIN game_tags gt ON gt.tag_id=t.id WHERE gt.game_id=? ORDER BY t.name', (game['id'],))]
                restored.append({'external_id': external, 'slug': game['slug'], 'url': game['url'], 'title': game['title'], 'developer': game['developer'], 'tags': tags, 'tag_labels': {}, **metadata[game['id']]})
        finally:
            source.close()
        verified = {}
        for slug, item in index.items():
            raw = (output / 'covers' / item['filename']).read_bytes()
            if len(raw) != item['bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('Active publication artwork mismatch')
            validate_image(raw)
            verified[slug] = raw
        for item in restored:
            self.conn.execute('INSERT INTO works(external_id,url,discovered_at,item) VALUES(?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET item=COALESCE(works.item,excluded.item)', (item['external_id'], item['url'], self.clock(), json.dumps(item, ensure_ascii=False)))
        existing = self.covers()
        adopted = skipped = 0
        for slug, item in index.items():
            external = slug.removeprefix('dlsite-').upper()
            if slug in existing or not self.conn.execute('SELECT 1 FROM works WHERE external_id=? AND item IS NOT NULL', (external,)).fetchone():
                continue
            try:
                path = output / 'covers' / item['filename']
                if path.stat().st_size != item['bytes']:
                    raise ValueError('Bootstrap image size mismatch')
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != item['sha256']:
                    raise ValueError('Bootstrap image hash mismatch')
                validate_image(raw)
                atomic_bytes(self.directory / 'covers' / item['filename'], raw)
            except (OSError, ValueError):
                skipped += 1
                continue
            self.conn.execute('UPDATE works SET cover=?,cover_checked=? WHERE external_id=?',
                              (json.dumps(item), self.clock(), external))
            adopted += 1
        self.conn.commit()
        self._set('active_hash', manifest['db_sha256'])
        self.conn.commit()
        return {'adopted': adopted, 'skipped': skipped}

    def stats(self):
        result = dict(self.conn.execute('SELECT count(*) candidates, sum(item IS NOT NULL) eligible, sum(cover IS NOT NULL) covers, sum(last_error IS NOT NULL) failures FROM works').fetchone())
        groups = dict.fromkeys(('url_pending_download', 'no_url_pending_detail', 'unsupported_parse', 'invalid_backoff', 'network_failure', 'pending_publish'), 0)
        for row in self.conn.execute('SELECT * FROM works WHERE item IS NOT NULL'):
            if row['cover']:
                if row['cover_checked'] > (self._get('last_publish', 0) or 0):
                    groups['pending_publish'] += 1
                continue
            if row['last_error'] == 'missing':
                key = 'invalid_backoff'
            elif row['last_error'] == 'retryable_failure':
                key = 'network_failure'
            elif row['image_url']:
                key = 'url_pending_download'
            elif row['checked_at']:
                key = 'unsupported_parse'
            else:
                key = 'no_url_pending_detail'
            groups[key] += 1
        result['artwork_backlog'] = groups
        result['request_lanes'] = dict(self.conn.execute('SELECT lane,used FROM request_lanes WHERE day=?', (self._day(),)))
        return result

    def _failure(self, external, lane, error):
        count = self.conn.execute('SELECT ' + lane + '_failures FROM works WHERE external_id=?', (external,)).fetchone()[0] + 1
        is_missing = isinstance(error, urllib.error.HTTPError) and error.code == 404
        delay = DETAIL_TTL if is_missing else (3600, 21600, 86400, DETAIL_TTL)[min(count - 1, 3)]
        self.conn.execute('UPDATE works SET ' + lane + '_failures=?, next_' + lane + '_due=?,attempted_at=?,failures=failures+1,last_error=? WHERE external_id=?', (count, self.clock() + delay, self.clock(), 'missing' if is_missing else 'retryable_failure', external))
        self.conn.commit()
        self._service_failure(error, delay)

    def _service_failure(self, error, delay):
        # Retry history of one item is distinct from consecutive source outages.
        outage = (isinstance(error, urllib.error.HTTPError) and error.code >= 500
                  or isinstance(error, (urllib.error.URLError, OSError))
                  and not isinstance(error, urllib.error.HTTPError))
        count = self._get('consecutive_service_failures', 0) + 1 if outage else 0
        self._set('consecutive_service_failures', count)
        self.conn.commit()
        if count >= 3:
            self._set('pause_until', self.clock() + delay)
            self.conn.commit()
            raise slg_dlsite.AccessStopped('Consecutive service failures; source paused')

    def _service_success(self):
        self._set('consecutive_service_failures', 0)
        self.conn.commit()

    def run(self, client, discovery=True, images=True, refresh=True, protocol_only=False):
        self._client = client
        self._active_lane = 'check'
        self._active_group = 'check'
        access_control={key:self._get(key,None) for key in ('state','pause_until','reason','manual_pause','permanent_pause','pause')}
        started = self.clock()
        status, reason = 'complete', None
        metrics = dict(new_verified=0, facts_updated=0, covers_added=0, failed=0)
        self.conn.execute("UPDATE run_metrics SET status='interrupted',finished_at=? WHERE status='running'", (started,))
        run_id = self.conn.execute('INSERT INTO run_metrics(started_at,status,metrics) VALUES(?,?,?)', (started, 'running', json.dumps(metrics))).lastrowid
        self.conn.commit()
        def save_metrics(final=False):
            self.conn.execute('UPDATE run_metrics SET metrics=?,status=?,finished_at=? WHERE run_id=?', (json.dumps(metrics), status if final else 'running', self.clock() if final else None, run_id))
            self.conn.commit()
        previous_new=self.daily_new_count()
        retry_failures = 0
        self.event('started', 'running')
        prior = self.conn.execute("SELECT kind FROM events WHERE kind!='started' ORDER BY rowid DESC LIMIT 1").fetchone()
        if prior and prior[0] == 'heartbeat':
            self.event('recovered', 'running')
        native_accounting = hasattr(client, 'on_request')
        if native_accounting:
            client.on_request = self.reserve_request
        def call(method, *args):
            if native_accounting:
                return method(*args)
            # Offline clients still exercise durable accounting.
            self.reserve_request()
            return method(*args)
        try:
            decision = pause_decision(access_control,self.clock())
            if not decision['allowed'] and not decision['probe_due']:
                raise slg_dlsite.AccessStopped('Source pause still active',kind=decision['reason'])
            if decision['probe_due']:
                probes=self._get('recovery_probes',{})
                count=probes.get('count',0) if probes.get('day')==self._day() else 0
                if count>=4:
                    midnight=(datetime.datetime.fromisoformat(self._day())+datetime.timedelta(days=1)).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
                    self._set('pause_until',midnight)
                    self.conn.commit()
                    raise slg_dlsite.AccessStopped('Daily recovery probe ceiling reached',kind='probe_limit')
                self._set('recovery_probes',{'day':self._day(),'count':count+1})
                self.conn.commit()
                client.force_robots=True
            if protocol_only:
                client.force_robots=True
            try:
                call(client.load_robots)
            except (urllib.error.URLError, OSError) as error:
                raise slg_dlsite.AccessStopped('Robots unavailable',kind='network') from error
            # Only an actual protocol check permits clearing an expired temporary pause.
            self._set('pause_until',0)
            self._set('reason',None)
            self._set('last_protocol_success',self.clock())
            self._set('state','running')
            self.conn.commit()
            if protocol_only:
                raise ProtocolChecked()
            # Due existing facts/covers receive the first third of actual requests.
            old = self.conn.execute('SELECT * FROM works WHERE item IS NOT NULL ORDER BY min(next_fact_due,next_cover_due),external_id').fetchall()
            fresh = self.conn.execute('SELECT * FROM works WHERE item IS NULL AND next_fact_due<=? ORDER BY next_fact_due,discovered_at,external_id', (self.clock(),)).fetchall()
            def process(row, facts=True, artwork=True):
                nonlocal retry_failures
                external = row['external_id']
                current = row
                if facts and row['next_fact_due'] <= self.clock() and (refresh or row['checked_at'] == 0 or not row['image_url']):
                    try:
                        text = call(client.fetch, row['url'] + '?locale=zh_CN')
                        evidence = slg_dlsite.FactParser()
                        evidence.feed(text)
                        if not evidence.outline:
                            raise slg_dlsite.AccessStopped('Product outline absent; possible access gate')
                        self._service_success()
                        item = slg_dlsite.parse_product(text, row['url'])
                        previous_item = json.loads(row['item']) if row['item'] else None
                        image_url = parse_main_artwork(text, external) if item else None
                        # Missing evidence never deletes verified facts or artwork.
                        self.conn.execute('UPDATE works SET checked_at=?,attempted_at=?,next_fact_due=?,fact_failures=0,item=COALESCE(?,item),next_cover_due=CASE WHEN ? IS NOT NULL AND (image_url IS NULL OR image_url<>?) THEN 0 ELSE next_cover_due END,image_url=COALESCE(?,image_url) WHERE external_id=?', (self.clock(), self.clock(), self.clock()+DETAIL_TTL, json.dumps(item,ensure_ascii=False) if item else None, image_url,image_url,image_url,external))
                        if image_url and image_url == row['image_url'] and row['cover']:
                            self.conn.execute('UPDATE works SET next_cover_due=MAX(next_cover_due,cover_checked+?) WHERE external_id=?', (COVER_TTL, external))
                        self.conn.commit()
                        if item is not None:
                            metrics['new_verified' if previous_item is None else 'facts_updated'] += int(previous_item != item)
                        save_metrics()
                        current = self.conn.execute('SELECT * FROM works WHERE external_id=?', (external,)).fetchone()
                    except slg_dlsite.RequestBudgetReached:
                        raise
                    except slg_dlsite.PathDisallowed:
                        retry_failures += 1
                        self._failure(external, 'fact', ValueError('path_disallowed'))
                        self.conn.execute("UPDATE works SET last_error='path_disallowed',next_fact_due=? WHERE external_id=?", (self.clock()+DETAIL_TTL,external))
                        self.conn.commit()
                        return
                    except slg_dlsite.AccessStopped:
                        metrics['failed'] += 1
                        save_metrics()
                        raise
                    except Exception as error:
                        retry_failures += 1
                        metrics['failed'] += 1
                        self._failure(external, 'fact', error)
                        save_metrics()
                        return
                if artwork and images and current['item'] and current['image_url'] and current['next_cover_due'] <= self.clock():
                    try:
                        raw = normalise_image(call(client.fetch_image, current['image_url']))
                        self._service_success()
                        digest = hashlib.sha256(raw).hexdigest()
                        filename = f'dlsite-{external}-{digest}.jpg'
                        atomic_bytes(self.directory / 'covers' / filename, raw)
                        cover = {'filename':filename,'sha256':digest,'bytes':len(raw)}
                        self.conn.execute('UPDATE works SET cover=?,cover_checked=?,next_cover_due=?,cover_failures=0,last_error=NULL WHERE external_id=?', (json.dumps(cover),self.clock(),self.clock()+COVER_TTL,external))
                        self.conn.commit()
                        old_cover = json.loads(current['cover']) if current['cover'] else None
                        metrics['covers_added'] += int(old_cover != cover)
                        save_metrics()
                    except slg_dlsite.RequestBudgetReached:
                        raise
                    except slg_dlsite.PathDisallowed:
                        retry_failures+=1
                        metrics['failed']+=1
                        self.conn.execute("UPDATE works SET last_error='path_disallowed',next_cover_due=? WHERE external_id=?",(self.clock()+COVER_TTL,external))
                        self.conn.commit()
                        save_metrics()
                    except slg_dlsite.AccessStopped:
                        metrics['failed'] += 1
                        save_metrics()
                        raise
                    except Exception as error:
                        retry_failures += 1
                        metrics['failed'] += 1
                        self._failure(external, 'cover', error)
                        save_metrics()
            # Quotas are persisted once per Beijing day, after actual checks.
            # Existing maintenance consumption is retained; restart never restores it.
            target = self._get('history_target', {})
            used = self.status()['request_used']
            # Repair a zero target created by the old single-run/daily-limit
            # confusion; only scheduling metadata changes, never budget receipts.
            if target.get('day') != self._day() or (target.get('requests') == 0 and used < self.max_requests):
                prior = self.conn.execute("SELECT used FROM request_groups WHERE day=? AND group_name='history'", (self._day(),)).fetchone()
                self._set('history_target', {'day': self._day(), 'requests': (prior[0] if prior else 0)+math.ceil(max(0,self.max_requests-used)*.2), 'daily_limit': self.max_requests})
                self.conn.commit()
            if discovery:
                self.discover([], [slg_dlsite.LATEST_URL, *getattr(client, 'sitemaps', [])])

            def queued(group):
                now = self.clock()
                candidates = {}
                # The exact latest entry gets a daily opportunity regardless of
                # older pagination cursors. Other observed pages rotate oldest first.
                if discovery and self._get('next_discovery_due', 0) <= now:
                    predicate = 'pages.url=?' if group == 'update' else 'pages.url<>?'
                    page = self.conn.execute('SELECT pages.url FROM pages JOIN page_schedule USING(url) WHERE next_due<=? AND '+predicate+' ORDER BY checked_at,pages.url LIMIT 1', (now,slg_dlsite.LATEST_URL)).fetchone()
                    if page:
                        if group == 'update':
                            return 'discovery', page
                        candidates['discovery'] = page
                origin = "COALESCE((SELECT priority FROM discovery_origins WHERE kind='work' AND identity=external_id),'history')"
                fresh = self.conn.execute('SELECT * FROM works WHERE item IS NULL AND next_fact_due<=? AND '+origin+'=? ORDER BY attempted_at,discovered_at,external_id LIMIT 1', (now,group)).fetchone()
                if fresh:
                    candidates['new'] = fresh
                    # Reach a unique-work goal before optional refresh maintenance.
                    # History allocation remains separately protected below.
                    if group=='update' and previous_new+metrics['new_verified']<50:
                        return 'new',fresh
                if group == 'update':
                    missing = self.conn.execute('SELECT * FROM works WHERE item IS NOT NULL AND cover IS NULL AND (('+('image_url IS NOT NULL' if images else '0')+' AND next_cover_due<=?) OR (image_url IS NULL AND next_fact_due<=?)) ORDER BY attempted_at,external_id LIMIT 1', (now,now)).fetchone()
                    if missing:
                        candidates['missing'] = missing
                    old = self.conn.execute('SELECT * FROM works WHERE item IS NOT NULL AND cover IS NOT NULL AND (next_fact_due<=? '+('' if refresh else 'AND (checked_at=0 OR image_url IS NULL) ')+'OR '+('image_url IS NOT NULL' if images else '0')+' AND next_cover_due<=?) ORDER BY attempted_at,external_id LIMIT 1', (now,now)).fetchone()
                    if old:
                        candidates['old'] = old
                lanes = ('new','missing','old') if group == 'update' else ('new','discovery')
                cursor = self._get(group+'_cursor',0) % len(lanes)
                for offset in range(len(lanes)):
                    lane = lanes[(cursor+offset) % len(lanes)]
                    if lane in candidates:
                        return lane,candidates[lane]
                return None

            force_history = False
            while True:
                history, update = queued('history'), queued('update')
                self._protect_history = history is not None
                if not history and not update:
                    break
                remaining = self.max_requests-self.status()['request_used']
                group = 'history' if history and (force_history or not update or remaining<=self.history_remaining()) else 'update'
                lane, row = history if group == 'history' else update
                self._active_group, self._active_lane = group, lane
                lanes = ('new','missing','old') if group == 'update' else ('new','discovery')
                if lane in lanes:
                    self._set(group+'_cursor',(lanes.index(lane)+1) % len(lanes))
                    self.conn.commit()
                force_history = False
                if lane != 'discovery':
                    try:
                        process(row, facts=lane != 'missing' or not row['image_url'], artwork=lane != 'new')
                    except HistoryTurn:
                        force_history = True
                    continue
                try:
                    client.check_url(row['url'])
                    products, linked = slg_dlsite.discover_links(call(client.fetch,row['url']),row['url'])
                    if not products and not linked:
                        raise slg_dlsite.AccessStopped('Discovery page evidence missing')
                    self.discover(products,linked,priority=group)
                    self._service_success()
                    self.conn.execute('UPDATE page_schedule SET next_due=?,failures=0 WHERE url=?', (self.clock()+86400,row['url']))
                except HistoryTurn:
                    force_history = True
                    continue
                except slg_dlsite.PathDisallowed:
                    retry_failures += 1
                    # No request was made. Keep the cursor and recheck next day;
                    # a forbidden pagination URL is not a forbidden source.
                    self.conn.execute('UPDATE page_schedule SET next_due=? WHERE url=?',(self.clock()+86400,row['url']))
                    self.event('path_disallowed','running')
                except (slg_dlsite.AccessStopped,slg_dlsite.RequestBudgetReached):
                    raise
                except Exception as error:
                    retry_failures += 1
                    metrics['failed'] += 1
                    count = self.conn.execute('SELECT failures FROM page_schedule WHERE url=?',(row['url'],)).fetchone()[0]+1
                    delay = (3600,21600,86400,DETAIL_TTL)[min(count-1,3)]
                    self.conn.execute('UPDATE page_schedule SET next_due=?,failures=? WHERE url=?',(self.clock()+delay,count,row['url']))
                    self.conn.commit()
                    self._service_failure(error,delay)
                self.conn.execute('UPDATE pages SET checked_at=? WHERE url=?',(self.clock(),row['url']))
                self.conn.commit()
                save_metrics()
            if retry_failures:
                status, reason = 'partial', 'retry_pending'
            else:
                self._set('last_success', self.clock())
        except ProtocolChecked:
            status, reason = 'partial', 'protocol_checked'
        except slg_dlsite.RequestBudgetReached:
            status, reason = 'partial', 'budget'
        except slg_dlsite.AccessStopped as error:
            status, reason = 'stopped', 'source_paused'
            kind=getattr(error,'kind',None)
            if not kind:
                message=str(error).lower()
                kind=next((code for needle,code in [('robots','robots_parse'),('consecutive','network'),('pause still','pause_active'),('outline','verification_gate'),('refusal','access_refused')] if needle in message),'access_refused')
            reason=kind
            if kind not in ('pause_active','manual_pause','probe_limit'):
                failures=int(self._get('access_failures',0))+1
                delay=retry_delay(kind,failures,getattr(error,'retry_after',None),self.clock())
                self._set('access_failures',failures)
                self._set('pause_until',self.clock()+delay)
                diagnostic=getattr(error,'diagnostic',None) or getattr(client,'last_diagnostic',None)
                if diagnostic:
                    diagnostic=dict(diagnostic,next_check_at=self.clock()+delay)
                    self._set('access_diagnostic',diagnostic)
        except Exception:
            status, reason = 'failed', 'collection_failed'
        if status=='complete' or (status=='partial' and reason=='budget'):
            self._set('access_failures',0)
        self._set('reason', reason)
        self._set('state',status)
        self.conn.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (started,self.clock(),client.requests,status,reason))
        self.event('finished', status)
        save_metrics(final=True)
        return {'run_id':run_id,'metrics':dict(metrics),'status':status,'reason':reason,'requests':client.requests,**self.stats(),**self.status()}


def publish(ledger, output):
    """Write immutable resources first; active manifest is the final atomic operation."""
    from tools.build_dlsite_pilot import build
    from slg_dlsite_online import validate_manifest, validate_snapshot, validate_image
    output = Path(output)
    ledger.adopt_publication(output)
    catalogue_dir = output / 'catalogues' / 'dlsite'
    items = ledger.items()
    if not items:
        raise ValueError('Refusing empty DLsite publication')
    index = ledger.covers()
    with tempfile.TemporaryDirectory(prefix='dlsite-publish-') as temp:
        facts = Path(temp) / 'facts.json'
        facts.write_text(json.dumps({'catalog_id': 'dlsite', 'collected_at': slg_dlsite.utc_now(), 'items': items}, ensure_ascii=False), encoding='utf-8')
        bundle = Path(temp) / 'bundle'
        built = build(facts, bundle, covers=index)
        raw_db = (bundle / 'slgking.db').read_bytes()
        raw_index = json.dumps(index, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        index_hash = hashlib.sha256(raw_index).hexdigest()
        manifest = {'catalog_id': 'dlsite', 'count': built['count'], 'revision': slg_dlsite.utc_now(),
                    'source_status_path': '/catalogues/dlsite/status.json',
                    'db_sha256': built['db_sha256'], 'db_path': '/catalogues/dlsite/snapshots/' + built['db_sha256'] + '.db',
                    'cover_index_sha256': index_hash, 'cover_index_path': '/catalogues/dlsite/cover-index/' + index_hash + '.json'}
        validate_manifest(manifest)
        validate_snapshot(bundle / 'slgking.db', manifest, index)
        verified_images=[]
        for item in index.values():
            raw=(ledger.directory / 'covers' / item['filename']).read_bytes()
            if len(raw)!=item['bytes'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:
                raise ValueError('Publication image identity mismatch')
            validate_image(raw)
            verified_images.append((item['filename'],raw))
        for filename,raw in verified_images:
            atomic_bytes(output / 'covers' / filename,raw)
        atomic_bytes(catalogue_dir / 'snapshots' / (built['db_sha256'] + '.db'), raw_db)
        atomic_bytes(catalogue_dir / 'cover-index' / (index_hash + '.json'), raw_index)
        active_path = catalogue_dir / 'manifest.json'
        if active_path.is_file():
            previous = json.loads(active_path.read_text(encoding='utf-8'))
            if previous['db_sha256'] == manifest['db_sha256'] and previous['cover_index_sha256'] == index_hash:
                manifest = previous
        atomic_bytes(active_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        ledger._set('last_publish', ledger.clock())
        ledger._set('active_hash', manifest['db_sha256'])
        ledger.event('published', ledger._get('state', 'offline'), 'complete')
    return manifest
