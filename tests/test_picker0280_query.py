"""Read-only picker query, cancellation, and latest-only worker regression."""
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import slg_db
from slg_game_picker_query import PickerQuery, LatestLookup, LookupCancelled


def seed(path, count=80):
    conn = slg_db.connect(str(path))
    conn.executemany("INSERT INTO games(id,slug,url,title,developer,origin,first_seen) VALUES(?,?,?,?,?,'site','2026-10-15')",
                     ((i, 'game-%d'%i, 'https://example.invalid/%d'%i, 'Game %05d'%i, 'Studio') for i in range(1,count+1)))
    conn.commit()
    return conn


class PickerQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'catalogue.db'
        self.writer = seed(self.path)
        self.query = PickerQuery(self.path)
    def tearDown(self):
        self.query.close()
        self.writer.close()
        self.tmp.cleanup()

    def test_paged_sql_is_bounded_deterministic_and_read_only(self):
        trace = []
        self.query.conn.set_trace_callback(trace.append)
        first, second, last = self.query.search(), self.query.search(page=2), self.query.search(page=3)
        self.assertEqual([len(x['rows']) for x in (first,second,last)], [30,30,20])
        self.assertTrue(first['has_next'])
        self.assertFalse(last['has_next'])
        self.assertFalse({r['id'] for r in first['rows']} & {r['id'] for r in second['rows']})
        select = [s for s in trace if s.startswith('SELECT g.id')]
        self.assertTrue(all('LIMIT 31 OFFSET ' in s for s in select))
        self.assertFalse(any('COUNT(' in s or 'cover' in s or 'rating' in s for s in select))
        with self.assertRaises(sqlite3.OperationalError):
            self.query.conn.execute("UPDATE games SET title='changed'")

    def test_hidden_redirect_candidates_dedup_and_fresh_selection(self):
        self.writer.execute("INSERT INTO hidden_games VALUES(1,'now')")
        # Redirects schema is intentionally exercised through the real helper.
        self.writer.execute("INSERT INTO catalog_redirects(old_slug,canonical_slug,active) VALUES('game-2','game-3',1)")
        self.writer.commit()
        result = self.query.search(candidates=[{'id':75},75,1,2])
        ids = [r['id'] for r in result['rows']]
        self.assertEqual(ids[0],75)
        self.assertNotIn(1,ids); self.assertNotIn(2,ids)
        self.assertIsNone(self.query.selected(1)); self.assertIsNone(self.query.selected(2))
        self.writer.execute("DELETE FROM games WHERE id=75"); self.writer.commit()
        self.assertIsNone(self.query.selected(75))

    def test_alias_rj_developer_and_literal_sql_characters(self):
        self.writer.execute("INSERT INTO game_aliases(game_id,alias) VALUES(4,'多娜多娜 一起干坏事吧')")
        self.writer.execute("UPDATE games SET slug='dlsite-rj012345',url='https://www.dlsite.com/work/RJ012345.html' WHERE id=5")
        self.writer.execute("UPDATE games SET title='100%_literal' WHERE id=6")
        self.writer.commit()
        self.assertEqual(self.query.search('多娜多娜')['rows'][0]['id'],4)
        self.assertEqual(self.query.search('RJ012345')['rows'][0]['id'],5)
        self.assertEqual(len(self.query.search('Studio')['rows']),30)
        self.assertEqual([r['id'] for r in self.query.search('%_')['rows']], [6])
        self.assertFalse(self.query.search("' OR 1=1 --")['rows'])

    def test_translation_cache_invalidates_for_other_connection_changes(self):
        self.writer.execute("INSERT INTO manual_translations(kind,ref,lang,text,updated_at) VALUES('title','7','zh','正确译名','now')")
        self.writer.commit()
        self.assertEqual(self.query.search('正确译名')['rows'][0]['id'],7)
        self.assertEqual(self.query.translation_builds,1)
        self.query.search('正确'); self.assertEqual(self.query.translation_builds,1)
        self.writer.execute("UPDATE manual_translations SET text='新译名' WHERE ref='7'")
        self.writer.commit()
        self.assertFalse(self.query.search('正确译名')['rows'])
        self.assertEqual(self.query.search('新译名')['rows'][0]['id'],7)
        self.assertEqual(self.query.translation_builds,2)

    def test_machine_translation_source_hash_and_manual_precedence(self):
        row = self.writer.execute('SELECT title FROM games WHERE id=8').fetchone()
        self.writer.execute("INSERT INTO translations(kind,ref,lang,engine,text,src_hash,updated_at) VALUES('title','8','zh','test','机器译名',?,'now')", (slg_db.src_hash(row['title']),))
        self.writer.commit()
        self.assertEqual(self.query.search('机器译名')['rows'][0]['id'],8)
        self.writer.execute("UPDATE games SET title='New source' WHERE id=8"); self.writer.commit()
        self.assertFalse(self.query.search('机器译名')['rows'])
        self.writer.execute("INSERT INTO manual_translations(kind,ref,lang,text,updated_at) VALUES('title','8','zh','人工优先','now')")
        self.writer.commit()
        self.assertEqual(self.query.search('人工优先')['rows'][0]['id'],8)
        self.assertFalse(self.query.search('机器译名')['rows'])

    def test_sql_running_cancellation_is_translated_and_connection_recovers(self):
        class TripStop:
            calls = 0
            def is_set(self):
                self.calls += 1
                return self.calls > 1
        stop = TripStop()
        with self.assertRaises(LookupCancelled):
            self.query.search(stop=stop)
        self.assertGreaterEqual(stop.calls,2)
        self.assertEqual(len(self.query.search()['rows']),30)

    def test_cancel_checks_translation_python_loops_and_sql_progress(self):
        stop = threading.Event(); stop.set()
        with self.assertRaises(LookupCancelled): self.query.search('Game',stop=stop)
        with self.assertRaises(LookupCancelled): self.query.selected(1,stop=stop)
        self.assertTrue(self.query.search('Game')['rows'])


class LatestWorkerTests(unittest.TestCase):
    def test_only_running_and_latest_pending_task_survive(self):
        entered,release = threading.Event(),threading.Event()
        calls=[]
        class FakeQuery:
            def __init__(self,path): pass
            def search(self,text='',stop=None,**kw):
                calls.append(text)
                if text=='first':
                    entered.set(); release.wait(2)
                if stop.is_set(): raise LookupCancelled()
                return text
            def interrupt(self): pass
            def close(self): pass
        with patch('slg_game_picker_query.PickerQuery',FakeQuery):
            worker=LatestLookup('unused')
            try:
                worker.submit(text='first'); self.assertTrue(entered.wait(2))
                for n in range(100): last=worker.submit(text='pending%d'%n)
                self.assertEqual(worker._pending[3]['text'],'pending99')
                release.set()
                item=worker.results.get(timeout=3)
                self.assertEqual(item,(last,'search','pending99',None))
                self.assertEqual(calls,['first','pending99'])
            finally:
                release.set(); worker.close(); worker.thread.join(2)
            self.assertFalse(worker.thread.is_alive())

    def test_connection_failure_is_reported_for_actual_request_and_retryable(self):
        worker=LatestLookup('Z:/missing-do-not-create/game.db')
        try:
            seq=worker.submit(text='test')
            result=worker.results.get(timeout=3)
            self.assertEqual(result[0],seq); self.assertTrue(result[3])
            seq=worker.submit(text='retry')
            self.assertEqual(worker.results.get(timeout=3)[0],seq)
        finally:
            worker.close(); worker.thread.join(2)

    def test_cancel_and_close_drops_pending_result(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'games.db'; conn=seed(path,1000);conn.close()
            worker=LatestLookup(path)
            worker.submit(text='Game');worker.cancel();worker.close();worker.thread.join(2)
            self.assertFalse(worker.thread.is_alive())

if __name__=='__main__': unittest.main()
