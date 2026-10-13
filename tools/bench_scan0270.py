"""Offline same-machine scan benchmark: catalogue scale != disk folder count."""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import slg_db
import slg_scan
import slg_game_detection


def run(baseline, sizes=(1000, 10000), repeats=30, folders=30):
    spec = importlib.util.spec_from_file_location('scan_baseline0267', baseline)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    reports = []
    for size in sizes:
        with tempfile.TemporaryDirectory(prefix='slg-scan-bench-') as tmp:
            root = Path(tmp)
            conn = slg_db.connect(':memory:')
            try:
                stamp = '2026-10-10'
                conn.executemany('INSERT INTO games(slug,url,title,first_seen,engine) VALUES(?,?,?,?,?)',
                    ((f'bench-{n}', f'https://example.invalid/{n}', f'Bench Game {n:05}', stamp, 'Manual Engine')
                     for n in range(size)))
                for n in range(folders):
                    folder = root / f'Bench Game {n:05}'
                    folder.mkdir()
                    exe = folder / 'Game.exe'
                    exe.touch()
                    (folder / 'UnityPlayer.dll').touch()
                    data = folder / 'Game_Data'
                    data.mkdir()
                    (data / 'globalgamemanagers').touch()
                    slg_db.set_local_folder(conn, n + 1, str(folder))
                    slg_db.set_local_exe(conn, n + 1, str(exe))
                conn.commit()
                for label, module, force in [('baseline0267_full', old, False),
                                               ('candidate0270_full', slg_scan, True),
                                               ('candidate0270_quick', slg_scan, False)]:
                    visits = {'entries': 0, 'directories': 0}
                    original = os.scandir
                    class CountScan:
                        def __init__(self, path):
                            self.iterator = original(path)
                        def __enter__(self):
                            visits['directories'] += 1
                            def items():
                                for entry in self.iterator:
                                    visits['entries'] += 1
                                    yield entry
                            return items()
                        def __exit__(self, *args):
                            self.iterator.close()
                    times = []
                    with patch.object(slg_game_detection.os, 'scandir', CountScan):
                        for _ in range(repeats):
                            start = time.perf_counter()
                            kwargs = {'force_redetect': force} if module is slg_scan else {}
                            result = module.scan(conn, roots=[str(root)], apply=False,
                                                 log=lambda _: None, **kwargs)
                            times.append((time.perf_counter() - start) * 1000)
                            if result['matched'] != folders or result['errors']:
                                raise AssertionError(result)
                    ordered = sorted(times)
                    reports.append({'label': label, 'catalogue_games': size,
                        'real_disk_game_folders': folders, 'operations': repeats,
                        'each_operation': 'preview all direct child game folders; no database writes',
                        'median_ms': statistics.median(times),
                        'p95_ms': ordered[max(0, math.ceil(.95 * len(ordered)) - 1)],
                        'max_ms': max(times), 'detection_directories_total': visits['directories'],
                        'detection_entries_total': visits['entries']})
            finally:
                conn.close()
    return {'scope': 'local scan only; no GUI, network or upstream latency claims',
            'baseline_file': str(Path(baseline).resolve()), 'results': reports}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--output')
    parser.add_argument('--repeats', type=int, default=30)
    args = parser.parse_args()
    report = run(args.baseline, repeats=args.repeats)
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding='utf-8')
    print(content)
