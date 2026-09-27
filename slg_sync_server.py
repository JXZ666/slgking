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
import re
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import slg_db
import slg_scrape

SERVER_BASE = "https://slg-king.com"
PREF_LAST_PULL = "last_pull_lastmod"
PREF_LAST_PULL_HASH = "last_pull_db_sha256"


def _server_http_get(url, timeout=30):
    """Fetch one HTTPS catalogue asset, preferring direct access.

    Some users need a system/VPN proxy to reach the domain even while the
    server itself is healthy. Keep the direct path first, then retry through
    urllib's normal system-proxy configuration when one is configured for this
    host. Both paths retain HTTPS; the database caller still verifies SHA-256.
    """
    try:
        return slg_scrape.http_get(url, timeout=timeout, direct=True)
    except Exception as direct_error:  # noqa: BLE001 - retry only this fixed HTTPS origin
        parsed = urllib.parse.urlsplit(url)
        proxies = urllib.request.getproxies()
        proxy_configured = bool(
            (parsed.scheme.lower() in proxies or "all" in proxies)
            and not urllib.request.proxy_bypass(parsed.hostname or ""))
        if not proxy_configured:
            raise direct_error
        try:
            return slg_scrape.http_get(url, timeout=timeout, direct=False)
        except Exception as proxy_error:  # noqa: BLE001 - surface a useful combined failure
            raise RuntimeError(
                "HTTPS server request failed directly and through the system proxy"
            ) from proxy_error


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(
        "PRAGMA table_info(\"%s\")" % str(table).replace('"', '""'))}


def _redirect_rows(rows, catalogue_slugs):
    """Validate a snapshot redirect graph and flatten it to existing games."""
    redirects = {}
    for row in rows:
        old_slug, canonical_slug = str(row[0] or ""), str(row[1] or "")
        if old_slug and canonical_slug and old_slug != canonical_slug:
            redirects[old_slug] = canonical_slug
    valid = {}
    for old_slug, canonical_slug in redirects.items():
        current = canonical_slug
        seen = {old_slug}
        while current in redirects:
            if current in seen:
                current = ""
                break
            seen.add(current)
            current = redirects[current]
        if (old_slug in catalogue_slugs and current and
                current in catalogue_slugs and current != old_slug):
            valid[old_slug] = current
    return valid


def _create_catalog_backup(conn):
    """Create a content-addressed, non-overwriting sibling database backup."""
    row = conn.execute("PRAGMA database_list").fetchone()
    database_path = row[2] if row and len(row) > 2 else ""
    if not database_path:
        raise RuntimeError("slug redirect migration requires a file-backed local database")
    directory = os.path.dirname(os.path.abspath(database_path))
    fd, temporary = tempfile.mkstemp(
        prefix=".slgking-redirect-backup-", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        backup = sqlite3.connect(temporary)
        try:
            conn.backup(backup)
        finally:
            backup.close()
        digest = hashlib.sha256()
        with open(temporary, "rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        stem = "slgking-redirect-backup-" + digest.hexdigest()
        suffix = 0
        while True:
            tail = "" if suffix == 0 else "-%02d" % suffix
            target = os.path.join(directory, stem + tail + ".db")
            try:
                # A hard link publishes the complete SQLite backup atomically
                # and fails instead of replacing any existing user file.
                os.link(temporary, target)
                return target
            except FileExistsError:
                existing_hash = hashlib.sha256()
                with open(target, "rb") as existing:
                    for chunk in iter(lambda: existing.read(65536), b""):
                        existing_hash.update(chunk)
                if existing_hash.hexdigest() == digest.hexdigest():
                    return target
                suffix += 1
            except OSError as exc:
                if os.path.exists(target):
                    suffix += 1
                    continue
                raise RuntimeError("could not reserve redirect backup path") from exc
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def _meaningful(value):
    return value is not None and not (isinstance(value, str) and not value.strip())


def _fields_conflict(left, right, fields):
    return any(_meaningful(left[name]) and _meaningful(right[name])
               and left[name] != right[name] for name in fields)


def _translation_conflict(conn, table, old_ref, canonical_ref):
    columns = _table_columns(conn, table)
    if not {"kind", "ref", "lang"} <= columns:
        return False
    content = sorted(columns - {"kind", "ref", "lang"})
    old_rows = conn.execute(
        "SELECT * FROM \"%s\" WHERE kind IN ('title','overview') AND ref=?" % table,
        (old_ref,)).fetchall()
    for old in old_rows:
        new = conn.execute(
            "SELECT * FROM \"%s\" WHERE kind=? AND ref=? AND lang=?" % table,
            (old["kind"], canonical_ref, old["lang"])).fetchone()
        if new and any(old[name] != new[name] for name in content):
            return True
    return False


def _unknown_game_reference(conn, old_id, old_slug):
    """Conservatively reject hidden cards with unhandled local references."""
    supported = {
        "games", "tags", "game_tags", "game_aliases", "local", "state",
        "collection_items", "wishlist_version_seen", "translations",
        "manual_translations", "comments", "game_sources", "catalog_redirects",
        "game_redirects", "weights", "affinities", "prefs", "collections",
        "exclusions", "sync_log", "points_log", "signin", "owned_titles",
        "source_sync_state", "sqlite_sequence",
    }
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table in supported:
            continue
        columns = _table_columns(conn, table)
        probes = []
        if "game_id" in columns:
            probes.append(("game_id", old_id))
        if "game_slug" in columns:
            probes.append(("game_slug", old_slug))
        if "ref" in columns:
            probes.append(("ref", str(old_id)))
        for fk in conn.execute(
                "PRAGMA foreign_key_list(\"%s\")" % table.replace('"', '""')):
            if str(fk[2]).casefold() == "games" and fk[3]:
                probes.append((fk[3], old_id))
        for column, value in probes:
            if column not in columns:
                continue
            sql = "SELECT 1 FROM \"%s\" WHERE \"%s\"=? LIMIT 1" % (
                table.replace('"', '""'), column.replace('"', '""'))
            if conn.execute(sql, (value,)).fetchone():
                return True
    return False


def _redirect_pair_conflict(conn, old_id, canonical_id, old_slug):
    for table, fields in (("state", ("status", "note", "my_rating")),
                          ("local", ("folder_path", "folder_version", "exe_path"))):
        if not {"game_id"} <= _table_columns(conn, table):
            continue
        old = conn.execute("SELECT * FROM %s WHERE game_id=?" % table,
                           (old_id,)).fetchone()
        new = conn.execute("SELECT * FROM %s WHERE game_id=?" % table,
                           (canonical_id,)).fetchone()
        if old and new and _fields_conflict(old, new,
                                            [name for name in fields
                                             if name in old.keys() and name in new.keys()]):
            return True

    if "game_id" in _table_columns(conn, "wishlist_version_seen"):
        old = conn.execute(
            "SELECT seen_version FROM wishlist_version_seen WHERE game_id=?",
            (old_id,)).fetchone()
        new = conn.execute(
            "SELECT seen_version FROM wishlist_version_seen WHERE game_id=?",
            (canonical_id,)).fetchone()
        if old and new and _fields_conflict(old, new, ("seen_version",)):
            return True

    # A collection membership has its own creation timestamp. INSERT OR
    # IGNORE below would discard the old timestamp when both cards already
    # belong to the same collection, so leave that pair visible for review.
    if {"collection_id", "game_id", "added_at"} <= _table_columns(
            conn, "collection_items"):
        for old in conn.execute(
                "SELECT collection_id,added_at FROM collection_items"
                " WHERE game_id=?", (old_id,)):
            new = conn.execute(
                "SELECT added_at FROM collection_items"
                " WHERE collection_id=? AND game_id=?",
                (old["collection_id"], canonical_id)).fetchone()
            if new and _fields_conflict(old, new, ("added_at",)):
                return True

    if "game_id" in _table_columns(conn, "game_aliases"):
        old_aliases = conn.execute(
            "SELECT alias,source FROM game_aliases WHERE game_id=?", (old_id,))
        for alias in old_aliases:
            existing = conn.execute(
                "SELECT source FROM game_aliases WHERE game_id=? AND alias=?",
                (canonical_id, alias["alias"])).fetchone()
            if existing and existing["source"] != alias["source"]:
                return True

    old_ref, canonical_ref = str(old_id), str(canonical_id)
    for table in ("translations", "manual_translations"):
        if _translation_conflict(conn, table, old_ref, canonical_ref):
            return True

    source_columns = _table_columns(conn, "game_sources")
    if {"game_id", "source_id", "external_id"} <= source_columns:
        comparison = sorted(source_columns - {"game_id"})
        old_rows = conn.execute(
            "SELECT * FROM game_sources WHERE game_id=?", (old_id,)).fetchall()
        for old in old_rows:
            new = conn.execute(
                "SELECT * FROM game_sources WHERE source_id=? AND external_id=?",
                (old["source_id"], old["external_id"])).fetchone()
            if new and any(old[name] != new[name] for name in comparison):
                return True

    return _unknown_game_reference(conn, old_id, old_slug)


def _merge_redirect_pair(conn, old_id, canonical_id, old_slug, canonical_slug):
    """Move supported references after a conflict-free preflight."""
    for table, fields in (("state", ("status", "note", "my_rating")),
                          ("local", ("folder_path", "folder_version", "exe_path"))):
        columns = _table_columns(conn, table)
        if "game_id" not in columns:
            continue
        old = conn.execute("SELECT * FROM %s WHERE game_id=?" % table,
                           (old_id,)).fetchone()
        if not old:
            continue
        new = conn.execute("SELECT * FROM %s WHERE game_id=?" % table,
                           (canonical_id,)).fetchone()
        if not new:
            conn.execute("UPDATE %s SET game_id=? WHERE game_id=?" % table,
                         (canonical_id, old_id))
            continue
        assignments, values = [], []
        for name in fields:
            if name in columns and not _meaningful(new[name]) and _meaningful(old[name]):
                assignments.append("%s=?" % name)
                values.append(old[name])
        if "updated_at" in columns:
            stamp = max([value for value in (old["updated_at"], new["updated_at"])
                         if value], default=None)
            assignments.append("updated_at=?")
            values.append(stamp)
        if assignments:
            conn.execute("UPDATE %s SET %s WHERE game_id=?" % (
                table, ",".join(assignments)), values + [canonical_id])
        conn.execute("DELETE FROM %s WHERE game_id=?" % table, (old_id,))

    if "game_id" in _table_columns(conn, "wishlist_version_seen"):
        old = conn.execute(
            "SELECT seen_version FROM wishlist_version_seen WHERE game_id=?",
            (old_id,)).fetchone()
        new = conn.execute(
            "SELECT seen_version FROM wishlist_version_seen WHERE game_id=?",
            (canonical_id,)).fetchone()
        if old:
            if not new:
                conn.execute("UPDATE wishlist_version_seen SET game_id=? WHERE game_id=?",
                             (canonical_id, old_id))
            else:
                if not _meaningful(new["seen_version"]) and _meaningful(old["seen_version"]):
                    conn.execute(
                        "UPDATE wishlist_version_seen SET seen_version=? WHERE game_id=?",
                        (old["seen_version"], canonical_id))
                conn.execute("DELETE FROM wishlist_version_seen WHERE game_id=?",
                             (old_id,))

    if "game_id" in _table_columns(conn, "game_tags"):
        conn.execute(
            "INSERT OR IGNORE INTO game_tags(game_id,tag_id)"
            " SELECT ?,tag_id FROM game_tags WHERE game_id=?",
            (canonical_id, old_id))
        conn.execute("DELETE FROM game_tags WHERE game_id=?", (old_id,))

    if {"collection_id", "game_id"} <= _table_columns(conn, "collection_items"):
        for old in conn.execute(
                "SELECT collection_id,added_at FROM collection_items"
                " WHERE game_id=?", (old_id,)).fetchall():
            new = conn.execute(
                "SELECT added_at FROM collection_items"
                " WHERE collection_id=? AND game_id=?",
                (old["collection_id"], canonical_id)).fetchone()
            if new and not _meaningful(new["added_at"]) and _meaningful(
                    old["added_at"]):
                conn.execute(
                    "UPDATE collection_items SET added_at=?"
                    " WHERE collection_id=? AND game_id=?",
                    (old["added_at"], old["collection_id"], canonical_id))
        conn.execute(
            "INSERT OR IGNORE INTO collection_items(collection_id,game_id,added_at)"
            " SELECT collection_id,?,added_at FROM collection_items WHERE game_id=?",
            (canonical_id, old_id))
        conn.execute("DELETE FROM collection_items WHERE game_id=?", (old_id,))

    if {"game_id", "alias", "source"} <= _table_columns(conn, "game_aliases"):
        conn.execute(
            "INSERT OR IGNORE INTO game_aliases(game_id,alias,source)"
            " SELECT ?,alias,source FROM game_aliases WHERE game_id=?",
            (canonical_id, old_id))
        conn.execute("DELETE FROM game_aliases WHERE game_id=?", (old_id,))

    for table in ("translations", "manual_translations"):
        columns = _table_columns(conn, table)
        if not {"kind", "ref", "lang"} <= columns:
            continue
        old_rows = conn.execute(
            "SELECT * FROM \"%s\" WHERE kind IN ('title','overview') AND ref=?" % table,
            (str(old_id),)).fetchall()
        for old in old_rows:
            new = conn.execute(
                "SELECT 1 FROM \"%s\" WHERE kind=? AND ref=? AND lang=?" % table,
                (old["kind"], str(canonical_id), old["lang"])).fetchone()
            if new:
                conn.execute(
                    "DELETE FROM \"%s\" WHERE kind=? AND ref=? AND lang=?" % table,
                    (old["kind"], str(old_id), old["lang"]))
            else:
                conn.execute(
                    "UPDATE \"%s\" SET ref=? WHERE kind=? AND ref=? AND lang=?" % table,
                    (str(canonical_id), old["kind"], str(old_id), old["lang"]))

    if {"game_slug"} <= _table_columns(conn, "comments"):
        conn.execute("UPDATE comments SET game_slug=? WHERE game_slug=?",
                     (canonical_slug, old_slug))

    source_columns = _table_columns(conn, "game_sources")
    if {"game_id", "source_id", "external_id"} <= source_columns:
        old_rows = conn.execute(
            "SELECT * FROM game_sources WHERE game_id=?", (old_id,)).fetchall()
        for old in old_rows:
            duplicate = conn.execute(
                "SELECT game_id FROM game_sources WHERE source_id=? AND external_id=?",
                (old["source_id"], old["external_id"])).fetchone()
            if duplicate and int(duplicate["game_id"]) != int(old_id):
                conn.execute(
                    "DELETE FROM game_sources WHERE source_id=? AND external_id=?",
                    (old["source_id"], old["external_id"]))
            else:
                conn.execute(
                    "UPDATE game_sources SET game_id=? WHERE source_id=? AND external_id=?",
                    (canonical_id, old["source_id"], old["external_id"]))
        if "suggested_game_id" in source_columns:
            conn.execute("UPDATE game_sources SET suggested_game_id=?"
                         " WHERE suggested_game_id=?", (canonical_id, old_id))


def _redirect_has_user_references(conn, old_id, old_slug):
    for table in ("state", "local", "wishlist_version_seen",
                  "collection_items", "game_aliases"):
        if "game_id" in _table_columns(conn, table) and conn.execute(
                "SELECT 1 FROM %s WHERE game_id=? LIMIT 1" % table,
                (old_id,)).fetchone():
            return True
    if "game_slug" in _table_columns(conn, "comments") and conn.execute(
            "SELECT 1 FROM comments WHERE game_slug=? LIMIT 1", (old_slug,)
            ).fetchone():
        return True
    for table in ("translations", "manual_translations"):
        columns = _table_columns(conn, table)
        if {"kind", "ref"} <= columns and conn.execute(
                "SELECT 1 FROM \"%s\" WHERE kind IN ('title','overview')"
                " AND ref=? LIMIT 1" % table, (str(old_id),)).fetchone():
            return True
    return False


def _redirect_plan(conn, redirects):
    plan = []
    for old_slug, canonical_slug in sorted(redirects.items()):
        canonical = conn.execute(
            "SELECT id,origin FROM games WHERE slug=?", (canonical_slug,)
        ).fetchone()
        old = conn.execute(
            "SELECT id,origin FROM games WHERE slug=?", (old_slug,)
        ).fetchone()
        safe = bool(canonical and canonical["origin"] == "site")
        if old and old["origin"] != "site":
            safe = False
        if safe and old and int(old["id"]) != int(canonical["id"]):
            safe = not _redirect_pair_conflict(
                conn, int(old["id"]), int(canonical["id"]), old_slug)
        plan.append((old_slug, canonical_slug,
                     int(old["id"]) if old else None,
                     int(canonical["id"]) if canonical else None,
                     safe))
    return plan


def _sync_catalog_redirects(conn, redirects):
    """Back up only when a redirect state or local user reference will change."""
    existing = {row["old_slug"]: row for row in conn.execute(
        "SELECT old_slug,canonical_slug,active FROM catalog_redirects")}
    plan = _redirect_plan(conn, redirects)
    needs_backup = False
    if not redirects:
        needs_backup = any(row["active"] for row in existing.values())
    else:
        for old_slug, canonical_slug, old_id, canonical_id, safe in plan:
            previous = existing.get(old_slug)
            if (previous is None and safe) or (
                    previous is not None and
                    (previous["canonical_slug"] != canonical_slug or
                     bool(previous["active"]) != bool(safe))):
                needs_backup = True
                break
            if (safe and old_id is not None and canonical_id is not None
                    and old_id != canonical_id
                    and _redirect_has_user_references(conn, old_id, old_slug)):
                needs_backup = True
                break

    conn.commit()
    backup_path = _create_catalog_backup(conn) if needs_backup else None
    active = blocked = 0
    migrated_pair = False
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM catalog_redirects")
        for old_slug, canonical_slug, old_id, canonical_id, safe in plan:
            if safe and old_id is not None and canonical_id is not None \
                    and old_id != canonical_id:
                # Earlier redirects in this transaction may have populated
                # the target. A preflight made before the loop is no longer
                # sufficient when several old cards share one target.
                safe = not _redirect_pair_conflict(
                    conn, old_id, canonical_id, old_slug)
                if safe:
                    _merge_redirect_pair(conn, old_id, canonical_id,
                                         old_slug, canonical_slug)
                    migrated_pair = True
            conn.execute(
                "INSERT INTO catalog_redirects(old_slug,canonical_slug,active)"
                " VALUES (?,?,?)",
                (old_slug, canonical_slug, int(safe)))
            if safe:
                active += 1
            else:
                blocked += 1
        if migrated_pair:
            # Tag weights and developer/engine affinities are derived from
            # ratings. Rebuild them in the same transaction as the slug merge
            # so taste sorting cannot keep stale evidence from the hidden row.
            slg_db.recompute_weights(conn, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"active": active, "blocked": blocked, "backup": backup_path}


def fetch_manifest(timeout=30, tries=3):
    """The server's manifest dict, or None on any failure.

    Retried because the box is one cheap instance: the first request after a
    cold start or a publish can take a beat, and the manifest is ~150 bytes, so
    a retry costs nothing.
    """
    for attempt in range(tries):
        try:
            raw = _server_http_get(SERVER_BASE + "/manifest.json", timeout=timeout)
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
        snapshot_slug = {row["id"]: row["slug"] for row in games}
        source_rows = []
        source_cols = {r[1] for r in src.execute("PRAGMA table_info(game_sources)")}
        if {"source_id", "external_id", "game_id", "source_url"} <= source_cols:
            source_rows = src.execute(
                "SELECT * FROM game_sources WHERE source_id IN ('dikgames','f95zone')"
            ).fetchall()
        redirect_rows = []
        redirect_cols = {r[1] for r in src.execute(
            "PRAGMA table_info(game_redirects)")}
        if {"old_slug", "canonical_slug"} <= redirect_cols:
            redirect_rows = src.execute(
                "SELECT old_slug,canonical_slug FROM game_redirects"
            ).fetchall()
    finally:
        src.close()

    created = 0
    for g in games:
        # A locally added game with the same slug is still the user's game.
        # Never let a server source replace its title, URL, tags or local fields.
        existing = conn.execute(
            "SELECT id, origin FROM games WHERE slug=?", (g["slug"],)).fetchone()
        if existing is not None and existing["origin"] == "user":
            continue
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

    # Source provenance is additive. Re-key both the mapped game and any
    # proposed duplicate candidate through slugs, never through server IDs.
    # Old snapshots simply have no game_sources table and remain compatible.
    for source in source_rows:
        slug = snapshot_slug.get(source["game_id"])
        if not slug:
            continue
        local_game = conn.execute(
            "SELECT id, origin FROM games WHERE slug=?", (slug,)).fetchone()
        if local_game is None or local_game["origin"] != "site":
            continue
        suggested_id = None
        if "suggested_game_id" in source_cols and source["suggested_game_id"]:
            candidate_slug = snapshot_slug.get(source["suggested_game_id"])
            if candidate_slug:
                candidate = conn.execute(
                    "SELECT id FROM games WHERE slug=? AND origin='site'",
                    (candidate_slug,)).fetchone()
                suggested_id = candidate["id"] if candidate else None
        tags = []
        if "tags_json" in source_cols and source["tags_json"]:
            try:
                tags = json.loads(source["tags_json"])
            except (TypeError, ValueError):
                tags = []
        slg_db.upsert_game_source(
            conn, source["source_id"], source["external_id"], local_game["id"],
            source_url=source["source_url"],
            source_title=source["source_title"] if "source_title" in source_cols else None,
            source_version=source["source_version"] if "source_version" in source_cols else None,
            developer=source["developer"] if "developer" in source_cols else None,
            engine=source["engine"] if "engine" in source_cols else None,
            overview=source["overview"] if "overview" in source_cols else None,
            tags=tags,
            source_modified=source["source_modified"] if "source_modified" in source_cols else None,
            content_hash=source["content_hash"] if "content_hash" in source_cols else None,
            match_status=source["match_status"] if "match_status" in source_cols else "new",
            match_confidence=source["match_confidence"] if "match_confidence" in source_cols else None,
            suggested_game_id=suggested_id)
    conn.commit()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS catalog_redirects ("
        "old_slug TEXT PRIMARY KEY, canonical_slug TEXT NOT NULL,"
        "active INTEGER NOT NULL DEFAULT 0)")
    redirects = _redirect_rows(
        redirect_rows, {row["slug"] for row in games})
    _sync_catalog_redirects(conn, redirects)
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
                    blob = _server_http_get(url, timeout=30)
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
    want = manifest.get("db_sha256")
    if want is not None and (not isinstance(want, str) or
                             re.fullmatch(r"[0-9a-fA-F]{64}", want) is None):
        raise RuntimeError("服务器目录校验信息无效，请稍后再试")
    if want:
        want = want.casefold()
    count = manifest.get("count") or 0
    if not lastmod and not want:
        raise RuntimeError("服务器目录尚未生成，请稍后再试")

    # A manual source mapping changes the snapshot without necessarily
    # changing MAX(games.lastmod). Prefer its content hash when available.
    # Older manifests still use the original lastmod preference.
    last = slg_db.get_pref(conn, PREF_LAST_PULL_HASH if want else PREF_LAST_PULL)
    unchanged = (last is not None and last == (want or lastmod))
    created = 0

    if unchanged:
        log("目录已是最新")
    else:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            raw = _server_http_get(SERVER_BASE + "/slgking.db", timeout=120)
            if want and hashlib.sha256(raw).hexdigest() != want:
                raise RuntimeError("目录校验失败（sha256 不一致），已放弃本次更新")
            with open(tmp, "wb") as fh:
                fh.write(raw)
            created, _total = merge_catalog(conn, tmp)
        finally:
            os.remove(tmp)
        if lastmod:
            slg_db.set_pref(conn, PREF_LAST_PULL, lastmod)
        if want:
            slg_db.set_pref(conn, PREF_LAST_PULL_HASH, want)
        log("全站 %d 款 · 新增 %d" % (count, created))

    covers = _download_covers(_missing_covers(conn), log, on_progress, should_stop)
    slg_db.invalidate_cover_gaps()
    return {"catalogue": count, "new": created,
            "covers": covers, "unchanged": unchanged}
