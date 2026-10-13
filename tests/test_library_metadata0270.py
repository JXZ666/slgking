"""Confirmed provider facts remain personal and round-trip by game slug."""
import copy
import tempfile
import unittest
from pathlib import Path

import slg_db
import slg_library_metadata as metadata
import slg_scan


class PersonalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "fixture.sqlite"
        self.conn = slg_db.connect(str(self.db))
        self.addCleanup(lambda: self.conn.close())

    def candidate(self, source="dlsite", external="RJ99887766", title="Personal Product"):
        url = ("https://www.dlsite.com/maniax/work/=/product_id/" + external + ".html"
               if source == "dlsite" else "https://vndb.org/" + external)
        return dict(source=source, external_id=external, title=title,
                    original_title="作品原名", developer="Fixture Maker", source_url=url)

    def confirm(self, **kwargs):
        gid = metadata.confirm(self.conn, self.candidate(**kwargs))
        self.conn.commit()
        return gid

    def test_repeated_confirmation_uses_one_game_identity(self):
        gid = self.confirm()
        again = self.confirm(title="New Confirmed Alias")
        self.assertEqual(gid, again)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM games WHERE origin='user'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM personal_game_metadata").fetchone()[0], 1)
        self.assertEqual(metadata.get_metadata(self.conn, gid)[0]["title"], "New Confirmed Alias")
        aliases = {r[0] for r in self.conn.execute("SELECT alias FROM game_aliases WHERE game_id=?", (gid,))}
        self.assertTrue({"Personal Product", "New Confirmed Alias", "作品原名"}.issubset(aliases))

    def test_same_title_different_source_id_is_not_merged(self):
        first = self.confirm()
        second = self.confirm(external="RJ11223344")
        third = self.confirm(source="vndb", external="v123")
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM personal_game_metadata").fetchone()[0], 3)

    def test_public_identity_uses_catalogue_without_overwriting_facts(self):
        gid, _ = slg_db.upsert_game(self.conn, "public-product", "https://example.invalid/public", "Catalogue Title")
        self.conn.execute("UPDATE games SET developer='Catalogue Maker',engine='Manual Engine' WHERE id=?", (gid,))
        self.conn.execute("INSERT INTO game_sources(source_id,external_id,game_id,fetched_at)"
                          " VALUES('dlsite','RJ99887766',?,datetime('now'))", (gid,))
        self.conn.commit()
        before = dict(self.conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone())
        confirmed = self.confirm(title="Different Upstream Title")
        self.assertEqual(confirmed, gid)
        self.assertEqual(dict(self.conn.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()), before)
        self.assertFalse(metadata.get_metadata(self.conn, gid))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM game_aliases WHERE game_id=?", (gid,)).fetchone()[0], 0)

    def test_reopen_vndb_identity_is_exactly_recognized(self):
        gid = self.confirm(source="vndb", external="v123")
        self.conn.close()
        self.conn = slg_db.connect(str(self.db))
        folder = self.root / "random-folder"
        folder.mkdir()
        (folder / "Game.exe").touch()
        (folder / "official.url").write_text("[InternetShortcut]\nURL=https://vndb.org/v123\n")
        result = slg_scan.scan(self.conn, roots=[str(folder)], mode="single", apply=False, log=lambda _: None)
        self.assertEqual(result["items"][0]["game_id"], gid)
        self.assertEqual(result["items"][0]["status"], "matched")
        self.assertEqual(result["items"][0]["evidence"]["identities"][0]["external_id"], "v123")

    def test_full_backup_restores_personal_metadata_by_slug(self):
        gid = self.confirm()
        slg_db.set_state(self.conn, gid, "want")
        self.conn.commit()
        backup = slg_db.export_user_data(self.conn)
        self.assertEqual(len(backup["local_metadata"]), 1)
        restored = slg_db.connect(":memory:")
        self.addCleanup(restored.close)
        slg_db.upsert_game(restored, "id-collision", "https://example.invalid/collision", "Existing Catalogue")
        slg_db.import_user_data(restored, backup)
        restored_gid = metadata.find_existing(restored, "dlsite", "RJ99887766")
        self.assertIsNotNone(restored_gid)
        self.assertNotEqual(gid, restored_gid)
        self.assertEqual(metadata.get_metadata(restored, restored_gid), metadata.get_metadata(self.conn, gid))
        self.assertEqual(restored.execute("SELECT status FROM state WHERE game_id=?", (restored_gid,)).fetchone()[0], "want")
        self.assertEqual(metadata.export_sections(restored), metadata.export_sections(self.conn))

    def test_old_backup_without_optional_metadata_section_is_accepted(self):
        self.confirm()
        backup = slg_db.export_user_data(self.conn)
        del backup["local_metadata"]
        restored = slg_db.connect(":memory:")
        self.addCleanup(restored.close)
        slg_db.import_user_data(restored, backup)
        self.assertEqual(restored.execute("SELECT count(*) FROM games WHERE origin='user'").fetchone()[0], 1)
        self.assertEqual(restored.execute("SELECT count(*) FROM personal_game_metadata").fetchone()[0], 0)

    def test_invalid_official_identity_links_are_rejected_before_writing(self):
        good = self.candidate()
        invalid = [
            dict(good, source_url="http://www.dlsite.com/maniax/work/=/product_id/RJ99887766.html"),
            dict(good, source_url="https://www.dlsite.com.evil.invalid/maniax/work/=/product_id/RJ99887766.html"),
            dict(good, source_url="https://www.dlsite.com/maniax/work/=/product_id/RJ11223344.html"),
            dict(good, source_url="https://user:password@www.dlsite.com/maniax/work/=/product_id/RJ99887766.html"),
            dict(good, source="unsupported"),
            dict(good, external_id="RJ12345"),
            dict(self.candidate(source="vndb", external="v123"), source_url="https://vndb.org/v124"),
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    metadata.confirm(self.conn, candidate)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM games").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM personal_game_metadata").fetchone()[0], 0)

    def test_missing_backup_game_reference_rolls_back_all_personal_changes(self):
        gid = self.confirm()
        slg_db.set_state(self.conn, gid, "want")
        self.conn.commit()
        before = slg_db.export_user_data(self.conn)
        broken = copy.deepcopy(before)
        broken["local_metadata"][0]["slug"] = "nonexistent-personal-game"
        with self.assertRaises(ValueError):
            slg_db.import_user_data(self.conn, broken)
        self.assertEqual(slg_db.export_user_data(self.conn), before)

    def test_duplicate_identity_in_backup_is_rejected(self):
        self.confirm()
        original = slg_db.export_user_data(self.conn)
        broken = copy.deepcopy(original)
        broken["local_metadata"].append(copy.deepcopy(broken["local_metadata"][0]))
        with self.assertRaises(ValueError):
            slg_db.import_user_data(self.conn, broken)
        self.assertEqual(slg_db.export_user_data(self.conn), original)


if __name__ == "__main__":
    unittest.main()
