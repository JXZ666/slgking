"""Personal binding ownership and redirected folder identity regressions."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import slg_db
import slg_scan


class Client0260Data(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = slg_db.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.gid = slg_db.add_user_game(self.conn, 'Custom')
        self.a, self.b = (Path(self.tmp.name) / name for name in ('A', 'B'))
        self.a.mkdir()
        self.b.mkdir()
        self.exe_a = self.a / 'game.exe'
        self.exe_b = self.b / 'game.exe'
        self.exe_a.touch()
        self.exe_b.touch()

    def exe(self):
        return self.conn.execute('SELECT exe_path FROM local WHERE game_id=?', (self.gid,)).fetchone()[0]

    def test_exe_only_binding_new_folder_clears_unrelated_exe(self):
        slg_db.set_local_exe(self.conn, self.gid, str(self.exe_a))
        slg_db.set_local_folder(self.conn, self.gid, str(self.b))
        self.assertIsNone(self.exe())

    def test_exe_only_binding_preserves_nested_existing_exe(self):
        nested = self.b / 'bin'
        nested.mkdir()
        exe = nested / 'play.exe'
        exe.touch()
        slg_db.set_local_exe(self.conn, self.gid, str(exe))
        slg_db.set_local_folder(self.conn, self.gid, str(self.b))
        self.assertEqual(self.exe(), str(exe))

    def test_folder_change_preserves_exe_already_inside_new_folder(self):
        slg_db.set_local_folder(self.conn, self.gid, str(self.a))
        slg_db.set_local_exe(self.conn, self.gid, str(self.exe_b))
        slg_db.set_local_folder(self.conn, self.gid, str(self.b))
        self.assertEqual(self.exe(), str(self.exe_b))

    def test_prefix_sibling_is_not_inside_folder(self):
        sibling = Path(str(self.b) + '-other')
        sibling.mkdir()
        exe = sibling / 'game.exe'
        exe.touch()
        slg_db.set_local_exe(self.conn, self.gid, str(exe))
        slg_db.set_local_folder(self.conn, self.gid, str(self.b))
        self.assertIsNone(self.exe())

    @unittest.skipUnless(os.name == 'nt', 'Windows case normalization')
    def test_case_and_dot_segments_keep_same_directory_binding(self):
        slg_db.set_local_folder(self.conn, self.gid, str(self.b))
        slg_db.set_local_exe(self.conn, self.gid, str(self.exe_b))
        slg_db.set_local_folder(self.conn, self.gid, str(self.b).swapcase() + '/./')
        self.assertEqual(self.exe(), str(self.exe_b))

    def test_symlink_ownership_resolves_target(self):
        link = self.b / 'escape'
        try:
            link.symlink_to(self.a, target_is_directory=True)
        except OSError as exc:
            self.skipTest('Host does not permit symlinks: ' + str(exc))
        slg_db.set_local_exe(self.conn, self.gid, str(link / 'game.exe'))
        slg_db.set_local_folder(self.conn, self.gid, str(self.b))
        self.assertIsNone(self.exe())

    def test_resolved_link_escape_is_rejected_without_host_link_privilege(self):
        virtual = self.b / 'escape' / 'game.exe'
        realpath = os.path.realpath
        def resolved(path):
            return str(self.exe_a) if os.path.normcase(path) == os.path.normcase(str(virtual)) else realpath(path)
        slg_db.set_local_exe(self.conn, self.gid, str(virtual))
        with patch.object(slg_db.os.path, 'isfile', return_value=True), patch.object(slg_db.os.path, 'realpath', side_effect=resolved):
            slg_db.set_local_folder(self.conn, self.gid, str(self.b))
        self.assertIsNone(self.exe())

    def test_legacy_applied_scan_cannot_keep_exe_from_unrelated_folder(self):
        folder = Path(self.tmp.name) / 'Custom'
        folder.mkdir()
        slg_db.set_local_exe(self.conn, self.gid, str(self.exe_a))
        result = slg_scan.scan(self.conn, [str(folder)], mode='single', log=lambda _: None)
        self.assertEqual(result['matched'], 1)
        self.assertIsNone(self.exe())

    def test_active_redirect_names_aliases_and_translations_are_canonical(self):
        old, _ = slg_db.upsert_game(self.conn, 'old', 'https://example.invalid/old', 'Old Name')
        new, _ = slg_db.upsert_game(self.conn, 'canonical', 'https://example.invalid/new', 'New Name')
        slg_db.add_alias(self.conn, old, 'Historical Alias')
        slg_db.set_manual_translation(self.conn, 'title', old, '历史名称')
        self.conn.execute('INSERT INTO catalog_redirects VALUES (?,?,1)', ('old', 'canonical'))
        for name in ('Old Name', 'New Name', 'Historical Alias', '历史名称'):
            with self.subTest(name=name):
                self.assertEqual(slg_scan.match_game(self.conn, name), new)
        self.assertNotIn(old, [gid for _, gid, _ in slg_scan._title_index(self.conn)])
        self.assertEqual(slg_scan._title_index_titles(slg_scan._title_index(self.conn))[new], 'New Name')
        folder = Path(self.tmp.name) / 'Historical Alias'
        folder.mkdir()
        (folder / 'game.exe').touch()
        result = slg_scan.scan(self.conn, [str(folder)], mode='single', apply=False, log=lambda _: None)
        self.assertEqual(result['items'][0]['game_id'], new)
        self.assertEqual(result['matched'], 1)

    def test_inactive_conflicting_redirect_keeps_old_identity(self):
        old, _ = slg_db.upsert_game(self.conn, 'old', 'https://example.invalid/old', 'Old Name')
        slg_db.upsert_game(self.conn, 'canonical', 'https://example.invalid/new', 'New Name')
        self.conn.execute('INSERT INTO catalog_redirects VALUES (?,?,0)', ('old', 'canonical'))
        self.assertEqual(slg_scan.match_game(self.conn, 'Old Name'), old)
