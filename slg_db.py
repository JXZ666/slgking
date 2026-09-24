"""SQLite storage.

Everything the user does lands here on the spot - the ratings, the status
flags, the notes, the exclusions. Reopening the window must not lose any of
it, which is the one requirement that got its own question in the interview.
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta

STATUSES = ("want", "downloaded")
STATUS_LABELS = {"want": "想玩", "downloaded": "已下载"}

# The site stamps its own name onto the end of every og:title, so a game the
# detail page named arrives as 'Eternum [v0.9.5] [Caribdis] - dikgames' while
# the listing page's bracket parse gives just 'Eternum'. This lives in the
# storage layer rather than in slg_scrape because the migration below has to
# clean rows written before the parser knew about it, and slg_scrape already
# imports this module - the other direction would be a cycle.
SITE_SUFFIX = re.compile(r"\s*[-\u2013\u2014]\s*dikgames\s*$", re.I)


def clean_site_title(text):
    """An og:title without the site's own name hanging off the end."""
    if not text:
        return text
    return SITE_SUFFIX.sub("", text).strip() or text

# Tag weights come from star ratings: a 5-star game pushes its tags up, a
# 1-star game pushes them down. Dividing by count + SHRINK keeps a tag that
# showed up once or twice from swinging to the extremes.
SHRINK = 3.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    version      TEXT,
    developer    TEXT,
    engine       TEXT,
    rating       REAL,
    last_updated TEXT,
    overview     TEXT,
    cover_file   TEXT,
    complete     INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_synced  TEXT,
    lastmod      TEXT,
    fetch_failures INTEGER NOT NULL DEFAULT 0,
    site_views    INTEGER,
    site_likes    INTEGER,
    site_comments INTEGER,
    heat          REAL,
    origin        TEXT NOT NULL DEFAULT 'site',
    promoted      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS game_tags (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (game_id, tag_id)
);

-- Extra names a game is known by, so a folder that spells the title differently
-- still matches. Populated two ways: hand-typed by the user when they bind an
-- unmatched folder, and by tools that accumulate aliases from failed matches.
CREATE TABLE IF NOT EXISTS game_aliases (
    id      INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    alias   TEXT NOT NULL,
    source  TEXT NOT NULL DEFAULT 'user',
    UNIQUE (game_id, alias)
);

CREATE TABLE IF NOT EXISTS local (
    game_id         INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    folder_path     TEXT,
    folder_version  TEXT,
    exe_path        TEXT
);

CREATE TABLE IF NOT EXISTS state (
    game_id   INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    status    TEXT,
    note      TEXT,
    my_rating INTEGER,
    updated_at TEXT
);

-- Version baseline for games the user wants but has not installed. The local
-- folder scanner has its own comparison for installed games; this table keeps
-- only the last version the user has acknowledged for the wishlist view.
CREATE TABLE IF NOT EXISTS wishlist_version_seen (
    game_id      INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    seen_version TEXT
);

-- Named playlists the user curates. A game can sit in any number of them, and
-- deleting a collection drops its rows but never the games themselves.
CREATE TABLE IF NOT EXISTS collections (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    added_at      TEXT,
    PRIMARY KEY (collection_id, game_id)
);

CREATE TABLE IF NOT EXISTS weights (
    tag_id       INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
    weight       REAL    NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 0
);

-- Developer and engine affinities, the same shrinkage estimate the tag weights
-- use but keyed by a free-text name instead of a tag id. A game's developer and
-- engine are two more signals about taste that a plain tag list cannot carry.
CREATE TABLE IF NOT EXISTS affinities (
    kind         TEXT NOT NULL,          -- 'developer' or 'engine'
    name         TEXT NOT NULL,
    weight       REAL    NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, name)
);

CREATE TABLE IF NOT EXISTS exclusions (
    tag    TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'user'
);

CREATE TABLE IF NOT EXISTS prefs (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY,
    ran_at     TEXT NOT NULL,
    tag        TEXT,
    pages      INTEGER,
    games_seen INTEGER,
    new_games  INTEGER
);

-- Machine translations, cached so a string is only ever sent to the model
-- once. src_hash is the point of the table: the source text is not stored, so
-- the only way to tell a stale row from a fresh one is to hash what the site
-- has *now* and compare. An updated overview therefore re-translates itself
-- with no invalidation pass.
CREATE TABLE IF NOT EXISTS translations (
    kind       TEXT NOT NULL,          -- 'tag', 'overview' or 'title'
    ref        TEXT NOT NULL,          -- tag slug, or the game id as text
    lang       TEXT NOT NULL DEFAULT 'zh',
    src_hash   TEXT NOT NULL,
    text       TEXT NOT NULL,
    engine     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref, lang)
);

-- Hand-written translations, in a table of their own.
--
-- They used to share `translations` and its primary key, which made writing a
-- correction *replace* the machine row it was correcting - so editing a name
-- destroyed the translation that was there, and get_translation_row's
-- manual-outranks-everything rule then kept it destroyed. Two tables is what
-- makes the correction undoable: clear the manual row and the machine one is
-- still sitting there to fall back on.
--
-- No src_hash: the hash exists to spot "the site edited this overview", and a
-- hand-written translation answers that question the same way either way.
CREATE TABLE IF NOT EXISTS manual_translations (
    kind       TEXT NOT NULL,          -- 'tag', 'overview' or 'title'
    ref        TEXT NOT NULL,          -- tag slug, or the game id as text
    lang       TEXT NOT NULL DEFAULT 'zh',
    text       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref, lang)
);

-- User-authored comments on a game. cloud_id is the id the server handed back
-- when the comment was uploaded, NULL for a comment the author kept to
-- themselves. visibility is what the comment is meant to be - 'private' stays
-- on this machine, 'public' is meant for other users - and is independent of
-- cloud_id so a failed upload can be retried without losing the intent.
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY,
    game_slug  TEXT NOT NULL,
    content    TEXT NOT NULL,
    nickname   TEXT,
    cloud_id   TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL
);

-- Points ledger: append-only, so the balance is always SUM(delta) and every
-- change is auditable. Kept field-compatible with the future server-side
-- points_log so a local library can be imported whole when the cloud lands.
-- seal is the tamper-evident chained HMAC described next to _row_seal below.
CREATE TABLE IF NOT EXISTS points_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    delta  INTEGER NOT NULL,
    reason TEXT NOT NULL,
    at     TEXT NOT NULL,
    seal   TEXT
);

-- One row per calendar day the user signed in. Keyed by day only: the local
-- batch is single-user, so there is no device id to key on yet.
CREATE TABLE IF NOT EXISTS signin (
    day TEXT PRIMARY KEY,
    at  TEXT NOT NULL
);

-- Titles the user owns. source is how it was obtained: shop / code / default.
CREATE TABLE IF NOT EXISTS owned_titles (
    title_id     TEXT PRIMARY KEY,
    acquired_at  TEXT NOT NULL,
    source       TEXT NOT NULL,
    seal         TEXT
);
"""


def home_dir():
    """The real user profile - not whatever HOME claims to be.

    CodePilot shadows both HOME and USERPROFILE to a per-session scratch
    directory, so os.path.expanduser("~") resolves inside
    ...\\Temp\\codepilot-shadow-*\\, which vanishes when the session ends.
    HOMEDRIVE+HOMEPATH is set by Windows itself and survives the shadowing.
    """
    drive = os.environ.get("HOMEDRIVE") or ""
    path = os.environ.get("HOMEPATH") or ""
    if drive and path and os.path.isdir(drive + path):
        return drive + path
    profile = os.environ.get("USERPROFILE") or ""
    if profile and os.path.isdir(profile):
        return profile
    return os.path.expanduser("~")


def app_dir():
    """Where the db and the cover cache live: %LOCALAPPDATA%\\slgking.

    Not next to the exe - the exe is meant to be dropped on the Desktop, and a
    data folder sprouting there would be rude. Not through the shell APIs
    either: CodePilot shadows the profile, and SpecialFolders /
    [Environment]::GetFolderPath both come back empty under it. LOCALAPPDATA is
    set by Windows and survives, with home_dir() as the belt-and-braces
    fallback.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.join(home_dir(), "AppData", "Local")
    path = os.path.join(base, "slgking")
    os.makedirs(path, exist_ok=True)
    return path


def covers_dir():
    path = os.path.join(app_dir(), "covers")
    os.makedirs(path, exist_ok=True)
    return path


def db_path():
    return os.path.join(app_dir(), "slgking.db")


def avatar_path():
    """Where a user-set avatar lives: %LOCALAPPDATA%\\slgking\\avatar.png.

    Deliberately in the data folder and not the repo - it is the user's own
    picture, and the repo is public. Nothing needs to be registered anywhere:
    the file's existence *is* the setting, and deleting it restores the default
    avatar that ships in assets.
    """
    return os.path.join(app_dir(), "avatar.png")


def install_seed_db(db_src, covers_src=None):
    """Copy the bundled seed library into place on a first run.

    Called only when db_path() does not exist yet, so it can never overwrite a
    library the user has already built. The seed gives a fresh install a few
    hundred games - and their covers - to browse before the first sync. The
    caller (slg_gui) resolves the source paths from the bundle; this side stays
    packaging-agnostic.
    """
    os.makedirs(app_dir(), exist_ok=True)
    shutil.copyfile(db_src, db_path())
    if covers_src and os.path.isdir(covers_src):
        os.makedirs(covers_dir(), exist_ok=True)
        for name in os.listdir(covers_src):
            src = os.path.join(covers_src, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(covers_dir(), name))


def connect(path=None):
    conn = sqlite3.connect(path or db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Cover downloads and detail enrichment run in the background while the
    # window is already reading; WAL keeps those from blocking each other.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def session(path=None):
    """A connection that commits on success and always closes.

    The GUI workers each open their own connection - self.conn belongs to the
    tk thread and sqlite connections do not cross threads - and every one of
    them used to repeat connect()/work/close() with the close() in the try
    rather than a finally, so an exception leaked the WAL connection. This is
    the one place that pattern lives now.
    """
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Add columns that CREATE TABLE IF NOT EXISTS will not add to an old db."""
    # 账本封印列。老档补成 NULL：封印链是「链开始之前的历史行一律信任」，
    # 所以升级上来的存档不会因为一次升级就变成「被外部修改」。
    for table in _LEDGER_TABLES:
        have = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if "seal" not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN seal TEXT" % table)
            conn.commit()
    have = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    if "complete" not in have:
        conn.execute("ALTER TABLE games ADD COLUMN complete INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "lastmod" not in have:
        conn.execute("ALTER TABLE games ADD COLUMN lastmod TEXT")
        conn.commit()
    if "fetch_failures" not in have:
        conn.execute("ALTER TABLE games ADD COLUMN fetch_failures INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    for column in ("site_views", "site_likes", "site_comments"):
        if column not in have:
            conn.execute("ALTER TABLE games ADD COLUMN %s INTEGER" % column)
            conn.commit()
    if "heat" not in have:
        conn.execute("ALTER TABLE games ADD COLUMN heat REAL")
        conn.commit()
    if "origin" not in have:
        conn.execute("ALTER TABLE games ADD COLUMN origin TEXT NOT NULL DEFAULT 'site'")
        conn.commit()
    if "promoted" not in have:
        conn.execute("ALTER TABLE games ADD COLUMN promoted INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    have = {row["name"] for row in conn.execute("PRAGMA table_info(local)")}
    if "exe_path" not in have:
        conn.execute("ALTER TABLE local ADD COLUMN exe_path TEXT")
        conn.commit()
    have = {row["name"] for row in conn.execute("PRAGMA table_info(comments)")}
    if "visibility" not in have:
        # Everything written before the 评价 merge was the author's own private
        # note, so 'private' is the honest default for the back catalogue.
        conn.execute("ALTER TABLE comments ADD COLUMN visibility TEXT"
                     " NOT NULL DEFAULT 'private'")
        conn.commit()
    # Hand-written rows move out of the translation cache into their own table.
    # Deleting them from `translations` is what stops them from having already
    # destroyed the machine row underneath - but it also means anyone who edited
    # a name before this build has no machine row left to fall back to. Nothing
    # can be done about that: the old write overwrote it in place.
    conn.execute("INSERT OR REPLACE INTO manual_translations"
                 " (kind, ref, lang, text, updated_at)"
                 " SELECT kind, ref, lang, text, updated_at FROM translations"
                 " WHERE engine = ?", (ENGINE_MANUAL,))
    conn.execute("DELETE FROM translations WHERE engine = ?", (ENGINE_MANUAL,))
    conn.commit()
    # Rows the detail page named before clean_site_title existed still carry
    # the site's name. Guarded by the LIKE so the usual case - nothing to fix -
    # costs one scan and no write.
    if conn.execute("SELECT 1 FROM games WHERE title LIKE '%dikgames%'"
                    " LIMIT 1").fetchone():
        for row in conn.execute("SELECT id, title FROM games"
                                " WHERE title LIKE '%dikgames%'").fetchall():
            conn.execute("UPDATE games SET title = ? WHERE id = ?",
                         (clean_site_title(row["title"]), row["id"]))
        conn.commit()
    _repair_site_suffixed_translations(conn)
    # The "playing" status was retired: the sidebar view that filtered on it is
    # gone, so fold any rows still marked with it into "downloaded". Idempotent -
    # once migrated there are no "playing" rows left to match.
    if conn.execute("SELECT 1 FROM state WHERE status = 'playing'"
                    " LIMIT 1").fetchone():
        conn.execute("UPDATE state SET status = 'downloaded'"
                     " WHERE status = 'playing'")
        conn.commit()
    # 评价 merges into the comment list (see migrate_notes_to_comments).
    migrate_notes_to_comments(conn)


def _repair_site_suffixed_translations(conn):
    """Strip the site's name out of the cached translations too.

    Cleaning games.title is not enough on its own: the cards read the cache,
    not the title, so a name translated before the cleanup went on reading
    '永恒世界 [v0.9.5] [Caribdis] - dikgames' with a clean row underneath it.

    Dropping the row instead would work - title_translations refuses a hash
    that no longer matches - but it costs the user the Chinese name until they
    happen to switch that panel to 中文 again. The suffix came off the source,
    so taking the same suffix off the translation leaves a translation of the
    title the row now names, which is exactly what the hash is updated to say.
    Hand-typed names live in manual_translations and are not touched.
    """
    stale = [row for row in conn.execute("SELECT ref, text FROM translations"
                                         " WHERE kind = 'title'").fetchall()
             if clean_site_title(row["text"]) != row["text"]]
    for row in stale:
        game_id = _int_or_none(row["ref"])
        game = conn.execute("SELECT title FROM games WHERE id = ?",
                            (game_id,)).fetchone() if game_id is not None else None
        if game is None:
            continue
        conn.execute("UPDATE translations SET text = ?, src_hash = ?"
                     " WHERE kind = 'title' AND ref = ?",
                     (clean_site_title(row["text"]), src_hash(game["title"]),
                      row["ref"]))
    if stale:
        conn.commit()


def _now():
    return datetime.now().isoformat(timespec="seconds")


# --- catalogue -----------------------------------------------------------------

def upsert_game(conn, slug, url, title, version=None, developer=None,
                engine=None, rating=None, last_updated=None, tags=(),
                complete=0, lastmod=None):
    """Insert or refresh one scraped game. Returns (game_id, created)."""
    now = _now()
    row = conn.execute("SELECT id FROM games WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO games (slug, url, title, version, developer, engine,"
            " rating, last_updated, complete, first_seen, last_synced, lastmod)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, url, title, version, developer, engine,
             rating, last_updated, int(bool(complete)), now, now, lastmod))
        game_id, created = cur.lastrowid, True
    else:
        game_id, created = row["id"], False
        # The version is the one column where "newer source wins" is wrong, and
        # it is also the one that is not a plain COALESCE: the detail page's
        # 'Version:' line trails the listing page's bracket often enough that
        # an unconditional write walks the number down. Dropping it to NULL
        # hands the decision to the COALESCE below, which keeps what is there.
        if version is not None:
            current = conn.execute("SELECT version FROM games WHERE id = ?",
                                   (game_id,)).fetchone()["version"]
            if current and not _version_gt(version, current):
                version = None
        # Every nullable column is COALESCE'd, never assigned outright. Two
        # scrapers write this row - the tag walk has no rating to give and the
        # incremental pass has no list-page version or developer to give - so
        # a plain assignment would have each of them blanking the other's work.
        conn.execute(
            "UPDATE games SET url=?, title=?, version=COALESCE(?, version),"
            " developer=COALESCE(?, developer), engine=COALESCE(?, engine),"
            " rating=COALESCE(?, rating), last_updated=COALESCE(?, last_updated),"
            " complete=?, last_synced=?, lastmod=COALESCE(?, lastmod) WHERE id=?",
            (url, title, version, developer, engine,
             rating, last_updated, int(bool(complete)), now, lastmod, game_id))
    set_tags(conn, game_id, tags)
    return game_id, created


def set_lastmod(conn, game_id, lastmod):
    """Record the sitemap's <lastmod> without touching anything else.

    Used on the first incremental run, where every row starts with a NULL
    lastmod: adopting the current value beats treating all 1274 games as
    changed and fetching 1274 detail pages to learn nothing.
    """
    conn.execute("UPDATE games SET lastmod = ? WHERE id = ?", (lastmod, game_id))


def note_fetch_failure(conn, game_id):
    """Bump a game's consecutive-fetch-failure counter (enrich starvation)."""
    conn.execute("UPDATE games SET fetch_failures = fetch_failures + 1 WHERE id = ?",
                 (game_id,))


def clear_fetch_failures(conn, game_id):
    conn.execute("UPDATE games SET fetch_failures = 0 WHERE id = ?", (game_id,))


def set_tags(conn, game_id, tags, clear=False):
    """Replace a game's tags. An empty list means "leave them alone".

    The listing and detail parsers can both come back with zero tag links -
    a page whose markup moved, not a game that genuinely has no tags - and the
    old unconditional DELETE wiped a healthy row in that case. Reading the ids
    back in one IN query also drops the old one-SELECT-per-tag round trip.

    clear=True is the user's hand-edit path, where an empty list genuinely means
    "this game has no tags" and must clear them rather than skip.
    """
    if not tags:
        if clear:
            conn.execute("DELETE FROM game_tags WHERE game_id = ?", (game_id,))
        return
    names = list(dict.fromkeys(tags))
    conn.executemany("INSERT OR IGNORE INTO tags (name) VALUES (?)",
                     [(name,) for name in names])
    rows = conn.execute(
        "SELECT id FROM tags WHERE name IN (%s)" % ",".join("?" * len(names)),
        names).fetchall()
    conn.execute("DELETE FROM game_tags WHERE game_id = ?", (game_id,))
    conn.executemany("INSERT OR IGNORE INTO game_tags (game_id, tag_id) VALUES (?,?)",
                     [(game_id, row["id"]) for row in rows])


def set_cover(conn, game_id, cover_file):
    conn.execute("UPDATE games SET cover_file = ? WHERE id = ?", (cover_file, game_id))


def import_cover(src_path):
    """Copy a user-chosen cover image into the cache; return its filename.

    load_cover only ever reads from covers_dir(), so a user's cover has to land
    there under a name the row can point at. A fresh random name avoids stepping
    on a scraped cover and dodges any path weirdness in the source filename.
    """
    ext = os.path.splitext(src_path)[1].lower() or ".jpg"
    name = "user_" + uuid.uuid4().hex[:12] + ext
    shutil.copyfile(src_path, os.path.join(covers_dir(), name))
    return name


def add_user_game(conn, title, developer=None, engine=None, version=None,
                  overview=None, cover_file=None, tags=(), folder_path=None,
                  promoted=0):
    """Insert a game the user added themselves. Returns its id.

    The scraper's upsert_game is COALESCE-on-update because two writers share a
    row; a user game has exactly one writer (the user), so every column is a
    plain assignment here. The slug is synthetic - 'user-' + a short random hex
    - and never collides with the dikgames slugs the scraper keys on, which is
    also what keeps a later site sync from ever touching this row.
    """
    now = _now()
    slug = "user-" + uuid.uuid4().hex[:8]
    cur = conn.execute(
        "INSERT INTO games (slug, url, title, version, developer, engine,"
        " overview, cover_file, first_seen, last_synced, origin, promoted)"
        " VALUES (?,?,?,?,?,?,?,?,?,?, 'user', ?)",
        (slug, "user://" + slug, title, version, developer, engine,
         overview, cover_file, now, now, int(bool(promoted))))
    game_id = cur.lastrowid
    if folder_path:
        conn.execute(
            "INSERT INTO local (game_id, folder_path)"
            " VALUES (?,?)", (game_id, folder_path))
    set_tags(conn, game_id, tags, clear=True)
    return game_id


def update_user_game(conn, game_id, title, developer=None, engine=None,
                     version=None, overview=None, cover_file=None,
                     tags=(), folder_path=None, promoted=0):
    """Rewrite a game the user added themselves.

    The user is the only writer of their own rows, so this is a plain UPDATE,
    not the scraper's COALESCE merge. cover_file=None means "keep the current
    cover", folder_path=None means "keep the current folder"; the edit dialog
    passes them through only when the user actually changed them.
    """
    conn.execute(
        "UPDATE games SET title=?, version=?, developer=?, engine=?,"
        " overview=?, promoted=? WHERE id=?",
        (title, version, developer, engine, overview,
         int(bool(promoted)), game_id))
    if cover_file:
        conn.execute("UPDATE games SET cover_file=? WHERE id=?",
                     (cover_file, game_id))
    if folder_path:
        set_local_folder(conn, game_id, folder_path)
    set_tags(conn, game_id, tags, clear=True)


def delete_game(conn, game_id):
    """Delete a game row. Callers must only pass origin='user' games.

    The FK ON DELETE CASCADE clauses clean game_tags, local, state,
    collection_items and game_aliases; tags themselves are the shared vocabulary
    and stay behind (an orphan tag is harmless).
    """
    conn.execute("DELETE FROM games WHERE id = ?", (game_id,))


def set_local_folder(conn, game_id, folder_path, folder_version=None):
    """Point a game at a local folder, keeping the fields scan() would not know.

    Used when the user binds an unmatched folder by hand, where there is no
    Ren'Py probe or size measurement - just the path and the version in its name.
    """
    conn.execute(
        "INSERT INTO local (game_id, folder_path, folder_version)"
        " VALUES (?,?,?)"
        " ON CONFLICT(game_id) DO UPDATE SET"
        " folder_path=excluded.folder_path,"
        " folder_version=excluded.folder_version",
        (game_id, folder_path, folder_version))


def set_local_exe(conn, game_id, exe_path):
    """Remember which exe launches this locally-installed game."""
    conn.execute(
        "INSERT INTO local (game_id, exe_path)"
        " VALUES (?,?)"
        " ON CONFLICT(game_id) DO UPDATE SET"
        " exe_path=excluded.exe_path",
        (game_id, exe_path))


def add_alias(conn, game_id, alias, source="user"):
    """Record another name a game is known by, for folder matching."""
    conn.execute(
        "INSERT OR IGNORE INTO game_aliases (game_id, alias, source)"
        " VALUES (?,?,?)", (game_id, alias, source))


def promote_game(conn, game_id, on=True):
    """Let a user game into the main list (or take it back out)."""
    conn.execute("UPDATE games SET promoted = ? WHERE id = ?",
                 (int(bool(on)), game_id))


def _version_gt(left, right):
    """Is `left` a higher version than `right`?

    Digit runs only, padded on the right, so '0.5b' reads as 0.5 and
    'Ep.7 Free' as 7 - the same reading slg_scan uses to decide whether a
    local copy is behind. An unparseable side answers False, which leaves the
    stored value alone: refusing to move beats moving backwards.
    """
    def parts(text):
        return [int(n) for n in re.findall(r"\d+", str(text or ""))[:4]]
    a, b = parts(left), parts(right)
    if not a or not b:
        return False
    length = max(len(a), len(b))
    a += [0] * (length - len(a))
    b += [0] * (length - len(b))
    return a > b


def compute_heat(rating, views, likes, comments):
    """0-100 热度，偏人气：浏览 40% · 点赞 25% · 评论 20% · 评分 15%.

    Counts are log-scaled because views (tens of thousands) and likes/comments
    (single digits) otherwise differ by orders of magnitude. A missing value
    reads as zero, so a game with no data scores 0 rather than erroring.
    """
    r = (rating or 0) / 10.0
    v = math.log10((views or 0) + 1) / 5.0
    l = math.log10((likes or 0) + 1) / 3.0
    c = math.log10((comments or 0) + 1) / 3.0
    return round(100.0 * (0.40 * v + 0.25 * l + 0.20 * c + 0.15 * r), 1)


def _recompute_heat(conn, game_id):
    row = conn.execute(
        "SELECT rating, site_views, site_likes, site_comments FROM games"
        " WHERE id = ?", (game_id,)).fetchone()
    if row is None:
        return
    heat = compute_heat(row["rating"], row["site_views"],
                        row["site_likes"], row["site_comments"])
    conn.execute("UPDATE games SET heat = ? WHERE id = ?", (heat, game_id))


def backfill_heat(conn, log=print, on_progress=None):
    """Compute heat for rows that carry metrics but a NULL heat column.

    Rows migrated in before the heat column existed keep rating/views/likes/
    comments while heat stays NULL, and enrich never revisits them (its WHERE
    tests the metric columns, not heat). This is a local pass - no network -
    over exactly those rows, so it finishes in seconds.
    """
    rows = conn.execute(
        "SELECT id FROM games WHERE heat IS NULL"
        " AND (rating IS NOT NULL OR site_views IS NOT NULL"
        "      OR site_likes IS NOT NULL OR site_comments IS NOT NULL)"
        " ORDER BY last_updated DESC").fetchall()
    done = 0
    for i, (game_id,) in enumerate(rows):
        _recompute_heat(conn, game_id)
        done += 1
        if on_progress is not None:
            on_progress(done, len(rows))
    if done:
        conn.commit()
    log("补齐热度 %d/%d" % (done, len(rows)))
    return done


def upsert_detail(conn, game_id, rating=None, version=None, developer=None,
                  overview=None, site_views=None, site_likes=None,
                  site_comments=None):
    """Merge what only the detail page knows.

    Never clobbers a value with NULL, and never walks the version backwards.
    The listing page's '[v0.26.6]' bracket is the site's current version; the
    detail page's 'Version:' line is a field the author often forgets to bump,
    and letting it win is what took agent17 from 0.26.6 down to 0.25.3.
    """
    if version is not None:
        row = conn.execute("SELECT version FROM games WHERE id = ?",
                           (game_id,)).fetchone()
        if row is not None and row["version"] and not _version_gt(version, row["version"]):
            version = None
    sets, params = [], []
    for column, value in (("rating", rating), ("version", version),
                          ("developer", developer), ("overview", overview),
                          ("site_views", site_views), ("site_likes", site_likes),
                          ("site_comments", site_comments)):
        if value is not None:
            sets.append("%s = COALESCE(?, %s)" % (column, column))
            params.append(value)
    if sets:
        params.append(game_id)
        conn.execute("UPDATE games SET %s WHERE id = ?" % ", ".join(sets), params)
        conn.commit()
        _recompute_heat(conn, game_id)


# --- querying ------------------------------------------------------------------

def find_games(conn, include=(), exclude=(), search=None, statuses=None,
               downloaded_only=False, collection_id=None,
               origin="main", sort="score", desc=True):
    """Games matching a tag intersection (include) minus a tag union (exclude).

    include is an AND: every tag listed must be present. exclude is a NOT:
    any one of them is enough to drop the row.

    origin controls which games the result may contain:
      'main' - the catalogue plus user games the user promoted into it.
      'user' - only games the user added themselves (the sidebar's own view).
      None   - no origin filter (maintenance paths that need every row).
    """
    where, params = [], []

    if origin == "main":
        where.append("(g.origin = 'site' OR g.promoted = 1)")
    elif origin == "user":
        where.append("g.origin = 'user'")

    if include:
        qmarks = ",".join("?" * len(include))
        where.append(
            "g.id IN (SELECT gt.game_id FROM game_tags gt JOIN tags t"
            " ON t.id = gt.tag_id WHERE t.name IN (%s)"
            " GROUP BY gt.game_id HAVING COUNT(DISTINCT gt.tag_id) = ?)" % qmarks)
        params.extend(include)
        params.append(len(set(include)))

    if exclude:
        qmarks = ",".join("?" * len(exclude))
        where.append(
            "g.id NOT IN (SELECT gt.game_id FROM game_tags gt JOIN tags t"
            " ON t.id = gt.tag_id WHERE t.name IN (%s))" % qmarks)
        params.extend(exclude)

    if search:
        where.append("g.title LIKE ?")
        params.append("%" + search + "%")

    if statuses:
        qmarks = ",".join("?" * len(statuses))
        where.append("s.status IN (%s)" % qmarks)
        params.extend(statuses)

    if downloaded_only:
        where.append("l.game_id IS NOT NULL")

    if collection_id is not None:
        where.append(
            "g.id IN (SELECT game_id FROM collection_items WHERE collection_id = ?)")
        params.append(collection_id)

    sql = """
        SELECT g.*, s.status, s.my_rating, l.folder_path, l.folder_version,
               l.exe_path,
               COALESCE((SELECT AVG(my_rating - 3.0) FROM state
                         WHERE my_rating IS NOT NULL), 0)
             + (COALESCE(g.rating, 0)
                - COALESCE((SELECT AVG(rating) FROM games
                            WHERE rating IS NOT NULL), 0))
             + COALESCE((SELECT SUM(w.weight) FROM game_tags gt
                         JOIN weights w ON w.tag_id = gt.tag_id
                         WHERE gt.game_id = g.id), 0)
             + COALESCE((SELECT weight FROM affinities
                         WHERE kind = 'developer' AND name = g.developer), 0)
             + COALESCE((SELECT weight FROM affinities
                         WHERE kind = 'engine' AND name = g.engine), 0) AS score
        FROM games g
        LEFT JOIN state s ON s.game_id = g.id
        LEFT JOIN local l ON l.game_id = g.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)

    # Direction lives in the SQL rather than a reversed() in the caller: the
    # list is paged, so reversing after the fetch would show the wrong 80.
    direction = "DESC" if desc else "ASC"
    if sort == "title":
        # A name sort wants no tiebreaker, and titles are never NULL.
        order = "g.title COLLATE NOCASE %s" % direction
    else:
        column = {"score": "score", "rating": "g.rating",
                  "updated": "g.last_updated", "heat": "g.heat"}.get(sort, "score")
        # (col IS NULL) leads so unrated rows sink in both directions. 985
        # rows have no rating, and plain ASC would open the list with all of
        # them; score is COALESCEd, so its guard is free.
        order = "(%s IS NULL), %s %s, g.title COLLATE NOCASE" % (
            column, column, direction)
    sql += " ORDER BY " + order
    return conn.execute(sql, params).fetchall()


def game_tags(conn, game_id):
    return [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN game_tags gt ON gt.tag_id = t.id"
        " WHERE gt.game_id = ? ORDER BY t.name", (game_id,))]


def game_tags_bulk(conn, game_ids):
    """Tags for many games in one query: {game_id: [name, ...]}.

    Rendering a page of cards used to cost one query per card. Every id handed
    in comes back, with an empty list when the game carries no tags, so callers
    can index the result blindly.
    """
    ids = list(game_ids)
    out = {gid: [] for gid in ids}
    # Old SQLite builds cap a statement at 999 bound variables; a full
    # catalogue is bigger than that.
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        qmarks = ",".join("?" * len(batch))
        rows = conn.execute(
            "SELECT gt.game_id, t.name FROM game_tags gt"
            " JOIN tags t ON t.id = gt.tag_id"
            " WHERE gt.game_id IN (%s) ORDER BY t.name" % qmarks, batch)
        for row in rows:
            out[row["game_id"]].append(row["name"])
    return out


def tag_counts(conn, limit=None):
    sql = ("SELECT t.name, COUNT(*) AS n FROM tags t"
           " JOIN game_tags gt ON gt.tag_id = t.id"
           " GROUP BY t.id ORDER BY n DESC, t.name")
    if limit:
        sql += " LIMIT %d" % int(limit)
    return conn.execute(sql).fetchall()


def get_game(conn, game_id):
    return conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()


# One filesystem call per stored cover, and the library has ~1500 of them:
# 190ms on the author's machine. _render_stats asks for this on every refresh -
# a sort, a scroll-page, a status write - so the answer is memoised briefly and
# dropped outright by whatever actually changes it.
_COVER_GAP_TTL = 15.0
_missing_covers = None   # (count, monotonic seconds)
_cover_gap_lock = threading.Lock()


def invalidate_cover_gaps():
    """Forget the memo. Called after a cover download or a local scan."""
    global _missing_covers
    with _cover_gap_lock:
        _missing_covers = None


def missing_cover_files(conn, ttl=_COVER_GAP_TTL):
    """Stored covers whose file is not on disk.

    load_cover only accepts a file it can actually open, so a row naming a
    deleted file renders as a blank card however healthy the database looks.
    """
    global _missing_covers
    with _cover_gap_lock:
        now = time.monotonic()
        if _missing_covers is not None and now - _missing_covers[1] < ttl:
            return _missing_covers[0]
        rows = conn.execute("SELECT cover_file FROM games WHERE origin = 'site'"
                            " AND cover_file IS NOT NULL AND cover_file NOT LIKE 'pending:%'"
                            ).fetchall()
        directory = covers_dir()
        count = sum(1 for row in rows
                    if not os.path.exists(os.path.join(directory, row["cover_file"])))
        _missing_covers = (count, now)
        return count


# One definition of "this game's popularity counts are still unknown", shared
# by the number the maintenance dialog shows and the queue the backfill walks.
# Defined twice they drift, and the dialog ends up reading 热度已齐 while rows
# sit unread - which is the shape of the bug this counter exists to expose.
_METRICS_GAP_SQL = (
    "FROM games WHERE url IS NOT NULL AND fetch_failures < 3"
    " AND (site_views IS NULL OR site_likes IS NULL OR site_comments IS NULL)")


def metrics_gap_count(conn):
    return conn.execute("SELECT COUNT(*) " + _METRICS_GAP_SQL).fetchone()[0]


def metrics_gap_rows(conn, limit=None):
    """Games whose views/likes/comments are still unknown.

    fetch_failures leads the ordering because a page that failed last run is
    the likeliest to fail again, and sorting it behind the healthy rows keeps
    one dead page from consuming the head of every run.
    """
    sql = ("SELECT id, slug, url " + _METRICS_GAP_SQL
           + " ORDER BY fetch_failures ASC, last_updated DESC")
    if limit is None:
        return conn.execute(sql).fetchall()
    return conn.execute(sql + " LIMIT ?", (limit,)).fetchall()


def data_gaps(conn):
    """How much of the catalogue is still missing each enrichable field.

    A whitespace-only overview — and a cover_file naming a file that is not on
    disk — both count as gaps. The first because the parser's _clean turns an
    empty blurb into NULL but an old row may hold "": the panel renders the two
    identically. The second because the button's job is to promise a picture
    the user will actually see, and load_cover only accepts a file it can open,
    so counting the stored name alone is how 封面已齐 ended up meaning nothing.
    """
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "covers": one("SELECT COUNT(*) FROM games WHERE origin = 'site'"
                      " AND cover_file LIKE 'pending:%'")
        + missing_cover_files(conn),
        "rating": one("SELECT COUNT(*) FROM games WHERE rating IS NULL"),
        "overview": one("SELECT COUNT(*) FROM games WHERE origin = 'site'"
                        " AND (overview IS NULL OR TRIM(overview) = '')"),
        "heat": one("SELECT COUNT(*) FROM games WHERE heat IS NULL"
                    " AND (rating IS NOT NULL OR site_views IS NOT NULL"
                    "      OR site_likes IS NOT NULL OR site_comments IS NOT NULL)"),
        "metrics": metrics_gap_count(conn),
    }


# --- translation cache ---------------------------------------------------------

TAG_LANG = "zh"

# Written into the engine column of a row that means "this string was tried and
# cannot be translated" - a game name whose version number the model will not
# leave alone, say. The row is what stops every visit to the detail panel from
# paying for the same hopeless request again.
ENGINE_UNTRANSLATED = "untranslated"

# The engine reported for a hand-typed row. Machine translation of a name or a
# summary is never going to satisfy everyone, so the app lets them fix it - and
# the one thing that would make that useless is the next sync quietly
# translating over it. Hand-typed rows live in manual_translations, survive a
# changed source text, and refuse to be overwritten (set_auto_translation).
ENGINE_MANUAL = "manual"

# The engine reported for a row imported from the bundled tag seed
# (assets/tag_zh.json). It marks the translation as a shipped default rather
# than a per-install machine translation: the two behave identically for
# display, but the tag lets a future "reset to defaults" tell them apart.
ENGINE_SEED = "seed"


def src_hash(text):
    """Cache key for a piece of source text. Truncated: this only has to spot
    'the site edited this overview', not resist an attacker."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def get_translation_row(conn, kind, ref, src_text, lang=TAG_LANG):
    """(text, engine) for what should be shown, or None when there is nothing.

    The single resolution point for the whole app: a hand-written row wins,
    then the machine cache, then nothing. Everything that displays or edits a
    translation asks here, which is what keeps the two from disagreeing.

    The engine comes back because ENGINE_UNTRANSLATED is a marker rather than a
    translation: for those rows the caller shows the original text, not the
    stored one.

    A hand-written row is returned without checking src_hash. The hash exists to
    spot 'the site edited this overview', and the right answer to that question
    for a hand-written translation is still the hand-written translation.
    """
    manual = get_manual_translation(conn, kind, ref, lang)
    if manual is not None:
        return manual, ENGINE_MANUAL
    row = conn.execute(
        "SELECT src_hash, text, engine FROM translations"
        " WHERE kind=? AND ref=? AND lang=?", (kind, str(ref), lang)).fetchone()
    if row is None or row["src_hash"] != src_hash(src_text):
        return None
    return row["text"], row["engine"]


def get_manual_translation(conn, kind, ref, lang=TAG_LANG):
    """The user's own text for this row, or None if they have not written one."""
    row = conn.execute(
        "SELECT text FROM manual_translations WHERE kind=? AND ref=? AND lang=?",
        (kind, str(ref), lang)).fetchone()
    return None if row is None else row["text"]


def set_manual_translation(conn, kind, ref, text, lang=TAG_LANG):
    """Record a hand-written translation. The machine row underneath is left
    exactly where it is, so clearing this one brings that text back."""
    conn.execute(
        "INSERT OR REPLACE INTO manual_translations"
        " (kind, ref, lang, text, updated_at) VALUES (?,?,?,?,?)",
        (kind, str(ref), lang, text, _now()))
    conn.commit()


def set_auto_translation(conn, kind, ref, src_text, text, engine=None, lang=TAG_LANG):
    """Write a machine translation, unless the user has edited this row by hand.

    Every automatic writer goes through here rather than set_translation: a
    translation runs on a worker thread that can land seconds after the user
    started typing their own version of the same field, and losing that is the
    one failure this whole mechanism exists to prevent.

    Returns False when the write was refused.
    """
    if get_manual_translation(conn, kind, ref, lang) is not None:
        return False
    set_translation(conn, kind, ref, src_text, text, engine=engine, lang=lang)
    return True


def delete_translation(conn, kind, ref, lang=TAG_LANG):
    """Drop the cached machine row, which just forces a re-fetch.

    It cannot reach a hand-written row: those are in manual_translations and are
    removed by delete_manual_translation below. Clearing a hand-typed name has
    to hand the field back to the translator, and it cannot do that by deleting
    the machine row it never wrote over in the first place.
    """
    conn.execute("DELETE FROM translations WHERE kind=? AND ref=? AND lang=?",
                 (kind, str(ref), lang))
    conn.commit()


def delete_manual_translation(conn, kind, ref, lang=TAG_LANG):
    """Drop one hand-written row, leaving the machine one to surface in its
    place. Returns True when there was one."""
    cur = conn.execute(
        "DELETE FROM manual_translations WHERE kind=? AND ref=? AND lang=?",
        (kind, str(ref), lang))
    conn.commit()
    return cur.rowcount > 0


def delete_manual_translations(conn, kind, lang=TAG_LANG):
    """Drop every hand-typed row of a kind, leaving machine ones alone.

    The tag editor's clear button goes through this. The hundred tags nobody
    touched must not be thrown away with it.
    """
    cur = conn.execute("DELETE FROM manual_translations WHERE kind=? AND lang=?",
                       (kind, lang))
    conn.commit()
    return cur.rowcount


def get_translation(conn, kind, ref, src_text, lang=TAG_LANG):
    """The cached translation, or None when there is none *or* it is stale.

    Staleness is folded into the miss: callers only ever want to know whether
    they can display this without paying for a request.
    """
    row = get_translation_row(conn, kind, ref, src_text, lang)
    return None if row is None else row[0]


def set_translation(conn, kind, ref, src_text, text, engine=None, lang=TAG_LANG):
    conn.execute(
        "INSERT OR REPLACE INTO translations"
        " (kind, ref, lang, src_hash, text, engine, updated_at) VALUES (?,?,?,?,?,?,?)",
        (kind, str(ref), lang, src_hash(src_text), text, engine, _now()))
    conn.commit()


def import_seed_tag_translations(conn, path, lang=TAG_LANG):
    """Import the bundled tag seed (assets/tag_zh.json) into `translations`.

    Shipped defaults for users who have no translation API: the ~120 tag slugs
    are rendered on every card, so a fresh install would otherwise show English
    slugs until the user wired up a paid engine. The seed is the author's own
    machine translation, exported from his db.

    INSERT OR IGNORE means the seed only fills gaps - it never overwrites a
    translation the user already has, and a hand-typed row in manual_translations
    (which always wins at read time) is left completely alone. Returns the number
    of rows actually imported.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    now = _now()
    inserted = 0
    for slug, text in data.items():
        if not slug or not text:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO translations"
            " (kind, ref, lang, src_hash, text, engine, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("tag", str(slug), lang, src_hash(slug), text, ENGINE_SEED, now))
        inserted += cur.rowcount
    conn.commit()
    return inserted


def tag_translations(conn, lang=TAG_LANG):
    """{slug: translation} for every translated tag.

    Both tables, hand-typed last so it wins - the same precedence
    get_translation_row applies to one row at a time, for the same reason. The
    tag editor writes its entries from this dict and compares against it to skip
    untouched rows, so it has to be the text that is actually on screen.

    slg_gui caches the result at module level so display_tag() stays a dict
    lookup rather than a query per rendered chip.
    """
    both = {}
    for row in conn.execute(
            "SELECT ref, text FROM translations WHERE kind='tag' AND lang=?", (lang,)):
        both[row["ref"]] = row["text"]
    for row in conn.execute("SELECT ref, text FROM manual_translations"
                            " WHERE kind='tag' AND lang=?", (lang,)):
        both[row["ref"]] = row["text"]
    return both


def title_translations(conn, lang=TAG_LANG):
    """{game id as text: (text, engine)} for every translated name.

    One query rather than one per card: the list renders eighty cards at a time
    out of a catalogue of a thousand and a half. slg_gui caches this at module
    level, the same way it does the tag dictionary.

    Only rows whose src_hash still names the game's current title are returned.
    The hash is stored on the row but is not part of its primary key - there is
    one row per game - so a title the site or the migration rewrote leaves its
    translation behind, and serving that quietly undoes the rewrite. This is
    exactly how a card went on reading '... - dikgames' with a cleaned database
    underneath: the source had been fixed and the cache had not.

    Hand-typed names are exempt and always win. They are corrections to the
    text rather than translations of it, and carry no source hash to match.
    """
    current = {row["id"]: src_hash(row["title"]) for row in
               conn.execute("SELECT id, title FROM games")}
    both = {}
    for row in conn.execute("SELECT ref, text, engine, src_hash FROM translations"
                            " WHERE kind='title' AND lang=?", (lang,)):
        if current.get(_int_or_none(row["ref"])) == row["src_hash"]:
            both[row["ref"]] = (row["text"], row["engine"])
    for row in conn.execute("SELECT ref, text FROM manual_translations"
                            " WHERE kind='title' AND lang=?", (lang,)):
        both[row["ref"]] = (row["text"], ENGINE_MANUAL)
    return both


def _int_or_none(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def all_tags(conn):
    """Every tag slug, alphabetical. Feeds the tag-name editor, which shows the
    whole vocabulary rather than only the tags on the game being looked at."""
    return [row["name"] for row in
            conn.execute("SELECT name FROM tags ORDER BY name")]


def missing_tags(conn, lang=TAG_LANG):
    """Tag slugs with no translation of either kind. Tags never change text, so
    there is nothing to re-translate - a row exists or it does not.

    Both tables count as a row. A hand-typed tag is not in `translations`, and
    reading only that one would put the hundred tags the user renamed back in
    the queue to be paid for again.
    """
    return [row["name"] for row in conn.execute(
        "SELECT t.name FROM tags t WHERE NOT EXISTS ("
        "  SELECT 1 FROM translations tr"
        "  WHERE tr.kind='tag' AND tr.lang=? AND tr.ref=t.name)"
        " AND NOT EXISTS ("
        "  SELECT 1 FROM manual_translations mt"
        "  WHERE mt.kind='tag' AND mt.lang=? AND mt.ref=t.name)"
        " ORDER BY t.name", (lang, lang))]


def translation_counts(conn, lang=TAG_LANG):
    """How much of the catalogue is in Chinese.

    UNION rather than a sum: a hand-typed row can sit on top of a machine one
    for the same ref, and counting both would report more translated tags than
    there are tags.
    """
    def translated(kind):
        return conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT ref FROM translations WHERE kind=? AND lang=?"
            "  UNION SELECT ref FROM manual_translations WHERE kind=? AND lang=?)",
            (kind, lang, kind, lang)).fetchone()[0]

    return {
        "tags": translated("tag"),
        "tag_total": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
        "overviews": translated("overview"),
    }


# --- user state ----------------------------------------------------------------

def set_state(conn, game_id, status=None, my_rating=None):
    """Patch the user's own fields. None means 'leave alone'; '' clears."""
    conn.execute("INSERT OR IGNORE INTO state (game_id) VALUES (?)", (game_id,))
    sets, params = [], []
    for column, value in (("status", status), ("my_rating", my_rating)):
        if value is not None:
            sets.append("%s = ?" % column)
            params.append(value or None)
    if sets:
        sets.append("updated_at = ?")
        params.extend([_now(), game_id])
        conn.execute("UPDATE state SET %s WHERE game_id = ?" % ", ".join(sets), params)
    conn.commit()


def _wishlist_write_scope(conn, savepoint):
    """A small transaction boundary that also works inside caller transactions."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT " + savepoint)
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


def _finish_wishlist_write(conn, savepoint, owns_transaction, error=False):
    if error:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            conn.execute("RELEASE SAVEPOINT " + savepoint)
    elif owns_transaction:
        conn.commit()
    else:
        conn.execute("RELEASE SAVEPOINT " + savepoint)


def wishlist_version_changes(conn):
    """Return newer site versions for wanted games that are not installed.

    The first observed version is only a baseline, not a notification. A missing
    version remains an empty baseline until the catalogue first provides one.
    Pending changes are derived from the saved seen version, so repeated calls
    keep returning them until mark_wishlist_version_seen() advances that value.
    """
    savepoint = "wishlist_version_changes"
    owns_transaction = _wishlist_write_scope(conn, savepoint)
    changes = []
    try:
        rows = conn.execute(
            "SELECT g.id, g.slug, g.title, g.version, g.last_updated,"
            "       v.game_id AS seen_game_id, v.seen_version"
            " FROM games g"
            " JOIN state s ON s.game_id = g.id AND s.status = 'want'"
            " LEFT JOIN local l ON l.game_id = g.id"
            " LEFT JOIN wishlist_version_seen v ON v.game_id = g.id"
            " WHERE l.game_id IS NULL"
            " ORDER BY g.title COLLATE NOCASE").fetchall()
        for row in rows:
            current = str(row["version"] or "").strip() or None
            seen = str(row["seen_version"] or "").strip() or None
            if row["seen_game_id"] is None:
                # First observation establishes the baseline silently.
                conn.execute(
                    "INSERT INTO wishlist_version_seen (game_id, seen_version)"
                    " VALUES (?, ?)", (row["id"], current))
                continue
            if seen is None:
                # No comparable version was available on the first pass. Treat
                # the first usable catalogue version as a baseline as well.
                if current is not None:
                    conn.execute(
                        "UPDATE wishlist_version_seen SET seen_version = ?"
                        " WHERE game_id = ?", (current, row["id"]))
                continue
            if (current is not None
                    and not re.search(r"\d+", seen)
                    and re.search(r"\d+", current)):
                # A previously stored label with no version digits cannot be
                # compared by _version_gt. Rebaseline silently on the first
                # comparable value instead of leaving this game stuck forever.
                conn.execute(
                    "UPDATE wishlist_version_seen SET seen_version = ?"
                    " WHERE game_id = ?", (current, row["id"]))
                continue
            if current is not None and _version_gt(current, seen):
                changes.append({
                    "id": row["id"],
                    "slug": row["slug"],
                    "title": row["title"],
                    "seen_version": seen,
                    "version": current,
                    "last_updated": row["last_updated"],
                })
        _finish_wishlist_write(conn, savepoint, owns_transaction)
    except Exception:
        _finish_wishlist_write(conn, savepoint, owns_transaction, error=True)
        raise
    return changes


def mark_wishlist_version_seen(conn, game_id, version=None):
    """Acknowledge a wanted game's version, defaulting to its current version.

    Passing the version shown to the user acknowledges only that snapshot; if
    the catalogue advanced again meanwhile, the newer version remains pending.
    The saved version is local and cascades away if the game row is deleted.
    """
    savepoint = "mark_wishlist_version_seen"
    owns_transaction = _wishlist_write_scope(conn, savepoint)
    try:
        game = conn.execute(
            "SELECT version FROM games WHERE id = ?", (game_id,)).fetchone()
        if game is None:
            _finish_wishlist_write(conn, savepoint, owns_transaction)
            return False
        target = version if version is not None else game["version"]
        target = str(target or "").strip() or None
        if target is None:
            # A missing current version must not erase a previously comparable
            # baseline. Insert a NULL row only when this game had no baseline.
            conn.execute(
                "INSERT OR IGNORE INTO wishlist_version_seen"
                " (game_id, seen_version) VALUES (?, NULL)", (game_id,))
        else:
            conn.execute(
                "INSERT INTO wishlist_version_seen (game_id, seen_version)"
                " VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET"
                " seen_version = excluded.seen_version", (game_id, target))
        _finish_wishlist_write(conn, savepoint, owns_transaction)
    except Exception:
        _finish_wishlist_write(conn, savepoint, owns_transaction, error=True)
        raise
    return True


# --- comments ------------------------------------------------------------------

def add_comment(conn, game_slug, content, nickname=None, cloud_id=None,
                visibility="private", created_at=None):
    """Insert a locally-authored comment, returning its local row id."""
    cur = conn.execute(
        "INSERT INTO comments (game_slug, content, nickname, cloud_id,"
        " visibility, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (game_slug, content, nickname, cloud_id, visibility,
         created_at or _now()))
    conn.commit()
    return cur.lastrowid


def mark_comment_uploaded(conn, comment_id, cloud_id):
    conn.execute("UPDATE comments SET cloud_id = ? WHERE id = ?",
                 (cloud_id, comment_id))
    conn.commit()


def list_comments(conn, game_slug):
    """The author's own local comments for a game, oldest first."""
    return [dict(row) for row in conn.execute(
        "SELECT id, content, nickname, cloud_id, visibility, created_at "
        "FROM comments WHERE game_slug = ? ORDER BY id", (game_slug,))]


def delete_comment(conn, comment_id):
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()


NOTES_MERGED_PREF = "comments.notes_merged"


def migrate_notes_to_comments(conn):
    """Fold the retired per-game 评价 note into the comment list, once.

    The panel used to carry two separate ways to write about a game - a private
    note in state.note and a comment list - and users read them as two competing
    comment boxes. The note earns a row in `comments` flagged private, and the
    state column is left alone so an older build can still read it.

    Guarded by a pref rather than by looking for matching rows: a user is free
    to delete the migrated comment, and it must not come back on the next start.
    connect() runs this from _migrate on every connection, so the write lock is
    taken up front - otherwise two connections opened at once on a fresh db both
    read the pref before either sets it, and the note lands twice.
    """
    if get_pref(conn, NOTES_MERGED_PREF):
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        if get_pref(conn, NOTES_MERGED_PREF):
            conn.execute("COMMIT")
            return 0
        # TRIM() only strips spaces by default, so a note that is nothing but
        # newlines would sail through as content. Name the characters.
        rows = conn.execute(
            "SELECT g.slug, s.note, s.updated_at FROM state s"
            " JOIN games g ON g.id = s.game_id"
            " WHERE s.note IS NOT NULL"
            " AND TRIM(s.note, ' ' || CHAR(9) || CHAR(10) || CHAR(13)) <> ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO comments (game_slug, content, nickname, cloud_id,"
                " visibility, created_at) VALUES (?, ?, NULL, NULL, ?, ?)",
                (row["slug"], row["note"].strip(), "private",
                 row["updated_at"]))
        conn.execute("INSERT OR REPLACE INTO prefs (key, value) VALUES (?, ?)",
                     (NOTES_MERGED_PREF, "1"))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


# --- collections ---------------------------------------------------------------

def list_collections(conn):
    """All collections, newest first, each with its member count."""
    return [dict(row) for row in conn.execute(
        "SELECT c.id, c.name, "
        "(SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id = c.id) AS count "
        "FROM collections c ORDER BY c.id")]


def create_collection(conn, name):
    """Create a collection, or return the id of the one that already has the name."""
    name = (name or "").strip()
    if not name:
        return None
    conn.execute("INSERT OR IGNORE INTO collections (name, created_at) VALUES (?, ?)",
                 (name, _now()))
    conn.commit()
    row = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def delete_collection(conn, collection_id):
    conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    conn.commit()


def collections_for_game(conn, game_id):
    """Ids of the collections `game_id` currently belongs to."""
    return [row["collection_id"] for row in conn.execute(
        "SELECT collection_id FROM collection_items WHERE game_id = ?", (game_id,))]


def set_game_collections(conn, game_id, collection_ids):
    """Replace the game's collection membership with exactly `collection_ids`."""
    conn.execute("DELETE FROM collection_items WHERE game_id = ?", (game_id,))
    for cid in collection_ids:
        conn.execute(
            "INSERT OR IGNORE INTO collection_items (collection_id, game_id, added_at)"
            " VALUES (?, ?, ?)", (cid, game_id, _now()))
    conn.commit()


def remove_from_collection(conn, game_id, collection_id):
    """Drop one game from one collection; leave the collection itself alone."""
    conn.execute(
        "DELETE FROM collection_items WHERE collection_id = ? AND game_id = ?",
        (collection_id, game_id))
    conn.commit()


# --- backup / restore ----------------------------------------------------------

# The user's own curation, in JSON: ratings/notes/status, tag exclusions,
# collections and their members, and hand-written translations. prefs (which
# holds the API key and the sync gate) and the machine-translation cache stay
# out - neither is something to carry into another machine's library.
_BACKUP_TABLES = (
    ("state", ("game_id", "status", "note", "my_rating", "updated_at")),
    ("exclusions", ("tag", "source")),
    ("collections", ("id", "name", "created_at")),
    ("collection_items", ("collection_id", "game_id", "added_at")),
    ("manual_translations", ("kind", "ref", "lang", "text", "updated_at")),
    ("points_log", ("delta", "reason", "at")),
    ("signin", ("day", "at")),
    ("owned_titles", ("title_id", "acquired_at", "source")),
)

# Every non-id column of `games`, in order, for the user-game backup round trip.
# The id is deliberately left out: it is a local rowid that means nothing on
# another machine, and the slug ('user-…') is the stable identity across them.
_USER_GAME_COLS = (
    "slug", "url", "title", "version", "developer", "engine", "rating",
    "last_updated", "overview", "cover_file", "complete", "first_seen",
    "last_synced", "lastmod", "fetch_failures", "site_views", "site_likes",
    "site_comments", "heat", "origin", "promoted",
)


def _game_id_by_slug(conn, slug):
    if not slug:
        return None
    row = conn.execute("SELECT id FROM games WHERE slug = ?", (slug,)).fetchone()
    return row["id"] if row else None


def _export_game_rows(conn, table):
    """Export a game-referencing table with each game's slug attached.

    game_id is a machine-local rowid that means nothing on another machine, so
    the restore re-keys through the stable slug instead; carrying it alongside
    every row is what makes that possible.
    """
    return [dict(r) for r in conn.execute(
        "SELECT t.*, g.slug AS game_slug FROM %s t"
        " JOIN games g ON g.id = t.game_id" % table)]


def export_user_data(conn):
    """All of the user's own data, as a JSON-serialisable dict."""
    out = {}
    for table, _cols in _BACKUP_TABLES:
        out[table] = [dict(r) for r in conn.execute("SELECT * FROM %s" % table)]
    out["state"] = _export_game_rows(conn, "state")
    out["collection_items"] = _export_game_rows(conn, "collection_items")
    out["user_games"] = [dict(r) for r in conn.execute(
        "SELECT * FROM games WHERE origin = 'user' ORDER BY id")]
    out["user_game_tags"] = [dict(r) for r in conn.execute(
        "SELECT g.slug, t.name FROM game_tags gt"
        " JOIN games g ON g.id = gt.game_id"
        " JOIN tags t ON t.id = gt.tag_id"
        " WHERE g.origin = 'user' ORDER BY g.slug, t.name")]
    out["user_local"] = [dict(r) for r in conn.execute(
        "SELECT g.slug, l.folder_path, l.folder_version, l.exe_path"
        " FROM local l JOIN games g ON g.id = l.game_id"
        " WHERE g.origin = 'user'")]
    out["user_game_aliases"] = [dict(r) for r in conn.execute(
        "SELECT g.slug, a.alias, a.source FROM game_aliases a"
        " JOIN games g ON g.id = a.game_id WHERE g.origin = 'user'")]
    return out


def import_user_data(conn, data):
    """Overwrite the user's own tables with the backup's contents.

    Children (collection_items) are cleared before their parents (collections),
    and parents are written back before their children, so the foreign keys
    never see a dangling reference.

    User games are merged rather than overwritten: each is keyed by its synthetic
    slug, and tags/aliases/local are re-linked to whatever id that slug lands on
    in this library.
    """
    def load(table, cols, records):
        if not records:
            return
        qmarks = ",".join("?" * len(cols))
        conn.executemany(
            "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
            % (table, ",".join(cols), qmarks),
            [tuple(r.get(c) for c in cols) for r in records])

    # Children cleared before parents. state and collection_items are held back:
    # their game_id is a rowid that must be re-keyed through the slug, not
    # bulk-loaded straight off the backup.
    for table in ("collection_items", "state", "exclusions",
                  "collections", "manual_translations",
                  "points_log", "signin", "owned_titles"):
        conn.execute("DELETE FROM %s" % table)
    for table, cols in _BACKUP_TABLES:
        if table in ("state", "collection_items"):
            continue
        load(table, cols, data.get(table, []))
    # User games first: the rows below may point at them, and the foreign key
    # is enforced on this connection.
    _import_user_games(conn, data)
    _import_game_rows(conn, "state", data.get("state", []))
    _import_game_rows(conn, "collection_items", data.get("collection_items", []))
    conn.commit()


def _import_game_rows(conn, table, records):
    """Re-key a game-referencing table's rows from slug to the local game id.

    Rows whose game is not in this library are dropped rather than forced
    against a dangling id, which would trip the foreign key.
    """
    if not records:
        return
    cols = dict(_BACKUP_TABLES)[table]
    id_map = {r["slug"]: r["id"]
              for r in conn.execute("SELECT id, slug FROM games")}
    qmarks = ",".join("?" * len(cols))
    rows = []
    for rec in records:
        gid = id_map.get(rec.get("game_slug"))
        if gid is None:
            continue
        rows.append(tuple(gid if c == "game_id" else rec.get(c) for c in cols))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
            % (table, ",".join(cols), qmarks), rows)


def _import_user_games(conn, data):
    for rec in data.get("user_games", []):
        slug = rec.get("slug")
        if not slug:
            continue
        values = tuple(rec.get(c) for c in _USER_GAME_COLS)
        row = conn.execute("SELECT id FROM games WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            qmarks = ",".join("?" * len(_USER_GAME_COLS))
            conn.execute(
                "INSERT INTO games (%s) VALUES (%s)"
                % (",".join(_USER_GAME_COLS), qmarks), values)
        else:
            sets = ",".join("%s = ?" % c for c in _USER_GAME_COLS)
            conn.execute("UPDATE games SET %s WHERE id = ?" % sets,
                         values + (row["id"],))

    for rec in data.get("user_game_tags", []):
        game_id = _game_id_by_slug(conn, rec.get("slug"))
        name = rec.get("name")
        if game_id is None or not name:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?",
                              (name,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO game_tags (game_id, tag_id)"
                     " VALUES (?,?)", (game_id, tag_id))

    for rec in data.get("user_local", []):
        game_id = _game_id_by_slug(conn, rec.get("slug"))
        if game_id is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO local (game_id, folder_path, folder_version,"
            " exe_path)"
            " VALUES (?,?,?,?)",
            (game_id, rec.get("folder_path"), rec.get("folder_version"),
             rec.get("exe_path")))

    for rec in data.get("user_game_aliases", []):
        game_id = _game_id_by_slug(conn, rec.get("slug"))
        alias = rec.get("alias")
        if game_id is None or not alias:
            continue
        conn.execute("INSERT OR IGNORE INTO game_aliases (game_id, alias, source)"
                     " VALUES (?,?,?)",
                     (game_id, alias, rec.get("source") or "user"))


# Tables that hold one user's own data and must never ride along in a public
# snapshot. A client needs only games, tags and game_tags; everything else is
# ratings, notes, collections, comments, prefs and translation caches that
# belong to whoever built the library.
_PRIVATE_TABLES = (
    "state", "collections", "collection_items", "comments", "prefs",
    "exclusions", "local", "game_aliases", "weights", "affinities",
    "sync_log", "translations", "manual_translations",
    "points_log", "signin", "owned_titles",
)


def strip_to_site_catalogue(db_path):
    """Reduce a db file to the public site catalogue, in place.

    Deletes every game the author added themselves (origin != 'site'), drops
    the personal-data tables, and reaps the tags left dangling by the game
    deletion. A snapshot built from an origin-less db - one that predates the
    origin column - keeps its games: such a db cannot hold user games, so the
    rows it does hold are all site rows already.
    """
    conn = sqlite3.connect(db_path)
    try:
        for table in _PRIVATE_TABLES:
            conn.execute("DROP TABLE IF EXISTS %s" % table)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
        if "origin" in cols:
            conn.execute("DELETE FROM games WHERE origin IS NULL OR origin != 'site'")
        conn.execute("DELETE FROM game_tags WHERE game_id NOT IN (SELECT id FROM games)")
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM game_tags)")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


# --- preference weights --------------------------------------------------------

def recompute_weights(conn):
    """Derive per-tag weights and per-developer/engine affinities from ratings.

    Each rated game contributes (rating - 3) to every tag it carries, and to its
    developer and engine, so the scale stays symmetrical around 'no opinion'.
    The result is an average with a shrinkage term, not a sum, so a tag (or
    studio) seen twice cannot outrank one seen two hundred times.

    Tag weights are then scaled by inverse document frequency: a tag that turns
    up on nearly every game (3dcg, big-tits) is weak evidence of taste, while a
    rare one (ntr, monster) is strong. The IDF lives in `weight` itself so the
    sort query stays a plain SUM with no correlated COUNT behind it.
    """
    conn.execute("DELETE FROM weights")
    conn.execute("DELETE FROM affinities")
    total_games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] or 1
    doc_freq = {r["tag_id"]: r["n"] for r in conn.execute(
        "SELECT tag_id, COUNT(*) AS n FROM game_tags GROUP BY tag_id").fetchall()}
    rows = conn.execute("""
        SELECT gt.tag_id, SUM(s.my_rating - 3.0) AS total, COUNT(*) AS n
        FROM state s
        JOIN game_tags gt ON gt.game_id = s.game_id
        WHERE s.my_rating IS NOT NULL
        GROUP BY gt.tag_id
    """).fetchall()
    conn.executemany(
        "INSERT INTO weights (tag_id, weight, sample_count) VALUES (?,?,?)",
        [(r["tag_id"],
          (r["total"] / (r["n"] + SHRINK))
          * (1.0 + math.log(total_games / float(doc_freq.get(r["tag_id"], 1)))),
          r["n"]) for r in rows])
    _recompute_affinity(conn, "developer")
    _recompute_affinity(conn, "engine")
    conn.commit()


def _recompute_affinity(conn, kind):
    """Fill `affinities` for one column (developer or engine) of `games`.

    `kind` is also the column name, so the caller never passes user input here -
    the string is interpolated into the SQL as a trusted literal.
    """
    rows = conn.execute(
        "SELECT %s AS name, SUM(s.my_rating - 3.0) AS total, COUNT(*) AS n"
        " FROM state s JOIN games g ON g.id = s.game_id"
        " WHERE s.my_rating IS NOT NULL AND %s IS NOT NULL AND %s != ''"
        " GROUP BY %s" % (kind, kind, kind, kind)).fetchall()
    conn.executemany(
        "INSERT INTO affinities (kind, name, weight, sample_count) VALUES (?,?,?,?)",
        [(kind, r["name"], r["total"] / (r["n"] + SHRINK), r["n"]) for r in rows])


def weight_table(conn, limit=40):
    return conn.execute("""
        SELECT t.name, w.weight, w.sample_count
        FROM weights w JOIN tags t ON t.id = w.tag_id
        ORDER BY ABS(w.weight) DESC, t.name LIMIT ?
    """, (limit,)).fetchall()


# --- prefs ---------------------------------------------------------------------

def get_pref(conn, key, default=None):
    row = conn.execute("SELECT value FROM prefs WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_pref(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO prefs (key, value) VALUES (?,?)", (key, value))
    conn.commit()


# --- 账本完整性：门槛，不是保险箱 ---------------------------------------------
#
# 先把话说在前面：**本地存档 100% 防不住。** 数据就在用户自己的
# %LOCALAPPDATA%\slgking\slgking.db 里，任何 SQLite 工具都能直接 INSERT 一行；
# 仓库是公开的，所以任何加密方案也就跟着公开了。这里能做的只有两件事：
#
#   1. 提高门槛 —— 账本每行带一个 HMAC 封印，密钥存在库外（integrity.key）。
#      不会算封印就伪造不出一行；用 DB 浏览器直接改数字、塞一行积分，也就是
#      99% 的「改档」，会留下断链。
#   2. 让篡改可见 —— 每次读余额/头衔时校验一次，断链之后的记录不再计入，
#      个人中心直接写「存档被外部修改」。
#
# 挡不住的：读源码把密钥和链一起复刻；或者把两张表清空重来（链从头开始，跟新档
# 长得一样）。真正的解法是服务器记账，那需要账号体系，与「轻量优先」冲突，
# 留给后面版本 —— 这里只是门槛。

INTEGRITY_KEY_FILE = "integrity.key"

# 表 -> 参与封印的列。**列序就是封印内容的一部分**：改动这里等于让所有老档断链，
# 所以只在确实要换格式时动它。
_LEDGER_TABLES = {
    "points_log": ("delta", "reason", "at"),
    "owned_titles": ("title_id", "acquired_at", "source"),
}

# 校验结果按库文件缓存。写账本的地方调 invalidate_ledger()，否则余额会用着
# 上一次的断链位置。单用户单连接，所以一个进程一份就够。
_LEDGER_CACHE = {}


def integrity_key_path():
    """密钥文件的位置。库外，跟存档同目录。"""
    return os.path.join(app_dir(), INTEGRITY_KEY_FILE)


def integrity_key(create=True):
    """库外的 HMAC 密钥；没有就生成一个。写不进去就返回 None（封印功能关闭）。

    放库外是这台机器上唯一还有点用的做法：库和密钥一起被改就无效了。跟着
    %LOCALAPPDATA%\\slgking 整个目录走，所以按帮助文档那样整个目录拷到新机器，
    链依然能验。
    """
    path = integrity_key_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        if not create:
            return None
    key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError:
        return None
    return key


def _row_seal(key, prev, rowid, values):
    """一行账目的封印：HMAC(密钥, 上一行封印 + 行号 + 行内容)。

    带上「上一行封印」是为了能发现**删除**：中间少一行，后面那行的 prev 就对不上。
    """
    payload = "\x1f".join([prev or "", str(rowid)] + [str(v) for v in values])
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _last_seal(conn, table, before_rowid):
    """这一行之前最后一枚**有封印**的行的封印（老档的历史行跳过不算）。"""
    row = conn.execute(
        "SELECT seal FROM %s WHERE rowid < ? AND seal IS NOT NULL"
        " ORDER BY rowid DESC LIMIT 1" % table, (before_rowid,)).fetchone()
    return row["seal"] if row else None


def _insert_sealed(conn, table, values, ignore=False):
    """往账本里写一行并盖封印。调用方负责 commit。返回行号，没插进去返回 None。

    INSERT OR IGNORE 撞到已有行时 rowcount 是 0，这时**不能**去盖封印 ——
    lastrowid 还是上一次插入的值，会把上一行的封印改写成这一行的，平白断链。
    """
    cols = _LEDGER_TABLES[table]
    cur = conn.execute(
        "INSERT %s INTO %s (%s) VALUES (%s)"
        % ("OR IGNORE" if ignore else "", table, ",".join(cols),
           ",".join("?" * len(cols))), tuple(values))
    rowid = cur.lastrowid
    if not cur.rowcount or rowid is None:
        return None
    key = integrity_key()
    if key:
        conn.execute(
            "UPDATE %s SET seal = ? WHERE rowid = ?" % table,
            (_row_seal(key, _last_seal(conn, table, rowid), rowid, values), rowid))
    invalidate_ledger(conn)
    return rowid


def verify_ledger(conn):
    """校验封印链。返回 {"sealed": bool, "breaks": {表: 第一处断链的行号}}。

    "sealed" 表示这份存档里有封印行。"breaks" 为空且 sealed 为真 = 账本干净。

    链开始之前的 NULL 封印一律信任：那是老版本写下的历史行，当时还没有这套东西
    （升级上来的存档不该因为一次升级就被判成「被改过」）。但链已经开始的**之后**
    再出现 NULL 封印，就是有人绕过程序塞了一行进来 —— 那正是不装封印的改档手法，
    必须算断链。

    没有密钥时不校验：能看出问题前提是能算出封印，算不出来就不该冤枉用户
    （常见于只拷了 slgking.db 没拷 integrity.key）。
    """
    key = integrity_key(create=False)
    breaks = {}
    sealed = False
    for table, cols in _LEDGER_TABLES.items():
        rows = conn.execute("SELECT rowid AS rid, seal, %s FROM %s ORDER BY rowid"
                            % (",".join(cols), table)).fetchall()
        if any(row["seal"] for row in rows):
            sealed = True
        if not key:
            continue
        prev = None
        started = False
        for row in rows:
            seal = row["seal"]
            if seal is None:
                if started:
                    breaks.setdefault(table, row["rid"])
                continue
            started = True
            if seal != _row_seal(key, prev, row["rid"], [row[c] for c in cols]):
                # 断链之后不再往下推 prev：后面每一行都会跟着断，只报第一处就够。
                breaks.setdefault(table, row["rid"])
                started = False
                prev = None
                continue
            prev = seal
    return {"sealed": sealed, "breaks": breaks}


def _conn_path(conn):
    """这个连接指向哪个库文件 —— 缓存键。内存库返回空串。"""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            return row["file"]
    return ""


def ledger_state(conn):
    """校验结果（按库缓存）。

    内存库不缓存：所有 `:memory:` 连接的路径都是空串，共用一个键会让一个测试的
    断链结论漏到下一个测试里（sqlite3.Connection 既不给挂属性也不能弱引用，没有
    更细的键可用）。真实存档走文件路径，照常缓存。
    """
    path = _conn_path(conn)
    if not path:
        return verify_ledger(conn)
    state = _LEDGER_CACHE.get(path)
    if state is None:
        state = _LEDGER_CACHE[path] = verify_ledger(conn)
    return state


def invalidate_ledger(conn):
    _LEDGER_CACHE.pop(_conn_path(conn), None)


def _trusted(conn, table):
    """SQL 条件：只算到第一处断链为止的行。"""
    brk = ledger_state(conn)["breaks"].get(table)
    return "rowid < %d" % brk if brk is not None else "1"


def tamper_report(conn):
    """被断链排除掉的东西；账本没被人动过就返回 None。

    给个人中心用：返回 {"points": 被排掉的积分合计, "titles": [被排掉的头衔 id]}。
    """
    state = ledger_state(conn)
    if not state["breaks"]:
        return None
    points = 0
    brk = state["breaks"].get("points_log")
    if brk is not None:
        points = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM points_log"
                              " WHERE rowid >= ?", (brk,)).fetchone()[0]
    titles = []
    brk = state["breaks"].get("owned_titles")
    if brk is not None:
        titles = [row["title_id"] for row in conn.execute(
            "SELECT title_id FROM owned_titles WHERE rowid >= ?", (brk,))]
    return {"points": points, "titles": titles}


# --- points / sign-in / titles ------------------------------------------------
#
# Persistence primitives only; the catalogue (titles, costs, redemption codes)
# and the business rules live in slg_titles, which imports this module. Keeping
# the values as explicit parameters means slg_db stays decoupled from that
# catalogue, and the server scripts that import slg_db keep working untouched.

def points_balance(conn):
    return conn.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM points_log WHERE %s"
        % _trusted(conn, "points_log")).fetchone()[0]


def add_points(conn, delta, reason):
    _insert_sealed(conn, "points_log", (delta, reason, _now()))
    conn.commit()


def record_signin(conn, day, points):
    """Record a sign-in for `day`, awarding `points`. Returns (already, day, gained)."""
    row = conn.execute("SELECT 1 FROM signin WHERE day = ?", (day,)).fetchone()
    if row:
        return (True, day, 0)
    conn.execute("INSERT INTO signin (day, at) VALUES (?,?)", (day, _now()))
    add_points(conn, points, "签到")
    return (False, day, points)


def makeup_signin(conn, day):
    """Insert a sign-in row for a past `day` without awarding daily points."""
    conn.execute("INSERT OR IGNORE INTO signin (day, at) VALUES (?,?)",
                 (day, _now()))
    conn.commit()


def last_signin_day(conn):
    return conn.execute("SELECT MAX(day) FROM signin").fetchone()[0]


def owned_title_ids(conn):
    return {r["title_id"] for r in conn.execute(
        "SELECT title_id FROM owned_titles WHERE %s"
        % _trusted(conn, "owned_titles"))}


def own_title(conn, title_id, source):
    _insert_sealed(conn, "owned_titles", (title_id, _now(), source), ignore=True)
    conn.commit()


def redeem_title(conn, title_id):
    """Own a title via a redemption code. Returns True if it was newly owned."""
    if title_id in owned_title_ids(conn):
        return False
    own_title(conn, title_id, "code")
    return True


def buy_title(conn, title_id, cost):
    """Deduct `cost` points and own the title, in one transaction. Returns bool."""
    if points_balance(conn) < cost:
        return False
    _insert_sealed(conn, "points_log", (-cost, "兑换:" + title_id, _now()))
    _insert_sealed(conn, "owned_titles", (title_id, _now(), "shop"), ignore=True)
    conn.commit()
    return True


def set_equipped_title(conn, title_id):
    set_pref(conn, "profile.equipped_title", title_id)


def get_equipped_title(conn):
    return get_pref(conn, "profile.equipped_title", "") or ""


def log_sync(conn, tag, pages, games_seen, new_games):
    conn.execute("INSERT INTO sync_log (ran_at, tag, pages, games_seen, new_games)"
                 " VALUES (?,?,?,?,?)",
                 (_now(), tag, pages, games_seen, new_games))
    conn.commit()


def stats(conn):
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "games": one("SELECT COUNT(*) FROM games"),
        "tags": one("SELECT COUNT(*) FROM tags"),
        "downloaded": one("SELECT COUNT(*) FROM local"),
        "rated": one("SELECT COUNT(*) FROM state WHERE my_rating IS NOT NULL"),
        "last_sync": one("SELECT MAX(ran_at) FROM sync_log"),
    }


# --- profile / usage counters --------------------------------------------------
#
# Read-only aggregates for the 个人中心 page. Each is one cheap COUNT over a
# table that is already append-only; nothing here writes, so the profile panel
# can call them freely on every open.

def collection_count(conn):
    """Games the user has put into any collection."""
    return conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0]


def rating_count(conn):
    """Games the user has personally rated."""
    return conn.execute(
        "SELECT COUNT(*) FROM state WHERE my_rating IS NOT NULL").fetchone()[0]


def local_count(conn):
    """Games the user has added from a local folder."""
    return conn.execute("SELECT COUNT(*) FROM local").fetchone()[0]


def signin_days(conn):
    """Sign-in totals: total days and the current consecutive streak.

    The streak counts back from today (or yesterday, so signing in before the
    next midnight still reads as a run) while the days stay consecutive.
    """
    days = [r["day"] for r in conn.execute(
        "SELECT day FROM signin ORDER BY day DESC")]
    total = len(days)
    streak = 0
    if days:
        cursor = date.today()
        if days[0] != cursor.isoformat():
            cursor -= timedelta(days=1)
        for d in days:
            if d == cursor.isoformat():
                streak += 1
                cursor -= timedelta(days=1)
            else:
                break
    return {"total": total, "streak": streak}


def lottery_count(conn):
    """How many times the user has drawn the daily lottery."""
    return conn.execute(
        "SELECT COUNT(*) FROM points_log WHERE reason = '每日抽奖' AND %s"
        % _trusted(conn, "points_log")).fetchone()[0]


def points_flow(conn):
    """Points earned (delta > 0) and spent (delta < 0) across the whole ledger."""
    trusted = _trusted(conn, "points_log")
    earned = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM points_log"
        " WHERE delta > 0 AND %s" % trusted).fetchone()[0]
    spent = conn.execute(
        "SELECT COALESCE(SUM(-delta), 0) FROM points_log"
        " WHERE delta < 0 AND %s" % trusted).fetchone()[0]
    return {"earned": earned, "spent": spent}


def signin_month_days(conn, year, month):
    """Day-of-month numbers signed in during the given year/month."""
    prefix = "%04d-%02d" % (year, month)
    rows = conn.execute(
        "SELECT day FROM signin WHERE day LIKE ?", (prefix + "-%",)).fetchall()
    return {int(r["day"][-2:]) for r in rows}
