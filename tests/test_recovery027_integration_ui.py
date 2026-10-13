"""Real App integration, isolated profile, no production/provider requests.

Run this module in its own process; CTk root/image lifetime is process-local.
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_profile = tempfile.TemporaryDirectory(prefix='slg-recovery-ui-')
_previous_local = os.environ.get('LOCALAPPDATA')
os.environ['LOCALAPPDATA'] = _profile.name

import customtkinter as ctk
import slg_db
import slg_gui
import slg_library_cover_candidates as covers
import slg_scan_targets


def widgets(parent):
    result = []
    pending = [parent]
    while pending:
        widget = pending.pop()
        result.append(widget)
        pending.extend(widget.winfo_children())
    return result


class RecoveryAppIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = slg_gui.App(notify=False)
        cls.access = patch.object(cls.app, '_require_personal_access', return_value=True)
        cls.access.start()
        cls.errors = []
        cls.app.report_callback_exception = lambda *args: cls.errors.append(args)
        gid = slg_db.add_user_game(cls.app.conn, 'Fixture Local Game', engine='Unity')
        cls.app.conn.commit()
        cls.game = dict(cls.app.conn.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone())
        cls.app.geometry('1180x900')
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.access.stop()
        cls.app.destroy()
        cls.app.conn.close()
        if _previous_local is None:
            os.environ.pop('LOCALAPPDATA', None)
        else:
            os.environ['LOCALAPPDATA'] = _previous_local
        _profile.cleanup()

    def tearDown(self):
        self.app._network_enabled = False
        for child in list(self.app.winfo_children()):
            if isinstance(child, ctk.CTkToplevel) and child.winfo_exists():
                child.destroy()
        self.app.update()
        self.assertFalse(self.errors)

    def button(self, parent, text):
        return next(w for w in widgets(parent)
                    if isinstance(w, ctk.CTkButton) and w.cget('text') == text)

    def wait(self, condition):
        deadline = time.monotonic() + 3
        while not condition():
            self.app.update()
            if time.monotonic() > deadline:
                self.fail('GUI worker timed out')
            time.sleep(.01)

    def test_editor_online_entry_no_request_on_open_in_both_themes(self):
        with patch.object(covers.providers, 'search') as search, \
             patch.object(covers.providers, 'details') as details, \
             patch.object(covers.providers, 'download_cover') as download:
            for theme in ('light', 'dark'):
                self.app._apply_theme(theme)
                self.app.update()
                self.app.open_cover_editor(self.game)
                editor = self.app._cover_editor['win']
                self.button(editor, '联网查找封面…').invoke()
                self.assertFalse(editor.winfo_exists())
                dialog = self.app._library_cover_dialog
                self.app.update()
                self.assertTrue(dialog['win'].winfo_exists())
                self.assertEqual(dialog['query'].get(), 'Fixture Local Game')
                self.assertEqual(dialog['source'].get(), 'VNDB')
                self.assertIn('仅查询封面', '\n'.join(w.cget('text') for w in widgets(dialog['win'])
                                                   if isinstance(w, ctk.CTkLabel)))
                query_button = self.button(dialog['win'], '查询封面候选')
                self.assertEqual(tuple(query_button.cget('text_color')),
                                 (slg_gui._LIGHT['TEXT'], slg_gui._DARK['TEXT']))
                dialog['win'].destroy()
            search.assert_not_called()
            details.assert_not_called()
            download.assert_not_called()
        self.assertEqual(self.app.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)

    def test_app_queue_routes_explicit_query_without_auto_select(self):
        candidate = dict(source='vndb', external_id='v27519', title='Fixture Product',
                         developer='Fixture Developer', source_url='https://vndb.org/v27519')
        self.app._network_enabled = True
        with patch.object(covers.providers, 'search', return_value=[candidate]) as search, \
             patch.object(covers.providers, 'details') as details:
            self.app.open_cover_editor(self.game)
            self.button(self.app._cover_editor['win'], '联网查找封面…').invoke()
            dialog = self.app._library_cover_dialog
            self.button(dialog['win'], '查询封面候选').invoke()
            self.wait(lambda: not dialog['state']['busy'])
            self.assertEqual(len(dialog['state']['candidates']), 1)
            self.assertIsNone(dialog['state']['prepared'])
            search.assert_called_once()
            details.assert_not_called()

    def test_app_queued_query_closed_before_worker_no_network(self):
        gate = threading.Event()
        entered = [threading.Event(), threading.Event()]
        def blocked(event):
            event.set()
            gate.wait(3)
        blockers = [self.app._cover_executor.submit(blocked, event) for event in entered]
        try:
            self.assertTrue(all(event.wait(1) for event in entered))
            self.app._network_enabled = True
            with patch.object(covers.providers, 'search') as search:
                self.app.open_cover_editor(self.game)
                self.button(self.app._cover_editor['win'], '联网查找封面…').invoke()
                dialog = self.app._library_cover_dialog
                self.button(dialog['win'], '查询封面候选').invoke()
                future = dialog['state']['future']
                dialog['win'].destroy()
                self.assertTrue(future.cancelled())
                gate.set()
                for blocker in blockers:
                    blocker.result(3)
                self.app.update()
                search.assert_not_called()
        finally:
            gate.set()
            for blocker in blockers:
                blocker.result(3)

    def test_discovery_mode_tenth_layer_persists_without_scan(self):
        target = Path(_profile.name) / 'fixture_collection'
        target.mkdir(exist_ok=True)
        slg_scan_targets.save_targets(self.app.conn, [{'path': str(target), 'mode': 'children', 'depth': 1}])
        with patch.object(self.app, '_run_job') as run:
            self.app.open_library_import()
            dialog = self.app._library_targets_dialog
            mode = next(w for w in widgets(dialog['win']) if isinstance(w, ctk.CTkOptionMenu)
                        and '自动找游戏根' in w.cget('values'))
            mode._dropdown_callback('自动找游戏根')
            depth = next(w for w in widgets(dialog['win']) if isinstance(w, ctk.CTkOptionMenu)
                         and '第10层' in w.cget('values'))
            self.assertEqual(depth.get(), '第4层')
            depth._dropdown_callback('第10层')
            self.button(dialog['win'], '保存路径').invoke()
            saved = slg_scan_targets.load_targets(self.app.conn)
            self.assertEqual(saved[0]['mode'], 'discover')
            self.assertEqual(saved[0]['depth'], 10)
            self.assertFalse(dialog['win'].winfo_exists())
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
