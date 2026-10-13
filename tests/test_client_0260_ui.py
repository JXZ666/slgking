"""Real-card actions and deliberately reordered callbacks; run Tk suites serially."""
from concurrent.futures import Future
import json
from pathlib import Path
import threading
from unittest.mock import patch
import customtkinter as ctk

from PIL import Image
import test_client_0256_ui as fixture
import slg_db
import slg_gui
import slg_local_library as library


class ImmediateQueryExecutor:
    def submit(self, task):
        task()
        future = Future()
        future.set_result(None)
        return future


class Client0260UI(fixture.Client0256UI):
    def setUp(self):
        # The inherited fixture intentionally shares one Tk root. Restore the
        # initial registrations so clearing in one case cannot hide another.
        for catalogue in ('western_slg', 'dlsite'):
            slg_db.set_local_folder(self.app.conn, self.ids[catalogue], str(self.folders[catalogue]))
            slg_db.set_local_exe(self.app.conn, self.ids[catalogue], None)
        self.app.conn.commit()
        self.app.library_source = 'all'
        super().setUp()

    def settle(self):
        self.wait(lambda: not getattr(self.app, '_card_render_pending', False)
                  and not (self.app.origin == 'local' and getattr(self.app, '_library_query_pending', False)))

    def local(self):
        slg_db.set_pref(self.app.conn, 'library.onboarding_seen', '1')
        self.app.set_library_view()
        self.settle()

    def test_actual_reused_card_launches_new_binding_and_clear_stops_old_exe(self):
        app = self.app
        gid = self.ids['western_slg']
        first = self.folders['western_slg'] / 'a.exe'
        second = self.folders['western_slg'] / 'b.exe'
        first.touch()
        second.touch()
        slg_db.set_local_exe(app.conn, gid, str(first))
        app.conn.commit()
        self.local()
        slot = app._card_slot[gid]
        slg_db.set_local_exe(app.conn, gid, str(second))
        app.conn.commit()
        app.refresh(preserve_scroll=True)
        self.settle()
        self.assertIs(app._card_slot[gid], slot)
        self.assertEqual(slot['frame']._library_game['exe_path'], str(second))
        with patch.object(app, '_start_game_exe') as launch:
            self.button(slot['actions'], '启动').invoke()
            launch.assert_called_once_with(str(second))
        library.clear_local_binding(app.conn, gid, clear_folder=False, clear_exe=True)
        app.conn.commit()
        # The old card is still visible while the next query is outstanding.
        with patch.object(app, '_pick_game_exe', return_value=None), patch.object(app, '_start_game_exe') as launch:
            self.button(slot['actions'], '启动').invoke()
            launch.assert_not_called()
        app.refresh(preserve_scroll=True)
        self.settle()
        self.assertIsNone(slot['frame']._library_game['exe_path'])

    def test_old_query_snapshot_cannot_replace_personal_cover_edit_or_binding(self):
        app = self.app
        gid = self.ids['western_slg']
        self.local()
        picture = Path(self.tmp.name) / 'revision-cover.png'
        Image.new('RGB', (80, 90), 'blue').save(picture)
        cover = library.import_cover(str(picture))
        exe = self.folders['western_slg'] / 'revision.exe'
        exe.touch()
        for field, value, write in (
                ('cover_file', cover, lambda: library.set_cover_override(app.conn, gid, cover)),
                ('title', 'Edited while query pending', lambda: app.conn.execute('UPDATE games SET title=? WHERE id=?', ('Edited while query pending', gid))),
                ('exe_path', str(exe), lambda: slg_db.set_local_exe(app.conn, gid, str(exe)))):
            with self.subTest(field=field):
                callbacks = []
                real_put = app.queue.put
                def intercept(message):
                    if message[0] == 'library_callback':
                        callbacks.append(message[1])
                    else:
                        real_put(message)
                with patch.object(app, '_query_executor', ImmediateQueryExecutor()), patch.object(app.queue, 'put', side_effect=intercept):
                    app.refresh(preserve_scroll=True)  # snapshot is already taken
                    self.assertEqual(len(callbacks), 1)
                    write()
                    app.conn.commit()
                    app._library_refresh_game(gid)
                    self.assertEqual(len(callbacks), 2)
                    self.assertEqual(self.game(gid)[field], value)
                    stale, fresh = callbacks
                    stale[0](stale[1], stale[2])
                    self.assertEqual(self.game(gid)[field], value)
                    fresh[0](fresh[1], fresh[2])
                self.settle()
                self.assertEqual(app._card_slot[gid]['frame']._library_game[field], value)
        app.conn.execute('UPDATE games SET title=? WHERE id=?', ('western_slg Fixture', gid))
        app.conn.commit()

    def test_picker_identity_switch_does_not_write_or_launch(self):
        app = self.app
        gid = self.ids['western_slg']
        exe = self.folders['western_slg'] / 'identity.exe'
        exe.touch()
        library.clear_local_binding(app.conn, gid, clear_folder=False, clear_exe=True)
        app.conn.commit()
        with patch.object(app, '_library_identity', side_effect=['original', 'replacement']), \
                patch.object(slg_gui.filedialog, 'askopenfilename', return_value=str(exe)), \
                patch.object(slg_db, 'set_local_exe') as write:
            self.assertIsNone(app._pick_game_exe(gid, str(exe.parent)))
            write.assert_not_called()
        self.assertIsNone(app.conn.execute('SELECT exe_path FROM local WHERE game_id=?', (gid,)).fetchone()[0])

    def test_close_dispatch_discards_late_personal_write_callback(self):
        event = threading.Event()
        event.set()
        with patch.object(self.app, '_closing_event', event), patch.object(slg_db, 'set_local_exe') as write:
            self.app._dispatch('library_callback', (lambda value, error: write(self.app.conn, self.ids['user'], value), 'late.exe', None))
            write.assert_not_called()

    def test_backup_confirmation_identity_switch_cannot_restore(self):
        app = self.app
        backup = Path(self.tmp.name) / 'identity-backup.json'
        backup.write_text(json.dumps({'data': slg_db.export_user_data(app.conn)}), encoding='utf-8')
        with patch.object(app, '_library_identity', side_effect=['original', 'original', 'original', 'replacement']):
            app.open_backup()
            dialog = next(w for w in app.winfo_children() if isinstance(w, ctk.CTkToplevel) and w.title() == '备份与恢复')
            with patch.object(slg_gui.filedialog, 'askopenfilename', return_value=str(backup)), \
                    patch.object(slg_gui.messagebox, 'askyesno', return_value=True), \
                    patch.object(slg_db, 'import_user_data') as restore:
                self.button(dialog, '导入数据').invoke()
                restore.assert_not_called()

    def test_backup_export_identity_switch_cannot_write_file(self):
        app = self.app
        backup = Path(self.tmp.name) / 'forbidden-export.json'
        with patch.object(app, '_library_identity', side_effect=['original', 'original', 'replacement']):
            app.open_backup()
            dialog = next(w for w in app.winfo_children() if isinstance(w, ctk.CTkToplevel) and w.title() == '备份与恢复')
            with patch.object(slg_gui.filedialog, 'asksaveasfilename', return_value=str(backup)), \
                    patch.object(slg_db, 'export_user_data') as export:
                self.button(dialog, '导出数据').invoke()
                export.assert_not_called()
        self.assertFalse(backup.exists())
