"""Is there a newer slgking on GitHub?

The app ships as a GitHub Release asset, so "is there a new version" is one
API call and one comparison. Kept out of slg_gui entirely: the version parsing
and the once-a-day gate are the parts that can be wrong, and neither needs a
window to be tested.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta

import slg_db
import slg_scrape

REPO = "JXZ666/slgking"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_URL = "https://github.com/%s/releases/latest" % REPO

# Once a day is enough for a project that cuts a release every few days, and
# it keeps the app well under GitHub's 60-requests-an-hour anonymous limit.
CHECK_EVERY = timedelta(hours=24)
PREF_CHECKED_AT = "update_checked_at"

_VERSION = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", re.I)


def parse_version(text):
    """'v0.14.0' -> (0, 14, 0). None when it does not start with a version.

    Trailing junk is dropped rather than rejected: a tag like 'v0.14.0-beta'
    still answers "am I behind", and refusing to parse it would mean the user
    never hears about that release at all. A missing minor or patch reads as
    zero, so 'v1' and 'v1.0.0' compare equal instead of one of them failing.
    """
    if not text:
        return None
    match = _VERSION.match(str(text).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


def is_newer(remote, local):
    """True when the tag `remote` names a later release than `local`."""
    theirs, ours = parse_version(remote), parse_version(local)
    if theirs is None or ours is None:
        return False
    return theirs > ours


def latest_release(timeout=8, want=None):
    """The newest release's tag and page, or None.

    Every failure returns None: no network, a proxy, GitHub's rate limit, or a
    body that is not the JSON we expect. A version check is never worth a
    traceback, and this one runs unattended three seconds after startup. The
    timeout is deliberately short for the same reason - the user did not ask
    for this call and should never wait on it.

    `want` is threaded to _match_asset so a caller whose running executable is
    not the download name (Android) can name the asset itself.
    """
    try:
        raw = slg_scrape.http_get(API, timeout=timeout)
        data = json.loads(raw.decode("utf-8", "replace"))
        tag = data.get("tag_name") or ""
        if parse_version(tag) is None:
            return None
        result = {"version": tag,
                "url": data.get("html_url") or RELEASES_URL,
                "name": data.get("name") or tag,
                "body": data.get("body") or "",
                "asset_url": _match_asset(data.get("assets") or [], want)}
        if want == ".apk":
            result["release_tag"] = tag
            apks = [a for a in data.get("assets", []) if (a.get("name") or "").endswith(".apk")]
            if len(apks) > 1:
                result["asset_url"] = None
                result["asset_error"] = "发行页有多个 APK，请到发布页选择正确安装包"
            elif not apks:
                result["asset_error"] = "当前发行尚未提供 Android APK"
            else:
                match = re.fullmatch(r"slgking-android-(.+)\.apk", apks[0].get("name") or "")
                version = match.group(1) if match else None
                if version and android_version(version) is not None:
                    result["version"] = version
                else:
                    result["asset_url"] = None
                    result["asset_error"] = "无法识别 APK 版本，请到发布页手动查看"
        return result
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def _match_asset(assets, want=None):
    """The download URL for the asset matching the running exe, or None.

    The release carries one asset per channel (slgking.exe for the formal build;
    the test build has no GitHub asset). Matching on the running exe's basename
    keeps a test build from silently overwriting itself with the formal one, and
    a source checkout (sys.executable is python.exe) simply gets None.

    `want` overrides the basename for callers where sys.executable is not the
    download name (Android/Chaquopy: it is the embedded interpreter): an asset
    named exactly `want`, or - when `want` starts with '.' - one ending in that
    suffix (e.g. '.apk').
    """
    running = want or os.path.basename(sys.executable)
    if not running:
        return None
    for asset in assets:
        name = asset.get("name") or ""
        if name == running:
            return asset.get("browser_download_url")
        if want and want.startswith(".") and name.endswith(want):
            return asset.get("browser_download_url")
    return None


def download_asset(release, dest_path, timeout=180):
    """Fetch the release asset to `dest_path`. Returns True on success.

    Unlike the manifest fetch, this goes through the normal opener (no
    direct=True): GitHub is a public host and must respect any VPN/proxy the
    user relies on. The asset is a ~30MB exe, so the timeout is generous and the
    bytes are streamed straight to disk rather than held in memory.
    """
    url = release.get("asset_url")
    if not url:
        return False
    try:
        raw = slg_scrape.http_get(url, timeout=timeout)
    except Exception:  # noqa: BLE001 - a failed download is a message, not a crash
        return False
    try:
        tmp = dest_path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, dest_path)
        return True
    except OSError:
        return False


def android_version(text):
    """Android candidate order: a1 < a2 < beta < rc < stable; desktop unchanged."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(a|alpha|b|beta|rc)[.-]?(\d+))?", str(text).strip(), re.I)
    if not match:
        return None
    major, minor, patch, stage, number = match.groups()
    rank = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, None: 3}[stage.lower() if stage else None]
    return (int(major), int(minor), int(patch), rank, int(number or 0))


def android_is_newer(remote, local):
    theirs, ours = android_version(remote), android_version(local)
    return theirs is not None and ours is not None and theirs > ours


def check(conn, local_version, now=None, force=False, want=None, android=False):
    """A newer release as {'version', 'url'}, or None.

    At most one network call per CHECK_EVERY, and the stamp is written *before*
    the request rather than after: a lookup that fails - offline laptop, GitHub
    down, rate-limited - must not become a retry on every single launch, which
    is the one way a feature like this turns into a nuisance.

    `force` skips the gate, for the manual button in the settings dialog.
    `want` selects the release asset (Android passes '.apk'); omitted, the
    running executable's basename is used as before.
    """
    now = now or datetime.now()
    if not force:
        last = slg_db.get_pref(conn, PREF_CHECKED_AT)
        if last:
            try:
                if now - datetime.fromisoformat(last) < CHECK_EVERY:
                    return None
            except ValueError:
                pass  # a stamp we cannot read is a stamp worth overwriting

    slg_db.set_pref(conn, PREF_CHECKED_AT, now.isoformat(timespec="seconds"))

    release = latest_release(want=want)
    if android and release is not None and release.get("asset_error"):
        return release if force else None
    newer = android_is_newer if android else is_newer
    if release is None or not newer(release["version"], local_version):
        return None
    return release
