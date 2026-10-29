"""Synthetic Desktop transcripts, real bridge/socket/proxy/guard and fresh ORM."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import pytest

from agentstack_mail.fresh_runtime import _read_private
from agentstack_mail.storage_origin_contract import B1Error, canonical
from test_fresh_guard import live
from test_fresh_proxy import Peer, environment, refused

ROOT=Path(__file__).resolve().parents[3]
APP=ROOT/'integrations/codex_app'
sys.path.insert(0,str(APP/'src'))
from agentstack_codex_app.fresh_bridge import AppContext, reference


def event(home,sid,*,sub=None,name='SessionStart',originator='Codex Desktop'):
    transcript=home/'sessions'/('rollout-'+sid+'.jsonl')
    if not transcript.exists():
        transcript.write_text(canonical(dict(type='session_meta',payload=dict(id=sid,originator=originator)))+'\n')
    return dict(session_id=sid,agent_id=sub,cwd=str(home),model='fixture-model',hook_event_name=name,
                turn_id=None,transcript_path=str(transcript))


@asynccontextmanager
async def desktop(tmp_path,monkeypatch,*,overrides=None):
    async with live(tmp_path) as (runtime,people,template,_):
        private=Path(tempfile.mkdtemp(prefix='o213-app-',dir='/private/tmp'))
        home=private/'home';home.mkdir(mode=0o700);(home/'sessions').mkdir()
        state=private/'runtime'
        env=environment(template,AGENTSTACK_RUNTIME_MODE='fresh',AGENTSTACK_HOME=str(ROOT),CODEX_HOME=str(home),
            AGENTSTACK_CODEX_APP_RUNTIME_DIR=str(state),AGENTSTACK_CODEX_APP_INSTALL_DIR=str(APP))
        env.update(overrides or {})
        # App routes/settings use this explicit fixture environment only.
        for key in ('AGENTSTACK_RUNTIME_MODE','AGENTSTACK_HOME','CODEX_HOME','AGENTSTACK_CODEX_APP_RUNTIME_DIR',
                    'AGENTSTACK_CODEX_APP_INSTALL_DIR','AGENTSTACK_FRESH_CLIENT_CONTEXT'):
            monkeypatch.setenv(key,env[key])
        monkeypatch.delenv('AGENTSTACK_CLIENT_CONFIG',raising=False);monkeypatch.delenv('AGENTSTACK_GLOBAL_SERVER_CONFIG',raising=False)
        for key,value in (overrides or {}).items():monkeypatch.setenv(key,value)
        log=(tmp_path/'app-bridge.log').open('w')
        process=None
        try:
            process=await asyncio.create_subprocess_exec('/bin/bash',str(ROOT/'scripts/run-codex-app-bridge.sh'),
                env=env,stdout=log,stderr=log)
            deadline=time.monotonic()+15
            while not (state/'bridge.sock').exists():
                assert process.returncode is None,(tmp_path/'app-bridge.log').read_text()
                assert time.monotonic()<deadline,'bridge did not listen'
                await asyncio.sleep(.05)
            app=AppContext()
            yield runtime,people,template,app,home,env
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:await asyncio.wait_for(process.wait(),10)
                except asyncio.TimeoutError:process.kill();await process.wait()
            log.close()
            (tmp_path/'app-cleanup.json').write_text(canonical(dict(own_bridge_stopped=process is None or process.returncode is not None,
                private_runtime=str(state),real_app=False)))
            shutil.rmtree(private)


async def invoke(script,payload,env,*,limit=20):
    started=time.monotonic()
    process=await asyncio.create_subprocess_exec('/bin/bash',str(script),env=env,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await asyncio.wait_for(process.communicate(canonical(payload).encode()),limit)
    return dict(returncode=process.returncode,stdout=out.decode(),stderr=err.decode(),elapsed=time.monotonic()-started)


async def wait_route(app,sid,sub=None):
    deadline=time.monotonic()+20
    while True:
        try:return app.resolve(sid,sub)
        except B1Error:
            assert time.monotonic()<deadline,'Desktop route not prepared'
            await asyncio.sleep(.05)


@asynccontextmanager
async def proxy(env):
    process=await asyncio.create_subprocess_exec('/bin/bash',str(APP/'plugin/scripts/run-mcp.sh'),env=env,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    peer=Peer(process)
    try:
        await peer.rpc('initialize',{});yield peer
    finally:
        if process.returncode is None:
            process.stdin.close()
            try:await asyncio.wait_for(process.wait(),10)
            except asyncio.TimeoutError:process.terminate();await process.wait()


def test_desktop_root_subagent_real_entries_bootstrap_fixed_owner_mail_and_guard(tmp_path,monkeypatch):
    async def run():
        async with desktop(tmp_path,monkeypatch) as (runtime,people,template,app,home,env):
            sid=str(uuid4());payload=event(home,sid)
            hook=APP/'plugin/scripts/run-hook.sh'
            first=await invoke(hook,payload,env)
            assert first['returncode']==0,first['stderr']
            assert 'queued' in first['stdout'] and 'bootstrap' in first['stdout']
            root=await wait_route(app,sid)
            repeated=await invoke(hook,payload,env);assert repeated['returncode']==0
            assert (await wait_route(app,sid)).path==root.path
            sub_event=event(home,sid,sub='subagent-1',name='SubagentStart')
            sub_hook=await invoke(hook,sub_event,env);assert sub_hook['returncode']==0,sub_hook['stderr']
            # This path imports no ORM/Mail client or credential reader.
            assert sub_hook['elapsed']<1.0
            child=await wait_route(app,sid,'subagent-1')
            assert len({template.context['mail_agent_id'],root.context['mail_agent_id'],child.context['mail_agent_id']})==3
            assert _read_private(app.path(sid,'subagent-1'))['parent_context']==str(root.path)
            contexts_before={p:p.read_bytes() for p in (template.path,root.path,child.path)}
            async with proxy(env) as root_proxy,proxy(env) as sub_proxy:
                assert refused(await root_proxy.call('runtime_status'))=='BOOTSTRAP_REQUIRED'
                tools=(await root_proxy.rpc('tools/list'))['result']['tools']
                assert 'bootstrap' in {row['name'] for row in tools}
                for row in tools:
                    assert not {'auth','project_key','actor_agent_id','expected_server_instance_id'} & set(row['inputSchema']['properties'])
                root_status=await root_proxy.value('bootstrap',dict(session_id=sid))
                sub_status=await sub_proxy.value('bootstrap',dict(session_id=sid,agent_id='subagent-1'))
                assert root_status['mail_agent_id']==root.context['mail_agent_id']
                assert sub_status['mail_agent_id']==child.context['mail_agent_id']
                assert refused(await root_proxy.call('bootstrap',dict(session_id=sid,agent_id='subagent-1')))=='FRESH_APP_PROCESS_BINDING_MISMATCH'
                assert refused(await root_proxy.call('runtime_status',{'auth':{}}))=='INVALID_ARGUMENTS'
                sent=await sub_proxy.value('send_message',dict(recipients=[{'target_agent_id':root.context['mail_agent_id']}],
                    subject='subagent-result',body_md='private fixture result'))
                inbox=await root_proxy.value('fetch_inbox')
                assert len(inbox)==1 and inbox[0]['sender_id']==child.context['mail_agent_id']
                await root_proxy.value('mark_message_read',dict(message_id=sent['message_id']))
                await root_proxy.value('acknowledge_message',dict(message_id=sent['message_id']))
                target=home/'reserved.txt'
                await root_proxy.value('reserve_files',dict(paths=[str(target)],ttl_seconds=900))
                guard=APP/'plugin/scripts/run-reservation-guard.sh'
                patch='*** Begin Patch\n*** Add File: '+str(target)+'\n+fixture\n*** End Patch'
                guarded=dict(payload,tool_name='apply_patch',tool_input={'command':patch})
                allowed=await invoke(guard,guarded,env);assert allowed['returncode']==0,allowed['stderr']
                denied=await invoke(guard,{**guarded,'agent_id':'subagent-1'},env)
                assert denied['returncode']!=0 and 'RESERVATION_CONFLICT' in denied['stderr']
                move='*** Begin Patch\n*** Update File: '+str(target)+'\n*** Move to: '+str(home/'unreserved.txt')+'\n@@\n-fixture\n+next\n*** End Patch'
                denied=await invoke(guard,{**guarded,'tool_input':{'command':move}},env)
                assert denied['returncode']!=0 and 'RESERVATION_REQUIRED' in denied['stderr']
                await root_proxy.value('release_reservations',dict(paths=[str(target)]))
                assert (await invoke(guard,guarded,env))['returncode']!=0
                # SubagentStop is irreversible for this observed native subagent.
                stopped=await invoke(hook,{**sub_event,'hook_event_name':'SubagentStop'},env)
                assert stopped['returncode']==0
                deadline=time.monotonic()+10
                while not app.path(sid,'subagent-1').with_suffix('.stopped.json').exists():
                    assert time.monotonic()<deadline;await asyncio.sleep(.05)
                assert refused(await sub_proxy.call('runtime_status'))=='FRESH_APP_SUBAGENT_STOPPED'
                assert (await root_proxy.value('runtime_status'))['mail_agent_id']==root.context['mail_agent_id']
            assert all(p.read_bytes()==raw for p,raw in contexts_before.items())
            (tmp_path/'app-proof.json').write_text(canonical(dict(real_bridge=True,real_socket=True,
                synthetic_desktop=True,real_app=False,root_agent=root.context['mail_agent_id'],
                subagent=child.context['mail_agent_id'],template_not_used=True,fixed_process=True,real_guard=True,
                notification_connected=False,cold_wake=False,subagent_hook_seconds=sub_hook['elapsed'])))
    asyncio.run(run())


def test_app_root_and_sub_use_common_child_entries_and_synthetic_handoff(tmp_path,monkeypatch):
    import shlex
    from agentstack_mail.fresh_child import family,handoff
    from agentstack_mail.fresh_client import FreshBoundClient
    from test_fresh_launch import NATIVE,prepare
    async def run():
        async with desktop(tmp_path,monkeypatch,overrides={'AGENTSTACK_CODEX_APP_COLD_WAKE':'0'}) as (runtime,people,template,app,home,env):
            sid=str(uuid4());payload=event(home,sid);hook=APP/'plugin/scripts/run-hook.sh'
            assert (await invoke(hook,payload,env))['returncode']==0
            root=await wait_route(app,sid)
            sub_event=event(home,sid,sub='delegate-sub',name='SubagentStart')
            assert (await invoke(hook,sub_event,env))['returncode']==0
            sub=await wait_route(app,sid,'delegate-sub')
            provider_home,binary=prepare(template,'claude',tmp_path)
            saved={p:p.read_bytes() for p in (template.path,root.path,sub.path,provider_home/'CLAUDE.md')}
            env=dict(env,AGENTSTACK_CLAUDE_BIN=str(binary))
            monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(binary))
            task=tmp_path/'app-child-task.md';task.write_text('Literal `no shell` $(no shell).')
            async def cli(script,*args,override=None):
                return await asyncio.to_thread(subprocess.run,['/bin/bash',str(ROOT/script),*map(str,args)],
                    env=dict(env,**(override or {})),cwd=ROOT,stdin=subprocess.DEVNULL,text=True,capture_output=True,timeout=45)
            common=['--child-client-name','external','--name','ExternalRoot','--program','claude-code',
                '--model','fixture-model','--task-description','App automatic child','--prepare-only']
            missing=await cli('bin/agentstack-preregister-child',*common)
            assert missing.returncode==2 and 'FRESH_APP_REFERENCE_REQUIRED' in missing.stderr
            false_cli=await cli('bin/agentstack-preregister-child',*common,override={
                'AGENTSTACK_FRESH_LAUNCH_ID':str(uuid4()),'AGENTSTACK_FRESH_LAUNCH_NONCE':'not-a-receipt'})
            assert false_cli.returncode==2
            wrong=await cli('bin/agentstack-preregister-child',*common,'--app-session-id',str(uuid4()))
            assert wrong.returncode==2
            proofs=[]
            for index,parent in enumerate((root,sub)):
                work=home/('external-'+str(index));work.mkdir()
                selectors=['--app-session-id',sid]+(['--app-agent-id','delegate-sub'] if index else [])
                args=common.copy();args[3]='ExternalSub' if index else 'ExternalRoot'
                prepared=await cli('bin/agentstack-preregister-child',*args,*selectors)
                assert prepared.returncode==0,prepared.stderr
                result=json.loads(prepared.stdout);child=FreshBoundClient(result['context_path'])
                assert family(child)[0].path==parent.path and child.context['mail_agent_id']!=parent.context['mail_agent_id']
                accepted=await cli('hooks/spawn_child.sh','--child-client-name','external','--embed-task',
                    '--task-file',task,'--resources','note.py','--prepare-only',*selectors,work)
                assert accepted.returncode==0,accepted.stderr
                # Missing explicit prepared socket is a refusal, not default tmux.
                no_socket=await cli('hooks/spawn_child.sh','--child-client-name','external','--embed-task',
                    '--task-file',task,'--resources','note.py',*selectors,work)
                assert no_socket.returncode==2 and 'FRESH_CHILD_TMUX_PREPARE_REQUIRED' in no_socket.stderr
                class Transport:
                    identity=(1,1);path=tmp_path/'synthetic.sock';calls=[]
                    def command(self,*args):
                        self.calls.append(args);return subprocess.CompletedProcess(args,0,'','')
                transport=Transport()
                sent=await handoff(parent,ROOT,slot='external',cwd=work,task=task.read_text(),resources=['note.py'],transport=transport)
                assert sent['accepted'] and not sent['native_ready'] and len(transport.calls)==1
                trace=tmp_path/('app-child-native-'+str(index)+'.json')
                message=dict(jsonrpc='2.0',id=3,method='tools/call',params=dict(name='send_message',arguments=dict(
                    recipients=[{'target_agent_id':parent.context['mail_agent_id']}],subject='app-external-result',body_md='complete')))
                source=NATIVE.replace('proxied = subprocess.run(proxy_cmd,','requests.insert(1, '+repr(message)+')\nproxied = subprocess.run(proxy_cmd,')
                binary.write_text('#!'+sys.executable+'\nimport os\nos.environ.update('+repr(dict(TEST_PROVIDER='claude',TEST_TRACE=str(trace)))+')\n'+source)
                argv=shlex.split(transport.calls[0][-1]);child_env=dict(env)
                child_env.pop('TMUX',None);child_env.pop('TMUX_PANE',None)
                native=await asyncio.to_thread(subprocess.run,argv,cwd=work,env=child_env,stdin=subprocess.DEVNULL,
                    text=True,capture_output=True,timeout=45)
                assert native.returncode==0,native.stderr
                actual=json.loads(trace.read_text());assert actual['status']['mail_agent_id']==child.context['mail_agent_id']
                inbox=await parent.call('fetch_inbox',{})
                assert any(m['subject']=='app-external-result' and m['sender_id']==child.context['mail_agent_id'] for m in inbox)
                proofs.append(dict(parent_id=parent.context['mail_agent_id'],child_id=child.context['mail_agent_id'],
                    common_entries=True,synthetic_transport=True,native_callback=True))
            assert all(p.read_bytes()==v for p,v in saved.items())
            (tmp_path/'app-child-proof.json').write_text(canonical(dict(routes=proofs,actual_tmux=False,real_app=False)))
    asyncio.run(run())


@pytest.mark.parametrize('owner',['root','subagent'])
def test_external_app_common_child_native_on_explicit_private_socket(tmp_path,monkeypatch,owner):
    """ProOpus only: real tmux/ps, fake Claude, no inherited/default socket."""
    from agentstack_mail.fresh_child import family,status
    from agentstack_mail.fresh_client import FreshBoundClient
    from test_fresh_launch import NATIVE,prepare
    async def run():
        private=Path(tempfile.mkdtemp(prefix='o213-app-tmux-',dir='/private/tmp'));private.chmod(0o700)
        socket=private/'tmux.sock';created=False;tmux=shutil.which('tmux');assert tmux
        env=None
        async def command(*args):
            return await asyncio.to_thread(subprocess.run,[tmux,'-u','-S',str(socket),*args],
                env=env,text=True,capture_output=True,timeout=15)
        try:
            async with desktop(tmp_path,monkeypatch,overrides={'AGENTSTACK_CODEX_APP_COLD_WAKE':'0',
                'AGENTSTACK_CODEX_APP_CHILD_SOCKET':str(socket)}) as (runtime,people,template,app,home,env):
                sid=str(uuid4());payload=event(home,sid);hook=APP/'plugin/scripts/run-hook.sh'
                assert (await invoke(hook,payload,env))['returncode']==0
                parent=await wait_route(app,sid)
                selectors=['--app-session-id',sid]
                if owner=='subagent':
                    sub=event(home,sid,sub='automatic-parent',name='SubagentStart')
                    assert (await invoke(hook,sub,env))['returncode']==0
                    parent=await wait_route(app,sid,'automatic-parent');selectors+=['--app-agent-id','automatic-parent']
                _,binary=prepare(template,'claude',tmp_path)
                trace=tmp_path/'external-app-native.json'
                message=dict(jsonrpc='2.0',id=3,method='tools/call',params=dict(name='send_message',arguments=dict(
                    recipients=[{'target_agent_id':parent.context['mail_agent_id']}],subject='app-external-result',body_md='complete')))
                source=NATIVE.replace('proxied = subprocess.run(proxy_cmd,','requests.insert(1, '+repr(message)+')\nproxied = subprocess.run(proxy_cmd,')
                binary.write_text('#!'+sys.executable+'\nimport os\nos.environ.update('+repr(dict(TEST_PROVIDER='claude',TEST_TRACE=str(trace)))+')\n'+source)
                env=dict(env,AGENTSTACK_CLAUDE_BIN=str(binary));env.pop('TMUX',None);env.pop('TMUX_PANE',None)
                assert not env.get('TMUX') and not env.get('TMUX_PANE')
                started=await command('new-session','-d','-s','fixture-anchor','sleep 120')
                assert started.returncode==0,started.stderr
                created=True
                async def cli(script,*args):
                    return await asyncio.to_thread(subprocess.run,['/bin/bash',str(ROOT/script),*map(str,args)],
                        env=env,cwd=ROOT,stdin=subprocess.DEVNULL,text=True,capture_output=True,timeout=45)
                registered=await cli('bin/agentstack-preregister-child',*selectors,'--child-client-name','worker',
                    '--name','AppExternal','--program','claude-code','--model','fixture-model',
                    '--task-description','private automatic App child','--prepare-only')
                assert registered.returncode==0,registered.stderr
                child=FreshBoundClient(json.loads(registered.stdout)['context_path'])
                assert family(child)[0].path==parent.path
                task=tmp_path/'external-app-task.md';task.write_text('Private task `literal` $(literal).')
                args=[*selectors,'--child-client-name','worker','--embed-task','--task-file',task,'--resources','note.py',home]
                launched=await cli('hooks/spawn_child.sh',*args)
                assert launched.returncode==0,launched.stderr
                _,base,_=family(child);deadline=time.monotonic()+45
                while True:
                    observed=status(child,base)
                    if observed['execution']=='ended':break
                    assert time.monotonic()<deadline,'native child did not end'
                    await asyncio.sleep(.1)
                assert observed['native_ready']
                native=json.loads(trace.read_text())
                assert native['session_hook_rc']==native['guard_rc']==native['proxy_rc']==0
                assert native['status']['mail_agent_id']==child.context['mail_agent_id']
                inbox=await parent.call('fetch_inbox',{})
                assert any(m['subject']=='app-external-result' and m['sender_id']==child.context['mail_agent_id'] for m in inbox)
                duplicate=await cli('hooks/spawn_child.sh',*args)
                assert duplicate.returncode==0 and json.loads(duplicate.stdout)==observed
                (tmp_path/'external-app-child-proof.json').write_text(canonical(dict(owner=owner,actual_private_tmux=True,
                    inherited_tmux=False,common_entries=True,native_ready=True,parent_id=parent.context['mail_agent_id'],
                    child_id=child.context['mail_agent_id'],exact_parent_mail=True,real_app=False,actual_provider=False)))
        finally:
            if created:
                stopped=await command('kill-server');assert stopped.returncode==0,stopped.stderr
            shutil.rmtree(private)
            (tmp_path/'app-tmux-cleanup.json').write_text(canonical(dict(socket=str(socket),stopped=created)))
    asyncio.run(run())


def test_desktop_unknown_cli_header_mixed_and_stale_routes_refuse(tmp_path,monkeypatch):
    async def run():
        async with desktop(tmp_path,monkeypatch) as (runtime,people,template,app,home,env):
            sid=str(uuid4());payload=event(home,sid,originator='codex_cli_rs')
            hook=APP/'plugin/scripts/run-hook.sh'
            bad=await invoke(hook,payload,env);assert bad['returncode']!=0 and 'FRESH_APP_' in bad['stderr']
            assert not app.path(sid).exists()
            async with proxy(env) as peer:
                assert refused(await peer.call('bootstrap',dict(session_id=sid)))=='FRESH_PRIVATE_FILE_UNAVAILABLE'
            transcript=Path(payload['transcript_path'])
            transcript.write_text(canonical(dict(type='session_meta',payload=dict(id=sid,originator='Codex Desktop')))+'\n')
            assert (await invoke(hook,payload,env))['returncode']==0
            root=await wait_route(app,sid)
            async with proxy(env) as peer:
                await peer.value('bootstrap',dict(session_id=sid))
                mixed=await invoke(hook,payload,dict(env,AGENTSTACK_CLIENT_CONFIG='/fixture/old-global'))
                assert mixed['returncode']!=0
                wrong=await invoke(hook,{**payload,'session_id':str(uuid4())},env);assert wrong['returncode']!=0
                # Changed immutable Desktop header cannot keep a bound proxy alive.
                transcript.write_text(canonical(dict(type='session_meta',payload=dict(id=sid,originator='codex_cli_rs')))+'\n')
                assert refused(await peer.call('runtime_status'))=='FRESH_APP_ROUTE_MISMATCH'
            assert (await template.call('reconnect',{}))['mail_agent_id']==template.context['mail_agent_id']
    asyncio.run(run())


def test_desktop_expired_lease_and_revoked_current_owner_never_use_template(tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from sqlalchemy import select
    from agentstack_mail.models import FileReservation
    async def run():
        async with desktop(tmp_path,monkeypatch) as (runtime,people,template,app,home,env):
            sid=str(uuid4());payload=event(home,sid)
            hook=APP/'plugin/scripts/run-hook.sh'
            assert (await invoke(hook,payload,env))['returncode']==0
            owner=await wait_route(app,sid)
            async with proxy(env) as peer:
                await peer.value('bootstrap',dict(session_id=sid))
                target=home/'expiry.txt'
                await peer.value('reserve_files',dict(paths=[str(target)],ttl_seconds=900))
                async with runtime.sessions() as session:
                    lease=(await session.execute(select(FileReservation).where(FileReservation.agent_id==owner.context['mail_agent_id']))).scalars().one()
                    lease.expires_ts=datetime.now(timezone.utc)-timedelta(seconds=1)
                    await session.commit()
                guard=APP/'plugin/scripts/run-reservation-guard.sh'
                command='*** Begin Patch\n*** Add File: '+str(target)+'\n+fixture\n*** End Patch'
                denied=await invoke(guard,dict(payload,tool_name='apply_patch',tool_input={'command':command}),env)
                assert denied['returncode']!=0 and 'RESERVATION_REQUIRED' in denied['stderr']
                await runtime.core.recover(owner.context['mail_agent_id'],expected_generation=owner.context['credential_generation'],
                                           new_token='new-private-fixture-credential')
                assert (await peer.call('runtime_status'))['result']['isError']
                assert (await invoke(guard,dict(payload,tool_name='apply_patch',tool_input={'command':command}),env))['returncode']!=0
                # Another valid template does not become this process's owner.
                assert (await template.call('reconnect',{}))['mail_agent_id']==template.context['mail_agent_id']
    asyncio.run(run())
