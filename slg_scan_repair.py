"""Explicit repair of RPG data directory registrations, retaining the game.

Preview on a worker connection. Apply only after the caller confirms a preview
and checks its account/window generation. Apply uses the caller's transaction
and never commits, launches a program, renames a game or merges records.
"""
import os
from pathlib import Path

from slg_game_detection import detect_game
from slg_scan_presentation import path_key, valid_launcher


def _linked(path):
    return os.path.islink(path) or getattr(os.path, 'isjunction', lambda p: False)(path)


def _rows(conn, game_id=None):
    sql = ('SELECT l.game_id,l.folder_path,l.exe_path,l.folder_version,g.title '
           'FROM local l JOIN games g ON g.id=l.game_id '
           "WHERE NULLIF(TRIM(l.folder_path),'') IS NOT NULL")
    args = ()
    if game_id is not None:
        sql += ' AND l.game_id=?'
        args = (game_id,)
    return conn.execute(sql + ' ORDER BY l.game_id', args)


def _occupied(conn, game_id, target):
    key = path_key(target)
    for row in _rows(conn):
        if row['game_id'] != game_id and path_key(row['folder_path']) == key:
            return '父目录已登记到“%s”，请先核对已有绑定。' % (row['title'] or row['game_id'])
    return ''


def _preview(conn, row, stop):
    old = Path(os.path.abspath(os.path.normpath(row['folder_path'])))
    parent = old.parent
    if old == parent:
        return None
    web_name = old.name.casefold()
    result = dict(game_id=row['game_id'], title=row['title'] or '',
                  old_folder_path=row['folder_path'], old_exe_path=row['exe_path'],
                  folder_version=row['folder_version'], folder_path=str(parent),
                  executables=[], engine='', engine_variant='', can_apply=False,
                  reason='')
    if stop():
        return None
    if _linked(old) or _linked(parent):
        result['reason'] = '跳过符号链接或目录联接，请手动核对目录。'
        return result
    if not old.is_dir() or not parent.is_dir():
        result['reason'] = '原目录或父目录不可用，请连接磁盘后重试。'
        return result
    if web_name != 'www' and not all(path.is_file() and not _linked(path)
                                     for path in (parent / 'package.json', parent / 'nw.dll')):
        return None
    found = detect_game(parent, should_stop=stop, max_depth=4, max_entries=5000)
    if stop() or 'cancelled' in found['warnings']:
        return None
    rpg = [fact for fact in found['evidence']
           if fact['engine'] == 'RPG Maker' and fact['game_root'] == '.' and
           (web_name == 'www' or fact.get('rpg_data_dir', '').casefold() == web_name) and
           any(value.casefold().startswith(web_name + '/') for value in fact['paths'])]
    if not rpg:
        result['reason'] = '没有足够的父目录 RPG 组合证据，请通过目录管理手动核对。'
        return result
    if any(value.startswith(('entry_limit:', 'unreadable:', 'multiple_game_roots:',
                             'ambiguous_engines:')) for value in found['warnings']):
        result['reason'] = '父目录检查不完整或包含多个游戏，请手动核对。'
        return result
    parent_key = path_key(str(parent))
    result['executables'] = [value for value in found['executables']
                             if path_key(os.path.dirname(value)) == parent_key and
                             valid_launcher(value) and not _linked(value)]
    result['engine'] = 'RPG Maker'
    variants = {fact.get('engine_variant', '') for fact in rpg}
    result['engine_variant'] = next(iter(variants)) if len(variants) == 1 else ''
    if not result['executables']:
        result['reason'] = '父目录没有可确认的启动程序，请手动选择／修复目录。'
        return result
    result['reason'] = _occupied(conn, row['game_id'], str(parent))
    if result['reason']:
        return result
    result['can_apply'] = True
    result['reason'] = '确认后仅将本地目录改为父目录并保存所选启动程序；个人资料保持原样。'
    return result


def preview_www_repairs(conn, game_id=None, should_stop=None, max_items=80):
    """Return at most 80 previews; cancellation discards the partial result.

    The existing multi-row check only considers `www` paths. For an explicitly
    selected game, also check a data directory referenced by its parent package.
    Each candidate inspects one parent within the normal 5000-entry budget.
    """
    stop = should_stop or (lambda: False)
    limit = max(1, min(80, int(max_items)))
    previews = []
    for row in _rows(conn, game_id):
        if stop():
            return []
        if game_id is None and Path(os.path.normpath(row['folder_path'])).name.casefold() != 'www':
            continue
        result = _preview(conn, row, stop)
        if stop():
            return []
        if result is not None:
            previews.append(result)
            if len(previews) >= limit:
                break
    return previews


def apply_www_repair(conn, preview, executable=None):
    """Revalidate and apply one confirmed preview; return unchanged game_id.

    Begins a write transaction if needed, but leaves commit/rollback to caller.
    The savepoint contains only this repair and preserves other caller work.
    All source/target facts are checked afresh; edited preview flags, filenames
    or stale snapshots cannot authorize a different registration.
    """
    if (not isinstance(preview, dict) or type(preview.get('game_id')) is not int or
            preview.get('can_apply') is not True):
        raise ValueError('请先选择可修正的目录预览。')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    current = _rows(conn, preview['game_id']).fetchone()
    if not current:
        raise ValueError('本地登记已变化，请重新检查。')
    if (current['folder_path'] != preview.get('old_folder_path') or
            current['exe_path'] != preview.get('old_exe_path') or
            current['folder_version'] != preview.get('folder_version')):
        raise ValueError('目录或启动程序已变化，请重新预览后确认。')
    fresh = _preview(conn, current, lambda: False)
    if not fresh or not fresh['can_apply']:
        raise ValueError(fresh['reason'] if fresh else '原登记不再是可修正的 RPG 数据目录。')
    if path_key(fresh['folder_path']) != path_key(preview.get('folder_path')):
        raise ValueError('候选父目录已变化，请重新预览。')
    if executable is None and len(fresh['executables']) == 1:
        executable = fresh['executables'][0]
    if not valid_launcher(executable):
        raise ValueError('请选择父目录中存在的 .exe 启动程序。')
    choices = {path_key(value): value for value in fresh['executables']}
    chosen = choices.get(path_key(executable))
    if not chosen:
        raise ValueError('启动程序不属于已核实的父目录候选，请重新选择。')
    conn.execute('SAVEPOINT repair_www_registration')
    try:
        # Keep folder_version and every other local/state field byte-for-byte.
        # set_local_folder also initializes downloaded state, which is useful
        # for new imports but is not part of repairing an existing registration.
        changed = conn.execute('UPDATE local SET folder_path=?,exe_path=? WHERE game_id=?',
                               (fresh['folder_path'], chosen, current['game_id']))
        if changed.rowcount != 1:
            raise ValueError('本地登记已变化，请重新检查。')
    except Exception:
        conn.execute('ROLLBACK TO repair_www_registration')
        conn.execute('RELEASE repair_www_registration')
        raise
    conn.execute('RELEASE repair_www_registration')
    return current['game_id']
