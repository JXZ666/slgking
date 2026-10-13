"""Offline 0.25.7 UI acceptance. Also inherits 13 personal-cover regressions.

Run this suite in its own process; the fixture owns the sole Tk main thread.
"""
import http.client
from pathlib import Path
import threading
from unittest.mock import patch

import customtkinter as ctk
import test_client_0256_ui as fixture
import slg_db
import slg_library_query
import slg_library_ui
import slg_scan_targets


class Client0257UI(fixture.Client0256UI):
    @classmethod
    def setUpClass(cls):
        cls.network_guard = patch.object(http.client.HTTPConnection, 'request', side_effect=RuntimeError('network forbidden in 0.25.7 acceptance'))
        cls.network_guard.start()
        cls.addClassCleanup(cls.network_guard.stop)
        super().setUpClass()

    def setUp(self):
        super().setUp()
        self.app.library_source = 'all'
        self.app._library_intro_open = False
        slg_db.set_pref(self.app.conn, 'library.onboarding_seen', '1')
        slg_scan_targets.save_targets(self.app.conn, [])

    def settle(self):
        self.wait(lambda: not getattr(self.app, '_card_render_pending', False)
                  and not (self.app.origin == 'local' and getattr(self.app, '_library_query_pending', False)))

    def test_removed_navigation_and_maintenance_entry(self):
        labels = [w.cget('text') for w in self.widgets(self.app) if isinstance(w, ctk.CTkButton)]
        self.assertNotIn('已下载', labels)
        self.assertNotIn('我添加的游戏', labels)
        self.assertNotIn('扫描本地目录', labels)
        self.assertFalse(any(str(text).startswith('更多…') for text in labels))
        self.assertIsNone(self.app.maintenance_btn)
        self.assertIn('本地游戏库', labels)
        self.assertIn('SLG大类', self.app.catalogue_switch.cget('values'))

    def test_scan_targets_multiple_save_and_intentional_clear(self):
        roots = [Path(self.tmp.name) / name for name in ('target-one', 'target-two')]
        for root in roots:
            root.mkdir(exist_ok=True)
        self.app.open_library_import()
        dialog = self.app._library_targets_dialog
        with patch.object(slg_library_ui.filedialog, 'askdirectory', side_effect=[str(root) for root in roots]):
            dialog['choose']()
            dialog['choose']()
        dialog['targets'][1].update(mode='single', depth=0)
        dialog['save']()
        self.assertEqual(slg_scan_targets.load_targets(self.app.conn), [dict(path=str(roots[0]), mode='discover', depth=4), dict(path=str(roots[1]), mode='single', depth=0)])
        slg_db.set_pref(self.app.conn, 'scan_root', str(roots[0]))
        self.app.open_library_import()
        dialog = self.app._library_targets_dialog
        dialog['targets'].clear()
        dialog['save']()
        self.assertEqual(slg_scan_targets.load_targets(self.app.conn), [])

    def test_first_library_intro_skip_once(self):
        self.app.set_library_view()
        self.settle()
        slg_db.set_pref(self.app.conn, 'library.onboarding_seen', '')
        self.app._offer_library_onboarding()
        dialog = next(w for w in self.app.winfo_children() if isinstance(w, ctk.CTkToplevel) and w.title() == '建立你的本地游戏库')
        self.button(dialog, '稍后再说').invoke()
        self.assertTrue(slg_db.get_pref(self.app.conn, 'library.onboarding_seen'))
        self.app.set_view(None)
        self.app.set_library_view()
        self.settle()
        self.assertFalse(any(isinstance(w, ctk.CTkToplevel) and w.title() == '建立你的本地游戏库' for w in self.app.winfo_children()))

    def test_manual_add_enters_library_clears_filter_and_focuses(self):
        self.app.set_library_view()
        self.app.search = 'hidden-by-filter'
        self.app.include = ['missing-tag']
        self.app.library_source = 'user'
        self.app.category_ids = ['rpg']
        self.app.refresh()
        self.settle()
        self.app.open_add_game(prefill_title='ZZZ 0257 New game')
        dialog = next(w for w in self.app.winfo_children() if isinstance(w, ctk.CTkToplevel) and w.title() == '添加我的游戏')
        self.button(dialog, '保存').invoke()
        self.settle()
        row = self.app.conn.execute('SELECT id FROM games WHERE title=?', ('ZZZ 0257 New game',)).fetchone()
        try:
            self.assertEqual(self.app.origin, 'local')
            self.assertEqual((self.app.search, self.app.include, self.app.category_ids), ('', [], []))
            self.assertEqual(self.app.selected['id'], row['id'])
            self.assertIn(row['id'], [r['id'] for r in self.app.rows])
        finally:
            slg_db.delete_game(self.app.conn, row['id'])
            self.app.conn.commit()

    def test_path_result_updates_only_labels_no_query_or_refill(self):
        self.app.set_library_view()
        self.settle()
        game = self.game()
        original = self.app.conn
        queries = []
        class Spy:
            def __getattr__(self, name):
                return getattr(original, name)
            def execute(self, *args, **kwargs):
                queries.append(args[0])
                return original.execute(*args, **kwargs)
        self.app.conn = Spy()
        try:
            with patch.object(self.app, '_fill_card') as refill, patch.object(self.app, '_bind_cover') as bind:
                self.app._dispatch('library_path', (game['folder_path'], game['id'], '路径需修复'))
                refill.assert_not_called()
                bind.assert_not_called()
            self.assertEqual(queries, [])
            self.assertIn('路径需修复', self.app._card_slot[game['id']]['tagline'].cget('text'))
        finally:
            self.app.conn = original

    def test_personal_access_denial_opens_no_dialog_and_writes_nothing(self):
        existing = set(self.app.winfo_children())
        before = self.app.conn.total_changes
        with patch.object(self.app, '_require_personal_access', return_value=False):
            self.app.open_library_import()
            self.app.open_cover_editor(self.game())
            self.app.open_local_binding(self.game())
        self.assertEqual(set(self.app.winfo_children()), existing)
        self.assertEqual(self.app.conn.total_changes, before)

    def test_late_library_query_cannot_replace_public_page(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        real = slg_library_query.query_local_page
        def delayed(*args, **kwargs):
            started.set()
            release.wait(4)
            try:
                return real(*args, **kwargs)
            finally:
                finished.set()
        try:
            with patch.object(slg_library_query, 'query_local_page', side_effect=delayed):
                self.app.set_library_view()
                self.wait(started.is_set)
                self.app.set_view(None)
                self.settle()
                ids = [r['id'] for r in self.app.rows]
                release.set()
                self.wait(finished.is_set)
                self.wait(lambda: self.app.queue.empty())
                self.assertEqual(self.app.origin, 'main')
                self.assertEqual([r['id'] for r in self.app.rows], ids)
        finally:
            release.set()

    def test_late_scan_from_previous_identity_is_discarded(self):
        identity = self.app._library_identity()
        generation = self.app._identity_generation
        result = self.result('Late identity scan')
        try:
            self.app._identity_generation += 1
            with patch.object(self.app, 'open_library_scan_results') as show:
                self.app._dispatch('library_scan', (result, identity))
                show.assert_not_called()
                self.app._dispatch('library_scan', (result, self.app._library_identity()))
                show.assert_called_once_with(result)
        finally:
            self.app._identity_generation = generation

    def test_keyboard_cross_page_selects_after_new_query(self):
        added = [slg_db.add_user_game(self.app.conn, 'Keyboard %02d' % i) for i in range(16)]
        self.app.conn.commit()
        started, release = threading.Event(), threading.Event()
        real = slg_library_query.query_local_page
        def delayed(*args, **kwargs):
            started.set()
            release.wait(4)
            return real(*args, **kwargs)
        try:
            self.app.set_library_view()
            self.settle()
            self.app.select(self.app.rows[-1])
            old_id = self.app.selected['id']
            with patch.object(slg_library_query, 'query_local_page', side_effect=delayed):
                self.app._move_selection(1)
                self.wait(started.is_set)
                self.assertEqual(self.app.selected['id'], old_id)
                release.set()
                self.settle()
                self.assertEqual(self.app.page, 2)
                self.assertEqual(self.app.selected['id'], self.app.rows[0]['id'])
                self.assertNotEqual(self.app.selected['id'], old_id)
        finally:
            release.set()
            for gid in added:
                slg_db.delete_game(self.app.conn, gid)
            self.app.conn.commit()
