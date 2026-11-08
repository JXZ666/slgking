"""Personal, confirmed provider metadata. Never changes public catalogue facts."""
import json
import re
from urllib.parse import urlsplit
from slg_source_registry import SOURCES, valid_id, valid_identity_url

BACKUP_FIELDS = {"local_metadata": {"slug", "source", "external_id", "metadata_json"},
                 "local_source_bindings": {"slug", "source", "external_id", "metadata_json"},
                 "local_source_settings": {"slug", "source", "enabled"}}
FIELDS = ("title", "original_title", "developer", "release_date", "source_url", "cover_url")


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS personal_game_metadata ("
                 "source TEXT NOT NULL,external_id TEXT NOT NULL,"
                 "game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,"
                 "metadata_json TEXT NOT NULL,PRIMARY KEY(source,external_id))")

    conn.execute("CREATE TABLE IF NOT EXISTS private_source_bindings ("
                 "game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,"
                 "source TEXT NOT NULL,external_id TEXT NOT NULL,metadata_json TEXT NOT NULL,"
                 "PRIMARY KEY(game_id,source),UNIQUE(source,external_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS private_source_settings ("
                 "game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,"
                 "source TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(game_id,source))")


def validate(candidate):
    source, external = candidate.get("source"), candidate.get("external_id")
    if not valid_id(source, external):
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
    if not valid_identity_url(source, external, data["source_url"]):
        raise ValueError("资料链接不是对应官方作品")
    if data['cover_url']:
        from slg_source_registry import valid_url
        if not valid_url(source,data['cover_url'],True):
            raise ValueError('封面链接不是对应官方来源')
    return data


def find_existing(conn, source, external_id):
    row = conn.execute("SELECT game_id FROM game_sources WHERE source_id=? AND external_id=?",
                       (source, external_id)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT game_id FROM personal_game_metadata WHERE source=? AND external_id=?",
                       (source, external_id)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT game_id FROM private_source_bindings WHERE source=? AND external_id=?",
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
    return {"local_source_bindings": [dict(row) for row in conn.execute(
        "SELECT g.slug,m.source,m.external_id,m.metadata_json FROM private_source_bindings m"
        " JOIN games g ON g.id=m.game_id ORDER BY g.slug,m.source")],
        "local_source_settings": [dict(row) for row in conn.execute(
        "SELECT g.slug,s.source,CAST(s.enabled AS TEXT) AS enabled FROM private_source_settings s"
        " JOIN games g ON g.id=s.game_id ORDER BY g.slug,s.source")],
        "local_metadata": [dict(row) for row in conn.execute(
        "SELECT g.slug,m.source,m.external_id,m.metadata_json FROM personal_game_metadata m"
        " JOIN games g ON g.id=m.game_id WHERE g.origin='user' ORDER BY g.slug,m.source")]}


def validate_sections(data):
    settings = data.get("local_source_settings", [])
    if not isinstance(settings, list):
        raise ValueError("来源查询设置必须是列表")
    setting_keys = set()
    for row in settings:
        if (not isinstance(row, dict) or set(row) != BACKUP_FIELDS["local_source_settings"]
                or not isinstance(row["slug"], str) or not row["slug"]
                or row["source"] not in SOURCES or row["enabled"] not in ("0", "1")
                or (row["slug"], row["source"]) in setting_keys):
            raise ValueError("来源查询设置无效")
        setting_keys.add((row["slug"], row["source"]))
    associations, game_sources = {}, {}
    for section in ("local_metadata", "local_source_bindings"):
        _validate_metadata_records(data.get(section, []))
        seen_games = set()
        for row in data.get(section, []):
            key = row['source'], row['external_id']
            if key in associations and associations[key] != row['slug']:
                raise ValueError('私人来源编号跨游戏冲突')
            associations[key] = row['slug']
            game_key = row['slug'], row['source']
            if game_key in game_sources and game_sources[game_key] != row['external_id']:
                raise ValueError('同一游戏的私人来源编号不一致')
            game_sources[game_key] = row['external_id']
            if section == 'local_source_bindings':
                game_key = row['slug'], row['source']
                if game_key in seen_games:
                    raise ValueError('同一游戏包含多个私人来源编号')
                seen_games.add(game_key)


def _validate_metadata_records(records):
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
    conn.execute('SAVEPOINT import_source_metadata')
    try:
        _import_sections(conn, data)
    except Exception:
        conn.execute('ROLLBACK TO import_source_metadata')
        conn.execute('RELEASE import_source_metadata')
        raise
    conn.execute('RELEASE import_source_metadata')


def _import_sections(conn, data):
    conn.execute("DELETE FROM private_source_bindings")
    conn.execute("DELETE FROM personal_game_metadata")
    conn.execute("DELETE FROM private_source_settings")
    for row in data.get("local_source_settings", []):
        game = conn.execute("SELECT id FROM games WHERE slug=?", (row["slug"],)).fetchone()
        if not game:
            raise ValueError("来源查询设置引用的游戏不存在")
        set_enabled(conn, game[0], row["source"], row["enabled"] == "1")
    for row in data.get("local_source_bindings", []):
        game = conn.execute("SELECT id FROM games WHERE slug=?", (row["slug"],)).fetchone()
        if not game:
            raise ValueError("私人来源关联引用的游戏不存在")
        attach(conn, game[0], json.loads(row["metadata_json"]))
    for row in data.get("local_metadata", []):
        game = conn.execute("SELECT id FROM games WHERE slug=? AND origin='user'", (row["slug"],)).fetchone()
        if game is None:
            raise ValueError("个人来源资料引用的自建游戏不存在")
        existing = find_existing(conn, row['source'], row['external_id'])
        if existing is not None and existing != game[0]:
            raise ValueError('来源编号与现有公共或私人游戏冲突')
        conn.execute("INSERT INTO personal_game_metadata VALUES (?,?,?,?)",
                     (row["source"], row["external_id"], game[0], row["metadata_json"]))


def source_rows(conn, game_id):
    """Public identity is read-only; a private binding takes display precedence."""
    result = {source: dict(source=source, external_id='', enabled=True, private=False, title='')
              for source in SOURCES}
    for row in conn.execute("SELECT source_id,external_id FROM game_sources WHERE game_id=?", (game_id,)):
        if row[0] in result:
            result[row[0]]['external_id'] = row[1]
    for facts in get_metadata(conn, game_id):
        result[facts['source']].update(facts, private=True)
    for row in conn.execute("SELECT metadata_json FROM private_source_bindings WHERE game_id=?", (game_id,)):
        facts = json.loads(row[0])
        result[facts['source']].update(facts, private=True)
    for row in conn.execute("SELECT source,enabled FROM private_source_settings WHERE game_id=?", (game_id,)):
        if row[0] in result:
            result[row[0]]['enabled'] = bool(row[1])
    return list(result.values())


def set_enabled(conn, game_id, source, enabled):
    if source not in SOURCES or type(enabled) is not bool:
        raise ValueError('来源查询设置无效')
    conn.execute("INSERT INTO private_source_settings VALUES (?,?,?) ON CONFLICT(game_id,source)"
                 " DO UPDATE SET enabled=excluded.enabled", (game_id, source, int(enabled)))


def attach(conn, game_id, candidate):
    """Caller confirms and commits. Never updates public facts, names, or covers."""
    data = validate(candidate)
    if not conn.execute("SELECT 1 FROM games WHERE id=?", (game_id,)).fetchone():
        raise ValueError('游戏不存在')
    owners = conn.execute("SELECT game_id FROM game_sources WHERE source_id=? AND external_id=?"
                          " UNION SELECT game_id FROM personal_game_metadata WHERE source=? AND external_id=?"
                          " UNION SELECT game_id FROM private_source_bindings WHERE source=? AND external_id=?",
                          (data['source'], data['external_id']) * 3).fetchall()
    if any(owner[0] != game_id for owner in owners):
        raise ValueError('此来源编号已关联另一款游戏，请先核对身份')
    conn.execute('SAVEPOINT attach_private_source')
    try:
        # An explicit correction replaces old private identity, never public facts.
        conn.execute('DELETE FROM personal_game_metadata WHERE game_id=? AND source=?', (game_id, data['source']))
        conn.execute("INSERT INTO private_source_bindings VALUES (?,?,?,?) ON CONFLICT(game_id,source)"
                     " DO UPDATE SET external_id=excluded.external_id,metadata_json=excluded.metadata_json",
                     (game_id, data['source'], data['external_id'], json.dumps(data, ensure_ascii=False)))
    except Exception:
        conn.execute('ROLLBACK TO attach_private_source')
        conn.execute('RELEASE attach_private_source')
        raise
    conn.execute('RELEASE attach_private_source')
    return data


def detach(conn, game_id, source):
    if source not in SOURCES:
        raise ValueError('来源无效')
    conn.execute("DELETE FROM private_source_bindings WHERE game_id=? AND source=?", (game_id, source))
    conn.execute("DELETE FROM personal_game_metadata WHERE game_id=? AND source=?", (game_id, source))

def query_allowed(conn, game_id, source):
    if source not in SOURCES:
        return False
    row = conn.execute('SELECT enabled FROM private_source_settings WHERE game_id=? AND source=?',
                       (game_id, source)).fetchone()
    return row is None or bool(row[0])


def private_dlsite_sql(alias='g'):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', alias):
        raise ValueError('SQL别名无效')
    return ("(EXISTS (SELECT 1 FROM personal_game_metadata pm WHERE pm.game_id=" + alias + ".id AND pm.source='dlsite')"
            " OR EXISTS (SELECT 1 FROM private_source_bindings pb WHERE pb.game_id=" + alias + ".id AND pb.source='dlsite'))")


def confirmed_facts(conn, game_ids, source):
    """Bounded batch for catalogue decoration; private corrections take precedence."""
    if source not in SOURCES:
        raise ValueError('来源无效')
    result = {}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ids = list(dict.fromkeys(game_ids))
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        if not batch:
            continue
        placeholders = ','.join('?' for _ in batch)
        for table in ('personal_game_metadata', 'private_source_bindings'):
            if table not in tables:
                continue
            for gid, raw in conn.execute('SELECT game_id,metadata_json FROM ' + table +
                                         ' WHERE source=? AND game_id IN (' + placeholders + ')', [source, *batch]):
                try:
                    result[gid] = validate(json.loads(raw))
                except (TypeError, ValueError):
                    continue
    return result
