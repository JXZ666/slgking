"""Verify stable client artifacts using synthetic data and child-only app directories."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_client_package import verify, REQUIRED_MODULES


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pyz(path):
    archive = CArchiveReader(str(path))
    return archive, archive.open_embedded_archive(next(
        name for name, entry in archive.toc.items() if entry[-1] == "z"))


def module(archive, name):
    result = types.ModuleType(name)
    exec(archive.extract(name), result.__dict__)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--old-exe", type=Path, required=True)
    parser.add_argument("--tag-reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    old_hash = digest(args.old_exe)
    result = verify(args.exe, "0.25.0", "0.25.0")
    archive, packaged = pyz(args.exe)
    source_hashes = {}
    for name in REQUIRED_MODULES:
        source = (ROOT / (name + ".py")).read_bytes()
        code = packaged.extract(name)
        if code != compile(source, code.co_filename, "exec"):
            raise ValueError("Packaged code differs from source: " + name)
        source_hashes[name + ".py"] = hashlib.sha256(source).hexdigest()
    names = {name.replace("\\", "/"): name for name in archive.toc}
    label_bytes = archive.extract(names["assets/tag_zh.json"])
    labels = json.loads(label_bytes)
    if labels != json.loads(args.tag_reference.read_text(encoding="utf-8")):
        raise ValueError("Packaged tag labels differ from reference")
    if label_bytes != (ROOT / "assets/tag_zh.json").read_bytes():
        raise ValueError("Packaged tag bytes differ from source")
    forbidden = {"cloud_session.json", "cloud_admin_session.json", "id_rsa", "id_ed25519",
                 "developer_key.txt", "admin_key.txt", ".env", "known_hosts"}
    if any(Path(name).name.lower() in forbidden or name.startswith(".claude/")
           or name.endswith((".pfx", ".p12", ".key")) for name in names):
        raise ValueError("Sensitive artifact included in archive")
    _, old_archive = pyz(args.old_exe)
    legacy = module(old_archive, "slg_db")
    current = module(packaged, "slg_db")
    checks = {}
    with tempfile.TemporaryDirectory(prefix="slgking-stable-check-") as work:
        base = Path(work)
        for kind in ("fresh", "old"):
            directory = base / kind
            directory.mkdir()
            if kind == "old":
                db_path = directory / "slgking" / "slgking.db"
                db_path.parent.mkdir()
                conn = legacy.connect(str(db_path))
                try:
                    game = legacy.add_user_game(conn, "Old fixture", folder_path="Z:/synthetic-game")
                    conn.execute("INSERT INTO state(game_id,status,my_rating) VALUES(?, 'want', 5)", (game,))
                    collection = legacy.create_collection(conn, "Fixture collection")
                    conn.execute("INSERT INTO collection_items(collection_id,game_id) VALUES(?,?)", (collection, game))
                    conn.commit()
                finally:
                    conn.close()
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(directory)
            for flag in ("--version", "--smoke"):
                completed = subprocess.run([str(args.exe), flag], env=env, timeout=60,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                checks[kind + flag] = completed.returncode
                if completed.returncode:
                    raise ValueError("EXE check failed: " + kind + flag)
            if list(directory.rglob("*crash*")):
                raise ValueError("Crash file produced")
            if kind == "old":
                conn = current.connect(str(db_path))
                try:
                    row = conn.execute("SELECT status,my_rating FROM state WHERE game_id=?", (game,)).fetchone()
                    assert tuple(row) == ("want", 5)
                    assert conn.execute("SELECT folder_path FROM local WHERE game_id=?", (game,)).fetchone()[0] == "Z:/synthetic-game"
                    assert conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0] == 1
                    assert current.game_categories(conn, game) == ()
                    current.set_game_categories(conn, game, ("slg", "rpg"))
                    assert current.find_games(conn, origin="user", category_ids=("slg",))[0]["id"] == game
                    exported = current.export_user_data(conn)
                    current.set_game_categories(conn, game, ())
                    current.import_user_data(conn, exported)
                    assert current.game_categories(conn, game) == ("slg", "rpg")
                    current.update_user_game(conn, game, "Edited", categories=())
                    assert current.game_categories(conn, game) == ()
                    checks["old_personal_data_and_categories_backup"] = True
                finally:
                    conn.close()
    assert digest(args.old_exe) == old_hash
    result.update({"compiled_modules_equal_source": True, "source_sha256": source_hashes,
                   "tag_count": len(labels), "tag_reference_equal": True,
                   "archive_sensitive_filename_check": True, "synthetic_exe_checks": checks,
                   "old_stable_sha256_unchanged": old_hash})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
