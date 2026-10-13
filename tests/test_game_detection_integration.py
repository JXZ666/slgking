"""Integration contract for bounded detection through legacy scan entry points."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import slg_db
import slg_game_detection
import slg_scan


class EngineScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = slg_db.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.gid, _ = slg_db.upsert_game(self.conn, 'test', 'https://example.invalid/test', self.root.name)
        self.conn.commit()

    def files(self, *paths):
        for name in paths:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()

    def scan(self, **kwargs):
        return slg_scan.scan(self.conn, roots=[str(self.root)], mode='single', log=lambda _: None, **kwargs)

    def test_unencrypted_rpgmaker_xp_vx_vxace_packages(self):
        for extension in ('.rxdata', '.rvdata', '.rvdata2'):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as folder:
                for name in ('Game.exe', 'Game.ini', 'System/RGSS301.dll', 'Data/System' + extension):
                    p = Path(folder) / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch()
                self.assertEqual(slg_scan.detect_engine(folder), 'RPG Maker')

    def test_rgss_runtime_or_data_alone_does_not_identify(self):
        self.files('Game.exe', 'Game.ini', 'Data/System.rvdata2')
        self.assertEqual(slg_scan.detect_engine(self.root), '')
        self.files('System/RGSS301.dll')
        self.assertEqual(slg_scan.detect_engine(self.root), 'RPG Maker')

    def test_top_level_exes_precede_nested_exes_and_do_not_bind_automatically(self):
        self.files('ZGame.exe', 'aLauncher.exe', 'A/BGame.exe', 'unins000.exe')
        paths = slg_scan.executable_candidates(self.root)
        self.assertEqual([str(Path(p).relative_to(self.root)) for p in paths],
                         ['aLauncher.exe', 'ZGame.exe', str(Path('A/BGame.exe'))])
        self.assertEqual(paths, slg_scan.executable_candidates(self.root, entries=['ignored.exe']))
        result = self.scan()
        self.assertEqual(result['items'][0]['executables'], paths)
        row = self.conn.execute('SELECT exe_path FROM local WHERE game_id=?', (self.gid,)).fetchone()
        self.assertIsNone(row['exe_path'])

    def test_preview_exposes_unreal_nested_candidate_without_save_claim(self):
        self.files('Project/Binaries/Win64/Game-Win64-Shipping.exe', 'Project/Content/Paks/Game.pak')
        result = self.scan(apply=False)
        item = result['items'][0]
        self.assertEqual(item['engine'], 'Unreal Engine')
        self.assertEqual(len(item['executables']), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local').fetchone()[0], 0)

    def test_cancel_inside_detection_never_writes_scan_bindings(self):
        self.files('Game.exe', 'index.html')
        stopped = [False]
        calls = [0]
        def stop():
            calls[0] += 1
            if calls[0] >= 6:
                stopped[0] = True
            return stopped[0]
        result = self.scan(should_stop=stop)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['matched'], 0)
        self.assertEqual(result['items'], [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM local').fetchone()[0], 0)

    def test_scan_does_not_replace_manual_engine_or_executable(self):
        self.files('UnityPlayer.dll', 'Game.exe', 'Game_Data/globalgamemanagers')
        self.conn.execute('UPDATE games SET engine=? WHERE id=?', ('Custom Engine', self.gid))
        self.conn.execute('INSERT INTO local(game_id,folder_path,exe_path) VALUES(?,?,?)',
                          (self.gid, str(self.root), 'chosen.exe'))
        self.conn.commit()
        result = self.scan()
        self.assertEqual(result['items'][0]['engine'], 'Unity')
        self.assertEqual(self.conn.execute('SELECT engine FROM games WHERE id=?', (self.gid,)).fetchone()[0], 'Custom Engine')
        self.assertEqual(self.conn.execute('SELECT exe_path FROM local WHERE game_id=?', (self.gid,)).fetchone()[0], 'chosen.exe')

    def test_limit_warning_reaches_preview_without_engine_selection(self):
        self.files('index.html', 'Game.exe')
        with patch.object(slg_game_detection, 'MAX_ENTRIES', 1):
            item = self.scan(apply=False)['items'][0]
        self.assertEqual(item['engine'], '')
        self.assertIn('entry_limit: 1', item['detection_warnings'])


if __name__ == '__main__':
    unittest.main()
