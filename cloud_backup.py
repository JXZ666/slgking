"""Bounded hot backups for the private cloud account database.

Run once a day after the catalogue sync.  SQLite's backup API gives a
consistent snapshot while the HTTP service continues to accept requests.
Archives stay outside the public publish directory and never contain keys in
their filenames or logs.
"""

import argparse
from datetime import datetime
import gzip
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile


DEFAULT_BUDGET = 8 * 1024 ** 3
MIN_FREE = 5 * 1024 ** 3


def _archives(directory, prefix):
    return sorted(Path(directory).glob("%s-*.sqlite3.gz" % prefix),
                  key=lambda path: path.stat().st_mtime, reverse=True)


def _prune(directory, keep, budget, prefix):
    archives = _archives(directory, prefix)
    total = sum(path.stat().st_size for path in archives)
    for path in archives[1:]:
        if len(archives) <= keep and total <= budget:
            break
        size = path.stat().st_size
        path.unlink()
        archives.remove(path)
        total -= size
    return total


def backup(db_path, backup_dir, *, keep=3, budget=DEFAULT_BUDGET,
           min_free=MIN_FREE, today=None, force=False, prefix="cloud"):
    """Return the archive path, or None when no safe backup can be made."""
    prefix = str(prefix or "cloud")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", prefix):
        raise ValueError("invalid backup prefix")
    source = Path(db_path)
    if not source.is_file():
        return None
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    day = today or datetime.now().strftime("%Y%m%d")
    existing = list(target_dir.glob("%s-%s-*.sqlite3.gz" % (prefix, day)))
    if existing and not force:
        _prune(target_dir, keep, budget, prefix)
        return existing[0]

    live_size = sum(path.stat().st_size for path in
                    (source, Path(str(source) + "-wal")) if path.is_file())
    # A live snapshot and its compressed copy briefly coexist. Leave enough
    # headroom for the service and the operating system before starting.
    if shutil.disk_usage(target_dir).free < min_free + 2 * live_size:
        return None

    raw_fd, raw_name = tempfile.mkstemp(prefix=".%s-" % prefix, suffix=".db",
                                       dir=target_dir)
    os.close(raw_fd)
    zip_fd, zip_name = tempfile.mkstemp(prefix=".%s-" % prefix, suffix=".gz",
                                       dir=target_dir)
    os.close(zip_fd)
    archive = target_dir / ("%s-%s-%s.sqlite3.gz" %
                            (prefix, day, datetime.now().strftime("%H%M%S%f")))
    try:
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(raw_name)
        try:
            src.backup(dst, pages=256, sleep=0.05)
        finally:
            dst.close()
            src.close()
        with open(raw_name, "rb") as inp, gzip.open(zip_name, "wb", compresslevel=4) as out:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
        if os.path.getsize(zip_name) > budget:
            return None
        os.replace(zip_name, archive)
        _prune(target_dir, keep, budget, prefix)
        return archive
    finally:
        for temporary in (raw_name, zip_name):
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--prefix", default="cloud",
                        help="archive name prefix, e.g. cloud or catalogue")
    parser.add_argument("--force", action="store_true",
                        help="create a fresh pre-deployment backup today")
    args = parser.parse_args()
    result = backup(args.db, args.backup_dir,
                    keep=max(1, args.keep), budget=max(1, args.max_bytes),
                    force=args.force, prefix=args.prefix)
    print("%s backup: %s" % (args.prefix,
          result if result else "skipped (missing DB or low space)"))


if __name__ == "__main__":
    main()
