"""Android contracts exercised offline with disposable data and mocked cloud."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

import slg_android_api as api
import slg_android_bootstrap as bootstrap
import slg_db
import slg_titles
import slg_update


class AndroidParity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        api._close_conn()
        bootstrap.init(self.tmp.name, crypto="stub")

    def tearDown(self):
        api._close_conn()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, method, **params):
        return json.loads(api.call(json.dumps(dict(params, method=method))))

    def backup(self, payload):
        path = Path(self.tmp.name) / "backup.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_categories_crud_and_filters(self):
        both = self.call("usergames.add", title="Both", categories=["rpg", "slg"], tags=["x", "y"])["id"]
        none = self.call("usergames.add", title="None")["id"]
        self.assertEqual(self.call("game.get", id=both)["game"]["categories"], ["slg", "rpg"])
        self.call("usergames.update", id=both, title="Kept", tags=["x", "y"])
        self.assertEqual(self.call("game.get", id=both)["game"]["categories"], ["slg", "rpg"])
        rows = self.call("catalog.list", view="user", category_ids=["slg", "rpg"], tags=["x", "y"])
        self.assertEqual([x["id"] for x in rows["items"]], [both])
        self.assertEqual(self.call("catalog.list", view="user", uncategorized=True)["items"][0]["id"], none)
        self.assertEqual(self.call("catalog.list", view="user", category_ids=["slg"], uncategorized=True)["total"], 2)
        self.call("usergames.update", id=both, title="Cleared", categories=[])
        self.assertEqual(self.call("game.get", id=both)["game"]["categories"], [])

    def test_collection_includes_user_games_across_pages(self):
        cid = self.call("collection.save", name="Paged")["id"]
        expected = set()
        for index in range(85):
            gid = self.call("usergames.add", title=f"Paged {index:03}")["id"]
            expected.add(gid)
            self.call("game.collections.set", id=gid, collection_ids=[cid])
        seen = []
        for page in range(1, 4):
            result = self.call("catalog.list", view="collection", collection_id=cid,
                               page=page, page_size=30)
            self.assertEqual(result["total"], 85)
            seen.extend(item["id"] for item in result["items"])
        self.assertEqual(len(seen), 85)
        self.assertEqual(set(seen), expected)

    def test_invalid_categories_do_not_mutate(self):
        gid = self.call("usergames.add", title="Original", categories=["slg"])["id"]
        self.assertFalse(self.call("usergames.update", id=gid, title="Bad", categories=["unknown"])["ok"])
        self.assertEqual(self.call("game.get", id=gid)["game"]["title"], "Original")
        self.assertFalse(self.call("usergames.add", title="Bad", categories=["unknown"])["ok"])

    def test_mobile_edit_preserves_pc_only_fields_when_omitted(self):
        gid = self.call("usergames.add", title="Imported", categories=["slg"],
                        overview="PC description", tags=["x"], promoted=True)["id"]
        self.call("usergames.update", id=gid, title="Renamed", categories=["rpg"])
        game = self.call("game.get", id=gid)["game"]
        self.assertEqual(game["overview"], "PC description")
        self.assertEqual(game["tags"][0]["name"], "x")
        self.assertTrue(game["promoted"])

    def test_writes_visible_to_other_dispatcher_threads_and_restart(self):
        def worker():
            try:
                return self.call("usergames.add", title="Threaded", categories=["slg"])
            finally:
                api._close_conn()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(worker).result(timeout=10)
        self.assertTrue(result["ok"])
        self.assertEqual(self.call("game.get", id=result["id"])["game"]["categories"], ["slg"])
        self.assertFalse(api._conn().in_transaction)
        api._close_conn()
        self.assertEqual(self.call("catalog.list", view="user")["total"], 1)

    def test_failed_bridge_write_rolls_back_and_releases_lock(self):
        def failing(req):
            api._conn().execute("INSERT INTO prefs(key,value) VALUES ('partial','bad')")
            raise RuntimeError("injected failure")
        with mock.patch.dict(api._HANDLERS, {"test.failure": failing}):
            self.assertFalse(self.call("test.failure")["ok"])
        self.assertFalse(api._conn().in_transaction)
        self.assertIsNone(slg_db.get_pref(api._conn(), "partial"))

    def test_pc_android_roundtrip_preview_and_data_preserved(self):
        gid = self.call("usergames.add", title="Roundtrip", categories=["galgame", "slg"])["id"]
        self.call("game.rate", id=gid, stars=4)
        self.call("game.note", id=gid, note="private note")
        self.call("game.state", id=gid, status="want")
        cid = self.call("collection.save", name="Test")["id"]
        self.call("game.collections.set", id=gid, collection_ids=[cid])
        self.call("comments.post", slug=self.call("game.get", id=gid)["game"]["slug"], content="private", public=False)
        before = slg_db.export_user_data(api._conn())
        path = self.backup({"app": "slgking", "version": "0.25.0", "data": before})
        preview = self.call("backup.preview", path=path)
        self.assertTrue(preview["ok"])
        self.assertGreater(preview["preview"]["total_records"], 0)
        self.assertEqual(before, slg_db.export_user_data(api._conn()))
        self.assertTrue(self.call("backup.import", path=path)["ok"])
        exported = self.call("backup.export")["data"]
        other = slg_db.connect(":memory:")
        try:
            slg_db.import_user_data(other, exported)
            self.assertEqual(exported, slg_db.export_user_data(other))
        finally:
            other.close()
        self.assertTrue(self.call("backup.import", path=self.backup(exported))["ok"])

    def test_bad_backup_never_changes_data(self):
        self.call("usergames.add", title="Safe", categories=["slg"])
        before = slg_db.export_user_data(api._conn())
        for payload in ([1], {"data": {"state": "broken"}}, {"unknown": []}):
            path = self.backup(payload)
            self.assertFalse(self.call("backup.preview", path=path)["ok"])
            self.assertFalse(self.call("backup.import", path=path)["ok"])
            self.assertEqual(before, slg_db.export_user_data(api._conn()))

    def test_cloud_lottery_is_authoritative_and_draw_is_wrapped(self):
        account = mock.Mock()
        account.session.return_value = {"account_id": "test"}
        account.me.return_value = {"draws_today": 2, "balance": 43, "recent_lottery_history": [{"kind": "points", "value": 20}]}
        account.lottery_draw.return_value = {"prize": {"kind": "points", "value": 20}, "balance": 58}
        with mock.patch.object(api, "_account_module", return_value=account):
            status = self.call("lottery.status")
            self.assertEqual((status["draws_today"], status["balance"], status["source"]), (2, 43, "cloud"))
            self.assertEqual(status["history"][0]["value"], 20)
            self.assertEqual(self.call("lottery.spin", request_id="once")["draw"]["balance"], 58)
            account.lottery_draw.assert_called_once_with("once")
            account.me.side_effect = RuntimeError("offline")
            self.assertFalse(self.call("lottery.status")["ok"])

    def test_wardrobe_appearance_and_clear(self):
        item = next(i for i in slg_titles.SHOP_ITEMS if i.get("appearance") == "avatar_frame")
        account = mock.Mock()
        account.me.return_value = {"titles": [item["id"]], "equipped_appearances": {"avatar_frame": item["id"]}}
        account.equip_appearance.return_value = {}
        account.equip_cosmetic.return_value = {}
        with mock.patch.object(api, "_account_module", return_value=account):
            result = self.call("wardrobe.list")
            self.assertEqual(result["appearances"][0]["slot"], "avatar_frame")
            self.assertTrue(result["appearances"][0]["equipped"])
            self.call("wardrobe.equip", appearance_slot="avatar_frame", item_id="")
            account.equip_appearance.assert_called_once_with("avatar_frame", "")
            self.call("wardrobe.equip", cosmetic_id="")
            account.equip_cosmetic.assert_called_once_with("")

    def test_cloud_collection_envelopes_are_unwrapped(self):
        account = mock.Mock()
        account.devices.return_value = {"devices": [{"id": 7, "label": "mock device"}]}
        account.authenticated.return_value = {"comments": [{"id": 9, "content": "mock comment"}]}
        account.leaderboard.return_value = {"board": "points", "entries": [{"rank": 1, "score": 20}]}
        with mock.patch.object(api, "_account_module", return_value=account):
            self.assertEqual(self.call("account.devices")["devices"][0]["id"], 7)
            self.assertEqual(self.call("comments.mine")["comments"][0]["id"], 9)
            self.assertEqual(self.call("leaderboard.list")["board"]["entries"][0]["score"], 20)


class AndroidUpdates(unittest.TestCase):
    def test_absent_single_apk_and_offline(self):
        for assets, expected in [([], None), ([{"name": "slgking-android-0.25.0-a3.apk", "browser_download_url": "apk"}], "apk")]:
            raw = json.dumps({"tag_name": "v0.25.0", "assets": assets}).encode()
            with mock.patch("slg_scrape.http_get", return_value=raw):
                release = slg_update.latest_release(want=".apk")
                self.assertEqual(release["asset_url"], expected)
                self.assertEqual("asset_error" in release, expected is None)
        with mock.patch("slg_scrape.http_get", side_effect=TimeoutError):
            self.assertIsNone(slg_update.latest_release(want=".apk"))

    def test_asset_version_not_stable_tag_controls_android_updates(self):
        conn = slg_db.connect(":memory:")
        try:
            for name, local, expected in [
                ("slgking-android-0.25.0-a3.apk", "0.25.0-a2", "0.25.0-a3"),
                ("slgking-android-0.25.0-a3.apk", "0.25.0-a3", None),
                ("slgking-android-0.25.0-a2.apk", "0.25.0-a3", None),
                ("slgking-android-0.25.0.apk", "0.25.0-a3", "0.25.0"),
            ]:
                raw = json.dumps({"tag_name": "v0.25.0", "assets": [
                    {"name": name, "browser_download_url": "apk"}]}).encode()
                with mock.patch("slg_scrape.http_get", return_value=raw):
                    result = slg_update.check(conn, local, force=True, want=".apk", android=True)
                self.assertEqual(result["version"] if result else None, expected)
                if result:
                    self.assertEqual(result["release_tag"], "v0.25.0")
        finally:
            conn.close()

    def test_invalid_android_assets_manual_error_without_automatic_download(self):
        conn = slg_db.connect(":memory:")
        try:
            for names in [[], ["candidate.apk"], ["slgking-android-garbage.apk"],
                          ["one.apk", "two.apk"]]:
                raw = json.dumps({"tag_name": "v0.25.0", "assets": [
                    {"name": name, "browser_download_url": "apk"} for name in names]}).encode()
                with mock.patch("slg_scrape.http_get", return_value=raw):
                    result = slg_update.check(conn, "0.25.0-a3", force=True, want=".apk", android=True)
                    self.assertIn("asset_error", result)
                    self.assertIsNone(result["asset_url"])
                    slg_db.set_pref(conn, slg_update.PREF_CHECKED_AT, "")
                    self.assertIsNone(slg_update.check(conn, "0.25.0-a3", want=".apk", android=True))
        finally:
            conn.close()

    def test_candidates_and_stable(self):
        for remote, local in [("0.25.0-a2", "0.25.0-a1"), ("v0.25.0", "0.25.0-a2"), ("0.25.1-a1", "0.25.0")]:
            self.assertTrue(slg_update.android_is_newer(remote, local))
            self.assertFalse(slg_update.android_is_newer(local, remote))
        self.assertFalse(slg_update.android_is_newer("garbage", "0.25.0"))
        self.assertFalse(slg_update.is_newer("v0.25.0", "0.25.0-a2"))

    def test_multiple_apks_are_not_selected_and_desktop_unchanged(self):
        assets = [{"name": "slgking.exe", "browser_download_url": "exe"}, {"name": "one.apk", "browser_download_url": "one"}, {"name": "two.apk", "browser_download_url": "two"}]
        raw = json.dumps({"tag_name": "v0.25.1", "assets": assets}).encode()
        with mock.patch("slg_scrape.http_get", return_value=raw):
            result = slg_update.latest_release(want=".apk")
            self.assertIsNone(result["asset_url"])
            self.assertIn("asset_error", result)
            self.assertEqual(slg_update.latest_release(want="slgking.exe")["asset_url"], "exe")


if __name__ == "__main__":
    unittest.main()
