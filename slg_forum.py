"""Independent SQLite storage for the web-only forum.

This module deliberately does not import the cloud account store or use the
game-comment APIs.  It can share the cloud database file while keeping forum
tables, moderation state, and side effects separate.  HTTP routes must validate
the web session (and administrator session for moderation calls) before calling
these methods.
"""

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import time
import unicodedata
import uuid


THREAD_STATES = frozenset(("pending", "approved", "rejected", "hidden", "deleted"))
REPLY_STATES = THREAD_STATES
REPORT_STATES = frozenset(("pending", "actioned", "dismissed"))
REPORT_REASONS = frozenset(("spam", "abuse", "illegal", "other"))
MODERATION_ACTIONS = frozenset(("approve", "reject", "hide"))
CONTENT_TYPES = frozenset(("thread", "reply"))

MAX_ACCOUNT_ID = 128
MAX_AUTHOR_NAME = 60
MAX_THREAD_TITLE = 160
MAX_THREAD_CONTENT = 12000
MAX_REPLY_CONTENT = 6000
MAX_MODERATION_REASON = 500
MAX_REPORT_DETAILS = 1000
MAX_PAGE_SIZE = 30
MAX_PAGE_NUMBER = 1000


class ForumError(Exception):
    """A safe, HTTP-neutral error for the route layer to translate."""

    def __init__(self, message, status=400):
        Exception.__init__(self, message)
        self.status = status


def _now():
    return int(time.time())


def _text(value, field, maximum, allow_empty=False):
    if not isinstance(value, str):
        raise ForumError("invalid " + field)
    value = unicodedata.normalize("NFC", value).strip()
    if (not allow_empty and not value) or len(value) > maximum:
        raise ForumError("invalid " + field)
    if "\x00" in value:
        raise ForumError("invalid " + field)
    return value


def _content(value, field, maximum):
    value = _text(value, field, maximum)
    # Newlines and tabs are useful in plain-text posts, while other C0 control
    # characters have no place in stored user content.
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ForumError("invalid " + field)
    return value


def _identity(value, field="account id"):
    return _text(value, field, MAX_ACCOUNT_ID)


def _page(page, page_size):
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > MAX_PAGE_NUMBER:
        raise ForumError("invalid page")
    if (isinstance(page_size, bool) or not isinstance(page_size, int)
            or page_size < 1 or page_size > MAX_PAGE_SIZE):
        raise ForumError("invalid page size")
    return page, page_size, (page - 1) * page_size


def _row_dict(row):
    return dict(row) if row is not None else None


def _public_record(row):
    """Keep internal account ids out of public and author-facing responses."""
    record = dict(row)
    record.pop("author_id", None)
    record.pop("moderation_reason", None)
    return record


def _viewer_record(row, viewer_id):
    """Add deletion affordances without exposing the internal account id."""
    record = _public_record(row)
    if viewer_id is not None and row["author_id"] == viewer_id:
        record["is_mine"] = True
        record["can_delete"] = row["status"] != "deleted"
    return record


class ForumStore:
    """Web forum storage, optionally colocated with the existing cloud DB."""

    def __init__(self, path, now=None, author_profiles=None):
        self.path = os.path.abspath(path)
        self.now = now or _now
        self.author_profiles = author_profiles
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS forum_threads (
                    id TEXT PRIMARY KEY,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    game_slug TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected','hidden','deleted')),
                    moderation_reason TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    approved_at INTEGER);
                CREATE INDEX IF NOT EXISTS idx_forum_threads_public
                    ON forum_threads(status,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_forum_threads_game_public
                    ON forum_threads(game_slug,status,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_forum_threads_author
                    ON forum_threads(author_id,created DESC,id DESC);

                CREATE TABLE IF NOT EXISTS forum_replies (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    parent_reply_id TEXT,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected','hidden','deleted')),
                    moderation_reason TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL,
                    updated INTEGER NOT NULL,
                    approved_at INTEGER,
                    FOREIGN KEY(thread_id) REFERENCES forum_threads(id),
                    FOREIGN KEY(parent_reply_id) REFERENCES forum_replies(id));
                CREATE INDEX IF NOT EXISTS idx_forum_replies_thread_public
                    ON forum_replies(thread_id,status,created,id);
                CREATE INDEX IF NOT EXISTS idx_forum_replies_author
                    ON forum_replies(author_id,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_forum_replies_parent
                    ON forum_replies(parent_reply_id,status,created,id);
                CREATE INDEX IF NOT EXISTS idx_forum_replies_moderation
                    ON forum_replies(status,created DESC,id DESC);

                CREATE TABLE IF NOT EXISTS forum_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_id TEXT NOT NULL,
                    target_type TEXT NOT NULL CHECK(target_type IN ('thread','reply')),
                    target_id TEXT NOT NULL,
                    reason TEXT NOT NULL CHECK(reason IN ('spam','abuse','illegal','other')),
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','actioned','dismissed')),
                    created INTEGER NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at INTEGER,
                    UNIQUE(reporter_id,target_type,target_id));
                CREATE INDEX IF NOT EXISTS idx_forum_reports_status_created
                    ON forum_reports(status,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_forum_reports_target
                    ON forum_reports(target_type,target_id,status);

                CREATE TABLE IF NOT EXISTS forum_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_type TEXT NOT NULL CHECK(target_type IN ('thread','reply','report')),
                    target_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN (
                        'approve','reject','hide','report_actioned','report_dismissed')),
                    reason TEXT NOT NULL DEFAULT '',
                    created INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_forum_decisions_target
                    ON forum_decisions(target_type,target_id,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_forum_decisions_created
                    ON forum_decisions(created DESC,id DESC);
                CREATE TRIGGER IF NOT EXISTS forum_decisions_no_update
                    BEFORE UPDATE ON forum_decisions BEGIN
                    SELECT RAISE(ABORT,'forum decisions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS forum_decisions_no_delete
                    BEFORE DELETE ON forum_decisions BEGIN
                    SELECT RAISE(ABORT,'forum decisions are append-only'); END;
                CREATE TABLE IF NOT EXISTS forum_automatic_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_type TEXT NOT NULL CHECK(target_type IN ('thread','reply')),
                    target_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL DEFAULT 'automatic',
                    action TEXT NOT NULL CHECK(action IN ('approve','review','reject')),
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source='automatic'),
                    rule_version TEXT NOT NULL,
                    created INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS forum_submission_operations (
                    author_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('thread','reply')),
                    operation_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    PRIMARY KEY(author_id,kind,operation_id));
                CREATE INDEX IF NOT EXISTS idx_forum_automatic_target
                    ON forum_automatic_decisions(target_type,target_id,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_forum_automatic_created
                    ON forum_automatic_decisions(created DESC,id DESC);
                CREATE TRIGGER IF NOT EXISTS forum_automatic_no_update
                    BEFORE UPDATE ON forum_automatic_decisions BEGIN
                    SELECT RAISE(ABORT,'forum automatic decisions are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS forum_automatic_no_delete
                    BEFORE DELETE ON forum_automatic_decisions BEGIN
                    SELECT RAISE(ABORT,'forum automatic decisions are append-only'); END;
            """)

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

    @staticmethod
    def _target(target_type):
        if target_type == "thread":
            return "forum_threads"
        if target_type == "reply":
            return "forum_replies"
        raise ForumError("invalid content type")

    @staticmethod
    def _moderation(moderation):
        if moderation is None:
            return "pending", "", None
        if (not isinstance(moderation, dict)
                or moderation.get("decision") not in ("approve", "review", "reject")
                or moderation.get("source") != "automatic"):
            raise ForumError("invalid moderation")
        reason = _text(moderation.get("reason"), "moderation reason", MAX_MODERATION_REASON)
        version = _text(moderation.get("rule_version"), "rule version", 120)
        decision = moderation["decision"]
        return ({"approve": "approved", "review": "pending", "reject": "rejected"}[decision],
                reason, {"action": decision, "reason": reason, "rule_version": version})

    @staticmethod
    def _record_automatic(db, target_type, target_id, moderation, now):
        if moderation is not None:
            db.execute(
                "INSERT INTO forum_automatic_decisions "
                "(target_type,target_id,action,reason,source,rule_version,created) "
                "VALUES (?,?,?,?,'automatic',?,?)",
                (target_type, target_id, moderation["action"], moderation["reason"],
                 moderation["rule_version"], now))

    @staticmethod
    def _operation_key(operation_id, fields):
        if operation_id is None:
            return None
        operation_id = _text(operation_id, "operation id", 64)
        if not operation_id.isascii() or any(
                not (char.isalnum() or char in "-_") for char in operation_id):
            raise ForumError("invalid operation id")
        digest = hashlib.sha256(json.dumps(fields, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
        return operation_id, digest

    def _records(self, rows, viewer_id=None):
        rows = list(rows)
        profiles = (self.author_profiles([row["author_id"] for row in rows])
                    if self.author_profiles else {})
        return [{**_viewer_record(row, viewer_id),
                 **profiles.get(row["author_id"], {})} for row in rows]

    def _operation_replay(self, db, author_id, kind, operation):
        if operation is None:
            return None
        row = db.execute(
            "SELECT target_id,request_hash FROM forum_submission_operations "
            "WHERE author_id=? AND kind=? AND operation_id=?",
            (author_id, kind, operation[0])).fetchone()
        if row is None:
            return None
        if row["request_hash"] != operation[1]:
            raise ForumError("operation conflict", 409)
        table = "forum_threads" if kind == "thread" else "forum_replies"
        stored = db.execute("SELECT * FROM " + table + " WHERE id=? AND author_id=?",
                            (row["target_id"], author_id)).fetchone()
        if stored is None:
            raise ForumError("operation conflict", 409)
        return self._records([stored])[0]

    @staticmethod
    def _record_operation(db, author_id, kind, operation, target_id, now):
        if operation is not None:
            db.execute(
                "INSERT INTO forum_submission_operations "
                "(author_id,kind,operation_id,target_id,request_hash,created) VALUES (?,?,?,?,?,?)",
                (author_id, kind, operation[0], target_id, operation[1], now))

    def create_thread(self, author_id, title, content, author_name="", game_slug=None,
                      *, moderation=None, operation_id=None):
        """Store a trusted server verdict; legacy callers keep pending behavior."""
        author_id = _identity(author_id)
        author_name = _text(author_name, "author name", MAX_AUTHOR_NAME, allow_empty=True)
        title = _text(title, "title", MAX_THREAD_TITLE)
        content = _content(content, "content", MAX_THREAD_CONTENT)
        if game_slug is not None:
            game_slug = _text(game_slug, "game slug", 200)
            if any(char.isspace() for char in game_slug):
                raise ForumError("invalid game slug")
        item_id = uuid.uuid4().hex
        now = int(self.now())
        status, reason, automatic = self._moderation(moderation)
        operation = self._operation_key(operation_id, [title, content, game_slug])
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._operation_replay(db, author_id, "thread", operation)
            if replay is not None:
                return replay
            db.execute(
                "INSERT INTO forum_threads(id,author_id,author_name,title,content,game_slug,"
                "status,moderation_reason,created,updated,approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, author_id, author_name, title, content, game_slug, status, reason,
                 now, now, now if status == "approved" else None))
            self._record_automatic(db, "thread", item_id, automatic, now)
            self._record_operation(db, author_id, "thread", operation, item_id, now)
            row = db.execute("SELECT * FROM forum_threads WHERE id=?", (item_id,)).fetchone()
            return self._records([row])[0]

    def create_reply(self, author_id, thread_id, content, author_name="", parent_reply_id=None,
                     *, moderation=None, operation_id=None):
        """Store a trusted reply verdict under an approved thread and optional parent."""
        author_id = _identity(author_id)
        author_name = _text(author_name, "author name", MAX_AUTHOR_NAME, allow_empty=True)
        thread_id = _text(thread_id, "thread id", 80)
        content = _content(content, "content", MAX_REPLY_CONTENT)
        if parent_reply_id is not None:
            parent_reply_id = _text(parent_reply_id, "reply id", 80)
        reply_id = uuid.uuid4().hex
        now = int(self.now())
        status, reason, automatic = self._moderation(moderation)
        operation = self._operation_key(operation_id, [thread_id, content, parent_reply_id])
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._operation_replay(db, author_id, "reply", operation)
            if replay is not None:
                return replay
            thread = db.execute(
                "SELECT status FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
            if thread is None or thread["status"] != "approved":
                raise ForumError("thread not found", 404)
            if parent_reply_id is not None:
                parent = db.execute(
                    "SELECT thread_id,status FROM forum_replies WHERE id=?",
                    (parent_reply_id,)).fetchone()
                if (parent is None or parent["thread_id"] != thread_id
                        or parent["status"] != "approved"):
                    raise ForumError("reply not found", 404)
            db.execute(
                "INSERT INTO forum_replies(id,thread_id,parent_reply_id,author_id,author_name,"
                "content,status,moderation_reason,created,updated,approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (reply_id, thread_id, parent_reply_id, author_id, author_name,
                 content, status, reason, now, now, now if status == "approved" else None))
            self._record_automatic(db, "reply", reply_id, automatic, now)
            self._record_operation(db, author_id, "reply", operation, reply_id, now)
            return self._records([db.execute(
                "SELECT * FROM forum_replies WHERE id=?", (reply_id,)).fetchone()])[0]

    def list_threads(self, page=1, page_size=20, game_slug=None):
        """Page through approved, non-deleted threads only."""
        page, page_size, offset = _page(page, page_size)
        where = "status='approved'"
        params = []
        if game_slug is not None:
            game_slug = _text(game_slug, "game slug", 200)
            where += " AND game_slug=?"
            params.append(game_slug)
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM forum_threads WHERE " + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT id,author_id,author_name,title,substr(content,1,180) AS content,"
                "game_slug,status,created,updated,approved_at FROM forum_threads WHERE " + where
                + " ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                params + [page_size, offset]).fetchall()
        return {"items": self._records(rows),
                "page": page, "page_size": page_size, "total": total}

    def get_thread(self, thread_id, viewer_id=None):
        """Return an approved thread, or its private state to its author."""
        thread_id = _text(thread_id, "thread id", 80)
        viewer_id = _identity(viewer_id) if viewer_id is not None else None
        with self._db() as db:
            row = db.execute("SELECT * FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
        if row is None or (row["status"] != "approved" and row["author_id"] != viewer_id):
            raise ForumError("thread not found", 404)
        return self._records([row], viewer_id)[0]

    def list_replies(self, thread_id, page=1, page_size=30, viewer_id=None):
        """Page approved replies; an author also sees private states for their thread."""
        thread_id = _text(thread_id, "thread id", 80)
        page, page_size, offset = _page(page, page_size)
        viewer_id = _identity(viewer_id) if viewer_id is not None else None
        with self._db() as db:
            thread = db.execute(
                "SELECT author_id,status FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
            if thread is None or (thread["status"] != "approved"
                                  and thread["author_id"] != viewer_id):
                raise ForumError("thread not found", 404)
            owner_view = viewer_id is not None and thread["author_id"] == viewer_id
            if owner_view:
                where = "thread_id=? AND (status='approved' OR author_id=?)"
                args = [thread_id, viewer_id]
            else:
                where = "thread_id=? AND status='approved'"
                args = [thread_id]
            total = db.execute(
                "SELECT COUNT(*) FROM forum_replies WHERE " + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM forum_replies WHERE " + where
                + " ORDER BY created,id LIMIT ? OFFSET ?",
                args + [page_size, offset]).fetchall()
        return {"items": self._records(rows, viewer_id),
                "page": page, "page_size": page_size, "total": total}

    def list_my_threads(self, author_id, page=1, page_size=20):
        """Return the caller's own threads, including pending/rejected/deleted states."""
        author_id = _identity(author_id)
        page, page_size, offset = _page(page, page_size)
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM forum_threads WHERE author_id=?", (author_id,)).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM forum_threads WHERE author_id=? ORDER BY created DESC,id DESC "
                "LIMIT ? OFFSET ?", (author_id, page_size, offset)).fetchall()
        return {"items": self._records(rows, author_id),
                "page": page, "page_size": page_size, "total": total}

    def list_my_replies(self, author_id, page=1, page_size=20):
        """Return the caller's own replies, including pending and rejected states."""
        author_id = _identity(author_id)
        page, page_size, offset = _page(page, page_size)
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM forum_replies WHERE author_id=?", (author_id,)).fetchone()[0]
            rows = db.execute(
                "SELECT r.*,t.title AS thread_title,t.status AS thread_status "
                "FROM forum_replies r JOIN forum_threads t ON t.id=r.thread_id "
                "WHERE r.author_id=? ORDER BY r.created DESC,r.id DESC LIMIT ? OFFSET ?",
                (author_id, page_size, offset)).fetchall()
        return {"items": self._records(rows, author_id),
                "page": page, "page_size": page_size, "total": total}

    def soft_delete_thread(self, author_id, thread_id):
        """Soft-delete only the caller's own thread; moderation history remains intact."""
        author_id = _identity(author_id)
        thread_id = _text(thread_id, "thread id", 80)
        now = int(self.now())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM forum_threads WHERE id=? AND author_id=?",
                (thread_id, author_id)).fetchone()
            if row is None:
                raise ForumError("thread not found", 404)
            if row["status"] == "deleted":
                raise ForumError("thread already deleted", 409)
            db.execute(
                "UPDATE forum_threads SET status='deleted',updated=? WHERE id=?",
                (now, thread_id))
            row = db.execute("SELECT * FROM forum_threads WHERE id=?", (thread_id,)).fetchone()
            return _public_record(row)

    def soft_delete_reply(self, author_id, reply_id):
        """Soft-delete only the caller's own reply."""
        author_id = _identity(author_id)
        reply_id = _text(reply_id, "reply id", 80)
        now = int(self.now())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM forum_replies WHERE id=? AND author_id=?",
                (reply_id, author_id)).fetchone()
            if row is None:
                raise ForumError("reply not found", 404)
            if row["status"] == "deleted":
                raise ForumError("reply already deleted", 409)
            db.execute(
                "UPDATE forum_replies SET status='deleted',updated=? WHERE id=?",
                (now, reply_id))
            row = db.execute("SELECT * FROM forum_replies WHERE id=?", (reply_id,)).fetchone()
            return _public_record(row)

    def report_content(self, reporter_id, target_type, target_id, reason, details=""):
        """Report an approved thread/reply once per reporter and target."""
        reporter_id = _identity(reporter_id, "reporter id")
        table = self._target(target_type)
        target_id = _text(target_id, "content id", 80)
        if not isinstance(reason, str) or reason not in REPORT_REASONS:
            raise ForumError("invalid report reason")
        if details is None:
            details = ""
        else:
            details = _text(details, "report details", MAX_REPORT_DETAILS, allow_empty=True)
            if any(ord(char) < 32 and char not in "\n\r\t" for char in details):
                raise ForumError("invalid report details")
        now = int(self.now())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT author_id,status FROM " + table + " WHERE id=?", (target_id,)).fetchone()
            if row is None or row["status"] != "approved":
                raise ForumError("content not found", 404)
            if row["author_id"] == reporter_id:
                raise ForumError("cannot report own content", 403)
            prior = db.execute(
                "SELECT id,status,created FROM forum_reports WHERE reporter_id=? "
                "AND target_type=? AND target_id=?",
                (reporter_id, target_type, target_id)).fetchone()
            if prior is not None:
                raise ForumError("content already reported", 409)
            try:
                cursor = db.execute(
                    "INSERT INTO forum_reports(reporter_id,target_type,target_id,reason,details,created) "
                    "VALUES (?,?,?,?,?,?)",
                    (reporter_id, target_type, target_id, reason, details, now))
            except sqlite3.IntegrityError:
                raise ForumError("content already reported", 409)
            report = db.execute(
                "SELECT id,target_type,target_id,reason,details,status,created "
                "FROM forum_reports WHERE id=?", (cursor.lastrowid,)).fetchone()
            return _row_dict(report)

    def list_moderation_queue(self, content_type="all", status="pending", page=1, page_size=30):
        """Administrator-only queue; route code must perform admin authorization."""
        if content_type not in ("all", "thread", "reply"):
            raise ForumError("invalid content type")
        if not isinstance(status, str) or status not in THREAD_STATES:
            raise ForumError("invalid content status")
        page, page_size, offset = _page(page, page_size)
        kinds = ("thread", "reply") if content_type == "all" else (content_type,)
        source = " UNION ALL ".join(
            "SELECT '%s' AS target_type,id,created FROM %s WHERE status=?"
            % (kind, self._target(kind)) for kind in kinds)
        args = [status] * len(kinds)
        with self._db() as db:
            total = sum(db.execute("SELECT COUNT(*) FROM " + self._target(kind)
                                   + " WHERE status=?", (status,)).fetchone()[0] for kind in kinds)
            rows = db.execute(
                "SELECT p.target_type,p.id,COALESCE(t.author_id,r.author_id) AS author_id,"
                "COALESCE(t.author_name,r.author_name) AS author_name,"
                "COALESCE(t.title,parent.title) AS title,COALESCE(t.content,r.content) AS content,"
                "COALESCE(t.game_slug,parent.game_slug) AS game_slug,"
                "COALESCE(t.status,r.status) AS status,"
                "COALESCE(t.moderation_reason,r.moderation_reason) AS moderation_reason,"
                "p.created,COALESCE(t.updated,r.updated) AS updated FROM "
                "(SELECT * FROM (" + source + ") ORDER BY created DESC,id DESC,target_type "
                "LIMIT ? OFFSET ?) p "
                "LEFT JOIN forum_threads t ON p.target_type='thread' AND t.id=p.id "
                "LEFT JOIN forum_replies r ON p.target_type='reply' AND r.id=p.id "
                "LEFT JOIN forum_threads parent ON parent.id=r.thread_id "
                "ORDER BY p.created DESC,p.id DESC,p.target_type",
                args + [page_size, offset]).fetchall()
        return {"items": [_row_dict(row) for row in rows],
                "page": page, "page_size": page_size, "total": total}

    def list_reports(self, status="pending", page=1, page_size=30):
        """Administrator-only report queue; route code must perform admin authorization."""
        if not isinstance(status, str) or status not in REPORT_STATES:
            raise ForumError("invalid report status")
        page, page_size, offset = _page(page, page_size)
        with self._db() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM forum_reports WHERE status=?", (status,)).fetchone()[0]
            rows = db.execute(
                "SELECT r.id,r.reporter_id,r.target_type,r.target_id,r.reason,r.details,r.status,"
                "r.created,r.reviewed_by,r.reviewed_at,"
                "COALESCE(t.author_id,p.author_id) AS content_author_id,"
                "COALESCE(t.title,parent.title) AS content_title,"
                "COALESCE(t.content,p.content) AS content_body,"
                "COALESCE(t.status,p.status) AS content_status "
                "FROM (SELECT * FROM forum_reports WHERE status=? "
                "ORDER BY created DESC,id DESC LIMIT ? OFFSET ?) r "
                "LEFT JOIN forum_threads t ON r.target_type='thread' AND t.id=r.target_id "
                "LEFT JOIN forum_replies p ON r.target_type='reply' AND p.id=r.target_id "
                "LEFT JOIN forum_threads parent ON parent.id=p.thread_id "
                "ORDER BY r.created DESC,r.id DESC",
                (status, page_size, offset)).fetchall()
        return {"items": [_row_dict(row) for row in rows],
                "page": page, "page_size": page_size, "total": total}

    def moderate_content(self, moderator_id, target_type, target_id, action, reason=""):
        """Apply an independent forum moderation decision and append its audit row."""
        moderator_id = _identity(moderator_id, "moderator id")
        table = self._target(target_type)
        target_id = _text(target_id, "content id", 80)
        if not isinstance(action, str) or action not in MODERATION_ACTIONS:
            raise ForumError("invalid moderation action")
        reason = _text(reason, "moderation reason", MAX_MODERATION_REASON, allow_empty=True)
        if action in ("reject", "hide") and not reason:
            raise ForumError("moderation reason required")
        now = int(self.now())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,approved_at FROM " + table + " WHERE id=?", (target_id,)).fetchone()
            if row is None:
                raise ForumError("content not found", 404)
            if row["status"] == "deleted":
                raise ForumError("content deleted", 409)
            if action == "hide" and row["status"] != "approved":
                raise ForumError("only approved content can be hidden", 409)
            new_status = {"approve": "approved", "reject": "rejected", "hide": "hidden"}[action]
            approved_at = now if action == "approve" else row["approved_at"]
            db.execute(
                "UPDATE " + table + " SET status=?,moderation_reason=?,updated=?,approved_at=? "
                "WHERE id=?",
                (new_status, "" if action == "approve" else reason, now, approved_at, target_id))
            db.execute(
                "INSERT INTO forum_decisions(target_type,target_id,actor_id,action,reason,created) "
                "VALUES (?,?,?,?,?,?)", (target_type, target_id, moderator_id, action, reason, now))
            return _row_dict(db.execute(
                "SELECT * FROM " + table + " WHERE id=?", (target_id,)).fetchone())

    def review_report(self, moderator_id, report_id, outcome, reason=""):
        """Close a report as actioned/dismissed and append a separate audit record."""
        moderator_id = _identity(moderator_id, "moderator id")
        if isinstance(report_id, bool) or not isinstance(report_id, int) or report_id < 1:
            raise ForumError("invalid report id")
        if outcome not in ("actioned", "dismissed"):
            raise ForumError("invalid report outcome")
        reason = _text(reason, "review reason", MAX_MODERATION_REASON, allow_empty=True)
        now = int(self.now())
        action = "report_actioned" if outcome == "actioned" else "report_dismissed"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM forum_reports WHERE id=?", (report_id,)).fetchone()
            if row is None:
                raise ForumError("report not found", 404)
            if row["status"] != "pending":
                raise ForumError("report already reviewed", 409)
            db.execute(
                "UPDATE forum_reports SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?",
                (outcome, moderator_id, now, report_id))
            db.execute(
                "INSERT INTO forum_decisions(target_type,target_id,actor_id,action,reason,created) "
                "VALUES ('report',?,?,?,?,?)",
                (str(report_id), moderator_id, action, reason, now))
            return _row_dict(db.execute(
                "SELECT * FROM forum_reports WHERE id=?", (report_id,)).fetchone())

    def list_decisions(self, target_type=None, target_id=None, page=1, page_size=30):
        """Page through append-only moderation audit records (administrator-only route)."""
        if target_type is not None and target_type not in ("thread", "reply", "report"):
            raise ForumError("invalid audit target type")
        if target_id is not None:
            target_id = _text(target_id, "content id", 80)
        page, page_size, offset = _page(page, page_size)
        where = []
        args = []
        if target_type is not None:
            where.append("target_type=?")
            args.append(target_type)
        if target_id is not None:
            where.append("target_id=?")
            args.append(target_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._db() as db:
            source = (
                "SELECT id,target_type,target_id,actor_id,action,reason,created,"
                "'manual' AS source,'' AS rule_version FROM forum_decisions" + clause
                + " UNION ALL SELECT id,target_type,target_id,actor_id,action,reason,created,"
                "source,rule_version FROM forum_automatic_decisions" + clause)
            total = (db.execute("SELECT COUNT(*) FROM forum_decisions" + clause, args).fetchone()[0]
                     + db.execute("SELECT COUNT(*) FROM forum_automatic_decisions" + clause, args).fetchone()[0])
            rows = db.execute(
                "SELECT * FROM (" + source + ") ORDER BY created DESC,id DESC,source LIMIT ? OFFSET ?",
                args + args + [page_size, offset]).fetchall()
        return {"items": [_row_dict(row) for row in rows],
                "page": page, "page_size": page_size, "total": total}
