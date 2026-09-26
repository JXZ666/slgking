"""Resolve the newest published stable release for server-side dashboards.

GitHub's ``releases/latest`` endpoint excludes drafts and prereleases. Results
are cached in memory to avoid a request per dashboard refresh. If GitHub is
temporarily unavailable, the last successful value or the configured fallback
is used; no release data is persisted to disk.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen


REPO = "JXZ666/slgking"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
LATEST_URL = "https://github.com/%s/releases/latest" % REPO
DEFAULT_FALLBACK = "0.23.8"
CACHE_SECONDS = 30 * 60
RETRY_SECONDS = 5 * 60
REQUEST_TIMEOUT = 5

_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$", re.IGNORECASE)
_LOCK = threading.Lock()
_CACHED = None
_CACHED_AT = 0.0
_LAST_ATTEMPT = 0.0


def _utc_now():
    return (datetime.now(timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


def _normalize_tag(tag):
    match = _TAG_RE.fullmatch(str(tag or "").strip())
    return match.group(1) if match else None


def _release_info(version, source):
    return {
        "version": version,
        "checked_at": _utc_now(),
        "source": source,
        "fallback": False,
    }


def _fetch_latest_api():
    request = Request(API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "SLGking-admin-release-check",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        data = json.loads(response.read(65536).decode("utf-8"))
    if not isinstance(data, dict) or data.get("draft") is True or data.get("prerelease") is True:
        raise ValueError("GitHub did not return a stable release")
    version = _normalize_tag(data.get("tag_name"))
    if not version:
        raise ValueError("GitHub release tag is not a stable semantic version")
    return _release_info(version, "github-api")


def _fetch_latest_page():
    request = Request(LATEST_URL, headers={
        "User-Agent": "SLGking-admin-release-check",
        "Accept": "text/html",
    })
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        final = urlparse(response.geturl())
    parts = final.path.strip("/").split("/")
    if (final.netloc.lower() != "github.com"
            or len(parts) != 5
            or parts[0].lower() != REPO.split("/")[0].lower()
            or parts[1].lower() != REPO.split("/")[1].lower()
            or parts[2:4] != ["releases", "tag"]):
        raise ValueError("GitHub latest-release page did not redirect to a release tag")
    version = _normalize_tag(parts[4])
    if not version:
        raise ValueError("GitHub latest-release page returned a non-stable tag")
    return _release_info(version, "github-release-page")


def _fetch_latest():
    """Try the API first, then use GitHub's canonical latest-release redirect."""
    try:
        return _fetch_latest_api()
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError):
        return _fetch_latest_page()


def _fallback_info(fallback=None):
    version = _normalize_tag(fallback or os.environ.get(
        "SLGKING_STABLE_VERSION", DEFAULT_FALLBACK))
    return {
        "version": version or DEFAULT_FALLBACK,
        "checked_at": _utc_now(),
        "source": "configured-fallback",
        "fallback": True,
    }


def get_stable_release(fallback=None):
    """Return a small, JSON-safe stable-release record.

    The GitHub lookup happens at most once per cache period. When it fails,
    retries are throttled for five minutes and the last known good release is
    retained in memory. ``fallback`` is primarily useful for isolated tests;
    production uses SLGKING_STABLE_VERSION and then DEFAULT_FALLBACK.
    """
    global _CACHED, _CACHED_AT, _LAST_ATTEMPT
    now = time.monotonic()
    with _LOCK:
        if (_CACHED and _CACHED.get("source", "").startswith("github")
                and now - _CACHED_AT < CACHE_SECONDS):
            return dict(_CACHED)
        if _LAST_ATTEMPT and now - _LAST_ATTEMPT < RETRY_SECONDS:
            if _CACHED:
                result = dict(_CACHED)
                if result.get("source", "").startswith("github"):
                    result["source"] = "last-known-good"
                    result["fallback"] = True
                return result
            return _fallback_info(fallback)

        _LAST_ATTEMPT = now
        try:
            _CACHED = _fetch_latest()
            _CACHED_AT = now
            return dict(_CACHED)
        except (OSError, ValueError, TimeoutError, json.JSONDecodeError):
            if _CACHED:
                result = dict(_CACHED)
                if result.get("source", "").startswith("github"):
                    result["source"] = "last-known-good"
                    result["fallback"] = True
                return result
            result = _fallback_info(fallback)
            _CACHED = dict(result)
            _CACHED_AT = 0.0
            return result


def _reset_cache_for_tests():
    """Clear process cache; private helper used only by unit tests."""
    global _CACHED, _CACHED_AT, _LAST_ATTEMPT
    with _LOCK:
        _CACHED = None
        _CACHED_AT = 0.0
        _LAST_ATTEMPT = 0.0
