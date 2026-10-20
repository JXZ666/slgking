"""Fixed-path, persistent administrator acquisition worker.

Native parsers, publisher and ledgers are reused; scheduled request accounting is
never charged or reset. A per-source child process isolates native module names.
"""
from contextlib import contextmanager, closing
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from urllib.robotparser import RobotFileParser

from slg_collection_jobs import CollectionJobs, CollectionConflict, LIMITS, canonical_target, describe_reason

ROOT = Path('/opt/slgking')


class SourcePaused(RuntimeError):
    pass


class StopWork(RuntimeError):
    pass


class CollectorBusy(OSError):
    pass


@contextmanager
def file_lock(path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise CollectorBusy('collector_busy') from None
        else:
            import fcntl
            try:fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:raise CollectorBusy('collector_busy') from None
        yield


def source_status(source, root=ROOT):
    """Read protection state without migrating or mutating source databases."""
    try:
        gate = json.loads((root / 'cloud/adminops_collection_gate.json').read_text(encoding='utf-8'))
        if not isinstance(gate,dict):raise ValueError('Invalid maintenance gate')
        if gate.get('blocked') is not False:
            return dict(paused=True,global_protection=True,reason='maintenance_guard',
                        reason_text=str(gate.get('reason') or describe_reason('maintenance_guard')))
    except (OSError, ValueError, TypeError):
        return dict(paused=True,global_protection=True,reason='maintenance_guard_not_verified')
    path = root / ('f95-data/ledger.sqlite3' if source == 'f95zone' else 'dlsite-data/ledger.db')
    if source == 'dikgames':
        path = root / 'cloud/collection-source-protection.json'
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            state = value.get(source, {})
            return dict(paused=bool(state.get('paused')), reason=state.get('reason'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return dict(paused=True, reason='source_state_unavailable')
    if not path.is_file():
        return dict(paused=True, reason='source_ledger_unavailable')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            table = 'meta' if source == 'f95zone' else 'control'
            state = dict(db.execute('SELECT key,value FROM ' + table))
            if source == 'f95zone':
                paused = bool(state.get('paused')) or float(state.get('pause_until') or 0) > time.time()
                reason = state.get('paused') or 'pause_active'
            else:
                state = {k: json.loads(v) for k, v in state.items()}
                import importlib.util
                spec = importlib.util.spec_from_file_location('_slg_dlsite_recovery', root / 'dlsite-worker/slg_dlsite_recovery.py')
                policy = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(policy)
                decision = policy.pause_decision(state, time.time())
                paused = not decision['allowed']
                reason = decision['reason'] or state.get('reason') or 'pause_active'
                if decision['probe_due']:
                    probes = state.get('recovery_probes', {})
                    today = datetime.fromtimestamp(time.time(), timezone(timedelta(hours=8))).date().isoformat()
                    if probes.get('day') == today and probes.get('count', 0) >= 4:
                        next_day = (datetime.fromisoformat(today)+timedelta(days=1)).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
                        return dict(paused=True, reason='probe_limit', next_check_at=next_day)
                    return dict(paused=False, probe_due=True, reason='protocol_check_required', next_check_at=decision['next_check_at'])
            return dict(paused=paused, reason=reason if paused else None, next_check_at=decision['next_check_at'] if source == 'dlsite' else None)
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return dict(paused=True, reason='source_state_unavailable')


def read_source_status(source, root=ROOT):
    state=source_status(source,root)
    reason=state.get('reason')
    return dict(state,source_id=source,reason_text=state.get('reason_text') or describe_reason(reason))


_WORKER_CACHE = {'until': 0, 'active': False}


def collection_worker_ready():
    """Bounded read-only status check, fail closed; never invokes service control."""
    now = time.monotonic()
    if now >= _WORKER_CACHE['until']:
        try:
            result = subprocess.run(['systemctl', 'is-active', 'slgking-admin-collection.service'],
                                    capture_output=True, text=True, timeout=2, check=False)
            active = result.returncode == 0 and result.stdout.strip() == 'active'
        except (OSError, subprocess.TimeoutExpired):
            active = False
        _WORKER_CACHE.update(until=now+5, active=active)
    return _WORKER_CACHE['active']


def collection_source_availability(source, root=ROOT, *, worker_ready=None, now=None):
    """No migrations, writes, worker control or upstream requests on admin GET."""
    now = time.time() if now is None else now
    root = Path(root)
    value = read_source_status(source, root)
    reason = value.get('reason')
    if not value.get('global_protection') and not (collection_worker_ready() if worker_ready is None else worker_ready):
        value.update(paused=True); reason = 'worker_unavailable'
    if not value.get('paused'):
        path = root / 'cloud/collection_jobs.sqlite3'
        if path.is_file():
            try:
                day = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()
                with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
                    row = db.execute('SELECT requests,new FROM collection_budget WHERE day=? AND source=?', (day,source)).fetchone()
                if row and row[0] >= LIMITS[source][0]:
                    value.update(paused=True); reason='manual_daily_request_budget'
                elif row and row[1] >= LIMITS[source][1]:
                    value.update(paused=True); reason='manual_daily_new_budget'
            except sqlite3.Error:
                value.update(paused=True); reason='source_state_unavailable'
    value.update(reason=reason, reason_code=reason, reason_text=describe_reason(reason),
                 can_submit=not value.get('paused', False),
                 state='blocked' if value.get('global_protection') else 'paused' if value.get('paused') else 'check_due' if value.get('probe_due') else 'ready',
                 next_check_at=value.get('next_check_at'))
    return value


def scheduled_due(source, now=None):
    """Do not start/continue network work in a source's scheduled protection window.

    Locks exclude an already active timer. The fixed overnight windows reserve
    enough lead time to finish a single bounded HTTP request before the timer.
    """
    current = datetime.fromtimestamp(time.time() if now is None else now, timezone(timedelta(hours=8)))
    minute = current.hour * 60 + current.minute
    start = {'f95zone': 60, 'dlsite': 120, 'dikgames': 263}[source]
    duration = {'f95zone': 21, 'dlsite': 61, 'dikgames': 90}[source]
    return (minute - (start - 2)) % 1440 <= duration + 2


class Context:
    def __init__(self, store, job, root=ROOT, monotonic=time.monotonic):
        self.store, self.job, self.root = store, job, Path(root)
        self.monotonic = monotonic
        self.deadline = monotonic() + LIMITS[job['source_id']][2]

    def check(self):
        live = self.store.get(self.job['id'])
        if live['cancel_requested']:
            raise StopWork('cancelled')
        if self.monotonic() >= self.deadline:
            raise StopWork('deadline')
        if scheduled_due(self.job['source_id'], self.store.clock()):
            raise StopWork('scheduled_priority')

    def request(self):
        self.check()
        try:
            self.store.reserve_request(self.job['id'])
        except CollectionConflict as exc:
            raise StopWork(str(exc)) from None

    def before_commit(self, is_new, target=None, proof=None, result=None):
        self.check()
        if target is not None:
            try:
                self.store.begin_intent(self.job['id'],target,proof,result,reserve_new=is_new)
            except CollectionConflict as exc:
                raise StopWork(str(exc)) from None
            return
        if is_new:
            try:
                self.store.reserve_new(self.job['id'])
            except CollectionConflict as exc:
                raise StopWork(str(exc)) from None
        # Crash between fact commit and receipt must still request publication.
        self.store.update(self.job['id'], publication_pending=True)


def execute_job(store, job, adapter, root=ROOT):
    try:
        return _execute_job(store, job, adapter, root)
    finally:
        close = getattr(adapter, 'close', None)
        if close:
            close()


def _execute_job(store, job, adapter, root=ROOT):
    context = Context(store, job, root)
    status, reason = 'completed', None
    failed_before = job['counters']['failed']
    global_blocked=False
    try:
        protection=source_status(job['source_id'],root)
        global_blocked=bool(protection.get('global_protection'))
        if global_blocked:
            raise SourcePaused(protection.get('reason_text') or protection['reason'])
        if shutil.disk_usage(root).free < 5 * 1024 ** 3:
            raise StopWork('storage_low')
        with adapter.locks():
            recover=getattr(adapter,'recover_intents',None)
            if recover:recover(store,job)
            current = store.get(job['id'])
            # Failed publishing retries never perform discovery or acquisition.
            if current.get('reason') == 'publication_failed' or current.get('publish_only'):
                pass
            elif not (job['publication_pending'] and job.get('resume_publish_only')):
                protected = source_status(job['source_id'], root)
                if protected.get('paused'):
                    raise SourcePaused(protected.get('reason') or 'source_paused')
                done = store.done_targets(job['id'])
                unfinished={name[:-6] for name in done if name.endswith('#facts') and name[:-6] not in done}
                cover_only=store.missing_cover_targets(job['id'])
                unfinished|=cover_only
                adapter.cover_only_targets=cover_only
                adapter.pending_targets=sorted(unfinished)
                reached=job['mode']=='count' and store.get(job['id'])['counters']['new']>=job['target_new']
                candidates=() if reached and not unfinished else adapter.candidates(context)
                for target in candidates:
                    context.check()
                    if target in done and target not in unfinished:
                        continue
                    if job['mode'] == 'count' and store.get(job['id'])['counters']['new'] >= job['target_new'] and target not in unfinished:
                        break
                    try:
                        result = adapter.collect(target, context)
                    except (StopWork, SourcePaused):
                        raise
                    except Exception as exc:
                        adapter.handle_error(exc)
                        result = {'failed': 1, 'reason': 'item_failed'}
                    store.receipt(job['id'], target, result)
            live = store.get(job['id'])
            if live['publication_pending']:
                try:
                    latest=source_status(job['source_id'],root)
                    if latest.get('global_protection'):
                        global_blocked=True
                        raise SourcePaused(latest.get('reason_text') or latest['reason'])
                    adapter.publish()
                    store.update(job['id'], publication_pending=False, resume_publish_only=False)
                except SourcePaused:
                    raise
                except Exception:
                    store.update(job['id'], status='publication_failed', reason='publication_failed')
                    return
            if job['mode'] == 'count' and live['counters']['new'] < job['target_new']:
                status, reason = 'partial', 'candidate_pool_exhausted'
            elif live['counters']['failed'] > failed_before:
                status, reason = 'partial', 'item_failures'
            elif live['counters']['cover_missing']:
                status,reason='partial','covers_pending'
    except (CollectorBusy,BlockingIOError):
        status, reason = 'queued', 'collector_busy'
    except StopWork as exc:
        reason = str(exc)
        status = 'cancelled' if reason == 'cancelled' else ('queued' if reason == 'scheduled_priority' else 'partial')
    except SourcePaused as exc:
        status, reason = 'paused', str(exc)[:100]
        if not global_blocked:
            try:
                adapter.handle_error(exc)
            except SourcePaused:
                pass
    except Exception as exc:
        try:
            adapter.handle_error(exc)
        except StopWork as stop:
            status, reason = 'partial', str(stop)
        except SourcePaused as stop:
            status, reason = 'paused', str(stop)[:100]
        else:
            status, reason = 'failed', 'worker_failed'
    latest=source_status(job['source_id'],root)
    if latest.get('global_protection'):
        global_blocked=True
        status,reason='paused',latest.get('reason_text') or latest['reason']
    if store.get(job['id'])['publication_pending'] and status != 'queued' and not global_blocked:
        try:
            with adapter.locks():
                adapter.publish()
                store.update(job['id'], publication_pending=False)
        except Exception:
            status, reason = 'publication_failed', 'publication_failed'
    # Committed items survive every exit. A subsequent explicit retry publishes
    # before resuming unfinished work; cancelled jobs are not silently restarted.
    store.update(job['id'], status=status, reason=reason, not_before=store.clock()+60 if status=='queued' else 0)


class BaseAdapter:
    def __init__(self, job, root=ROOT):
        self.job, self.root = job, Path(root)

    def handle_error(self, error):
        return None

    def recover_intents(self,store,job):
        tables={'games': {'id','slug','title','origin','version','cover_file'},
                'game_sources': {'source_id','external_id','content_hash','game_id'},
                'works': {'external_id','item','cover'}}
        for target,intent in store.intents(job['id']):
            proof=intent['proof'];table=proof['table'];key=proof['key'];expected=proof['expected']
            if table not in tables or (set(key)|set(expected))-tables[table]:
                raise ValueError('Invalid native recovery proof')
            database=self.root/('dlsite-data/ledger.db' if table=='works' else 'data/slgking/slgking.db')
            if not database.is_file():continue
            with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                row=db.execute('SELECT * FROM '+table+' WHERE '+' AND '.join(name+'=?' for name in key),tuple(key.values())).fetchone()
                if row is None or any(row[name]!=value for name,value in expected.items()):continue
                if table=='game_sources' and not db.execute("SELECT 1 FROM games WHERE id=? AND origin='site'",(row['game_id'],)).fetchone():continue
            picture=proof.get('picture')
            if picture:
                directory=self.root/('dlsite-data/covers' if table=='works' else 'data/slgking/covers')
                path=directory/picture['filename']
                if Path(picture['filename']).name!=picture['filename'] or not path.is_file():continue
                if hashlib.sha256(path.read_bytes()).hexdigest()!=picture['sha256']:continue
            if '#cover' in target:
                store.cover_result(job['id'],target,True,changed=True)
            elif proof['table']=='game_sources':
                with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                    cover=db.execute('SELECT cover_file FROM games WHERE id=?',(row['game_id'],)).fetchone()[0]
                present=self.existing_cover(cover)
                store.cover_result(job['id'],target,present)
            elif proof['table']=='works':
                cover=json.loads(row['cover']) if row['cover'] else None
                present=bool(cover and self.existing_cover(cover['filename'],cover))
                store.cover_result(job['id'],target,present)
            elif proof['table']=='games':
                cover=row['cover_file']
                present=self.existing_cover(cover)
                store.cover_result(job['id'],target,present)
            store.receipt(job['id'],target,intent['result'])

    def close(self):
        database = getattr(self, 'db', None)
        if database is not None:
            database.close()
        ledger = getattr(self, 'ledger', None)
        if ledger is not None:
            ledger.conn.close()

    def existing_cover(self,filename,metadata=None):
        if not filename or str(filename).startswith('pending:') or Path(filename).name!=filename:return False
        directory=self.root/('dlsite-data/covers' if self.job['source_id']=='dlsite' else 'data/slgking/covers')
        path=directory/filename
        if not path.is_file():return False
        if metadata:
            return path.stat().st_size==metadata['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==metadata['sha256']
        return path.stat().st_size>0

    def publish(self):
        args = [str(self.root / '.venv/bin/python'), str(self.root / 'serve_catalog.py'),
                '--publish', '--data-dir', str(self.root / 'data/slgking'),
                '--publish-dir', str(self.root / 'publish')]
        with file_lock(self.root / 'data/slgking/catalogue-write.lock'):
            result = subprocess.run(args, env=dict(os.environ, LOCALAPPDATA=str(self.root / 'data')),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=120, check=False)
            if result.returncode:
                raise RuntimeError('publication_failed')


class DikgamesAdapter(BaseAdapter):
    def _request_outcome(self,failed):
        path=self.root/'cloud/collection-source-protection.json'
        try:value=json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:value={}
        state=value.get('dikgames',{})
        count=int(state.get('consecutive_failures',0))+1 if failed else 0
        state.update(consecutive_failures=count);value['dikgames']=state
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value),encoding='utf-8');os.replace(temporary,path)
        if count>=3:raise SourcePaused('consecutive_failures')

    def handle_error(self, error):
        if isinstance(error, SourcePaused):
            path = self.root / 'cloud/collection-source-protection.json'
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                value = {}
            value['dikgames'] = dict(value.get('dikgames',{}),paused=True, reason=str(error), at=time.time())
            temporary = path.with_suffix('.tmp'); temporary.write_text(json.dumps(value), encoding='utf-8'); os.replace(temporary, path)
            raise error
    @contextmanager
    def locks(self):
        # Source lock is additional; catalogue writes retain the production lock.
        with file_lock(self.root / 'data/slgking/dikgames-manual.lock'):
            yield

    def _prepare(self, context):
        import slg_scrape
        self.scrape = slg_scrape
        # Cron owns this same lock for its entire scheduled pipeline.
        with file_lock(self.root / 'data/slgking/catalogue-write.lock'):
            pass
        self.db = slg_scrape.slg_db.connect(str(self.root / 'data/slgking/slgking.db'))
        adapter = self
        class Redirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                adapter._validate_url(newurl)
                raise SourcePaused('redirect_policy')
        self.opener = build_opener(Redirect())
        self.last_request = 0
        self.interval = max(1, slg_scrape.DELAY)
        self.policy = None
        raw = self._request('https://dikgames.com/robots.txt', context)
        self.policy = RobotFileParser(); self.policy.parse(raw.decode('utf-8', 'replace').splitlines())
        if not raw.strip():
            raise SourcePaused('robots_unavailable')
        self.interval = max(self.interval, self.policy.crawl_delay(slg_scrape.UA) or 0)

    def _validate_url(self, url):
        value = urlsplit(url)
        if (value.scheme != 'https' or value.hostname != 'dikgames.com' or value.port
                or value.username or value.password or value.fragment or value.query or '..' in value.path):
            raise ValueError('unsafe_source_url')

    def _request(self, url, context):
        self._validate_url(url)
        if self.policy is not None and not self.policy.can_fetch(self.scrape.UA, url):
            raise SourcePaused('robots_disallowed')
        delay = max(0, self.interval - (time.time() - self.last_request))
        if context.monotonic() + delay + 30 >= context.deadline:
            raise StopWork('deadline')
        while delay > 0:
            context.check(); time.sleep(min(delay, .2)); delay = max(0, self.interval - (time.time() - self.last_request))
        context.request()
        try:
            self.last_request = time.time()
            with self.opener.open(Request(url, headers={'User-Agent': self.scrape.UA, 'Accept-Encoding': 'identity'}), timeout=30) as response:
                if response.geturl() != url:
                    raise SourcePaused('redirect_policy')
                raw = response.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    raise ValueError('response_limit')
                mime = response.headers.get('Content-Type', '').lower()
                if 'html' in mime and any(word in raw.lower() for word in (b'cf-chl-', b'captcha', b'verify you are human')):
                    raise SourcePaused('verification')
                self._request_outcome(False)
                return raw
        except HTTPError as exc:
            if exc.code in (403, 429) or url.endswith('/robots.txt'):
                raise SourcePaused('http_' + str(exc.code)) from None
            self._request_outcome(exc.code>=500)
            raise
        except OSError:
            self._request_outcome(True)
            raise

    def candidates(self, context):
        self._prepare(context)
        if self.job['mode'] == 'specified':
            yield from self.job['targets']; return
        yield from getattr(self,'pending_targets',())
        adapter = self
        class Fetch:
            def get(self, url, **kwargs):
                return adapter._request(url, context).decode('utf-8', 'replace')
        index = self.scrape.fetch_sitemap(Fetch(), log=lambda *v: None)
        self.index = index
        for slug in index:
            if not self.db.execute('SELECT 1 FROM games WHERE slug=?', (slug,)).fetchone():
                try:
                    target = canonical_target('dikgames', slug)
                except ValueError:
                    continue
                yield target

    def collect(self, target, context):
        slug = urlsplit(target).path.strip('/')
        cover_only=target in getattr(self,'cover_only_targets',())
        prior=self.db.execute('SELECT * FROM games WHERE slug=?',(slug,)).fetchone()
        if cover_only and prior and str(prior['cover_file'] or '').startswith('pending:'):
            detail={key:prior[key] for key in ('title','version','developer','overview','rating','site_views','site_likes','site_comments','last_updated')}
            detail.update(cover_url=prior['cover_file'][8:],tags=[])
        else:
            detail = self.scrape.parse_detail_page(self._request(target, context).decode('utf-8', 'replace'))
        if not detail.get('title'):
            raise ValueError('invalid_detail')
        picture = None
        cover = detail.get('cover_url')
        if cover:
            try:
                raw = self._request(cover, context)
                import io
                from PIL import Image
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 16777216 or max(image.size) > 4096:
                        raise ValueError('image_dimensions')
                    image.verify()
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                picture = ('manual-' + hashlib.sha256(raw).hexdigest() + self.scrape._ext_from_url(cover), raw)
            except (SourcePaused, StopWork):
                raise
            except Exception:
                pass
        with file_lock(self.root / 'data/slgking/catalogue-write.lock'):
            old = self.db.execute('SELECT * FROM games WHERE slug=?', (slug,)).fetchone()
            new = old is None
            equivalent = old is not None and all(detail.get(field) is None or detail.get(field) == old[field]
                for field in ('title', 'version', 'overview', 'rating', 'site_views', 'site_likes', 'site_comments'))
            if equivalent and (not picture or picture[0] == old['cover_file']) and not self.scrape._needs_cover(self.db, old['id']):
                return dict(duplicate=1, cover_ok=1)
            result=dict(new=int(new),updated=int(not new and not cover_only),cover_ok=int(bool(picture)),cover_missing=int(not picture),cover_changed=bool(picture))
            proof=dict(table='games',key={'slug':slug},expected={'title':detail['title'],'origin':'site'},preexisting=dict(old) if old else None)
            if picture:
                proof['expected']['cover_file']=picture[0]
                proof['picture']=dict(filename=picture[0],sha256=hashlib.sha256(picture[1]).hexdigest())
            checkpoint=target+'#cover' if cover_only else target
            context.before_commit(new,checkpoint,proof,result)
            self.scrape.slg_db.upsert_game(self.db, slug=slug, url=target, title=detail['title'],
                version=detail.get('version'), developer=detail.get('developer') if new else None,
                tags=detail.get('tags') or [], last_updated=detail.get('last_updated'),
                lastmod=getattr(self, 'index', {}).get(slug, {}).get('lastmod'))
            game_id = self.db.execute('SELECT id FROM games WHERE slug=?', (slug,)).fetchone()[0]
            if picture:
                directory = Path(self.scrape.slg_db.covers_dir()); directory.mkdir(parents=True, exist_ok=True)
                temporary = directory / (picture[0] + '.tmp'); temporary.write_bytes(picture[1]); os.replace(temporary, directory / picture[0])
                self.scrape.slg_db.set_cover(self.db, game_id, picture[0])
            elif cover:
                self.scrape._note_cover(self.db, game_id, cover)
            self.scrape.slg_db.upsert_detail(self.db, game_id, overview=detail.get('overview'),
                rating=detail.get('rating'), site_views=detail.get('site_views'), site_likes=detail.get('site_likes'), site_comments=detail.get('site_comments'))
            self.db.commit()
            actual=self.db.execute('SELECT cover_file FROM games WHERE id=?',(game_id,)).fetchone()[0]
            present=self.existing_cover(actual)
            context.store.cover_result(self.job['id'],target,present,changed=bool(picture))
            result.update(cover_ok=int(present),cover_missing=int(not present))
            if cover_only:context.store.receipt(self.job['id'],checkpoint,result)
        return result


class F95Adapter(BaseAdapter):
    def handle_error(self, error):
        if not hasattr(self, 'native'):
            return
        if isinstance(error, self.native.Stop):
            raise StopWork(str(error)) from None
        if isinstance(error, self.native.FetchError):
            if not getattr(error, 'failure_counted', False):
                count = int(self.ledger.get('consecutive_5xx', '0')) + 1 if error.reason.startswith(('http_5', 'network_')) else 0
                self.ledger.set('consecutive_5xx', count)
                error.failure_counted = True
                error.pause |= count >= 3
        if isinstance(error, self.native.FetchError) and error.pause:
            self.ledger.set('paused', error.reason)
            self.ledger.set('pause_until', max(float(self.ledger.get('pause_until', '0')), self.ledger.now()+max(3600, error.retry_after)))
            raise SourcePaused(error.reason) from None
    @contextmanager
    def locks(self):
        with file_lock(self.root / 'f95-data/run.lock'), file_lock(self.root / 'f95-data/collector.lock'):
            yield

    def _prepare(self, context):
        import slg_f95_worker as native
        self.native = native
        self.ledger = native.Ledger(self.root / 'f95-data/ledger.sqlite3')
        self.db = native.slg_db.connect(str(self.root / 'data/slgking/slgking.db'))
        ledger = self.ledger
        # Same last_request_at and pacing, separate manual budget receipts.
        def reserve(url, kind, budget, category=None):
            context.request()
            ledger.set('last_request_at', ledger.now())
        ledger.reserve = reserve
        class ManualClient(native.BudgetClient):
            def get(self, url, host, accept='text/html,application/json,text/plain', **kwargs):
                mime, raw = super().get(url, host, accept, **kwargs)
                if accept.startswith('image/'):
                    import io
                    from PIL import Image
                    try:
                        with Image.open(io.BytesIO(raw)) as image:
                            if max(image.size)>4096 or image.width*image.height>16777216:
                                raise ValueError('image_dimensions')
                            image.verify()
                        with Image.open(io.BytesIO(raw)) as image:
                            image.load()
                    except Exception:
                        raise native.FetchError('invalid_cover') from None
                return mime, raw
        self.client = ManualClient(ledger,LIMITS['f95zone'][0],context.deadline,clock=context.monotonic)

    def candidates(self, context):
        self._prepare(context)
        if self.job['mode'] == 'specified':
            yield from self.job['targets']; return
        yield from getattr(self,'pending_targets',())
        seen = set()
        for row in self.ledger.conn.execute("SELECT url FROM candidates WHERE kind='new' AND next_due<=? ORDER BY last_attempt,next_due,identity", (self.ledger.now(),)):
            seen.add(row[0]); yield row[0]
        native = self.native
        url = native.sources.F95_LISTING
        visited = set()
        while url and url not in visited:
            visited.add(url)
            self.client.kind = 'listing'
            self.client.check_robots(native.sources.F95_BASE, urlsplit(url).path, native.sources.F95_HOST)
            page = self.client.html(url, native.sources.F95_HOST)
            for label, target in native.listing_candidates(page, url):
                with self.ledger.conn:
                    identity = urlsplit(target).path.strip('/').casefold()
                    self.ledger.enqueue(identity, target, label, ['Strategy'])
                if target not in seen and not self.db.execute("SELECT 1 FROM game_sources WHERE source_id='f95zone' AND external_id=?", (identity,)).fetchone():
                    seen.add(target); yield target
            url = native.next_page(page, url)

    def collect(self, target, context):
        native, ledger, client = self.native, self.ledger, self.client
        identity = urlsplit(target).path.strip('/').casefold()
        candidate = ledger.conn.execute('SELECT * FROM candidates WHERE identity=?', (identity,)).fetchone()
        cover_only=target in getattr(self,'cover_only_targets',())
        if candidate and candidate['next_due'] > ledger.now() and not cover_only:
            raise StopWork('target_backoff')
        client.kind = 'detail'
        try:
            cached=self.db.execute("SELECT * FROM game_sources WHERE source_id='f95zone' AND external_id=?",(identity,)).fetchone()
            cover_task=ledger.conn.execute('SELECT * FROM cover_tasks WHERE identity=?',(identity,)).fetchone()
            if cover_only and cached and not cover_task and candidate and candidate['next_due']>ledger.now():
                raise StopWork('cover_backoff')
            if cover_only and cached and cover_task:
                if cover_task['next_due']>ledger.now():raise StopWork('cover_backoff')
                record=dict(source_id='f95zone',external_id=identity,url=target,title=cached['source_title'],version=cached['source_version'],developer=cached['developer'],engine=cached['engine'],overview=cached['overview'],tags=json.loads(cached['tags_json'] or '[]'),modified=cached['source_modified'],hash=cached['content_hash'])
                client.cover_urls[target]=cover_task['url']
            else:
                record = native.sources._f95_record(client, target, candidate['label'] if candidate else None,
                                                    json.loads(candidate['tags']) if candidate else [])
            if not record or not record.get('title'):
                raise ValueError('invalid_detail')
            with file_lock(self.root / 'data/slgking/catalogue-write.lock'), self.db:
                old = self.db.execute("SELECT 1 FROM game_sources WHERE source_id='f95zone' AND external_id=?", (record['external_id'],)).fetchone()
                changed = native.needs_ingest(self.db, record)
                before_count = self.db.execute("SELECT count(*) FROM games WHERE origin='site'").fetchone()[0]
                if changed:
                    match_id, match_status, _ = native.sources._candidate_matches(self.db, record.get('title'), record.get('developer')) if old is None else (None, None, None)
                    will_create = old is None and (match_id is None or match_status == 'review_candidate')
                    context.before_commit(will_create,target+'#facts',
                        dict(table='game_sources',key={'source_id':'f95zone','external_id':record['external_id']},
                             expected={'content_hash':record['hash']},preexisting_source=bool(old),anticipated_new=will_create),
                        dict(new=int(will_create),updated=int(not will_create)))
                    ledger.set('pending_publish', '1')
                    native.sources._ingest_record(self.db, record)
                created = self.db.execute("SELECT count(*) FROM games WHERE origin='site'").fetchone()[0] > before_count
            image = client.cover_urls.get(target)
            context.store.receipt(self.job['id'], target + '#facts',
                dict(new=int(created), updated=int(changed and not created), duplicate=int(not changed)))
            cover_before=self.db.execute("SELECT g.cover_file FROM games g JOIN game_sources s ON s.game_id=g.id WHERE s.source_id='f95zone' AND s.external_id=?",(record['external_id'],)).fetchone()
            context.store.cover_result(self.job['id'],target,bool(cover_before and self.existing_cover(cover_before[0])))
            cover_changed = 0
            if image:
                ledger.queue_cover(record['external_id'], image)
                task = ledger.conn.execute('SELECT * FROM cover_tasks WHERE identity=?', (record['external_id'],)).fetchone()
                client.kind = 'cover'
                original_get=client.get
                def cover_get(url,host,accept='text/html,application/json,text/plain',**kwargs):
                    mime,raw=original_get(url,host,accept,**kwargs)
                    if accept.startswith('image/'):
                        valid=native.sources.slg_source_covers._valid_image(raw,mime)
                        if valid:
                            filename='cover-'+hashlib.sha256(raw).hexdigest()[:24]+'.'+valid[0]
                            game= self.db.execute("SELECT g.id,g.cover_file FROM games g JOIN game_sources s ON s.game_id=g.id WHERE s.source_id='f95zone' AND s.external_id=?",(record['external_id'],)).fetchone()
                            if game:
                                context.before_commit(False,target+'#cover',
                                    dict(table='games',key={'id':game['id']},expected={'cover_file':filename},picture=dict(filename=filename,sha256=hashlib.sha256(raw).hexdigest()),preexisting_cover=game['cover_file']),
                                    dict(cover_ok=1,cover_changed=True))
                    return mime,raw
                client.get=cover_get
                try:
                    cover_changed = native.supplement_cover(ledger, self.db, client, task, self.root / 'data/slgking/catalogue-write.lock')
                    if cover_changed:
                        context.store.cover_result(self.job['id'],target,True,changed=True)
                        context.store.receipt(self.job['id'],target+'#cover',dict(cover_ok=1,cover_changed=True))
                except (StopWork, SourcePaused):
                    raise
                except native.FetchError as exc:
                    native.fail_cover(ledger, task, exc)
                finally:
                    client.get=original_get
            with ledger.conn:
                ledger.enqueue(record['external_id'], target, record['title'], record.get('tags') or [], 'refresh')
                ledger.conn.execute("UPDATE candidates SET kind='refresh',last_success=?,next_due=?,error=NULL WHERE identity=?", (ledger.now(), ledger.now()+604800, record['external_id']))
            row = self.db.execute("SELECT g.cover_file FROM games g JOIN game_sources s ON s.game_id=g.id WHERE s.source_id='f95zone' AND s.external_id=?", (record['external_id'],)).fetchone()
            ok = bool(row and self.existing_cover(row[0]))
            context.store.cover_result(self.job['id'],target,ok)
            counted_cover=target+'#cover' in context.store.done_targets(self.job['id'])
            return dict(cover_ok=int(ok and not counted_cover),cover_missing=int(not ok),cover_changed=bool(cover_changed))
        except native.FetchError as exc:
            if exc.pause:
                ledger.set('paused', exc.reason)
                ledger.set('pause_until', max(float(ledger.get('pause_until', '0')), ledger.now()+max(3600, exc.retry_after)))
                raise SourcePaused(exc.reason) from None
            if candidate:
                native.fail(ledger, candidate, exc)
            raise


class DLsiteAdapter(BaseAdapter):
    def __init__(self, job, root=ROOT):
        super().__init__(job, root)
        # New works publish image_url/official image metadata only. Legacy cover
        # bytes stay opt-in for an explicit repair run, while an already stored
        # legacy cover keeps refreshing under its normal TTL.
        self.legacy_cover_bytes = str(
            os.environ.get('SLGKING_DLSITE_LEGACY_COVERS', '')).strip().lower() in ('1', 'true', 'yes')

    def _cover_bytes_enabled(self, previous_cover):
        return bool(self.legacy_cover_bytes or previous_cover)

    def handle_error(self, error):
        if not hasattr(self, 'native'):
            return
        if isinstance(error, self.native.slg_dlsite.RequestBudgetReached):
            raise StopWork('deadline_or_request_budget') from None
        if isinstance(error, self.native.slg_dlsite.AccessStopped):
            if isinstance(error, self.native.slg_dlsite.PathDisallowed):
                raise ValueError('path_disallowed') from None
            import slg_dlsite_recovery as policy
            kind = getattr(error, 'kind', None) or 'access_protection'
            if kind not in ('pause_active', 'manual_pause', 'probe_limit'):
                failures = int(self.ledger._get('access_failures', 0)) + 1
                delay = policy.retry_delay(kind, failures, getattr(error, 'retry_after', None), self.ledger.clock())
                self.ledger._set('access_failures', failures)
                self.ledger._set('pause_until', max(self.ledger._get('pause_until', 0), self.ledger.clock()+delay))
                diagnostic = getattr(error, 'diagnostic', None) or getattr(getattr(self, 'client', None), 'last_diagnostic', None)
                if diagnostic:
                    self.ledger._set('access_diagnostic', dict(diagnostic, next_check_at=self.ledger._get('pause_until')))
            self.ledger._set('state', 'stopped'); self.ledger._set('reason', kind); self.ledger.conn.commit()
            raise SourcePaused(kind) from None
    @contextmanager
    def locks(self):
        with file_lock(self.root / 'dlsite-data/run.lock'), file_lock(self.root / 'dlsite-data/collector.lock'):
            yield

    def _prepare(self, context):
        import slg_dlsite_worker as native
        self.native = native
        self.ledger = native.Ledger(self.root / 'dlsite-data', max_requests=300)
        self.client = native.WorkerClient(self.root / 'dlsite-data/http', max_requests=300, max_seconds=3600)
        self.client.monotonic=context.monotonic
        self.client.deadline=context.deadline
        self.client.on_request = context.request
        self.ledger.adopt_publication(self.root / 'publish')
        import slg_dlsite_recovery as policy
        control = {row[0]: json.loads(row[1]) for row in self.ledger.conn.execute('SELECT key,value FROM control')}
        decision = policy.pause_decision(control, self.ledger.clock())
        if not decision['allowed'] and not decision['probe_due']:
            raise native.slg_dlsite.AccessStopped('Source pause still active', kind=decision['reason'])
        if decision['probe_due']:
            probes = self.ledger._get('recovery_probes', {})
            count = probes.get('count', 0) if probes.get('day') == self.ledger._day() else 0
            if count >= 4:
                midnight = (datetime.fromisoformat(self.ledger._day())+timedelta(days=1)).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
                self.ledger._set('pause_until', midnight); self.ledger.conn.commit()
                raise native.slg_dlsite.AccessStopped('Daily recovery ceiling', kind='probe_limit')
            self.ledger._set('recovery_probes', {'day': self.ledger._day(), 'count': count+1})
            self.ledger.conn.commit()
            self.client.force_robots = True
        try:
            self.client.load_robots()
        except native.slg_dlsite.AccessStopped:
            raise
        except OSError as error:
            diagnostic = policy.safe_diagnostic('network', 'robots', exception=error, now=self.ledger.clock())
            raise native.slg_dlsite.AccessStopped('Robots unavailable', kind='network', diagnostic=diagnostic) from None
        self.ledger._set('pause_until', 0)
        self.ledger._set('reason', None)
        self.ledger._set('last_protocol_success', self.ledger.clock())
        self.ledger._set('state', 'running')
        self.ledger.conn.commit()

    def candidates(self, context):
        self._prepare(context)
        if self.job['mode'] == 'specified':
            self.ledger.discover(self.job['targets']); yield from self.job['targets']; return
        yield from getattr(self,'pending_targets',())
        seen = set()
        for row in self.ledger.conn.execute('SELECT url FROM works WHERE item IS NULL AND next_fact_due<=? ORDER BY discovered_at,external_id', (self.ledger.clock(),)):
            seen.add(row[0]); yield row[0]
        native = self.native
        pages = [native.slg_dlsite.LATEST_URL, *getattr(self.client, 'sitemaps', [])]
        visited = set()
        while pages:
            url = pages.pop(0)
            if url in visited:
                continue
            visited.add(url)
            self.client.check_url(url)
            try:
                text=self.client.fetch(url)
            except OSError as exc:
                self.ledger._service_failure(exc,3600)
                raise
            self.ledger._service_success()
            works, linked = native.slg_dlsite.discover_links(text, url)
            if not works and not linked:
                raise SourcePaused('discovery_evidence_missing')
            self.ledger.discover(works, linked)
            for target in works:
                row = self.ledger.conn.execute('SELECT item FROM works WHERE external_id=?', (native.slg_dlsite.product_id(target),)).fetchone()
                if target not in seen and row and row[0] is None:
                    seen.add(target); yield target
            pages.extend(linked)

    def collect(self, target, context):
        native, ledger, client = self.native, self.ledger, self.client
        external = native.slg_dlsite.product_id(target)
        ledger.discover([target])
        row = ledger.conn.execute('SELECT * FROM works WHERE external_id=?', (external,)).fetchone()
        cover_only=target in getattr(self,'cover_only_targets',())
        if row['next_fact_due'] > ledger.clock() and row['last_error'] and not cover_only:
            raise StopWork('target_backoff')
        try:
            if cover_only and row['item'] and row['image_url']:
                if row['next_cover_due']>ledger.clock():raise StopWork('cover_backoff')
                item=json.loads(row['item']);image=row['image_url']
            else:
                text = client.fetch(target + '?locale=zh_CN')
                item = native.slg_dlsite.parse_product(text, target)
                image = native.parse_main_artwork(text, external) if item else None
            if not item:
                raise ValueError('ineligible_or_invalid_detail')
            previous = json.loads(row['item']) if row['item'] else None
            changed = previous != item
            if changed:
                context.before_commit(previous is None,target+'#facts',
                    dict(table='works',key={'external_id':external},expected={'item':json.dumps(item,ensure_ascii=False)},preexisting=previous),
                    dict(new=int(previous is None),updated=int(previous is not None)))
            ledger.conn.execute('UPDATE works SET item=?,checked_at=?,next_fact_due=?,image_url=COALESCE(?,image_url),last_error=NULL WHERE external_id=?',
                (json.dumps(item, ensure_ascii=False), ledger.clock(), ledger.clock()+native.DETAIL_TTL, image, external))
            ledger.conn.commit()
            context.store.receipt(self.job['id'], target + '#facts',
                dict(new=int(previous is None), updated=int(previous is not None and changed), duplicate=int(previous is not None and not changed)))
            previous_cover=json.loads(row['cover']) if row['cover'] else None
            context.store.cover_result(self.job['id'],target,bool(previous_cover and self.existing_cover(previous_cover['filename'],previous_cover)))
            cover_changed = False
            if image and row['next_cover_due'] <= ledger.clock() and self._cover_bytes_enabled(previous_cover):
                try:
                    raw = native.normalise_image(client.fetch_image(image))
                    digest = hashlib.sha256(raw).hexdigest(); filename = 'dlsite-' + external + '-' + digest + '.jpg'
                    native.atomic_bytes(ledger.directory / 'covers' / filename, raw)
                    cover = dict(filename=filename, sha256=digest, bytes=len(raw))
                    context.before_commit(False,target+'#cover',
                        dict(table='works',key={'external_id':external},expected={'cover':json.dumps(cover)},picture=dict(filename=filename,sha256=digest),preexisting_cover=row['cover']),
                        dict(cover_ok=1,cover_changed=True))
                    ledger.conn.execute('UPDATE works SET cover=?,cover_checked=?,next_cover_due=?,cover_failures=0 WHERE external_id=?',
                        (json.dumps(cover), ledger.clock(), ledger.clock()+native.COVER_TTL, external)); ledger.conn.commit()
                    cover_changed = not row['cover'] or json.loads(row['cover']) != cover
                    context.store.cover_result(self.job['id'],target,True,changed=cover_changed)
                    context.store.receipt(self.job['id'],target+'#cover',dict(cover_ok=1,cover_changed=cover_changed))
                except (StopWork, SourcePaused, native.slg_dlsite.AccessStopped):
                    raise
                except Exception as exc:
                    ledger._failure(external, 'cover', exc)
            cover = ledger.conn.execute('SELECT cover FROM works WHERE external_id=?', (external,)).fetchone()[0]
            cover_data=json.loads(cover) if cover else None
            ok=bool(cover_data and self.existing_cover(cover_data['filename'],cover_data))
            # Without legacy bytes, a verified official image_url is a usable
            # reference for clients that fetch img.dlsite.jp directly.
            official=image or row['image_url']
            if not ok and official:
                ok=True
            context.store.cover_result(self.job['id'],target,ok)
            counted_cover=target+'#cover' in context.store.done_targets(self.job['id'])
            return dict(cover_ok=int(ok and not counted_cover),cover_missing=int(not ok),cover_changed=cover_changed)
        except native.slg_dlsite.RequestBudgetReached:
            raise StopWork('deadline_or_request_budget') from None
        except native.slg_dlsite.AccessStopped as exc:
            self.handle_error(exc)
        except (StopWork, SourcePaused):
            raise
        except Exception as exc:
            ledger._failure(external, 'fact', exc)
            raise

    def publish(self):
        if not hasattr(self, 'ledger'):
            import slg_dlsite_worker as native
            self.native = native
            self.ledger = native.Ledger(self.root / 'dlsite-data', max_requests=300)
        self.native.publish(self.ledger, self.root / 'publish')


def run_child(job_id, root=ROOT):
    store = CollectionJobs(root / 'cloud/collection_jobs.sqlite3', source_status=lambda s: source_status(s, root))
    job = store.get(job_id)
    if not job or job['status'] != 'running':
        return 1
    worker = 'f95-worker' if job['source_id'] == 'f95zone' else 'dlsite-worker' if job['source_id'] == 'dlsite' else ''
    if worker:
        sys.path.insert(0, str(root / worker))
        # Native workers resolve the shared import implementation here.
        sys.path.append(str(root))
    adapter = {'dikgames': DikgamesAdapter, 'f95zone': F95Adapter, 'dlsite': DLsiteAdapter}[job['source_id']](job, root)
    execute_job(store, job, adapter, root)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    if args.job_id:
        if len(args.job_id) != 32 or any(c not in '0123456789abcdef' for c in args.job_id):
            parser.error('Invalid internal job identity')
        return run_child(args.job_id)
    with file_lock(ROOT / 'cloud/collection-worker.lock'):
        store = CollectionJobs(ROOT / 'cloud/collection_jobs.sqlite3', source_status=source_status)
        store.recover()
        next_recovery = 0
        while True:
            if not args.once and time.monotonic() >= next_recovery:
                next_recovery = time.monotonic() + 600
                protection = source_status('dlsite')
                if not protection.get('global_protection'):
                    # Fixed entry checks due state again under the source run lock.
                    environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                                       PYTHONNOUSERSITE='1')
                    try:
                        subprocess.run([str(ROOT / '.venv/bin/python'), str(ROOT / 'tools/run_collector_service.py'),
                                        '--source', 'dlsite', '--max-requests', '10', '--max-seconds', '900', '--recovery-only'],
                                       env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=4800, check=False)
                    except subprocess.TimeoutExpired:
                        pass  # Source status and persistent receipts survive; next pass rechecks locks.
            job = store.claim()
            if job:
                environment = dict(os.environ, LOCALAPPDATA=str(ROOT / ('dlsite-data' if job['source_id']=='dlsite' else 'data')),
                                   PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                                   PYTHONPATH=str(ROOT / 'dlsite-worker/vendor'))
                try:
                    result = subprocess.run([str(ROOT / '.venv/bin/python'), str(ROOT / 'slg_collection_worker.py'), '--job-id', job['id']],
                        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=LIMITS[job['source_id']][2]+180, check=False)
                    if result.returncode and store.get(job['id'])['status'] == 'running':
                        store.update(job['id'], status='interrupted', reason='worker_exit')
                except subprocess.TimeoutExpired:
                    store.update(job['id'], status='interrupted', reason='worker_timeout')
            if args.once:
                return 0
            time.sleep(10)


if __name__ == '__main__':
    raise SystemExit(main())
