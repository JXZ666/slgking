"""Run the frozen test client against isolated fresh and retained upgrade profiles."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile


def personal_rows(profile):
    database = profile / "slgking" / "slgking.db"
    if not database.exists():
        return {}
    tables = ("state", "local", "collections", "collection_items", "comments",
              "game_aliases", "user_game_categories", "game_cover_overrides", "hidden_games")
    with sqlite3.connect(database) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: sorted(conn.execute('SELECT * FROM "' + t + '"').fetchall(), key=repr)
                for t in tables if t in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--upgrades", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    exe = args.exe.resolve()
    upgrade = json.loads(args.upgrades.read_text(encoding="utf-8"))
    results = []
    with tempfile.TemporaryDirectory(prefix="slg-0265-frozen-", ignore_cleanup_errors=True) as directory:
        profiles = [("fresh", Path(directory))] + [(r["baseline"], Path(r["runtime_profile"]))
                                                  for r in upgrade["results"]]
        for name, profile in profiles:
            before = personal_rows(profile)
            env = os.environ.copy()
            env.update({key: str(profile) for key in ("LOCALAPPDATA", "APPDATA", "HOME", "USERPROFILE")})
            checks = {}
            for flag in ("--version", "--smoke"):
                run = subprocess.run([str(exe), flag], env=env, capture_output=True, timeout=90)
                checks[flag] = run.returncode
                if run.returncode:
                    raise RuntimeError((name, flag, run.returncode))
            after = personal_rows(profile)
            assert all(after.get(t) == rows for t, rows in before.items()), (name, "personal data changed")
            assert not list(profile.rglob("*crash*")), (name, "crash file")
            database = profile / "slgking" / "slgking.db"
            with sqlite3.connect(database) as conn:
                assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert conn.execute("SELECT COUNT(*) FROM game_catalogs WHERE catalog_id='dlsite'").fetchone()[0] == 151
                assert conn.execute("SELECT COUNT(*) FROM hidden_games").fetchone()[0] == len(before.get("hidden_games", []))
            results.append(dict(baseline=name, checks=checks, personal_data_preserved=True,
                                integrity="ok", dlsite_games=151, crash_files=0))
    report = dict(exe=str(exe), sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                  profiles=results, cli_output_note="Windowless CLI output depends on parent console; version is also checked from frozen bytecode.")
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
