"""Offline DLsite client queries; source facts remain unchanged.

SQL helpers return bound parameters for the caller's existing bulk query.
Only the dedicated DLsite catalogue applies language/work-type filters.
"""
import re
from urllib.parse import urlparse

UNKNOWN = "__unknown__"


def _alias(alias):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("Invalid SQL alias")
    return alias


def _values(values):
    if isinstance(values, str):
        values = (values,)
    return tuple(dict.fromkeys(value.strip() for value in values
                               if isinstance(value, str) and value.strip()))


def search_clause(search, *, alias="g"):
    """An OR term added to existing title/alias search, never a separate query."""
    alias = _alias(alias)
    if not search or not str(search).strip():
        return "0", []
    pattern = "%" + str(search).strip() + "%"
    sql = (f"(EXISTS (SELECT 1 FROM game_catalogs dc WHERE dc.game_id={alias}.id"
           " AND dc.catalog_id='dlsite') AND ("
           f"{alias}.developer LIKE ? OR {alias}.slug LIKE ? OR "
           "EXISTS (SELECT 1 FROM game_sources ds"
           f" WHERE ds.game_id={alias}.id AND ds.source_id='dlsite'"
           " AND ds.external_id LIKE ?)))")
    return sql, [pattern, pattern, pattern]


def filter_clauses(catalog_id, languages=(), work_types=(), *, alias="g"):
    """Same-group OR, cross-group AND. Unknown is not an inferred language."""
    alias = _alias(alias)
    if catalog_id != "dlsite":
        return [], []
    where, params = [], []
    languages, work_types = _values(languages), _values(work_types)
    for values, field in ((languages, "languages"), (work_types, "work_type")):
        if not values:
            continue
        selected = [value for value in values if value != UNKNOWN]
        alternatives = []
        if field == "languages":
            present = ("EXISTS (SELECT 1 FROM json_each(dm.metadata_json,'$.languages') dl"
                       " WHERE dl.type='text' AND trim(dl.value) != '')")
            if selected:
                alternatives.append(
                    "EXISTS (SELECT 1 FROM json_each(dm.metadata_json,'$.languages') dl"
                    " WHERE dl.type='text' AND trim(dl.value) IN ("
                    + ",".join("?" for _ in selected) + "))")
        else:
            present = "coalesce(trim(json_extract(dm.metadata_json,'$.work_type')),'') != ''"
            if selected:
                alternatives.append("trim(json_extract(dm.metadata_json,'$.work_type')) IN ("
                                    + ",".join("?" for _ in selected) + ")")
        params.extend(selected)
        if UNKNOWN in values:
            alternatives.append("NOT (" + present + ")")
        predicate = "EXISTS (SELECT 1 FROM game_metadata dm WHERE dm.game_id=" + alias + ".id AND (" + " OR ".join(alternatives) + "))"
        if UNKNOWN in values:
            predicate = "(" + predicate + " OR NOT EXISTS (SELECT 1 FROM game_metadata dx WHERE dx.game_id=" + alias + ".id))"
        where.append(predicate)
    return where, params


def filter_options(conn):
    """Two bulk queries; no Python decode of all game metadata or per-card IO."""
    join = (" FROM game_metadata dm JOIN game_catalogs dc ON dc.game_id=dm.game_id"
            " AND dc.catalog_id='dlsite'")
    languages = [row[0] for row in conn.execute(
        "SELECT DISTINCT trim(dl.value)" + join +
        " JOIN json_each(dm.metadata_json,'$.languages') dl"
        " WHERE dl.type='text' AND trim(dl.value) != '' ORDER BY 1 COLLATE NOCASE")]
    types = [row[0] for row in conn.execute(
        "SELECT DISTINCT trim(json_extract(dm.metadata_json,'$.work_type'))" + join +
        " WHERE coalesce(trim(json_extract(dm.metadata_json,'$.work_type')),'') != ''"
        " ORDER BY 1 COLLATE NOCASE")]
    return {"languages": tuple(languages) + (UNKNOWN,),
            "work_types": tuple(types) + (UNKNOWN,)}


def detail_facts(game):
    """Facts for an already-bulk-loaded row. None means unknown, not negative."""
    metadata = game.get("source_metadata") or {}
    slug = game.get("slug") or ""
    match = re.fullmatch(r"dlsite-((?:rj|vj)\d+)", slug, re.I)
    product_id = match.group(1).upper() if match else None
    languages = tuple(value for value in metadata.get("languages", ())
                      if isinstance(value, str) and value.strip())
    url = game.get("url") or ""
    parsed = urlparse(url)
    section = {"RJ": "maniax", "VJ": "pro"}.get(product_id[:2]) if product_id else None
    official_url = url if (product_id and parsed.scheme == "https"
                          and parsed.hostname == "www.dlsite.com"
                          and "/" + section + "/" in parsed.path.lower()
                          and product_id.lower() in parsed.path.lower()) else None
    def text(value):
        return value if isinstance(value, str) and value.strip() else None
    return {"product_id": product_id, "developer": text(game.get("developer")),
            "release_date": text(metadata.get("release_date")),
            "updated_date": text(metadata.get("updated_date")),
            "languages": languages or None, "work_type": text(metadata.get("work_type")),
            "age_rating": text(metadata.get("age_rating")),
            "rating_value": metadata.get("rating_value"),
            "rating_count": metadata.get("rating_count"),
            "rating_scale_max": metadata.get("rating_scale_max"),
            "official_url": official_url}


def detail_lines(game):
    """Chinese factual text; the official URL is separate for a clickable button."""
    facts = detail_facts(game)
    fields = (("商品编号", "product_id"), ("开发者", "developer"),
              ("发布日期", "release_date"), ("更新日期", "updated_date"),
              ("语言", "languages"), ("作品形式", "work_type"))
    return tuple(f"{label}：" + ("、".join(facts[key]) if key == "languages" and facts[key]
                              else str(facts[key]) if facts[key] is not None else "未知")
                 for label, key in fields)
