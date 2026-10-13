"""Inspect a client EXE offline without executing bundled Python or reading user data."""
import argparse
import dis
import hashlib
import json
import types
from pathlib import Path
import sqlite3

from PyInstaller.archive.readers import CArchiveReader


PERSONAL_TABLES = ("personal_game_metadata", "hidden_games", "game_cover_overrides", "state", "local", "local_saves", "user_game_categories",
                   "game_aliases", "collections", "collection_items",
                   "manual_translations", "comments", "points_log", "signin",
                   "owned_titles", "wishlist_version_seen", "weights",
                   "affinities", "exclusions", "prefs", "sync_log")
REQUIRED_MODULES = ("slg_public_cover_cache", "slg_scan_evidence", "slg_scan_review", "slg_library_metadata", "slg_library_providers", "slg_library_query_ui", "slg_collection_menu", "slg_image_resources", "slg_sidebar_layout", "slg_hidden_games", "slg_game_detection", "slg_tag_selector", "slg_refresh_coordination", "slg_source_status", "slg_game_labels", "slg_library_query", "slg_scan_targets", "slg_scan_identity", "slg_dlsite_online", "slg_library_ui", "slg_local_library", "slg_dlsite_client", "slg_gui", "slg_motion", "slg_game_categories", "slg_scan",
                    "slg_account", "slg_db", "slg_catalogues", "slg_sync_server", "slg_crash", "slg_log_ui")


REQUIRED_MODULES += ("slg_game_roots", "slg_library_cover_candidates", "slg_library_cover_ui")


def seed_counts(raw):
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(raw)
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {table: conn.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]
                  if table in tables else 0 for table in PERSONAL_TABLES}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
        counts["user_games"] = conn.execute(
            "SELECT COUNT(*) FROM games WHERE origin='user'" if "origin" in columns
            else "SELECT COUNT(*) FROM games WHERE slug LIKE 'user-%'").fetchone()[0]
        return counts
    finally:
        conn.close()


def module_versions(code):
    # Inspect bytecode constants, never execute slg_gui or any bundled module.
    values = {}
    previous = None
    for instruction in dis.get_instructions(code):
        if (instruction.opname == "STORE_NAME"
                and instruction.argval in ("APP_VERSION", "TEST_APP_VERSION")
                and previous is not None and previous.opname == "LOAD_CONST"):
            values[instruction.argval] = previous.argval
        previous = instruction
    return values


def verify(path, expected_stable="0.26.0", expected_test="0.26.0", source_root=None):
    archive = CArchiveReader(str(path))
    pyz_name = next(name for name, entry in archive.toc.items() if entry[-1] == "z")
    pyz = archive.open_embedded_archive(pyz_name)
    modules = {name: name in pyz.toc for name in REQUIRED_MODULES}
    if not all(modules.values()):
        raise ValueError("Required client module is missing")
    versions = module_versions(pyz.extract("slg_gui"))
    if versions != {"APP_VERSION": expected_stable, "TEST_APP_VERSION": expected_test}:
        raise ValueError("Bundled stable/test versions do not match expected channels")
    source_matches = {}
    if source_root is not None:
        def normalized(code):
            return code.replace(co_filename="<verified-source>", co_consts=tuple(
                normalized(value) if isinstance(value, types.CodeType) else value
                for value in code.co_consts))
        for name in REQUIRED_MODULES:
            source = Path(source_root) / (name + ".py")
            expected = compile(source.read_bytes(), str(source), "exec", optimize=0)
            source_matches[name] = normalized(pyz.extract(name)) == normalized(expected)
        if not all(source_matches.values()):
            raise ValueError("Bundled bytecode differs from reviewed client source: " +
                             ", ".join(name for name, same in source_matches.items() if not same))
    names = {name.replace("\\", "/"): name for name in archive.toc}
    seed = next(original for name, original in names.items()
                if name.endswith("assets/seed/slgking.db"))
    counts = seed_counts(archive.extract(seed))
    if any(counts.values()):
        raise ValueError("Seed contains personal records")
    pilot = None
    if True:  # Both release channels require the sanitized DLsite bootstrap.
        pilot_db = names.get("assets/dlsite/slgking.db")
        pilot_manifest = names.get("assets/dlsite/manifest.json")
        if not pilot_db or not pilot_manifest:
            raise ValueError("DLsite test snapshot is missing")
        raw = archive.extract(pilot_db)
        manifest = json.loads(archive.extract(pilot_manifest))
        if manifest.get("catalog_id") != "dlsite" or hashlib.sha256(raw).hexdigest() != manifest.get("db_sha256"):
            raise ValueError("DLsite test snapshot identity/hash mismatch")
        personal = seed_counts(raw)
        if any(personal.values()):
            raise ValueError("DLsite snapshot contains personal data")
        conn = sqlite3.connect(":memory:")
        try:
            conn.deserialize(raw)
            count = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            if count != manifest["count"] or count < 1:
                raise ValueError("DLsite snapshot count mismatch")
            if conn.execute("SELECT COUNT(*) FROM games WHERE slug NOT LIKE 'dlsite-rj%' OR rating IS NOT NULL").fetchone()[0]:
                raise ValueError("DLsite snapshot contains incompatible games/ratings")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("DLsite snapshot integrity check failed")
        finally:
            conn.close()
        pilot = {"count": count, "collected_at": manifest.get("collected_at"),
                 "target_reached": manifest.get("provenance", {}).get("target_reached"),
                 "personal_counts": personal, "sha256": manifest["db_sha256"]}
    forbidden = {"cloud_session.json", "cloud_admin_session.json", "id_ed25519", "id_rsa"}
    if any(Path(name).name.lower() in forbidden for name in names):
        raise ValueError("Archive contains a forbidden credential file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest(),
            "required_modules": modules, "versions": versions, "source_matches": source_matches,
            "seed_personal_counts": counts, "dlsite_pilot": pilot, "credential_file_present": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--stable-version", default="0.26.0")
    parser.add_argument("--test-version", default="0.26.0")
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.exe, args.stable_version, args.test_version, args.source_root),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
