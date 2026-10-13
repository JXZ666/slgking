"""Offline integration of discover previews with the real scanner and temp DB."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import slg_db
import slg_game_detection
import slg_game_roots
import slg_scan
import slg_scan_targets


class DiscoverScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = slg_db.connect(':memory:')
        self.addCleanup(self.conn.close)

    def files(self, *names):
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def targets(self, *paths):
        return [{'path': str(path), 'mode': 'discover', 'depth': 4} for path in (paths or (self.root,))]

    def scan(self, **kwargs):
        return slg_scan.scan(self.conn, targets=kwargs.pop('targets', self.targets()),
                             log=lambda message: None, **kwargs)

    def test_collection_preview_is_per_root_and_read_only(self):
        self.files('Alpha/Game.exe', 'Alpha/Game.pck', 'Beta/Game.exe', 'Beta/Game.pck')
        gids = [slg_db.add_user_game(self.conn, title) for title in ('Alpha', 'Beta')]
        self.conn.commit()
        before = self.conn.total_changes
        result = self.scan(apply=False)
        self.assertEqual(result['matched'], 2)
        self.assertEqual({Path(item['path']).name for item in result['items']}, {'Alpha', 'Beta'})
        self.assertEqual({item['game_id'] for item in result['items']}, set(gids))
        self.assertTrue(all(item['detection_mode'] == 'discovered_root' for item in result['items']))
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM local').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM state').fetchone()[0], 0)

    def test_cancellation_after_match_never_applies(self):
        self.files('Alpha/Game.exe', 'Alpha/Game.pck', 'Beta/Game.exe', 'Beta/Game.pck')
        for title in ('Alpha', 'Beta'):
            slg_db.add_user_game(self.conn, title)
        self.conn.commit()
        stopped = False
        def progress(name, gid):
            nonlocal stopped
            stopped = True
        result = self.scan(apply=True, should_stop=lambda: stopped, on_progress=progress)
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM local').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM state').fetchone()[0], 0)

    def test_unknown_fallback_remains_available_for_manual_add(self):
        self.files('Independent.exe')
        result = self.scan(apply=False)
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['path'], str(self.root))
        self.assertEqual(item['status'], 'unmatched')
        self.assertEqual(item['engine'], '')
        self.assertEqual(item['executables'], [str(self.root / 'Independent.exe')])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0], 0)
        # Explicit confirmation can use the existing manual-add API.
        gid = slg_db.add_user_game(self.conn, 'Independent', folder_path=item['path'])
        self.assertEqual(self.conn.execute('SELECT folder_path FROM local WHERE game_id=?', (gid,)).fetchone()[0], str(self.root))

    def test_overlapping_and_repeated_targets_do_not_duplicate_items(self):
        self.files('Alpha/Game.exe', 'Alpha/Game.pck')
        plans = self.targets(self.root, self.root, self.root / 'Alpha')
        self.assertEqual(len(slg_scan_targets.normalize_targets(plans)), 2)
        result = self.scan(targets=plans, apply=False)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['path'], str(self.root / 'Alpha'))

    def test_variant_exposed_and_manual_engine_preserved(self):
        self.files('Alpha/Game.exe', 'Alpha/Game.ini', 'Alpha/Game.rgss3a')
        gid = slg_db.add_user_game(self.conn, 'Alpha', engine='Manual Engine')
        self.conn.commit()
        result = self.scan(apply=True)
        item = result['items'][0]
        self.assertEqual((item['engine'], item['engine_variant']), ('RPG Maker', 'VX Ace'))
        self.assertEqual(self.conn.execute('SELECT engine FROM games WHERE id=?', (gid,)).fetchone()[0], 'Manual Engine')
        self.assertEqual(self.conn.execute('SELECT folder_path FROM local WHERE game_id=?', (gid,)).fetchone()[0], str(self.root / 'Alpha'))

    def test_partial_discovery_warning_is_retained_for_confirmation_ui(self):
        with patch.object(slg_game_roots, 'discover_game_roots', return_value={
                'roots': [], 'fallback': None, 'cancelled': False, 'warnings': ['entry_limit: 5000']}):
            result = self.scan(apply=False)
        self.assertIn('entry_limit', result['warnings'][0])
        self.assertIn('缩小扫描范围', result['warnings'][0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM local').fetchone()[0], 0)

    def test_registered_manual_engine_is_separate_from_detected_variant(self):
        self.files('Alpha/Game.exe', 'Alpha/Game.ini', 'Alpha/Game.rgss3a')
        gid = slg_db.add_user_game(self.conn, 'Alpha', engine='Manual Engine', folder_path=str(self.root/'Alpha'))
        slg_db.set_local_exe(self.conn, gid, str(self.root/'Alpha/Game.exe'))
        self.conn.commit()
        item = self.scan(apply=False)['items'][0]
        self.assertEqual(item['engine'], 'Manual Engine')
        self.assertEqual((item['detected_engine'], item['engine_variant']), ('RPG Maker', 'VX Ace'))

    def test_candidate_inspection_reuses_single_inventory(self):
        self.files('Alpha/Game.exe', 'Alpha/Game.pck', 'Beta/Game.exe', 'Beta/Game.pck')
        original = os.scandir
        visits = []
        def counted(path):
            visits.append(str(path))
            return original(path)
        with patch.object(slg_game_roots, 'detect_game', wraps=slg_game_detection.detect_game) as detect, \
             patch.object(slg_game_detection.os, 'scandir', side_effect=counted):
            result = self.scan(apply=False)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(detect.call_count, 1)
        self.assertEqual(len(visits), 3)
        self.assertEqual(len(visits), len(set(visits)))

    def test_discover_depth_validation_and_boundary(self):
        plans = [{'path': str(self.root), 'mode': 'discover', 'depth': 10}]
        self.assertEqual(slg_scan_targets.normalize_plans(plans)[0]['depth'], 10)
        for depth in (0, 11, True):
            with self.subTest(depth=depth), self.assertRaises(ValueError):
                slg_scan_targets.normalize_targets([{**plans[0], 'depth': depth}])

    def test_title_only_unknown_fallback_reaches_scanner(self):
        (self.root / 'Game.ini').write_text('[Game]\nTitle=Unlisted Custom Game\n', encoding='utf-8')
        result = self.scan(apply=False)
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['status'], 'unmatched')
        self.assertEqual(item['evidence']['titles'][0]['text'], 'Unlisted Custom Game')
        self.assertFalse(item['executables'])


if __name__ == '__main__':
    unittest.main()
