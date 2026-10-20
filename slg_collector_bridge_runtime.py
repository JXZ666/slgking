"""Fixed JSON RPC isolation for the legacy collector's conflicting module names.

The account HTTP process never imports the worker or changes sys.path/modules.
Only its private child imports the reviewed worker. No network methods are exposed.
"""
import atexit
from contextlib import redirect_stdout
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading

MAX_LINE=65536


class RuntimeFailure(RuntimeError):
    pass


class IsolatedProductionAdapters:
    """Drop-in bridge adapter, using only paths and a parent-side pause callback.

    jobs_path is the actual existing CollectionJobs DB, not a new quota DB.
    worker_directory must be the complete fresh production dlsite-worker tree.
    A crashed/timed-out child is NOT retried automatically: an uncertain budget
    reservation may already be committed. Recreate the facade after inspection.
    """
    def __init__(self, *, worker_directory, server_directory, ledger_directory,
                 jobs_path, mapping_path, lock_path, manual_pause, output,
                 bridge_path, timeout=45, publish_timeout=300, python=None):
        self.config={k:str(Path(v).resolve()) for k,v in {
            'worker_directory':worker_directory,'server_directory':server_directory,
            'ledger_directory':ledger_directory,'jobs_path':jobs_path,
            'mapping_path':mapping_path,'lock_path':lock_path,'output':output,
            'bridge_path':bridge_path}.items()}
        self.manual_pause=manual_pause
        self.timeout=timeout;self.publish_timeout=publish_timeout
        self.python=python or sys.executable
        self._mutex=threading.RLock();self._process=None
        self._responses=queue.Queue();self._sequence=0;self._closed=False
        atexit.register(self.close)

    def pause_gate(self):
        return bool(self.manual_pause())

    def _read(self,stream):
        try:
            while True:
                line=stream.readline(MAX_LINE+1)
                if not line:break
                if len(line)>MAX_LINE:
                    self._responses.put(None);return
                try:self._responses.put(json.loads(line))
                except (ValueError,UnicodeError):self._responses.put(None);return
        except (OSError,ValueError):pass
        finally:self._responses.put(None)

    def _start(self):
        if self._closed:raise RuntimeFailure('collector_runtime_closed')
        if self._process is not None:return
        path=Path(__file__).resolve()
        kwargs={}
        if sys.platform=='win32':kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
        self._process=subprocess.Popen([self.python,'-I','-u',str(path),'--worker'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            cwd=str(path.parent),**kwargs)
        threading.Thread(target=self._read,args=(self._process.stdout,),daemon=True).start()
        try:self._exchange('init',self.config,self.timeout)
        except Exception:
            self.close();raise

    def _exchange(self,action,args,timeout):
        self._sequence+=1;request_id=self._sequence
        packet=json.dumps({'id':request_id,'action':action,'args':args},ensure_ascii=True,allow_nan=False).encode()+b'\n'
        if len(packet)>MAX_LINE:raise RuntimeFailure('collector_rpc_too_large')
        try:
            self._process.stdin.write(packet);self._process.stdin.flush()
            response=self._responses.get(timeout=timeout)
        except (OSError,ValueError,queue.Empty):
            self.close();raise RuntimeFailure('collector_runtime_unavailable') from None
        if not isinstance(response,dict) or response.get('id')!=request_id:
            self.close();raise RuntimeFailure('collector_runtime_protocol_failed')
        if not response.get('ok'):
            from slg_collector_bridge import BridgeError
            code=response.get('error')
            if not isinstance(code,str) or len(code)>100:code='collector_runtime_failed'
            raise BridgeError(code,int(response.get('status',503)))
        return response.get('result')

    def _rpc(self,action,args=None,publish=False):
        with self._mutex:
            if self._closed:raise RuntimeFailure('collector_runtime_closed')
            self._start()
            return self._exchange(action,args or {},self.publish_timeout if publish else self.timeout)

    def bind_manual(self,task_id,job_id):
        if self.pause_gate():
            from slg_collector_bridge import BridgeError
            raise BridgeError('manual_paused',409)
        return self._rpc('bind_manual',{'task_id':task_id,'job_id':job_id})

    def manual_job(self,task_id):return self._rpc('manual_job',{'task_id':task_id})
    def check_ids(self,ids):return self._rpc('check_ids',{'ids':ids})
    def hold(self,task_id):
        if self.pause_gate():return False
        return self._rpc('hold',{'task_id':task_id})
    def heartbeat(self,task_id):return self._rpc('heartbeat',{'task_id':task_id})
    def sweep(self):return self._rpc('sweep')
    def release(self,task_id):return self._rpc('release',{'task_id':task_id})

    def budget_gate(self, *,mode,url,task_id):
        from slg_collector_bridge import BridgeError
        if self.pause_gate():raise BridgeError('manual_paused',409)
        return self._rpc('budget_gate',{'mode':mode,'url':url,'task_id':task_id})

    def publish_task(self,store,task_id):
        from slg_collector_bridge import BridgeError
        if self.pause_gate():raise BridgeError('manual_paused',409)
        if str(Path(store.path).resolve())!=self.config['bridge_path']:
            raise BridgeError('unexpected_bridge_database',409)
        return self._rpc('publish_task',{'task_id':task_id},publish=True)

    def close(self):
        with self._mutex:
            self._closed=True
            process=self._process
            if process is None:return
            try:
                if process.poll() is None:process.terminate()
                process.wait(timeout=5)
            except (OSError,subprocess.TimeoutExpired):
                try:process.kill();process.wait(timeout=5)
                except (OSError,subprocess.TimeoutExpired):pass
            for stream in (process.stdin,process.stdout):
                try:stream.close()
                except (OSError,ValueError):pass


def _worker():
    """Private subprocess; whitelist only, no import/eval/subprocess RPC."""
    from contextlib import redirect_stderr
    import re
    import time
    inbox=queue.Queue()
    protocol=sys.stdout
    def read_input():
        try:
            while True:
                line=sys.stdin.buffer.readline(MAX_LINE+1)
                if not line:break
                if len(line)>MAX_LINE:break
                inbox.put(json.loads(line))
        except (OSError,ValueError,UnicodeError):pass
        finally:inbox.put(None)
    threading.Thread(target=read_input,daemon=True).start()
    adapter=None;store=None;store_path=None
    root=Path(__file__).resolve().parent
    try:
        while True:
            try:request=inbox.get(timeout=1)
            except queue.Empty:
                if adapter:adapter.sweep()
                continue
            if request is None:break
            rid=request.get('id') if isinstance(request,dict) else None
            reply={'id':rid,'ok':False,'error':'collector_rpc_invalid','status':400}
            try:
                if not isinstance(request,dict) or set(request)!= {'id','action','args'}:
                    raise ValueError()
                action=request['action'];args=request['args']
                if not isinstance(args,dict):raise ValueError()
                # Legacy worker may print progress. Its stdout is never RPC output.
                with redirect_stdout(sys.stderr):
                    if action=='init' and adapter is None:
                        keys={'worker_directory','server_directory','ledger_directory','jobs_path',
                              'mapping_path','lock_path','output','bridge_path'}
                        if set(args)!=keys or any(not isinstance(v,str) or len(v)>4096 for v in args.values()):raise ValueError()
                        worker_dir=Path(args['worker_directory']).resolve()
                        server_dir=Path(args['server_directory']).resolve()
                        if not (worker_dir/'slg_dlsite_worker.py').is_file():raise ValueError()
                        if not (server_dir/'slg_collection_jobs.py').is_file():raise ValueError()
                        # This affects ONLY this private interpreter.
                        sys.path[:0]=[str(worker_dir),str(server_dir),str(root)]
                        import slg_dlsite_worker as worker
                        from slg_collection_jobs import CollectionJobs
                        from slg_collector_bridge import BridgeStore
                        from slg_collector_bridge_adapters import ProductionAdapters
                        jobs=CollectionJobs(args['jobs_path'])
                        adapter=ProductionAdapters(worker_module=worker,ledger_directory=args['ledger_directory'],
                             jobs=jobs,mapping_path=args['mapping_path'],lock_path=args['lock_path'],
                             manual_pause=lambda:False,output=args['output'])
                        # Do not open bridge SQLite here: first launch can occur
                        # inside parent reserve()'s write transaction. Publication
                        # alone needs this store, after the parent transaction ends.
                        store_path=args['bridge_path']
                        result={'ready':True}
                    elif adapter is None:raise ValueError()
                    elif action in ('hold','heartbeat','release','manual_job'):
                        if set(args)!={'task_id'} or not isinstance(args['task_id'],str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,100}',args['task_id']):raise ValueError()
                        result=getattr(adapter,action)(args['task_id'])
                    elif action=='check_ids':
                        if set(args)!={'ids'}:raise ValueError()
                        ids=args['ids']
                        if not isinstance(ids,list) or not 1<=len(ids)<=100:raise ValueError()
                        result=adapter.check_ids(ids)
                    elif action=='bind_manual':
                        if set(args)!={'task_id','job_id'}:raise ValueError()
                        result=adapter.bind_manual(args['task_id'],args['job_id'])
                    elif action=='sweep':
                        if args:raise ValueError()
                        result=adapter.sweep()
                    elif action=='budget_gate':
                        if set(args)!={'mode','url','task_id'}:raise ValueError()
                        from slg_collector_bridge import approved_url
                        approved_url(args['url'])
                        result=adapter.budget_gate(**args)
                    elif action=='publish_task':
                        if set(args)!={'task_id'}:raise ValueError()
                        if store is None:store=BridgeStore(store_path)
                        result=adapter.publish_task(store,args['task_id'])
                    else:raise ValueError()
                reply={'id':rid,'ok':True,'result':result}
            except Exception as exc:
                # Do not serialize tracebacks, DB contents, credentials or paths.
                code=getattr(exc,'code','collector_runtime_failed')
                if not isinstance(code,str) or not re.fullmatch(r'[a-z_]{1,100}',code):code='collector_runtime_failed'
                reply={'id':rid,'ok':False,'error':code,'status':getattr(exc,'status',503)}
            protocol.write(json.dumps(reply,ensure_ascii=True,allow_nan=False)+'\n');protocol.flush()
    finally:
        if adapter:
            for task in list(adapter._leases):adapter.release(task)


if __name__=='__main__':
    if sys.argv[1:]!=['--worker']:raise SystemExit('private collector runtime')
    _worker()
