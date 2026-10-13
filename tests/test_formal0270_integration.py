import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import slg_db
import slg_library_metadata as metadata
from slg_public_cover_cache import PublicCoverCache
import slg_scan
import slg_sync_server


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (24, 32), "red").save(out, "PNG")
    return out.getvalue()


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_valid_cover_download_is_atomic_and_cached(self):
        calls = []
        raw = image_bytes()
        cache = PublicCoverCache(self.root, lambda path: calls.append(path) or raw)
        cache.get("f95-fixture.jpg")
        cache.get("f95-fixture.jpg")
        self.assertEqual(calls, ["/covers/f95-fixture.jpg"])
        self.assertEqual((self.root / "f95-fixture.jpg").read_bytes(), raw)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_failure_preserves_previous_file_and_explicit_update_retries(self):
        path = self.root / "fixture.jpg"
        path.write_bytes(b"previous damaged file")
        calls = []
        cache = PublicCoverCache(self.root, lambda _: calls.append(1) or b"invalid")
        with self.assertRaises(ValueError):
            cache.get("fixture.jpg")
        with self.assertRaises(RuntimeError):
            cache.get("fixture.jpg")
        self.assertEqual(len(calls), 1)
        self.assertEqual(path.read_bytes(), b"previous damaged file")
        cache.clear_retry()
        cache.fetch = lambda _: image_bytes()
        cache.get("fixture.jpg")
        self.assertEqual(path.read_bytes(), image_bytes())

    def test_path_traversal_rejected_before_network(self):
        cache = PublicCoverCache(self.root, lambda _: self.fail("network reached"))
        for name in ("../escape.jpg", "bad/name.jpg", "C:/escape.png", "bad.exe"):
            with self.assertRaises(ValueError):
                cache.get(name)

    def test_failed_downloads_do_not_leave_unbounded_retry_records(self):
        cache = PublicCoverCache(self.root, lambda _: b"invalid")
        for i in range(220):
            with self.assertRaises(ValueError):
                cache.get("cover%d.jpg" % i)
        self.assertEqual(len(cache._failure), 192)


class PersonalCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = slg_db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_heat_migration_only_uses_existing_metrics_and_is_idempotent(self):
        measured, _ = slg_db.upsert_game(self.conn, "measured", "https://example.invalid/m", "Measured")
        unknown, _ = slg_db.upsert_game(self.conn, "unknown", "https://example.invalid/u", "Unknown")
        dlsite, _ = slg_db.upsert_game(self.conn, "dlsite-rj123456", "https://example.invalid/d", "DLsite")
        self.conn.execute("UPDATE games SET rating=4,heat=NULL WHERE id IN (?,?)", (measured, dlsite))
        self.conn.commit()
        self.assertEqual(slg_db.migrate_legacy_heat(self.conn), 1)
        self.assertEqual(slg_db.migrate_legacy_heat(self.conn), 0)
        rows = dict(self.conn.execute("SELECT id,heat FROM games"))
        self.assertIsNotNone(rows[measured])
        self.assertIsNone(rows[unknown])
        self.assertIsNone(rows[dlsite])

    def test_confirmed_multilingual_aliases_survive_backup_without_duplicate(self):
        candidate = dict(source="vndb", external_id="v27519", title="Dohna Dohna",
                         source_url="https://vndb.org/v27519",
                         aliases=["多娜多娜", "多娜多娜 一起干坏事吧"])
        gid = metadata.confirm(self.conn, candidate)
        self.conn.commit()
        self.assertEqual(metadata.confirm(self.conn, candidate), gid)
        self.conn.commit()
        backup = slg_db.export_user_data(self.conn)
        restored = slg_db.connect(":memory:")
        try:
            slg_db.import_user_data(restored, backup)
            same = metadata.find_existing(restored, "vndb", "v27519")
            aliases = {r[0] for r in restored.execute("SELECT alias FROM game_aliases WHERE game_id=?", (same,))}
            self.assertTrue(set(candidate["aliases"]) <= aliases)
            self.assertEqual(metadata.get_metadata(restored, same)[0]["aliases"], candidate["aliases"])
        finally:
            restored.close()

    def test_invalid_alias_rejected_before_creating_game(self):
        with self.assertRaises(ValueError):
            metadata.confirm(self.conn, dict(source="vndb", external_id="v27519", title="Name",
                                            source_url="https://vndb.org/v27519", aliases=[None]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM games").fetchone()[0], 0)

    def test_mixed_encoding_title_cannot_automatically_bind_other_game(self):
        gid, _ = slg_db.upsert_game(self.conn, "wrong-game", "https://example.invalid/a", "Wrong Game")
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "unrelated-folder"
            folder.mkdir()
            (folder / "Game.exe").touch()
            facts = dict(identities=[], titles=[dict(text="Wrong Game", reason="alice_game_name", ambiguous=True)], warnings=[])
            with patch("slg_scan_evidence.extract_evidence", return_value=facts):
                result = slg_scan.scan(self.conn, roots=[str(folder)], mode="single", apply=False, log=lambda _: None)
            self.assertNotEqual(result["items"][0].get("game_id"), gid)

    def test_gui_sync_mode_does_not_bulk_download_catalogue_images(self):
        slg_db.set_pref(self.conn, slg_sync_server.PREF_LAST_PULL_HASH, "a" * 64)
        manifest = dict(count=10, db_sha256="a" * 64, lastmod="fixture")
        with patch.object(slg_sync_server, "fetch_manifest", return_value=manifest), \
                patch.object(slg_sync_server, "_collector_status", return_value=None), \
                patch.object(slg_sync_server, "_download_covers", side_effect=AssertionError("bulk download")):
            result = slg_sync_server.pull(self.conn, log=lambda _: None, download_images=False)
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["covers"], 0)


if __name__ == "__main__":
    unittest.main()
