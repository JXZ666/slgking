"""Local version reminders for wanted games that are not installed."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slg_db  # noqa: E402


class WishlistVersionChanges(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.game_id, _ = slg_db.upsert_game(
            self.conn, "wishlist-game", "https://example.invalid/game",
            "Wishlist Game", version="1.0", last_updated="2026-09-01")
        slg_db.set_state(self.conn, self.game_id, status="want")

    def tearDown(self):
        self.conn.close()

    def _set_version(self, version):
        slg_db.upsert_game(
            self.conn, "wishlist-game", "https://example.invalid/game",
            "Wishlist Game", version=version, last_updated="2026-09-20")

    def test_first_observation_is_baseline_then_new_version_stays_pending(self):
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), [])
        self._set_version("1.1")
        expected = [{
            "id": self.game_id,
            "slug": "wishlist-game",
            "title": "Wishlist Game",
            "seen_version": "1.0",
            "version": "1.1",
            "last_updated": "2026-09-20",
        }]
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), expected)
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), expected)

    def test_acknowledging_visible_snapshot_keeps_a_later_update_pending(self):
        slg_db.wishlist_version_changes(self.conn)  # establish 1.0 baseline
        self._set_version("1.1")
        visible = slg_db.wishlist_version_changes(self.conn)[0]
        self._set_version("1.2")
        self.assertTrue(slg_db.mark_wishlist_version_seen(
            self.conn, self.game_id, visible["version"]))
        changes = slg_db.wishlist_version_changes(self.conn)
        self.assertEqual([(r["seen_version"], r["version"]) for r in changes],
                         [("1.1", "1.2")])

    def test_installed_game_is_excluded_from_wishlist_reminders(self):
        slg_db.wishlist_version_changes(self.conn)  # establish baseline
        self._set_version("1.1")
        slg_db.set_local_folder(self.conn, self.game_id, "C:/Games/Wishlist")
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), [])

    def test_missing_version_is_baselined_when_first_available(self):
        slg_db.delete_game(self.conn, self.game_id)
        self.game_id, _ = slg_db.upsert_game(
            self.conn, "wishlist-game", "https://example.invalid/game",
            "Wishlist Game", version=None)
        slg_db.set_state(self.conn, self.game_id, status="want")
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), [])
        self._set_version("1.0")
        self.assertEqual(slg_db.wishlist_version_changes(self.conn), [])
        self._set_version("1.1")
        changes = slg_db.wishlist_version_changes(self.conn)
        self.assertEqual([(r["seen_version"], r["version"]) for r in changes],
                         [("1.0", "1.1")])


if __name__ == "__main__":
    unittest.main()
