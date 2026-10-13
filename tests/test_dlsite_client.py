"""Offline DLsite UI queries and immutable native source facts."""
import copy
import hashlib
from pathlib import Path
import sqlite3
import unittest

import slg_db
import slg_dlsite_client as client


class ClientQueries(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.ids = []
        for number, developer, facts in (
                (1, "Circle Alpha", {"languages": ["日本語", "简体中文"], "work_type": "RPG"}),
                (2, "Circle Beta", {"languages": ["English"], "work_type": "ACT"}),
                (3, "Circle Alpha", {"languages": [], "work_type": ""})):
            slug = "dlsite-rj%08d" % number
            gid, _ = slg_db.upsert_game(self.conn, slug,
                    "https://www.dlsite.com/maniax/work/=/product_id/RJ%08d.html" % number,
                    "Unrelated title " + str(number), developer=developer)
            slg_db.set_dlsite_metadata(self.conn, gid, facts)
            self.ids.append(gid)
        self.western, _ = slg_db.upsert_game(self.conn, "western-circle-alpha", "https://dikgames.com/x/",
                                            "Circle Alpha", developer="Circle Alpha")

    def query(self, clauses, params):
        return [row[0] for row in self.conn.execute(
            "SELECT g.id FROM games g WHERE " + (" AND ".join(clauses) or "1") + " ORDER BY g.id", params)]

    def test_rj_case_insensitive_and_developer_only_dlsite(self):
        sql, params = client.search_clause("rJ00000001")
        self.assertEqual(self.query([sql], params), self.ids[:1])
        sql, params = client.search_clause("circle alpha")
        self.assertEqual(self.query([sql], params), [self.ids[0], self.ids[2]])
        sql, params = client.search_clause("' OR 1=1 --")
        self.assertEqual(self.query([sql], params), [])

    def test_filter_or_and_unknown(self):
        where, params = client.filter_clauses("dlsite", ["日本語", "English"], ["ACT"])
        self.assertEqual(self.query(where, params), [self.ids[1]])
        where, params = client.filter_clauses("dlsite", [client.UNKNOWN], [client.UNKNOWN])
        # Filter predicates are combined with catalogue membership by find_games.
        self.assertIn(self.ids[2], self.query(where, params))
        self.conn.execute("DELETE FROM game_metadata WHERE game_id=?", (self.ids[1],))
        self.assertIn(self.ids[1], self.query(where, params))
        where, params = client.filter_clauses("dlsite", ["简体中文", client.UNKNOWN])
        self.assertIn(self.ids[0], self.query(where, params))
        self.assertIn(self.ids[2], self.query(where, params))

    def test_other_catalogues_ignore_filters(self):
        for scope in (None, "western_slg", "all"):
            self.assertEqual(client.filter_clauses(scope, ["English"], ["ACT"]), ([], []))

    def test_options_only_dlsite_and_no_per_card_queries(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        options = client.filter_options(self.conn)
        self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 2)
        self.assertEqual(set(options["work_types"]), {"RPG", "ACT", client.UNKNOWN})
        self.assertEqual(set(options["languages"]), {"English", "日本語", "简体中文", client.UNKNOWN})

    def test_detail_is_immutable_unknown_is_not_negative(self):
        game = slg_db.find_games(self.conn, catalog_id="dlsite", with_scores=False)[0]
        original = copy.deepcopy(game)
        facts = client.detail_facts(game)
        self.assertIsNotNone(facts["official_url"])
        self.assertEqual(game, original)
        unknown = client.detail_facts({"slug": "dlsite-rj00000003", "source_metadata": {}})
        self.assertIsNone(unknown["languages"])
        self.assertIsNone(unknown["updated_date"])
        self.assertIn("语言：未知", client.detail_lines({"slug": "dlsite-rj3"}))
        bad = dict(game, url="https://evil.invalid/" + facts["product_id"])
        self.assertIsNone(client.detail_facts(bad)["official_url"])

    def test_alias_is_validated(self):
        with self.assertRaises(ValueError):
            client.search_clause("RJ1", alias="g; DROP TABLE games")


class BundledFacts(unittest.TestCase):
    def test_all_151_source_facts_are_preserved_without_writes(self):
        path = Path(__file__).resolve().parents[1] / "assets/dlsite/slgking.db"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = slg_db.find_games(conn, catalog_id="dlsite", with_scores=False)
            self.assertEqual(len(rows), 151)
            saved = copy.deepcopy(rows)
            for row in rows:
                facts = client.detail_facts(row)
                self.assertTrue(facts["product_id"].startswith("RJ"))
                self.assertEqual(facts["rating_value"], row["source_metadata"].get("rating_value"))
                self.assertEqual(facts["rating_count"], row["source_metadata"].get("rating_count"))
            client.filter_options(conn)
            self.assertEqual(rows, saved)
        finally:
            conn.close()
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
