"""Campaign bridge contracts: fake codes, isolated state, and mocked HTTP only."""
import json
import tempfile
import threading
import unittest
from unittest import mock

import slg_account as account
import slg_android_api as api
import slg_android_bootstrap as boot
import slg_titles as titles


FAKE_CODE = "ECHOES-01234567-89ABCDEF-01234567-89ABCDEF"


class CampaignBridgeTest(unittest.TestCase):
    def setUp(self):
        self.old_boot = dict(boot._state)
        self.old_epoch = api._SESSION_EPOCH
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict("os.environ", {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        api._close_conn()
        boot.init(self.tmp.name, crypto="stub")
        self.session = mock.patch.object(account, "session", return_value={
            "account_id": "campaign-test-only", "device_token": "fake-device-token"})
        self.session.start()
        self.request = mock.patch.object(account, "request", side_effect=AssertionError("unexpected HTTP"))
        self.request.start()

    def tearDown(self):
        api._close_conn()
        self.request.stop()
        self.session.stop()
        self.env.stop()
        self.tmp.cleanup()
        boot._state.clear()
        boot._state.update(self.old_boot)
        api._SESSION_EPOCH = self.old_epoch

    def redeem(self, value):
        return json.loads(api.call({"method": "redeem.code", "code": value}))

    def test_lowercase_trimmed_routes_to_campaign_with_one_request(self):
        reward = {"ok": True, "campaign_id": "echoes-launch", "title_id": "echoes_of_abyss",
                  "gained": 100, "already_claimed": False, "balance": 107}
        with mock.patch.object(account, "authenticated", return_value=reward) as request:
            result = self.redeem("  " + FAKE_CODE.lower() + "\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["redeem"], reward)
        request.assert_called_once_with("/account/rewards/campaign/redeem", method="POST", payload={"code": FAKE_CODE})

    def test_unconfirmed_or_malformed_server_result_is_not_shown_as_success(self):
        for value in [{"ok": False}, {}, {"ok": True, "gained": 100}]:
            with mock.patch.object(account, "authenticated", return_value=value):
                self.assertFalse(self.redeem(FAKE_CODE)["ok"])

    def test_repeat_keeps_server_zero_gain(self):
        reward = {"ok": True, "campaign_id": "echoes-launch", "title_id": "echoes_of_abyss",
                  "gained": 0, "already_claimed": True, "balance": 107}
        with mock.patch.object(account, "redeem_campaign", return_value=reward):
            result = self.redeem(FAKE_CODE)
        self.assertTrue(result["redeem"]["already_claimed"])
        self.assertEqual(result["redeem"]["gained"], 0)

    def test_bad_campaign_codes_do_not_fall_back_to_group(self):
        with mock.patch.object(account, "redeem_group") as group:
            for value in ["ECHOES-123", FAKE_CODE.replace("01234567", "０1234567"), FAKE_CODE + "0"]:
                self.assertFalse(self.redeem(value)["ok"])
        group.assert_not_called()

    def test_group_codes_still_use_existing_endpoint(self):
        with mock.patch.object(account, "redeem_group", return_value={"ok": True}) as group:
            self.assertTrue(self.redeem("  EXISTING-GROUP  ")["ok"])
        group.assert_called_once_with("EXISTING-GROUP")

    def test_network_error_is_not_reported_as_award(self):
        with mock.patch.object(account, "redeem_campaign", side_effect=TimeoutError("mock timeout")):
            result = self.redeem(FAKE_CODE)
        self.assertFalse(result["ok"])
        self.assertNotIn("redeem", result)

    def test_late_response_is_rejected_after_account_epoch_change(self):
        ready, finish = threading.Event(), threading.Event()
        observed = []
        def delayed(code):
            ready.set()
            self.assertTrue(finish.wait(3))
            return {"ok": True, "gained": 100}
        with mock.patch.object(account, "redeem_campaign", side_effect=delayed):
            worker = threading.Thread(target=lambda: observed.append(self.redeem(FAKE_CODE)))
            worker.start()
            self.assertTrue(ready.wait(3))
            with api._INIT_LOCK:
                api._SESSION_EPOCH += 1
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(observed[0]["ok"])
        self.assertIn("账号身份已改变", observed[0]["error"])

    def test_title_is_campaign_epic_and_never_shop_or_lottery(self):
        title = next(item for item in titles.TITLES if item["id"] == "echoes_of_abyss")
        self.assertEqual((title["name"], title["rarity"], title["obtain"]), ("深渊回响", "史诗", "campaign"))
        self.assertNotIn("cost", title)
        self.assertIn("每账号一次", titles.obtain_title_text(title))
        self.assertIsNone(account.normalize_campaign_code(FAKE_CODE.replace("ECHOES", "ＥCHOES")))


if __name__ == "__main__":
    unittest.main()
