import unittest
from datetime import date
import slg_date_filters as f
import slg_db

class DateFilters(unittest.TestCase):
    def test_presets_inclusive(self):
        self.assertEqual(f.resolve_date_range('7',today=date(2026,10,14)),('2026-10-08','2026-10-14'))
        self.assertEqual(f.resolve_date_range('30',today='2026-03-01'),('2026-01-31','2026-03-01'))
        self.assertEqual(f.resolve_date_range('all'),(None,None))
    def test_validation(self):
        for start,end in [('2026-2-01','2026-02-02'),('2026-02-30','2026-03-02'),('2026-02-02','2026-02-01'),(None,None),("x' OR 1=1--",'2026-10-14')]:
            with self.assertRaises(ValueError): f.resolve_date_range('custom',start,end)
        with self.assertRaises(ValueError): f.updated_date_sql('g); DROP TABLE games;--')
        with self.assertRaises(ValueError): f.resolve_date_range('bad')
    def test_authoritative_sql_boundaries(self):
        conn=slg_db.connect(':memory:')
        self.addCleanup(conn.close)
        for i,day in enumerate(('2026-10-08','2026-10-14 12:03:00','2026-10-07','2026-02-30',None)):
            slg_db.upsert_game(conn,str(i),'https://example.invalid',str(i),last_updated=day)
        gid,_=slg_db.upsert_game(conn,'dlsite-rj123456','https://example.invalid','DL',last_updated='2026-10-14')
        slg_db.set_dlsite_metadata(conn,gid,{'release_date':'2026-10-14'})
        clause,args=f.date_clause({'date_preset':'7','date_today':'2026-10-14'})
        self.assertEqual([r[0] for r in conn.execute('SELECT g.title FROM games g WHERE '+clause,args)],['0','1'])
        slg_db.set_dlsite_metadata(conn,gid,{'updated_date':'2026-10-10'})
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM games g WHERE '+clause,args).fetchone()[0],3)

if __name__=='__main__': unittest.main()
