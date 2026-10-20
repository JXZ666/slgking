"""Validate sanitized public SQLite data and publish immutable western snapshots.

The caller retains its existing backup/strip/source sanitization sequence. This
module compares logical public tables, so SQLite headers and crawl checkpoints
cannot turn an unchanged catalogue into a new download.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

HASH = re.compile(r'[0-9a-f]{64}\Z')
PUBLIC_TABLES = frozenset(('games', 'tags', 'game_tags', 'game_sources',
                           'game_redirects', 'game_catalogs', 'game_metadata'))
STATUS_PATH = '/catalogues/western_slg/status.json'


def _encode(value):
    if isinstance(value, bytes):
        return {'sqlite_blob': value.hex()}
    return value


def semantic_hash(path):
    """Hash ordered schema/rows of a validated public database, never file layout."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Invalid public database path')
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        conn.execute('PRAGMA query_only=ON')
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Public database integrity failure')
        objects = conn.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = {name for kind, name in objects if kind == 'table'}
        if not {'games','tags','game_tags'}.issubset(tables) or tables - PUBLIC_TABLES:
            raise ValueError('Unsanitized public database tables')
        if any(kind in ('trigger','view') for kind, _ in objects):
            raise ValueError('Unexpected executable public schema')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(games)')}
        if 'origin' in columns and conn.execute("SELECT 1 FROM games WHERE origin IS NULL OR origin!='site' LIMIT 1").fetchone():
            raise ValueError('Private game in public catalogue')
        if 'game_sources' in tables and conn.execute("SELECT 1 FROM game_sources WHERE source_id NOT IN ('dikgames','f95zone') LIMIT 1").fetchone():
            raise ValueError('Internal source in public catalogue')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Invalid public references')
        digest = hashlib.sha256()
        for name in sorted(tables):
            schema = [tuple(row[1:]) for row in conn.execute('PRAGMA table_info("' + name + '")')]
            encoded = [json.dumps([_encode(value) for value in row], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                       for row in conn.execute('SELECT * FROM "' + name + '"')]
            payload = json.dumps([name,schema,sorted(encoded)], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
            digest.update(len(payload).to_bytes(8,'big'))
            digest.update(payload)
        count = conn.execute('SELECT count(*) FROM games').fetchone()[0]
        if count < 1:
            raise ValueError('Refusing empty public catalogue')
        return digest.hexdigest(), count
    finally:
        conn.close()


def _safe_directory(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Publication path contains a symlink')
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_bytes(path, raw):
    path = Path(path)
    _safe_directory(path.parent)
    if path.is_symlink():
        raise ValueError('Publication file is a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
        if os.name != 'nt':
            directory_fd = os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish_snapshot(staged_db, publish_dir, *, count, lastmod, generated_at):
    """Write verified immutable DB, compatibility alias, then active manifest.

    Existing activity must validate before reuse. The caller has already
    sanitized staged_db with the production stripping logic.
    """
    output = _safe_directory(publish_dir)
    logical, actual_count = semantic_hash(staged_db)
    if count != actual_count:
        raise ValueError('Public catalogue count mismatch')
    raw = Path(staged_db).read_bytes()
    active = output/'manifest.json'
    compatibility = output/'slgking.db'
    previous = None
    previous_raw = None
    if active.exists():
        if active.is_symlink() or compatibility.is_symlink():
            raise ValueError('Active publication symlink')
        previous = json.loads(active.read_text(encoding='utf-8'))
        if (not isinstance(previous,dict) or type(previous.get('count')) is not int
                or previous['count'] < 1 or type(previous.get('db_size')) is not int
                or previous['db_size'] < 1 or not isinstance(previous.get('lastmod'),str)
                or not previous['lastmod']):
            raise ValueError('Invalid active publication fields')
        revision = previous.get('db_sha256')
        if not isinstance(revision,str) or not HASH.fullmatch(revision):
            raise ValueError('Invalid active publication hash')
        old_path = previous.get('db_path')
        expected = '/catalogues/western_slg/snapshots/'+revision+'.db'
        if old_path is not None and old_path != expected:
            raise ValueError('Invalid active publication path')
        selected = output/old_path.lstrip('/') if old_path else compatibility
        _safe_directory(selected.parent)
        if selected.is_symlink():
            raise ValueError('Active snapshot symlink')
        previous_raw = selected.read_bytes()
        if hashlib.sha256(previous_raw).hexdigest() != revision or len(previous_raw) != previous.get('db_size'):
            raise ValueError('Active publication hash/size mismatch')
        old_logical, old_count = semantic_hash(selected)
        if old_count != previous.get('count'):
            raise ValueError('Active publication count mismatch')
        if previous.get('semantic_sha256',old_logical) != old_logical:
            raise ValueError('Active semantic hash mismatch')
        if old_logical == logical:
            raw = previous_raw
    revision = hashlib.sha256(raw).hexdigest()
    db_path = '/catalogues/western_slg/snapshots/'+revision+'.db'
    snapshot = output/db_path.lstrip('/')
    _safe_directory(snapshot.parent)
    if snapshot.exists():
        if snapshot.is_symlink() or snapshot.read_bytes() != raw:
            raise ValueError('Immutable publication collision/corruption')
    else:
        atomic_bytes(snapshot,raw)
    if semantic_hash(snapshot) != (logical,count):
        raise ValueError('Immutable publication verification failure')
    unchanged = previous and previous['db_sha256'] == revision
    manifest = dict(previous or {}, **{'count':count,'lastmod':previous['lastmod'] if unchanged else '%s:%s' % (lastmod or 'catalogue',revision[:16]),
                'db_sha256':revision,'db_size':len(raw),'generated_at':previous.get('generated_at',generated_at) if unchanged else generated_at,
                'db_path':db_path,'source_status_path':STATUS_PATH,'semantic_sha256':logical})
    # The immutable resource is durable and verified before either public cursor.
    atomic_bytes(compatibility,raw)
    try:
        atomic_bytes(active,json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))
    except Exception:
        if previous_raw is not None:
            atomic_bytes(compatibility,previous_raw)
        raise
    return manifest


def bootstrap_from_active(publish_dir):
    """Add immutable/status metadata using only verified active public bytes.

    No source DB, stripping, collection or live facts are touched. Legacy
    count/hash/lastmod/generated_at remain exactly the active values.
    """
    output = _safe_directory(publish_dir)
    active = output/'manifest.json'
    database = output/'slgking.db'
    if active.is_symlink() or database.is_symlink():
        raise ValueError('Active publication symlink')
    previous = json.loads(active.read_text(encoding='utf-8'))
    raw = database.read_bytes()
    if hashlib.sha256(raw).hexdigest() != previous.get('db_sha256') or len(raw) != previous.get('db_size'):
        raise ValueError('Active compatibility database mismatch')
    result = publish_snapshot(database,output,count=previous['count'],lastmod=previous['lastmod'],generated_at=previous.get('generated_at'))
    manifest = dict(previous,**result)
    for key in ('count','db_sha256','db_size','lastmod','generated_at'):
        if key in previous and manifest[key] != previous[key]:
            raise ValueError('Bootstrap changed active catalogue cursor')
    return manifest
