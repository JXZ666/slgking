import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import slg_account
import slg_db
import slg_gui
import slg_remote


class ClientGuiTests(unittest.TestCase):
    def test_add_edit_hide_restore_and_source_dialog(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'LOCALAPPDATA':temp,'APPDATA':temp}))
            stack.enter_context(patch.object(slg_account,'session',return_value=None))
            stack.enter_context(patch.object(slg_remote,'report',return_value=None))
            stack.enter_context(patch.object(slg_gui.App,'_require_personal_access',return_value=True))
            app=slg_gui.App(notify=False)
            errors=[]
            app.report_callback_exception=lambda *e:errors.append(e)
            slg_db.set_pref(app.conn,'library.onboarding_seen','1')
            ctk=slg_gui.ctk
            def children(w):
                for c in w.winfo_children():
                    yield c
                    yield from children(c)
            def dialog():
                return next(w for w in app.winfo_children() if isinstance(w,ctk.CTkToplevel))
            def wait():
                app.after(180,app.quit)
                app.mainloop()
            try:
                app.update()
                app.open_add_game()
                win=dialog()
                entries=[w for w in children(win) if isinstance(w,ctk.CTkEntry)]
                entries[0].insert(0,'Fixture custom game')
                combo=next(w for w in children(win) if isinstance(w,ctk.CTkComboBox))
                combo.set('My custom engine')
                next(w for w in children(win) if isinstance(w,ctk.CTkButton) and w.cget('text')=='保存').invoke()
                wait()
                game=dict(app.conn.execute("SELECT * FROM games WHERE origin='user'").fetchone())
                self.assertEqual(game['engine'],'My custom engine')
                app.open_add_game(game=game)
                win=dialog()
                combo=next(w for w in children(win) if isinstance(w,ctk.CTkComboBox))
                self.assertEqual(combo.get(),'My custom engine')
                combo.set('Unity')
                next(w for w in children(win) if isinstance(w,ctk.CTkButton) and w.cget('text')=='保存').invoke()
                wait()
                self.assertEqual(slg_db.get_game(app.conn,game['id'])['engine'],'Unity')
                app.selected=slg_db.get_game(app.conn,game['id'])
                app._hide_selected_game()
                self.assertFalse(slg_db.find_games(app.conn,origin=None))
                app.open_hidden_games()
                win=dialog()
                search=next(w for w in children(win) if isinstance(w,ctk.CTkEntry))
                search.insert(0,'Fixture')
                search.event_generate('<KeyRelease>')
                wait()
                next(w for w in children(win) if isinstance(w,ctk.CTkButton) and w.cget('text')=='恢复').invoke()
                self.assertEqual(len(slg_db.find_games(app.conn,origin=None)),1)
                win.destroy()
                app.open_data_sources()
                win=dialog()
                self.assertTrue(any(isinstance(w,ctk.CTkLabel) and w.cget('text')=='DLsite' for w in children(win)))
                win.destroy()
                # Closing the manager during its delayed search is safe.
                app.open_hidden_games()
                win=dialog()
                search=next(w for w in children(win) if isinstance(w,ctk.CTkEntry))
                search.event_generate('<KeyRelease>')
                win.destroy()
                wait()
                self.assertFalse(errors)
            finally:
                app.destroy()
                app.conn.close()


if __name__=='__main__':unittest.main()
