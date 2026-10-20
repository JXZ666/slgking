"""Offline RJ/VJ identity and official-cover contracts; no real network."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ANDROID = Path(__file__).resolve().parents[1]
if str(ANDROID) not in sys.path:
    sys.path.insert(0, str(ANDROID))

from PIL import Image  # noqa: E402

import slg_android_api as api  # noqa: E402
import slg_catalogue_query as catalogue_query  # noqa: E402
import slg_db  # noqa: E402
import slg_dlsite_client  # noqa: E402
import slg_dlsite_online as online  # noqa: E402
import slg_library_metadata  # noqa: E402
import slg_scrape  # noqa: E402
import slg_sync_server as sync  # noqa: E402

RJ = dict(slug='dlsite-rj123456', external='RJ123456',
          url='https://www.dlsite.com/maniax/work/=/product_id/RJ123456.html',
          title='RJ Game')
VJ = dict(slug='dlsite-vj123456', external='VJ123456',
          url='https://www.dlsite.com/pro/work/=/product_id/VJ123456.html',
          title='VJ Game')
RJ_URL = 'https://img.dlsite.jp/modpub/images2/work/doujin/RJ123456/RJ123456_img_main.jpg'
VJ_URL = 'https://img.dlsite.jp/modpub/images2/work/pro/VJ123456/VJ123456_img_main.jpg'


def image_bytes(fmt='PNG', color='red', size=(20, 40)):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, fmt)
    return buffer.getvalue()


def reference(url):
    return {'url': url}


def cover_item(external='RJ123456'):
    raw = image_bytes('JPEG')
    digest = hashlib.sha256(raw).hexdigest()
    return {'filename': 'dlsite-%s-%s.jpg' % (external, digest),
            'sha256': digest, 'bytes': len(raw)}


def write_snapshot(path, works):
    """Build a synthetic public snapshot with site games and DL sources."""
    path = Path(path)
    conn = slg_db.connect(str(path))
    try:
        for work in works:
            game_id, _ = slg_db.upsert_game(conn, work['slug'], work['url'], work['title'])
            slg_db.upsert_game_source(conn, 'dlsite', work['external'], game_id,
                                      source_url=work['url'])
            if work.get('cover'):
                conn.execute('UPDATE games SET cover_file=? WHERE id=?',
                             (work['cover'], game_id))
        conn.execute('DELETE FROM prefs')
        conn.commit()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('PRAGMA journal_mode=DELETE')
    finally:
        conn.close()
    return path.read_bytes()


def publish_fixture(root, works, legacy_index):
    """Write manifest + legacy index + official index under catalogues/dlsite."""
    root = Path(root)
    raw = write_snapshot(root / 'snapshot.db', works)
    legacy_raw = json.dumps(legacy_index, sort_keys=True).encode()
    official_raw = json.dumps({
        'version': 1, 'catalog_id': 'dlsite',
        'items': {work['slug']: reference(work['official'])
                  for work in works if work.get('official')}}).encode()
    manifest = {
        'catalog_id': 'dlsite', 'count': len(works), 'revision': 'synthetic-vj',
        'db_sha256': hashlib.sha256(raw).hexdigest(),
        'db_path': '/catalogues/dlsite/snapshots/%s.db' % hashlib.sha256(raw).hexdigest(),
        'cover_index_sha256': hashlib.sha256(legacy_raw).hexdigest(),
        'cover_index_path': '/catalogues/dlsite/cover-index/%s.json'
                            % hashlib.sha256(legacy_raw).hexdigest(),
        'official_cover_index_sha256': hashlib.sha256(official_raw).hexdigest(),
        'official_cover_index_path': '/catalogues/dlsite/official-cover-index/%s.json'
                                     % hashlib.sha256(official_raw).hexdigest(),
        'official_cover_delivery': 'client-direct',
    }
    files = {
        manifest['db_path']: raw,
        manifest['cover_index_path']: legacy_raw,
        manifest['official_cover_index_path']: official_raw,
        online.MANIFEST_PATH: json.dumps(manifest).encode(),
    }
    for route, data in files.items():
        online.atomic_bytes(root / 'public' / route.lstrip('/'), data)
    return manifest


def fixture_works():
    legacy = cover_item('RJ123456')
    return ([dict(RJ, cover=legacy['filename'], official=RJ_URL),
             dict(VJ, cover=None, official=VJ_URL)],
            {'dlsite-rj123456': legacy})


class OnlineFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.works, self.legacy_index = fixture_works()
        self.manifest = publish_fixture(self.root, self.works, self.legacy_index)
        self.requests = []
        self.conn = slg_db.connect(str(self.root / 'client.db'))

    def fetch(self, path):
        self.requests.append(path)
        return (self.root / 'public' / path.lstrip('/')).read_bytes()

    def pull(self, merge=None, now=10000):
        return online.pull(self.conn, self.fetch,
                           merge or (lambda *args: self.fail('unchanged DB merged')),
                           now=now)

    def fail(self, message):
        raise AssertionError(message)

    def close(self):
        self.conn.close()


class IdentityProtocolTests(unittest.TestCase):
    def setUp(self):
        online._OFFICIAL_CACHE.clear()

    def test_cover_index_accepts_rj_and_vj_and_rejects_malformed(self):
        vj_item = cover_item('VJ123456')
        self.assertEqual(online.validate_cover_index({'dlsite-vj123456': vj_item}),
                         {'dlsite-vj123456': vj_item})
        for bad in ({'dlsite-vj123456': cover_item('RJ123456')},
                    {'dlsite-vx123456': vj_item},
                    {'dlsite-vj12345': vj_item},
                    {'dlsite-vj12345678901': vj_item},
                    {'dlsite-vj123456': dict(vj_item, bytes=0)}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                online.validate_cover_index(bad)

    def test_manifest_keeps_legacy_index_mandatory(self):
        base = dict(catalog_id='dlsite', count=1, revision='r',
                    db_sha256='a' * 64,
                    db_path='/catalogues/dlsite/snapshots/' + 'a' * 64 + '.db',
                    cover_index_sha256='b' * 64,
                    cover_index_path='/catalogues/dlsite/cover-index/' + 'b' * 64 + '.json')
        self.assertEqual(online.validate_manifest(base), base)
        legacy_optional = {key: value for key, value in base.items()
                           if not key.startswith('cover_index')}
        legacy_optional['cover_index_path'] = base['cover_index_path']
        with self.assertRaises(ValueError):
            online.validate_manifest(legacy_optional)

    def test_manifest_official_fields_are_optional_and_consistent(self):
        base = dict(catalog_id='dlsite', count=1, revision='r',
                    db_sha256='a' * 64,
                    db_path='/catalogues/dlsite/snapshots/' + 'a' * 64 + '.db',
                    cover_index_sha256='b' * 64,
                    cover_index_path='/catalogues/dlsite/cover-index/' + 'b' * 64 + '.json')
        good = dict(base, official_cover_index_sha256='c' * 64,
                    official_cover_index_path='/catalogues/dlsite/official-cover-index/'
                                              + 'c' * 64 + '.json',
                    official_cover_delivery='client-direct')
        self.assertEqual(online.validate_manifest(good), good)
        for bad in (dict(good, official_cover_index_path='/catalogues/dlsite/official-cover-index/'
                     + 'd' * 64 + '.json'),
                    dict(good, official_cover_delivery='server'),
                    dict(good, official_cover_index_sha256='not-a-hash'),
                    {key: value for key, value in good.items()
                     if key != 'official_cover_index_path'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                online.validate_manifest(bad)


class OfficialIndexTests(unittest.TestCase):
    def setUp(self):
        online._OFFICIAL_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = slg_db.connect(str(Path(self.temp.name) / 'db.sqlite'))
        self.addCleanup(self.conn.close)

    def test_index_parses_rj_and_vj_and_rejects_identity_mismatch(self):
        value = {'version': 1, 'catalog_id': 'dlsite',
                 'items': {'dlsite-rj123456': reference(RJ_URL),
                           'dlsite-vj123456': reference(VJ_URL)}}
        self.assertEqual(online.validate_official_cover_index(value), value)
        bad_values = [
            dict(value, catalog_id='western_slg'),
            dict(value, version=2),
            dict(value, version=True),
            dict(value, items=[]),
            {'version': 1, 'catalog_id': 'dlsite',
             'items': {'dlsite-rx123456': reference(RJ_URL)}},
            {'version': 1, 'catalog_id': 'dlsite',
             'items': {'dlsite-vj123456': reference(RJ_URL)}},
        ]
        bad_urls = [
            VJ_URL.replace('https://', 'http://'),
            VJ_URL.replace('img.dlsite.jp', 'evil.test'),
            VJ_URL.replace('img.dlsite.jp', 'img.dlsite.jp.evil.test'),
            VJ_URL.replace('https://', 'https://user:pass@'),
            VJ_URL + '?token=1',
            VJ_URL + '#x',
            VJ_URL.replace('/work/', '/work/../'),
            VJ_URL.replace('VJ123456_img_main', 'RJ123456_img_main'),
            'https://img.dlsite.jp/modpub/images2/work/pro/VJ123456/',
            'https://img.dlsite.jp/modpub/images2/work/pro/VJ123456/%2e%2e/x.jpg',
        ]
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                online.validate_official_cover_index(bad)
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                online.validate_official_cover_index(
                    {'version': 1, 'catalog_id': 'dlsite',
                     'items': {'dlsite-vj123456': reference(url)}})

    def test_load_checks_hash_and_caps_size(self):
        raw = json.dumps({'version': 1, 'catalog_id': 'dlsite',
                          'items': {'dlsite-vj123456': reference(VJ_URL)}}).encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(online.load_official_index(raw, digest)['items'],
                         {'dlsite-vj123456': reference(VJ_URL)})
        with self.assertRaises(ValueError):
            online.load_official_index(raw, '0' * 64)
        with self.assertRaises(ValueError):
            online.load_official_index(b'x' * (online.MAX_OFFICIAL_INDEX_BYTES + 1), digest)

    def test_pref_getter_roundtrip_and_corruption(self):
        items = {'dlsite-rj123456': reference(RJ_URL),
                 'dlsite-vj123456': reference(VJ_URL)}
        slg_db.set_pref(self.conn, online.OFFICIAL_INDEX_KEY, json.dumps(items))
        slg_db.set_pref(self.conn, online.OFFICIAL_HASH_KEY, 'h1')
        self.assertEqual(online.get_official_index(self.conn), items)
        # Cached return is a fresh mapping: later catalogue rewrites win.
        slg_db.set_pref(self.conn, online.OFFICIAL_INDEX_KEY,
                        json.dumps({'dlsite-vj123456': reference(
                            VJ_URL.replace('_img_main', '_img_smp'))}))
        slg_db.set_pref(self.conn, online.OFFICIAL_HASH_KEY, 'h2')
        self.assertEqual(online.get_official_index(self.conn)['dlsite-vj123456']['url'],
                         VJ_URL.replace('_img_main', '_img_smp'))
        slg_db.set_pref(self.conn, online.OFFICIAL_INDEX_KEY,
                        json.dumps({'dlsite-vj123456': reference(RJ_URL)}))
        slg_db.set_pref(self.conn, online.OFFICIAL_HASH_KEY, 'h3')
        with self.assertRaises(ValueError):
            online.get_official_index(self.conn)


class PullTests(unittest.TestCase):
    def setUp(self):
        online._OFFICIAL_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = OnlineFixture(self.temp.name)
        self.addCleanup(self.fixture.close)

    def test_pull_merges_vj_and_installs_official_index(self):
        merged = []
        result = online.pull(
            self.fixture.conn, self.fixture.fetch,
            lambda conn, path, catalog: merged.append(catalog) or
            sync.merge_catalog(conn, path, catalog), now=10000)
        self.assertFalse(result['unchanged'])
        self.assertEqual(merged, ['dlsite'])
        self.assertEqual(result['official_cover_references'], 2)
        self.assertEqual(result['official_cover_hash'],
                         self.fixture.manifest['official_cover_index_sha256'])
        self.assertEqual(result['covers'], len(self.fixture.legacy_index))
        self.assertEqual(online.get_official_index(self.fixture.conn),
                         {'dlsite-rj123456': reference(RJ_URL),
                          'dlsite-vj123456': reference(VJ_URL)})
        self.assertEqual(online.get_cover_index(self.fixture.conn),
                         self.fixture.legacy_index)
        vj = self.fixture.conn.execute(
            "SELECT id, cover_file FROM games WHERE slug='dlsite-vj123456'").fetchone()
        self.assertIsNone(vj['cover_file'])
        self.assertEqual(slg_db.game_catalogues_bulk(
            self.fixture.conn, [vj['id']])[vj['id']], ('dlsite',))
        self.assertEqual(self.fixture.conn.execute(
            "SELECT external_id FROM game_sources WHERE external_id='VJ123456'"
        ).fetchone()[0], 'VJ123456')

    def test_unchanged_database_still_installs_official_index(self):
        conn = self.fixture.conn
        slg_db.set_pref(conn, online.ONLINE_HASH_KEY,
                        self.fixture.manifest['db_sha256'])
        result = self.fixture.pull()
        self.assertTrue(result['unchanged'])
        self.assertEqual(result['official_cover_references'], 2)
        self.assertNotIn(self.fixture.manifest['db_path'], self.fixture.requests)
        self.assertEqual(len(online.get_official_index(conn)), 2)
        self.assertEqual(online.get_cover_index(conn), self.fixture.legacy_index)

    def test_bad_official_hash_installs_nothing(self):
        path = self.fixture.root / 'public' / \
            self.fixture.manifest['official_cover_index_path'].lstrip('/')
        path.write_bytes(path.read_bytes() + b'bad')
        with self.assertRaises(ValueError):
            online.pull(self.fixture.conn, self.fixture.fetch,
                        lambda *args: self.fail('merged'), now=10000)
        self.assertIsNone(online._pref(self.fixture.conn, online.ONLINE_HASH_KEY))
        self.assertEqual(online.get_official_index(self.fixture.conn), {})
        self.assertEqual(self.fixture.conn.execute(
            'SELECT count(*) FROM games').fetchone()[0], 0)

    def test_legacy_cover_cache_accepts_vj_filename(self):
        calls = []
        raw = image_bytes('JPEG')
        digest = hashlib.sha256(raw).hexdigest()
        filename = 'dlsite-VJ123456-%s.jpg' % digest
        item = {'filename': filename, 'sha256': digest, 'bytes': len(raw)}
        with tempfile.TemporaryDirectory() as directory:
            cache = online.CoverCache(directory, lambda path: calls.append(path) or raw)
            self.assertEqual(Path(cache.get(item)).read_bytes(), raw)
            self.assertEqual(calls, ['/covers/' + filename])

    def test_snapshot_accepts_vj_with_null_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_snapshot(root / 'snapshot.db', [dict(RJ, cover=None), dict(VJ, cover=None)])
            online.validate_snapshot(root / 'snapshot.db', {'count': 2}, {})

    def test_snapshot_rejects_legacy_cover_for_official_only_vj(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_snapshot(root / 'snapshot.db', [dict(VJ, cover=None)])
            item = cover_item('VJ123456')
            with self.assertRaises(ValueError):
                online.validate_snapshot(root / 'snapshot.db', {'count': 1},
                                         {'dlsite-vj123456': item})

    def test_snapshot_rejects_mismatched_vj_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_snapshot(root / 'snapshot.db', [dict(VJ, cover=None)])
            conn = slg_db.connect(str(root / 'snapshot.db'))
            try:
                conn.execute("UPDATE game_sources SET external_id='RJ123456'")
                conn.commit()
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                conn.execute('PRAGMA journal_mode=DELETE')
            finally:
                conn.close()
            with self.assertRaises(ValueError):
                online.validate_snapshot(root / 'snapshot.db', {'count': 1}, {})


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = slg_db.connect(str(Path(self.temp.name) / 'db.sqlite'))
        self.addCleanup(self.conn.close)

    def test_source_url_section_must_match_work(self):
        self.assertEqual(slg_db._safe_source_url('dlsite', RJ['url']), RJ['url'])
        self.assertEqual(slg_db._safe_source_url('dlsite', VJ['url']), VJ['url'])
        for url in (VJ['url'].replace('/pro/', '/maniax/'),
                    RJ['url'].replace('/maniax/', '/pro/'),
                    VJ['url'].replace('https://', 'http://'),
                    VJ['url'].replace('www.dlsite.com', 'evil.test'),
                    VJ['url'].replace('www.dlsite.com', 'www.dlsite.com:444'),
                    VJ['url'].replace('/pro/', '/girls/')):
            with self.subTest(url=url):
                self.assertIsNone(slg_db._safe_source_url('dlsite', url))

    def test_upsert_game_source_accepts_vj_and_rejects_mismatch(self):
        game_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        slg_db.upsert_game_source(self.conn, 'dlsite', 'VJ123456', game_id,
                                  source_url=VJ['url'])
        self.assertEqual(slg_db.list_game_sources(self.conn, game_id)[0]['url'],
                         VJ['url'])
        for external, url in (('RJ123456', VJ['url']),
                              ('VJ123456', VJ['url'].replace('/pro/', '/maniax/')),
                              ('VJ12345x', VJ['url']),
                              ('vj123456', VJ['url'])):
            with self.subTest(external=external, url=url), self.assertRaises(ValueError):
                slg_db.upsert_game_source(self.conn, 'dlsite', external, game_id,
                                          source_url=url)

    def test_database_prefix_classifies_vj_as_dlsite(self):
        vj_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        rj_id, _ = slg_db.upsert_game(self.conn, RJ['slug'], RJ['url'], RJ['title'])
        west_id, _ = slg_db.upsert_game(self.conn, 'west', 'https://dikgames.com/x/',
                                        'Western')
        catalogues = slg_db.game_catalogues_bulk(
            self.conn, [vj_id, rj_id, west_id])
        self.assertEqual(catalogues[vj_id], ('dlsite',))
        self.assertEqual(catalogues[rj_id], ('dlsite',))
        self.assertEqual(catalogues[west_id], ('western_slg',))
        source = self.conn.execute(
            'SELECT source_id,external_id FROM game_sources WHERE game_id=?',
            (vj_id,)).fetchone()
        self.assertEqual((source['source_id'], source['external_id']),
                         ('dlsite', 'VJ123456'))
        self.assertIsNone(slg_db.get_game(self.conn, vj_id)['rating'])

    def test_malformed_dlsite_slugs_are_rejected(self):
        for slug in ('dlsite-vx123456', 'dlsite-vjabc', 'dlsite-vj', 'dlsite-999999'):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                slg_db.upsert_game(self.conn, slug, VJ['url'], 'bad')

    def test_connect_backfills_vj_catalogue_membership(self):
        vj_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        self.conn.execute('DELETE FROM game_catalogs WHERE game_id=?', (vj_id,))
        self.conn.commit()
        self.assertEqual(
            slg_db.game_catalogues_bulk(self.conn, [vj_id])[vj_id], ())
        path = str(Path(self.temp.name) / 'db.sqlite')
        self.conn.close()
        reopened = slg_db.connect(path)
        self.addCleanup(reopened.close)
        self.assertEqual(slg_db.game_catalogues_bulk(reopened, [vj_id]),
                         {vj_id: ('dlsite',)})

    def test_personal_metadata_link_section_matches_work(self):
        base = dict(source='dlsite', external_id='VJ123456', title='VJ Game',
                    source_url=VJ['url'])
        self.assertEqual(slg_library_metadata.validate(dict(base))['external_id'],
                         'VJ123456')
        for url in (VJ['url'].replace('/pro/', '/maniax/'), RJ['url']):
            with self.subTest(url=url), self.assertRaises(ValueError):
                slg_library_metadata.validate(dict(base, source_url=url))

    def test_native_metadata_requires_a_dlsite_work(self):
        vj_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        slg_db.set_dlsite_metadata(self.conn, vj_id, {'rating_value': 4,
                                                     'rating_count': 10,
                                                     'work_type': 'Adventure'})
        self.assertEqual(slg_db.get_game_metadata_bulk(self.conn, [vj_id])[vj_id]
                         ['rating_value'], 4)
        west_id, _ = slg_db.upsert_game(self.conn, 'west', 'https://dikgames.com/x/',
                                        'Western')
        with self.assertRaises(ValueError):
            slg_db.set_dlsite_metadata(self.conn, west_id, {'work_type': 'RPG'})

    def test_western_metrics_scopes_exclude_vj(self):
        vj_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        self.conn.execute("UPDATE games SET cover_file='pending:https://x/y.jpg',"
                          " overview=NULL, site_views=NULL WHERE id=?", (vj_id,))
        self.conn.commit()
        slg_db.invalidate_cover_gaps()
        self.assertEqual(slg_db.metrics_gap_rows(self.conn), [])
        self.assertEqual(slg_db.data_gaps(self.conn),
                         {'covers': 0, 'rating': 0, 'overview': 0, 'heat': 0,
                          'metrics': 0})
        self.assertEqual(slg_db.backfill_heat(self.conn, log=lambda _: None), 0)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = slg_db.connect(str(self.root / 'local.db'))
        self.addCleanup(self.conn.close)

    def test_vj_snapshot_merges_into_dlsite_catalogue(self):
        write_snapshot(self.root / 'remote.db',
                       [dict(RJ, cover=None), dict(VJ, cover=None)])
        created, total = sync.merge_catalog(
            self.conn, str(self.root / 'remote.db'), 'dlsite')
        self.assertEqual((created, total), (2, 2))
        rows = {row['slug']: row['catalog_id'] for row in self.conn.execute(
            'SELECT g.slug,gc.catalog_id FROM games g JOIN game_catalogs gc'
            ' ON gc.game_id=g.id ORDER BY g.slug')}
        self.assertEqual(rows, {'dlsite-rj123456': 'dlsite',
                                'dlsite-vj123456': 'dlsite'})
        self.assertEqual(self.conn.execute(
            "SELECT external_id FROM game_sources WHERE external_id='VJ123456'"
        ).fetchone()[0], 'VJ123456')

    def test_western_merge_still_rejects_vj_snapshot(self):
        write_snapshot(self.root / 'remote.db', [dict(VJ, cover=None)])
        with self.assertRaises(ValueError):
            sync.merge_catalog(self.conn, str(self.root / 'remote.db'), 'western_slg')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM games').fetchone()[0], 0)

    def test_vj_redirect_merges_within_dlsite_and_respects_boundary(self):
        old, _ = slg_db.upsert_game(
            self.conn, 'dlsite-vj111111',
            'https://www.dlsite.com/pro/work/=/product_id/VJ111111.html', 'Old VJ')
        new, _ = slg_db.upsert_game(
            self.conn, 'dlsite-vj222222',
            'https://www.dlsite.com/pro/work/=/product_id/VJ222222.html', 'New VJ')
        slg_db.set_state(self.conn, old, my_rating=5)
        self.conn.commit()
        result = sync._sync_catalog_redirects(
            self.conn, {'dlsite-vj111111': 'dlsite-vj222222'}, 'dlsite')
        self.assertEqual(result['active'], 1)
        self.assertEqual(self.conn.execute(
            "SELECT game_id FROM state WHERE my_rating=5").fetchone()[0], new)
        visible = [row['id'] for row in slg_db.find_games(self.conn, catalog_id='dlsite')]
        self.assertNotIn(old, visible)
        west, _ = slg_db.upsert_game(self.conn, 'west', 'https://dikgames.com/x/',
                                     'Western')
        with self.assertRaises(ValueError):
            sync._sync_catalog_redirects(
                self.conn, {'dlsite-vj222222': 'west'}, 'western_slg')
        self.assertEqual(slg_db.get_game(self.conn, west)['title'], 'Western')


class BridgeItemTests(unittest.TestCase):
    """`_item` and the bridge handlers that feed Kotlin's GameItem."""

    def setUp(self):
        online._OFFICIAL_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.covers = self.root / 'covers'
        self.covers.mkdir()
        self.conn = slg_db.connect(str(self.root / 'client.db'))
        self.addCleanup(self.conn.close)
        covers_patch = patch.object(slg_db, 'covers_dir', return_value=str(self.covers))
        covers_patch.start()
        self.addCleanup(covers_patch.stop)
        self.vj_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        slg_db.set_dlsite_metadata(self.conn, self.vj_id, {
            'rating_value': 4, 'rating_count': 10, 'languages': ['日本語'],
            'work_type': 'Adventure'})
        self.rj_id, _ = slg_db.upsert_game(self.conn, RJ['slug'], RJ['url'], RJ['title'])
        self.west_id, _ = slg_db.upsert_game(self.conn, 'west', 'https://dikgames.com/x/',
                                             'Western')
        (self.covers / 'legacy.jpg').write_bytes(image_bytes('JPEG'))
        self.conn.execute("UPDATE games SET cover_file='legacy.jpg' WHERE id=?",
                          (self.rj_id,))
        self.conn.commit()
        slg_db.set_pref(self.conn, online.OFFICIAL_INDEX_KEY,
                        json.dumps({'dlsite-vj123456': reference(VJ_URL)}))
        slg_db.set_pref(self.conn, online.OFFICIAL_HASH_KEY, 'item-hash')
        self.conn.commit()

    def item(self, slug):
        rows = {r['slug']: r for r in slg_db.find_games(
            self.conn, catalog_id='all', with_scores=False)}
        return api._item(rows[slug], {}, api._official_cover_index(self.conn))

    def test_item_exposes_official_cover_url_and_legacy_cover(self):
        vj = self.item(VJ['slug'])
        self.assertEqual(vj['cover_url'], VJ_URL)
        self.assertEqual(vj['catalog_id'], 'dlsite')
        self.assertEqual(vj['rating'], 4)
        self.assertEqual(vj['rating_count'], 10)
        self.assertIsNone(vj['cover'])
        rj = self.item(RJ['slug'])
        self.assertEqual(rj['cover_url'], '')
        self.assertEqual(rj['cover'], str(self.covers / 'legacy.jpg'))
        self.assertEqual(rj['cover_file'], 'legacy.jpg')
        self.assertFalse(rj['has_cover_override'])
        west = self.item('west')
        self.assertEqual(west['cover_url'], '')
        self.assertEqual(west['catalog_id'], 'western_slg')

    def test_item_marks_personal_cover_override(self):
        import slg_local_library
        name = 'personal.jpg'
        (self.covers / name).write_bytes(image_bytes('JPEG'))
        slg_local_library.set_cover_override(self.conn, self.rj_id, name)
        self.conn.commit()
        rj = self.item(RJ['slug'])
        self.assertTrue(rj['has_cover_override'])
        self.assertEqual(rj['cover'], str(self.covers / name))
        self.assertEqual(rj['cover_file'], name)

    def test_catalog_list_bridge_includes_vj_cover_url(self):
        with patch.object(api, '_conn', return_value=self.conn):
            payload = api._h_catalog_list(
                {'catalog_id': 'dlsite', 'page': 1, 'page_size': 10})
        items = {item['slug']: item for item in payload['items']}
        self.assertEqual(set(items), {RJ['slug'], VJ['slug']})
        self.assertEqual(payload['total'], 2)
        self.assertEqual(items[VJ['slug']]['cover_url'], VJ_URL)
        self.assertEqual(items[RJ['slug']]['cover_url'], '')
        self.assertEqual(items[RJ['slug']]['cover'], str(self.covers / 'legacy.jpg'))

    def test_game_detail_has_vj_facts_and_source_link(self):
        with patch.object(api, '_conn', return_value=self.conn):
            payload = api._h_game_get({'id': self.vj_id})
        game = payload['game']
        self.assertEqual(game['cover_url'], VJ_URL)
        self.assertEqual(game['dlsite_facts']['product_id'], 'VJ123456')
        self.assertEqual(game['dlsite_facts']['official_url'], VJ['url'])
        self.assertEqual([s['url'] for s in game['sources']], [VJ['url']])

    def test_cover_get_treats_vj_as_dlsite(self):
        entry = cover_item('VJ123456')
        cache = Mock()
        cache.get.return_value = str(self.covers / 'legacy.jpg')
        with patch.object(api, '_conn', return_value=self.conn), \
                patch.object(api, '_COVER_CACHES', {(str(self.covers), True): cache}), \
                patch('slg_dlsite_online.get_cover_index',
                      return_value={'dlsite-vj123456': entry}):
            response = api._h_cover_get({'id': self.vj_id})
        self.assertEqual(response, {'path': cache.get.return_value})
        cache.get.assert_called_once_with(entry)

    def test_dlsite_client_vj_facts_and_section_link(self):
        row = slg_db.find_games(self.conn, catalog_id='dlsite',
                                with_scores=False)
        vj = next(r for r in row if r['slug'] == VJ['slug'])
        facts = slg_dlsite_client.detail_facts(vj)
        self.assertEqual(facts['product_id'], 'VJ123456')
        self.assertEqual(facts['official_url'], VJ['url'])
        broken = dict(vj, url=VJ['url'].replace('/pro/', '/maniax/'))
        self.assertIsNone(slg_dlsite_client.detail_facts(broken)['official_url'])
        self.assertIn('商品编号：VJ123456', slg_dlsite_client.detail_lines(vj))


class CatalogueQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = slg_db.connect(str(Path(self.temp.name) / 'db.sqlite'))
        self.addCleanup(self.conn.close)
        self.vj_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        slg_db.set_dlsite_metadata(self.conn, self.vj_id, {'rating_value': 4,
                                                          'work_type': 'RPG'})
        self.west_id, _ = slg_db.upsert_game(self.conn, 'west',
                                             'https://dikgames.com/x/', 'Western',
                                             rating=7)

    def test_dl_catalogue_finds_and_filters_vj(self):
        for spec in ({'catalog_id': 'dlsite'},
                     {'catalog_id': 'dlsite', 'source': 'dlsite'},
                     {'catalog_id': 'all', 'source': 'dlsite'}):
            result = catalogue_query.query_catalogue_page(
                self.conn, dict(spec, sort='title'), 1)
            self.assertEqual([row['id'] for row in result.rows], [self.vj_id])
            self.assertEqual(result.total, 1)

    def test_western_recommendation_scope_excludes_vj(self):
        result = catalogue_query.query_catalogue_page(
            self.conn, {'catalog_id': 'all', 'sort': 'score'}, 1)
        scores = {row['slug']: row['score'] for row in result.rows}
        self.assertIsNotNone(scores['west'])
        self.assertIsNone(scores[VJ['slug']])
        western = slg_db.find_games(self.conn, catalog_id='western_slg', sort='score')
        self.assertEqual([row['slug'] for row in western], ['west'])


class ScrapeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.covers = Path(self.temp.name) / 'covers'
        self.covers.mkdir()
        covers_patch = patch.object(slg_db, 'covers_dir', return_value=str(self.covers))
        covers_patch.start()
        self.addCleanup(covers_patch.stop)
        self.conn = slg_db.connect(str(Path(self.temp.name) / 'db.sqlite'))
        self.addCleanup(self.conn.close)
        self.vj_id, _ = slg_db.upsert_game(self.conn, VJ['slug'], VJ['url'], VJ['title'])
        self.conn.execute("UPDATE games SET cover_file='pending:https://example.invalid/x.jpg',"
                          " overview=NULL, site_views=NULL, rating=NULL WHERE id=?",
                          (self.vj_id,))
        self.conn.commit()
        slg_db.invalidate_cover_gaps()

    def test_legacy_parser_never_touches_vj(self):
        fetcher = Mock()
        fetcher.get.side_effect = AssertionError('legacy fetch forbidden')
        with patch.object(slg_scrape, 'http_get',
                          side_effect=AssertionError('legacy fetch forbidden')):
            self.assertEqual(slg_scrape.download_covers(self.conn, log=lambda _: None), 0)
            slg_scrape.enrich(self.conn, fetcher, log=lambda _: None)
            slg_scrape.backfill_metrics(self.conn, fetcher, log=lambda _: None)
        fetcher.get.assert_not_called()
        self.assertEqual(slg_db.metrics_gap_rows(self.conn), [])


class RegressionTests(unittest.TestCase):
    """The RJ path and the western catalogue keep their previous behaviour."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = slg_db.connect(str(Path(self.temp.name) / 'db.sqlite'))
        self.addCleanup(self.conn.close)

    def test_rj_source_identity_still_works(self):
        rj_id, _ = slg_db.upsert_game(self.conn, RJ['slug'], RJ['url'], RJ['title'])
        self.assertEqual(slg_db.game_catalogues_bulk(self.conn, [rj_id])[rj_id],
                         ('dlsite',))
        self.assertEqual(self.conn.execute(
            'SELECT external_id FROM game_sources WHERE game_id=?', (rj_id,)
        ).fetchone()[0], 'RJ123456')
        slg_db.set_dlsite_metadata(self.conn, rj_id, {'rating_value': 3})
        self.assertEqual(slg_db.get_game_metadata_bulk(self.conn, [rj_id])[rj_id]
                         ['rating_value'], 3)

    def test_western_game_is_unaffected(self):
        west_id, _ = slg_db.upsert_game(self.conn, 'west', 'https://dikgames.com/x/',
                                        'Western', rating=6)
        self.assertEqual(slg_db.game_catalogues_bulk(self.conn, [west_id])[west_id],
                         ('western_slg',))
        self.assertEqual(slg_db.get_game(self.conn, west_id)['rating'], 6)
        self.assertEqual(self.conn.execute(
            'SELECT count(*) FROM game_sources WHERE game_id=?', (west_id,)).fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
