"""Official DLsite image references and an opt-in, local-only artwork cache.

Reference publication carries URLs, never image bytes, and the client fetches
``img.dlsite.jp`` itself: there is no same-server fallback for official art.
The HTTP opener keeps urllib's default ProxyHandler so a configured Windows or
environment proxy (VPN/proxy clients included) is honoured.

Futures finish on the worker: consumers must dispatch Tk updates through their
existing queue. A failed official download leaves a validated previous local
image in place and never deletes the legacy server-cover path.
"""
from collections import OrderedDict
from concurrent.futures import Future, CancelledError
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from slg_dlsite_online import atomic_bytes, validate_image, MAX_IMAGE_BYTES

REFERENCE_PREF_KEY = 'catalogue.online.dlsite.official_cover_index'
REFERENCE_HASH_PREF_KEY = 'catalogue.online.dlsite.official_cover_hash'
MAX_INDEX_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SLUG = re.compile(r'dlsite-(?:rj|vj)[0-9]{6,10}\Z')
_WORK = re.compile(r'(?:RJ|VJ)[0-9]{6,10}\Z', re.I)
# The work number must appear as a token in the path, not as a substring of a
# different identity. img.dlsite.jp never needs a query string for artwork.
_WORK_IN_PATH = re.compile(r'(?<![A-Za-z0-9])((?:RJ|VJ)[0-9]{6,10})(?![0-9])', re.I)
_IMAGE_PATH = re.compile(
    r'/modpub/images2/work/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+\.(?:jpg|jpeg|png|webp)\Z')


def official_url(url, work_id=None):
    """Reject credentials, foreign hosts, aliases and guessed identities."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('Invalid official image URL')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != 'img.dlsite.jp'
                or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError('Invalid official image route')
        path = parsed.path
        if (not _IMAGE_PATH.fullmatch(path) or '\\' in path
                or any(segment in ('.', '..') for segment in path.split('/'))):
            raise ValueError('Invalid official image route')
        found = {match.group(1).upper() for match in _WORK_IN_PATH.finditer(path)}
        if not found:
            raise ValueError('Official image has no work identity')
        if work_id is not None:
            if not isinstance(work_id, str) or not _WORK.fullmatch(work_id):
                raise ValueError('Invalid work identity')
            if found != {work_id.upper()}:
                raise ValueError('Image URL/work identity mismatch')
        elif len(found) != 1:
            raise ValueError('Ambiguous work identity')
    except (TypeError, ValueError) as error:
        raise ValueError('Invalid official image URL') from error
    return url


def validate_reference(value, slug=None):
    """One official reference is exactly the URL for its work."""
    if not isinstance(value, dict) or not isinstance(value.get('url'), str):
        raise ValueError('Invalid official image reference')
    work_id = None
    if slug is not None:
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ValueError('Invalid DLsite work')
        work_id = slug.split('-', 1)[1].upper()
    return {'url': official_url(value['url'], work_id)}


def validate_official_index(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('catalog_id') != 'dlsite'
            or not isinstance(value.get('items'), dict) or len(value['items']) > 2_000_000):
        raise ValueError('Invalid official reference index')
    result = {}
    for slug, item in value['items'].items():
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ValueError('Invalid reference work')
        result[slug] = validate_reference(item, slug)
    return {'version': 1, 'catalog_id': 'dlsite', 'items': result}


def load_official_index(raw, digest):
    if (not isinstance(raw, bytes) or len(raw) > MAX_INDEX_BYTES
            or not _HASH.fullmatch(str(digest))
            or hashlib.sha256(raw).hexdigest() != digest):
        raise ValueError('Official reference hash/size mismatch')
    return validate_official_index(json.loads(raw.decode('utf-8')))


def _cache_key(slug, reference, allow_network=True):
    return hashlib.sha256((slug + '\n' + reference['url']
                           + ('' if allow_network else '\ncache-only')).encode()).hexdigest()


class ImageHTTPError(RuntimeError):
    def __init__(self, status, retry_after=0):
        self.status, self.retry_after = status, retry_after
        super().__init__('Official image HTTP %s' % status)


class _Redirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            original = next(_WORK_IN_PATH.finditer(urlsplit(request.full_url).path)).group(1)
        except StopIteration:
            raise ValueError('Redirect has no work identity')
        official_url(newurl, original)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch_official_image(url, cancel):
    """One bounded HTTP fetch through the system proxy (urllib default)."""
    if cancel.is_set():
        raise CancelledError()
    official_url(url)
    request = Request(url, headers={
        'User-Agent': 'SLGking/0.29 (+https://slg-king.com)',
        'Accept': 'image/jpeg,image/png,image/webp'})
    # Default build_opener installs ProxyHandler (Windows registry / env), which
    # is exactly what mainland users behind a local proxy or VPN need. Do not
    # pass an empty proxy map or trust_env=False here.
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

    ``consumer`` is an opaque, hashable card/page generation token. request
    returns a Future[str|None]; no Tk method or image binding happens here.
    On a changed URL's failed download a validated previous local image
    survives. ``allow_network=False`` reads only the local cache.
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
                            if (key == 'entries' and _SLUG.fullmatch(str(name))
                                    and _HASH.fullmatch(str(item.get('sha256', '')))
                                    and _HASH.fullmatch(str(item.get('key', '')))):
                                self._state[key][name] = item
                            elif (key == 'failures' and _HASH.fullmatch(str(name))
                                  and type(item.get('attempts')) is int
                                  and 0 <= item['attempts'] <= 1000000):
                                self._state[key][name] = item
                retry = value.get('host_retry_at')
                if type(retry) in (int, float) and math.isfinite(retry) and retry >= 0:
                    self._state['host_retry_at'] = retry
        except (OSError, ValueError):
            pass
        self._thread = threading.Thread(target=self._run, name='dlsite-official-images', daemon=True)
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

    def request(self, slug, reference, *, allow_network=True, consumer=None):
        future = Future()
        if not _SLUG.fullmatch(str(slug)):
            raise ValueError('Invalid DLsite work')
        reference = validate_reference(reference, slug)
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
        """Memory-only association check, not an on-disk integrity assertion."""
        if not _SLUG.fullmatch(str(slug)):
            return False
        reference = validate_reference(reference, slug)
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
            raw = self.fetch(task.reference['url'], task.cancel)
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
