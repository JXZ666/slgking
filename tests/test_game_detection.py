import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import slg_game_detection as detection


class GameDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def files(self, *names):
        for name in names:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

    def result(self):
        return detection.detect_game(self.root)

    def test_explicit_engine_fixtures(self):
        fixtures = {
            "Ren'Py": ('renpy/__init__.py', 'game/script.rpyc', 'Game.exe'),
            'Unity': ('UnityPlayer.dll', 'Game.exe', 'Game_Data/globalgamemanagers'),
            'Unreal Engine': ('Project/Binaries/Win64/Game-Win64-Shipping.exe', 'Project/Content/Paks/Game.pak'),
            'RPG Maker': ('www/index.html', 'www/js/rmmz_core.js', 'www/data/System.json', 'Game.exe'),
            'Godot': ('Game.exe', 'Game.pck'),
            'TyranoBuilder': ('index.html', 'tyrano/tyrano.js', 'data/system/config.tjs'),
            'Kirikiri': ('krkr.exe', 'data.xp3'),
            'HTML': ('index.html',),
        }
        for engine, paths in fixtures.items():
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder:
                for name in paths:
                    target = Path(folder) / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
                result = detection.detect_game(folder)
                self.assertEqual(result['engine'], engine)
                self.assertEqual(result['engines'], [engine])
                self.assertTrue(result['evidence'])
                self.assertEqual(detection.detect_engine(folder), engine)

    def test_false_positive_markers(self):
        self.files('Renpy/readme.txt', 'script.rpy', 'UnityPlayer.dll', 'Game.exe',
                   'Game_Data/readme.txt', 'Unrelated.pck', 'data.xp3', 'Game.ini',
                   'Project/Binaries/Win64/readme.txt', 'Project/Content/Paks/readme.txt',
                   'www/readme.txt', 'js/rpg_core.js')
        self.assertEqual(self.result()['engine'], '')
        self.assertEqual(self.result()['engines'], [])

    def test_data_name_alone_is_not_unity(self):
        (self.root / 'Game_Data').mkdir()
        self.files('Game.exe')
        self.assertEqual(self.result()['engine'], '')

    def test_ambiguity_never_selects_first(self):
        self.files('UnityPlayer.dll', 'Game.exe', 'Game_Data/data.unity3d',
                   'renpy/__init__.py', 'game/script.rpa')
        result = self.result()
        self.assertEqual(result['engine'], '')
        self.assertEqual(result['engines'], ["Ren'Py", 'Unity'])
        self.assertIn('ambiguous_engines: user selection required', result['warnings'])
        self.assertEqual(detection.detect_engine(self.root), '')

    def test_multiple_launchers_retained_helpers_excluded(self):
        self.files('Game.exe', 'Launcher.exe', 'unins000.exe', 'UnityCrashHandler64.exe',
                   'UECrashReportClient.exe', 'setup.exe', 'index.html')
        result = self.result()
        self.assertEqual([Path(p).name for p in result['executables']], ['Game.exe', 'Launcher.exe'])
        self.assertIn('multiple_executables: user selection required', result['warnings'])

    def test_nested_unreal_launch_candidate(self):
        self.files('Project/Binaries/Win64/Project-Win64-Shipping.exe',
                   'Project/Content/Paks/Project-Windows.pak')
        result = self.result()
        self.assertEqual(result['engine'], 'Unreal Engine')
        self.assertEqual(result['executables'], [str(self.root / 'Project/Binaries/Win64/Project-Win64-Shipping.exe')])

    def test_rgss_and_godot_source_combinations(self):
        self.files('Game.ini', 'Game.rgss3a', 'Game.exe')
        self.assertEqual(self.result()['engine'], 'RPG Maker')
        self.files('project.godot', 'start.tscn')
        self.assertEqual(self.result()['engines'], ['RPG Maker', 'Godot'])

    def test_cancellation_discards_partial_suggestions(self):
        self.files('Game.exe', 'index.html')
        calls = 0
        def stop():
            nonlocal calls
            calls += 1
            return calls >= 3
        result = detection.detect_game(self.root, stop)
        self.assertEqual(result['engine'], '')
        self.assertEqual(result['executables'], [])
        self.assertIn('cancelled', result['warnings'])
        self.assertLessEqual(calls, 3)

    def test_entry_limit(self):
        self.files(*('file{}.txt'.format(i) for i in range(12)))
        with patch.object(detection, 'MAX_ENTRIES', 5):
            result = self.result()
        self.assertIn('entry_limit: 5', result['warnings'])

    def test_depth_limit(self):
        self.files('a/b/c/d/index.html')
        result = self.result()
        self.assertEqual(result['engine'], '')
        self.assertIn('depth_limit: 4', result['warnings'])
        self.assertFalse(result['evidence'])

    def test_depth_boundary_keeps_explicit_engine(self):
        self.files('UnityPlayer.dll', 'Game.exe', 'Game_Data/globalgamemanagers',
                   'assets/a/b/c/deep.txt')
        result = self.result()
        self.assertEqual(result['engine'], 'Unity')
        self.assertIn('depth_limit: 4', result['warnings'])

    def test_partial_entry_limit_requires_user_selection(self):
        self.files('index.html', 'Game.exe')
        with patch.object(detection, 'MAX_ENTRIES', 1):
            result = self.result()
        self.assertEqual(result['engine'], '')
        self.assertIn('entry_limit: 1', result['warnings'])

    def test_symlink_root_without_host_privileges(self):
        with patch.object(detection.Path, 'is_symlink', return_value=True):
            result = self.result()
        self.assertEqual(result['warnings'], ['symlink_skipped: root'])
        self.assertFalse(result['evidence'])

    def test_child_directory_disappearing_keeps_warning_and_evidence(self):
        self.files('index.html', 'child/readme.txt')
        scandir = os.scandir
        def vanished(path):
            if Path(path).name == 'child':
                raise FileNotFoundError('fixture')
            return scandir(path)
        with patch.object(detection.os, 'scandir', side_effect=vanished):
            result = self.result()
        self.assertIn('unreadable: child (FileNotFoundError)', result['warnings'])
        self.assertEqual(result['engine'], '')
        self.assertEqual(result['engines'], ['HTML'])

    def test_cache_is_skipped(self):
        self.files('cache/index.html', 'node_modules/demo/Game.exe', '.git/index.html')
        self.assertFalse(self.result()['evidence'])
        self.assertFalse(self.result()['executables'])

    def test_symlink_is_skipped(self):
        self.files('real/index.html')
        try:
            (self.root / 'link').symlink_to(self.root / 'real', target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit directory symlinks')
        result = detection.detect_game(self.root / 'link')
        self.assertEqual(result['warnings'], ['symlink_skipped: root'])
        self.assertFalse(result['evidence'])

    def test_permission_failure_reports_warning(self):
        with patch.object(detection.os, 'scandir', side_effect=PermissionError('fixture')):
            result = self.result()
        self.assertEqual(result['engine'], '')
        self.assertIn('unreadable: root (PermissionError)', result['warnings'])

    def test_vanished_directory_reports_warning(self):
        missing = self.root / 'vanished'
        result = detection.detect_game(missing)
        self.assertIn('unreadable: root (FileNotFoundError)', result['warnings'])
        self.assertEqual(result['engine'], '')

    def test_case_insensitive_paths(self):
        self.files('UNITYPLAYER.DLL', 'GAME.EXE', 'GAME_DATA/GLOBALGAMEMANAGERS')
        self.assertEqual(self.result()['engine'], 'Unity')


if __name__ == '__main__':
    unittest.main()
