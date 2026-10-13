"""Personal, confirmed provider metadata. Never changes public catalogue facts."""
import json
import re
from urllib.parse import urlsplit

BACKUP_FIELDS = {"local_metadata": {"slug", "source", "external_id", "metadata_json"}}
FIELDS = ("title", "original_title", "developer", "release_date", "source_url", "cover_url")


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS personal_game_metadata ("
                 "source TEXT NOT NULL,external_id TEXT NOT NULL,"
                 "game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,"
                 "metadata_json TEXT NOT NULL,PRIMARY KEY(source,external_id))")


def validate(candidate):
    source, external = candidate.get("source"), candidate.get("external_id")
    pattern = r"(?:RJ|VJ)[0-9]{6,10}" if source == "dlsite" else r"v[0-9]+" if source == "vndb" else None
    if not pattern or not isinstance(external, str) or not re.fullmatch(pattern, external):
        raise ValueError("资料来源或编号无效")
    data = {"source": source, "external_id": external}
    for name in FIELDS:
        value = candidate.get(name) or ""
        if not isinstance(value, str) or len(value) > 2048 or "\x00" in value:
            raise ValueError("资料字段无效：" + name)
        data[name] = value.strip()
    if not data["title"]:
        raise ValueError("资料缺少作品名称")
    aliases = candidate.get("aliases", [])
    if not isinstance(aliases, (list, tuple)) or len(aliases) > 64:
        raise ValueError("资料别名格式无效")
    data["aliases"] = []
    for alias in aliases:
        if not isinstance(alias, str) or len(alias) > 512 or "\x00" in alias:
            raise ValueError("资料别名无效")
        alias = alias.strip()
        if alias and alias not in data["aliases"]:
            data["aliases"].append(alias)
    url = urlsplit(data["source_url"])
    expected = "www.dlsite.com" if source == "dlsite" else "vndb.org"
    if url.scheme != "https" or url.hostname != expected or url.username or url.password:
        raise ValueError("资料链接不是对应官方来源")
    if ((source == "vndb" and url.path.rstrip("/") != "/" + external)
            or (source == "dlsite" and not re.search(r"/product_id/" + re.escape(external) + r"\.html(?:/|$)", url.path, re.I))):
        raise ValueError("资料链接与作品编号不同")
    return data


def find_existing(conn, source, external_id):
    row = conn.execute("SELECT game_id FROM game_sources WHERE source_id=? AND external_id=?",
                       (source, external_id)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT game_id FROM personal_game_metadata WHERE source=? AND external_id=?",
                       (source, external_id)).fetchone()
    return row[0] if row else None


def confirm(conn, candidate, folder_path=None, engine=None):
    import slg_db
    data = validate(candidate)
    gid = find_existing(conn, data["source"], data["external_id"])
    if gid is None:
        gid = slg_db.add_user_game(conn, data["title"], developer=data["developer"] or None,
                                   engine=engine, folder_path=folder_path)
    origin = conn.execute("SELECT origin FROM games WHERE id=?", (gid,)).fetchone()[0]
    if origin == "user":
        conn.execute("INSERT INTO personal_game_metadata VALUES (?,?,?,?)"
                     " ON CONFLICT(source,external_id) DO UPDATE SET metadata_json=excluded.metadata_json",
                     (data["source"], data["external_id"], gid, json.dumps(data, ensure_ascii=False)))
        for title in (data["title"], data["original_title"], *data["aliases"]):
            if title:
                slg_db.add_alias(conn, gid, title, source="confirmed_metadata")
    return gid


def get_metadata(conn, game_id):
    return [json.loads(row[0]) for row in conn.execute(
        "SELECT metadata_json FROM personal_game_metadata WHERE game_id=?", (game_id,))]


def export_sections(conn):
    return {"local_metadata": [dict(row) for row in conn.execute(
        "SELECT g.slug,m.source,m.external_id,m.metadata_json FROM personal_game_metadata m"
        " JOIN games g ON g.id=m.game_id WHERE g.origin='user' ORDER BY g.slug,m.source")]}


def validate_sections(data):
    records = data.get("local_metadata", [])
    if not isinstance(records, list):
        raise ValueError("个人来源资料必须是列表")
    seen = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != BACKUP_FIELDS["local_metadata"]:
            raise ValueError("个人来源资料结构错误")
        if not all(isinstance(v, str) and len(v) <= 20000 for v in row.values()) or not row["slug"]:
            raise ValueError("个人来源资料字段错误")
        try:
            facts = json.loads(row["metadata_json"])
            if not isinstance(facts, dict):
                raise ValueError()
            validated = validate(facts)
        except (ValueError, TypeError) as exc:
            raise ValueError("个人来源资料内容错误") from exc
        key = row["source"], row["external_id"]
        if key != (validated["source"], validated["external_id"]) or key in seen:
            raise ValueError("个人来源资料身份冲突")
        seen.add(key)


def import_sections(conn, data):
    validate_sections(data)
    conn.execute("DELETE FROM personal_game_metadata")
    for row in data.get("local_metadata", []):
        game = conn.execute("SELECT id FROM games WHERE slug=? AND origin='user'", (row["slug"],)).fetchone()
        if game is None:
            raise ValueError("个人来源资料引用的自建游戏不存在")
        conn.execute("INSERT INTO personal_game_metadata VALUES (?,?,?,?)",
                     (row["source"], row["external_id"], game[0], row["metadata_json"]))
