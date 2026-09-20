"""SQLite storage.

Everything the user does lands here on the spot - the ratings, the status
flags, the notes, the exclusions. Reopening the window must not lose any of
it, which is the one requirement that got its own question in the interview.
"""

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

STATUSES = ("want", "downloaded", "playing")
STATUS_LABELS = {"want": "想玩", "downloaded": "已下载", "playing": "正在玩"}

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
    heat          REAL
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

CREATE TABLE IF NOT EXISTS local (
    game_id         INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    folder_path     TEXT,
    folder_version  TEXT,
    has_translation INTEGER NOT NULL DEFAULT 0,
    has_fontpatch   INTEGER NOT NULL DEFAULT 0,
    size_bytes      INTEGER,
    scanned_at      TEXT
);

CREATE TABLE IF NOT EXISTS state (
    game_id   INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    status    TEXT,
    note      TEXT,
    my_rating INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS weights (
    tag_id       INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
    weight       REAL    NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 0
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


def set_tags(conn, game_id, tags):
    """Replace a game's tags. An empty list means "leave them alone".

    The listing and detail parsers can both come back with zero tag links -
    a page whose markup moved, not a game that genuinely has no tags - and the
    old unconditional DELETE wiped a healthy row in that case. Reading the ids
    back in one IN query also drops the old one-SELECT-per-tag round trip.
    """
    if not tags:
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
               downloaded_only=False, sort="score", desc=True):
    """Games matching a tag intersection (include) minus a tag union (exclude).

    include is an AND: every tag listed must be present. exclude is a NOT:
    any one of them is enough to drop the row.
    """
    where, params = [], []

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

    sql = """
        SELECT g.*, s.status, s.note, s.my_rating, l.folder_path, l.folder_version,
               l.has_translation, l.has_fontpatch,
               COALESCE(g.rating, 0)
             + 2.0 * COALESCE((SELECT AVG(w.weight) FROM game_tags gt
                               JOIN weights w ON w.tag_id = gt.tag_id
                               WHERE gt.game_id = g.id), 0) AS score
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
        rows = conn.execute("SELECT cover_file FROM games WHERE cover_file"
                            " IS NOT NULL AND cover_file NOT LIKE 'pending:%'"
                            ).fetchall()
        directory = covers_dir()
        count = sum(1 for row in rows
                    if not os.path.exists(os.path.join(directory, row["cover_file"])))
        _missing_covers = (count, now)
        return count


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
        "covers": one("SELECT COUNT(*) FROM games WHERE cover_file LIKE 'pending:%'")
        + missing_cover_files(conn),
        "rating": one("SELECT COUNT(*) FROM games WHERE rating IS NULL"),
        "overview": one("SELECT COUNT(*) FROM games WHERE overview IS NULL"
                        " OR TRIM(overview) = ''"),
    }


def newest_last_updated(conn):
    """The newest last_updated in the library, as 'YYYY-MM-DD'.

    The natural cutoff for the sync gate: every game the user already has
    predates it, so gating there takes nothing away that was there before.
    Empty when the library has no dated rows yet.
    """
    row = conn.execute("SELECT MAX(last_updated) AS d FROM games").fetchone()
    text = (row["d"] or "").strip()
    return text[:10] if len(text) >= 10 else ""


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

def set_state(conn, game_id, status=None, note=None, my_rating=None):
    """Patch the user's own fields. None means 'leave alone'; '' clears."""
    conn.execute("INSERT OR IGNORE INTO state (game_id) VALUES (?)", (game_id,))
    sets, params = [], []
    for column, value in (("status", status), ("note", note), ("my_rating", my_rating)):
        if value is not None:
            sets.append("%s = ?" % column)
            params.append(value or None)
    if sets:
        sets.append("updated_at = ?")
        params.extend([_now(), game_id])
        conn.execute("UPDATE state SET %s WHERE game_id = ?" % ", ".join(sets), params)
    conn.commit()


def get_state(conn, game_id):
    return conn.execute("SELECT * FROM state WHERE game_id = ?", (game_id,)).fetchone()


def rates_histogram(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM state WHERE my_rating IS NOT NULL"
                        ).fetchone()["n"]


# --- preference weights --------------------------------------------------------

def recompute_weights(conn):
    """Derive per-tag weights from the star ratings.

    Each rated game contributes (rating - 3) to every tag it carries, so the
    scale stays symmetrical around 'no opinion'. The result is an average with
    a shrinkage term, not a sum, so a tag seen twice cannot outrank one seen
    two hundred times.
    """
    conn.execute("DELETE FROM weights")
    rows = conn.execute("""
        SELECT gt.tag_id, SUM(s.my_rating - 3.0) AS total, COUNT(*) AS n
        FROM state s
        JOIN game_tags gt ON gt.game_id = s.game_id
        WHERE s.my_rating IS NOT NULL
        GROUP BY gt.tag_id
    """).fetchall()
    conn.executemany(
        "INSERT INTO weights (tag_id, weight, sample_count) VALUES (?,?,?)",
        [(r["tag_id"], r["total"] / (r["n"] + SHRINK), r["n"]) for r in rows])
    conn.commit()


def weight_table(conn, limit=40):
    return conn.execute("""
        SELECT t.name, w.weight, w.sample_count
        FROM weights w JOIN tags t ON t.id = w.tag_id
        ORDER BY ABS(w.weight) DESC, t.name LIMIT ?
    """, (limit,)).fetchall()


# --- exclusions ----------------------------------------------------------------

def exclusions(conn):
    return [r["tag"] for r in conn.execute("SELECT tag FROM exclusions ORDER BY tag")]


def add_exclusion(conn, tag, source="user"):
    conn.execute("INSERT OR REPLACE INTO exclusions (tag, source) VALUES (?,?)",
                 (tag, source))
    conn.commit()


def remove_exclusion(conn, tag):
    conn.execute("DELETE FROM exclusions WHERE tag = ?", (tag,))
    conn.commit()


# --- prefs ---------------------------------------------------------------------

def get_pref(conn, key, default=None):
    row = conn.execute("SELECT value FROM prefs WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_pref(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO prefs (key, value) VALUES (?,?)", (key, value))
    conn.commit()


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
