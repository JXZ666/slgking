"""Local, identity-scoped hidden cards; never delete personal or disk data.

Read helpers also work on older/read-only catalogues. Writable connections
must call ensure_schema; transaction composition uses commit=False.
"""
from datetime import datetime, timezone
import re
import uuid

BACKUP_FIELDS = {"hidden_games": {"slug", "hidden_at"}}


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS hidden_games ("
                 "game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,"
                 "hidden_at TEXT NOT NULL)")


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def hidden_ids(conn):
    if not _has_table(conn, "hidden_games"):
        return set()
    return {row[0] for row in conn.execute("SELECT game_id FROM hidden_games")}


def is_hidden(conn, game_id):
    return (_has_table(conn, "hidden_games") and conn.execute(
        "SELECT 1 FROM hidden_games WHERE game_id=?", (game_id,)).fetchone() is not None)


def visibility_sql(conn, alias="g", hidden=False):
    """Trusted SQL predicate for visible lists, or hidden-only management.

    Alias must be a plain identifier; all user search text stays in parameters.
    Missing tables produce a constant predicate without mutating old databases.
    """
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("游戏表别名无效。")
    if not _has_table(conn, "hidden_games"):
        return "0" if hidden else "1"
    predicate = "EXISTS (SELECT 1 FROM hidden_games hg WHERE hg.game_id=%s.id)" % alias
    return predicate if hidden else "NOT " + predicate


def set_hidden(conn, game_id, hidden, commit=True):
    """Return whether the flag changed; repeated hiding preserves its timestamp."""
    if type(hidden) is not bool or type(game_id) is not int or game_id <= 0:
        raise ValueError("隐藏游戏参数无效。")
    if conn.execute("SELECT 1 FROM games WHERE id=?", (game_id,)).fetchone() is None:
        raise ValueError("游戏不存在。")
    ensure_schema(conn)
    if hidden:
        changed = conn.execute(
            "INSERT OR IGNORE INTO hidden_games(game_id,hidden_at) VALUES (?,?)",
            (game_id, datetime.now(timezone.utc).isoformat(timespec="seconds"))).rowcount
    else:
        changed = conn.execute("DELETE FROM hidden_games WHERE game_id=?", (game_id,)).rowcount
    if commit:
        conn.commit()
    return bool(changed)


def restore_all(conn, commit=True):
    """Clear only visibility flags and return the number restored."""
    count = 0
    if _has_table(conn, "hidden_games"):
        count = conn.execute("DELETE FROM hidden_games").rowcount
    if commit:
        conn.commit()
    return count


def query_hidden(conn, search=""):
    """Management rows including every hidden card, without normal-list filters."""
    if not isinstance(search, str):
        raise ValueError("搜索内容无效。")
    if not _has_table(conn, "hidden_games"):
        return []
    sql = "SELECT g.*,hg.hidden_at FROM games g JOIN hidden_games hg ON hg.game_id=g.id"
    params = ()
    if search.strip():
        # Literal substring search: % and _ typed by a user are not wildcards.
        text = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " WHERE (g.title LIKE ? ESCAPE '\\' OR g.slug LIKE ? ESCAPE '\\')"
        params = ("%" + text + "%",) * 2
    cursor = conn.execute(sql + " ORDER BY hg.hidden_at DESC,g.id", params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _game_id_by_slug(conn, slug):
    """Follow the same active local catalogue identity redirects as backups."""
    current = slug
    seen = set()
    if _has_table(conn, "catalog_redirects"):
        while current not in seen:
            seen.add(current)
            row = conn.execute("SELECT canonical_slug FROM catalog_redirects"
                               " WHERE old_slug=? AND active=1", (current,)).fetchone()
            if not row or not row[0]:
                break
            current = row[0]
        else:
            raise ValueError("游戏目录重定向存在循环。")
    row = conn.execute("SELECT id FROM games WHERE slug=?", (current,)).fetchone()
    return row[0] if row else None


def _valid_timestamp(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    try:
        # Accept historical local ISO times as well as UTC-aware new records.
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def export_sections(conn):
    if not _has_table(conn, "hidden_games"):
        return {"hidden_games": []}
    return {"hidden_games": [{"slug": row[0], "hidden_at": row[1]} for row in conn.execute(
        "SELECT g.slug,hg.hidden_at FROM hidden_games hg"
        " JOIN games g ON g.id=hg.game_id ORDER BY g.slug")]}


def validate_sections(data, conn=None):
    """Validate completely before restore writes; absent old sections are kept."""
    if not isinstance(data, dict):
        raise ValueError("备份内容必须是 JSON 对象。")
    if "hidden_games" not in data:
        return {}
    rows = data["hidden_games"]
    if not isinstance(rows, list):
        raise ValueError("已隐藏游戏备份节必须是列表。")
    user_games = data.get("user_games", [])
    if not isinstance(user_games, list):
        raise ValueError("自建游戏备份节必须是列表。")
    pending = {row.get("slug") for row in user_games
               if isinstance(row, dict) and isinstance(row.get("slug"), str)}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != BACKUP_FIELDS["hidden_games"]:
            raise ValueError("已隐藏游戏备份字段无效。")
        slug = row["slug"]
        if not isinstance(slug, str) or not slug.strip() or "\x00" in slug or slug in seen:
            raise ValueError("已隐藏游戏备份标识无效或重复。")
        if not _valid_timestamp(row["hidden_at"]):
            raise ValueError("已隐藏游戏备份时间无效。")
        seen.add(slug)
        if conn is not None and slug not in pending and _game_id_by_slug(conn, slug) is None:
            raise ValueError("请先导入已隐藏游戏备份需要的游戏目录。")
    return {"hidden_games": len(rows)}


def import_sections(conn, data):
    """Replace a present section atomically, keyed by current canonical slug.

    The caller owns the transaction. User games must already be imported.
    Different backup slugs redirecting to one game coalesce into one flag.
    """
    validate_sections(data, conn)
    if "hidden_games" not in data:
        return []
    resolved = {}
    for row in data["hidden_games"]:
        game_id = _game_id_by_slug(conn, row["slug"])
        if game_id is None:
            raise ValueError("游戏目录不存在。")
        # Choose one portable timestamp deterministically when identities merge.
        resolved[game_id] = min(resolved.get(game_id, row["hidden_at"]), row["hidden_at"])
    savepoint = "hidden_restore_" + uuid.uuid4().hex
    conn.execute("SAVEPOINT " + savepoint)
    try:
        ensure_schema(conn)
        conn.execute("DELETE FROM hidden_games")
        conn.executemany("INSERT INTO hidden_games(game_id,hidden_at) VALUES (?,?)", resolved.items())
        conn.execute("RELEASE SAVEPOINT " + savepoint)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        raise
    return []


def merge_redirect(conn, old_id, canonical_id):
    """Union visibility flags without a conflict; caller owns the transaction."""
    if old_id == canonical_id or not _has_table(conn, "hidden_games"):
        return
    if conn.execute("SELECT 1 FROM games WHERE id=?", (canonical_id,)).fetchone() is None:
        raise ValueError("重定向目标游戏不存在。")
    conn.execute("INSERT INTO hidden_games(game_id,hidden_at)"
                 " SELECT ?,hidden_at FROM hidden_games WHERE game_id=?"
                 " ON CONFLICT(game_id) DO UPDATE SET hidden_at=MIN(hidden_games.hidden_at,excluded.hidden_at)",
                 (canonical_id, old_id))
    conn.execute("DELETE FROM hidden_games WHERE game_id=?", (old_id,))
