"""Independent integration checks for the 0.25.6 personal library contract."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import slg_db
import slg_local_library as library


class DataIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.western, _ = slg_db.upsert_game(self.conn, "public-alpha", "https://dikgames.com/a/", "Public Alpha")
        self.unbound, _ = slg_db.upsert_game(self.conn, "public-unbound", "https://dikgames.com/b/", "No binding")
        self.dlsite, _ = slg_db.upsert_game(self.conn, "dlsite-rj01234567",
            "https://www.dlsite.com/maniax/work/=/product_id/RJ01234567.html", "Japanese title", developer="Circle Beacon")
        slg_db.set_dlsite_metadata(self.conn, self.dlsite,
            {"languages": ["日本語", "English"], "work_type": "RPG", "rating_value": 4.9, "rating_count": 9})
        self.custom = slg_db.add_user_game(self.conn, "Own game", categories=("galgame", "slg"))
        slg_db.set_local_folder(self.conn, self.western, str(self.root))
        slg_db.set_local_exe(self.conn, self.dlsite, str(self.root / "run.exe"))

    def ids(self, **kwargs):
        return {row["id"] for row in slg_db.find_games(self.conn, sort="title", with_scores=False, **kwargs)}

    def test_local_includes_bound_public_and_custom_across_catalogues(self):
        expected = {self.western, self.dlsite, self.custom}
        for catalogue in (None, "all", "western_slg", "dlsite"):
            self.assertEqual(self.ids(origin="local", catalog_id=catalogue), expected)

    def test_rj_and_developer_search_all_and_local(self):
        for origin, catalogue in (("main", "all"), ("local", None), ("local", "western_slg")):
            for term in ("rj01234567", "Circle Beacon"):
                self.assertEqual(self.ids(origin=origin, catalog_id=catalogue, search=term), {self.dlsite})
        self.assertEqual(self.ids(catalog_id="western_slg", search="Circle Beacon"), set())

    def test_dlsite_filter_isolation(self):
        self.assertEqual(self.ids(catalog_id="dlsite", dlsite_languages=("English",), dlsite_work_types=("RPG",)), {self.dlsite})
        self.assertEqual(self.ids(catalog_id="dlsite", dlsite_languages=("简体中文",)), set())
        self.assertEqual(self.ids(catalog_id="western_slg", dlsite_languages=("简体中文",)), {self.western, self.unbound})
        self.assertEqual(self.ids(origin="local", catalog_id="dlsite", dlsite_languages=("简体中文",)),
                         {self.western, self.dlsite, self.custom})

    def test_add_valid_folder_sets_downloaded_without_overwriting_choice(self):
        gid = slg_db.add_user_game(self.conn, "Installed custom", folder_path=str(self.root))
        self.assertEqual(self.conn.execute("SELECT status FROM state WHERE game_id=?", (gid,)).fetchone()[0], "downloaded")
        slg_db.set_state(self.conn, self.western, status="want", my_rating=5)
        slg_db.set_local_folder(self.conn, self.western, str(self.root))
        state = self.conn.execute("SELECT status,my_rating FROM state WHERE game_id=?", (self.western,)).fetchone()
        self.assertEqual(tuple(state), ("want", 5))

    def test_clear_binding_keeps_files_and_personal_state(self):
        exe = self.root / "run.exe"
        exe.write_bytes(b"not executable")
        slg_db.set_state(self.conn, self.dlsite, status="want", my_rating=4)
        library.clear_local_binding(self.conn, self.dlsite)
        self.assertTrue(exe.exists())
        self.assertNotIn(self.dlsite, self.ids(origin="local"))
        self.assertEqual(tuple(self.conn.execute("SELECT status,my_rating FROM state WHERE game_id=?", (self.dlsite,)).fetchone()), ("want", 4))

    def test_backup_roundtrip_categories_cover_and_bindings(self):
        name = "custom-cover.png"
        Image.new("RGB", (8, 8)).save(self.root / name)
        library.set_cover_override(self.conn, self.western, name)
        with patch.object(slg_db, "covers_dir", return_value=str(self.root)):
            before = slg_db.export_user_data(self.conn)
            library.clear_local_binding(self.conn, self.western)
            library.clear_cover_override(self.conn, self.western)
            slg_db.set_game_categories(self.conn, self.custom, ())
            warnings = slg_db.import_user_data(self.conn, before)
            self.assertEqual(warnings, [])
            after = slg_db.export_user_data(self.conn)
            self.assertEqual(after["local_bindings"], before["local_bindings"])
            self.assertEqual(after["cover_overrides"], before["cover_overrides"])
            row = next(r for r in slg_db.find_games(self.conn, origin="local") if r["id"] == self.western)
            self.assertEqual(row["cover_file"], name)
            self.assertTrue(row["has_cover_override"])
        self.assertEqual(slg_db.game_categories_bulk(self.conn, [self.custom])[self.custom], ("galgame", "slg"))

    def test_old_backup_missing_new_sections_and_invalid_preflight(self):
        before = slg_db.export_user_data(self.conn)
        legacy = copy.deepcopy(before)
        legacy.pop("cover_overrides")
        legacy.pop("local_bindings")
        self.assertEqual(slg_db.import_user_data(self.conn, legacy), [])
        broken = copy.deepcopy(before)
        broken["local_bindings"] = [{"slug": "missing", "folder_path": "D:/bad"}]
        snapshot = slg_db.export_user_data(self.conn)
        with self.assertRaises(ValueError):
            slg_db.import_user_data(self.conn, broken)
        self.assertEqual(slg_db.export_user_data(self.conn), snapshot)

    def test_missing_backup_image_warns_and_keeps_catalogue_fallback(self):
        slg_db.set_cover(self.conn, self.western, "catalogue.png")
        library.set_cover_override(self.conn, self.western, "missing.png")
        saved = slg_db.export_user_data(self.conn)
        with patch.object(slg_db, "covers_dir", return_value=str(self.root)):
            warnings = slg_db.import_user_data(self.conn, saved)
            self.assertEqual(len(warnings), 1)
            row = next(row for row in slg_db.find_games(self.conn) if row["id"] == self.western)
            self.assertEqual(row["cover_file"], "catalogue.png")
            self.assertFalse(row["has_cover_override"])

    def test_local_categories_remain_private_and_survive_update(self):
        with self.assertRaises(ValueError):
            slg_db.set_game_categories(self.conn, self.dlsite, ("rpg",))
        slg_db.update_user_game(self.conn, self.custom, "Renamed custom", categories=None)
        row = next(row for row in slg_db.find_games(self.conn, origin="local") if row["id"] == self.custom)
        self.assertEqual(row["categories"], ("galgame", "slg"))
        slg_db.update_user_game(self.conn, self.custom, "Renamed custom", categories=())
        self.assertEqual(slg_db.game_categories(self.conn, self.custom), ())

    def test_restore_cannot_replace_public_game_identity(self):
        before = slg_db.export_user_data(self.conn)
        bad = copy.deepcopy(before)
        slug = slg_db.get_game(self.conn, self.western)["slug"]
        bad["user_games"][0]["slug"] = slug
        with self.assertRaises(ValueError):
            slg_db.import_user_data(self.conn, bad)
        self.assertEqual(slg_db.export_user_data(self.conn), before)
        self.assertEqual(slg_db.get_game(self.conn, self.western)["origin"], "site")

    def test_changing_directory_clears_stale_launcher(self):
        slg_db.set_local_exe(self.conn, self.western, str(self.root / "Old.exe"))
        slg_db.set_local_folder(self.conn, self.western, str(self.root / "Another"))
        self.assertIsNone(self.conn.execute("SELECT exe_path FROM local WHERE game_id=?",
                                           (self.western,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
