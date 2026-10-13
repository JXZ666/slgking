"""Safe, side-effect-free DLsite pause and diagnostic policy."""
import datetime
import email.utils
import urllib.parse
import hashlib
import re

def response_evidence(response):
    """Bounded evidence only; never persist response text, cookies or headers.

    A marker is an observation, not proof of IP/region blocking. Failure to
    collect diagnostic evidence must never mask the original refusal.
    """
    result = {}
    try:
        headers = getattr(response, 'headers', None) or {}
        mime = str(headers.get('Content-Type', '')).split(';', 1)[0].strip().lower()
        if re.fullmatch(r'[a-z0-9.+-]{1,64}/[a-z0-9.+-]{1,64}', mime):
            result['content_type'] = mime
        result['response_url'] = safe_url(response.geturl())
        # Hash a fixed prefix; neither raw HTML nor exception text leaves RAM.
        raw = response.read(8192)
        if isinstance(raw, bytes):
            result['prefix_bytes'] = len(raw)
            result['prefix_sha256'] = hashlib.sha256(raw).hexdigest()
            lower = raw.lower()
            markers = {
                'human_verification': (b'cf-chl-', b'g-recaptcha', b'verify you are human'),
                'access_denied': (b'access denied', b'forbidden'),
            }
            result['observed_markers'] = [key for key, values in markers.items() if any(value in lower for value in values)]
    except Exception as error:
        result['evidence_exception_type'] = type(error).__name__
    return result

NETWORK_KINDS = frozenset(('network','server_error','robots_parse'))
def pause_decision(control, now):
    if any(control.get(k) for k in ('manual_pause','permanent_pause','pause')):
        return dict(allowed=False,probe_due=False,reason='manual_pause',next_check_at=None)
    until=float(control.get('pause_until') or 0)
    stopped=control.get('state') in ('paused','stopped') or bool(control.get('reason')) and until>0
    return dict(allowed=not stopped and until<=now,probe_due=stopped and until<=now,
                reason='pause_active' if until>now else 'protocol_check_required' if stopped else None,
                next_check_at=until or None)

def retry_delay(kind, failures=1, retry_after=None, now=0):
    base=(900,3600,21600)[min(2,max(0,int(failures)-1))] if kind in NETWORK_KINDS else 86400
    try: extra=max(0,float(retry_after))
    except (TypeError,ValueError):
        try: extra=max(0,email.utils.parsedate_to_datetime(retry_after).timestamp()-now)
        except (ValueError,TypeError,OverflowError): extra=0
    return max(base,extra)

def safe_url(url):
    try:
        parsed=urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme,parsed.hostname or '',parsed.path,'',''))
    except (ValueError,TypeError): return ''

def safe_diagnostic(kind, stage, url='', status=None, exception=None, redirects=(), now=0, next_check_at=None):
    return dict(kind=kind,stage=stage,url=safe_url(url),http_status=status,
        exception_type=type(exception).__name__ if exception else None,
        redirects=[safe_url(x) for x in redirects][-8:],checked_at=now,next_check_at=next_check_at)
