"""Pull the catalogue from the server instead of scraping dikgames.

The server (deploy_server.py + serve_catalog.py) does the scraping and publishes
a snapshot; this side downloads that snapshot, merges it into the local db by
slug, and fetches any missing covers. The client never touches dikgames, so a
user's IP cannot be banned for scraping.

Merging is keyed on slug and only ever writes the site catalogue: user-added
games (origin='user'), ratings, notes, manual translations, collections and
prefs live in their own tables and are left alone. A full merge every pull is
deliberate - it is a few seconds of local sqlite, and it is the only way to be
sure a change on the site actually lands.

Failures never fall back to scraping: the whole point is that the client does
not reach dikgames. A down server surfaces as a message, nothing more.
"""

import hashlib
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time

import slg_db
import slg_scrape

SERVER_BASE = "http://43.130.240.89:8080"
PREF_LAST_PULL = "last_pull_lastmod"


def fetch_manifest(timeout=30, tries=3):
    """The server's manifest dict, or None on any failure.

    Retried because the box is one cheap instance: the first request after a
    cold start or a publish can take a beat, and the manifest is ~150 bytes, so
    a retry costs nothing.
    """
    for attempt in range(tries):
        try:
            raw = slg_scrape.http_get(
                SERVER_BASE + "/manifest.json", timeout=timeout, direct=True)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a down server must read as None
            if attempt == tries - 1:
                return None
            time.sleep(0.6 * (attempt + 1))
    return None


def merge_catalog(conn, snapshot_path):
    """Upsert every site game from `snapshot_path` into `conn`. Returns (created, total).

    Reads the snapshot's games and game_tags with the snapshot's own ids, then
    re-keys them through upsert_game/upsert_detail by slug - the local ids are
    what the user's state/notes/collections point at, and they must not move.
    """
    src = sqlite3.connect(snapshot_path)
    src.row_factory = sqlite3.Row
    try:
        # The server publishes a snapshot built by its own slg_db.py, which may
        # predate the origin column. Old snapshots carry only site games, so
        # fall back to the whole table when origin is absent.
        cols = {r[1] for r in src.execute("PRAGMA table_info(games)")}
        if "origin" in cols:
            games = src.execute("SELECT * FROM games WHERE origin = 'site'").fetchall()
        else:
            games = src.execute("SELECT * FROM games").fetchall()
        tags_by_game = {}
        for game_id, name in src.execute(
                "SELECT gt.game_id, t.name FROM game_tags gt"
                " JOIN tags t ON t.id = gt.tag_id"):
            tags_by_game.setdefault(game_id, []).append(name)
    finally:
        src.close()

    created = 0
    for g in games:
        tags = tags_by_game.get(g["id"], [])
        game_id, is_new = slg_db.upsert_game(
            conn, slug=g["slug"], url=g["url"], title=g["title"],
            version=g["version"], developer=g["developer"], engine=g["engine"],
            rating=g["rating"], last_updated=g["last_updated"], tags=tags,
            complete=g["complete"], lastmod=g["lastmod"])
        slg_db.upsert_detail(
            conn, game_id, rating=g["rating"], version=g["version"],
            developer=g["developer"], overview=g["overview"],
            site_views=g["site_views"], site_likes=g["site_likes"],
            site_comments=g["site_comments"])
        if g["cover_file"] and not g["cover_file"].startswith("pending:"):
            slg_db.set_cover(conn, game_id, g["cover_file"])
        created += int(is_new)
    conn.commit()
    return created, len(games)


def _missing_covers(conn):
    """Site games whose cover_file names a file not yet in covers_dir().

    User-added games are excluded: their covers are local files the server does
    not publish, so requesting them would only ever 404 and leave the cover
    counter permanently stuck above zero.
    """
    dest = slg_db.covers_dir()
    rows = conn.execute(
        "SELECT id, cover_file FROM games"
        " WHERE origin = 'site'"
        " AND cover_file IS NOT NULL AND cover_file NOT LIKE 'pending:%'"
    ).fetchall()
    return [r for r in rows
            if r["cover_file"] and not os.path.isfile(os.path.join(dest, r["cover_file"]))]


def _download_covers(rows, log, on_progress, should_stop):
    """Fetch missing covers from the server. Returns the count that landed."""
    dest = slg_db.covers_dir()
    work, results = queue.Queue(), queue.Queue()
    for row in rows:
        work.put(row)

    def worker():
        try:
            while not should_stop():
                try:
                    row = work.get_nowait()
                except queue.Empty:
                    return
                url = SERVER_BASE + "/covers/" + row["cover_file"]
                try:
                    blob = slg_scrape.http_get(url, timeout=30, direct=True)
                except Exception:  # noqa: BLE001 - a dead image must not stop the run
                    blob = None
                results.put((row["cover_file"], blob))
        finally:
            # One sentinel per worker, however it exits. A worker that returned
            # early (queue empty) used to skip this, leaving the main loop
            # waiting on a sentinel count that could never arrive.
            results.put((None, None))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()

    done, received, sentinels = 0, 0, 0
    while received < len(rows) and sentinels < len(threads):
        if should_stop():
            break
        try:
            name, blob = results.get(timeout=0.5)
        except queue.Empty:
            continue
        if name is None:
            sentinels += 1
            continue
        received += 1
        if blob:
            try:
                with open(os.path.join(dest, name), "wb") as fh:
                    fh.write(blob)
                done += 1
            except OSError as exc:
                log("  ! 写入失败 %s：%s" % (name, exc))
        if on_progress:
            on_progress(done, len(rows))
    return done


def download_covers(conn, log=print, on_progress=None, should_stop=None):
    """Fetch every missing cover from the server. Returns the count that landed."""
    should_stop = should_stop or (lambda: False)
    missing = _missing_covers(conn)
    if not missing:
        log("封面都已下载")
        return 0
    done = _download_covers(missing, log, on_progress, should_stop)
    slg_db.invalidate_cover_gaps()
    return done


def pull(conn, log=print, on_progress=None, should_stop=None):
    """Download + merge the catalogue and its missing covers. Returns a summary dict.

    `should_stop` is a callable, as in slg_scrape, so the GUI's stop button can
    cut a long cover download short.
    """
    should_stop = should_stop or (lambda: False)
    manifest = fetch_manifest()
    if manifest is None:
        raise RuntimeError("服务器不可用，请稍后再试")

    lastmod = manifest.get("lastmod")
    count = manifest.get("count") or 0
    if not lastmod:
        raise RuntimeError("服务器目录尚未生成（manifest 缺 lastmod），请稍后再试")

    last = slg_db.get_pref(conn, PREF_LAST_PULL)
    unchanged = (last is not None and last == lastmod)
    created = 0

    if unchanged:
        log("目录已是最新（lastmod %s）" % lastmod)
    else:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            raw = slg_scrape.http_get(
                SERVER_BASE + "/slgking.db", timeout=120, direct=True)
            want = manifest.get("db_sha256")
            if want and hashlib.sha256(raw).hexdigest() != want:
                raise RuntimeError("目录校验失败（sha256 不一致），已放弃本次更新")
            with open(tmp, "wb") as fh:
                fh.write(raw)
            created, _total = merge_catalog(conn, tmp)
        finally:
            os.remove(tmp)
        slg_db.set_pref(conn, PREF_LAST_PULL, lastmod)
        log("全站 %d 款 · 新增 %d" % (count, created))

    covers = _download_covers(_missing_covers(conn), log, on_progress, should_stop)
    slg_db.invalidate_cover_gaps()
    return {"catalogue": count, "new": created,
            "covers": covers, "unchanged": unchanged}
