"""Local-only DLsite identity extraction. No URL guessing or network access."""
import re

_PRODUCT = re.compile(r"(?<![A-Za-z0-9])(?:RJ|VJ)[0-9]{6,10}(?![A-Za-z0-9])", re.I)


def product_ids(folder_name):
    """Preserve leading zeroes and require a complete, bounded identifier."""
    return list(dict.fromkeys(m.group(0).upper() for m in _PRODUCT.finditer(folder_name or "")))


def product_index(conn, canonical=None):
    """Read the complete local source index once per scan, not once per folder."""
    canonical = canonical or {}
    index = {}
    for row in conn.execute("SELECT s.external_id,s.game_id,g.title FROM game_sources s"
                            " JOIN games g ON g.id=s.game_id WHERE s.source_id='dlsite'"
                            " UNION ALL SELECT m.external_id,m.game_id,g.title FROM personal_game_metadata m"
                            " JOIN games g ON g.id=m.game_id WHERE m.source='dlsite'"):
        external = (row["external_id"] or "").upper()
        if not re.fullmatch(r"(?:RJ|VJ)[0-9]{6,10}", external):
            continue
        gid = canonical.get(row["game_id"], row["game_id"])
        choices = index.setdefault(external, [])
        if not any(choice["game_id"] == gid for choice in choices):
            choices.append({"game_id": gid, "title": row["title"], "reason": "product_id"})
    return index


def identify(folder_name, index):
    ids = product_ids(folder_name)
    choices = {}
    for identifier in ids:
        for choice in index.get(identifier, ()):
            choices.setdefault(choice["game_id"], choice)
    warning = ""
    if len(ids) > 1:
        warning = "目录包含多个商品编号，请人工确认对应作品"
    elif ids and ids[0].startswith("VJ") and not choices:
        warning = "当前目录暂不支持VJ编号，请人工确认或添加游戏"
    elif ids and not choices:
        warning = "目录未收录此RJ编号，请勿按同名作品自动绑定"
    return ids, list(choices.values()), warning
