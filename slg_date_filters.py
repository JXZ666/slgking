"""Natural-day source update filtering, independent of UI and local sync time."""
from datetime import date, datetime, timedelta, timezone
import re

BEIJING = timezone(timedelta(hours=8))

def _alias(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('Invalid SQL alias')
    return value

def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期须为 YYYY-MM-DD')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('日期无效') from exc

def resolve_date_range(preset='all', start=None, end=None, today=None):
    if preset == 'all':
        return None, None
    if preset in ('7', '30', '90', 7, 30, 90):
        current = _date(today) if today is not None else datetime.now(BEIJING).date()
        return (current - timedelta(days=int(preset)-1)).isoformat(), current.isoformat()
    if preset != 'custom':
        raise ValueError('Unknown date preset')
    first, last = _date(start), _date(end)
    if first > last:
        raise ValueError('起始日期不能晚于结束日期')
    return first.isoformat(), last.isoformat()

def updated_date_sql(alias='g'):
    """DLsite metadata is authoritative even when the legacy column is non-null."""
    alias = _alias(alias)
    raw = (f"CASE WHEN EXISTS (SELECT 1 FROM game_catalogs dc WHERE dc.game_id={alias}.id AND dc.catalog_id='dlsite') "
           f"THEN (SELECT json_extract(dm.metadata_json,'$.updated_date') FROM game_metadata dm WHERE dm.game_id={alias}.id) "
           f"ELSE {alias}.last_updated END")
    # Reject empty/invalid dates; timestamps may accompany the source's calendar date.
    day = f'substr(({raw}),1,10)'
    return f"CASE WHEN {day} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND date({day},'+0 days')={day} THEN {day} END"

def date_clause(spec, *, alias='g'):
    first, last = resolve_date_range(spec.get('date_preset', 'all'), spec.get('date_start'), spec.get('date_end'), spec.get('date_today'))
    if first is None:
        return '1', []
    return updated_date_sql(alias) + ' BETWEEN ? AND ?', [first, last]
