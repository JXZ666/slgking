"""Official VNDB image references and an opt-in, local-only artwork cache.

Reference publication carries URLs/facts, never image bytes. Futures finish on
the worker: consumers must dispatch Tk updates through their existing queue.
"""
from collections import OrderedDict
from concurrent.futures import Future, CancelledError
from contextlib import closing
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from slg_vndb_online import atomic_bytes, validate_image, MAX_IMAGE_BYTES

REFERENCE_PREF_KEY = 'catalogue.online.vndb.official_cover_index'
REFERENCE_HASH_PREF_KEY = 'catalogue.online.vndb.official_cover_hash'
MAX_INDEX_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SLUG = re.compile(r'vndb-v[1-9]\d*\Z')
_IMAGE = re.compile(r'cv([1-9]\d*)\Z')


def official_url(url, image_id=None):
    """Reject credentials, foreign redirects, aliases and guessed identities."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('Invalid official image URL')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.netloc in ('t.vndb.org', 't.vndb.org:443')
                 and not parsed.query and not parsed.fragment)
        match = re.fullmatch(r'/(?:cv|cv\.t)/[0-9]{2}/([1-9]\d*)\.(?:jpg|png|webp)', parsed.path)
        if not valid or not match:
            raise ValueError('Invalid official image route')
        if image_id is not None and (not _IMAGE.fullmatch(image_id) or image_id[2:] != match[1]):
            raise ValueError('Image URL/identity mismatch')
    except (TypeError, ValueError) as error:
        raise ValueError('Invalid official image URL') from error
    return url


def _dims(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(n) is not int or not 1 <= n <= 100000 for n in value)):
        raise ValueError('Invalid image dimensions')
    return list(value)


def validate_reference(value):
    if not isinstance(value, dict) or not _IMAGE.fullmatch(str(value.get('image_id', ''))):
        raise ValueError('Invalid image identity')
    result = {'image_id': value['image_id'], 'url': official_url(value.get('url'), value['image_id']),
              'dims': _dims(value.get('dims'))}
    for key in ('sexual', 'violence'):
        number = value.get(key)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 2):
            raise ValueError('Invalid image content mark')
        result[key] = number
    if value.get('thumbnail') is not None:
        result['thumbnail'] = official_url(value['thumbnail'], value['image_id'])
        result['thumbnail_dims'] = _dims(value.get('thumbnail_dims'))
    if value.get('votecount') is not None:
        if type(value['votecount']) is not int or value['votecount'] < 0:
            raise ValueError('Invalid image vote count')
        result['votecount'] = value['votecount']
    return result


def validate_official_index(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1 or value.get('catalog_id') != 'vndb'
            or not isinstance(value.get('items'), dict) or len(value['items']) > 200000):
        raise ValueError('Invalid official reference index')
    result = {}
    for slug, item in value['items'].items():
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ValueError('Invalid reference work')
        result[slug] = validate_reference(item)
    return {'version': 1, 'catalog_id': 'vndb', 'items': result}


def load_official_index(raw, digest):
    if (not isinstance(raw, bytes) or len(raw) > MAX_INDEX_BYTES or not _HASH.fullmatch(str(digest))
            or hashlib.sha256(raw).hexdigest() != digest):
        raise ValueError('Official reference hash/size mismatch')
    return validate_official_index(json.loads(raw.decode('utf-8')))


def reference_hidden(reference, enabled):
    """Unknown marks are not silently considered safe. This precedes cache/HTTP."""
    return bool(enabled and any(reference.get(key) is None or reference[key] > 0
                                for key in ('sexual', 'violence')))


def _cache_key(slug, reference, allow_network=True):
    url = reference.get('thumbnail') or reference['url']
    return hashlib.sha256((slug + '\n' + reference['image_id'] + '\n' + url
                           + ('' if allow_network else '\ncache-only')).encode()).hexdigest()


def publish_references(ledger_path, catalogue_root, output):
    """Copy current immutable metadata and add references for its exact work set.

    Does not rebuild SQLite (so db SHA is preserved), download images, or alter
    the old cover index. catalogue_root/output point at catalogues/vndb itself.
    """
    root, target = Path(catalogue_root), Path(output)
    manifest_raw = (root / 'manifest.json').read_bytes()
    from slg_vndb_online import validate_manifest, validate_snapshot, validate_cover_index
    manifest = validate_manifest(json.loads(manifest_raw))
    snapshot = root / 'snapshots' / (manifest['db_sha256'] + '.db')
    index_raw = (root / 'cover-index' / (manifest['cover_index_sha256'] + '.json')).read_bytes()
    if hashlib.sha256(index_raw).hexdigest() != manifest['cover_index_sha256']:
        raise ValueError('Old cover index hash mismatch')
    index = validate_cover_index(json.loads(index_raw))
    validate_snapshot(snapshot, manifest, index)
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        targets = {row[0] for row in conn.execute('SELECT slug FROM games')}
    references = {}
    with closing(sqlite3.connect(Path(ledger_path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        for vid, raw in conn.execute('SELECT id,raw_json FROM facts ORDER BY id'):
            slug = 'vndb-' + vid
            if slug not in targets:
                continue
            image = json.loads(raw).get('image')
            if image:
                references[slug] = validate_reference(dict(image, image_id=image.get('id')))
    value = validate_official_index({'version': 1, 'catalog_id': 'vndb', 'items': references})
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    # Only explicitly named catalogue artifacts, never arbitrary recursive copy.
    for folder, name in [('snapshots', snapshot.name), ('cover-index', manifest['cover_index_sha256'] + '.json')]:
        atomic_bytes(target / folder / name, (root / folder / name).read_bytes())
    for filename in ('LICENSE.txt', 'licenses/README.txt', 'licenses/LICENSE-ODBL.txt', 'licenses/LICENSE-DBCL.txt'):
        atomic_bytes(target / filename, (root / filename).read_bytes())
    atomic_bytes(target / 'official-cover-index' / (digest + '.json'), raw)
    manifest.update(official_cover_index_path='/catalogues/vndb/official-cover-index/' + digest + '.json',
                    official_cover_index_sha256=digest, official_cover_delivery='client-direct')
    atomic_bytes(target / 'manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    return manifest


class ImageHTTPError(RuntimeError):
    def __init__(self, status, retry_after=0):
        self.status, self.retry_after = status, retry_after
        super().__init__('Official image HTTP %s' % status)


class _Redirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        original_id = 'cv' + re.search(r'/([1-9]\d*)\.(?:jpg|png|webp)$', urlsplit(request.full_url).path)[1]
        official_url(newurl, original_id)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch_official_image(url, cancel):
    """One bounded HTTP fetch, without changing machine proxy configuration."""
    if cancel.is_set():
        raise CancelledError()
    official_url(url)
    request = Request(url, headers={'User-Agent': 'SLGking/0.29 (+https://slg-king.com)', 'Accept': 'image/jpeg,image/png,image/webp'})
    # Each request owns its opener; no global urllib installation or cookies.
    opener = build_opener(_Redirects())
    deadline = time.monotonic() + 60
    try:
        with opener.open(request, timeout=20) as response:
            official_url(response.geturl())
            if response.status != 200:
                raise ImageHTTPError(response.status)
            if response.headers.get_content_type() not in ('image/jpeg', 'image/png', 'image/webp'):
                raise ValueError('Official response is not an image')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > MAX_IMAGE_BYTES):
                raise ValueError('Official image exceeds byte limit')
            chunks, size = [], 0
            while True:
                if cancel.is_set() or time.monotonic() >= deadline:
                    raise CancelledError()
                chunk = response.read(min(65536, MAX_IMAGE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError('Official image exceeds byte limit')
            return b''.join(chunks)
    except HTTPError as error:
        wait = error.headers.get('Retry-After', '')
        try:
            delay = float(wait) if wait.isdecimal() else parsedate_to_datetime(wait).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            delay = 0
        raise ImageHTTPError(error.code, max(0, delay)) from error


@dataclass
class _Task:
    slug: str
    reference: dict
    key: str
    allow_network: bool = True
    cancel: threading.Event = field(default_factory=threading.Event)
    consumers: dict = field(default_factory=dict)


class OfficialImageCache:
    """One worker, at most 32 waiting tasks, cancellation and persistent backoff.

    Consumer is an opaque, hashable page/card generation token. request returns
    a Future[str|None]; no Tk method or image binding is performed here. On a
    changed image's failed download a validated previous local image survives.
    """
    def __init__(self, directory, fetch=fetch_official_image, clock=time.time,
                 max_pending=32, max_bytes=256 * 1024 * 1024):
        self.directory, self.fetch, self.clock = Path(directory), fetch, clock
        self.max_pending, self.max_bytes = max_pending, max_bytes
        self._condition = threading.Condition(threading.RLock())
        self._waiting, self._checks, self._tasks = OrderedDict(), OrderedDict(), {}
        self._closed = False
        self._state = {'entries': {}, 'failures': {}, 'host_retry_at': 0}
        try:
            state_path = self.directory / 'state.json'
            if state_path.is_symlink() or state_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('Invalid local cache state')
            value = json.loads(state_path.read_text('utf-8'))
            if isinstance(value, dict):
                for key in ('entries', 'failures'):
                    if isinstance(value.get(key), dict) and len(value[key]) <= 512:
                        for name, item in value[key].items():
                            if not isinstance(item, dict):
                                continue
                            numeric = item.get('at')
                            if type(numeric) not in (int, float) or not math.isfinite(numeric) or numeric < 0:
                                continue
                            if key == 'entries' and _SLUG.fullmatch(str(name)) and _HASH.fullmatch(str(item.get('sha256', ''))) and _HASH.fullmatch(str(item.get('key', ''))):
                                self._state[key][name] = item
                            elif key == 'failures' and _HASH.fullmatch(str(name)) and type(item.get('attempts')) is int and 0 <= item['attempts'] <= 1000000:
                                self._state[key][name] = item
                retry = value.get('host_retry_at')
                if type(retry) in (int, float) and math.isfinite(retry) and retry >= 0:
                    self._state['host_retry_at'] = retry
        except (OSError, ValueError):
            pass
        self._thread = threading.Thread(target=self._run, name='vndb-official-images', daemon=True)
        self._thread.start()

    def _save(self):
        atomic_bytes(self.directory / 'state.json', json.dumps(self._state, sort_keys=True).encode())

    def _previous(self, slug):
        entry = self._state['entries'].get(slug)
        if not isinstance(entry, dict) or not _HASH.fullmatch(str(entry.get('sha256', ''))):
            return None
        path = self.directory / (entry['sha256'] + '.img')
        try:
            if path.is_symlink() or path.stat().st_size > MAX_IMAGE_BYTES:
                return None
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() == entry['sha256']:
                validate_image(raw)
                return str(path)
        except (OSError, ValueError):
            pass
        return None

    def request(self, slug, reference, *, hide_sensitive=True, personal_cover=None, consumer=None, allow_network=True):
        future = Future()
        if personal_cover:
            future.set_result(str(personal_cover))
            return future
        if not _SLUG.fullmatch(str(slug)):
            raise ValueError('Invalid VNDB work')
        reference = validate_reference(reference)
        if reference_hidden(reference, hide_sensitive):
            future.set_result(None)
            return future
        key = _cache_key(slug, reference, allow_network)
        with self._condition:
            if self._closed:
                future.cancel()
                return future
            task = self._tasks.get(key)
            if task is not None and not task.cancel.is_set():
                for existing, owner in task.consumers.items():
                    if owner == consumer and not existing.cancelled():
                        return existing
            if task is None or task.cancel.is_set():
                if len(self._waiting) + len(self._checks) >= self.max_pending:
                    future.set_exception(RuntimeError('Official image queue is full'))
                    return future
                task = _Task(slug, reference, key, bool(allow_network))
                self._tasks[key] = task
                (self._waiting if allow_network else self._checks)[key] = task
            task.consumers[future] = consumer
            def cancelled(done):
                if done.cancelled():
                    with self._condition:
                        task.consumers.pop(done, None)
                        if not task.consumers:
                            task.cancel.set()
            future.add_done_callback(cancelled)
            self._condition.notify()
        return future

    def cancel_consumer(self, consumer):
        with self._condition:
            for task in list(self._tasks.values()):
                for future, owner in list(task.consumers.items()):
                    if owner == consumer:
                        future.cancel()
                        task.consumers.pop(future, None)
                if not task.consumers:
                    task.cancel.set()
                    if task.key in self._waiting or task.key in self._checks:
                        self._waiting.pop(task.key, None)
                        self._checks.pop(task.key, None)
                        self._tasks.pop(task.key, None)

    def close(self, wait=False):
        with self._condition:
            self._closed = True
            for task in list(self._tasks.values()):
                task.cancel.set()
                for future in list(task.consumers):
                    future.cancel()
            self._waiting.clear()
            self._checks.clear()
            self._tasks.clear()
            self._condition.notify_all()
        if wait:
            self._thread.join(timeout=25)

    def status(self):
        with self._condition:
            return {'waiting': len(self._waiting), 'cache_checks': len(self._checks),
                    'inflight': len(self._tasks) - len(self._waiting) - len(self._checks),
                    'failures': len(self._state['failures']), 'host_retry_at': self._state['host_retry_at']}

    def is_current(self, slug, reference):
        """Memory-only association check, not an on-disk integrity assertion.

        False after fallback to a previous image: a GUI must not permanently
        memoize that image as the new reference's successful CTkImage.
        """
        if not _SLUG.fullmatch(str(slug)):
            return False
        reference = validate_reference(reference)
        with self._condition:
            entry = self._state['entries'].get(slug, {})
            return isinstance(entry, dict) and entry.get('key') == _cache_key(slug, reference)

    def _download(self, task):
        with self._condition:
            old = self._previous(task.slug)
            if not task.allow_network:
                return old
            entry = self._state['entries'].get(task.slug, {})
            if old and entry.get('key') == task.key:
                return old
            failure = self._state['failures'].get(task.key, {})
            retry_at = max(float(failure.get('at', 0)), float(self._state.get('host_retry_at', 0)))
            if retry_at > self.clock():
                if old:
                    return old
                raise RuntimeError('Official image retry delayed')
        try:
            if task.cancel.is_set():
                raise CancelledError()
            raw = self.fetch(task.reference.get('thumbnail') or task.reference['url'], task.cancel)
            validate_image(raw)
            if len(raw) > self.max_bytes:
                raise ValueError('Image exceeds local cache capacity')
            if task.cancel.is_set():
                raise CancelledError()
            digest = hashlib.sha256(raw).hexdigest()
            path = self.directory / (digest + '.img')
            with self._condition:
                if task.cancel.is_set():
                    raise CancelledError()
                atomic_bytes(path, raw)
                self._state['entries'][task.slug] = {'sha256': digest, 'key': task.key, 'at': self.clock()}
                self._state['failures'].pop(task.key, None)
                self._prune()
                try:
                    self._save()
                except OSError:
                    pass  # Disk failure must not discard an existing usable cover.
            return str(path)
        except CancelledError:
            raise
        except Exception as error:
            with self._condition:
                previous = self._state['failures'].get(task.key, {})
                attempts = int(previous.get('attempts', 0)) + 1
                delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
                if isinstance(error, ImageHTTPError) and error.status in (403, 429):
                    delay = max(delay, 3600, error.retry_after)
                    self._state['host_retry_at'] = self.clock() + delay
                self._state['failures'][task.key] = {'attempts': attempts, 'at': self.clock() + delay}
                if len(self._state['failures']) > 512:
                    oldest = min(self._state['failures'], key=lambda k: self._state['failures'][k]['at'])
                    self._state['failures'].pop(oldest)
                try:
                    self._save()
                except OSError:
                    pass
            if old:
                return old
            raise

    def _prune(self):
        entries = self._state['entries']
        ordered = sorted(entries, key=lambda slug: entries[slug].get('at', 0), reverse=True)
        keep, total = set(), 0
        for slug in ordered:
            digest = entries[slug].get('sha256', '')
            path = self.directory / (digest + '.img')
            size = path.stat().st_size if _HASH.fullmatch(digest) and path.is_file() else 0
            if len(keep) < 512 and total + size <= self.max_bytes:
                keep.add(digest)
                total += size
            else:
                entries.pop(slug, None)
        for path in self.directory.glob('*.img'):
            if _HASH.fullmatch(path.stem) and path.stem not in keep:
                path.unlink(missing_ok=True)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._checks or self._waiting)
                if self._closed:
                    return
                key, task = (self._checks if self._checks else self._waiting).popitem(last=False)
            try:
                result, error = self._download(task), None
            except BaseException as caught:
                result, error = None, caught
            with self._condition:
                if self._tasks.get(key) is task:
                    self._tasks.pop(key, None)
                consumers = list(task.consumers)
            for future in consumers:
                if not future.cancelled():
                    try:
                        future.set_exception(error) if error else future.set_result(result)
                    except Exception:
                        pass  # A consumer may cancel between this check and completion.
