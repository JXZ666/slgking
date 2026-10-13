import unittest

from slg_refresh_coordination import RefreshCoordinator


class FakeLoop:
    def __init__(self):
        self.jobs = {}
        self.serial = 0
        self.renders = []
        self.coord = RefreshCoordinator(self.idle, self.cancel, self.renders.append)
        self.coord.set_identity(('regular', 'a', 1))
        self.coord.open_page()

    def idle(self, callback):
        self.serial += 1
        self.jobs[self.serial] = callback
        return self.serial

    def cancel(self, job):
        self.jobs.pop(job, None)

    def tick(self):
        jobs, self.jobs = self.jobs, {}
        for callback in jobs.values():
            callback()


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.loop = FakeLoop()
        self.c = self.loop.coord

    def test_one_read_per_business_key(self):
        me = self.c.begin_read('me')
        self.assertIsNone(self.c.begin_read('me'))
        self.assertIsNotNone(self.c.begin_read('quests'))
        self.assertTrue(self.c.finish_read(me))
        self.assertFalse(self.c.finish_read(me))
        self.assertIsNotNone(self.c.begin_read('me'))

    def test_failed_completion_allows_explicit_retry(self):
        failed = self.c.begin_read('quests')
        self.assertTrue(self.c.finish_read(failed))
        self.assertIsNotNone(self.c.begin_read('quests'))

    def test_write_barrier_rejects_prewrite_read_and_old_error(self):
        old = self.c.begin_read('me')
        self.c.invalidate_reads('me')
        fresh = self.c.begin_read('me')
        self.assertFalse(self.c.finish_read(old))
        self.assertIsNone(self.c.begin_read('me'))
        self.assertTrue(self.c.finish_read(fresh))

    def test_two_write_barriers_reject_read_during_write(self):
        self.c.invalidate_reads('me', 'quests')
        during = self.c.begin_read('me')
        self.c.invalidate_reads('me', 'quests')
        fresh = self.c.begin_read('me')
        self.assertFalse(self.c.finish_read(during))
        self.assertTrue(self.c.finish_read(fresh))

    def test_identity_switch_away_and_back_expires_reads(self):
        old = self.c.begin_read('me')
        self.c.set_identity(('regular', 'b', 2))
        self.c.set_identity(('regular', 'a', 3))
        self.assertFalse(self.c.finish_read(old))

    def test_close_and_reopen_expires_page_callbacks(self):
        page = self.c.capture_page()
        token = self.c.begin_read('preview', page_bound=True)
        self.c.close_page()
        self.c.open_page()
        self.assertFalse(self.c.page_is_current(page))
        self.assertFalse(self.c.finish_read(token))

    def test_identity_read_populates_cache_without_closed_page_render(self):
        token = self.c.begin_read('me')
        self.c.close_page()
        self.assertTrue(self.c.finish_read(token))
        self.c.mark('balance')
        self.loop.tick()
        self.assertEqual(self.loop.renders, [])

    def test_next_turn_merges_dirty_domains(self):
        self.c.mark('balance', 'signin')
        self.c.mark('balance', 'quests')
        self.assertEqual(len(self.loop.jobs), 1)
        self.loop.tick()
        self.assertEqual(self.loop.renders,
                         [frozenset(('balance', 'signin', 'quests'))])

    def test_same_product_state_skips_repaint(self):
        state = (('title-a', False, True), ('title-b', True, False))
        for _ in range(20):
            if self.c.changed('products', state):
                self.c.mark('products')
            self.loop.tick()
        self.assertEqual(self.loop.renders, [frozenset(('products',))])

    def test_signin_signature_includes_day_and_pending(self):
        state = ('a', '2026-10-09', True, False)
        self.assertTrue(self.c.changed('signin', state))
        self.assertFalse(self.c.changed('signin', state))
        self.assertTrue(self.c.changed('signin', ('a', '2026-10-10', False, False)))
        self.assertTrue(self.c.changed('signin', ('a', '2026-10-10', False, True)))

    def test_unchanged_columns_do_not_schedule_relayout(self):
        for width in (700, 701, 702, 703):
            columns = width // 200
            if self.c.changed('layout', columns):
                self.c.mark('layout')
            self.loop.tick()
        self.assertEqual(len(self.loop.renders), 1)
        self.assertTrue(self.c.changed('layout', 4))

    def test_stale_idle_cannot_clear_new_page_job(self):
        self.c.mark('products')
        stale = next(iter(self.loop.jobs.values()))
        self.c.open_page()
        self.c.mark('quests')
        stale()
        self.loop.tick()
        self.assertEqual(self.loop.renders, [frozenset(('quests',))])

    def test_close_cancels_and_rejects_work(self):
        token = self.c.begin_read('me')
        self.c.mark('products')
        self.c.close()
        self.loop.tick()
        self.assertFalse(self.c.finish_read(token))
        self.assertIsNone(self.c.open_page())
        self.assertIsNone(self.c.begin_read('me'))
        self.assertFalse(self.c.changed('products', ()))
        self.assertEqual(self.loop.renders, [])

    def test_render_can_mark_next_tick(self):
        def render(domains):
            self.loop.renders.append(domains)
            if 'products' in domains:
                self.c.mark('balance')
        self.c._render = render
        self.c.mark('products')
        self.loop.tick()
        self.assertEqual(len(self.loop.jobs), 1)
        self.loop.tick()
        self.assertEqual(self.loop.renders,
                         [frozenset(('products',)), frozenset(('balance',))])


if __name__ == '__main__':
    unittest.main()
