"""Offline stage/transport/atomicity regressions for the 0.27.7 client."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

import slg_db
import slg_sync_server as sync
from slg_sync_progress import StageProgress, SyncCancelled
import test_dlsite_online as dlsite_fixture


class Response(io.BytesIO):
    def __init__(self, raw, length=None):
        super().__init__(raw)
        self.headers = {} if length is None else {'Content-Length': str(length)}
        self.reads = 0

    def read(self, amount=-1):
        self.reads += 1
        assert amount == 65536, 'asset body must be read in bounded chunks'
        return super().read(amount)


class TransportTests(unittest.TestCase):
    def fetch(self, raw, length=None, stop=None, max_bytes=None, callback=None):
        events = []
        def observe(event):
            events.append(event)
            if callback:
                callback(event)
        ticks = iter(range(10000))
        reporter = StageProgress(observe, 'western_slg', stop, clock=lambda: next(ticks))
        reporter.emit('download')
        response = Response(raw, length)
        with patch.object(sync.slg_scrape._DIRECT_OPENER, 'open', return_value=response):
            result = sync._server_http_get(sync.SERVER_BASE + '/asset', progress=reporter,
                                           max_bytes=max_bytes)
        return result, events, response.reads

    def test_chunked_known_and_unknown_length(self):
        for total in (None, 200000):
            with self.subTest(total=total):
                raw, events, reads = self.fetch(b'x' * 200000, total)
                self.assertEqual(len(raw), 200000)
                self.assertGreater(reads, 2)
                self.assertEqual(events[-1]['done'], 200000)
                self.assertEqual(events[-1]['total'], total)
                self.assertEqual(events[-1]['status'], 'completed')
                self.assertEqual(events[-1]['unit'], 'bytes')

    def test_cancel_during_body_stops_without_trying_other_routes(self):
        stopped = [False]
        def observe(event):
            if event['done']:
                stopped[0] = True
        with patch.object(sync.slg_server_transport.ORIGIN_OPENER, 'open') as origin:
            with self.assertRaises(SyncCancelled):
                self.fetch(b'x' * 200000, stop=lambda: stopped[0], callback=observe)
            origin.assert_not_called()

    def test_size_and_length_reject_before_merge(self):
        for raw, length, limit in ((b'x' * 10, 10, 5), (b'x' * 10, None, 5), (b'x', 2, 10)):
            with self.subTest(length=length, limit=limit):
                with patch.object(sync.urllib.request, 'getproxies', return_value={}):
                    with patch.object(sync.slg_server_transport.ORIGIN_OPENER, 'open',
                                      return_value=Response(raw, length)):
                        with self.assertRaises(ConnectionError):
                            self.fetch(raw, length, max_bytes=limit)

    def test_http_redirect_keeps_streamed_progress(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/payload')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '7')
                    self.end_headers()
                    self.wfile.write(b'payload')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            events = []
            progress = StageProgress(events.append, 'western_slg')
            progress.emit('download')
            raw = sync._server_http_get('http://127.0.0.1:%d/redirect' % server.server_port,
                                       progress=progress)
            self.assertEqual(raw, b'payload')
            self.assertEqual((events[-1]['done'], events[-1]['total']), (7, 7))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_callback_failure_and_legacy_bytes_transport(self):
        def broken(_event):
            raise RuntimeError('observer failed')
        raw, _, _ = self.fetch(b'ok', 2, callback=broken)
        self.assertEqual(raw, b'ok')
        with patch.object(sync.slg_scrape, 'http_get', return_value=b'legacy') as get:
            self.assertEqual(sync._server_http_get(sync.SERVER_BASE), b'legacy')
            get.assert_called_once_with(sync.SERVER_BASE, timeout=30, direct=True)

    def test_throttle_always_delivers_terminal(self):
        events = []
        progress = StageProgress(events.append, 'dlsite', clock=lambda: 1)
        for done in range(100):
            progress.emit('download', done, 1000, 'bytes')
        progress.emit('download', 100, 1000, 'bytes', 'cancelled')
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]['status'], 'cancelled')


class WesternTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.conn = slg_db.connect(str(root / 'personal.db'))
        self.addCleanup(self.conn.close)
        source = slg_db.connect(str(root / 'source.db'))
        for slug in ('keep', 'new'):
            slg_db.upsert_game(source, slug, 'https://dikgames.com/' + slug + '/', 'Updated ' + slug)
        source.commit()
        source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        source.execute('PRAGMA journal_mode=DELETE')
        source.close()
        self.raw = (root / 'source.db').read_bytes()
        self.manifest = {'catalog_id': 'western_slg', 'lastmod': '2026-10-15', 'count': 2,
                         'db_sha256': hashlib.sha256(self.raw).hexdigest()}
        self.events = []

    def pull(self, stop=None, body=None, callback=None):
        def transport(url, timeout=30, **kwargs):
            progress = kwargs.get('progress')
            if progress:
                progress.check_stop()
                progress.emit(progress.phase, 0, None, 'bytes')
            if url.endswith('/manifest.json'):
                return json.dumps(self.manifest).encode()
            return self.raw if body is None else body
        with patch.object(sync, '_server_http_get', side_effect=transport):
            return sync.pull(self.conn, log=lambda _: None, download_images=False,
                             should_stop=stop, on_stage=callback or self.events.append)

    def test_real_merge_preserves_ids_and_personal_data(self):
        gid, _ = slg_db.upsert_game(self.conn, 'keep', 'https://dikgames.com/keep/', 'Old')
        slg_db.set_state(self.conn, gid, status='want', my_rating=5)
        self.conn.execute('UPDATE state SET note=? WHERE game_id=?', ('mine', gid))
        self.conn.commit()
        result = self.pull()
        self.assertEqual(result['new'], 1)
        self.assertEqual(tuple(self.conn.execute('SELECT id,title FROM games WHERE slug="keep"').fetchone()),
                         (gid, 'Updated keep'))
        self.assertEqual(tuple(self.conn.execute('SELECT status,my_rating,note FROM state WHERE game_id=?', (gid,)).fetchone()),
                         ('want', 5, 'mine'))
        self.assertTrue({'manifest', 'download', 'verify', 'merge', 'complete'}.issubset({e['phase'] for e in self.events}))
        self.assertEqual(self.events[-1]['status'], 'completed')
        self.assertFalse(any('percent' in e for e in self.events))

    def test_old_manifest_no_change_without_hash(self):
        self.manifest.pop('db_sha256')
        slg_db.set_pref(self.conn, sync.PREF_LAST_PULL, self.manifest['lastmod'])
        result = self.pull()
        self.assertTrue(result['unchanged'])
        self.assertEqual(self.events[-1]['status'], 'unchanged')
        self.assertNotIn('download', [e['phase'] for e in self.events])

    def test_hash_failure_and_network_failure_have_terminal_event(self):
        with self.assertRaisesRegex(RuntimeError, 'sha256'):
            self.pull(body=b'corrupt')
        self.assertEqual(self.events[-1]['status'], 'failed')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 0)
        with patch.object(sync, '_server_http_get', side_effect=ConnectionError('offline')):
            with patch.object(sync.time, 'sleep'):
                with self.assertRaises(ConnectionError):
                    sync.pull(self.conn, on_stage=self.events.append)
        self.assertEqual(self.events[-1]['status'], 'failed')

    def test_partial_merge_exception_rolls_back(self):
        real = slg_db.upsert_game
        calls = []
        def broken(*args, **kwargs):
            calls.append(kwargs['slug'])
            result = real(*args, **kwargs)
            if len(calls) == 2:
                raise RuntimeError('after second upsert')
            return result
        with patch.object(slg_db, 'upsert_game', side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, 'second upsert'):
                self.pull()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 0)
        self.assertIsNone(slg_db.get_pref(self.conn, sync.PREF_LAST_PULL_HASH))
        self.assertEqual(self.events[-1]['status'], 'failed')

    def test_cancel_before_merge_writes_nothing(self):
        stopped = [False]
        def observe(event):
            self.events.append(event)
            if event['phase'] == 'merge':
                stopped[0] = True
        with self.assertRaises(SyncCancelled):
            self.pull(stop=lambda: stopped[0], callback=observe)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 0)
        self.assertEqual(self.events[-1]['status'], 'cancelled')

    def test_stop_during_merge_finishes_atomically_and_stops_later_work(self):
        stopped = [False]
        real = sync._merge_catalog
        def merge(*args):
            stopped[0] = True
            return real(*args)
        with patch.object(sync, '_merge_catalog', side_effect=merge):
            with patch.object(sync, '_collector_status') as telemetry:
                with self.assertRaises(SyncCancelled):
                    self.pull(stop=lambda: stopped[0])
                telemetry.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 2)
        self.assertEqual(slg_db.get_pref(self.conn, sync.PREF_LAST_PULL_HASH), self.manifest['db_sha256'])
        self.assertEqual(self.events[-1]['status'], 'cancelled')
        self.assertNotIn('complete', [e['phase'] for e in self.events])


class DLsiteStageTests(unittest.TestCase):
    setUp = dlsite_fixture.OnlineTests.setUp
    publish_fixture = dlsite_fixture.OnlineTests.publish_fixture
    fetch = dlsite_fixture.OnlineTests.fetch
    def test_stage_dispatch_and_stop_inside_atomic_merge(self):
        events, stopped = [], [False]
        def transport(url, timeout=30, **kwargs):
            reporter = kwargs.get('progress')
            if reporter:
                reporter.check_stop()
            return self.fetch(url[len(sync.SERVER_BASE):])
        real = sync._merge_catalog
        def merge(*args):
            stopped[0] = True
            return real(*args)
        with patch.object(sync, '_server_http_get', side_effect=transport):
            with patch.object(sync, '_merge_catalog', side_effect=merge):
                with self.assertRaises(SyncCancelled):
                    sync.pull(self.conn, catalog_id='dlsite', log=lambda _: None,
                              should_stop=lambda: stopped[0], on_stage=events.append)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 151)
        self.assertTrue(sync.slg_db.get_pref(self.conn, 'catalogue.online.dlsite.hash'))
        self.assertEqual(events[-1]['status'], 'cancelled')
        self.assertIn('cover_index', [e['phase'] for e in events])
        self.assertNotIn('complete', [e['phase'] for e in events])


if __name__ == '__main__':
    unittest.main()
