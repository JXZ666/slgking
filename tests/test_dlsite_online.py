import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PIL import Image
import slg_db
import slg_dlsite_online as online
import slg_sync_server



def jpeg():
    target = io.BytesIO()
    Image.new('RGB', (20, 30), 'blue').save(target, 'JPEG')
    return target.getvalue()


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = slg_db.connect(str(self.root / 'personal.db'))
        self.addCleanup(self.conn.close)
        self.cover_index = {}
        self.manifest = self.publish_fixture()
        self.requests = []

    def publish_fixture(self):
        """Synthetic public-wire fixture, independent of private collection tools."""
        folder = self.root / 'public/catalogues/dlsite'
        folder.mkdir(parents=True, exist_ok=True)
        db = self.root / 'snapshot.db'
        if db.exists():
            db.unlink()
        conn = slg_db.connect(str(db))
        for i in range(151):
            slug = f'dlsite-rj{123456+i}'
            gid, _ = slg_db.upsert_game(conn, slug,
                f'https://www.dlsite.com/maniax/work/=/product_id/RJ{123456+i}.html', f'Game {i}')
            slg_db.set_dlsite_metadata(conn, gid, {'age_rating': 'R18', 'work_type': 'RPG'})
            slg_db.upsert_game_source(conn, 'dlsite', f'RJ{123456+i}', gid, source_url=f'https://www.dlsite.com/maniax/work/=/product_id/RJ{123456+i}.html')
            if slug in self.cover_index:
                conn.execute('UPDATE games SET cover_file=? WHERE id=?', (self.cover_index[slug]['filename'], gid))
        conn.execute("DELETE FROM prefs")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        raw = db.read_bytes()
        index = json.dumps(self.cover_index, sort_keys=True).encode()
        db_hash, index_hash = hashlib.sha256(raw).hexdigest(), hashlib.sha256(index).hexdigest()
        manifest = {'catalog_id': 'dlsite', 'count': 151, 'revision': 'synthetic',
            'db_sha256': db_hash, 'db_path': f'/catalogues/dlsite/snapshots/{db_hash}.db',
            'cover_index_sha256': index_hash, 'cover_index_path': f'/catalogues/dlsite/cover-index/{index_hash}.json',
            'source_status_path': '/catalogues/dlsite/status.json'}
        for route, data in ((manifest['db_path'], raw), (manifest['cover_index_path'], index),
                            (online.MANIFEST_PATH, json.dumps(manifest).encode()),
                            ('/catalogues/dlsite/status.json', b'{}')):
            online.atomic_bytes(self.root / 'public' / route.lstrip('/'), data)
        return manifest

    def fetch(self, path):
        self.requests.append(path)
        return (self.root / 'public' / path.lstrip('/')).read_bytes()

    def pull(self):
        return online.pull(self.conn, self.fetch, slg_sync_server.merge_catalog, now=10000)

    def test_pull_personal_state_unchanged_and_restart_cursor(self):
        self.assertEqual(self.pull()['catalogue'], 151)
        game = self.conn.execute('SELECT id FROM games LIMIT 1').fetchone()[0]
        self.conn.execute('INSERT INTO local(game_id,folder_path) VALUES (?,?)', (game, 'C:/Games/test'))
        self.conn.commit()
        self.requests.clear()
        self.assertTrue(self.pull()['unchanged'])
        self.assertEqual(self.requests, [online.MANIFEST_PATH,
                                      self.manifest['cover_index_path'],
                                      '/catalogues/dlsite/status.json'])
        self.assertEqual(self.conn.execute('SELECT folder_path FROM local WHERE game_id=?', (game,)).fetchone()[0], 'C:/Games/test')
        self.assertTrue(online.has_online_catalogue(self.conn))
        self.assertFalse(online.should_check(self.conn, now=10001))

    def test_wrong_manifest_count_is_rejected_before_merge(self):
        self.manifest['count'] = 150
        (self.root / 'public/catalogues/dlsite/manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaises(ValueError):
            self.pull()
        self.assertFalse(online.has_online_catalogue(self.conn))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 0)

    def test_bad_hash_leaves_database_and_cursor(self):
        path = self.root / 'public' / self.manifest['db_path'].lstrip('/')
        path.write_bytes(path.read_bytes() + b'bad')
        with self.assertRaises(ValueError):
            self.pull()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 0)

    def test_merge_failure_rolls_back(self):
        def fail(conn, path, catalog):
            conn.execute("INSERT INTO prefs VALUES ('bad','value')")
            raise RuntimeError('write failed')
        with self.assertRaises(RuntimeError):
            online.pull(self.conn, self.fetch, fail)
        self.assertIsNone(online._pref(self.conn, 'bad'))
        self.assertFalse(online.has_online_catalogue(self.conn))

    def test_disallowed_manifest_routes(self):
        for path in ('https://evil.test/a.db', '/covers/../a', self.manifest['db_path'] + '?x=1'):
            bad = dict(self.manifest, db_path=path)
            with self.assertRaises(ValueError):
                online.validate_manifest(bad)

    def test_server_change_during_publish_reads_immutable_files(self):
        original = dict(self.manifest)
        def fetch(path):
            if path == online.MANIFEST_PATH:
                raw = self.fetch(path)
                (self.root / 'public/catalogues/dlsite/manifest.json').write_text('{}')
                return raw
            return self.fetch(path)
        self.assertEqual(online.pull(self.conn, fetch, slg_sync_server.merge_catalog)['catalogue'], original['count'])

    def test_bad_index_identity(self):
        raw = jpeg()
        digest = hashlib.sha256(raw).hexdigest()
        item = {'filename': 'dlsite-RJ123456-' + digest + '.jpg', 'sha256': digest, 'bytes': len(raw)}
        with self.assertRaises(ValueError):
            online.validate_cover_index({'dlsite-rj654321': item})

    def test_online_cover_keeps_personal_override_and_same_game_id(self):
        self.pull()
        game = self.conn.execute("SELECT id FROM games WHERE slug='dlsite-rj123456'").fetchone()[0]
        self.conn.execute('INSERT INTO game_cover_overrides(game_id,cover_file) VALUES (?,?)', (game, 'user_keep.jpg'))
        self.conn.commit()
        raw = jpeg()
        digest = hashlib.sha256(raw).hexdigest()
        filename = f'dlsite-RJ123456-{digest}.jpg'
        online.atomic_bytes(self.root / 'public/covers' / filename, raw)
        item = {'filename': filename, 'sha256': digest, 'bytes': len(raw)}
        self.cover_index['dlsite-rj123456'] = item
        self.manifest = self.publish_fixture()
        self.pull()
        self.assertEqual(self.conn.execute("SELECT id,cover_file FROM games WHERE slug='dlsite-rj123456'").fetchone()[:], (game, filename))
        self.assertEqual(self.conn.execute('SELECT cover_file FROM game_cover_overrides WHERE game_id=?', (game,)).fetchone()[0], 'user_keep.jpg')
        self.assertEqual(online.get_cover_index(self.conn)['dlsite-rj123456'], item)

    def test_disconnect_retains_cursor(self):
        self.pull()
        old_hash = online._pref(self.conn, online.ONLINE_HASH_KEY)
        def offline(path):
            raise OSError('offline')
        with self.assertRaises(OSError):
            online.pull(self.conn, offline, slg_sync_server.merge_catalog)
        self.assertEqual(online._pref(self.conn, online.ONLINE_HASH_KEY), old_hash)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 151)


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = jpeg()
        digest = hashlib.sha256(self.raw).hexdigest()
        self.item = {'filename': 'dlsite-RJ123456-' + digest + '.jpg', 'sha256': digest, 'bytes': len(self.raw)}

    def test_duplicate_and_cache_never_refetch(self):
        calls = []
        def fetch(path):
            calls.append(path)
            time.sleep(.02)
            return self.raw
        cache = online.CoverCache(self.temp.name, fetch)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            result = list(pool.map(lambda _: cache.get(self.item), range(4)))
        self.assertEqual(len(set(result)), 1)
        self.assertEqual(len(calls), 1)
        cache.get(self.item)
        self.assertEqual(len(calls), 1)

    def test_failed_save_and_hash_do_not_replace_old_file(self):
        path = Path(self.temp.name) / self.item['filename']
        path.write_bytes(b'previous')
        cache = online.CoverCache(self.temp.name, lambda path: b'wrong')
        with self.assertRaises(ValueError):
            cache.get(self.item)
        self.assertEqual(path.read_bytes(), b'previous')
        with self.assertRaises(RuntimeError):
            cache.get(self.item)

    def test_decode_real_image_and_reject_invalid(self):
        for raw in (b'bad', b'0' * (online.MAX_IMAGE_BYTES + 1)):
            with self.assertRaises(ValueError):
                online.validate_image(raw)
        self.assertEqual(online.validate_image(self.raw).size, (20, 30))

    def test_animated_image_refused(self):
        target = io.BytesIO()
        first = Image.new('RGB', (2, 2), 'red')
        first.save(target, 'WEBP', save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')], duration=100, loop=0)
        with self.assertRaises(ValueError):
            online.validate_image(target.getvalue())


if __name__ == '__main__':
    unittest.main()
