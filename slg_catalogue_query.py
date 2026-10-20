"""Read-only paged catalogue queries. Workers own connections; no migrations/Tk."""
import json
import time
import slg_db
import slg_dlsite_client
import slg_hidden_games
import slg_local_library
from slg_game_categories import normalize_categories
from slg_date_filters import date_clause, updated_date_sql
from slg_library_query import PageResult, open_readonly, _decorate

BASE = ' FROM games g LEFT JOIN state s ON s.game_id=g.id LEFT JOIN local l ON l.game_id=g.id'
SCOPE = "((g.origin='site' AND g.slug NOT LIKE 'dlsite-rj%') OR (g.origin='user' AND g.promoted=1))"
SCORE = ("COALESCE((SELECT AVG(ss.my_rating-3.0) FROM state ss JOIN games sg ON sg.id=ss.game_id WHERE ss.my_rating IS NOT NULL AND " + SCOPE.replace('g.', 'sg.') + "),0)"
         " + COALESCE(g.rating,0)-COALESCE((SELECT AVG(g.rating) FROM games g WHERE g.rating IS NOT NULL AND " + SCOPE + "),0)"
         " + COALESCE((SELECT SUM(w.weight) FROM game_tags gt JOIN weights w ON w.tag_id=gt.tag_id WHERE gt.game_id=g.id),0)"
         " + COALESCE((SELECT weight FROM affinities WHERE kind='developer' AND name=g.developer),0)"
         " + COALESCE((SELECT weight FROM affinities WHERE kind='engine' AND name=g.engine),0)")

def _where(conn, spec):
    origin = spec.get('origin', 'main')
    catalog = spec.get('catalog_id')
    source = spec.get('source')
    if source is not None:
        if source not in ('all','western_slg','dlsite','user'):
            raise ValueError('Unknown source')
        if source == 'user':
            origin, catalog = 'user', None
        else:
            catalog = source
    if origin not in ('main','user','local',None):
        raise ValueError('Unknown origin')
    if catalog not in (None,'all','western_slg','dlsite'):
        raise ValueError('Unknown catalogue')
    hidden = spec.get('hidden_mode','exclude')
    if hidden not in ('exclude','only','all'):
        raise ValueError('Unknown hidden mode')
    parts = ['NOT EXISTS (SELECT 1 FROM catalog_redirects cr WHERE cr.old_slug=g.slug AND cr.active=1)']
    params = []
    if hidden != 'all':
        parts.append(slg_hidden_games.visibility_sql(conn, hidden=hidden=='only'))
    if origin == 'main':
        parts.append("(g.origin='site' OR g.promoted=1)")
    elif origin == 'user':
        parts.append("g.origin='user'")
        categories = normalize_categories(spec.get('category_ids',()))
        alternatives = []
        if categories:
            alternatives.append('EXISTS (SELECT 1 FROM user_game_categories uc WHERE uc.game_id=g.id AND uc.category IN (' + ','.join('?' for _ in categories) + '))')
            params.extend(categories)
        if spec.get('uncategorized'):
            alternatives.append('NOT EXISTS (SELECT 1 FROM user_game_categories uc WHERE uc.game_id=g.id)')
        if alternatives:
            parts.append('(' + ' OR '.join(alternatives) + ')')
    elif origin == 'local':
        parts.append(slg_local_library.LOCAL_LIBRARY_SQL)
    if catalog not in (None,'all') and origin != 'user':
        membership = 'EXISTS (SELECT 1 FROM game_catalogs gc WHERE gc.game_id=g.id AND gc.catalog_id=?)'
        if catalog == 'western_slg':
            membership = '(' + membership + " OR (g.origin='user' AND g.promoted=1))"
        parts.append(membership)
        params.append(catalog)
    for key, negate in (('include',False),('exclude',True)):
        values = tuple(dict.fromkeys(spec.get(key,())))
        if values:
            clause = 'SELECT gt.game_id FROM game_tags gt JOIN tags t ON t.id=gt.tag_id WHERE t.name IN (' + ','.join('?' for _ in values) + ')'
            params.extend(values)
            if not negate:
                clause += ' GROUP BY gt.game_id HAVING COUNT(DISTINCT gt.tag_id)=?'
                params.append(len(values))
            parts.append('g.id ' + ('NOT IN' if negate else 'IN') + ' (' + clause + ')')
    search = spec.get('search')
    if search:
        pattern = '%' + str(search) + '%'
        alternatives = ['g.title LIKE ?','EXISTS (SELECT 1 FROM game_aliases a WHERE a.game_id=g.id AND a.alias LIKE ?)']
        params.extend((pattern,pattern))
        if catalog in ('dlsite','all') or origin in ('local',None):
            clause, values = slg_dlsite_client.search_clause(search)
            alternatives.append(clause)
            params.extend(values)
        translated = spec.get('translation_ids')
        if translated is None:
            translated = slg_db.title_translation_game_ids(conn, search)
        if translated:
            alternatives.append('g.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))')
            params.append(json.dumps(sorted({int(gid) for gid in translated})))
        parts.append('(' + ' OR '.join(alternatives) + ')')
    clauses, values = slg_dlsite_client.filter_clauses(catalog, spec.get('dlsite_languages',()), spec.get('dlsite_work_types',()))
    parts.extend(clauses)
    params.extend(values)
    statuses = tuple(dict.fromkeys(spec.get('statuses',())))
    if statuses:
        parts.append('s.status IN (' + ','.join('?' for _ in statuses) + ')')
        params.extend(statuses)
    if spec.get('downloaded_only'):
        parts.append('l.game_id IS NOT NULL')
    if spec.get('collection_id') is not None:
        parts.append('EXISTS (SELECT 1 FROM collection_items ci WHERE ci.game_id=g.id AND ci.collection_id=?)')
        params.append(int(spec['collection_id']))
    clause, values = date_clause(spec)
    parts.append(clause)
    params.extend(values)
    return ' AND '.join(parts), params, catalog

def query_catalogue_page(conn, spec, page, page_size=8):
    size = int(page_size)
    if not 1 <= size <= 80:
        raise ValueError('page_size must be between 1 and 80')
    sort = spec.get('sort','score')
    if sort not in ('score','title','updated','rating','heat','source_rating'):
        raise ValueError('Unknown catalogue sort')
    started = time.perf_counter()
    conn.execute('SAVEPOINT catalogue_page')
    try:
        version = conn.execute('PRAGMA data_version').fetchone()[0]
        where, params, catalog = _where(conn, spec)
        if sort == 'source_rating' and catalog != 'dlsite':
            raise ValueError('来源评分只支持 DLsite 目录内排序。')
        if sort == 'score' and catalog == 'dlsite':
            raise ValueError('DLsite 不支持 xp 推荐排序。')
        total = conn.execute('SELECT COUNT(*)' + BASE + ' WHERE ' + where,params).fetchone()[0]
        direction = 'DESC' if spec.get('desc',True) else 'ASC'
        score = 'CASE WHEN ' + SCOPE + ' THEN (' + SCORE + ') END' if sort=='score' else 'NULL'
        columns = {'title':'g.title COLLATE NOCASE','updated':updated_date_sql(),'rating':'g.rating','heat':'g.heat', 'source_rating':"(SELECT CAST(json_extract(dm.metadata_json,'$.rating_value') AS REAL) FROM game_metadata dm WHERE dm.game_id=g.id)",'score':score}
        column = columns[sort]
        order = column + ' ' + direction if sort=='title' else '(' + column + ' IS NULL),' + column + ' ' + direction + ',g.title COLLATE NOCASE'
        order += ',g.id ASC'
        if spec.get('focus_id') is not None:
            rank = conn.execute('SELECT position FROM (SELECT g.id,ROW_NUMBER() OVER (ORDER BY ' + order + ') AS position' + BASE + ' WHERE ' + where + ') WHERE id=?',params+[int(spec['focus_id'])]).fetchone()
            if rank:
                page = (rank[0]-1)//size+1
        page = max(1,min(int(page),max(1,(total+size-1)//size)))
        if sort == 'score':
            # ORDER alias once: recommendation includes correlated tag sums.
            order = 'score ' + direction + ' NULLS LAST,g.title COLLATE NOCASE,g.id ASC'
        date_sort = updated_date_sql() if sort == 'updated' else 'NULL'
        if sort == 'updated':
            # Evaluate the normalized source date once, instead of once for
            # the null guard and again for ordering every candidate.
            order = '_source_date_sort ' + direction + ' NULLS LAST,g.title COLLATE NOCASE,g.id ASC'
        cursor = conn.execute('SELECT g.*,s.status,s.my_rating,l.folder_path,l.folder_version,l.exe_path,' + score + ' AS score,' + date_sort + ' AS _source_date_sort' + BASE + ' WHERE ' + where + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?',params+[size,(page-1)*size])
        names = [item[0] for item in cursor.description]
        rows = [dict(zip(names,row)) for row in cursor]
        for row in rows:
            row.pop('_source_date_sort', None)
        selected = time.perf_counter()
        _decorate(conn,rows)
        stats = {'games':total}
        if spec.get('collection_id') is not None:
            stats['collection_total'] = conn.execute('SELECT COUNT(*) FROM collection_items WHERE collection_id=?',(int(spec['collection_id']),)).fetchone()[0]
        conn.execute('RELEASE catalogue_page')
        return PageResult(rows,total,page,version,stats,dict(query_ms=(selected-started)*1000,total_ms=(time.perf_counter()-started)*1000))
    except BaseException:
        conn.execute('ROLLBACK TO catalogue_page')
        conn.execute('RELEASE catalogue_page')
        raise
