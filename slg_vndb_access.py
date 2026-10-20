"""VNDB compatibility gate; headers are not proof of user identity.

Call before serving GET/HEAD, including conditional 304 responses. The caller
must set RESPONSE_HEADERS on every protected success and error response and
must not expose these resources through an unguarded static/CDN origin.
"""
from dataclasses import dataclass
import posixpath
import re
from urllib.parse import unquote, urlsplit

RESPONSE_HEADERS = {'Cache-Control': 'private, no-store', 'Vary': 'X-SLGking-Product, X-SLGking-Platform, X-SLGking-Version, X-SLGking-Channel'}
VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z', re.ASCII)


def asset_kind(target):
    if not isinstance(target, str):
        return None
    try:
        path = urlsplit(target).path
    except ValueError:
        return None
    # The service decodes URL paths and reduces cover paths to their basename;
    # recognize these aliases before the filesystem route or cache can see them.
    for _ in range(4):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    path = path.replace('\\', '/')
    lower = path.lower()
    normal = posixpath.normpath(lower)
    if (lower == '/catalogues/vndb' or lower.startswith('/catalogues/vndb/')
            or normal == '/catalogues/vndb' or normal.startswith('/catalogues/vndb/')):
        return 'catalogue'
    if (lower.startswith('/covers/') or normal.startswith('/covers/')) and posixpath.basename(normal).startswith('vndb-'):
        return 'cover'
    return None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    version: tuple = ()
    channel: str = ''


def decision(headers):
    # Accept HTTPMessage or a plain mapping, and reject duplicate identity
    # headers rather than trusting a proxy-dependent choice of the first one.
    normalized = {}
    if not hasattr(headers, 'items'):
        return Decision(False, 'missing_headers')
    for name, value in headers.items():
        name = str(name).lower()
        if name.startswith('x-slgking-') and name in normalized:
            return Decision(False, 'duplicate_header')
        normalized[name] = value
    def field(name):
        value = normalized.get('x-slgking-' + name)
        return value if isinstance(value, str) else ''
    if field('product') != 'SLGking' or field('platform') != 'windows':
        return Decision(False, 'unsupported_client')
    version, channel = field('version'), field('channel')
    if len(version) > 32 or not VERSION.fullmatch(version):
        return Decision(False, 'invalid_version')
    parts = tuple(map(int, version.split('.')))
    allowed = ((channel == 'test' and parts >= (0,28,7))
               or (channel == 'stable' and parts >= (0,29,0)))
    return Decision(allowed, 'allowed' if allowed else 'unsupported_release', parts, channel)


def gate(headers):
    return decision(headers).allowed
