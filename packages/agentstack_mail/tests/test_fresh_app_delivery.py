"""Private App hook/worker and resume subprocess; no Desktop or real trust."""
import asyncio
from dataclasses import replace
from contextlib import contextmanager
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from sqlalchemy import select

from agentstack_mail.fresh_notifications import facts
from agentstack_mail.fresh_runtime import _read_private, _write_private
from agentstack_mail.models import MessageRecipient
from agentstack_mail.storage_origin_contract import canonical
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_app import APP, ROOT, desktop, event, invoke, wait_route
from agentstack_codex_app.fresh_app_delivery import AppDelivery
from agentstack_codex_app.fresh_bridge import FreshBridge
from agentstack_codex_app.hook_entry import forward_event_response


async def send(sender,recipient,subject,importance='high'):
    return await sender.call('send_message',dict(recipients=[{'target_agent_id':recipient.context['mail_agent_id']}],
        subject=subject,body_md='Private notification fixture.',importance=importance))


async def pair(runtime,mid,aid):
    async with runtime.sessions() as session:
        value=await session.get(MessageRecipient,(mid,aid))
        completed=(await session.execute(select(facts.c.completed_ts).where(
            facts.c.message_id==mid,facts.c.agent_id==aid))).scalar_one()
        return completed,value.read_ts,value.ack_ts


async def until(action,predicate,seconds=15):
    deadline=time.monotonic()+seconds
    while True:
        value=await action()
        if predicate(value):return value
        assert time.monotonic()<deadline,'fixture condition did not become ready'
        await asyncio.sleep(.1)


def test_real_hook_root_sub_recipient_flush_completion_and_uncertain_ipc(tmp_path,monkeypatch):
    async def run():
        async with desktop(tmp_path,monkeypatch,overrides={'AGENTSTACK_CODEX_APP_COLD_WAKE':'0',
            'AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE':'high'}) as (runtime,people,template,app,home,env):
            sid=str(uuid4());root_event=event(home,sid);hook=APP/'plugin/scripts/run-hook.sh'
            assert (await invoke(hook,root_event,env))['returncode']==0
            root=await wait_route(app,sid)
            sub_event=event(home,sid,sub='worker',name='SubagentStart')
            assert (await invoke(hook,sub_event,env))['returncode']==0
            sub=await wait_route(app,sid,'worker')
            low=await send(template,root,'excluded-low','low')
            parent_mail=await send(template,root,'root-only')
            sub_mail=await send(template,sub,'sub-only')
            async def root_hook():return await invoke(hook,{**root_event,'hook_event_name':'PostToolUse'},env)
            shown=await until(root_hook,lambda r:'root-only' in r['stdout'])
            assert shown['returncode']==0 and shown['elapsed']<1
            assert 'sub-only' not in shown['stdout'] and 'excluded-low' not in shown['stdout']
            await until(lambda:pair(runtime,parent_mail['message_id'],root.context['mail_agent_id']),lambda v:v[0] is not None)
            assert (await pair(runtime,parent_mail['message_id'],root.context['mail_agent_id']))[1:]==(None,None)
            assert await pair(runtime,low['message_id'],root.context['mail_agent_id'])==(None,None,None)
            assert await pair(runtime,sub_mail['message_id'],sub.context['mail_agent_id'])==(None,None,None)
            async def sub_hook():return await invoke(hook,{**sub_event,'hook_event_name':'PostToolUse'},env)
            shown_sub=await until(sub_hook,lambda r:'sub-only' in r['stdout'])
            assert 'root-only' not in shown_sub['stdout']
            await until(lambda:pair(runtime,sub_mail['message_id'],sub.context['mail_agent_id']),lambda v:v[0] is not None)
            assert (await pair(runtime,sub_mail['message_id'],sub.context['mail_agent_id']))[1:]==(None,None)
            assert 'root-only' not in (await root_hook())['stdout']
            large=[]
            for index in range(8):
                large.append(await template.call('send_message',dict(recipients=[{'target_agent_id':root.context['mail_agent_id']}],
                    subject='bounded-'+str(index),body_md='日本語'*1000,importance='high')))
            shown_large=await until(root_hook,lambda r:'bounded-' in r['stdout'])
            assert shown_large['returncode']==0 and len(shown_large['stdout'].encode())<65536
            # Remaining facts are delivered in later bounded batches.
            async def consumed():
                return [await pair(runtime,m['message_id'],root.context['mail_agent_id']) for m in large]
            deadline=time.monotonic()+15
            hook_results=[]
            while True:
                states=await consumed()
                if all(v[0] is not None for v in states):break
                assert time.monotonic()<deadline, dict(
                    pending=[m['message_id'] for m,v in zip(large,states) if v[0] is None],
                    states=states,hook_results=hook_results)
                result=await root_hook()
                hook_results.append(dict(returncode=result['returncode'],elapsed=result['elapsed'],
                    stdout_bytes=len(result['stdout'].encode()),stderr_bytes=len(result['stderr'].encode())))
                await asyncio.sleep(.1)
            assert all(v[1:]==(None,None) for v in await consumed())
            ambiguous=await send(template,root,'no-flush-receipt')
            raw={k:v for k,v in {**root_event,'hook_event_name':'PostToolUse','schema_version':1}.items() if k!='transcript_path'}
            async def ipc():return await asyncio.to_thread(forward_event_response,raw,app.directory/'bridge.sock')
            response=await until(ipc,lambda v:bool(v and v.get('fresh_notification')))
            assert 'no-flush-receipt' in '\n'.join(response['fresh_notification']['notifications'])
            # Taking IPC output without a post-flush receipt cannot consume it.
            await asyncio.sleep(.3)
            assert await pair(runtime,ambiguous['message_id'],root.context['mail_agent_id'])==(None,None,None)
            (tmp_path/'app-notification-proof.json').write_text(canonical(dict(real_hook=True,local_ipc=True,
                recipient_separated=True,completion_after_flush=True,uncertain_pending=True,read_ack_unchanged=True,
                root_hook_seconds=shown['elapsed'],real_app=False)))
    asyncio.run(run())


WAKE=r'''
import json,os,pathlib,subprocess,sys
args=sys.argv[1:]
session=args[-2]  # Existing resume adapter puts -c options before the positionals.
values=[args[i+1] for i,v in enumerate(args) if v=='-c']
command=json.loads(next(v.split('=',1)[1] for v in values if v.startswith('mcp_servers.agentstack.command=')))
extra=json.loads(next(v.split('=',1)[1] for v in values if v.startswith('mcp_servers.agentstack.args=')))
requests=[{'jsonrpc':'2.0','id':1,'method':'initialize'},
 {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'bootstrap','arguments':{'session_id':session}}},
 {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'runtime_status','arguments':{}}}]
result=subprocess.run([command,*extra],input=''.join(json.dumps(v)+'\n' for v in requests),text=True,capture_output=True,timeout=20)
replies=[json.loads(v) for v in result.stdout.splitlines()]
status=json.loads(replies[-1]['result']['content'][0]['text'])
pathlib.Path(os.environ['APP_WAKE_TRACE']).write_text(json.dumps(dict(session=session,home=os.environ['CODEX_HOME'],
    aid=status['mail_agent_id'],proxy_rc=result.returncode,context=os.environ['AGENTSTACK_FRESH_CLIENT_CONTEXT'])))
sys.exit(int(os.environ.get('APP_WAKE_EXIT','0')))
'''


def synthetic_approval(app,binary,cwd):
    # Existing verifier's private synthetic format; no native approval occurs.
    plugin=app.home/'plugin';shutil.copytree(APP/'plugin',plugin)
    trust=runpy.run_path(str(ROOT/'scripts/codex_plugin_trust.py'))
    pid='agentstack-codex-app@fresh-fixture';key='synthetic-app-wake'
    plan=dict(schema_version=1,approved=True,enable_hooks=True,codex_binary=str(binary),
        shared_codex_home=str(app.home),cwd=str(cwd),plugin_id=pid,plugin_root=str(plugin),
        payload_digest=trust['payload_digest'](plugin),declared_hooks=json.loads((plugin/'hooks/hooks.json').read_text()),
        hooks=[dict(key=key,currentHash='sha256:synthetic')])
    _write_private(app.home/'hook-plan.json',plan)
    config=app.home/'config.toml'
    config.write_text('[plugins.'+json.dumps(pid)+']\nenabled=true\n[hooks.state.'+json.dumps(key)+']\ntrusted_hash="sha256:synthetic"\nenabled=true\n')
    config.chmod(0o600)


@pytest.mark.parametrize('exit_code',[0,17,'unknown'])
def test_waiting_root_only_existing_resume_process_and_failure_keeps_pending(tmp_path,monkeypatch,exit_code):
    async def run():
        binary=tmp_path/'fake-codex-resume';binary.write_text('#!'+sys.executable+'\n'+WAKE);binary.chmod(0o700)
        trace=tmp_path/'wake-trace.json'
        async with desktop(tmp_path,monkeypatch,overrides={'AGENTSTACK_CODEX_APP_COLD_WAKE':'1',
            'AGENTSTACK_CODEX_BINARY':str(binary),'APP_WAKE_TRACE':str(trace),'APP_WAKE_EXIT':str(exit_code),
            'AGENTSTACK_CODEX_APP_PLUGIN_ID':'agentstack-codex-app@fresh-fixture',
            'AGENTSTACK_CODEX_APP_WAKE_COALESCE_SECONDS':'0','AGENTSTACK_CODEX_APP_WAKE_MAX_ATTEMPTS':'1'}) as (runtime,people,template,app,home,env):
            sid=str(uuid4());payload=event(home,sid);hook=APP/'plugin/scripts/run-hook.sh'
            assert (await invoke(hook,payload,env))['returncode']==0
            root=await wait_route(app,sid)
            synthetic_approval(app,binary,home)
            message=await send(template,root,'wake-on-observed-stop')
            # SessionStart is a live turn, not evidence it has ended.
            await asyncio.sleep(.8);assert not trace.exists()
            if exit_code=='unknown':
                _write_private(app.path(sid).with_suffix('.wake.json'),dict(phase='unknown',messages=[message['message_id']],attempts=1))
            assert (await invoke(hook,{**payload,'hook_event_name':'Stop'},env))['returncode']==0
            if exit_code=='unknown':
                await asyncio.sleep(.8);assert not trace.exists()
                assert await pair(runtime,message['message_id'],root.context['mail_agent_id'])==(None,None,None)
                return
            async def read_trace():return json.loads(trace.read_text()) if trace.exists() else None
            result=await until(read_trace,lambda v:v is not None)
            assert result['session']==sid and result['home']==str(home)
            assert result['aid']==root.context['mail_agent_id'] and result['aid']!=template.context['mail_agent_id']
            assert result['proxy_rc']==0
            marker=app.path(sid).with_suffix('.wake.json')
            async def read_marker():return _read_private(marker)
            final=await until(read_marker,lambda v:v.get('phase') in {'done','failed'})
            assert final['phase']==('done' if exit_code==0 else 'failed')
            values=await pair(runtime,message['message_id'],root.context['mail_agent_id'])
            assert (values[0] is not None)==(exit_code==0) and values[1:]==(None,None)
            original=trace.read_bytes();await asyncio.sleep(.5);assert trace.read_bytes()==original
            if exit_code!=0:
                await send(template,root,'new-mail-does-not-reset-failure-budget')
                await asyncio.sleep(.5);assert trace.read_bytes()==original
            sub_event=event(home,sid,sub='ended-sub',name='SubagentStart')
            assert (await invoke(hook,sub_event,env))['returncode']==0
            sub=await wait_route(app,sid,'ended-sub')
            assert (await invoke(hook,{**sub_event,'hook_event_name':'SubagentStop'},env))['returncode']==0
            await send(template,sub,'do-not-resume-subagent');await asyncio.sleep(.5)
            assert trace.read_bytes()==original
            (tmp_path/'app-wake-proof.json').write_text(canonical(dict(fake_resume_process=True,exit_code=exit_code,
                waiting_root_only=True,current_app_owner=True,read_ack_unchanged=True,real_app=False,actual_trust=False)))
    asyncio.run(run())


def test_unprepared_wake_refuses_without_writing_trust(tmp_path,monkeypatch):
    async def run():
        async with desktop(tmp_path,monkeypatch,overrides={'AGENTSTACK_CODEX_APP_COLD_WAKE':'0'}) as (runtime,people,template,app,home,env):
            sid=str(uuid4());payload=event(home,sid);hook=APP/'plugin/scripts/run-hook.sh'
            assert (await invoke(hook,payload,env))['returncode']==0
            root=await wait_route(app,sid)
            delivery=AppDelivery(app,replace(FreshBridge(app).config,cold_wake_enabled=True,wake_coalesce_seconds=0))
            try:
                # Use the existing admission routine directly with no trust plan.
                state=dict(delivery.state(root),state='waiting')
                with pytest.raises(B1Error):delivery.start_wake(root,[dict(id=1,sender_id=1,subject='fixture')],state)
                # Admission does not fabricate approval or a parallel process.
                assert not (home/'hook-plan.json').exists() and not delivery.inflight
            finally:delivery.close()
    asyncio.run(run())


def test_accepted_start_during_pending_invalidates_wake_dispatch(tmp_path,monkeypatch):
    """Real handler/queue/tick/state/dispatch; only owner/pending/process synthetic."""
    from types import SimpleNamespace
    home=tmp_path/'desktop';home.mkdir(mode=0o700);(home/'sessions').mkdir()
    runtime=tmp_path/'app';runtime.mkdir(mode=0o700);routes=runtime/'routes';routes.mkdir(mode=0o700)
    mail=tmp_path/'mail';mail.mkdir(mode=0o700)
    sid=str(uuid4());client=SimpleNamespace(session_id=sid,agent_id=None,
        context=dict(root=str(mail),mail_agent_id=7),path=mail/'context.json',_load=lambda:None)
    path=routes/(sid+'.json')
    app=SimpleNamespace(root=ROOT,home=home,directory=runtime,routes=routes,sessions=home/'sessions',
        template=SimpleNamespace(path=mail/'template.json',context={'endpoint':'http://127.0.0.1:19001/mcp'}),
        path=lambda session,sub=None:path,resolve=lambda session,sub=None:client,
        check=lambda selected,session,sub:dict(session_id=sid,agent_id=None),transcript=lambda session:home/'transcript.jsonl')
    _write_private(path,dict(session_id=sid,agent_id=None))
    binary=tmp_path/'fake-codex';binary.write_text('fixture, never executed');binary.chmod(0o700)
    synthetic_approval(app,binary,home)
    monkeypatch.setenv('AGENTSTACK_CODEX_APP_COLD_WAKE','1')
    monkeypatch.setenv('AGENTSTACK_CODEX_BINARY',str(binary))
    monkeypatch.setenv('AGENTSTACK_CODEX_APP_PLUGIN_ID','agentstack-codex-app@fresh-fixture')
    monkeypatch.setenv('AGENTSTACK_CODEX_APP_WAKE_COALESCE_SECONDS','0')
    bridge=FreshBridge(app);delivery=bridge.delivery
    stop=dict(schema_version=1,session_id=sid,agent_id=None,cwd=str(home),model=None,
        hook_event_name='Stop',turn_id=None)
    start=dict(stop,hook_event_name='UserPromptSubmit')
    class Connection:
        def __init__(self,payload):self.raw=canonical(payload).encode()+b'\n';self.answer=None
        def recv(self,size):value=self.raw;self.raw=b'';return value
        def sendall(self,data):self.answer=json.loads(data)
    def accept(payload):
        connection=Connection(payload);bridge._handle_connection(connection)
        assert connection.answer['ok'];return connection
    def process_next():
        payload,sequence=bridge._events.get_nowait()
        if bridge.process_event(payload,sequence=sequence) is not None:delivery.processed(payload,sequence)
    accept(stop);process_next()
    popens=[]
    class Process:
        stdout=None
        def poll(self):return None
        def terminate(self):pass
        def wait(self,timeout=None):return 0
    def popen(*args,**kwargs):
        # Other-thread acceptance cannot interleave in the dispatch interval.
        assert not delivery.admission.acquire(blocking=False)
        popens.append(args);return Process()
    monkeypatch.setattr('agentstack_codex_app.fresh_app_delivery.subprocess.Popen',popen)
    completed=[]
    async def complete(row):completed.append(row)
    monkeypatch.setattr(delivery,'complete',complete)
    message=dict(id=1,sender_id=8,subject='pending fixture')
    async def pending(selected):
        # HTTP/owner work is outside admission; the actual IPC can accept start.
        assert delivery.admission.acquire(blocking=False);delivery.admission.release()
        accept(start)
        return [message]
    monkeypatch.setattr(delivery,'pending',pending)
    try:
        delivery.tick()
        assert bridge._events.qsize()==1 and not popens and not delivery.inflight and not completed
        assert not path.with_suffix('.wake.json').exists()  # Cancelled before accounting/dispatch.
        # Repeated ticks must keep the hook handoff while start is queued,
        # rather than releasing/recreating it on the stale waiting projection.
        ready=delivery.ready[str(path)]
        delivery.tick()
        assert delivery.ready[str(path)] is ready and not popens and not completed
        process_next();assert delivery.state(client)['state']=='working'
        # A queued old Stop cannot publish waiting over its accepted successor.
        accept(stop);accept(start);process_next()
        assert delivery.state(client)['state']=='working'
        process_next()
        # Recovered spool events use the same versioned queue.
        from agentstack_codex_app.hook_entry import append_spool
        append_spool(stop,bridge.config.spool_path);bridge.queue_spool(bridge.config.spool_path);process_next()
        assert delivery.state(client)['state']=='waiting'
        async def quiet_pending(selected):return [message]
        monkeypatch.setattr(delivery,'pending',quiet_pending)
        delivery.tick()
        assert len(popens)==1 and len(delivery.inflight)==1 and not completed
        (tmp_path/'app-wake-order-proof.json').write_text(canonical(dict(real_handler=True,real_tick=True,
            real_start_wake=True,accepted_start_blocks_dispatch=True,pending_retained=True,
            old_stop_cannot_override_new_start=True,spool_same_queue=True,quiet_root_dispatches=True,
            synthetic_owner_pending_process=True,native_processes=0,http_requests=0)))
    finally:delivery.close()


@contextmanager
def _app_ordering_fixture(tmp_path,monkeypatch):
    """Real delivery/consumer locks and FIFO; only App routes and Mail wire fake."""
    from types import SimpleNamespace
    from agentstack_codex_app.fresh_bridge import reference
    home=tmp_path/'desktop';home.mkdir(mode=0o700);(home/'sessions').mkdir()
    runtime=tmp_path/'app';runtime.mkdir(mode=0o700);routes=runtime/'routes';routes.mkdir(mode=0o700)
    mail=tmp_path/'mail';mail.mkdir(mode=0o700)
    sid=str(uuid4());faults={};calls=[];rows={};clients={};callback=[None]
    def path(session,sub=None):return routes/(reference(session,sub)+'.json')
    class Client:
        def __init__(self,aid,sub):
            self.session_id,self.agent_id=sid,sub
            self.path=mail/(str(aid)+'.json')
            self.context=dict(root=str(mail),mail_instance_id='a'*32,mail_agent_id=aid,credential_generation=1)
        def _load(self):
            fault=faults.get(self.context['mail_agent_id'])
            if fault in {'route','stopped'}:
                raise B1Error('FRESH_APP_SUBAGENT_STOPPED' if fault=='stopped' else 'FRESH_APP_ROUTE_MISMATCH')
        async def call(self,tool,args):
            self._load();aid=self.context['mail_agent_id'];calls.append((aid,tool,dict(args)))
            if callback[0] is not None:callback[0](self,tool)
            if faults.get(aid)=='transport':raise OSError('synthetic transport unavailable')
            identity={k:self.context[k] for k in ('mail_instance_id','mail_agent_id','credential_generation')}
            if tool=='reconnect':
                if faults.get(aid)=='owner':identity['mail_agent_id']+=100
                return dict(identity,agent_name='Fixture'+str(aid))
            if tool=='notification_pending':
                # A legal bounded wire response; do not mock AppDelivery.pending.
                values=[value['message'] for (owner,mid),value in sorted(rows.items())
                    if owner==aid and not value['completed'] and args.get('message_id',mid)==mid]
                return dict(identity,messages=values[:1])
            assert tool=='notification_complete','unexpected Mail side effect'
            rows[aid,args['message_id']]['completed']=True
            return dict(identity,message_id=args['message_id'])
    def check(client,session,sub):
        assert clients[sub] is client and session==sid
        client._load();return dict(session_id=sid,agent_id=sub)
    for aid,sub in ((7,None),(8,'worker')):
        client=Client(aid,sub);clients[sub]=client
        _write_private(path(sid,sub),dict(session_id=sid,agent_id=sub))
        _write_private(path(sid,sub).with_suffix('.state.json'),dict(session_id=sid,agent_id=sub,
            state='working',event='PostToolUse',cwd=str(home)))
        for mid in range(1,4):
            rows[aid,mid]=dict(message=dict(id=mid,sender_id=99,sender_name='Sender',
                subject='recipient-'+str(aid)+'-'+str(mid),body_md='fixture',importance='high'),
                completed=False,read_ts=None,ack_ts=None)
    app=SimpleNamespace(root=ROOT,home=home,directory=runtime,routes=routes,sessions=home/'sessions',
        template=SimpleNamespace(path=mail/'template.json',context={'endpoint':'http://127.0.0.1:19001/mcp'}),
        path=path,resolve=lambda session,sub=None:clients[sub],check=check,
        transcript=lambda session:home/'transcript.jsonl')
    monkeypatch.setenv('AGENTSTACK_CODEX_APP_COLD_WAKE','0')
    monkeypatch.delenv('AGENTSTACK_MAIL_DISABLED',raising=False)
    bridge=FreshBridge(app);delivery=bridge.delivery
    post=dict(schema_version=1,session_id=sid,agent_id=None,cwd=str(home),model=None,
        hook_event_name='PostToolUse',turn_id=None)
    class Connection:
        def __init__(self,payload):self.raw=canonical(payload).encode()+b'\n';self.answer=None
        def recv(self,size):value=self.raw;self.raw=b'';return value
        def sendall(self,data):self.answer=json.loads(data)
    def accept(payload=None):
        connection=Connection(post if payload is None else payload);bridge._handle_connection(connection)
        assert connection.answer['ok'];return connection.answer
    def batch():return bridge._process_event_batch(bridge._events.get_nowait())
    def receipt(notice):return accept(dict(fresh_receipt=notice['receipt'],
        session_id=notice['session_id'],agent_id=notice['agent_id']))
    try:
        yield SimpleNamespace(app=app,bridge=bridge,delivery=delivery,clients=clients,rows=rows,
            calls=calls,faults=faults,callback=callback,post=post,accept=accept,batch=batch,receipt=receipt)
    finally:delivery.close()


def test_post_tool_acceptance_keeps_hot_batches_moving_without_quiet(tmp_path,monkeypatch):
    with _app_ordering_fixture(tmp_path,monkeypatch) as case:
        delivery=case.delivery;key=str(case.app.path(case.post['session_id']))
        case.accept();case.batch()
        def incoming(client,tool):
            if client.agent_id is None and tool=='reconnect':case.accept()
        case.callback[0]=incoming
        for _ in range(3):
            # Never wait for a quiet queue before preparing the next batch.
            case.accept();case.accept()
            delivery.tick()
            assert delivery.accepted[key]!=delivery.applied.get(key,0)
            notice=case.accept()['fresh_notification']
            assert all('recipient-7-' in text and 'recipient-8-' not in text for text in notice['notifications'])
            assert not case.rows[8,1]['completed']
            completed=sum(v['completed'] for (aid,_),v in case.rows.items() if aid==7)
            case.receipt(notice)
            assert sum(v['completed'] for (aid,_),v in case.rows.items() if aid==7)==completed
            case.batch()  # Completion happens through the real FIFO receipt handler.
            assert sum(v['completed'] for (aid,_),v in case.rows.items() if aid==7)==completed+1
        assert all(v['completed'] for (aid,_),v in case.rows.items() if aid==7)
        other=case.accept(dict(case.post,agent_id='worker'))['fresh_notification']
        assert all('recipient-8-' in text and 'recipient-7-' not in text for text in other['notifications'])
        case.receipt(other);case.batch()
        assert case.rows[8,1]['completed'] and not case.rows[8,2]['completed']
        assert all((v['read_ts'],v['ack_ts'])==(None,None) for v in case.rows.values())
        assert {tool for _,tool,_ in case.calls}=={'reconnect','notification_pending','notification_complete'}


def test_claim_without_receipt_stays_pending_during_new_hooks(tmp_path,monkeypatch):
    with _app_ordering_fixture(tmp_path,monkeypatch) as case:
        case.delivery.tick();notice=case.accept()['fresh_notification']
        key=str(case.app.path(case.post['session_id']))
        held=case.delivery.ready[key]
        for _ in range(3):
            assert 'fresh_notification' not in case.accept()
            case.batch();case.delivery.tick()
            assert case.delivery.ready[key] is held and held['claimed']
        assert not any(v['completed'] for v in case.rows.values())
        with pytest.raises(B1Error,match='FRESH_APP_RECEIPT_INVALID'):
            case.delivery.receipt(dict(fresh_receipt='wrong',session_id=notice['session_id'],agent_id=None))
        assert case.delivery.ready[key] is held


@pytest.mark.parametrize('fault',['owner','route','transport','stopped'])
def test_hot_batches_do_not_convert_owner_route_or_transport_refusals(tmp_path,monkeypatch,fault):
    with _app_ordering_fixture(tmp_path,monkeypatch) as case:
        case.faults[7]=fault
        case.accept();case.accept()
        key=str(case.app.path(case.post['session_id']))
        case.delivery.tick()
        assert key not in case.delivery.ready and not any(v['completed'] for v in case.rows.values())
        assert 'fresh_notification' not in case.accept()


@pytest.mark.parametrize('fault',['route','stopped'])
def test_ready_take_rechecks_route_and_stopped_owner(tmp_path,monkeypatch,fault):
    with _app_ordering_fixture(tmp_path,monkeypatch) as case:
        case.delivery.tick();case.faults[7]=fault
        with pytest.raises(B1Error):case.delivery.take(case.post)
        key=str(case.app.path(case.post['session_id']))
        assert not case.delivery.ready[key].get('claimed')
        assert not any(v['completed'] for v in case.rows.values())


def test_fifo_batch_limit_leaves_receipt_for_next_batch(tmp_path,monkeypatch):
    with _app_ordering_fixture(tmp_path,monkeypatch) as case:
        case.delivery.tick();notice=case.accept()['fresh_notification'];case.batch()
        for _ in range(64):case.accept()
        case.receipt(notice)
        assert case.batch()==64 and case.bridge._events.qsize()==1
        assert not any(v['completed'] for v in case.rows.values())
        assert case.batch()==1 and case.rows[7,1]['completed']


def test_worker_ticks_once_per_bounded_fifo_batch(tmp_path,monkeypatch):
    with _app_ordering_fixture(tmp_path,monkeypatch) as case:
        for _ in range(65):case.accept()
        remaining=[]
        def tick():
            remaining.append(case.bridge._events.qsize())
            if len(remaining)==2:case.bridge._stop.set()
        monkeypatch.setattr(case.delivery,'tick',tick)
        case.bridge._worker_loop()
        assert remaining==[1,0]
        key=str(case.app.path(case.post['session_id']))
        assert case.delivery.accepted[key]==case.delivery.applied[key]==65


@pytest.mark.parametrize('interruption',['stop','unexpected','spooled'])
def test_fifo_batch_preserves_unprocessed_receipt_and_event_failures(tmp_path,monkeypatch,interruption):
    with _app_ordering_fixture(tmp_path,monkeypatch) as case:
        case.delivery.tick();notice=case.accept()['fresh_notification'];case.batch()
        case.accept();case.receipt(notice)
        if interruption!='spooled':case.accept()  # A receipt and lifecycle tail must both remain.
        original=case.bridge.process_event
        def process(payload,**kw):
            if interruption=='unexpected':raise RuntimeError('synthetic processing interruption')
            if interruption=='spooled':raise B1Error('FRESH_APP_ROUTE_MISMATCH')
            result=original(payload,**kw);case.bridge._stop.set();return result
        monkeypatch.setattr(case.bridge,'process_event',process)
        if interruption=='unexpected':
            with pytest.raises(RuntimeError):case.batch()
        else:assert case.batch()==(2 if interruption=='spooled' else 1)
        if interruption=='spooled':
            assert case.rows[7,1]['completed']
            spooled=[json.loads(line) for line in case.bridge.config.spool_path.read_text().splitlines()]
            assert spooled==[case.post]
        else:
            assert case.bridge._events.qsize()==2 and not case.rows[7,1]['completed']
            payload=case.bridge._events.queue[0]
            assert payload['fresh_receipt']==notice['receipt']
            assert case.bridge._events.queue[1][0]==case.post
