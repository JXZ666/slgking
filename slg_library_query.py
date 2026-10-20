"""Read-only, bounded local-library pages. No Tk, network or schema writes."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3
import time

import slg_db
import slg_dlsite_client
from slg_game_categories import normalize_categories
import slg_hidden_games
import slg_local_library
from slg_date_filters import date_clause, updated_date_sql


@dataclass
class PageResult:
    rows: list
    total: int
    page: int
    data_version: int
    stats: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)


def open_readonly(path):
    """Open on the calling worker, never migrate or create the database."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _where(conn, spec):
    source = spec.get('source', 'all')
    if source not in ('all', 'user'):
        raise ValueError('Unknown local-library source')
    parts = [slg_local_library.LOCAL_LIBRARY_SQL,
             'NOT EXISTS (SELECT 1 FROM catalog_redirects cr WHERE cr.old_slug=g.slug AND cr.active=1)']
    parts.append(slg_hidden_games.visibility_sql(conn))
    params = []
    if source == 'user':
        parts.append("g.origin='user'")
        categories = normalize_categories(spec.get('category_ids', ()))
        alternatives = []
        if categories:
            alternatives.append('EXISTS (SELECT 1 FROM user_game_categories uc WHERE uc.game_id=g.id AND uc.category IN (%s))' % ','.join('?' for _ in categories))
            params.extend(categories)
        if spec.get('uncategorized'):
            alternatives.append('NOT EXISTS (SELECT 1 FROM user_game_categories uc WHERE uc.game_id=g.id)')
        if alternatives:
            parts.append('(' + ' OR '.join(alternatives) + ')')
    include = tuple(dict.fromkeys(spec.get('include', ())))
    exclude = tuple(dict.fromkeys(spec.get('exclude', ())))
    if include:
        parts.append('g.id IN (SELECT gt.game_id FROM game_tags gt JOIN tags t ON t.id=gt.tag_id WHERE t.name IN (%s) GROUP BY gt.game_id HAVING COUNT(DISTINCT gt.tag_id)=?)' % ','.join('?' for _ in include))
        params.extend(include)
        params.append(len(include))
    if exclude:
        parts.append('g.id NOT IN (SELECT gt.game_id FROM game_tags gt JOIN tags t ON t.id=gt.tag_id WHERE t.name IN (%s))' % ','.join('?' for _ in exclude))
        params.extend(exclude)
    search = spec.get('search')
    if search:
        pattern = '%' + search + '%'
        clause, values = slg_dlsite_client.search_clause(search)
        alternatives = ['g.title LIKE ?', 'EXISTS (SELECT 1 FROM game_aliases a WHERE a.game_id=g.id AND a.alias LIKE ?)', clause]
        params.extend((pattern, pattern))
        params.extend(values)
        translated = spec.get('translation_ids')
        if translated is None:
            translated = slg_db.title_translation_game_ids(conn, search)
        if translated:
            alternatives.append('g.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))')
            params.append(json.dumps(sorted({int(gid) for gid in translated})))
        parts.append('(' + ' OR '.join(alternatives) + ')')
    if spec.get('statuses'):
        statuses = tuple(dict.fromkeys(spec['statuses']))
        parts.append('s.status IN (' + ','.join('?' for _ in statuses) + ')')
        params.extend(statuses)
    clause, values = date_clause(spec)
    parts.append(clause)
    params.extend(values)
    return ' AND '.join(parts), params


def _decorate(conn, rows):
    ids = [row['id'] for row in rows]
    categories = slg_db.game_categories_bulk(conn, [row['id'] for row in rows if row['origin'] == 'user'])
    catalogues = slg_db.game_catalogues_bulk(conn, [row['id'] for row in rows if row['origin'] == 'site'])
    metadata = slg_db.get_game_metadata_bulk(conn, [gid for gid in ids if 'dlsite' in catalogues.get(gid, ())])
    overrides = {}
    if ids and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='game_cover_overrides'").fetchone():
        overrides = dict(conn.execute('SELECT game_id,cover_file FROM game_cover_overrides WHERE game_id IN (%s)' % ','.join('?' for _ in ids), ids))
    directory = slg_local_library._directory(None) if overrides else None
    for row in rows:
        gid = row['id']
        row['categories'] = categories.get(gid, ())
        row['catalog_ids'] = catalogues.get(gid, ())
        row['source_metadata'] = metadata.get(gid, {})
        row['catalogue_cover_file'] = row.get('cover_file')
        name = overrides.get(gid)
        available = bool(name and slg_local_library._valid_filename(name) and os.path.isfile(os.path.join(directory, name)))
        row['has_cover_override'] = available
        row['cover_override_missing'] = bool(name and not available)
        if available:
            row['cover_file'] = name


def query_local_page(conn, spec, page, page_size=8):
    """Count and decorate only one page inside a coherent SQLite snapshot.

    page is 1-based and clamped. Categories apply only to source=user. All
    returned rows preserve the public find_games dictionary field contract.
    The caller owns connection lifetime; existing transactions are preserved.
    """
    page_size = int(page_size)
    if not 1 <= page_size <= 80:
        raise ValueError('page_size must be between 1 and 80')
    sort = spec.get('sort', 'title')
    if sort not in ('title', 'updated'):
        raise ValueError('Unknown local-library sort')
    started = time.perf_counter()
    conn.execute('SAVEPOINT library_page')
    try:
        version = conn.execute('PRAGMA data_version').fetchone()[0]
        where, params = _where(conn, spec)
        base = ' FROM games g LEFT JOIN state s ON s.game_id=g.id LEFT JOIN local l ON l.game_id=g.id'
        total = conn.execute('SELECT COUNT(*)' + base + ' WHERE ' + where, params).fetchone()[0]
        direction = 'DESC' if spec.get('desc', False) else 'ASC'
        order = 'g.title COLLATE NOCASE ' + direction if sort == 'title' else '(' + updated_date_sql() + ' IS NULL), ' + updated_date_sql() + ' ' + direction + ', g.title COLLATE NOCASE'
        order += ',g.id ASC'
        if spec.get('focus_id') is not None:
            rank = conn.execute('SELECT position FROM (SELECT g.id,ROW_NUMBER() OVER (ORDER BY ' + order + ') AS position' + base + ' WHERE ' + where + ') WHERE id=?', params + [int(spec['focus_id'])]).fetchone()
            if rank is not None:
                page = (rank[0] - 1) // page_size + 1
        page = max(1, min(int(page), max(1, (total + page_size - 1) // page_size)))
        cursor = conn.execute('SELECT g.*,s.status,s.my_rating,l.folder_path,l.folder_version,l.exe_path,NULL AS score' + base + ' WHERE ' + where + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', params + [page_size, (page - 1) * page_size])
        columns = [item[0] for item in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor]
        selected = time.perf_counter()
        _decorate(conn, rows)
        decorated = time.perf_counter()
        scope = slg_local_library.LOCAL_LIBRARY_SQL + ' AND NOT EXISTS (SELECT 1 FROM catalog_redirects cr WHERE cr.old_slug=g.slug AND cr.active=1)'
        scope += ' AND ' + slg_hidden_games.visibility_sql(conn)
        counts = conn.execute('SELECT COUNT(*),SUM(s.my_rating IS NOT NULL),SUM(NULLIF(TRIM(l.folder_path),\'\') IS NOT NULL OR NULLIF(TRIM(l.exe_path),\'\') IS NOT NULL)' + base + ' WHERE ' + scope).fetchone()
        tags = conn.execute('SELECT COUNT(DISTINCT gt.tag_id) FROM game_tags gt JOIN games g ON g.id=gt.game_id LEFT JOIN local l ON l.game_id=g.id WHERE ' + scope).fetchone()[0]
        stats = dict(games=counts[0], rated=counts[1] or 0, downloaded=counts[2] or 0, tags=tags)
        conn.execute('RELEASE library_page')
        return PageResult(rows, total, page, version, stats, dict(query_ms=(selected-started)*1000, decorate_ms=(decorated-selected)*1000, total_ms=(time.perf_counter()-started)*1000))
    except BaseException:
        conn.execute('ROLLBACK TO library_page')
        conn.execute('RELEASE library_page')
        raise
