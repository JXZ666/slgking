"""Optional Everything ES CLI discovery, scoped to one selected local directory.

Does not install Everything, traverse the disk, fetch metadata, or execute games.
"""
import csv
import io
import os
from pathlib import Path
import subprocess
import tempfile
import time


class EverythingUnavailable(RuntimeError):
    pass


def discover(executable, root, should_stop=None, limit=5000, timeout=30):
    """Return unique existing game-file parent paths within root; caller previews them.

    Official ES uses its configured Everything service. A configured absolute es.exe
    is required. CSV export avoids decoding filenames using the console codepage.
    """
    cli, target = Path(executable), Path(root)
    if not cli.is_absolute() or cli.name.lower() != 'es.exe' or not cli.is_file():
        raise EverythingUnavailable('未配置官方Everything命令行es.exe；可继续使用普通扫描。')
    if not target.is_absolute() or not target.is_dir():
        raise ValueError('请选择有效的扫描目标目录')
    target = target.resolve()
    stop = should_stop or (lambda: False)
    if stop():
        return []
    limit = max(1, min(5000, int(limit)))
    with tempfile.TemporaryDirectory(prefix='slg-es-') as temporary:
        output = Path(temporary) / 'matches.csv'
        # path: is an Everything search operator; escape literal embedded quotes.
        if '"' in str(target):
            raise ValueError('目标路径格式无效')
        query = 'path:"' + str(target) + '" ext:exe;sh;bat;cmd'
        args = [str(cli), '-n', str(limit), '-full-path', '-export-csv', str(output), query]
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            raise EverythingUnavailable('无法启动Everything命令行，请使用普通扫描。') from exc
        start = time.monotonic()
        try:
            while process.poll() is None:
                if stop():
                    process.terminate()
                    process.wait(timeout=3)
                    return []
                if time.monotonic() - start > timeout:
                    process.terminate()
                    raise EverythingUnavailable('Everything索引查询超时，请使用普通扫描。')
                time.sleep(.03)
            if process.returncode != 0 or not output.exists():
                raise EverythingUnavailable('Everything索引不可用，请启动Everything或使用普通扫描。')
            if output.stat().st_size > 8 * 1024 * 1024:
                raise EverythingUnavailable('Everything返回结果过大，请缩小扫描范围。')
            return parse_results(output.read_text(encoding='utf-8-sig'), target, limit, stop)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


def parse_results(text, root, limit=5000, should_stop=None):
    root = Path(root).resolve()
    result, seen = [], set()
    for row in csv.DictReader(io.StringIO(text)):
        if should_stop and should_stop():
            return []
        value = row.get('Filename') or row.get('Full Path') or row.get('Full Filename') or row.get('Name')
        if value and not Path(value).is_absolute() and row.get('Path'):
            value = str(Path(row['Path']) / value)
        if not value:
            continue
        path = Path(value)
        try:
            # resolve rejects junction escapes before returning selected roots.
            path = path.resolve(strict=True)
            path.relative_to(root)
        except (OSError, ValueError):
            continue
        if not path.is_file() or path.suffix.lower() not in ('.exe', '.sh', '.bat', '.cmd'):
            continue
        parent = str(path.parent)
        key = os.path.normcase(parent)
        if key not in seen:
            seen.add(key)
            result.append(parent)
        if len(result) >= max(1, min(5000, limit)):
            break
    return result
