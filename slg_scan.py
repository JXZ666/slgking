"""Local game folders.

Two jobs: work out what is actually on disk and which catalogue entry it
corresponds to, and compare the version in the folder name against the version
dikgames is currently serving.

Folder names are the only version record these games have - Ren'Py installs
carry no manifest - so the whole thing leans on a title/version split.
"""

import difflib
import os
import re

import slg_db

# The author's own library. It used to be the unconditional default, which made
# 扫描本地目录 a no-op on every other machine that ran the exe. The GUI now asks
# for a folder and remembers the answer, and this is only what the CLI falls
# back to - and only while the directory is actually there.
DEFAULT_ROOTS = [path for path in (r"D:\game&novel\黄油",) if os.path.isdir(path)]

_VER_PREFIXED = re.compile(r"[vV](\d+(?:\.\d+){1,3}[a-z]?)")
_VER_BARE = re.compile(r"(\d+(?:\.\d+){1,3}[a-z]?)")
# Trailing platform/quality markers the scene puts on every folder name.
_NOISE = re.compile(
    r"[-_\s]*(?:pc|win|windows|linux|mac|android|compressed|full\s*release|final|"
    r"eng|english|uncensored|censored|repack|patched|multi)\s*$", re.I)


_ORDINALS = {"1st": "first", "2nd": "second", "3rd": "third",
             "4th": "fourth", "5th": "fifth"}


def _key(text):
    """Fold a title to a comparison key.

    'Hikari: First Interlude' and 'Hikari1stInterlude' are the same game and
    the site and the folder disagree about how to spell the ordinal, so fold
    those too.
    """
    text = (text or "").lower()
    for word, replacement in _ORDINALS.items():
        text = text.replace(word, replacement)
    return re.sub(r"[^a-z0-9]", "", text)


def parse_folder_version(name):
    """'ParadiseCity-0.6.23-pc' -> '0.6.23'; 'Supower-Re0.69-pc' -> '0.69'."""
    match = _VER_PREFIXED.search(name) or _VER_BARE.search(name)
    return match.group(1) if match else None


def folder_title(name):
    """The folder name with the version and platform noise taken off."""
    if not name:
        return name
    title = re.sub(r"[vV]?\d+(?:\.\d+){1,3}[a-z]?", " ", name)
    title = re.sub(r"[_]+", " ", title)
    previous = None
    while previous != title:
        previous = title
        title = _NOISE.sub("", title).strip(" -_.")
    return re.sub(r"\s+", " ", title).strip() or name


def _title_index(conn):
    """normalised title -> (game_id, original title), longest keys first.

    Longest first so 'lust theory' wins over 'lust' when both are in the
    catalogue.
    """
    rows = conn.execute("SELECT id, title FROM games").fetchall()
    index = [(_key(r["title"]), r["id"], r["title"]) for r in rows]
    index.sort(key=lambda item: -len(item[0]))
    return index


def match_game(conn, folder_name, index=None):
    """Best catalogue row for a folder name, or None.

    Three passes, cheapest first: exact, then either-way containment (the site
    files 'LustBound' as 'Couples: Lustbound' and the folder keeps only the
    tail), then a similarity ratio for the rest.
    """
    index = index if index is not None else _title_index(conn)
    key = _key(folder_title(folder_name))
    if not key:
        return None

    for title_key, game_id, _ in index:
        if title_key and title_key == key:
            return game_id

    for title_key, game_id, _ in index:
        if len(title_key) >= 6 and (title_key in key or key in title_key):
            return game_id

    return _closest(key, index)


def _closest(key, index, threshold=0.86):
    """Fuzzy last resort. The length guard keeps this from scanning all 1274."""
    best_id, best = None, threshold
    for title_key, game_id, _ in index:
        if len(title_key) < 5 or len(key) < 5:
            continue
        longest = max(len(title_key), len(key))
        if abs(len(title_key) - len(key)) > longest * 0.4:
            continue
        ratio = difflib.SequenceMatcher(None, key, title_key).ratio()
        if ratio > best:
            best_id, best = game_id, ratio
    return best_id


def inspect(folder):
    """What the folder itself says about the game."""
    info = {"has_translation": 0, "has_fontpatch": 0, "is_renpy": False}
    game_dir = os.path.join(folder, "game")
    if os.path.isdir(game_dir) and os.path.isdir(os.path.join(folder, "renpy")):
        info["is_renpy"] = True
    tl_dir = os.path.join(game_dir, "tl")
    if os.path.isdir(tl_dir):
        for entry in os.listdir(tl_dir):
            if "chin" in entry.lower():
                info["has_translation"] = 1
                break
    # rpykit-luna drops this shim; its presence means the font has been fixed.
    if info["is_renpy"]:
        for name in os.listdir(game_dir) if os.path.isdir(game_dir) else []:
            if "rpykit" in name.lower() or name.lower().startswith("zz_fontgroup"):
                info["has_fontpatch"] = 1
                break
    return info


def scan(conn, roots=None, with_size=False, on_progress=None, log=print,
         should_stop=None):
    """Walk the roots and reconcile every game folder against the catalogue."""
    roots = roots or DEFAULT_ROOTS
    if not roots:
        log("没有可扫描的目录。在软件里点「扫描本地目录」时会让你选一个文件夹。")
        return {"matched": 0, "unmatched": []}
    index = _title_index(conn)
    found, unmatched = 0, []

    for root in roots:
        if not os.path.isdir(root):
            log("跳过（不存在）：%s" % root)
            continue
        for name in sorted(os.listdir(root)):
            if should_stop and should_stop():
                conn.commit()
                slg_db.invalidate_cover_gaps()
                log("扫描已停止：%d 个文件夹已登记" % found)
                return {"matched": found, "unmatched": unmatched}
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            game_id = match_game(conn, name, index)
            if game_id is None:
                unmatched.append(name)
                continue
            info = inspect(folder)
            size = None
            if with_size:
                size = sum(
                    os.path.getsize(os.path.join(dirpath, f))
                    for dirpath, _, files in os.walk(folder) for f in files
                    if os.path.exists(os.path.join(dirpath, f)))
            conn.execute(
                "INSERT OR REPLACE INTO local (game_id, folder_path, folder_version,"
                " has_translation, has_fontpatch, size_bytes, scanned_at)"
                " VALUES (?,?,?,?,?,?,datetime('now'))",
                (game_id, folder, parse_folder_version(name),
                 info["has_translation"], info["has_fontpatch"], size))
            # Downloaded is a fact about the disk, so it should not need saying.
            conn.execute(
                "INSERT INTO state (game_id, status, updated_at)"
                " VALUES (?, 'downloaded', datetime('now'))"
                " ON CONFLICT(game_id) DO UPDATE SET status = 'downloaded'"
                " WHERE state.status IS NULL OR state.status = ''",
                (game_id,))
            found += 1
            if on_progress:
                on_progress(name, game_id)
            log("匹配：%s -> #%d %s" % (name, game_id, _title_index_title(index, game_id)))
    conn.commit()
    # A scan can name games the catalogue had no row for, and those rows bring
    # their own cover_file with them - so the memoised cover-gap count is now
    # describing a library that no longer exists.
    slg_db.invalidate_cover_gaps()
    log("扫描完成：%d 个文件夹匹配上，%d 个没匹配" % (found, len(unmatched)))
    for name in unmatched:
        log("  未匹配：%s" % name)
    return {"matched": found, "unmatched": unmatched}


def _title_index_title(index, game_id):
    for _, gid, title in index:
        if gid == game_id:
            return title
    return "?"


def check_updates(conn):
    """Local folder version vs the version dikgames is serving now."""
    rows = conn.execute("""
        SELECT g.id, g.title, g.version, g.url, g.last_updated,
               l.folder_path, l.folder_version
        FROM games g JOIN local l ON l.game_id = g.id
        WHERE l.folder_version IS NOT NULL AND g.version IS NOT NULL
        ORDER BY g.title COLLATE NOCASE
    """).fetchall()

    behind, same, unknown = [], [], []
    for row in rows:
        local_v = (row["folder_version"] or "").strip()
        site_v = (row["version"] or "").strip().lstrip("vV")
        if not local_v or not site_v:
            unknown.append(row)
        elif _compare(local_v, site_v) < 0:
            behind.append(row)
        else:
            same.append(row)
    return {"behind": behind, "same": same, "unknown": unknown}


def _compare(left, right):
    """Version compare that does not fall over on '0.5b' or 'Ep.7 Free'."""
    if slg_db._version_gt(left, right):
        return 1
    if slg_db._version_gt(right, left):
        return -1
    return 0


# --- CLI -----------------------------------------------------------------------

def _main(argv=None):
    import argparse
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(prog="slgking scan", description="扫描本地游戏目录")
    parser.add_argument("--root", action="append", default=[],
                        help="要扫描的根目录（可重复，默认 %s）" % DEFAULT_ROOTS[0])
    parser.add_argument("--size", action="store_true", help="顺便统计文件夹体积（较慢）")
    parser.add_argument("--updates", action="store_true", help="只列出有更新的游戏")
    args = parser.parse_args(argv)

    conn = slg_db.connect()
    try:
        if args.updates:
            report = check_updates(conn)
            print("有新版：%d 款" % len(report["behind"]))
            for row in report["behind"]:
                print("  %-40s 本地 %-12s 站点 %s  (%s)"
                      % (row["title"][:40], row["folder_version"],
                         row["version"], row["last_updated"] or "?"))
            print("\n已最新：%d 款 · 无法比较：%d 款"
                  % (len(report["same"]), len(report["unknown"])))
        else:
            scan(conn, args.root or None, with_size=args.size)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
