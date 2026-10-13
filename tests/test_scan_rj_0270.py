"""Offline RJ identity and registered-launch quick checks; no real user data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import slg_db
import slg_scan
import slg_scan_identity


class ScanRJTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def game(self, slug, title='Shared Title', rj=None):
        gid, _ = slg_db.upsert_game(self.conn, slug, 'https://example.invalid/' + slug, title)
        if rj:
            self.conn.execute("INSERT INTO game_sources(source_id,external_id,game_id,fetched_at)"
                              " VALUES(?,?,?,datetime('now'))",
                              ('dlsite', rj, gid))
        self.conn.commit()
        return gid

    def folder(self, name):
        folder = self.root / name
        folder.mkdir()
        (folder / 'Game.exe').touch()
        return folder

    def scan(self, **kwargs):
        return slg_scan.scan(self.conn, roots=[self.root], log=lambda _: None, **kwargs)

    def test_bounded_extraction_before_annotation_stripping(self):
        self.assertEqual(slg_scan_identity.product_ids('Title [rj00123456] RJ00123456 VJ123456'),
                         ['RJ00123456', 'VJ123456'])
        for name in ('RJ12345', 'RJ12345678901', 'xRJ123456', 'RJ123456x'):
            self.assertEqual(slg_scan_identity.product_ids(name), [])

    def test_rj_exact_beats_cross_source_namesake_and_repeat_is_unique(self):
        self.game('western')
        gid = self.game('dlsite', rj='RJ00123456')
        self.folder('Shared Title [rj00123456]')
        item = self.scan()['items'][0]
        self.assertEqual((item['game_id'], item['match_method']), (gid, 'product_id'))
        self.scan()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local').fetchone()[0], 1)

    def test_unknown_number_does_not_fallback_to_title(self):
        self.game('western')
        self.folder('Shared Title [RJ99887766]')
        item = self.scan()['items'][0]
        self.assertIsNone(item['game_id'])
        self.assertEqual(item['reason'], 'unknown_product_id')
        self.assertTrue(item['identity_warning'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local').fetchone()[0], 0)

    def test_vj_not_title_matched(self):
        self.game('western')
        self.folder('Shared Title VJ123456')
        item = self.scan()['items'][0]
        self.assertEqual(item['reason'], 'unsupported_product_id')
        self.assertIsNone(item['game_id'])

    def test_multiple_numbers_always_require_confirmation(self):
        self.game('dlsite', rj='RJ00123456')
        name = 'RJ00123456 RJ99887766'
        self.folder(name)
        item = self.scan()['items'][0]
        self.assertEqual(item['status'], 'ambiguous')
        self.assertIsNone(slg_scan.match_game(self.conn, name))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local').fetchone()[0], 0)

    def test_binding_conflict_is_not_written_and_both_choices_visible(self):
        original = self.game('original', title='Original')
        dl = self.game('dlsite', rj='RJ00123456')
        folder = self.folder('RJ00123456')
        slg_db.set_local_folder(self.conn, original, str(folder), 'old')
        self.conn.commit()
        item = self.scan()['items'][0]
        self.assertEqual(item['status'], 'ambiguous')
        self.assertEqual([c['game_id'] for c in item['candidates']], [original, dl])
        self.assertTrue(item['identity_warning'])
        self.assertEqual(self.conn.execute('SELECT folder_version FROM local').fetchone()[0], 'old')

    def test_valid_registered_launch_quick_check_and_force_refresh(self):
        gid = slg_db.add_user_game(self.conn, 'Saved Game', engine='Manual Engine')
        folder = self.folder('Saved Game')
        slg_db.set_local_folder(self.conn, gid, str(folder))
        slg_db.set_local_exe(self.conn, gid, str(folder / 'Game.exe'))
        self.conn.commit()
        with patch('slg_game_detection.detect_game', side_effect=AssertionError('must not walk')):
            item = self.scan(apply=False)['items'][0]
        self.assertEqual(item['detection_mode'], 'quick_check')
        self.assertEqual(item['engine'], 'Manual Engine')
        with patch('slg_game_detection.detect_game', return_value={
                'executables': [], 'engine': 'Unity', 'warnings': []}) as detect:
            item = self.scan(force_redetect=True, apply=False)['items'][0]
        detect.assert_called_once()
        self.assertEqual(item['detection_mode'], 'full')
        self.assertEqual(self.conn.execute('SELECT engine FROM games WHERE id=?', (gid,)).fetchone()[0],
                         'Manual Engine')

    def test_invalid_or_outside_launch_uses_full_detection(self):
        gid = self.game('saved', title='Saved Game')
        folder = self.folder('Saved Game')
        outside = self.root / 'outside.exe'
        outside.touch()
        slg_db.set_local_folder(self.conn, gid, str(folder))
        for exe in (outside, folder / 'gone.exe'):
            slg_db.set_local_exe(self.conn, gid, str(exe))
            self.assertEqual(self.scan(apply=False)['items'][0]['detection_mode'], 'full')

    def test_source_index_is_read_once_and_cancel_does_not_bind(self):
        for n in range(3):
            self.game('dl' + str(n), rj='RJ{:06}'.format(n))
            self.folder('RJ{:06}'.format(n))
        with patch.object(slg_scan_identity, 'product_index', wraps=slg_scan_identity.product_index) as index:
            stopped = [False]
            result = self.scan(on_progress=lambda *_: stopped.__setitem__(0, True),
                               should_stop=lambda: stopped[0])
        index.assert_called_once()
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
