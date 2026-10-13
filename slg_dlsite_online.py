"""Validated, same-server DLsite snapshots and on-demand artwork cache.

The transport is injected: this module never follows upstream artwork URLs.
The merge callback must preserve personal state and accept (conn, path, 'dlsite').
"""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time

MANIFEST_PATH = '/catalogues/dlsite/manifest.json'
ONLINE_HASH_KEY = 'catalogue.online.dlsite.hash'
BUNDLE_HASH_KEY = 'catalogue.bundle.dlsite.hash'
ONLINE_CHECK_KEY = 'catalogue.online.dlsite.checked'
INDEX_KEY = 'catalogue.online.dlsite.cover_index'
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_DB_BYTES = 256 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SLUG = re.compile(r'dlsite-rj\d{6,10}\Z')
_COVER = re.compile(r'dlsite-(RJ\d{6,10})-([0-9a-f]{64})\.jpg\Z')


def _json(raw, limit):
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise ValueError('Invalid or oversized DLsite response')
    return json.loads(raw.decode('utf-8'))


def validate_manifest(value):
    if not isinstance(value, dict) or value.get('catalog_id') != 'dlsite':
        raise ValueError('Wrong catalogue')
    if type(value.get('count')) is not int or not 1 <= value['count'] <= 2_000_000:
        raise ValueError('Invalid catalogue count; empty replacement refused')
    for key, folder, suffix in [('db_sha256', 'snapshots', '.db'),
                                ('cover_index_sha256', 'cover-index', '.json')]:
        digest = value.get(key)
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ValueError('Invalid content hash')
        path_key = 'db_path' if key == 'db_sha256' else 'cover_index_path'
        if value.get(path_key) != f'/catalogues/dlsite/{folder}/{digest}{suffix}':
            raise ValueError('Unexpected DLsite route')
    if not isinstance(value.get('revision'), str) or not value['revision']:
        raise ValueError('Missing revision')
    return dict(value)


def validate_cover_index(value):
    if not isinstance(value, dict) or len(value) > 2_000_000:
        raise ValueError('Invalid cover index')
    result = {}
    for slug, item in value.items():
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or not isinstance(item, dict):
            raise ValueError('Invalid cover identity')
        match = _COVER.fullmatch(str(item.get('filename', '')))
        if not match or slug != 'dlsite-' + match[1].lower() or item.get('sha256') != match[2]:
            raise ValueError('Cover hash/name mismatch')
        if type(item.get('bytes')) is not int or not 0 < item['bytes'] <= MAX_IMAGE_BYTES:
            raise ValueError('Invalid cover byte count')
        result[slug] = {key: item[key] for key in ('filename', 'sha256', 'bytes')}
    return result


def validate_image(raw):
    """Decode before publication; reject oversized and multi-frame images."""
    from PIL import Image
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_IMAGE_BYTES:
        raise ValueError('Artwork exceeds 5 MiB')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Unsupported artwork format')
            if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Oversized or animated artwork')
            image.load()
            return image.copy()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('Damaged or oversized artwork') from error


def normalise_image(raw):
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(validate_image(raw))
    image.thumbnail((1600, 2400))
    if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
        rgba = image.convert('RGBA')
        base = Image.new('RGB', rgba.size, 'white')
        base.paste(rgba, mask=rgba.getchannel('A'))
        image = base
    else:
        image = image.convert('RGB')
    target = io.BytesIO()
    image.save(target, 'JPEG', quality=90)
    return target.getvalue()


def atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _pref(conn, key):
    row = conn.execute('SELECT value FROM prefs WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def _set_pref(conn, key, value):
    conn.execute('INSERT INTO prefs(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, value))


def has_online_catalogue(conn):
    return bool(_pref(conn, ONLINE_HASH_KEY))


def should_check(conn, now=None, interval=3600):
    try:
        return (time.time() if now is None else now) - float(_pref(conn, ONLINE_CHECK_KEY) or 0) >= interval
    except (TypeError, ValueError):
        return True


def get_cover_index(conn):
    raw = _pref(conn, INDEX_KEY)
    return validate_cover_index(json.loads(raw)) if raw else {}


def validate_snapshot(path, manifest, index):
    """SQLite read-only checks before invoking any personal database merge."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as source:
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Damaged snapshot')
        if source.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Dangling snapshot relationship')
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('state', 'local', 'local_saves', 'game_cover_overrides', 'user_game_categories',
                      'collections', 'collection_items', 'comments', 'prefs', 'points_log', 'signin',
                      'owned_titles', 'wishlist_version_seen', 'weights', 'affinities', 'exclusions',
                      'manual_translations', 'sync_log'):
            if table in tables and source.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                raise ValueError('Personal data in public snapshot')
        if source.execute("SELECT 1 FROM sqlite_master WHERE name='game_redirects'").fetchone():
            if source.execute('SELECT 1 FROM game_redirects LIMIT 1').fetchone():
                raise ValueError('DLsite identity redirects are not supported')
        rows = source.execute('SELECT id,slug,origin,cover_file FROM games').fetchall()
        if len(rows) != manifest['count'] or any(not _SLUG.fullmatch(row[1]) or row[2] != 'site' for row in rows):
            raise ValueError('Snapshot membership/count mismatch')
        slugs = {row[1] for row in rows}
        if len(slugs) != len(rows) or not set(index).issubset(slugs):
            raise ValueError('Duplicate or unknown cover identity')
        for _gid, slug, _origin, cover in rows:
            if (cover or None) != (index.get(slug, {}).get('filename') or None):
                raise ValueError('Snapshot cover/index mismatch')
        sources = source.execute('SELECT game_id,source_id,external_id FROM game_sources').fetchall()
        identities = {row[0]: row[1] for row in rows}
        if any(kind != 'dlsite' or identities.get(gid) != 'dlsite-' + external.lower()
               for gid, kind, external in sources) or len({gid for gid, _, _ in sources}) != len(rows):
            raise ValueError('Snapshot source mismatch')


def pull(conn, fetch, merge, log=lambda message: None, now=None):
    """Pull immutable files through fetch(relative_path)->bytes, then merge atomically.

    No cursor is written on download/validation/merge failure. Refreshing an
    identical manifest still updates the cover index and the successful check.
    """
    manifest = validate_manifest(_json(fetch(MANIFEST_PATH), 64 * 1024))
    raw_index = fetch(manifest['cover_index_path'])
    if hashlib.sha256(raw_index).hexdigest() != manifest['cover_index_sha256']:
        raise ValueError('Cover index hash mismatch')
    index = validate_cover_index(_json(raw_index, 32 * 1024 * 1024))
    unchanged = _pref(conn, ONLINE_HASH_KEY) == manifest['db_sha256']
    result = (0, manifest['count'])
    with tempfile.TemporaryDirectory(prefix='dlsite-online-') as directory:
        snapshot = Path(directory) / 'snapshot.db'
        if not unchanged:
            raw = fetch(manifest['db_path'])
            if not isinstance(raw, bytes) or len(raw) > MAX_DB_BYTES or hashlib.sha256(raw).hexdigest() != manifest['db_sha256']:
                raise ValueError('Snapshot hash/size mismatch')
            snapshot.write_bytes(raw)
            validate_snapshot(snapshot, manifest, index)
        conn.execute('SAVEPOINT dlsite_online_pull')
        try:
            if not unchanged:
                result = merge(conn, str(snapshot), 'dlsite')
            _set_pref(conn, ONLINE_HASH_KEY, manifest['db_sha256'])
            _set_pref(conn, INDEX_KEY, json.dumps(index, ensure_ascii=False))
            _set_pref(conn, ONLINE_CHECK_KEY, str(time.time() if now is None else now))
            conn.execute('RELEASE SAVEPOINT dlsite_online_pull')
        except BaseException:
            conn.execute('ROLLBACK TO SAVEPOINT dlsite_online_pull')
            conn.execute('RELEASE SAVEPOINT dlsite_online_pull')
            raise
    source_status=None
    from slg_source_status import STATUS_PATHS,public_status
    if manifest.get('source_status_path')==STATUS_PATHS['dlsite']:
        try:
            source_status=public_status(_json(fetch(STATUS_PATHS['dlsite']),8192),'dlsite')
        except Exception:
            pass
    log('DLsite online: %d works, %d covers'  % (manifest['count'], len(index)))
    return {'catalog_id': 'dlsite', 'new': result[0], 'catalogue': result[1],
            'unchanged': unchanged, 'covers': len(index), 'revision': manifest['revision'], 'source_status':source_status}


class CoverCache:
    """Thread-safe duplicate suppression, bounded caller concurrency and failure backoff."""
    def __init__(self, directory, fetch, clock=time.monotonic):
        self.directory, self.fetch, self.clock = Path(directory), fetch, clock
        self._lock = threading.Lock()
        self._inflight, self._failure = {}, {}
        self._slots = threading.BoundedSemaphore(2)

    def clear_retry(self):
        """An explicit catalogue update permits another visible-cover attempt."""
        with self._lock:
            self._failure.clear()

    def get(self, item):
        match = _COVER.fullmatch(str(item.get('filename', '')))
        if not match:
            raise ValueError('Invalid cover name')
        item = validate_cover_index({'dlsite-' + match[1].lower(): item})['dlsite-' + match[1].lower()]
        filename = item['filename']
        path = self.directory / filename
        with self._lock:
            event = self._inflight.get(filename)
            if event is None:
                retry_at, attempts = self._failure.get(filename, (0, 0))
                if retry_at > self.clock():
                    raise RuntimeError('Cover retry delayed')
                event = self._inflight[filename] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            event.wait(90)
            if path.is_file() and self._valid_cached(path, item):
                return str(path)
            raise RuntimeError('Concurrent cover request failed')
        try:
            if path.is_file() and self._valid_cached(path, item):
                return str(path)
            with self._slots:
                raw = self.fetch('/covers/' + filename)
                if len(raw) != item['bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                    raise ValueError('Cover hash/size mismatch')
                validate_image(raw)
                atomic_bytes(path, raw)
            with self._lock:
                self._failure.pop(filename, None)
            return str(path)
        except Exception:
            with self._lock:
                attempts = self._failure.get(filename, (0, 0))[1] + 1
                self._failure[filename] = (self.clock() + min(3600, 30 * 2 ** min(attempts - 1, 7)), attempts)
            raise
        finally:
            with self._lock:
                self._inflight.pop(filename, None)
                event.set()

    @staticmethod
    def _valid_cached(path, item):
        try:
            if path.stat().st_size != item['bytes']:
                return False
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item['sha256']:
                return False
            validate_image(raw)
            return True
        except Exception:
            return False
