"""Lexical local-folder scopes. No stat, realpath or filesystem enumeration.

The index is disposable. Local-table triggers mark changed rows; read-only
queries safely normalize dirty rows until the writer refreshes the index.
"""
import ntpath
import json

ALL = 'all'
OTHER = 'other'


def normalize_path(path):
    value = str(path or '').strip().replace('/', '\\')
    if not value or '\x00' in value:
        return ''
    if value[:8].lower() == '\\\\?\\unc\\':
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    value = ntpath.normcase(ntpath.normpath(value))
    drive, tail = ntpath.splitdrive(value)
    if drive.startswith('\\\\') and not tail:
        return drive + '\\'
    if not drive or not tail.startswith('\\'):
        return ''  # no process-CWD-dependent paths
    return value.rstrip('\\') + ('\\' if tail == '\\' else '')


def binding_path(folder, exe):
    folder = str(folder or '').strip()
    return normalize_path(folder if folder else ntpath.dirname(str(exe or '').strip()))


def targets(paths):
    result = {}
    for item in paths or ():
        raw = item.get('path', '') if isinstance(item, dict) else item
        key = normalize_path(raw)
        if key:
            result.setdefault(key, str(raw).strip())
    return result


def options(paths):
    return [dict(value=ALL, label='全部本地游戏'),
            *[dict(value=key, label=label) for key, label in targets(paths).items()],
            dict(value=OTHER, label='其他／未归属')]


def load_targets(conn):
    """Read saved roots without scan normalization's realpath/disk access.

    This read does not migrate preferences. The scan manager remains the
    authority for editing/validating scan depth, sources and collection plans.
    """
    from slg_db import get_pref
    raw = get_pref(conn, 'library.scan_targets', None)
    if raw is None:
        legacy = get_pref(conn, 'scan_root', '')
        return [{'path': legacy}] if normalize_path(legacy) else []
    try:
        saved = json.loads(raw)
        if not isinstance(saved, list) or any(not isinstance(item, dict) or not isinstance(item.get('path'), str) for item in saved):
            raise ValueError('invalid saved scan targets')
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid saved scan targets') from exc
    return [{'path': label} for label in targets(saved).values()]


def ensure_schema(conn):
    """Writer only; does not commit the caller's transaction."""
    conn.execute('SAVEPOINT ensure_folder_index')
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS local_folder_index (game_id INTEGER PRIMARY KEY, path TEXT NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS local_folder_path_idx ON local_folder_index(path)')
        conn.execute('CREATE TABLE IF NOT EXISTS local_folder_dirty (game_id INTEGER PRIMARY KEY)')
        for action, ref in [('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')]:
            name = 'local_folder_dirty_' + action.lower()
            # The outer local UPSERT can override a trigger's OR IGNORE
            # conflict policy. Avoid the duplicate INSERT altogether, and
            # replace old definitions so existing test/user databases upgrade.
            conn.execute('DROP TRIGGER IF EXISTS ' + name)
            conn.execute('CREATE TRIGGER ' + name + ' AFTER ' + action + ' ON local BEGIN '
                         'INSERT INTO local_folder_dirty SELECT ' + ref + '.game_id '
                         'WHERE NOT EXISTS (SELECT 1 FROM local_folder_dirty WHERE game_id=' + ref + '.game_id); END')
        conn.execute('INSERT OR IGNORE INTO local_folder_dirty SELECT game_id FROM local WHERE game_id NOT IN (SELECT game_id FROM local_folder_index)')
        count = refresh_index(conn)
        conn.execute('RELEASE ensure_folder_index')
        return count
    except BaseException:
        conn.execute('ROLLBACK TO ensure_folder_index')
        conn.execute('RELEASE ensure_folder_index')
        raise


def refresh_index(conn):
    conn.execute('SAVEPOINT refresh_folder_index')
    try:
        rows = conn.execute('SELECT d.game_id,l.folder_path,l.exe_path FROM local_folder_dirty d LEFT JOIN local l ON l.game_id=d.game_id').fetchall()
        for gid, folder, exe in rows:
            path = binding_path(folder, exe)
            if path:
                conn.execute('INSERT OR REPLACE INTO local_folder_index VALUES (?,?)', (gid, path))
            else:
                conn.execute('DELETE FROM local_folder_index WHERE game_id=?', (gid,))
        conn.execute('DELETE FROM local_folder_dirty')
        conn.execute('RELEASE refresh_folder_index')
        return len(rows)
    except BaseException:
        conn.execute('ROLLBACK TO refresh_folder_index')
        conn.execute('RELEASE refresh_folder_index')
        raise


def invalidate_index(conn):
    """Writer hook after bulk restore; no commit and no disk path inspection."""
    conn.execute('INSERT OR IGNORE INTO local_folder_dirty SELECT game_id FROM local')
    conn.execute('INSERT OR IGNORE INTO local_folder_dirty SELECT game_id FROM local_folder_index')


def query_clause(conn, spec):
    """Return AND-able predicate and bindings; query spec contains paths only."""
    selected = spec.get('folder_scope', ALL)
    if selected in (None, '', ALL):
        return '1', []
    roots = targets(spec.get('folder_targets', ()))
    if selected != OTHER:
        selected = normalize_path(selected)
        if selected not in roots:
            return '0', []  # stale/removed target must not silently expose all
        roots = {selected: roots[selected]}
    conn.create_function('slg_binding_path', 2, binding_path, deterministic=True)
    exists = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('local_folder_index','local_folder_dirty')").fetchone()[0] == 2
    def predicate(expression):
        clauses, params = [], []
        for root in roots:
            prefix = root if root.endswith('\\') else root + '\\'
            clauses.append('(' + expression + '=? OR (' + expression + '>=? AND ' + expression + '<?))')
            # BINARY lexical prefix interval avoids LIKE wildcard/case behaviour.
            params.extend((root, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)))
        return '(' + ' OR '.join(clauses) + ')' if clauses else '0', params
    raw, params = predicate('slg_binding_path(l.folder_path,l.exe_path)')
    clause = raw
    if exists:
        indexed, index_params = predicate('fi.path')
        # The first subquery uses the path index, the fallback touches only
        # changed or missing rows. This remains correct before writer refresh.
        clause = ('(g.id IN (SELECT fi.game_id FROM local_folder_index fi WHERE ' + indexed +
                 ' AND fi.game_id NOT IN (SELECT game_id FROM local_folder_dirty)) OR g.id IN (' +
                 'SELECT l.game_id FROM local l WHERE CASE WHEN (l.game_id IN (SELECT game_id FROM local_folder_dirty) OR '
                 'NOT EXISTS(SELECT 1 FROM local_folder_index fi WHERE fi.game_id=l.game_id)) THEN ' + raw + ' ELSE 0 END))')
        params = index_params + params
    return ('NOT ' + clause if selected == OTHER else clause), params
