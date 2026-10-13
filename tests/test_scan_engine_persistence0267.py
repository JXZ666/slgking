"""Run the actual confirmation handler against real, offline scan results."""
import ast
from contextlib import contextmanager
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import slg_db
import slg_scan


def confirmation_handler(scope):
    source = Path(__file__).resolve().parents[1] / 'slg_library_ui.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LibraryUI')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == 'open_library_scan_results')
    handler = next(n for n in method.body if isinstance(n, ast.FunctionDef) and n.name == 'apply')
    exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                 str(source), 'exec'), scope)
    return scope['apply']


class ScanEnginePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'My Game'
        self.root.mkdir()
        self.conn = slg_db.connect(str(Path(self.temp.name) / 'audit.db'))
        self.addCleanup(self.conn.close)

    def files(self, names):
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def confirm(self, *, new=True):
        result = slg_scan.scan(self.conn, roots=[str(self.root)], mode='single',
                               apply=False, log=lambda _: None)
        item = result['items'][0]
        if new:
            item['new'] = True
        state = dict(item=item, var=SimpleNamespace(get=lambda: True), exe=None)
        errors = []
        scope = dict(identity=1, states=[state], index=[1], items=[item],
                     self=SimpleNamespace(_library_identity=lambda: 1,
                                          _require_personal_access=lambda _: True,
                                          _library_personal_changed=lambda: None,
                                          refresh=lambda: None),
                     status=SimpleNamespace(configure=lambda **kw: errors.append(kw)),
                     win=SimpleNamespace(destroy=lambda: None),
                     slg_db=slg_db, slg_scan=slg_scan, os=os)
        @contextmanager
        def session():
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
        with patch.object(slg_db, 'session', session):
            confirmation_handler(scope)()
        self.assertEqual(errors, [])
        return item

    def test_new_unity_keeps_confident_detection(self):
        self.files(('Game.exe', 'UnityPlayer.dll', 'Game_Data/globalgamemanagers'))
        self.assertEqual(self.confirm()['engine'], 'Unity')
        self.assertEqual(self.conn.execute("SELECT engine FROM games WHERE origin='user'").fetchone()[0], 'Unity')

    def test_new_unreal_keeps_confident_detection(self):
        self.files(('Project/Binaries/Win64/Game-Win64-Shipping.exe',
                    'Project/Content/Paks/Game.pak'))
        self.assertEqual(self.confirm()['engine'], 'Unreal Engine')
        self.assertEqual(self.conn.execute("SELECT engine FROM games WHERE origin='user'").fetchone()[0], 'Unreal Engine')

    def test_new_unknown_remains_unknown(self):
        self.files(('Game.exe',))
        self.assertEqual(self.confirm()['engine'], '')
        self.assertIsNone(self.conn.execute("SELECT engine FROM games WHERE origin='user'").fetchone()[0])

    def test_existing_public_and_manually_selected_engines_are_preserved(self):
        self.files(('Game.exe', 'UnityPlayer.dll', 'Game_Data/globalgamemanagers'))
        for origin, engine in (('site', 'Public Fact'), ('user', 'Manual Engine')):
            with self.subTest(origin=origin):
                self.conn.execute('DELETE FROM games')
                if origin == 'site':
                    gid, _ = slg_db.upsert_game(self.conn, 'existing',
                        'https://example.invalid/existing', self.root.name)
                    self.conn.execute('UPDATE games SET engine=? WHERE id=?', (engine, gid))
                else:
                    gid = slg_db.add_user_game(self.conn, self.root.name, engine=engine)
                self.conn.commit()
                item = self.confirm(new=False)
                self.assertEqual(item['game_id'], gid)
                self.assertEqual(item['engine'], 'Unity')
                self.assertEqual(self.conn.execute('SELECT engine FROM games WHERE id=?', (gid,)).fetchone()[0], engine)


if __name__ == '__main__':
    unittest.main()
