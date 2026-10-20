"""Persistent local-library scan targets, without requiring disks to be online.

Public APIs: ``load_targets(conn)``, ``save_targets(conn, targets)`` and
``normalize_targets(targets)``. A stored empty list is intentional; only a
missing preference migrates the legacy single ``scan_root`` preference.
"""
import json
import os

PREF_TARGETS = "library.scan_targets"
LEGACY_PREF_ROOT = "scan_root"
MODES = ("children", "single", "discover")
SOURCES = ("dlsite", "vndb")


def normalize_targets(targets):
    """Validate, absolutize and deduplicate targets; first path entry wins.

    Missing/inaccessible directories stay configured. Parent/child targets
    remain distinct, because they may identify different games; the scanner
    deduplicates the actual discovered directories across all targets.
    """
    if not isinstance(targets, (list, tuple)):
        raise ValueError("scan targets must be a list")
    result, seen = [], set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("each scan target must be an object")
        if set(target) - {"path", "mode", "depth", "online_sources", "target_collection_id"}:
            raise ValueError("unknown scan target fields")
        path = target.get("path")
        mode = target.get("mode", "children")
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            raise ValueError("scan target requires a nonempty path")
        if mode not in MODES:
            raise ValueError("scan target mode must be children, single or discover")
        depth = target.get("depth", 0 if mode == "single" else 4 if mode == "discover" else 1)
        if type(depth) is not int or (depth != 0 if mode == "single" else not 1 <= depth <= (10 if mode == "discover" else 4)):
            raise ValueError("scan depth must be 0 for single, 1..4 for children or 1..10 for discover")
        sources = target.get("online_sources", [])
        if not isinstance(sources, (list, tuple)) or any(x not in SOURCES for x in sources):
            raise ValueError("online sources must contain only dlsite or vndb")
        collection = target.get("target_collection_id")
        if collection is not None and (type(collection) is not int or collection < 1):
            raise ValueError("target collection must be a positive integer or None")
        path = os.path.abspath(path)
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        item = {"path": path, "mode": mode}
        if "depth" in target:
            item["depth"] = depth
        if "online_sources" in target:
            item["online_sources"] = list(dict.fromkeys(sources))
        if "target_collection_id" in target:
            item["target_collection_id"] = collection
        result.append(item)
    return result


def normalize_plans(targets):
    """Return fully expanded plans; legacy normalize_targets retains its shape."""
    return [{"depth": 0 if t["mode"] == "single" else 4 if t["mode"] == "discover" else 1,
                  "online_sources": [], "target_collection_id": None, **t}
            for t in normalize_targets(targets)]


def save_targets(conn, targets):
    """Persist a validated list (including []) and return canonical entries."""
    import slg_db
    normalized = normalize_targets(targets)
    slg_db.set_pref(conn, PREF_TARGETS,
                    json.dumps(normalized, ensure_ascii=False))
    return normalized


def load_targets(conn):
    """Read configured targets, migrating only an absent new preference.

    Malformed stored values raise ValueError rather than silently replacing
    user configuration or resurrecting a legacy path.
    """
    import slg_db
    missing = object()
    raw = slg_db.get_pref(conn, PREF_TARGETS, missing)
    if raw is missing:
        root = slg_db.get_pref(conn, LEGACY_PREF_ROOT)
        targets = ([{"path": root, "mode": "children"}]
                   if isinstance(root, str) and root.strip() else [])
        return save_targets(conn, targets)
    try:
        return normalize_targets(json.loads(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid stored scan targets") from exc
