"""Reviewed collector ceilings and first-tier defaults; requests are not works."""
TIERS = {"f95zone": ((54, 1200), (81, 1500), (108, 2100)),
         "dlsite": ((300, 3600), (450, 5400), (600, 7200))}
DEFAULTS = {source: values[0] for source, values in TIERS.items()}
MAX_REQUESTS = {source: values[-1][0] for source, values in TIERS.items()}
MAX_SECONDS = {source: values[-1][1] for source, values in TIERS.items()}

def validate_limits(source, requests=None, seconds=None):
    default_requests, default_seconds = DEFAULTS[source]
    requests = default_requests if requests is None else requests
    seconds = default_seconds if seconds is None else seconds
    if isinstance(requests, bool) or not isinstance(requests, int) or not 1 <= requests <= MAX_REQUESTS[source]:
        raise ValueError("request budget out of range")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 30 <= seconds <= MAX_SECONDS[source]:
        raise ValueError("time budget out of range")
    return requests, seconds
