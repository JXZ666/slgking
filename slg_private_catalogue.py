"""Private source placement, shared by paged and legacy catalogue readers."""
import re


def private_tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('personal_game_metadata','private_source_bindings')")}


def dlsite_sql(alias="g", conn=None):
    import slg_library_metadata
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("Invalid SQL alias")
    if conn is None:
        return slg_library_metadata.private_dlsite_sql(alias)
    tables = private_tables(conn)
    clauses = ["EXISTS (SELECT 1 FROM " + table + " pm WHERE pm.game_id=" + alias + ".id AND pm.source='dlsite')"
               for table in sorted(tables)]
    return "(" + " OR ".join(clauses) + ")" if clauses else "0"


def source_sql(source,alias="g",conn=None):
    if source not in ('dlsite','vndb','bangumi','steam') or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',alias):
        raise ValueError('Invalid source or SQL alias')
    tables=private_tables(conn) if conn is not None else ('personal_game_metadata','private_source_bindings')
    clauses=["EXISTS (SELECT 1 FROM "+table+" pm WHERE pm.game_id="+alias+".id AND pm.source='"+source+"')" for table in sorted(tables)]
    return '('+' OR '.join(clauses)+')' if clauses else '0'


def promoted_sql(catalogue, alias="g", conn=None):
    condition = dlsite_sql(alias, conn)
    if catalogue == "western_slg":
        condition = "NOT (" + " OR ".join(source_sql(source,alias,conn) for source in ("dlsite","vndb","bangumi","steam")) + ")"
    elif catalogue == "vndb":
        condition=source_sql("vndb",alias,conn)
    elif catalogue != "dlsite":
        return "0"
    return "(" + alias + ".origin='user' AND " + alias + ".promoted=1 AND " + condition + ")"


def membership_sql(catalogue, alias="g", conn=None):
    return ("((" + alias + ".origin='site' AND EXISTS (SELECT 1 FROM game_catalogs gc"
            " WHERE gc.game_id=" + alias + ".id AND gc.catalog_id=?)) OR "
            + promoted_sql(catalogue, alias, conn) + ")")


def decorate(conn, rows):
    ids = [row["id"] for row in rows if row.get("origin") == "user"]
    if not ids:
        return rows
    import slg_library_metadata
    facts = slg_library_metadata.confirmed_facts(conn, ids, "dlsite")
    other={source:slg_library_metadata.confirmed_facts(conn,ids,source) for source in ("vndb","bangumi","steam")}
    for row in rows:
        if row.get("origin") != "user":
            continue
        members=tuple(source for source,values in (("dlsite",facts),("vndb",other["vndb"])) if row["id"] in values)
        row["catalog_ids"] = members or (() if any(row["id"] in values for values in other.values()) else ("western_slg",))
        # These are private facts, never a synthetic public score.
        if row["id"] in facts:
            row["source_metadata"] = facts[row["id"]]
    return rows
