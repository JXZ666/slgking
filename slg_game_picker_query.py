"""Read-only paged lookup and a latest-request-only SQLite worker.

No UI, scoring, cover access, schema migration, or database writes occur here.
"""
from pathlib import Path
import json
import queue
import sqlite3
import threading

PAGE_SIZE = 30


class LookupCancelled(Exception):
    pass


def _check(stop):
    if stop is not None and stop.is_set():
        raise LookupCancelled()


class PickerQuery:
    """One worker-owned connection; data_version invalidates translated names."""
    def __init__(self, path):
        self.conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True,
                                    timeout=0.2, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA query_only=ON')
        self._version = None
        self._translations = ()
        self.translation_builds = 0
        self.tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def close(self):
        self.conn.close()

    def interrupt(self):
        self.conn.interrupt()

    def _translation_index(self, stop):
        import slg_db
        version = self.conn.execute('PRAGMA data_version').fetchone()[0]
        if version == self._version:
            return self._translations
        current = {}
        for row in self.conn.execute('SELECT id,title FROM games'):
            _check(stop)
            current[row['id']] = slg_db.src_hash(row['title'])
        translated = {}
        if 'translations' in self.tables:
            for row in self.conn.execute("SELECT ref,text,src_hash FROM translations WHERE kind='title' AND lang=?", (slg_db.TAG_LANG,)):
                _check(stop)
                try:
                    gid = int(row['ref'])
                except (ValueError, TypeError):
                    continue
                if current.get(gid) == row['src_hash']:
                    translated[gid] = str(row['text'] or '').casefold()
        if 'manual_translations' in self.tables:
            for row in self.conn.execute("SELECT ref,text FROM manual_translations WHERE kind='title' AND lang=?", (slg_db.TAG_LANG,)):
                _check(stop)
                try:
                    gid = int(row['ref'])
                except (ValueError, TypeError):
                    continue
                if gid in current:
                    translated[gid] = str(row['text'] or '').casefold()
        _check(stop)
        self._translations = tuple(translated.items())
        self._version = version
        self.translation_builds += 1
        return self._translations

    def _visibility(self):
        clauses = []
        if 'hidden_games' in self.tables:
            clauses.append('NOT EXISTS(SELECT 1 FROM hidden_games h WHERE h.game_id=g.id)')
        if 'catalog_redirects' in self.tables:
            clauses.append('NOT EXISTS(SELECT 1 FROM catalog_redirects c WHERE c.old_slug=g.slug AND c.active=1)')
        return clauses or ['1']

    @staticmethod
    def candidate_ids(candidates):
        found = []
        for item in candidates or ():
            if isinstance(item, dict):
                item = item.get('id', item.get('game_id'))
            if type(item) is int and item > 0 and item not in found:
                found.append(item)
        return found[:80]

    def search(self, text='', page=1, candidates=(), stop=None):
        _check(stop)
        page = max(1, int(page))
        text = str(text or '').strip()[:500]
        self.conn.set_progress_handler(lambda: int(stop is not None and stop.is_set()), 500)
        try:
            where = self._visibility()
            params = []
            if text:
                ids = []
                needle = text.casefold()
                for gid, value in self._translation_index(stop):
                    _check(stop)
                    if needle in value:
                        ids.append(gid)
                # Treat user '%'/'_' literally, like translated-name lookup.
                pattern = '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                parts = ["g.title LIKE ? ESCAPE '\\'", "g.slug LIKE ? ESCAPE '\\'",
                         "g.developer LIKE ? ESCAPE '\\'", "g.url LIKE ? ESCAPE '\\'"]
                params.extend([pattern] * 4)
                if 'game_aliases' in self.tables:
                    parts.append("EXISTS(SELECT 1 FROM game_aliases a WHERE a.game_id=g.id AND a.alias LIKE ? ESCAPE '\\')")
                    params.append(pattern)
                if ids:
                    parts.append('g.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))')
                    params.append(json.dumps(ids))
                if 'game_sources' in self.tables:
                    parts.append("EXISTS(SELECT 1 FROM game_sources m WHERE m.game_id=g.id AND m.external_id LIKE ? ESCAPE '\\')")
                    params.append(pattern)
                where.append('(' + ' OR '.join(parts) + ')')
            candidates = self.candidate_ids(candidates)
            order = 'g.title COLLATE NOCASE,g.id'
            if candidates:
                order = '(CASE WHEN g.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) THEN 0 ELSE 1 END),' + order
            common = ' FROM games g WHERE ' + ' AND '.join(where)
            # LIMIT+1 detects next page without synchronous COUNT/whole result fetch.
            sql = 'SELECT g.id,g.slug,g.title,g.developer,g.origin,g.engine' + common + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?'
            values = params + ([json.dumps(candidates)] if candidates else []) + [PAGE_SIZE + 1, (page - 1) * PAGE_SIZE]
            rows = [dict(row) for row in self.conn.execute(sql, values)]
            _check(stop)
            return dict(rows=rows[:PAGE_SIZE], page=page, has_next=len(rows) > PAGE_SIZE,
                        has_previous=page > 1, page_size=PAGE_SIZE)
        except sqlite3.OperationalError:
            _check(stop)
            raise
        finally:
            self.conn.set_progress_handler(None, 0)

    def selected(self, game_id, stop=None):
        _check(stop)
        row = self.conn.execute('SELECT g.* FROM games g WHERE g.id=? AND ' + ' AND '.join(self._visibility()), (game_id,)).fetchone()
        _check(stop)
        return dict(row) if row else None


class LatestLookup:
    """One active and one replaceable pending task; results never touch Tk."""
    def __init__(self, path):
        self.path = path
        self.results = queue.Queue(maxsize=2)
        self._condition = threading.Condition()
        self._pending = None
        self._running = None
        self._query = None
        self._closed = False
        self._sequence = 0
        self.thread = threading.Thread(target=self._run, name='game-picker-lookup', daemon=True)
        self.thread.start()

    def submit(self, kind='search', **kwargs):
        with self._condition:
            if self._closed:
                return None
            self._sequence += 1
            stop = threading.Event()
            if self._pending:
                self._pending[2].set()
            if self._running:
                self._running.set()
                if self._query:
                    self._query.interrupt()
            self._pending = (self._sequence, kind, stop, kwargs)
            self._condition.notify()
            return self._sequence

    def cancel(self):
        with self._condition:
            self._sequence += 1
            if self._pending:
                self._pending[2].set()
            self._pending = None
            if self._running:
                self._running.set()
            if self._query:
                self._query.interrupt()

    def close(self):
        with self._condition:
            self._closed = True
            if self._pending:
                self._pending[2].set()
            self._pending = None
            if self._running:
                self._running.set()
            if self._query:
                self._query.interrupt()
            self._condition.notify()

    def _run(self):
        query = None
        try:
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    seq, kind, stop, kwargs = self._pending
                    self._pending = None
                    self._running = stop
                try:
                    if query is None:
                        query = PickerQuery(self.path)
                        with self._condition:
                            self._query = query
                    result = (query.selected(stop=stop, **kwargs) if kind == 'selected'
                              else query.search(stop=stop, **kwargs))
                    item = (seq, kind, result, None)
                except LookupCancelled:
                    continue
                except Exception as exc:
                    if stop.is_set():
                        continue
                    item = (seq, kind, None, str(exc))
                with self._condition:
                    if self._closed or stop.is_set() or seq != self._sequence:
                        continue
                    while not self.results.empty():
                        try:
                            self.results.get_nowait()
                        except queue.Empty:
                            break
                    self.results.put_nowait(item)
        except Exception as exc:
            # Connection errors are delivered as the latest task error as well.
            with self._condition:
                if not self._closed:
                    self.results.put_nowait((self._sequence, 'error', None, str(exc)))
        finally:
            with self._condition:
                self._query = None
            if query:
                query.close()
