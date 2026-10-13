"""Isolated real UI checks for catalogue scope and switching lifecycle.

No App is created or account read until the fixture redirects all profile paths
and blocks HTTP. Run separately from other Tk suites to avoid contention.
"""
import contextlib
import json
import subprocess
import sys
import os
from pathlib import Path
import tempfile
import time
import unittest
import weakref
from unittest.mock import patch
import urllib.request
import slg_db
import slg_gui
import slg_account
import slg_remote
import slg_translate

class CatalogueSwitchUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not hasattr(slg_gui.App,'set_catalogue'):
            raise unittest.SkipTest('catalogue GUI not integrated yet')
        cls.real_pilot_loader=slg_gui.App._load_dlsite_pilot
        cls.tmp=tempfile.TemporaryDirectory(prefix='slg-catalogue-ui-',ignore_cleanup_errors=True)
        cls.stack=contextlib.ExitStack()
        cls.stack.enter_context(patch.dict(os.environ,{key:cls.tmp.name for key in
            ('LOCALAPPDATA','APPDATA','HOME','USERPROFILE')}))
        def offline(*args,**kwargs): raise RuntimeError('network forbidden in catalogue UI tests')
        for obj,name,kwargs in (
            (urllib.request,'urlopen',dict(side_effect=offline)),
            (urllib.request.OpenerDirector,'open',dict(side_effect=offline)),
            (slg_account,'request',dict(side_effect=offline)),
            (slg_account,'session',dict(return_value=None)),
            (slg_remote,'report',dict(return_value=None)),
            (slg_remote,'fetch_config',dict(return_value=None)),
            (slg_translate,'translate_title',dict(side_effect=offline)),
            (slg_translate,'translate_overview',dict(side_effect=offline)),
            (slg_gui.App,'_load_dlsite_pilot',dict(return_value=None)),
            (slg_gui.App,'_require_personal_access',dict(return_value=True))):
            cls.stack.enter_context(patch.object(obj,name,**kwargs))
        conn=slg_db.connect(); cls.ids={}; cls.sources={}
        for source in ('western_slg','dlsite'):
            cls.ids[source]=[]
            for i in range(60):
                slug='bench-western-%d'%i if source=='western_slg' else 'dlsite-rj%08d'%i
                gid,_=slg_db.upsert_game(conn,slug,'https://example.invalid/'+slug,
                    '%s common game %02d'%(source,i),tags=['common',source],complete=0)
                slg_db.set_game_catalogue(conn,gid,source)
                slg_db.set_state(conn,gid,status='want' if i%2==0 else 'playing')
                if source=='dlsite':
                    slg_db.set_dlsite_metadata(conn,gid,dict(work_type='game',age_rating='R18',
                        rating_value=None,rating_count=None,rating_scale_max=None,languages=['ja']))
                cls.ids[source].append(gid); cls.sources[gid]=source
        cls.user_id=slg_db.add_user_game(conn,'Local unrelated game',categories=['galgame'])
        cls.collection=slg_db.create_collection(conn,'Mixed fixture')
        slg_db.set_game_collections(conn,cls.ids['western_slg'][0],[cls.collection])
        slg_db.set_game_collections(conn,cls.ids['dlsite'][0],[cls.collection])
        conn.commit(); conn.close()
        cls.app=slg_gui.App(notify=False); cls.app.geometry('1280x820')
        slg_db.set_pref(cls.app.conn, 'library.onboarding_seen', '1')
        cls.settle()
    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'app'): cls.app.destroy(); cls.app.conn.close()
        cls.stack.close(); cls.tmp.cleanup()
    @classmethod
    def settle(cls):
        deadline=time.perf_counter()+3
        errors=[]
        def poll():
            if (not getattr(cls.app,'_card_render_pending',False)
                    and not (cls.app.origin=='local' and getattr(cls.app,'_library_query_pending',False))):
                cls.app.quit()
            elif time.perf_counter()>deadline:
                errors.append('card batches failed to finish'); cls.app.quit()
            else:
                cls.app.after(2,poll)
        cls.app.after(0,poll)
        cls.app.tk.mainloop()
        if errors: raise AssertionError(errors[0])
    def setUp(self):
        app=self.app
        if app._search_after is not None:
            app.after_cancel(app._search_after); app._search_after=None
        app._catalogue_states.clear(); app.catalogue_id='western_slg'
        app.origin='main'; app.search=''; app.include=[]; app.exclude=[]
        app.view=None; app.collection_id=None; app.selected=None; app.page=1
        app.category_ids=[]; app.category_uncategorized=False
        app.refresh(); self.settle()
    def visible_ids(self): return {row['id'] for row in self.app.rows}
    def test_source_scopes_and_all_union(self):
        self.assertEqual(self.visible_ids(),set(self.ids['western_slg']))
        self.app.set_catalogue('dlsite'); self.settle()
        self.assertEqual(self.visible_ids(),set(self.ids['dlsite']))
        self.app.set_catalogue('all'); self.settle()
        self.assertEqual(self.visible_ids(),set(self.ids['western_slg']+self.ids['dlsite']))
    def test_switch_round_trip_restores_search_status_page_and_tags(self):
        app=self.app
        app.search='western'; app.view='want'; app.include=['common']; app.refresh()
        app._goto_page(2); self.settle()
        before=(app.search,app.view,app.page,tuple(app.include))
        app.set_catalogue('dlsite'); self.settle()
        app.search='dlsite'; app.view='playing'; app.refresh(); self.settle()
        app.set_catalogue('all'); self.settle()
        app.search='common'; app.view=None; app.refresh(); self.settle()
        app.set_catalogue('western_slg'); self.settle()
        self.assertEqual((app.search,app.view,app.page,tuple(app.include)),before)
        self.assertTrue(self.visible_ids()<=set(self.ids['western_slg']))
        app.set_catalogue('dlsite'); self.settle()
        self.assertEqual((app.search,app.view),('dlsite','playing'))
    def test_collection_intersects_current_catalogue(self):
        self.app._on_collection('Mixed fixture'); self.settle()
        self.assertEqual(self.visible_ids(),{self.ids['western_slg'][0]})
        self.app.set_catalogue('dlsite'); self.settle()
        self.app._on_collection('Mixed fixture'); self.settle()
        self.assertEqual(self.visible_ids(),{self.ids['dlsite'][0]})
    def test_wishlist_intersects_current_catalogue(self):
        for source in ('western_slg','dlsite','all'):
            self.app.set_catalogue(source); self.app.set_view('want'); self.settle()
            expected={gid for key,ids in self.ids.items() if source=='all' or key==source
                      for i,gid in enumerate(ids) if i%2==0}
            self.assertEqual(self.visible_ids(),expected)
    def test_user_library_and_categories_are_independent(self):
        for source in ('western_slg','dlsite','all'):
            self.app.set_view(None)
            self.app.set_catalogue(source); self.app.set_user_view(); self.settle()
            self.assertEqual((self.app.origin,self.app.library_source), ('local','user'))
            self.assertEqual(self.visible_ids(),{self.user_id})
            self.app.category_ids=['galgame']; self.app.refresh(); self.settle()
            self.assertEqual(self.visible_ids(),{self.user_id})
    def test_switch_cancels_pending_search_from_previous_catalogue(self):
        app=self.app
        app.search_entry.delete(0,'end'); app.search_entry.insert(0,'western')
        from types import SimpleNamespace
        app._on_search(SimpleNamespace(widget=app.search_entry))
        handle=app._search_after
        app.set_catalogue('dlsite'); self.settle()
        pending=app.tk.splitlist(app.tk.call('after','info'))
        self.assertNotIn(handle,pending)
        self.assertEqual(app.catalogue_id,'dlsite')
        self.assertEqual(self.visible_ids(),set(self.ids['dlsite']))
    def test_rapid_switch_card_batches_end_in_latest_catalogue(self):
        for source in ('dlsite','all','western_slg','dlsite'):
            self.app.set_catalogue(source)
        self.settle()
        self.assertEqual(self.visible_ids(),set(self.ids['dlsite']))
        self.assertEqual(set(gid for gid in self.app._pool_gid if gid is not None),
                         {row['id'] for row in self.app._page_slice()})
    def test_late_cover_from_previous_catalogue_cannot_repaint_reused_card(self):
        from PIL import Image
        widget=self.app._card_pool[0]['img']
        old_key=widget._slg_cover_key
        generation=self.app._card_render_generation
        self.app.set_catalogue('dlsite'); self.settle()
        self.assertIs(widget,self.app._card_pool[0]['img'])
        self.assertNotEqual(widget._slg_cover_key,old_key)
        before=widget.cget('image')
        self.app._cover_waiters[old_key]=weakref.WeakKeyDictionary({widget:generation})
        self.app._cover_decoded(old_key,Image.new('RGB',(128,79),'red'))
        self.assertIs(widget.cget('image'),before)
        slg_gui._image_cache.pop(old_key,None)
    def test_catalogue_preference_is_restored_on_real_process_restart(self):
        self.app.set_catalogue('dlsite'); self.settle()
        self.assertEqual(slg_db.get_pref(self.app.conn,'catalogue.active'),'dlsite')
        code="""import json,urllib.request
from unittest.mock import patch
import slg_gui,slg_account,slg_remote
with patch.object(slg_gui.App,'_load_dlsite_pilot',return_value=False), patch.object(slg_account,'session',return_value=None), patch.object(slg_remote,'report'), patch.object(urllib.request.OpenerDirector,'open',side_effect=RuntimeError('offline')):
 app=slg_gui.App(notify=False)
 print(json.dumps({'catalogue':app.catalogue_id}))
 app.destroy();app.conn.close()
"""
        result=subprocess.run([sys.executable,'-c',code],cwd=str(Path(slg_gui.__file__).parent),
            env=dict(os.environ),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        output=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(output['catalogue'],'dlsite')
    def test_missing_local_snapshot_reports_status_and_preserves_existing_library(self):
        before=self.app.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0]
        with patch.object(slg_gui.os.path,'isfile',return_value=False):
            loaded=type(self).real_pilot_loader(self.app)
        self.assertFalse(loaded)
        self.assertIn('数据快照',self.app._dlsite_pilot_status)
        self.assertEqual(self.app.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0],before)
    def test_destroy_dlsite_details_with_pending_child_textbox_timer(self):
        code="""import urllib.request,customtkinter as ctk
from unittest.mock import patch
import slg_gui,slg_account,slg_remote,slg_translate
with patch.object(slg_gui.App,'_load_dlsite_pilot',return_value=False), patch.object(slg_account,'session',return_value=None), patch.object(slg_remote,'report'), patch.object(urllib.request.OpenerDirector,'open',side_effect=RuntimeError('offline')), patch.object(slg_translate,'translate_title',side_effect=RuntimeError('offline')), patch.object(slg_translate,'translate_overview',side_effect=RuntimeError('offline')):
 app=slg_gui.App(notify=False)
 app.set_catalogue('dlsite')
 app.select(app.rows[0]); app.update()
 child=ctk.CTkTextbox(app)
 child.place(x=0,y=0); child.after(10000,lambda:None)
 app.destroy();app.conn.close()
 print('destroyed safely')
"""
        result=subprocess.run([sys.executable,'-c',code],cwd=str(Path(slg_gui.__file__).parent),
            env=dict(os.environ),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('TclError',result.stderr)
        self.assertIn('destroyed safely',result.stdout)
    def test_cached_footer_invalidates_after_local_rating_write(self):
        self.app._render_stats()
        before=self.app.stat_label.cget('text')
        slg_db.set_state(self.app.conn,self.ids['western_slg'][1],my_rating=5)
        self.app._render_stats()
        self.assertNotEqual(self.app.stat_label.cget('text'),before)
    def test_cached_footer_invalidates_after_worker_connection_commit(self):
        self.app._render_stats()
        before=self.app.stat_label.cget('text')
        with slg_db.session() as other:
            slg_db.set_state(other,self.ids['western_slg'][3],my_rating=4)
        self.app._render_stats()
        self.assertNotEqual(self.app.stat_label.cget('text'),before)
    def test_leaving_user_library_for_collection_restores_public_search(self):
        self.app.set_catalogue('dlsite'); self.settle()
        self.app.search='dlsite'; self.app.refresh(); self.settle()
        self.app.set_user_view(); self.settle()
        self.app.search='Local'; self.app.refresh(); self.settle()
        self.assertEqual(self.visible_ids(),{self.user_id})
        self.app._on_collection('Mixed fixture'); self.settle()
        self.assertEqual(self.app.catalogue_id,'dlsite')
        self.assertEqual(self.app.search,'dlsite')
        self.assertEqual(self.visible_ids(),{self.ids['dlsite'][0]})
    def test_unknown_dlsite_score_not_presented_as_zero_or_serial(self):
        self.app.set_catalogue('dlsite'); self.settle()
        text=slg_gui.meta_text(self.app.rows[0])
        self.assertNotIn('★ 0',text); self.assertNotIn('连载中',text)

if __name__=='__main__': unittest.main()
