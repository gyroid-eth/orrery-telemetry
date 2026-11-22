"""Global App lifecycle routes over the common client and delivery intent.

The external index stores only client_name. Owner, window, launch and session
state remain in their existing client slots; no delivery database is opened.
"""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import threading
import time

from .daemon import BridgeConfig, BridgeDaemon, _recv_payload, _state_for_event, wake_execution_settings, wake_timing_settings
from .hook_entry import validate_event, transcript_is_codex_desktop
from .identity_store import external_id_for
from .wake import codex_provider_home, _resume_environment


class GlobalBridge(BridgeDaemon):
    def __init__(self, api, template):
        self.api, self.template = api, template
        if not template.identity:
            raise api['ClientError']('BRIDGE_TEMPLATE_OWNER_REQUIRED')
        self.delivery = api['runtime_delivery'](template)
        self.delivery.consumer_agent()
        runtime = self.delivery.directory(template.runtime_dir/'codex-app')
        self.config = BridgeConfig(runtime_dir=runtime,socket_path=runtime/'bridge.sock',
            spool_path=runtime/'hook-events.jsonl',retry_path=runtime/'registration-retry.jsonl',
            snapshot_path=runtime/'snapshot.json',project_key='',agent_mail_endpoint=template.endpoint,
            codex_sessions_root=codex_provider_home()/'sessions', **wake_execution_settings(os.environ), **wake_timing_settings(os.environ))
        self._events=queue.Queue(); self._stop=threading.Event();self._worker=None
        self._inflight={};self._history={};self._seen={}
        self._last_warning=None
        self.policy=api['delivery_contract']()['policy']('codex-app')

    def transcript(self, session_id):
        root=self.config.codex_sessions_root
        if not root.exists():raise self.api['ClientError']('PROVIDER_SESSION_REQUIRED')
        for path in root.rglob('rollout-*.jsonl'):
            if session_id in path.name and transcript_is_codex_desktop(path,session_id,sessions_root=root):
                return path
        raise self.api['ClientError']('PROVIDER_SESSION_REQUIRED')

    def process_event(self,event,**unused):
        event=validate_event(event)
        sid,sub=event['session_id'],event['agent_id']
        transcript=self.transcript(sid)
        route=self.delivery.external_route(sid,sub,create=True)
        parent=self.template
        if sub is not None:
            parent=self.delivery.external_client(sid)
        name='app-'+hashlib.sha256(external_id_for(sid,sub).encode()).hexdigest()[:32]
        if route.exists() or route.is_symlink():
            saved=self.delivery.local(route,'runtime_route')
            if saved['client_name']!=name:raise self.api['ClientError']('ROUTE_OWNER_CONFLICT')
            client=self.api['RuntimeClient'](parent.clients_parent/name/'runtime-client.json')
        else:
            result=parent.prepare_child({'child_client_name':name,'name':'App'+name[4:20],
                'program':'codex-app','model':event.get('model') or 'unknown',
                'workdir':event['cwd'],'task_description':'Codex App lifecycle','prepare_only':True})
            client=self.api['RuntimeClient'](Path(result['client_config']))
            self.delivery.save(route,{'kind':'orrery-global-runtime-route-v1','client_name':name},'runtime_route')
        delivery=self.api['runtime_delivery'](client)
        if not client.outputs['launch'].exists():
            row=delivery.prepare_launch('codex',cwd=event['cwd'])
            delivery.bind_window()
            with client.operation_mutex():
                row['phase']='exec-unknown'
                delivery.save(client.outputs['launch'],row,'launch_intent')
        row=delivery.launch()
        payload={'session_id':sid,'transcript_path':str(transcript),'cwd':event['cwd']}
        # Admission is the existing immutable Desktop transcript check above.
        # This trusted in-process callback is not exposed as an MCP argument.
        delivery.session_start(payload,trusted_receipt=(row['launch_id'],row['receipt_nonce']))
        with client.operation_mutex():
            path=client.runtime_dir/'live-sessions'/(sid+'.json')
            live=self.api['read_json'](path)
            self.api['atomic_json'](path,{**live,'app_state':_state_for_event(event),'app_subagent':sub is not None})
        if event['hook_event_name'] in ('Stop','SubagentStop'):
            # An App Stop means a completed turn, not the end of its session.
            pass
        return external_id_for(sid,sub)

    def _handle_connection(self,connection):
        try:
            event=validate_event(_recv_payload(connection))
            self.process_event(event)
            client=self.delivery.external_client(event['session_id'],event['agent_id'])
            delivery=self.api['runtime_delivery'](client)
            notices=delivery.hook_notifications('codex-app') if event['hook_event_name']=='PostToolUse' else []
            response={'ok':True,'client_name':client.config['client_name'],'notifications':notices}
        except (OSError,ValueError,RuntimeError):
            response={'ok':False,'reason':'GLOBAL_EVENT_RETRY_REQUIRED'}
        connection.sendall(json.dumps(response).encode()+b'\n')

    def _worker_loop(self):
        self.recover_stale_drains()
        while not self._stop.wait(self.policy['poll']):
            while not self._events.empty():
                event=self._events.get()
                if event is None:return
                try:self.process_event(event)
                except (OSError,ValueError,RuntimeError):
                    # Keep the one sanitized event spool, no owner/token copy.
                    from .hook_entry import append_spool
                    append_spool(event,self.config.spool_path)
            self.queue_spool(self.config.spool_path)
            try:
                self.wake_tick()
                self._last_warning=None
            except (OSError,ValueError,RuntimeError) as exc:
                reason=str(exc) if isinstance(exc,self.api['ClientError']) else 'LOCAL_STATE_UNAVAILABLE'
                if reason!=self._last_warning:
                    print('global-app-consumer: '+reason,file=__import__('sys').stderr)
                    self._last_warning=reason

    def stop(self):
        super().stop()
        # These are only the process groups created by this Bridge instance.
        # Teardown never terminates an interactive/Desktop session.
        for process,*_ in list(self._inflight.values()):
            if process.poll() is None:
                try:os.killpg(process.pid,__import__('signal').SIGTERM)
                except ProcessLookupError:pass
                try:process.wait(timeout=5)
                except __import__('subprocess').TimeoutExpired:
                    try:os.killpg(process.pid,__import__('signal').SIGKILL)
                    except ProcessLookupError:pass
                    process.wait()
        self._inflight.clear()

    def resume_command(self, delivery, launch, live):
        # App history stays in the original Desktop HOME, as in the old wake
        # adapter. No config, transcript or hook approval is written here.
        client = delivery.client
        sid = live['session_id']
        transcript = self.transcript(sid)
        if delivery.provider_header(transcript) != sid:
            raise self.api['ClientError']('PROVIDER_SESSION_REQUIRED')
        binary = os.environ.get('AGENTSTACK_CODEX_BIN') or self.config.codex_binary
        proxy = [__import__('sys').executable, str(Path(self.api['__file__'])),
                 '--context', str(client.path), 'proxy']
        return [binary, '-C', launch['cwd'], '-c',
                'plugins.'+json.dumps(self.config.plugin_id)+'.mcp_servers.agentstack.enabled=false',
                '-c', 'mcp_servers.orrery-mail.command='+json.dumps(proxy[0]),
                '-c', 'mcp_servers.orrery-mail.args='+json.dumps(proxy[1:]),
                'exec', 'resume', *(['--skip-git-repo-check'] if self.config.skip_git_repo_check else []), sid]

    def wake_tick(self):
        # Channel bounds are selected by the same consumer used by hooks.
        now=time.monotonic()
        for key,(process,delivery,mid,attempt,started) in list(self._inflight.items()):
            code=process.poll()
            if code is None and now-started<self.policy['process_timeout']:continue
            if code is None:
                process.terminate()
                try:process.wait(timeout=5)
                except Exception:process.kill();process.wait()
            try:
                delivery.callback(mid,attempt,confirmed=code==0)
            except (OSError,ValueError,RuntimeError) as exc:
                delivery.report_error(mid,exc)
            finally:
                self._inflight.pop(key,None)
        if not self.config.cold_wake_enabled or os.environ.get('AGENTSTACK_MAIL_DISABLED')=='1':return
        root=self.delivery.consumer_agent().parent.parent/'external'
        if not root.exists():
            self._seen.clear()
            return
        active_seen=set()
        for folder in root.iterdir():
            if len(folder.name)!=64 or any(c not in '0123456789abcdef' for c in folder.name):continue
            try:self.wake_root(folder,now,active_seen)
            except (OSError,ValueError,RuntimeError) as exc:self.delivery.report_error(folder.name,exc)
        self._seen={key:value for key,value in self._seen.items() if key in active_seen}

    def wake_root(self, folder, now, active_seen):
        route=self.delivery.local(folder/'route.json','runtime_route')
        client=self.api['RuntimeClient'](self.template.clients_parent/route['client_name']/'runtime-client.json')
        delivery=self.api['runtime_delivery'](client)
        delivery.reconcile_confirmed()
        launch=delivery.launch()
        lives=delivery.live_rows(launch['launch_id'])
        if not lives:return
        live=lives[-1][1]
        state=live.get('app_state')
        if live.get('app_subagent') or state not in ('waiting','dormant'):return
        history=self._history.setdefault(folder.name,deque())
        while history and now-history[0]>=3600:history.popleft()
        rate_limited=len(history)>=self.policy['wakes_per_hour']
        hints=delivery.hints()
        active_seen.update((folder.name,mid) for mid in hints)
        for mid in hints:
            try:
                key=folder.name
                if key in self._inflight:continue
                seen=self._seen.setdefault((key,mid),now)
                if now-seen<self.policy['coalesce']:continue
                # Provider configuration is admission, before handoff accounting.
                command=self.resume_command(delivery,launch,live)
                prepared=delivery.prepare(mid,'codex-app',exclude_self=True,
                    admission_reason='WAKE_RATE_LIMITED' if rate_limited else None)
                if prepared['state']!='handoff':continue
                # Real executable discovery uses the ordinary provider command;
                # the global proxy owner is injected without token arguments.
                import subprocess
                command.append(prepared['notification'])
                env=dict(_resume_environment(command[0]),AGENTSTACK_CLIENT_CONFIG=str(client.path),
                    AGENTSTACK_LAUNCH_ID=launch['launch_id'],AGENTSTACK_LAUNCH_NONCE=launch['receipt_nonce'],
                    CODEX_HOME=str(codex_provider_home()),AGENTSTACK_HOME=client.config['wrapper_root'])
                try:
                    process=subprocess.Popen(command,cwd=launch['cwd'],env=env,stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
                except OSError:
                    delivery.callback(mid,prepared['attempt_id'],known_failure=True)
                    continue
                history.append(now)
                self._inflight[key]=(process,delivery,mid,prepared['attempt_id'],now)
                break  # one resume per App root; other hints remain pending
            except (OSError,ValueError,RuntimeError) as exc:
                delivery.report_error(mid,exc)
