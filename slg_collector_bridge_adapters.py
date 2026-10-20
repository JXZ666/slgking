"""Production adapters for existing DLsite/append ledgers; no network work.

The host passes the freshly reviewed worker module, existing CollectionJobs and
the SAME lock path used by cloud workers. Construction never changes schedules.
"""
import base64
from contextlib import contextmanager
import hashlib
import html
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from slg_collector_bridge import BridgeError, validate_candidate


class ProductionAdapters:
    def __init__(self, *, worker_module, ledger_directory, jobs, mapping_path,
                 lock_path, manual_pause, output, clock=time.time):
        self.worker=worker_module
        self.directory=Path(ledger_directory)
        self.jobs=jobs
        self.mapping_path=Path(mapping_path)
        self.lock_path=Path(lock_path)
        self.manual_pause=manual_pause
        self.output=Path(output)
        self.clock=clock
        self._lock=threading.RLock()
        self._leases={}
        with self.mapping() as c:
            c.execute('CREATE TABLE IF NOT EXISTS bridge_manual_jobs(task TEXT PRIMARY KEY,job TEXT NOT NULL UNIQUE)')

    @contextmanager
    def mapping(self):
        self.mapping_path.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(self.mapping_path,timeout=10)
        try:
            yield c; c.commit()
        except BaseException:
            c.rollback(); raise
        finally: c.close()

    def pause_gate(self):
        # Host must distinguish manual total pause from direct-route 403.
        return bool(self.manual_pause())

    def bind_manual(self, task_id, job_id):
        job=self.jobs.get(job_id)
        if not job or job.get('source_id')!='dlsite' or job.get('cancel_requested'):
            raise BridgeError('manual_job_mapping_invalid')
        with self.mapping() as c:
            c.execute('INSERT INTO bridge_manual_jobs(task,job) VALUES(?,?)',(task_id,job_id))
        # Existing worker claims only queued jobs. Hold the legacy job while the
        # explicitly paired device owns execution of this separate typed route.
        self.jobs.update(job_id,status='running')

    def manual_job(self, task_id):
        with self.mapping() as c:
            row=c.execute('SELECT job FROM bridge_manual_jobs WHERE task=?',(task_id,)).fetchone()
        if not row: raise BridgeError('manual_job_mapping_missing',409)
        job=self.jobs.get(row[0])
        if not job or job.get('source_id')!='dlsite' or job.get('cancel_requested'):
            raise BridgeError('manual_job_cancelled',409)
        return job

    def hold(self, task_id):
        """Process-held OS lock, shared with legacy workers, not just a DB flag."""
        with self._lock:
            self.sweep()
            if task_id in self._leases:
                ctx,_=self._leases[task_id]
                self._leases[task_id]=(ctx,self.clock()+120)
                return True
            if self._leases: return False
            ctx=self.worker.worker_lock(self.directory,lock_path=self.lock_path)
            try: ctx.__enter__()
            except (OSError,RuntimeError): return False
            self._leases[task_id]=(ctx,self.clock()+120)
            return True

    def sweep(self):
        with self._lock:
            for task,(_,expiry) in list(self._leases.items()):
                if expiry<=self.clock(): self.release(task)

    def heartbeat(self, task_id):
        with self._lock:
            if task_id in self._leases:
                ctx,_=self._leases[task_id]
                self._leases[task_id]=(ctx,self.clock()+120)

    def release(self, task_id):
        with self._lock:
            old=self._leases.pop(task_id,None)
            if old: old[0].__exit__(None,None,None)

    def check_ids(self, ids):
        """Read-only ledger dedupe: works.item IS NOT NULL means collected.

        Missing ledger is an empty result (nothing collected yet); unreadable
        storage raises so the host fails closed instead of re-collecting.
        """
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise BridgeError('invalid_ids')
        wanted = []
        for value in ids:
            if not isinstance(value, str):
                raise BridgeError('invalid_ids')
            external = value.strip().upper()
            if not re.fullmatch(r'(?:RJ|VJ)[0-9]{6,10}', external):
                raise BridgeError('invalid_ids')
            if external not in wanted:
                wanted.append(external)
        path = self.directory / 'ledger.db'
        if not path.is_file():
            return []
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            marks = ','.join('?' for _ in wanted)
            rows = conn.execute('SELECT external_id FROM works WHERE item IS NOT NULL'
                                ' AND external_id IN (%s)' % marks, wanted).fetchall()
        finally:
            conn.close()
        found = {row[0] for row in rows}
        return [external for external in wanted if external in found]

    def budget_gate(self, *, mode, url, task_id):
        if self.pause_gate(): raise BridgeError('manual_paused',409)
        if not self.hold(task_id): return {'allowed':False,'wait_seconds':30}
        if mode=='manual':
            job=self.manual_job(task_id)
            # Exact existing quota key and durable job counters; one reservation.
            self.jobs.reserve_request(job['id'])
        elif mode in ('daily','diagnostic','auto','specified'):
            ledger=self.worker.Ledger(self.directory,clock=self.clock,max_requests=300)
            try:
                day=ledger._day()
                c=ledger.conn; c.execute('BEGIN IMMEDIATE')
                used=c.execute('SELECT used FROM budget WHERE day=?',(day,)).fetchone()
                if used and used[0]>=ledger.max_requests:
                    c.rollback(); raise BridgeError('budget_exhausted',409)
                # Same tables as Ledger.reserve_request, deliberately without
                # its event('running') side effect: direct pause stays untouched.
                c.execute('INSERT INTO budget VALUES(?,1) ON CONFLICT(day) DO UPDATE SET used=used+1',(day,))
                c.execute("INSERT INTO request_lanes VALUES(?,'bridge',1) ON CONFLICT(day,lane) DO UPDATE SET used=used+1",(day,))
                c.execute("INSERT INTO request_groups VALUES(?,'update',1) ON CONFLICT(day,group_name) DO UPDATE SET used=used+1",(day,))
                c.commit()
            finally: ledger.close()
        else: raise BridgeError('invalid_mode')
        return {'allowed':True}

    def _fact(self, value):
        """Re-run production fact eligibility on minimal safe evidence markup.

        Raw authenticated HTML is never uploaded: only heading/table text and
        official canonical link are reconstructed. Unknown fields remain unknown.
        RJ and VJ both run the reviewed parser; the canonical link keeps the
        original /maniax/ or /pro/ section so the parser cannot relabel a work.
        """
        value=validate_candidate(value)
        esc=html.escape
        aliases={'作品类型':'作品形式','作品類型':'作品形式'}
        rows=''.join('<tr><th>'+esc(aliases.get(k,k))+'</th><td>'+esc(v)+'</td></tr>' for k,v in value['outline'].items())
        document=('<link rel="canonical" href="'+esc(value['source_url'],quote=True)+'">'
                  '<h1 id="work_name">'+esc(value['title'])+'</h1>'
                  '<span class="maker_name">'+esc(value.get('developer') or '')+'</span>'
                  '<table id="work_outline">'+rows+'</table>')
        fact=self.worker.slg_dlsite.parse_product(document,value['source_url'])
        if fact is None: raise BridgeError('production_fact_eligibility_missing',409)
        return value,fact

    def publish_task(self, store, task_id):
        """Explicit admin-reviewed import + existing atomic publisher, zero HTTP.

        Publication failures leave staging pending; a retry does not recollect.
        Existing facts/covers are adopted before mutation to prevent reductions.
        """
        with store.db() as c:
            task=c.execute('SELECT mode,state,execution_kind FROM bridge_tasks WHERE id=?',(task_id,)).fetchone()
            reviewable=('awaiting_review','failed','publication_failed')
            if task and task['execution_kind']=='edge_session':reviewable+=('interrupted',)
            if not task or task['state'] not in reviewable:
                raise BridgeError('task_not_ready_for_review',409)
            rows=c.execute("SELECT payload FROM bridge_staging WHERE task=? AND state='pending_review' ORDER BY work_id",(task_id,)).fetchall()
        if not rows: raise BridgeError('no_pending_candidates',409)
        prepared=[self._fact(json.loads(row[0])) for row in rows]
        # No live device may still own this task by the time review starts.
        self.release(task_id)
        with self.worker.worker_lock(self.directory,lock_path=self.lock_path):
            ledger=self.worker.Ledger(self.directory,clock=self.clock,max_requests=300)
            try:
                ledger.adopt_publication(self.output)
                counters={'new':0,'updated':0,'duplicate':0,'cover_ok':0,'cover_missing':0}
                manual=self.manual_job(task_id) if task['mode'] in ('manual','auto','specified') else None
                for value,fact in prepared:
                    external=value['external_id']
                    old=ledger.conn.execute('SELECT item,cover FROM works WHERE external_id=?',(external,)).fetchone()
                    # Per-work intent means retries after publication failure do
                    # not reserve the manual new-work allowance a second time.
                    if manual and not (old and old['item']):
                        self.jobs.begin_intent(manual['id'],value['source_url'],
                             proof={'source':'dlsite_local_bridge','sha256':value['page_sha256']},
                             result={'external_id':external},reserve_new=True)
                    kind='new' if not old or not old['item'] else ('duplicate' if json.loads(old['item'])==fact else 'updated')
                    with store.db() as c:
                        receipt=c.execute('SELECT kind FROM bridge_import_receipts WHERE task=? AND work_id=?',(task_id,external)).fetchone()
                        if receipt: kind=receipt[0]
                        else:c.execute('INSERT INTO bridge_import_receipts VALUES(?,?,?)',(task_id,external,kind))
                    counters[kind]+=1
                    now=self.clock()
                    ledger.conn.execute('INSERT OR IGNORE INTO works(external_id,url,discovered_at) VALUES(?,?,?)',(external,fact['url'],now))
                    # None-valued optional facts cannot erase already verified fields.
                    if old and old['item']:
                        prior=json.loads(old['item'])
                        fact={k:(prior.get(k) if v is None or v in ('',[],{}) else v) for k,v in fact.items()}
                    ledger.conn.execute('UPDATE works SET item=?,checked_at=?,attempted_at=?,next_fact_due=?,image_url=COALESCE(?,image_url) WHERE external_id=?',
                        (json.dumps(fact,ensure_ascii=False),now,now,now+self.worker.DETAIL_TTL,value.get('image_url'),external))
                    old_cover=json.loads(old['cover']) if old and old['cover'] else {}
                    image=value.get('image_base64')
                    if image:
                        # Optional fallback path (mainland network): the device may
                        # still upload bounded legacy cover bytes; keep the legacy
                        # cover file and cover index exactly as before.
                        raw=self.worker.normalise_image(base64.b64decode(image,validate=True))
                        digest=hashlib.sha256(raw).hexdigest()
                        filename=f'dlsite-{external}-{digest}.jpg'
                        self.worker.atomic_bytes(ledger.directory/'covers'/filename,raw)
                        cover={'filename':filename,'sha256':digest,'bytes':len(raw)}
                        ledger.conn.execute('UPDATE works SET cover=?,cover_checked=?,next_cover_due=? WHERE external_id=?',
                            (json.dumps(cover),now,now+self.worker.COVER_TTL,external))
                        counters['cover_ok']+=int(old_cover.get('sha256')!=digest)
                    elif value.get('image_url'):
                        # VNDB-style official reference: metadata only, no bytes.
                        # worker.publish() writes it into the official cover index.
                        counters['cover_ok']+=1
                    else:
                        counters['cover_missing']+=int(not old_cover)
                ledger.conn.commit()
                manifest=self.worker.publish(ledger,self.output)
                if manual:
                    previous=self.jobs.get(manual['id'])['counters']
                    updated={**previous,**{k:v for k,v in counters.items() if k in previous}}
                    self.jobs.update(manual['id'],status='completed',counters=updated,publication_pending=False)
                with store.db() as c:
                    c.execute("UPDATE bridge_staging SET state='published' WHERE task=? AND state='pending_review'",(task_id,))
                    c.execute("UPDATE bridge_tasks SET state='published',error=NULL WHERE id=?",(task_id,))
                    c.execute('INSERT OR REPLACE INTO bridge_results VALUES(?,?)',(task_id,json.dumps(counters)))
                return {'manifest':manifest,'counters':counters,'direct_route_pause_unchanged':True}
            except Exception:
                ledger.conn.rollback()
                with store.db() as c:
                    c.execute("UPDATE bridge_tasks SET state='publication_failed',error='publication_failed' WHERE id=?",(task_id,))
                raise
            finally: ledger.close()
