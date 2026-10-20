"""Android 0.27 boundary regressions: isolated storage and mocked transport only."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from PIL import Image
import slg_android_api as api
import slg_android_bootstrap as bootstrap
import slg_db as db
import slg_dlsite_online as online
import slg_sync_server as sync

ASSETS = Path(__file__).resolve().parents[1] / "assets"


class Android027(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        api._close_conn()
        api._COVER_CACHES.clear()
        api._INDEX_RETRIES.clear()
        self.transport = mock.patch.object(sync, "_server_http_get",
            side_effect=AssertionError("Unexpected network access"))
        self.transport.start()
        bootstrap.init(self.tmp.name, crypto="stub")

    def tearDown(self):
        api._close_conn()
        api._COVER_CACHES.clear()
        api._INDEX_RETRIES.clear()
        self.transport.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, method, **params):
        return json.loads(api.call(json.dumps(dict(params, method=method))))

    def seed(self):
        api._close_conn()
        return bootstrap.init(self.tmp.name, ASSETS, crypto="stub")

    def listing(self, **params):
        result = self.call("catalog.list", catalog_id="dlsite", page_size=200, **params)
        self.assertTrue(result["ok"], result)
        for page in range(2, (result["total"] + result["page_size"] - 1) // result["page_size"] + 1):
            extra = self.call("catalog.list", catalog_id="dlsite", page=page, page_size=200, **params)
            self.assertTrue(extra["ok"], extra)
            result["items"].extend(extra["items"])
        return result

    def test_upgrade_installs_151_seed_preserves_personal_and_is_idempotent(self):
        game = self.call("usergames.add", title="Old mobile record", version="v1",
                         source_url="https://example.org/mobile")
        self.assertTrue(game["ok"])
        self.call("game.rate", id=game["id"], stars=4)
        self.call("game.note", id=game["id"], note="Keep me")
        self.seed()
        self.assertEqual(self.listing()["total"], 151)
        detail = self.call("game.get", id=game["id"])["game"]
        self.assertEqual((detail["my_rating"], detail["note"], detail["url"]),
                         (4, "Keep me", "https://example.org/mobile"))
        self.assertTrue(self.seed()["dlsite_imported"]["unchanged"])
        self.assertEqual(self.listing()["total"], 151)

    def test_new_install_offline_seed_and_catalogue_separation(self):
        api._close_conn()
        bootstrap.init(Path(self.tmp.name) / "fresh-install", ASSETS, crypto="stub")
        dls = self.listing()
        western = self.call("catalog.list", catalog_id="western_slg", page_size=200)
        all_games = self.call("catalog.list", catalog_id="all", page_size=200)
        self.assertEqual(dls["total"], 151)
        self.assertTrue(all(x["catalog_id"] == "dlsite" for x in dls["items"]))
        self.assertFalse(any(x["slug"].startswith("dlsite-") for x in western["items"]))
        self.assertEqual(all_games["total"], dls["total"] + western["total"])

    def test_online_catalogue_is_not_replaced_by_bundled_seed(self):
        self.seed()
        item = self.listing()["items"][0]
        conn = api._conn()
        conn.execute("UPDATE games SET title='Online refreshed title' WHERE id=?", (item["id"],))
        db.set_pref(conn, online.ONLINE_HASH_KEY, "a" * 64)
        conn.commit()
        self.assertTrue(self.seed()["dlsite_imported"]["unchanged"])
        self.assertEqual(self.call("game.get", id=item["id"])["game"]["title"], "Online refreshed title")

    def test_native_rating_facts_are_not_cross_source_normalized(self):
        self.seed()
        items = self.listing()["items"]
        item = next(x for x in items if x["rating"] is not None)
        detail = self.call("game.get", id=item["id"])["game"]
        facts = detail["dlsite_facts"]
        self.assertTrue(facts["product_id"].startswith("RJ"))
        self.assertEqual(item["rating"], facts["rating_value"])
        self.assertEqual(item["rating_scale_max"], facts["rating_scale_max"])
        self.assertEqual(item["rating_count"], facts["rating_count"])
        # The actual bundle omits scale_max; keep that uncertainty rather than inventing a scale.
        if item["rating_scale_max"] is not None:
            self.assertLessEqual(item["rating"], item["rating_scale_max"])
        self.assertEqual(detail["sources"][0]["source_id"], "dlsite")

    def test_dlsite_combined_filters_obey_real_metadata(self):
        self.seed()
        filters = self.call("dlsite.filters")
        self.assertTrue(filters["ok"])
        self.assertTrue(all(isinstance(x, str) for x in filters["languages"] + filters["work_types"]))
        items = self.listing()["items"]
        details = [self.call("game.get", id=x["id"])["game"] for x in items]
        example = next(x for x in details if x["dlsite_facts"]["languages"] and x["dlsite_facts"]["work_type"])
        language = example["dlsite_facts"]["languages"][0]
        work_type = example["dlsite_facts"]["work_type"]
        expected = {x["id"] for x in details if language in (x["dlsite_facts"]["languages"] or [])
                    and x["dlsite_facts"]["work_type"] == work_type}
        actual = self.listing(languages=[language], work_types=[work_type])
        self.assertEqual({x["id"] for x in actual["items"]}, expected)
        self.assertIn(example["id"], expected)
        self.assertEqual(self.listing(languages=["not a real language"])["total"], 0)

    def test_public_sources_deduplicate_sites_and_exclude_zyoudao(self):
        conn = api._conn()
        gid, _ = db.upsert_game(conn, "public-test", "https://dikgames.com/public-test", "Public game")
        for source, external, url in (
            ("dikgames", "test-a", "https://dikgames.com/public-test"),
            ("dikgames", "test-b", "https://dikgames.com/public-test-alt"),
            ("f95zone", "thread-1", "https://f95zone.to.it/threads/test.123/"),
            ("zyoudao", "zy-1", "https://zyoudao.net/test/"),
        ):
            db.upsert_game_source(conn, source, external, gid, source_url=url)
        conn.commit()
        sources = self.call("game.get", id=gid)["game"]["sources"]
        self.assertEqual({x["source_id"] for x in sources}, {"dikgames", "f95zone"})
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(x["url"].startswith("https://") for x in sources))

    def test_legacy_url_fallback_and_unknown_source_remains_hidden(self):
        conn = api._conn()
        gid, _ = db.upsert_game(conn, "legacy", "https://f95zone.to.it/threads/legacy.456/", "Legacy")
        conn.execute("DELETE FROM game_sources WHERE game_id=?", (gid,))
        conn.commit()
        sources = self.call("game.get", id=gid)["game"]["sources"]
        self.assertEqual([x["source_id"] for x in sources], ["f95zone"])
        conn.execute("UPDATE games SET url='https://zyoudao.net/legacy' WHERE id=?", (gid,))
        conn.commit()
        self.assertEqual(self.call("game.get", id=gid)["game"]["sources"], [])

    def test_personal_url_omitted_preserved_empty_cleared_invalid_atomic(self):
        gid = self.call("usergames.add", title="Phone port", source_url="https://example.org/port")["id"]
        self.assertTrue(self.call("usergames.update", id=gid, title="Edited")["ok"])
        self.assertEqual(self.call("game.get", id=gid)["game"]["url"], "https://example.org/port")
        for bad in ("file:///sdcard/game.apk", "javascript:alert(1)", "intent://install", "https://user:password@example.org/x", "https://exa mple.org/x", "https://example.org/a b"):
            with self.subTest(bad=bad):
                self.assertFalse(self.call("usergames.update", id=gid, title="Must not persist", source_url=bad)["ok"])
                detail = self.call("game.get", id=gid)["game"]
                self.assertEqual(detail["title"], "Edited")
                self.assertEqual(detail["url"], "https://example.org/port")
        self.assertTrue(self.call("usergames.update", id=gid, title="Edited", source_url="")["ok"])
        self.assertEqual(self.call("game.get", id=gid)["game"]["sources"], [])
        self.assertFalse(self.call("usergames.add", title="Bad", source_url="file:///bad")["ok"])
        self.assertEqual(self.call("catalog.list", view="user", catalog_id="all")["total"], 1)

    def test_backup_pc_wrapper_preserves_personal_url_and_dlsite_state(self):
        self.seed()
        gid = self.call("usergames.add", title="Backup port", source_url="https://example.org/backup")["id"]
        dlsite = self.listing()["items"][0]
        self.call("game.state", id=dlsite["id"], status="want")
        payload = self.call("backup.export")["data"]
        path = Path(self.tmp.name) / "pc-backup.json"
        path.write_text(json.dumps({"data": payload}), encoding="utf-8")
        self.call("usergames.update", id=gid, title="Changed", source_url="")
        self.call("game.state", id=dlsite["id"], status="")
        self.assertTrue(self.call("backup.preview", path=str(path))["ok"])
        result = self.call("backup.import", path=str(path))
        self.assertTrue(result["ok"], result)
        user = self.call("catalog.list", view="user", catalog_id="all")["items"][0]
        self.assertEqual(self.call("game.get", id=user["id"])["game"]["url"], "https://example.org/backup")
        self.assertEqual(self.call("game.get", slug=dlsite["slug"])["game"]["status"], "want")

    def cover_fixture(self):
        self.seed()
        item = self.listing()["items"][0]
        product = item["slug"].removeprefix("dlsite-").upper()
        buffer = io.BytesIO()
        Image.new("RGB", (20, 20), "gold").save(buffer, format="JPEG")
        raw = buffer.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        entry = {"filename": f"dlsite-{product}-{digest}.jpg", "bytes": len(raw), "sha256": digest}
        return item, raw, entry

    def test_dlsite_cover_verified_on_demand_cached_without_upstream(self):
        item, raw, entry = self.cover_fixture()
        with mock.patch.object(online, "get_cover_index", return_value={item["slug"]: entry}), \
                mock.patch.object(sync, "_server_http_get", return_value=raw) as fetch:
            first = self.call("cover.get", id=item["id"])
            second = self.call("cover.get", id=item["id"])
        self.assertTrue(first["ok"], first)
        self.assertEqual(Path(first["path"]).read_bytes(), raw)
        self.assertEqual(first["path"], second["path"])
        fetch.assert_called_once_with(sync.SERVER_BASE + "/covers/" + entry["filename"], timeout=30)

    def test_dlsite_cover_missing_index_and_corrupt_response(self):
        item, raw, entry = self.cover_fixture()
        with mock.patch.object(online, "get_cover_index", return_value={}):
            result = self.call("cover.get", id=item["id"])
        self.assertTrue(result["unavailable"])
        self.assertIsNone(result["path"])
        with mock.patch.object(online, "get_cover_index", return_value={item["slug"]: entry}), \
                mock.patch.object(sync, "_server_http_get", return_value=b"not an image"):
            result = self.call("cover.get", id=item["id"])
        self.assertFalse(result["ok"])
        self.assertFalse((Path(db.covers_dir()) / entry["filename"]).exists())

    def test_cover_matching_hash_still_requires_decodable_image(self):
        item, _, _ = self.cover_fixture()
        raw = b"known-size-and-hash-but-not-an-image"
        digest = hashlib.sha256(raw).hexdigest()
        product = item["slug"].removeprefix("dlsite-").upper()
        bad = {"filename": f"dlsite-{product}-{digest}.jpg", "bytes": len(raw), "sha256": digest}
        with mock.patch.object(online, "get_cover_index", return_value={item["slug"]: bad}), \
                mock.patch.object(sync, "_server_http_get", return_value=raw):
            self.assertFalse(self.call("cover.get", id=item["id"])["ok"])
        self.assertFalse((Path(db.covers_dir()) / bad["filename"]).exists())

    def index_transport_fixture(self, item, raw, entry):
        index_raw = json.dumps({item["slug"]: entry}).encode("utf-8")
        index_hash = hashlib.sha256(index_raw).hexdigest()
        db_hash = "a" * 64
        manifest = {"catalog_id": "dlsite", "count": 151, "revision": "test-safe-index",
                    "db_sha256": db_hash, "db_path": f"/catalogues/dlsite/snapshots/{db_hash}.db",
                    "cover_index_sha256": index_hash,
                    "cover_index_path": f"/catalogues/dlsite/cover-index/{index_hash}.json"}
        responses = {online.MANIFEST_PATH: json.dumps(manifest).encode("utf-8"),
                     manifest["cover_index_path"]: index_raw, "/covers/" + entry["filename"]: raw}
        def fetch(url, **kwargs):
            self.assertTrue(url.startswith(sync.SERVER_BASE + "/"))
            return responses[url.removeprefix(sync.SERVER_BASE)]
        return manifest, index_raw, responses, fetch

    def test_first_cover_fetches_safe_index_without_marking_online_catalogue(self):
        item, raw, entry = self.cover_fixture()
        manifest, index_raw, _, transport = self.index_transport_fixture(item, raw, entry)
        self.assertFalse(online.get_cover_index(api._conn()))
        with mock.patch.object(sync, "_server_http_get", side_effect=transport) as fetched:
            result = self.call("cover.get", id=item["id"])
            cached = self.call("cover.get", id=item["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], cached["path"])
        self.assertEqual(Path(result["path"]).read_bytes(), raw)
        self.assertEqual(online.get_cover_index(api._conn()), {item["slug"]: entry})
        self.assertIsNone(db.get_pref(api._conn(), online.ONLINE_HASH_KEY))
        self.assertEqual([x.args[0] for x in fetched.call_args_list], [
            sync.SERVER_BASE + online.MANIFEST_PATH,
            sync.SERVER_BASE + manifest["cover_index_path"],
            sync.SERVER_BASE + "/covers/" + entry["filename"],
        ])

    def test_bad_index_hash_not_saved_and_explicit_retry_bypasses_backoff(self):
        item, raw, entry = self.cover_fixture()
        manifest, index_raw, responses, transport = self.index_transport_fixture(item, raw, entry)
        responses[manifest["cover_index_path"]] = index_raw + b"corrupt"
        with mock.patch.object(sync, "_server_http_get", side_effect=transport) as fetched:
            bad = self.call("cover.get", id=item["id"])
            self.assertTrue(bad["unavailable"])
            self.assertIsNone(db.get_pref(api._conn(), online.INDEX_KEY))
            self.assertIsNone(db.get_pref(api._conn(), online.ONLINE_HASH_KEY))
            self.assertFalse((Path(db.covers_dir()) / entry["filename"]).exists())
            second = self.call("cover.get", id=item["id"])
            self.assertTrue(second["unavailable"])
            self.assertEqual(fetched.call_count, 2, "Automatic retry must respect 30-second backoff")
            responses[manifest["cover_index_path"]] = index_raw
            retried = self.call("cover.get", id=item["id"], retry=True)
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(Path(retried["path"]).read_bytes(), raw)
        self.assertEqual(fetched.call_count, 5)

    def test_explicit_cover_retry_only_clears_selected_file_failure(self):
        item, raw, entry = self.cover_fixture()
        with mock.patch.object(online, "get_cover_index", return_value={item["slug"]: entry}), \
                mock.patch.object(sync, "_server_http_get", side_effect=[b"bad", raw]) as fetched:
            self.assertFalse(self.call("cover.get", id=item["id"])["ok"])
            cache = next(iter(api._COVER_CACHES.values()))
            other = "other-failed-image.jpg"
            cache._failure[other] = (cache.clock() + 3600, 3)
            other_before = cache._failure[other]
            self.assertFalse(self.call("cover.get", id=item["id"])["ok"])
            self.assertEqual(fetched.call_count, 1)
            retry = self.call("cover.get", id=item["id"], retry=True)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(Path(retry["path"]).read_bytes(), raw)
        self.assertNotIn(entry["filename"], cache._failure)
        self.assertEqual(cache._failure[other], other_before)
        self.assertEqual(fetched.call_count, 2)

    def test_existing_corrupt_cover_is_revalidated_and_replaced(self):
        item, raw, entry = self.cover_fixture()
        path = Path(db.covers_dir()) / entry["filename"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"corrupted existing cache")
        with mock.patch.object(online, "get_cover_index", return_value={item["slug"]: entry}), \
                mock.patch.object(sync, "_server_http_get", return_value=raw) as fetched:
            result = self.call("cover.get", id=item["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["path"]).read_bytes(), raw)
        self.assertEqual(fetched.call_count, 1)

    def job(self):
        return {"id": "test", "running": True, "text": "", "progress": None, "summary": None,
                "error": None, "log": [], "stop": threading.Event(), "catalog_id": "all",
                "catalog_updated": False, "cancelled": False}

    def test_all_sync_runs_in_order_and_preserves_partial_success(self):
        job = self.job()
        calls = []
        def pull(conn, **kwargs):
            calls.append(kwargs["catalog_id"])
            if kwargs["catalog_id"] == "dlsite":
                raise RuntimeError("Simulated offline")
            db.set_pref(conn, "test.completed.catalogue", "western_slg")
            return {"new": 2, "catalogue": 10, "covers": 1, "unchanged": False, "catalog_updated": True}
        with mock.patch.object(sync, "pull", side_effect=pull):
            api._sync_worker(job)
        self.assertEqual(calls, ["western_slg", "dlsite"])
        self.assertTrue(job["catalog_updated"])
        self.assertEqual(job["summary"]["new"], 2)
        self.assertIn("dlsite", job["error"])
        self.assertEqual(db.get_pref(api._conn(), "test.completed.catalogue"), "western_slg")
        self.assertFalse(job["running"])

    def test_all_sync_cancel_after_first_commit_never_starts_second(self):
        job = self.job()
        def pull(conn, **kwargs):
            db.set_pref(conn, "test.completed.catalogue", kwargs["catalog_id"])
            job["stop"].set()
            return {"new": 1, "catalogue": 10, "unchanged": False, "catalog_updated": True, "cancelled": False}
        with mock.patch.object(sync, "pull", side_effect=pull) as called:
            api._sync_worker(job)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(len(job["summary"]["catalogues"]), 1)
        self.assertTrue(job["cancelled"])
        self.assertTrue(job["catalog_updated"])
        self.assertEqual(db.get_pref(api._conn(), "test.completed.catalogue"), "western_slg")

    def test_all_sync_success_reports_both_and_pre_cancel_touches_neither(self):
        job = self.job()
        with mock.patch.object(sync, "pull", side_effect=[
            {"new": 2, "catalogue": 300, "unchanged": False, "catalog_updated": True},
            {"new": 1, "catalogue": 151, "unchanged": False, "catalog_updated": True},
        ]) as pulled:
            api._sync_worker(job)
        self.assertEqual([x.kwargs["catalog_id"] for x in pulled.call_args_list], ["western_slg", "dlsite"])
        self.assertEqual(job["summary"]["catalogue"], 451)
        self.assertIsNone(job["error"])
        self.assertFalse(job["cancelled"])
        job = self.job()
        job["stop"].set()
        with mock.patch.object(sync, "pull") as pulled:
            api._sync_worker(job)
        pulled.assert_not_called()
        self.assertTrue(job["cancelled"])
        self.assertFalse(job["catalog_updated"])


if __name__ == "__main__":
    unittest.main()
