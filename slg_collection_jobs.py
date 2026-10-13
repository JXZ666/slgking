"""Durable administrator acquisition queue; contains no network or subprocess work."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

LIMITS = {'dikgames': (500, 200, 1200), 'f95zone': (54, 200, 1200), 'dlsite': (300, 200, 3600)}
COUNTERS = ('new', 'updated', 'duplicate', 'failed', 'cover_ok', 'cover_missing', 'requests')
TERMINAL = frozenset(('completed', 'partial', 'cancelled', 'paused', 'interrupted', 'failed', 'publication_failed'))


def describe_reason(reason):
    text={
        'maintenance_guard_not_verified':'固定151补图任务完成结果尚未核验，手动采集暂未启用。',
        'worker_unavailable':'采集进程尚未就绪，请等待维护核验后再提交。',
        'maintenance_guard':'维护保护任务尚未完成，手动采集暂未启用。',
        'source_ledger_unavailable':'来源账本不可读取，已安全暂停。',
        'source_state_unavailable':'来源保护状态不可读取，已安全暂停。',
        'pause_active':'来源仍在等待期；到期后自动复核访问条件，通过后可重试。',
        'protocol_check_required':'来源等待期已结束，可提交任务；任务先检查访问规则，通过后才开始采集。',
        'manual_pause':'来源已由维护人员暂停，不会自动解除。',
        'network':'来源网络请求失败，按15分钟／1小时／6小时退避后自动复核。',
        'server_error':'来源服务器暂时故障，等待后自动复核，已有资料保留。',
        'robots_parse':'来源抓取规则未能可靠解析，等待复核，不会忽略规则继续抓取。',
        'verification_gate':'来源要求验证，已停止采集，不会绕过验证。',
        'probe_limit':'今日访问恢复检查已达到上限，次日继续复核。',
        'access_protection':'来源触发访问保护。需维护人员核查后解除原来源暂停，再重试本任务。',
        'http_403':'来源拒绝访问，已暂停。需维护人员核查，不能通过重试绕过访问保护。',
        'http_429':'来源限制请求，已暂停。需遵守来源等待要求，核查后再重试。',
        'verification':'来源要求人工验证，已暂停；不会自动绕过验证。',
        'consecutive_failures':'来源连续请求失败，已暂停；核查恢复情况后再重试。',
        'robots_unavailable':'来源抓取规则不可读取，已暂停。',
        'robots_disallowed':'来源抓取规则不允许该路径，已暂停。',
        'manual_daily_request_budget':'今日手动采集请求额度已用完；北京时间次日可重试，定时额度不受影响。',
        'manual_daily_new_budget':'今日手动新增额度已用完；北京时间次日可重试，定时额度不受影响。',
        'scheduled_priority':'已让出给定时采集，稍后自动继续排队。',
        'collector_busy':'采集或发布锁正被使用，稍后自动继续排队。',
        'cover_backoff':'封面仍在原来源重试等待期，等待到期后再重试。',
        'covers_pending':'作品事实已完成，仍有封面缺失；重试会优先补这些封面。',
        'publication_failed':'事实已保留，公开快照发布失败；重试只重做发布。',
        'worker_restart':'采集进程中断，已提交事实保留，请手动重试未完成部分。',
        'deadline':'本任务运行时间额度已用完，已提交事实保留，可重试未完成部分。',
        'deadline_or_request_budget':'本任务时间或请求额度已用完，已提交事实保留。',
        'storage_low':'可用空间不足5GiB，已停止；请先释放或扩容存储，再重试。',
        'item_failures':'部分作品采集失败，成功事实已保留，可重试失败部分。',
        'candidate_pool_exhausted':'当前允许且可访问的候选作品不足，未达到目标。',
    }
    if not reason:return None
    return text.get(reason,str(reason))


def read_job(path, job_id):
    from contextlib import closing
    path=Path(path)
    if not path.is_file():return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT document FROM collection_jobs WHERE id=?',(job_id,)).fetchone()
    return json.loads(row[0]) if row else None


def read_jobs(path, limit=25, offset=0):
    """List without creating a database or applying WAL/schema migrations."""
    from contextlib import closing
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Invalid pagination')
    path=Path(path)
    if not path.is_file():return dict(jobs=[],total=0)
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        total=db.execute('SELECT count(*) FROM collection_jobs').fetchone()[0]
        rows=db.execute("SELECT document FROM collection_jobs ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit,offset)).fetchall()
    return dict(total=total,jobs=[json.loads(row[0]) for row in rows])


class CollectionConflict(ValueError):
    pass


def canonical_target(source, value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError('Invalid target')
    value = value.strip()
    if source == 'dlsite' and re.fullmatch(r'RJ[0-9]{6,10}', value, re.I):
        return 'https://www.dlsite.com/maniax/work/=/product_id/' + value.upper() + '.html'
    if source == 'dikgames' and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value):
        value = 'https://dikgames.com/' + value + '/'
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.port or parsed.username or parsed.password
            or parsed.query or parsed.fragment or '%' in parsed.path or '..' in parsed.path):
        raise ValueError('Only canonical source HTTPS work URLs are accepted')
    if source == 'dikgames' and parsed.hostname == 'dikgames.com' and re.fullmatch(r'/[a-z0-9]+(?:-[a-z0-9]+)*/?', parsed.path):
        slug = parsed.path.strip('/')
        if slug in ('tag', 'category', 'page', 'wp-admin', 'wp-json', 'feed', 'robots', 'sitemap'):
            raise ValueError('Not a work URL')
        return 'https://dikgames.com/' + slug + '/'
    if source == 'f95zone' and parsed.hostname == 'f95zone.to.it' and re.fullmatch(r'/[a-z0-9][a-z0-9-]{4,}/?', parsed.path):
        if parsed.path.strip('/') in ('genres', 'search', 'page', 'robots', 'wp-admin', 'hot-games', 'category', 'author', 'privacy-policy', 'contact-us', 'dmca', 'about-us', 'terms-of-service'):
            raise ValueError('Not a work URL')
        return 'https://f95zone.to.it/' + parsed.path.strip('/') + '/'
    if source == 'dlsite' and parsed.hostname == 'www.dlsite.com' and re.fullmatch(r'/maniax/work/=/product_id/RJ[0-9]{6,10}\.html', parsed.path, re.I):
        external = re.search(r'RJ[0-9]{6,10}', parsed.path, re.I).group().upper()
        return 'https://www.dlsite.com/maniax/work/=/product_id/' + external + '.html'
    raise ValueError('Source and target do not match')


class CollectionJobs:
    def __init__(self, path, *, clock=time.time, source_status=None):
        self.path = str(path)
        self.clock = clock
        self.source_status = source_status or (lambda source: {})
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS collection_jobs(
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                specification TEXT NOT NULL, state TEXT NOT NULL, document TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS collection_budget(
                day TEXT,source TEXT,requests INTEGER NOT NULL DEFAULT 0,new INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(day,source));
              CREATE TABLE IF NOT EXISTS collection_receipts(
                job_id TEXT,target TEXT,document TEXT NOT NULL,PRIMARY KEY(job_id,target));
              CREATE TABLE IF NOT EXISTS collection_audit(
                at REAL NOT NULL,job_id TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS collection_intents(
                job_id TEXT,target TEXT,document TEXT NOT NULL,PRIMARY KEY(job_id,target));
              CREATE TABLE IF NOT EXISTS collection_cover_state(
                job_id TEXT,target TEXT,ok INTEGER NOT NULL,PRIMARY KEY(job_id,target));
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def day(self):
        return datetime.fromtimestamp(self.clock(), timezone(timedelta(hours=8))).date().isoformat()

    def _save(self, db, job):
        job['updated_at'] = self.clock()
        job['reason_text']=describe_reason(job.get('reason'))
        db.execute('UPDATE collection_jobs SET state=?,document=? WHERE id=?',
                   (job['status'], json.dumps(job, ensure_ascii=False), job['id']))

    def _read(self, db, job_id):
        row = db.execute('SELECT document FROM collection_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return json.loads(row[0])

    def create(self, source_id, mode='count', target_new=20, targets=None, request_id=None, actor='admin'):
        if source_id not in LIMITS or mode not in ('count', 'specified'):
            raise ValueError('Unsupported source or mode')
        if type(target_new) is not int or not 1 <= target_new <= 200:
            raise ValueError('target_new must be 1–200')
        if mode == 'specified':
            if not isinstance(targets, list) or not 1 <= len(targets) <= 50:
                raise ValueError('Specify 1–50 work URLs or IDs')
            targets = list(dict.fromkeys(canonical_target(source_id, v) for v in targets))
        elif targets:
            raise ValueError('count mode does not accept targets')
        else:
            targets = []
        request_id = request_id or uuid.uuid4().hex
        if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
            raise ValueError('request_id must be 32 lowercase hexadecimal characters')
        specification = json.dumps([source_id, mode, target_new, targets], ensure_ascii=False)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT specification,document FROM collection_jobs WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old[0] != specification:
                    raise CollectionConflict('request_id already used for a different task')
                return json.loads(old[1])
            status = self.source_status(source_id) or {}
            if status.get('paused') or status.get('state') == 'paused':
                raise CollectionConflict(str(status.get('reason_text') or status.get('reason') or status.get('reason_code') or 'Source paused'))
            now = self.clock()
            job = dict(id=uuid.uuid4().hex, source_id=source_id, mode=mode, target_new=target_new,
                       targets=targets, request_id=request_id, status='queued', cancel_requested=False,
                       counters=dict.fromkeys(COUNTERS, 0), reason=None, created_at=now, updated_at=now,
                       publication_pending=False, attempt=0)
            job['reason_text']=None
            db.execute('INSERT INTO collection_jobs VALUES(?,?,?,?,?)',
                       (job['id'], request_id, specification, 'queued', json.dumps(job, ensure_ascii=False)))
            db.execute('INSERT INTO collection_audit VALUES(?,?,?,?)', (now, job['id'], actor, 'create'))
            return job

    def get(self, job_id):
        with self._db() as db:
            try:
                return self._read(db, job_id)
            except KeyError:
                return None

    def list(self, limit=25, offset=0):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise ValueError('Invalid pagination')
        with self._db() as db:
            rows = db.execute('SELECT document FROM collection_jobs ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset))
            return dict(jobs=[json.loads(row[0]) for row in rows], total=db.execute('SELECT count(*) FROM collection_jobs').fetchone()[0])

    def cancel(self, job_id, actor='admin'):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._read(db, job_id)
            if job['status'] in ('queued', 'running'):
                job['cancel_requested'] = True
                if job['status'] == 'queued':
                    job['status'] = 'cancelled'
                self._save(db, job)
                db.execute('INSERT INTO collection_audit VALUES(?,?,?,?)', (self.clock(), job_id, actor, 'cancel'))
            return job

    def retry(self, job_id, actor='admin'):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._read(db, job_id)
            if job['status'] not in TERMINAL or job['status'] == 'completed':
                raise CollectionConflict('Task is not retryable')
            state = self.source_status(job['source_id']) or {}
            if (state.get('paused') or state.get('state') == 'paused') and not job['publication_pending']:
                raise CollectionConflict(str(state.get('reason_text') or state.get('reason') or 'Source paused'))
            publish_only = job['status'] == 'publication_failed'
            job.update(status='queued', cancel_requested=False, reason=None, resume_publish_only=publish_only)
            db.execute("DELETE FROM collection_receipts WHERE job_id=? AND json_extract(document,'$.failed')=1", (job_id,))
            self._save(db, job)
            db.execute('INSERT INTO collection_audit VALUES(?,?,?,?)', (self.clock(), job_id, actor, 'retry'))
            return job

    def recover(self):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            jobs = [json.loads(r[0]) for r in db.execute("SELECT document FROM collection_jobs WHERE state='running'")]
            for job in jobs:
                job.update(status='interrupted', reason='worker_restart')
                self._save(db, job)
            return len(jobs)

    def claim(self):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT document FROM collection_jobs WHERE state='queued' AND COALESCE(json_extract(document,'$.not_before'),0)<=? ORDER BY rowid LIMIT 1", (self.clock(),)).fetchone()
            if not row:
                return None
            job = json.loads(row[0]); job.update(status='running', attempt=job['attempt'] + 1, reason=None)
            self._save(db, job)
            return job

    def update(self, job_id, **values):
        allowed = {'status', 'reason', 'publication_pending', 'counters', 'resume_publish_only', 'not_before'}
        if set(values) - allowed:
            raise ValueError('Invalid worker update')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._read(db, job_id); job.update(values); self._save(db, job)
            return job

    def reserve_request(self, job_id):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._read(db, job_id)
            if job['cancel_requested']:
                raise CollectionConflict('cancelled')
            day, source = self.day(), job['source_id']
            row = db.execute('SELECT requests FROM collection_budget WHERE day=? AND source=?', (day, source)).fetchone()
            if row and row[0] >= LIMITS[source][0]:
                raise CollectionConflict('manual_daily_request_budget')
            db.execute('INSERT INTO collection_budget(day,source,requests) VALUES(?,?,1) ON CONFLICT(day,source) DO UPDATE SET requests=requests+1', (day, source))
            job['counters']['requests'] += 1
            self._save(db, job)

    def reserve_new(self, job_id):
        """Reserve before committing a new fact; conservative even on process death."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._read(db, job_id); day, source = self.day(), job['source_id']
            row = db.execute('SELECT new FROM collection_budget WHERE day=? AND source=?', (day, source)).fetchone()
            if row and row[0] >= LIMITS[source][1]:
                raise CollectionConflict('manual_daily_new_budget')
            db.execute('INSERT INTO collection_budget(day,source,new) VALUES(?,?,1) ON CONFLICT(day,source) DO UPDATE SET new=new+1', (day, source))

    def receipt(self, job_id, target, result):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM collection_receipts WHERE job_id=? AND target=?', (job_id, target)).fetchone():
                return False
            job = self._read(db, job_id)
            base_target=target.split('#',1)[0] if isinstance(target,str) else target
            has_cover_state=db.execute('SELECT 1 FROM collection_cover_state WHERE job_id=? AND target=?',(job_id,base_target)).fetchone()
            for name in ('new', 'updated', 'duplicate', 'failed', 'cover_ok', 'cover_missing'):
                if name.startswith('cover_') and has_cover_state:continue
                job['counters'][name] += int(result.get(name, 0))
            job['publication_pending'] |= bool(result.get('new') or result.get('updated') or result.get('cover_changed'))
            db.execute('INSERT INTO collection_receipts VALUES(?,?,?)', (job_id, target, json.dumps(result)))
            db.execute('DELETE FROM collection_intents WHERE job_id=? AND target=?', (job_id, target))
            self._save(db, job)
            return True

    def begin_intent(self, job_id, target, proof, result, *, reserve_new=False):
        """Durable pre-commit proof and quota receipt, not a successful result.

        A replay of an uncommitted same-day intent reuses its reservation. Native
        storage must be queried before reconstructing a successful receipt.
        """
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            job=self._read(db,job_id)
            old=db.execute('SELECT document FROM collection_intents WHERE job_id=? AND target=?',(job_id,target)).fetchone()
            old=json.loads(old[0]) if old else {}
            day,source=self.day(),job['source_id']
            if reserve_new and not (old.get('reserved_new') and old.get('day')==day):
                used=db.execute('SELECT new FROM collection_budget WHERE day=? AND source=?',(day,source)).fetchone()
                if used and used[0]>=LIMITS[source][1]:raise CollectionConflict('manual_daily_new_budget')
                db.execute('INSERT INTO collection_budget(day,source,new) VALUES(?,?,1) ON CONFLICT(day,source) DO UPDATE SET new=new+1',(day,source))
            intent=dict(proof=proof,result=result,day=day,reserved_new=reserve_new or bool(old.get('reserved_new')),created_at=old.get('created_at',self.clock()))
            db.execute('INSERT OR REPLACE INTO collection_intents VALUES(?,?,?)',(job_id,target,json.dumps(intent,ensure_ascii=False)))
            job['publication_pending']=True;self._save(db,job)

    def intents(self,job_id):
        with self._db() as db:
            return [(row[0],json.loads(row[1])) for row in db.execute('SELECT target,document FROM collection_intents WHERE job_id=?',(job_id,))]

    def cover_result(self,job_id,target,ok,*,changed=False):
        """Current per-work cover outcome, not an accumulating retry event count."""
        target=target.split('#',1)[0]
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE');job=self._read(db,job_id)
            row=db.execute('SELECT ok FROM collection_cover_state WHERE job_id=? AND target=?',(job_id,target)).fetchone()
            if row is None:
                job['counters']['cover_ok' if ok else 'cover_missing']+=1
            elif bool(row[0])!=bool(ok):
                job['counters']['cover_ok' if row[0] else 'cover_missing']-=1
                job['counters']['cover_ok' if ok else 'cover_missing']+=1
            db.execute('INSERT OR REPLACE INTO collection_cover_state VALUES(?,?,?)',(job_id,target,int(bool(ok))))
            job['publication_pending']|=changed;self._save(db,job)

    def missing_cover_targets(self,job_id):
        with self._db() as db:
            return {row[0] for row in db.execute('SELECT target FROM collection_cover_state WHERE job_id=? AND ok=0',(job_id,))}

    def done_targets(self, job_id):
        with self._db() as db:
            return {row[0] for row in db.execute('SELECT target FROM collection_receipts WHERE job_id=? AND json_extract(document,\'$.failed\') IS NOT 1', (job_id,))}
