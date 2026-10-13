"""Execute actual App identity/write handlers without Tk startup or networking."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from slg_refresh_coordination import RefreshCoordinator


def app_handlers():
    path = Path(__file__).resolve().parents[1] / 'slg_gui.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    app = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == 'App')
    names = {'_reset_identity_state', '_shop_coord', '_shop_wallet_busy',
             '_finish_shop_write', '_cloud_action_result'}
    methods = [node for node in app.body
               if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    probe = ast.Module(body=[ast.ClassDef(
        name='AppHandlers', bases=[], keywords=[], body=methods,
        decorator_list=[])], type_ignores=[])
    scope = {'RefreshCoordinator': RefreshCoordinator,
             'slg_titles': SimpleNamespace(DEFAULT_TITLE_ID='default')}
    exec(compile(ast.fix_missing_locations(probe), str(path), 'exec'), scope)
    return scope['AppHandlers']


class ShopIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = app_handlers()

    def setUp(self):
        app = self.app = self.handlers()
        app._identity_generation = 10
        app.account_id = 'account-a'
        app._is_admin_mode = lambda: False
        app._current_session_account_id = lambda: app.account_id
        app.after_idle = lambda callback: 1
        app.after_cancel = lambda job: None
        app._flush_shop_domains = lambda domains: None
        app._reset_quests_for_account = lambda: None
        app._refresh_announcement_badge = lambda: None
        app.closed_identity_windows = 0
        app._close_identity_scoped_dialogs = lambda: setattr(
            app, 'closed_identity_windows', app.closed_identity_windows + 1)
        app._quest_generation = 3
        app._quests_fetching = False
        app._mark_shop = lambda *domains: app._shop_coord().mark(*domains)
        app._cloud_balance = 70
        app._lottery_request_active = True
        app.old_completion_calls = []
        app._lottery_request_complete = app.old_completion_calls.append
        self.old_token = (10, 'account-a', 20, 'lottery')
        app._shop_wallet_pending = self.old_token
        app._lottery_pending_pref_key = 'lottery.pending_request.account-a'
        app.prefs = {app._lottery_pending_pref_key: 'a' * 32}
        app._shop_coord().open_page()

    def switch_identity(self):
        self.app.account_id = 'account-b'
        self.app._reset_identity_state()

    def test_switch_retires_old_active_wallet_state(self):
        self.assertTrue(self.app._shop_wallet_busy())
        self.switch_identity()
        self.assertEqual(self.app.closed_identity_windows, 1)
        self.assertIsNone(self.app._shop_wallet_pending)
        self.assertFalse(self.app._lottery_request_active)
        self.assertIsNone(self.app._lottery_request_complete)
        self.assertFalse(self.app._shop_wallet_busy())
        self.assertIsNotNone(self.app._shop_coord().begin_read('me'))

    def test_old_result_cannot_clear_new_account_lottery(self):
        self.switch_identity()
        new_token = (self.app._identity_generation, 'account-b', 21, 'lottery')
        self.app._shop_wallet_pending = new_token
        self.app._lottery_request_active = True
        new_completion_calls = []
        completion = new_completion_calls.append
        self.app._lottery_request_complete = completion
        self.app._cloud_balance = 80
        for error in (None, 'old request failed'):
            self.app._cloud_action_result(
                'lottery', {'balance': 999}, error,
                10, 'account-a', self.old_token)
            self.assertEqual(self.app._shop_wallet_pending, new_token)
            self.assertTrue(self.app._lottery_request_active)
            self.assertIs(self.app._lottery_request_complete, completion)
            self.assertEqual(self.app._cloud_balance, 80)
        self.assertEqual(new_completion_calls, [])
        self.assertEqual(self.app.old_completion_calls, [])

    def test_old_confirmed_result_keeps_original_account_retry_key(self):
        key = self.app._lottery_pending_pref_key
        request_id = self.app.prefs[key]
        self.switch_identity()
        self.app._cloud_action_result(
            'lottery', {'balance': 999, 'prize': {'kind': 'points'}}, None,
            10, 'account-a', self.old_token)
        self.assertEqual(self.app.prefs[key], request_id)
        self.assertFalse(self.app._shop_wallet_busy())
        self.assertEqual(self.app.old_completion_calls, [])


if __name__ == '__main__':
    unittest.main()
