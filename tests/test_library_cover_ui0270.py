"""Real Tk lifecycle checks; all providers and profiles remain isolated."""
from concurrent.futures import ThreadPoolExecutor
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import customtkinter as ctk
from PIL import Image
import slg_db
import slg_library_cover_candidates as covers
import slg_local_library as library
from slg_image_resources import ResourceToplevel
from slg_library_cover_ui import LibraryCoverUI


def candidate():
    return dict(source='vndb', external_id='v27519', title='多娜多娜', developer='ALICESOFT',
                source_url='https://vndb.org/v27519', cover_url='https://t.vndb.org/cv/12/123.jpg')


def prepared():
    return covers.PreparedCover(candidate(), Image.new('RGB', (400, 600), 'red'),
                                Image.new('RGB', (160, 240), 'red'))


class Harness(LibraryCoverUI, ctk.CTk):
    def __init__(self, conn):
        super().__init__()
        self.withdraw()
        self.conn = conn
        self.queue = queue.Queue()
        self._cover_executor = ThreadPoolExecutor(max_workers=2)
        self.identity = 'fixture'
        self.refreshed = []
        self.allowed = True
        self._network_enabled = True
        self.callback_errors = []
        self.report_callback_exception = lambda *args: self.callback_errors.append(args)

    def _require_personal_access(self, action):
        return self.allowed

    def _library_identity(self):
        return self.identity

    def _library_refresh_game(self, gid):
        self.refreshed.append(gid)

    def _new_dialog(self, title, geometry):
        win = ResourceToplevel(self)
        win.title(title)
        win.geometry(geometry)
        return win


class CoverUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load theme definitions before creating our first root.
        import slg_gui

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = slg_db.connect(str(self.root / 'profile.sqlite'))
        gid = slg_db.add_user_game(self.conn, 'Existing Game', engine='Unity')
        self.conn.commit()
        self.game = dict(self.conn.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone())
        self.app = Harness(self.conn)
        self.patches = [patch.object(covers, 'search', return_value=[candidate()]),
                        patch.object(covers, 'prepare', side_effect=lambda *a, **kw: prepared()),
                        patch.object(slg_db, 'covers_dir', return_value=str(self.root / 'covers'))]
        self.search_mock, self.prepare_mock, _ = [p.start() for p in self.patches]

    def tearDown(self):
        dialog = getattr(self.app, '_library_cover_dialog', None)
        if dialog:
            dialog['win'].destroy()
        self.app._cover_executor.shutdown(wait=True)
        self.pump()
        self.assertFalse(self.app.callback_errors)
        # CTk root owns additional periodic toolkit jobs; teardown must cancel
        # them before creating the next root in this isolated lifecycle suite.
        for handle in self.app.tk.splitlist(self.app.tk.call('after', 'info')):
            self.app.after_cancel(handle)
        self.app.destroy()
        for p in self.patches:
            p.stop()
        self.conn.close()
        self.tmp.cleanup()

    def pump(self, until=None):
        deadline = time.monotonic() + 3
        while True:
            self.app.update()
            while not self.app.queue.empty():
                kind, payload = self.app.queue.get_nowait()
                self.assertEqual(kind, 'library_callback')
                callback, value, error = payload
                callback(value, error)
            if until is None or until():
                return
            if time.monotonic() > deadline:
                self.fail('worker timed out')
            time.sleep(.01)

    def load(self):
        d = self.app.open_library_cover_query(self.game)
        d['search']()
        self.pump(lambda: not d['state']['busy'])
        d['pick'](d['state']['candidates'][0])
        self.pump(lambda: not d['state']['busy'])
        return d

    def test_no_automatic_network_or_write(self):
        d = self.app.open_library_cover_query(self.game)
        self.pump()
        self.search_mock.assert_not_called()
        self.prepare_mock.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)
        d['search']()
        d['search']()
        self.pump(lambda: not d['state']['busy'])
        self.assertEqual(self.search_mock.call_count, 1)
        self.assertIsNone(d['state']['prepared'])

    def test_explicit_save_preserves_metadata_and_refreshes_only_game(self):
        before = dict(self.game)
        d = self.load()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)
        d['save']()
        d['save']()
        self.pump(lambda: getattr(self.app, '_library_cover_dialog', None) is None)
        self.assertEqual(self.app.refreshed, [self.game['id']])
        self.assertEqual(dict(self.conn.execute('SELECT * FROM games WHERE id=?', (self.game['id'],)).fetchone()), before)
        self.assertEqual(len(list((self.root / 'covers').glob('user_*.jpg'))), 1)

    def test_cancel_detaches_callbacks_and_closes_preview(self):
        d = self.load()
        image, photo = d['state']['prepared'], d['state']['image']
        self.assertTrue(photo._configure_callback_list)
        d['win'].destroy()
        self.assertIsNone(image.image)
        self.assertFalse(photo._configure_callback_list)
        self.assertFalse((self.root / 'covers').exists())

    def test_source_failure_visible_and_retryable(self):
        self.search_mock.side_effect = ValueError('来源限流')
        d = self.app.open_library_cover_query(self.game)
        d['search']()
        self.pump(lambda: not d['state']['busy'])
        self.assertIn('来源限流', d['status'].cget('text'))
        self.search_mock.side_effect = None
        d['search']()
        self.pump(lambda: not d['state']['busy'])
        self.assertEqual(len(d['state']['candidates']), 1)

    def test_save_failure_old_override_preserved(self):
        library.set_cover_override(self.conn, self.game['id'], 'old.jpg')
        self.conn.commit()
        d = self.load()
        with patch.object(covers, 'apply', side_effect=RuntimeError('cannot commit')):
            d['save']()
            self.pump(lambda: not d['state']['busy'])
        self.assertIn('保存失败', d['status'].cget('text'))
        self.assertFalse(list((self.root / 'covers').glob('user_*.jpg')))
        self.assertEqual(self.conn.execute('SELECT cover_file FROM game_cover_overrides').fetchone()[0], 'old.jpg')

    def test_account_change_drops_save_result(self):
        d = self.load()
        gate, started = threading.Event(), threading.Event()
        original = covers.save_image
        def delayed(*args):
            started.set()
            gate.wait(3)
            return original(*args)
        with patch.object(covers, 'save_image', side_effect=delayed):
            d['save']()
            self.assertTrue(started.wait(1))
            self.app.identity = 'different-account'
            gate.set()
            self.app._cover_executor.shutdown(wait=True)
            self.pump()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)
        self.assertFalse(list((self.root / 'covers').glob('user_*.jpg')))

    def test_closed_window_discards_late_prepared_result(self):
        gate, started = threading.Event(), threading.Event()
        result = prepared()
        def delayed(*args, **kwargs):
            started.set()
            gate.wait(3)
            return result
        with patch.object(covers, 'prepare', side_effect=delayed):
            d = self.app.open_library_cover_query(self.game)
            d['pick'](candidate())
            self.assertTrue(started.wait(1))
            d['win'].destroy()
            gate.set()
            self.app._cover_executor.shutdown(wait=True)
            self.pump()
        self.assertIsNone(result.image)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)

    def test_reset_and_theme_reopen(self):
        library.set_cover_override(self.conn, self.game['id'], 'old.jpg')
        self.conn.commit()
        try:
            for theme in ('Light', 'Dark', 'Light'):
                ctk.set_appearance_mode(theme)
                d = self.load()
                self.pump()
                d['win'].destroy()
            d = self.app.open_library_cover_query(self.game)
            d['reset']()
            self.assertEqual(self.conn.execute('SELECT count(*) FROM game_cover_overrides').fetchone()[0], 0)
            self.assertEqual(self.app.refreshed, [self.game['id']])
        finally:
            ctk.set_appearance_mode('Dark')

    def test_offline_mode_keeps_local_workflow(self):
        d = self.app.open_library_cover_query(self.game)
        self.app._network_enabled = False
        d['search']()
        self.search_mock.assert_not_called()
        self.assertIn('离线模式', d['status'].cget('text'))

    def test_queued_closed_query_is_cancelled_before_network(self):
        gate = threading.Event()
        blockers = [self.app._cover_executor.submit(gate.wait, 3) for _ in range(2)]
        try:
            d = self.app.open_library_cover_query(self.game)
            d['search']()
            future = d['state']['future']
            d['win'].destroy()
            self.assertTrue(future.cancelled())
        finally:
            gate.set()
            for blocker in blockers:
                blocker.result(3)
        self.search_mock.assert_not_called()

    def test_queued_cancelled_save_releases_worker_copy(self):
        d = self.load()
        gate = threading.Event()
        blockers = [self.app._cover_executor.submit(gate.wait, 3) for _ in range(2)]
        image = d['state']['prepared'].image.copy()
        try:
            with patch.object(d['state']['prepared'].image, 'copy', return_value=image):
                d['save']()
            future = d['state']['future']
            d['win'].destroy()
            self.assertTrue(future.cancelled())
            with self.assertRaises(ValueError):
                image.getbbox()
        finally:
            gate.set()
            for blocker in blockers:
                blocker.result(3)
        self.assertFalse((self.root / 'covers').exists())


if __name__ == '__main__':
    unittest.main()
