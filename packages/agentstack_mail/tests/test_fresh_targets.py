"""Current owner/launch/pane controls over real ORM and Handler, mocked OS."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_launch import identity
from agentstack_mail.fresh_runtime import _read_private, _write_private
from agentstack_mail.fresh_targets import Targets
from agentstack_mail.storage_origin_contract import B1Error
from dashboard.fresh_backend import Backend
from test_fresh_guard import live
from test_fresh_mail import snapshot
from test_fresh_dashboard import dashboard
from test_fresh_delivery import prepared_route, mocked_os
from test_fresh_launch import prepare as prepare_provider

ROOT=Path(__file__).resolve().parents[3]


def test_host_target_import_needs_no_mcp_sdk(tmp_path):
    code='''
import importlib.abc,json,sys
class NoSDK(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.',1)[0] in {'fastmcp','mcp'}:
            raise AssertionError('host target imported an unused MCP SDK')
sys.meta_path.insert(0,NoSDK())
import agentstack_mail.fresh_targets
from agentstack_mail.fresh_client import Client
assert callable(Client)
assert not any(n.split('.',1)[0] in {'fastmcp','mcp'} for n in sys.modules)
print(json.dumps({'host_import_ok':True,'mcp_loaded':False}))
'''
    env=dict(os.environ,PYTHONPATH=str(ROOT/'packages/agentstack_mail/src'),PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([sys.executable,'-c',code],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'host_import_ok':True,'mcp_loaded':False}


def replace(path,value):
    temporary=path.with_name('.fixture-'+uuid4().hex)
    _write_private(temporary,value);os.replace(temporary,path)


async def setup(client,tmp_path,monkeypatch,*,ended=False):
    run,row=await prepared_route(client,tmp_path,monkeypatch)
    intent=_read_private(run/'intent.json');intent['cwd']=str(tmp_path);replace(run/'intent.json',intent)
    commands,tmux,processes=mocked_os(monkeypatch)
    tmux['Transport'].binary='/fixture/tmux'
    tmux['Transport'].argv=property(lambda self:[self.binary,'-S',str(self.path)])
    old=processes['belongs_to']
    processes['belongs_to']=lambda child,parent,uid,**kw:(child,parent)==(100,100) or old(child,parent,uid,**kw)
    if ended:_write_private(run/'ended.json',dict(launch_id=row['launch_id'],exit_code=0))
    return run,row,commands,tmux,processes


@asynccontextmanager
async def cleanup_fixture(tmp_path,monkeypatch):
    async with live(tmp_path) as (runtime,people,client,_):
        run,row,calls,tmux,processes=await setup(client,tmp_path,monkeypatch,ended=True)
        operator=await FreshBoundClient.prepare(runtime.root/'config.json',people[1]['binding_path'],
            client.context['endpoint'],client_id=str(uuid4()))
        socket=tmp_path/'private.sock';socket.touch()
        row=dict(row,socket_path=str(socket));replace(run/'tmux.json',row)
        state=dict(present=True,attached=False,failure=False,provider=False,changed=False)
        tmux['validate_socket']=lambda path:(11,22)
        processes['process_snapshot']=lambda **kw:({100:(1,os.getuid(),'wrong' if state['changed'] else 'pane-birth')}
            if state['present'] else {})|({102:(100,os.getuid(),'provider-birth')} if state['provider'] else {})|{999:(1,os.getuid(),'unrelated')}
        def command(self,*args,**kwargs):
            calls.append((args,kwargs))
            stdout=''
            if args[0]=='list-panes':stdout='$1\t%2\t100\t0\tzsh\n' if state['present'] else ''
            if args[0]=='list-clients':stdout='$1\n' if state['attached'] else ''
            if args[0]=='kill-pane':
                from agentstack_mail.fresh_launch import operation
                with pytest.raises(B1Error,match='FRESH_LAUNCH_BUSY'):
                    with operation(run.parent.parent):pass
                if not state['failure']:state['present']=False
            return subprocess.CompletedProcess(args,1 if args[0]=='kill-pane' and state['failure'] else 0,stdout,'')
        monkeypatch.setattr(tmux['Transport'],'command',command)
        yield runtime,people,client,operator,run,row,calls,tmux,processes,state


@pytest.mark.parametrize('mode',['both','retire','tmux'])
def test_host_cleanup_same_identity_effects_and_exactly_once(tmp_path,monkeypatch,mode):
    async def scenario():
        async with cleanup_fixture(tmp_path,monkeypatch) as (runtime,people,client,operator,run,row,calls,*_):
            target=Targets(operator,ROOT);query=dict(mail_instance_id=runtime.config['instance_id'],mail_agent_id=people[0]['agent_id'])
            result=await target.cleanup(query,mode=mode)
            assert result['ok'] and result['execution']=='completed'
            assert result['effects']['tmux']==('not_requested' if mode=='retire' else 'pane_removed')
            assert result['effects']['retire']==('not_requested' if mode=='tmux' else 'retired')
            before=len(calls);assert await target.cleanup(query,mode=mode)==result
            assert len(calls)==before
            if mode!='tmux':
                with pytest.raises(B1Error,match='TARGET_RETIRED'):await target.select(query)
            assert not any(args[0]=='kill-session' for args,kw in calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('damage',['attached','provider','changed','not_ended','unknown_socket','own_operator'])
def test_cleanup_rejects_unproven_or_attached_target_before_effect(tmp_path,monkeypatch,damage):
    async def scenario():
        async with cleanup_fixture(tmp_path,monkeypatch) as (runtime,people,client,operator,run,row,calls,tmux,processes,state):
            if damage in state:state[damage]=True
            if damage=='not_ended':(run/'ended.json').unlink()
            if damage=='unknown_socket':tmux['validate_socket']=lambda path:(33,44)
            if damage=='own_operator':operator=client
            before=await snapshot(runtime)
            if damage=='own_operator':
                assert not (await Targets(operator,ROOT).cleanup(dict(session='Alpha')))['ok']
            else:
                with pytest.raises(B1Error):await Targets(operator,ROOT).cleanup(dict(session='Alpha'))
            assert not any(args[0]=='kill-pane' for args,kw in calls)
            assert await snapshot(runtime)==before
    asyncio.run(scenario())


def test_cleanup_partial_retire_survives_unknown_kill_without_resend(tmp_path,monkeypatch):
    async def scenario():
        async with cleanup_fixture(tmp_path,monkeypatch) as (runtime,people,client,operator,run,row,calls,tmux,processes,state):
            state['failure']=True;targets=Targets(operator,ROOT)
            result=await targets.cleanup(dict(session='Alpha'))
            assert not result['ok'] and result['effects']==dict(retire='retired',tmux='unknown')
            count=len(calls);assert await targets.cleanup(dict(session='Alpha'))==result
            assert len(calls)==count
            assert len([a for a,k in calls if a[0]=='kill-pane'])==1
            with pytest.raises(B1Error,match='TARGET_RETIRED'):await targets.select(dict(session='Alpha'))
    asyncio.run(scenario())


def test_real_handler_cleanup_and_existing_pc_predicates(tmp_path,monkeypatch):
    async def scenario():
        async with cleanup_fixture(tmp_path,monkeypatch) as (runtime,people,client,operator,run,row,calls,tmux,processes,state):
            backend=Backend(operator.path,ROOT)
            with dashboard(operator,backend,monkeypatch,tmp_path) as (_,request):
                code,roster=await asyncio.to_thread(request,'/api/agents')
                a=next(a for a in roster['agents'] if a['name']=='Alpha')
                assert code==200 and a['category']=='finished' and a['attached'] is False
                page=(ROOT/'dashboard/index.html').read_text()
                start=page.index('function canKill(');end=page.index('\n',start)
                script=page[start:end]+'\nconst a='+json.dumps(a)+';if(!canKill(a)||a.running)process.exit(1);'
                checked=await asyncio.to_thread(subprocess.run,['/usr/local/bin/node','-e',script],capture_output=True,text=True,timeout=15)
                assert checked.returncode==0,checked.stderr
                state['attached']=True
                code,rejected=await asyncio.to_thread(request,'/api/kill',dict(session='Alpha'))
                assert code==409 and rejected['error_code']=='FRESH_CLEANUP_ATTACHED'
                assert 'Close the attached' in rejected['error']
                state['attached']=False
                code,result=await asyncio.to_thread(request,'/api/kill',dict(session='Alpha'))
                assert code==200 and result['ok']
    asyncio.run(scenario())


def test_display_batch_limits_sockets_and_snapshots_and_never_grants_cleanup(tmp_path,monkeypatch):
    async def scenario():
        from dataclasses import replace as selected_copy
        import time
        async with cleanup_fixture(tmp_path,monkeypatch) as (runtime,people,client,operator,run,row,calls,tmux,processes,state):
            target=Targets(operator,ROOT);selected=await target.select_managed(dict(session='Alpha'))
            observed=[];original=processes['process_snapshot']
            processes['process_snapshot']=lambda **kw:observed.append(kw) or original(**kw)
            chosen=[]
            for index in range(128):
                path=tmp_path/('batch-'+str(index%3)+'.sock');path.touch(exist_ok=True)
                chosen.append(selected_copy(selected,client=SimpleNamespace(context=dict(mail_agent_id=index+1)),
                    route=dict(row,socket_path=str(path))))
            facts=target.display_snapshot(chosen,deadline=time.monotonic()+1)
            assert len(observed)==2
            assert sum(a[0]=='list-panes' for a,k in calls)==2
            assert sum(a[0]=='list-clients' for a,k in calls)==2
            assert facts and all(v['category']=='finished' for v in facts.values())
            assert 3 not in facts  # Third socket remains explicitly unobserved.
            count=(len(observed),len(calls))
            assert target.display_snapshot(chosen,deadline=time.monotonic()-1)=={}
            assert count==(len(observed),len(calls))
            state['attached']=True
            with pytest.raises(B1Error,match='FRESH_CLEANUP_ATTACHED'):
                await target.cleanup(dict(session='Alpha'))
            assert not any(a[0]=='kill-pane' for a,k in calls)
    asyncio.run(scenario())


@pytest.mark.parametrize('damage',['deadline','malformed','socket_after','uid','ps_unavailable'])
def test_display_budget_and_failed_observations_stay_unknown(tmp_path,monkeypatch,damage):
    async def scenario():
        import agentstack_mail.fresh_targets as module
        import time
        async with cleanup_fixture(tmp_path,monkeypatch) as (runtime,people,client,operator,run,row,calls,tmux,processes,state):
            target=Targets(operator,ROOT);selected=await target.select_managed(dict(session='Alpha'))
            clock=[10.0];snapshots=[];original_snapshot=processes['process_snapshot']
            def snapshot(**kw):
                snapshots.append(kw)
                if damage=='ps_unavailable':return None
                value=original_snapshot(**kw)
                if damage=='uid':value[100]=(1,os.getuid()+1,'pane-birth')
                return value
            processes['process_snapshot']=snapshot
            original_command=tmux['Transport'].command
            def command(self,*args,**kw):
                result=original_command(self,*args,**kw)
                if args[0]=='list-panes':
                    if damage=='deadline':clock[0]+=.3
                    if damage=='malformed':result.stdout='unparseable row'
                return result
            monkeypatch.setattr(tmux['Transport'],'command',command)
            if damage=='socket_after':tmux['validate_socket']=lambda p:(33,44)
            monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
            started=time.monotonic();assert target.display_snapshot([selected],deadline=10.25)=={}
            elapsed=time.monotonic()-started
            assert len(snapshots)<=2 and len(calls)<=2
            if damage=='deadline':assert len(snapshots)==1 and len(calls)==1
            if damage=='ps_unavailable':assert not calls
            (tmp_path/'display-budget-proof.json').write_text(json.dumps(dict(synthetic_os=True,case=damage,
                snapshots=len(snapshots),tmux_commands=len(calls),elapsed_ms=elapsed*1000,unknown=True)))
    asyncio.run(scenario())


def test_borrowed_runtime_rechecks_target_without_closing_owner(tmp_path,monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime,people,client,_):
            await setup(client,tmp_path,monkeypatch)
            resident=Targets(client,ROOT,runtime=runtime)
            ordinary=Targets(client,ROOT)
            first=await resident.inspect({'session':'Alpha'})
            second=await ordinary.inspect({'session':'Alpha'})
            assert first == second
            assert (await resident.catalog())['agents'][0]['mail_agent_id']==people[0]['agent_id']
            assert (await runtime.core.health(runtime.config['instance_id']))['mail_instance_id']==runtime.config['instance_id']
    asyncio.run(run())


def test_real_handler_capture_history_jump_exit_are_current_and_finite(tmp_path,monkeypatch):
    async def run_test():
        async with live(tmp_path) as (runtime,people,parent,_):
            run,row,commands,_,_=await setup(parent,tmp_path,monkeypatch)
            receipt=_read_private(run/'session.json');transcript=Path(receipt['transcript_path'])
            binding=_read_private(Path(parent.context['binding_path']))
            transcript.write_text(json.dumps(dict(type='assistant',message=dict(role='assistant',content=[
                dict(type='text',text='Visible text '+binding['registration_token']+' '+str(parent.path)),
                dict(type='tool_use',name='private-tool',input=dict(secret='TOOL-PRIVATE')),
                dict(type='tool_result',content='TOOL-RESULT-PRIVATE')]),timestamp='fixture'))+'\n')
            before=await snapshot(runtime);backend=Backend(parent.path,ROOT)
            opened=[]
            with dashboard(parent,backend,monkeypatch,tmp_path) as (module,request):
                monkeypatch.setattr(module,'_open_terminal_tmux',lambda argv,title:opened.append((argv,title)) or dict(ok=True))
                code,term=await asyncio.to_thread(request,'/api/term?session=Alpha&lines=1')
                assert code==200 and term['content']=='❯ ' and term['mail_agent_id']==people[0]['agent_id']
                assert commands[-1][0][-1]=='-40'
                code,history=await asyncio.to_thread(request,'/api/history?session=Alpha&limit=2')
                assert code==200 and history['shown']==1 and history['events'][0]['text'].startswith('Visible text [private]')
                assert 'TOOL-' not in json.dumps(history) and str(parent.path) not in json.dumps(history)
                assert binding['registration_token'] not in json.dumps(history)
                code,focused=await asyncio.to_thread(request,'/api/jump',dict(session='Alpha',open=True))
                assert code==200 and focused['execution']=='active' and len(opened)==1
                assert opened[0][0][0]=='env' and 'target-json' in opened[0][0]
                assert not any(c[0][0]=='respawn-pane' for c in commands)
                code,exited=await asyncio.to_thread(request,'/api/exit',dict(session='Alpha'))
                assert code==200 and exited['execution']=='accepted'
                sent=sum(c[0][0]=='send-keys' for c in commands)
                code,repeated=await asyncio.to_thread(request,'/api/exit',dict(session='Alpha'))
                assert code==409 and repeated['execution']=='unknown'
                assert sum(c[0][0]=='send-keys' for c in commands)==sent==1
                for payload in (dict(session='Alpha',socket_path='/other'),dict(session='Alpha',open='true'),
                                dict(session='Alpha',mail_instance_id='0'*32,mail_agent_id=people[0]['agent_id'])):
                    code,error=await asyncio.to_thread(request,'/api/jump',payload)
                    assert code==409 and not error['ok']
                assert await snapshot(runtime)==before
                wire=json.dumps([term,history,focused,exited,repeated])
                assert not any(v in wire for v in ('registration_token','context_digest','binding_path','socket_path','transcript_path'))
    asyncio.run(run_test())


@pytest.mark.parametrize('changed',['current','native','generation','pane','socket','birth','owner'])
def test_changed_current_identity_or_os_facts_refuse_before_capture(tmp_path,monkeypatch,changed):
    async def run_test():
        async with live(tmp_path) as (runtime,people,parent,_):
            client=parent
            if changed=='generation':
                client=await FreshBoundClient.prepare(runtime.root/'config.json',people[1]['binding_path'],
                    parent.context['endpoint'],client_id=str(uuid4()))
            run,row,commands,tmux,processes=await setup(client,tmp_path,monkeypatch)
            targets=Targets(parent,ROOT)
            selected=await targets.select(dict(session='Beta' if changed=='generation' else 'Alpha'))
            if changed=='current':
                replace(run.parent.parent/'current.json',dict(launch_id=str(uuid4())))
            elif changed=='native':
                receipt=_read_private(run/'session.json');receipt['native_session_id']=str(uuid4());replace(run/'session.json',receipt)
            elif changed in {'generation','owner'}:
                actor=people[1] if changed=='generation' else people[0]
                await runtime.core.recover(actor['agent_id'],expected_generation=actor['credential_generation'],
                                           new_token='private-target-generation-recovery')
            elif changed=='pane':
                tmux['panes']=lambda *a,**kw:[dict(session_id='$1',session='Other',pane_id='%9',pid=100)]
            elif changed=='socket':tmux['Transport'].identity=(33,44)
            elif changed=='birth':processes['process_snapshot']=lambda **kw:{100:(1,os.getuid(),'reused')}
            with pytest.raises(B1Error):
                await targets.again(selected);targets.pane(selected)
            assert not any(c[0][0] in {'capture-pane','load-buffer','paste-buffer','send-keys','respawn-pane'} for c in commands)
    asyncio.run(run_test())


def test_ended_husk_capture_and_single_resume_are_distinct_from_delivery(tmp_path,monkeypatch):
    async def run_test():
        async with live(tmp_path) as (runtime,people,parent,_):
            run,row,commands,_,processes=await setup(parent,tmp_path,monkeypatch,ended=True)
            # Ended husk stays owned; dead native runner/provider are not a
            # reason to weaken the active notification target admission.
            start=processes['process_start'];processes['process_start']=lambda pid,**kw:start(pid) if pid==100 else 'absent'
            from agentstack_mail import fresh_delivery
            with pytest.raises(B1Error,match='ENDED'):fresh_delivery.target(parent,ROOT)
            targets=Targets(parent,ROOT)
            assert (await targets.capture(dict(session='Alpha')))['ok']
            _,binary=prepare_provider(parent,'claude',tmp_path)
            monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(binary))
            result=await targets.jump(dict(session='Alpha'),open_terminal=False)
            assert result['ok'] and result['execution']=='unknown' and not result['native_ready']
            resumed=[c for c in commands if c[0][0]=='respawn-pane']
            assert len(resumed)==1 and resumed[0][0][3]=='%2'
            assert '--session-id' in resumed[0][0][-1] and row['launch_id'] not in resumed[0][0][-1]
            again=await targets.jump(dict(session='Alpha'),open_terminal=False)
            assert not again['ok'] and len([c for c in commands if c[0][0]=='respawn-pane'])==1
            with pytest.raises(B1Error,match='ENDED'):await targets.exit(dict(session='Alpha'))
    asyncio.run(run_test())


def test_capture_rechecks_after_os_command_and_missing_pane_is_not_an_app_template(tmp_path,monkeypatch):
    async def run_test():
        async with live(tmp_path) as (_,_,parent,_):
            run,row,commands,tmux,_=await setup(parent,tmp_path,monkeypatch)
            original=tmux['Transport'].command
            def change(self,*args,**kwargs):
                done=original(self,*args,**kwargs)
                if args[0]=='capture-pane':replace(run.parent.parent/'current.json',dict(launch_id=str(uuid4())))
                return done
            monkeypatch.setattr(tmux['Transport'],'command',change)
            with pytest.raises(B1Error):await Targets(parent,ROOT).capture(dict(session='Alpha'))
            replace(run.parent.parent/'current.json',dict(launch_id=row['launch_id']))
            (run/'tmux.json').unlink()
            with pytest.raises(B1Error,match='HAS_NO_PANE'):await Targets(parent,ROOT).capture(dict(session='Alpha'))
    asyncio.run(run_test())


def test_resume_ack_during_native_startup_and_attach_require_same_saved_session(tmp_path,monkeypatch):
    async def run_test():
        from agentstack_mail import fresh_targets
        async with live(tmp_path) as (_,_,parent,_):
            old,row,commands,tmux,_=await setup(parent,tmp_path,monkeypatch,ended=True)
            targets=Targets(parent,ROOT);selected=await targets.select(dict(session='Alpha'))
            _,binary=prepare_provider(parent,'claude',tmp_path)
            monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(binary))
            launch_id=str(uuid4());new=old.parent/launch_id;new.mkdir(mode=0o700)
            intent=dict(selected.intent,launch_id=launch_id,resume_session_id=selected.receipt['native_session_id'])
            original=tmux['Transport'].command
            def start(self,*args,**kwargs):
                result=original(self,*args,**kwargs)
                if args[0]=='wait-for':
                    assert args[1]=='-S'
                    # Real startup publishes current before the native callback.
                    _write_private(new/'intent.json',intent)
                    replace(old.parent.parent/'current.json',dict(launch_id=launch_id))
                return result
            monkeypatch.setattr(tmux['Transport'],'command',start)
            response=await targets.jump(dict(session='Alpha'),open_terminal=False)
            assert response['ok'] and response['execution']=='unknown' and not response['native_ready']
            assert not (new/'session.json').exists()
            # Simulate the trusted native callback and route, not an OS launch.
            _write_private(new/'session.json',dict(selected.receipt,launch_id=launch_id))
            _write_private(new/'tmux.json',dict(row,launch_id=launch_id))
            request=dict(operation='attach-resumed',resume_from=old.name,expected=selected.signature,**selected.public())
            attached=[]
            class ExecReached(Exception):pass
            def attach(binary,argv):
                attached.append(argv);raise ExecReached
            monkeypatch.setattr(fresh_targets.os,'execv',attach)
            with pytest.raises(ExecReached):
                await fresh_targets.execute(SimpleNamespace(context=parent.path,wrapper_root=ROOT),request)
            assert len(attached)==1 and attached[0][-3:]==['attach-session','-t','$1']
            assert [c[0] for c in commands if c[0][0].startswith('select-')]==[
                ('select-window','-t','$1:.%2'),('select-pane','-t','%2')]
            replace(new/'intent.json',dict(intent,resume_session_id=str(uuid4())))
            with pytest.raises(B1Error,match='TARGET_CHANGED'):
                await fresh_targets.execute(SimpleNamespace(context=parent.path,wrapper_root=ROOT),request)
            assert len(attached)==1 and sum(c[0][0]=='respawn-pane' for c in commands)==1
    asyncio.run(run_test())


@pytest.mark.parametrize('failure',['none','dispatch-rc','dispatch-exception','signal-rc','signal-exception'])
def test_resume_orders_final_check_dispatch_lock_and_gate_without_retry(tmp_path,monkeypatch,failure):
    async def run_test():
        from agentstack_mail.fresh_launch import operation
        async with live(tmp_path) as (_,_,parent,_):
            run,row,commands,tmux,_=await setup(parent,tmp_path,monkeypatch,ended=True)
            targets=Targets(parent,ROOT);base=run.parent.parent
            _,binary=prepare_provider(parent,'claude',tmp_path)
            monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(binary))
            original_pane=targets.pane;attempts=[];order=[]
            def competing_writer():
                with pytest.raises(B1Error,match='FRESH_LAUNCH_BUSY'):
                    with operation(base):
                        # A competing regular writer must not reach publication.
                        replace(base/'current.json',dict(launch_id=str(uuid4())))
                attempts.append('blocked')
            def final_pane(selected,**kwargs):
                result=original_pane(selected,**kwargs)
                if (run/'pc-resume.json').exists() and not order:competing_writer()
                return result
            monkeypatch.setattr(targets,'pane',final_pane)
            original=tmux['Transport'].command
            def command(self,*args,**kwargs):
                result=original(self,*args,**kwargs)
                if args[0]=='respawn-pane':
                    competing_writer();order.append('dispatch-locked')
                    assert _read_private(base/'current.json')==dict(launch_id=row['launch_id'])
                    gate=_read_private(run/'pc-resume.json')['gate']
                    assert shlex.split(args[-1])[:8]==[
                        '/fixture/tmux','-S','/private/tmp/fixture-only.sock','wait-for',gate,'&&','exec','env']
                    assert shlex.split(args[-1])[-2:]==['--session-id',_read_private(run/'session.json')['native_session_id']]
                    if failure=='dispatch-exception':raise RuntimeError('fixture lost command ACK')
                    if failure=='dispatch-rc':return subprocess.CompletedProcess(args,1,'','')
                if args[0]=='wait-for':
                    assert args==('wait-for','-S',_read_private(run/'pc-resume.json')['gate'])
                    # Same nonblocking lock is now available to the child.
                    with operation(base):order.append('signal-unlocked')
                    if failure=='signal-exception':raise RuntimeError('fixture lost gate ACK')
                    if failure=='signal-rc':return subprocess.CompletedProcess(args,1,'','')
                return result
            monkeypatch.setattr(tmux['Transport'],'command',command)
            if failure=='none':
                response=await targets.jump(dict(session='Alpha'),open_terminal=False)
                assert response['ok'] and response['execution']=='unknown' and not response['native_ready']
            else:
                with pytest.raises(B1Error,match='FRESH_RESUME_OUTCOME_UNKNOWN'):
                    await targets.jump(dict(session='Alpha'),open_terminal=False)
            assert len(attempts)==2 and order[0]=='dispatch-locked'
            assert order==(['dispatch-locked'] if failure.startswith('dispatch-') else ['dispatch-locked','signal-unlocked'])
            marker=_read_private(run/'pc-resume.json');assert marker['execution']=='unknown'
            before=len(commands)
            response=await targets.jump(dict(session='Alpha'),open_terminal=False)
            assert not response['ok'] and response['execution']=='unknown'
            assert len(commands)==before and sum(c[0][0]=='respawn-pane' for c in commands)==1
    asyncio.run(run_test())


@pytest.mark.parametrize('damage',['none','window-rc','pane-rc','current','birth'])
def test_attach_focuses_selected_pane_window_and_refuses_failed_or_changed_target(tmp_path,monkeypatch,damage):
    async def run_test():
        from agentstack_mail import fresh_targets
        async with live(tmp_path) as (_,_,parent,_):
            run,_,commands,tmux,processes=await setup(parent,tmp_path,monkeypatch)
            targets=Targets(parent,ROOT);selected=await targets.select(dict(session='Alpha'))
            # Another agent's window/pane starts active in the same session.
            active=dict(window='@9',pane='%9');order=[]
            original=tmux['Transport'].command
            def command(self,*args,**kwargs):
                result=original(self,*args,**kwargs)
                if args[0]=='select-window':
                    assert args==('select-window','-t','$1:.%2')
                    order.append('window')
                    if damage=='window-rc':return subprocess.CompletedProcess(args,1,'','')
                    active['window']='@2'
                    if damage=='current':replace(run.parent.parent/'current.json',dict(launch_id=str(uuid4())))
                if args[0]=='select-pane':
                    assert args==('select-pane','-t','%2')
                    order.append('pane')
                    if damage=='pane-rc':return subprocess.CompletedProcess(args,1,'','')
                    active['pane']='%2'
                    if damage=='birth':
                        processes['process_start']=lambda *a,**kw:'reused'
                        processes['process_snapshot']=lambda **kw:{100:(1,os.getuid(),'reused')}
                return result
            monkeypatch.setattr(tmux['Transport'],'command',command)
            class ExecReached(Exception):pass
            attached=[]
            def attach(binary,argv):
                assert active==dict(window='@2',pane='%2')
                assert argv==['/fixture/tmux','-S','/private/tmp/fixture-only.sock','attach-session','-t','$1']
                attached.append(argv);order.append('attach');raise ExecReached
            monkeypatch.setattr(fresh_targets.os,'execv',attach)
            request=dict(operation='attach',expected=selected.signature,**selected.public())
            if damage=='none':
                with pytest.raises(ExecReached):
                    await fresh_targets.execute(SimpleNamespace(context=parent.path,wrapper_root=ROOT),request)
                assert order==['window','pane','attach'] and len(attached)==1
            else:
                error='FOCUS_FAILED' if damage.endswith('-rc') else 'TARGET_UNAVAILABLE' if damage=='current' else 'TARGET_CHANGED'
                with pytest.raises(B1Error,match=error):
                    await fresh_targets.execute(SimpleNamespace(context=parent.path,wrapper_root=ROOT),request)
                assert not attached and 'attach' not in order
                if damage in {'window-rc','current'}:assert order==['window']
    asyncio.run(run_test())


def test_target_names_count_retired_candidates_and_explicit_ids_keep_owner(tmp_path,monkeypatch):
    async def run_test():
        async with live(tmp_path) as (runtime,people,parent,_):
            await setup(parent,tmp_path,monkeypatch)
            alias=await runtime.register_local(request_id=str(uuid4()),name='ALPHA',program='fixture',model='fixture')
            await runtime.core.retire(alias['agent_id'])
            targets=Targets(parent,ROOT);before=await snapshot(runtime)
            with pytest.raises(B1Error,match='TARGET_NAME_AMBIGUOUS'):
                await targets.capture(dict(session='Alpha'))
            selected=await targets.select(dict(mail_instance_id=runtime.config['instance_id'],mail_agent_id=people[0]['agent_id']))
            assert selected.public()['mail_agent_id']==people[0]['agent_id']
            with pytest.raises(B1Error,match='TARGET_RETIRED'):
                await targets.select(dict(mail_instance_id=runtime.config['instance_id'],mail_agent_id=alias['agent_id']))
            assert await snapshot(runtime)==before
    asyncio.run(run_test())


def test_host_wrapper_is_explicit_fresh_and_not_a_model_tool(tmp_path,monkeypatch):
    from agentstack_mail.fresh_proxy import PUBLISHED
    assert {'target-json','inspect','capture','jump','exit'}.isdisjoint(PUBLISHED)
    # A missing explicit context is refused by the real wrapper, before Python.
    env=dict(os.environ,AGENTSTACK_RUNTIME_MODE='fresh')
    env.pop('AGENTSTACK_FRESH_CLIENT_CONTEXT',None)
    result=subprocess.run(['/bin/bash',str(ROOT/'bin/agentstack-runtime-client'),'target-json'],
                          input='{}',text=True,capture_output=True,env=env,cwd=tmp_path,timeout=10)
    assert result.returncode==2 and result.stderr.strip()=='FRESH_CONTEXT_REQUIRED'

    async def run_test():
        async with live(tmp_path) as (_,_,parent,_):
            env.update(AGENTSTACK_FRESH_CLIENT_CONTEXT=str(parent.path),AGENTSTACK_PYTHON=sys.executable)
            for query,error in [(dict(operation='inspect',session='Alpha',endpoint='http://evil.invalid'),'FRESH_TARGET_QUERY_INVALID'),
                                (dict(operation='inspect',session='Alpha'),'FRESH_TARGET_UNAVAILABLE')]:
                done=await asyncio.to_thread(subprocess.run,
                    ['/bin/bash',str(ROOT/'bin/agentstack-runtime-client'),'target-json'],input=json.dumps(query),
                    text=True,capture_output=True,env=env,cwd=tmp_path,timeout=30)
                assert done.returncode==2 and json.loads(done.stdout)['error']==error
                assert 'registration_token' not in done.stdout
    asyncio.run(run_test())


def test_catalog_and_use_specific_inspection_keep_current_owner_and_bounded_public_rows(tmp_path,monkeypatch):
    async def run_test():
        from agentstack_mail.fresh_targets import execute
        async with live(tmp_path) as (runtime,people,parent,_):
            await setup(parent,tmp_path,monkeypatch)
            args=SimpleNamespace(context=parent.path,wrapper_root=ROOT)
            catalog=await execute(args,dict(operation='catalog'))
            assert set(catalog)=={'ok','mail_instance_id','agents','truncated'} and len(catalog['agents'])==3
            assert not catalog['truncated'] and catalog['agents'][0]==dict(session='Alpha',
                mail_instance_id=runtime.config['instance_id'],mail_agent_id=people[0]['agent_id'])
            assert all(set(row)=={'session','mail_instance_id','mail_agent_id'} for row in catalog['agents'])
            proof=await execute(args,dict(operation='inspect',session='Alpha',purpose='input'))
            assert (await execute(args,dict(operation='inspect',session='Alpha',purpose='capture',expected=proof['revision'])))==proof
            for payload,error in [(dict(operation='inspect',session='Alpha',purpose='kill'),'QUERY_INVALID'),
                    (dict(operation='inspect',session='Alpha',expected='0'*64),'TARGET_CHANGED'),
                    (dict(operation='catalog',session='Alpha'),'QUERY_INVALID')]:
                with pytest.raises(B1Error,match=error):await execute(args,payload)
            await runtime.core.retire(people[2]['agent_id'])
            assert len((await execute(args,dict(operation='catalog')))['agents'])==2
            await runtime.core.recover(people[0]['agent_id'],expected_generation=people[0]['credential_generation'],
                                       new_token='private-catalog-recovery')
            with pytest.raises(B1Error):await execute(args,dict(operation='catalog'))
    asyncio.run(run_test())

