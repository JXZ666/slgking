"""Offline dual-catalogue identity, atomic imports, and personal-data safety."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import slg_db
import slg_sync_server as sync


class Catalogues(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = slg_db.connect(str(self.root / "personal.db"))

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def dl(self, conn=None, number=100, title="Same", rating=4):
        conn = conn or self.conn
        gid, _ = slg_db.upsert_game(conn, "dlsite-rj%d" % number,
            "https://www.dlsite.com/maniax/work/=/product_id/RJ%d.html" % number,
            title, tags=["shared", "Japanese"])
        slg_db.set_dlsite_metadata(conn, gid, {"rating_value": rating, "rating_count": 10,
            "rating_scale_max": None, "work_type": "RPG", "release_date": "2026-01-01",
            "updated_date": "2026-01-02", "languages": ["日本語"], "age_rating": "成人向"})
        return gid

    def snapshot(self, catalog="dlsite", count=2):
        path = self.root / (catalog + ".db")
        with slg_db.session(str(path)) as source:
            for i in range(count):
                if catalog == "dlsite":
                    self.dl(source, 100 + i)
                else:
                    slg_db.upsert_game(source, "west%d" % i, "https://dikgames.com/west%d/" % i,
                                       "Western", tags=["shared"])
        manifest = self.root / (catalog + ".json")
        manifest.write_text(json.dumps({"catalog_id": catalog, "count": count,
            "db_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "lastmod": "local"}), encoding="utf-8")
        return path, manifest

    def test_directory_and_user_filters_preserve_identities(self):
        west, _ = slg_db.upsert_game(self.conn, "west", "u", "Same", tags=["shared", "extra"])
        dl = self.dl()
        user = slg_db.add_user_game(self.conn, "User", promoted=True, categories=("slg",))
        find = lambda **kw: slg_db.find_games(self.conn, **kw)
        self.assertEqual({r["id"] for r in find(catalog_id="western_slg")}, {west, user})
        self.assertEqual({r["id"] for r in find(catalog_id="dlsite")}, {dl})
        self.assertEqual(len(find(catalog_id="all")), 3)
        self.assertEqual(find(origin="user", catalog_id="dlsite")[0]["categories"], ("slg",))
        self.assertEqual(find(catalog_id="dlsite", include=("shared", "extra")), [])
        self.assertEqual(find(catalog_id="dlsite")[0]["catalog_ids"], ("dlsite",))
        self.assertEqual(dict(slg_db.tag_counts(self.conn, catalog_id="dlsite"))["Japanese"], 1)
        self.assertEqual(slg_db.catalogue_stats(self.conn, "dlsite")["games"], 1)

    def test_source_url_and_native_rating(self):
        gid = self.dl()
        self.assertIsNone(slg_db.get_game(self.conn, gid)["rating"])
        self.assertEqual(slg_db.get_game_metadata_bulk(self.conn, [gid])[gid]["rating_value"], 4)
        self.assertEqual(slg_db.list_game_sources(self.conn, gid)[0]["label"], "DLsite")
        for url in ("https://evil.com/maniax/work/=/product_id/RJ100.html",
                    "http://www.dlsite.com/maniax/work/=/product_id/RJ100.html",
                    "https://www.dlsite.com/maniax/", "https://www.dlsite.com:444/maniax/work/=/product_id/RJ100.html"):
            self.assertIsNone(slg_db._safe_source_url("dlsite", url))
        slg_db.delete_game(self.conn, gid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM game_catalogs").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM game_metadata").fetchone()[0], 0)

    def test_native_rating_sort_missing_last_and_no_cross_directory(self):
        low = self.dl(number=100, rating=2)
        high = self.dl(number=101, rating=4)
        missing = self.dl(number=102, rating=None)
        self.assertEqual([r["id"] for r in slg_db.find_games(self.conn, catalog_id="dlsite", sort="source_rating")],
                         [high, low, missing])
        with self.assertRaises(ValueError):
            slg_db.find_games(self.conn, catalog_id="all", sort="source_rating")

    def test_old_database_membership_upgrade_preserves_records(self):
        gid, _ = slg_db.upsert_game(self.conn, "old", "u", "Old")
        slg_db.set_state(self.conn, gid, my_rating=5)
        self.conn.execute("DROP TABLE game_catalogs")
        self.conn.commit()
        self.conn.close()
        self.conn = slg_db.connect(str(self.root / "personal.db"))
        row = slg_db.find_games(self.conn, catalog_id="western_slg")[0]
        self.assertEqual(row["id"], gid)
        self.assertEqual(row["my_rating"], 5)

    def test_local_import_hash_and_idempotency_offline(self):
        path, manifest = self.snapshot()
        slg_db.set_pref(self.conn, sync.PREF_LAST_PULL_HASH, "western-cursor")
        with patch.object(sync, "_server_http_get", side_effect=AssertionError("network forbidden")):
            result = sync.import_local_catalogue(self.conn, path, manifest, log=lambda _: None)
            self.assertEqual(result["new"], 2)
            self.assertFalse(result["unchanged"])
            self.assertTrue(sync.import_local_catalogue(self.conn, path, manifest)["unchanged"])
            with self.assertRaisesRegex(AssertionError, "network forbidden"):
                sync.pull(self.conn, catalog_id="dlsite")
        self.assertEqual(slg_db.get_pref(self.conn, sync.PREF_LAST_PULL_HASH), "western-cursor")
        data = json.loads(manifest.read_text())
        data["db_sha256"] = "0" * 64
        manifest.write_text(json.dumps(data))
        before = [dict(r) for r in self.conn.execute("SELECT * FROM games")]
        with self.assertRaises(ValueError):
            sync.import_local_catalogue(self.conn, path, manifest)
        self.assertEqual([dict(r) for r in self.conn.execute("SELECT * FROM games")], before)

    def test_atomic_failure_and_directory_mismatch_do_not_write(self):
        path, _ = self.snapshot()
        import sqlite3
        with __import__("contextlib").closing(sqlite3.connect(path)) as source:
            source.execute("UPDATE game_metadata SET metadata_json=? WHERE game_id=2", ('{"rating_value":"bad"}',))
            source.commit()
        with self.assertRaises(ValueError):
            sync.merge_catalog(self.conn, path, "dlsite")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            sync.merge_catalog(self.conn, path, "western_slg")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 0)

    def test_interleaved_sync_preserves_other_directory_and_redirects(self):
        west, _ = slg_db.upsert_game(self.conn, "west", "u", "Original")
        dl = self.dl()
        slg_db.set_state(self.conn, dl, my_rating=5)
        self.conn.execute("INSERT INTO catalog_redirects VALUES ('old-west','west',0)")
        self.conn.commit()
        path, _ = self.snapshot()
        sync.merge_catalog(self.conn, path, "dlsite")
        self.assertEqual(slg_db.get_game(self.conn, west)["title"], "Original")
        self.assertEqual(slg_db.find_games(self.conn, catalog_id="dlsite", search="Same")[0]["my_rating"], 5)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM catalog_redirects WHERE old_slug='old-west'").fetchone())

    def test_western_recommendation_unchanged_after_dlsite_import_and_rating(self):
        west, _ = slg_db.upsert_game(self.conn, "west", "u", "Western", tags=["shared"])
        slg_db.set_state(self.conn, west, my_rating=5)
        slg_db.recompute_weights(self.conn)
        weights = [tuple(r) for r in self.conn.execute("SELECT * FROM weights")]
        score = slg_db.find_games(self.conn, catalog_id="western_slg")[0]["score"]
        dl = self.dl()
        slg_db.set_state(self.conn, dl, my_rating=1)
        slg_db.recompute_weights(self.conn)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM weights")], weights)
        self.assertEqual(slg_db.find_games(self.conn, catalog_id="western_slg")[0]["score"], score)

    def test_backup_dependency_rejection_is_non_mutating(self):
        dl = self.dl()
        slg_db.set_state(self.conn, dl, my_rating=5)
        data = slg_db.export_user_data(self.conn)
        with slg_db.session(":memory:") as target:
            slg_db.add_user_game(target, "Keep", categories=("slg",))
            before = slg_db.export_user_data(target)
            with self.assertRaisesRegex(ValueError, "目录"):
                slg_db.import_user_data(target, data)
            self.assertEqual(slg_db.export_user_data(target), before)
            del data["catalogue_requirements"]
            with self.assertRaisesRegex(ValueError, "DLsite"):
                slg_db.import_user_data(target, data)
            self.assertEqual(slg_db.export_user_data(target), before)

    def test_backup_restores_when_catalogue_available(self):
        dl = self.dl()
        slg_db.set_state(self.conn, dl, my_rating=5)
        data = slg_db.export_user_data(self.conn)
        with slg_db.session(":memory:") as target:
            slg_db.add_user_game(target, "Offset")
            restored = self.dl(target)
            slg_db.import_user_data(target, data)
            self.assertEqual(slg_db.find_games(target, catalog_id="dlsite")[0]["my_rating"], 5)
            self.assertNotEqual(restored, dl)

    def test_user_slug_conflict_and_empty_snapshot_safe(self):
        path, _ = self.snapshot(count=0)
        sync.merge_catalog(self.conn, path, "dlsite")
        self.assertEqual(slg_db.find_games(self.conn), [])
        gid = slg_db.add_user_game(self.conn, "Mine", categories=("galgame",))
        self.conn.execute("UPDATE games SET slug='dlsite-rj100' WHERE id=?", (gid,))
        self.conn.commit()
        path.unlink()
        path, _ = self.snapshot(count=1)
        sync.merge_catalog(self.conn, path, "dlsite")
        self.assertEqual(slg_db.get_game(self.conn, gid)["title"], "Mine")
        self.assertEqual(slg_db.game_categories(self.conn, gid), ("galgame",))

    def test_maintenance_never_fetches_dlsite_with_legacy_parser(self):
        import slg_scrape
        dl = self.dl()
        self.conn.execute("UPDATE games SET cover_file='pending:remote' WHERE id=?", (dl,))
        self.assertEqual(slg_db.metrics_gap_rows(self.conn), [])
        self.assertEqual(slg_db.data_gaps(self.conn)["rating"], 0)
        self.assertEqual(slg_db.data_gaps(self.conn)["overview"], 0)
        fetcher = Mock()
        fetcher.get.side_effect = AssertionError("legacy fetch forbidden")
        with patch.object(slg_scrape, "http_get", side_effect=AssertionError("legacy fetch forbidden")):
            slg_scrape.enrich(self.conn, fetcher, log=lambda _: None)
            slg_scrape.backfill_metrics(self.conn, fetcher, log=lambda _: None)
            slg_scrape.download_covers(self.conn, log=lambda _: None)
        fetcher.get.assert_not_called()

    def test_wishlist_changes_are_scoped_by_directory(self):
        west, _ = slg_db.upsert_game(self.conn, "west", "u", "Western", version="1.0")
        dl = self.dl()
        self.conn.execute("UPDATE games SET version='1.0' WHERE id=?", (dl,))
        for gid in (west, dl):
            slg_db.set_state(self.conn, gid, status="want")
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), [])
        self.conn.execute("UPDATE games SET version='2.0'")
        self.assertEqual([r["id"] for r in slg_db.wishlist_version_changes(self.conn, "western_slg")], [west])
        self.assertEqual([r["id"] for r in slg_db.wishlist_version_changes(self.conn, "dlsite")], [dl])

    def test_personal_writes_keep_dlsite_facts_and_local_id(self):
        dl = self.dl()
        slg_db.set_state(self.conn, dl, status="want", my_rating=5)
        collection = slg_db.create_collection(self.conn, "Favorite")
        slg_db.set_game_collections(self.conn, dl, [collection])
        slg_db.set_local_folder(self.conn, dl, "C:/games/example")
        row = slg_db.find_games(self.conn, catalog_id="dlsite")[0]
        self.assertEqual(row["id"], dl)
        self.assertEqual(row["my_rating"], 5)
        self.assertEqual(row["source_metadata"]["rating_value"], 4)
        self.assertEqual(row["catalog_ids"], ("dlsite",))
        self.assertEqual(row["folder_path"], "C:/games/example")

    def test_redirect_metadata_preservation_conflict_and_directory_boundary(self):
        old = self.dl(number=100)
        new, _ = slg_db.upsert_game(self.conn, "dlsite-rj101", "u", "New")
        self.conn.commit()
        sync._sync_catalog_redirects(self.conn, {"dlsite-rj100": "dlsite-rj101"}, "dlsite")
        self.assertEqual(slg_db.get_game_metadata_bulk(self.conn, [new])[new]["rating_value"], 4)
        # Conflicting native metadata must keep both rows visible and intact.
        other = self.dl(number=102, rating=1)
        self.conn.commit()
        sync._sync_catalog_redirects(self.conn, {"dlsite-rj102": "dlsite-rj101"}, "dlsite")
        self.assertEqual(self.conn.execute("SELECT active FROM catalog_redirects WHERE old_slug='dlsite-rj102'").fetchone()[0], 0)
        self.assertEqual(slg_db.get_game_metadata_bulk(self.conn, [other])[other]["rating_value"], 1)
        west, _ = slg_db.upsert_game(self.conn, "west", "u", "Western")
        self.conn.commit()
        with self.assertRaises(ValueError):
            sync._sync_catalog_redirects(self.conn, {"west": "dlsite-rj101"}, "western_slg")
        self.assertEqual(slg_db.get_game(self.conn, west)["title"], "Western")

    def test_recommendation_always_computes_real_scores(self):
        low, _ = slg_db.upsert_game(self.conn, "low", "u", "A Low", rating=2)
        high, _ = slg_db.upsert_game(self.conn, "high", "u", "Z High", rating=8)
        expected = slg_db.find_games(self.conn, catalog_id="western_slg", sort="score")
        actual = slg_db.find_games(self.conn, catalog_id="western_slg", sort="score",
                                   with_scores=False, catalogue_cache={})
        self.assertEqual([row["id"] for row in actual], [high, low])
        self.assertEqual(actual, expected)
        self.assertTrue(all(row["score"] is not None for row in actual))

    def test_non_recommendation_can_skip_scores_without_changing_other_fields(self):
        self.dl()
        slg_db.upsert_game(self.conn, "west", "u", "Western", rating=6)
        expected = slg_db.find_games(self.conn, catalog_id="all", sort="title", desc=False)
        queries = []
        self.conn.set_trace_callback(queries.append)
        actual = slg_db.find_games(self.conn, catalog_id="all", sort="title", desc=False,
                                   with_scores=False, catalogue_cache={})
        self.conn.set_trace_callback(None)
        self.assertTrue(all(row["score"] is None for row in actual))
        self.assertEqual([{k: v for k, v in row.items() if k != "score"} for row in actual],
                         [{k: v for k, v in row.items() if k != "score"} for row in expected])
        self.assertFalse(any("SUM(w.weight)" in query or "AVG(ss.my_rating" in query
                             for query in queries))

    def test_catalogue_cache_invalidates_same_and_other_connection_writes(self):
        dl = self.dl()
        self.conn.commit()
        cache = {}
        query = lambda: slg_db.find_games(self.conn, catalog_id="dlsite", sort="title",
                                         with_scores=False, catalogue_cache=cache)[0]
        self.assertEqual(query()["source_metadata"]["rating_value"], 4)
        slg_db.set_dlsite_metadata(self.conn, dl, {"rating_value": 3, "languages": ["ja"]})
        slg_db.set_state(self.conn, dl, status="want", my_rating=5)
        self.conn.commit()
        row = query()
        self.assertEqual(row["source_metadata"]["rating_value"], 3)
        self.assertEqual(row["my_rating"], 5)
        with slg_db.session(str(self.root / "personal.db")) as writer:
            slg_db.set_dlsite_metadata(writer, dl, {"rating_value": 2, "languages": ["en"]})
            writer.execute("UPDATE games SET title='Changed externally' WHERE id=?", (dl,))
            slg_db.set_state(writer, dl, my_rating=1)
        row = query()
        self.assertEqual(row["source_metadata"]["rating_value"], 2)
        self.assertEqual(row["source_metadata"]["languages"], ["en"])
        self.assertEqual(row["title"], "Changed externally")
        self.assertEqual(row["my_rating"], 1)

    def test_mutating_returned_source_facts_does_not_pollute_cache(self):
        self.dl()
        cache = {}
        query = lambda: slg_db.find_games(self.conn, catalog_id="dlsite", sort="title",
                                         with_scores=False, catalogue_cache=cache)[0]
        row = query()
        row["source_metadata"]["rating_value"] = 999
        row["source_metadata"]["languages"].append("accidental GUI mutation")
        fresh = query()
        self.assertEqual(fresh["source_metadata"]["rating_value"], 4)
        self.assertEqual(fresh["source_metadata"]["languages"], ["日本語"])


if __name__ == "__main__":
    unittest.main()
