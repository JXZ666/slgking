from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import slg_db
import slg_catalogue_query as q
import slg_library_query

class CatalogueQuery(unittest.TestCase):
    def setUp(self):
        self.conn=slg_db.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.ids=[]
        for i in range(20):
            gid,_=slg_db.upsert_game(self.conn,'western-'+str(i),'https://example.invalid',f'Game {i:02}',rating=float(i%5),last_updated='2026-10-10' if i%2 else '2026-09-01',tags=('one','two') if i%3 else ('one',))
            self.ids.append(gid)
            self.conn.execute('INSERT INTO state(game_id,status) VALUES (?,?)',(gid,'downloaded' if i%2 else 'playing'))
        self.dl,_=slg_db.upsert_game(self.conn,'dlsite-rj123456','https://example.invalid','DL Game',last_updated='2026-10-14')
        slg_db.set_dlsite_metadata(self.conn,self.dl,{'rating_value':4.8,'updated_date':'2026-10-12','languages':['日本語']})
        self.user=slg_db.add_user_game(self.conn,'User')
        self.collection=slg_db.create_collection(self.conn,'Collection')
        for gid in (self.ids[1],self.dl,self.user):
            self.conn.execute('INSERT INTO collection_items(collection_id,game_id) VALUES (?,?)',(self.collection,gid))
        self.conn.commit()
    def test_paging_and_focus(self):
        spec={'catalog_id':'western_slg','sort':'title','desc':False}
        pages=[q.query_catalogue_page(self.conn,spec,p) for p in (1,2,3,999)]
        self.assertEqual([len(p.rows) for p in pages],[8,8,4,4])
        self.assertEqual(pages[-1].page,3)
        focused=q.query_catalogue_page(self.conn,dict(spec,focus_id=self.ids[-1]),1)
        self.assertEqual(focused.page,3)
        self.assertIn(self.ids[-1],[r['id'] for r in focused.rows])
    def test_intersection(self):
        spec=dict(catalog_id='western_slg',sort='score',include=['one','two'],statuses=['downloaded'],search='Game',date_preset='7',date_today='2026-10-14')
        got=q.query_catalogue_page(self.conn,spec,1,80)
        expected=[r for r in slg_db.find_games(self.conn,include=['one','two'],statuses=['downloaded'],search='Game',catalog_id='western_slg',sort='score') if r['last_updated']=='2026-10-10']
        self.assertEqual([r['id'] for r in got.rows],[r['id'] for r in expected])
        for a,b in zip(got.rows,expected): self.assertAlmostEqual(a['score'],b['score'])
        self.assertEqual(q.query_catalogue_page(self.conn,dict(spec,exclude=['one']),1).total,0)
    def test_collection_all_sources_and_user(self):
        spec=dict(origin=None,collection_id=self.collection,source='all',sort='title')
        self.assertEqual(q.query_catalogue_page(self.conn,spec,1).total,3)
        self.assertEqual(q.query_catalogue_page(self.conn,dict(spec,source='user'),1).rows[0]['id'],self.user)
        self.assertEqual(q.query_catalogue_page(self.conn,dict(spec,source='dlsite'),1).rows[0]['id'],self.dl)
    def test_dlsite_source_rating_date_and_xp(self):
        got=q.query_catalogue_page(self.conn,dict(catalog_id='dlsite',sort='source_rating',date_preset='custom',date_start='2026-10-12',date_end='2026-10-12'),1)
        self.assertEqual(got.rows[0]['source_metadata']['rating_value'],4.8)
        self.assertIsNone(got.rows[0]['score'])
        with self.assertRaises(ValueError): q.query_catalogue_page(self.conn,dict(catalog_id='dlsite',sort='score'),1)
        self.assertFalse(self.conn.in_transaction)
    def test_explicit_all_source_clears_previous_catalogue(self):
        got=q.query_catalogue_page(self.conn,dict(origin=None,catalog_id='dlsite',source='all',sort='title'),1)
        self.assertEqual(got.total,22)

    def test_alias_translation_and_unknown_dlsite_date(self):
        slg_db.add_alias(self.conn,self.ids[0],'Translated Alias')
        self.assertEqual(q.query_catalogue_page(self.conn,dict(search='Translated Alias',sort='title'),1).rows[0]['id'],self.ids[0])
        slg_db.set_dlsite_metadata(self.conn,self.dl,{'release_date':'2026-10-14'})
        spec=dict(catalog_id='dlsite',sort='updated',date_preset='7',date_today='2026-10-14')
        self.assertEqual(q.query_catalogue_page(self.conn,spec,1).total,0)

    def test_injection_validation(self):
        self.assertEqual(q.query_catalogue_page(self.conn,dict(search="' OR 1=1 --",sort='title'),1).total,0)
        for spec in ({'sort':'x;--'},{'source':'x'},{'catalog_id':'x'},{'origin':'x'},{'hidden_mode':'x'}):
            with self.assertRaises(ValueError): q.query_catalogue_page(self.conn,spec,1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0],22)
    def test_readonly_and_page_bulk_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'games.db'
            disk=sqlite3.connect(path)
            self.conn.backup(disk)
            disk.close()
            with closing(q.open_readonly(path)) as conn:
                with patch.object(slg_db,'game_categories_bulk',wraps=slg_db.game_categories_bulk) as cats:
                    q.query_catalogue_page(conn,{'sort':'title'},1)
                    self.assertEqual(cats.call_count,1)
                    self.assertLessEqual(len(cats.call_args.args[1]),8)
                with self.assertRaises(sqlite3.OperationalError): conn.execute('DELETE FROM games')
    def test_local_date_reuse(self):
        self.conn.execute('UPDATE games SET last_updated=? WHERE id=?',('2026-10-10',self.user))
        got=slg_library_query.query_local_page(self.conn,dict(date_preset='7',date_today='2026-10-14'),1)
        self.assertEqual([r['id'] for r in got.rows],[self.user])

if __name__=='__main__': unittest.main()
