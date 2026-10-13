"""Private, durable anonymous usage aggregates. Never stores report payloads."""
import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))
PLATFORMS = ("windows", "android", "web", "unknown")
FEATURES = ("catalogue", "search", "detail", "library", "forum", "shop", "tools")
TOOLS = frozenset(("check_updates", "collection_manager", "backup_restore",
                  "translation_tools", "tag_translations", "preference_weights",
                  "translation_settings", "scan_local_library", "sync_maintenance"))
IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
VERSION = re.compile(r"[A-Za-z0-9.+_-]{1,40}\Z")


def _timestamp(value):
    if isinstance(value, bool):
        raise ValueError("bad timestamp")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str) and len(value) <= 64:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI)
        result = dt.timestamp()
    else:
        raise ValueError("bad timestamp")
    if not math.isfinite(result) or result < 0 or result > 253402214400:
        raise ValueError("bad timestamp")
    return result


def sanitize_event(event):
    """Accept narrow v1 compatibility and v2; drop legacy business metadata."""
    if not isinstance(event, dict) or set(event) - {
            "app", "v", "device", "ts", "event", "data", "event_id"}:
        return None
    v = event.get("v", "1")
    kind, data = event.get("event"), event.get("data", {})
    if event.get("app", "slgking") != "slgking" or v not in ("1", "2"):
        return None
    if not isinstance(kind, str) or not isinstance(data, dict):
        return None
    for field in ("device", "event_id"):
        if field in event and (not isinstance(event[field], str)
                               or not IDENTIFIER.fullmatch(event[field])):
            return None
    if "ts" in event:
        try:
            if not isinstance(event["ts"], str):
                return None
            _timestamp(event["ts"])
        except (ValueError, OverflowError):
            return None
    clean_data = {}
    if v == "2":
        allowed = {"platform", "client_version"}
        if kind == "feature":
            allowed.add("feature")
        elif kind not in ("launch", "active"):
            return None
        if set(data) - allowed or data.get("platform") not in PLATFORMS[:-1]:
            return None
        if "event_id" not in event:
            return None
        if kind == "feature" and data.get("feature") not in FEATURES:
            return None
        clean_data = dict(data)
    elif kind == "startup":
        if data != {"platform": "android", "app": "slgking"}:
            return None
        clean_data = {"platform": "android", "app": "slgking"}
    else:
        fields = {
            "launch": {"platform", "client_version", "games"},
            "snapshot": {"points", "titles", "collection"},
            "signin": {"dev"}, "lottery": {"grand"},
            "buy": {"item"}, "redeem": {"success"},
            "tools_open": set(), "settings_open": set(),
            "tool_open": {"tool_id"},
        }.get(kind)
        if fields is None or set(data) - fields:
            return None
        for key, value in data.items():
            if key == "platform":
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", value):
                    return None
                clean_data[key] = value
            elif key == "client_version":
                clean_data[key] = value
            elif key in ("games", "points", "titles", "collection"):
                if type(value) is not int or not 0 <= value <= 10000000:
                    return None
            elif key in ("dev", "grand", "success"):
                if type(value) is not bool:
                    return None
            elif key == "item":
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value):
                    return None
            elif key == "tool_id":
                if not isinstance(value, str) or value not in TOOLS:
                    return None
                clean_data[key] = value
        if kind == "tool_open" and set(data) != {"tool_id"}:
            return None
    version = clean_data.get("client_version")
    if version is not None and (not isinstance(version, str) or not VERSION.fullmatch(version)):
        return None
    result = {"app": "slgking", "v": v, "event": kind, "data": clean_data}
    for field in ("device", "event_id", "ts"):
        if field in event:
            result[field] = event[field]
    return result


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _legacy_key(event):
    return _hash("legacy:" + json.dumps(event, sort_keys=True, separators=(",", ":")))


def _day(timestamp):
    return datetime.fromtimestamp(timestamp, SHANGHAI).date().isoformat()


class UsageStore:
    """Each operation owns its connection; safe for threaded HTTP handlers."""
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY, dedup TEXT UNIQUE, received REAL NOT NULL,
                    day TEXT NOT NULL, platform TEXT NOT NULL, device_hash TEXT,
                    kind TEXT NOT NULL, feature TEXT, version TEXT,
                    historical INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS usage_days ON usage_events(day,platform);
                CREATE TABLE IF NOT EXISTS usage_imports (
                    source_hash TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,
                    prefix TEXT, prefix_length INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS usage_meta (key TEXT PRIMARY KEY,value TEXT);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(usage_imports)")}
            if "prefix" not in columns:
                db.execute("ALTER TABLE usage_imports ADD COLUMN prefix TEXT")
            if "prefix_length" not in columns:
                db.execute("ALTER TABLE usage_imports ADD COLUMN prefix_length INTEGER NOT NULL DEFAULT 0")
            db.execute("INSERT OR IGNORE INTO usage_meta VALUES ('started_at',?)", (str(time.time()),))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _insert(db, event, timestamp, dedup=None, historical=False):
        data = event["data"]
        platform = data.get("platform", "unknown").lower()
        if platform in ("win", "win32", "win64"):
            platform = "windows"
        if platform not in PLATFORMS:
            platform = "unknown"
        device = event.get("device")
        device_hash = _hash(platform + ":" + device) if device else None
        if event.get("event_id"):
            dedup = _hash("event:" + platform + ":" + (device or "") + ":" + event["event_id"])
        feature = data.get("feature")
        if event["event"] in ("tools_open", "tool_open", "settings_open"):
            feature = "tools"
        return db.execute("""INSERT OR IGNORE INTO usage_events
            (dedup,received,day,platform,device_hash,kind,feature,version,historical)
            VALUES (?,?,?,?,?,?,?,?,?)""", (dedup, timestamp, _day(timestamp), platform,
                device_hash, event["event"], feature, data.get("client_version"), int(historical))).rowcount == 1

    @staticmethod
    def _prune(db, now):
        first = (datetime.fromtimestamp(now, SHANGHAI).date() - timedelta(days=89)).isoformat()
        db.execute("DELETE FROM usage_events WHERE day < ?", (first,))

    def record(self, event, received_at=None):
        clean = sanitize_event(event)
        if clean is None:
            raise ValueError("invalid usage event")
        now = _timestamp(time.time() if received_at is None else received_at)
        with self._connect() as db:
            inserted = self._insert(db, clean, now, _legacy_key(clean) if "ts" in clean else None)
            self._prune(db, now)
            return inserted

    def import_jsonl(self, paths, now=None):
        """Incremental import offsets are committed with inserts, including bad lines."""
        now = _timestamp(time.time() if now is None else now)
        cutoff = (datetime.fromtimestamp(now, SHANGHAI).date() - timedelta(days=89)).isoformat()
        result = dict(imported=0, skipped=0, invalid=0, missing=0)
        for path in paths:
            source = _hash(str(Path(path).resolve()))
            try:
                fh = open(path, "rb")
            except FileNotFoundError:
                result["missing"] += 1
                continue
            with fh, self._connect() as db:
                row = db.execute("SELECT offset,prefix,prefix_length FROM usage_imports WHERE source_hash=?", (source,)).fetchone()
                offset = row[0] if row else 0
                prefix = hashlib.sha256(fh.read(row[2])).hexdigest() if row else None
                if Path(path).stat().st_size < offset or (row and row[1] and prefix != row[1]):
                    offset = 0
                fh.seek(offset)
                while True:
                    start = fh.tell()
                    line = fh.readline(8193)
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        if len(line) <= 8192:
                            fh.seek(start)
                            break  # Writer may not have finished its last line.
                        while line and not line.endswith(b"\n"):
                            line = fh.readline(8193)
                        result["invalid"] += 1
                        continue
                    try:
                        raw = json.loads(line)
                        clean = sanitize_event(raw)
                        if clean is None or "ts" not in clean:
                            raise ValueError("invalid historical event")
                        stamp = _timestamp(clean["ts"])
                        if stamp > now + 300 or _day(stamp) < cutoff:
                            result["skipped"] += 1
                            continue
                        dedup = _legacy_key(clean)
                        inserted = self._insert(db, clean, min(stamp, now), dedup, True)
                        result["imported" if inserted else "skipped"] += 1
                    except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
                        result["invalid"] += 1
                offset = fh.tell()
                prefix_length = min(offset, 4096)
                fh.seek(0)
                prefix = hashlib.sha256(fh.read(prefix_length)).hexdigest()
                db.execute("INSERT OR REPLACE INTO usage_imports (source_hash,offset,prefix,prefix_length) VALUES (?,?,?,?)",
                           (source, offset, prefix, prefix_length))
                self._prune(db, now)
        return result

    def stats(self, days=7, now=None):
        if type(days) is not int or days not in (1, 7, 14, 30, 90):
            raise ValueError("days must be 1, 7, 14, 30 or 90")
        now = _timestamp(time.time() if now is None else now)
        today = datetime.fromtimestamp(now, SHANGHAI).date()
        first = (today - timedelta(days=days - 1)).isoformat()
        with self._connect() as db:
            rows = db.execute("""SELECT received,day,platform,device_hash,kind,feature,version,historical
                FROM usage_events WHERE day>=? AND received<=? ORDER BY received,id""", (first, now)).fetchall()
            started = db.execute("SELECT value FROM usage_meta WHERE key='started_at'").fetchone()[0]
        platforms = []
        for platform in PLATFORMS:
            selected = [r for r in rows if r[2] == platform]
            features = {feature: sum(r[5] == feature for r in selected) for feature in FEATURES}
            versions = {}
            latest_versions = {}
            for r in selected:
                if r[4] in ("launch", "startup", "active") and r[3]:
                    latest_versions[r[3]] = r[6] or "unknown"
            for device, version in latest_versions.items():
                versions.setdefault(version, set()).add(device)
            by_day = []
            day_rows = {}
            for row in selected:
                day_rows.setdefault(row[1], []).append(row)
            for index in range(days):
                day = (today - timedelta(days=days - 1 - index)).isoformat()
                daily = day_rows.get(day, [])
                by_day.append({"day": day, "active_installs": len({r[3] for r in daily if r[3]}),
                               "events": len(daily), "launches": sum(r[4] in ("launch", "startup") for r in daily)})
            platforms.append({"platform": platform, "active_installs": len({r[3] for r in selected if r[3]}),
                "events": len(selected), "launches": sum(r[4] in ("launch", "startup") for r in selected),
                "feature_counts": features, "version_distribution": {k: len(v) for k, v in sorted(versions.items())},
                "by_day": by_day, "last_report": max((r[0] for r in selected), default=None),
                "coverage": {"identified_events": sum(bool(r[3]) for r in selected),
                             "anonymous_without_id": sum(not r[3] for r in selected),
                             "historical_events": sum(r[7] for r in selected),
                             "data_start": min((r[1] for r in selected), default=None)}})
        return {"available": True, "days": days, "timezone": "Asia/Shanghai", "from": first,
                "to": today.isoformat(), "platforms": platforms,
                "coverage": {"started_at": float(started), "data_start": min((r[1] for r in rows), default=None),
                             "historical_android_gap": True, "business_source": "cloud_success_records"}}
