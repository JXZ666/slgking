"""Offline tests exercising the actual public sync dispatcher and merge."""
import json
import unittest
from unittest.mock import patch

import test_dlsite_online as fixture
import slg_db
import slg_dlsite_online as online
import slg_local_library
import slg_sync_server as sync


class PullIntegration(unittest.TestCase):
    publish_fixture = fixture.OnlineTests.publish_fixture
    setUp = fixture.OnlineTests.setUp
    fetch = fixture.OnlineTests.fetch

    def pull(self, **kwargs):
        def transport(url, timeout):
            self.assertTrue(url.startswith(sync.SERVER_BASE + '/catalogues/dlsite/'))
            return self.fetch(url[len(sync.SERVER_BASE):])
        with patch.object(sync, '_server_http_get', side_effect=transport):
            return sync.pull(self.conn, catalog_id='dlsite', log=lambda _: None, **kwargs)

    def dump(self):
        return '\n'.join(self.conn.iterdump())

    def test_dispatch_actual_merge_preserves_id_and_personal_records(self):
        gid, _ = slg_db.upsert_game(self.conn, 'dlsite-rj123456', 'https://old.invalid', 'Old title')
        slg_db.set_game_catalogue(self.conn, gid, 'dlsite')
        slg_db.set_state(self.conn, gid, status='want', my_rating=5)
        self.conn.execute('UPDATE state SET note=? WHERE game_id=?', ('Personal note', gid))
        slg_db.set_local_exe(self.conn, gid, 'C:/Games/keep.exe')
        slg_local_library.set_cover_override(self.conn, gid, 'user_keep.jpg')
        collection = slg_db.create_collection(self.conn, 'Favourite')
        slg_db.set_game_collections(self.conn, gid, [collection])
        user = slg_db.add_user_game(self.conn, 'My custom game', categories=['galgame'])
        western, _ = slg_db.upsert_game(self.conn, 'west-keep', 'https://western.invalid', 'Western keep')
        slg_db.set_game_catalogue(self.conn, western, 'western_slg')
        self.conn.commit()
        result = self.pull()
        self.assertEqual((result['new'], result['catalogue']), (150, 151))
        row = self.conn.execute('SELECT id,title FROM games WHERE slug=?', ('dlsite-rj123456',)).fetchone()
        self.assertEqual(tuple(row), (gid, 'Game 0'))
        self.assertEqual(tuple(self.conn.execute('SELECT status,my_rating,note FROM state WHERE game_id=?', (gid,)).fetchone()), ('want', 5, 'Personal note'))
        self.assertEqual(self.conn.execute('SELECT exe_path FROM local WHERE game_id=?', (gid,)).fetchone()[0], 'C:/Games/keep.exe')
        self.assertEqual(self.conn.execute('SELECT cover_file FROM game_cover_overrides WHERE game_id=?', (gid,)).fetchone()[0], 'user_keep.jpg')
        self.assertEqual(self.conn.execute('SELECT collection_id FROM collection_items WHERE game_id=?', (gid,)).fetchone()[0], collection)
        self.assertEqual(slg_db.game_categories(self.conn, user), ('galgame',))
        self.assertEqual(slg_db.get_game(self.conn, western)['title'], 'Western keep')
        self.assertTrue(all(not path.startswith('/covers/') for path in self.requests))

    def test_damaged_snapshot_keeps_entire_database_and_cursors(self):
        slg_db.set_pref(self.conn, sync.PREF_LAST_PULL_HASH, 'western-hash')
        before = self.dump()
        path = self.root / 'public' / self.manifest['db_path'].lstrip('/')
        path.write_bytes(path.read_bytes() + b'bad hash')
        with self.assertRaises(ValueError):
            self.pull()
        self.assertEqual(self.dump(), before)

    def test_invalid_index_keeps_entire_database(self):
        self.pull()
        before = self.dump()
        path = self.root / 'public' / self.manifest['cover_index_path'].lstrip('/')
        path.write_bytes(b'{"untrusted":{}}')
        with self.assertRaises(ValueError):
            self.pull()
        self.assertEqual(self.dump(), before)

    def test_merge_exception_rolls_back_all_writes(self):
        before = self.dump()
        def failing(conn, path, catalogue):
            self.assertEqual(catalogue, 'dlsite')
            conn.execute("INSERT INTO prefs VALUES ('partial-write','bad')")
            raise RuntimeError('simulated database failure')
        with patch.object(sync, '_merge_catalog', side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                self.pull()
        self.assertEqual(self.dump(), before)

    def test_actual_merge_partial_upserts_roll_back(self):
        before = self.dump()
        real = slg_db.upsert_game
        calls = []
        def upsert(*args, **kwargs):
            calls.append(kwargs.get('slug', args[1] if len(args) > 1 else None))
            if len(calls) == 5:
                raise RuntimeError('failure after four real upserts')
            return real(*args, **kwargs)
        with patch.object(slg_db, 'upsert_game', side_effect=upsert):
            with self.assertRaisesRegex(RuntimeError, 'four real'):
                self.pull()
        self.assertEqual(len(calls), 5)
        self.assertEqual(self.dump(), before)
        self.assertFalse(online.has_online_catalogue(self.conn))

    def test_bundle_does_not_replace_online_or_read_old_assets(self):
        self.pull()
        self.conn.execute("UPDATE games SET title='Latest online title' WHERE slug='dlsite-rj123456'")
        self.conn.commit()
        before = self.dump()
        result = sync.import_local_catalogue(self.conn, '/does/not/exist.db', '/does/not/exist.json', log=lambda _: None)
        self.assertTrue(result['unchanged'])
        self.assertEqual(result['catalogue'], 151)
        self.assertEqual(self.dump(), before)

    def test_online_cursor_independent_and_western_dispatch_unchanged(self):
        slg_db.set_pref(self.conn, sync.PREF_LAST_PULL_HASH, 'a' * 64)
        slg_db.set_pref(self.conn, sync.PREF_LAST_PULL, 'old-western-mod')
        slg_db.set_pref(self.conn, online.BUNDLE_HASH_KEY, 'old-bundle-hash')
        self.pull()
        self.assertEqual(slg_db.get_pref(self.conn, sync.PREF_LAST_PULL_HASH), 'a' * 64)
        self.assertEqual(slg_db.get_pref(self.conn, sync.PREF_LAST_PULL), 'old-western-mod')
        self.assertEqual(slg_db.get_pref(self.conn, online.BUNDLE_HASH_KEY), 'old-bundle-hash')
        self.assertEqual(slg_db.get_pref(self.conn, online.ONLINE_HASH_KEY), self.manifest['db_sha256'])
        dlsite_hash = slg_db.get_pref(self.conn, online.ONLINE_HASH_KEY)
        with patch.object(sync, 'fetch_manifest', return_value=dict(catalog_id='western_slg', db_sha256='a'*64, count=7)), patch.object(sync, '_missing_covers', return_value=[]), patch.object(sync, '_server_http_get', side_effect=AssertionError('unexpected download')):
            result = sync.pull(self.conn, log=lambda _: None)
        self.assertTrue(result['unchanged'])
        self.assertEqual(result['catalogue'], 7)
        self.assertEqual(slg_db.get_pref(self.conn, online.ONLINE_HASH_KEY), dlsite_hash)

    def test_cancellation_preserves_database_before_fetch(self):
        before = self.dump()
        with self.assertRaisesRegex(RuntimeError, '取消'):
            self.pull(should_stop=lambda: True)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.dump(), before)


if __name__ == '__main__':
    unittest.main()
