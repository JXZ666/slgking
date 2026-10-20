"""Privacy-preserving aggregates for successful cloud business events."""

from datetime import date, datetime, time as datetime_time, timedelta
from datetime import timezone

from slg_cloud_store import CHINA, DEVELOPER_ACCOUNT_ID


EVENT_KEYS = ("signin", "lottery", "shop_purchase", "group_redeem")


def aggregate_cloud_platforms(conn, days, today=None):
    """Count actual committed business rows; old provenance remains unknown."""
    last_day = _as_china_date(today)
    start = _day_start_epoch(last_day - timedelta(days=max(1, int(days)) - 1))
    end = _day_start_epoch(last_day + timedelta(days=1))
    result = {p: _empty_events() for p in ('windows', 'android', 'web', 'unknown')}
    has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_platforms'").fetchone()
    for table, source, reasons in (('ledger', 'ledger', {'signin': 'signin', 'shop': 'shop_purchase', 'group_code': 'group_redeem'}),
                                   ('lottery_draws', 'lottery', None)):
        platform = "COALESCE(b.platform,'unknown')" if has_table else "'unknown'"
        join = "LEFT JOIN business_platforms b ON b.origin=?||':'||a.id" if has_table else ''
        event = 'a.reason' if reasons else "'lottery'"
        query = f"SELECT {platform},{event},COUNT(*) FROM {table} a {join} WHERE a.ts>=? AND a.ts<? AND a.account_id<>?"
        params = ([source] if has_table else []) + [start, end, DEVELOPER_ACCOUNT_ID]
        if reasons:
            query += " AND a.reason IN ('signin','shop','group_code')"
        query += f" GROUP BY {platform},{event}"
        for p, e, count in conn.execute(query, params):
            p = p if p in result else 'unknown'
            result[p][reasons[e] if reasons else e] += count
    return result


def _as_china_date(value):
    if value is None:
        return datetime.now(CHINA).date()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(CHINA).date()
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("today must be a date or ISO date") from exc
    raise TypeError("today must be a date, datetime, ISO date, or None")


def _day_start_epoch(day):
    """Return the epoch for the start of a Shanghai calendar day."""
    local_start = datetime.combine(day, datetime_time(0, 0)).replace(tzinfo=CHINA)
    return int(local_start.timestamp())


def _empty_events():
    return {key: 0 for key in EVENT_KEYS}


def _daily_bucket(day, events):
    counts = {key: int(events.get(key, 0)) for key in EVENT_KEYS}
    iso_day = day.isoformat()
    return {
        "start": iso_day,
        "end": iso_day,
        "label": iso_day,
        "events": counts,
        "total": sum(counts.values()),
    }


def aggregate_cloud_business_events(conn, days, today=None):
    """Aggregate persisted cloud activity into Shanghai calendar-day buckets.

    Only event timestamps and aggregate counts are returned. Account IDs,
    credentials, devices, and client-reported telemetry are never selected for
    or included in the result.

    Weekly buckets are consecutive groups of up to seven selected calendar
    days, anchored backwards from ``today``. The first group is shorter when
    ``days`` is not a multiple of seven.
    """
    if isinstance(days, bool):
        raise ValueError("days must be a positive integer")
    try:
        range_days = int(days)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("days must be a positive integer") from exc
    range_days = max(1, range_days)

    last_day = _as_china_date(today)
    first_day = last_day - timedelta(days=range_days - 1)
    start_ts = _day_start_epoch(first_day)
    end_ts = _day_start_epoch(last_day + timedelta(days=1))

    # Group by timestamp in SQLite so no account-level row values leave the
    # database cursor. Sign-in claim keys have a UNIQUE constraint.
    query = """
        SELECT 'signin' AS event, ts, COUNT(*) AS amount
        FROM ledger
        WHERE reason='signin' AND account_id<>? AND ts>=? AND ts<?
        GROUP BY ts
        UNION ALL
        SELECT 'lottery' AS event, ts, COUNT(*) AS amount
        FROM lottery_draws
        WHERE account_id<>? AND ts>=? AND ts<?
        GROUP BY ts
        UNION ALL
        SELECT 'shop_purchase' AS event, ts, COUNT(*) AS amount
        FROM ledger
        WHERE reason='shop' AND account_id<>? AND ts>=? AND ts<?
        GROUP BY ts
        UNION ALL
        SELECT 'group_redeem' AS event, ts, COUNT(*) AS amount
        FROM ledger
        WHERE reason='group_code' AND account_id<>? AND ts>=? AND ts<?
        GROUP BY ts
    """
    params = (
        DEVELOPER_ACCOUNT_ID, start_ts, end_ts,
        DEVELOPER_ACCOUNT_ID, start_ts, end_ts,
        DEVELOPER_ACCOUNT_ID, start_ts, end_ts,
        DEVELOPER_ACCOUNT_ID, start_ts, end_ts,
    )

    counts_by_day = {}
    for event, timestamp, amount in conn.execute(query, params):
        try:
            local_time = datetime.fromtimestamp(
                int(timestamp), timezone.utc).astimezone(CHINA)
        except (OverflowError, OSError, TypeError, ValueError):
            continue
        calendar_day = local_time.date()
        if first_day <= calendar_day <= last_day and event in EVENT_KEYS:
            bucket = counts_by_day.setdefault(calendar_day, _empty_events())
            bucket[event] += int(amount)

    daily = [
        _daily_bucket(first_day + timedelta(days=offset), counts_by_day.get(
            first_day + timedelta(days=offset), {}))
        for offset in range(range_days)
    ]

    weekly = []
    back = 0
    while back < range_days:
        end_day = last_day - timedelta(days=back)
        start_day = max(first_day, end_day - timedelta(days=6))
        weekly_events = _empty_events()
        cursor = start_day
        while cursor <= end_day:
            for key, value in counts_by_day.get(cursor, {}).items():
                weekly_events[key] += value
            cursor += timedelta(days=1)
        weekly.append({
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "label": (start_day.isoformat() if start_day == end_day else
                      start_day.isoformat() + " 至 " + end_day.isoformat()),
            "events": weekly_events,
            "total": sum(weekly_events.values()),
        })
        back += (end_day - start_day).days + 1
    weekly.reverse()

    return {"range_days": range_days, "daily": daily, "weekly": weekly}
