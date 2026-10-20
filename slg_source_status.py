"""Small, non-sensitive collector status shared by publication and clients."""
from datetime import datetime, timezone
import re

STATUS_PATHS = {'f95zone': '/catalogues/western_slg/status.json',
                'dlsite': '/catalogues/dlsite/status.json'}
STATES = frozenset(('complete','partial','paused','error','running'))
TIME_FIELDS = ('started_at','finished_at','last_success','last_publish','oldest_pending_at','next_run')
COUNT_FIELDS = ('requests_used','pending_due')

def public_status(value, source_id):
    if source_id not in STATUS_PATHS or not isinstance(value,dict):
        raise ValueError('Invalid source status')
    if value.get('source_id',source_id)!=source_id or value.get('state') not in STATES:
        raise ValueError('Invalid source status identity/state')
    result={'source_id':source_id,'state':value['state']}
    reason=value.get('reason_code')
    result['reason_code']=reason if isinstance(reason,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',reason) else 'unknown'
    for key in TIME_FIELDS:
        stamp=value.get(key)
        if stamp is None: result[key]=None;continue
        try:
            if type(stamp) in (int,float):
                parsed=datetime.fromtimestamp(stamp,timezone.utc)
            elif isinstance(stamp,str) and len(stamp)<=64:
                if re.fullmatch(r'\d{1,12}(?:\.\d{1,6})?',stamp):
                    parsed=datetime.fromtimestamp(float(stamp),timezone.utc)
                else:
                    parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
            else:raise ValueError()
            result[key]=parsed.astimezone(timezone.utc).isoformat()
        except (ValueError,TypeError,OverflowError,OSError):
            raise ValueError('Invalid source status timestamp') from None
    for key in COUNT_FIELDS:
        number=value.get(key,0)
        if type(number) is not int or not 0<=number<=100_000_000:
            raise ValueError('Invalid source status count')
        result[key]=number
    return result

def status_note(value):
    if not isinstance(value,dict) or value.get('state') not in STATES or value.get('source_id') not in STATUS_PATHS:return ''
    if value.get('reason_code')=='awaiting_first_run':return ('F95镜像' if value['source_id']=='f95zone' else 'DLsite')+'：等待首次采集'
    label='F95镜像' if value['source_id']=='f95zone' else 'DLsite'
    state={'complete':'本轮采集完成','partial':'预算内分批推进','paused':'采集暂停','error':'采集失败','running':'采集中'}[value['state']]
    return '%s：%s' % (label,state)
