"""Small, private SQLite store for SLGking cloud identities and discussion.

The database lives alongside the private comments log, outside the published
catalogue. No third-party packages are needed on the server.
"""

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import random
import os
import secrets
import shutil
import sqlite3
import time
import unicodedata
import uuid


_BUSINESS_PLATFORM = ContextVar('slg_business_platform', default='unknown')

@contextmanager
def business_platform(platform):
    platform = platform if platform in ('windows', 'android', 'web') else 'unknown'
    token = _BUSINESS_PLATFORM.set(platform)
    try:
        yield
    finally:
        _BUSINESS_PLATFORM.reset(token)

CHINA = timezone(timedelta(hours=8))
QUEST_PERIODS = {
    "daily_lottery": "daily",
    "daily_public_comment": "daily",
    "weekly_comments_3": "weekly",
    "weekly_signin_5": "weekly",
}
MIN_FREE_BYTES = 5 * 1024 ** 3
SESSION_IDLE_SECONDS = 7 * 86400
DEVICE_SESSION_TOUCH_SECONDS = 60
DEVELOPER_ACCOUNT_ID = "developer"
MAX_FEEDBACK_ITEMS = 100000
ADMIN_SESSION_SECONDS = 7 * 86400
ADMIN_SESSION_MAX_ROWS = 256
# Browser sessions are independent of desktop devices. Logout, credential
# rotation, account bans, idle expiry, and the bounded session store revoke them.
WEB_SESSION_SECONDS = 30 * 86400
WEB_SESSION_MAX_ROWS = 5000
WEB_SESSION_TOUCH_SECONDS = 60
ADMIN_PUSH_SUBSCRIPTION_MAX_ROWS = 50
FEEDBACK_TITLE_ID = "feedback_contributor"
FEEDBACK_TITLE_NAME = "共创者"
MAINTENANCE_EVENT_KINDS = frozenset(("account_registration", "points_ledger"))
MAINTENANCE_OUTCOMES = frozenset(("success", "client_error", "server_error"))
MAINTENANCE_STAGES = frozenset((
    "dikgames_incremental", "external_sources", "ai_review",
    "publish_snapshot", "catalogue_backup", "cloud_backup"))
MAINTENANCE_STATUSES = frozenset(("ok", "warning", "error", "unknown"))
MAINTENANCE_SUMMARY_CODES = frozenset((
    "healthy", "no_recent_activity", "no_history", "run_active",
    "sync_overdue", "partial_failure", "stage_failed", "stage_skipped",
    "service_unavailable", "database_unavailable", "operation_errors",
    "disk_low", "backup_missing", "backup_stale", "status_read_failed"))
MAINTENANCE_RETENTION_SECONDS = 30 * 86400
MAINTENANCE_EVENT_LIMIT = 5000
MAINTENANCE_RUN_LIMIT = 40
_UNSET = object()


class CloudError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class OwnerAccountAlreadyBound(CloudError):
    """Conflict raised when the single owner account slot is already filled."""

    def __init__(self, account_id):
        super().__init__("owner account already bound", 409)
        self.account_id = account_id


class MaintenanceStore:
    """Bounded operational status storage that never records account identity."""

    def __init__(self, path, now=None):
        self.path = os.path.abspath(path)
        self.now = now or _utc
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS maintenance_events (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN
                        ('account_registration','points_ledger')),
                    outcome TEXT NOT NULL CHECK(outcome IN
                        ('success','client_error','server_error')),
                    ts INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_maintenance_events_kind_ts
                    ON maintenance_events(kind,ts);
                CREATE TABLE IF NOT EXISTS maintenance_sync_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    status TEXT NOT NULL CHECK(status IN
                        ('ok','warning','error','unknown')),
                    exit_code INTEGER,
                    summary_code TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_maintenance_runs_started
                    ON maintenance_sync_runs(started_at DESC);
                CREATE TABLE IF NOT EXISTS maintenance_sync_stages (
                    run_id TEXT NOT NULL,
                    stage_id TEXT NOT NULL CHECK(stage_id IN
                        ('dikgames_incremental','external_sources','ai_review',
                         'publish_snapshot','catalogue_backup','cloud_backup')),
                    status TEXT NOT NULL CHECK(status IN
                        ('ok','warning','error','unknown')),
                    started_at INTEGER,
                    finished_at INTEGER,
                    exit_code INTEGER,
                    summary_code TEXT NOT NULL,
                    PRIMARY KEY(run_id,stage_id));
                CREATE INDEX IF NOT EXISTS idx_maintenance_stages_run
                    ON maintenance_sync_stages(run_id,started_at);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def record_event(self, kind, outcome, ts=None):
        if kind not in MAINTENANCE_EVENT_KINDS or outcome not in MAINTENANCE_OUTCOMES:
            raise ValueError("invalid maintenance event")
        now = int(self.now() if ts is None else ts)
        with self._db() as db:
            db.execute("INSERT INTO maintenance_events(kind,outcome,ts) VALUES (?,?,?)",
                       (kind, outcome, now))
            db.execute("DELETE FROM maintenance_events WHERE ts<?",
                       (now - MAINTENANCE_RETENTION_SECONDS,))
            db.execute(
                "DELETE FROM maintenance_events WHERE id NOT IN "
                "(SELECT id FROM maintenance_events ORDER BY ts DESC,id DESC LIMIT ?)",
                (MAINTENANCE_EVENT_LIMIT,))

    def begin_sync_run(self, run_id, started_at=None):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 80:
            raise ValueError("invalid maintenance run id")
        now = int(self.now() if started_at is None else started_at)
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO maintenance_sync_runs "
                "(run_id,started_at,finished_at,status,exit_code,summary_code) "
                "VALUES (?,?,NULL,'warning',NULL,'run_active')", (run_id, now))
            self._prune_sync(db)

    def record_sync_stage(self, run_id, stage_id, status, started_at,
                          finished_at=None, exit_code=None,
                          summary_code="healthy"):
        if (not isinstance(run_id, str) or not run_id or len(run_id) > 80
                or stage_id not in MAINTENANCE_STAGES
                or status not in MAINTENANCE_STATUSES
                or summary_code not in MAINTENANCE_SUMMARY_CODES):
            raise ValueError("invalid maintenance stage")
        started = None if started_at in (None, "") else int(started_at)
        finished = None if finished_at in (None, "") else int(finished_at)
        code = None if exit_code in (None, "") else int(exit_code)
        with self._db() as db:
            db.execute(
                "INSERT INTO maintenance_sync_stages "
                "(run_id,stage_id,status,started_at,finished_at,exit_code,summary_code) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id,stage_id) DO UPDATE SET "
                "status=excluded.status,started_at=excluded.started_at,"
                "finished_at=excluded.finished_at,exit_code=excluded.exit_code,"
                "summary_code=excluded.summary_code",
                (run_id, stage_id, status, started, finished, code, summary_code))
            self._prune_sync(db)

    def finish_sync_run(self, run_id, status, finished_at=None, exit_code=0,
                        summary_code="healthy"):
        if (status not in MAINTENANCE_STATUSES
                or summary_code not in MAINTENANCE_SUMMARY_CODES):
            raise ValueError("invalid maintenance run result")
        finished = int(self.now() if finished_at is None else finished_at)
        with self._db() as db:
            db.execute(
                "UPDATE maintenance_sync_runs SET finished_at=?,status=?,"
                "exit_code=?,summary_code=? WHERE run_id=?",
                (finished, status, int(exit_code), summary_code, run_id))
            self._prune_sync(db)

    @staticmethod
    def _prune_sync(db):
        db.execute(
            "DELETE FROM maintenance_sync_runs WHERE run_id NOT IN "
            "(SELECT run_id FROM maintenance_sync_runs "
            "ORDER BY started_at DESC LIMIT ?)", (MAINTENANCE_RUN_LIMIT,))
        db.execute(
            "DELETE FROM maintenance_sync_stages WHERE run_id NOT IN "
            "(SELECT run_id FROM maintenance_sync_runs)")

    def snapshot(self, now=None, window_seconds=86400):
        """Return aggregate counters and only the newest structured sync run."""
        now = int(self.now() if now is None else now)
        window_start = now - max(1, int(window_seconds))
        events = {}
        with self._db() as db:
            for kind in MAINTENANCE_EVENT_KINDS:
                row = db.execute(
                    "SELECT COUNT(*) AS total,"
                    "COALESCE(SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END),0) AS success_count,"
                    "COALESCE(SUM(CASE WHEN outcome='client_error' THEN 1 ELSE 0 END),0) AS client_error_count,"
                    "COALESCE(SUM(CASE WHEN outcome='server_error' THEN 1 ELSE 0 END),0) AS server_error_count,"
                    "MAX(CASE WHEN outcome='success' THEN ts END) AS last_success_at,"
                    "MAX(CASE WHEN outcome='server_error' THEN ts END) AS last_failure_at "
                    "FROM maintenance_events WHERE kind=? AND ts>=?",
                    (kind, window_start)).fetchone()
                events[kind] = dict(row)
            runs = db.execute(
                "SELECT run_id,started_at,finished_at,status,exit_code,summary_code "
                "FROM maintenance_sync_runs ORDER BY started_at DESC LIMIT 40").fetchall()
            stages = (db.execute(
                "SELECT stage_id,status,started_at,finished_at,exit_code,summary_code "
                "FROM maintenance_sync_stages WHERE run_id=?",
                (runs[0]["run_id"],)).fetchall() if runs else [])
        return {"events": events, "runs": [dict(row) for row in runs],
                "stages": [dict(row) for row in stages]}


def _maintenance_cli(argv):
    """CLI called by run_sync.sh to persist stage timestamps and exit codes."""
    if not argv or argv[0] not in ("begin", "stage", "finish"):
        return None
    try:
        action = argv[0]
        if action == "begin" and len(argv) == 4:
            _path, run_id, started = argv[1:]
            MaintenanceStore(_path).begin_sync_run(run_id, int(started))
        elif action == "stage" and len(argv) == 9:
            (_path, run_id, stage_id, status, started, finished,
             exit_code, summary_code) = argv[1:]
            MaintenanceStore(_path).record_sync_stage(
                run_id, stage_id, status, started, finished, exit_code,
                summary_code)
        elif action == "finish" and len(argv) == 7:
            _path, run_id, status, finished, exit_code, summary_code = argv[1:]
            MaintenanceStore(_path).finish_sync_run(
                run_id, status, finished, exit_code, summary_code)
        else:
            return 2
        return 0
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return 1


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _secret(prefix):
    return prefix + secrets.token_urlsafe(32)


def _utc():
    return int(time.time())


class CloudStore:
    def __init__(self, path, min_free_bytes=MIN_FREE_BYTES, now=None):
        self.path = path
        self.min_free_bytes = min_free_bytes
        self.now = now or _utc
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=1000")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, login_hash TEXT NOT NULL,
                    recovery_hash TEXT NOT NULL, created INTEGER NOT NULL,
                    banned INTEGER NOT NULL DEFAULT 0,
                    equipped_title TEXT NOT NULL DEFAULT 'normal_user',
                    equipped_cosmetic TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    referral_eligible INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS referral_codes (
                    account_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    created INTEGER NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE TABLE IF NOT EXISTS referrals (
                    invitee_account_id TEXT PRIMARY KEY,
                    inviter_account_id TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    FOREIGN KEY(invitee_account_id) REFERENCES accounts(id),
                    FOREIGN KEY(inviter_account_id) REFERENCES accounts(id));
                CREATE INDEX IF NOT EXISTS idx_referrals_inviter
                    ON referrals(inviter_account_id,created);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, created INTEGER NOT NULL,
                    last_used INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    label TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE INDEX IF NOT EXISTS idx_devices_account ON devices(account_id);
                CREATE TABLE IF NOT EXISTS business_platforms (
                    origin TEXT PRIMARY KEY, platform TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL,
                    delta INTEGER NOT NULL, reason TEXT NOT NULL,
                    claim_key TEXT NOT NULL UNIQUE, ts INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger(account_id,ts);
                CREATE INDEX IF NOT EXISTS idx_ledger_account_reason_id
                    ON ledger(account_id,reason,id);
                CREATE INDEX IF NOT EXISTS idx_ledger_reason_ts_account
                    ON ledger(reason,ts,account_id);
                CREATE TABLE IF NOT EXISTS owned_titles (
                    account_id TEXT NOT NULL,title_id TEXT NOT NULL,
                    source TEXT NOT NULL,ts INTEGER NOT NULL,
                    PRIMARY KEY(account_id,title_id));
                CREATE TABLE IF NOT EXISTS account_appearances (
                    account_id TEXT NOT NULL,
                    slot TEXT NOT NULL CHECK(slot IN ('comment_frame','avatar_frame')),
                    item_id TEXT NOT NULL,
                    PRIMARY KEY(account_id,slot),
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE TABLE IF NOT EXISTS lottery_draws (
                    id INTEGER PRIMARY KEY,account_id TEXT NOT NULL,
                    day TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,
                    ts INTEGER NOT NULL,request_id TEXT,result_json TEXT);
                CREATE INDEX IF NOT EXISTS idx_lottery_account_day ON lottery_draws(account_id,day);
                CREATE INDEX IF NOT EXISTS idx_lottery_ts_account
                    ON lottery_draws(ts,account_id);
                CREATE TABLE IF NOT EXISTS personal_game_comments (
                    id TEXT PRIMARY KEY,account_id TEXT NOT NULL,
                    game TEXT NOT NULL,content TEXT NOT NULL,ts INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_personal_comment_account
                    ON personal_game_comments(account_id,ts DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_personal_comment_game
                    ON personal_game_comments(account_id,game,ts DESC,id DESC);
                CREATE TABLE IF NOT EXISTS web_comment_operations (
                    account_id TEXT NOT NULL,kind TEXT NOT NULL,
                    operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
                    target_id TEXT NOT NULL,ts INTEGER NOT NULL,
                    PRIMARY KEY(account_id,kind,operation_id));
                CREATE INDEX IF NOT EXISTS idx_web_comment_operation_time
                    ON web_comment_operations(account_id,kind,ts);
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    game TEXT NOT NULL, content TEXT NOT NULL,
                    nickname TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL,
                    rewarded INTEGER NOT NULL DEFAULT 0,
                    approved_ts INTEGER);
                CREATE INDEX IF NOT EXISTS idx_comments_game ON comments(game,status,ts);
                CREATE INDEX IF NOT EXISTS idx_comments_account ON comments(account_id,ts);
                CREATE TABLE IF NOT EXISTS comment_replies (
                    id TEXT PRIMARY KEY,root_id TEXT NOT NULL,account_id TEXT NOT NULL,
                    content TEXT NOT NULL,reply_to_id TEXT,status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,
                    approved_ts INTEGER,operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
                    moderation_source TEXT NOT NULL DEFAULT 'automatic',rule_version TEXT NOT NULL DEFAULT '',
                    UNIQUE(account_id,operation_id));
                CREATE INDEX IF NOT EXISTS idx_reply_root ON comment_replies(root_id,status,created,id);
                CREATE INDEX IF NOT EXISTS idx_reply_account ON comment_replies(account_id,created);
                CREATE TABLE IF NOT EXISTS comment_reply_reports (
                    account_id TEXT NOT NULL,reply_id TEXT NOT NULL,created INTEGER NOT NULL,
                    PRIMARY KEY(account_id,reply_id));
                CREATE TABLE IF NOT EXISTS comment_reply_report_baselines (
                    account_id TEXT NOT NULL,reply_id TEXT NOT NULL,
                    PRIMARY KEY(account_id,reply_id));
                CREATE TABLE IF NOT EXISTS comment_reply_decisions (
                    id INTEGER PRIMARY KEY,reply_id TEXT NOT NULL,decision TEXT NOT NULL,
                    reason TEXT NOT NULL,created INTEGER NOT NULL,source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_notifications (
                    id TEXT PRIMARY KEY,account_id TEXT NOT NULL,kind TEXT NOT NULL,
                    root_id TEXT NOT NULL,reply_id TEXT NOT NULL,created INTEGER NOT NULL,
                    read_at INTEGER,UNIQUE(account_id,reply_id));
                CREATE INDEX IF NOT EXISTS idx_notifications_account ON account_notifications(account_id,created DESC,id);
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    category TEXT NOT NULL CHECK(category IN
                        ('bug','suggestion','data_correction','other')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN
                        ('pending','accepted','rejected','duplicate')),
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    reward_points INTEGER NOT NULL DEFAULT 0,
                    reward_title TEXT NOT NULL DEFAULT '',
                    admin_note TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE INDEX IF NOT EXISTS idx_feedback_account_created
                    ON feedback(account_id,created DESC);
                CREATE INDEX IF NOT EXISTS idx_feedback_status_created
                    ON feedback(status,created DESC);
                CREATE INDEX IF NOT EXISTS idx_feedback_category_created
                    ON feedback(category,created DESC);
                -- Private source-review state. The public catalogue is built
                -- from a separate database and never receives these tables.
                CREATE TABLE IF NOT EXISTS source_review_ai_suggestions (
                    source_id TEXT NOT NULL CHECK(source_id IN ('f95zone','zyoudao')),
                    external_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK(verdict IN ('same','distinct','uncertain')),
                    record_kind TEXT NOT NULL DEFAULT 'game'
                        CHECK(record_kind IN ('game','non_game','insufficient')),
                    rationale TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    updated INTEGER NOT NULL,
                    PRIMARY KEY(source_id,external_id,content_hash));
                CREATE TABLE IF NOT EXISTS source_review_decisions (
                    source_id TEXT NOT NULL CHECK(source_id IN ('f95zone','zyoudao')),
                    external_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK(decision IN ('same','distinct','skip')),
                    canonical_game_id INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    decided INTEGER NOT NULL,
                    PRIMARY KEY(source_id,external_id,content_hash));
                CREATE INDEX IF NOT EXISTS idx_source_review_decisions_decided
                    ON source_review_decisions(decided);
                CREATE TABLE IF NOT EXISTS votes (
                    account_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                    value INTEGER NOT NULL, ts INTEGER NOT NULL,
                    PRIMARY KEY(account_id,comment_id));
                CREATE INDEX IF NOT EXISTS idx_votes_comment ON votes(comment_id,value);
                CREATE TABLE IF NOT EXISTS reports (
                    account_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                    ts INTEGER NOT NULL, PRIMARY KEY(account_id,comment_id));
                CREATE INDEX IF NOT EXISTS idx_reports_comment ON reports(comment_id);
                CREATE TABLE IF NOT EXISTS comment_decisions (
                    id INTEGER PRIMARY KEY,comment_id TEXT NOT NULL,
                    decision TEXT NOT NULL,reason TEXT NOT NULL,ts INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_comment_decisions_comment ON comment_decisions(comment_id,ts);
                CREATE TABLE IF NOT EXISTS comment_feedback_baselines (
                    comment_id TEXT NOT NULL, kind TEXT NOT NULL, account_id TEXT NOT NULL,
                    PRIMARY KEY(comment_id,kind,account_id));
                CREATE TABLE IF NOT EXISTS migration_requests (
                    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL UNIQUE,
                    claimed_points INTEGER NOT NULL, titles TEXT NOT NULL,
                    source_digest TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending', ts INTEGER NOT NULL,
                    decided_ts INTEGER,approved_points INTEGER,
                    approved_titles TEXT,reason TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS legacy_migrations (
                    account_id TEXT PRIMARY KEY, source_digest TEXT NOT NULL UNIQUE,
                    points INTEGER NOT NULL, titles TEXT NOT NULL, ts INTEGER NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE TABLE IF NOT EXISTS account_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_key_hash TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    contact TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected')),
                    created INTEGER NOT NULL,
                    decided INTEGER,
                    decision_reason TEXT NOT NULL DEFAULT '',
                    account_id TEXT,
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE INDEX IF NOT EXISTS idx_account_applications_status_created
                    ON account_applications(status,created DESC,id DESC);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_events (
                    identity TEXT NOT NULL, ts INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_auth_events ON auth_events(identity,ts);
                CREATE INDEX IF NOT EXISTS idx_auth_events_age ON auth_events(ts);
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY, created INTEGER NOT NULL,
                    expires INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry
                    ON admin_sessions(expires,created);
                -- There is exactly one server-owned ordinary account. The
                -- slot primary key and unique account id prevent rebinding or
                -- accidentally attaching multiple accounts to the owner.
                CREATE TABLE IF NOT EXISTS owner_account_binding (
                    slot TEXT PRIMARY KEY CHECK(slot='owner'),
                    account_id TEXT NOT NULL UNIQUE,
                    operation_id TEXT NOT NULL UNIQUE,
                    created INTEGER NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE TRIGGER IF NOT EXISTS owner_account_binding_no_update
                    BEFORE UPDATE ON owner_account_binding BEGIN
                    SELECT RAISE(ABORT,'owner account binding is permanent'); END;
                CREATE TRIGGER IF NOT EXISTS owner_account_binding_no_delete
                    BEFORE DELETE ON owner_account_binding BEGIN
                    SELECT RAISE(ABORT,'owner account binding is permanent'); END;
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    device_id TEXT NOT NULL, created INTEGER NOT NULL,
                    last_used INTEGER NOT NULL, expires INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry
                    ON web_sessions(expires,created);
                CREATE INDEX IF NOT EXISTS idx_web_sessions_account
                    ON web_sessions(account_id);
                -- Registration operation IDs are high-entropy browser retry
                -- keys. Only their hashes and account association are stored.
                CREATE TABLE IF NOT EXISTS web_registration_requests (
                    operation_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL UNIQUE,
                    created INTEGER NOT NULL,
                    credentials_acknowledged INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(account_id) REFERENCES accounts(id));
                CREATE INDEX IF NOT EXISTS idx_web_registration_account
                    ON web_registration_requests(account_id,created);
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS shop_availability (
                    item_id TEXT PRIMARY KEY, selling INTEGER NOT NULL DEFAULT 1
                        CHECK(selling IN (0,1)),
                    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
                    reason TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_admin_audit_ts
                    ON admin_audit(ts DESC,id DESC);
                CREATE TRIGGER IF NOT EXISTS admin_audit_no_update
                    BEFORE UPDATE ON admin_audit BEGIN
                    SELECT RAISE(ABORT,'admin audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS admin_audit_no_delete
                    BEFORE DELETE ON admin_audit BEGIN
                    SELECT RAISE(ABORT,'admin audit is append-only'); END;
                CREATE TABLE IF NOT EXISTS admin_push_subscriptions (
                    endpoint_hash TEXT PRIMARY KEY, endpoint TEXT NOT NULL,
                    p256dh TEXT NOT NULL, auth TEXT NOT NULL,
                    created INTEGER NOT NULL, updated INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_admin_push_created
                    ON admin_push_subscriptions(created);
                CREATE TABLE IF NOT EXISTS quest_ratings (
                    account_id TEXT NOT NULL, day TEXT NOT NULL,
                    game_id INTEGER NOT NULL, rating INTEGER NOT NULL,
                    ts INTEGER NOT NULL, PRIMARY KEY(account_id,day));
                CREATE INDEX IF NOT EXISTS idx_quest_ratings_day
                    ON quest_ratings(day,account_id);
                CREATE TABLE IF NOT EXISTS quest_claim_periods (
                    account_id TEXT NOT NULL,
                    quest_id TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    source_claim_key TEXT NOT NULL UNIQUE,
                    source_ts INTEGER NOT NULL,
                    PRIMARY KEY(account_id,quest_id,period_key));
            """)
            # Serialize schema/data backfills across server workers.  The
            # migration marker and all quest-period rows are committed by the
            # surrounding _db context as one idempotent initialization step.
            db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(accounts)")}
            if "equipped_title" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN equipped_title TEXT NOT NULL DEFAULT 'normal_user'")
            if "nickname" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN nickname TEXT NOT NULL DEFAULT ''")
            if "equipped_cosmetic" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN equipped_cosmetic TEXT NOT NULL DEFAULT ''")
            if "profile_message" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN profile_message TEXT NOT NULL DEFAULT ''")
            if "profile_visible" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN profile_visible INTEGER NOT NULL DEFAULT 1")
            if "public_profile_id" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN public_profile_id TEXT")
            # Independent opaque public handles never encode login credentials.
            # The transaction serializes startup backfills; the trigger covers
            # every account creation path, including owner/application accounts.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_public_profile_id "
                       "ON accounts(public_profile_id)")
            db.execute("UPDATE accounts SET public_profile_id='pp_' || lower(hex(randomblob(16))) "
                       "WHERE public_profile_id IS NULL OR public_profile_id=''")
            db.execute("""CREATE TRIGGER IF NOT EXISTS accounts_public_profile_id
                AFTER INSERT ON accounts
                WHEN NEW.public_profile_id IS NULL OR NEW.public_profile_id=''
                BEGIN UPDATE accounts
                    SET public_profile_id='pp_' || lower(hex(randomblob(16)))
                    WHERE id=NEW.id; END""")
            if "referral_eligible" not in columns:
                # Existing accounts are never retroactively eligible to redeem.
                db.execute("ALTER TABLE accounts ADD COLUMN referral_eligible INTEGER NOT NULL DEFAULT 0")
            comment_columns = {r[1] for r in db.execute("PRAGMA table_info(comments)")}
            if "approved_ts" not in comment_columns:
                db.execute("ALTER TABLE comments ADD COLUMN approved_ts INTEGER")
            for name, default in (("original_reason", ""), ("moderation_source", "unknown"),
                                  ("rule_version", "")):
                if name not in comment_columns:
                    db.execute("ALTER TABLE comments ADD COLUMN " + name +
                               " TEXT NOT NULL DEFAULT '" + default + "'")
            decision_columns = {r[1] for r in db.execute("PRAGMA table_info(comment_decisions)")}
            for name, default in (("source", "unknown"), ("rule_version", "")):
                if name not in decision_columns:
                    db.execute("ALTER TABLE comment_decisions ADD COLUMN " + name +
                               " TEXT NOT NULL DEFAULT '" + default + "'")
            for statement in ("""
                CREATE TRIGGER IF NOT EXISTS comment_decisions_no_update
                BEFORE UPDATE ON comment_decisions BEGIN
                    SELECT RAISE(ABORT,'comment decisions are append-only'); END""", """
                CREATE TRIGGER IF NOT EXISTS comment_decisions_no_delete
                BEFORE DELETE ON comment_decisions BEGIN
                    SELECT RAISE(ABORT,'comment decisions are append-only'); END""", """
                CREATE TRIGGER IF NOT EXISTS comment_original_reason_immutable
                BEFORE UPDATE OF original_reason ON comments
                WHEN NEW.original_reason <> OLD.original_reason BEGIN
                    SELECT RAISE(ABORT,'original comment reason is immutable'); END"""):
                db.execute(statement)
            feedback_columns = {r[1] for r in db.execute("PRAGMA table_info(feedback)")}
            if "reward_title" not in feedback_columns:
                db.execute("ALTER TABLE feedback ADD COLUMN reward_title TEXT NOT NULL DEFAULT ''")
            lottery_columns = {r[1] for r in db.execute("PRAGMA table_info(lottery_draws)")}
            if "request_id" not in lottery_columns:
                db.execute("ALTER TABLE lottery_draws ADD COLUMN request_id TEXT")
            if "result_json" not in lottery_columns:
                db.execute("ALTER TABLE lottery_draws ADD COLUMN result_json TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_lottery_request "
                "ON lottery_draws(account_id,request_id) "
                "WHERE request_id IS NOT NULL AND request_id<>''")
            # Preserve namecard frames equipped by clients that predate the
            # multi-slot appearance table. INSERT OR IGNORE makes this safe on
            # every server startup and never overwrites a newer slot choice.
            db.execute(
                "INSERT OR IGNORE INTO account_appearances(account_id,slot,item_id) "
                "SELECT id,'comment_frame',equipped_cosmetic FROM accounts "
                "WHERE equipped_cosmetic<>''")
            # Backfill the feedback-contributor title for existing accounts
            # whose feedback was already accepted before the threshold changed
            # to one.  This is intentionally title-only: it never grants
            # feedback points, and the owned_titles primary key makes repeated
            # server starts safe.  Pending, rejected, and duplicate feedback
            # do not qualify; the developer account is outside this reward.
            db.execute(
                "INSERT OR IGNORE INTO owned_titles(account_id,title_id,source,ts) "
                "SELECT account_id,?,'feedback',? FROM feedback "
                "WHERE status='accepted' AND account_id<>? "
                "GROUP BY account_id",
                (FEEDBACK_TITLE_ID, self.now(), DEVELOPER_ACCOUNT_ID))
            if not db.execute(
                    "SELECT 1 FROM metadata "
                    "WHERE key='quest_claim_periods_v2_backfill'").fetchone():
                self._migrate_legacy_quest_claims(db)
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES (?,?)",
                    ("quest_claim_periods_v2_backfill", "complete"))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _writable(self):
        try:
            if shutil.disk_usage(os.path.dirname(self.path)).free < self.min_free_bytes:
                raise CloudError("storage unavailable", 507)
        except OSError:
            raise CloudError("storage unavailable", 507)

    @staticmethod
    def _append_admin_audit(db, action, target="", details=None, now=None):
        details = details if isinstance(details, dict) else {}
        try:
            encoded = json.dumps(details, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise CloudError("invalid audit details")
        if (not isinstance(action, str) or not action or len(action) > 64
                or not isinstance(target, str) or len(target) > 200
                or len(encoded) > 2048):
            raise CloudError("invalid audit entry")
        db.execute("INSERT INTO admin_audit(ts,action,target,details) VALUES (?,?,?,?)",
                   (int(time.time() if now is None else now), action, target, encoded))

    def append_admin_audit(self, action, target="", details=None):
        """Append a small, secret-free owner action record."""
        with self._db() as db:
            self._append_admin_audit(db, action, target, details, self.now())

    def admin_audit(self, limit=100):
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad limit")
        with self._db() as db:
            rows = db.execute(
                "SELECT id,ts,action,target,details FROM admin_audit "
                "ORDER BY ts DESC,id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            try:
                details = json.loads(row["details"])
            except (TypeError, ValueError):
                details = {}
            result.append({"id": row["id"], "ts": row["ts"],
                           "action": row["action"], "target": row["target"],
                           "details": details})
        return {"entries": result, "limit": limit}

    def create_admin_session(self):
        """Mint an opaque owner session while persisting only its SHA-256 digest."""
        self._writable()
        token = secrets.token_urlsafe(32)
        now = int(self.now())
        expires = now + ADMIN_SESSION_SECONDS
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM admin_sessions WHERE token_hash IN "
                "(SELECT token_hash FROM admin_sessions WHERE expires<=? "
                "ORDER BY expires LIMIT 500)", (now,))
            count = db.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0]
            excess = count - ADMIN_SESSION_MAX_ROWS + 1
            if excess > 0:
                db.execute(
                    "DELETE FROM admin_sessions WHERE token_hash IN "
                    "(SELECT token_hash FROM admin_sessions "
                    "ORDER BY created,token_hash LIMIT ?)", (excess,))
            db.execute("INSERT INTO admin_sessions(token_hash,created,expires) "
                       "VALUES (?,?,?)", (_hash(token), now, expires))
            self._append_admin_audit(
                db, "admin_session_created", "", {}, now)
        return token, expires

    def validate_admin_session(self, token):
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        now = int(self.now())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM admin_sessions WHERE token_hash IN "
                "(SELECT token_hash FROM admin_sessions WHERE expires<=? "
                "ORDER BY expires LIMIT 500)", (now,))
            row = db.execute(
                "SELECT expires FROM admin_sessions WHERE token_hash=? AND expires>?",
                (_hash(token), now)).fetchone()
            return int(row["expires"]) if row else None

    def revoke_admin_session(self, token):
        if not isinstance(token, str) or not token or len(token) > 128:
            return False
        now = int(self.now())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute(
                "DELETE FROM admin_sessions WHERE token_hash=?", (_hash(token),))
            if removed.rowcount:
                self._append_admin_audit(
                    db, "admin_session_revoked", "", {}, now)
            return bool(removed.rowcount)

    def create_web_session(self, aid, key, *, recovery=None, label="",
                           identity=""):
        """Verify credentials and issue only a browser cookie session.

        Browser authentication keeps the recovery-code requirement and login
        rate limit, but does not create a desktop device or Bearer token.
        ``label`` remains accepted for route compatibility and is not persisted.
        """
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer must authenticate with the developer key", 401)
        self._auth_limit(identity, "login", 30, 3600)
        if not isinstance(key, str):
            raise CloudError("unauthorized", 401)
        if not aid and key.startswith("sk_u_"):
            pieces = key.split("_", 3)
            if len(pieces) == 4:
                aid = "u_" + pieces[2]
        if not isinstance(aid, str):
            raise CloudError("unauthorized", 401)
        with self._db() as db:
            account = self._account(db, aid)
            if not hmac.compare_digest(_hash(key), account["login_hash"]):
                raise CloudError("unauthorized", 401)
            if not isinstance(recovery, str) or not hmac.compare_digest(
                    _hash(recovery), account["recovery_hash"]):
                raise CloudError("recovery required", 401)
        token, expires = self._issue_web_session(aid)
        return token, expires, aid

    def _issue_web_session(self, aid, device_id=None):
        self._writable()
        token = secrets.token_urlsafe(32)
        now = int(self.now())
        expires = now + WEB_SESSION_SECONDS
        # A browser-only registration has no desktop device and must never mint
        # a device token just to satisfy the cookie-session table.
        device_id = device_id or ""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM web_sessions WHERE token_hash IN "
                "(SELECT token_hash FROM web_sessions WHERE expires<=? "
                "ORDER BY expires LIMIT 500)", (now,))
            count = db.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
            excess = count - WEB_SESSION_MAX_ROWS + 1
            if excess > 0:
                db.execute(
                    "DELETE FROM web_sessions WHERE token_hash IN "
                    "(SELECT token_hash FROM web_sessions "
                    "ORDER BY created,token_hash LIMIT ?)", (excess,))
            db.execute("INSERT INTO web_sessions(token_hash,account_id,device_id,"
                       "created,last_used,expires) VALUES (?,?,?,?,?,?)",
                       (_hash(token), aid, device_id, now, now, expires))
        return token, expires

    def validate_web_session(self, token):
        """Resolve a browser cookie to its account, or None when unusable.

        Browser-only sessions are checked against account status and their own
        idle expiry. Older sessions that have a device_id also die when that
        device is revoked.
        """
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        now = int(self.now())
        digest = _hash(token)

        def lookup(db):
            row = db.execute(
                "SELECT * FROM web_sessions WHERE token_hash=? AND expires>?",
                (digest, now)).fetchone()
            if not row:
                return None
            if row["device_id"]:
                device = db.execute(
                    "SELECT revoked FROM devices WHERE id=? AND account_id=?",
                    (row["device_id"], row["account_id"])).fetchone()
                if not device or device["revoked"]:
                    return None
            if now - row["last_used"] >= SESSION_IDLE_SECONDS:
                return None
            try:
                self._account(db, row["account_id"])
            except CloudError:
                return None
            return row

        # Most browser reads need no writer lock. Revocation and account status
        # are still read on every request, never cached as an authorization.
        with self._db() as db:
            row = lookup(db)
        if row is None:
            return None
        if now - row["last_used"] >= WEB_SESSION_TOUCH_SECONDS:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = lookup(db)
                if row is None:
                    return None
                if now - row["last_used"] >= WEB_SESSION_TOUCH_SECONDS:
                    db.execute("UPDATE web_sessions SET last_used=? WHERE token_hash=?",
                               (now, digest))
                    if row["device_id"]:
                        db.execute("UPDATE devices SET last_used=? WHERE id=?",
                                   (now, row["device_id"]))
                    db.execute(
                        "DELETE FROM web_sessions WHERE token_hash IN "
                        "(SELECT token_hash FROM web_sessions WHERE expires<=? "
                        "ORDER BY expires LIMIT 500)", (now,))
        return {"account_id": row["account_id"],
                "device_id": row["device_id"],
                "expires": int(row["expires"])}

    def revoke_web_session(self, token):
        if not isinstance(token, str) or not token or len(token) > 128:
            return False
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute("DELETE FROM web_sessions WHERE token_hash=?",
                                 (_hash(token),))
            return bool(removed.rowcount)

    def save_admin_push_subscription(self, endpoint, p256dh, auth):
        self._writable()
        if (not all(isinstance(value, str) for value in (endpoint, p256dh, auth))
                or not endpoint or len(endpoint) > 2048
                or not p256dh or len(p256dh) > 256
                or not auth or len(auth) > 256):
            raise CloudError("invalid subscription")
        now = int(self.now())
        endpoint_hash = _hash(endpoint)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO admin_push_subscriptions "
                "(endpoint_hash,endpoint,p256dh,auth,created,updated) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint_hash) DO UPDATE SET "
                "endpoint=excluded.endpoint,p256dh=excluded.p256dh,"
                "auth=excluded.auth,updated=excluded.updated",
                (endpoint_hash, endpoint, p256dh, auth, now, now))
            db.execute(
                "DELETE FROM admin_push_subscriptions WHERE endpoint_hash IN "
                "(SELECT endpoint_hash FROM admin_push_subscriptions "
                "ORDER BY updated DESC,created DESC LIMIT -1 OFFSET ?)",
                (ADMIN_PUSH_SUBSCRIPTION_MAX_ROWS,))
        return {"ok": True}

    def admin_push_subscriptions(self):
        with self._db() as db:
            rows = db.execute(
                "SELECT endpoint,p256dh,auth FROM admin_push_subscriptions "
                "ORDER BY updated DESC,created DESC LIMIT ?",
                (ADMIN_PUSH_SUBSCRIPTION_MAX_ROWS,)).fetchall()
        return [{"endpoint": row["endpoint"],
                 "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
                for row in rows]

    def delete_admin_push_subscription(self, endpoint):
        if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
            return False
        with self._db() as db:
            return bool(db.execute(
                "DELETE FROM admin_push_subscriptions WHERE endpoint_hash=?",
                (_hash(endpoint),)).rowcount)

    def _new_device(self, db, aid, label, now):
        token = _secret("sd_")
        did = uuid.uuid4().hex
        db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?)",
                   (did, aid, _hash(token), now, now, 0, str(label or "")[:60]))
        return did, token

    def _auth_limit(self, identity, kind, maximum, window):
        if not identity:
            return
        if identity == "proxy_unknown":
            maximum = max(maximum, 1000 if kind in ("create", "application", "legacy_comment") else 10000)
        now = self.now()
        ident = kind + ":" + _hash(str(identity))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_events WHERE ts<?", (now - 86400,))
            count = db.execute("SELECT COUNT(*) FROM auth_events WHERE identity=? AND ts>?",
                               (ident, now - window)).fetchone()[0]
            if count >= maximum:
                raise CloudError("rate limited", 429)
            db.execute("INSERT INTO auth_events VALUES (?,?)", (ident, now))

    def create(self, label="", identity=""):
        self._writable()
        self._auth_limit(identity, "create", 10, 86400)
        now = self.now()
        aid = "u_" + uuid.uuid4().hex
        key, recovery = _secret("sk_" + aid + "_"), _secret("sr_")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO accounts(id,login_hash,recovery_hash,created,referral_eligible) VALUES (?,?,?,?,1)",
                       (aid, _hash(key), _hash(recovery), now))
            self._ensure_referral_code(db, aid)
            did, token = self._new_device(db, aid, label, now)
        return {"account_id": aid, "login_key": key, "recovery_code": recovery,
                "device_id": did, "device_token": token}

    @staticmethod
    def _owner_operation_id(value):
        if (not isinstance(value, str) or len(value) != 32
                or any(ch not in "0123456789abcdef" for ch in value)):
            raise CloudError("bad operation_id", 400)
        return value

    @staticmethod
    def _owner_device_label(value):
        if (not isinstance(value, str) or len(value) > 60
                or any(ord(ch) < 32 for ch in value)):
            raise CloudError("bad device_label", 400)
        return value.strip()

    def create_owner_account(self, operation_id, device_label="黄油之王 管理员账号"):
        """Create and permanently bind the one ordinary account for the owner.

        The generated credentials are returned once. Only their hashes and the
        account binding are persisted. The owner key remains the sole authority
        for admin APIs; this account's normal Bearer token is not an admin key.
        """
        operation_id = self._owner_operation_id(operation_id)
        device_label = self._owner_device_label(device_label)
        # Once the slot is filled, duplicate calls must always report the
        # binding conflict, even if storage is now read-only or the creation
        # rate limit has been reached. The write transaction below repeats
        # this check to close the race between parallel first-time requests.
        with self._db() as db:
            existing = db.execute(
                "SELECT account_id FROM owner_account_binding WHERE slot='owner'"
            ).fetchone()
        if existing:
            raise OwnerAccountAlreadyBound(existing["account_id"])
        self._writable()
        self._auth_limit("owner-account", "owner_account_create", 3, 86400)
        now = self.now()
        aid = "u_" + uuid.uuid4().hex
        login_key, recovery_code = _secret("sk_" + aid + "_"), _secret("sr_")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT account_id FROM owner_account_binding WHERE slot='owner'"
            ).fetchone()
            if existing:
                raise OwnerAccountAlreadyBound(existing["account_id"])
            db.execute(
                "INSERT INTO accounts(id,login_hash,recovery_hash,created,"
                "referral_eligible) VALUES (?,?,?,?,1)",
                (aid, _hash(login_key), _hash(recovery_code), now))
            self._ensure_referral_code(db, aid)
            device_id, device_token = self._new_device(
                db, aid, device_label, now)
            db.execute(
                "INSERT INTO owner_account_binding(slot,account_id,operation_id,created) "
                "VALUES ('owner',?,?,?)", (aid, operation_id, now))
            self._append_admin_audit(
                db, "owner_account_created", aid,
                {"account_id": aid, "operation_id": operation_id}, now)
        return {"account_id": aid, "login_key": login_key,
                "recovery_code": recovery_code, "device_id": device_id,
                "device_token": device_token}

    def reissue_owner_account_credentials(self):
        """Rotate credentials for the bound account and revoke all its devices."""
        with self._db() as db:
            binding = db.execute(
                "SELECT account_id FROM owner_account_binding WHERE slot='owner'"
            ).fetchone()
        if not binding:
            raise CloudError("owner account is not bound", 409)
        self._writable()
        self._auth_limit("owner-account", "owner_account_reissue", 5, 3600)
        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute(
                "SELECT account_id FROM owner_account_binding WHERE slot='owner'"
            ).fetchone()
            if not binding:
                raise CloudError("owner account is not bound", 409)
            aid = binding["account_id"]
            self._account(db, aid)
            login_key, recovery_code = _secret("sk_" + aid + "_"), _secret("sr_")
            db.execute(
                "UPDATE accounts SET login_hash=?,recovery_hash=? WHERE id=?",
                (_hash(login_key), _hash(recovery_code), aid))
            db.execute("UPDATE devices SET revoked=1 WHERE account_id=?", (aid,))
            db.execute("DELETE FROM web_sessions WHERE account_id=?", (aid,))
            device_id, device_token = self._new_device(
                db, aid, "黄油之王 管理员账号重新签发", now)
            self._append_admin_audit(
                db, "owner_account_credentials_reissued", aid,
                {"account_id": aid}, now)
        return {"account_id": aid, "login_key": login_key,
                "recovery_code": recovery_code, "device_id": device_id,
                "device_token": device_token}

    @staticmethod
    def _application_text(value, field, maximum, *, required=False):
        if not isinstance(value, str):
            raise CloudError("bad application fields")
        value = value.strip()
        allowed_controls = "\n\t" if field == "reason" else ""
        if ((required and not value) or len(value) > maximum
                or any(ord(ch) < 32 and ch not in allowed_controls for ch in value)):
            raise CloudError("bad application fields")
        return value

    @staticmethod
    def _application_submission_key(value):
        # Clients generate a random URL-safe token. Hash it before persistence;
        # the original token is never needed after idempotency lookup.
        if (not isinstance(value, str) or not 32 <= len(value) <= 128
                or value.strip() != value
                or any(not (ch.isascii() and (ch.isalnum() or ch in "-_"))
                       for ch in value)):
            raise CloudError("bad submission key")
        return value

    def _web_registration_secret(self, db):
        """Return the private key used only to derive retryable web credentials."""
        row = db.execute(
            "SELECT value FROM metadata WHERE key='web_registration_secret'"
        ).fetchone()
        if row:
            return bytes.fromhex(row["value"])
        secret = secrets.token_hex(32)
        db.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES "
            "('web_registration_secret',?)", (secret,))
        row = db.execute(
            "SELECT value FROM metadata WHERE key='web_registration_secret'"
        ).fetchone()
        return bytes.fromhex(row["value"])

    @staticmethod
    def _derive_web_registration_credentials(secret, operation_hash, aid):
        """Derive high-entropy one-time credentials without storing plaintext."""
        def derive(label):
            message = (b"slgking:web-registration:v1\0" + label
                       + b"\0" + operation_hash.encode("ascii")
                       + b"\0" + aid.encode("ascii"))
            digest = hmac.new(secret, message, hashlib.sha256).digest()
            return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        return ("sk_" + aid + "_" + derive(b"login"),
                "sr_" + derive(b"recovery"))

    def register_web_account(self, operation_id, *, identity):
        """Create a browser-only account and an HttpOnly-session candidate.

        The route must validate HTTPS and same-origin Origin before calling this
        method, then set ``session_token`` as the HttpOnly cookie without placing
        it in the JSON response. Repeating an unacknowledged operation ID safely
        returns the same credentials; only their hashes and the acknowledgement
        bit are persisted. This method never creates a devices row or an sd_ key.
        """
        operation_id = self._application_submission_key(operation_id)
        if (not isinstance(identity, str) or not identity
                or len(identity) > 256 or identity.strip() != identity
                or any(ord(ch) < 32 for ch in identity)):
            raise CloudError("bad identity")
        self._writable()
        self._auth_limit(identity, "web_registration", 5, 86400)
        operation_hash = _hash(operation_id)
        now = int(self.now())

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute(
                "SELECT account_id,credentials_acknowledged "
                "FROM web_registration_requests WHERE operation_hash=?",
                (operation_hash,)).fetchone()
            if request:
                aid = request["account_id"]
                self._account(db, aid)
                if request["credentials_acknowledged"]:
                    raise CloudError("registration credentials already acknowledged", 409)
                login_key, recovery_code = self._derive_web_registration_credentials(
                    self._web_registration_secret(db), operation_hash, aid)
                duplicate = True
            else:
                aid = "u_" + uuid.uuid4().hex
                secret = self._web_registration_secret(db)
                login_key, recovery_code = self._derive_web_registration_credentials(
                    secret, operation_hash, aid)
                db.execute(
                    "INSERT INTO accounts(id,login_hash,recovery_hash,created,"
                    "referral_eligible) VALUES (?,?,?,?,1)",
                    (aid, _hash(login_key), _hash(recovery_code), now))
                self._ensure_referral_code(db, aid)
                db.execute(
                    "INSERT INTO web_registration_requests "
                    "(operation_hash,account_id,created,credentials_acknowledged) "
                    "VALUES (?,?,?,0)", (operation_hash, aid, now))
                duplicate = False

        # Issuing a browser session is independent of the account transaction.
        # If the HTTP response is lost, retrying the same op_id replays these
        # credentials until the browser explicitly acknowledges saving them.
        session_token, session_expires = self._issue_web_session(aid)
        return {"account_id": aid, "login_key": login_key,
                "recovery_code": recovery_code,
                "credentials_available": True, "duplicate": duplicate,
                "session_token": session_token,
                "session_expires": session_expires}

    def pending_web_registration_credentials(self, aid):
        """Reveal an unacknowledged registration only to its active web account."""
        if not isinstance(aid, str) or not aid or len(aid) > 80:
            raise CloudError("bad account_id")
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, aid)
            request = db.execute(
                "SELECT operation_hash FROM web_registration_requests "
                "WHERE account_id=? AND credentials_acknowledged=0", (aid,)
            ).fetchone()
            if not request:
                return None
            login_key, recovery_code = self._derive_web_registration_credentials(
                self._web_registration_secret(db), request["operation_hash"], aid)
            # A credential rotation invalidates the original one-time material.
            # Do not reveal stale credentials or leave them pending indefinitely.
            if (not hmac.compare_digest(_hash(login_key), account["login_hash"])
                    or not hmac.compare_digest(_hash(recovery_code),
                                               account["recovery_hash"])):
                db.execute(
                    "UPDATE web_registration_requests "
                    "SET credentials_acknowledged=1 WHERE account_id=?",
                    (aid,))
                return None
            return {"credentials_available": True, "account_id": aid,
                    "login_key": login_key, "recovery_code": recovery_code}

    def acknowledge_web_registration_credentials(self, aid):
        """Stop replaying registration credentials for the verified web account."""
        if not isinstance(aid, str) or not aid or len(aid) > 80:
            raise CloudError("bad account_id")
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            updated = db.execute(
                "UPDATE web_registration_requests "
                "SET credentials_acknowledged=1 "
                "WHERE account_id=? AND credentials_acknowledged=0", (aid,))
        return {"ok": True, "acknowledged": bool(updated.rowcount)}

    def submit_account_application(self, submission_key, display_name="",
                                   contact="", reason="", identity=""):
        """Store one public account request, with a content-bound idempotency key."""
        submission_key = self._application_submission_key(submission_key)
        display_name = self._application_text(
            display_name, "display_name", 40)
        contact = self._application_text(contact, "contact", 120, required=True)
        reason = self._application_text(reason, "reason", 500)
        content_hash = _hash(json.dumps(
            [display_name, contact, reason], ensure_ascii=False,
            separators=(",", ":")))
        key_hash = _hash(submission_key)

        # A retry with the same payload is safe even if the storage is now
        # read-only. A key cannot be rebound to different submitted content.
        with self._db() as db:
            existing = db.execute(
                "SELECT id,status,content_hash FROM account_applications "
                "WHERE submission_key_hash=?", (key_hash,)).fetchone()
        if existing:
            if not hmac.compare_digest(existing["content_hash"], content_hash):
                raise CloudError("submission key already used", 409)
            return {"ok": True, "id": existing["id"],
                    "status": existing["status"], "duplicate": True}

        self._writable()
        self._auth_limit(identity, "application", 5, 86400)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            # Another request may have inserted this idempotency key while this
            # request waited for the write lock.
            existing = db.execute(
                "SELECT id,status,content_hash FROM account_applications "
                "WHERE submission_key_hash=?", (key_hash,)).fetchone()
            if existing:
                if not hmac.compare_digest(existing["content_hash"], content_hash):
                    raise CloudError("submission key already used", 409)
                return {"ok": True, "id": existing["id"],
                        "status": existing["status"], "duplicate": True}
            cursor = db.execute(
                "INSERT INTO account_applications "
                "(submission_key_hash,content_hash,display_name,contact,reason,created) "
                "VALUES (?,?,?,?,?,?)",
                (key_hash, content_hash, display_name, contact, reason, self.now()))
            application_id = cursor.lastrowid
        return {"ok": True, "id": application_id, "status": "pending",
                "duplicate": False}

    def admin_account_applications(self, limit=30, offset=0, status=""):
        if (isinstance(limit, bool) or isinstance(offset, bool)
                or not isinstance(limit, int) or not isinstance(offset, int)
                or not 1 <= limit <= 100 or not 0 <= offset <= 1000000):
            raise CloudError("bad pagination")
        if status and status not in ("pending", "approved", "rejected"):
            raise CloudError("bad status")
        where = "WHERE status=?" if status else ""
        args = (status,) if status else ()
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM account_applications " + where,
                args).fetchone()[0]
            rows = db.execute(
                "SELECT id,display_name,contact,reason,status,created,decided,"
                "decision_reason,account_id FROM account_applications " + where +
                " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,"
                "created DESC,id DESC LIMIT ? OFFSET ?",
                args + (limit, offset)).fetchall()
        return {"applications": [dict(row) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def decide_account_application(self, application_id, decision, reason=""):
        if (isinstance(application_id, bool) or not isinstance(application_id, int)
                or application_id <= 0):
            raise CloudError("not found", 404)
        if decision not in ("approve", "reject"):
            raise CloudError("bad decision")
        reason = self._application_text(reason, "reason", 200,
                                        required=(decision == "reject"))
        if decision == "approve" and reason:
            raise CloudError("approval reason must be empty")
        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM account_applications WHERE id=?",
                (application_id,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            if row["status"] == "approved":
                raise CloudError("already approved; use reissue", 409)
            if row["status"] != "pending":
                raise CloudError("already decided", 409)
            if decision == "reject":
                db.execute(
                    "UPDATE account_applications SET status='rejected',decided=?,"
                    "decision_reason=? WHERE id=?",
                    (now, reason, application_id))
                self._append_admin_audit(
                    db, "account_application_rejected", str(application_id),
                    {"decision": "reject"}, now)
                return {"ok": True, "status": "rejected",
                        "application_id": application_id}

            self._writable()
            aid = "u_" + uuid.uuid4().hex
            login_key = _secret("sk_" + aid + "_")
            recovery_code = _secret("sr_")
            db.execute(
                "INSERT INTO accounts(id,login_hash,recovery_hash,created,"
                "referral_eligible) VALUES (?,?,?,?,1)",
                (aid, _hash(login_key), _hash(recovery_code), now))
            self._ensure_referral_code(db, aid)
            db.execute(
                "UPDATE account_applications SET status='approved',decided=?,"
                "decision_reason='',account_id=? WHERE id=?",
                (now, aid, application_id))
            self._append_admin_audit(
                db, "account_application_approved", str(application_id),
                {"decision": "approve", "account_id": aid}, now)
        return {"ok": True, "status": "approved",
                "application_id": application_id, "account_id": aid,
                "login_key": login_key, "recovery_code": recovery_code}

    def reissue_account_application_credentials(self, application_id):
        if (isinstance(application_id, bool) or not isinstance(application_id, int)
                or application_id <= 0):
            raise CloudError("not found", 404)
        self._writable()
        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,account_id FROM account_applications WHERE id=?",
                (application_id,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            if row["status"] != "approved" or not row["account_id"]:
                raise CloudError("application is not approved", 409)
            aid = row["account_id"]
            if aid == DEVELOPER_ACCOUNT_ID:
                raise CloudError("invalid application account", 409)
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
                raise CloudError("application account unavailable", 409)
            login_key = _secret("sk_" + aid + "_")
            recovery_code = _secret("sr_")
            db.execute(
                "UPDATE accounts SET login_hash=?,recovery_hash=? WHERE id=?",
                (_hash(login_key), _hash(recovery_code), aid))
            db.execute("UPDATE devices SET revoked=1 WHERE account_id=?", (aid,))
            self._append_admin_audit(
                db, "account_application_credentials_reissued",
                str(application_id), {"account_id": aid}, now)
        return {"ok": True, "status": "approved",
                "application_id": application_id, "account_id": aid,
                "login_key": login_key, "recovery_code": recovery_code}

    def developer_login(self, developer_key, *, old_token=None, label=""):
        """Open the stable developer account using the owner key, without recovery."""
        self._writable()
        if not isinstance(developer_key, str) or not developer_key:
            raise CloudError("unauthorized", 401)
        now = self.now()
        key_hash = _hash(developer_key)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE id=?",
                             (DEVELOPER_ACCOUNT_ID,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO accounts(id,login_hash,recovery_hash,created) "
                    "VALUES (?,?,?,?)",
                    (DEVELOPER_ACCOUNT_ID, key_hash,
                     _hash(_secret("sr_")), now))
            else:
                # Keep the account's key hash current if the owner rotates the
                # server-side developer key. The key itself is never stored.
                # This endpoint is reached only after Handler validates the
                # owner key, so it can also recover legacy accidental bans.
                db.execute("UPDATE accounts SET login_hash=?,banned=0 WHERE id=?",
                           (key_hash, DEVELOPER_ACCOUNT_ID))
            old = None
            if isinstance(old_token, str) and old_token.startswith("sd_"):
                old = db.execute(
                    "SELECT id FROM devices WHERE token_hash=? AND account_id=? "
                    "AND revoked=0",
                    (_hash(old_token), DEVELOPER_ACCOUNT_ID)).fetchone()
            if old:
                db.execute("UPDATE devices SET last_used=? WHERE id=?",
                           (now, old["id"]))
                device_id, token = old["id"], old_token
            else:
                device_id, token = self._new_device(
                    db, DEVELOPER_ACCOUNT_ID, label, now)
        return {"account_id": DEVELOPER_ACCOUNT_ID,
                "device_id": device_id, "device_token": token}

    def _account(self, db, aid):
        row = db.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        if not row or row["banned"]:
            raise CloudError("unauthorized", 401)
        return row

    @staticmethod
    def _referral_title_steps():
        import slg_titles
        return slg_titles.REFERRAL_TITLE_STEPS

    def _ensure_referral_code(self, db, aid):
        row = db.execute("SELECT code FROM referral_codes WHERE account_id=?",
                         (aid,)).fetchone()
        if row:
            return row["code"]
        # Codes do not contain account identifiers or credentials. A unique
        # index arbitrates the vanishingly unlikely random collision.
        while True:
            code = secrets.token_hex(8).upper()
            try:
                db.execute(
                    "INSERT INTO referral_codes(account_id,code,created) "
                    "VALUES (?,?,?)", (aid, code, self.now()))
                return code
            except sqlite3.IntegrityError:
                row = db.execute(
                    "SELECT code FROM referral_codes WHERE account_id=?",
                    (aid,)).fetchone()
                if row:
                    return row["code"]

    @staticmethod
    def _referral_summary(db, aid):
        count = db.execute(
            "SELECT COUNT(*) FROM referrals WHERE inviter_account_id=?",
            (aid,)).fetchone()[0]
        title_ids = [title_id for threshold, title_id
                     in CloudStore._referral_title_steps() if count >= threshold]
        if title_ids:
            marks = ",".join("?" for _ in title_ids)
            earned = {row[0] for row in db.execute(
                "SELECT title_id FROM owned_titles WHERE account_id=? "
                "AND title_id IN (%s)" % marks, [aid] + title_ids)}
        else:
            earned = set()
        return count, [title_id for title_id in title_ids if title_id in earned]

    def referral_info(self, aid):
        """Return the authenticated account's stable invite code and progress."""
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, aid)
            code = self._ensure_referral_code(db, aid)
            count, earned_titles = self._referral_summary(db, aid)
            redeemed = db.execute(
                "SELECT 1 FROM referrals WHERE invitee_account_id=?",
                (aid,)).fetchone() is not None
            return {"ok": True, "invite_code": code,
                    "eligible_to_redeem": bool(
                        account["referral_eligible"] and not redeemed
                        and aid != DEVELOPER_ACCOUNT_ID),
                    "redeemed": redeemed, "invite_count": count,
                    "earned_titles": earned_titles}

    def redeem_referral(self, aid, code):
        """Atomically bind a new account to an inviter and award both sides."""
        if (not isinstance(code, str) or len(code.strip()) != 16
                or any(ch not in "0123456789abcdefABCDEF"
                       for ch in code.strip())):
            raise CloudError("unknown referral code", 404)
        code = code.strip().upper()

        # Return completed retries before checking storage writability. A client
        # whose first response was lost must be able to confirm success safely.
        with self._db() as db:
            account = self._account(db, aid)
            if aid == DEVELOPER_ACCOUNT_ID:
                raise CloudError("developer account cannot redeem referrals", 403)
            code_row = db.execute(
                "SELECT account_id FROM referral_codes WHERE code=?",
                (code,)).fetchone()
            if not code_row:
                raise CloudError("unknown referral code", 404)
            inviter = code_row["account_id"]
            if inviter == aid:
                raise CloudError("cannot use your own referral code", 400)
            if inviter == DEVELOPER_ACCOUNT_ID:
                raise CloudError("developer account cannot earn referral rewards", 403)
            existing = db.execute(
                "SELECT inviter_account_id FROM referrals "
                "WHERE invitee_account_id=?", (aid,)).fetchone()
            if existing:
                if existing["inviter_account_id"] == inviter:
                    return {"ok": True, "redeemed": True,
                            "duplicate": True, "points_awarded": 0,
                            "inviter_awarded": 0}
                raise CloudError("referral code already redeemed", 409)
            if not account["referral_eligible"]:
                raise CloudError("account is not eligible to redeem a referral code", 403)

        self._writable()
        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, aid)
            if aid == DEVELOPER_ACCOUNT_ID:
                raise CloudError("developer account cannot redeem referrals", 403)
            code_row = db.execute(
                "SELECT account_id FROM referral_codes WHERE code=?",
                (code,)).fetchone()
            if not code_row:
                raise CloudError("unknown referral code", 404)
            inviter = code_row["account_id"]
            if inviter == aid:
                raise CloudError("cannot use your own referral code", 400)
            if inviter == DEVELOPER_ACCOUNT_ID:
                raise CloudError("developer account cannot earn referral rewards", 403)

            existing = db.execute(
                "SELECT inviter_account_id FROM referrals "
                "WHERE invitee_account_id=?", (aid,)).fetchone()
            if existing:
                if existing["inviter_account_id"] == inviter:
                    return {"ok": True, "redeemed": True,
                            "duplicate": True, "points_awarded": 0,
                            "inviter_awarded": 0}
                raise CloudError("referral code already redeemed", 409)
            if not account["referral_eligible"]:
                raise CloudError("account is not eligible to redeem a referral code", 403)

            # A banned account's code must not continue generating rewards.
            self._account(db, inviter)
            db.execute(
                "INSERT INTO referrals(invitee_account_id,inviter_account_id,created) "
                "VALUES (?,?,?)", (aid, inviter, now))
            invitee_award = db.execute(
                "INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) "
                "VALUES (?,?,?,?,?)",
                (aid, 100, "referral_invitee", "referral:invitee:%s" % aid,
                 now))
            inviter_award = db.execute(
                "INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) "
                "VALUES (?,?,?,?,?)",
                (inviter, 100, "referral_inviter",
                 "referral:inviter:%s:%s" % (inviter, aid), now))
            db.execute("UPDATE accounts SET referral_eligible=0 WHERE id=?",
                       (aid,))
            count, _earned_titles = self._referral_summary(db, inviter)
            for threshold, title_id in self._referral_title_steps():
                if count >= threshold:
                    db.execute(
                        "INSERT OR IGNORE INTO owned_titles "
                        "(account_id,title_id,source,ts) VALUES (?,?,?,?)",
                        (inviter, title_id, "referral", now))
        return {"ok": True, "redeemed": True, "duplicate": False,
                "points_awarded": 100 if invitee_award.rowcount else 0,
                "inviter_awarded": 100 if inviter_award.rowcount else 0}

    def authenticate(self, token, *, touch=True, allow_expired=False):
        if not isinstance(token, str) or not token.startswith("sd_") or len(token) > 100:
            raise CloudError("unauthorized", 401)
        with self._db() as db:
            row = db.execute("SELECT * FROM devices WHERE token_hash=? AND revoked=0",
                             (_hash(token),)).fetchone()
            if not row:
                raise CloudError("unauthorized", 401)
            self._account(db, row["account_id"])
            now = self.now()
            if not allow_expired and now - row["last_used"] >= SESSION_IDLE_SECONDS:
                raise CloudError("reauthentication required", 401)
            if touch and now - row["last_used"] >= DEVICE_SESSION_TOUCH_SECONDS:
                # Coalesce activity writes, not authorization. The predicate also
                # prevents an older concurrent request from overwriting a newer touch.
                db.execute("UPDATE devices SET last_used=? WHERE id=? AND revoked=0 "
                           "AND last_used<=?",
                           (now, row["id"], now - DEVICE_SESSION_TOUCH_SECONDS))
            return row["account_id"], row["id"]

    def login(self, aid, key, *, old_token=None, recovery=None, label="", identity=""):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer must authenticate with the developer key", 401)
        self._auth_limit(identity, "login", 30, 3600)
        if not isinstance(key, str):
            raise CloudError("unauthorized", 401)
        if not aid and key.startswith("sk_u_"):
            pieces = key.split("_", 3)
            if len(pieces) == 4:
                aid = "u_" + pieces[2]
        if not isinstance(aid, str):
            raise CloudError("unauthorized", 401)
        now = self.now()
        with self._db() as db:
            account = self._account(db, aid)
            if not hmac.compare_digest(_hash(key), account["login_hash"]):
                raise CloudError("unauthorized", 401)
            old = None
            if isinstance(old_token, str) and old_token.startswith("sd_"):
                old = db.execute("SELECT * FROM devices WHERE token_hash=? AND account_id=? AND revoked=0",
                                 (_hash(old_token), aid)).fetchone()
            if not old:
                if not isinstance(recovery, str) or not hmac.compare_digest(
                        _hash(recovery), account["recovery_hash"]):
                    raise CloudError("recovery required", 401)
            if old:
                db.execute("UPDATE devices SET revoked=1 WHERE id=?", (old["id"],))
            did, token = self._new_device(db, aid, label, now)
        return {"account_id": aid, "device_id": did, "device_token": token}

    def me(self, aid):
        with self._db() as db:
            db.execute("BEGIN")
            account = self._account(db, aid)
            appearances = self._appearance_map(db, aid)
            balance = db.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE account_id=?",
                                 (aid,)).fetchone()[0]
            titles = [r[0] for r in db.execute("SELECT title_id FROM owned_titles WHERE account_id=? ORDER BY ts",
                                             (aid,))]
            today = datetime.fromtimestamp(self.now(), CHINA).date()
            month = today.strftime("%Y-%m")
            if aid == DEVELOPER_ACCOUNT_ID:
                # Developer rewards have one ledger row per click, so derive
                # attendance by distinct China-local dates from timestamps.
                # This keeps repeated rewards from duplicating calendar days.
                oldest_day = today - timedelta(days=365)
                oldest_ts = int(datetime.combine(
                    oldest_day, datetime.min.time(), CHINA).timestamp())
                dates = db.execute(
                    "SELECT DISTINCT date(ts,'unixepoch','+8 hours') "
                    "FROM ledger WHERE account_id=? "
                    "AND reason IN ('signin','developer_signin') AND ts>=? "
                    "ORDER BY 1", (aid, oldest_ts)).fetchall()
                all_days = {row[0] for row in dates}
                signin_days = [day for day in sorted(all_days)
                               if day.startswith(month)]
                # Developer mode has no monthly signin milestone payouts.
                milestones = []
            else:
                signin_days = [r[0].rsplit(":", 1)[-1] for r in db.execute(
                    "SELECT claim_key FROM ledger WHERE account_id=? AND reason='signin' AND claim_key LIKE ? ORDER BY ts",
                    (aid, "signin:%s:%s-%%" % (aid, month)))]
                milestones = [n for n in (5, 10, 20) if db.execute(
                    "SELECT 1 FROM ledger WHERE claim_key=?",
                    ("signin_bonus:%s:%s:%d" % (aid, month, n),)).fetchone()]
                all_days = {r[0].rsplit(":", 1)[-1] for r in db.execute(
                    "SELECT claim_key FROM ledger WHERE account_id=? AND reason='signin' ORDER BY ts DESC LIMIT 366",
                    (aid,))}
            streak = 0
            cursor = today if today.isoformat() in all_days else today - timedelta(days=1)
            while cursor.isoformat() in all_days:
                streak += 1
                cursor -= timedelta(days=1)
            draw_rows = db.execute("SELECT day,kind,value,ts FROM lottery_draws WHERE account_id=? ORDER BY ts DESC,id DESC LIMIT 20",
                                   (aid,)).fetchall()
            lottery_pity = 0
            for draw in db.execute("SELECT kind FROM lottery_draws WHERE account_id=? ORDER BY ts DESC,id DESC LIMIT 100",
                                   (aid,)):
                if draw[0] == "title":
                    break
                lottery_pity += 1
            draws_today = db.execute(
                "SELECT COUNT(*) FROM lottery_draws WHERE account_id=? AND day=?",
                (aid, today.isoformat())).fetchone()[0]
            return {"account_id": aid, "created": account["created"],
                    "nickname": account["nickname"], "balance": balance,
                    "profile_message": account["profile_message"],
                    "profile_visible": bool(account["profile_visible"]),
                    "public_profile_id": account["public_profile_id"],
                    "banned": False, "titles": titles,
                     "owned_titles": titles, "equipped_title": account["equipped_title"],
                     "equipped_cosmetic": appearances["comment_frame"],
                     "equipped_appearances": appearances,
                    "signin_month_days": signin_days,
                    "signin_month_count": len(signin_days), "signin_streak": streak,
                    "signin_milestones": milestones,
                    "signed_today": today.isoformat() in all_days,
                    "draws_today": draws_today,
                    "lottery_pity": lottery_pity,
                    "recent_lottery_history": [dict(r) for r in draw_rows]}

    def devices(self, aid):
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,label,created,last_used FROM devices WHERE account_id=? AND revoked=0 ORDER BY last_used DESC",
                (aid,))]

    def revoke(self, aid, did):
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot revoke devices", 403)
        with self._db() as db:
            changed = db.execute("UPDATE devices SET revoked=1 WHERE account_id=? AND id=? AND revoked=0",
                                 (aid, did)).rowcount
        if not changed:
            raise CloudError("not found", 404)

    @staticmethod
    def _appearance_map(db, aid):
        rows = db.execute(
            "SELECT slot,item_id FROM account_appearances WHERE account_id=?",
            (aid,)).fetchall()
        result = {"comment_frame": "", "avatar_frame": ""}
        for row in rows:
            if row["slot"] in result:
                result[row["slot"]] = row["item_id"]
        return result

    @staticmethod
    def _builtin_appearance(item_id, slot):
        import slg_titles
        return next((item for item in slg_titles.SHOP_ITEMS
                     if item.get("id") == item_id
                     and item.get("kind") == "decoration"
                     and item.get("appearance") == slot
                     and item.get("asset_source") == "builtin"
                     and item.get("cloud_only")), None)

    def equip(self, aid, title_id=None, cosmetic_id=None,
              appearance_slot=None, item_id=None):
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot use ordinary appearance equipment", 403)
        if title_id is not None and (not isinstance(title_id, str) or len(title_id) > 80):
            raise CloudError("bad title")
        if cosmetic_id is not None and (not isinstance(cosmetic_id, str) or len(cosmetic_id) > 80):
            raise CloudError("bad cosmetic")
        if appearance_slot is not None and appearance_slot not in ("comment_frame", "avatar_frame"):
            raise CloudError("bad appearance slot")
        if item_id is not None and (not isinstance(item_id, str) or len(item_id) > 80):
            raise CloudError("bad appearance")
        if (appearance_slot is None) != (item_id is None):
            raise CloudError("bad appearance")
        if cosmetic_id is not None and appearance_slot == "comment_frame":
            raise CloudError("duplicate appearance slot")
        if title_id is None and cosmetic_id is None and item_id is None:
            raise CloudError("missing equipment")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, aid)
            if title_id is not None:
                if title_id != "normal_user" and not db.execute(
                        "SELECT 1 FROM owned_titles WHERE account_id=? AND title_id=?",
                        (aid, title_id)).fetchone():
                    raise CloudError("title not owned", 403)
                db.execute("UPDATE accounts SET equipped_title=? WHERE id=?", (title_id, aid))
                account = self._account(db, aid)
            # The legacy cosmetic_id field is the comment/namecard frame slot.
            slot_updates = []
            if cosmetic_id is not None:
                slot_updates.append(("comment_frame", cosmetic_id))
            if appearance_slot is not None:
                slot_updates.append((appearance_slot, item_id))
            for slot, equipped_id in slot_updates:
                if equipped_id and not db.execute(
                        "SELECT 1 FROM owned_titles WHERE account_id=? AND title_id=?",
                        (aid, equipped_id)).fetchone():
                    raise CloudError("cosmetic not owned", 403)
                if equipped_id and self._builtin_appearance(equipped_id, slot) is None:
                    raise CloudError("cosmetic unavailable", 404)
                if equipped_id:
                    db.execute(
                        "INSERT INTO account_appearances(account_id,slot,item_id) VALUES (?,?,?) "
                        "ON CONFLICT(account_id,slot) DO UPDATE SET item_id=excluded.item_id",
                        (aid, slot, equipped_id))
                else:
                    db.execute("DELETE FROM account_appearances WHERE account_id=? AND slot=?",
                               (aid, slot))
                if slot == "comment_frame":
                    db.execute("UPDATE accounts SET equipped_cosmetic=? WHERE id=?",
                               (equipped_id, aid))
                account = self._account(db, aid)
            appearances = self._appearance_map(db, aid)
        return {"ok": True, "equipped_title": account["equipped_title"],
                "equipped_cosmetic": appearances["comment_frame"],
                "equipped_appearances": appearances}

    def profile(self, aid, nickname=_UNSET, profile_message=_UNSET,
                moderation=None, profile_visible=_UNSET):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer credentials are managed by the server", 403)
        updates = []
        values = []
        if profile_visible is not _UNSET:
            if not isinstance(profile_visible, bool):
                raise CloudError("bad profile visibility")
            updates.append("profile_visible=?")
            values.append(int(profile_visible))
        if nickname is not _UNSET:
            if not isinstance(nickname, str):
                raise CloudError("bad nickname")
            nickname = nickname.strip()
            try:
                nickname_bytes = len(nickname.encode("utf-8"))
            except UnicodeEncodeError:
                raise CloudError("bad nickname")
            if len(nickname) > 40 or nickname_bytes > 160:
                raise CloudError("bad nickname")
            updates.append("nickname=?")
            values.append(nickname)
        if profile_message is not _UNSET:
            if not isinstance(profile_message, str):
                raise CloudError("bad profile message")
            profile_message = profile_message.strip()
            try:
                message_bytes = len(profile_message.encode("utf-8"))
            except UnicodeEncodeError:
                raise CloudError("bad profile message")
            if (len(profile_message) > 20 or message_bytes > 80
                    or any(unicodedata.category(ch) == "Cc"
                           for ch in profile_message)):
                raise CloudError("profile message must be at most 20 characters")
            if profile_message:
                if moderation is None:
                    raise CloudError("profile message moderation unavailable", 503)
                # The shared classifier requires 20 characters for a review.
                # Add neutral context for this short profile field so its
                # political, link, advertising, and age-safety rules still run.
                verdict = moderation(
                    profile_message + "；这是用于账号公开展示的个人名片寄语内容。")
                if not isinstance(verdict, dict) or verdict.get("decision") != "approve":
                    raise CloudError("寄语未通过内容检查，请修改后重试", 400)
            updates.append("profile_message=?")
            values.append(profile_message)
        if not updates:
            raise CloudError("missing profile fields")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            if profile_message is not _UNSET and profile_message:
                import slg_titles
                namecard_ids = sorted({
                    item.get("id") for item in slg_titles.SHOP_ITEMS
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and item.get("kind") == "decoration"
                    and item.get("appearance") == "comment_frame"
                    and item.get("cloud_only")
                    and item.get("asset_source") == "builtin"
                })
                if not namecard_ids:
                    raise CloudError("namecard frame unavailable", 503)
                placeholders = ",".join("?" for _ in namecard_ids)
                owns_frame = db.execute(
                    "SELECT 1 FROM owned_titles WHERE account_id=? "
                    "AND title_id IN (%s) LIMIT 1" % placeholders,
                    (aid, *namecard_ids)).fetchone()
                if not owns_frame:
                    raise CloudError("购买名片框后才能设置寄语", 403)
            values.append(aid)
            db.execute("UPDATE accounts SET %s WHERE id=?" % ",".join(updates),
                       values)
            account = self._account(db, aid)
        return {"ok": True, "nickname": account["nickname"],
                "public_profile_id": account["public_profile_id"],
                "profile_visible": bool(account["profile_visible"]),
                "profile_message": account["profile_message"]}

    def rotate(self, aid, login_key, recovery_code):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer credentials are managed by the server", 403)
        now = self.now()
        with self._db() as db:
            account = self._account(db, aid)
            if not isinstance(login_key, str) or not hmac.compare_digest(
                    _hash(login_key), account["login_hash"]):
                raise CloudError("unauthorized", 401)
            if recovery_code is not None and (not isinstance(recovery_code, str) or
                    not hmac.compare_digest(_hash(recovery_code), account["recovery_hash"])):
                raise CloudError("unauthorized", 401)
            key, recovery = _secret("sk_" + aid + "_"), _secret("sr_")
            db.execute("UPDATE accounts SET login_hash=?,recovery_hash=? WHERE id=?",
                       (_hash(key), _hash(recovery), aid))
            db.execute("UPDATE devices SET revoked=1 WHERE account_id=?", (aid,))
            # Browser-only sessions have no device row to revoke with.
            db.execute("DELETE FROM web_sessions WHERE account_id=?", (aid,))
            did, token = self._new_device(db, aid, "rotated", now)
        return {"account_id": aid, "login_key": key, "recovery_code": recovery,
                "device_id": did, "device_token": token}

    def ban(self, aid, banned=True):
        # The stable developer identity is the owner principal. Even an
        # authenticated admin-console request must not be able to lock it out
        # through the ordinary account-moderation action.
        if aid == DEVELOPER_ACCOUNT_ID and banned:
            raise CloudError("developer account cannot be banned", 403)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if banned and db.execute(
                    "SELECT 1 FROM owner_account_binding WHERE account_id=?",
                    (aid,)).fetchone():
                raise CloudError("owner account cannot be banned", 403)
            if not db.execute("UPDATE accounts SET banned=? WHERE id=?", (int(banned), aid)).rowcount:
                raise CloudError("not found", 404)
            if banned:
                db.execute("DELETE FROM web_sessions WHERE account_id=?", (aid,))
            self._append_admin_audit(
                db, "account_ban" if banned else "account_unban", aid,
                {"banned": bool(banned)}, self.now())

    def grant_all_developer_appearances(self):
        """Grant every collectible appearance to the fixed owner account.

        Authorization is enforced by the HTTP handler using the server-side
        developer-key digest. This method deliberately accepts no caller-supplied
        account ID, so it cannot be repurposed to grant ordinary accounts.
        """
        import slg_titles

        appearance_ids = sorted({
            item_id for item_id in slg_titles.collectible_appearance_ids()
            if isinstance(item_id, str) and item_id and item_id != "normal_user"
        })
        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute("SELECT * FROM accounts WHERE id=?",
                                 (DEVELOPER_ACCOUNT_ID,)).fetchone()
            if account is None:
                # Keep the owner's cloud profile stable even if the title
                # action is the first developer operation after deployment.
                db.execute(
                    "INSERT INTO accounts(id,login_hash,recovery_hash,created) "
                    "VALUES (?,?,?,?)",
                    (DEVELOPER_ACCOUNT_ID, _hash(_secret("sk_")),
                     _hash(_secret("sr_")), now))
                equipped_title = "normal_user"
            else:
                equipped_title = account["equipped_title"] or "normal_user"
                # The HTTP route has already validated the owner key. An old
                # deployment could have let the owner ban this fixed account;
                # this action is a safe recovery path for that legacy state.
                db.execute("UPDATE accounts SET banned=0 WHERE id=?",
                           (DEVELOPER_ACCOUNT_ID,))

            catalog_ids = {item["id"] for item in slg_titles.TITLES
                           if isinstance(item, dict)
                           and isinstance(item.get("id"), str)}
            if equipped_title not in catalog_ids:
                equipped_title = "normal_user"
                db.execute("UPDATE accounts SET equipped_title=? WHERE id=?",
                           (equipped_title, DEVELOPER_ACCOUNT_ID))

            granted = []
            for title_id in appearance_ids:
                cur = db.execute(
                    "INSERT OR IGNORE INTO owned_titles "
                    "(account_id,title_id,source,ts) VALUES (?,?,?,?)",
                    (DEVELOPER_ACCOUNT_ID, title_id, "developer", now))
                if cur.rowcount:
                    granted.append(title_id)
            owned = [row[0] for row in db.execute(
                "SELECT title_id FROM owned_titles WHERE account_id=? "
                "ORDER BY title_id", (DEVELOPER_ACCOUNT_ID,))]
        owned_appearances = sorted(set(owned).intersection(appearance_ids))
        return {"ok": True, "account_id": DEVELOPER_ACCOUNT_ID,
                "granted": granted, "titles": owned,
                "items": owned_appearances,
                "equipped_title": equipped_title}

    def grant_all_developer_titles(self):
        """Backward-compatible alias for the developer appearance grant."""
        return self.grant_all_developer_appearances()

    def _group_secret(self, db):
        row = db.execute("SELECT value FROM metadata WHERE key='group_secret'").fetchone()
        if row:
            return row[0]
        secret = secrets.token_hex(32)
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('group_secret',?)", (secret,))
        return db.execute("SELECT value FROM metadata WHERE key='group_secret'").fetchone()[0]

    def group_code(self, day=None):
        day = day or datetime.fromtimestamp(self.now(), CHINA).date()
        with self._db() as db:
            secret = self._group_secret(db)
        digest = hmac.new(bytes.fromhex(secret), day.isoformat().encode("ascii"), hashlib.sha256).digest()
        return "QUNYOU-" + base64.b32encode(digest).decode("ascii")[:10]

    def redeem_group(self, aid, code):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot redeem group rewards", 403)
        if not isinstance(code, str):
            raise CloudError("invalid code")
        today = datetime.fromtimestamp(self.now(), CHINA).date()
        day = next((d for d in (today, today - timedelta(days=1))
                    if hmac.compare_digest(code.strip().upper(), self.group_code(d))), None)
        if day is None:
            raise CloudError("invalid code")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            cur = db.execute("INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                             (aid, 10, "group_code", "group:%s:%s" % (aid, day), self.now()))
            if cur.rowcount:
                self._stamp_business_platform(db, 'ledger', cur.lastrowid)
            db.execute("INSERT OR IGNORE INTO owned_titles VALUES (?,?,?,?)",
                       (aid, "group_friend", "group_code", self.now()))
            balance = db.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE account_id=?",
                                 (aid,)).fetchone()[0]
        return {"ok": True, "gained": 10 if cur.rowcount else 0,
                "already_claimed": not bool(cur.rowcount), "balance": balance,
                "date": day.isoformat(), "titles": self.me(aid)["titles"]}

    def migrate_legacy(self, aid, source_id, points, titles):
        """Queue an untrusted local snapshot for review, once per account/source."""
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot migrate legacy data", 403)
        if (not isinstance(source_id, str) or len(source_id) != 32
                or any(char not in "0123456789abcdef" for char in source_id)):
            raise CloudError("bad source")
        if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= 100000:
            raise CloudError("bad points")
        if not isinstance(titles, list) or len(titles) > 100 or any(
                not isinstance(title, str) or len(title) > 80 for title in titles):
            raise CloudError("bad titles")
        import slg_titles
        referral_title_ids = {title_id for _threshold, title_id
                              in slg_titles.REFERRAL_TITLE_STEPS}
        allowed_titles = {item["id"] for item in slg_titles.TITLES
                          if item["id"] not in referral_title_ids}
        clean_titles = sorted(set(titles))
        if any(title not in allowed_titles for title in clean_titles):
            raise CloudError("bad titles")
        source_digest = hashlib.sha256(source_id.encode("ascii")).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            existing = db.execute(
                "SELECT source_digest,points,titles FROM legacy_migrations WHERE account_id=?",
                (aid,)).fetchone()
            if existing:
                if existing["source_digest"] != source_digest:
                    raise CloudError("account already migrated from another local source", 409)
                balance = db.execute(
                    "SELECT COALESCE(SUM(delta),0) FROM ledger WHERE account_id=?",
                    (aid,)).fetchone()[0]
                return {"ok": True, "status": "already_migrated",
                        "points": existing["points"],
                        "titles": json.loads(existing["titles"]),
                        "points_added": 0, "titles_added": 0,
                        "balance": balance}
            used = db.execute(
                "SELECT 1 FROM legacy_migrations WHERE source_digest=?",
                (source_digest,)).fetchone()
            if used:
                raise CloudError("legacy source already used; contact support", 409)
            old_request = db.execute(
                "SELECT id,source_digest,status,approved_points,approved_titles "
                "FROM migration_requests WHERE account_id=?",
                (aid,)).fetchone()
            if old_request:
                # An approval for a different local source must not make the
                # client remove assets from this installation's database.
                if old_request["source_digest"] != source_digest:
                    raise CloudError("legacy source already used; contact support", 409)
                if old_request["status"] == "approved":
                    approved_titles = json.loads(old_request["approved_titles"] or "[]")
                    return {"ok": True, "status": "manual_approved",
                            "request_id": old_request["id"],
                            "points": old_request["approved_points"] or 0,
                            "titles": approved_titles}
                if old_request["status"] == "rejected":
                    return {"ok": True, "status": "manual_rejected",
                            "request_id": old_request["id"]}
                return {"ok": True, "status": "pending_manual_review",
                        "request_id": old_request["id"],
                        "request_status": old_request["status"]}
            if points == 0 and not clean_titles:
                return {"ok": True, "status": "nothing_to_migrate",
                        "points_added": 0, "titles_added": 0}
            # Local databases and exported backups are editable, so their
            # balances cannot authorize a server-side ledger credit.
            if db.execute(
                    "SELECT 1 FROM migration_requests "
                    "WHERE source_digest=? AND account_id<>?",
                    (source_digest, aid)).fetchone():
                raise CloudError("legacy source already used; contact support", 409)
            request = db.execute(
                "INSERT INTO migration_requests"
                "(account_id,claimed_points,titles,source_digest,ts) "
                "VALUES (?,?,?,?,?)",
                (aid, points, json.dumps(clean_titles, ensure_ascii=False),
                 source_digest, self.now()))
            request_id = request.lastrowid
        return {"ok": True, "status": "pending_manual_review",
                "request_id": request_id, "request_status": "pending"}

    def migration_request(self, aid, points, titles, source_digest=""):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot request legacy migration", 403)
        if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= 1000000:
            raise CloudError("bad points")
        if not isinstance(titles, list) or len(titles) > 100 or any(
                not isinstance(t, str) or len(t) > 80 for t in titles):
            raise CloudError("bad titles")
        if not isinstance(source_digest, str) or len(source_digest) > 128:
            raise CloudError("bad digest")
        with self._db() as db:
            if db.execute("SELECT 1 FROM legacy_migrations WHERE account_id=?",
                          (aid,)).fetchone():
                return {"ok": True, "pending": False, "already_migrated": True}
            self._account(db, aid)
            cur = db.execute("INSERT OR IGNORE INTO migration_requests(account_id,claimed_points,titles,source_digest,ts) VALUES (?,?,?,?,?)",
                             (aid, points, json.dumps(titles, ensure_ascii=False), source_digest,
                              self.now()))
        return {"ok": True, "pending": True, "already_requested": not bool(cur.rowcount)}

    def migration_requests(self, limit=30, offset=0, status=""):
        limit = max(1, min(int(limit), 50))
        offset = max(0, min(int(offset), 100000))
        if status and status not in ("pending", "approved", "rejected"):
            raise CloudError("bad status")
        where = "WHERE status=?" if status else ""
        args = (status,) if status else ()
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM migration_requests " + where,
                               args).fetchone()[0]
            rows = db.execute("SELECT * FROM migration_requests " + where +
                              " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,ts DESC LIMIT ? OFFSET ?",
                              args + (limit, offset)).fetchall()
            out = [dict(r) for r in rows]
        for r in out:
            r["titles"] = json.loads(r["titles"])
            if r["approved_titles"]:
                r["approved_titles"] = json.loads(r["approved_titles"])
        return {"requests": out, "total": total}

    def decide_migration(self, request_id, decision, points=0, titles=None, reason=""):
        if decision not in ("approve", "reject"):
            raise CloudError("bad decision")
        if not isinstance(points, int) or isinstance(points, bool) or not 0 <= points <= 100000:
            raise CloudError("bad points")
        if not isinstance(titles, list) or len(titles) > 100 or any(not isinstance(t, str) for t in titles):
            raise CloudError("bad titles")
        import slg_titles
        allowed = {t["id"] for t in slg_titles.TITLES}
        if any(t not in allowed for t in titles):
            raise CloudError("bad titles")
        reason = str(reason)[:200]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM migration_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            if row["status"] != "pending":
                raise CloudError("already decided", 409)
            now = self.now()
            aid = row["account_id"]
            if decision == "approve":
                self._writable()
                if points:
                    db.execute("INSERT INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                               (aid, points, "legacy_migration", "legacy:%s" % request_id, now))
                for title in set(titles):
                    db.execute("INSERT OR IGNORE INTO owned_titles VALUES (?,?,?,?)",
                               (aid, title, "legacy_migration", now))
            db.execute("UPDATE migration_requests SET status=?,decided_ts=?,approved_points=?,approved_titles=?,reason=? WHERE id=?",
                       ("approved" if decision == "approve" else "rejected", now,
                        points if decision == "approve" else 0,
                        json.dumps(titles if decision == "approve" else [], ensure_ascii=False),
                        reason, request_id))
        return {"ok": True, "status": "approved" if decision == "approve" else "rejected",
                "account_id": aid}

    def decide_migrations_bulk(self, request_ids, reason="", points_cap=2000):
        # 批量批准迁移申请：整批共用一个 BEGIN IMMEDIATE 事务，逐条独立判定，
        # 超出上限或已决/缺失的条目跳过而不夹紧积分，保证不静默少发。
        if (not isinstance(request_ids, list) or not 1 <= len(request_ids) <= 200
                or any(not isinstance(i, int) or isinstance(i, bool) or i <= 0
                       for i in request_ids)):
            raise CloudError("bad request ids")
        ordered = []
        seen = set()
        for request_id in request_ids:
            if request_id not in seen:
                seen.add(request_id)
                ordered.append(request_id)
        reason = str(reason)[:200]
        import slg_titles
        allowed = {t["id"] for t in slg_titles.TITLES}
        items = []
        applied = 0
        skipped = 0
        total_points = 0
        total_titles = 0
        wrote = False
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for request_id in ordered:
                row = db.execute("SELECT * FROM migration_requests WHERE id=?",
                                 (request_id,)).fetchone()
                if not row:
                    items.append({"id": request_id, "status": "skipped",
                                  "reason": "not_found", "points": 0, "titles": []})
                    skipped += 1
                    continue
                if row["status"] != "pending":
                    items.append({"id": request_id, "status": "skipped",
                                  "reason": "already_decided", "points": 0, "titles": []})
                    skipped += 1
                    continue
                claimed = int(row["claimed_points"] or 0)
                if claimed > points_cap:
                    items.append({"id": request_id, "status": "skipped",
                                  "reason": "over_cap", "points": 0, "titles": []})
                    skipped += 1
                    continue
                points = claimed
                titles = [t for t in json.loads(row["titles"] or "[]")
                          if t in allowed][:100]
                aid = row["account_id"]
                now = self.now()
                if not wrote:
                    self._writable()
                    wrote = True
                if points:
                    db.execute("INSERT INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                               (aid, points, "legacy_migration",
                                "legacy:%d" % request_id, now))
                for title in titles:
                    db.execute("INSERT OR IGNORE INTO owned_titles VALUES (?,?,?,?)",
                               (aid, title, "legacy_migration", now))
                db.execute("UPDATE migration_requests SET status='approved',decided_ts=?,approved_points=?,approved_titles=?,reason=? WHERE id=?",
                           (now, points, json.dumps(titles, ensure_ascii=False),
                            reason, request_id))
                items.append({"id": request_id, "status": "approved", "reason": "",
                              "points": points, "titles": titles})
                applied += 1
                total_points += points
                total_titles += len(titles)
        return {"ok": True, "applied": applied, "skipped": skipped,
                "total_points": total_points, "total_titles": total_titles,
                "items": items}

    @staticmethod
    def _stamp_business_platform(db, source, row_id):
        db.execute('INSERT OR IGNORE INTO business_platforms(origin,platform) VALUES (?,?)',
                   (source + ':' + str(row_id), _BUSINESS_PLATFORM.get()))

    def signin(self, aid):
        self._writable()
        import slg_titles
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account is not eligible for attendance rewards", 403)

        day = datetime.fromtimestamp(self.now(), CHINA).date().isoformat()
        claim_key = "signin:%s:%s" % (aid, day)
        with self._db() as db:
            # Repeat attendance reads a consistent snapshot without competing
            # for the writer lock. A first claim still revalidates below.
            db.execute("BEGIN")
            self._account(db, aid)
            if db.execute("SELECT 1 FROM ledger WHERE claim_key=?",
                          (claim_key,)).fetchone():
                return {"ok": True, "already": True, "gained": 0, "bonus": 0,
                        "balance": self._balance(db, aid), "day": day,
                        "developer": False}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            cur = db.execute(
                "INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) "
                "VALUES (?,?,?,?,?)",
                (aid, slg_titles.DAILY_SIGNIN_POINTS, "signin",
                 claim_key, self.now()))
            first_today = bool(cur.rowcount)
            if first_today:
                self._stamp_business_platform(db, 'ledger', cur.lastrowid)
            bonus = 0
            if first_today:
                month = day[:7]
                count = db.execute(
                    "SELECT COUNT(*) FROM ledger WHERE account_id=? AND reason='signin' "
                    "AND claim_key LIKE ?",
                    (aid, "signin:%s:%s-%%" % (aid, month))).fetchone()[0]
                for milestone, points in ((5, 15), (10, 30), (20, 50)):
                    if count >= milestone:
                        award = db.execute(
                            "INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) "
                            "VALUES (?,?,?,?,?)",
                            (aid, points, "signin_bonus",
                             "signin_bonus:%s:%s:%d" % (aid, month, milestone),
                             self.now()))
                        if award.rowcount:
                            bonus += points
            balance = db.execute(
                "SELECT COALESCE(SUM(delta),0) FROM ledger WHERE account_id=?",
                (aid,)).fetchone()[0]
        gained = slg_titles.DAILY_SIGNIN_POINTS if first_today else 0
        return {"ok": True, "already": not first_today,
                "gained": gained, "bonus": bonus,
                "balance": balance, "day": day, "developer": False}

    def maintenance_reward_status(self, aid, campaign_id, campaign):
        """Return whether this account has claimed the configured campaign."""
        with self._db() as db:
            self._account(db, aid)
            balance = self._balance(db, aid)
            claimed = False
            if campaign:
                claimed = db.execute(
                    "SELECT 1 FROM ledger WHERE account_id=? AND claim_key=?",
                    (aid, "maintenance:%s:%s" % (aid, campaign_id))).fetchone() is not None
        return {"campaign": campaign, "claimed": claimed, "balance": balance}

    def claim_maintenance_reward(self, aid, campaign_id, amount):
        """Idempotently claim one bounded, server-configured maintenance award."""
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account is not eligible for maintenance rewards", 403)
        if (not isinstance(campaign_id, str) or not campaign_id
                or len(campaign_id) > 64):
            raise CloudError("campaign unavailable", 409)
        if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= 100000:
            raise CloudError("campaign unavailable", 409)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            cur = db.execute(
                "INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) "
                "VALUES (?,?,?,?,?)",
                (aid, amount, "maintenance_compensation",
                 "maintenance:%s:%s" % (aid, campaign_id), self.now()))
            balance = self._balance(db, aid)
        return {"ok": True, "claimed": True,
                "gained": amount if cur.rowcount else 0, "balance": balance}

    def _balance(self, db, aid):
        return db.execute("SELECT COALESCE(SUM(delta),0) FROM ledger WHERE account_id=?",
                          (aid,)).fetchone()[0]

    def _titles(self, db, aid):
        return [r[0] for r in db.execute("SELECT title_id FROM owned_titles WHERE account_id=? ORDER BY ts",
                                         (aid,))]

    @staticmethod
    def _leaderboard_nickname(value):
        # Nicknames are plain display text. Remove control characters and use
        # a generic label for accounts that have not set one.
        cleaned = "".join(ch for ch in str(value or "")
                          if unicodedata.category(ch) != "Cc").strip()[:40]
        return cleaned or "匿名用户"

    def leaderboard(self, board="points", limit=30):
        if board not in ("points", "titles", "wardrobe"):
            raise CloudError("bad leaderboard")
        if isinstance(limit, bool):
            raise CloudError("bad pagination")
        try:
            if isinstance(limit, str) and (
                    not limit or any(ch not in "0123456789" for ch in limit)):
                raise ValueError
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination")
        if limit < 1:
            raise CloudError("bad pagination")
        # Old clients still ask for 50; cap them to the public top 30 so the
        # response remains compatible while the leaderboard policy is unified.
        limit = min(limit, 30)

        with self._db() as db:
            if board == "points":
                rows = db.execute(
                    "SELECT a.nickname,a.created,a.id,a.public_profile_id,a.profile_visible,COALESCE(SUM(l.delta),0) AS score "
                    "FROM accounts a LEFT JOIN ledger l ON l.account_id=a.id "
                    "WHERE a.id<>? AND a.banned=0 "
                    "GROUP BY a.id HAVING score>0 "
                    "ORDER BY score DESC,a.created ASC,a.id ASC LIMIT ?",
                    (DEVELOPER_ACCOUNT_ID, limit)).fetchall()
            else:
                import slg_titles
                appearance_ids = sorted({
                    item_id for item_id in slg_titles.collectible_appearance_ids()
                    if isinstance(item_id, str) and item_id
                    and item_id != "normal_user"
                })
                if appearance_ids:
                    placeholders = ",".join("?" for _ in appearance_ids)
                    rows = db.execute(
                        "SELECT a.nickname,a.created,a.id,a.public_profile_id,a.profile_visible,COUNT(DISTINCT o.title_id) AS score "
                        "FROM accounts a LEFT JOIN owned_titles o "
                        "ON o.account_id=a.id AND o.title_id IN (%s) "
                        "WHERE a.id<>? AND a.banned=0 "
                        "GROUP BY a.id HAVING score>0 "
                        "ORDER BY score DESC,a.created ASC,a.id ASC LIMIT ?" % placeholders,
                        (*appearance_ids, DEVELOPER_ACCOUNT_ID, limit)).fetchall()
                else:
                    rows = []
        entries = [{"rank": rank, "nickname": self._leaderboard_nickname(row["nickname"]),
                    "score": int(row["score"]), "public_id": row["public_profile_id"],
                    "profile_visible": bool(row["profile_visible"])}
                   for rank, row in enumerate(rows, 1)]
        # `titles` remains a valid request/response value for clients predating
        # the renamed wardrobe board; both keys now share the same score.
        return {"board": board, "entries": entries}

    def refresh_leaderboard_public(self, entries):
        """Refresh display/privacy data without caching private account fields.

        Rank and score may come from the short aggregate cache; identity fields
        always come from one current database snapshot, including across workers.
        """
        if not isinstance(entries, (list, tuple)) or len(entries) > 30:
            raise CloudError("bad leaderboard entries")
        ids = list(dict.fromkeys(row.get("public_id") for row in entries
                                 if isinstance(row, dict)
                                 and isinstance(row.get("public_id"), str)
                                 and row["public_id"]))
        if not ids:
            return []
        with self._db() as db:
            rows = db.execute(
                "SELECT public_profile_id,nickname,profile_visible FROM accounts "
                "WHERE banned=0 AND public_profile_id IN (%s)" %
                ",".join("?" for _ in ids), ids).fetchall()
        current = {row["public_profile_id"]: row for row in rows}
        result = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            account = current.get(entry.get("public_id"))
            if account is None:
                continue
            result.append({"rank": entry["rank"], "score": entry["score"],
                           "public_id": account["public_profile_id"],
                           "nickname": self._leaderboard_nickname(account["nickname"]),
                           "profile_visible": bool(account["profile_visible"])})
        return result

    @staticmethod
    def _shop_item_state(item, override, day):
        selling = bool(override["selling"]) if override else True
        reason = "selling"
        if item.get("locked"):
            reason = "locked"
        elif item.get("kind") not in ("title", "decoration", "lottery"):
            reason = "unsupported"
        elif item.get("kind") == "decoration" and not (
                item.get("cloud_only")
                and item.get("appearance") in ("comment_frame", "avatar_frame")
                and item.get("asset_source") == "builtin"):
            reason = "unsupported"
        elif item.get("limited_until") and day > item["limited_until"]:
            reason = "expired"
        elif not selling:
            reason = "admin_delisted"
        return dict(item, selling=selling, available=reason == "selling",
                    manageable=item.get("kind") in ("title", "decoration"),
                    availability_reason=reason,
                    revision=int(override["revision"]) if override else 0)

    def shop_items(self):
        """Effective cloud shelf; includes unavailable definitions for owned previews."""
        import slg_titles
        day = datetime.fromtimestamp(self.now(), CHINA).date().isoformat()
        with self._db() as db:
            overrides = {row["item_id"]: row for row in
                         db.execute("SELECT * FROM shop_availability")}
        return {"items": [self._shop_item_state(item, overrides.get(item["id"]), day)
                          for item in slg_titles.SHOP_ITEMS], "day": day}

    def admin_shop_items(self):
        import slg_titles
        day = datetime.fromtimestamp(self.now(), CHINA).date().isoformat()
        with self._db() as db:
            # One read transaction keeps revision and audit history consistent.
            db.execute("BEGIN")
            overrides = {row["item_id"]: row for row in
                         db.execute("SELECT * FROM shop_availability")}
            items = []
            for item in slg_titles.SHOP_ITEMS:
                override = overrides.get(item["id"])
                state = self._shop_item_state(item, override, day)
                state.update(reason=override["reason"] if override else "",
                             updated=override["updated"] if override else None,
                             history=[{"ts": row["ts"],
                                       "details": json.loads(row["details"])}
                                      for row in db.execute(
                                          "SELECT ts,details FROM admin_audit "
                                          "WHERE action='shop_availability' AND target=? "
                                          "ORDER BY id DESC LIMIT 20", (item["id"],))])
                items.append(state)
        return {"items": items, "day": day}

    def admin_set_shop_availability(self, item_id, selling, reason, expected_revision):
        """Compare-and-set a sales override without altering definitions or ownership."""
        import slg_titles
        if not isinstance(item_id, str):
            raise CloudError("invalid item")
        item = slg_titles.shop_item_by_id(item_id)
        if item is None:
            raise CloudError("unknown item")
        if item.get("kind") not in ("title", "decoration"):
            raise CloudError("item is not an ordinary shop product")
        if type(selling) is not bool:
            raise CloudError("invalid selling")
        if type(expected_revision) is not int or expected_revision < 0:
            raise CloudError("invalid revision")
        if not isinstance(reason, str):
            raise CloudError("invalid reason")
        reason = reason.strip()
        try:
            reason_bytes = len(reason.encode("utf-8"))
        except UnicodeError:
            raise CloudError("invalid reason")
        if not reason or len(reason) > 300 or reason_bytes > 1200:
            raise CloudError("invalid reason")
        self._writable()
        now = self.now()
        day = datetime.fromtimestamp(now, CHINA).date().isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM shop_availability WHERE item_id=?",
                                  (item_id,)).fetchone()
            revision = int(previous["revision"]) if previous else 0
            if revision != expected_revision:
                raise CloudError("shop revision conflict", 409)
            db.execute("INSERT INTO shop_availability(item_id,selling,revision,reason,updated) "
                       "VALUES (?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET "
                       "selling=excluded.selling,revision=excluded.revision,"
                       "reason=excluded.reason,updated=excluded.updated",
                       (item_id, int(selling), revision + 1, reason, now))
            self._append_admin_audit(db, "shop_availability", item_id,
                                     {"selling": selling, "previous_selling":
                                      bool(previous["selling"]) if previous else True,
                                      "revision": revision + 1, "reason": reason}, now)
            state = self._shop_item_state(item, {"selling": selling,
                                                "revision": revision + 1}, day)
            state.update(reason=reason, updated=now)
        return {"ok": True, "item": state}

    def buy(self, aid, item_id):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot use the ordinary shop", 403)
        import slg_titles
        item = next((i for i in slg_titles.SHOP_ITEMS if i["id"] == item_id), None)
        if (not item or item.get("locked")
                or item.get("kind") not in ("title", "decoration")):
            raise CloudError("item unavailable", 404)
        if item.get("kind") == "decoration" and not (
                item.get("cloud_only")
                and item.get("appearance") in ("comment_frame", "avatar_frame")
                and item.get("asset_source") == "builtin"):
            raise CloudError("item unavailable", 404)
        today = datetime.fromtimestamp(self.now(), CHINA).date().isoformat()
        if item.get("limited_until") and today > item["limited_until"]:
            raise CloudError("item unavailable", 404)
        cost = int(item["cost"])
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            override = db.execute("SELECT * FROM shop_availability WHERE item_id=?",
                                  (item_id,)).fetchone()
            day = datetime.fromtimestamp(self.now(), CHINA).date().isoformat()
            if not self._shop_item_state(item, override, day)["available"]:
                raise CloudError("item unavailable", 404)
            if db.execute("SELECT 1 FROM owned_titles WHERE account_id=? AND title_id=?",
                          (aid, item_id)).fetchone():
                raise CloudError("already owned", 409)
            if self._balance(db, aid) < cost:
                raise CloudError("insufficient points", 409)
            now = self.now()
            db.execute("INSERT INTO owned_titles VALUES (?,?,?,?)", (aid, item_id, "shop", now))
            purchase = db.execute("INSERT INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                       (aid, -cost, "shop", "shop:%s:%s" % (aid, item_id), now))
            self._stamp_business_platform(db, 'ledger', purchase.lastrowid)
            return {"ok": True, "item_id": item_id, "balance": self._balance(db, aid),
                    "titles": self._titles(db, aid)}

    def lottery(self, aid, request_id=None):
        self._writable()
        import slg_titles
        request_id = str(request_id or "").strip().lower()
        if request_id and (len(request_id) != 32 or
                           any(c not in "0123456789abcdef" for c in request_id)):
            raise CloudError("invalid lottery request", 400)
        now = self.now()
        day = datetime.fromtimestamp(now, CHINA).date().isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            if request_id:
                previous = db.execute(
                    "SELECT day,kind,value,result_json FROM lottery_draws "
                    "WHERE account_id=? AND request_id=?",
                    (aid, request_id)).fetchone()
                if previous:
                    try:
                        prize = json.loads(previous["result_json"] or "")
                    except (TypeError, ValueError):
                        prize = {"kind": previous["kind"],
                                 "value": previous["value"]}
                    count = db.execute(
                        "SELECT COUNT(*) FROM lottery_draws WHERE account_id=? AND day=?",
                        (aid, day)).fetchone()[0]
                    return {"ok": True, "prize": prize,
                            "balance": self._balance(db, aid),
                            "titles": self._titles(db, aid), "draws_today": count}
            if aid == DEVELOPER_ACCOUNT_ID:
                raise CloudError("developer account cannot use ordinary lottery", 403)
            count = db.execute("SELECT COUNT(*) FROM lottery_draws WHERE account_id=? AND day=?",
                               (aid, day)).fetchone()[0]
            if count >= slg_titles.LOTTERY_DAILY_LIMIT:
                raise CloudError("daily limit", 409)
            if self._balance(db, aid) < slg_titles.LOTTERY_COST:
                raise CloudError("insufficient points", 409)
            draws = db.execute("SELECT kind FROM lottery_draws WHERE account_id=? ORDER BY ts DESC,id DESC LIMIT ?",
                               (aid, slg_titles.LOTTERY_PITY)).fetchall()
            pity = 0
            for draw in draws:
                if draw[0] == "title":
                    break
                pity += 1
            if pity + 1 >= slg_titles.LOTTERY_PITY:
                prize = {"kind": "title", "value": slg_titles.LOTTERY_GRAND_TITLE}
            else:
                roll = secrets.randbelow(slg_titles.LOTTERY_WEIGHT_TOTAL) + 1
                cumulative = 0
                prize = None
                for weight, kind, value in slg_titles.LOTTERY_PRIZES:
                    cumulative += weight
                    if roll <= cumulative:
                        prize = {"kind": kind, "value": value}
                        break
                if prize is None:
                    prize = {"kind": "points", "value": 0}
            db.execute(
                "INSERT INTO lottery_draws(account_id,day,kind,value,ts,request_id) "
                "VALUES (?,?,?,?,?,?)",
                (aid, day, prize["kind"], str(prize["value"]), now,
                 request_id or None))
            draw_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._stamp_business_platform(db, 'lottery', draw_id)
            db.execute("INSERT INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                       (aid, -slg_titles.LOTTERY_COST, "lottery", "lottery:cost:%d" % draw_id, now))
            if prize["kind"] == "points":
                points = int(prize["value"])
                if points:
                    db.execute("INSERT INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                               (aid, points, "lottery_prize", "lottery:prize:%d" % draw_id, now))
            else:
                title = str(prize["value"])
                added = db.execute("INSERT OR IGNORE INTO owned_titles VALUES (?,?,?,?)",
                                   (aid, title, "lottery", now)).rowcount
                if not added:
                    points = int(slg_titles.duplicate_points(title))
                    prize = dict(prize, duplicate=True, points=points)
                    db.execute("INSERT INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                               (aid, points, "lottery_duplicate", "lottery:prize:%d" % draw_id, now))
            db.execute("UPDATE lottery_draws SET result_json=? WHERE id=?",
                       (json.dumps(prize, ensure_ascii=False, separators=(",", ":")),
                        draw_id))
            return {"ok": True, "prize": prize, "balance": self._balance(db, aid),
                    "titles": self._titles(db, aid), "draws_today": count + 1}

    @staticmethod
    def _quest_period_key(ts, period):
        """Return the 04:00 China-local task day or Monday-based week key."""
        task_day = (datetime.fromtimestamp(int(ts), CHINA)
                    - timedelta(hours=4)).date()
        if period == "daily":
            return task_day.isoformat()
        week_start = task_day - timedelta(days=task_day.weekday())
        return week_start.isoformat()

    @staticmethod
    def _quest_period_bounds(period_key, period):
        start_day = datetime.fromisoformat(period_key).date()
        if period == "weekly":
            end_day = start_day + timedelta(days=7)
        else:
            end_day = start_day + timedelta(days=1)
        start = datetime.combine(start_day, datetime.min.time(), CHINA) \
            + timedelta(hours=4)
        end = datetime.combine(end_day, datetime.min.time(), CHINA) \
            + timedelta(hours=4)
        return int(start.timestamp()), int(end.timestamp())

    def _migrate_legacy_quest_claims(self, db):
        """Map old natural-period quest ledger rows onto 04:00 task periods.

        Historical ledger rows are immutable.  The primary key on the mapping
        table collapses multiple old rewards in one new period to one claimed
        marker, while preserving every original credit and claim key.
        """
        rows = db.execute(
            "SELECT account_id,claim_key,ts FROM ledger "
            "WHERE reason='quest' ORDER BY id").fetchall()
        for row in rows:
            aid = row["account_id"]
            claim_key = row["claim_key"]
            for quest_id, period in QUEST_PERIODS.items():
                prefix = "quest:%s:%s:" % (quest_id, aid)
                if not claim_key.startswith(prefix):
                    continue
                # Only migrate the old YYYY-MM-DD key form.  Versioned keys
                # and malformed rows are intentionally left untouched.
                legacy_period_key = claim_key[len(prefix):]
                try:
                    if (len(legacy_period_key) != 10
                            or datetime.fromisoformat(legacy_period_key).date().isoformat()
                            != legacy_period_key):
                        continue
                    source_ts = int(row["ts"])
                    period_key = self._quest_period_key(source_ts, period)
                except (OverflowError, OSError, TypeError, ValueError):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO quest_claim_periods "
                    "(account_id,quest_id,period_key,source_claim_key,source_ts) "
                    "VALUES (?,?,?,?,?)",
                    (aid, quest_id, period_key, claim_key, source_ts))
                break

    def _quest_snapshot(self, db, aid, now):
        day_key = self._quest_period_key(now, "daily")
        week_key = self._quest_period_key(now, "weekly")
        day_start_ts, day_end_ts = self._quest_period_bounds(day_key, "daily")
        start_ts, end_ts = self._quest_period_bounds(week_key, "weekly")
        lottery_progress = db.execute(
            "SELECT COUNT(*) FROM lottery_draws WHERE account_id=? AND ts>=? AND ts<?",
            (aid, day_start_ts, day_end_ts)).fetchone()[0]
        daily_public_comment_progress = db.execute(
            "SELECT COUNT(*) FROM comments WHERE account_id=? "
            "AND status='approved' AND COALESCE(approved_ts,ts)>=? "
            "AND COALESCE(approved_ts,ts)<?",
            (aid, day_start_ts, day_end_ts)).fetchone()[0]
        weekly_comment_progress = db.execute(
            "SELECT COUNT(*) FROM comments WHERE account_id=? "
            "AND status='approved' AND COALESCE(approved_ts,ts)>=? "
            "AND COALESCE(approved_ts,ts)<?",
            (aid, start_ts, end_ts)).fetchone()[0]
        weekly_signin_progress = db.execute(
            "SELECT COUNT(DISTINCT substr(claim_key,-10)) FROM ledger "
            "WHERE account_id=? AND reason='signin' AND ts>=? AND ts<?",
            (aid, start_ts, end_ts)).fetchone()[0]
        developer = aid == DEVELOPER_ACCOUNT_ID
        definitions = [
            ("daily_lottery", "\u6bcf\u65e5\u62bd\u5956\u4e00\u6b21", "daily", day_key,
             lottery_progress, 1, 5),
            ("daily_public_comment", "\u53d1\u5e03\u4e00\u6761\u516c\u5f00\u8bc4\u8bba", "daily", day_key,
             daily_public_comment_progress, 1, 15),
            ("weekly_comments_3", "\u7d2f\u8ba1\u53d1\u5e03\u4e09\u6761\u516c\u5f00\u8bc4\u8bba", "weekly", week_key,
             weekly_comment_progress, 3, 30),
            ("weekly_signin_5", "\u7d2f\u8ba1\u767b\u5f555\u5929", "weekly", week_key,
             weekly_signin_progress, 5, 30),
        ]
        tasks = []
        for quest_id, label, period, period_key, progress, target, reward in definitions:
            claimed = db.execute(
                "SELECT 1 FROM quest_claim_periods "
                "WHERE account_id=? AND quest_id=? AND period_key=?",
                (aid, quest_id, period_key)).fetchone() is not None
            progress = min(int(progress), target)
            tasks.append({
                "id": quest_id,
                "label": label,
                "period": period,
                "progress": progress,
                "target": target,
                "reward": reward,
                "claimable": (not developer and not claimed
                              and progress >= target),
                "claimed": claimed,
            })
        return {"timezone": "Asia/Shanghai", "day": day_key,
                "week": week_key, "tasks": tasks}

    def quests(self, aid):
        now = int(self.now())
        with self._db() as db:
            self._account(db, aid)
            return self._quest_snapshot(db, aid, now)

    def record_quest_rating(self, aid, game_id, rating):
        """Record one low-trust client rating signal per account and China day."""
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account is excluded from quests", 403)
        if (isinstance(game_id, bool) or not isinstance(game_id, int)
                or game_id <= 0 or isinstance(rating, bool)
                or not isinstance(rating, int) or not 1 <= rating <= 5):
            raise CloudError("bad rating")
        now = int(self.now())
        day_key = datetime.fromtimestamp(now, CHINA).date().isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            cur = db.execute(
                "INSERT OR IGNORE INTO quest_ratings "
                "(account_id,day,game_id,rating,ts) VALUES (?,?,?,?,?)",
                (aid, day_key, game_id, rating, now))
            return {"ok": True, "recorded": bool(cur.rowcount),
                    "already": not bool(cur.rowcount), "day": day_key,
                    "low_trust": True}

    def claim_quest(self, aid, quest_id):
        self._writable()
        if not isinstance(quest_id, str):
            raise CloudError("invalid quest")
        now = int(self.now())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            if aid == DEVELOPER_ACCOUNT_ID:
                raise CloudError("developer account is excluded from quests", 403)
            state = self._quest_snapshot(db, aid, now)
            task = next((item for item in state["tasks"]
                         if item["id"] == quest_id), None)
            if task is None:
                raise CloudError("invalid quest")
            period_key = state["day"] if task["period"] == "daily" else state["week"]
            claim_key = "quest:v2:%s:%s:%s" % (quest_id, aid, period_key)
            prior = db.execute(
                "SELECT 1 FROM ledger WHERE account_id=? AND claim_key=?",
                (aid, claim_key)).fetchone()
            if prior:
                db.execute(
                    "INSERT OR IGNORE INTO quest_claim_periods "
                    "(account_id,quest_id,period_key,source_claim_key,source_ts) "
                    "VALUES (?,?,?,?,?)",
                    (aid, quest_id, period_key, claim_key, now))
                return {"ok": True, "quest_id": quest_id, "claimed": True,
                        "already": True, "gained": 0,
                        "balance": self._balance(db, aid), "period": task["period"],
                        "period_key": period_key}
            if task["claimed"]:
                # A pre-04:00 reward can be present only in the legacy ledger.
                # Its migrated period marker is still a successful prior claim.
                return {"ok": True, "quest_id": quest_id, "claimed": True,
                        "already": True, "gained": 0,
                        "balance": self._balance(db, aid), "period": task["period"],
                        "period_key": period_key}
            if not task["claimable"]:
                raise CloudError("quest not claimable", 409)
            cur = db.execute(
                "INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) "
                "VALUES (?,?,?,?,?)",
                (aid, task["reward"], "quest", claim_key, now))
            db.execute(
                "INSERT OR IGNORE INTO quest_claim_periods "
                "(account_id,quest_id,period_key,source_claim_key,source_ts) "
                "VALUES (?,?,?,?,?)",
                (aid, quest_id, period_key, claim_key, now))
            return {"ok": True, "quest_id": quest_id, "claimed": bool(cur.rowcount),
                    "already": not bool(cur.rowcount),
                    "gained": task["reward"] if cur.rowcount else 0,
                    "balance": self._balance(db, aid), "period": task["period"],
                    "period_key": period_key}

    def _grant_comment(self, db, aid, cid, game):
        now = self.now()
        if db.execute("SELECT 1 FROM comments c JOIN ledger l ON l.claim_key='comment:'||c.id "
                      "WHERE c.account_id=? AND c.game=? AND l.reason='comment' AND l.ts>? "
                      "AND NOT EXISTS (SELECT 1 FROM ledger r WHERE r.claim_key='comment:reversal:'||c.id) LIMIT 1",
                      (aid, game, now - 30 * 86400)).fetchone():
            return 0
        if db.execute("SELECT COUNT(*) FROM ledger l WHERE l.account_id=? AND l.reason='comment' AND l.ts>? "
                      "AND NOT EXISTS (SELECT 1 FROM ledger r WHERE r.claim_key='comment:reversal:'||substr(l.claim_key,9))",
                      (aid, now - 7 * 86400)).fetchone()[0] >= 3:
            return 0
        cur = db.execute("INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                         (aid, 20, "comment", "comment:" + cid, now))
        if cur.rowcount:
            db.execute("UPDATE comments SET rewarded=1 WHERE id=?", (cid,))
            return 20
        return 0

    @staticmethod
    def _web_comment_operation(game, content, operation_id):
        if operation_id is None:
            return None
        if not isinstance(operation_id, str):
            raise CloudError("invalid operation")
        try:
            key = uuid.UUID(operation_id).hex
        except (ValueError, AttributeError):
            raise CloudError("invalid operation")
        digest = _hash(json.dumps([game, content], ensure_ascii=False))
        return key, digest

    @staticmethod
    def _operation_target(db, aid, kind, operation):
        if operation is None:
            return None
        row = db.execute(
            "SELECT request_hash,target_id FROM web_comment_operations "
            "WHERE account_id=? AND kind=? AND operation_id=?",
            (aid, kind, operation[0])).fetchone()
        if row is not None and row["request_hash"] != operation[1]:
            raise CloudError("operation conflict", 409)
        return row["target_id"] if row else None

    @staticmethod
    def _record_comment_operation(db, aid, kind, operation, target_id, now):
        if operation:
            if (db.execute("SELECT COUNT(*) FROM web_comment_operations WHERE account_id=? "
                           "AND kind=? AND ts>?", (aid, kind, now - 60)).fetchone()[0] >= 10
                    or db.execute("SELECT COUNT(*) FROM web_comment_operations WHERE account_id=? "
                                  "AND kind=? AND ts>?", (aid, kind, now - 86400)).fetchone()[0] >= 200):
                raise CloudError("rate limited", 429)
            if db.execute("SELECT COUNT(*) FROM web_comment_operations WHERE account_id=?",
                          (aid,)).fetchone()[0] >= 20000:
                raise CloudError("submission history storage limit", 429)
            db.execute("INSERT INTO web_comment_operations VALUES (?,?,?,?,?,?)",
                       (aid, kind, operation[0], operation[1], target_id, now))

    def personal_comments_page(self, aid, game="", game_slugs=None, page=1, limit=20):
        try:
            page, limit = int(page), int(limit)
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination")
        if not 1 <= page <= 1000 or not 1 <= limit <= 50:
            raise CloudError("bad pagination")
        where, params = "account_id=?", [aid]
        if game:
            slugs = list(dict.fromkeys(game_slugs or [game]))
            where += " AND game IN (%s)" % ",".join("?" for _ in slugs)
            params.extend(slugs)
        with self._db() as db:
            db.execute("BEGIN")
            self._account(db, aid)
            total = db.execute("SELECT COUNT(*) FROM personal_game_comments WHERE " + where,
                               params).fetchone()[0]
            rows = db.execute("SELECT id,game,content,ts FROM personal_game_comments WHERE "
                              + where + " ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?",
                              params + [limit, (page - 1) * limit]).fetchall()
        return {"ok": True, "items": [self._personal_comment(row) for row in rows],
                "page": page, "limit": limit, "total": total,
                "total_pages": (total + limit - 1) // limit}

    @staticmethod
    def _personal_comment(row):
        return {"id": row["id"], "game": row["game"], "content": row["content"],
                "ts": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat()}

    def post_personal_comment(self, aid, game, content, operation_id):
        self._writable()
        if (not isinstance(game, str) or not game or len(game) > 200
                or not isinstance(content, str)):
            raise CloudError("bad fields")
        content = content.strip()
        if (not 1 <= len(content) <= 500 or len(content.encode("utf-8")) > 2048
                or any(ord(c) < 32 and c not in "\n\r\t" for c in content)):
            raise CloudError("invalid comment length")
        operation = self._web_comment_operation(game, content, operation_id)
        if operation is None:
            raise CloudError("invalid operation")
        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            target = self._operation_target(db, aid, "personal", operation)
            if target:
                row = db.execute("SELECT * FROM personal_game_comments WHERE id=? AND account_id=?",
                                 (target, aid)).fetchone()
                if row is None:
                    raise CloudError("comment deleted", 409)
                return {"ok": True, **self._personal_comment(row), "duplicate": True}
            if db.execute("SELECT COUNT(*) FROM web_comment_operations WHERE account_id=? "
                          "AND kind='personal' AND ts>?", (aid, now - 60)).fetchone()[0] >= 10:
                raise CloudError("rate limited", 429)
            if db.execute("SELECT COUNT(*) FROM personal_game_comments WHERE account_id=?",
                          (aid,)).fetchone()[0] >= 10000:
                raise CloudError("personal comment storage limit", 429)
            cid = uuid.uuid4().hex[:20]
            db.execute("INSERT INTO personal_game_comments VALUES (?,?,?,?,?)",
                       (cid, aid, game, content, now))
            self._record_comment_operation(db, aid, "personal", operation, cid, now)
            row = db.execute("SELECT * FROM personal_game_comments WHERE id=?", (cid,)).fetchone()
            return {"ok": True, **self._personal_comment(row), "duplicate": False}

    def delete_personal_comment(self, aid, cid):
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            if not db.execute("DELETE FROM personal_game_comments WHERE account_id=? AND id=?",
                              (aid, cid)).rowcount:
                raise CloudError("not found", 404)
        return {"ok": True, "id": cid}

    def _public_profile(self, db, account, appearances=None):
        """Whitelist public fields; never serialize inventory or local avatars."""
        import slg_titles
        result = {"public_id": account["public_profile_id"],
                  "nickname": self._leaderboard_nickname(account["nickname"]),
                  "profile_visible": bool(account["profile_visible"])}
        if result["profile_visible"]:
            title = slg_titles.title_by_id(account["equipped_title"]) or {}
            if appearances is None:
                appearances = self._appearance_map(db, account["id"])
            appearances = {slot: item if self._builtin_appearance(item, slot) else ""
                           for slot, item in appearances.items()}
            result.update(equipped_title=title.get("id", ""),
                          equipped_appearances=appearances,
                          equipped_cosmetic=appearances["comment_frame"],
                          profile_message=account["profile_message"])
        return result

    @staticmethod
    def _public_author_fields(profile):
        import slg_titles
        title_id = profile.get("equipped_title", "")
        title = slg_titles.title_by_id(title_id) or {}
        return {"author_public_id": profile["public_id"],
                "author_profile_visible": profile["profile_visible"],
                "author_title_id": title_id,
                "author_title_name": title.get("name", ""),
                "author_cosmetic": profile.get("equipped_cosmetic", ""),
                "author_appearances": profile.get("equipped_appearances",
                    {"avatar_frame": "", "comment_frame": ""}),
                "profile_message": profile.get("profile_message", "")}

    def public_profile(self, public_id):
        if not isinstance(public_id, str) or not public_id or len(public_id) > 80:
            raise CloudError("profile not found", 404)
        with self._db() as db:
            db.execute("BEGIN")
            account = db.execute("SELECT * FROM accounts WHERE public_profile_id=?",
                                 (public_id,)).fetchone()
            if not account:
                raise CloudError("profile not found", 404)
            return {"ok": True, "profile": self._public_profile(db, account)}

    def public_author_profiles(self, account_ids):
        """One fresh read snapshot per bounded forum page, without private IDs."""
        ids = list(dict.fromkeys(account_ids))
        if not ids:
            return {}
        if len(ids) > 50:
            raise CloudError("too many authors")
        marks = ",".join("?" for _ in ids)
        with self._db() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT * FROM accounts WHERE id IN (%s)" % marks,
                              ids).fetchall()
            frames = db.execute(
                "SELECT account_id,slot,item_id FROM account_appearances "
                "WHERE account_id IN (%s)" % marks, ids).fetchall()
            appearances = {account["id"]: {"comment_frame": "", "avatar_frame": ""}
                           for account in rows}
            for frame in frames:
                slots = appearances.get(frame["account_id"], {})
                if frame["slot"] in slots:
                    slots[frame["slot"]] = frame["item_id"]
            result = {}
            for account in rows:
                profile = self._public_profile(db, account, appearances[account["id"]])
                fields = self._public_author_fields(profile)
                fields.update(author_name=profile["nickname"], nickname=profile["nickname"])
                result[account["id"]] = fields
            return result


    # Replies deliberately have no reward, title, vote or edit integration.
    @staticmethod
    def _reply_pagination(page, limit):
        try:
            if isinstance(page, bool) or isinstance(limit, bool):
                raise ValueError()
            page, limit = int(page), int(limit)
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination")
        if not 1 <= page <= 1000000 or not 1 <= limit <= 20:
            raise CloudError("bad pagination")
        return page, limit

    @staticmethod
    def _reply_root(db, root_id):
        row = db.execute("SELECT * FROM comments WHERE id=? AND status='approved'", (root_id,)).fetchone()
        if row is None:
            raise CloudError("discussion unavailable", 404)
        return row

    def _reply_page_context(self, db, rows):
        """Batch one bounded reply page inside its caller's read snapshot."""
        if not rows:
            return {"profiles": {}, "games": {}, "targets": {}}
        authors = list(dict.fromkeys(row["account_id"] for row in rows))
        roots = list(dict.fromkeys(row["root_id"] for row in rows))
        target_ids = list(dict.fromkeys(row["reply_to_id"] for row in rows if row["reply_to_id"]))
        author_marks = ",".join("?" for _ in authors)
        root_marks = ",".join("?" for _ in roots)
        accounts = db.execute("SELECT * FROM accounts WHERE id IN (%s)" % author_marks,
                              authors).fetchall()
        frames = db.execute("SELECT account_id,slot,item_id FROM account_appearances "
                            "WHERE account_id IN (%s)" % author_marks, authors).fetchall()
        appearances = {account["id"]: {"comment_frame": "", "avatar_frame": ""}
                       for account in accounts}
        for frame in frames:
            slots = appearances.get(frame["account_id"], {})
            if frame["slot"] in slots:
                slots[frame["slot"]] = frame["item_id"]
        profiles = {account["id"]: self._public_profile(db, account, appearances[account["id"]])
                    for account in accounts}
        games = {row["id"]: row["game"] for row in db.execute(
            "SELECT id,game FROM comments WHERE id IN (%s)" % root_marks, roots)}
        targets = {}
        if target_ids:
            target_marks = ",".join("?" for _ in target_ids)
            targets = {(row["root_id"], row["id"]): self._leaderboard_nickname(row["nickname"])
                       for row in db.execute(
                           "SELECT r.id,r.root_id,a.nickname FROM comment_replies r "
                           "JOIN accounts a ON a.id=r.account_id "
                           "WHERE r.id IN (%s) AND r.root_id IN (%s)" % (target_marks, root_marks),
                           target_ids + roots)}
        return {"profiles": profiles, "games": games, "targets": targets}

    def _reply_row(self, db, row, *, own=False, admin=False, page_context=None):
        if page_context is None:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
            profile = self._public_profile(db, account) if account else {}
            target = db.execute("SELECT a.nickname FROM comment_replies r JOIN accounts a ON a.id=r.account_id WHERE r.id=? AND r.root_id=?",
                                (row["reply_to_id"], row["root_id"])).fetchone() if row["reply_to_id"] else None
            root = db.execute("SELECT game FROM comments WHERE id=?", (row["root_id"],)).fetchone()
            game = root["game"] if root else ""
            target_nickname = self._leaderboard_nickname(target[0]) if target else None
        else:
            profile = page_context["profiles"].get(row["account_id"], {})
            game = page_context["games"].get(row["root_id"], "")
            target_nickname = page_context["targets"].get((row["root_id"], row["reply_to_id"]))
        item = {"id": row["id"], "root_id": row["root_id"], "game": game,
                "content": row["content"], "created": self._feedback_timestamp(row["created"]),
                "status": row["status"], "nickname": profile.get("nickname", "匿名用户"),
                "author_public_id": profile.get("public_id", ""),
                "author_profile_visible": profile.get("profile_visible", False),
                "equipped_title": profile.get("equipped_title", ""),
                "equipped_appearances": dict(profile.get("equipped_appearances", {"avatar_frame": "", "comment_frame": ""})),
                "profile_message": profile.get("profile_message", ""),
                "reply_to_id": row["reply_to_id"], "reply_to_nickname": target_nickname}
        if own:
            item.update(owned=True, reason=row["reason"])
        if admin:
            item.update(account_id=row["account_id"], reason=row["reason"],
                        moderation_source=row["moderation_source"], rule_version=row["rule_version"],
                        reports=db.execute("SELECT COUNT(*) FROM comment_reply_reports WHERE reply_id=?", (row["id"],)).fetchone()[0])
            item["moderation_history"] = [dict(r) for r in db.execute(
                "SELECT decision,reason,created,source FROM comment_reply_decisions WHERE reply_id=? ORDER BY id", (row["id"],))]
        return item

    def _approve_reply_notification(self, db, row):
        # Called under the same write transaction as FIRST approval. Mark first
        # approval even for self-replies / deleted recipients to prevent replay.
        if row["approved_ts"] is not None:
            return
        root = self._reply_root(db, row["root_id"])
        recipient = root["account_id"]
        if row["reply_to_id"]:
            target = db.execute("SELECT account_id FROM comment_replies WHERE id=? AND root_id=? AND status='approved'",
                                (row["reply_to_id"], row["root_id"])).fetchone()
            if target is None:
                raise CloudError("reply target unavailable", 409)
            recipient = target[0]
        now = self.now()
        db.execute("UPDATE comment_replies SET approved_ts=? WHERE id=?", (now, row["id"]))
        if recipient != row["account_id"] and db.execute("SELECT 1 FROM accounts WHERE id=? AND banned=0", (recipient,)).fetchone():
            db.execute("INSERT OR IGNORE INTO account_notifications(id,account_id,kind,root_id,reply_id,created) VALUES (?,?,'comment_reply',?,?,?)",
                       (uuid.uuid4().hex, recipient, row["root_id"], row["id"], now))

    def post_comment_reply(self, aid, root_id, content, moderation, *, reply_to_id=None, operation_id=None):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot post ordinary comments", 403)
        content = self._feedback_text(content, 500, "reply")
        if len(content) < 2 or len(content.encode("utf-8")) > 2048:
            raise CloudError("invalid reply length")
        if not isinstance(operation_id, str):
            raise CloudError("invalid operation_id")
        try:
            operation_id = str(uuid.UUID(operation_id))
        except (ValueError, AttributeError):
            raise CloudError("invalid operation_id")
        if reply_to_id is not None and (not isinstance(reply_to_id, str) or not reply_to_id or len(reply_to_id) > 128):
            raise CloudError("invalid reply target")
        digest = hashlib.sha256(json.dumps([root_id, content, reply_to_id], ensure_ascii=False).encode()).hexdigest()
        verdict = moderation(content)
        status = {"approve": "approved", "review": "pending", "reject": "rejected"}.get(verdict.get("decision"), "pending")
        reason = str(verdict.get("reason", ""))[:200]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            prior = db.execute("SELECT * FROM comment_replies WHERE account_id=? AND operation_id=?", (aid, operation_id)).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise CloudError("operation conflict", 409)
                return {"ok": True, "reply": self._reply_row(db, prior, own=True), "replayed": True}
            self._reply_root(db, root_id)
            if reply_to_id and not db.execute("SELECT 1 FROM comment_replies WHERE id=? AND root_id=? AND status='approved'", (reply_to_id, root_id)).fetchone():
                raise CloudError("reply target unavailable", 404)
            now = self.now()
            if db.execute("SELECT COUNT(*) FROM comment_replies WHERE account_id=? AND created>?", (aid, now-600)).fetchone()[0] >= 5:
                raise CloudError("reply rate limited", 429)
            day_start = ((now + 8*3600)//86400)*86400 - 8*3600
            if db.execute("SELECT COUNT(*) FROM comment_replies WHERE account_id=? AND created>=?", (aid, day_start)).fetchone()[0] >= 30:
                raise CloudError("daily reply limit", 429)
            rid = uuid.uuid4().hex[:20]
            db.execute("INSERT INTO comment_replies(id,root_id,account_id,content,reply_to_id,status,reason,created,operation_id,request_hash,moderation_source,rule_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, root_id, aid, content, reply_to_id, status, reason, now, operation_id, digest,
                        str(verdict.get("source", "automatic"))[:40], str(verdict.get("rule_version", ""))[:100]))
            db.execute("INSERT INTO comment_reply_decisions(reply_id,decision,reason,created,source) VALUES (?,?,?,?,?)",
                       (rid, verdict.get("decision", "review"), reason, now, "automatic"))
            row = db.execute("SELECT * FROM comment_replies WHERE id=?", (rid,)).fetchone()
            if status == "approved":
                self._approve_reply_notification(db, row)
            return {"ok": True, "reply": self._reply_row(db, row, own=True), "replayed": False}

    def comment_replies(self, root_id, *, page=1, limit=20, focus_id=None, aid=None):
        page, limit = self._reply_pagination(page, limit)
        with self._db() as db:
            db.execute("BEGIN")
            self._reply_root(db, root_id)
            if aid is not None:
                self._account(db, aid)
            where = "root_id=? AND status='approved'"
            params = [root_id]
            if focus_id:
                focus = db.execute("SELECT * FROM comment_replies WHERE id=? AND " + where, [focus_id]+params).fetchone()
                if not focus:
                    raise CloudError("reply unavailable", 404)
                before = db.execute("SELECT COUNT(*) FROM comment_replies WHERE " + where + " AND (created<? OR (created=? AND id<?))", params+[focus["created"], focus["created"], focus_id]).fetchone()[0]
                page = before//limit+1
            total = db.execute("SELECT COUNT(*) FROM comment_replies WHERE " + where, params).fetchone()[0]
            rows = db.execute("SELECT * FROM comment_replies WHERE " + where + " ORDER BY created,id LIMIT ? OFFSET ?", params+[limit, (page-1)*limit]).fetchall()
            context = self._reply_page_context(db, rows)
            replies = [self._reply_row(db, row, own=bool(aid and row["account_id"] == aid), page_context=context) for row in rows]
        pages = (total+limit-1)//limit
        return {"ok": True, "root_id": root_id, "replies": replies, "page": page, "page_size": limit, "total_count": total, "total_pages": pages, "has_previous": page>1 and bool(pages), "has_next": page<pages}

    def account_comment_replies(self, aid, *, root_id="", page=1, limit=20):
        page, limit = self._reply_pagination(page, limit)
        with self._db() as db:
            db.execute("BEGIN")
            self._account(db, aid)
            # Private status is retained even if a root was deleted; no other
            # author's text or private status is included.
            where, params = "account_id=?", [aid]
            if root_id:
                where += " AND root_id=?"
                params.append(root_id)
            total = db.execute("SELECT COUNT(*) FROM comment_replies WHERE "+where, params).fetchone()[0]
            rows = db.execute("SELECT * FROM comment_replies WHERE "+where+" ORDER BY created DESC,id DESC LIMIT ? OFFSET ?", params+[limit, (page-1)*limit]).fetchall()
            context = self._reply_page_context(db, rows)
            return {"ok": True, "replies": [self._reply_row(db, r, own=True, page_context=context) for r in rows], "page": page, "page_size": limit, "total_count": total, "total_pages": (total+limit-1)//limit}

    def comment_reply_context(self, rid):
        with self._db() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM comment_replies WHERE id=? AND status='approved'", (rid,)).fetchone()
            if not row:
                raise CloudError("reply unavailable", 404)
            root = self._reply_root(db, row["root_id"])
            item = self._comment_row(db, root, include_profile_message=True)
            for key in ("account_id", "reason", "reports", "rewarded"):
                item.pop(key, None)
            before = db.execute("SELECT COUNT(*) FROM comment_replies WHERE root_id=? AND status='approved' AND (created<? OR (created=? AND id<?))", (row["root_id"], row["created"], row["created"], rid)).fetchone()[0]
            return {"ok": True, "game": root["game"], "root": item, "reply_id": rid, "page": before//20+1, "page_size": 20}

    def delete_comment_reply(self, aid, rid, *, admin=False):
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not admin:
                self._account(db, aid)
            row = db.execute("SELECT * FROM comment_replies WHERE id=?", (rid,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            if not admin and row["account_id"] != aid:
                raise CloudError("not allowed", 403)
            db.execute("UPDATE comment_replies SET status='deleted' WHERE id=?", (rid,))
            if admin:
                self._append_admin_audit(db, "reply_delete", rid, {}, self.now())
        return {"ok": True, "id": rid}

    def report_comment_reply(self, aid, rid):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot report comments", 403)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            row = db.execute("SELECT * FROM comment_replies WHERE id=? AND status='approved'", (rid,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            self._reply_root(db, row["root_id"])
            if row["account_id"] == aid:
                raise CloudError("own reply", 403)
            if db.execute("SELECT COUNT(*) FROM comment_reply_reports WHERE account_id=? AND created>?", (aid, self.now()-600)).fetchone()[0] >= 8:
                raise CloudError("rate limited", 429)
            if not db.execute("INSERT OR IGNORE INTO comment_reply_reports VALUES (?,?,?)", (aid, rid, self.now())).rowcount:
                raise CloudError("duplicate report", 409)
            mature = db.execute("SELECT COUNT(*) FROM comment_reply_reports r JOIN accounts a ON a.id=r.account_id WHERE r.reply_id=? AND a.created<=? AND a.banned=0 AND NOT EXISTS (SELECT 1 FROM comment_reply_report_baselines b WHERE b.reply_id=r.reply_id AND b.account_id=r.account_id)", (rid, self.now()-86400)).fetchone()[0]
            if mature >= 3:
                db.execute("UPDATE comment_replies SET status='hidden',reason='report threshold',moderation_source='feedback' WHERE id=?", (rid,))
                db.execute("INSERT INTO comment_reply_decisions(reply_id,decision,reason,created,source) VALUES (?,'hide','report threshold',?,'feedback')", (rid, self.now()))
            return {"ok": True, "hidden": mature>=3}

    @staticmethod
    def _visible_notifications_sql():
        return (" FROM account_notifications n JOIN comment_replies r ON r.id=n.reply_id "
                "JOIN comments c ON c.id=n.root_id JOIN accounts a ON a.id=r.account_id "
                "WHERE n.account_id=? AND r.status='approved' AND c.status='approved'")

    def account_notifications(self, aid, *, page=1, limit=20):
        page, limit = self._reply_pagination(page, limit)
        with self._db() as db:
            db.execute("BEGIN")
            self._account(db, aid)
            tail = self._visible_notifications_sql()
            total, unread = db.execute("SELECT COUNT(*),COALESCE(SUM(n.read_at IS NULL),0)"+tail, (aid,)).fetchone()
            rows = db.execute("SELECT n.*,c.game,a.nickname,a.public_profile_id"+tail+" ORDER BY n.created DESC,n.id DESC LIMIT ? OFFSET ?", (aid, limit, (page-1)*limit)).fetchall()
            return {"ok": True, "notifications": [{"id": r["id"], "kind": "comment_reply", "root_id": r["root_id"], "reply_id": r["reply_id"], "game": r["game"], "created": self._feedback_timestamp(r["created"]), "read": r["read_at"] is not None, "nickname": self._leaderboard_nickname(r["nickname"]), "author_public_id": r["public_profile_id"]} for r in rows], "unread_count": unread, "page": page, "page_size": limit, "total_count": total, "total_pages": (total+limit-1)//limit}

    def read_account_notifications(self, aid, *, ids=None, all=False):
        self._writable()
        if not isinstance(all, bool) or (all and ids is not None) or (not all and (not isinstance(ids, list) or len(ids)>100 or any(not isinstance(i,str) or len(i)>128 or not i for i in ids))):
            raise CloudError("bad notification selection")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            if all:
                db.execute("UPDATE account_notifications SET read_at=COALESCE(read_at,?) WHERE account_id=?", (self.now(), aid))
            elif ids:
                db.execute("UPDATE account_notifications SET read_at=COALESCE(read_at,?) WHERE account_id=? AND id IN (%s)" % ",".join("?" for _ in ids), [self.now(), aid]+ids)
            unread = db.execute("SELECT COUNT(*)"+self._visible_notifications_sql()+" AND n.read_at IS NULL", (aid,)).fetchone()[0]
            return {"ok": True, "unread_count": unread}

    def admin_comment_replies(self, *, status="", q="", limit=30, offset=0):
        try:
            limit, offset = int(limit), int(offset)
        except (ValueError, TypeError, OverflowError):
            raise CloudError("bad pagination")
        if not 1<=limit<=50 or not 0<=offset<=100000 or not isinstance(q,str) or len(q)>200 or status not in ("", "all", "pending", "approved", "rejected", "hidden", "deleted", "reported", "review"):
            raise CloudError("bad filter")
        clauses, params = [], []
        if status == "review":
            clauses.append("r.status IN ('pending','hidden')")
        elif status == "reported":
            clauses.append("EXISTS(SELECT 1 FROM comment_reply_reports p WHERE p.reply_id=r.id)")
        elif status not in ("", "all"):
            clauses.append("r.status=?"); params.append(status)
        if q:
            clauses.append("(instr(r.content,?)>0 OR instr(a.nickname,?)>0 OR instr(r.account_id,?)>0)"); params.extend([q,q,q])
        tail = " FROM comment_replies r LEFT JOIN accounts a ON a.id=r.account_id" + (" WHERE "+" AND ".join(clauses) if clauses else "")
        with self._db() as db:
            total = db.execute("SELECT COUNT(*)"+tail, params).fetchone()[0]
            rows = db.execute("SELECT r.*"+tail+" ORDER BY r.created DESC,r.id DESC LIMIT ? OFFSET ?", params+[limit,offset]).fetchall()
            return {"ok": True, "replies": [self._reply_row(db,r,admin=True) for r in rows], "total_count": total, "limit": limit, "offset": offset}

    def decide_comment_reply(self, rid, decision, reason=""):
        self._writable()
        if decision not in ("approve", "reject", "hide") or not isinstance(reason,str) or len(reason)>200:
            raise CloudError("bad decision")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM comment_replies WHERE id=?", (rid,)).fetchone()
            if not row:
                raise CloudError("not found",404)
            if row["status"] == "deleted":
                raise CloudError("reply deleted",409)
            if decision == "approve":
                self._reply_root(db,row["root_id"])
                self._approve_reply_notification(db,row)
                if row["status"] != "approved":
                    db.execute("INSERT OR IGNORE INTO comment_reply_report_baselines(account_id,reply_id) SELECT account_id,reply_id FROM comment_reply_reports WHERE reply_id=?", (rid,))
            status = {"approve":"approved", "reject":"rejected", "hide":"hidden"}[decision]
            db.execute("UPDATE comment_replies SET status=?,reason=?,moderation_source='manual' WHERE id=?",(status,reason,rid))
            db.execute("INSERT INTO comment_reply_decisions(reply_id,decision,reason,created,source) VALUES (?,?,?,?,'manual')",(rid,decision,reason,self.now()))
            self._append_admin_audit(db,"reply_moderation",rid,{"decision":decision,"status":status},self.now())
            return {"ok":True,"id":rid,"status":status}

    def post_comment(self, aid, game, content, nickname, moderation, *, operation_id=None):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot post ordinary comments", 403)
        if not all(isinstance(x, str) for x in (game, content, nickname)):
            raise CloudError("bad fields")
        game, content, nickname = game.strip(), content.strip(), nickname.strip()
        operation = self._web_comment_operation(game, content, operation_id)
        if not game or len(game) > 200 or not 20 <= len(content) <= 500 or len(content.encode("utf-8")) > 2048 or len(nickname) > 40:
            raise CloudError("invalid comment length")
        verdict = moderation(content)
        status = {"approve": "approved", "review": "pending", "reject": "rejected"}.get(
            verdict.get("decision"), "pending")
        reason = str(verdict.get("reason", ""))[:120]
        now = self.now()
        cid = uuid.uuid4().hex[:20]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, aid)
            nickname = account["nickname"]
            target = self._operation_target(db, aid, "public", operation)
            if target:
                prior = db.execute("SELECT id,status,rewarded FROM comments WHERE id=? AND account_id=?",
                                   (target, aid)).fetchone()
                if prior is None:
                    raise CloudError("operation conflict", 409)
                return {"ok": True, "id": prior["id"], "status": prior["status"],
                        "rewarded": bool(prior["rewarded"]), "gained": 0, "duplicate": True}
            prior = db.execute("SELECT id,status,rewarded FROM comments WHERE account_id=? AND game=? AND content=? AND ts>? LIMIT 1",
                               (aid, game, content, now - 30 * 86400)).fetchone()
            if prior:
                self._record_comment_operation(db, aid, "public", operation, prior["id"], now)
                return {"ok": True, "id": prior["id"], "status": prior["status"],
                        "rewarded": bool(prior["rewarded"]), "gained": 0,
                        "duplicate": True}
            if db.execute("SELECT COUNT(*) FROM comments WHERE account_id=? AND ts>?",
                          (aid, now - 600)).fetchone()[0] >= 3:
                raise CloudError("rate limited", 429)
            if db.execute("SELECT COUNT(*) FROM comments WHERE account_id=? AND ts>?",
                          (aid, now - 86400)).fetchone()[0] >= 20:
                raise CloudError("daily comment limit", 429)
            if db.execute("SELECT COUNT(*) FROM comments WHERE account_id=? AND game=? AND ts>?",
                          (aid, game, now - 86400)).fetchone()[0] >= 5:
                raise CloudError("game comment limit", 429)
            db.execute("INSERT INTO comments(id,account_id,game,content,nickname,status,reason,ts,approved_ts,original_reason,moderation_source,rule_version) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, aid, game, content, nickname, status, reason, now,
                        now if status == "approved" else None, reason,
                        verdict.get("source", "automatic"), verdict.get("rule_version", "")))
            self._comment_event(db, cid, verdict.get("decision", "review"), reason, now,
                                verdict.get("source", "automatic"), verdict.get("rule_version", ""))
            gained = self._grant_comment(db, aid, cid, game) if status == "approved" else 0
            if status == "approved":
                self._grant_connoisseur(db, aid)
            self._record_comment_operation(db, aid, "public", operation, cid, now)
        return {"ok": True, "id": cid, "status": status, "rewarded": bool(gained), "gained": gained}

    def _grant_connoisseur(self, db, aid):
        """Award the existing public-review title from server-owned reviews."""
        import slg_titles
        count = db.execute(
            "SELECT COUNT(*) FROM comments WHERE account_id=? AND status='approved'",
            (aid,)).fetchone()[0]
        if count >= slg_titles.CONNOISSEUR_NEED:
            db.execute("INSERT OR IGNORE INTO owned_titles VALUES (?,?,?,?)",
                       (aid, "connoisseur", "public_comments", self.now()))

    def _comment_row(self, db, row, include_profile_message=False, admin=False, private=False):
        cid = row["id"]
        votes = db.execute("SELECT COALESCE(SUM(value=1),0),COALESCE(SUM(value=-1),0) FROM votes WHERE comment_id=?",
                           (cid,)).fetchone()
        reports = db.execute("SELECT COUNT(*) FROM reports WHERE comment_id=?", (cid,)).fetchone()[0]
        account = db.execute(
            "SELECT * FROM accounts WHERE id=?",
            (row["account_id"],)).fetchone()
        nickname = ((account["nickname"] if account else "") or row["nickname"])
        title_id = account["equipped_title"] if account else ""
        appearances = (self._appearance_map(db, row["account_id"])
                       if account else {"comment_frame": "", "avatar_frame": ""})
        cosmetic_id = (appearances["comment_frame"] or account["equipped_cosmetic"]
                       if account else "")
        result = {"id": cid, "game": row["game"], "content": row["content"],
                "nickname": nickname, "author_title_id": title_id,
                "author_cosmetic": cosmetic_id,
                "author_appearances": appearances,
                "ts": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
                "status": row["status"], "reason": row["reason"], "rewarded": bool(row["rewarded"]),
                "likes": votes[0], "dislikes": votes[1],
                "votes_up": votes[0], "votes_down": votes[1],
                "reports": reports,
                "account_id": row["account_id"]}
        import slg_titles
        title = slg_titles.title_by_id(title_id) or {}
        result["author_title_name"] = title.get("name", "")
        if include_profile_message:
            result["profile_message"] = (
                account["profile_message"] if account else "")
        if not admin and not private:
            result.pop("account_id", None)
            if account:
                result.update(self._public_author_fields(self._public_profile(db, account)))
            else:
                # Legacy authors without a live account have no public card.
                result.update(author_public_id="", author_profile_visible=False,
                              author_title_id="", author_title_name="", author_cosmetic="",
                              author_appearances={"avatar_frame": "", "comment_frame": ""},
                              profile_message="")
        if admin:
            result["account_banned"] = bool(account["banned"]) if account else False
            result["moderation_source"] = row["moderation_source"]
            result["original_reason"] = row["original_reason"]
            result["rule_version"] = row["rule_version"]
            result["moderation_history"] = [dict(event) for event in db.execute(
                "SELECT decision,reason,ts,source,rule_version FROM comment_decisions "
                "WHERE comment_id=? ORDER BY ts,id", (cid,))]
            reversed_reward = db.execute("SELECT 1 FROM ledger WHERE claim_key=?",
                                         ("comment:reversal:" + cid,)).fetchone()
            result["reward_state"] = ("reversed" if reversed_reward else
                                      "rewarded" if row["rewarded"] else "not_rewarded")
        return result

    @staticmethod
    def _append_reply_counts(db, items):
        """One query for a bounded page, shared by public and own-root views."""
        if not items:
            return
        ids = [item["id"] for item in items]
        counts = dict(db.execute(
            "SELECT r.root_id,COUNT(*) FROM comment_replies r "
            "JOIN comments c ON c.id=r.root_id "
            "WHERE r.status='approved' AND c.status='approved' "
            "AND r.root_id IN (%s) GROUP BY r.root_id" %
            ",".join("?" for _ in ids), ids).fetchall())
        for item in items:
            item["reply_count"] = counts.get(item["id"], 0)

    def comments_page(self, game, sort="latest", page=1, limit=20,
                      game_slugs=None):
        """Fetch one public, approved-comment page and its count metadata.

        The count is a scalar SQL query. Comment rows are selected with LIMIT
        and OFFSET before vote totals are hydrated, so a popular game never
        loads its entire discussion into application memory.
        """
        if sort not in ("latest", "popular"):
            raise CloudError("bad sort")
        try:
            page = int(page)
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination")
        if page < 1 or page > 1_000_000 or limit < 1 or limit > 50:
            raise CloudError("bad pagination")
        offset = (page - 1) * limit
        games = [str(value) for value in (game_slugs or [game])
                 if isinstance(value, str)]
        if str(game) not in games:
            games.append(str(game))
        games = list(dict.fromkeys(games))
        game_filter = "game IN (%s)" % ",".join("?" for _ in games)
        qualified_game_filter = "c." + game_filter
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM comments WHERE %s AND status='approved'"
                % game_filter, games).fetchone()[0]
            total_pages = (total + limit - 1) // limit
            rows = []
            # Avoid asking SQLite to walk a huge OFFSET for an out-of-range
            # page. The response keeps the requested page and returns []
            # consistently when there are no rows on it.
            if offset < total:
                if sort == "popular":
                    rows = db.execute(
                        "SELECT c.* FROM comments c WHERE %s AND c.status='approved' "
                        "ORDER BY (SELECT COALESCE(SUM(v.value),0) FROM votes v "
                        "WHERE v.comment_id=c.id) DESC,c.ts DESC,c.id DESC LIMIT ? OFFSET ?"
                        % qualified_game_filter,
                        games + [limit, offset]).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM comments WHERE %s AND status='approved' "
                        "ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?" % game_filter,
                        games + [limit, offset]).fetchall()
            out = [self._comment_row(db, row, include_profile_message=True)
                   for row in rows]
            self._append_reply_counts(db, out)
        # Keep the public API deliberately free of account IDs and moderation
        # internals. `nickname` is the stable display-name extension point for
        # a future public profile decoration.
        for comment in out:
            comment.pop("account_id", None)
            comment.pop("reason", None)
            comment.pop("reports", None)
            comment.pop("rewarded", None)
        return {
            "comments": out,
            "page": page,
            "page_size": limit,
            "total_count": total,
            "total_pages": total_pages,
            "sort": sort,
            "has_previous": page > 1 and total_pages > 0,
            "has_next": page < total_pages,
        }

    def comments(self, game, sort="latest", limit=30, aid=None, admin=False,
                 game_slugs=None):
        """List comments for existing account/admin code paths.

        Public callers get the first bounded page. Authenticated account and
        admin callers retain their legacy list behavior and are not exposed by
        the public pagination endpoint.
        """
        if not aid and not admin:
            return self.comments_page(game, sort=sort, page=1,
                                      limit=min(max(int(limit), 1), 50))["comments"]
        if sort not in ("latest", "popular"):
            raise CloudError("bad sort")
        limit = max(1, min(int(limit), 100))
        with self._db() as db:
            if aid:
                if game_slugs and game:
                    games = list(dict.fromkeys(
                        [str(value) for value in game_slugs
                         if isinstance(value, str)] + [str(game)]))
                    game_filter = "game IN (%s)" % ",".join("?" for _ in games)
                    rows = db.execute(
                        "SELECT * FROM comments WHERE account_id=? AND %s"
                        " ORDER BY ts DESC,id DESC LIMIT ?" % game_filter,
                        [aid] + games + [limit]).fetchall()
                else:
                    rows = db.execute("SELECT * FROM comments WHERE account_id=? AND (?='' OR game=?) ORDER BY ts DESC,id DESC LIMIT ?",
                                      (aid, game, game, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM comments ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'hidden' THEN 1 ELSE 2 END, ts DESC,id DESC LIMIT ?",
                                  (limit,)).fetchall()
            out = [self._comment_row(db, row, admin=admin, private=bool(aid)) for row in rows]
            if aid:
                self._append_reply_counts(db, out)
        return out[:limit]

    def account_comments_page(self, aid, *, game="", game_slugs=None,
                              page=1, limit=20, comment_ids=None):
        try:
            page, limit = int(page), int(limit)
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination")
        if not 1 <= page <= 1_000_000 or not 1 <= limit <= 50:
            raise CloudError("bad pagination")
        clauses, params = ["account_id=?"], [aid]
        if comment_ids is not None:
            clauses.append("id IN (%s)" % ",".join("?" for _ in comment_ids))
            params.extend(comment_ids)
        if game:
            games = list(dict.fromkeys(list(game_slugs or []) + [game]))
            clauses.append("game IN (%s)" % ",".join("?" for _ in games))
            params.extend(games)
        where = " AND ".join(clauses)
        with self._db() as db:
            self._account(db, aid)
            total = db.execute("SELECT COUNT(*) FROM comments WHERE " + where,
                               params).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM comments WHERE " + where
                + " ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?",
                params + [limit, (page - 1) * limit]).fetchall()
            comments = [self._comment_row(db, row, private=True) for row in rows]
            self._append_reply_counts(db, comments)
        for comment in comments:
            comment.pop("account_id", None)
            comment.pop("reports", None)
        return {"ok": True, "comments": comments, "page": page,
                "page_size": limit, "total": total,
                "total_pages": (total + limit - 1) // limit,
                "has_more": page * limit < total}

    def approved_count(self, aid=None, legacy_ids=None):
        with self._db() as db:
            if aid:
                return db.execute("SELECT COUNT(*) FROM comments WHERE account_id=? AND status='approved'",
                                  (aid,)).fetchone()[0]
            ids = list(legacy_ids or [])
            total = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                total += db.execute("SELECT COUNT(*) FROM comments WHERE status='approved' AND id IN (%s)" %
                                    ",".join("?" for _ in chunk), chunk).fetchone()[0]
            return total

    def admin_comments(self, limit=30, offset=0, status="", q="",
                       game_slugs=None):
        try:
            limit = max(1, min(int(limit), 50))
            offset = max(0, min(int(offset), 100000))
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination")
        if status and status not in ("pending", "approved", "rejected", "hidden", "reported",
                                     "all", "review", "auto_rejected", "manual_rejected"):
            raise CloudError("bad status")
        if not isinstance(q, str):
            raise CloudError("bad search")
        q = q.strip()[:100]
        game_slugs = list(dict.fromkeys(
            str(value) for value in (game_slugs or [])
            if isinstance(value, str) and value))[:500]
        clauses, args = [], []
        if status == "review":
            clauses.append("comments.status IN ('pending','hidden')")
        elif status in ("auto_rejected", "manual_rejected"):
            clauses.append("comments.status='rejected' AND comments.moderation_source=?")
            args.append("automatic" if status == "auto_rejected" else "manual")
        elif status == "reported":
            clauses.append("EXISTS (SELECT 1 FROM reports r WHERE r.comment_id=comments.id)")
        elif status and status != "all":
            clauses.append("comments.status=?")
            args.append(status)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = "%" + escaped + "%"
            search = [
                "comments.game LIKE ? ESCAPE '\\'",
                "comments.nickname LIKE ? ESCAPE '\\'",
                "comments.content LIKE ? ESCAPE '\\'",
                "comments.account_id LIKE ? ESCAPE '\\'",
                "EXISTS (SELECT 1 FROM accounts a WHERE a.id=comments.account_id "
                "AND a.nickname LIKE ? ESCAPE '\\')",
            ]
            search_args = [pattern] * 5
            if game_slugs:
                search.append("comments.game IN (%s)" %
                              ",".join("?" for _ in game_slugs))
                search_args.extend(game_slugs)
            clauses.append("(" + " OR ".join(search) + ")")
            args.extend(search_args)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as db:
            total = db.execute("SELECT COUNT(*) FROM comments " + where, args).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM comments WHERE status IN ('pending','hidden')").fetchone()[0]
            rows = db.execute("SELECT * FROM comments " + where +
                              " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'hidden' THEN 1 ELSE 2 END,ts DESC,id DESC LIMIT ? OFFSET ?",
                              args + [limit, offset]).fetchall()
            out = [self._comment_row(db, row, admin=True) for row in rows]
            counts = {row["status"]: row["n"] for row in db.execute(
                "SELECT status,COUNT(*) AS n FROM comments GROUP BY status")}
            for state in ("pending", "approved", "rejected", "hidden", "deleted"):
                counts.setdefault(state, 0)
            counts.update(all=sum(counts.values()), review=pending)
            for name, source in (("auto_rejected", "automatic"), ("manual_rejected", "manual")):
                counts[name] = db.execute("SELECT COUNT(*) FROM comments WHERE status='rejected' "
                                          "AND moderation_source=?", (source,)).fetchone()[0]
            counts["reported"] = db.execute("SELECT COUNT(*) FROM comments WHERE EXISTS "
                "(SELECT 1 FROM reports WHERE reports.comment_id=comments.id)").fetchone()[0]
        return {"comments": out, "total": total, "pending_count": pending, "status_counts": counts}

    def admin_comment_metrics(self, now=None, days=14):
        """Return aggregate-only comment activity for the owner dashboard.

        The queries expose no account IDs, nicknames, or comment contents. They
        reuse the existing comments index and create no persistent analytics
        tables, which keeps storage growth negligible.
        """
        now = int(self.now() if now is None else now)
        days = int(days)
        if days not in (7, 14, 30, 90):
            raise CloudError("bad analytics range")
        recent_start = now - 30 * 86400
        previous_start = now - 60 * 86400
        today = datetime.fromtimestamp(now, CHINA).date()
        range_start = today - timedelta(days=days - 1)
        range_start_ts = int(datetime.combine(
            range_start, datetime.min.time(), CHINA).timestamp())
        range_end_ts = int(datetime.combine(
            today + timedelta(days=1), datetime.min.time(), CHINA).timestamp())
        with self._db() as db:
            status_rows = db.execute(
                "SELECT status,COUNT(*),"
                "SUM(CASE WHEN ts>=? AND ts<=? THEN 1 ELSE 0 END) "
                "FROM comments GROUP BY status",
                (recent_start, now)).fetchall()
            game_rows = db.execute(
                "SELECT game,"
                "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN status='approved' AND ts>=? AND ts<=? "
                "THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN status='approved' AND ts>=? AND ts<? "
                "THEN 1 ELSE 0 END) "
                "FROM comments GROUP BY game",
                (recent_start, now, previous_start, recent_start)).fetchall()
            daily_rows = db.execute(
                "SELECT strftime('%Y-%m-%d',ts,'unixepoch','+8 hours'),COUNT(*),"
                "SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) "
                "FROM comments WHERE ts>=? AND ts<? GROUP BY 1",
                (range_start_ts, range_end_ts)).fetchall()

        counts = {str(row[0]): int(row[1] or 0) for row in status_rows}
        total = sum(counts.values())
        recent = sum(int(row[2] or 0) for row in status_rows)
        games = [{"game": str(row[0]), "approved": int(row[1] or 0),
                  "count": int(row[2] or 0), "previous": int(row[3] or 0)}
                 for row in game_rows]
        top_games = sorted(
            (row for row in games if row["approved"]),
            key=lambda row: (-row["approved"], row["game"]))[:10]
        trend = sorted(
            (row for row in games if row["count"] or row["previous"]),
            key=lambda row: (-row["count"], -row["previous"], row["game"]))[:10]
        daily = {str(row[0]): (int(row[1] or 0), int(row[2] or 0))
                 for row in daily_rows if row[0]}
        comment_by_day = []
        for back in range(days):
            day = (today - timedelta(days=back)).isoformat()
            submitted, approved = daily.get(day, (0, 0))
            comment_by_day.append({"day": day, "submitted": submitted,
                                   "approved": approved})
        return {
            "comment_metrics": {
                "total": total,
                "approved": counts.get("approved", 0),
                "pending": counts.get("pending", 0) + counts.get("hidden", 0),
                "hidden": counts.get("hidden", 0),
                "rejected": counts.get("rejected", 0),
                "last_30_days": recent,
                "top_games": [{"game": row["game"],
                               "count": row["approved"]} for row in top_games],
            },
            "comment_by_day": comment_by_day,
            "game_trend": {
                "source": "按已通过的公开云端评论数统计",
                "period": "近30天，对比此前30天",
                "items": [{"game": row["game"], "count": row["count"],
                           "delta": row["count"] - row["previous"]}
                          for row in trend],
            },
        }

    def import_legacy(self, path, moderation):
        """Import only new complete JSONL records; preserve the old file for audit.

        The byte offset and inserted rows commit together, so a restart is safe.
        Legacy posts cannot acquire an account or points from a client device ID.
        """
        if not os.path.isfile(path):
            return
        size = os.path.getsize(path)
        with self._db() as read_db:
            old = read_db.execute("SELECT value FROM metadata WHERE key='legacy_offset'").fetchone()
            if old and size == int(old[0]):
                return
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM metadata WHERE key='legacy_offset'").fetchone()
            offset = int(row[0]) if row else 0
            size = os.path.getsize(path)
            if size == offset:
                return
            if size < offset:
                # A restored/rotated file can be scanned again. INSERT OR IGNORE
                # keeps stable comment IDs idempotent.
                offset = 0
            with open(path, "rb") as fh:
                fh.seek(offset)
                while True:
                    start = fh.tell()
                    line = fh.readline()
                    if not line or not line.endswith(b"\n"):
                        fh.seek(start)
                        break
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    cid = str(obj.get("id", ""))[:128]
                    if "content" in obj and cid:
                        content = str(obj.get("content", ""))
                        saved = obj.get("moderation")
                        saved_verdict = (isinstance(saved, dict) and
                                         saved.get("decision") in ("approve", "review", "reject"))
                        if saved_verdict:
                            verdict = saved
                        elif len(content) < 20 or len(content) > 500 or len(content.encode("utf-8")) > 2048:
                            verdict = {"decision": "review", "reason": "legacy length"}
                            # Preserve historical text but still scan all of
                            # it for explicit disallowed material. Overlap
                            # prevents a phrase crossing a chunk boundary.
                            for start in range(0, len(content), 400):
                                sample = content[start:start + 450]
                                if len(sample) < 20:
                                    sample += " 游戏剧情角色体验有足够内容说明"
                                part = moderation(sample)
                                if part.get("decision") == "reject":
                                    verdict = part
                                    break
                        else:
                            verdict = moderation(content)
                        status = {"approve": "approved", "review": "pending",
                                  "reject": "rejected"}.get(verdict.get("decision"), "pending")
                        ts_text = obj.get("ts")
                        try:
                            ts = int(datetime.fromisoformat(ts_text).timestamp())
                        except (TypeError, ValueError):
                            ts = self.now()
                        source = "automatic" if saved_verdict else "unknown"
                        reason = str(verdict.get("reason", ""))[:120]
                        version = str(verdict.get("rule_version", ""))[:100] if saved_verdict else ""
                        inserted = db.execute("INSERT OR IGNORE INTO comments(id,account_id,game,content,nickname,status,reason,ts,original_reason,moderation_source,rule_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   (cid, "legacy", str(obj.get("game", ""))[:200], content,
                                     str(obj.get("nickname", ""))[:40], status, reason, ts,
                                     reason if saved_verdict else "", source, version))
                        if inserted.rowcount:
                            self._comment_event(db, cid, verdict.get("decision", "review"),
                                                reason, ts, source, version)
                    elif "tombstone" in obj:
                        db.execute("UPDATE comments SET status='deleted',reason='legacy tombstone' WHERE id=?",
                                   (str(obj["tombstone"])[:128],))
                    elif "report" in obj:
                        rcid = str(obj["report"])[:128]
                        actor = str(obj.get("device", "legacy"))[:64]
                        db.execute("INSERT OR IGNORE INTO reports(account_id,comment_id,ts) VALUES (?,?,?)",
                                   ("legacy:" + _hash(actor), rcid, self.now()))
                new_offset = fh.tell()
            db.execute("INSERT INTO metadata(key,value) VALUES ('legacy_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(new_offset),))

    def vote(self, aid, cid, value):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot vote on comments", 403)
        if value not in (-1, 0, 1) or isinstance(value, bool):
            raise CloudError("bad vote")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            comment = db.execute("SELECT * FROM comments WHERE id=? AND status='approved'", (cid,)).fetchone()
            if not comment:
                raise CloudError("not found", 404)
            if comment["account_id"] == aid:
                raise CloudError("own comment", 403)
            if value:
                db.execute("INSERT INTO votes(account_id,comment_id,value,ts) VALUES (?,?,?,?) ON CONFLICT(account_id,comment_id) DO UPDATE SET value=excluded.value,ts=excluded.ts",
                           (aid, cid, value, self.now()))
            else:
                db.execute("DELETE FROM votes WHERE account_id=? AND comment_id=?", (aid, cid))
            votes = db.execute("SELECT COALESCE(SUM(value=1),0),COALESCE(SUM(value=-1),0),COALESCE(SUM(value),0) FROM votes WHERE comment_id=?",
                               (cid,)).fetchone()
            mature_down = db.execute("SELECT COUNT(*) FROM votes v JOIN accounts a ON a.id=v.account_id "
                                     "WHERE v.comment_id=? AND v.value=-1 AND a.created<=? AND a.banned=0 "
                                     "AND NOT EXISTS (SELECT 1 FROM comment_feedback_baselines b WHERE "
                                     "b.comment_id=v.comment_id AND b.kind='vote' AND b.account_id=v.account_id)",
                                     (cid, self.now() - 86400)).fetchone()[0]
            score = db.execute("SELECT COALESCE(SUM(v.value),0) FROM votes v WHERE v.comment_id=? "
                               "AND NOT EXISTS (SELECT 1 FROM comment_feedback_baselines b WHERE "
                               "b.comment_id=v.comment_id AND b.kind='vote' AND b.account_id=v.account_id)",
                               (cid,)).fetchone()[0]
            hidden = mature_down >= 5 and score <= -3
            if hidden:
                db.execute("UPDATE comments SET status='hidden',reason='downvote threshold',moderation_source='feedback' WHERE id=?", (cid,))
                self._comment_event(db, cid, "hide", "downvote threshold", self.now(), "feedback")
        return {"ok": True, "likes": votes[0], "dislikes": votes[1],
                "votes_up": votes[0], "votes_down": votes[1], "hidden": hidden}

    def report(self, aid, cid):
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot report comments", 403)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            target = db.execute("SELECT account_id,status FROM comments WHERE id=?", (cid,)).fetchone()
            if not target or target["status"] != "approved":
                raise CloudError("not found", 404)
            if target["account_id"] == aid:
                raise CloudError("own comment", 403)
            if db.execute("SELECT COUNT(*) FROM reports WHERE account_id=? AND ts>?",
                          (aid, self.now() - 600)).fetchone()[0] >= 8:
                raise CloudError("rate limited", 429)
            cur = db.execute("INSERT OR IGNORE INTO reports VALUES (?,?,?)", (aid, cid, self.now()))
            if not cur.rowcount:
                raise CloudError("duplicate report", 409)
            count = db.execute("SELECT COUNT(*) FROM reports WHERE comment_id=?", (cid,)).fetchone()[0]
            mature = db.execute("SELECT COUNT(*) FROM reports r JOIN accounts a ON a.id=r.account_id "
                                "WHERE r.comment_id=? AND a.created<=? AND a.banned=0 "
                                "AND NOT EXISTS (SELECT 1 FROM comment_feedback_baselines b WHERE "
                                "b.comment_id=r.comment_id AND b.kind='report' AND b.account_id=r.account_id)",
                                (cid, self.now() - 86400)).fetchone()[0]
            if mature >= 3:
                db.execute("UPDATE comments SET status='hidden',reason='report threshold',moderation_source='feedback' WHERE id=?", (cid,))
                self._comment_event(db, cid, "hide", "report threshold", self.now(), "feedback")
        return {"ok": True, "reports": count, "hidden": mature >= 3}

    @staticmethod
    def _comment_event(db, cid, decision, reason, ts, source, rule_version=""):
        db.execute("INSERT INTO comment_decisions(comment_id,decision,reason,ts,source,rule_version) "
                   "VALUES (?,?,?,?,?,?)", (cid, decision, reason, ts, source, rule_version))

    def decide(self, cid, decision, reason=""):
        if decision not in ("approve", "reject", "hide"):
            raise CloudError("bad decision")
        if decision == "approve":
            self._writable()
        if not isinstance(reason, str):
            raise CloudError("bad reason")
        reason = reason.strip()[:200]
        status = {"approve": "approved", "reject": "rejected", "hide": "hidden"}[decision]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            if row["status"] == "deleted":
                raise CloudError("comment deleted", 409)
            now = self.now()
            if status == "approved":
                if row["status"] != "approved":
                    for table, kind in (("votes", "vote"), ("reports", "report")):
                        db.execute("INSERT OR IGNORE INTO comment_feedback_baselines "
                                   "SELECT comment_id,?,account_id FROM " + table + " WHERE comment_id=?",
                                   (kind, cid))
                first_public = row["approved_ts"] or (
                    row["ts"] if row["status"] == "approved" else now)
                db.execute("UPDATE comments SET status=?,reason=?,approved_ts=?,moderation_source='manual' WHERE id=?",
                           (status, reason or "admin decision", first_public, cid))
            else:
                db.execute("UPDATE comments SET status=?,reason=?,moderation_source='manual' WHERE id=?",
                           (status, reason or "admin decision", cid))
            self._comment_event(db, cid, "restore" if status == "approved" and row["status"] in
                                ("hidden", "rejected") else decision, reason, now, "manual")
            gained = 0
            if status == "approved" and row["account_id"] != "legacy" and not row["rewarded"]:
                gained = self._grant_comment(db, row["account_id"], cid, row["game"])
            if status == "approved" and row["account_id"] != "legacy":
                self._grant_connoisseur(db, row["account_id"])
            elif status == "rejected" and row["rewarded"]:
                db.execute("INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                           (row["account_id"], -20, "comment_reversal",
                            "comment:reversal:" + cid, self.now()))
                db.execute("UPDATE comments SET rewarded=0 WHERE id=?", (cid,))
            self._append_admin_audit(
                db, "comment_moderation", cid,
                {"decision": decision, "status": status}, self.now())
        return {"ok": True, "id": cid, "status": status, "gained": gained}

    @staticmethod
    def _feedback_text(value, max_chars, field):
        if not isinstance(value, str):
            raise CloudError("invalid " + field)
        value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
        if len(value) > max_chars:
            raise CloudError("invalid " + field)
        if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t"
               for ch in value):
            raise CloudError("invalid " + field)
        return value

    @staticmethod
    def _feedback_timestamp(value):
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()

    @staticmethod
    def _feedback_user_label(value):
        cleaned = "".join(ch for ch in str(value or "")
                          if unicodedata.category(ch) != "Cc").strip()[:40]
        return cleaned or "匿名用户"

    def submit_feedback(self, aid, category, content):
        """Store bounded, private account feedback; only admins can read it."""
        self._writable()
        if aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot submit ordinary feedback", 403)
        allowed = ("bug", "suggestion", "data_correction", "other")
        if not isinstance(category, str) or category not in allowed:
            raise CloudError("invalid feedback category")
        content = self._feedback_text(content, 500, "feedback content")
        if not 20 <= len(content) <= 500 or len(content.encode("utf-8")) > 4096:
            raise CloudError("feedback must be 20-500 characters")

        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._account(db, aid)
            duplicate = db.execute(
                "SELECT 1 FROM feedback WHERE account_id=? AND category=? "
                "AND content=? AND created>? LIMIT 1",
                (aid, category, content, now - 30 * 86400)).fetchone()
            if duplicate:
                raise CloudError("duplicate feedback", 409)
            pending = db.execute(
                "SELECT COUNT(*) FROM feedback WHERE account_id=? AND status='pending'",
                (aid,)).fetchone()[0]
            if pending >= 5:
                raise CloudError("too many pending feedback items", 429)
            daily = db.execute(
                "SELECT COUNT(*) FROM feedback WHERE account_id=? AND created>?",
                (aid, now - 86400)).fetchone()[0]
            if daily >= 2:
                raise CloudError("feedback daily limit", 429)
            monthly = db.execute(
                "SELECT COUNT(*) FROM feedback WHERE account_id=? AND created>?",
                (aid, now - 30 * 86400)).fetchone()[0]
            if monthly >= 10:
                raise CloudError("feedback monthly limit", 429)
            total_feedback = db.execute(
                "SELECT COUNT(*) FROM feedback").fetchone()[0]
            if total_feedback >= MAX_FEEDBACK_ITEMS:
                raise CloudError("feedback temporarily unavailable", 503)
            cur = db.execute(
                "INSERT INTO feedback(account_id,category,content,status,created,updated) "
                "VALUES (?,?,?,'pending',?,?)",
                (aid, category, content, now, now))
            return {"ok": True, "id": int(cur.lastrowid),
                    "status": "pending", "reward_points": 0}

    def admin_feedback(self, limit=30, offset=0, status="", category=""):
        """Return a bounded, secret-free page for the developer dashboard."""
        try:
            if isinstance(limit, bool) or isinstance(offset, bool):
                raise ValueError
            limit, offset = int(limit), int(offset)
        except (TypeError, ValueError, OverflowError):
            raise CloudError("bad pagination")
        if not 1 <= limit <= 500 or not 0 <= offset <= 100000:
            raise CloudError("bad pagination")
        statuses = ("pending", "accepted", "rejected", "duplicate")
        categories = ("bug", "suggestion", "data_correction", "other")
        if status and status not in statuses:
            raise CloudError("bad status")
        if category and category not in categories:
            raise CloudError("bad category")
        clauses, args = [], []
        if status:
            clauses.append("f.status=?")
            args.append(status)
        if category:
            clauses.append("f.category=?")
            args.append(category)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM feedback f" + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT f.id,f.category,f.content,f.status,f.created,f.updated,"
                "f.reward_points,f.reward_title,f.admin_note,a.nickname "
                "FROM feedback f JOIN accounts a ON a.id=f.account_id" + where +
                " ORDER BY CASE f.status WHEN 'pending' THEN 0 ELSE 1 END,"
                "f.created DESC,f.id DESC LIMIT ? OFFSET ?",
                args + [limit, offset]).fetchall()
        items = [{
            "id": row["id"], "category": row["category"],
            "content": row["content"], "status": row["status"],
            "created_at": self._feedback_timestamp(row["created"]),
            "updated_at": self._feedback_timestamp(row["updated"]),
            "user_label": self._feedback_user_label(row["nickname"]),
            "reward_points": row["reward_points"],
            "reward_title": row["reward_title"] or None,
            "admin_note": row["admin_note"],
        } for row in rows]
        return {"ok": True, "total": total, "items": items}

    def decide_feedback(self, feedback_id, action, note=""):
        """Finalize one item and atomically grant its eligible reward/title."""
        if action not in ("accept", "reject", "duplicate"):
            raise CloudError("bad feedback action")
        note = self._feedback_text(note, 500, "admin note")
        try:
            feedback_id = int(feedback_id)
        except (TypeError, ValueError, OverflowError):
            raise CloudError("not found", 404)
        if feedback_id <= 0:
            raise CloudError("not found", 404)
        status = {"accept": "accepted", "reject": "rejected",
                  "duplicate": "duplicate"}[action]
        if action == "accept":
            self._writable()
        now = self.now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            if row["status"] != "pending":
                if row["status"] == status:
                    return {"ok": True, "id": feedback_id, "status": status,
                            "reward_points": int(row["reward_points"]),
                            "reward_title": row["reward_title"] or None}
                raise CloudError("feedback already decided", 409)

            db.execute("UPDATE feedback SET status=?,updated=?,admin_note=? WHERE id=?",
                       (status, now, note, feedback_id))
            reward_points = 0
            reward_title = ""
            if action == "accept" and row["account_id"] != DEVELOPER_ACCOUNT_ID:
                prior_reward = db.execute(
                    "SELECT 1 FROM ledger WHERE account_id=? AND reason='feedback_reward' "
                    "AND ts>? LIMIT 1",
                    (row["account_id"], now - 30 * 86400)).fetchone()
                if not prior_reward:
                    claim_key = "feedback:%d" % feedback_id
                    cur = db.execute(
                        "INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) "
                        "VALUES (?,?,?,?,?)",
                        (row["account_id"], 20, "feedback_reward", claim_key, now))
                    if cur.rowcount:
                        reward_points = 20
                db.execute("UPDATE feedback SET reward_points=? WHERE id=?",
                           (reward_points, feedback_id))

                accepted = db.execute(
                    "SELECT COUNT(*) FROM feedback WHERE account_id=? AND status='accepted'",
                    (row["account_id"],)).fetchone()[0]
                title_id = FEEDBACK_TITLE_ID
                try:
                    import slg_titles
                    valid_titles = {item["id"] for item in slg_titles.TITLES}
                except (ImportError, AttributeError, KeyError, TypeError):
                    valid_titles = set()
                if accepted >= 1 and title_id in valid_titles:
                    title_granted = bool(db.execute(
                        "INSERT OR IGNORE INTO owned_titles(account_id,title_id,source,ts) "
                        "VALUES (?,?,?,?)",
                        (row["account_id"], title_id, "feedback", now)).rowcount)
                    if title_granted:
                        reward_title = FEEDBACK_TITLE_NAME
                        db.execute("UPDATE feedback SET reward_title=? WHERE id=?",
                                   (reward_title, feedback_id))

            return {"ok": True, "id": feedback_id, "status": status,
                    "reward_points": reward_points,
                    "reward_title": reward_title or None}

    def admin_feedback_export(self, limit=500, offset=0, status="", category=""):
        """Export one bounded JSONL page using the same privacy-filtered fields."""
        return self.admin_feedback(limit, offset, status, category)["items"]

    @staticmethod
    def _source_review_key(source_id, external_id, content_hash):
        source_id = str(source_id or "")
        external_id = str(external_id or "")
        content_hash = str(content_hash or "")
        if source_id not in ("f95zone", "zyoudao"):
            raise CloudError("invalid source identity")
        if not external_id or len(external_id) > 128:
            raise CloudError("invalid external identity")
        if not content_hash or len(content_hash) > 96:
            raise CloudError("invalid source revision")
        return source_id, external_id, content_hash

    def admin_source_review_state(self, identities):
        """Read private AI suggestions and human decisions for a bounded page."""
        result = {}
        with self._db() as db:
            for source_id, external_id, content_hash in identities:
                key = (source_id, external_id, content_hash)
                suggestion = db.execute(
                    "SELECT verdict,record_kind,rationale,confidence,updated "
                    "FROM source_review_ai_suggestions WHERE source_id=? "
                    "AND external_id=? AND content_hash=?", key).fetchone()
                decision = db.execute(
                    "SELECT decision,canonical_game_id,note,decided "
                    "FROM source_review_decisions WHERE source_id=? "
                    "AND external_id=? AND content_hash=?", key).fetchone()
                result[key] = {
                    "ai": ({"verdict": suggestion["verdict"],
                            "record_kind": suggestion["record_kind"],
                            "rationale": suggestion["rationale"],
                            "confidence": suggestion["confidence"],
                            "updated": suggestion["updated"]}
                           if suggestion else None),
                    "manual": ({"decision": decision["decision"],
                                "canonical_game_id": decision["canonical_game_id"],
                                "note": decision["note"],
                                "decided": decision["decided"]}
                               if decision else None),
                }
        return result

    def admin_source_review_index(self):
        """Compact private state index used to filter catalog rows for a page."""
        with self._db() as db:
            suggestions = db.execute(
                "SELECT source_id,external_id,content_hash,verdict,record_kind,"
                "rationale,confidence,updated FROM source_review_ai_suggestions"
            ).fetchall()
            decisions = db.execute(
                "SELECT source_id,external_id,content_hash,decision,"
                "canonical_game_id,note,decided FROM source_review_decisions"
            ).fetchall()
        ai = {(r["source_id"], r["external_id"], r["content_hash"]): {
            "verdict": r["verdict"], "record_kind": r["record_kind"],
            "rationale": r["rationale"], "confidence": r["confidence"],
            "updated": r["updated"]} for r in suggestions}
        manual = {(r["source_id"], r["external_id"], r["content_hash"]): {
            "decision": r["decision"],
            "canonical_game_id": r["canonical_game_id"],
            "note": r["note"], "decided": r["decided"]} for r in decisions}
        return ai, manual

    def admin_import_source_review_suggestions(self, items):
        """Persist AI proposals per row; a proposal never approves a mapping."""
        self._writable()
        outcomes = []
        with self._db() as db:
            for index, item in enumerate(items):
                savepoint = "source_review_%d" % index
                db.execute("SAVEPOINT " + savepoint)
                try:
                    source_id, external_id, content_hash = self._source_review_key(
                        item.get("source_id"), item.get("external_id"),
                        item.get("content_hash"))
                    verdict = item.get("verdict")
                    if verdict not in ("same", "distinct", "uncertain"):
                        raise CloudError("invalid AI verdict")
                    record_kind = item.get("record_kind", "game")
                    if record_kind not in ("game", "non_game", "insufficient"):
                        raise CloudError("invalid record kind")
                    rationale = item.get("rationale", "")
                    if not isinstance(rationale, str) or len(rationale) > 500:
                        raise CloudError("AI rationale exceeds limit")
                    confidence = item.get("confidence")
                    if (isinstance(confidence, bool)
                            or not isinstance(confidence, (int, float))
                            or not math.isfinite(float(confidence))
                            or confidence < 0 or confidence > 1):
                        raise CloudError("invalid confidence")
                    key = (source_id, external_id, content_hash)
                    decided = db.execute(
                        "SELECT decision FROM source_review_decisions WHERE "
                        "source_id=? AND external_id=? AND content_hash=?", key
                    ).fetchone()
                    if decided and decided["decision"] in ("same", "distinct"):
                        raise CloudError("item already has a human decision")
                    db.execute(
                        "INSERT INTO source_review_ai_suggestions "
                        "(source_id,external_id,content_hash,verdict,record_kind,"
                        "rationale,confidence,updated) VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(source_id,external_id,content_hash) DO UPDATE "
                        "SET verdict=excluded.verdict,record_kind=excluded.record_kind,"
                        "rationale=excluded.rationale,confidence=excluded.confidence,"
                        "updated=excluded.updated WHERE "
                        "source_review_ai_suggestions.verdict<>excluded.verdict OR "
                        "source_review_ai_suggestions.record_kind<>excluded.record_kind OR "
                        "source_review_ai_suggestions.rationale<>excluded.rationale OR "
                        "source_review_ai_suggestions.confidence<>excluded.confidence",
                        key + (verdict, record_kind, rationale.strip(),
                               float(confidence), self.now()))
                    db.execute("RELEASE SAVEPOINT " + savepoint)
                    outcomes.append({"source_id": source_id,
                                     "external_id": external_id, "ok": True})
                except (CloudError, sqlite3.Error, TypeError, ValueError) as exc:
                    db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                    db.execute("RELEASE SAVEPOINT " + savepoint)
                    outcomes.append({"source_id": str(item.get("source_id", ""))[:32],
                                     "external_id": str(item.get("external_id", ""))[:128],
                                     "ok": False, "error": str(exc)[:160]})
        return {"ok": True, "items": outcomes,
                "accepted": sum(1 for row in outcomes if row["ok"]),
                "rejected": sum(1 for row in outcomes if not row["ok"])}

    def admin_decide_source_review(self, source_id, external_id, content_hash,
                                   decision, canonical_game_id=None, note=""):
        """Store a separate, explicit human decision; never changes game_sources."""
        self._writable()
        key = self._source_review_key(source_id, external_id, content_hash)
        if decision not in ("same", "distinct", "skip"):
            raise CloudError("invalid review decision")
        if canonical_game_id is not None:
            if (isinstance(canonical_game_id, bool)
                    or not isinstance(canonical_game_id, int)
                    or canonical_game_id <= 0):
                raise CloudError("invalid canonical game id")
        if not isinstance(note, str) or len(note) > 500:
            raise CloudError("review note exceeds limit")
        now = self.now()
        with self._db() as db:
            existing = db.execute(
                "SELECT decision,canonical_game_id,note,decided FROM "
                "source_review_decisions WHERE source_id=? AND external_id=? "
                "AND content_hash=?", key).fetchone()
            if existing:
                if (existing["decision"] == decision
                        and existing["canonical_game_id"] == canonical_game_id
                        and existing["note"] == note.strip()):
                    return {"ok": True, "decision": decision,
                            "decided": existing["decided"], "idempotent": True}
                if existing["decision"] in ("same", "distinct"):
                    raise CloudError("human decision is final for this source revision", 409)
            db.execute(
                "INSERT INTO source_review_decisions "
                "(source_id,external_id,content_hash,decision,canonical_game_id,note,decided) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_id,external_id,content_hash) "
                "DO UPDATE SET decision=excluded.decision,"
                "canonical_game_id=excluded.canonical_game_id,note=excluded.note,"
                "decided=excluded.decided",
                key + (decision, canonical_game_id, note.strip(), now))
        return {"ok": True, "decision": decision, "decided": now,
                "idempotent": False}

    def delete_comment(self, aid, cid, admin=False):
        if not admin and aid == DEVELOPER_ACCOUNT_ID:
            raise CloudError("developer account cannot delete ordinary comments", 403)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT account_id,rewarded FROM comments WHERE id=?", (cid,)).fetchone()
            if not row:
                raise CloudError("not found", 404)
            if not admin and row[0] != aid:
                raise CloudError("forbidden", 403)
            db.execute("UPDATE comments SET status='deleted',reason='deleted' WHERE id=?", (cid,))
            if row["rewarded"]:
                db.execute("INSERT OR IGNORE INTO ledger(account_id,delta,reason,claim_key,ts) VALUES (?,?,?,?,?)",
                           (row["account_id"], -20, "comment_reversal",
                            "comment:reversal:" + cid, self.now()))
                db.execute("UPDATE comments SET rewarded=0 WHERE id=?", (cid,))
            if admin:
                self._append_admin_audit(
                    db, "comment_deleted", cid, {}, self.now())

    def storage_bytes(self):
        return sum(os.path.getsize(self.path + suffix) for suffix in ("", "-wal", "-shm")
                   if os.path.isfile(self.path + suffix))


if __name__ == "__main__":
    import sys
    result = _maintenance_cli(sys.argv[1:])
    if result is not None:
        raise SystemExit(result)
