"""Read a bounded model catalogue from the user's current API endpoint."""

from dataclasses import dataclass
import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MODELS = 500
TIMEOUT = 8
_NETWORK_SLOTS = threading.BoundedSemaphore(2)
_AUTHORITY = re.compile(r"(?:\[[0-9a-f:.]+\]|[a-z0-9._-]+)(?::[0-9]+)?", re.I | re.ASCII)
_PATH = re.compile(r"(?:/[a-zA-Z0-9._~-]+)*")
_MODALITIES = ("text", "image", "audio", "video", "file")


class ModelListError(Exception):
    """Carry a local error code and an optional numeric HTTP status."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ModelOption:
    id: str
    input_modalities: tuple[str, ...]


@dataclass(frozen=True)
class ModelList:
    options: tuple[ModelOption, ...]
    truncated: bool


def models_endpoint(base):
    """Normalize the API base without changing its path or accepting URL extras."""
    try:
        if not isinstance(base, str) or len(base) > 2048:
            raise ValueError()
        base = base.strip(" ")
        if any(c.isspace() or ord(c) < 32 for c in base) or "?" in base or "#" in base:
            raise ValueError()
        parsed = urllib.parse.urlsplit(base)
        if not _AUTHORITY.fullmatch(parsed.netloc) or parsed.port == 0:
            raise ValueError()
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        loopback = host == "localhost" or (address is not None and address.is_loopback)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError()
        path = parsed.path.removesuffix("/")
        if not _PATH.fullmatch(path) or {".", ".."}.intersection(path.split("/")):
            raise ValueError()
        authority = f"[{address.compressed}]" if isinstance(address, ipaddress.IPv6Address) else host
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port not in (None, default_port):
            authority += f":{parsed.port}"
        return f"{parsed.scheme}://{authority}{path}/models"
    except (TypeError, ValueError):
        raise ModelListError("endpoint") from None


def model_list_request(base, key):
    """Build one GET and return its credential fingerprint for in-memory caching."""
    endpoint = models_endpoint(base)
    key = key.strip(" ") if isinstance(key, str) else ""
    if not re.fullmatch(r"[!-~]{1,8192}", key):
        raise ModelListError("key")
    request = urllib.request.Request(endpoint, method="GET", headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    })
    return request, hashlib.sha256(key.encode("ascii")).digest()


class _ReturnStatus(urllib.request.HTTPErrorProcessor):
    # Prevent urllib from dispatching status codes to redirect or auth handlers.
    def http_response(self, request, response):
        return response

    https_response = http_response


class _LocalhostConnection(http.client.HTTPConnection):
    def __init__(self, host, **kwargs):
        super().__init__(host, **kwargs)
        self._create_connection = self._connect_loopback

    @staticmethod
    def _connect_loopback(address, timeout, source_address):
        # Preserve Host: localhost while keeping plaintext traffic on loopback.
        try:
            return socket.create_connection(("127.0.0.1", address[1]), timeout, source_address)
        except OSError:
            return socket.create_connection(("::1", address[1]), timeout, source_address)


class _LocalhostHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_LocalhostConnection, request)


def _read_models(request):
    handlers = [_ReturnStatus()]
    if request.type == "http":
        handlers.append(urllib.request.ProxyHandler({}))
        if urllib.parse.urlsplit(request.full_url).hostname == "localhost":
            handlers.append(_LocalhostHandler())
    opener = urllib.request.build_opener(*handlers)
    with opener.open(request, timeout=TIMEOUT) as response:
        if response.status == 204:
            raise ModelListError("empty")
        if not 200 <= response.status < 300:
            raise ModelListError("http", response.status)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelListError("oversize")
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(MAX_RESPONSE_BYTES + 1)
        except (EOFError, OSError):
            raise ModelListError("invalid") from None
    elif encoding not in ("", "identity"):
        raise ModelListError("invalid")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelListError("oversize")
    return raw


def _declared_inputs(row):
    architecture = row.get("architecture")
    sources = [row.get("input_modalities")]
    if isinstance(architecture, dict):
        sources.append(architecture.get("input_modalities"))
    declarations = [tuple(kind for kind in _MODALITIES if kind in source)
                    for source in sources if isinstance(source, list)]
    if declarations and all(item == declarations[0] for item in declarations):
        return declarations[0]
    return ()


def _parse_models(raw):
    try:
        envelope = json.loads(raw.decode("utf-8"))
        rows = envelope["data"]
        if not isinstance(rows, list):
            raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ModelListError("invalid") from None
    options = {}
    for row in rows:
        model = row.get("id") if isinstance(row, dict) else None
        if (not isinstance(model, str) or not 0 < len(model) <= 256 or model != model.strip()
                or not model.isprintable() or model in options):
            continue
        if len(options) == MAX_MODELS:
            return ModelList(tuple(options.values()), truncated=True)
        options[model] = ModelOption(model, _declared_inputs(row))
    if not options:
        raise ModelListError("empty")
    return ModelList(tuple(options.values()), truncated=False)


class ModelListJob:
    """One daemon transport, with a UI deadline and no queue of waiting requests."""

    def __init__(self, request):
        self._result = queue.Queue(maxsize=1)
        self._cancelled = threading.Event()
        self._deadline = time.monotonic() + TIMEOUT
        if not _NETWORK_SLOTS.acquire(blocking=False):
            raise ModelListError("busy")
        try:
            threading.Thread(target=self._run, args=(request,), name="slg-model-list", daemon=True).start()
        except Exception:
            _NETWORK_SLOTS.release()
            raise ModelListError("network") from None

    def _run(self, request):
        try:
            if self._cancelled.is_set():
                return
            if time.monotonic() >= self._deadline:
                raise ModelListError("timeout")
            raw = _read_models(request)
            if self._cancelled.is_set():
                return
            result = _parse_models(raw)
        except ModelListError as error:
            result = ModelListError(error.code, error.status)
        except urllib.error.HTTPError as error:
            error.close()
            result = ModelListError("http", error.code)
        except (TimeoutError, urllib.error.URLError) as error:
            timed_out = isinstance(error, TimeoutError) or isinstance(error.reason, TimeoutError)
            result = ModelListError("timeout" if timed_out else "network")
        except Exception:
            result = ModelListError("network")
        finally:
            # A cancelled/expired DNS worker still holds its slot until it exits.
            _NETWORK_SLOTS.release()
        if not self._cancelled.is_set():
            if time.monotonic() >= self._deadline:
                result = ModelListError("timeout")
            self._result.put_nowait(result)

    def cancel(self):
        self._cancelled.set()

    def poll(self):
        try:
            result = self._result.get_nowait()
        except queue.Empty:
            if time.monotonic() >= self._deadline:
                self.cancel()
                raise ModelListError("timeout") from None
            return None
        if isinstance(result, ModelListError):
            raise result
        return result
