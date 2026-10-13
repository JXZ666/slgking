"""Bounded local scan review history. No scans, binding changes or network I/O."""
import hashlib
import json
import os
import time

PREF_REVIEW = "library.scan_review"
MAX_ITEMS = 2000
_STATUSES = {"matched", "unmatched", "ambiguous", "conflict", "read_failed", "query_failed"}


def path_key(path):
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise ValueError("review requires a valid path")
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def scope_key(plans):
    """A scope is one path, one target object or a list of scan plans."""
    from slg_scan_targets import normalize_plans
    if isinstance(plans, str):
        plans = [{"path": plans, "mode": "single"}]
    elif isinstance(plans, dict):
        plans = [plans]
    normalized = normalize_plans(plans)
    for item in normalized:
        item["path"] = path_key(item["path"])
    normalized.sort(key=lambda p: p["path"])
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def _load(conn):
    import slg_db
    raw = slg_db.get_pref(conn, PREF_REVIEW)
    if raw is None:
        return {"version": 1, "scopes": {}}
    try:
        data = json.loads(raw)
        if data.get("version") != 1 or not isinstance(data.get("scopes"), dict):
            raise ValueError("unsupported scan review history")
        return data
    except (TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid scan review history") from exc


def _save(conn, data):
    import slg_db
    scopes = data["scopes"]
    ordered = sorted(scopes, key=lambda key: scopes[key]["updated_at"], reverse=True)
    remaining = MAX_ITEMS
    for key in ordered:
        rows = scopes[key]["items"][:remaining]
        remaining -= len(rows)
        if rows:
            scopes[key]["items"] = rows
        else:
            del scopes[key]
    slg_db.set_pref(conn, PREF_REVIEW, json.dumps(data, ensure_ascii=False))


def save_recent(conn, plans, result):
    """Save only review data from a completed run; a cancellation is a no-op."""
    if not isinstance(result, dict):
        raise ValueError("scan result must be an object")
    if result.get("cancelled"):
        return False
    key = scope_key(plans)
    data = _load(conn)
    old = {row["key"]: row for row in data["scopes"].get(key, {}).get("items", [])}
    rows, seen = [], set()
    incoming = list(result.get("items", []))
    incoming += [{"path": e["path"], "status": "read_failed",
                  "reason": e.get("kind", "read_failed"), "message": e.get("message", "")}
                 for e in result.get("errors", []) if e.get("path")]
    for item in incoming:
        identity = path_key(item["path"])
        if identity in seen:
            continue
        status = item.get("status", "unmatched")
        if status not in _STATUSES:
            raise ValueError("unknown review status")
        seen.add(identity)
        rows.append({"key": identity, "path": os.path.abspath(item["path"]),
                     "name": str(item.get("name", ""))[:512], "status": status,
                     "reason": str(item.get("reason", ""))[:512],
                     "message": str(item.get("message", ""))[:2048],
                     "ignored": bool(old.get(identity, {}).get("ignored", False))})
        if len(rows) >= MAX_ITEMS:
            break
    data["scopes"][key] = {"updated_at": time.time(), "items": rows}
    _save(conn, data)
    return True


def get_recent(conn, plans, include_ignored=True):
    rows = _load(conn)["scopes"].get(scope_key(plans), {}).get("items", [])
    return [dict(row) for row in rows if include_ignored or not row["ignored"]]


def retry_selection(conn, plans, paths):
    """Return selected nonignored failures as single-directory scan targets."""
    selected = {path_key(p) for p in paths}
    return [{"path": row["path"], "mode": "single", "depth": 0}
            for row in get_recent(conn, plans, include_ignored=False)
            if row["key"] in selected and row["status"] != "matched"]


def _set_ignored(conn, plans, paths, value):
    selected = {path_key(p) for p in paths}
    key, data = scope_key(plans), _load(conn)
    changed = 0
    for row in data["scopes"].get(key, {}).get("items", []):
        if row["key"] in selected and row["ignored"] != value:
            row["ignored"] = value
            changed += 1
    if changed:
        _save(conn, data)
    return changed


def ignore(conn, plans, paths):
    return _set_ignored(conn, plans, paths, True)


def unignore(conn, plans, paths):
    return _set_ignored(conn, plans, paths, False)
