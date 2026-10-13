"""Offline 30-operation scan comparison on a small, explicitly labelled fixture.

This measures catalogue-size sensitivity, not a population-wide match rate.
Only disposable SQLite/filesystem fixtures are used. No Tk or upstream queries.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def statistics_ms(samples):
    ordered = sorted(samples)
    return {"median_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[max(0, math.ceil(.95 * len(ordered)) - 1)], 3),
            "max_ms": round(max(ordered), 3)}


def worker(workspace, catalogue_size, iterations):
    sys.path.insert(0, str(workspace))
    import slg_db
    import slg_scan
    with tempfile.TemporaryDirectory(prefix="slgking-benchmark0270-") as temporary:
        base = Path(temporary)
        profile = base / "isolated-profile"
        profile.mkdir()
        for variable in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
            os.environ[variable] = str(profile)
        games = base / "fixture-games"
        games.mkdir()
        conn = slg_db.connect(str(base / "fixture.sqlite"))
        try:
            titles = ["Exact Alpha Story", "Exact Beta World", "Exact Gamma Journey",
                      "Exact Delta Game", "Exact Epsilon Novel", "Exact Zeta Tale",
                      "Shared Conflict", "Shared Conflict", "Exact Eta Adventure",
                      "Exact Theta Game", "Exact Iota Legend"]
            for index, title in enumerate(titles, 1):
                conn.execute("INSERT INTO games(id,slug,url,title,first_seen) VALUES(?,?,?,?,?)",
                             (index, "fixture-" + str(index), "https://example.invalid/fixture-" + str(index), title, "2000-01-01"))
            conn.executemany("INSERT INTO games(id,slug,url,title,first_seen) VALUES(?,?,?,?,?)", [
                (index, "background-" + str(index), "https://example.invalid/background-" + str(index),
                 "Synthetic Benchmark Unrelated Catalog Entry " + str(index), "2000-01-01")
                for index in range(len(titles) + 1, catalogue_size + 1)])
            conn.executemany("INSERT INTO game_sources(source_id,external_id,game_id,fetched_at) VALUES('dlsite',?,?,?)",
                             [("RJ01234567", 3, "2000-01-01"), ("RJ04561234", 9, "2000-01-01")])
            slg_db.add_alias(conn, 5, "中文标注作品")
            slg_db.add_alias(conn, 6, "Known English Alias")
            cases = []

            def folder(name, expected=None, expected_pending=False, exe="Game.exe", files=None):
                path = games / name
                path.mkdir()
                (path / exe).touch()
                for relative, content in (files or {}).items():
                    target = path / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                cases.append({"folder": name, "expected_game_id": expected,
                              "expected_confirmation": expected_pending})
                return path

            folder("nxxa", 1, files={"game/options.rpy": 'define config.name = "Exact Alpha Story"'})
            folder("nxxb", 2, files={"www/data/System.json": '{"gameTitle":"Exact Beta World"}'})
            folder("RJ01234567", 3)
            folder("Exact Delta Game", 4)
            folder("nxxe", 5, files={"game/options.rpy": 'define config.name = "中文标注作品"'})
            folder("Known English Alias", 6)
            folder("Shared Conflict", expected_pending=True)
            folder("Exact Delta Game [RJ99887766]")
            folder("qzzx")
            folder("pzzx", expected_pending=True, files={
                "game/options.rpy": 'define config.name = "Exact Alpha Story"',
                "data/System.json": '{"gameTitle":"Exact Beta World"}'})
            folder("wzzx", 9, exe="RJ04561234.exe")
            registered = folder("Exact Theta Game", 11)
            slg_db.set_local_folder(conn, 11, str(registered), "fixed-version")
            conn.commit()
            labels = {case["folder"]: case for case in cases}
            times, outcomes = [], []
            with patch("subprocess.Popen", side_effect=AssertionError("unexpected process execution")), \
                    patch("os.system", side_effect=AssertionError("unexpected shell execution")), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network request")):
                for _ in range(iterations):
                    started = time.perf_counter()
                    result = slg_scan.scan(conn, roots=[str(games)], apply=False, log=lambda _: None)
                    times.append((time.perf_counter() - started) * 1000)
                    assert not result["cancelled"] and not result["errors"], result
                    assert len(result["items"]) == len(cases), result
                    summary = {"correct_matches": 0, "requires_confirmation": 0,
                               "unmatched": 0, "incorrect_automatic_bindings": 0}
                    rows = []
                    for item in sorted(result["items"], key=lambda row: row["name"]):
                        label = labels[item["name"]]
                        if item["status"] == "matched":
                            summary["correct_matches" if item["game_id"] == label["expected_game_id"]
                                    else "incorrect_automatic_bindings"] += 1
                        elif item["status"] in ("ambiguous", "conflict"):
                            summary["requires_confirmation"] += 1
                        else:
                            summary["unmatched"] += 1
                        rows.append({"folder": item["name"], "status": item["status"],
                                     "game_id": item["game_id"], "expected_game_id": label["expected_game_id"],
                                     "reason": item["reason"],
                                     "candidates": [candidate["game_id"] for candidate in item["candidates"]]})
                    outcomes.append({"counts": summary, "cases": rows})
            assert all(outcome == outcomes[0] for outcome in outcomes), "non-deterministic scan results"
            assert conn.execute("SELECT count(*) FROM local").fetchone()[0] == 1, "preview wrote local data"
            source_hashes = {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
                             for name in ("slg_scan.py", "slg_scan_identity.py", "slg_scan_evidence.py",
                                          "slg_db.py", "slg_game_detection.py", "slg_scan_targets.py")
                             if (workspace / name).is_file()}
            return {"workspace": str(workspace), "catalogue_records": catalogue_size,
                    "sample_directories": len(cases), "iterations": iterations,
                    "timing": statistics_ms(times), "samples_ms": [round(value, 3) for value in times],
                    **outcomes[0], "preview_preserved_database": True, "source_sha256": source_hashes}
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="D:/AI agent/slgking-client-0270-fix")
    parser.add_argument("--candidate", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--report", default=".claude/library-matching-benchmark.json")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--size", type=int)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    if args.worker:
        print(json.dumps(worker(Path(args.workspace), args.size, args.iterations), ensure_ascii=True))
        return
    runs = []
    for label, workspace in (("baseline", Path(args.baseline)), ("candidate", Path(args.candidate))):
        revision = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        for size in (1000, 10000):
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                                       "--workspace", str(workspace), "--size", str(size),
                                       "--iterations", str(args.iterations)], capture_output=True, timeout=600)
            if completed.returncode:
                raise RuntimeError(completed.stderr.decode("utf-8", errors="replace"))
            run = json.loads(completed.stdout.decode("ascii"))
            run.update(label=label, head_revision=revision)
            runs.append(run)
            print(json.dumps({"label": label, "catalogue_records": size,
                              "timing": run["timing"], "counts": run["counts"]}, ensure_ascii=True), flush=True)
    report = {"created_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
              "method": f"Same-machine offline scan(apply=False), 12 labelled directories, {args.iterations} operations per catalogue size and workspace; full scan/index/detection latency.",
              "limitations": ["Synthetic targeted regression fixture, not a claim of general user-library matching accuracy.",
                              "No Tk/first-frame/image-readiness or online query latency measured.",
                              "Candidate may contain uncommitted implementation changes; HEAD alone does not represent their exact contents."],
              "runs": runs}
    destination = Path(args.report)
    if not destination.is_absolute():
        destination = Path(args.candidate) / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(destination))


if __name__ == "__main__":
    main()
