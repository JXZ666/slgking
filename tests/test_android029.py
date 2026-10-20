"""Android 0.29 contracts: temporary databases, mocked HTTP/translation only."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
from datetime import datetime, timedelta, timezone

import slg_android_api as api
import slg_android_bootstrap as bootstrap
import slg_account as account
import slg_db as db
import slg_engines
import slg_sync_server as sync
import slg_translate


class Android029(unittest.TestCase):
    def setUp(self):
        self.original_bootstrap = dict(bootstrap._state)
        self.original_epoch = api._SESSION_EPOCH
        self.original_jobs = set(api._JOBS)
        self.mode = mock.patch.object(account, "_DEVELOPER_MODE", False)
        self.mode.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        api._close_conn()
        self.network = mock.patch.object(sync, "_server_http_get", side_effect=AssertionError("Unexpected network"))
        self.network.start()
        self.account_network = mock.patch.object(account, "request", side_effect=AssertionError("Unexpected account network"))
        self.account_network.start()
        bootstrap.init(self.tmp.name, crypto="stub")
        import slg_android_029
        with slg_android_029._TRANSLATION_LOCK:
            self.previous_translations = dict(slg_android_029._TRANSLATIONS)
            slg_android_029._TRANSLATIONS.clear()
        self.config = SimpleNamespace(engine="google_free", provider="test", base_url="https://example.invalid/v1",
            model="mock-model", api_key="test-secret-never-log", build=lambda: SimpleNamespace(needs_key=False))

    def tearDown(self):
        api._close_conn()
        self.account_network.stop()
        self.network.stop()
        self.env.stop()
        self.mode.stop()
        self.tmp.cleanup()
        bootstrap._state.clear()
        bootstrap._state.update(self.original_bootstrap)
        api._SESSION_EPOCH = self.original_epoch
        with api._JOBS_LOCK:
            for key in set(api._JOBS) - self.original_jobs:
                api._JOBS.pop(key, None)
        import slg_android_029
        with slg_android_029._TRANSLATION_LOCK:
            slg_android_029._TRANSLATIONS.clear()
            slg_android_029._TRANSLATIONS.update(self.previous_translations)

    def call(self, method, **params):
        return json.loads(api.call(json.dumps(dict(params, method=method))))

    def game(self, slug="contract-game", title="English game", overview="An English overview", day="2026-10-16", dlsite=False):
        c = api._conn()
        if dlsite:
            slug = "dlsite-rj02900001"
        gid, _ = db.upsert_game(c, slug, "https://www.dlsite.com/maniax/work/=/product_id/RJ02900001.html" if dlsite else "https://dikgames.com/contract", title, last_updated=day)
        c.execute("UPDATE games SET overview=? WHERE id=?", (overview, gid))
        c.commit()
        return gid

    def wait_job(self, started):
        self.assertTrue(started.get("ok"), started)
        self.assertTrue(started.get("job"), started)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.call("job.poll", job=started["job"])
            self.assertTrue(state["ok"], state)
            if not state.get("running"):
                return state
            time.sleep(0.01)
        self.fail("mock translation did not complete")

    def test_date_presets_include_natural_day_boundary(self):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for days in (7, 30, 90):
            with self.subTest(days=days):
                inside = self.game(slug=f"inside-{days}", day=(today - timedelta(days=days - 1)).isoformat())
                outside = self.game(slug=f"outside-{days}", day=(today - timedelta(days=days)).isoformat())
                result = self.call("catalog.list", date_preset=str(days), page_size=50)
                self.assertTrue(result["ok"], result)
                ids = {x["id"] for x in result["items"]}
                self.assertIn(inside, ids)
                self.assertNotIn(outside, ids)

    def test_custom_date_invalid_and_inclusive(self):
        gid = self.game()
        ok = self.call("catalog.list", date_preset="custom", date_start="2026-10-16", date_end="2026-10-16")
        self.assertTrue(ok["ok"], ok)
        self.assertEqual([gid], [x["id"] for x in ok["items"]])
        for first, last in (("2026-02-29", "2026-10-16"), ("2026-10-17", "2026-10-16"), (None, None), ("2026-1-01", "2026-10-16")):
            self.assertFalse(self.call("catalog.list", date_preset="custom", date_start=first, date_end=last)["ok"])
        self.assertFalse(self.call("catalog.list", date_preset="bogus")["ok"])

    def test_dlsite_unknown_updated_date_does_not_use_release_or_legacy(self):
        gid = self.game(dlsite=True, day="2026-10-16")
        db.set_dlsite_metadata(api._conn(), gid, {"release_date": "2026-10-16", "updated_date": None})
        api._conn().commit()
        result = self.call("catalog.list", catalog_id="dlsite", date_preset="custom", date_start="2026-10-16", date_end="2026-10-16")
        self.assertTrue(result["ok"], result)
        self.assertEqual(0, result["total"])
        self.assertEqual(1, self.call("catalog.list", catalog_id="dlsite")["total"])

    def test_hidden_preserves_state_and_backup_roundtrip(self):
        gid = self.game()
        self.assertTrue(self.call("game.rate", id=gid, stars=4)["ok"])
        self.assertTrue(self.call("game.state", id=gid, status="want")["ok"])
        self.assertTrue(self.call("game.note", id=gid, note="keep personal data")["ok"])
        self.assertTrue(self.call("game.hidden.set", id=gid, hidden=True)["ok"])
        self.assertEqual(0, self.call("catalog.list")["total"])
        hidden = self.call("catalog.list", catalog_id="all", hidden_mode="only")
        self.assertEqual([gid], [x["id"] for x in hidden["items"]])
        original = self.call("game.get", id=gid)["game"]
        self.assertEqual((4, "want", "keep personal data", True), (original["my_rating"], original["status"], original["note"], original["hidden"]))
        payload = self.call("backup.export")["data"]
        path = Path(self.tmp.name) / "backup.json"
        path.write_text(json.dumps({"data": payload}), encoding="utf-8")
        self.call("game.hidden.set", id=gid, hidden=False)
        result = self.call("backup.import", path=str(path))
        self.assertTrue(result["ok"], result)
        restored = self.call("game.get", id=gid)["game"]
        self.assertTrue(restored["hidden"])
        self.assertEqual("keep personal data", restored["note"])
        self.assertTrue(self.call("game.hidden.set", id=gid, hidden=False)["ok"])
        self.assertEqual(1, self.call("catalog.list")["total"])
        self.assertFalse(self.call("game.hidden.set", id=gid, hidden="false")["ok"])

    def test_collection_status_date_name_and_tags_are_combined(self):
        specs = [
            ("first", "Matching title", "2026-10-16", "want", "RPG"),
            ("old-date", "Matching old", "2026-09-01", "want", "RPG"),
            ("other-status", "Matching downloaded", "2026-10-16", "downloaded", "RPG"),
            ("other-tag", "Matching tag", "2026-10-16", "want", "Other"),
            ("other-name", "Unrelated name", "2026-10-16", "want", "RPG"),
        ]
        c = api._conn()
        cid = db.create_collection(c, "Test collection")
        first = None
        for slug, title, day, status, tag in specs:
            gid = self.game(slug=slug, title=title, day=day)
            if first is None:
                first = gid
            db.set_game_collections(c, gid, [cid])
            db.set_state(c, gid, status=status)
            db.set_tags(c, gid, [tag], clear=True)
        c.commit()
        result = self.call("catalog.list", view="collection", catalog_id="all", collection_id=cid, q="Matching", tags=["RPG"], statuses=["want"], date_preset="custom", date_start="2026-10-01", date_end="2026-10-31")
        self.assertTrue(result["ok"], result)
        self.assertEqual([first], [x["id"] for x in result["items"]])

    def test_game_get_keeps_full_original_and_translation_state(self):
        source = "Long original. " * 400
        gid = self.game(overview=source)
        missing = self.call("game.get", id=gid)["game"]
        self.assertEqual(source, missing["overview"])
        self.assertEqual("missing", missing["overview_translation_state"])
        db.set_auto_translation(api._conn(), "overview", gid, source, "机器简介", engine="mock")
        db.set_manual_translation(api._conn(), "title", gid, "人工名称")
        detail = self.call("game.get", id=gid)["game"]
        self.assertEqual(source, detail["overview"])
        self.assertEqual(("机器简介", "auto", "manual"), (detail["overview_zh"], detail["overview_translation_state"], detail["title_translation_state"]))

    def test_manual_save_clear_reveals_cache_without_changing_original(self):
        gid = self.game()
        db.set_auto_translation(api._conn(), "overview", gid, "An English overview", "缓存简介", engine="mock")
        result = self.call("game.translation.save", id=gid, kind="overview", text="人工简介")
        self.assertTrue(result["ok"], result)
        self.assertEqual(("人工简介", "manual"), (result["game"]["overview_zh"], result["game"]["overview_translation_state"]))
        cleared = self.call("game.translation.save", id=gid, kind="overview", text="")
        self.assertEqual(("缓存简介", "auto"), (cleared["game"]["overview_zh"], cleared["game"]["overview_translation_state"]))
        self.assertEqual("An English overview", cleared["game"]["overview"])
        self.assertFalse(self.call("game.translation.save", id=gid, kind="invalid", text="no")["ok"])
        self.assertFalse(self.call("game.translation.save", id=gid, kind="title", text=123)["ok"])

    def test_tags_are_canonical_deduped_and_invalid_input_atomic(self):
        gid = self.game()
        result = self.call("game.tags.set", id=gid, tags=[" RPG ", "RPG", "Story", ""])
        self.assertTrue(result["ok"], result)
        tags = {x["name"] for x in result["game"]["tags"]}
        self.assertEqual({"RPG", "Story"}, tags)
        for invalid in ("RPG", [1], ["x" * 201], [str(i) for i in range(501)]):
            self.assertFalse(self.call("game.tags.set", id=gid, tags=invalid)["ok"])
            self.assertEqual(tags, {x["name"] for x in self.call("game.get", id=gid)["game"]["tags"]})

    def test_account_profile_is_read_only_and_strips_credentials(self):
        with mock.patch.object(account, "authenticated", return_value={"nickname": "test", "balance": 4, "login_key": "secret", "device_token": "token", "recovery_code": "code"}) as get, mock.patch.object(account, "migrate_legacy", side_effect=AssertionError("must not migrate")):
            result = self.call("account.profile")
        self.assertTrue(result["ok"], result)
        get.assert_called_once_with("/account/me")
        self.assertFalse({"login_key", "device_token", "recovery_code"} & result["me"].keys())

    def test_public_profile_hides_decorations_and_internal_fields(self):
        public_id = "pp_" + "a" * 32
        for visible in (False, True, 1, "true"):
            with self.subTest(visible=visible), mock.patch.object(account, "public_profile", return_value={"profile": {"public_id": public_id, "nickname": "Public", "profile_visible": visible, "profile_message": "hidden note", "equipped_title": "vip", "equipped_appearances": {"avatar_frame": "frame"}, "account_id": "internal", "avatar_path": "/private/me.jpg", "device_token": "secret"}}):
                result = self.call("account.public_profile", public_id=public_id)
                self.assertTrue(result["ok"], result)
                profile = result["profile"]
                self.assertFalse({"account_id", "avatar_path", "device_token"} & profile.keys())
                self.assertEqual(visible is True, profile["profile_visible"])
                self.assertEqual(visible is True, "profile_message" in profile)

    def test_reply_page_and_context_keep_ids_and_twenty_item_limit(self):
        with mock.patch.object(account, "request", return_value={"ok": True, "items": []}) as request:
            self.assertTrue(self.call("replies.page", root_id="root_1", page=2, focus_id="reply_1")["ok"])
            self.assertIn("/comments/root_1/replies?", request.call_args.args[0])
            self.assertIn("limit=20", request.call_args.args[0]); self.assertIn("focus_id=reply_1", request.call_args.args[0])
            self.assertTrue(self.call("replies.context", id="reply_1")["ok"])
            self.assertEqual("/comment-replies/reply_1/context", request.call_args.args[0])
            self.assertFalse(self.call("replies.page", root_id="../bad")["ok"])

    def test_reply_post_uuid_validation_and_developer_write_block(self):
        operation = str(uuid.uuid4())
        with mock.patch.object(account, "session", return_value={"account_id": "ordinary"}), mock.patch.object(account, "authenticated", return_value={"ok": True}) as send:
            self.assertTrue(self.call("replies.post", root_id="root_1", content="hello reply", reply_to_id="reply_2", operation_id=operation)["ok"])
            self.assertEqual("POST", send.call_args.kwargs["method"])
            self.assertEqual(operation, send.call_args.kwargs["payload"]["operation_id"])
            self.assertEqual("reply_2", send.call_args.kwargs["payload"]["reply_to_id"])
            send.reset_mock()
            self.assertFalse(self.call("replies.post", root_id="root_1", content="hello", operation_id="not-uuid")["ok"])
            self.assertFalse(self.call("replies.post", root_id="root_1", content="x", operation_id=operation)["ok"])
            send.assert_not_called()
        with mock.patch.object(account, "session", return_value={"account_id": "developer"}), mock.patch.object(account, "authenticated") as send:
            self.assertFalse(self.call("replies.post", root_id="root_1", content="hello", operation_id=operation)["ok"])
            self.assertFalse(self.call("replies.report", id="reply_1")["ok"])
            send.assert_not_called()

    def test_replies_own_delete_report_and_notifications_read_explicit(self):
        with mock.patch.object(account, "session", return_value={"account_id": "ordinary"}), mock.patch.object(account, "authenticated", return_value={"ok": True, "items": []}) as send:
            self.assertTrue(self.call("replies.mine", root_id="root_1", page=3)["ok"])
            self.assertIn("root_id=root_1", send.call_args.args[0])
            self.assertTrue(self.call("replies.delete", id="reply_1")["ok"])
            self.assertEqual("DELETE", send.call_args.kwargs["method"])
            self.assertTrue(self.call("replies.report", id="reply_1")["ok"])
            self.assertTrue(self.call("notifications.list", page=2)["ok"])
            self.assertIn("/account/notifications?", send.call_args.args[0])
            self.assertNotIn("method", send.call_args.kwargs)
            self.assertTrue(self.call("notifications.read", ids=["n_1"])["ok"])
            self.assertEqual({"ids": ["n_1"]}, send.call_args.kwargs["payload"])
            self.assertTrue(self.call("notifications.read", all=True)["ok"])
            self.assertEqual({"all": True}, send.call_args.kwargs["payload"])
            self.assertFalse(self.call("notifications.read", ids=[])["ok"])

    def test_slow_profile_does_not_block_local_reads_and_logout_discards_result(self):
        account.save_session("account-A", "token-A")
        entered, release, probe_done = threading.Event(), threading.Event(), threading.Event()
        output = {}
        def remote(*args, **kwargs):
            output["captured_identity"] = account.session()["account_id"]
            entered.set()
            if not release.wait(5):
                raise AssertionError("test barrier not released")
            return {"nickname": "Account A", "balance": 123}
        def load():
            try:
                output["profile"] = self.call("account.profile")
            finally:
                api._close_conn()
        def local_reads():
            try:
                output["status"] = self.call("account.status")
                output["catalogue"] = self.call("catalog.list")
                output["logout"] = self.call("account.logout")
            finally:
                api._close_conn()
                probe_done.set()
        with mock.patch.object(account, "authenticated", side_effect=remote):
            worker = threading.Thread(target=load)
            probe = threading.Thread(target=local_reads)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                probe.start()
                self.assertTrue(probe_done.wait(2), "local operations blocked behind remote profile")
                self.assertEqual("account-A", output["status"]["account_id"])
                self.assertTrue(output["catalogue"]["ok"])
                self.assertTrue(output["logout"]["ok"])
            finally:
                release.set()
                worker.join(4)
                if probe.ident is not None:
                    probe.join(4)
        self.assertEqual("account-A", output["captured_identity"])
        self.assertFalse(output["profile"]["ok"], "stale account A must be discarded after logout")
        self.assertNotIn("me", output["profile"])
        self.assertFalse(self.call("account.status")["logged_in"])

    def test_translation_empty_overview_skipped_and_cached_second_request(self):
        gid = self.game(overview="")
        with mock.patch.object(slg_engines, "resolve_config", return_value=self.config), mock.patch.object(slg_translate, "translate_title", return_value="自动名称") as title, mock.patch.object(slg_translate, "translate_overview", side_effect=AssertionError("empty overview must not translate")) as overview:
            first = self.wait_job(self.call("game.translate.start", id=gid, fields=["title", "overview"]))
            self.assertEqual("skipped", first["summary"]["fields"]["overview"]["status"])
            self.assertEqual("自动名称", first["summary"]["fields"]["title"]["text"])
            self.wait_job(self.call("game.translate.start", id=gid, fields=["title", "overview"]))
            self.assertEqual(1, title.call_count); overview.assert_not_called()

    def test_translation_partial_failure_never_leaks_provider_key(self):
        gid = self.game()
        with mock.patch.object(slg_engines, "resolve_config", return_value=self.config), mock.patch.object(slg_translate, "translate_title", return_value="成功标题"), mock.patch.object(slg_translate, "translate_overview", side_effect=RuntimeError("https://bad.invalid?api_key=test-secret-never-log")):
            result = self.wait_job(self.call("game.translate.start", id=gid, fields=["title", "overview"]))
        fields = result["summary"]["fields"]
        self.assertEqual("succeeded", fields["title"]["status"])
        self.assertEqual("error", fields["overview"]["status"])
        self.assertNotIn("test-secret-never-log", json.dumps(result))
        self.assertNotIn("bad.invalid", json.dumps(result))

    def test_translation_inflight_deduplicates_and_old_source_not_written(self):
        gid = self.game()
        started, release = threading.Event(), threading.Event()
        def provider(*args, **kwargs):
            started.set(); self.assertTrue(release.wait(5)); return "过时译名"
        with mock.patch.object(slg_engines, "resolve_config", return_value=self.config), mock.patch.object(slg_translate, "translate_title", side_effect=provider) as translate:
            try:
                first = self.call("game.translate.start", id=gid, fields=["title"])
                self.assertTrue(started.wait(3))
                second = self.call("game.translate.start", id=gid, fields=["title"])
                self.assertEqual(first["job"], second["job"])
                c = api._conn(); c.execute("UPDATE games SET title='Changed source' WHERE id=?", (gid,)); c.commit()
            finally:
                release.set()
            result = self.wait_job(first)
            translate.assert_called_once()
        self.assertEqual("skipped", result["summary"]["fields"]["title"]["status"])
        self.assertIsNone(db.get_translation_row(api._conn(), "title", gid, "Changed source"))

    def test_overlapping_field_requests_share_inflight_title_translation(self):
        gid = self.game()
        title_started, overview_started, release = threading.Event(), threading.Event(), threading.Event()
        def title_provider(*args, **kwargs):
            title_started.set()
            if not release.wait(5):
                raise AssertionError("title test barrier timed out")
            return "共同译名"
        def overview_provider(*args, **kwargs):
            overview_started.set()
            return "简介译文"
        with mock.patch.object(slg_engines, "resolve_config", return_value=self.config), mock.patch.object(slg_translate, "translate_title", side_effect=title_provider) as title, mock.patch.object(slg_translate, "translate_overview", side_effect=overview_provider):
            try:
                first = self.call("game.translate.start", id=gid, fields=["title"])
                self.assertTrue(title_started.wait(2))
                second = self.call("game.translate.start", id=gid, fields=["title", "overview"])
                self.assertTrue(overview_started.wait(2))
            finally:
                release.set()
            self.wait_job(first)
            self.wait_job(second)
            self.assertEqual(1, title.call_count, "overlapping requests must share the title field's in-flight work")

    def test_translation_missing_api_key_is_visible_without_provider_request(self):
        gid = self.game()
        config = SimpleNamespace(engine="openai", provider="test", base_url="https://example.invalid/v1", model="mock", api_key="", build=lambda: SimpleNamespace(needs_key=True))
        with mock.patch.object(slg_engines, "resolve_config", return_value=config), mock.patch.object(slg_translate, "translate_title") as translate:
            result = self.wait_job(self.call("game.translate.start", id=gid, fields=["title"]))
        translate.assert_not_called()
        self.assertEqual("error", result["summary"]["fields"]["title"]["status"])
        self.assertIn("API Key", result["summary"]["fields"]["title"]["error"])

    def test_title_guard_is_cached_as_untranslatable(self):
        gid = self.game()
        with mock.patch.object(slg_engines, "resolve_config", return_value=self.config), mock.patch.object(slg_translate, "translate_title", side_effect=slg_translate.TitleGuardError("guarded")) as translate:
            self.wait_job(self.call("game.translate.start", id=gid, fields=["title"]))
            self.wait_job(self.call("game.translate.start", id=gid, fields=["title"]))
            self.assertEqual(1, translate.call_count)
        detail = self.call("game.get", id=gid)["game"]
        self.assertEqual("English game", detail["title"])
        self.assertEqual("untranslatable", detail["title_translation_state"])

    def test_manual_edit_wins_over_late_translation(self):
        gid = self.game()
        started, release = threading.Event(), threading.Event()
        def provider(*args, **kwargs):
            started.set(); self.assertTrue(release.wait(5)); return "自动译名"
        with mock.patch.object(slg_engines, "resolve_config", return_value=self.config), mock.patch.object(slg_translate, "translate_title", side_effect=provider):
            try:
                job = self.call("game.translate.start", id=gid, fields=["title"])
                self.assertTrue(started.wait(3))
                result = self.call("game.translation.save", id=gid, kind="title", text="人工优先")
                self.assertTrue(result["ok"], result)
            finally:
                release.set()
            self.wait_job(job)
        detail = self.call("game.get", id=gid)["game"]
        self.assertEqual("人工优先", detail["title_zh"])
        self.assertEqual("manual", detail["title_translation_state"])


if __name__ == "__main__":
    unittest.main()
