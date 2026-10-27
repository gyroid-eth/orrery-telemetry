"""Opt-in test-only observation; invoke the unchanged fresh App daemon."""
from contextlib import contextmanager
import functools
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from agentstack_mail.storage_origin_contract import B1Error
from agentstack_codex_app import daemon, fresh_app_delivery, fresh_bridge

output = Path(os.environ['ORRERY_FRESH_APP_DIAGNOSTIC_FILE'])
guard = threading.Lock()
counts = {'lines': 0, 'bytes': 0, 'failed': False}
routes = {}


def incomplete():
    if not counts['failed']:
        counts['failed']=True
        try:sys.stderr.write('FRESH_APP_DIAGNOSTIC_INCOMPLETE\n');sys.stderr.flush()
        except Exception:pass


def route(key):
    with guard:
        key=str(key)
        if key not in routes:routes[key]=len(routes)+1
        return routes[key]


def client_route(delivery, client):
    try:return route(delivery.app.path(client.session_id,client.agent_id))
    except Exception:
        incomplete()
        return None


def record(stage, **values):
    try:
        row = json.dumps(dict(stage=stage, monotonic=time.monotonic(), **values), separators=(',', ':'))+'\n'
        data = row.encode()
        with guard:
            if counts['lines'] >= 2000 or counts['bytes'] + len(data) > 262144:
                incomplete()
                return
            with output.open('ab') as stream:
                stream.write(data)
            counts['lines'] += 1
            counts['bytes'] += len(data)
    except Exception:
        incomplete()


def error_code(error):
    known={'FRESH_APP_TURN_CHANGED','FRESH_APP_RECEIPT_INVALID','FRESH_PROXY_BINDING_MISMATCH',
           'FRESH_APP_ROUTE_MISMATCH','FRESH_PRIVATE_FILE_UNSAFE'}
    return error.code if isinstance(error, B1Error) and error.code in known else 'OTHER_ERROR'


def wrap(cls, name, before=None, after=None):
    original = getattr(cls, name)
    @functools.wraps(original)
    def observed(self, *args, **kwargs):
        if before:observe(before,self,*args,**kwargs)
        try:
            result = original(self, *args, **kwargs)
        except Exception as error:
            fields={'code':error_code(error)}
            if name=='version':
                try:
                    client=args[0];fields['route']=route(self.app.path(client.session_id,client.agent_id))
                except Exception:incomplete()
            record(name+'_error', **fields)
            raise
        if after:observe(after,self,result,*args,**kwargs)
        return result
    setattr(cls, name, observed)


def observe(fn, *args, **kwargs):
    try:fn(*args,**kwargs)
    except Exception:incomplete()


def queue_state(queue, _result, *args, **kwargs):
    delivery=queue.delivery
    event=args[0] if args else None
    if not isinstance(event,dict) or 'hook_event_name' not in event:return
    key=str(delivery.app.path(event['session_id'],event['agent_id']))
    with delivery.admission:
        accepted=delivery.accepted.get(key,0);applied=delivery.applied.get(key,0)
    record('queue_put', route=route(key), depth=queue.qsize(), accepted=accepted, applied=applied)


def tick_state(delivery, _result):
    with delivery.mutex:
        ready={key:bool(row.get('claimed')) for key,row in delivery.ready.items()}
    with delivery.admission:
        accepted=dict(delivery.accepted);applied=dict(delivery.applied)
    for key in accepted.keys() | applied.keys() | ready.keys():
        record('tick', route=route(key), ready=key in ready, claimed=ready.get(key,False),
               accepted=accepted.get(key,0), applied=applied.get(key,0))


wrap(fresh_bridge.LifecycleQueue, 'put', after=queue_state)
wrap(fresh_bridge.FreshBridge, 'process_event', after=lambda self,result,event,*a,**k:
    record('process_event', route=route(self.app.path(event['session_id'],event['agent_id'])),
           applied=bool(result), depth=self._events.qsize()))
wrap(fresh_app_delivery.AppDelivery, 'version', before=lambda self,client:
    record('version_start',route=route(self.app.path(client.session_id,client.agent_id))),
    after=lambda self,result,client:record('version_ok',route=route(self.app.path(client.session_id,client.agent_id))))
wrap(fresh_app_delivery.AppDelivery, 'tick', after=tick_state)
def take_before(self, event):
    key=str(self.app.path(event['session_id'],event['agent_id']))
    with self.mutex:
        row=self.ready.get(key)
        ready=row is not None;claimed=bool(row and row.get('claimed'))
    record('take_state', route=route(key), ready=ready, claimed=claimed)
wrap(fresh_app_delivery.AppDelivery, 'take', before=take_before, after=lambda self,result,*a,**k:
    record('take', route=route(self.app.path(a[0]['session_id'],a[0]['agent_id'])),
           notice=result is not None, count=len(result['notifications']) if result else 0))
wrap(fresh_app_delivery.AppDelivery, 'receipt', before=lambda self,payload:record('receipt_worker',
    route=route(self.app.path(payload['session_id'],payload['agent_id']))),
    after=lambda self,result,payload:record('receipt_complete',
    route=route(self.app.path(payload['session_id'],payload['agent_id']))))

original_handle=fresh_bridge.FreshBridge._handle_connection
def handle(self, connection):
    class ObservedConnection:
        def __init__(self):self.kind='unknown';self.buffer=bytearray();self.route=None
        def recv(self, size):
            data=connection.recv(size)
            self.buffer.extend(data)
            if b'\n' in self.buffer and self.kind=='unknown':
                try:
                    payload=json.loads(bytes(self.buffer).split(b'\n',1)[0])
                    self.kind='receipt' if 'fresh_receipt' in payload else 'event'
                    self.route=route(self_app.path(payload['session_id'],payload.get('agent_id')))
                    record('ipc_received', kind=self.kind, route=self.route)
                except (ValueError,TypeError):
                    pass
                except Exception:incomplete()
            return data
        def sendall(self, data):
            try:connection.sendall(data)
            except OSError:
                record('ipc_response', kind=self.kind, route=self.route, sent=False)
                raise
            record('ipc_response', kind=self.kind, route=self.route, sent=True)
    self_app=self.app
    return original_handle(self, ObservedConnection())
fresh_bridge.FreshBridge._handle_connection=handle

original_pending=fresh_app_delivery.AppDelivery.pending
@functools.wraps(original_pending)
async def pending(self, client):
    key=client_route(self,client)
    try:
        result=await original_pending(self, client)
    except Exception as error:
        record('pending_error', route=key, code=error_code(error))
        raise
    record('pending', route=key, count=len(result))
    return result
fresh_app_delivery.AppDelivery.pending=pending

original_complete=fresh_app_delivery.AppDelivery.complete
@functools.wraps(original_complete)
async def complete(self, row):
    try:
        client=row['client'];key=client_route(self,client);count=len(row['messages'])
    except Exception:
        incomplete();key=None;count=None
    record('complete_start', route=key, count=count)
    try:
        result=await original_complete(self, row)
    except Exception as error:
        record('complete_error', route=key, code=error_code(error))
        raise
    record('complete_done', route=key, count=count)
    return result
fresh_app_delivery.AppDelivery.complete=complete

original_lock=fresh_app_delivery.consumer_lock
@contextmanager
def consumer_lock(client):
    with original_lock(client) as acquired:
        try:key=route(client.app.path(client.session_id,client.agent_id))
        except Exception:key=None;incomplete()
        record('lock', route=key, acquired=bool(acquired))
        yield acquired
fresh_app_delivery.consumer_lock=consumer_lock

record('bootstrap_start')
def stop(_signum, _frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
try:
    daemon.serve()
finally:
    record('bootstrap_end')
