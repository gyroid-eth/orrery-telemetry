"""Closure truth, private records, refusal races, and ordinary fresh gates.

OS observations here are injected adapter fixtures, never host acceptance.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentstack_mail import fresh_delivery as delivery, fresh_end as end
from agentstack_mail import fresh_launch as launch
from agentstack_mail.fresh_runtime import _write_private, _read_private
from agentstack_mail.storage_origin_contract import B1Error, digest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def closed_candidate(tmp_path, monkeypatch):
    parent = tmp_path / 'client';parent.mkdir(mode=0o700)
    client = SimpleNamespace(path=parent/'context.json', context_digest='fixture-context')
    client.context = dict(mail_instance_id='fixture-instance',mail_agent_id=7,credential_generation=2)
    client._load = lambda: (client.context, None)
    async def call(name, args):
        assert name == 'reconnect' and args == {}
        return dict(client.context, agent_name='Fixture')
    client.call = call
    base = launch.layout(client);lid = str(uuid4())
    run = base/'runs'/lid;run.mkdir(mode=0o700)
    intent = dict(kind='orrery-fresh-launch-v1',launch_id=lid,provider='claude',
        context_digest=client.context_digest,nonce='fixture-nonce',resume_session_id=None,
        wrapper_root=str(ROOT),cwd=str(tmp_path),**launch.identity(client))
    _write_private(run/'intent.json',intent);_write_private(base/'current.json',dict(launch_id=lid))
    route = dict(kind='orrery-fresh-tmux-route-v1',launch_id=lid,context_digest=client.context_digest,
        socket_path=str(tmp_path/'fixture.sock'),socket_identity=[11,22],
        pane=dict(session_id='$1',pane_id='%2',pid=100),pane_process_start='pane-birth',
        runner=dict(pid=101,process_start='runner-birth'),provider=dict(pid=102,process_start='provider-birth'),
        process_observation=dict(kind=end.GROUPS_KIND,groups=[dict(pgid=101,leader_start='runner-birth')],
            members=[dict(pid=101,process_start='runner-birth'),dict(pid=102,process_start='provider-birth'),
                     dict(pid=100,process_start='pane-birth')]),**launch.identity(client))
    _write_private(run/'tmux.json',route)
    state = dict(rows={900:(1,os.getuid(),'unrelated-birth',900)},starts={},panes='',commands=[])
    class Refused(RuntimeError):pass
    class Transport:
        identity=(11,22)
        def __init__(self,path):self.path=Path(path)
        def command(self,*args,**kwargs):
            state['commands'].append(args)
            assert args[0]=='list-panes'
            return subprocess.CompletedProcess(args,state.get('tmux_rc',0),state['panes'],'')
    api = dict(process_snapshot=lambda **_:state['rows'],
        process_start=lambda pid,**_:state['starts'].get(pid,'absent'))
    tmux = dict(TargetError=Refused,Transport=Transport,validate_socket=lambda _:tuple(state.get('socket_identity',[11,22])))
    monkeypatch.setattr(delivery,'helpers',lambda _: (tmux,api,{}))
    monkeypatch.setenv('AGENTSTACK_FRESH_LAUNCH_ID',lid)
    monkeypatch.setenv('AGENTSTACK_FRESH_LAUNCH_NONCE','fixture-nonce')
    return client,run,intent,route,state,api,tmux


def replace(path, value):
    # Fixture mutation, not a supported product recovery operation.
    path.write_text(json.dumps(value));path.chmod(0o600)


def close(f):
    client,run,*_=f
    return end.resolve_ended(client,ROOT,run.name,confirm_ended=True)


def bytes_before(run):
    return {p.name:p.read_bytes() for p in run.iterdir() if p.is_file()}


def test_missing_session_resolves_without_invented_exit_and_is_idempotent(closed_candidate):
    client,run,intent,route,state,*_=closed_candidate
    before=bytes_before(run);current=(run.parent.parent/'current.json').read_bytes()
    assert close(closed_candidate)==dict(ok=True,launch_id=run.name,end_kind='resolved',provider_exit_code=None)
    first=(run/'resolved-ended.json').read_bytes();record=end.read_run_end(client,run)
    assert record.kind=='resolved' and record.exit_code is None
    assert record.record['reason']=='operator-confirmed-ended'
    assert record.record['intent_digest']==digest(intent)
    assert not (run/'ended.json').exists() and not (run/'session.json').exists()
    assert (run/'resolved-ended.json').stat().st_mode & 0o777==0o600
    assert all((run/name).read_bytes()==raw for name,raw in before.items())
    assert (run.parent.parent/'current.json').read_bytes()==current
    assert close(closed_candidate)['end_kind']=='resolved' and (run/'resolved-ended.json').read_bytes()==first
    assert not state['commands']
    assert not any(word in first.decode() for word in ('fixture-nonce','argv','TOKEN','password'))


@pytest.mark.parametrize('code',[0,23,-15,143])
def test_original_normal_format_remains_distinct(closed_candidate,code):
    client,run,*_=closed_candidate
    _write_private(run/'ended.json',dict(launch_id=run.name,exit_code=code))
    result=end.read_run_end(client,run)
    assert result.kind=='normal' and result.exit_code==code
    assert close(closed_candidate)['provider_exit_code']==code
    assert not (run/'resolved-ended.json').exists()


@pytest.mark.parametrize('damage', ['runner','provider','pane','group-child','escaped-member','pid-reuse',
    'snapshot-none','birth-none','snapshot-race','group-missing','group-corrupt','wrong-run','wrong-context',
    'wrong-I','wrong-A','wrong-generation','wrong-intent-id','wrong-route-id','wrong-root','symlink','unsafe',
    'broken','run-symlink','busy','not-confirmed'])
def test_refusals_leave_current_and_proofs_unchanged(closed_candidate,damage,tmp_path):
    client,run,intent,route,state,api,_=closed_candidate
    if damage in {'runner','provider','pane','pid-reuse','snapshot-race'}:
        role = route.get(damage,route['provider']);pid=role['pid']
        birth=route['pane_process_start'] if damage=='pane' else role['process_start']
        state['rows'][pid]=(1,os.getuid(),birth,101)
        state['starts'][pid]= 'replacement-birth' if damage=='pid-reuse' else birth
        if damage=='snapshot-race':state['rows'].pop(pid)
    elif damage=='group-child':state['rows'][109]=(1,os.getuid(),'child-birth',101)
    elif damage=='escaped-member':
        route['process_observation']['members'].append(dict(pid=109,process_start='child-birth'))
        replace(run/'tmux.json',route);state['rows'][109]=(1,os.getuid(),'child-birth',109);state['starts'][109]='child-birth'
    elif damage=='snapshot-none':state['rows']=None
    elif damage=='birth-none':state['starts'][102]=None
    elif damage=='group-missing':route.pop('process_observation');replace(run/'tmux.json',route)
    elif damage=='group-corrupt':route['process_observation']['groups'][0]['leader_start']='absent';replace(run/'tmux.json',route)
    elif damage=='wrong-run':replace(run.parent.parent/'current.json',dict(launch_id=str(uuid4())))
    elif damage=='wrong-context':intent['context_digest']='other';replace(run/'intent.json',intent)
    elif damage in {'wrong-I','wrong-A','wrong-generation'}:
        key={'wrong-I':'mail_instance_id','wrong-A':'mail_agent_id','wrong-generation':'credential_generation'}[damage]
        intent[key]='wrong';replace(run/'intent.json',intent)
    elif damage=='wrong-intent-id':intent['launch_id']=str(uuid4());replace(run/'intent.json',intent)
    elif damage=='wrong-route-id':route['launch_id']=str(uuid4());replace(run/'tmux.json',route)
    elif damage=='wrong-root':intent['wrapper_root']=str(tmp_path);replace(run/'intent.json',intent)
    elif damage=='symlink':
        original=run/'tmux.json';original.rename(run/'saved.json');original.symlink_to(run/'saved.json')
    elif damage=='unsafe':(run/'tmux.json').chmod(0o644)
    elif damage=='broken':(run/'tmux.json').write_text('{broken')
    elif damage=='run-symlink':
        original=run;other=tmp_path/'other-run';original.rename(other);original.symlink_to(other,target_is_directory=True)
    before=bytes_before(run);current=(run.parent.parent/'current.json').read_bytes()
    with pytest.raises(B1Error):
        if damage=='busy':
            with launch.operation(run.parent.parent):close(closed_candidate)
        elif damage=='not-confirmed':end.resolve_ended(client,ROOT,run.name)
        else:close(closed_candidate)
    assert not (run/'resolved-ended.json').exists()
    assert bytes_before(run)==before and (run.parent.parent/'current.json').read_bytes()==current


@pytest.mark.parametrize('damage',['reused-pane','alive-pane','empty-alive','empty-dead','command-error','socket-changed','malformed','dead-pane'])
def test_pinned_tmux_inventory_never_searches_or_mutates(closed_candidate,damage):
    client,run,intent,route,state,*_=closed_candidate
    Path(route['socket_path']).touch(mode=0o600)
    state['panes']='$1\t%2\t100\t1\n'
    if damage=='reused-pane':state['panes']='$3\t%2\t300\t1\n'
    if damage=='alive-pane':state['panes']='$1\t%2\t100\t0\n'
    if damage=='empty-alive':state['panes']='$1\t%2\t\t0\n'
    if damage=='empty-dead':state['panes']='$1\t%2\t\t1\n'
    if damage=='command-error':state['tmux_rc']=1
    if damage=='socket-changed':state['socket_identity']=[11,23]
    if damage=='malformed':state['panes']='unrecognised format'
    if damage in {'dead-pane','empty-dead'}:assert close(closed_candidate)['end_kind']=='resolved'
    else:
        with pytest.raises(B1Error):close(closed_candidate)
        assert not (run/'resolved-ended.json').exists()
    assert all(args[0]=='list-panes' for args in state['commands'])


@pytest.mark.parametrize('phase',['current','intent','route','alive','snapshot','credential','atomic'])
def test_final_recheck_and_publication_failure_keep_run_unknown(closed_candidate,monkeypatch,phase):
    client,run,intent,route,state,api,_=closed_candidate
    original=api['process_snapshot'];calls=[]
    def snapshot(**kwargs):
        calls.append(True)
        if len(calls)==1:
            if phase=='current':replace(run.parent.parent/'current.json',dict(launch_id=str(uuid4())))
            if phase=='intent':replace(run/'intent.json',dict(intent,nonce='changed'))
            if phase=='route':replace(run/'tmux.json',dict(route,socket_identity=[2,3]))
        if len(calls)==2:
            if phase=='alive':state['rows'][102]=(1,os.getuid(),'provider-birth',101);state['starts'][102]='provider-birth'
            if phase=='snapshot':return None
        return original(**kwargs)
    api['process_snapshot']=snapshot
    if phase=='credential':
        async def reconnect(*_):raise B1Error('OWNER_CREDENTIAL_REVOKED')
        client.call=reconnect
    if phase=='atomic':
        def fail(*_):raise OSError('synthetic publication failure')
        monkeypatch.setattr(end,'_write_private',fail)
    with pytest.raises((B1Error,OSError)):close(closed_candidate)
    assert not (run/'ended.json').exists() and not (run/'resolved-ended.json').exists()


@pytest.mark.parametrize('damage',['exit-code','reason','intent-digest','context','generation','route','observation','both'])
def test_corrupt_or_rebound_resolution_is_not_an_end(closed_candidate,damage):
    client,run,intent,route,*_=closed_candidate
    close(closed_candidate);value=_read_private(run/'resolved-ended.json')
    if damage=='exit-code':value['provider_exit_code']=0
    if damage=='reason':value['reason']='force'
    if damage=='intent-digest':value['intent_digest']='wrong'
    if damage=='context':value['context_digest']='other'
    if damage=='generation':value['credential_generation']=3
    if damage=='route':route['provider']['pid']=10000;replace(run/'tmux.json',route)
    if damage=='observation':value['observation']['processes']=[]
    if damage=='both':_write_private(run/'ended.json',dict(launch_id=run.name,exit_code=0))
    replace(run/'resolved-ended.json',value)
    with pytest.raises(B1Error):end.read_run_end(client,run)


def test_resolved_run_is_excluded_before_session_read_by_routes_and_session_gate(closed_candidate):
    client,run,*_=closed_candidate
    close(closed_candidate)
    with pytest.raises(B1Error,match='FRESH_TMUX_TARGET_ENDED'):delivery.route(client)
    with pytest.raises(B1Error,match='FRESH_LAUNCH_RECEIPT_MISMATCH'):
        launch.session_state(client,dict(session_id=str(uuid4())))
    with pytest.raises(B1Error,match='FRESH_LAUNCH_RECEIPT_MISMATCH'):
        asyncio.run(launch.complete_pending_session(client,dict(session_id=str(uuid4()))))


def test_new_group_record_preserves_strict_roles_and_leader_birth(closed_candidate):
    _,_,_,route,state,api,_=closed_candidate
    state['rows']={100:(1,os.getuid(),'pane-birth',100),101:(100,os.getuid(),'runner-birth',101),
                   102:(101,os.getuid(),'provider-birth',101)}
    value=end.capture_groups(api,route)
    assert value['kind']==end.GROUPS_KIND
    assert value['groups']==[dict(pgid=101,leader_start='runner-birth')]
    assert set(route['runner'])==set(route['provider'])=={'pid','process_start'}
    assert value['members']==route['process_observation']['members']


@pytest.mark.parametrize('platform',['darwin','linux'])
def test_process_adapter_group_and_birth_fixtures(monkeypatch,platform):
    api=runpy.run_path(str(ROOT/'bin/lib/process_observation.py'))
    output=f'101 100 {os.getuid()} 101 Sun Nov 15 12:00:00 2026\n'
    monkeypatch.setattr(sys,'platform',platform)
    def run(argv,**kwargs):
        assert kwargs['env']['LC_ALL']=='C'
        if '-axo' in argv:
            assert argv[-1]=='pid=,ppid=,uid=,pgid=,lstart='
            return subprocess.CompletedProcess(argv,0,output,'')
        return subprocess.CompletedProcess(argv,1,'','')
    monkeypatch.setattr(subprocess,'run',run)
    assert api['process_snapshot'](include_groups=True)=={101:(100,os.getuid(),'Sun Nov 15 12:00:00 2026',101)}
    assert api['process_start'](101)=='absent'


def test_cli_dispatch_requires_confirmation_and_emits_typed_result(closed_candidate,monkeypatch,capsys):
    from agentstack_mail import fresh_client
    client,run,*_=closed_candidate
    monkeypatch.setattr(fresh_client,'FreshBoundClient',lambda _:client)
    args=['resolve-ended','--context',str(client.path),'--wrapper-root',str(ROOT),'--launch-id',run.name]
    with pytest.raises(SystemExit):fresh_client.main(args)
    assert not (run/'resolved-ended.json').exists()
    assert fresh_client.main([*args,'--confirm-ended'])==0
    assert json.loads(capsys.readouterr().out)['provider_exit_code'] is None


def actual_candidate(client, fixture):
    _,old,intent,route,state,api,tmux=fixture
    base=launch.layout(client);run=base/'runs'/old.name;run.mkdir(mode=0o700)
    intent=dict(intent,context_digest=client.context_digest,**launch.identity(client))
    route=dict(route,context_digest=client.context_digest,**launch.identity(client))
    _write_private(run/'intent.json',intent);_write_private(run/'tmux.json',route)
    _write_private(base/'current.json',dict(launch_id=run.name))
    return run,intent,route


def test_real_bound_owner_resolution_skips_watcher_and_rejects_all_callbacks(tmp_path,monkeypatch,closed_candidate):
    from test_fresh_guard import live
    from agentstack_mail import fresh_notifications, fresh_persistent, fresh_harness
    async def check():
        async with live(tmp_path) as (runtime,people,client,_):
            run,intent,route=actual_candidate(client,closed_candidate)
            intent['persistent']=True;replace(run/'intent.json',intent)
            result=await asyncio.to_thread(end.resolve_ended,client,ROOT,run.name,confirm_ended=True)
            assert result['end_kind']=='resolved' and result['provider_exit_code'] is None
            assert await delivery.watch_once(runtime.root/'config.json',ROOT)==dict(ok=True,routes=[])
            event=dict(hook_event_name='SessionStart',session_id=str(uuid4()))
            monkeypatch.setenv('AGENTSTACK_RUNTIME_MODE','fresh')
            monkeypatch.setenv('AGENTSTACK_FRESH_CLIENT_CONTEXT',str(client.path))
            for callback in (lambda:fresh_notifications.current_hook(client,event),
                             lambda:launch.session_state(client,event)):
                with pytest.raises(B1Error,match='FRESH_LAUNCH_RECEIPT_MISMATCH'):callback()
            before=bytes_before(run)
            fresh_persistent.request_restart(client,event)
            with pytest.raises(B1Error,match='FRESH_LAUNCH_RECEIPT_MISMATCH'):
                await fresh_harness.hook(client,'post-edit',event)
            assert bytes_before(run)==before and not (run/'restart-required.json').exists()
            from agentstack_mail.fresh_targets import Targets
            targets=Targets(client,ROOT,runtime=runtime)
            with pytest.raises(B1Error):await targets.select(dict(session='Alpha'))
            assert not (run/'session.json').exists() and not list((run.parent.parent/'sessions').iterdir())
            from dashboard.fresh_backend import Backend
            roster=await Backend(client.path,ROOT).roster()
            row=next(r for r in roster['agents'] if r['mail_agent_id']==client.context['mail_agent_id'])
            # Lifecycle closure is not a fresh OS liveness observation.
            assert row['state']=='finished' and row['running'] is not True and not row.get('native_ready')
    asyncio.run(check())


def test_real_credential_revocation_cannot_close(tmp_path,closed_candidate):
    from test_fresh_guard import live
    async def check():
        async with live(tmp_path) as (runtime,people,client,_):
            run,_,_=actual_candidate(client,closed_candidate)
            await runtime.core.recover(people[0]['agent_id'],expected_generation=people[0]['credential_generation'],
                new_token='revoked-fixture-only-token')
            with pytest.raises(B1Error):await asyncio.to_thread(end.resolve_ended,client,ROOT,run.name,confirm_ended=True)
            assert not (run/'resolved-ended.json').exists()
    asyncio.run(check())


@pytest.mark.parametrize('provider',['claude','codex'])
def test_child_explicit_launch_after_resolution_keeps_one_shot_exec_and_original_task(tmp_path,monkeypatch,closed_candidate,provider):
    from test_fresh_child import binary_for_child,prepared_task
    from test_fresh_guard import live
    from test_fresh_proxy import environment
    from agentstack_mail.fresh_child import handoff,family,status
    import shlex
    async def check():
        async with live(tmp_path) as (_,_,parent,_):
            home,binary=binary_for_child(parent,provider,tmp_path)
            monkeypatch.setenv('AGENTSTACK_'+provider.upper()+'_BIN',str(binary))
            child,work,task,_=await prepared_task(parent,binary,provider,tmp_path)
            class Transport:
                identity=(1,1);path=tmp_path/'child-fixture.sock';calls=[]
                def command(self,*args):
                    self.calls.append(args);return subprocess.CompletedProcess(args,0,'','')
            transport=Transport()
            await handoff(parent,ROOT,slot='worker',cwd=work,task=task.read_text(),resources=['note.py'],transport=transport)
            argv=shlex.split(transport.calls[0][-1])
            env=dict(os.environ);env.pop('TMUX',None);env.pop('TMUX_PANE',None)
            first=await asyncio.to_thread(subprocess.run,argv,env=env,cwd=work,stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=60)
            assert first.returncode==0,first.stderr
            _,family_base,_=family(child)
            preserved={p:p.read_bytes() for p in (family_base/'exec.json',family_base/'handoff.json',family_base/'task.json')}
            base=child.path.parent/'launch';lid=_read_private(base/'current.json')['launch_id'];run=base/'runs'/lid
            # Rehearse launcher-loss recovery without editing product records by a CLI.
            # The fixture alone removes the known wait receipt from consideration.
            (run/'ended.json').rename(run/'fixture-original-ended.json')
            route=dict(closed_candidate[3],launch_id=lid,context_digest=child.context_digest,**launch.identity(child))
            _write_private(run/'tmux.json',route)
            before=bytes_before(run)
            result=await asyncio.to_thread(end.resolve_ended,child,ROOT,lid,confirm_ended=True)
            assert result['end_kind']=='resolved'
            closed=status(child,family_base)
            assert closed['execution']=='resolved-ended' and not closed['native_ready'] and closed['provider_exit_code'] is None
            duplicate=await asyncio.to_thread(subprocess.run,argv,env=env,cwd=work,stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=20)
            assert duplicate.returncode==2 and 'FRESH_CHILD_LAUNCH_OUTCOME_UNKNOWN' in duplicate.stderr
            ordinary=environment(child,**{'AGENTSTACK_'+provider.upper()+'_BIN':str(binary)})
            ordinary.pop('TMUX',None);ordinary.pop('TMUX_PANE',None)
            again=await asyncio.to_thread(subprocess.run,['/bin/bash',str(ROOT/'bin/agentstack-runtime-client'),
                'launch',provider,str(work)],env=ordinary,cwd=work,stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=60)
            assert again.returncode==0,again.stderr
            new_id=_read_private(base/'current.json')['launch_id'];assert new_id!=lid
            assert _read_private(base/'runs'/new_id/'intent.json')['child_spawn_id']==_read_private(family_base/'task.json')['spawn_id']
            assert task.read_text() in json.loads((tmp_path/'child-native.json').read_text())['args'][-1]
            assert all(p.read_bytes()==raw for p,raw in preserved.items())
            assert all((run/name).read_bytes()==raw for name,raw in before.items())
            assert len(transport.calls)==1
    asyncio.run(check())


@pytest.mark.parametrize('code,observation,remaining',[(23,True,False),(0,True,False),
    (23,True,True),(0,True,True),(23,False,False),(23,True,None)])
def test_wait_exit_status_needs_complete_child_observation(tmp_path,monkeypatch,code,observation,remaining):
    from agentstack_mail import fresh_persistent as persistent
    class Process:
        pid=999
        def poll(self):return code
        def wait(self,**kwargs):return code
    monkeypatch.setattr(os,'getpgrp',os.getpid)
    def snapshot(**_):
        if remaining is None:return None
        if remaining:return {1000:(999,os.getuid(),'child-birth',os.getpid())}
        return {}
    monkeypatch.setattr(persistent.runpy,'run_path',lambda _:{'process_snapshot':snapshot,
        'snapshot_belongs_to':lambda *args:True})
    if observation and remaining is False:
        assert persistent.wait_provider(Process(),tmp_path,wrapper_root=ROOT)==(code,False)
    else:
        with pytest.raises(B1Error,match='FRESH_LAUNCH_OUTCOME_UNKNOWN'):
            persistent.wait_provider(Process(),tmp_path,wrapper_root=ROOT if observation else None)


@pytest.mark.parametrize('stage',['snapshot','birth','deadline'])
def test_observation_budget_failure_is_a_fixed_hold(closed_candidate,stage,monkeypatch):
    _,_,_,route,_,api,_=closed_candidate
    def fail(*args,**kwargs):raise RuntimeError('OBSERVATION_INCOMPLETE')
    api['process_snapshot' if stage=='snapshot' else 'process_start']=fail
    import time
    with pytest.raises(B1Error,match='FRESH_RESOLVE_OBSERVATION_UNAVAILABLE'):
        end.observe_ended(route,ROOT,deadline=time.monotonic()+(-1 if stage=='deadline' else 10))


@pytest.mark.parametrize('persistent,code',[(False,0),(True,0),(True,23)])
def test_waited_run_is_closed_before_missing_native_receipt_is_reported(closed_candidate,monkeypatch,persistent,code,tmp_path):
    from agentstack_mail import fresh_persistent
    client,old,*_=closed_candidate
    close(closed_candidate);before=bytes_before(old)
    monkeypatch.setattr(launch,'provider_setup',lambda *args,**kwargs:(tmp_path,'fake-native',None))
    monkeypatch.setattr(launch,'command',lambda *args,**kwargs:['fake-native'])
    monkeypatch.setattr(delivery,'capture',lambda *args:None)
    monkeypatch.setattr(delivery,'publish',lambda *args:None)
    class Process:
        def __init__(self,*args,**kwargs):pass
        def wait(self):return code
        def poll(self):return code
    monkeypatch.setattr(subprocess,'Popen',Process)
    # Child-observation semantics have their own tests; this verifies the
    # launch publication order after wait_provider has admitted termination.
    monkeypatch.setattr(fresh_persistent,'wait_provider',lambda *args,**kwargs:(code,False))
    options=dict(provider='claude',channels=['plugin:fixture']) if persistent else None
    if code==0:
        with pytest.raises(B1Error,match='FRESH_PRIVATE_FILE_UNAVAILABLE'):
            launch.launch(client,ROOT,'claude',tmp_path,persistent=options)
    else:assert launch.launch(client,ROOT,'claude',tmp_path,persistent=options)==23
    new_id=_read_private(old.parent.parent/'current.json')['launch_id'];assert new_id!=old.name
    current=old.parent/new_id
    outcome=end.read_run_end(client,current)
    assert outcome.kind=='normal' and outcome.exit_code==code
    assert not (current/'session.json').exists()
    assert all((old/name).read_bytes()==raw for name,raw in before.items())


@pytest.mark.parametrize('field,value',[('provider',None),('provider','other'),('cwd',None),
    ('cwd','relative'),('nonce',None),('resume_session_id',23),('kind','other')])
def test_incomplete_or_invalid_intent_is_not_resolvable(closed_candidate,field,value):
    _,run,intent,*_=closed_candidate
    if value is None:intent.pop(field)
    else:intent[field]=value
    replace(run/'intent.json',intent)
    with pytest.raises(B1Error,match='FRESH_LAUNCH_BINDING_MISMATCH'):close(closed_candidate)
    assert not (run/'resolved-ended.json').exists()


@pytest.mark.parametrize('initial_group',[101,109])
@pytest.mark.parametrize('grandchild',[False,True])
def test_captured_descendant_that_leaves_group_blocks_resolution(closed_candidate,initial_group,grandchild):
    _,run,_,route,state,api,_=closed_candidate
    uid=os.getuid()
    state['rows']={100:(1,uid,'pane-birth',100),101:(100,uid,'runner-birth',101),
        102:(101,uid,'provider-birth',101),109:(108 if grandchild else 102,uid,'child-birth',initial_group)}
    if grandchild:state['rows'][108]=(102,uid,'middle-birth',101)
    # Persist the product's output, without inserting a child into members by hand.
    route['process_observation']=end.capture_groups(api,route)
    replace(run/'tmux.json',route)
    state['rows']={109:(1,uid,'child-birth',109)}
    state['starts']={109:'child-birth'}
    before=bytes_before(run)
    with pytest.raises(B1Error,match='FRESH_RESOLVE_PROCESS_ALIVE'):close(closed_candidate)
    assert dict(pid=109,process_start='child-birth') in route['process_observation']['members']
    assert bytes_before(run)==before and not (run/'resolved-ended.json').exists()


@pytest.mark.parametrize('damage',['overflow','short-row','bad-birth','foreign-child','cycle'])
def test_capture_cannot_silently_drop_unrecordable_descendants(closed_candidate,damage):
    _,run,_,route,state,api,_=closed_candidate
    uid=os.getuid()
    state['rows']={100:(1,uid,'pane-birth',100),101:(100,uid,'runner-birth',101),
        102:(101,uid,'provider-birth',101),109:(102,uid,'child-birth',109)}
    if damage=='overflow':
        state['rows'].update({pid:(102,uid,'child-birth',pid) for pid in range(200,262)})
    elif damage=='short-row':state['rows'][109]=(102,uid,'child-birth')
    elif damage=='bad-birth':state['rows'][109]=(102,uid,None,109)
    elif damage=='foreign-child':state['rows'][109]=(102,uid+1,'child-birth',109)
    elif damage=='cycle':state['rows'][100]=(109,uid,'pane-birth',100)
    before=bytes_before(run)
    with pytest.raises(B1Error):end.capture_groups(api,route)
    assert bytes_before(run)==before


@pytest.mark.parametrize('with_command',[False,True])
@pytest.mark.parametrize('pid,dead,valid',[('100','0',True),('100','1',True),('','1',True),
    ('','0',False),('0','1',False),('-1','1',False),('100','?',False)])
def test_shared_pane_parser_retains_only_dead_empty_pid(with_command,pid,dead,valid):
    raw=f'$1\t%2\t{pid}\t{dead}'+('\tpython' if with_command else '')+'\n'
    if valid:
        row,=delivery.parse_panes(raw,with_command=with_command)
        assert row['pid']==(int(pid) if pid else None) and row['dead']==(dead=='1')
    else:
        with pytest.raises(ValueError):delivery.parse_panes(raw,with_command=with_command)


def test_capture_member_bound_is_not_truncated_and_gone_children_allow_close(closed_candidate):
    _,run,_,route,state,api,_=closed_candidate
    uid=os.getuid()
    state['rows']={100:(1,uid,'pane-birth',100),101:(100,uid,'runner-birth',101),
        102:(101,uid,'provider-birth',101)}
    state['rows'].update({pid:(102,uid,'child-birth',pid) for pid in range(200,261)})
    value=end.capture_groups(api,route)
    assert len(value['members'])==64
    route['process_observation']=value;replace(run/'tmux.json',route)
    state['rows']={999:(1,uid,'other-birth',999)}
    assert close(closed_candidate)['end_kind']=='resolved'


@pytest.mark.parametrize('kind',['normal','resolved'])
@pytest.mark.parametrize('with_receipt',[False,True])
def test_api40_spawn_status_keeps_resolution_separate_from_native_receipt(tmp_path,monkeypatch,closed_candidate,kind,with_receipt):
    from test_fresh_guard import live
    from test_fresh_child import binary_for_child
    from test_fresh_dashboard import dashboard,Transport
    from dashboard.fresh_backend import Backend
    from agentstack_mail.fresh_child import family,provider_home
    async def check():
        async with live(tmp_path) as (_,_,parent,_):
            _,binary=binary_for_child(parent,'claude',tmp_path)
            monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(binary))
            transport=Transport(tmp_path);backend=Backend(parent.path,ROOT,transport=lambda:transport)
            with dashboard(parent,backend,monkeypatch,tmp_path) as (module,request):
                code,version=await asyncio.to_thread(request,'/api/version')
                assert code==200 and version['api']==module.API_GENERATION==40
                payload=dict(standalone=True,name='ResolvedVerdict',task='bounded status fixture',
                    dir=str(tmp_path),provider='claude',model='claude-sonnet-4-6')
                code,accepted=await asyncio.to_thread(request,'/api/spawn',payload)
                assert code==200 and accepted['ok']
                child=backend.context_for(await backend.resolve(name='ResolvedVerdict'))
                _,task_base,_=family(child)
                run,intent,_=actual_candidate(child,closed_candidate)
                intent['child_spawn_id']=_read_private(task_base/'task.json')['spawn_id'];replace(run/'intent.json',intent)
                if with_receipt:
                    sid=str(uuid4());transcript=provider_home(child,'claude')/(sid+'.jsonl')
                    transcript.write_text('synthetic native receipt\n')
                    await launch.record_session(child,dict(hook_event_name='SessionStart',session_id=sid,transcript_path=str(transcript)))
                if kind=='resolved':
                    assert (await asyncio.to_thread(end.resolve_ended,child,ROOT,run.name,confirm_ended=True))['end_kind']=='resolved'
                else:_write_private(run/'ended.json',dict(launch_id=run.name,exit_code=23))
                code,status=await asyncio.to_thread(request,'/api/spawn-status?name=ResolvedVerdict')
                assert code==200
                expected='resolved-ended' if kind=='resolved' else 'ended'
                ready=with_receipt and kind=='normal'
                assert status['detail']==status['result']['execution']==expected
                assert status['result']['native_ready'] is ready
                assert status['state']==('ready' if ready else 'failed')
                assert len(transport.calls)==1  # Status never launches another native.
    asyncio.run(check())
