import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import slg_db
import slg_library_cover_candidates as covers
import slg_local_library as library


def candidate(source='vndb', external='v27519'):
    return dict(source=source, external_id=external, title='多娜多娜', developer='ALICESOFT',
                source_url='https://vndb.org/' + external if source == 'vndb' else
                'https://www.dlsite.com/maniax/work/=/product_id/' + external + '.html',
                cover_url='https://t.vndb.org/cv/12/123.jpg' if source == 'vndb' else
                'https://img.dlsite.jp/modpub/images2/work/' + external + '_img_main.jpg')


def image_bytes():
    output = io.BytesIO()
    with Image.new('RGB', (400, 600), 'red') as image:
        image.save(output, 'PNG')
    return output.getvalue()


class CoverCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = slg_db.connect(str(self.root / 'fixture.sqlite'))
        self.addCleanup(self.conn.close)
        self.gid = slg_db.add_user_game(self.conn, 'Existing Title', engine='Unity')
        self.conn.commit()
        self.slug = self.conn.execute('SELECT slug FROM games WHERE id=?', (self.gid,)).fetchone()[0]

    def prepare(self):
        with patch.object(covers.providers, 'details', return_value=candidate()), \
             patch.object(covers.providers, 'download_cover', return_value=image_bytes()):
            prepared = covers.prepare(candidate())
        self.addCleanup(prepared.close)
        return prepared

    def test_preview_never_writes_files_or_metadata(self):
        before = tuple(self.root.iterdir())
        prepared = self.prepare()
        self.assertEqual(prepared.image.size, (400, 600))
        self.assertEqual(prepared.preview.size, (160, 240))
        self.assertEqual(tuple(self.root.iterdir()), before)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM personal_game_metadata').fetchone()[0], 0)
        prepared.close()
        prepared.close()

    def test_save_only_changes_personal_cover(self):
        before = dict(self.conn.execute('SELECT * FROM games WHERE id=?', (self.gid,)).fetchone())
        prepared = self.prepare()
        saved = covers.save_image(prepared.image, self.root / 'covers')
        covers.apply(self.conn, self.gid, self.slug, saved)
        self.conn.commit()
        saved.retained = True
        saved.discard()
        self.assertTrue(Path(saved.path).exists())
        after = dict(self.conn.execute('SELECT * FROM games WHERE id=?', (self.gid,)).fetchone())
        self.assertEqual(before, after)
        rows = library.apply_cover_overrides(self.conn, [after], self.root / 'covers')
        self.assertTrue(rows[0]['has_cover_override'])
        self.assertEqual(rows[0]['cover_file'], saved.name)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM personal_game_metadata').fetchone()[0], 0)
        library.clear_cover_override(self.conn, self.gid)
        self.conn.commit()
        self.assertFalse(library.apply_cover_overrides(self.conn, [after], self.root / 'covers')[0]['has_cover_override'])

    def test_identity_mismatch_never_downloads(self):
        with patch.object(covers.providers, 'details', return_value=candidate(external='v99')), \
             patch.object(covers.providers, 'download_cover') as download:
            with self.assertRaisesRegex(ValueError, '编号不一致'):
                covers.prepare(candidate())
            download.assert_not_called()

    def test_save_failure_keeps_old_cover(self):
        library.set_cover_override(self.conn, self.gid, 'old.jpg')
        self.conn.commit()
        prepared = self.prepare()
        saved = covers.save_image(prepared.image, self.root / 'covers')
        with patch.object(library, 'set_cover_override', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                covers.apply(self.conn, self.gid, self.slug, saved)
        self.conn.rollback()
        saved.discard()
        self.assertFalse(Path(saved.path).exists())
        self.assertEqual(self.conn.execute('SELECT cover_file FROM game_cover_overrides WHERE game_id=?',
                                         (self.gid,)).fetchone()[0], 'old.jpg')

    def test_closed_or_replaced_game_discard(self):
        prepared = self.prepare()
        saved = covers.save_image(prepared.image, self.root / 'covers')
        with self.assertRaises(ValueError):
            covers.apply(self.conn, self.gid, 'different-slug', saved)
        saved.discard()
        self.assertFalse(Path(saved.path).exists())

    def test_discard_does_not_delete_changed_file(self):
        prepared = self.prepare()
        saved = covers.save_image(prepared.image, self.root / 'covers')
        Path(saved.path).write_bytes(b'replacement')
        saved.discard()
        self.assertEqual(Path(saved.path).read_bytes(), b'replacement')

    def test_search_validates_source_and_deduplicates(self):
        with patch.object(covers.providers, 'search', return_value=[candidate(), candidate()]):
            self.assertEqual(len(covers.search('vndb', 'test')), 1)
            with self.assertRaises(ValueError):
                covers.search('dlsite', 'test')

    def test_decode_failure_no_cache_or_override(self):
        with patch.object(covers.providers, 'details', return_value=candidate()), \
             patch.object(covers.providers, 'download_cover', return_value=b'bad png'):
            with self.assertRaises(ValueError):
                covers.prepare(candidate())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)

    def test_no_cover_and_cancel_are_visible(self):
        facts = candidate()
        facts['cover_url'] = ''
        with patch.object(covers.providers, 'details', return_value=facts):
            with self.assertRaisesRegex(ValueError, '没有可读取'):
                covers.prepare(candidate())
        with patch.object(covers.providers, 'details', return_value=candidate()), \
             patch.object(covers.providers, 'download_cover', return_value=image_bytes()):
            with self.assertRaisesRegex(ValueError, '取消'):
                covers.prepare(candidate(), should_stop=lambda: True)

    def test_default_query_prefers_confirmed_identity(self):
        self.assertEqual(covers.default_query(self.conn, dict(id=self.gid, title='Title')), ('vndb', 'Title'))
        self.conn.execute("INSERT INTO game_sources(source_id,external_id,game_id,fetched_at) VALUES"
                          "('dlsite','RJ123456',?,datetime('now'))", (self.gid,))
        self.assertEqual(covers.default_query(self.conn, dict(id=self.gid, title='Title')), ('dlsite', 'RJ123456'))


if __name__ == '__main__':
    unittest.main()
