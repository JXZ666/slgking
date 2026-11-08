"""Read-only scan presentation against current local registrations.

Call from a worker with its own SQLite connection. A successful scan match is
not a registration until the user confirms import. No filesystem scan, network
request, binding write or Tk operation belongs in this module.
"""
import os


_ERRORS = {"read_failed", "query_failed"}
_ATTENTION = {"ambiguous", "conflict"}
_LABELS = {"unmatched": "未匹配", "ambiguous": "待确认", "conflict": "绑定冲突",
           "read_failed": "读取失败", "query_failed": "查询失败"}


def valid_launcher(path):
    """Check the currently supported launch type; never walk or execute files.

    Explicit manual launchers outside the registered folder are supported by
    the existing launcher. HTML remains a recognized game format but cannot
    be claimed launchable through the current EXE-only launch action.
    """
    return (isinstance(path, str) and path.lower().endswith('.exe') and
            '\0' not in path and os.path.isfile(path))


def default_search(item):
    """Prefer usable file-title evidence, then a cleaned folder display name.

    A candidate's catalogue title is deliberately not a query hint: selecting
    the first candidate would repeat a potentially incorrect match.
    """
    from slg_scan_evidence import auxiliary_title

    def usable(value):
        if not isinstance(value, str):
            return ""
        # Failed decoding and invalid Unicode are not useful search terms.
        if "\ufffd" in value or any(
                0xD800 <= ord(char) <= 0xDFFF or
                (ord(char) < 32 and not char.isspace()) or ord(char) == 127
                for char in value):
            return ""
        value = " ".join(value.split())
        return value if value and not auxiliary_title(value) else ""

    if not isinstance(item, dict):
        return ""
    evidence = item.get("evidence")
    titles = evidence.get("titles", ()) if isinstance(evidence, dict) else ()
    if isinstance(titles, (list, tuple)):
        for fact in titles:
            if not isinstance(fact, dict) or fact.get("ambiguous"):
                continue
            value = usable(fact.get("text"))
            if value:
                return value
    name = usable(item.get("name"))
    if not name:
        return ""
    from slg_scan import folder_title
    return usable(folder_title(name))


def path_key(path):
    """Use the same physical-path identity for current bindings and history."""
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise ValueError("scan presentation requires a valid path")
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _priority(item):
    status = item.get("status", "unmatched")
    return (3 if status in _ERRORS else
            2 if status in _ATTENTION or item.get("identity_warning") else 1)


def _error_item(error):
    if not error.get("path"):
        return None
    path = error["path"]
    return {"path": path, "name": os.path.basename(path.rstrip("/\\")),
            "status": "read_failed", "reason": error.get("kind", "read_failed"),
            "message": str(error.get("message") or error.get("reason") or ""),
            "game_id": None, "candidates": [], "executables": []}


def prepare_items(conn, items, errors=()):
    """Return shallow copies with current registration and display annotations.

    ``_scan_importable`` distinguishes ordinary rows (which still require the
    existing manual confirmation) from read-only failure rows. It is never an
    authorization to bind automatically. Original scan status and candidates
    stay intact; derived registration conflicts are expressed by the display
    label/group. Duplicate physical paths keep their most actionable result,
    with errors taking priority over successful scans.
    """
    bindings, registered = {}, {}
    for row in conn.execute(
            "SELECT l.game_id,l.folder_path,l.exe_path,g.title FROM local l "
            "LEFT JOIN games g ON g.id=l.game_id "
            "WHERE NULLIF(TRIM(folder_path),'') IS NOT NULL"):
        key = path_key(row["folder_path"])
        bindings.setdefault(key, set()).add(row["game_id"])
        registered[row['game_id']] = dict(row)

    rows, positions = [], {}
    incoming = list(items)
    incoming.extend(item for item in (_error_item(error) for error in errors) if item is not None)
    for original in incoming:
        item = dict(original)
        key = path_key(item["path"])
        item.setdefault("name", os.path.basename(item["path"].rstrip("/\\")))
        item["_scan_key"] = key
        previous = positions.get(key)
        if previous is not None:
            if _priority(item) > _priority(rows[previous]):
                rows[previous] = item
        else:
            positions[key] = len(rows)
            rows.append(item)

    for item in rows:
        status = item.get("status", "unmatched")
        bound = bindings.get(item["_scan_key"], set())
        gid = item.get("game_id")
        conflict = len(bound) > 1 or (bound and gid is not None and gid not in bound)
        item["_scan_group"] = "pending"
        item["_scan_importable"] = status not in _ERRORS
        item['_scan_registered'] = bool(bound)
        item['_scan_bindings'] = [dict(registered[value]) for value in sorted(bound)]
        item['_scan_bound_game_id'] = next(iter(bound)) if len(bound) == 1 else None
        current = registered.get(item['_scan_bound_game_id'], {})
        item['_scan_bound_title'] = current.get('title') or ''
        item['_scan_needs_launcher'] = bool(len(bound) == 1 and not valid_launcher(current.get('exe_path')))
        item['_scan_saved_exe'] = current.get('exe_path')
        engine_unconfirmed = any(str(warning).startswith('incomplete_web_runtime:')
                                 for warning in item.get('detection_warnings', ()))
        if status in _ERRORS:
            item["_scan_label"] = _LABELS[status]
        elif conflict or status == "conflict":
            item["_scan_label"] = "绑定冲突"
        elif item.get("identity_warning") or status == "ambiguous" or engine_unconfirmed:
            item["_scan_label"] = "待确认"
        elif len(bound) == 1:
            if item['_scan_needs_launcher']:
                item['_scan_label'] = '已登记·待设置启动程序'
            else:
                item["_scan_group"] = "registered"
                item["_scan_label"] = "已登记"
        elif status == "matched":
            item["_scan_label"] = "已匹配·待导入"
        else:
            item["_scan_label"] = _LABELS.get(status, "待处理")
        item['_scan_default_selected'] = bool(
            gid is not None and not bound and item['_scan_importable'] and
            item['_scan_label'] == '已匹配·待导入')
    return rows
