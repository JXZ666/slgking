"""Run one isolated DLsite increment or publish already verified ledger data."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from slg_dlsite_worker import Ledger, WorkerClient, publish, worker_lock
from slg_dlsite import atomic_write
from slg_collector_policy import DEFAULTS, validate_limits
import time
from slg_dlsite_recovery import pause_decision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seed', '--bootstrap', dest='seed', type=Path)
    parser.add_argument('--recovery-only',action='store_true',help='No upstream requests unless a temporary pause is due for protocol recheck')
    parser.add_argument('--publish-only', action='store_true')
    parser.add_argument('--restore-only', action='store_true')
    parser.add_argument('--status-file', type=Path)
    parser.add_argument('--lock-path', type=Path)
    parser.add_argument('--covers-only', action='store_true', help='Refresh known facts to extract and fetch main covers; no discovery')
    parser.add_argument('--max-requests', type=int, default=DEFAULTS['dlsite'][0])
    parser.add_argument('--daily-limit',type=int,help='Persistent daily ceiling, independent of this invocation limit')
    parser.add_argument('--max-seconds', type=int, default=DEFAULTS['dlsite'][1])
    args = parser.parse_args(argv)
    if args.publish_only and (args.recovery_only or args.restore_only or args.covers_only):
        parser.error('publish-only is an independent offline mode')
    try:
        validate_limits('dlsite', args.max_requests, args.max_seconds)
        daily_limit = args.daily_limit if args.daily_limit is not None else max(DEFAULTS['dlsite'][0],args.max_requests)
        validate_limits('dlsite',daily_limit,args.max_seconds)
    except ValueError as error:
        parser.error(str(error))
    started = time.time()
    status_file = args.status_file or args.state / 'status.json'
    def report(ledger, state, reason=None):
        facts = ledger.status()
        result = {'source_id':'dlsite','state':state,'reason_code':reason,
                  'started_at':started,'finished_at':None if state=='running' else time.time(),
                  'last_success':facts['last_success'],'last_publish':facts['last_publish'],
                  'requests_used':facts['request_used'],'pending_due':facts['pending'],
                  'oldest_pending_at':facts['oldest_due'],'active_hash':facts['active_hash'],
                  'next_check_at':ledger._get('pause_until',None),'access_kind':ledger._get('reason',None),
                  'daily_new_target':50,'daily_new_count':ledger.daily_new_count(),
                  'protocol_checked_at':ledger._get('last_protocol_success',None),
                  'recovery_check':args.recovery_only,
                  'resume_ready':bool(args.recovery_only and state in ('complete','partial') and float(ledger._get('last_protocol_success',0) or 0)>=started)}
        atomic_write(status_file, result)
        return result
    with worker_lock(args.state, args.lock_path):
        ledger = Ledger(args.state, max_requests=daily_limit)
        try:
            if args.publish_only:
                # Offline publication does not begin, finish or reset acquisition.
                acquisition_state=ledger._get('state','partial')
                acquisition_reason=ledger._get('reason',None)
                try:
                    previous=json.loads(status_file.read_text(encoding='utf-8'))
                    if not isinstance(previous,dict):previous={}
                    previous={key:value for key,value in previous.items() if key in ('source_id','state','reason_code','started_at','finished_at','last_success','last_publish','requests_used','pending_due','oldest_pending_at','active_hash','next_check_at','access_kind','daily_new_target','daily_new_count','protocol_checked_at','recovery_check','resume_ready')}
                except (OSError,ValueError):
                    previous={}
                try:
                    manifest=publish(ledger,args.output)
                except Exception:
                    ledger.event('publication_failed',acquisition_state,'failed')
                    print(json.dumps({'source_id':'dlsite','publication_status':'error',
                                      'state':{'stopped':'paused','failed':'error'}.get(acquisition_state,acquisition_state),
                                      'reason_code':acquisition_reason,'publication_reason':'validation_or_publication_failed'}))
                    return 1
                facts=ledger.status()
                # Preserve real acquisition timestamps, counters and safety reason.
                previous.update(source_id='dlsite',
                    state={'stopped':'paused','failed':'error','offline':'partial'}.get(acquisition_state,acquisition_state),
                    reason_code=acquisition_reason,last_success=facts['last_success'],
                    last_publish=facts['last_publish'],active_hash=facts['active_hash'],
                    next_check_at=ledger._get('pause_until',None),requests_used=facts['request_used'],
                    pending_due=facts['pending'],oldest_pending_at=facts['oldest_due'],
                    daily_new_target=50,daily_new_count=ledger.daily_new_count(),
                    publication_status='complete',resume_ready=False)
                if previous['state'] not in ('complete','partial','paused','error','running'):
                    previous['state']='partial'
                atomic_write(status_file,previous)
                print(json.dumps(previous,ensure_ascii=False,indent=2))
                return 0  # Publication success, not acquisition success.
            if args.recovery_only:
                decision=pause_decision({key:ledger._get(key,None) for key in ('state','pause_until','reason','manual_pause','permanent_pause','pause')},time.time())
                if not decision['probe_due']:
                    print(json.dumps({'status':'waiting','reason':decision['reason'],'next_check_at':decision['next_check_at']}))
                    return 0
            report(ledger, 'running')
            if args.publish_only or args.restore_only:
                ledger.event('started','offline')
            bootstrap_covers = ledger.adopt_publication(args.output)
            if args.seed:
                if args.seed.is_file():
                    ledger.seed(args.seed)
            result = {'status': 'offline', **ledger.stats()}
            if not args.publish_only and not args.restore_only:
                client = WorkerClient(args.state / 'http', args.max_requests, args.max_seconds)
                original_reserve = ledger.reserve_request
                def heartbeat():
                    original_reserve()
                    report(ledger, 'running')
                ledger.reserve_request = heartbeat
                result = ledger.run(client, discovery=not args.covers_only, refresh=not args.covers_only, protocol_only=args.recovery_only)
            result['bootstrap_covers'] = bootstrap_covers
            if 'metrics' in result:
                atomic_write(args.state / 'last-run-metrics.json', {'run_id':result['run_id'],'metrics':result['metrics'],'status':result['status']})
            atomic_write(args.state / 'artwork-backlog.json', ledger.stats()['artwork_backlog'])
            # A later access stop does not invalidate facts positively verified earlier.
            changed_verified=any(result.get('metrics',{}).get(key,0)>0 for key in ('new_verified','facts_updated','covers_added'))
            if not args.restore_only and not args.recovery_only and (result['status'] not in ('failed','stopped') or result['status']=='stopped' and changed_verified):
                try:
                    result['manifest'] = publish(ledger, args.output)
                except Exception:
                    if result['status']!='stopped':
                        raise
                    # A publication failure must not overwrite a real HTTP refusal.
                    result['publication_error']='validation_or_publication_failed'
            state = {'failed':'error','stopped':'paused','partial':'partial'}.get(result['status'],'complete')
            ledger.event('finished', result['status'], 'complete' if 'manifest' in result else 'retained')
            final_report=report(ledger,state,result.get('reason'))
            if result.get('publication_error'):
                final_report.update(publication_status='error',publication_reason=result['publication_error'])
                atomic_write(status_file,final_report)
            print(json.dumps(final_report,ensure_ascii=False,indent=2))
            return {'error':1,'paused':2,'partial':3}.get(state,0)
        except Exception:
            ledger.event('finished', 'failed', 'failed')
            print(json.dumps(report(ledger, 'error', 'validation_or_publication_failed'), ensure_ascii=False, indent=2))
            return 1
        finally:
            ledger.close()


if __name__ == '__main__':
    raise SystemExit(main())
