"""Offline real-Tk integration checks for the shop GUI candidate.

Runs production candidate methods with inert account calls and captured worker
threads. No App startup, credentials, databases, or network are used.
"""
import argparse
import ast
from collections import Counter
import copy
import importlib.util
from pathlib import Path
import queue
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gui', default=str(ROOT / 'slg_gui.py'))
    parser.add_argument('--cold-recovery', action='store_true',
                        help='also require recovery after a cold-cache failed write')
    args = parser.parse_args()
    source = Path(args.gui).read_text(encoding='utf-8')
    tree = ast.parse(source)
    app_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'App')
    duplicates = [name for name, count in Counter(
        n.name for n in app_node.body if isinstance(n, ast.FunctionDef)).items() if count > 1]
    assert not duplicates, duplicates
    spec = importlib.util.spec_from_file_location('shop_candidate', args.gui)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctk = mod.ctk
    calls = Counter()
    workers = []
    account = {'account_id': 'offline-a', 'device_token': 'not-a-real-token'}
    today = mod.datetime.now(mod.timezone(mod.timedelta(hours=8))).date().isoformat()
    initial = {'account_id': account['account_id'], 'balance': 50,
               'titles': ['owned'], 'signed_today': False, 'signin_month_days': [],
               'equipped_title': 'owned'}

    class Worker:
        def __init__(self, target, daemon=True):
            self.target = target
        def start(self):
            workers.append(self.target)

    mod.threading = SimpleNamespace(Thread=Worker)
    mod.slg_account.session = lambda: dict(account)
    mod.slg_account.me = lambda: (calls.update(['me']) or copy.deepcopy(initial))
    def authenticated(path, **kwargs):
        assert path == '/account/me', 'only an inert account GET is allowed'
        calls.update(['me_get'])
        return copy.deepcopy(initial)
    mod.slg_account.authenticated = authenticated
    mod.slg_account.get_quests = lambda: (calls.update(['quests']) or {'tasks': []})
    mod.slg_account.signin = lambda: (calls.update(['signin']) or {
        'balance': 60, 'gained': 10, 'bonus': 0, 'day': today,
        'signin_month_days': [today]})
    mod.slg_account.buy = lambda item_id: (calls.update(['buy']) or {
        'balance': 30, 'titles': ['owned', item_id]})
    mod.slg_titles.available_shop_items = lambda: [
        {'id': 'owned', 'kind': 'title', 'name': 'Owned', 'cost': 20, 'category': '头衔'},
        {'id': 'cheap', 'kind': 'title', 'name': 'Cheap', 'cost': 10, 'category': '头衔'},
        {'id': 'costly', 'kind': 'title', 'name': 'Costly', 'cost': 100, 'category': '头衔'},
    ]

    class Harness(mod.App):
        def __init__(self):
            ctk.CTk.__init__(self)
            self.geometry('860x760')
            self.queue = queue.Queue()
            self.conn = None
            self.detail = ctk.CTkScrollableFrame(self)
            self.detail.pack(fill='both', expand=True)
            self._panel_mode = 'game'
            self._identity_generation = 1
            self._quest_generation = 0
            self._quests_supported = None
            self._quests_fetching = False
            self._quests_error = None
            self._quests_data = None
            self._quest_claiming = None
            self._remote_flags = {}
            self._cloud_me = copy.deepcopy(initial)
            self._cloud_me_day = today
            self._cloud_balance = 50
            self._cloud_titles = {'owned'}
            self._cloud_equipped = 'owned'
            self._cloud_equipped_cosmetic = ''
            self._cloud_appearances = {}
            self._test_columns = (2, 220)
            self.created = Counter()
            self.skeletons = 0
            self.results = 0
            self._profile_balance_label = None
            self._cloud_account_label = None

        def _is_admin_mode(self): return False
        def _has_cloud_account(self): return True
        def _has_quest_session(self): return True
        def _current_session_account_id(self): return account['account_id']
        def _require_personal_access(self, _purpose): return True
        def _require_regular_user_mode(self, _purpose): return True
        def _set_shop_wide_layout(self, _enabled): pass
        def _set_progress(self, text): self.progress = text
        def _refresh_open_wardrobe(self): pass
        def _show_signin_result(self, **kwargs): self.results += 1
        def _shop_columns_now(self): return self._test_columns

        def _destroy_detail(self):
            self.skeletons += 1
            return super()._destroy_detail()

        def _shop_tile(self, parent, item, owned, equipped, points, width=None):
            # Real widgets, controlled visuals: inspect identity/pooling without
            # unrelated image and title-animation costs.
            self.created[item['id']] += 1
            card = ctk.CTkFrame(parent, width=width or 220, height=120)
            card.pack_propagate(False)
            button = ctk.CTkButton(card, text=item['name'])
            button.pack()
            return card

    app = Harness()

    def pump():
        app.update_idletasks()
        app.update()

    def deliver(index=0, target=None):
        target = target or app
        workers.pop(index)()
        while not target.queue.empty():
            kind, payload = target.queue.get_nowait()
            assert kind == 'cloud_action'
            target._cloud_action_result(*payload)
        target.update_idletasks()
        target.update()

    try:
        app.open_shop()
        pump()
        assert app.skeletons == 1 and len(workers) == 2
        for _ in range(20): app.open_shop()
        pump()
        assert app.skeletons == 1 and len(workers) == 2
        deliver()
        deliver()
        assert calls['me'] == calls['quests'] == 1
        before = dict(app.created)
        for _ in range(20):
            app._mark_shop('products', 'balance')
            pump()
        assert dict(app.created) == before
        stable = dict(app._shop_cards)
        app._test_columns = (2, 230)
        app._shop_grid_resized(app._shop_coord().capture_page())
        pump()
        assert app._shop_cards == stable
        assert app._shop_cols[1] == 220

        # Older me worker completes after signin confirmation.
        app._run_cloud_action('me', mod.slg_account.me)
        for _ in range(20): app._on_signin_click()
        assert len(workers) == 2
        signin_token = app._shop_wallet_pending
        app._buy_item({'id': 'cheap', 'name': 'Cheap'})
        assert len(workers) == 2, 'buy must not race signin'
        deliver(1)
        assert calls['signin'] == 1 and app.results == 1
        assert app._cloud_balance == 60
        assert app._shop_cards['owned'] is stable['owned']
        assert app._shop_cards['cheap'] is stable['cheap']
        assert app._shop_cards['costly'] is not stable['costly']
        deliver(0)
        assert app._cloud_balance == 60 and app._cloud_me['signed_today'] is True
        deliver(0)
        assert calls['quests'] == 2 and app.skeletons == 1
        # Duplicate write completion cannot show a second result.
        app._cloud_action_result('signin', {'balance': 999}, None,
                                 app._identity_generation, account['account_id'], signin_token)
        assert app.results == 1 and app._cloud_balance == 60

        owned = app._shop_cards['owned']
        costly = app._shop_cards['costly']
        app._buy_item({'id': 'cheap', 'name': 'Cheap'})
        for _ in range(10): app._buy_item({'id': 'cheap', 'name': 'Cheap'})
        assert len(workers) == 1
        deliver()
        assert calls['buy'] == 1 and app.skeletons == 1 and not workers
        assert app._shop_cards['owned'] is owned
        assert app._shop_cards['costly'] is not costly

        # The actual day timer distinguishes midnight signin from 04:00 quests.
        real_datetime = mod.datetime
        class Clock(real_datetime):
            current = real_datetime.fromisoformat(today + 'T23:59:00+08:00')
            @classmethod
            def now(cls, tz=None):
                return cls.current
        mod.datetime = Clock
        app._shop_calendar_day = today
        app._shop_quest_period = today
        app._shop_day_tick(app._shop_coord().capture_page())
        assert not workers
        Clock.current += mod.timedelta(minutes=2)
        app._shop_day_tick(app._shop_coord().capture_page())
        assert len(workers) == 1, 'midnight requests me, not quest rollover'
        app._update_shop_signin_button()
        assert app._shop_signin_button.cget('state') == 'normal'
        deliver()
        Clock.current = Clock.current.replace(hour=4, minute=0)
        app._shop_day_tick(app._shop_coord().capture_page())
        assert len(workers) == 1, '04:00 requests one new quest snapshot'
        deliver()
        mod.datetime = real_datetime

        if args.cold_recovery:
            mod.messagebox.showwarning = lambda *a, **kw: None
            for operation in ('signin', 'buy'):
                cold = Harness()
                try:
                    cold._cloud_me = {}
                    cold._cloud_me_day = None
                    cold._cloud_balance = None
                    cold._cloud_titles = set()
                    cold.open_shop()
                    cold.update_idletasks()
                    cold.update()
                    assert len(workers) == 2
                    if operation == 'signin':
                        cold._on_signin_click()
                    else:
                        cold._buy_item({'id': 'cheap', 'name': 'Cheap'})
                    assert len(workers) == 3
                    workers.pop(2)  # Deliver error instead of running the write.
                    token = cold._shop_wallet_pending
                    cold._cloud_action_result(operation, None, 'offline cold failure',
                                              cold._identity_generation,
                                              account['account_id'], token)
                    cold.update_idletasks()
                    cold.update()
                    assert cold._shop_wallet_pending is None
                    assert len(workers) == 4, (
                        operation, 'expected two invalid old reads plus exactly '
                        'one replacement me and one replacement quests', len(workers))
                    while workers:
                        deliver(target=cold)
                    assert cold._cloud_me['account_id'] == account['account_id']
                    assert cold._cloud_balance == 50
                    assert cold._shop_signin_button.cget('state') == 'normal'
                    assert cold.skeletons == 1
                finally:
                    workers.clear()
                    cold.destroy()

        # Failure retires the lock and allows one explicit retry.
        app._on_signin_click()
        workers.pop(0)  # Deliberately deliver the transport error instead.
        failed_token = app._shop_wallet_pending
        mod.messagebox.showwarning = lambda *a, **kw: None
        app._cloud_action_result('signin', None, 'offline test failure',
                                 app._identity_generation, account['account_id'], failed_token)
        assert app._shop_wallet_pending is None
        while workers:
            deliver()
        app._on_signin_click()
        assert len(workers) == 1
        old_token = app._shop_wallet_pending
        old_page = app._shop_coord().capture_page()
        app._identity_generation += 1
        account['account_id'] = 'offline-b'
        app._shop_wallet_pending = None
        app._shop_coord()
        app._shop_coord().open_page()
        app._shop_wallet_pending = (app._identity_generation, account['account_id'], 999, 'signin')
        app._cloud_action_result('signin', {'balance': 999}, None,
                                 1, 'offline-a', old_token)
        assert app._shop_wallet_pending[-2] == 999
        assert not app._shop_coord().page_is_current(old_page)
        app._shop_coord().close_page()
        app._reopen_shop_if_showing(old_page)
        pump()
        assert app.skeletons == 1
        print({'checks': 'PASS', 'skeletons': app.skeletons,
               'network_completed': dict(calls), 'tiles_created': dict(app.created),
               'signin_dialogs': app.results, 'duplicate_methods': duplicates})
    finally:
        # Exercise the original App.destroy hooks as well as coordinator close.
        closing = SimpleNamespace(set=lambda: calls.update(['closing_event']))
        executor = SimpleNamespace(shutdown=lambda **kw: calls.update(['executor_shutdown']))
        app._closing_event = closing
        app._query_executor = executor
        coord = app._shop_coord()
        app.destroy()
        assert calls['closing_event'] == calls['executor_shutdown'] == 1
        assert coord.begin_read('me') is None


if __name__ == '__main__':
    main()
