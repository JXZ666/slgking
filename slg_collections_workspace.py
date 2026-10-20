"""Collection operations over existing personal tables; never delete games."""
import sqlite3
from datetime import datetime, timezone

import slg_hidden_games


def normalize_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("请输入收藏夹名称。")
    return name.strip()


def _unique_name(conn, name, excluding=None):
    name = normalize_name(name)
    if any(row["id"] != excluding and row["name"].strip().casefold() == name.casefold()
           for row in conn.execute("SELECT id,name FROM collections")):
        raise ValueError("已存在同名收藏夹。")
    return name


def create_collection(conn, name):
    name = _unique_name(conn, name)
    with conn:
        cursor = conn.execute("INSERT INTO collections(name,created_at) VALUES (?,?)",
                              (name, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return cursor.lastrowid


def rename_collection(conn, collection_id, name):
    name = _unique_name(conn, name, collection_id)
    with conn:
        if not conn.execute("UPDATE collections SET name=? WHERE id=?", (name, collection_id)).rowcount:
            raise ValueError("收藏夹已不存在。")


def delete_collection(conn, collection_id):
    # Explicit child cleanup also works with legacy connections lacking FK pragma.
    with conn:
        conn.execute("DELETE FROM collection_items WHERE collection_id=?", (collection_id,))
        conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))


def remove_member(conn, collection_id, game_id):
    with conn:
        conn.execute("DELETE FROM collection_items WHERE collection_id=? AND game_id=?",
                     (collection_id, game_id))


def overview(conn):
    visible = slg_hidden_games.visibility_sql(conn)
    cursor = conn.execute("SELECT c.id,c.name,COUNT(ci.game_id) AS total_count,"
                          "SUM(CASE WHEN g.id IS NOT NULL AND " + visible +
                          " AND NOT EXISTS(SELECT 1 FROM catalog_redirects cr"
                          " WHERE cr.old_slug=g.slug AND cr.active=1) THEN 1 ELSE 0 END) AS visible_count"
                          " FROM collections c LEFT JOIN collection_items ci ON ci.collection_id=c.id"
                          " LEFT JOIN games g ON g.id=ci.game_id GROUP BY c.id ORDER BY c.id")
    return [dict(id=row[0], name=row[1], total_count=row[2], visible_count=row[3] or 0) for row in cursor]


def read_snapshot(db_path, spec, page=0, page_size=8):
    """One read transaction; no profile migration or per-card queries."""
    from pathlib import Path
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        collections = overview(conn)
        if spec.get("collection_id") is None:
            return collections, None
        from slg_catalogue_query import query_catalogue_page
        return collections, query_catalogue_page(conn, dict(spec, origin=None), page, page_size)
    finally:
        conn.close()
