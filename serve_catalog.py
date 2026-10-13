"""Serve the synced catalogue over HTTP so clients stop scraping dikgames.

Two jobs, split by argv so one file does both and the box still needs no pip:

  publish  --publish         snapshot the db + covers into PUBLISH_DIR and write
                             manifest.json, then exit. Cron runs this after each
                             incremental sync.
  serve    (default)         a long-running stdlib HTTP server exposing that
                             snapshot. Run under systemd or nohup.

Layout (matches deploy_server.py's LOCALAPPDATA=/opt/slgking/data):
  DATA_DIR     /opt/slgking/data/slgking   the live db + covers the scraper writes
  PUBLISH_DIR  /opt/slgking/publish        the consistent snapshot clients read

The split is what keeps a client from ever downloading a db the cron job is
still writing: the scraper never touches PUBLISH_DIR, and publish strips a
private SQLite snapshot before atomically replacing the public db and manifest.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from collections import OrderedDict, deque
from difflib import SequenceMatcher
from email.utils import formatdate
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

import slg_db
from slg_cloud_store import (CloudStore, CloudError, MaintenanceStore,
                             MIN_FREE_BYTES, OwnerAccountAlreadyBound, DEVELOPER_ACCOUNT_ID)
from slg_forum import ForumError, ForumStore
from slg_web_catalogue import WebCatalogue, safe_source_url
import slg_release_info
import slg_source_review_ai
import slg_admin_metrics

DATA_DIR = "/opt/slgking/data/slgking"
PUBLISH_DIR = "/opt/slgking/publish"
PORT = 8080
STABLE_APP_VERSION = os.environ.get(
    "SLGKING_STABLE_VERSION", slg_release_info.DEFAULT_FALLBACK)
CHINA_TZ = timezone(timedelta(hours=8))

# The report log is append-only and shared across ThreadingHTTPServer's worker
# threads; a single lock keeps two concurrent reports from interleaving mid-line.
_REPORT_LOCK = threading.Lock()
_REPORT_RATE_LOCK = threading.Lock()
_REPORT_RATE_LIMITS = OrderedDict()
_REPORT_RATE_PER_MINUTE = 30
_REPORT_RATE_PER_HOUR = 300
_REPORT_FILE_MAX_BYTES = 8 * 1024 * 1024
_ADMIN_STATS_LOCK = threading.Lock()
_ADMIN_STATS_CACHE = {}
_ADMIN_STATS_CACHE_SECONDS = 20

# Comments are also an append-only JSONL, so they need their own lock for the
# same reason: two concurrent writes must not interleave mid-line.
_COMMENT_LOCK = threading.Lock()
_CLOUD_STORES = {}
_CLOUD_STORES_LOCK = threading.Lock()
_FORUM_STORES = {}
_FORUM_STORES_LOCK = threading.Lock()
def _forum_capacity(name, default, maximum):
    try:
        return max(1, min(maximum, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


_FORUM_MAX_REQUESTS = _forum_capacity("SLGKING_FORUM_MAX_REQUESTS", 1, 8)
_FORUM_MAX_WRITES = _forum_capacity("SLGKING_FORUM_MAX_WRITES", 1, 2)
_FORUM_REQUEST_SLOTS = threading.BoundedSemaphore(_FORUM_MAX_REQUESTS)
_FORUM_WRITE_SLOTS = threading.BoundedSemaphore(_FORUM_MAX_WRITES)
_FORUM_BURST_LOCK = threading.Lock()
_FORUM_BURST_BUCKETS = OrderedDict()
_FORUM_BURST_MAX_KEYS = 4096
_ACCOUNT_ACTIVITY_LOCK = threading.Lock()
_ACCOUNT_ACTIVE_REQUESTS = 0


@contextmanager
def _account_request_activity(enabled):
    global _ACCOUNT_ACTIVE_REQUESTS
    if not enabled:
        yield
        return
    with _ACCOUNT_ACTIVITY_LOCK:
        _ACCOUNT_ACTIVE_REQUESTS += 1
    try:
        yield
    finally:
        with _ACCOUNT_ACTIVITY_LOCK:
            _ACCOUNT_ACTIVE_REQUESTS -= 1


def _forum_burst_allowed(identity, kind, now=None):
    """Bounded, in-memory admission budget; never writes the account DB."""
    now = time.monotonic() if now is None else now
    rate, burst = {"read": (60, 20), "write_ip": (30, 5),
                   "write_account": (6, 2)}[kind]
    key = (kind, identity)
    with _FORUM_BURST_LOCK:
        while _FORUM_BURST_BUCKETS:
            oldest, (_, touched) = next(iter(_FORUM_BURST_BUCKETS.items()))
            if now - touched <= 300:
                break
            _FORUM_BURST_BUCKETS.pop(oldest)
        tokens, touched = _FORUM_BURST_BUCKETS.pop(key, (float(burst), now))
        tokens = min(float(burst), tokens + max(0, now - touched) * rate / 60)
        allowed = tokens >= 1
        _FORUM_BURST_BUCKETS[key] = (tokens - 1 if allowed else tokens, now)
        while len(_FORUM_BURST_BUCKETS) > _FORUM_BURST_MAX_KEYS:
            _FORUM_BURST_BUCKETS.popitem(last=False)
        return allowed


@contextmanager
def _forum_admission(write=False):
    # Reserve forum capacity atomically with checking account activity. Account
    # requests never wait for existing forum work and can always enter.
    with _ACCOUNT_ACTIVITY_LOCK:
        if _ACCOUNT_ACTIVE_REQUESTS or not _FORUM_REQUEST_SLOTS.acquire(blocking=False):
            raise CloudError("rate limited", 429)
        acquired_write = False
        if write:
            acquired_write = _FORUM_WRITE_SLOTS.acquire(blocking=False)
            if not acquired_write:
                _FORUM_REQUEST_SLOTS.release()
                raise CloudError("rate limited", 429)
    try:
        yield
    finally:
        if acquired_write:
            _FORUM_WRITE_SLOTS.release()
        _FORUM_REQUEST_SLOTS.release()

_MAINTENANCE_STORES = {}
_MAINTENANCE_STORES_LOCK = threading.Lock()
_SOURCE_REVIEW_AI_LOCK = threading.Lock()
_SOURCE_REVIEW_AI_JOBS_LOCK = threading.Lock()
_SOURCE_REVIEW_AI_JOBS = OrderedDict()
_SOURCE_REVIEW_AI_JOB_RETENTION_SECONDS = 6 * 60 * 60
_SOURCE_REVIEW_AI_JOB_LIMIT = 32
_ADMIN_SESSION_COOKIE = "slg_admin_session"
_ADMIN_SESSION_MAX_AGE = 7 * 86400
_ADMIN_ASSET_ALLOWLIST = {
    "admin-icon-192.svg": ("image/svg+xml; charset=utf-8",
                           "public, max-age=86400"),
    "admin-icon-512.svg": ("image/svg+xml; charset=utf-8",
                           "public, max-age=86400"),
}
# --- /m/ web client (0.25.0 sample) -----------------------------------------
# The browser identity is an opaque cookie, not a Bearer device token: a browser
# cannot use the DPAPI-protected token the desktop client stores on disk.
_WEB_SESSION_COOKIE = "slg_web_session"
_WEB_SESSION_MAX_AGE = 30 * 86400
_CATALOGUE_PAGE_MAX = 50
_CATALOGUE_TAG_MAX = 8
_CATALOGUE_SLUG_MAX = 160
# Public source ids only. The published snapshot already drops internal source
# rows; this allowlist is defence in depth so the web detail page can never turn
# an internal source into a link (see mem-compliance.md, 站内零资源链接).
# The slug is a separate matter: it is the catalogue's public identifier and is
# already returned by the existing /comments endpoint.
_PUBLIC_SOURCE_IDS = ("dikgames", "f95zone")
_LEADERBOARD_CACHE = {}
_LEADERBOARD_CACHE_LOCK = threading.Lock()
_CATALOGUE_READ_SLOTS = threading.BoundedSemaphore(2)


@contextmanager
def _catalogue_admission():
    with _ACCOUNT_ACTIVITY_LOCK:
        if _ACCOUNT_ACTIVE_REQUESTS or not _CATALOGUE_READ_SLOTS.acquire(blocking=False):
            raise CloudError("rate limited", 429)
    try:
        yield
    finally:
        _CATALOGUE_READ_SLOTS.release()
_CATALOGUE_SORTS = {
    "heat": "g.heat DESC, g.id ASC",
    "rating": "(g.rating IS NULL), g.rating DESC, g.heat DESC, g.id ASC",
    "updated": "(g.last_updated IS NULL), g.last_updated DESC, g.id ASC",
    "title": "g.title COLLATE NOCASE ASC, g.id ASC",
}
# Explicit allowlist instead of a directory walk: a new web asset must be named
# here before it can be served, so no traversal or stray file can leak.
_WEB_ASSET_ALLOWLIST = {
    "catalogue.js": "text/javascript; charset=utf-8",
    "catalogue.css": "text/css; charset=utf-8",
    "leaderboard.js": "text/javascript; charset=utf-8",
    "leaderboard.css": "text/css; charset=utf-8",
    "version.js": "text/javascript; charset=utf-8",
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "search.js": "application/javascript; charset=utf-8",
    "game.js": "application/javascript; charset=utf-8",
    "login.js": "application/javascript; charset=utf-8",
    "forum.js": "application/javascript; charset=utf-8",
    "forum.css": "text/css; charset=utf-8",
    "comments.js": "application/javascript; charset=utf-8",
    "account.css": "text/css; charset=utf-8",
    "author.js": "application/javascript; charset=utf-8",
    "author.css": "text/css; charset=utf-8",
    "usage.js": "application/javascript; charset=utf-8",
    "shop.js": "application/javascript; charset=utf-8",
    "shop.css": "text/css; charset=utf-8",
    "announcements.js": "application/javascript; charset=utf-8",
    "announcements.css": "text/css; charset=utf-8",
    "comments.css": "text/css; charset=utf-8",
}
_WEB_NOINDEX = {"X-Robots-Tag": "noindex, nofollow"}
# Cover content types by file extension. .jpg is the overwhelming majority and
# remains the fallback so an unknown extension keeps the old behaviour.
_COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp",
                ".avif": "image/avif", ".gif": "image/gif"}
_REPORT_LEGACY_EVENTS = frozenset((
    "launch", "snapshot", "signin", "lottery", "buy", "redeem"))
_REPORT_TOOL_EVENTS = frozenset(("tools_open", "tool_open", "settings_open"))
_REPORT_TOOL_IDS = frozenset((
    "check_updates", "collection_manager", "backup_restore",
    "translation_tools", "tag_translations", "preference_weights",
    "translation_settings", "scan_local_library", "sync_maintenance"))
_REPORT_TOP_LEVEL_FIELDS = frozenset((
    "v", "device", "app", "ts", "event", "data"))
_SYNC_STAGE_IDS = (
    "dikgames_incremental", "external_sources", "ai_review",
    "publish_snapshot", "catalogue_backup", "cloud_backup")
_SYNC_CORE_STAGE_IDS = frozenset((
    "dikgames_incremental", "external_sources", "publish_snapshot"))
_POINT_ACCOUNT_PATHS = frozenset((
    "/account/legacy-migrate", "/account/signin", "/rewards/group",
    "/account/rewards/maintenance/claim", "/account/shop/buy",
    "/account/lottery/draw", "/account/quests/claim",
    "/account/referrals/redeem", "/comments"))
_MAINTENANCE_SUMMARY_CODES = frozenset((
    "healthy", "no_recent_activity", "no_history", "run_active",
    "sync_overdue", "partial_failure", "stage_failed", "stage_skipped",
    "service_unavailable", "database_unavailable", "operation_errors",
    "disk_low", "backup_missing", "backup_stale", "status_read_failed"))


def _report_sanitize(event):
    """Validate old telemetry and the narrow 0.24.1 tools event schema.

    Only launch.client_version/platform/games and tool_open.tool_id are kept:
    the other legacy payloads are still validated (a malformed client is still
    rejected) but their values are dropped, so the event only counts.
    """
    if (not isinstance(event, dict)
            or set(event) - _REPORT_TOP_LEVEL_FIELDS):
        return None
    kind = event.get("event")
    if not isinstance(kind, str) or kind not in (
            _REPORT_LEGACY_EVENTS | _REPORT_TOOL_EVENTS):
        return None
    app = event.get("app", "slgking")
    version = event.get("v", "1")
    device = event.get("device")
    timestamp = event.get("ts")
    data = event.get("data", {})
    if (app != "slgking" or not isinstance(version, str)
            or not re.fullmatch(r"[0-9]{1,8}", version)
            or (device is not None and
                (not isinstance(device, str) or not 1 <= len(device) <= 128
                 or not re.fullmatch(r"[A-Za-z0-9._:-]+", device)))
            or (timestamp is not None and
                (not isinstance(timestamp, str) or len(timestamp) > 64
                 or _event_day(timestamp) is None))
            or not isinstance(data, dict)):
        return None

    if kind in _REPORT_TOOL_EVENTS:
        if kind == "tool_open":
            if (set(data) != {"tool_id"}
                    or data.get("tool_id") not in _REPORT_TOOL_IDS):
                return None
            clean_data = {"tool_id": data["tool_id"]}
        else:
            if data:
                return None
            clean_data = {}
    else:
        fields = {
            "launch": {"platform", "client_version", "games"},
            "snapshot": {"points", "titles", "collection"},
            "signin": {"dev"},
            "lottery": {"grand"},
            "buy": {"item"},
            "redeem": {"success"},
        }[kind]
        if set(data) - fields:
            return None
        clean_data = {}
        for name, value in data.items():
            if kind == "launch" and name == "platform":
                if (not isinstance(value, str) or len(value) > 24
                        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)):
                    return None
                clean_data[name] = value
            elif kind == "launch" and name == "client_version":
                if (not isinstance(value, str) or len(value) > 40
                        or not re.fullmatch(r"[A-Za-z0-9.+_-]+", value)):
                    return None
                clean_data[name] = value
            elif (kind == "launch" and name == "games") or (
                    kind == "snapshot" and name in ("points", "titles", "collection")):
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000000:
                    return None
                if kind == "launch":
                    clean_data[name] = value
            elif kind == "signin" and name == "dev":
                if not isinstance(value, bool):
                    return None
            elif kind == "lottery" and name == "grand":
                if not isinstance(value, bool):
                    return None
            elif kind == "buy" and name == "item":
                if (not isinstance(value, str) or len(value) > 96
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)):
                    return None
            elif kind == "redeem" and name == "success":
                if not isinstance(value, bool):
                    return None
            else:
                return None

    clean = {"event": kind, "data": clean_data}
    clean["app"] = app
    clean["v"] = version
    if device is not None:
        clean["device"] = device
    if timestamp is not None:
        clean["ts"] = timestamp
    return clean


def _report_rate_limited(identity, now=None):
    """Bound anonymous telemetry by source address without persisting it."""
    now = time.monotonic() if now is None else now
    key = hashlib.sha256(str(identity or "unknown").encode("utf-8")).digest()
    with _REPORT_RATE_LOCK:
        events = _REPORT_RATE_LIMITS.setdefault(key, deque())
        while events and now - events[0] >= 3600:
            events.popleft()
        minute_count = sum(now - stamp < 60 for stamp in events)
        if minute_count >= _REPORT_RATE_PER_MINUTE or len(events) >= _REPORT_RATE_PER_HOUR:
            return True
        events.append(now)
        _REPORT_RATE_LIMITS.move_to_end(key)
        while len(_REPORT_RATE_LIMITS) > 4096:
            _REPORT_RATE_LIMITS.popitem(last=False)
    return False


def _prune_source_review_ai_jobs(now=None):
    now = time.time() if now is None else now
    with _SOURCE_REVIEW_AI_JOBS_LOCK:
        for job_id, job in list(_SOURCE_REVIEW_AI_JOBS.items()):
            finished_at = job.get("finished_at")
            if (finished_at is not None
                    and now - finished_at > _SOURCE_REVIEW_AI_JOB_RETENTION_SECONDS):
                _SOURCE_REVIEW_AI_JOBS.pop(job_id, None)
        while len(_SOURCE_REVIEW_AI_JOBS) >= _SOURCE_REVIEW_AI_JOB_LIMIT:
            removable = next((job_id for job_id, job in _SOURCE_REVIEW_AI_JOBS.items()
                              if job.get("state") != "running"), None)
            if removable is None:
                break
            _SOURCE_REVIEW_AI_JOBS.pop(removable, None)

# Best-effort comment abuse controls. Device IDs are client supplied and can be
# reset, so these limits reduce casual bursts rather than establish identity.
_COMMENT_POST_WINDOW_SECONDS = 10 * 60
_COMMENT_POST_MAX_PER_WINDOW = 3
_COMMENT_REPORT_WINDOW_SECONDS = 10 * 60
_COMMENT_REPORT_MAX_PER_WINDOW = 8
_COMMENT_DUPLICATE_REPORT_SECONDS = 24 * 60 * 60
_COMMENT_RATE_MAX_IDENTITIES = 10000
_COMMENT_DUPLICATE_REPORT_MAX_KEYS = 50000
_COMMENT_POST_TIMES = OrderedDict()
_COMMENT_REPORT_TIMES = OrderedDict()
_COMMENT_REPORTED_IDS = OrderedDict()


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _read_comments(path):
    """Replay the append-only comment log.

    Returns (comments, deleted_ids, report_counts). Each line is one event:
    a comment (has "content"), a tombstone (has "tombstone"), or a report
    (has "report"); the latter two reference a comment id. Append-only means
    delete/report never rewrite history - they add a line - which is exactly
    the audit trail a moderation flow needs.
    """
    comments, deleted, reported = [], set(), {}
    if not os.path.isfile(path):
        return comments, deleted, reported
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "tombstone" in obj:
                deleted.add(obj["tombstone"])
            elif "report" in obj:
                cid = obj["report"]
                reported[cid] = reported.get(cid, 0) + 1
            elif "content" in obj:
                comments.append(obj)
    return comments, deleted, reported


def _public_comment(c):
    """A comment as served to other users - the device id stays private."""
    return {k: c.get(k) for k in ("id", "game", "content", "nickname", "ts")}


def _rate_limited(table, identity, now, window, maximum):
    """Prune and check one identity's rolling event window. Caller holds lock."""
    bucket = table.get(identity)
    if bucket is None:
        return False
    cutoff = now - window
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if not bucket:
        del table[identity]
        return False
    table.move_to_end(identity)
    return len(bucket) >= maximum


def _record_rate_event(table, identity, now):
    """Record an accepted event and bound per-identity memory. Caller locks."""
    bucket = table.get(identity)
    if bucket is None:
        bucket = deque()
        table[identity] = bucket
    bucket.append(now)
    table.move_to_end(identity)
    while len(table) > _COMMENT_RATE_MAX_IDENTITIES:
        table.popitem(last=False)


def _report_already_recorded(identity, cid, now):
    """Check/prune the bounded device-and-comment duplicate cache. Caller locks."""
    cutoff = now - _COMMENT_DUPLICATE_REPORT_SECONDS
    while _COMMENT_REPORTED_IDS:
        _key, timestamp = next(iter(_COMMENT_REPORTED_IDS.items()))
        if timestamp > cutoff:
            break
        _COMMENT_REPORTED_IDS.popitem(last=False)
    return (identity, cid) in _COMMENT_REPORTED_IDS


def _record_report(identity, cid, now):
    key = (identity, cid)
    _COMMENT_REPORTED_IDS[key] = now
    _COMMENT_REPORTED_IDS.move_to_end(key)
    while len(_COMMENT_REPORTED_IDS) > _COMMENT_DUPLICATE_REPORT_MAX_KEYS:
        _COMMENT_REPORTED_IDS.popitem(last=False)


# The mobile admin page, served inline so there is no second asset to deploy.
# Its API operations use the developer secret held only in page memory.
ADMIN_PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1c1c1e">
<title>黄油之王 管理台</title>
<style>
:root{--bg:#1c1c1e;--card:#26262a;--chip:#33333c;--acc:#4a8ee0;--txt:#e8e8ea;--mut:#9a9aa2;--danger:#e88a8a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px 16px 48px}
h1{font-size:20px;letter-spacing:1px}
h2{font-size:13px;color:var(--acc);margin:20px 0 8px;text-transform:uppercase;letter-spacing:1px}
.card{background:var(--card);border:1px solid #2e2e34;border-radius:10px;padding:14px;margin-bottom:6px}
input,textarea{width:100%;background:var(--chip);color:var(--txt);border:0;border-radius:8px;padding:10px;font:inherit}
textarea{min-height:80px;resize:vertical}
label{color:var(--mut);font-size:12px;display:block;margin:8px 0 4px}
.row{display:flex;gap:8px;align-items:center}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 16px;font:inherit;cursor:pointer}
button.flat{background:var(--chip);color:var(--txt)}
button.danger{background:#5a2b2b;color:var(--danger)}
code{background:var(--chip);padding:4px 8px;border-radius:6px;font-size:15px;letter-spacing:1px}
.switch{display:flex;justify-content:space-between;align-items:center;padding:6px 0}
.hint{color:var(--mut);font-size:12px;margin-top:6px}
.cmt{border-bottom:1px solid #2e2e34;padding:8px 0}
.cmt .meta{color:var(--mut);font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:#3a2b2b;color:var(--danger);margin-left:6px}
</style>
</head>
<body>
<h1>黄油之王 管理台</h1>
<div class="card">
  <label>开发者密钥</label>
  <div class="row"><input id="key" type="password" autocomplete="off" placeholder="输入开发者密钥"><button id="saveKey" class="flat">验证并进入</button><button id="clearKey" class="flat">退出并清除</button></div>
  <div class="hint" id="status">密钥只暂存在当前页面内存，刷新或退出后清除。</div>
</div>

<h2>公告</h2>
<div class="card">
  <label>标题</label><input id="aTitle">
  <label>公告 ID</label><input id="aId">
  <label>内容</label><textarea id="aBody"></textarea>
</div>

<h2>功能开关</h2>
<div class="card">
  <div class="switch"><span>签到维护</span><input type="checkbox" id="fSignin"></div>
  <div class="switch"><span>抽奖维护</span><input type="checkbox" id="fLottery"></div>
  <div class="switch"><span>整体维护</span><input type="checkbox" id="fMaint"></div>
  <label>维护提示文案</label><input id="fMaintMsg">
  <label>强制最低版本</label><input id="fMinver" placeholder="留空=不强制">
</div>

<h2>群友兑换码</h2>
<div class="card">
  <div class="row"><code id="code">—</code><button id="copyCode" class="flat">复制</button><button id="refreshCode" class="flat">刷新</button></div>
  <div class="hint">当天有效，跨天自动换。</div>
</div>

<h2>评论审核</h2>
<div class="card" id="comments"><div class="hint">加载中…</div></div>

<div class="row" style="margin-top:16px">
  <button id="load">读取服务器</button>
  <button id="save">推送到服务器</button>
</div>

<script>
var KEY = '', currentConfig = {};
var keyInput = document.getElementById('key');
function status(m,d){var s=document.getElementById('status');s.textContent=m;s.style.color=d?'var(--danger)':'var(--mut)'}
document.getElementById('saveKey').onclick=function(){KEY=keyInput.value;if(!KEY){status('请输入开发者密钥',true);return}loadAll()};
document.getElementById('clearKey').onclick=function(){KEY='';keyInput.value='';status('已退出，页面内存中的密钥已清除');document.getElementById('aTitle').value='';document.getElementById('aId').value='';document.getElementById('aBody').value='';document.getElementById('comments').textContent=''};
async function api(path,opts){opts=opts||{};var headers={'X-SLG-Developer-Key':KEY};
  if(opts.body)headers['Content-Type']='application/json';
  var resp=await fetch(path,{method:opts.method||'GET',headers:headers,body:opts.body?JSON.stringify(opts.body):undefined});
  var data=await resp.json();if(!resp.ok)throw new Error(data.error||('HTTP '+resp.status));return data}
async function load(){status('读取中…');try{var cfg=await api('/admin/config');
  if(cfg.locked){status('开发者密钥不正确或服务器尚未配置',true);return}
  currentConfig=cfg||{};
  document.getElementById('aTitle').value=(cfg.announcement||{}).title||'';
  document.getElementById('aId').value=(cfg.announcement||{}).id||'';
  document.getElementById('aBody').value=(cfg.announcement||{}).body||'';
  document.getElementById('fSignin').checked=!!(cfg.flags||{}).disable_signin;
  document.getElementById('fLottery').checked=!!(cfg.flags||{}).disable_lottery;
  document.getElementById('fMaint').checked=!!cfg.maintenance;
  document.getElementById('fMaintMsg').value=cfg.maintenance_msg||'';
  document.getElementById('fMinver').value=cfg.min_version||'';
  status('已读取服务器配置')}catch(e){status('读取失败：'+e,true)}}
async function save(){status('推送中…');
  var body={flags:{disable_signin:document.getElementById('fSignin').checked,disable_lottery:document.getElementById('fLottery').checked},
    announcement:(document.getElementById('aTitle').value.trim()||document.getElementById('aBody').value.trim())
      ?{id:document.getElementById('aId').value.trim()||String(Date.now()),title:document.getElementById('aTitle').value.trim()||'公告',body:document.getElementById('aBody').value.trim()}:{},
    min_version:document.getElementById('fMinver').value.trim(),maintenance:document.getElementById('fMaint').checked,maintenance_msg:document.getElementById('fMaintMsg').value.trim()};
  if(Object.prototype.hasOwnProperty.call(currentConfig,'maintenance_compensation'))body.maintenance_compensation=currentConfig.maintenance_compensation;
  try{var r=await api('/admin/config',{method:'POST',body:body});if(r.locked){status('开发者密钥不正确',true);return}currentConfig=body;status('已推送到服务器')}catch(e){status('推送失败：'+e,true)}}
async function loadCode(){try{var r=await api('/admin/group_code');document.getElementById('code').textContent=r.code||'—'}catch(e){document.getElementById('code').textContent='—'}}
document.getElementById('copyCode').onclick=function(){var c=document.getElementById('code').textContent;if(c&&c!=='—')navigator.clipboard.writeText(c).then(function(){status('已复制 '+c)})};
async function loadComments(){var box=document.getElementById('comments');try{var r=await api('/admin/comments');
  if(r.locked){box.innerHTML='<div class="hint">密钥不对</div>';return}
  var list=r.comments||[];if(!list.length){box.innerHTML='<div class="hint">还没有评论</div>';return}
  box.innerHTML='';list.forEach(function(c){var div=document.createElement('div');div.className='cmt';
    var meta=document.createElement('div');meta.className='meta';
    meta.textContent=(c.nickname||'匿名')+' · '+(c.ts||'').replace('T',' ').slice(0,16)+(c.deleted?' · 已删除':'')+(c.reports?' · 被举报 '+c.reports+' 次':'');
    if(c.reports){var b=document.createElement('span');b.className='badge';b.textContent='举报';meta.appendChild(b)}
    var content=document.createElement('div');content.textContent=c.content;
    var btn=document.createElement('button');btn.className=c.deleted?'flat':'danger';btn.textContent=c.deleted?'已删':'删除';btn.disabled=!!c.deleted;
    btn.style.cssText='float:right;padding:4px 10px;font-size:12px';
    btn.onclick=async function(){try{var d=await api('/admin/comments/'+encodeURIComponent(c.id),{method:'DELETE'});if(!d.ok){status('删除失败',true);return}loadComments()}catch(e){status('删除失败',true)}};
    div.appendChild(meta);div.appendChild(content);div.appendChild(btn);box.appendChild(div)})
  }catch(e){box.innerHTML='<div class="hint">加载失败</div>'}}
async function loadAll(){await load();await loadCode();await loadComments()}
keyInput.addEventListener('keydown',function(e){if(e.key==='Enter')document.getElementById('saveKey').click()});
document.getElementById('load').onclick=loadAll;
document.getElementById('refreshCode').onclick=loadCode;
</script>
</body>
</html>
"""


_GAMES_BUCKETS = ("0", "1-9", "10-49", "50-199", "200+")


def _games_bucket(value):
    """Map an int library size to its fixed histogram label, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    if value == 0:
        return "0"
    if value < 10:
        return "1-9"
    if value < 50:
        return "10-49"
    if value < 200:
        return "50-199"
    return "200+"


def _retention(day_devices, days, today):
    """Day-1 and day-7 return rates for the trailing `days`-day window."""
    days = max(1, int(days))
    first = today - timedelta(days=days - 1)
    d1_days = [first + timedelta(days=offset)
               for offset in range(days)
               if first + timedelta(days=offset + 1) <= today]
    d7_days = [first + timedelta(days=offset)
               for offset in range(days)
               if first + timedelta(days=offset + 7) <= today]
    cohort = set()
    for day in d1_days:
        cohort |= day_devices.get(day.isoformat(), set())
    d1_hits = set()
    for day in d1_days:
        d1_hits |= day_devices.get(
            (day + timedelta(days=1)).isoformat(), set())
    d7_hits = set()
    for day in d7_days:
        d7_hits |= day_devices.get(
            (day + timedelta(days=7)).isoformat(), set())
    return {
        "cohort": len(cohort),
        "d1_pct": round(len(cohort & d1_hits) * 100.0 / len(cohort), 1)
        if cohort else None,
        "d7_pct": round(len(cohort & d7_hits) * 100.0 / len(cohort), 1)
        if cohort else None,
        "range_days": days,
    }


def aggregate_lines(lines, days=14, today=None, active_days=30,
                    stable_version=STABLE_APP_VERSION):
    """Count anonymous events, distinct devices, the newest timestamp, and a
    per-day breakdown.

    Takes an iterable of jsonl lines so the deploy script can aggregate the raw
    log it just pulled off the box without writing it to a file first.

    Two different questions are answered here, and conflating them is what made
    the panel's numbers confusing: `events`/`devices` are totals since the log
    began, while `by_day` is what happened on each of the last `days` calendar
    days. "devices" in either sense is a count of distinct installs, not of
    people, and not of anyone currently online.
    """
    today = today or datetime.now(CHINA_TZ).date()
    counts = {}
    devices = set()
    day_events = {}
    day_devices = {}
    event_type_by_day = {}
    undated_events = {}
    range_devices = set()
    latest_launches = {}
    tool_opens = {}
    last = None
    last_report_time = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("event")
        if not isinstance(kind, str) or not kind.strip():
            kind = "unknown"
        else:
            kind = kind.strip()[:64]
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "tool_open":
            tool_data = ev.get("data")
            tool_id = tool_data.get("tool_id") if isinstance(tool_data, dict) else None
            if isinstance(tool_id, str) and tool_id.strip():
                tool_id = tool_id.strip()[:64]
                tool_opens[tool_id] = tool_opens.get(tool_id, 0) + 1
        dev = ev.get("device")
        if isinstance(dev, str) and dev.strip() and len(dev) <= 128:
            dev = dev.strip()
            devices.add(dev)
        else:
            dev = None
        ts = ev.get("ts")
        if isinstance(ts, str) and ts.strip():
            raw_ts = ts.strip()
            ts = raw_ts[:64]
        else:
            raw_ts = None
            ts = None
        parsed_ts = (_parse_event_timestamp(raw_ts)
                     if raw_ts and len(raw_ts) <= 64 else None)
        day = parsed_ts.date().isoformat() if parsed_ts is not None else None
        if day is None:
            # Keep undated legacy reports in all-time totals, but never make
            # them look like reports from today or include them in a range.
            undated_events[kind] = undated_events.get(kind, 0) + 1
        else:
            if last_report_time is None or parsed_ts > last_report_time:
                last_report_time = parsed_ts
                last = ts
            day_events[day] = day_events.get(day, 0) + 1
            by_type = event_type_by_day.setdefault(day, {})
            by_type[kind] = by_type.get(kind, 0) + 1
            if dev:
                day_devices.setdefault(day, set()).add(dev)
                if (today - timedelta(days=days - 1)).isoformat() <= day <= today.isoformat():
                    range_devices.add(dev)

        # Version coverage is based on each installation's most recent launch
        # in the active window. It never emits installation identifiers.
        if kind == "launch" and dev and day:
            try:
                launch_day = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                launch_day = None
            age = (today - launch_day).days if launch_day else active_days
            if 0 <= age < active_days:
                data = ev.get("data")
                version = data.get("client_version") if isinstance(data, dict) else None
                if not isinstance(version, str) or not version.strip():
                    version = "未上报"
                else:
                    version = version.strip()[:40]
                platform = data.get("platform") if isinstance(data, dict) else None
                if isinstance(platform, str) and platform.strip():
                    platform = platform.strip()[:24]
                else:
                    platform = None
                games = data.get("games") if isinstance(data, dict) else None
                if (isinstance(games, bool) or not isinstance(games, int)
                        or games < 0):
                    games = None
                previous = latest_launches.get(dev)
                if previous is None or parsed_ts > previous[0]:
                    latest_launches[dev] = (parsed_ts, version, platform, games)

    versions = {}
    platforms = {}
    game_buckets = {}
    for _ts, version, platform, games in latest_launches.values():
        versions[version] = versions.get(version, 0) + 1
        if platform is not None:
            platforms[platform] = platforms.get(platform, 0) + 1
        bucket = _games_bucket(games)
        if bucket is not None:
            game_buckets[bucket] = game_buckets.get(bucket, 0) + 1
    active_installs = len(latest_launches)
    stable_installs = versions.get(stable_version, 0)
    unknown_installs = versions.get("未上报", 0)
    return {
        "events": counts,
        "devices": len(devices),
        "last_report": last,
        "by_day": _by_day(day_events, day_devices, days, today),
        "event_timeline": _event_timeline(
            event_type_by_day, undated_events, days, today),
        "range_devices": len(range_devices),
        "client_versions": [
            {"version": version, "installs": installs}
            for version, installs in sorted(
                versions.items(), key=lambda item: (-item[1], item[0]))],
        "platform_breakdown": [
            {"platform": platform, "installs": installs}
            for platform, installs in sorted(
                platforms.items(), key=lambda item: (-item[1], item[0]))],
        "games_histogram": [
            {"bucket": bucket, "installs": game_buckets.get(bucket, 0)}
            for bucket in _GAMES_BUCKETS],
        "tool_breakdown": [
            {"tool_id": tool_id, "opens": opens}
            for tool_id, opens in sorted(
                tool_opens.items(), key=lambda item: (-item[1], item[0]))],
        "funnel": {
            "installs_30d": len(range_devices),
            "accounts_total": None,
            "accounts_30d": None,
            "install_to_account_pct": None,
        },
        "retention": _retention(day_devices, days, today),
        "update_coverage": {
            "stable_version": stable_version,
            "active_installs": active_installs,
            "stable_installs": stable_installs,
            "unknown_installs": unknown_installs,
            "coverage_pct": round(stable_installs * 100.0 / active_installs, 1)
            if active_installs else 0.0,
            "active_days": active_days,
        },
    }


def _parse_event_timestamp(timestamp):
    """Parse report time, treating legacy offset-free values as China time."""
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    try:
        text = timestamp.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CHINA_TZ)
        return parsed.astimezone(CHINA_TZ)
    except (TypeError, ValueError):
        return None


def _event_day(timestamp):
    """Return the Asia/Shanghai calendar date for an ISO timestamp."""
    parsed = _parse_event_timestamp(timestamp)
    return parsed.date().isoformat() if parsed is not None else None


def _event_timeline(event_type_by_day, undated_events, days, today=None):
    """Build zero-filled daily and trailing seven-day event-type buckets.

    The seven-day buckets are grouped backward from today so an incomplete
    leading bucket (for example, two days for a 30-day range) has its true
    start/end dates instead of being labeled as a full week.
    """
    if today is None:
        today = datetime.now(CHINA_TZ).date()
    days = max(1, int(days))
    first_day = today - timedelta(days=days - 1)

    def bucket(start, end):
        events = {}
        cursor = start
        while cursor <= end:
            for kind, count in event_type_by_day.get(cursor.isoformat(), {}).items():
                events[kind] = events.get(kind, 0) + count
            cursor += timedelta(days=1)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": (start.isoformat() if start == end else
                      start.isoformat() + " 至 " + end.isoformat()),
            "events": events,
            "total": sum(events.values()),
        }

    daily = [bucket(first_day + timedelta(days=offset),
                    first_day + timedelta(days=offset))
             for offset in range(days)]
    weekly = []
    back = 0
    while back < days:
        end = today - timedelta(days=back)
        start = max(first_day, end - timedelta(days=6))
        weekly.append(bucket(start, end))
        back += (end - start).days + 1
    weekly.reverse()
    undated = {kind: count for kind, count in sorted(undated_events.items())}
    return {
        "range_days": days,
        "daily": daily,
        "weekly": weekly,
        "undated": undated,
        "undated_total": sum(undated.values()),
    }


def _by_day(day_events, day_devices, days, today=None):
    """The last `days` calendar days, newest first, gaps filled with zeroes.

    Zero-filling is the point: a day with no reports is information (nobody
    opened it), and dropping those rows would make a quiet week look busy.
    """
    if today is None:
        today = datetime.now(CHINA_TZ).date()
    out = []
    for back in range(days):
        day = (today - timedelta(days=back)).isoformat()
        out.append({
            "day": day,
            "events": day_events.get(day, 0),
            "devices": len(day_devices.get(day, ())),
        })
    return out


def aggregate_stats(path, days=14, today=None, active_days=30,
                    stable_version=STABLE_APP_VERSION):
    """One-pass count over the report log. Missing file reads as all-empty."""
    if not os.path.isfile(path):
        return {"events": {}, "devices": 0, "last_report": None,
                "by_day": _by_day({}, {}, days, today),
                "event_timeline": _event_timeline({}, {}, days, today),
                "range_devices": 0,
                "client_versions": [],
                "platform_breakdown": [],
                "games_histogram": [
                    {"bucket": bucket, "installs": 0}
                    for bucket in _GAMES_BUCKETS],
                "tool_breakdown": [],
                "funnel": {"installs_30d": 0, "accounts_total": None,
                           "accounts_30d": None,
                           "install_to_account_pct": None},
                "retention": {"cohort": 0, "d1_pct": None, "d7_pct": None,
                              "range_days": days},
                "update_coverage": {
                    "stable_version": stable_version,
                    "active_installs": 0, "stable_installs": 0,
                    "unknown_installs": 0, "coverage_pct": 0.0,
                    "active_days": active_days}}
    with open(path, "r", encoding="utf-8") as fh:
        return aggregate_lines(fh, days=days, today=today,
                               active_days=active_days,
                               stable_version=stable_version)


def _source_review_fingerprint_for_row(row):
    value = str(row["content_hash"] or "").strip()
    if value:
        return value[:96]
    material = {name: row[name] for name in (
        "source_title", "developer", "source_version", "engine",
        "overview", "tags_json", "source_modified")}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "derived:" + hashlib.sha256(encoded).hexdigest()


def _source_review_slug(source_id, external_id):
    safe_id = re.sub(r"[^a-z0-9-]+", "-", str(external_id or "").casefold()
                     ).strip("-")[:80]
    if not safe_id:
        safe_id = hashlib.sha256(str(external_id).encode("utf-8")).hexdigest()[:20]
    return "%s-%s" % (source_id, safe_id)


def _game_redirect_map(conn):
    columns = {r[1] for r in conn.execute(
        "PRAGMA table_info(game_redirects)")}
    if not {"old_slug", "canonical_slug"} <= columns:
        return {}
    return {str(row["old_slug"]): str(row["canonical_slug"])
            for row in conn.execute(
                "SELECT old_slug,canonical_slug FROM game_redirects")}


def _record_game_redirect(conn, old_slug, canonical_slug):
    """Add a stable redirect if it cannot introduce or extend a cycle."""
    old_slug, canonical_slug = str(old_slug or ""), str(canonical_slug or "")
    if not old_slug or not canonical_slug or old_slug == canonical_slug:
        return False
    redirects = _game_redirect_map(conn)
    if old_slug in redirects:
        # A published identity is stable. Do not silently retarget it on a
        # later source review; an existing chain may still resolve to target.
        current = old_slug
        seen = set()
        while current in redirects and current not in seen:
            seen.add(current)
            current = redirects[current]
        return current == canonical_slug
    current = canonical_slug
    seen = {old_slug}
    while current in redirects:
        if current in seen:
            return False
        seen.add(current)
        current = redirects[current]
    if current in seen:
        return False
    conn.execute(
        "INSERT INTO game_redirects(old_slug,canonical_slug) VALUES (?,?)",
        (old_slug, canonical_slug))
    return True


def _queue_game_redirect_candidate(conn, old_slug, canonical_slug,
                                  source_id, external_id):
    """Remember a same decision until every source leaves the old game row."""
    conn.execute(
        "INSERT INTO pending_game_redirects(old_slug,canonical_slug,source_id,"
        "external_id) VALUES (?,?,?,?) ON CONFLICT(old_slug,source_id,external_id)"
        " DO UPDATE SET canonical_slug=excluded.canonical_slug",
        (old_slug, canonical_slug, source_id, external_id))


def _finish_pending_game_redirects(conn, result):
    """Publish only unambiguous redirects whose old row has no sources left."""
    old_slugs = [row[0] for row in conn.execute(
        "SELECT DISTINCT old_slug FROM pending_game_redirects ORDER BY old_slug")]
    for old_slug in old_slugs:
        targets = {row[0] for row in conn.execute(
            "SELECT DISTINCT canonical_slug FROM pending_game_redirects"
            " WHERE old_slug=?", (old_slug,))}
        old = conn.execute("SELECT id FROM games WHERE slug=?", (old_slug,)).fetchone()
        if len(targets) != 1 or old is None:
            continue
        still_mapped = conn.execute(
            "SELECT 1 FROM game_sources WHERE game_id=? LIMIT 1",
            (old[0],)).fetchone()
        if still_mapped:
            continue
        canonical_slug = next(iter(targets))
        if _record_game_redirect(conn, old_slug, canonical_slug):
            result["redirected"] += 1
            conn.execute("DELETE FROM pending_game_redirects WHERE old_slug=?",
                         (old_slug,))


def _public_game_slugs(publish_dir, slug):
    """Return a canonical slug and every old slug whose comments belong to it."""
    slug = str(slug or "").strip()
    if slug.lower().startswith("dlsite-"):
        return WebCatalogue(publish_dir).game_slugs(slug)
    if not slug:
        return slug, [slug]
    path = os.path.join(publish_dir, "slgking.db")
    try:
        conn = sqlite3.connect(path, timeout=3)
        try:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(game_redirects)")}
            if not {"old_slug", "canonical_slug"} <= columns:
                return slug, [slug]
            redirects = {str(row[0]): str(row[1]) for row in conn.execute(
                "SELECT old_slug,canonical_slug FROM game_redirects")}
        finally:
            conn.close()
    except sqlite3.Error:
        return slug, [slug]

    def resolve(value):
        current, seen = value, set()
        while current in redirects:
            if current in seen:
                return value
            seen.add(current)
            current = redirects[current]
        return current

    canonical = resolve(slug)
    related = {slug, canonical}
    related.update(old for old in redirects if resolve(old) == canonical)
    return canonical, sorted(related)


def _resolve_public_game_slug(publish_dir, slug):
    """Resolve a public catalogue slug for comment write paths."""
    return _public_game_slugs(publish_dir, slug)[0]


def _sanitize_game_redirects(conn):
    """Keep only non-cyclic redirects whose source and target games exist.

    Chains are flattened to their terminal canonical slug for clients. This
    table is independent of game_sources, so it survives private-source row
    removal (including Zyoudao) later in publish().
    """
    columns = {r[1] for r in conn.execute(
        "PRAGMA table_info(game_redirects)")}
    if not {"old_slug", "canonical_slug"} <= columns:
        return 0
    rows = conn.execute(
        "SELECT old_slug,canonical_slug FROM game_redirects").fetchall()
    redirects = {str(row[0]): str(row[1]) for row in rows}
    games = {str(row[0]) for row in conn.execute("SELECT slug FROM games")}
    valid = {}
    for old_slug, canonical_slug in redirects.items():
        if (not old_slug or not canonical_slug or old_slug == canonical_slug
                or old_slug not in games):
            continue
        current = canonical_slug
        seen = {old_slug}
        while current in redirects:
            if current in seen:
                current = ""
                break
            seen.add(current)
            current = redirects[current]
        if current and current in games and current != old_slug:
            valid[old_slug] = current
    conn.execute("DELETE FROM game_redirects")
    conn.executemany(
        "INSERT INTO game_redirects(old_slug,canonical_slug) VALUES (?,?)",
        sorted(valid.items()))
    return len(valid)


def _apply_source_review_decisions(data_dir, publish_dir):
    """Apply only explicit, revision-matched human source decisions.

    Cloud review decisions and the catalogue live in separate SQLite files, so
    this is intentionally a repeatable desired-state operation. A crash after
    the decision is saved is repaired by the next publish; no source/game row
    is deleted, preserving IDs that client-local progress may still reference.
    """
    catalogue_path = os.path.join(data_dir, "slgking.db")
    cloud_path = os.path.join(os.path.dirname(publish_dir), "cloud", "cloud.sqlite3")
    if not os.path.isfile(catalogue_path) or not os.path.isfile(cloud_path):
        return {"same": 0, "distinct": 0, "stale": 0, "redirected": 0,
                "comment_redirect_blocked": 0, "redirect_pending": 0}

    _ai, decisions = CloudStore(cloud_path).admin_source_review_index()
    if not decisions:
        return {"same": 0, "distinct": 0, "stale": 0, "redirected": 0,
                "comment_redirect_blocked": 0, "redirect_pending": 0}

    conn = sqlite3.connect(catalogue_path, timeout=10)
    conn.row_factory = sqlite3.Row
    result = {"same": 0, "distinct": 0, "stale": 0, "redirected": 0,
              "comment_redirect_blocked": 0, "redirect_pending": 0}
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(game_sources)")}
        if not {"source_id", "external_id", "game_id"} <= columns:
            return result
        conn.execute(
            "CREATE TABLE IF NOT EXISTS game_redirects ("
            "old_slug TEXT PRIMARY KEY, canonical_slug TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_game_redirects ("
            "old_slug TEXT NOT NULL, canonical_slug TEXT NOT NULL,"
            "source_id TEXT NOT NULL, external_id TEXT NOT NULL,"
            "PRIMARY KEY(old_slug,source_id,external_id))")
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM game_sources WHERE source_id IN ('f95zone','zyoudao')"
        ).fetchall()
        by_identity = {(row["source_id"], row["external_id"]): row for row in rows}
        for (source_id, external_id, revision), decision in decisions.items():
            action = decision.get("decision")
            if action not in ("same", "distinct"):
                # skip records an operator's review choice; it does not silently
                # suppress a game from users' catalogues.
                conn.execute(
                    "DELETE FROM pending_game_redirects WHERE source_id=?"
                    " AND external_id=?", (source_id, external_id))
                continue
            row = by_identity.get((source_id, external_id))
            if row is None or _source_review_fingerprint_for_row(row) != revision:
                result["stale"] += 1
                continue

            if action == "same":
                canonical_id = decision.get("canonical_game_id")
                if (canonical_id is None
                        and row["match_status"] == "strong_match"):
                    # An exact title+developer match already points at the
                    # intended row; a same decision without a picker value is
                    # still a valid explicit confirmation of that association.
                    canonical_id = int(row["game_id"])
                if (isinstance(canonical_id, bool)
                        or not isinstance(canonical_id, int) or canonical_id <= 0):
                    continue
                target = conn.execute(
                    "SELECT origin,slug FROM games WHERE id=?", (canonical_id,)
                ).fetchone()
                if target is None or target["origin"] != "site":
                    result["stale"] += 1
                    continue
                pending = conn.execute(
                    "SELECT old_slug FROM pending_game_redirects"
                    " WHERE source_id=? AND external_id=? LIMIT 1",
                    (source_id, external_id)).fetchone()
                if (int(row["game_id"]) != canonical_id
                        or row["match_status"] != "manual_match"
                        or row["suggested_game_id"] is not None or pending):
                    old_game = conn.execute(
                        "SELECT id,slug FROM games WHERE slug=?" if pending else
                        "SELECT id,slug FROM games WHERE id=?",
                        (pending["old_slug"] if pending else row["game_id"],)
                    ).fetchone()
                    if old_game and int(old_game["id"]) != canonical_id:
                        _queue_game_redirect_candidate(
                            conn, old_game["slug"], target["slug"],
                            source_id, external_id)
                        remaining = conn.execute(
                            "SELECT 1 FROM game_sources WHERE game_id=?"
                            " AND NOT (source_id=? AND external_id=?) LIMIT 1",
                            (old_game["id"], source_id, external_id)).fetchone()
                        if remaining:
                            result["redirect_pending"] += 1
                    conn.execute(
                        "UPDATE game_sources SET game_id=?,match_status='manual_match',"
                        "match_confidence=1.0,suggested_game_id=NULL "
                        "WHERE source_id=? AND external_id=?",
                        (canonical_id, source_id, external_id))
                    result["same"] += 1
                continue

            # A manual distinct decision must undo an automatic exact match if
            # one was made. Keep the original row intact and give this source a
            # deterministic, source-namespaced catalogue row of its own.
            source_slug = _source_review_slug(source_id, external_id)
            conn.execute(
                "DELETE FROM pending_game_redirects WHERE source_id=?"
                " AND external_id=?", (source_id, external_id))
            current_game = conn.execute(
                "SELECT id,slug,origin FROM games WHERE id=?", (row["game_id"],)
            ).fetchone()
            if current_game is None or current_game["origin"] != "site":
                result["stale"] += 1
                continue
            if current_game["slug"] != source_slug:
                new_id = None
                collision = conn.execute(
                    "SELECT id,origin FROM games WHERE slug=?", (source_slug,)
                ).fetchone()
                if collision is not None:
                    mapped_identity = conn.execute(
                        "SELECT 1 FROM game_sources WHERE game_id=? AND source_id=? "
                        "AND external_id=? LIMIT 1",
                        (collision["id"], source_id, external_id)).fetchone()
                    if mapped_identity and collision["origin"] == "site":
                        # A prior run created the source-specific row and the
                        # mapping is being retried after a partial write.
                        new_id = int(collision["id"])
                    else:
                        source_slug += "-" + hashlib.sha256(
                            (source_id + ":" + external_id).encode("utf-8")
                        ).hexdigest()[:10]
                        collision = conn.execute(
                            "SELECT id,origin FROM games WHERE slug=?", (source_slug,)
                        ).fetchone()
                        if collision is not None:
                            mapped_identity = conn.execute(
                                "SELECT 1 FROM game_sources WHERE game_id=? "
                                "AND source_id=? AND external_id=? LIMIT 1",
                                (collision["id"], source_id, external_id)).fetchone()
                            if (mapped_identity
                                    and collision["origin"] == "site"):
                                new_id = int(collision["id"])
                            else:
                                result["stale"] += 1
                                continue
                if new_id is None:
                    tags = []
                    try:
                        parsed = json.loads(row["tags_json"] or "[]")
                        if isinstance(parsed, list):
                            tags = [str(tag) for tag in parsed[:24]]
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    source_url = (row["source_url"]
                                  if source_id == "f95zone" else "")
                    new_id, _created = slg_db.upsert_game(
                        conn, slug=source_slug, url=source_url or "",
                        title=row["source_title"] or source_slug,
                        version=row["source_version"], developer=row["developer"],
                        engine=row["engine"], last_updated=row["source_modified"],
                        tags=tags, complete=0)
                    if row["overview"]:
                        conn.execute(
                            "UPDATE games SET overview=COALESCE(overview,?) WHERE id=?",
                            (row["overview"], new_id))
                conn.execute(
                    "UPDATE game_sources SET game_id=? WHERE source_id=? AND external_id=?",
                    (new_id, source_id, external_id))
            conn.execute(
                "UPDATE game_sources SET match_status='manual_distinct',"
                "match_confidence=1.0,suggested_game_id=NULL "
                "WHERE source_id=? AND external_id=?",
                (source_id, external_id))
            result["distinct"] += 1
        _finish_pending_game_redirects(conn, result)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result


def publish(data_dir=DATA_DIR, publish_dir=PUBLISH_DIR):
    """Copy a consistent snapshot and write manifest.json. Returns the manifest."""
    src_db = os.path.join(data_dir, "slgking.db")
    if not os.path.isfile(src_db):
        raise SystemExit("找不到 %s" % src_db)

    # Reapply saved decisions before taking a snapshot. This repairs the
    # cross-database crash window between storing an admin decision and applying
    # it to the site catalogue.
    _apply_source_review_decisions(data_dir, publish_dir)

    os.makedirs(os.path.join(publish_dir, "covers"), exist_ok=True)

    # Build in a private sibling directory on the same filesystem. The source
    # may contain the author's own library; even a static web server that serves
    # all of publish_dir must not be able to expose this unfinished copy.
    pub_db = os.path.join(publish_dir, "slgking.db")
    staging_dir = tempfile.mkdtemp(prefix=".slgking-publish-",
                                   dir=os.path.dirname(os.path.abspath(publish_dir)))
    staged_db = os.path.join(staging_dir, "slgking.db")
    try:
        # A single backup() is a consistent point-in-time copy, unlike a plain
        # file copy of a WAL-mode db that cron might be mid-write on.
        src = sqlite3.connect(src_db)
        dst = sqlite3.connect(staged_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # Strip all personal data before the staging file can be served.
        slg_db.strip_to_site_catalogue(staged_db)

        # The Zyoudao adapter is an internal enrichment feed. Distribute its
        # normalized game fields through the canonical game row, but keep its URL,
        # external post identity and source metadata server-side. Public clients
        # only receive selectable Dikgames/F95zone destination records.
        conn = sqlite3.connect(staged_db)
        try:
            conn.execute("DROP TABLE IF EXISTS source_sync_state")
            # This is a per-device wishlist checkpoint, not catalogue data.
            # Keep this explicit while older slg_db versions still omit it
            # from their private-table list.
            conn.execute("DROP TABLE IF EXISTS wishlist_version_seen")
            game_cols = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
            game_updates = []
            if "last_synced" in game_cols:
                game_updates.append("last_synced=NULL")
            if "fetch_failures" in game_cols:
                game_updates.append("fetch_failures=0")
            if game_updates:
                conn.execute("UPDATE games SET " + ",".join(game_updates))
            source_cols = {r[1] for r in conn.execute("PRAGMA table_info(game_sources)")}
            if source_cols:
                conn.execute("DELETE FROM game_sources WHERE source_id NOT IN"
                             " ('dikgames', 'f95zone')")
                # These are private reconciliation and crawler fields. Public
                # clients only need each source's display metadata and URL.
                # Clearing volatile timestamps also avoids needless catalogue
                # downloads when a crawl found no user-visible change.
                safe_defaults = {
                    "fetched_at": "fetched_at=''",
                    "content_hash": "content_hash=NULL",
                    "match_status": "match_status='new'",
                    "match_confidence": "match_confidence=NULL",
                    "suggested_game_id": "suggested_game_id=NULL",
                }
                source_updates = [assignment for name, assignment in
                                  safe_defaults.items() if name in source_cols]
                if source_updates:
                    conn.execute("UPDATE game_sources SET " +
                                 ",".join(source_updates))
            _sanitize_game_redirects(conn)
            conn.commit()
            # Deleted SQLite rows can remain recoverable in free pages. Compact
            # after removing internal source URLs and the local wishlist table.
            conn.execute("VACUUM")
        finally:
            conn.close()

        conn = sqlite3.connect(staged_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            lastmod = conn.execute("SELECT MAX(lastmod) FROM games").fetchone()[0]
            covers = {r[0] for r in conn.execute(
                "SELECT cover_file FROM games WHERE cover_file IS NOT NULL"
                " AND cover_file NOT LIKE 'pending:%'")}
        finally:
            conn.close()

        # Covers are immutable per slug, so only copy the ones not already there -
        # and only those a site game actually references, so a user's own cover art
        # never ships to the public snapshot.
        src_covers = os.path.join(data_dir, "covers")
        dst_covers = os.path.join(publish_dir, "covers")
        if os.path.isdir(src_covers):
            for name in covers:
                if not name or os.path.isfile(os.path.join(dst_covers, name)):
                    continue
                src_path = os.path.join(src_covers, name)
                if os.path.isfile(src_path):
                    with open(src_path, "rb") as fh:
                        with open(os.path.join(dst_covers, name), "wb") as out:
                            out.write(fh.read())

        sha = hashlib.sha256()
        with open(staged_db, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        # Older clients use only manifest.lastmod to decide whether to pull.
        # A human source mapping can change game_sources without changing any
        # games.lastmod, so include the published bytes in that cursor.
        from slg_catalogue_publish import publish_snapshot
        return publish_snapshot(staged_db, publish_dir, count=count,
                                lastmod=lastmod, generated_at=_now())
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


# Explicit homepage assets; no arbitrary files or directory traversal.
_HOME_ASSET_ALLOWLIST = {
    "home.css": "text/css; charset=utf-8",
    "home.js": "application/javascript; charset=utf-8",
    "overview.webp": "image/webp",
    "comments.webp": "image/webp",
    "library.webp": "image/webp",
}


class CatalogueHTTPServer(http.server.ThreadingHTTPServer):
    request_queue_size = 32
    worker_count = 16
    queued_request_limit = 16
    request_socket_timeout = 30

    def __init__(self, *args, **kwargs):
        self._stopping = threading.Event()
        self._worker_lock = threading.Lock()
        self._request_sockets = set()
        self._outstanding = threading.BoundedSemaphore(32)
        self._queued_requests = threading.BoundedSemaphore(self.queued_request_limit)
        self._executor = None
        self._serve_thread_id = None
        self._closed = False
        super().__init__(*args, **kwargs)
        self._executor = ThreadPoolExecutor(max_workers=self.worker_count,
                                            thread_name_prefix="catalogue-http")

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.request_socket_timeout)
        return request, address

    def serve_forever(self, poll_interval=0.5):
        if self._stopping.is_set():
            return
        self._serve_thread_id = threading.get_ident()
        try:
            super().serve_forever(poll_interval)
        finally:
            self._serve_thread_id = None

    def _acquire_request_slot(self, semaphore):
        while not self._stopping.is_set():
            if semaphore.acquire(timeout=0.05):
                return True
        return False

    def process_request(self, request, client_address):
        with self._worker_lock:
            self._request_sockets.add(request)
        outstanding = queued = submitted = False
        try:
            outstanding = self._acquire_request_slot(self._outstanding)
            if not outstanding:
                return
            queued = self._acquire_request_slot(self._queued_requests)
            if not queued or self._stopping.is_set():
                return
            self._executor.submit(self._run_request, request, client_address)
            submitted = True
        except RuntimeError:
            if not self._stopping.is_set():
                raise
        finally:
            if not submitted:
                if queued:
                    self._queued_requests.release()
                if outstanding:
                    self._outstanding.release()
                self.shutdown_request(request)
                with self._worker_lock:
                    self._request_sockets.discard(request)

    def _run_request(self, request, client_address):
        self._queued_requests.release()
        try:
            if not self._stopping.is_set():
                self.finish_request(request, client_address)
        except Exception:
            if not self._stopping.is_set():
                self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            with self._worker_lock:
                self._request_sockets.discard(request)
            self._outstanding.release()

    def shutdown(self):
        self._stopping.set()
        super().shutdown()

    def server_close(self):
        with self._worker_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            requests = list(self._request_sockets)
        # Interrupt active reads and queued sockets before waiting for workers.
        for request in requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                descriptor = request.detach()
                if descriptor != -1:
                    socket.close(descriptor)
            except OSError:
                pass
        if self._serve_thread_id is not None and self._serve_thread_id != threading.get_ident():
            self.shutdown()
        super().server_close()
        if self._executor is not None:
            self._executor.shutdown(wait=True)


class Handler(http.server.BaseHTTPRequestHandler):
    publish_dir = PUBLISH_DIR
    data_dir = DATA_DIR

    def _cloud_path(self):
        return os.path.join(os.path.dirname(self.publish_dir),
                            "cloud", "cloud.sqlite3")

    def _cloud(self):
        path = self._cloud_path()
        with _CLOUD_STORES_LOCK:
            store = _CLOUD_STORES.get(path)
            if store is None:
                store = CloudStore(path)
                _CLOUD_STORES[path] = store
            return store

    def _usage_store(self):
        from slg_usage_metrics import UsageStore
        return UsageStore(os.path.join(os.path.dirname(self._cloud_path()), 'usage.sqlite3'))

    def _collection_store(self):
        from slg_collection_jobs import CollectionJobs
        return CollectionJobs(os.path.join(os.path.dirname(self._cloud_path()),
                                          'collection_jobs.sqlite3'),
                              source_status=__import__('slg_collection_worker').collection_source_availability)

    def _serve_admin_operations(self, path, writing=False):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        if path == '/admin/shop':
            if writing:
                raise CloudError('not found', 404)
            self._send_json({'ok': True, **self._cloud().admin_shop_items()})
            return
        if path.startswith('/admin/shop/'):
            if not writing or not path.endswith('/availability'):
                raise CloudError('not found', 404)
            item = path[len('/admin/shop/'):-len('/availability')]
            obj = self._json_body(maximum=2048)
            if set(obj) != {'selling', 'reason', 'expected_revision'}:
                raise CloudError('invalid shop settings')
            self._send_json(self._cloud().admin_set_shop_availability(
                item, obj['selling'], obj['reason'], obj['expected_revision']))
            return
        from slg_collection_jobs import CollectionConflict
        store = self._collection_store() if writing else None
        try:
            if path == '/admin/collection-jobs':
                if writing:
                    obj = self._json_body(maximum=16384)
                    if set(obj) - {'source_id', 'mode', 'target_new', 'targets', 'request_id'}:
                        raise ValueError('invalid collection fields')
                    value = store.create(**obj)
                    self._cloud().append_admin_audit('collection_create', value['id'],
                                                     {'source_id': value['source_id']})
                    self._send_json({'ok': True, 'job': value}, 202)
                else:
                    query = parse_qs(urlparse(self.path).query)
                    if set(query) - {'limit', 'offset'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('invalid collection query')
                    from slg_collection_jobs import read_jobs, LIMITS
                    from slg_collection_worker import collection_source_availability
                    self._send_json({'ok': True, **read_jobs(
                        os.path.join(os.path.dirname(self._cloud_path()), 'collection_jobs.sqlite3'),
                        limit=int(query.get('limit', ['25'])[0]),
                        offset=int(query.get('offset', ['0'])[0])),
                        'sources': [collection_source_availability(source) for source in LIMITS]})
            else:
                parts = path[len('/admin/collection-jobs/'):].split('/')
                if len(parts) == 1 and not writing:
                    from slg_collection_jobs import read_job
                    value = read_job(os.path.join(os.path.dirname(self._cloud_path()), 'collection_jobs.sqlite3'), parts[0])
                    if value is None:
                        raise KeyError('not found')
                elif len(parts) == 2 and writing and parts[1] in ('cancel', 'retry'):
                    if self._json_body(maximum=1024):
                        raise ValueError('body must be empty')
                    value = getattr(store, parts[1])(parts[0])
                    self._cloud().append_admin_audit('collection_' + parts[1], parts[0])
                else:
                    raise KeyError('not found')
                self._send_json({'ok': True, 'job': value})
        except CollectionConflict as exc:
            raise CloudError(str(exc), 409) from None
        except ValueError as exc:
            raise CloudError(str(exc), 400) from None
        except KeyError:
            raise CloudError('collection job not found', 404) from None

    def _forum(self):
        path = self._cloud_path()
        with _FORUM_STORES_LOCK:
            store = _FORUM_STORES.get(path)
            if store is None:
                store = ForumStore(path, author_profiles=self._cloud().public_author_profiles)
                _FORUM_STORES[path] = store
            return store

    def _maintenance_store(self):
        path = os.path.join(os.path.dirname(self.publish_dir),
                            "maintenance.sqlite3")
        with _MAINTENANCE_STORES_LOCK:
            store = _MAINTENANCE_STORES.get(path)
            if store is None:
                store = MaintenanceStore(path)
                _MAINTENANCE_STORES[path] = store
            return store

    @staticmethod
    def _maintenance_iso(timestamp):
        if timestamp is None:
            return None
        return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")

    def _record_maintenance_event(self, kind, outcome):
        # Operational telemetry must never break the account operation it observes.
        try:
            self._maintenance_store().record_event(kind, outcome)
        except Exception:  # noqa: BLE001 - observation must not fail a business API
            sys.stderr.write("[%s] maintenance event write failed\n" % _now())

    def _measure_maintenance_operation(self, kind, action):
        self._defer_maintenance_json = True
        self._deferred_maintenance_json = None
        try:
            result = action()
        except CloudError as exc:
            outcome = "client_error" if 400 <= exc.status < 500 else "server_error"
            self._record_maintenance_event(kind, outcome)
            self._defer_maintenance_json = False
            self._deferred_maintenance_json = None
            raise
        except Exception:
            self._record_maintenance_event(kind, "server_error")
            self._defer_maintenance_json = False
            self._deferred_maintenance_json = None
            raise
        self._record_maintenance_event(kind, "success")
        response = self._deferred_maintenance_json
        self._defer_maintenance_json = False
        self._deferred_maintenance_json = None
        if response is not None:
            self._send_json(response[0], status=response[1], headers=response[2])
        return result

    def _cloud_call(self, action):
        account_request = self.path.startswith(("/account/", "/web/account/", "/web/personal-comments"))
        # The page boot checks its lightweight Cookie session alongside public
        # browsing; that lookup must not reject its own initial catalogue read.
        if self.path.split("?", 1)[0] == "/account/session" and self.command == "GET":
            account_request = False
        from slg_cloud_store import business_platform
        platform = 'web' if self.path.startswith('/web/') else self.headers.get('X-SLG-Client', 'unknown')
        # Native platforms are explicitly self-reported; never infer them from UA.
        with _account_request_activity(account_request), business_platform(platform):
            self._cloud_call_result(action)

    def _cloud_call_result(self, action):
        try:
            action()
        except CloudError as exc:
            body = {"ok": False, "error": str(exc)}
            if isinstance(exc, OwnerAccountAlreadyBound):
                body["account_id"] = exc.account_id
            self._send_json(body, status=exc.status,
                            headers={"Retry-After": "10"} if exc.status == 429 else None)
        except ForumError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
        except (sqlite3.Error, OSError) as exc:
            # Never expose private filesystem paths or database internals.
            self._send_json({"ok": False, "error": "storage unavailable"}, status=503)

    def _json_body(self, maximum=4096):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= maximum:
                raise ValueError("bad length")
            obj = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("bad body")
            return obj
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CloudError("bad body", 400)

    def _account_id(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise CloudError("unauthorized", 401)
        return self._cloud().authenticate(auth[7:].strip())[0]

    def _auth_identity(self):
        peer = str(self.client_address[0])
        if peer in ("127.0.0.1", "::1"):
            # Only trust proxy headers when the socket peer itself is local.
            # The last XFF hop is the client seen by the trusted reverse proxy.
            candidates = (self.headers.get("X-Real-IP", ""),
                          self.headers.get("X-Forwarded-For", "").split(",")[-1])
            for candidate in candidates:
                try:
                    return "ip:" + str(ipaddress.ip_address(candidate.strip()))
                except ValueError:
                    pass
            # An old Nginx config without real-IP headers shares this bucket.
            # CloudStore permits a larger bounded budget for this case.
            return "proxy_unknown"
        return peer

    def _catalogue_game_exists(self, game_id):
        if isinstance(game_id, bool) or not isinstance(game_id, int) or game_id <= 0:
            return False
        path = os.path.join(self.publish_dir, "slgking.db")
        if not os.path.isfile(path):
            raise CloudError("catalogue unavailable", 503)
        uri = "file:%s?mode=ro" % quote(
            os.path.abspath(path).replace(os.sep, "/"), safe="/:")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                return conn.execute(
                    "SELECT 1 FROM games WHERE id=? AND origin='site'",
                    (game_id,)).fetchone() is not None
            finally:
                conn.close()
        except sqlite3.Error:
            raise CloudError("catalogue unavailable", 503)

    def _catalogue_canonical_slug(self, slug):
        if not isinstance(slug, str) or not slug.strip() or len(slug) > 200:
            raise CloudError("not found", 404)
        if slug.strip().lower().startswith("dlsite-"):
            return WebCatalogue(self.publish_dir).canonical_slug(slug.strip())
        canonical = _resolve_public_game_slug(self.publish_dir, slug.strip())
        candidates = [canonical, unquote(canonical)]
        candidates = list(dict.fromkeys(value for value in candidates if value))
        conn = self._open_publish_db()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
            marks = ",".join("?" for _ in candidates)
            origin = "AND origin='site' " if "origin" in columns else ""
            row = conn.execute(
                "SELECT slug FROM games WHERE slug IN (%s) %sLIMIT 1" %
                (marks, origin), candidates).fetchone()
            if row is None:
                raise CloudError("not found", 404)
            return str(row["slug"])
        except sqlite3.Error:
            raise CloudError("catalogue unavailable", 503)
        finally:
            conn.close()

    def _catalogue_slugs_for_title(self, query):
        query = str(query or "").strip()[:100]
        if not query:
            return []
        path = os.path.join(self.publish_dir, "slgking.db")
        if not os.path.isfile(path):
            return []
        uri = "file:%s?mode=ro" % quote(
            os.path.abspath(path).replace(os.sep, "/"), safe="/:")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                rows = conn.execute(
                    "SELECT slug FROM games WHERE origin='site' "
                    "AND title LIKE ? ESCAPE '\\' ORDER BY id LIMIT 500",
                    ("%" + escaped + "%",)).fetchall()
                return [str(row[0]) for row in rows if row[0]]
            finally:
                conn.close()
        except sqlite3.Error:
            return []

    def _account_post(self, path):
        store = self._cloud()
        obj = self._json_body()
        if path == "/account/referrals/redeem":
            if set(obj) != {"code"}:
                raise CloudError("bad referral body", 400)
            self._send_json(store.redeem_referral(
                self._account_id(), obj.get("code")))
        elif path == "/account/applications":
            if set(obj) - {"submission_key", "display_name", "contact", "reason"}:
                raise CloudError("bad application fields")
            result = store.submit_account_application(
                obj.get("submission_key"), obj.get("display_name", ""),
                obj.get("contact", ""), obj.get("reason", ""),
                identity=self._auth_identity())
            duplicate = result.get("duplicate")
            self._send_json({"application_id": str(result["id"]),
                             "status": result["status"]},
                            200 if duplicate else 201)
        elif path == "/account/create":
            self._send_json(store.create(obj.get("device_label", ""),
                                         identity=self._auth_identity()), 201)
        elif path == "/account/admin-login":
            if not self._has_owner_key():
                self._send_unauthorized()
                return
            self._send_json(store.developer_login(
                self.headers.get("X-SLG-Developer-Key", ""),
                old_token=obj.get("device_token"),
                label=obj.get("device_label", "管理员设备")))
        elif path == "/account/login":
            aid = obj.get("account_id", "")
            key = obj.get("login_key", "")
            self._send_json(store.login(aid, key, old_token=obj.get("device_token"),
                                        recovery=obj.get("recovery_code"),
                                        label=obj.get("device_label", ""),
                                        identity=self._auth_identity()))
        elif path == "/account/rotate":
            aid = self._account_id()
            self._send_json(store.rotate(aid, obj.get("login_key"),
                                         obj.get("recovery_code")))
        elif path == "/account/quests/claim":
            self._send_json(store.claim_quest(
                self._account_id(), obj.get("quest_id")))
        elif path == "/account/quests/rating":
            aid = self._account_id()
            game_id = obj.get("game_id")
            if not self._catalogue_game_exists(game_id):
                raise CloudError("public game not found", 404)
            self._send_json(store.record_quest_rating(
                aid, game_id, obj.get("rating")))
        elif path == "/account/legacy-request":
            aid = self._account_id()
            self._send_json(store.migration_request(
                aid, obj.get("points"), obj.get("titles"),
                obj.get("source_digest", "")))
        elif path == "/account/legacy-migrate":
            aid = self._account_id()
            self._send_json(store.migrate_legacy(
                aid, obj.get("source_id"), obj.get("points"),
                obj.get("titles")))
        elif path == "/rewards/group":
            aid = self._account_id()
            self._send_json(store.redeem_group(aid, obj.get("code")))
        elif path == "/account/signin":
            if self._cloud_feature_disabled("disable_signin"):
                raise CloudError("signin disabled", 503)
            aid = self._account_id()
            self._send_json(store.signin(aid))
        elif path == "/account/shop/buy":
            aid = self._account_id()
            self._send_json(store.buy(aid, obj.get("item_id")))
        elif path == "/account/lottery/draw":
            if self._cloud_feature_disabled("disable_lottery"):
                raise CloudError("lottery disabled", 503)
            aid = self._account_id()
            # Accept a stable client request ID so a retry after a lost response
            # returns the original prize without charging or recording a second draw.
            self._send_json(store.lottery(aid, obj.get("request_id")))
        elif path == "/account/equip":
            aid = self._account_id()
            self._send_json(store.equip(aid, title_id=obj.get("title_id"),
                                        cosmetic_id=obj.get("cosmetic_id"),
                                        appearance_slot=obj.get("appearance_slot"),
                                        item_id=obj.get("item_id")))
        elif path == "/account/profile":
            aid = self._account_id()
            profile_fields = {}
            if "nickname" in obj:
                profile_fields["nickname"] = obj["nickname"]
            if "profile_message" in obj:
                profile_fields["profile_message"] = obj["profile_message"]
                if (isinstance(obj["profile_message"], str)
                        and obj["profile_message"].strip()):
                    try:
                        from slg_moderation import classify
                    except ImportError:
                        raise CloudError("profile message moderation unavailable", 503)
                    profile_fields["moderation"] = classify
            self._send_json(store.profile(aid, **profile_fields))
        elif path == "/account/feedback":
            aid = self._account_id()
            self._send_json(store.submit_feedback(
                aid, obj.get("category"), obj.get("content")), 201)
        else:
            self.send_error(404, "not found")

    def _account_developer_grant_all_titles(self):
        """Owner-only title grant; local client flags are never trusted here."""
        if not self._has_owner_key():
            self._send_unauthorized()
            return
        # The request may use the client's standard JSON helper, but it may not
        # choose a target account or any grant contents. The server owns both.
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raise CloudError("bad length", 400)
        if length < 0:
            raise CloudError("bad length", 400)
        if length:
            obj = self._json_body(maximum=4096)
            if obj:
                raise CloudError("bad body")
        self._send_json(self._cloud().grant_all_developer_appearances())

    def _account_get(self, path):
        store = self._cloud()
        aid = self._account_id()
        if path == "/account/referrals/me":
            self._send_json(store.referral_info(aid))
        elif path == "/account/me":
            self._send_json(store.me(aid))
        elif path == "/account/devices":
            self._send_json({"devices": store.devices(aid)})
        elif path == "/account/comments":
            query = parse_qs(urlparse(self.path).query)
            game = query.get("game", [""])[0]
            if game:
                game, game_slugs = _public_game_slugs(self.publish_dir, game)
                comments = store.comments(game, aid=aid,
                                          game_slugs=game_slugs)
            else:
                comments = store.comments("", aid=aid)
            for comment in comments:
                comment.pop("account_id", None)
                comment.pop("account_banned", None)
            self._send_json({"comments": comments})
        elif path == "/account/quests":
            self._send_json(store.quests(aid))
        else:
            self.send_error(404, "not found")

    def _account_comment_post(self, obj):
        aid = self._account_id()
        try:
            from slg_moderation import classify_comment
        except ImportError:
            self._send_json({"ok": False, "error": "comment moderation unavailable"},
                            status=503)
            return
        game = _resolve_public_game_slug(self.publish_dir, obj.get("game", ""))
        store = self._cloud()
        result = store.post_comment(aid, game, obj.get("content", ""),
                                    obj.get("nickname", ""), classify_comment)
        if result.get("status") == "pending" and not result.get("duplicate"):
            self._queue_admin_pending_notification(store)
        self._send_json(result, 201)

    def _send_file(self, path, content_type, cache_control=None,
                   extra_headers=None):
        try:
            fh = open(path, "rb")
        except OSError:
            self.send_error(404, "not found")
            return
        # Pin the descriptor before reading its size. publish() atomically
        # replaces catalogue files, so path-based getsize followed by open
        # could otherwise send a Content-Length for a different file version.
        with fh:
            size = os.fstat(fh.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            if cache_control:
                self.send_header("Cache-Control", cache_control)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # --- /m/ web client (0.25.0 sample) -------------------------------------

    def _serve_web_static(self, path):
        """Serve the hand-written /m/ single-page assets from an allowlist."""
        name = "index.html" if path in ("/m", "/m/") else path[len("/m/"):]
        spec = _WEB_ASSET_ALLOWLIST.get(name)
        if (not spec or "/" in name or "\\" in name
                or os.path.basename(name) != name):
            self._send_json({"ok": False, "error": "not found"}, status=404,
                            headers=_WEB_NOINDEX)
            return
        self._send_file(os.path.join(os.path.dirname(__file__), "web", name),
                        spec, "no-store", _WEB_NOINDEX)

    def _open_publish_db(self):
        path = os.path.join(self.publish_dir, "slgking.db")
        if not os.path.isfile(path):
            raise CloudError("catalogue unavailable", 503)
        uri = "file:%s?mode=ro" % quote(
            os.path.abspath(path).replace(os.sep, "/"), safe="/:")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3)
        except sqlite3.Error:
            raise CloudError("catalogue unavailable", 503)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _catalogue_tags(conn, game_ids):
        if not game_ids:
            return {}
        marks = ",".join("?" for _ in game_ids)
        tags = {}
        for game_id, name in conn.execute(
                "SELECT gt.game_id, t.name FROM game_tags gt "
                "JOIN tags t ON t.id=gt.tag_id WHERE gt.game_id IN (%s) "
                "ORDER BY t.name" % marks, game_ids):
            tags.setdefault(game_id, []).append(name)
        return tags

    @staticmethod
    def _catalogue_item(row, tags):
        cover = row["cover_file"]
        return {"slug": row["slug"], "title": row["title"], "catalog_id": "western_slg",
                "developer": row["developer"] or "", "engine": row["engine"] or "",
                "rating": row["rating"], "version": row["version"] or "",
                "updated": row["last_updated"] or "",
                "cover": "/covers/" + cover if cover else None,
                "tags": tags.get(row["id"], [])}

    def _serve_catalogue_tags(self):
        """List public catalogue tags with display labels and game counts."""
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if set(query) - {"catalog_id"} or any(len(v) != 1 for v in query.values()):
            raise CloudError("bad query")
        source = query.get("catalog_id", ["western_slg"])[0]
        if source != "western_slg":
            self._send_json(WebCatalogue(self.publish_dir).tags(source))
            return
        conn = self._open_publish_db()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
            where = "WHERE g.origin='site'" if "origin" in columns else ""
            rows = conn.execute(
                "SELECT t.name AS slug, COUNT(DISTINCT g.id) AS count "
                "FROM tags t JOIN game_tags gt ON gt.tag_id=t.id "
                "JOIN games g ON g.id=gt.game_id %s "
                "GROUP BY t.id,t.name "
                "ORDER BY count DESC,t.name COLLATE NOCASE ASC" % where
            ).fetchall()
        except sqlite3.Error:
            raise CloudError("catalogue unavailable", 503)
        finally:
            conn.close()

        labels_path = os.path.join(os.path.dirname(__file__), "assets", "tag_zh.json")
        try:
            with open(labels_path, "r", encoding="utf-8") as fh:
                raw_labels = json.load(fh)
        except (OSError, ValueError):
            raw_labels = {}
        labels = raw_labels if isinstance(raw_labels, dict) else {}
        tags = []
        for row in rows:
            slug = row["slug"]
            if not isinstance(slug, str) or not slug:
                continue
            label = labels.get(slug)
            if not isinstance(label, str) or not label.strip():
                label = slug
            tags.append({"slug": slug, "label": label, "count": int(row["count"])})
        self._send_json({"ok": True, "tags": tags})

    def _catalogue_request(self, action):
        with _catalogue_admission():
            action()

    def _serve_catalogue_filters(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if set(query) - {"catalog_id"} or any(len(v) != 1 for v in query.values()):
            raise CloudError("bad query")
        self._send_json(WebCatalogue(self.publish_dir).filters(query.get("catalog_id", ["dlsite"])[0]))

    def _serve_catalogue_search(self):
        """Paged catalogue search straight off the published snapshot.

        The browser never downloads the whole 5.8 MB db; browse traffic stays a
        few KB per page, which is the point on a phone.
        """
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if "catalog_id" in query and len(query["catalog_id"]) != 1:
            raise CloudError("bad query")
        source = query.pop("catalog_id", ["western_slg"])[0]
        if source != "western_slg":
            self._send_json(WebCatalogue(self.publish_dir).search({**query, "catalog_id": [source]}))
            return
        if set(query) - {"q", "tag", "sort", "page", "limit"}:
            raise CloudError("bad query")
        if any(len(values) != 1 for key, values in query.items() if key != "tag"):
            raise CloudError("bad query")
        text = query.get("q", [""])[0].strip()
        if len(text) > 100:
            raise CloudError("bad query")
        tags = []
        for raw in query.get("tag", []):
            name = raw.strip()
            if not name or len(name) > 40:
                raise CloudError("bad query")
            if name not in tags:
                tags.append(name)
        if len(tags) > _CATALOGUE_TAG_MAX:
            raise CloudError("bad query")
        sort = query.get("sort", ["heat"])[0]
        if sort not in _CATALOGUE_SORTS:
            raise CloudError("bad query")
        try:
            page = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["20"])[0])
        except ValueError:
            raise CloudError("bad query")
        if not 1 <= page <= 1000 or not 1 <= limit <= _CATALOGUE_PAGE_MAX:
            raise CloudError("bad query")

        conn = self._open_publish_db()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
            where, params = [], []
            # A snapshot predating the origin column cannot hold user games, so
            # it needs no filter; the same allowance strip_to_site_catalogue makes.
            if "origin" in columns:
                where.append("g.origin='site'")
            if text:
                escaped = (text.replace("\\", "\\\\")
                           .replace("%", "\\%").replace("_", "\\_"))
                where.append("g.title LIKE ? ESCAPE '\\'")
                params.append("%" + escaped + "%")
            if tags:
                marks = ",".join("?" for _ in tags)
                where.append(
                    "(SELECT COUNT(DISTINCT t.name) FROM game_tags gt "
                    "JOIN tags t ON t.id=gt.tag_id WHERE gt.game_id=g.id "
                    "AND t.name IN (%s))=?" % marks)
                params.extend(tags)
                params.append(len(tags))
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(
                "SELECT COUNT(*) FROM games g " + clause, params).fetchone()[0]
            rows = conn.execute(
                "SELECT g.id,g.slug,g.title,g.developer,g.engine,g.rating,"
                "g.version,g.last_updated,g.cover_file FROM games g " + clause
                + " ORDER BY " + _CATALOGUE_SORTS[sort] + " LIMIT ? OFFSET ?",
                params + [limit, (page - 1) * limit]).fetchall()
            tag_map = self._catalogue_tags(conn, [row["id"] for row in rows])
        except sqlite3.Error:
            raise CloudError("catalogue unavailable", 503)
        finally:
            conn.close()
        items = [self._catalogue_item(row, tag_map) for row in rows]
        self._send_json({"ok": True, "page": page, "limit": limit,
                         "total": total, "has_more": page * limit < total,
                         "items": items})

    def _serve_catalogue_game(self, slug):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if set(query) - {"catalog_id"} or any(len(v) != 1 for v in query.values()):
            raise CloudError("bad query")
        source = query.get("catalog_id", [None])[0]
        if source not in (None, "western_slg") or slug.lower().startswith("dlsite-"):
            self._send_json(WebCatalogue(self.publish_dir).game(slug, source))
            return
        if not 1 <= len(slug) <= _CATALOGUE_SLUG_MAX:
            raise CloudError("not found", 404)
        conn = self._open_publish_db()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
            origin = "AND origin='site' " if "origin" in columns else ""
            # Slugs are stored percent-encoded in the catalogue, so a request may
            # arrive either raw or once-decoded; match both rather than guess.
            candidates = [slug]
            decoded = unquote(slug)
            if decoded not in candidates:
                candidates.append(decoded)
            marks = ",".join("?" for _ in candidates)
            row = conn.execute(
                "SELECT id,slug,title,developer,engine,version,rating,overview,"
                "last_updated,cover_file FROM games WHERE slug IN (%s) %sLIMIT 1"
                % (marks, origin), candidates).fetchone()
            if not row:
                raise CloudError("not found", 404)
            tag_map = self._catalogue_tags(conn, [row["id"]])
            sources = []
            source_marks = ",".join("?" for _ in _PUBLIC_SOURCE_IDS)
            for source in conn.execute(
                    "SELECT source_id,source_url,source_title FROM game_sources "
                    "WHERE game_id=? AND source_id IN (%s) ORDER BY source_id"
                    % source_marks, [row["id"]] + list(_PUBLIC_SOURCE_IDS)):
                url = source["source_url"] or ""
                if not safe_source_url(source["source_id"], url, slug):
                    continue
                sources.append({"source_id": source["source_id"],
                                "source_url": url,
                                "source_title": source["source_title"] or ""})
        except sqlite3.Error:
            raise CloudError("catalogue unavailable", 503)
        finally:
            conn.close()
        item = self._catalogue_item(row, tag_map)
        item["overview"] = row["overview"] or ""
        item["sources"] = sources
        self._send_json({"ok": True, "game": item})

    def _serve_public_leaderboard(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if set(query) - {"board", "limit"} or any(len(v) != 1 for v in query.values()):
            raise CloudError("bad leaderboard parameters")
        board = query.get("board", ["points"])[0]
        raw_limit = query.get("limit", ["30"])[0]
        if (board not in ("points", "wardrobe", "titles") or len(raw_limit) > 64
                or not re.fullmatch(r"[0-9]+", raw_limit)):
            raise CloudError("bad leaderboard parameters")
        limit = min(int(raw_limit), 30)
        if limit < 1:
            raise CloudError("bad pagination")
        key = "points" if board == "points" else "wardrobe"
        store = self._cloud()
        entry = _LEADERBOARD_CACHE.get(key)
        now = time.monotonic()
        if entry is None or entry[0] != store.path or entry[1] <= now:
            # Bound anonymous cold aggregation without queueing account work.
            if not _LEADERBOARD_CACHE_LOCK.acquire(blocking=False):
                raise CloudError("rate limited", 429)
            try:
                entry = _LEADERBOARD_CACHE.get(key)
                if entry is None or entry[0] != store.path or entry[1] <= now:
                    entries = store.leaderboard(key, 30)["entries"]
                    entry = (store.path, time.monotonic() + 30, entries)
                    _LEADERBOARD_CACHE[key] = entry
            finally:
                _LEADERBOARD_CACHE_LOCK.release()
        self._send_json({"board": board, "entries": entry[2][:limit]})

    def _web_session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(_WEB_SESSION_COOKIE)
        token = morsel.value if morsel else ""
        return token if len(token) <= 128 else ""

    def _serve_web_comments_get(self):
        aid = self._web_session()["account_id"]
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if set(query) - {"game", "page", "limit", "ids"} or any(
                len(values) != 1 for values in query.values()):
            raise CloudError("bad query")
        game = query.get("game", [""])[0]
        comment_ids = None
        if "ids" in query:
            comment_ids = query["ids"][0].split(",")
            if (not 1 <= len(comment_ids) <= 50 or any(
                    not value or len(value) > 128 for value in comment_ids)):
                raise CloudError("bad query")
        game_slugs = None
        if game:
            game, game_slugs = _public_game_slugs(self.publish_dir, game)
        result = self._cloud().account_comments_page(
            aid, game=game, game_slugs=game_slugs, comment_ids=comment_ids,
            page=query.get("page", ["1"])[0],
            limit=query.get("limit", ["20"])[0])
        self._send_json(result)

    def _serve_web_comments_write(self, path, method):
        if not self._same_origin_request():
            raise CloudError("cross-origin request blocked", 403)
        if not self._is_secure_transport():
            raise CloudError("HTTPS required", 403)
        aid = self._web_session()["account_id"]
        store = self._cloud()
        if path == "/web/comments" and method == "POST":
            obj = self._json_body()
            if not {"game", "content"}.issubset(obj) or set(obj) - {"game", "content", "operation_id"}:
                raise CloudError("bad body")
            if "operation_id" in obj and not isinstance(obj["operation_id"], str):
                raise CloudError("invalid operation")
            game = obj["game"]
            if not isinstance(game, str) or not game.strip():
                raise CloudError("bad fields")
            game = self._catalogue_canonical_slug(game)
            from slg_moderation import classify_comment
            result = store.post_comment(aid, game, obj["content"], "", classify_comment,
                                        operation_id=obj.get("operation_id"))
            if result.get("status") == "pending" and not result.get("duplicate"):
                self._queue_admin_pending_notification(store)
            self._send_json(result, 201)
            return
        parts = path[len("/web/comments/"):].split("/")
        cid = parts[0]
        if not cid or len(cid) > 128:
            raise CloudError("not found", 404)
        if method == "DELETE" and len(parts) == 1:
            store.delete_comment(aid, cid)
            self._send_json({"ok": True})
            return
        obj = self._json_body()
        if method == "POST" and len(parts) == 2:
            if parts[1] == "vote" and set(obj) == {"value"}:
                self._send_json(store.vote(aid, cid, obj["value"]))
                return
            if parts[1] == "report" and not obj:
                self._send_json(store.report(aid, cid))
                return
        raise CloudError("bad body")

    def _web_session(self):
        token = self._web_session_token()
        if not token:
            raise CloudError("unauthorized", 401)
        info = self._cloud().validate_web_session(token)
        if not info:
            raise CloudError("unauthorized", 401)
        return info

    def _serve_personal_comments(self, method):
        if not self._is_secure_transport():
            raise CloudError("HTTPS required", 403)
        if method != "GET" and not self._same_origin_request():
            raise CloudError("cross-origin request blocked", 403)
        aid = self._web_session()["account_id"]
        store = self._cloud()
        if method == "GET":
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            if set(query) - {"game", "page", "limit"} or any(len(v) != 1 for v in query.values()):
                raise CloudError("bad query")
            game = query.get("game", [""])[0]
            slugs = None
            if game:
                game, slugs = _public_game_slugs(self.publish_dir, game)
            self._send_json(store.personal_comments_page(
                aid, game, slugs, query.get("page", [1])[0], query.get("limit", [20])[0]))
        elif method == "POST":
            obj = self._json_body()
            if set(obj) != {"game", "content", "operation_id"}:
                raise CloudError("bad body")
            game = self._catalogue_canonical_slug(obj["game"])
            result = store.post_personal_comment(aid, game, obj["content"], obj["operation_id"])
            self._send_json(result, 200 if result["duplicate"] else 201)
        else:
            cid = self.path.split("?", 1)[0][len("/web/personal-comments/"):]
            if not re.fullmatch(r"[a-f0-9]{20}", cid):
                raise CloudError("not found", 404)
            self._send_json(store.delete_personal_comment(aid, cid))

    def _serve_web_shop(self, include_functional=False):
        import slg_titles
        shelf = self._cloud().shop_items()
        if not include_functional:
            shelf['items'] = [item for item in shelf['items']
                              if item.get('kind') in ('title', 'decoration')]
        self._send_json({'ok': True, **shelf,
                         'timezone': 'Asia/Shanghai', 'titles': slg_titles.TITLES})

    def _web_announcements(self):
        path = os.path.join(self.publish_dir, "config.json")
        try:
            if os.path.getsize(path) > 1024 * 1024:
                raise ValueError("oversized config")
            with open(path, encoding="utf-8") as source:
                config = json.load(source)
            if not isinstance(config, dict):
                raise ValueError("invalid config")
        except (OSError, ValueError):
            config = {}
        def plain(item):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                return None
            title, body = item.get("title", ""), item.get("body", "")
            if not isinstance(title, str) or not isinstance(body, str) or not (title.strip() or body.strip()):
                return None
            return {"id": item["id"][:64], "title": title[:200], "body": body[:10000]}
        current = plain(config.get("announcement"))
        if current:
            current["reward"] = self._maintenance_campaign(config=config)
        history = config.get("announcement_history", [])
        history = history[-50:] if isinstance(history, list) else []
        seen = {current["id"]} if current else set()
        result = []
        for item in reversed(history):
            entry = plain(item)
            if entry and entry["id"] not in seen:
                result.append(entry)
                seen.add(entry["id"])
        return current, result

    def _serve_web_announcements(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if set(query) - {"page", "limit"} or any(len(v) != 1 for v in query.values()):
            raise CloudError("bad query")
        try:
            page, limit = int(query.get("page", [1])[0]), int(query.get("limit", [20])[0])
        except (ValueError, TypeError):
            raise CloudError("bad pagination")
        if not 1 <= page <= 1000 or not 1 <= limit <= 50:
            raise CloudError("bad pagination")
        current, history = self._web_announcements()
        self._send_json({"ok": True, "current": current,
                         "history": history[(page - 1) * limit:page * limit],
                         "page": page, "total": len(history),
                         "total_pages": (len(history) + limit - 1) // limit})

    def _serve_web_maintenance_reward(self, claim=False):
        aid = self._web_session()["account_id"]
        current, _history = self._web_announcements()
        campaign = current.get("reward") if current else None
        if not claim:
            self._send_json({"ok": True, **self._cloud().maintenance_reward_status(
                aid, campaign["id"] if campaign else "", campaign)})
            return
        obj = self._json_body()
        if set(obj) != {"campaign_id"}:
            raise CloudError("bad body")
        if not campaign or obj["campaign_id"] != campaign["id"]:
            raise CloudError("campaign unavailable", 409)
        self._send_json(self._cloud().claim_maintenance_reward(aid, campaign["id"], campaign["amount"]))

    @staticmethod
    def _web_cookie(token, expires):
        return ("%s=%s; Max-Age=%d; Expires=%s; Path=/; HttpOnly; Secure; "
                "SameSite=Strict") % (_WEB_SESSION_COOKIE, token,
                                      _WEB_SESSION_MAX_AGE,
                                      formatdate(expires, usegmt=True))

    def _serve_web_session(self, method):
        if method in ("POST", "DELETE") and not self._same_origin_request():
            raise CloudError("cross-origin request blocked", 403)
        store = self._cloud()
        if method == "POST":
            obj = self._json_body()
            if set(obj) - {"account_id", "login_key", "recovery_code",
                           "device_label"}:
                raise CloudError("bad body")
            token, expires, aid = store.create_web_session(
                obj.get("account_id", ""), obj.get("login_key", ""),
                recovery=obj.get("recovery_code"),
                label=obj.get("device_label", "网页端"),
                identity=self._auth_identity())
            self._send_json({"ok": True, "account_id": aid,
                             "expires_at": expires}, status=201,
                            headers={"Set-Cookie": self._web_cookie(token, expires)})
            return
        if method == "GET":
            info = self._web_session()
            self._send_json({"ok": True, "account_id": info["account_id"],
                             "expires_at": info["expires"]})
            return
        info = self._web_session()
        token = self._web_session_token()
        if token:
            store.revoke_web_session(token)
        expired = ("%s=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
                   "Path=/; HttpOnly; Secure; SameSite=Strict"
                   % _WEB_SESSION_COOKIE)
        self._send_json({"ok": True, "account_id": info["account_id"]},
                        headers={"Set-Cookie": expired})

    def _serve_web_registration(self, path):
        store = self._cloud()
        if path == "/web/account/register":
            obj = self._json_body(maximum=512)
            if set(obj) != {"operation_id"}:
                raise CloudError("bad registration body", 400)
            result = store.register_web_account(
                obj.get("operation_id"), identity=self._auth_identity())
            token = result.pop("session_token")
            expires = result.pop("session_expires")
            status = 200 if result.get("duplicate") else 201
            self._send_json({"ok": True, **result, "expires_at": expires},
                            status=status,
                            headers={"Set-Cookie": self._web_cookie(token, expires)})
            return
        if path == "/web/account/register/pending":
            aid = self._web_session()["account_id"]
            pending = store.pending_web_registration_credentials(aid)
            self._send_json({"ok": True, **(pending or {
                "credentials_available": False})})
            return
        if path == "/web/account/register/ack":
            aid = self._web_session()["account_id"]
            obj = self._json_body(maximum=16)
            if obj:
                raise CloudError("bad body", 400)
            self._send_json(store.acknowledge_web_registration_credentials(aid))
            return
        self.send_error(404, "not found")

    @staticmethod
    def _web_title_display(title_id):
        if not isinstance(title_id, str) or not title_id:
            return None
        try:
            import slg_titles
            title = slg_titles.title_by_id(title_id)
        except ImportError:
            title = None
        name = title.get("name") if isinstance(title, dict) else None
        return {"id": title_id, "name": name if isinstance(name, str) else title_id}

    def _serve_web_account_get(self, path):
        info = self._web_session()
        aid = info["account_id"]
        store = self._cloud()
        if path == "/web/account/me":
            details = store.me(aid)
            account_fields = (
                "account_id", "created", "nickname", "balance",
                "profile_message", "signin_month_count", "signin_streak",
                "signin_month_days", "signed_today", "equipped_title", "titles",
                "equipped_appearances")
            account = {key: details[key] for key in account_fields}
            import slg_titles
            account["owned_item_ids"] = list(details["titles"])
            account["day"] = datetime.fromtimestamp(store.now(), CHINA_TZ).date().isoformat()
            account["timezone"] = "Asia/Shanghai"
            account["is_developer"] = aid == DEVELOPER_ACCOUNT_ID
            account["equipped_title"] = self._web_title_display(
                account.get("equipped_title"))
            account["titles"] = [self._web_title_display(title_id)
                                 for title_id in account.get("titles", [])
                                 if slg_titles.title_by_id(title_id)]
            self._send_json({
                "ok": True,
                "account": account,
            })
            return
        if path == "/web/account/quests":
            quests = store.quests(aid)
            self._send_json({
                "ok": True,
                "timezone": quests["timezone"],
                "day": quests["day"],
                "week": quests["week"],
                "tasks": quests["tasks"],
            })
            return
        self.send_error(404, "not found")

    def _serve_web_account_post(self, path):
        info = self._web_session()
        obj = self._json_body()
        aid = info["account_id"]
        if path == "/web/account/shop/buy":
            if set(obj) != {"item_id"}:
                raise CloudError("bad body")
            self._send_json(self._cloud().buy(aid, obj["item_id"]))
            return
        if path == "/web/account/equip":
            if set(obj) == {"title_id"}:
                import slg_titles
                result = self._cloud().equip(aid, title_id=(
                    slg_titles.DEFAULT_TITLE_ID if obj["title_id"] == "" else obj["title_id"]))
            elif set(obj) == {"appearance_slot", "item_id"}:
                result = self._cloud().equip(aid, appearance_slot=obj["appearance_slot"],
                                            item_id=obj["item_id"])
            else:
                raise CloudError("bad body")
            self._send_json(result)
            return
        if path == "/web/account/signin":
            if obj:
                raise CloudError("bad body", 400)
            if self._cloud_feature_disabled("disable_signin"):
                raise CloudError("signin disabled", 503)
            self._send_json(self._cloud().signin(aid))
            return
        if path == "/web/account/quests/claim":
            if (set(obj) != {"quest_id"}
                    or not isinstance(obj.get("quest_id"), str)):
                raise CloudError("bad body", 400)
            self._send_json(self._cloud().claim_quest(aid, obj["quest_id"]))
            return
        if path == "/web/account/profile":
            if not obj or set(obj) - {"nickname", "profile_message"}:
                raise CloudError("bad body", 400)
            fields = {}
            if "nickname" in obj:
                fields["nickname"] = obj["nickname"]
            if "profile_message" in obj:
                fields["profile_message"] = obj["profile_message"]
                if isinstance(obj["profile_message"], str) and obj["profile_message"].strip():
                    try:
                        from slg_moderation import classify
                    except ImportError:
                        raise CloudError("profile message moderation unavailable", 503)
                    fields["moderation"] = classify
            self._send_json(self._cloud().profile(aid, **fields))
            return
        self.send_error(404, "not found")

    def _forum_query(self, allowed):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if (set(query) - set(allowed)
                or any(len(values) != 1 for values in query.values())):
            raise CloudError("bad query", 400)
        try:
            page = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["20"])[0])
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination", 400)
        if page < 1 or page > 1000 or limit < 1 or limit > 30:
            raise CloudError("bad pagination", 400)
        return query, page, limit

    def _optional_web_account_id(self):
        try:
            return self._web_session()["account_id"]
        except CloudError as exc:
            if exc.status == 401:
                return None
            raise

    def _forum_rate_limit(self, aid, kind, account_max, ip_max):
        if not _forum_burst_allowed(aid, "write_account"):
            raise CloudError("rate limited", 429)
        cloud = self._cloud()
        cloud._writable()
        cloud._auth_limit(self._auth_identity(), "forum_%s_ip" % kind,
                          ip_max, 86400)
        cloud._auth_limit("account:" + aid, "forum_%s_account" % kind,
                          account_max, 86400)

    @staticmethod
    def _forum_moderation_precheck(title, content):
        if len(content.strip()) < 20:
            raise CloudError("forum content too short", 400)
        try:
            from slg_moderation import classify_discussion
        except ImportError:
            raise CloudError("forum moderation unavailable", 503)
        combined = (title + "\n" + content).strip()
        verdict = classify_discussion(combined)
        if not isinstance(verdict, dict) or verdict.get("decision") not in (
                "approve", "review", "reject"):
            raise CloudError("forum moderation unavailable", 503)
        # Emergency manual mode affects new safe submissions, not old content.
        if (verdict["decision"] == "approve" and os.environ.get(
                "SLGKING_FORUM_AUTO_PUBLISH", "1").lower() in ("0", "false", "off")):
            verdict = {**verdict, "decision": "review",
                       "reason": "论坛自动公开已关闭，等待人工审核"}
        return verdict

    def _forum_request(self, action, write=False):
        admitted = False
        try:
            if len(self.path) > 2048:
                raise CloudError("bad query", 400)
            kind = "write_ip" if write else "read"
            if not _forum_burst_allowed(self._auth_identity(), kind):
                raise CloudError("rate limited", 429)
            with _forum_admission(write):
                admitted = True
                return action()
        except CloudError as exc:
            if not admitted and exc.status == 429 and self.command == "POST":
                self._discard_rejected_forum_body()
            raise

    def _discard_rejected_forum_body(self):
        # Headers and body may arrive separately. Consume only bounded bodies
        # before closing an early 429, without parsing or admitting business work.
        self.close_connection = True
        maximum = 2048 if self.path.startswith("/admin/forum/") else 65536
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return
        if not 0 < size <= maximum or self.headers.get("Transfer-Encoding"):
            return
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + 0.2
        try:
            while size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(size, 8192))
                if not chunk:
                    break
                size -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def _serve_web_forum_get(self, path):
        forum = self._forum()
        if path == "/web/forum/threads":
            query, page, limit = self._forum_query({"page", "limit", "game"})
            game_slug = query.get("game", [""])[0].strip()
            if game_slug:
                game_slug = self._catalogue_canonical_slug(game_slug)
            result = forum.list_threads(page, limit, game_slug or None)
            self._send_json({"ok": True, **result})
            return
        if path == "/web/forum/my":
            query, page, limit = self._forum_query({"kind", "page", "limit"})
            kind = query.get("kind", ["threads"])[0]
            if kind not in ("threads", "replies"):
                raise CloudError("bad query", 400)
            aid = self._web_session()["account_id"]
            result = (forum.list_my_threads(aid, page, limit) if kind == "threads"
                      else forum.list_my_replies(aid, page, limit))
            self._send_json({"ok": True, **result})
            return
        prefix = "/web/forum/threads/"
        if path.startswith(prefix):
            thread_id = unquote(path[len(prefix):])
            if not thread_id or "/" in thread_id:
                raise CloudError("not found", 404)
            query, page, limit = self._forum_query({"page", "limit"})
            viewer_id = self._optional_web_account_id()
            thread = forum.get_thread(thread_id, viewer_id=viewer_id)
            replies = forum.list_replies(thread_id, page, limit, viewer_id=viewer_id)
            self._send_json({"ok": True, "thread": thread, "replies": replies})
            return
        self.send_error(404, "not found")

    def _serve_web_forum_post(self, path):
        info = self._web_session()
        aid = info["account_id"]
        obj = self._json_body(maximum=65536)
        forum = self._forum()
        if path == "/web/forum/threads":
            if (set(obj) - {"title", "content", "game_slug", "operation_id"}
                    or not isinstance(obj.get("title"), str)
                    or not isinstance(obj.get("content"), str)
                    or ("game_slug" in obj and not isinstance(obj["game_slug"], str))
                    or ("operation_id" in obj and not isinstance(obj["operation_id"], str))):
                raise CloudError("bad body", 400)
            title = obj["title"].strip()
            content = obj["content"].strip()
            if not title or len(title) > 160 or len(content) > 12000:
                raise CloudError("bad body", 400)
            self._forum_rate_limit(aid, "thread", 3, 12)
            moderation = self._forum_moderation_precheck(title, content)
            game_slug = obj.get("game_slug", "").strip()
            if game_slug:
                game_slug = self._catalogue_canonical_slug(game_slug)
            account = self._cloud().me(aid)
            thread = forum.create_thread(
                aid, title, content, account.get("nickname", ""), game_slug or None,
                moderation=moderation, operation_id=obj.get("operation_id"))
            self._send_json({"ok": True, "thread": thread}, status=201)
            return

        prefix = "/web/forum/threads/"
        if path.startswith(prefix) and path.endswith("/replies"):
            thread_id = unquote(path[len(prefix):-len("/replies")].rstrip("/"))
            if not thread_id or "/" in thread_id:
                raise CloudError("not found", 404)
            if (set(obj) - {"content", "parent_reply_id", "operation_id"}
                    or not isinstance(obj.get("content"), str)
                    or (obj.get("parent_reply_id") is not None
                        and not isinstance(obj.get("parent_reply_id"), str))
                    or ("operation_id" in obj and not isinstance(obj["operation_id"], str))):
                raise CloudError("bad body", 400)
            content = obj["content"].strip()
            if len(content) > 6000:
                raise CloudError("bad body", 400)
            self._forum_rate_limit(aid, "reply", 30, 120)
            moderation = self._forum_moderation_precheck("", content)
            account = self._cloud().me(aid)
            reply = forum.create_reply(
                aid, thread_id, content, account.get("nickname", ""),
                obj.get("parent_reply_id"), moderation=moderation,
                operation_id=obj.get("operation_id"))
            self._send_json({"ok": True, "reply": reply}, status=201)
            return

        if path == "/web/forum/reports":
            if (set(obj) - {"target_type", "target_id", "reason", "details"}
                    or not isinstance(obj.get("target_type"), str)
                    or not isinstance(obj.get("target_id"), str)
                    or not isinstance(obj.get("reason"), str)
                    or ("details" in obj and not isinstance(obj["details"], str))):
                raise CloudError("bad body", 400)
            self._forum_rate_limit(aid, "report", 20, 60)
            report = forum.report_content(
                aid, obj["target_type"], obj["target_id"], obj["reason"],
                obj.get("details", ""))
            self._send_json({"ok": True, "report": report}, status=201)
            return
        self.send_error(404, "not found")

    def _serve_web_forum_delete(self, path):
        aid = self._web_session()["account_id"]
        self._cloud()._writable()
        prefix = "/web/forum/threads/"
        if path.startswith(prefix):
            thread_id = unquote(path[len(prefix):])
            if not thread_id or "/" in thread_id:
                raise CloudError("not found", 404)
            self._send_json({"ok": True,
                             "thread": self._forum().soft_delete_thread(aid, thread_id)})
            return
        prefix = "/web/forum/replies/"
        if path.startswith(prefix):
            reply_id = unquote(path[len(prefix):])
            if not reply_id or "/" in reply_id:
                raise CloudError("not found", 404)
            self._send_json({"ok": True,
                             "reply": self._forum().soft_delete_reply(aid, reply_id)})
            return
        self.send_error(404, "not found")

    def _forum_admin_actor(self):
        token = self._admin_session_token()
        if token:
            return "admin:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
        return "admin:owner"

    def _serve_admin_forum_get(self, path):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        forum = self._forum()
        if path == "/admin/forum/queue":
            query, page, limit = self._forum_query({"type", "status", "page", "limit"})
            result = forum.list_moderation_queue(
                query.get("type", ["all"])[0],
                query.get("status", ["pending"])[0], page, limit)
        elif path == "/admin/forum/reports":
            query, page, limit = self._forum_query({"status", "page", "limit"})
            result = forum.list_reports(
                query.get("status", ["pending"])[0], page, limit)
        elif path == "/admin/forum/decisions":
            query, page, limit = self._forum_query(
                {"type", "target_id", "page", "limit"})
            result = forum.list_decisions(
                query.get("type", [None])[0],
                query.get("target_id", [None])[0], page, limit)
        else:
            self.send_error(404, "not found")
            return
        self._send_json({"ok": True, **result})

    def _serve_admin_forum_post(self, path):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        obj = self._json_body(maximum=2048)
        self._cloud()._writable()
        match = re.fullmatch(r"/admin/forum/(thread|reply)/([^/]+)/decision", path)
        if match:
            if set(obj) - {"decision", "reason"}:
                raise CloudError("bad body", 400)
            result = self._forum().moderate_content(
                self._forum_admin_actor(), match.group(1),
                unquote(match.group(2)), obj.get("decision"),
                obj.get("reason", ""))
            self._send_json({"ok": True, "item": result})
            return
        match = re.fullmatch(r"/admin/forum/reports/(\d+)/decision", path)
        if match:
            if set(obj) - {"decision", "reason"}:
                raise CloudError("bad body", 400)
            result = self._forum().review_report(
                self._forum_admin_actor(), int(match.group(1)),
                obj.get("decision"), obj.get("reason", ""))
            self._send_json({"ok": True, "report": result})
            return
        self.send_error(404, "not found")

    def _serve_admin_static(self, path):
        root = os.path.dirname(os.path.abspath(__file__))
        if path == "/admin-manifest.json":
            self._send_file(os.path.join(root, "admin-manifest.json"),
                            "application/manifest+json; charset=utf-8", "no-cache")
            return
        if path == "/admin-sw.js":
            self._send_file(os.path.join(root, "admin-sw.js"),
                            "application/javascript; charset=utf-8", "no-cache",
                            {"Service-Worker-Allowed": "/admin/"})
            return
        if path.startswith("/assets/admin/"):
            name = path[len("/assets/admin/"):]
            spec = _ADMIN_ASSET_ALLOWLIST.get(name)
            if (not spec or "/" in name or "\\" in name
                    or os.path.basename(name) != name):
                self.send_error(404, "not found")
                return
            content_type, cache_control = spec
            self._send_file(os.path.join(root, "assets", "admin", name),
                            content_type, cache_control)
            return
        self.send_error(404, "not found")

    def _vapid_material(self):
        """Load only enough VAPID state to expose the public application key."""
        path = os.environ.get("SLGKING_VAPID_PRIVATE_KEY", "").strip()
        if not path:
            path = os.path.join(os.path.dirname(os.path.abspath(self.publish_dir)),
                                "cloud", "admin_vapid_private.pem")
        if not os.path.isfile(path):
            return None
        try:
            import importlib.util
            if importlib.util.find_spec("pywebpush") is None:
                return None
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            with open(path, "rb") as fh:
                private_key = serialization.load_pem_private_key(
                    fh.read(), password=None)
            if (not isinstance(private_key, ec.EllipticCurvePrivateKey)
                    or not isinstance(private_key.curve, ec.SECP256R1)):
                return None
            point = private_key.public_key().public_numbers()
            public_bytes = (b"\x04" + point.x.to_bytes(32, "big")
                            + point.y.to_bytes(32, "big"))
            public_key = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
            subject = os.environ.get(
                "SLGKING_VAPID_SUBJECT", "https://slg-king.com/").strip()
            if (not subject or len(subject) > 200
                    or not (subject.startswith("mailto:")
                            or subject.startswith("https://"))):
                return None
            return path, public_key, subject
        except Exception:
            return None

    def _serve_admin_push_config(self):
        material = self._vapid_material()
        self._send_json({"public_key": material[1] if material else ""})

    @staticmethod
    def _valid_push_subscription(obj):
        if not isinstance(obj, dict):
            return None
        endpoint = obj.get("endpoint")
        keys = obj.get("keys")
        if not isinstance(endpoint, str) or len(endpoint) > 2048:
            return None
        if not isinstance(keys, dict):
            return None
        p256dh, auth = keys.get("p256dh"), keys.get("auth")
        if (not isinstance(p256dh, str) or not isinstance(auth, str)
                or len(p256dh) > 256 or len(auth) > 256
                or not re.fullmatch(r"[A-Za-z0-9_-]+", p256dh)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", auth)):
            return None
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username or parsed.password):
                return None
            try:
                address = ipaddress.ip_address(parsed.hostname)
                if not address.is_global:
                    return None
            except ValueError:
                hostname = parsed.hostname.lower().rstrip(".")
                if (hostname == "localhost" or hostname.endswith(".localhost")
                        or hostname.endswith(".local")):
                    return None
            p256dh_raw = base64.urlsafe_b64decode(
                p256dh + "=" * (-len(p256dh) % 4))
            auth_raw = base64.urlsafe_b64decode(auth + "=" * (-len(auth) % 4))
        except (ValueError, TypeError):
            return None
        if len(p256dh_raw) != 65 or p256dh_raw[0] != 4 or len(auth_raw) != 16:
            return None
        return endpoint, p256dh, auth

    def _serve_admin_push_subscription(self, delete=False):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        obj = self._json_body(maximum=8192)
        if delete:
            endpoint = obj.get("endpoint")
            if not isinstance(endpoint, str) or len(endpoint) > 2048:
                raise CloudError("invalid subscription", 400)
            removed = self._cloud().delete_admin_push_subscription(endpoint)
            self._send_json({"ok": True, "removed": removed})
            return
        if self._vapid_material() is None:
            raise CloudError("push is disabled", 503)
        parsed = self._valid_push_subscription(obj)
        if parsed is None:
            raise CloudError("invalid subscription", 400)
        self._send_json(self._cloud().save_admin_push_subscription(*parsed), 201)

    def _send_admin_pending_notification(self, store):
        material = self._vapid_material()
        if material is None:
            return
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            return
        private_path, _public_key, subject = material
        payload = json.dumps({
            "title": "有新评论待审核",
            "body": "管理员后台有一条新评论等待审核。",
            "url": "/admin/",
        }, ensure_ascii=False, separators=(",", ":"))
        for subscription in store.admin_push_subscriptions():
            try:
                webpush(subscription_info=subscription, data=payload,
                        vapid_private_key=private_path,
                        vapid_claims={"sub": subject}, ttl=60, timeout=5)
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    store.delete_admin_push_subscription(
                        subscription.get("endpoint", ""))
            except Exception:
                # Push services are optional. Never log endpoints, payloads, or
                # private-key material when a provider is temporarily unavailable.
                continue

    def _queue_admin_pending_notification(self, store):
        threading.Thread(target=self._send_admin_pending_notification,
                         args=(store,), daemon=True).start()

    def _send_json(self, obj, status=200, headers=None):
        if getattr(self, "_defer_maintenance_json", False):
            self._deferred_maintenance_json = (obj, status, headers)
            return
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _has_owner_key(self):
        """Check the owner secret without storing or comparing it in plaintext."""
        expected = os.environ.get("SLGKING_DEV_SECRET_SHA256", "").strip().lower()
        supplied = self.headers.get("X-SLG-Developer-Key", "")
        if (len(expected) != 64 or len(supplied) > 4096
                or any(ch not in "0123456789abcdef" for ch in expected)):
            return False
        digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        return bool(supplied) and hmac.compare_digest(digest, expected)

    def _is_secure_transport(self):
        """Accept TLS directly, or HTTPS asserted by a loopback reverse proxy."""
        cipher = getattr(self.connection, "cipher", None)
        if callable(cipher):
            try:
                if cipher():
                    return True
            except Exception:
                pass
        try:
            peer = ipaddress.ip_address(str(self.client_address[0]))
            mapped = getattr(peer, "ipv4_mapped", None)
            if mapped is not None:
                peer = mapped
            trusted_proxy = peer.is_loopback
        except ValueError:
            trusted_proxy = False
        if not trusted_proxy:
            return False
        forwarded_proto = self.headers.get(
            "X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        return forwarded_proto == "https"

    def _admin_session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(_ADMIN_SESSION_COOKIE)
        token = morsel.value if morsel else ""
        return token if len(token) <= 128 else ""

    @staticmethod
    def _origin_parts(value):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme.lower() not in ("http", "https")
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                return None
            port = parsed.port
            scheme = parsed.scheme.lower()
            if port is None:
                port = 443 if scheme == "https" else 80
            return scheme, parsed.hostname.lower(), port
        except (TypeError, ValueError):
            return None

    def _same_origin_request(self):
        origin = self._origin_parts(self.headers.get("Origin", ""))
        if origin is None:
            return False
        try:
            peer = ipaddress.ip_address(str(self.client_address[0]))
            trusted_proxy = peer.is_loopback
        except ValueError:
            trusted_proxy = False
        scheme = "https" if callable(getattr(self.connection, "cipher", None)) else "http"
        host = self.headers.get("Host", "")
        if trusted_proxy:
            forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
            if forwarded_proto in ("http", "https"):
                scheme = forwarded_proto
            forwarded_host = self.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
            if forwarded_host:
                host = forwarded_host
        target = self._origin_parts(scheme + "://" + host)
        return target is not None and origin == target

    def _has_admin_key(self):
        """Accept either the legacy owner header or a valid same-origin cookie."""
        if self._has_owner_key():
            return True
        token = self._admin_session_token()
        if (not token or not os.path.isfile(self._cloud_path())
                or not self._cloud().validate_admin_session(token)):
            return False
        if self.command in ("POST", "PUT", "PATCH", "DELETE") and not self._same_origin_request():
            raise CloudError("cross-origin admin request blocked", 403)
        return True

    def _serve_admin_session(self, method):
        store = self._cloud()
        if method == "POST":
            if not self._has_owner_key():
                self._send_unauthorized()
                return
            token, expires = store.create_admin_session()
            cookie = ("%s=%s; Max-Age=%d; Expires=%s; Path=/admin; "
                      "HttpOnly; Secure; SameSite=Strict") % (
                          _ADMIN_SESSION_COOKIE, token, _ADMIN_SESSION_MAX_AGE,
                          formatdate(expires, usegmt=True))
            self._send_json({"ok": True, "expires_at": expires},
                            headers={"Set-Cookie": cookie})
            return
        if method == "GET":
            token = self._admin_session_token()
            expires = store.validate_admin_session(token)
            if not expires:
                self._send_unauthorized()
                return
            self._send_json({"ok": True, "expires_at": expires})
            return
        token = self._admin_session_token()
        if not self._has_owner_key():
            if not self._same_origin_request():
                raise CloudError("cross-origin admin request blocked", 403)
            if not token or not store.validate_admin_session(token):
                self._send_unauthorized()
                return
        if token:
            store.revoke_admin_session(token)
        expired = ("%s=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
                   "Path=/admin; HttpOnly; Secure; SameSite=Strict") % _ADMIN_SESSION_COOKIE
        self._send_json({"ok": True}, headers={"Set-Cookie": expired})

    def _has_source_review_automation_token(self):
        # The scheduled helper connects over loopback directly. Never trust
        # X-Forwarded-For here: external requests pass through the local proxy.
        try:
            peer = ipaddress.ip_address(str(self.client_address[0]))
            if not peer.is_loopback:
                return False
            expected = slg_source_review_ai.read_config(
                self._source_review_ai_config_path())["automation_token"]
        except (ValueError, OSError, slg_source_review_ai.SourceReviewAIError):
            return False
        supplied = self.headers.get("X-SLG-Source-Review-Automation", "")
        return (bool(expected) and len(supplied) <= 256
                and hmac.compare_digest(supplied, expected))

    def _send_unauthorized(self):
        self._send_json({"ok": False, "error": "unauthorized"}, status=401)

    def _reports_path(self):
        # Reports sit beside the publish dir, not inside it: they are telemetry,
        # not part of the snapshot clients download.
        return os.path.join(os.path.dirname(self.publish_dir), "reports",
                            "reports.jsonl")

    @staticmethod
    def _copy_bounded_report_tail(source_path, temporary, max_bytes):
        """Write the newest complete JSONL rows that fit into a temporary file."""
        try:
            source_size = os.path.getsize(source_path)
        except FileNotFoundError:
            source_size = 0
        try:
            offset = max(0, source_size - max_bytes)
            with open(source_path, "rb") as source, open(temporary, "wb") as target:
                source.seek(offset)
                if offset:
                    source.seek(offset - 1)
                    previous = source.read(1)
                    source.seek(offset)
                    if previous != b"\n":
                        source.readline()  # discard the partial first row
                while True:
                    row = source.readline()
                    if not row or not row.endswith(b"\n"):
                        break  # do not preserve a truncated final JSONL row
                    target.write(row)
            if os.path.getsize(temporary) > max_bytes:
                raise OSError("trimmed telemetry log exceeded its size limit")
        except OSError:
            raise

    @staticmethod
    def _rotate_reports_if_needed(path, incoming_bytes=0):
        if incoming_bytes < 0 or incoming_bytes > _REPORT_FILE_MAX_BYTES:
            raise OSError("telemetry event exceeded its file-size budget")
        try:
            current_size = os.path.getsize(path)
        except FileNotFoundError:
            current_size = 0
        rotated = path + ".1"
        temporary = rotated + ".tmp"
        try:
            if current_size + incoming_bytes > _REPORT_FILE_MAX_BYTES and current_size:
                # Keep only the newest whole JSONL rows from active as the
                # archive. Truncate active after the bounded archive is durable.
                Handler._copy_bounded_report_tail(path, temporary,
                                                  _REPORT_FILE_MAX_BYTES)
                os.replace(temporary, rotated)
                with open(path, "wb"):
                    pass
            else:
                # Check the existing archive on every append too: it may have
                # been left oversized by an older server version even when the
                # active file is still small.
                try:
                    archive_size = os.path.getsize(rotated)
                except FileNotFoundError:
                    archive_size = 0
                if archive_size > _REPORT_FILE_MAX_BYTES:
                    Handler._copy_bounded_report_tail(
                        rotated, temporary, _REPORT_FILE_MAX_BYTES)
                    os.replace(temporary, rotated)
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _report_log_signature(path):
        signature = []
        for candidate in (path + ".1", path):
            try:
                st = os.stat(candidate)
                signature.append((candidate, st.st_size,
                                  getattr(st, "st_mtime_ns", st.st_mtime)))
            except OSError:
                signature.append((candidate, 0, 0))
        return tuple(signature)

    @staticmethod
    def _iter_report_lines(path):
        for candidate in (path + ".1", path):
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    yield from fh
            except FileNotFoundError:
                continue

    def _aggregate_stats(self, days=14, stable_version=None):
        """Read the append-only log, reusing a short cache for admin refreshes."""
        stable_version = stable_version or STABLE_APP_VERSION
        path = self._reports_path()
        signature = self._report_log_signature(path)
        cache_key = (path, signature, days, stable_version)
        now = time.monotonic()
        with _ADMIN_STATS_LOCK:
            cached = _ADMIN_STATS_CACHE.get(cache_key)
            if cached and now - cached[0] < _ADMIN_STATS_CACHE_SECONDS:
                return dict(cached[1])
        stats = aggregate_lines(self._iter_report_lines(path), days=days,
                                active_days=30,
                                stable_version=stable_version)
        with _ADMIN_STATS_LOCK:
            _ADMIN_STATS_CACHE.clear()
            _ADMIN_STATS_CACHE[cache_key] = (now, stats)
        return dict(stats)

    def _catalogue_tag_stats(self):
        """Count tags on the public site catalogue, never personal client tags."""
        path = os.path.join(self.publish_dir, "slgking.db")
        if not os.path.isfile(path):
            return {"catalogue_games": 0, "items": []}
        uri = "file:%s?mode=ro" % quote(
            os.path.abspath(path).replace(os.sep, "/"), safe="/:")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                games = conn.execute(
                    "SELECT COUNT(*) FROM games WHERE origin = 'site'").fetchone()[0]
                rows = conn.execute(
                    "SELECT t.name, COUNT(DISTINCT gt.game_id) AS games "
                    "FROM tags t JOIN game_tags gt ON gt.tag_id = t.id "
                    "JOIN games g ON g.id = gt.game_id "
                    "WHERE g.origin = 'site' GROUP BY t.id "
                    "ORDER BY games DESC, t.name LIMIT 30").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return {"catalogue_games": 0, "items": []}
        return {
            "catalogue_games": games,
            "items": [{"name": row[0], "games": row[1],
                       "coverage_pct": round(row[1] * 100.0 / games, 1)
                       if games else 0.0} for row in rows],
        }

    def _comment_game_labels(self, analytics):
        """Replace public game slugs with catalogue titles where available."""
        groups = (analytics.get("comment_metrics", {}).get("top_games", []),
                  analytics.get("game_trend", {}).get("items", []))
        slugs = sorted({item.get("game") for group in groups for item in group
                        if isinstance(item, dict) and item.get("game")})
        if not slugs:
            return analytics
        path = os.path.join(self.publish_dir, "slgking.db")
        if not os.path.isfile(path):
            return analytics
        uri = "file:%s?mode=ro" % quote(
            os.path.abspath(path).replace(os.sep, "/"), safe="/:")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                rows = conn.execute(
                    "SELECT slug,title FROM games WHERE slug IN (%s)" %
                    ",".join("?" for _ in slugs), slugs).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return analytics
        labels = {str(slug): str(title) for slug, title in rows if title}
        for group in groups:
            for item in group:
                item["game"] = labels.get(item["game"], item["game"])
        return analytics

    def _comment_analytics(self, days=14):
        """Return aggregate comment counts without private account data."""
        if not os.path.isfile(self._cloud_path()):
            return None
        try:
            analytics = self._cloud().admin_comment_metrics(days=days)
        except (CloudError, OSError, sqlite3.Error):
            return None
        return self._comment_game_labels(analytics)

    def _serve_admin_stats(self):
        """Admin-console analytics, protected by the existing admin key."""
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        query = parse_qs(urlparse(self.path).query)
        raw_days = query.get("days", ["14"])[0]
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "days must be 7, 14, 30, or 90"},
                            status=400)
            return
        if days not in (1, 7, 14, 30, 90):
            self._send_json({"ok": False, "error": "days must be 7, 14, 30, or 90"},
                            status=400)
            return
        release = slg_release_info.get_stable_release(
            fallback=STABLE_APP_VERSION)
        stable_version = release.get("version") or STABLE_APP_VERSION
        stats = self._aggregate_stats(days=days, stable_version=stable_version)
        try:
            cloud_path = self._cloud_path()
            uri = "file:%s?mode=ro" % quote(
                os.path.abspath(cloud_path).replace(os.sep, "/"), safe="/:")
            cloud_db = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                stats["cloud_business_timeline"] = (
                    slg_admin_metrics.aggregate_cloud_business_events(
                        cloud_db, days=days))
                stats['cloud_business_platforms'] = slg_admin_metrics.aggregate_cloud_platforms(cloud_db, days)
            finally:
                cloud_db.close()
        except (CloudError, OSError, sqlite3.Error):
            # An unavailable account database must not look like a real zero.
            stats["cloud_business_timeline"] = None
        try:
            cloud_path = self._cloud_path()
            uri = "file:%s?mode=ro" % quote(
                os.path.abspath(cloud_path).replace(os.sep, "/"), safe="/:")
            cloud_db = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                pending = {
                    "comments": cloud_db.execute(
                        "SELECT COUNT(*) FROM comments "
                        "WHERE status = 'pending'").fetchone()[0],
                    "account_applications": cloud_db.execute(
                        "SELECT COUNT(*) FROM account_applications "
                        "WHERE status = 'pending'").fetchone()[0],
                    "legacy_migrations": cloud_db.execute(
                        "SELECT COUNT(*) FROM migration_requests "
                        "WHERE status = 'pending'").fetchone()[0],
                }
                accounts_total = cloud_db.execute(
                    "SELECT COUNT(*) FROM accounts").fetchone()[0]
                cutoff = int(time.time()) - 30 * 86400
                accounts_30d = cloud_db.execute(
                    "SELECT COUNT(*) FROM accounts WHERE created >= ?",
                    (cutoff,)).fetchone()[0]
            finally:
                cloud_db.close()
        except (CloudError, OSError, sqlite3.Error):
            stats["pending"] = None
        else:
            stats["pending"] = pending
            funnel = dict(stats.get("funnel") or {})
            funnel["accounts_total"] = accounts_total
            funnel["accounts_30d"] = accounts_30d
            # The rate pairs all-time accounts with all-time installs. Using the
            # 30-day active-install count here let a long-lived account base
            # report conversions above 100%.
            installs_total = stats.get("devices") or 0
            funnel["install_to_account_pct"] = (
                round(accounts_total * 100.0 / installs_total, 1)
                if installs_total else None)
            stats["funnel"] = funnel
        try:
            forum_path = self._cloud_path()
            uri = "file:%s?mode=ro" % quote(
                os.path.abspath(forum_path).replace(os.sep, "/"), safe="/:")
            with closing(sqlite3.connect(uri, uri=True, timeout=3)) as forum_db:
                stats["forum_pending"] = {
                    key: forum_db.execute(
                        "SELECT COUNT(*) FROM " + table + " WHERE status='pending'"
                    ).fetchone()[0]
                    for key, table in (("threads", "forum_threads"),
                                       ("replies", "forum_replies"),
                                       ("reports", "forum_reports"))}
        except (CloudError, OSError, sqlite3.Error):
            stats["forum_pending"] = None
        stats["storage"] = self._storage_stats()
        stats["catalogue_tags"] = self._catalogue_tag_stats()
        stats["software_release"] = release
        comments = self._comment_analytics(days=days)
        if comments is None:
            stats["comment_metrics"] = None
            stats["comment_by_day"] = None
            stats["game_trend"] = {
                "source": "", "period": "评论数据暂不可用", "items": []}
        else:
            stats.update(comments)
        try:
            usage = self._usage_store()
            usage.import_jsonl([self._reports_path() + '.1', self._reports_path()])
            stats['platform_usage'] = usage.stats(days=days)
            stats['platform_usage']['business'] = stats.get('cloud_business_platforms')
        except (OSError, sqlite3.Error, ValueError):
            stats['platform_usage'] = None
        stats["generated_at"] = _now()
        self._send_json(stats)

    def _storage_stats(self):
        """Developer-only storage health, computed only on an explicit refresh."""
        reports = self._reports_path()
        comments = self._comments_path()
        base = os.path.dirname(os.path.dirname(reports))

        def file_info(path):
            try:
                st = os.stat(path)
                return {"bytes": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(
                            timespec="seconds")}
            except OSError:
                return {"bytes": 0, "modified": None}

        backup_latest = None
        backup_roots = (os.path.join(base, "backup"),
                        os.path.join(base, "backups"))
        for root in backup_roots:
            try:
                for name in os.listdir(root):
                    low = name.lower()
                    if not ("comment" in low or "report" in low):
                        continue
                    path = os.path.join(root, name)
                    if os.path.isfile(path):
                        mtime = os.path.getmtime(path)
                        if backup_latest is None or mtime > backup_latest[0]:
                            backup_latest = (mtime, datetime.fromtimestamp(
                                mtime).isoformat(timespec="seconds"))
            except OSError:
                pass
        try:
            disk = shutil.disk_usage(base)
            disk_info = {"total_bytes": disk.total, "free_bytes": disk.free}
        except OSError:
            disk_info = {"total_bytes": None, "free_bytes": None}
        cloud_path = self._cloud_path()
        if os.path.isfile(cloud_path):
            try:
                cloud_bytes = sum(os.path.getsize(cloud_path + suffix)
                                  for suffix in ("", "-wal", "-shm")
                                  if os.path.isfile(cloud_path + suffix))
            except OSError:
                cloud_bytes = None
        else:
            cloud_bytes = None
        return {
            "reports": file_info(reports),
            "comments": file_info(comments),
            "cloud_db": {"bytes": cloud_bytes,
                         "path": "private cloud database"},
            "disk": disk_info,
            "latest_backup": backup_latest[1] if backup_latest else None,
        }

    def _maintenance_cloud_db_healthy(self):
        path = os.path.join(os.path.dirname(self.publish_dir),
                            "cloud", "cloud.sqlite3")
        if not os.path.isfile(path):
            return False
        uri = "file:%s?mode=ro" % quote(
            os.path.abspath(path).replace(os.sep, "/"), safe="/:")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                for table in ("accounts", "ledger"):
                    conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return True

    def _maintenance_report_bytes(self):
        path = self._reports_path()
        total = 0
        for candidate in (path, path + ".1"):
            try:
                total += os.path.getsize(candidate)
            except FileNotFoundError:
                pass
        return total

    def _maintenance_backup_times(self):
        base = os.path.dirname(self.publish_dir)
        result = {"cloud": None, "catalogue": None}
        for kind in result:
            root = os.path.join(base, "backups", kind)
            try:
                names = os.listdir(root)
            except FileNotFoundError:
                continue
            prefix = kind + "-"
            for name in names:
                if not (name.startswith(prefix) and name.endswith(".sqlite3.gz")):
                    continue
                path = os.path.join(root, name)
                try:
                    if os.path.isfile(path):
                        mtime = int(os.path.getmtime(path))
                        if result[kind] is None or mtime > result[kind]:
                            result[kind] = mtime
                except OSError:
                    continue
        return result

    def _serve_admin_maintenance(self):
        """Read-only, privacy-safe operational summary for the admin console."""
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        now = int(time.time())
        generated = self._maintenance_iso(now)
        db_healthy = self._maintenance_cloud_db_healthy()
        checks = []

        def check(identifier, status, summary_code, **fields):
            item = {"id": identifier, "status": status,
                    "observed_at": generated, "summary_code": summary_code}
            item.update(fields)
            checks.append(item)
            return item

        check("http_service", "ok", "healthy")
        snapshot = None
        read_failed = False
        try:
            snapshot = self._maintenance_store().snapshot(now=now)
        except (OSError, sqlite3.Error, ValueError):
            read_failed = True
            sys.stderr.write("[%s] maintenance status read failed\n" % _now())

        runs = snapshot["runs"] if snapshot else []
        latest_run = runs[0] if runs else None
        last_success = max((row["finished_at"] for row in runs
                            if row["status"] == "ok" and row["finished_at"]),
                           default=None)
        last_failure = max((row["finished_at"] for row in runs
                            if row["status"] == "error" and row["finished_at"]),
                           default=None)
        stage_rows = {row["stage_id"]: row for row in
                      (snapshot["stages"] if snapshot else [])}
        stages = []
        for stage_id in _SYNC_STAGE_IDS:
            row = stage_rows.get(stage_id)
            if row is None:
                stages.append({"id": stage_id, "status": "unknown",
                               "started_at": None, "finished_at": None,
                               "exit_code": None, "summary_code": "no_history"})
                continue
            status = row["status"]
            summary_code = row["summary_code"]
            if not row["finished_at"] and latest_run and latest_run["finished_at"] is None:
                status, summary_code = "warning", "run_active"
            stages.append({
                "id": stage_id, "status": status,
                "started_at": self._maintenance_iso(row["started_at"]),
                "finished_at": self._maintenance_iso(row["finished_at"]),
                "exit_code": row["exit_code"], "summary_code": summary_code,
            })
        run_age = (now - latest_run["started_at"] if latest_run else None)
        if read_failed:
            sync_status, sync_code = "unknown", "status_read_failed"
        elif not latest_run:
            sync_status, sync_code = "unknown", "no_history"
        elif latest_run["finished_at"] is None:
            if run_age is not None and run_age > 3 * 3600:
                sync_status, sync_code = "error", "sync_overdue"
            else:
                sync_status, sync_code = "warning", "run_active"
        elif latest_run["status"] == "error":
            sync_status, sync_code = "error", "stage_failed"
        elif now - latest_run["finished_at"] > 36 * 3600:
            sync_status, sync_code = "warning", "sync_overdue"
        elif latest_run["status"] == "warning":
            sync_status, sync_code = "warning", "partial_failure"
        else:
            sync_status, sync_code = "ok", "healthy"
        latest_finished = latest_run["finished_at"] if latest_run else None
        check("catalog_sync", sync_status, sync_code,
              last_started_at=self._maintenance_iso(
                  latest_run["started_at"] if latest_run else None),
              last_finished_at=self._maintenance_iso(latest_finished),
              last_success_at=self._maintenance_iso(last_success),
              last_failure_at=self._maintenance_iso(last_failure),
              stages=stages)

        event_stats = snapshot["events"] if snapshot else {}
        for kind in ("account_registration", "points_ledger"):
            row = event_stats.get(kind) if event_stats else None
            row = row or {"total": 0, "success_count": 0,
                          "client_error_count": 0, "server_error_count": 0,
                          "last_success_at": None, "last_failure_at": None}
            if not db_healthy:
                status, summary_code = "error", "database_unavailable"
            elif read_failed:
                status, summary_code = "unknown", "status_read_failed"
            elif not row["total"]:
                status, summary_code = "unknown", "no_recent_activity"
            elif row["server_error_count"]:
                if (row["last_success_at"] is not None
                        and row["last_success_at"] > row["last_failure_at"]):
                    status, summary_code = "warning", "partial_failure"
                else:
                    status, summary_code = "error", "operation_errors"
            else:
                status, summary_code = "ok", "healthy"
            check(kind, status, summary_code, window_hours=24,
                  success_count=row["success_count"],
                  client_error_count=row["client_error_count"],
                  server_error_count=row["server_error_count"],
                  last_success_at=self._maintenance_iso(row["last_success_at"]),
                  last_failure_at=self._maintenance_iso(row["last_failure_at"]))

        cloud_path = os.path.join(os.path.dirname(self.publish_dir),
                                  "cloud", "cloud.sqlite3")
        disk_path = os.path.dirname(self.publish_dir)
        try:
            disk = shutil.disk_usage(disk_path)
            disk_total, disk_free = disk.total, disk.free
            disk_status = ("error" if disk_free < MIN_FREE_BYTES else "ok")
            disk_code = "disk_low" if disk_status == "error" else "healthy"
        except OSError:
            disk_total, disk_free = None, None
            disk_status, disk_code = "unknown", "status_read_failed"
        try:
            cloud_bytes = sum(os.path.getsize(cloud_path + suffix)
                              for suffix in ("", "-wal", "-shm")
                              if os.path.isfile(cloud_path + suffix))
        except OSError:
            cloud_bytes = None
            disk_status, disk_code = "unknown", "status_read_failed"
        comments_path = self._comments_path()
        try:
            comments_bytes = os.path.getsize(comments_path)
        except FileNotFoundError:
            comments_bytes = 0
        except OSError:
            comments_bytes = None
            disk_status, disk_code = "unknown", "status_read_failed"
        reports_bytes = self._maintenance_report_bytes()
        if not db_healthy and disk_status == "ok":
            disk_status, disk_code = "error", "database_unavailable"
        check("storage", disk_status, disk_code,
              disk_total_bytes=disk_total, disk_free_bytes=disk_free,
              cloud_db_bytes=cloud_bytes, reports_bytes=reports_bytes,
              comments_bytes=comments_bytes)

        backup_times = self._maintenance_backup_times()
        cloud_backup, catalogue_backup = (backup_times["cloud"],
                                          backup_times["catalogue"])
        backup_success_stages = {
            row["stage_id"]: row for row in
            (snapshot["stages"] if snapshot else [])
            if row["stage_id"] in ("cloud_backup", "catalogue_backup")}
        latest_backup_failure = any(
            row["status"] == "error" for row in backup_success_stages.values())
        if read_failed:
            backup_status, backup_code = "unknown", "status_read_failed"
        elif latest_backup_failure:
            backup_status, backup_code = "error", "stage_failed"
        elif cloud_backup is None or catalogue_backup is None:
            backup_status, backup_code = "warning", "backup_missing"
        elif now - min(cloud_backup, catalogue_backup) > 36 * 3600:
            backup_status, backup_code = "warning", "backup_stale"
        else:
            backup_status, backup_code = "ok", "healthy"
        check("backup", backup_status, backup_code,
              cloud_last_success_at=self._maintenance_iso(cloud_backup),
              catalogue_last_success_at=self._maintenance_iso(catalogue_backup))

        statuses = [item["status"] for item in checks]
        overall = ("error" if "error" in statuses else
                   "warning" if "warning" in statuses else
                   "unknown" if "unknown" in statuses else "ok")
        self._send_json({"generated_at": generated, "overall": overall,
                         "checks": checks})

    def _serve_stats(self):
        """Aggregate stats are owner-only, like the rest of the admin API.

        The raw developer secret is accepted only in X-SLG-Developer-Key, never
        in the URL or a cloud account token.
        """
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        stats = self._aggregate_stats()
        stats["storage"] = self._storage_stats()
        self._send_json(stats)

    def _maintenance_campaign(self, config=None):
        """Load only a small, explicitly active campaign from published config."""
        path = os.path.join(self.publish_dir, "config.json")
        try:
            # config.json now carries a bounded announcement archive.
            if config is None:
                if os.path.getsize(path) > 1024 * 1024:
                    return None
                with open(path, "r", encoding="utf-8") as fh:
                    config = json.load(fh)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(config, dict):
            return None
        announcement = config.get("announcement")
        # New admin pages attach an optional award to the announcement. Keep
        # the old config key as a fallback for already deployed admin pages.
        item = (announcement.get("reward")
                if isinstance(announcement, dict)
                and "reward" in announcement
                else config.get("maintenance_compensation"))
        if not isinstance(item, dict) or item.get("active") is not True:
            return None
        campaign_id = item.get("id")
        title = item.get("title")
        body = item.get("body")
        amount = item.get("amount")
        if (not isinstance(campaign_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", campaign_id)
                or not isinstance(title, str) or not title.strip() or len(title) > 100
                or not isinstance(body, str) or len(body) > 1000
                or not isinstance(amount, int) or isinstance(amount, bool)
                or not 1 <= amount <= 100000):
            return None
        return {"id": campaign_id, "title": title.strip(),
                "body": body, "amount": amount, "active": True}

    def _account_maintenance_reward(self, claim=False):
        aid = self._account_id()
        campaign = self._maintenance_campaign()
        if not claim:
            self._send_json(self._cloud().maintenance_reward_status(
                aid, campaign.get("id") if campaign else "", campaign))
            return
        if campaign is None:
            raise CloudError("campaign unavailable", 409)
        obj = self._json_body()
        if obj.get("campaign_id") != campaign["id"]:
            raise CloudError("campaign unavailable", 409)
        self._send_json(self._cloud().claim_maintenance_reward(
            aid, campaign["id"], campaign["amount"]))

    # --- comments ------------------------------------------------------------

    def _comments_path(self):
        # Beside reports, not in the publish snapshot: comments are user content
        # and must never ride along in the catalogue clients download.
        return os.path.join(os.path.dirname(self.publish_dir), "comments",
                            "comments.jsonl")

    def _sync_legacy_comments(self):
        try:
            from slg_moderation import classify_comment
        except ImportError:
            return False
        self._cloud().import_legacy(self._comments_path(), classify_comment)
        return True

    def _serve_comments(self):
        self._sync_legacy_comments()
        query = parse_qs(urlparse(self.path).query)
        game = query.get("game", [""])[0]
        game, game_slugs = _public_game_slugs(self.publish_dir, game)
        try:
            page = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["20"])[0])
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination", 400)
        sort = query.get("sort", ["latest"])[0]
        result = self._cloud().comments_page(
            game, sort=sort, page=page, limit=limit, game_slugs=game_slugs)
        # The legacy JSONL remains readable by older clients. Its one-time
        # moderation import is triggered on first access, not replayed per GET.
        # Keep `comments` as a top-level list for old clients; new clients also
        # receive the metadata needed to render page controls.
        self._send_json(result)

    def _serve_comment_count(self):
        self._sync_legacy_comments()
        if self.headers.get("Authorization", "").startswith("Bearer "):
            self._send_json({"count": self._cloud().approved_count(aid=self._account_id())})
            return
        device = self.headers.get("X-SLG-Device", "")
        if not device:
            self._send_json({"count": 0})
            return
        comments, deleted, _ = _read_comments(self._comments_path())
        ids = {c.get("id") for c in comments
               if c.get("device") == device and c.get("id") not in deleted}
        count = self._cloud().approved_count(legacy_ids=ids)
        self._send_json({"count": count})

    def _serve_post_comment(self):
        if self.headers.get("Authorization", "").startswith("Bearer "):
            self._account_comment_post(self._json_body())
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 4096:
                self._send_json({"ok": False, "error": "bad length"}, status=400)
                return
            obj = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "bad body"}, status=400)
            return
        if not isinstance(obj, dict):
            self._send_json({"ok": False, "error": "bad body"}, status=400)
            return
        game_value = obj.get("game")
        content_value = obj.get("content")
        device_value = obj.get("device")
        nickname_value = obj.get("nickname") or ""
        if not all(isinstance(value, str) for value in
                   (game_value, content_value, device_value, nickname_value)):
            self._send_json({"ok": False, "error": "bad fields"}, status=400)
            return
        game = game_value.strip()
        content = content_value.strip()
        device = device_value.strip()
        if not game or not content or not device:
            self._send_json({"ok": False, "error": "missing fields"}, status=400)
            return
        if len(content) < 20 or len(content) > 500 or len(content.encode("utf-8")) > 2048:
            self._send_json({"ok": False, "error": "invalid comment length"}, status=400)
            return
        try:
            from slg_moderation import classify_comment
            verdict = classify_comment(content)
        except ImportError:
            self._send_json({"ok": False, "error": "comment moderation unavailable"},
                            status=503)
            return
        self._cloud()._writable()
        self._cloud()._auth_limit(self._auth_identity(), "legacy_comment", 20, 86400)
        comment = {
            "id": uuid.uuid4().hex[:12],
            "game": _resolve_public_game_slug(self.publish_dir, game)[:200],
            "content": content,
            "nickname": nickname_value.strip()[:40],
            "device": device[:64],
            "ts": _now(),
            "moderation": verdict,
        }
        path = self._comments_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        limited = False
        with _COMMENT_LOCK:
            now = time.monotonic()
            identity = comment["device"]
            limited = _rate_limited(
                _COMMENT_POST_TIMES, identity, now,
                _COMMENT_POST_WINDOW_SECONDS, _COMMENT_POST_MAX_PER_WINDOW)
            if not limited:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(comment, ensure_ascii=False) + "\n")
                _record_rate_event(_COMMENT_POST_TIMES, identity, now)
        if limited:
            self._send_json({"ok": False, "error": "rate limited"}, status=429)
            return
        # Keep the old device/JSONL protocol, but persist the moderation result
        # before responding so rejected text remains available to the owner.
        self._sync_legacy_comments()
        if verdict.get("decision") == "reject":
            self._send_json({"ok": False, "error": "comment rejected"}, status=400)
            return
        if verdict.get("decision") == "review":
            self._queue_admin_pending_notification(self._cloud())
        self._send_json({"ok": True, "id": comment["id"]})

    def _serve_report_comment(self, cid):
        if self.headers.get("Authorization", "").startswith("Bearer "):
            self._send_json(self._cloud().report(self._account_id(), cid))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 4096:
                self._send_json({"ok": False, "error": "bad length"}, status=400)
                return
            obj = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "bad body"}, status=400)
            return
        if not isinstance(obj, dict):
            self._send_json({"ok": False, "error": "bad body"}, status=400)
            return
        device = obj.get("device", "")
        if not isinstance(device, str):
            self._send_json({"ok": False, "error": "bad fields"}, status=400)
            return
        # Use the client-supplied install ID when present, with the socket peer
        # as a best-effort fallback. Neither a device ID nor an IP is unforgeable.
        identity = device.strip()[:64] or str(self.client_address[0])
        if not cid or len(cid) > 128:
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        path = self._comments_path()
        self._cloud()._writable()
        self._cloud()._auth_limit(self._auth_identity(), "legacy_report", 50, 86400)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        error = None
        with _COMMENT_LOCK:
            now = time.monotonic()
            if _report_already_recorded(identity, cid, now):
                error = (409, "duplicate report")
            elif _rate_limited(
                    _COMMENT_REPORT_TIMES, identity, now,
                    _COMMENT_REPORT_WINDOW_SECONDS,
                    _COMMENT_REPORT_MAX_PER_WINDOW):
                error = (429, "rate limited")
            else:
                comments, deleted, _ = _read_comments(path)
                if not any(c.get("id") == cid for c in comments) or cid in deleted:
                    error = (404, "not found")
                else:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"report": cid, "device": identity,
                                             "ts": _now()}, ensure_ascii=False) + "\n")
                    _record_report(identity, cid, now)
                    _record_rate_event(_COMMENT_REPORT_TIMES, identity, now)
        if error:
            self._send_json({"ok": False, "error": error[1]}, status=error[0])
            return
        self._send_json({"ok": True})

    def _serve_delete_comment(self, cid, admin=False):
        if admin and not self._has_admin_key():
            self._send_unauthorized()
            return
        if self.headers.get("Authorization", "").startswith("Bearer ") or admin:
            try:
                self._cloud().delete_comment(None if admin else self._account_id(), cid,
                                             admin=admin)
                self._send_json({"ok": True})
                return
            except CloudError as exc:
                if str(exc) != "not found":
                    raise
        device = self.headers.get("X-SLG-Device", "")
        comments, _deleted, _ = _read_comments(self._comments_path())
        target = next((c for c in comments if c.get("id") == cid), None)
        if target is None:
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        if not admin and (not device or target.get("device") != device):
            self._send_json({"ok": False, "error": "forbidden"}, status=403)
            return
        path = self._comments_path()
        with _COMMENT_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tombstone": cid,
                                     "actor": "admin" if admin else "author",
                                     "ts": _now()},
                                    ensure_ascii=False) + "\n")
        if admin:
            self._cloud().append_admin_audit("comment_deleted", cid, {})
        self._send_json({"ok": True})

    def _serve_admin_comments(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        self._sync_legacy_comments()
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        try:
            limit = int(query.get("limit", ["30"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            raise CloudError("bad pagination")
        q = query.get("q", [""])[0]
        if not isinstance(q, str):
            raise CloudError("bad search")
        q = q.strip()[:100]
        self._send_json(self._cloud().admin_comments(limit, offset,
                        query.get("status", [""])[0], q,
                        self._catalogue_slugs_for_title(q)))

    def _serve_admin_audit(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        try:
            limit = int(query.get("limit", ["100"])[0])
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad limit")
        self._send_json(self._cloud().admin_audit(limit))

    @staticmethod
    def _source_review_fingerprint(row):
        # Older source rows may not have a native content hash. Derive a stable
        # revision key so an AI result cannot be attached after fields change.
        return _source_review_fingerprint_for_row(row)

    @staticmethod
    def _source_review_norm(value):
        value = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(ch for ch in value if ch.isalnum())

    def _read_source_review_catalog(self):
        path = os.path.join(self.data_dir, "slgking.db")
        if not os.path.isfile(path):
            raise CloudError("source catalogue unavailable", 503)
        uri = "file:" + quote(os.path.abspath(path), safe="/:\\") + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            source_rows = db.execute(
                "SELECT source_id,external_id,game_id,source_title,source_version,"
                "developer,engine,overview,tags_json,source_modified,content_hash,"
                "match_status,match_confidence,suggested_game_id,fetched_at "
                "FROM game_sources WHERE source_id IN ('f95zone','zyoudao')"
            ).fetchall()
            game_columns = {r[1] for r in db.execute("PRAGMA table_info(games)")}
            if "origin" in game_columns:
                candidate_rows = db.execute(
                    "SELECT id,title,developer,version,engine FROM games "
                    "WHERE origin='site'").fetchall()
            else:
                candidate_rows = db.execute(
                    "SELECT id,title,developer,version,engine FROM games").fetchall()
            db.close()
        except sqlite3.Error as exc:
            raise CloudError("source catalogue unavailable", 503) from exc
        return source_rows, candidate_rows

    @classmethod
    def _source_review_candidates(cls, row, candidates):
        source_title = cls._source_review_norm(row["source_title"])
        current_id = int(row["game_id"])
        suggested_id = row["suggested_game_id"]
        scored = []
        for game in candidates:
            game_id = int(game["id"])
            if game_id == current_id:
                continue
            title = str(game["title"] or "")
            target = cls._source_review_norm(title)
            if not source_title or not target:
                score = 0.0
            else:
                score = SequenceMatcher(None, source_title, target).ratio()
            if game_id == suggested_id and row["match_confidence"] is not None:
                score = max(score, float(row["match_confidence"]))
            if score >= 0.24 or game_id == suggested_id:
                scored.append((score, game))
        scored.sort(key=lambda item: (-item[0], int(item[1]["id"])))
        return [{"game_id": int(game["id"]), "title": str(game["title"] or "")[:300],
                 "developer": str(game["developer"] or "")[:160],
                 "version": str(game["version"] or "")[:100],
                 "engine": str(game["engine"] or "")[:100],
                 "match_score": round(score, 3)}
                for score, game in scored[:5]]

    @staticmethod
    def _source_review_status(identity, ai_index, manual_index):
        manual = manual_index.get(identity)
        if manual:
            return {"same": "same", "distinct": "distinct",
                    "skip": "skipped"}.get(manual["decision"], "pending")
        suggestion = ai_index.get(identity)
        if not suggestion:
            return "pending"
        return "uncertain" if suggestion["verdict"] == "uncertain" else "suggested"

    def _admin_source_review_items(self, query):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        allowed_query = {"limit", "offset", "status"}
        if set(query) - allowed_query or any(len(values) != 1 for values in query.values()):
            raise CloudError("bad source review query")
        try:
            limit = int(query.get("limit", ["20"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            raise CloudError("bad pagination")
        status_filter = query.get("status", ["pending"])[0]
        if not 1 <= limit <= 20 or not 0 <= offset <= 1000000:
            raise CloudError("bad pagination")
        if status_filter not in ("pending", "suggested", "uncertain", "same",
                                 "distinct", "skipped", "all"):
            raise CloudError("bad source review status")

        source_rows, candidates = self._read_source_review_catalog()
        ai_index, manual_index = self._cloud().admin_source_review_index()
        records = []
        counts = {name: 0 for name in ("pending", "suggested", "uncertain",
                                       "same", "distinct", "skipped")}
        for row in source_rows:
            identity = (row["source_id"], row["external_id"],
                        self._source_review_fingerprint(row))
            item_status = self._source_review_status(identity, ai_index, manual_index)
            counts[item_status] = counts.get(item_status, 0) + 1
            if status_filter != "all" and item_status != status_filter:
                continue
            records.append((row, identity, item_status))
        # Oldest first lets an initial backlog be worked through in stable
        # batches; source updates retain their row position until content changes.
        records.sort(key=lambda record: (str(record[0]["fetched_at"] or ""),
                                         record[0]["source_id"],
                                         record[0]["external_id"]))
        total = len(records)
        page = records[offset:offset + limit]
        page_identities = [identity for _row, identity, _status in page]
        page_states = self._cloud().admin_source_review_state(page_identities)
        items = []
        for row, identity, item_status in page:
            tags = []
            try:
                parsed_tags = json.loads(row["tags_json"] or "[]")
                if isinstance(parsed_tags, list):
                    tags = [str(tag)[:48] for tag in parsed_tags[:24]]
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            state = page_states.get(identity, {})
            items.append({
                "source_id": identity[0], "external_id": identity[1],
                "content_hash": identity[2], "status": item_status,
                "title": str(row["source_title"] or "")[:300],
                "developer": str(row["developer"] or "")[:160],
                "version": str(row["source_version"] or "")[:100],
                "engine": str(row["engine"] or "")[:100],
                "tags": tags,
                "overview": str(row["overview"] or "")[:600],
                "source_modified": str(row["source_modified"] or "")[:64],
                "match_status": str(row["match_status"] or "new")[:32],
                "match_confidence": row["match_confidence"],
                "suggested_game_id": row["suggested_game_id"],
                "candidates": self._source_review_candidates(row, candidates),
                "ai_suggestion": state.get("ai"),
                "manual_decision": state.get("manual"),
            })
        self._send_json({"items": items, "status": status_filter,
                         "limit": limit, "offset": offset, "total": total,
                         "counts": counts, "has_more": offset + len(items) < total,
                         "ai_schema": "slgking-source-review/v1"})

    def _serve_admin_source_review_export(self):
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        # Export uses the same authenticated, bounded, privacy-filtered response.
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        class _ResponseCapture:
            def __init__(self):
                self.value = None
        capture = _ResponseCapture()
        # Avoid duplicating the query/security logic: temporarily capture the
        # response payload from _send_json, which remains capped at 20 records.
        original_send_json = self._send_json
        def capture_json(value, status=200):
            if status != 200:
                raise CloudError(value.get("error", "export failed"), status)
            capture.value = value
        self._send_json = capture_json
        try:
            self._admin_source_review_items(query)
        finally:
            self._send_json = original_send_json
        payload = {"schema": "slgking-source-review/v1",
                   "instructions": {
                       "verdict": ["same", "distinct", "uncertain"],
                       "record_kind": ["game", "non_game", "insufficient"],
                       "rationale_max_chars": 500,
                       "confidence_range": [0, 1],
                       "return": "Return only {items:[{source_id,external_id,content_hash,verdict,record_kind,rationale,confidence}]}"
                   },
                   "items": (capture.value or {}).get("items", [])}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         'attachment; filename="slgking-source-review.json"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _serve_admin_source_review_import(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        obj = self._json_body(maximum=65536)
        items = obj.get("items", obj.get("results"))
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise CloudError("expected 1-20 AI suggestions")
        source_rows, _candidates = self._read_source_review_catalog()
        current = {}
        for row in source_rows:
            identity = (row["source_id"], row["external_id"])
            current[identity] = self._source_review_fingerprint(row)
        valid, rejected = [], []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                rejected.append({"index": index, "ok": False, "error": "item must be an object"})
                continue
            source_id = str(item.get("source_id", ""))[:32]
            external_id = str(item.get("external_id", ""))[:128]
            content_hash = str(item.get("content_hash", ""))[:96]
            if source_id not in ("f95zone", "zyoudao") or not external_id:
                rejected.append({"source_id": source_id, "external_id": external_id,
                                 "ok": False, "error": "invalid source identity"})
                continue
            if current.get((source_id, external_id)) != content_hash:
                rejected.append({"source_id": source_id, "external_id": external_id,
                                 "ok": False, "error": "stale or missing source revision"})
                continue
            if "verdict" not in item and item.get("category") in ("duplicate", "new_game", "non_game", "insufficient", "uncertain"):
                category = item["category"]
                item = dict(item)
                item["verdict"] = "same" if category == "duplicate" else (
                    "uncertain" if category in ("insufficient", "uncertain") else "distinct")
                if category in ("non_game", "insufficient"):
                    item["record_kind"] = category
            valid.append(item)
        result = self._cloud().admin_import_source_review_suggestions(valid) if valid else {
            "items": [], "accepted": 0, "rejected": 0}
        result["items"] = rejected + result.get("items", [])
        result["accepted"] = int(result.get("accepted", 0))
        result["rejected"] = len(rejected) + int(result.get("rejected", 0))
        result["ok"] = result["rejected"] == 0
        if result["accepted"] or result["rejected"]:
            self._cloud().append_admin_audit(
                "source_review_bulk_import", "",
                {"accepted": result["accepted"],
                 "rejected": result["rejected"]})
        self._send_json(result)

    def _source_review_ai_config_path(self):
        # Kept beside (not inside) the published snapshot and cloud SQLite DB.
        # Deployments only replace application files, so this private file
        # survives upgrades; cloud_backup.py does not copy it into DB backups.
        return os.path.join(os.path.dirname(os.path.abspath(self.publish_dir)),
                            "cloud", "source_review_ai.json")

    def _source_review_ai_jobs_path(self):
        return os.path.join(os.path.dirname(self._source_review_ai_config_path()),
                            "source_review_ai_jobs.sqlite3")

    def _persist_source_review_ai_job(self, job):
        path = self._source_review_ai_jobs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS source_review_ai_jobs ("
                "request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,"
                "state TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,"
                "result_json TEXT, error TEXT NOT NULL DEFAULT '')")
            conn.execute(
                "DELETE FROM source_review_ai_jobs WHERE finished_at IS NOT NULL"
                " AND finished_at<?",
                (time.time() - _SOURCE_REVIEW_AI_JOB_RETENTION_SECONDS,))
            result_json = (json.dumps(job.get("result"), ensure_ascii=False,
                                      separators=(",", ":"))
                           if job.get("result") is not None else None)
            conn.execute(
                "INSERT INTO source_review_ai_jobs"
                " (request_id,job_id,state,started_at,finished_at,result_json,error)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET"
                " job_id=excluded.job_id,state=excluded.state,"
                " started_at=excluded.started_at,finished_at=excluded.finished_at,"
                " result_json=excluded.result_json,error=excluded.error",
                (job["request_id"], job["job_id"], job["state"],
                 job["started_at"], job.get("finished_at"), result_json,
                 str(job.get("error") or "")[:500]))
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _load_source_review_ai_job(self, *, job_id=None, request_id=None):
        if not job_id and not request_id:
            return None
        path = self._source_review_ai_jobs_path()
        if not os.path.isfile(path):
            return None
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS source_review_ai_jobs ("
                "request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,"
                "state TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,"
                "result_json TEXT, error TEXT NOT NULL DEFAULT '')")
            conn.execute(
                "DELETE FROM source_review_ai_jobs WHERE finished_at IS NOT NULL"
                " AND finished_at<?",
                (time.time() - _SOURCE_REVIEW_AI_JOB_RETENTION_SECONDS,))
            column, value = (("job_id", job_id) if job_id
                             else ("request_id", request_id))
            row = conn.execute(
                "SELECT * FROM source_review_ai_jobs WHERE %s=?" % column,
                (value,)).fetchone()
            if row is None:
                conn.commit()
                return None
            state = row["state"]
            error = row["error"] or ""
            finished_at = row["finished_at"]
            if state == "running":
                state = "unknown"
                finished_at = time.time()
                error = ("服务器在任务结束前重启或中断，DeepSeek 请求结果未知；"
                         "系统没有自动重跑。请刷新来源队列，确认是否已有建议。")
                conn.execute(
                    "UPDATE source_review_ai_jobs SET state=?,finished_at=?,error=?"
                    " WHERE request_id=?",
                    (state, finished_at, error, row["request_id"]))
            conn.commit()
            result = None
            if row["result_json"]:
                try:
                    result = json.loads(row["result_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    state = "unknown"
                    error = "任务结果记录无法读取；请刷新来源队列后再决定是否重试。"
            return {"job_id": row["job_id"], "state": state,
                    "started_at": row["started_at"],
                    "finished_at": finished_at,
                    "result": result, "error": error}
        finally:
            conn.close()

    def _serve_admin_source_review_ai_config(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        try:
            value = slg_source_review_ai.public_config(
                self._source_review_ai_config_path())
        except slg_source_review_ai.SourceReviewAIError as exc:
            raise CloudError(str(exc), 503) from exc
        # This deliberately never returns the API key, even masked characters.
        self._send_json(value)

    def _serve_admin_source_review_ai_config_post(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        obj = self._json_body(maximum=8192)
        if set(obj) - {"api_key", "model", "clear_key", "auto_after_sync"}:
            raise CloudError("unsupported DeepSeek settings")
        api_key = obj.get("api_key", "")
        model = obj.get("model", slg_source_review_ai.ALLOWED_MODELS[0])
        clear_key = obj.get("clear_key", False)
        auto_after_sync = obj.get("auto_after_sync", False)
        if not isinstance(api_key, str) or not isinstance(model, str):
            raise CloudError("invalid DeepSeek settings")
        if not isinstance(clear_key, bool) or not isinstance(auto_after_sync, bool):
            raise CloudError("invalid DeepSeek settings")
        try:
            result = slg_source_review_ai.save_config(
                self._source_review_ai_config_path(), model=model,
                api_key=api_key, clear_key=clear_key,
                auto_after_sync=auto_after_sync)
        except slg_source_review_ai.SourceReviewAIError as exc:
            raise CloudError(str(exc), 400) from exc
        self._send_json(result)

    def _serve_admin_source_review_ai_test(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        self._json_body(maximum=1024)
        try:
            config = slg_source_review_ai.read_config(
                self._source_review_ai_config_path())
            result = slg_source_review_ai.test_connection(
                config["api_key"], config["model"])
        except slg_source_review_ai.SourceReviewAIError as exc:
            raise CloudError(str(exc), 502) from exc
        self._send_json(result)

    def _serve_admin_source_review_ai_job(self, job_id):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
            raise CloudError("source review job not found", 404)
        _prune_source_review_ai_jobs()
        with _SOURCE_REVIEW_AI_JOBS_LOCK:
            job = _SOURCE_REVIEW_AI_JOBS.get(job_id)
            if job is None:
                job = self._load_source_review_ai_job(job_id=job_id)
            if job is None:
                raise CloudError("source review job not found or expired", 404)
            value = {key: item for key, item in job.items()
                     if key != "request_id"}
        self._send_json(value)

    def _serve_admin_source_review_ai_request_status(self, request_id):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        if not re.fullmatch(r"[0-9a-f]{32}", request_id or ""):
            raise CloudError("source review request not found", 404)
        _prune_source_review_ai_jobs()
        with _SOURCE_REVIEW_AI_JOBS_LOCK:
            job = next((item for item in _SOURCE_REVIEW_AI_JOBS.values()
                        if item.get("request_id") == request_id), None)
            if job is None:
                job = self._load_source_review_ai_job(request_id=request_id)
            if job is None:
                raise CloudError("source review request not found", 404)
            value = {key: item for key, item in job.items()
                     if key != "request_id"}
        self._send_json(value)

    def _execute_source_review_ai_job(self, job_id):
        try:
            result = self._run_source_review_ai(already_locked=True)
            state = "complete"
            error = ""
        except CloudError as exc:
            result = None
            state = "failed"
            error = str(exc)
        except Exception as exc:
            # Keep unexpected exception details out of the browser response and
            # logs; the job id is enough to correlate a safe server-side entry.
            result = None
            state = "failed"
            error = "服务器处理 AI 核对时发生内部错误；任务编号 %s" % job_id[:8]
            print("source-review-ai job %s failed (%s)" %
                  (job_id, type(exc).__name__))
        with _SOURCE_REVIEW_AI_JOBS_LOCK:
            job = _SOURCE_REVIEW_AI_JOBS.get(job_id)
            if job is not None:
                job.update({"state": state, "finished_at": time.time(),
                            "result": result, "error": error})
                persisted_job = dict(job)
            else:
                persisted_job = None
        if persisted_job is not None:
            try:
                self._persist_source_review_ai_job(persisted_job)
            except Exception as exc:
                # Keep the durable "running" row. After a restart it is
                # reported as unknown, never automatically retried.
                print("source-review-ai job %s state persistence failed (%s)" %
                      (job_id, type(exc).__name__))

    def _capture_pending_source_reviews(self):
        class _Capture:
            value = None

        capture = _Capture()
        original_send_json = self._send_json

        def capture_json(value, status=200):
            if status != 200:
                raise CloudError(value.get("error", "source review read failed"),
                                 status)
            capture.value = value

        self._send_json = capture_json
        try:
            self._admin_source_review_items({
                "limit": [str(slg_source_review_ai.MAX_BATCH)],
                "offset": ["0"], "status": ["pending"]})
        finally:
            self._send_json = original_send_json
        return (capture.value or {}).get("items", [])

    def _serve_admin_source_review_ai_run(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        request = self._json_body(maximum=1024)
        request_id = request.get("request_id")
        if (set(request) != {"request_id"}
                or not isinstance(request_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", request_id)):
            raise CloudError("invalid source review request id", 400)
        _prune_source_review_ai_jobs()
        job_id = uuid.uuid4().hex
        with _SOURCE_REVIEW_AI_JOBS_LOCK:
            existing = next((job for job in _SOURCE_REVIEW_AI_JOBS.values()
                             if job.get("request_id") == request_id), None)
            if existing is None:
                existing = self._load_source_review_ai_job(
                    request_id=request_id)
            if existing is not None:
                existing_value = {key: item for key, item in existing.items()
                                  if key != "request_id"}
            elif not _SOURCE_REVIEW_AI_LOCK.acquire(blocking=False):
                raise CloudError("AI \u6838\u5bf9\u6b63\u5728\u8fd0\u884c\uff0c\u8bf7\u7a0d\u540e\u67e5\u770b\u7ed3\u679c", 409)
            else:
                job = {
                    "job_id": job_id, "state": "running",
                    "request_id": request_id,
                    "started_at": time.time(), "finished_at": None,
                    "result": None, "error": "",
                }
                try:
                    self._persist_source_review_ai_job(job)
                except Exception as exc:
                    _SOURCE_REVIEW_AI_LOCK.release()
                    raise CloudError(
                        "\u65e0\u6cd5\u5b89\u5168\u4fdd\u5b58 AI \u4efb\u52a1\u72b6\u6001\uff1b\u672c\u6b21\u672a\u53d1\u9001 DeepSeek \u8bf7\u6c42\uff0c\u8bf7\u68c0\u67e5\u670d\u52a1\u5668\u5b58\u50a8\u540e\u91cd\u8bd5",
                        503) from exc
                _SOURCE_REVIEW_AI_JOBS[job_id] = job
                existing_value = None
        if existing_value is not None:
            self._send_json(existing_value, status=202)
            return
        worker = threading.Thread(
            target=self._execute_source_review_ai_job, args=(job_id,),
            name="source-review-ai-%s" % job_id[:8], daemon=True)
        # Send the short acknowledgement before the worker starts. Its queue
        # capture temporarily overrides this handler's _send_json method.
        try:
            self._send_json({"ok": True, "job_id": job_id, "state": "running"},
                            status=202)
        except OSError:
            with _SOURCE_REVIEW_AI_JOBS_LOCK:
                job = _SOURCE_REVIEW_AI_JOBS.get(job_id)
                if job is not None:
                    job.update({"state": "failed", "finished_at": time.time(),
                                "error": "\u8bf7\u6c42\u672a\u80fd\u786e\u8ba4\u63d0\u4ea4\uff1bDeepSeek \u4efb\u52a1\u5c1a\u672a\u542f\u52a8"})
                    persisted_job = dict(job)
                else:
                    persisted_job = None
            if persisted_job is not None:
                try:
                    self._persist_source_review_ai_job(persisted_job)
                except Exception as exc:
                    print("source-review-ai job %s state persistence failed (%s)" %
                          (job_id, type(exc).__name__))
            _SOURCE_REVIEW_AI_LOCK.release()
            raise
        try:
            worker.start()
        except RuntimeError as exc:
            with _SOURCE_REVIEW_AI_JOBS_LOCK:
                job = _SOURCE_REVIEW_AI_JOBS.get(job_id)
                if job is not None:
                    job.update({"state": "failed", "finished_at": time.time(),
                                "error": "\u65e0\u6cd5\u542f\u52a8 AI \u6838\u5bf9\u4efb\u52a1\uff0c\u672c\u6b21\u672a\u8c03\u7528 DeepSeek"})
                    persisted_job = dict(job)
                else:
                    persisted_job = None
            if persisted_job is not None:
                try:
                    self._persist_source_review_ai_job(persisted_job)
                except Exception as persist_exc:
                    print("source-review-ai job %s state persistence failed (%s)" %
                          (job_id, type(persist_exc).__name__))
            _SOURCE_REVIEW_AI_LOCK.release()
            print("source-review-ai job %s failed to start (%s)" %
                  (job_id, type(exc).__name__))

    def _serve_internal_source_review_ai_run(self):
        if not self._has_source_review_automation_token():
            self._send_unauthorized()
            return
        self._json_body(maximum=1024)
        self._send_json(self._run_source_review_ai())

    def _run_source_review_ai(self, already_locked=False):
        if not already_locked and not _SOURCE_REVIEW_AI_LOCK.acquire(blocking=False):
            raise CloudError("AI 核对正在运行，请稍后查看结果", 409)
        try:
            try:
                config = slg_source_review_ai.read_config(
                    self._source_review_ai_config_path())
                if not config["api_key"]:
                    raise CloudError("请先配置 DeepSeek API 密钥", 400)
                pending = self._capture_pending_source_reviews()
                if not pending:
                    return {"ok": True, "batch_count": 0,
                            "accepted": 0, "rejected": 0,
                            "uncertain": 0, "usage": {}}
                ai_result = slg_source_review_ai.classify_batch(
                    config["api_key"], config["model"], pending)
            except slg_source_review_ai.SourceReviewAIError as exc:
                raise CloudError(str(exc), 502) from exc

            # The catalogue can refresh while the model is responding. Recheck
            # the revision before saving so stale advice is never attached.
            source_rows, _candidates = self._read_source_review_catalog()
            current = {
                (str(row["source_id"]), str(row["external_id"])):
                    self._source_review_fingerprint(row)
                for row in source_rows
            }
            valid, stale = [], 0
            for item in ai_result["items"]:
                if current.get((item["source_id"], item["external_id"])) != item["content_hash"]:
                    stale += 1
                else:
                    valid.append(item)
            result = self._cloud().admin_import_source_review_suggestions(valid) \
                if valid else {"accepted": 0, "rejected": 0, "items": []}
            accepted = int(result.get("accepted", 0))
            rejected = (int(ai_result.get("rejected", 0)) + stale
                        + int(result.get("rejected", 0)))
            return {
                "ok": rejected == 0,
                "batch_count": len(pending),
                "accepted": accepted,
                "rejected": rejected,
                "uncertain": sum(1 for item in valid
                                 if item["verdict"] == "uncertain"),
                "usage": ai_result.get("usage", {}),
                "model": config["model"],
            }
        finally:
            _SOURCE_REVIEW_AI_LOCK.release()

    @staticmethod
    def _source_review_canonical_id(decision, value):
        """Validate an optional same-game reference without coercing JSON types."""
        if decision not in ("same", "distinct", "skip"):
            raise CloudError("invalid review decision")
        if value is None:
            return None
        # bool is an int subclass in Python; accepting it (or a float/string via
        # int()) can turn malformed JSON into a real game ID.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CloudError("invalid canonical game id")
        if decision != "same":
            raise CloudError("canonical game id is only valid for a same decision")
        return value

    def _serve_admin_source_review_decision(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        obj = self._json_body(maximum=4096)
        source_id = obj.get("source_id")
        external_id = obj.get("external_id")
        content_hash = obj.get("content_hash")
        if source_id not in ("f95zone", "zyoudao") or not isinstance(external_id, str):
            raise CloudError("invalid source identity")
        source_rows, _candidates = self._read_source_review_catalog()
        row = next((r for r in source_rows if r["source_id"] == source_id
                    and r["external_id"] == external_id), None)
        if row is None or self._source_review_fingerprint(row) != content_hash:
            raise CloudError("stale or missing source revision", 409)
        decision = obj.get("decision")
        canonical_id = self._source_review_canonical_id(
            decision, obj.get("canonical_game_id"))
        if canonical_id is not None:
            if not any(int(game["id"]) == canonical_id for game in _candidates):
                raise CloudError("canonical game not found", 404)
        result = self._cloud().admin_decide_source_review(
            source_id, external_id, content_hash, decision,
            canonical_id, obj.get("note", ""))
        # Cloud review history and the site catalogue are separate SQLite files.
        # Apply the desired state now; publish() also retries this reconciliation
        # after a crash or transient catalogue lock.
        result["catalog_apply"] = _apply_source_review_decisions(
            self.data_dir, self.publish_dir)
        self._cloud().append_admin_audit(
            "source_review_decision", source_id + ":" + external_id,
            {"decision": decision, "canonical_game_id": canonical_id})
        self._send_json(result)

    def _serve_admin_source_review_batch_accept(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        obj = self._json_body(maximum=16384)
        items = obj.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise CloudError("expected 1-20 source review items")
        source_rows, _candidates = self._read_source_review_catalog()
        current = {
            (row["source_id"], row["external_id"]):
                self._source_review_fingerprint(row)
            for row in source_rows
        }
        ai_index, manual_index = self._cloud().admin_source_review_index()
        results = []
        applied = 0
        for item in items:
            if not isinstance(item, dict):
                results.append({"decision": "", "status": "skipped"})
                continue
            source_id = item.get("source_id")
            external_id = item.get("external_id")
            content_hash = item.get("content_hash")
            result_item = {"source_id": str(source_id or "")[:32],
                           "external_id": str(external_id or "")[:128]}
            valid_identity = (source_id in ("f95zone", "zyoudao")
                              and isinstance(external_id, str)
                              and 0 < len(external_id) <= 128
                              and isinstance(content_hash, str)
                              and 0 < len(content_hash) <= 96)
            identity = (source_id, external_id, content_hash)
            suggestion = ai_index.get(identity) if valid_identity else None
            decision = None
            if (valid_identity and current.get((source_id, external_id)) == content_hash
                    and identity not in manual_index and suggestion
                    and float(suggestion.get("confidence", 0)) >= 0.95):
                if (suggestion.get("record_kind") == "game"
                        and suggestion.get("verdict") == "distinct"):
                    decision = "distinct"
                elif (suggestion.get("record_kind") == "non_game"
                      and suggestion.get("verdict") == "distinct"):
                    decision = "skip"
            if decision is None:
                result_item.update({"decision": "", "status": "skipped"})
                results.append(result_item)
                continue
            try:
                saved = self._cloud().admin_decide_source_review(
                    source_id, external_id, content_hash, decision, None,
                    "high-confidence source-review batch acceptance")
            except CloudError:
                result_item.update({"decision": "", "status": "skipped"})
                results.append(result_item)
                continue
            if saved.get("ok") and not saved.get("idempotent"):
                applied += 1
                status = "applied"
            else:
                status = "skipped"
            result_item.update({"decision": decision, "status": status})
            results.append(result_item)
        catalog_apply = (_apply_source_review_decisions(
            self.data_dir, self.publish_dir) if applied else None)
        skipped = len(items) - applied
        self._cloud().append_admin_audit(
            "source_review_batch_accept", "",
            {"applied": applied, "skipped": skipped})
        self._send_json({"ok": True, "applied": applied, "skipped": skipped,
                         "items": results, "catalog_apply": catalog_apply})

    def _serve_admin_feedback(self, export=False):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise CloudError("bad feedback query")
        try:
            limit = int(query.get("limit", ["500" if export else "30"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            raise CloudError("bad pagination")
        status = query.get("status", [""])[0]
        category = query.get("category", [""])[0]
        store = self._cloud()
        if not export:
            self._send_json(store.admin_feedback(
                limit, offset, status, category))
            return
        rows = store.admin_feedback_export(limit, offset, status, category)
        body = "".join(json.dumps(row, ensure_ascii=False) + "\n"
                       for row in rows).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         'attachment; filename="slgking-feedback.jsonl"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # --- admin (announcements / flags / group code) ---------------------------

    def _admin_config_path(self):
        return os.path.join(self.publish_dir, "config.json")

    def _cloud_feature_disabled(self, flag):
        """Enforce admin feature switches at the API boundary for every account."""
        try:
            with open(self._admin_config_path(), "r", encoding="utf-8") as fh:
                config = json.load(fh)
            flags = config.get("flags", {}) if isinstance(config, dict) else {}
            return isinstance(flags, dict) and flags.get(flag) is True
        except (OSError, ValueError, json.JSONDecodeError):
            # A missing or malformed optional config should not make the
            # account service unavailable; the client uses the same default.
            return False

    def _serve_admin_page(self):
        page_path = os.path.join(os.path.dirname(__file__), "admin_page.html")
        try:
            with open(page_path, "rb") as fh:
                body = fh.read()
        except OSError:
            body = ADMIN_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_account_application_page(self):
        page_path = os.path.join(os.path.dirname(__file__), "account_apply.html")
        self._send_file(page_path, "text/html; charset=utf-8", "no-store")

    def _serve_admin_config(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        cfg = self._admin_config_path()
        if not os.path.isfile(cfg):
            self._send_json({})
            return
        try:
            with open(cfg, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._send_json(data if isinstance(data, dict) else {})
        except (ValueError, json.JSONDecodeError):
            self._send_json({})

    def _serve_admin_config_post(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 65536:
                self._send_json({"ok": False, "error": "bad length"}, status=400)
                return
            obj = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "bad body"}, status=400)
            return
        if not isinstance(obj, dict):
            self._send_json({"ok": False, "error": "bad body"}, status=400)
            return
        config_path = self._admin_config_path()
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}

        # Older admin pages submit only the latest announcement and do not
        # know about the archive or the nested reward field. Preserve them.
        if "announcement" not in obj and "announcement" in existing:
            obj["announcement"] = existing["announcement"]
        incoming_announcement = obj.get("announcement")
        existing_announcement = existing.get("announcement")
        if (isinstance(incoming_announcement, dict)
                and "reward" not in incoming_announcement
                and isinstance(existing_announcement, dict)
                and "reward" in existing_announcement):
            incoming_announcement["reward"] = existing_announcement["reward"]

        # Preserve the legacy campaign unless the request explicitly changes
        # it. New pages submit a nested reward and mirror it to this old key.
        if "maintenance_compensation" not in obj:
            if "maintenance_compensation" in existing:
                obj["maintenance_compensation"] = existing[
                    "maintenance_compensation"]
        if (isinstance(incoming_announcement, dict)
                and "reward" in incoming_announcement):
            obj["maintenance_compensation"] = incoming_announcement["reward"]
        campaign = obj.get("maintenance_compensation")

        # The archive is server-managed: a new ID moves the previous current
        # notice into history. Cap the count so config size stays predictable.
        history = existing.get("announcement_history")
        if not isinstance(history, list):
            history = []
        history = [dict(item) for item in history[-49:]
                   if isinstance(item, dict)]
        if (isinstance(existing_announcement, dict)
                and isinstance(incoming_announcement, dict)):
            old_id = str(existing_announcement.get("id") or "").strip()
            new_id = str(incoming_announcement.get("id") or "").strip()
            has_content = bool(
                str(existing_announcement.get("title") or "").strip()
                or str(existing_announcement.get("body") or "").strip())
            if new_id and new_id != old_id and any(
                    str(item.get("id") or "").strip() == new_id
                    for item in history):
                self._send_json({"ok": False,
                                 "error": "公告 ID 已用于历史公告，请使用新 ID"},
                                status=409)
                return
            if old_id != new_id and has_content:
                archived = dict(existing_announcement)
                if not old_id:
                    archived["id"] = "archived-" + str(int(time.time()))
                archived.setdefault("created_at", _now())
                history = [item for item in history
                           if not old_id or str(item.get("id") or "") != old_id]
                history.append(archived)
        obj["announcement_history"] = history[-50:]

        if isinstance(incoming_announcement, dict):
            ann_id = incoming_announcement.get("id", "")
            ann_title = incoming_announcement.get("title", "")
            ann_body = incoming_announcement.get("body", "")
            if (not isinstance(ann_id, str) or len(ann_id) > 64
                    or not isinstance(ann_title, str) or len(ann_title) > 120
                    or not isinstance(ann_body, str) or len(ann_body) > 5000):
                self._send_json({"ok": False,
                                 "error": "公告 ID、标题或正文长度超出限制"},
                                status=400)
                return
            if ("reward" in incoming_announcement
                    and incoming_announcement["reward"] is not None
                    and not isinstance(incoming_announcement["reward"], dict)):
                self._send_json({"ok": False,
                                 "error": "公告积分奖励配置无效"}, status=400)
                return
        if campaign is not None:
            valid = isinstance(campaign, dict)
            active = campaign.get("active") if valid else None
            campaign_id = campaign.get("id") if valid else None
            title = campaign.get("title") if valid else None
            body = campaign.get("body") if valid else None
            amount = campaign.get("amount") if valid else None
            valid = (valid and isinstance(active, bool)
                     and isinstance(campaign_id, str) and len(campaign_id) <= 64
                     and isinstance(title, str) and len(title) <= 100
                     and isinstance(body, str) and len(body) <= 1000
                     and isinstance(amount, int) and not isinstance(amount, bool)
                     and 0 <= amount <= 100000)
            if active:
                valid = (valid and bool(re.fullmatch(
                    r"[a-z0-9][a-z0-9._-]{0,63}", campaign_id))
                    and bool(title.strip()) and 1 <= amount <= 100000)
            elif campaign_id:
                valid = (valid and bool(re.fullmatch(
                    r"[a-z0-9][a-z0-9._-]{0,63}", campaign_id)))
            if not valid:
                self._send_json({"ok": False,
                                 "error": "积分活动配置无效，请检查活动 ID、说明和积分数"},
                                status=400)
                return
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path)
        flags = obj.get("flags", {})
        safe_flags = {key: flags[key] for key in
                      ("disable_signin", "disable_lottery")
                      if isinstance(flags, dict)
                      and isinstance(flags.get(key), bool)}
        min_version = obj.get("min_version", "")
        if not isinstance(min_version, str):
            min_version = ""
        min_version = min_version[:80]
        self._cloud().append_admin_audit(
            "config_update", "",
            {"flags": safe_flags, "min_version": min_version})
        announcement_changed = incoming_announcement != existing_announcement
        if announcement_changed:
            old_id = (str(existing_announcement.get("id") or "")[:64]
                      if isinstance(existing_announcement, dict) else "")
            new_id = (str(incoming_announcement.get("id") or "")[:64]
                      if isinstance(incoming_announcement, dict) else "")
            self._cloud().append_admin_audit(
                "announcement_update", new_id,
                {"previous_id": old_id, "announcement_id": new_id})
        self._send_json({"ok": True})

    def _serve_admin_group_code(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        self._send_json({"code": self._cloud().group_code()})

    def _serve_admin_legacy_requests(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        query = parse_qs(urlparse(self.path).query)
        try:
            limit = int(query.get("limit", ["30"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            raise CloudError("bad pagination")
        self._send_json(self._cloud().migration_requests(
            limit, offset, query.get("status", [""])[0]))

    def _serve_admin_account_applications(self):
        if not self._has_admin_key():
            self._send_unauthorized()
            return
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        if (set(query) - {"limit", "offset", "status"}
                or any(len(values) != 1 for values in query.values())):
            raise CloudError("bad pagination")
        try:
            limit = int(query.get("limit", ["30"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            raise CloudError("bad pagination")
        self._send_json(self._cloud().admin_account_applications(
            limit, offset, query.get("status", [""])[0]))

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler names it this way
        self._cloud_call(self._do_get)

    def _do_get(self):
        path = self.path.split("?", 1)[0]
        if path in ("/m", "/m/") or path.startswith("/m/"):
            self._serve_web_static(path)
        elif path in ("/admin/forum", "/admin/forum/"):
            self._send_file(os.path.join(os.path.dirname(__file__), "forum_admin.html"),
                            "text/html; charset=utf-8", "no-store", _WEB_NOINDEX)
        elif path.startswith("/admin/forum/"):
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._forum_request(lambda: self._serve_admin_forum_get(path))
        elif path == "/web/account/register/pending":
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._serve_web_registration(path)
        elif path == "/web/account/comments":
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._serve_web_comments_get()
        elif (path in ("/web/forum/threads", "/web/forum/my")
              or path.startswith("/web/forum/threads/")):
            self._forum_request(lambda: self._serve_web_forum_get(path))
        elif path == "/catalogue/tags":
            self._catalogue_request(self._serve_catalogue_tags)
        elif path == "/catalogue/filters":
            self._catalogue_request(self._serve_catalogue_filters)
        elif path == "/catalogue/search":
            self._catalogue_request(self._serve_catalogue_search)
        elif path.startswith("/catalogue/game/"):
            self._catalogue_request(lambda: self._serve_catalogue_game(path[len("/catalogue/game/"):]))
        elif path == "/account/session":
            self._serve_web_session("GET")
        elif path == "/web/shop":
            self._serve_web_shop()
        elif path == "/web/announcements":
            self._serve_web_announcements()
        elif path == "/web/account/personal-comments":
            self._serve_personal_comments("GET")
        elif path == "/web/account/rewards/maintenance":
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._serve_web_maintenance_reward()
        elif path in ("/web/account/me", "/web/account/quests"):
            self._serve_web_account_get(path)
        elif path in ("/admin-manifest.json", "/admin-sw.js") or path.startswith("/assets/admin/"):
            self._serve_admin_static(path)
        elif path == "/admin/session":
            self._serve_admin_session("GET")
        elif path == "/admin/push/config":
            self._serve_admin_push_config()
        elif path == "/manifest.json":
            # never cache: it is the update signal clients poll.
            self._send_file(os.path.join(self.publish_dir, "manifest.json"),
                            "application/json; charset=utf-8", "no-store")
        elif path == "/slgking.db":
            self._send_file(os.path.join(self.publish_dir, "slgking.db"),
                            "application/octet-stream", "no-cache")
        elif path.startswith("/catalogues/western_slg/"):
            # Strict source status and immutable hash-addressed DB allowlist.
            relative = path[len("/catalogues/western_slg/"):]
            parts = relative.split("/")
            content_type = None
            cache = "public, max-age=604800, immutable"
            if relative == "status.json":
                content_type = "application/json; charset=utf-8"
                cache = "no-store"
            elif len(parts) == 2 and parts[0] == "snapshots":
                name = parts[1]
                if re.fullmatch(r"[0-9a-f]{64}\.db", name):
                    content_type = "application/octet-stream"
            root = os.path.join(self.publish_dir, "catalogues", "western_slg")
            asset = os.path.join(root, *parts) if content_type else ""
            parents = [self.publish_dir, os.path.join(self.publish_dir, "catalogues"), root]
            if len(parts) == 2:
                parents.append(os.path.join(root, parts[0]))
            if (not content_type or any(os.path.islink(p) for p in parents)
                    or os.path.islink(asset) or not os.path.isfile(asset)):
                self._send_json({"ok": False, "error": "not found"}, 404)
            else:
                self._send_file(asset, content_type, cache)
        elif path.startswith("/catalogues/dlsite/"):
            # Only immutable catalogue assets and the active update signal.
            relative = path[len("/catalogues/dlsite/"):]
            parts = relative.split("/")
            content_type = None
            cache = "public, max-age=604800, immutable"
            if relative in ("manifest.json", "status.json"):
                content_type = "application/json; charset=utf-8"
                cache = "no-store"
            elif len(parts) == 2:
                folder, name = parts
                suffix = ".db" if folder == "snapshots" else ".json" if folder == "cover-index" else ""
                digest = name[:-len(suffix)] if suffix and name.endswith(suffix) else ""
                if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                    content_type = "application/octet-stream" if suffix == ".db" else "application/json; charset=utf-8"
            root = os.path.join(self.publish_dir, "catalogues", "dlsite")
            asset = os.path.join(root, *parts) if content_type else ""
            parents = [self.publish_dir, os.path.join(self.publish_dir, "catalogues"), root]
            if len(parts) == 2:
                parents.append(os.path.join(root, parts[0]))
            if (not content_type or any(os.path.islink(p) for p in parents)
                    or os.path.islink(asset) or not os.path.isfile(asset)):
                self._send_json({"ok": False, "error": "not found"}, 404)
            else:
                self._send_file(asset, content_type, cache)
        elif path.startswith("/covers/"):
            # basename() alone defeats ../ traversal.
            name = os.path.basename(path[len("/covers/"):])
            # A cover is immutable per slug, so let CDNs and browsers keep it.
            # The type must follow the stored file: ~18% of covers are webp or
            # avif, and a browser will not decode those when told image/jpeg.
            # The desktop client writes the bytes to disk and decodes with PIL,
            # so a more accurate type cannot affect it.
            self._send_file(os.path.join(self.publish_dir, "covers", name),
                            _COVER_TYPES.get(
                                os.path.splitext(name)[1].lower(), "image/jpeg"),
                            "public, max-age=604800, immutable")
        elif path == "/config.json":
            cfg = os.path.join(self.publish_dir, "config.json")
            if os.path.isfile(cfg):
                self._send_file(cfg, "application/json; charset=utf-8", "no-store")
            else:
                self._send_json({})
        elif path == "/stats.json":
            self._serve_stats()
        elif path == '/account/shop':
            self._serve_web_shop(include_functional=True)
        elif path == '/admin/shop' or path.startswith('/admin/collection-jobs'):
            self._serve_admin_operations(path)
        elif path == "/admin/stats":
            self._serve_admin_stats()
        elif path == "/admin/maintenance":
            self._serve_admin_maintenance()
        elif path == "/community/leaderboard":
            self._catalogue_request(self._serve_public_leaderboard)
        elif path == "/account/rewards/maintenance":
            self._account_maintenance_reward()
        elif path in ("/account/me", "/account/devices", "/account/comments",
                      "/account/quests", "/account/referrals/me"):
            self._account_get(path)
        elif path == "/comments":
            self._serve_comments()
        elif path == "/comments/count":
            self._serve_comment_count()
        elif path == "/admin/comments":
            self._serve_admin_comments()
        elif path == "/admin/audit":
            self._serve_admin_audit()
        elif path == "/admin/source-reviews":
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            self._admin_source_review_items(query)
        elif path == "/admin/source-review-ai/config":
            self._serve_admin_source_review_ai_config()
        elif path.startswith("/admin/source-review-ai/jobs/request/"):
            self._serve_admin_source_review_ai_request_status(
                path[len("/admin/source-review-ai/jobs/request/"):])
        elif path.startswith("/admin/source-review-ai/jobs/"):
            self._serve_admin_source_review_ai_job(
                path[len("/admin/source-review-ai/jobs/"):])
        elif path == "/admin/source-reviews/export":
            self._serve_admin_source_review_export()
        elif path == "/admin/feedback":
            self._serve_admin_feedback()
        elif path == "/admin/feedback/export":
            self._serve_admin_feedback(export=True)
        elif path == "/admin/config":
            self._serve_admin_config()
        elif path == "/admin/group_code":
            self._serve_admin_group_code()
        elif path == "/admin/legacy-requests":
            self._serve_admin_legacy_requests()
        elif path == "/admin/account-applications":
            self._serve_admin_account_applications()
        elif path in ("/account/apply", "/account/apply/"):
            self._serve_account_application_page()
        elif path in ("/admin", "/admin/"):
            self._serve_admin_page()
        elif path == "/robots.txt":
            robots = os.path.join(os.path.dirname(__file__), "robots.txt")
            self._send_file(robots, "text/plain; charset=utf-8", "no-store")
        elif path.startswith("/site-assets/"):
            name = path[len("/site-assets/"):]
            content_type = _HOME_ASSET_ALLOWLIST.get(name)
            root = os.path.join(os.path.dirname(__file__), "site-assets")
            asset = os.path.join(root, name) if content_type else ""
            if (not content_type or os.path.islink(root) or os.path.islink(asset)
                    or not os.path.isfile(asset)):
                self._send_json({"ok": False, "error": "not found"}, 404)
            else:
                self._send_file(asset, content_type, "no-store")
        elif path == "/":
            home_path = os.path.join(os.path.dirname(__file__), "site_home.html")
            self._send_file(home_path, "text/html; charset=utf-8", "no-store")
        elif path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "not found")

    def _serve_report(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 4096:
                self._send_json({"ok": False, "error": "bad length"}, status=400)
                return
            body = self.rfile.read(length)
            event = json.loads(body.decode("utf-8"))
            from slg_usage_metrics import sanitize_event
            usage_event = sanitize_event(event)
            clean_event = _report_sanitize(event)
            if clean_event is None or event.get('v') == '2':
                clean_event = usage_event
            if clean_event is None:
                self._send_json({"ok": False, "error": "bad body"}, status=400)
                return
            encoded = json.dumps(clean_event, ensure_ascii=False,
                                 separators=(",", ":"))
            payload = (encoded + "\n").encode("utf-8")
            if len(payload) > 2048:
                self._send_json({"ok": False, "error": "bad body"}, status=400)
                return
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "bad body"}, status=400)
            return
        if _report_rate_limited(self._auth_identity()):
            self._send_json({"ok": False, "error": "rate limited"}, status=429)
            return
        path = self._reports_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with _REPORT_LOCK:
                # Share the existing 5 GiB reserve; keep active+rotated logs
                # below 16 MiB, excluding only a bounded single event overshoot.
                self._cloud()._writable()
                self._rotate_reports_if_needed(path, len(payload))
                # Persist before acknowledging so accepted events survive log rotation.
                if usage_event is not None:
                    self._usage_store().record(event)
                with open(path, "ab") as fh:
                    fh.write(payload)
        except (OSError, sqlite3.Error):
            self._send_json({"ok": False, "error": "storage unavailable"},
                            status=503)
            return
        self._send_json({"ok": True})

    def do_POST(self):  # noqa: N802
        self._cloud_call(self._do_post)

    def _do_post(self):
        path = self.path.split("?", 1)[0]
        if path == '/admin/shop' or path.startswith(('/admin/shop/', '/admin/collection-jobs')):
            self._serve_admin_operations(path, writing=True)
            return
        if path == "/report":
            self._serve_report()
        elif path == "/admin/owner-account":
            if not self._is_secure_transport():
                self._send_json({"ok": False, "error": "HTTPS required"},
                                status=403)
                return
            if not self._has_owner_key():
                self._send_unauthorized()
                return
            obj = self._json_body(maximum=512)
            if "operation_id" not in obj or set(obj) - {
                    "operation_id", "device_label"}:
                raise CloudError("bad owner account request", 400)
            self._send_json(self._cloud().create_owner_account(
                obj.get("operation_id"),
                obj.get("device_label", "黄油之王 管理员账号")), 201)
        elif path == "/admin/owner-account/reissue":
            if not self._is_secure_transport():
                self._send_json({"ok": False, "error": "HTTPS required"},
                                status=403)
                return
            if not self._has_owner_key():
                self._send_unauthorized()
                return
            obj = self._json_body(maximum=16)
            if obj:
                raise CloudError("bad owner account request", 400)
            self._send_json(
                self._cloud().reissue_owner_account_credentials())
        elif path == "/account/session":
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._serve_web_session("POST")
        elif path == "/web/comments" or path.startswith("/web/comments/"):
            self._measure_maintenance_operation(
                "points_ledger", lambda: self._serve_web_comments_write(path, "POST"))
        elif path == "/web/personal-comments":
            self._serve_personal_comments("POST")
        elif path in ("/web/account/shop/buy", "/web/account/equip",
                      "/web/account/rewards/maintenance/claim"):
            if not self._same_origin_request():
                raise CloudError("cross-origin request blocked", 403)
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            if path.endswith("/claim"):
                self._measure_maintenance_operation(
                    "points_ledger", lambda: self._serve_web_maintenance_reward(claim=True))
            else:
                self._serve_web_account_post(path)
        elif path == "/account/admin-login":
            if not self._is_secure_transport():
                self._send_json({"ok": False, "error": "HTTPS required"},
                                status=403)
                return
            self._account_post(path)
        elif path in ("/web/account/register", "/web/account/register/ack"):
            if not self._same_origin_request():
                raise CloudError("cross-origin request blocked", 403)
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._serve_web_registration(path)
        elif path in ("/web/account/signin", "/web/account/quests/claim",
                      "/web/account/profile"):
            if not self._same_origin_request():
                raise CloudError("cross-origin request blocked", 403)
            if path == "/web/account/profile":
                if not self._is_secure_transport():
                    raise CloudError("HTTPS required", 403)
                self._serve_web_account_post(path)
            else:
                self._measure_maintenance_operation(
                    "points_ledger", lambda: self._serve_web_account_post(path))
        elif (path in ("/web/forum/threads", "/web/forum/reports")
              or (path.startswith("/web/forum/threads/")
                  and path.endswith("/replies"))):
            if not self._same_origin_request():
                raise CloudError("cross-origin request blocked", 403)
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._forum_request(lambda: self._serve_web_forum_post(path), write=True)
        elif path.startswith("/admin/forum/"):
            if not self._same_origin_request():
                raise CloudError("cross-origin request blocked", 403)
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._forum_request(lambda: self._serve_admin_forum_post(path), write=True)
        elif path == "/admin/session":
            self._serve_admin_session("POST")
        elif path == "/admin/push/subscription":
            self._serve_admin_push_subscription()
        elif path in ("/account/developer/titles/grant-all",
                      "/account/developer/appearances/grant-all"):
            if not self._is_secure_transport():
                self._send_json({"ok": False, "error": "HTTPS required"},
                                status=403)
                return
            self._account_developer_grant_all_titles()
        elif path in ("/account/create", "/account/admin-login", "/account/login", "/account/rotate",
                      "/account/applications",
                      "/account/legacy-request", "/account/legacy-migrate",
                      "/account/signin", "/rewards/group",
                      "/account/rewards/maintenance/claim",
                      "/account/shop/buy", "/account/lottery/draw", "/account/equip",
                      "/account/profile", "/account/feedback",
                      "/account/referrals/redeem",
                      "/account/quests/claim", "/account/quests/rating"):
            if path == "/account/rewards/maintenance/claim":
                self._measure_maintenance_operation(
                    "points_ledger",
                    lambda: self._account_maintenance_reward(claim=True))
            elif path == "/account/create":
                self._measure_maintenance_operation(
                    "account_registration", lambda: self._account_post(path))
            elif path in _POINT_ACCOUNT_PATHS:
                self._measure_maintenance_operation(
                    "points_ledger", lambda: self._account_post(path))
            else:
                self._account_post(path)
        elif path == "/admin/config":
            self._serve_admin_config_post()
        elif path == "/admin/source-reviews/import":
            self._serve_admin_source_review_import()
        elif path == "/admin/source-review-ai/config":
            self._serve_admin_source_review_ai_config_post()
        elif path == "/admin/source-review-ai/test":
            self._serve_admin_source_review_ai_test()
        elif path == "/admin/source-review-ai/run":
            self._serve_admin_source_review_ai_run()
        elif path == "/internal/source-review-ai/run":
            self._serve_internal_source_review_ai_run()
        elif path == "/admin/source-reviews/decision":
            self._serve_admin_source_review_decision()
        elif path == "/admin/source-reviews/batch-accept":
            self._serve_admin_source_review_batch_accept()
        elif path.startswith("/admin/comments/") and path.endswith("/decision"):
            if not self._has_admin_key():
                self._send_unauthorized()
                return
            cid = path[len("/admin/comments/"):-len("/decision")]
            obj = self._json_body()
            store = self._cloud()
            result = self._measure_maintenance_operation(
                "points_ledger", lambda: store.decide(
                    cid, obj.get("decision"), obj.get("reason", "")))
            self._send_json(result)
        elif (path.startswith("/admin/account-applications/")
              and path.endswith("/decision")):
            if not self._has_admin_key():
                self._send_unauthorized()
                return
            application_id = path[len("/admin/account-applications/"):-len("/decision")]
            if (not application_id.isdigit() or len(application_id) > 19
                    or int(application_id) <= 0):
                raise CloudError("not found", 404)
            obj = self._json_body(maximum=1024)
            if set(obj) - {"decision", "reason"}:
                raise CloudError("bad body")
            self._send_json(self._cloud().decide_account_application(
                int(application_id), obj.get("decision"), obj.get("reason", "")))
        elif (path.startswith("/admin/account-applications/")
              and path.endswith("/reissue")):
            if not self._has_admin_key():
                self._send_unauthorized()
                return
            application_id = path[len("/admin/account-applications/"):-len("/reissue")]
            if (not application_id.isdigit() or len(application_id) > 19
                    or int(application_id) <= 0):
                raise CloudError("not found", 404)
            obj = self._json_body(maximum=1024)
            if obj:
                raise CloudError("bad body")
            self._send_json(self._cloud().reissue_account_application_credentials(
                int(application_id)))
        elif path == "/admin/legacy-requests/batch-approve":
            if not self._has_admin_key():
                self._send_unauthorized()
                return
            obj = self._json_body(maximum=16384)
            ids = obj.get("ids")
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 200
                    or any(not isinstance(i, int) or isinstance(i, bool) or i <= 0
                           for i in ids)):
                raise CloudError("expected 1-200 migration ids")
            result = self._measure_maintenance_operation(
                "points_ledger", lambda: self._cloud().decide_migrations_bulk(ids))
            self._cloud().append_admin_audit(
                "legacy_migration_batch_approve", "",
                {"applied": result["applied"], "skipped": result["skipped"],
                 "total_points": result["total_points"],
                 "total_titles": result["total_titles"]})
            self._send_json(result)
        elif path.startswith("/admin/legacy-requests/") and path.endswith("/decision"):
            if not self._has_admin_key():
                self._send_unauthorized()
                return
            rid = path[len("/admin/legacy-requests/"):-len("/decision")]
            if not rid.isdigit():
                raise CloudError("not found", 404)
            obj = self._json_body()
            self._send_json(self._measure_maintenance_operation(
                "points_ledger", lambda: self._cloud().decide_migration(
                    int(rid), obj.get("decision"), obj.get("points", 0),
                    obj.get("titles", []), obj.get("reason", ""))))
        elif path.startswith("/admin/feedback/"):
            if not self._has_admin_key():
                self._send_unauthorized()
                return
            feedback_id = path[len("/admin/feedback/"):]
            if not feedback_id.isdigit():
                raise CloudError("not found", 404)
            obj = self._json_body()
            self._send_json(self._measure_maintenance_operation(
                "points_ledger", lambda: self._cloud().decide_feedback(
                    feedback_id, obj.get("action"), obj.get("note", ""))))
        elif path.startswith("/admin/accounts/") and path.endswith("/ban"):
            if not self._has_admin_key():
                self._send_unauthorized()
                return
            aid = path[len("/admin/accounts/"):-len("/ban")]
            banned = self._json_body().get("banned", True)
            if not isinstance(banned, bool):
                raise CloudError("bad body")
            store = self._cloud()
            store.ban(aid, banned)
            self._send_json({"ok": True, "account_id": aid, "banned": banned})
        elif path == "/comments":
            self._measure_maintenance_operation(
                "points_ledger", self._serve_post_comment)
        elif path.startswith("/comments/") and path.endswith("/vote"):
            cid = path[len("/comments/"):-len("/vote")]
            self._send_json(self._cloud().vote(self._account_id(), cid,
                                               self._json_body().get("value")))
        elif path.startswith("/comments/") and path.endswith("/report"):
            cid = path[len("/comments/"):-len("/report")]
            if cid:
                self._serve_report_comment(cid)
            else:
                self.send_error(404, "not found")
        else:
            self.send_error(404, "not found")

    def do_DELETE(self):  # noqa: N802
        self._cloud_call(self._do_delete)

    def _do_delete(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/web/personal-comments/"):
            self._serve_personal_comments("DELETE")
            return
        if path.startswith("/web/comments/"):
            self._measure_maintenance_operation(
                "points_ledger", lambda: self._serve_web_comments_write(path, "DELETE"))
            return
        if (path.startswith("/web/forum/threads/")
                or path.startswith("/web/forum/replies/")):
            if not self._same_origin_request():
                raise CloudError("cross-origin request blocked", 403)
            if not self._is_secure_transport():
                raise CloudError("HTTPS required", 403)
            self._forum_request(lambda: self._serve_web_forum_delete(path), write=True)
            return
        if path == "/account/session":
            self._serve_web_session("DELETE")
            return
        if path == "/admin/session":
            self._serve_admin_session("DELETE")
            return
        if path == "/admin/push/subscription":
            self._serve_admin_push_subscription(delete=True)
            return
        if path.startswith("/account/devices/"):
            aid = self._account_id()
            self._cloud().revoke(aid, path[len("/account/devices/"):])
            self._send_json({"ok": True})
            return
        if path.startswith("/admin/comments/"):
            cid = path[len("/admin/comments/"):]
            if cid:
                self._serve_delete_comment(cid, admin=True)
                return
        if path.startswith("/comments/"):
            cid = path[len("/comments/"):]
            if cid:
                self._serve_delete_comment(cid)
                return
        self.send_error(404, "not found")

    def log_message(self, fmt, *args):  # noqa: A003 - quieter than the default
        message = fmt % args
        # Redact the former query-token parameter if an old/manual request still
        # supplies it. Authentication itself never reads this parameter.
        message = re.sub(r"([?&]key=)[^&\s]*", r"\1[redacted]", message,
                         flags=re.IGNORECASE)
        sys.stderr.write("[%s] %s\n" % (_now(), message))


def serve(port=PORT, publish_dir=PUBLISH_DIR, data_dir=DATA_DIR):
    Handler.publish_dir = publish_dir
    Handler.data_dir = data_dir
    server = CatalogueHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write("serving %s on :%d\n" % (publish_dir, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--publish" in argv:
        m = publish()
        print(json.dumps(m, ensure_ascii=False))
        return 0

    port, publish_dir, data_dir = PORT, PUBLISH_DIR, DATA_DIR
    i = 0
    while i < len(argv):
        if argv[i] == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2
        elif argv[i] == "--publish-dir" and i + 1 < len(argv):
            publish_dir = argv[i + 1]; i += 2
        elif argv[i] == "--data-dir" and i + 1 < len(argv):
            data_dir = argv[i + 1]; i += 2
        else:
            i += 1
    serve(port, publish_dir, data_dir)


if __name__ == "__main__":
    sys.exit(main())
