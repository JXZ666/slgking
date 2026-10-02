"""Inspect a client EXE offline without executing bundled Python or reading user data."""
import argparse
import dis
import hashlib
import json
from pathlib import Path
import sqlite3

from PyInstaller.archive.readers import CArchiveReader


PERSONAL_TABLES = ("state", "local", "local_saves", "user_game_categories",
                   "game_aliases", "collections", "collection_items",
                   "manual_translations", "comments", "points_log", "signin",
                   "owned_titles", "wishlist_version_seen", "weights",
                   "affinities", "exclusions", "prefs", "sync_log")
REQUIRED_MODULES = ("slg_gui", "slg_motion", "slg_game_categories", "slg_scan",
                    "slg_account", "slg_db")


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


def verify(path, expected_stable="0.24.5", expected_test="0.25.0"):
    archive = CArchiveReader(str(path))
    pyz_name = next(name for name, entry in archive.toc.items() if entry[-1] == "z")
    pyz = archive.open_embedded_archive(pyz_name)
    modules = {name: name in pyz.toc for name in REQUIRED_MODULES}
    if not all(modules.values()):
        raise ValueError("Required client module is missing")
    versions = module_versions(pyz.extract("slg_gui"))
    if versions != {"APP_VERSION": expected_stable, "TEST_APP_VERSION": expected_test}:
        raise ValueError("Bundled stable/test versions do not match expected channels")
    names = {name.replace("\\", "/"): name for name in archive.toc}
    seed = next(original for name, original in names.items()
                if name.endswith("assets/seed/slgking.db"))
    counts = seed_counts(archive.extract(seed))
    if any(counts.values()):
        raise ValueError("Seed contains personal records")
    forbidden = {"cloud_session.json", "cloud_admin_session.json", "id_ed25519", "id_rsa"}
    if any(Path(name).name.lower() in forbidden for name in names):
        raise ValueError("Archive contains a forbidden credential file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest(),
            "required_modules": modules, "versions": versions,
            "seed_personal_counts": counts, "credential_file_present": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--stable-version", default="0.24.5")
    parser.add_argument("--test-version", default="0.25.0")
    args = parser.parse_args()
    print(json.dumps(verify(args.exe, args.stable_version, args.test_version),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
