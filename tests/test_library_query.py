import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import slg_db
import slg_library_query as query


class LibraryQuery(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.ids = [slg_db.add_user_game(self.conn, 'Same', categories=('galgame',) if i % 2 else ()) for i in range(19)]
        self.site, _ = slg_db.upsert_game(self.conn, 'dlsite-rj01234567', 'https://example.invalid', 'Japanese', developer='Circle')
        slg_db.set_local_exe(self.conn, self.site, '/missing/game.exe')
        slg_db.set_dlsite_metadata(self.conn, self.site, {'languages': ['日本語'], 'work_type': 'game'})
        self.hidden, _ = slg_db.upsert_game(self.conn, 'unbound', 'https://example.invalid/hidden', 'Hidden')
        self.conn.commit()

    def test_page_bounded_stable_clamped(self):
        pages = [query.query_local_page(self.conn, {}, p) for p in (1, 2, 3, 999)]
        self.assertEqual([len(p.rows) for p in pages], [8, 8, 4, 4])
        self.assertEqual(pages[-1].page, 3)
        self.assertEqual(pages[0].total, 20)
        self.assertEqual(len({row['id'] for p in pages[:3] for row in p.rows}), 20)
        self.assertEqual(pages[2].rows, pages[3].rows)
        self.assertNotIn(self.hidden, [row['id'] for p in pages for row in p.rows])

    def test_categories_only_self_created_and_or_unknown(self):
        self.assertEqual(query.query_local_page(self.conn, {'category_ids': ['galgame']}, 1).total, 20)
        spec = dict(source='user', category_ids=['galgame'])
        self.assertEqual(query.query_local_page(self.conn, spec, 1).total, 9)
        spec['uncategorized'] = True
        self.assertEqual(query.query_local_page(self.conn, spec, 1).total, 19)
        self.assertEqual(query.query_local_page(self.conn, dict(source='user', uncategorized=True), 1).total, 10)

    def test_search_metadata_alias_translation(self):
        for term in ('RJ01234567', 'Circle', 'Japanese'):
            result = query.query_local_page(self.conn, dict(search=term), 1)
            self.assertEqual([r['id'] for r in result.rows], [self.site])
            self.assertEqual(result.rows[0]['source_metadata']['languages'], ['日本語'])
        result = query.query_local_page(self.conn, dict(search='translated', translation_ids=[self.ids[0]]), 1)
        self.assertEqual([r['id'] for r in result.rows], [self.ids[0]])

    def test_effective_translation_and_alias_search(self):
        slg_db.add_alias(self.conn, self.ids[0], 'Personal Alias')
        slg_db.set_manual_translation(self.conn, 'title', self.ids[1], '手工名称')
        slg_db.set_auto_translation(self.conn, 'title', self.ids[2], 'Same', '旧机器名称')
        self.conn.execute('UPDATE games SET title=? WHERE id=?', ('Changed', self.ids[2]))
        self.assertEqual(query.query_local_page(self.conn, {'search': 'Alias'}, 1).rows[0]['id'], self.ids[0])
        self.assertEqual(query.query_local_page(self.conn, {'search': '手工'}, 1).rows[0]['id'], self.ids[1])
        self.assertEqual(query.query_local_page(self.conn, {'search': '旧机器'}, 1).total, 0)

    def test_focus_locates_using_filter_and_stable_sort(self):
        for spec in ({}, {'desc': True}, {'sort': 'updated'}, {'source': 'user'}, {'source': 'user', 'uncategorized': True}):
            all_rows = query.query_local_page(self.conn, spec, 1, page_size=80).rows
            gid = all_rows[-1]['id']
            result = query.query_local_page(self.conn, dict(spec, focus_id=gid), 1)
            self.assertIn(gid, [row['id'] for row in result.rows])
            self.assertEqual(result.page, (len(all_rows)-1)//8+1)
        result = query.query_local_page(self.conn, {'source': 'user', 'focus_id': self.site}, 2)
        self.assertEqual(result.page, 2)

    def test_tags_include_and_exclude_union(self):
        for i, gid in enumerate(self.ids):
            for tag in (['one', 'two'] if i % 2 else ['one']):
                self.conn.execute('INSERT OR IGNORE INTO tags(name) VALUES (?)', (tag,))
                self.conn.execute('INSERT INTO game_tags(game_id,tag_id) SELECT ?,id FROM tags WHERE name=?', (gid, tag))
        result = query.query_local_page(self.conn, dict(include=['one', 'two', 'one']), 1)
        self.assertEqual(result.total, 9)
        self.assertEqual(query.query_local_page(self.conn, dict(include=['one'], exclude=['two']), 1).total, 10)

    def test_transaction_and_batched_page_decorators(self):
        self.conn.execute('UPDATE games SET title=title WHERE id=?', (self.ids[0],))
        with patch.object(slg_db, 'get_game_metadata_bulk', wraps=slg_db.get_game_metadata_bulk) as facts, patch.object(slg_db, 'game_categories_bulk', wraps=slg_db.game_categories_bulk) as cats:
            result = query.query_local_page(self.conn, {}, 1)
            self.assertLessEqual(len(result.rows), 8)
            self.assertEqual(facts.call_count, 1)
            self.assertEqual(cats.call_count, 1)
            self.assertLessEqual(len(cats.call_args.args[1]), 8)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        with self.assertRaises(ValueError):
            query.query_local_page(self.conn, {'source': 'bad'}, 1)
        self.assertFalse(self.conn.in_transaction)

    def test_readonly_missing_override_table_no_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.db'
            disk = sqlite3.connect(path)
            self.conn.backup(disk)
            disk.execute('DROP TABLE game_cover_overrides')
            disk.commit()
            disk.close()
            with closing(query.open_readonly(path)) as conn:
                result = query.query_local_page(conn, {}, 1)
                self.assertEqual(result.total, 20)
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='game_cover_overrides'").fetchone())
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute('DELETE FROM games')

    def test_public_find_unchanged(self):
        before = slg_db.find_games(self.conn, origin='main', sort='title')
        query.query_local_page(self.conn, {}, 1)
        self.assertEqual(before, slg_db.find_games(self.conn, origin='main', sort='title'))

    def test_override_missing_and_available_preserve_catalogue(self):
        self.conn.execute('UPDATE games SET cover_file=? WHERE id=?', ('catalogue.jpg', self.site))
        self.conn.execute('INSERT INTO game_cover_overrides VALUES (?,?)', (self.site, 'user_custom.jpg'))
        with patch.object(query.os.path, 'isfile', return_value=False):
            row = query.query_local_page(self.conn, {'search': 'RJ01234567'}, 1).rows[0]
            self.assertEqual(row['cover_file'], 'catalogue.jpg')
            self.assertTrue(row['cover_override_missing'])
        with patch.object(query.os.path, 'isfile', return_value=True):
            row = query.query_local_page(self.conn, {'search': 'RJ01234567'}, 1).rows[0]
            self.assertEqual(row['cover_file'], 'user_custom.jpg')
            self.assertEqual(row['catalogue_cover_file'], 'catalogue.jpg')
            self.assertTrue(row['has_cover_override'])

    def test_redirect_exclusion_and_empty_result(self):
        self.conn.execute('INSERT INTO catalog_redirects VALUES (?,?,1)', ('dlsite-rj01234567', 'canonical'))
        result = query.query_local_page(self.conn, {'search': 'RJ01234567'}, 7)
        self.assertEqual((result.rows, result.total, result.page), ([], 0, 1))
        self.assertEqual(result.stats['games'], 19)

    def test_snapshot_count_and_rows_when_writer_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'wal.db'
            disk = sqlite3.connect(path)
            self.conn.backup(disk)
            disk.execute('PRAGMA journal_mode=WAL')
            disk.close()
            reader = query.open_readonly(path)
            writer = sqlite3.connect(path)
            try:
                with patch.object(query, '_decorate', wraps=query._decorate) as decorate:
                    original = query._where
                    def change_after_read(conn, spec):
                        value = original(conn, spec)
                        # Force a snapshot before a concurrent writer commits.
                        conn.execute('SELECT COUNT(*) FROM games').fetchone()
                        writer.execute("DELETE FROM games WHERE origin='user'")
                        writer.commit()
                        return value
                    with patch.object(query, '_where', side_effect=change_after_read):
                        result = query.query_local_page(reader, {}, 1)
                        self.assertEqual(result.total, 20)
                        self.assertEqual(len(result.rows), 8)
                        self.assertEqual(decorate.call_count, 1)
                self.assertEqual(query.query_local_page(reader, {}, 1).total, 1)
            finally:
                reader.close()
                writer.close()


if __name__ == '__main__':
    unittest.main()
