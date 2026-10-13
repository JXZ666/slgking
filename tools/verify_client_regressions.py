"""Run client test modules in isolated subprocesses; retain individual evidence."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=900,
                        help='Per-module timeout; the full real-Tk layout module has 202 checks')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for module in sorted((root / 'tests').glob('test_*.py')):
        start = time.monotonic()
        try:
            run = subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                '-s', 'tests', '-p', module.name], cwd=root,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=args.timeout)
            log = run.stdout.decode('utf-8', errors='replace')
            code = run.returncode
        except subprocess.TimeoutExpired as exc:
            log = (exc.stdout or b'').decode('utf-8', errors='replace') + '\nTIMEOUT\n'
            code = 124
        (args.output / (module.stem + '.log')).write_text(log, encoding='utf-8')
        count = re.search(r'Ran (\d+) tests?', log)
        row = dict(module=module.name, exit_code=code,
                   tests=int(count.group(1)) if count else None,
                   seconds=round(time.monotonic()-start, 3))
        rows.append(row)
        (args.output/'summary.json').write_text(json.dumps(dict(
            complete=False, results=rows), indent=2), encoding='utf-8')
        print(json.dumps(row), flush=True)
    result = dict(complete=True, tests=sum(row['tests'] or 0 for row in rows),
                  passed=all(row['exit_code']==0 for row in rows), results=rows)
    (args.output/'summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
