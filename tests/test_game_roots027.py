import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import slg_game_detection as detection
from slg_game_roots import discover_game_roots
from slg_scan_evidence import extract_evidence


class RootDetection027Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def files(self, *names):
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def test_rpg_archive_variants(self):
        for suffix, variant in [('rgssad', 'XP'), ('rgss2a', 'VX'), ('rgss3a', 'VX Ace')]:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                for name in ['Game.exe', 'Game.ini', 'Game.' + suffix]:
                    (Path(folder) / name).touch()
                result = detection.detect_game(folder)
                self.assertEqual((result['engine'], result['engine_variant']), ('RPG Maker', variant))

    def test_rpg_open_data_variants(self):
        for suffix, variant, dll in [('rxdata', 'XP', 'RGSS102E.dll'), ('rvdata', 'VX', 'RGSS202E.dll'), ('rvdata2', 'VX Ace', 'RGSS301.dll')]:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                (base / 'Data').mkdir()
                for name in ['Game.exe', 'Game.ini', dll, 'Data/Actors.' + suffix]:
                    (base / name).touch()
                result = detection.detect_game(base)
                self.assertEqual(result['engine_variant'], variant)

    def test_mv_mz_title_and_single_root(self):
        for core, variant in [('rpg_core.js', 'MV'), ('rmmz_core.js', 'MZ')]:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                for name in ['Game.exe', 'www/index.html', 'www/js/' + core, 'www/data/System.json']:
                    path = base / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                (base / 'www/data/System.json').write_text(json.dumps({'gameTitle': '中文游戏'}), encoding='utf-8')
                result = discover_game_roots(base)
                self.assertEqual(len(result['roots']), 1)
                item = result['roots'][0]
                self.assertEqual(item['path'], str(base))
                self.assertEqual(item['engine_variant'], variant)
                self.assertEqual(item['title_evidence'][0]['text'], '中文游戏')

    def test_unity_il2cpp_requires_metadata(self):
        self.files('Game.exe', 'UnityPlayer.dll', 'GameAssembly.dll', 'Game_Data/globalgamemanagers')
        self.assertEqual(detection.detect_game(self.root)['engine_variant'], '')
        self.files('Game_Data/il2cpp_data/Metadata/global-metadata.dat')
        self.assertEqual(detection.detect_game(self.root)['engine_variant'], 'IL2CPP')

    def test_additional_combination_engines(self):
        fixtures = {'WOLF': ['Game.exe', 'Game.ini', 'Data.wolf'],
                    'LiveMaker': ['Game.exe', 'live.dll', 'setting.ini', 'game.dat'],
                    'Bakin': ['BakinPlayer.exe', 'GameConfig.sbcf', 'libSkiaSharp.dll']}
        for engine, names in fixtures.items():
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder:
                for name in names:
                    (Path(folder) / name).touch()
                self.assertEqual(detection.detect_engine(folder), engine)

    def test_generic_library_and_weak_marker_not_engines(self):
        self.files('Game.exe', 'live.dll', 'BakinPlayer.dll', 'GameAssembly.dll', 'Data.wolf', 'Game_Data/readme.txt')
        self.assertEqual(detection.detect_engine(self.root), '')

    def test_collection_same_engine_does_not_choose_parent(self):
        self.files('one/Game.exe', 'one/Game.pck', 'two/Game.exe', 'two/Game.pck')
        result = detection.detect_game(self.root)
        self.assertEqual(result['engine'], '')
        roots = discover_game_roots(self.root)['roots']
        self.assertEqual({Path(item['path']).name for item in roots}, {'one', 'two'})
        self.assertTrue(all(len(item['executables']) == 1 for item in roots))

    def test_tools_loaders_ignored(self):
        self.files('Tools/loaders/Wolf/Game.exe', 'Tools/loaders/Wolf/Game.ini', 'Tools/loaders/Wolf/Data.wolf',
                   'real/Game.exe', 'real/Game.pck', 'real/OpenSaveFolder.exe')
        roots = discover_game_roots(self.root)['roots']
        self.assertEqual(len(roots), 1)
        self.assertEqual([Path(value).name for value in roots[0]['executables']], ['Game.exe'])

    def test_deep_root_configurable_limit(self):
        self.files('a/b/c/d/e/f/Game.exe', 'a/b/c/d/e/f/Game.pck')
        self.assertFalse(discover_game_roots(self.root)['roots'])
        self.assertEqual(len(discover_game_roots(self.root, max_depth=10)['roots']), 1)
        self.files('a/b/c/d/e/f/g/h/i/j/k/index.html')
        result = discover_game_roots(self.root, max_depth=100)
        self.assertTrue(any('depth_limit: 10' == warning for warning in result['warnings']))

    def test_global_enumeration_budget_no_rescans(self):
        self.files('a/Game.exe', 'a/Game.pck', 'b/Game.exe', 'b/Game.pck')
        import os
        original = os.scandir
        seen = []
        def tracked(path):
            seen.append(str(path))
            return original(path)
        with patch.object(detection.os, 'scandir', side_effect=tracked):
            result = discover_game_roots(self.root, max_entries=6)
        self.assertEqual(len(result['roots']), 2)
        self.assertEqual(len(seen), len(set(seen)))
        result = discover_game_roots(self.root, max_entries=3)
        self.assertIn('entry_limit: 3', result['warnings'])

    def test_root_cap(self):
        for i in range(3):
            self.files(str(i) + '/Game.exe', str(i) + '/Game.pck')
        result = discover_game_roots(self.root, max_roots=2)
        self.assertEqual(len(result['roots']), 2)
        self.assertIn('root_limit: 2', result['warnings'])

    def test_cancel_discards_preview(self):
        self.files('a/Game.exe', 'a/Game.pck')
        self.assertEqual(discover_game_roots(self.root, lambda: True)['roots'], [])
        self.assertTrue(discover_game_roots(self.root, lambda: True)['cancelled'])

    def test_junction_skipped(self):
        self.files('a/Game.exe', 'a/Game.pck')
        with patch.object(detection.os.path, 'isjunction', return_value=True, create=True):
            self.assertEqual(discover_game_roots(self.root)['roots'], [])

    def test_game_ini_multiencoding_not_confident(self):
        (self.root / 'Game.ini').write_bytes('[Game]\nTitle=多娜多娜\n'.encode('gb18030'))
        result = extract_evidence(self.root)
        self.assertTrue(result['titles'])
        self.assertTrue(all(item['ambiguous'] for item in result['titles']))

    def test_broken_metadata_and_truncated_executable_safe(self):
        self.files('Game.exe', 'Game.pck', 'data/System.json')
        (self.root / 'Game.exe').write_bytes(b'MZ')
        (self.root / 'data/System.json').write_text('{broken', encoding='utf-8')
        result = discover_game_roots(self.root)
        self.assertEqual(len(result['roots']), 1)
        self.assertTrue(result['roots'][0]['warnings'])

    def test_embedded_data_html_not_a_game_root(self):
        self.files('Game.exe', 'UnityPlayer.dll', 'Game_Data/globalgamemanagers', 'Game_Data/help/index.html')
        roots = discover_game_roots(self.root)['roots']
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]['engine'], 'Unity')

    def test_unknown_executable_fallback_reuses_enumeration(self):
        self.files('CustomGame.exe')
        import os
        original = os.scandir
        visits = []
        def tracked(path):
            visits.append(str(path))
            return original(path)
        with patch.object(detection.os, 'scandir', side_effect=tracked):
            result = discover_game_roots(self.root)
        self.assertEqual(result['roots'], [])
        self.assertEqual(result['fallback']['engine'], '')
        self.assertEqual(result['fallback']['path'], str(self.root))
        self.assertEqual(result['fallback']['executables'], [str(self.root / 'CustomGame.exe')])
        self.assertEqual(visits, [str(self.root)])

    def test_title_only_fallback(self):
        (self.root / 'Game.ini').write_text('[Game]\nTitle=Independent Game\n', encoding='utf-8')
        result = discover_game_roots(self.root)
        self.assertEqual(result['roots'], [])
        self.assertEqual(result['fallback']['executables'], [])
        self.assertEqual(result['fallback']['title_evidence'][0]['text'], 'Independent Game')

    def test_empty_root_has_no_fallback(self):
        self.assertIsNone(discover_game_roots(self.root)['fallback'])
        self.assertIsNone(discover_game_roots(self.root, lambda: True)['fallback'])

    def test_repeat_is_same_and_never_writes(self):
        self.files('Game.exe', 'Game.pck')
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.assertEqual(discover_game_roots(self.root), discover_game_roots(self.root))
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))


if __name__ == '__main__':
    unittest.main()
