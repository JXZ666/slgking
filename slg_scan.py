"""Local game folders.

Two jobs: work out what is actually on disk and which catalogue entry it
corresponds to, and compare the version in the folder name against the version
dikgames is currently serving.

Folder names are the only version record these games have - Ren'Py installs
carry no manifest - so the whole thing leans on a title/version split.
"""

import datetime
import difflib
import os
import re

import slg_db
import slg_util

# The author's own library. It used to be the unconditional default, which made
# 扫描本地目录 a no-op on every other machine that ran the exe. The GUI now asks
# for a folder and remembers the answer, and this is only what the CLI falls
# back to - and only while the directory is actually there.
DEFAULT_ROOTS = [path for path in (r"D:\game&novel\黄油",) if os.path.isdir(path)]

_VER_PREFIXED = re.compile(r"[vV](\d+(?:\.\d+){1,3}[a-z]?|\d{1,3}[a-z]?)")
_VER_BARE = re.compile(r"(\d+(?:\.\d+){1,3}[a-z]?)")
# Version-looking tokens to strip from a title. Dotted first so 'v0.9.5' matches
# whole; a bare 'v1'/'v12' next; then episode/chapter/season markers; then a
# trailing bare 1-2 digit build number. A 4-digit year (2024) never matches any
# of these, so it stays in the title.
_VERSION = re.compile(
    r"[vV]?\d+(?:\.\d+){1,3}[a-z]?"
    r"|[vV]\d{1,3}[a-z]?"
    r"|\b(?:ep|episode|ch|chapter|season|s)\s*\.?\s*\d{1,3}\b"
    r"|(?:\s|[-_])+\d{1,2}[a-z]?$",
    re.I)
# Trailing platform/quality markers the scene puts on every folder name, plus
# the Chinese suffixes the multi-source folders carry.
_NOISE = re.compile(
    r"[-_\s]*(?:pc|win|windows|linux|mac|android|compressed|full\s*release|final|"
    r"eng|english|uncensored|censored|repack|patched|multi|"
    r"汉化版|汉化|官方中文|官中|中文版|中文|完整版|最终版|无码|步兵|骑兵|"
    r"精翻|机翻|ai汉化|gpt|解码|去码)\s*$", re.I)
# Square brackets and their full-width twins are almost always annotations
# (开发组名、[汉化]、【官中】) rather than part of the title.
_BRACKET = re.compile(r"[\[【].*?[\]】]")
# Round parens are only stripped when the whole content is one known noise token,
# so a real subtitle like 'The DeLuca Family (Season 1)' survives.
_PAREN_NOISE = re.compile(
    r"\(\s*(?:pc|win|windows|linux|mac|android|汉化|官中|官方中文|中文版|完整版|"
    r"最终版|无码|步兵|骑兵|精翻|机翻|ai汉化|gpt|eng|english|uncensored|repack)"
    r"\s*\)", re.I)

_CJK = re.compile(r"[\u4e00-\u9fff]")


_ORDINALS = {"1st": "first", "2nd": "second", "3rd": "third",
             "4th": "fourth", "5th": "fifth"}


def _listdir(path):
    """os.listdir, but a locked or vanished directory reads as empty.

    One bad folder used to abort the whole scan: a directory the user deleted
    mid-scan, or one the OS holds open, raises OSError out of the walk and the
    GUI reports 扫描失败 with no partial result. Reading it as empty keeps the
    rest of the run intact.
    """
    try:
        return os.listdir(path)
    except OSError:
        return []


def _key(text):
    """Fold a title to a comparison key.

    'Hikari: First Interlude' and 'Hikari1stInterlude' are the same game and
    the site and the folder disagree about how to spell the ordinal, so fold
    those too. CJK survives so a Chinese folder name can match a Chinese title.
    """
    text = (text or "").lower()
    for word, replacement in _ORDINALS.items():
        text = text.replace(word, replacement)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text)


def parse_folder_version(name):
    """'ParadiseCity-0.6.23-pc' -> '0.6.23'; 'Supower-Re0.69-pc' -> '0.69'."""
    match = _VER_PREFIXED.search(name) or _VER_BARE.search(name)
    return match.group(1) if match else None


def folder_title(name):
    """The folder name with the version and platform noise taken off."""
    if not name:
        return name
    title = name
    previous = None
    while previous != title:
        previous = title
        title = _VERSION.sub(" ", title)
        title = _BRACKET.sub(" ", title)
        title = _PAREN_NOISE.sub(" ", title)
        title = title.replace("_", " ")
        title = _NOISE.sub("", title).strip(" -_.")
    return re.sub(r"\s+", " ", title).strip() or name


def _title_index(conn):
    """normalised title -> (game_id, display name), longest keys first.

    One game contributes several keys: its English title, its Chinese machine
    or hand-typed name, and any user-added aliases. Longest first so 'lust
    theory' wins over 'lust' when both are in the catalogue.
    """
    index = []
    for row in conn.execute("SELECT id, title FROM games"):
        index.append((_key(row["title"]), row["id"], row["title"]))
    for ref, (text, _engine) in slg_db.title_translations(conn).items():
        game_id = slg_db._int_or_none(ref)
        if game_id is not None and text:
            index.append((_key(text), game_id, text))
    for row in conn.execute("SELECT game_id, alias FROM game_aliases"):
        index.append((_key(row["alias"]), row["game_id"], row["alias"]))
    seen = set()
    deduped = []
    for key, game_id, title in index:
        if not key or (key, game_id) in seen:
            continue
        seen.add((key, game_id))
        deduped.append((key, game_id, title))
    deduped.sort(key=lambda item: -len(item[0]))
    return deduped


def _has_cjk(text):
    return bool(_CJK.search(text or ""))


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

    # CJK titles are often 2-4 characters, so the containment length guard has
    # to drop for them: an ASCII word needs length to be a reliable signal, but
    # a two-character Chinese substring is already near-unique.
    min_len = 2 if _has_cjk(key) else 6
    for title_key, game_id, _ in index:
        if title_key and len(title_key) >= min_len and \
                (title_key in key or key in title_key):
            return game_id

    return _closest(key, index)


def _closest(key, index, threshold=0.86):
    """Fuzzy last resort. The length guard keeps this from scanning all 1274.

    Chinese keys get a higher threshold and a lower length floor, and are never
    fuzzy-matched against an English key: difflib sees the two alphabets as
    nothing alike, and the guard skips the wasted comparison.
    """
    cjk = _has_cjk(key)
    best_id, best = None, (0.9 if cjk else threshold)
    for title_key, game_id, _ in index:
        if not title_key or _has_cjk(title_key) != cjk:
            continue
        min_len = 2 if cjk else 5
        if len(title_key) < min_len or len(key) < min_len:
            continue
        longest = max(len(title_key), len(key))
        if abs(len(title_key) - len(key)) > longest * 0.4:
            continue
        sm = difflib.SequenceMatcher(None, key, title_key)
        # quick_ratio 是 O(n) 的字符计数上界，先拿它挡掉一批进不了最佳的，
        # 省下昂贵的 LCS 计算 —— 未匹配文件夹越多，这一步越值。
        if sm.quick_ratio() < best:
            continue
        ratio = sm.ratio()
        if ratio > best:
            best_id, best = game_id, ratio
    return best_id


# --- add-game autofill ----------------------------------------------------------

# 方括号里的内容经常是开发组/社团名（[NTRMAN]），但也可能是翻译/画质标注，这些不算开发商。
_GROUP_NOISE = re.compile(
    r"汉化|官中|官方中文|中文|精翻|机翻|ai汉化|gpt|无码|步兵|骑兵|解码|去码|"
    r"完整版|最终版|pc|win|windows|linux|mac|android|eng|english|repack|"
    r"compressed|uncensored|censored", re.I)


def _folder_developer(name):
    """第一个「非翻译/画质标注」的方括号组，当作开发商/社团名。"""
    for match in re.finditer(r"[\[【]([^\]】]+)[\]】]", name):
        group = match.group(1).strip()
        if group and not _GROUP_NOISE.search(group):
            return group
    return ""


def detect_engine(folder):
    """从目录结构判断引擎，未知返回空串。"""
    entries = _listdir(folder)
    lowered = {e.lower() for e in entries}
    if "renpy" in lowered or any(e.lower().endswith(".rpy") for e in entries):
        return "Ren'Py"
    if "unityplayer.dll" in lowered or any(e.lower().endswith("_data") for e in entries):
        return "Unity"
    if "www" in lowered and os.path.isfile(os.path.join(folder, "www", "index.html")):
        return "RPG Maker"
    if any(e.lower().endswith((".rgss3a", ".rgss2a", ".rxproj", ".rvproj2")) for e in entries):
        return "RPG Maker"
    if any(e.lower().endswith(".html") for e in entries) or "index.html" in lowered:
        return "HTML"
    return ""


def autofill_folder(folder):
    """从一个游戏文件夹猜出标题/开发商/版本/引擎，供「添加我的游戏」表单预填。

    简介 / 标签 / 封面仍由玩家自己填，这里只填目录本身能确定的四项。
    """
    name = os.path.basename(folder.rstrip("/\\"))
    return {
        "title": folder_title(name) or "",
        "developer": _folder_developer(name) or "",
        "version": parse_folder_version(name) or "",
        "engine": detect_engine(folder) or "",
    }


def scan(conn, roots=None, on_progress=None, log=print,
         should_stop=None):
    """Walk the roots and reconcile every game folder against the catalogue."""
    roots = roots or DEFAULT_ROOTS
    if not roots:
        log("没有可扫描的目录。在软件里点「扫描本地目录」时会让你选一个文件夹。")
        return {"matched": 0, "unmatched": []}
    index = _title_index(conn)
    title_by_game_id = _title_index_titles(index)
    found, unmatched = 0, []

    for root in roots:
        if not os.path.isdir(root):
            log("跳过（不存在）：%s" % root)
            continue
        for name in sorted(_listdir(root)):
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
            conn.execute(
                "INSERT INTO local (game_id, folder_path, folder_version)"
                " VALUES (?,?,?)"
                " ON CONFLICT(game_id) DO UPDATE SET"
                " folder_path=excluded.folder_path,"
                " folder_version=excluded.folder_version",
                (game_id, folder, parse_folder_version(name)))
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
            log("匹配：%s -> #%d %s" % (name, game_id, title_by_game_id.get(game_id, "?")))
    conn.commit()
    # A scan can name games the catalogue had no row for, and those rows bring
    # their own cover_file with them - so the memoised cover-gap count is now
    # describing a library that no longer exists.
    slg_db.invalidate_cover_gaps()
    log("扫描完成：%d 个文件夹匹配上，%d 个没匹配" % (found, len(unmatched)))
    for name in unmatched:
        log("  未匹配：%s" % name)
    return {"matched": found, "unmatched": unmatched}


def _title_index_titles(index):
    """Build a constant-time display-title lookup for a scan's title index.

    Keep the first title for each game ID, matching the former linear lookup:
    catalogue titles appear before translations and aliases in ``index``.
    """
    titles = {}
    for _, gid, title in index:
        titles.setdefault(gid, title)
    return titles


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


# --- save files -----------------------------------------------------------------

# Where each engine writes saves *inside* its own folder. Ren'Py is the one
# this library is really built around; the rest are cheap to probe, and the
# scan is manual, so a miss here costs a directory listing and nothing else.
_ENGINE_SAVE_DIRS = {
    "Ren'Py": ("game/saves", "saves"),
    "Unity": ("saves", "save", "SaveData"),
    "RPG Maker": ("save", "saves", "www/save"),
    "HTML": ("save", "saves"),
}
# The fallback probe, for a folder whose engine could not be identified.
# "game/saves" is in here on purpose: that is the Ren'Py layout, and a Ren'Py
# build shipped without a top-level renpy/ folder still writes its saves
# there. Probing it against a non-Ren'Py game costs one isdir() and finds
# nothing.
_ANY_SAVE_DIRS = ("saves", "save", "SaveData", "game/saves")

# Ren'Py parks these under %APPDATA%/RenPy alongside the real game folders.
# Without this the sweep reports engine bookkeeping as if it were a game.
_RENPY_HOUSEKEEPING = {"backups", "tokens", "cache"}


def human_size(size):
    """Bytes as a short string, for a column that gets read at a glance."""
    value = float(size)
    if value < 1024:
        return "%d B" % value
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return "%.1f %s" % (value, unit)


def _dir_stats(path):
    """(file count, total bytes, newest mtime) for a save folder.

    Recursive and error-tolerant in the same spirit as _listdir: a save the
    game currently holds open must not abort the walk and lose the whole run.
    The stamp is None rather than an epoch date when nothing was readable.
    """
    count, total, newest = 0, 0, 0.0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                stat = os.stat(os.path.join(base, name))
            except OSError:
                continue
            count += 1
            total += stat.st_size
            newest = max(newest, stat.st_mtime)
    stamp = (datetime.datetime.fromtimestamp(newest)
             .strftime("%Y-%m-%d %H:%M:%S")) if newest else None
    return count, total, stamp


def _local_low_roots():
    """The Unity save root, when this machine has one."""
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    path = os.path.join(profile, "AppData", "LocalLow")
    return [path] if os.path.isdir(path) else []


def _system_save_dirs():
    """Yield (name hint, save directory, engine) for saves kept outside the game.

    Unity and some RPG Maker builds write to LocalLow/<company>/<product> and
    %APPDATA%/RenPy/<name> instead of next to the exe. Sweeping those is the
    half of the scan that can name a game the user never bound to a folder.
    """
    found = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        renpy = os.path.join(appdata, "RenPy")
        for name in sorted(_listdir(renpy)):
            if name.lower() in _RENPY_HOUSEKEEPING:
                continue
            path = os.path.join(renpy, name)
            if os.path.isdir(path):
                found.append((name, path, "Ren'Py"))
    for base in _local_low_roots():
        for company in sorted(_listdir(base)):
            company_dir = os.path.join(base, company)
            if not os.path.isdir(company_dir):
                continue
            for product in sorted(_listdir(company_dir)):
                path = os.path.join(company_dir, product)
                if os.path.isdir(path):
                    found.append((product, path, "Unity"))
    return found


def _slug_for(conn, game_id):
    if game_id is None:
        return None
    row = conn.execute("SELECT slug FROM games WHERE id = ?",
                       (game_id,)).fetchone()
    return row["slug"] if row is not None else None


def scan_saves(conn, roots=None, on_progress=None, log=print,
               should_stop=None):
    """Inventory the save files on this machine. Read-only.

    Two passes. Every folder the user has bound gets probed for the
    directories its engine would write saves into, and then the system
    locations get swept for saves belonging to a game that was never bound at
    all. Nothing here deletes, moves or writes a save - the table it fills is
    a report, and the only way to lose a row is to scan again.

    `roots` is accepted for signature parity with scan() and is not used: a
    save directory is found by engine, not by walking a user-chosen root.
    """
    index = _title_index(conn)
    titles = _title_index_titles(index)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seen, found = set(), []

    def record(folder_name, path, engine, source_kind, game_id):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        count, size, modified = _dir_stats(path)
        if not count:
            return          # an empty saves folder is not worth a row
        slg_db.set_local_save(
            conn, path, game_slug=_slug_for(conn, game_id),
            display_title=titles.get(game_id) or folder_title(folder_name),
            engine=engine, source_kind=source_kind, save_count=count,
            total_size=size, last_modified=modified, scanned_at=stamp)
        found.append({"path": path, "count": count, "size": size})

    if should_stop is None:
        should_stop = lambda: False          # noqa: E731 - a default, not logic

    # Pass 1: saves inside a folder the user has already bound to a game.
    for row in conn.execute(
            "SELECT g.id, g.title, l.folder_path FROM local l"
            " JOIN games g ON g.id = l.game_id"
            " WHERE l.folder_path IS NOT NULL").fetchall():
        if should_stop():
            break
        folder = row["folder_path"]
        if not os.path.isdir(folder):
            continue
        engine = detect_engine(folder) or ""
        for relative in _ENGINE_SAVE_DIRS.get(engine, _ANY_SAVE_DIRS):
            path = os.path.join(folder, relative.replace("/", os.sep))
            if os.path.isdir(path):
                record(os.path.basename(folder.rstrip("/\\")), path, engine,
                       "install", row["id"])
        if on_progress:
            on_progress(row["title"])

    # Pass 2: saves the engine put somewhere the install folder never sees.
    for name, path, engine in _system_save_dirs():
        if should_stop():
            break
        record(name, path, engine, "system", match_game(conn, name, index))

    conn.commit()
    total = sum(item["size"] for item in found)
    log("存档扫描完成：%d 个目录，共 %s" % (len(found), human_size(total)))
    return {"count": len(found), "bytes": total}


# --- CLI -----------------------------------------------------------------------

def _main(argv=None):
    import argparse
    slg_util.fix_console()

    parser = argparse.ArgumentParser(prog="slgking scan", description="扫描本地游戏目录")
    default_roots = " 或 ".join(DEFAULT_ROOTS) or "（无默认目录）"
    parser.add_argument("--root", action="append", default=[],
                        help="要扫描的根目录（可重复，默认 %s）" % default_roots)
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
            scan(conn, args.root or None)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
