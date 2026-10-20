"""Administrator-owned local collection bridge. No network or publication here.

The HTTP host authenticates administrators before constructing authctx. Device
credentials have no account privileges. Every source request needs reserve(),
which fails closed unless the host supplies its shared persistent budget gate.
"""
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import math
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

MAX_BODY = 8 * 1024 * 1024
EDGE_KIND = 'edge_session'
EDGE_CAPABILITY = 'edge_session_v1'
BROWSER_TTL = 45
BROWSER_PHASES = {'connected','ready','claiming','loading','reading','cover','staging','stopping','blocked','idle','waiting_budget','waiting_request','parsing','completed'}
OWNER_ERRORS = {'owner_closed','owner_logout','owner_connection_lost','browser_closed',
                'debugger_detached','native_unavailable','browser_session_expired','manual_stop'}
EDGE_ERRORS = OWNER_ERRORS | {'proxy_connection_failed','navigation_timeout','content_not_ready','cover_network_failed','cover_invalid'}
BROWSER_STATS = {'requests','blocked_requests','documents','covers','cover_failures','collected_new','skipped_existing'}

ID = re.compile(r'(RJ|VJ)\d{6,10}\Z')
# Default auto sources: official maniax/pro latest-work listings and daily
# rankings. All are allow-listed discovery pages, never account or cart routes.
DEFAULT_AUTO_SOURCES = (
    'https://www.dlsite.com/maniax/works/type/=/work_type_category/game',
    'https://www.dlsite.com/maniax/ranking/day',
    'https://www.dlsite.com/pro/works/type/=/work_type_category/game',
    'https://www.dlsite.com/pro/ranking/day',
)
_WORK_PATH = re.compile(r'/(maniax|pro)/work/=/product_id/((?:RJ|VJ)[0-9]{6,10})\.html\Z')


class BridgeError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def approved_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise BridgeError('invalid_url')
    p = urlsplit(value)
    if (p.scheme != 'https' or p.netloc not in ('www.dlsite.com', 'img.dlsite.jp')
            or p.username or p.password or p.fragment or '\\' in value
            or '%' in p.path or '..' in p.path or any(c in value for c in '\r\n\x00')):
        raise BridgeError('unapproved_url')
    if p.netloc == 'img.dlsite.jp' and not p.path.startswith('/modpub/images2/work/'):
        raise BridgeError('unapproved_image_url')
    return value


def approved_task_url(value):
    approved_url(value)
    p = urlsplit(value)
    if (p.netloc != 'www.dlsite.com' or not re.match(
            r'^/(?:maniax|pro)/(?:work/=/product_id/(?:RJ|VJ)\d{6,10}\.html/?$|works/|fsr/|ranking/)', p.path)):
        raise BridgeError('unapproved_task_url')
    return value


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def work_id_from_url(value):
    """Approved task URL -> canonical RJ/VJ id, or None for a list page.

    Section and prefix must agree: maniax holds RJ works and pro holds VJ works.
    """
    approved_task_url(value)
    match = _WORK_PATH.fullmatch(urlsplit(value).path.rstrip('/'))
    if not match:
        return None
    section, external = match.group(1), match.group(2)
    if (section == 'pro') != external.startswith('VJ'):
        raise BridgeError('identity_url_mismatch')
    return external


def normalise_ids(values, maximum=50):
    """Bound device-supplied ids and normalise case; duplicates collapse."""
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise BridgeError('invalid_ids')
    out = []
    for value in values:
        if not isinstance(value, str):
            raise BridgeError('invalid_ids')
        external = value.strip().upper()
        if not ID.fullmatch(external):
            raise BridgeError('invalid_ids')
        out.append(external)
    return list(dict.fromkeys(out))


def check_items(urls, collected=(), pending=()):
    """One check result per requested URL; invalid URLs keep a public error."""
    collected, pending = set(collected), set(pending)
    items = []
    for url in urls:
        item = {'url': url, 'external_id': None, 'collected': False, 'pending': False}
        try:
            external = work_id_from_url(url)
        except BridgeError as exc:
            item['error'] = exc.code
            items.append(item)
            continue
        if external is None:
            item['error'] = 'not_a_work_url'
            items.append(item)
            continue
        item['external_id'] = external
        item['collected'] = external in collected
        item['pending'] = external in pending
        items.append(item)
    return items


def validate_candidate(value):
    """Reject uncertain work identities/types; keep optional facts unknown."""
    if not isinstance(value, dict):
        raise BridgeError('invalid_candidate')
    work_id = value.get('external_id', '')
    if not isinstance(work_id, str) or not ID.fullmatch(work_id):
        raise BridgeError('invalid_work_id')
    url = approved_url(value.get('source_url'))
    p = urlsplit(url)
    section = 'maniax' if work_id.startswith('RJ') else 'pro'
    if (p.netloc != 'www.dlsite.com' or p.query or
            p.path != f'/{section}/work/=/product_id/{work_id}.html'):
        raise BridgeError('identity_url_mismatch')
    title = value.get('title')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 1000:
        raise BridgeError('missing_title')
    if value.get('work_type') != 'game' or not value.get('type_evidence'):
        raise BridgeError('unverified_game_type')
    if type(value.get('observed_at')) not in (int,float) or not math.isfinite(value['observed_at']):
        raise BridgeError('missing_observation_time')
    if not re.fullmatch('[a-f0-9]{64}', str(value.get('page_sha256', ''))):
        raise BridgeError('missing_evidence_hash')
    out = {k: value.get(k) for k in ('external_id', 'source_url', 'title',
           'developer', 'release_date', 'work_type', 'type_evidence',
           'observed_at', 'page_sha256', 'image_url', 'outline')}
    out['title'] = title.strip()
    outline=out.get('outline') or {}
    if (not isinstance(outline,dict) or len(outline)>40 or
            any(not isinstance(k,str) or len(k)>100 or not isinstance(v,str) or len(v)>2000 for k,v in outline.items())):
        raise BridgeError('invalid_outline')
    out['outline']=outline
    for field, limit in (('developer', 1000), ('release_date', 40), ('type_evidence', 1000)):
        if out[field] is not None and (not isinstance(out[field],str) or len(out[field])>limit):
            raise BridgeError('invalid_fact_field')
    if out['image_url']:
        image_url = approved_url(out['image_url'])
        if (urlsplit(image_url).netloc != 'img.dlsite.jp' or
                not re.search(r'(?<![A-Z0-9])' + work_id + r'(?!\d)', image_url)):
            raise BridgeError('wrong_image_identity')
    image = value.get('image_base64')
    if image:
        try:
            raw = base64.b64decode(image, validate=True)
            if not 0 < len(raw) <= 5 * 1024 * 1024:
                raise ValueError()
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as img:
                if (img.format not in ('JPEG', 'PNG', 'WEBP') or
                        img.width * img.height > 25_000_000 or
                        getattr(img, 'n_frames', 1) != 1):
                    raise ValueError()
                img.load()
            out['image_base64'] = image
            out['image_sha256'] = hashlib.sha256(raw).hexdigest()
        except Exception as exc:
            raise BridgeError('invalid_image') from exc
    return out


class BridgeStore:
    def __init__(self, path, clock=time.time, budget_gate=None, pause_gate=None):
        self.path, self.clock = str(path), clock
        self.budget_gate = budget_gate
        # Return True only for total/manual pauses, not server direct-route refusal.
        self.pause_gate = pause_gate or (lambda: False)
        with self.db() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS bridge_devices(id TEXT PRIMARY KEY,
                name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,last_seen REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS bridge_pairings(code_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL, expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS bridge_tasks(id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,
                created REAL NOT NULL,device TEXT,lease_hash TEXT,lease_until REAL,
                started REAL,requests INTEGER NOT NULL DEFAULT 0,
                request_limit INTEGER NOT NULL,runtime INTEGER NOT NULL,
                last_request REAL NOT NULL DEFAULT 0,error TEXT,stats TEXT);
              CREATE TABLE IF NOT EXISTS bridge_requests(id INTEGER PRIMARY KEY,
                task TEXT NOT NULL,day TEXT NOT NULL,url_hash TEXT NOT NULL,
                created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS bridge_staging(task TEXT NOT NULL,
                work_id TEXT NOT NULL,payload TEXT NOT NULL,payload_hash TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending_review',
                PRIMARY KEY(task,work_id));
              CREATE TABLE IF NOT EXISTS bridge_config(key TEXT PRIMARY KEY,value TEXT);
              CREATE TABLE IF NOT EXISTS bridge_results(task TEXT PRIMARY KEY,counters TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS bridge_import_receipts(task TEXT,work_id TEXT,kind TEXT NOT NULL,
                PRIMARY KEY(task,work_id));
            ''')
            c.execute('''CREATE TABLE IF NOT EXISTS bridge_browser_sessions(
                id TEXT PRIMARY KEY,device_id TEXT NOT NULL,owner_hash TEXT NOT NULL,
                capability TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,
                last_seen REAL NOT NULL,expires_at REAL NOT NULL,attach_hash TEXT,
                attach_until REAL NOT NULL,phase TEXT NOT NULL,reason TEXT,stats TEXT)''')
            # Backward-compatible columns: old tasks always remain legacy tasks.
            task_columns = {row[1] for row in c.execute('PRAGMA table_info(bridge_tasks)')}
            for name, declaration in (('execution_kind',"TEXT NOT NULL DEFAULT 'legacy'"),
                                      ('browser_session_id','TEXT'),('request_id','TEXT'),
                                      ('request_hash','TEXT')):
                if name not in task_columns:
                    c.execute('ALTER TABLE bridge_tasks ADD COLUMN '+name+' '+declaration)
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS bridge_edge_request ON bridge_tasks(request_id) WHERE request_id IS NOT NULL')
            # Idempotent migration for databases created before device stats.
            columns = {row[1] for row in c.execute('PRAGMA table_info(bridge_tasks)')}
            if 'stats' not in columns:
                c.execute('ALTER TABLE bridge_tasks ADD COLUMN stats TEXT')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=15000')
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @contextmanager
    def _read(self):
        """Read committed state without taking the write transaction."""
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def pair_code(self, name):
        code = secrets.token_urlsafe(24)
        with self.db() as c:
            c.execute('DELETE FROM bridge_pairings WHERE expires<?', (self.clock(),))
            c.execute('INSERT INTO bridge_pairings VALUES(?,?,?)',
                      (_digest(code), str(name)[:80], self.clock() + 600))
        return {'pair_code': code, 'expires_in': 600}

    def pair(self, code):
        with self.db() as c:
            row = c.execute('SELECT * FROM bridge_pairings WHERE code_hash=? AND expires>?',
                            (_digest(str(code)), self.clock())).fetchone()
            if not row:
                raise BridgeError('invalid_pair_code', 403)
            device, token = secrets.token_hex(12), secrets.token_urlsafe(48)
            c.execute('DELETE FROM bridge_pairings WHERE code_hash=?', (_digest(code),))
            c.execute('INSERT INTO bridge_devices VALUES(?,?,?,0,?)',
                      (device, row['name'], _digest(token), self.clock()))
            return {'device_id': device, 'device_token': token}

    def _device(self, c, token):
        row = c.execute('SELECT id FROM bridge_devices WHERE token_hash=? AND revoked=0',
                        (_digest(str(token)),)).fetchone()
        if not row:
            raise BridgeError('device_unauthorized', 401)
        c.execute('UPDATE bridge_devices SET last_seen=? WHERE id=?',
                  (self.clock(), row['id']))
        return row['id']

    @staticmethod
    def _capabilities(values):
        if (not isinstance(values,list) or not 1 <= len(values) <= 10 or
                any(not isinstance(v,str) or not re.fullmatch(r'[a-z0-9_]{1,80}',v) for v in values)
                or EDGE_CAPABILITY not in values):
            raise BridgeError('browser_capability_required',409)

    @staticmethod
    def _sid(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{32}',value):
            raise BridgeError('browser_session_required',409)
        return value

    def _safe_session(self,row):
        return {key:row[key] for key in ('id','device_id','state','last_seen','expires_at','phase','reason')} | {'proxy_verified':False}

    def _expire_sessions(self,c):
        now=self.clock()
        stale=[r['id'] for r in c.execute("SELECT id FROM bridge_browser_sessions WHERE state IN ('pending','connected') AND expires_at<=?",(now,))]
        for sid in stale:
            self._close_browser(c,sid,'browser_session_expired',state='expired')
        # A lost task lease is terminal for owner-bound jobs, never requeued.
        c.execute("UPDATE bridge_tasks SET state='interrupted',error='owner_connection_lost',lease_hash=NULL WHERE execution_kind=? AND state='running' AND lease_until<=?",(EDGE_KIND,now))

    def sweep_browser_sessions(self):
        with self.db() as c:
            self._expire_sessions(c)
            return [r['id'] for r in c.execute("SELECT id FROM bridge_tasks WHERE execution_kind=? AND state='interrupted'",(EDGE_KIND,))]

    def _session(self,c,sid,device=None):
        self._sid(sid)
        row=c.execute('SELECT * FROM bridge_browser_sessions WHERE id=?',(sid,)).fetchone()
        if not row or (device is not None and row['device_id']!=device):
            raise BridgeError('browser_session_mismatch',409)
        if row['state']!='connected' or row['expires_at']<=self.clock():
            raise BridgeError('browser_session_expired' if row['expires_at']<=self.clock() else 'browser_session_not_connected',409)
        return row

    def _close_browser(self,c,sid,reason,state='closed'):
        c.execute("UPDATE bridge_browser_sessions SET state=?,phase='stopping',reason=?,attach_hash=NULL WHERE id=? AND state IN ('pending','connected')",(state,reason,sid))
        c.execute("UPDATE bridge_tasks SET state='interrupted',error=?,lease_hash=NULL WHERE execution_kind=? AND browser_session_id=? AND state IN ('queued','running')",(reason,EDGE_KIND,sid))

    def create_browser_session(self,device_id,owner_nonce,capability):
        if not isinstance(device_id,str) or not re.fullmatch(r'[a-f0-9]{24}',device_id):
            raise BridgeError('device_unavailable',409)
        if capability!=EDGE_CAPABILITY:
            raise BridgeError('browser_capability_required',409)
        if not isinstance(owner_nonce,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',owner_nonce):
            raise BridgeError('invalid_owner_nonce')
        self.sweep_browser_sessions()
        code=secrets.token_urlsafe(32); sid=secrets.token_hex(16); now=self.clock()
        with self.db() as c:
            if not c.execute('SELECT 1 FROM bridge_devices WHERE id=? AND revoked=0',(device_id,)).fetchone():
                raise BridgeError('device_unavailable',409)
            c.execute('INSERT INTO bridge_browser_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (sid,device_id,_digest(owner_nonce),capability,'pending',now,now,now+30,_digest(code),now+30,'connected',None,None))
            row=c.execute('SELECT * FROM bridge_browser_sessions WHERE id=?',(sid,)).fetchone()
            return {'ok':True,'browser_session':self._safe_session(row),'attach_code':code}

    def browser_attach(self,token,sid,attach_code,owner_nonce,capabilities):
        self._capabilities(capabilities); self._sid(sid); self.sweep_browser_sessions()
        with self.db() as c:
            device=self._device(c,token)
            row=c.execute('SELECT * FROM bridge_browser_sessions WHERE id=?',(sid,)).fetchone()
            if (not row or row['device_id']!=device or row['state']!='pending' or
                    row['attach_until']<=self.clock() or
                    not secrets.compare_digest(row['attach_hash'] or '',_digest(str(attach_code))) or
                    not secrets.compare_digest(row['owner_hash'],_digest(str(owner_nonce)))):
                raise BridgeError('invalid_browser_attach',409)
            now=self.clock()
            c.execute("UPDATE bridge_browser_sessions SET state='connected',attach_hash=NULL,last_seen=?,expires_at=?,phase='ready' WHERE id=?",(now,now+BROWSER_TTL,sid))
            row=c.execute('SELECT * FROM bridge_browser_sessions WHERE id=?',(sid,)).fetchone()
            return {'ok':True,'browser_session':self._safe_session(row)}

    def browser_heartbeat(self,token,sid,phase,reason=None,stats=None):
        if not isinstance(phase,str) or phase not in BROWSER_PHASES or (reason is not None and (not isinstance(reason,str) or reason not in EDGE_ERRORS | {'access_denied','rate_limited','challenge','budget_exhausted','runtime_exhausted','parse_failed','network_failed','already_collected'})):
            raise BridgeError('invalid_browser_status')
        clean={}
        if stats is not None:
            if not isinstance(stats,dict) or set(stats)-BROWSER_STATS:
                raise BridgeError('invalid_stats')
            for key,value in stats.items():
                if type(value) is not int or not 0<=value<=1_000_000: raise BridgeError('invalid_stats')
                clean[key]=value
        self.sweep_browser_sessions()
        with self.db() as c:
            device=self._device(c,token); self._session(c,sid,device)
            now=self.clock()
            c.execute('UPDATE bridge_browser_sessions SET last_seen=?,expires_at=?,phase=?,reason=?,stats=? WHERE id=?',
                      (now,now+BROWSER_TTL,phase,reason,json.dumps(clean),sid))
        return {'ok':True,'expires_at':now+BROWSER_TTL}

    def browser_stop(self,sid,reason,token=None):
        self._sid(sid)
        if not isinstance(reason,str) or reason not in OWNER_ERRORS: raise BridgeError('invalid_error_code')
        self.sweep_browser_sessions()
        with self.db() as c:
            device=self._device(c,token) if token is not None else None
            row=c.execute('SELECT * FROM bridge_browser_sessions WHERE id=?',(sid,)).fetchone()
            if not row or (device is not None and row['device_id']!=device):
                raise BridgeError('browser_session_mismatch',409)
            self._close_browser(c,sid,reason)
        return {'ok':True,'state':'closed'}

    def check_browser_task(self,token,task,lease,sid):
        self.sweep_browser_sessions()
        with self.db() as c:
            self._task(c,token,task,lease,sid)
        return {'ok':True}

    def existing_request(self,request_id,sid,body=None):
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-f0-9]{32}',request_id):
            raise BridgeError('invalid_request_id')
        self.sweep_browser_sessions()
        with self.db() as c:
            self._session(c,sid)
            row=c.execute('SELECT * FROM bridge_tasks WHERE request_id=?',(request_id,)).fetchone()
            if not row:return None
            # Body excludes its ephemeral request id; full original intent is fixed.
            digest=_digest(json.dumps(body,sort_keys=True,separators=(',',':'))) if body is not None else None
            if row['browser_session_id']!=sid or (digest is not None and row['request_hash']!=digest):
                raise BridgeError('request_id_conflict',409)
            return {'task_id':row['id'],'state':row['state'],'idempotent':True}

    def enqueue(self, urls, mode='diagnostic', target=None, sources=None, *, execution_kind=None, browser_session_id=None, request_id=None, request_body=None):
        """Queue one device task.

        diagnostic keeps the legacy 10 request/900s envelope; auto discovers
        from sources up to target new works; specified visits explicit URLs.
        """
        if mode not in ('diagnostic', 'auto', 'specified', 'daily', 'manual'):
            raise BridgeError('invalid_task')
        limits = {'diagnostic': (10, 900), 'auto': (300, 3600),
                  'specified': (300, 3600), 'daily': (300, 3600),
                  'manual': (300, 3600)}
        if mode == 'auto':
            if target is None:
                target = 20
            if type(target) is not int or not 1 <= target <= 50:
                raise BridgeError('invalid_target')
            # Auto owns its own discovery list; explicit URLs belong to specified.
            if urls not in (None, []):
                raise BridgeError('invalid_task')
            if sources is None:
                sources = []
            if not isinstance(sources, list) or len(sources) > 50:
                raise BridgeError('invalid_sources')
            sources = list(dict.fromkeys(approved_task_url(u) for u in sources))
            payload = {'target': target,
                       'sources': sources or list(DEFAULT_AUTO_SOURCES)}
        else:
            maximum = 100 if mode in ('diagnostic', 'daily') else 50
            if not isinstance(urls, list) or not 1 <= len(urls) <= maximum:
                raise BridgeError('invalid_task')
            urls = list(dict.fromkeys(approved_task_url(u) for u in urls))
            payload = {'urls': urls}
        if execution_kind is not None and execution_kind!=EDGE_KIND:
            raise BridgeError('invalid_execution_kind')
        if execution_kind==EDGE_KIND:
            if mode not in ('diagnostic','auto','specified'): raise BridgeError('invalid_task')
            if self.pause_gate(): raise BridgeError('manual_paused',409)
            old=self.existing_request(request_id,browser_session_id,request_body)
            if old:return old
        elif browser_session_id is not None or request_id is not None:
            raise BridgeError('invalid_execution_kind')
        task = secrets.token_hex(12)
        with self.db() as c:
            if execution_kind==EDGE_KIND:
                self._session(c,browser_session_id)
                prior=c.execute('SELECT id,browser_session_id,request_hash,state FROM bridge_tasks WHERE request_id=?',(request_id,)).fetchone()
                if prior:
                    if prior['browser_session_id']!=browser_session_id or prior['request_hash']!=_digest(json.dumps(request_body,sort_keys=True,separators=(',',':'))):
                        raise BridgeError('request_id_conflict',409)
                    return {'task_id':prior['id'],'state':prior['state'],'idempotent':True}
                if c.execute("SELECT 1 FROM bridge_tasks WHERE execution_kind=? AND browser_session_id=? AND state IN ('queued','running')",(EDGE_KIND,browser_session_id)).fetchone():
                    raise BridgeError('browser_session_busy',409)
            c.execute('INSERT INTO bridge_tasks(id,mode,payload,state,created,request_limit,runtime) VALUES(?,?,?,?,?,?,?)',
                      (task, mode, json.dumps(payload), 'queued', self.clock(), *limits[mode]))
            if execution_kind==EDGE_KIND:
                c.execute('UPDATE bridge_tasks SET execution_kind=?,browser_session_id=?,request_id=?,request_hash=? WHERE id=?',
                    (EDGE_KIND,browser_session_id,request_id,_digest(json.dumps(request_body,sort_keys=True,separators=(',',':'))),task))
        return {'task_id': task, 'state': 'waiting_for_device',**({'execution_kind':EDGE_KIND,'browser_session_id':browser_session_id} if execution_kind else {})}

    def verify_device(self, token):
        """Validate a bearer device without updating last_seen (check is read-only)."""
        with self._read() as c:
            row = c.execute('SELECT id FROM bridge_devices WHERE token_hash=? AND revoked=0',
                            (_digest(str(token)),)).fetchone()
        if not row:
            raise BridgeError('device_unauthorized', 401)
        return row['id']

    def pending_ids(self, ids, exclude_task=None):
        """External ids already waiting: staging review or queued/running task URLs."""
        wanted = {str(value).upper() for value in ids or []
                  if isinstance(value, str)}
        if not wanted:
            return set()
        found = set()
        with self._read() as c:
            for row in c.execute("SELECT payload FROM bridge_staging WHERE state='pending_review'"):
                try:
                    external = json.loads(row[0]).get('external_id')
                except (ValueError, TypeError, AttributeError):
                    continue
                if external in wanted:
                    found.add(external)
            for row in c.execute("SELECT payload,id FROM bridge_tasks WHERE state IN ('queued','running')"):
                if exclude_task is not None and row['id']==exclude_task:continue
                try:
                    payload = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                for url in list(payload.get('urls') or []) + list(payload.get('sources') or []):
                    try:
                        external = work_id_from_url(url)
                    except BridgeError:
                        continue
                    if external in wanted:
                        found.add(external)
        return found

    def claim(self, token, task_id=None, execution_kind=None, browser_session_id=None, capabilities=None):
        if task_id is not None and (not isinstance(task_id,str) or
                not re.fullmatch(r'[a-zA-Z0-9-]{1,100}',task_id)):
            raise BridgeError('invalid_task_id')
        if execution_kind is not None and execution_kind!=EDGE_KIND: raise BridgeError('invalid_execution_kind')
        if execution_kind==EDGE_KIND:self._capabilities(capabilities)
        elif browser_session_id is not None:raise BridgeError('browser_session_required',409)
        self.sweep_browser_sessions()
        with self.db() as c:
            device = self._device(c, token)
            if execution_kind==EDGE_KIND:self._session(c,browser_session_id,device)
            if self.pause_gate():
                return {'task': None, 'state': 'manual_paused'}
            stop = c.execute("SELECT value FROM bridge_config WHERE key='auxiliary_stop'").fetchone()
            if stop:
                stop = json.loads(stop[0])
                if stop.get('until') is None or self.clock()<stop['until']:
                    return {'task':None,'state':'auxiliary_paused','reason':stop['reason']}
            c.execute("UPDATE bridge_tasks SET state='failed',error='runtime_exhausted',lease_hash=NULL WHERE state='running' AND started+runtime<=?", (self.clock(),))
            # A single global lease prevents two devices doubling request budgets.
            if c.execute("SELECT 1 FROM bridge_tasks WHERE state='running' AND lease_until>?", (self.clock(),)).fetchone():
                return {'task': None, 'state': 'busy'}
            c.execute("UPDATE bridge_tasks SET state='queued',device=NULL,lease_hash=NULL WHERE execution_kind!='edge_session' AND state='running' AND lease_until<=?", (self.clock(),))
            if execution_kind==EDGE_KIND:
                query="SELECT * FROM bridge_tasks WHERE state='queued' AND execution_kind=? AND browser_session_id=?"
                args=[EDGE_KIND,browser_session_id]
                if task_id is not None:query+=' AND id=?';args.append(task_id)
                row=c.execute(query+' ORDER BY created,id LIMIT 1',args).fetchone()
            elif task_id is None:
                row = c.execute("SELECT * FROM bridge_tasks WHERE state='queued' AND execution_kind!='edge_session' ORDER BY created,id LIMIT 1").fetchone()
            else:
                row = c.execute("SELECT * FROM bridge_tasks WHERE state='queued' AND execution_kind!='edge_session' AND id=? AND mode='diagnostic'",(task_id,)).fetchone()
            if not row:
                return {'task': None, 'state': 'idle'}
            lease = secrets.token_urlsafe(32)
            c.execute("UPDATE bridge_tasks SET state='running',device=?,lease_hash=?,lease_until=?,started=COALESCE(started,?) WHERE id=?",
                      (device, _digest(lease), self.clock()+120, self.clock(), row['id']))
            payload = json.loads(row['payload'])
            return {'task': {'id': row['id'], 'kind': 'dlsite_browser_v1',
                            'mode': row['mode'],
                            'target': payload.get('target'),
                            'sources': payload.get('sources') or [],
                            'urls': payload.get('urls') or [],
                            'lease': lease, 'request_limit': row['request_limit'],
                            'runtime': row['runtime'], 'requests_used': row['requests'],
                            'execution_kind':row['execution_kind'],'browser_session_id':row['browser_session_id']}}

    def _task(self, c, token, task, lease, browser_session_id=None):
        device = self._device(c, token)
        row = c.execute("SELECT * FROM bridge_tasks WHERE id=? AND device=? AND lease_hash=? AND state='running' AND lease_until>?",
                        (task, device, _digest(str(lease)), self.clock())).fetchone()
        if not row:
            raise BridgeError('stale_or_cancelled_lease', 409)
        if row['execution_kind']==EDGE_KIND:
            if row['browser_session_id']!=browser_session_id:raise BridgeError('browser_session_mismatch',409)
            self._session(c,browser_session_id,device)
        elif browser_session_id is not None:raise BridgeError('browser_session_mismatch',409)
        if self.pause_gate():
            raise BridgeError('manual_paused', 409)
        if self.clock() - row['started'] > row['runtime']:
            raise BridgeError('runtime_exhausted', 409)
        return row

    def heartbeat(self, token, task, lease, browser_session_id=None):
        self.sweep_browser_sessions()
        with self.db() as c:
            self._task(c, token, task, lease, browser_session_id)
            c.execute('UPDATE bridge_tasks SET lease_until=? WHERE id=?', (self.clock()+120, task))
        return {'ok': True}

    def reserve(self, token, task, lease, url, browser_session_id=None):
        self.sweep_browser_sessions()
        approved_url(url)
        with self.db() as c:
            row = self._task(c, token, task, lease, browser_session_id)
            if row['requests'] >= row['request_limit']:
                raise BridgeError('budget_exhausted', 409)
            source_last=c.execute("SELECT value FROM bridge_config WHERE key='last_request'").fetchone()
            last=max(row['last_request'],float(source_last[0]) if source_last else 0)
            wait = 10 - (self.clock()-last)
            if last and wait > 0:
                return {'allowed': False, 'wait_seconds': wait}
            if not self.budget_gate:
                raise BridgeError('shared_budget_not_configured', 503)
            # Host gate must atomically share current collector/append quotas and
            # persist BEFORE returning; charged failures are intentionally retained.
            result = self.budget_gate(mode=row['mode'], url=url, task_id=task)
            if not result or result.get('allowed') is not True:
                return {'allowed': False, 'wait_seconds': max(10, float((result or {}).get('wait_seconds', 10)))}
            now = self.clock()
            day = time.strftime('%Y-%m-%d', time.gmtime(now+8*3600))
            c.execute('INSERT INTO bridge_requests(task,day,url_hash,created) VALUES(?,?,?,?)', (task, day, _digest(url), now))
            c.execute('UPDATE bridge_tasks SET requests=requests+1,last_request=?,lease_until=? WHERE id=?', (now, now+120, task))
            c.execute("INSERT OR REPLACE INTO bridge_config VALUES('last_request',?)",(str(now),))
        if browser_session_id is not None:
            # The external persistent-budget adapter may take time. A permit
            # cannot outlive its owner while that call was in flight; retain any
            # already committed charge and reject transmission after expiry.
            self.check_browser_task(token,task,lease,browser_session_id)
        return {'allowed': True}

    def submit(self, token, task, lease, candidates, browser_session_id=None):
        self.sweep_browser_sessions()
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 10:
            raise BridgeError('batch_limit')
        values = [validate_candidate(v) for v in candidates]
        with self.db() as c:
            row = self._task(c, token, task, lease, browser_session_id)
            if not row['requests']:
                raise BridgeError('no_reserved_observation', 409)
            accepted = 0
            for v in values:
                if v['observed_at'] > self.clock()+300 or v['observed_at'] < row['created']-300:
                    raise BridgeError('stale_observation')
                payload = json.dumps(v, ensure_ascii=False, sort_keys=True)
                old = c.execute('SELECT payload_hash FROM bridge_staging WHERE task=? AND work_id=?', (task, v['external_id'])).fetchone()
                if old and old[0] != _digest(payload):
                    raise BridgeError('conflicting_duplicate', 409)
                if not old:
                    c.execute('INSERT INTO bridge_staging(task,work_id,payload,payload_hash) VALUES(?,?,?,?)', (task, v['external_id'], payload, _digest(payload)))
                    accepted += 1
        return {'accepted': accepted, 'state': 'pending_review'}

    @staticmethod
    def _device_stats(stats):
        if not isinstance(stats, dict) or set(stats) - {'collected_new', 'skipped_existing', 'stop_reason'}:
            raise BridgeError('invalid_stats')
        out = {}
        for key in ('collected_new', 'skipped_existing'):
            value = stats.get(key, 0)
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise BridgeError('invalid_stats')
            out[key] = value
        reason = stats.get('stop_reason')
        if reason is not None:
            if not isinstance(reason, str) or not re.fullmatch(r'[a-z_]{1,100}', reason):
                raise BridgeError('invalid_stats')
            out['stop_reason'] = reason
        return out

    def finish(self, token, task, lease, error=None, retry_after=None, stats=None, browser_session_id=None):
        self.sweep_browser_sessions()
        # Only fixed public error codes; never retain cookies, URLs or exception text.
        codes = {'access_denied','rate_limited','challenge','budget_exhausted',
                 'runtime_exhausted','parse_failed','network_failed','cancelled',
                 'already_collected'} | EDGE_ERRORS
        if error is not None and (not isinstance(error,str) or error not in codes):
            raise BridgeError('invalid_error_code')
        if retry_after is not None and (type(retry_after) not in (int,float) or not 0<=retry_after<=366*86400):
            raise BridgeError('invalid_wait')
        value = self._device_stats(stats) if stats is not None else None
        with self.db() as c:
            row=self._task(c, token, task, lease, browser_session_id)
            if row['execution_kind']!=EDGE_KIND and error in EDGE_ERRORS:raise BridgeError('invalid_error_code')
            if (row['execution_kind']==EDGE_KIND and value is not None and value.get('stop_reason') is not None
                    and value['stop_reason'] not in codes | {'all_existing','target_reached','sources_exhausted','diagnostic_complete','specified_complete'}):
                raise BridgeError('invalid_stats')
            if error == 'already_collected':
                # Explicit no-work terminal state; never a failure or a pause.
                state = 'already_collected'
            elif error in OWNER_ERRORS:
                state='interrupted'
            else:
                state = 'failed' if error else 'awaiting_review'
            c.execute('UPDATE bridge_tasks SET state=?,error=?,lease_hash=NULL,stats=? WHERE id=?',
                      (state, None if error == 'already_collected' else error,
                       json.dumps(value) if value is not None else None, task))
            if error in ('access_denied','challenge','rate_limited'):
                until = self.clock()+max(3600,retry_after or 0) if error=='rate_limited' else None
                c.execute("INSERT OR REPLACE INTO bridge_config VALUES('auxiliary_stop',?)", (json.dumps({'reason':error,'until':until}),))
        return {'ok': True, 'state': state}

    def status(self):
        self.sweep_browser_sessions()
        with self.db() as c:
            devices = [dict(r) for r in c.execute('SELECT id,name,revoked,last_seen FROM bridge_devices')]
            tasks = [dict(r) for r in c.execute('SELECT id,mode,payload,state,created,requests,request_limit,error,stats,execution_kind,browser_session_id FROM bridge_tasks ORDER BY created DESC LIMIT 100')]
            for task in tasks:
                task['stats']=json.loads(task['stats']) if task['stats'] else None
                try:
                    payload=json.loads(task.pop('payload'))
                except (ValueError,TypeError):
                    payload={}
                # Auto shows its numeric target; specified/diagnostic have none.
                task['target']=payload.get('target')
                task['staged']=c.execute('SELECT count(*) FROM bridge_staging WHERE task=?',(task['id'],)).fetchone()[0]
                result=c.execute('SELECT counters FROM bridge_results WHERE task=?',(task['id'],)).fetchone()
                task['result']=json.loads(result[0]) if result else None
            for d in devices:
                d['online'] = not d['revoked'] and self.clock()-d['last_seen'] < 120
            plan=c.execute("SELECT value FROM bridge_config WHERE key='daily'").fetchone()
            stop=c.execute("SELECT value FROM bridge_config WHERE key='auxiliary_stop'").fetchone()
            sessions=[]
            for row in c.execute('SELECT * FROM bridge_browser_sessions ORDER BY created DESC LIMIT 100'):
                safe=self._safe_session(row);safe['stats']=json.loads(row['stats']) if row['stats'] else {}
                sessions.append(safe)
            return {'devices': devices, 'tasks': tasks,'browser_sessions':sessions,
                    'plan':json.loads(plan[0]) if plan else {'enabled':False,'urls':[]},
                    'auxiliary_stop':json.loads(stop[0]) if stop else None,
                    'pending_review': c.execute("SELECT count(*) FROM bridge_staging WHERE state='pending_review'").fetchone()[0],
                    'direct_route_pause_unchanged': True}

    def pending_candidates(self, task_id, limit=10):
        """Host-only review/publication adapter; not a device download route."""
        if not 1<=limit<=10: raise BridgeError('batch_limit')
        with self.db() as c:
            return [json.loads(r[0]) for r in c.execute(
                "SELECT payload FROM bridge_staging WHERE task=? AND state='pending_review' ORDER BY work_id LIMIT ?", (task_id,limit))]

    def admin_action(self, action, body):
        if action == 'pair': return self.pair_code(body.get('name', '采集设备'))
        if action == 'tasks':
            return self.enqueue(body.get('urls'), body.get('mode', 'diagnostic'),
                                target=body.get('target'), sources=body.get('sources'),
                                execution_kind=body.get('execution_kind'),browser_session_id=body.get('browser_session_id'),
                                request_id=body.get('request_id'),request_body=body)
        self.sweep_browser_sessions()
        with self.db() as c:
            if action in ('cancel','retry'):
                row=c.execute('SELECT execution_kind,browser_session_id FROM bridge_tasks WHERE id=?',(body.get('task_id'),)).fetchone()
                if row and row['execution_kind']==EDGE_KIND:
                    if action=='retry':raise BridgeError('browser_session_required',409)
                    self._close_browser(c,row['browser_session_id'],'manual_stop')
                    return {'ok':True}
            if action == 'revoke':
                c.execute('UPDATE bridge_devices SET revoked=1 WHERE id=?', (body.get('device_id'),))
                for row in c.execute("SELECT id FROM bridge_browser_sessions WHERE device_id=? AND state IN ('pending','connected')",(body.get('device_id'),)).fetchall():
                    self._close_browser(c,row['id'],'owner_connection_lost')
                c.execute("UPDATE bridge_tasks SET state='queued',device=NULL,lease_hash=NULL WHERE execution_kind!='edge_session' AND device=? AND state='running'", (body.get('device_id'),))
            elif action == 'cancel':
                c.execute("UPDATE bridge_tasks SET state='cancelled',lease_hash=NULL WHERE id=? AND state IN ('queued','running','failed')", (body.get('task_id'),))
            elif action == 'retry':
                # Preserve consumed requests and original runtime: retry is not a
                # quota-reset button. A new-day plan is a separate shared-budget job.
                c.execute("UPDATE bridge_tasks SET state='queued',device=NULL,lease_hash=NULL,error=NULL WHERE id=? AND state='failed'", (body.get('task_id'),))
            elif action == 'plan':
                urls = body.get('urls', [])
                if not isinstance(urls, list) or len(urls)>100:
                    raise BridgeError('invalid_plan')
                for url in urls: approved_task_url(url)
                c.execute("INSERT OR REPLACE INTO bridge_config VALUES('daily',?)", (json.dumps({'enabled': body.get('enabled') is True, 'urls': urls}),))
            else: raise BridgeError('not_found', 404)
        return {'ok': True}

    def enqueue_daily(self, day):
        """Called by existing 02:00 scheduler; idempotent, no source I/O."""
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(day)):
            raise BridgeError('invalid_day')
        with self.db() as c:
            row = c.execute("SELECT value FROM bridge_config WHERE key='daily'").fetchone()
            plan = json.loads(row[0]) if row else {}
            if not plan.get('enabled') or not plan.get('urls'):
                return {'queued': False}
            task = 'daily-'+day
            c.execute("INSERT OR IGNORE INTO bridge_tasks(id,mode,payload,state,created,request_limit,runtime) VALUES(?,?,?,'queued',?,300,3600)", (task, 'daily', json.dumps({'urls': plan['urls']}), self.clock()))
            return {'queued': True, 'task_id': task}


class BridgeAPI:
    def __init__(self, store, publish_callback=None):
        self.store = store
        self.publish_callback=publish_callback

    def handle(self, method, path, body, authctx):
        """Host bounds body before JSON decode; maps tuple to existing JSON writer.

        Admin context MUST come from existing verified admin Cookie/CSRF checks.
        Device context is isolated bearer, never reused account authentication.
        """
        try:
            if not isinstance(body, dict) or len(json.dumps(body).encode()) > MAX_BODY:
                raise BridgeError('invalid_body', 413)
            admin = path.startswith('/admin/collector-bridge/')
            prefix = '/admin/collector-bridge/' if admin else '/collector-bridge/'
            if not path.startswith(prefix): raise BridgeError('not_found', 404)
            action = path[len(prefix):]
            if admin:
                if authctx.get('admin') is not True: raise BridgeError('admin_required', 403)
                if action == 'status' and method == 'GET': return 200, self.store.status()
                if method != 'POST': raise BridgeError('method_not_allowed', 405)
                if action=='browser-sessions':
                    if set(body)-{'device_id','owner_nonce','capability'}:raise BridgeError('invalid_body')
                    return 200,self.store.create_browser_session(body.get('device_id'),body.get('owner_nonce'),body.get('capability'))
                match=re.fullmatch(r'browser-sessions/([a-f0-9]{32})/stop',action)
                if match:
                    if set(body)-{'reason'}:raise BridgeError('invalid_body')
                    return 200,self.store.browser_stop(match.group(1),body.get('reason','manual_stop'))
                if action=='tasks':
                    if set(body)-{'urls','mode','target','sources','execution_kind','browser_session_id','request_id'}:raise BridgeError('invalid_body')
                if action=='publish':
                    if not self.publish_callback: raise BridgeError('publication_not_configured',503)
                    return 200,self.publish_callback(body.get('task_id'))
                return 200, self.store.admin_action(action, body)
            if method != 'POST': raise BridgeError('method_not_allowed', 405)
            if action == 'pair': return 200, self.store.pair(body.get('pair_code'))
            token = authctx.get('device_token', '')
            if action=='browser_hello':
                if set(body)-{'capabilities'}:raise BridgeError('invalid_body')
                self.store._capabilities(body.get('capabilities'))
                return 200,{'ok':True,'protocol':1,'device_id':self.store.verify_device(token),'capabilities':[EDGE_CAPABILITY]}
            if action=='browser_attach':
                if set(body)-{'browser_session_id','attach_code','owner_nonce','capabilities'}:raise BridgeError('invalid_body')
                return 200,self.store.browser_attach(token,body.get('browser_session_id'),body.get('attach_code'),body.get('owner_nonce'),body.get('capabilities'))
            if action=='browser_heartbeat':
                if set(body)-{'browser_session_id','phase','reason','stats'}:raise BridgeError('invalid_body')
                return 200,self.store.browser_heartbeat(token,body.get('browser_session_id'),body.get('phase'),body.get('reason'),body.get('stats'))
            if action=='browser_stop':
                if set(body)-{'browser_session_id','reason'}:raise BridgeError('invalid_body')
                return 200,self.store.browser_stop(body.get('browser_session_id'),body.get('reason','manual_stop'),token)
            if action == 'claim': return 200, self.store.claim(token, body.get('task_id'),body.get('execution_kind'),body.get('browser_session_id'),body.get('capabilities'))
            args = (token, body.get('task_id'), body.get('lease'))
            if action == 'heartbeat': value = self.store.heartbeat(*args,browser_session_id=body.get('browser_session_id'))
            elif action == 'reserve': value = self.store.reserve(*args, body.get('url'),browser_session_id=body.get('browser_session_id'))
            elif action == 'submit': value = self.store.submit(*args, body.get('candidates'),browser_session_id=body.get('browser_session_id'))
            elif action == 'finish': value = self.store.finish(*args, body.get('error'),body.get('retry_after'),body.get('stats'),browser_session_id=body.get('browser_session_id'))
            else: raise BridgeError('not_found', 404)
            return 200, value
        except BridgeError as exc:
            return exc.status, {'error': exc.code}
