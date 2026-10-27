"""Explicit fresh Desktop adapter over the existing Bridge/socket/spool.

No owner copy, legacy registration, new service or hook approval.
The template authorizes local child preparation; it never serves model calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from agentstack_mail.fresh_child import family, prepare
from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_launch import identity, operation, root_path
from agentstack_mail.fresh_proxy import serve as serve_proxy
from agentstack_mail.fresh_runtime import _directory, _read_private, _write_private
from agentstack_mail.storage_origin_contract import B1Error, canonical

from .daemon import (BridgeConfig, BridgeDaemon, _recv_payload, _state_for_event,
                     wake_execution_settings, wake_timing_settings)
from .hook_entry import append_spool, fresh_hook, transcript_is_codex_desktop, validate_event
from .identity_store import external_id_for


def selected():
    """Admission precedes all machine-wide env/legacy/global loaders."""
    if 'AGENTSTACK_FRESH_CLIENT_CONTEXT' not in os.environ and os.environ.get('AGENTSTACK_RUNTIME_MODE','legacy') != 'fresh':
        return False
    if (not os.environ.get('AGENTSTACK_FRESH_CLIENT_CONTEXT')
            or os.environ.get('AGENTSTACK_RUNTIME_MODE','legacy') not in ('legacy','fresh')
            or os.environ.get('AGENTSTACK_CLIENT_CONFIG') or os.environ.get('AGENTSTACK_GLOBAL_SERVER_CONFIG')):
        raise B1Error('FRESH_CONTEXT_CONFLICT')
    return True


def reference(session_id, agent_id=None):
    for index,value in enumerate((session_id, agent_id)):
        if index==1 and value is None:continue
        if not isinstance(value,str) or not value or len(value)>1024:
            raise B1Error('FRESH_APP_REFERENCE_INVALID')
    return hashlib.sha256(canonical([session_id,agent_id]).encode()).hexdigest()


class AppContext:
    def __init__(self, *, create=False):
        if not selected():raise B1Error('FRESH_CONTEXT_REQUIRED')
        self.template=FreshBoundClient(os.environ['AGENTSTACK_FRESH_CLIENT_CONTEXT'])
        self.root=root_path(os.environ.get('AGENTSTACK_HOME') or Path(__file__).resolve().parents[4])
        self.directory=self.explicit_directory('AGENTSTACK_CODEX_APP_RUNTIME_DIR',create=create)
        self.home=self.explicit_directory('CODEX_HOME')
        self.sessions=self.home/'sessions'
        if not self.sessions.is_dir():raise B1Error('FRESH_APP_HOME_REQUIRED')
        settings=dict(kind='orrery-fresh-app-v1',template_context=str(self.template.path),
            template_digest=self.template.context_digest,wrapper_root=str(self.root),codex_home=str(self.home),
            runtime_dir=str(self.directory),child_socket=os.environ.get('AGENTSTACK_CODEX_APP_CHILD_SOCKET'),**identity(self.template))
        path=self.directory/'settings.json'
        if create:
            try:_write_private(path,settings)
            except FileExistsError:pass
        if _read_private(path)!=settings:raise B1Error('FRESH_APP_SETTINGS_CHANGED')
        self.settings=settings
        self.routes=self.directory/'routes'
        if create:self.routes.mkdir(mode=0o700,exist_ok=True)
        _directory(self.routes)

    @staticmethod
    def explicit_directory(key, *, create=False):
        value=os.environ.get(key)
        if not value:raise B1Error('FRESH_APP_CONFIGURE_REQUIRED')
        path=Path(value)
        if not path.is_absolute() or path.resolve()!=path:raise B1Error('FRESH_APP_CONFIGURE_REQUIRED')
        if create:path.mkdir(mode=0o700,parents=True,exist_ok=True)
        _directory(path)
        return path

    def transcript(self,session_id):
        found=[path for path in self.sessions.rglob('rollout-*.jsonl') if session_id in path.name
               and transcript_is_codex_desktop(path,session_id,sessions_root=self.sessions)]
        if len(found)!=1:raise B1Error('FRESH_APP_DESKTOP_TRANSCRIPT_REQUIRED')
        return found[0]

    def path(self,session_id,agent_id=None):
        return self.routes/(reference(session_id,agent_id)+'.json')

    def check(self,client,session_id,agent_id):
        self.template._load()
        if _read_private(self.directory/'settings.json')!=self.settings:
            raise B1Error('FRESH_APP_SETTINGS_CHANGED')
        path=self.path(session_id,agent_id);row=_read_private(path)
        if (row.get('kind')!='orrery-fresh-app-route-v1' or row.get('session_id')!=session_id
                or row.get('agent_id')!=agent_id or row.get('client_context')!=str(client.path)
                or row.get('context_digest')!=client.context_digest
                or any(row.get(k)!=v for k,v in identity(client).items())
                or client.context['root']!=self.template.context['root']
                or client.context['mail_instance_id']!=self.template.context['mail_instance_id']
                or client.context['mail_agent_id']==self.template.context['mail_agent_id']
                or not transcript_is_codex_desktop(row.get('transcript_path',''),session_id,sessions_root=self.sessions)):
            raise B1Error('FRESH_APP_ROUTE_MISMATCH')
        if path.with_suffix('.stopped.json').exists():raise B1Error('FRESH_APP_SUBAGENT_STOPPED')
        parent,base,_=family(client)
        expected=self.template if agent_id is None else self.resolve(session_id)
        if base is None or parent.path!=expected.path or row.get('parent_context')!=str(parent.path):
            raise B1Error('FRESH_APP_ROUTE_MISMATCH')
        return row

    def resolve(self,session_id,agent_id=None):
        row=_read_private(self.path(session_id,agent_id))
        return AppClient(row['client_context'],self,session_id,agent_id)


class AppClient(FreshBoundClient):
    def __init__(self,path,app,session_id,agent_id):
        self.app,self.session_id,self.agent_id=app,session_id,agent_id
        super().__init__(path)

    def _load(self):
        result=super()._load()
        self.app.check(self,self.session_id,self.agent_id)
        return result


class LifecycleQueue(queue.Queue):
    """IPC and recovered spool events share the same admission ordering."""
    def __init__(self,delivery):
        super().__init__();self.delivery=delivery

    def put(self,item,*args,**kwargs):
        if isinstance(item,dict) and 'hook_event_name' in item:
            with self.delivery.admission:
                key=str(self.delivery.app.path(item['session_id'],item['agent_id']))
                version=self.delivery.accepted.get(key,0)+1
                self.delivery.accepted[key]=version
                return super().put((item,version),*args,**kwargs)
        return super().put(item,*args,**kwargs)


class FreshBridge(BridgeDaemon):
    def __init__(self,app):
        self.app=app
        runtime=app.directory
        self.config=BridgeConfig(runtime_dir=runtime,socket_path=runtime/'bridge.sock',
            spool_path=runtime/'hook-events.jsonl',retry_path=runtime/'retry.jsonl',snapshot_path=runtime/'snapshot.json',
            project_key='',agent_mail_endpoint=app.template.context['endpoint'],
            codex_sessions_root=app.sessions,**wake_execution_settings(os.environ),**wake_timing_settings(os.environ))
        self._events=queue.Queue();self._stop=threading.Event();self._worker=None
        from .fresh_app_delivery import AppDelivery
        self.delivery=AppDelivery(app,self.config)
        self._events=LifecycleQueue(self.delivery)

    def _handle_connection(self,connection):
        try:
            payload=_recv_payload(connection)
            if 'fresh_receipt' in payload:
                if set(payload)!={'fresh_receipt','session_id','agent_id'}:raise ValueError
                reference(payload['session_id'],payload['agent_id'])
                self._events.put(payload);response={'ok':True}
            else:
                event=validate_event(payload)
                # Hook events and receipts share the serial worker's ordering.
                self._events.put(event)
                notice=self.delivery.take(event)
                response=dict(ok=True,external_id=external_id_for(event['session_id'],event['agent_id']))
                if notice is not None:response['fresh_notification']=notice
        except (B1Error,OSError,ValueError,KeyError,TypeError):response={'ok':False}
        try:connection.sendall(canonical(response).encode()+b'\n')
        except OSError:pass

    def process_event(self,event,*,sequence=None,**unused):
        event=validate_event(event);sid,sub=event['session_id'],event['agent_id']
        transcript=self.app.transcript(sid);path=self.app.path(sid,sub)
        if path.exists():
            if event['hook_event_name']=='SubagentStop' and path.with_suffix('.stopped.json').exists():
                return external_id_for(sid,sub)
            client=self.app.resolve(sid,sub)
        else:
            if event['hook_event_name'] not in {'SessionStart','SubagentStart'}:
                raise B1Error('FRESH_APP_START_REQUIRED')
            parent=self.app.template if sub is None else self.app.resolve(sid)
            slot='app-'+reference(sid,sub)[:32]
            result=asyncio.run(prepare(parent,slot,'App'+reference(sid,sub)[:24],'codex',
                event.get('model') or 'unknown','Codex Desktop lifecycle'))
            client=FreshBoundClient(result['context_path'])
            row=dict(kind='orrery-fresh-app-route-v1',session_id=sid,agent_id=sub,
                client_context=str(client.path),context_digest=client.context_digest,
                transcript_path=str(transcript),parent_context=str(parent.path),**identity(client))
            try:_write_private(path,row)
            except FileExistsError:
                if _read_private(path)!=row:raise B1Error('FRESH_APP_ROUTE_MISMATCH') from None
            client=self.app.resolve(sid,sub)
        with self.delivery.admission, operation(self.app.directory):
            # Older work must not publish a waiting projection over a newer
            # accepted start, even while that start is still in this queue.
            if sequence is not None and self.delivery.accepted.get(str(path),0)!=sequence:
                return None
            # SessionStart begins a live turn; only an observed Stop permits wake.
            state=dict(session_id=sid,agent_id=sub,state=('working' if event['hook_event_name'] in
                {'SessionStart','SubagentStart'} else _state_for_event(event)),event=event['hook_event_name'],cwd=event['cwd'])
            temporary=path.with_suffix('.state-next.json')
            # Worker is serial; replace only the non-authoritative lifecycle projection.
            try:temporary.unlink()
            except FileNotFoundError:pass
            _write_private(temporary,state);os.replace(temporary,path.with_suffix('.state.json'))
            if event['hook_event_name']=='SubagentStop':
                try:_write_private(path.with_suffix('.stopped.json'),dict(stopped=True))
                except FileExistsError:pass
        return external_id_for(sid,sub)

    def _process_event_batch(self, event):
        """Process FIFO entries individually; leave the unclaimed tail queued."""
        processed = 0
        while event is not None:
            sequence = None
            if isinstance(event, tuple):event, sequence = event
            try:
                if 'fresh_receipt' in event:self.delivery.receipt(event)
                elif self.process_event(event,sequence=sequence) is not None:
                    self.delivery.processed(event,sequence)
            except (B1Error,OSError,ValueError,KeyError,TypeError):
                if 'fresh_receipt' not in event:append_spool(event,self.config.spool_path)
            processed += 1
            # An acquired entry is processed even if stop arrived after get().
            # Never prefetch a batch: receipts in the remaining tail stay FIFO.
            if processed >= 64 or self._stop.is_set():break
            try:event = self._events.get_nowait()
            except queue.Empty:break
        return processed

    def _worker_loop(self):
        # Reuse the existing sanitized event spool/queue. No delivery DB or watcher.
        for path in sorted(self.config.spool_path.parent.glob('.hook-events.jsonl.drain-*')):
            self._queue_event_file(path)
        try:
            while not self._stop.is_set():
                try:event=self._events.get(timeout=self.config.wake_poll_seconds)
                except queue.Empty:
                    self.queue_spool(self.config.spool_path);event=None
                self._process_event_batch(event)
                if self._stop.is_set():break
                try:self.delivery.tick()
                except (B1Error,OSError,ValueError,KeyError,TypeError):pass
        finally:self.delivery.close()


def child_context(root,session_id,agent_id=None,*,need_transport=False):
    """Host CLI selector over the same observed App route as bootstrap."""
    app=AppContext()
    if app.root!=root:raise B1Error('FRESH_APP_SETTINGS_CHANGED')
    client=app.resolve(session_id,agent_id)
    transport=None
    if need_transport:
        import runpy
        value=app.settings['child_socket']
        if not value:raise B1Error('FRESH_CHILD_TMUX_PREPARE_REQUIRED')
        tmux=runpy.run_path(str(root/'bin/lib/runtime_tmux.py'))
        try:
            transport=tmux['Transport'](Path(value))
            result=transport.command('display-message','-p','#{socket_path}|#{pid}')
            fields=result.stdout.strip().rsplit('|',1)
            if (result.returncode or len(fields)!=2 or fields[0]!=str(transport.path)
                    or not fields[1].isdigit()):raise B1Error('FRESH_CHILD_TMUX_PREPARE_REQUIRED')
            transport.server_pid=int(fields[1])
        except tmux['TargetError']:raise B1Error('FRESH_CHILD_TMUX_PREPARE_REQUIRED') from None
    return client,transport


def proxy():
    app=AppContext()
    asyncio.run(serve_proxy(app.template,resolve_external=app.resolve))


def guard(payload):
    app=AppContext()
    client=app.resolve(payload.get('session_id'),payload.get('agent_id'))
    # Pass the entire original patch to the existing Move/coverage adapter.
    env=dict(os.environ,AGENTSTACK_FRESH_CLIENT_CONTEXT=str(client.path))
    command=[sys.executable,str(app.root/'hooks/check-codex-file-reservation.py')]
    return subprocess.run(command,input=canonical(payload),text=True,env=env).returncode


def main():
    try:
        action=sys.argv[1]
        if action=='guard':return guard(json.load(sys.stdin))
        if action=='proxy':proxy();return 0
        if action=='hook':return fresh_hook(json.load(sys.stdin))
        raise B1Error('FRESH_APP_OPERATION_INVALID')
    except (B1Error,OSError,ValueError,KeyError,TypeError,IndexError):
        print('FRESH_APP_UNAVAILABLE',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
