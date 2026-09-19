"""Is there a newer slgking on GitHub?

The app ships as a GitHub Release asset, so "is there a new version" is one
API call and one comparison. Kept out of slg_gui entirely: the version parsing
and the once-a-day gate are the parts that can be wrong, and neither needs a
window to be tested.
"""

import json
import re
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


def latest_release(timeout=8):
    """The newest release's tag and page, or None.

    Every failure returns None: no network, a proxy, GitHub's rate limit, or a
    body that is not the JSON we expect. A version check is never worth a
    traceback, and this one runs unattended three seconds after startup. The
    timeout is deliberately short for the same reason - the user did not ask
    for this call and should never wait on it.
    """
    try:
        raw = slg_scrape.http_get(API, timeout=timeout)
        data = json.loads(raw.decode("utf-8", "replace"))
        tag = data.get("tag_name") or ""
        if parse_version(tag) is None:
            return None
        return {"version": tag, "url": data.get("html_url") or RELEASES_URL}
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def check(conn, local_version, now=None, force=False):
    """A newer release as {'version', 'url'}, or None.

    At most one network call per CHECK_EVERY, and the stamp is written *before*
    the request rather than after: a lookup that fails - offline laptop, GitHub
    down, rate-limited - must not become a retry on every single launch, which
    is the one way a feature like this turns into a nuisance.

    `force` skips the gate, for the manual button in the settings dialog.
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

    release = latest_release()
    if release is None or not is_newer(release["version"], local_version):
        return None
    return release
