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
import slg_scan_targets
import slg_scan_identity
import slg_scan_evidence

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


def _canonical_game_ids(conn):
    """Map active redirected identities, including their aliases, to one game."""
    games = {row["slug"]: row["id"] for row in conn.execute("SELECT id,slug FROM games")}
    redirects = dict(conn.execute("SELECT old_slug,canonical_slug FROM catalog_redirects WHERE active=1"))
    result = {}
    for slug, gid in games.items():
        seen, current = set(), slug
        while current in redirects and current not in seen:
            seen.add(current)
            current = redirects[current]
        result[gid] = games.get(current, gid) if current not in seen else gid
    return result


def _title_index(conn):
    """normalised title -> (game_id, display name), longest keys first.

    One game contributes several keys: its English title, its Chinese machine
    or hand-typed name, and any user-added aliases. Longest first so 'lust
    theory' wins over 'lust' when both are in the catalogue.
    """
    index = []
    canonical = _canonical_game_ids(conn)
    display_titles = {row["id"]: row["title"] for row in conn.execute("SELECT id,title FROM games")}
    for row in conn.execute("SELECT id, title FROM games"):
        if canonical[row["id"]] != row["id"]:
            continue
        index.append((_key(row["title"]), row["id"], row["title"]))
    # Canonical titles lead the index so display-title lookup does not select
    # the historical name. Historical names still recognize existing folders.
    for row in conn.execute("SELECT id,title FROM games"):
        if canonical[row["id"]] != row["id"]:
            index.append((_key(row["title"]), canonical[row["id"]], row["title"]))
    for ref, (text, _engine) in slg_db.title_translations(conn).items():
        game_id = slg_db._int_or_none(ref)
        if game_id is not None and text:
            index.append((_key(text), canonical.get(game_id, game_id), text))
    for row in conn.execute("SELECT game_id, alias FROM game_aliases"):
        index.append((_key(row["alias"]), canonical.get(row["game_id"], row["game_id"]), row["alias"]))
    seen = set()
    deduped = []
    for key, game_id, title in index:
        if not key or (key, game_id) in seen:
            continue
        seen.add((key, game_id))
        deduped.append((key, game_id, display_titles.get(game_id, title)))
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
    if len(slg_scan_identity.product_ids(folder_name)) > 1:
        return None
    candidates = match_candidates(conn, folder_name, index)
    return candidates[0]["game_id"] if len(candidates) == 1 else None


def match_candidates(conn, folder_name, index=None, *, products=None):
    """Return all plausible IDs at the strongest matching tier.

    Multiple catalogue entries sharing a name are a user decision, never an
    implicit first-row selection. Alias matches are deduplicated by game ID.
    """
    ids = slg_scan_identity.product_ids(folder_name)
    if ids:
        _ids, identified, _warning = slg_scan_identity.identify(
            folder_name, products if products is not None else slg_scan_identity.product_index(conn))
        # Multiple explicit identities are a confirmation, even when just one
        # is currently in the catalogue. scan() supplies the warning to the UI.
        return identified
    index = index if index is not None else _title_index(conn)
    key = _key(folder_title(folder_name))
    if not key:
        return []

    def candidates(matches, reason):
        unique = {}
        for _key_text, game_id, title in matches:
            unique.setdefault(game_id, {"game_id": game_id, "title": title,
                                        "reason": reason})
        return list(unique.values())

    exact = [entry for entry in index if entry[0] == key]
    if exact:
        return candidates(exact, "exact")

    # CJK titles are often 2-4 characters, so the containment length guard has
    # to drop for them: an ASCII word needs length to be a reliable signal, but
    # a two-character Chinese substring is already near-unique.
    min_len = 2 if _has_cjk(key) else 6
    contained = [entry for entry in index if entry[0] and
                 len(entry[0]) >= min_len and (entry[0] in key or key in entry[0])]
    if contained:
        # Prefer the longest matching title, but retain every ID with that
        # same title length (including Western/DLsite namesakes).
        longest = max(len(entry[0]) for entry in contained)
        return candidates([entry for entry in contained if len(entry[0]) == longest],
                          "contained")

    cjk = _has_cjk(key)
    threshold = 0.9 if cjk else 0.86
    matches = []
    for entry in index:
        title_key = entry[0]
        if not title_key or _has_cjk(title_key) != cjk:
            continue
        minimum = 2 if cjk else 5
        if min(len(key), len(title_key)) < minimum:
            continue
        longest = max(len(key), len(title_key))
        if abs(len(key) - len(title_key)) > longest * 0.4:
            continue
        matcher = difflib.SequenceMatcher(None, key, title_key)
        if matcher.quick_ratio() > threshold and matcher.ratio() > threshold:
            matches.append(entry)
    return candidates(matches, "fuzzy")


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
    """Compatibility wrapper; precise multi-engine detection lives separately."""
    from slg_game_detection import detect_engine as detect
    return detect(folder)


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


def normalize_path(path):
    """Canonical path key; resolve aliases and case-fold on Windows."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


_NON_GAME_EXE = re.compile(r"^(?:unins\d*|uninstall.*|crash.*|unitycrashhandler.*|"
                           r"vc_redist.*|dxsetup|setup|install.*)$", re.I)


def executable_candidates(folder, entries=None):
    """Bounded launch discovery, including packaged engine subdirectories."""
    from slg_game_detection import detect_game
    return detect_game(folder)["executables"]


def scan(conn, roots=None, on_progress=None, log=print,
         should_stop=None, *, mode="children", apply=True, targets=None,
         force_redetect=False):
    """Identify local directories, optionally applying unambiguous bindings.

    ``apply=False`` is the GUI preview API and never changes the database.
    Legacy callers retain ``matched`` / ``unmatched``; ``items`` contains full
    paths, candidate IDs and executables. Cancellation performs no writes.
    ``single`` inspects each supplied directory itself; ``children`` inspects
    only direct child directories. An existing different binding is protected.
    ``targets=[{"path": ..., "mode": "single" | "children"}, ...]`` allows
    mixed modes in one run and takes precedence over legacy roots/mode.
    Target validation finishes before any directory enumeration or writes.
    ``force_redetect=True`` bypasses valid registered-launch quick checks.
    """
    if targets is None:
        if mode not in ("single", "children", "discover"):
            raise ValueError("mode must be single or children")
        roots = DEFAULT_ROOTS if roots is None else roots
        targets = [{"path": os.fspath(root), "mode": mode} for root in roots]
    targets = slg_scan_targets.normalize_targets(targets)
    result = {"matched": 0, "unmatched": [], "items": [], "errors": [],
              "cancelled": False}
    index = _title_index(conn)
    title_by_game_id = _title_index_titles(index)
    bindings = {}
    by_id = {}
    canonical = _canonical_game_ids(conn)
    products = slg_scan_identity.product_index(conn, canonical)
    registered = {}
    for row in conn.execute("SELECT l.game_id,l.folder_path,l.exe_path,g.engine FROM local l"
                            " JOIN games g ON g.id=l.game_id"):
        if row["folder_path"]:
            key = normalize_path(row["folder_path"])
            gid = canonical.get(row["game_id"], row["game_id"])
            if gid not in bindings.setdefault(key, []):
                bindings[key].append(gid)
            by_id[gid] = key
            registered[gid] = dict(row)
    seen = set()

    def cancelled():
        return bool(should_stop and should_stop())

    def error(path, exc):
        result["errors"].append({"path": os.path.abspath(path),
                                 "reason": str(exc),
                                 "message": str(exc),
                                 "kind": type(exc).__name__})
        log("无法读取：%s：%s" % (path, exc))

    def inspect(folder, root_preview=None):
        if cancelled():
            return
        path = os.path.abspath(folder)
        if os.path.islink(path) or getattr(os.path, "isjunction", lambda p: False)(path):
            error(path, ValueError("跳过符号链接或目录联接"))
            return
        path_key = normalize_path(path)
        if path_key in seen:
            return
        seen.add(path_key)
        name = os.path.basename(path.rstrip("/\\"))
        bound = bindings.get(path_key, [])
        ids, identified, identity_warning = slg_scan_identity.identify(name, products)
        choices = ([{"game_id": gid, "title": title_by_game_id.get(gid, "?"),
                     "reason": "existing_path"} for gid in bound]
                   if bound else identified if ids else match_candidates(conn, name, index, products=products))
        if bound and ids and (not identified or
                {choice["game_id"] for choice in identified} != set(bound)):
            identity_warning = "已有路径绑定与目录商品编号不一致，请人工确认"
            # Retain the existing binding first, but expose the alternative for
            # confirmation. No automatic overwrite is allowed.
            choices += [choice for choice in identified
                        if choice["game_id"] not in set(bound)]
        detection_mode = "full"
        try:
            known = registered.get(bound[0]) if len(bound) == 1 else None
            exe = known["exe_path"] if known else None
            quick = False
            if not force_redetect and exe and os.path.isdir(path) and os.path.isfile(exe):
                try:
                    quick = os.path.commonpath((path_key, normalize_path(exe))) == path_key
                except ValueError:
                    pass
            if root_preview is not None and not quick:
                detection = root_preview
                exes = root_preview["executables"]
                evidence = {"titles": root_preview["title_evidence"], "identities": root_preview["identities"],
                            "warnings": root_preview["warnings"]}
                detection_mode = "discovered_root"
            elif quick:
                detection_mode = "quick_check"
                detection = {"executables": [os.path.abspath(exe)],
                             "engine": known["engine"] or "", "warnings": []}
            else:
                os.listdir(path)  # Surface unreadable/missing roots explicitly.
                from slg_game_detection import detect_game
                detection = detect_game(path, should_stop=should_stop)
            exes = detection["executables"]
        except OSError as exc:
            error(path, exc)
            return
        if cancelled():
            return
        if root_preview is not None:
            evidence = {"titles": root_preview["title_evidence"], "identities": root_preview["identities"],
                        "warnings": root_preview["warnings"]}
        else:
            evidence = slg_scan_evidence.extract_evidence(path, exes, should_stop=should_stop)
        explicit = evidence.get("identities", [])
        evidence_choices = {}
        unresolved_identity = False
        for ref in explicit:
            if ref["source"] == "dlsite":
                found = products.get(ref["external_id"], [])
            else:
                import slg_library_metadata
                known_id = slg_library_metadata.find_existing(conn, ref["source"], ref["external_id"])
                found = ([{"game_id": known_id, "title": title_by_game_id.get(known_id, "?"),
                           "reason": "source_id"}] if known_id is not None else [])
            for choice in found:
                evidence_choices[choice["game_id"]] = choice
            if not found:
                unresolved_identity = True
        if not bound:
            if explicit:
                choices = list(evidence_choices.values())
                if not choices:
                    identity_warning = "未收录明确来源编号，可按需查询或手动添加；不按同名自动绑定"
                elif len({(ref["source"], ref["external_id"]) for ref in explicit}) > 1:
                    identity_warning = "发现多个来源编号，请核对是否为同一作品"
            elif evidence.get("titles"):
                named = {c["game_id"]: c for c in choices}
                for ref in evidence["titles"]:
                    if ref.get("ambiguous"):
                        continue  # Mixed-encoding names are only query suggestions.
                    for choice in match_candidates(conn, ref["text"], index, products=products):
                        named[choice["game_id"]] = {**choice, "evidence_reason": ref["reason"]}
                choices = list(named.values())
        elif explicit and (unresolved_identity or set(evidence_choices) != {c["game_id"] for c in choices}):
            identity_warning = "已有绑定与文件内来源编号不同，请人工确认"
            choices += [c for gid, c in evidence_choices.items() if gid not in {x["game_id"] for x in choices}]
        gid = choices[0]["game_id"] if len(choices) == 1 else None
        reason = choices[0]["reason"] if len(choices) == 1 else (
            "ambiguous" if choices else "unmatched")
        status = "matched" if gid is not None else reason
        if ids and not bound and not choices:
            reason = "unsupported_product_id" if ids[0].startswith("VJ") else "unknown_product_id"
            status = "unmatched"
        if identity_warning and (choices or bound or len(ids) > 1):
            status, reason = "ambiguous", "identity_confirmation"
        if reason in ("fuzzy", "contained"):
            status = "ambiguous"
        if gid is not None and gid in by_id and by_id[gid] != path_key:
            status, reason = "conflict", "existing_other_path"
        item = {"path": path, "name": name,
                "version": parse_folder_version(name), "game_id": gid,
                "candidates": choices, "executables": exes,
                "engine": detection["engine"], "engine_variant": (root_preview or detection).get("engine_variant", ""),
                "detected_engine": (root_preview or detection).get("engine", ""),
                "detection_warnings": detection["warnings"],
                "product_ids": ids, "match_method": ("existing_path" if bound else
                    "product_id" if ids else "title"),
                "identity_warning": identity_warning, "detection_mode": detection_mode,
                "evidence": evidence,
                "online_sources": target.get("online_sources", []),
                "target_collection_id": target.get("target_collection_id"),
                "reason": reason, "status": status}
        result["items"].append(item)
        if gid is None:
            result["unmatched"].append(name)
        elif status == "matched":
            result["matched"] += 1
            # Prevent another folder in this same preview from silently
            # overwriting the first proposed binding for an identical ID.
            by_id.setdefault(gid, path_key)
            if on_progress:
                on_progress(name, gid)
            log("匹配：%s -> #%d %s" % (name, gid,
                                        title_by_game_id.get(gid, "?")))

    for target in targets:
        if cancelled():
            break
        root, target_mode = target["path"], target["mode"]
        try:
            if target_mode == "discover":
                from slg_game_roots import discover_game_roots
                preview = discover_game_roots(root, should_stop=should_stop, max_depth=target.get("depth", 4))
                if preview["cancelled"]:
                    result["cancelled"] = True
                    break
                for warning in preview["warnings"]:
                    log("游戏根识别提示：%s：%s" % (root, warning))
                    result.setdefault("warnings", []).append("%s：%s；请核对已找到的结果，必要时缩小扫描范围。" % (root, warning))
                if not preview["roots"]:
                    if preview.get("fallback"):
                        inspect(root, root_preview=preview["fallback"])
                    else:
                        error(root, ValueError("没有找到可靠游戏根；可选择单款目录或手动添加"))
                for candidate in preview["roots"]:
                    inspect(candidate["path"], root_preview=candidate)
            elif target_mode == "single":
                inspect(root)
            else:
                def walk(parent, level):
                    if cancelled():
                        return
                    try:
                        entries = []
                        with os.scandir(parent) as listing:
                            for entry in listing:
                                if cancelled():
                                    return
                                entries.append(entry)
                                if len(entries) >= 20000:
                                    error(parent, ValueError("单目录条目上限20000，请缩小扫描范围"))
                                    break
                        entries.sort(key=lambda e: e.name.casefold())
                    except OSError as exc:
                        error(parent, exc)
                        return
                    if len(seen) >= 5000:
                        error(parent, ValueError("扫描目录上限5000，缩小范围后继续"))
                        return
                    for entry in entries:
                        if cancelled():
                            return
                        if len(seen) >= 5000:
                            error(parent, ValueError("扫描目录上限5000，缩小范围后继续"))
                            return
                        if entry.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(entry.path):
                            continue
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                            if entry.name.casefold() in {"game", "renpy", "data", "www", "engine", "binaries", "content", "node_modules", "windows"}:
                                continue
                            if level == target.get("depth", 1):
                                inspect(entry.path)
                            else:
                                walk(entry.path, level + 1)
                        except OSError as exc:
                            error(entry.path, exc)
                walk(root, 1)
        except OSError as exc:
            error(root, exc)
    if cancelled() or result.get("cancelled"):
        result["cancelled"] = True
        result["matched"] = 0
        log("扫描已取消，未登记任何目录")
        return result
    if apply:
        conn.execute("SAVEPOINT library_scan")
        try:
            for item in result["items"]:
                if cancelled():
                    conn.execute("ROLLBACK TO library_scan")
                    conn.execute("RELEASE library_scan")
                    result["cancelled"], result["matched"] = True, 0
                    log("扫描已取消，未登记任何目录")
                    return result
                if item["status"] != "matched":
                    continue
                slg_db.set_local_folder(conn, item["game_id"], item["path"], item["version"])
                conn.execute(
                    "INSERT INTO state (game_id, status, updated_at)"
                    " VALUES (?, 'downloaded', datetime('now'))"
                    " ON CONFLICT(game_id) DO UPDATE SET status = 'downloaded'"
                    " WHERE state.status IS NULL OR state.status = ''",
                    (item["game_id"],))
            if cancelled():
                conn.execute("ROLLBACK TO library_scan")
                result["cancelled"], result["matched"] = True, 0
            conn.execute("RELEASE library_scan")
        except Exception:
            conn.execute("ROLLBACK TO library_scan")
            conn.execute("RELEASE library_scan")
            raise
        if not result["cancelled"]:
            slg_db.invalidate_cover_gaps()
    log("扫描%s：%d 个唯一匹配，%d 个待确认，%d 个读取失败" % (
        "已取消" if result["cancelled"] else "完成", result["matched"],
        sum(item["status"] != "matched" for item in result["items"]),
        len(result["errors"])))
    return result


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


class _SaveScanCancelled(Exception):
    """Internal control flow: never publish a partial inventory."""


def _save_checkpoint(should_stop):
    if should_stop and should_stop():
        raise _SaveScanCancelled()


def _dir_stats(path, should_stop=None):
    """Read an entire directory; an unreadable range must retain its old row."""
    count, total, newest = 0, 0, 0.0

    def failed(error):
        raise error

    for base, _dirs, files in os.walk(path, onerror=failed):
        _save_checkpoint(should_stop)
        for name in files:
            _save_checkpoint(should_stop)
            stat = os.stat(os.path.join(base, name))
            count += 1
            total += stat.st_size
            newest = max(newest, stat.st_mtime)
    stamp = (datetime.datetime.fromtimestamp(newest)
             .strftime("%Y-%m-%d %H:%M:%S")) if newest else None
    return count, total, stamp


def _local_low_roots():
    """Return the configured Unity data range, even when currently absent."""
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return [os.path.join(profile, "AppData", "LocalLow")]


def _system_save_dirs(should_stop=None, listing=None, is_dir=None):
    """Yield existing engine directories, with cancellable enumeration.

    Unity product directories contain game data, including caches and logs;
    they must not be described as containing only save files.
    """
    listing = listing or _listdir
    is_dir = is_dir or os.path.isdir
    appdata = os.environ.get("APPDATA")
    if appdata:
        renpy = os.path.join(appdata, "RenPy")
        for name in sorted(listing(renpy)):
            _save_checkpoint(should_stop)
            if name.lower() in _RENPY_HOUSEKEEPING:
                continue
            path = os.path.join(renpy, name)
            if is_dir(path):
                yield name, path, "Ren'Py"
    for base in _local_low_roots():
        _save_checkpoint(should_stop)
        for company in sorted(listing(base)):
            _save_checkpoint(should_stop)
            company_dir = os.path.join(base, company)
            if not is_dir(company_dir):
                continue
            for product in sorted(listing(company_dir)):
                _save_checkpoint(should_stop)
                path = os.path.join(company_dir, product)
                if is_dir(path):
                    yield product, path, "Unity"


def _slug_for(conn, game_id):
    if game_id is None:
        return None
    row = conn.execute("SELECT slug FROM games WHERE id = ?",
                       (game_id,)).fetchone()
    return row["slug"] if row is not None else None


def scan_saves(conn, roots=None, on_progress=None, log=print,
               should_stop=None):
    """Read engine data, then atomically replace the inventory on success.

    Never modify game files. Cancellation retains the previous complete report;
    unreadable ranges retain their previous rows. ``roots`` remains unused.
    """
    import stat as stat_module
    import uuid

    index = _title_index(conn)
    titles = _title_index_titles(index)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    previous = [dict(row) for row in conn.execute("SELECT * FROM local_saves")]
    seen, found, failed_ranges, errors = set(), [], [], []

    def key(path):
        return os.path.normcase(os.path.abspath(path))

    def failed(path, error):
        failed_ranges.append(key(path))
        errors.append({"path": path, "message": str(error)})

    def listing(path):
        _save_checkpoint(should_stop)
        try:
            return os.listdir(path)
        except FileNotFoundError:
            return []
        except OSError as exc:
            failed(path, exc)
            return []

    def is_dir(path):
        _save_checkpoint(should_stop)
        try:
            return stat_module.S_ISDIR(os.stat(path).st_mode)
        except FileNotFoundError:
            return False
        except OSError as exc:
            failed(path, exc)
            return False

    def record(folder_name, path, engine, source_kind, game_id):
        path_key = key(path)
        if path_key in seen:
            return
        seen.add(path_key)
        try:
            count, size, modified = _dir_stats(path, should_stop)
        except OSError as exc:
            failed(path, exc)
            return
        if count:
            found.append(dict(
                path=path, game_slug=_slug_for(conn, game_id),
                display_title=titles.get(game_id) or folder_title(folder_name),
                engine=engine, source_kind=source_kind, save_count=count,
                total_size=size, last_modified=modified, scanned_at=stamp))

    def summary(rows, cancelled=False, preserved_count=0):
        return {"count": len(rows),
                "bytes": sum(int(row["total_size"] or 0) for row in rows),
                "cancelled": cancelled, "errors": errors,
                "preserved_count": preserved_count,
                "game_data_count": sum(row["engine"] == "Unity"
                                       and row["source_kind"] == "system"
                                       for row in rows)}

    try:
        _save_checkpoint(should_stop)
        for row in conn.execute(
                "SELECT g.id, g.title, l.folder_path FROM local l"
                " JOIN games g ON g.id = l.game_id"
                " WHERE l.folder_path IS NOT NULL").fetchall():
            _save_checkpoint(should_stop)
            folder = row["folder_path"]
            if not is_dir(folder):
                continue
            # Detect permission failures before detect_engine's tolerant listing.
            listing(folder)
            if key(folder) in failed_ranges:
                continue
            engine = detect_engine(folder) or ""
            for relative in _ENGINE_SAVE_DIRS.get(engine, _ANY_SAVE_DIRS):
                path = os.path.join(folder, relative.replace("/", os.sep))
                if is_dir(path):
                    record(os.path.basename(folder.rstrip("/\\")), path, engine,
                           "install", row["id"])
            if on_progress:
                on_progress(row["title"])
        for name, path, engine in _system_save_dirs(should_stop, listing, is_dir):
            _save_checkpoint(should_stop)
            record(name, path, engine, "system", match_game(conn, name, index))
        _save_checkpoint(should_stop)
    except _SaveScanCancelled:
        log("存档扫描已停止，保留上次完整台账")
        return summary(previous, cancelled=True)

    found_keys = {key(row["path"]) for row in found}
    retained = [row for row in previous
                if key(row["path"]) not in found_keys
                and any(key(row["path"]) == base
                        or key(row["path"]).startswith(base + os.sep)
                        for base in failed_ranges)]
    rows = found + retained
    savepoint = "save_inventory_" + uuid.uuid4().hex
    conn.execute("SAVEPOINT " + savepoint)
    try:
        slg_db.clear_local_saves(conn)
        for row in rows:
            slg_db.set_local_save(conn, **row)
        conn.execute("RELEASE SAVEPOINT " + savepoint)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        raise
    result = summary(rows, preserved_count=len(retained))
    log("存档扫描完成：%d 个目录，共 %s%s" % (
        result["count"], human_size(result["bytes"]),
        "；%d 个范围未能读取，保留旧记录" % len(errors) if errors else ""))
    return result


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
