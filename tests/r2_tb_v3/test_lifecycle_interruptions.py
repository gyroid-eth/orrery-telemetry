"""Real HTTP/local publication interruption gates beyond the approved red probe."""
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from test_lifecycle_v3_red import api, server, ending_child, launch_probe
from test_recovery_v3_red import target_cli


@pytest.mark.parametrize('point',['phase-save','pending-clear','retained-save'])
def test_terminal_publication_interruptions_preserve_one_report(ending_child,server,monkeypatch,capsys,point):
    parent,child,delivery,payload=ending_child
    row=delivery.launch()
    row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'exited','returncode':0}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    cls=type(delivery);save=cls.save;clear=type(child).clear_mutation;write=type(child).write_output
    fired=False
    def save_fail(self,path,value,schema):
        nonlocal fired
        if point=='phase-save' and not fired and schema=='launch_intent' and value.get('termination',{}).get('phase')=='report-confirmed':
            fired=True;raise OSError('fixture phase interrupted')
        return save(self,path,value,schema)
    def clear_fail(self,raw):
        nonlocal fired
        if point=='pending-clear' and not fired and delivery.launch().get('termination',{}).get('phase')=='report-confirmed':
            fired=True;raise OSError('fixture clear interrupted')
        return clear(self,raw)
    def retained_fail(self,path,value,role):
        nonlocal fired
        if point=='retained-save' and not fired and role=='child' and value.get('phase')=='retained':
            fired=True;raise OSError('fixture retained interrupted')
        return write(self,path,value,role)
    monkeypatch.setattr(cls,'save',save_fail)
    monkeypatch.setattr(type(child),'clear_mutation',clear_fail)
    monkeypatch.setattr(type(child),'write_output',retained_fail)
    with pytest.raises(OSError):child.end_session(payload)
    assert fired
    rows=parent.call('fetch_inbox',{'limit':100})
    assert sum(x.get('sender',{}).get('agent_id')==child.identity['agent_id'] for x in rows)==1
    if point!='retained-save':
        assert child.outputs['mutation'].exists()
        assert child.inspect_owner()['retired_at'] is None
    else:
        assert child.inspect_owner()['retired_at'] is not None
        with pytest.raises(api['ClientError'],match='^OWNER_REQUIRED$'):child.end_session(payload)
    monkeypatch.setattr(cls,'save',save);monkeypatch.setattr(type(child),'clear_mutation',clear);monkeypatch.setattr(type(child),'write_output',write)
    target=target_cli(child,parent,server,monkeypatch,capsys)
    raw=child.outputs['mutation'].read_bytes() if child.outputs['mutation'].exists() else None
    args=['replay-end','--agent-id',str(child.identity['agent_id']),'--context',str(parent.path),
          '--confirm-state-digest',target['state_sha256'],'--expected-intent-sha256',hashlib.sha256(raw).hexdigest() if raw else 'null']
    assert api['main'](args)==0,capsys.readouterr()
    assert not child.outputs['mutation'].exists()
    assert delivery.launch()['termination']['phase']=='ended'
    rows=parent.call('fetch_inbox',{'limit':100})
    assert sum(x.get('sender',{}).get('agent_id')==child.identity['agent_id'] for x in rows)==1


def test_retired_unknown_report_needs_verified_parent_ack(ending_child,server,monkeypatch,capsys):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'exited','returncode':0}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    rpc=child.rpc
    def lost(method,params):
        reply=rpc(method,params)
        if params.get('name')=='send_message':
            assert api['result_value'](reply)['message_id']>0
            raise api['ClientError']('TRANSPORT_FAILED')
        return reply
    monkeypatch.setattr(child,'rpc',lost)
    with pytest.raises(api['ClientError'],match='^TRANSPORT_FAILED$'):child.end_session(payload)
    monkeypatch.setattr(child,'rpc',rpc)
    retired=child.call('retire_agent')
    assert retired['status']=='retired'
    pending=child.outputs['mutation'];raw=pending.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    binding={'server_instance_id':child.binding['expected_server_instance_id'],**{k:child.binding[k] for k in ('candidate_generation','authority_epoch')}}
    proof={'expected_old':binding,'expected_new':binding,'old_agent_id':child.identity['agent_id'],'new_agent_id':child.identity['agent_id'],'confirm':True}
    with pytest.raises(api['ClientError']):child.resolve_mutation(child,digest,**proof)
    assert pending.read_bytes()==raw
    with pytest.raises(api['ClientError'],match='^MUTATION_PENDING_CONFLICT$'):child.resolve_mutation(child,'0'*64,**proof,lifecycle_parent=parent)
    assert pending.read_bytes()==raw
    child.resolve_mutation(child,digest,**proof,lifecycle_parent=parent)
    assert not pending.exists()
    assert delivery.launch()['operator_resolution']['action']=='mutation-abandoned'
    assert delivery.launch()['termination']['phase']=='report-not-required'
    target=target_cli(child,parent,server,monkeypatch,capsys)
    assert api['main'](['replay-end','--agent-id',str(child.identity['agent_id']),'--context',str(parent.path),
        '--confirm-state-digest',target['state_sha256'],'--expected-intent-sha256','null'])==0,capsys.readouterr()
    assert delivery.launch()['termination']['phase']=='ended'


def test_sdk_end_cannot_retire_running_waited_provider(ending_child):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'pending','returncode':None}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    before=child.outputs['launch'].read_bytes()
    assert child.end_session(payload)=={'ok':True,'end':'unknown','changed':False}
    assert child.outputs['launch'].read_bytes()==before
    assert child.inspect_owner()['retired_at'] is None
    assert not child.outputs['mutation'].exists()


@pytest.mark.parametrize('bad',['nonce','parent','birth','waited'])
def test_exec_child_never_replays_consumed_or_foreign_runner(ending_child,monkeypatch,bad):
    import os
    import types
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row.update(phase='exec-unknown',execution={'runner':{'pid':310,'process_start':'fixture-birth'},'outcome':{'state':'exited' if bad=='waited' else 'pending','returncode':0 if bad=='waited' else None}})
    delivery.save(child.outputs['launch'],row,'launch_intent')
    original=delivery.exec_provider.__globals__['os'];effects=[]
    scoped=types.SimpleNamespace(**{**vars(original),'getppid':lambda:999 if bad=='parent' else 310,'execvpe':lambda *a:effects.append(a)})
    monkeypatch.setitem(delivery.exec_provider.__globals__,'os',scoped)
    monkeypatch.setitem(api['RuntimeClient'].record_live.__globals__,'process_start',lambda *a,**kw:'different-birth' if bad=='birth' else 'fixture-birth')
    before=child.outputs['launch'].read_bytes()
    with pytest.raises(api['ClientError'],match='^LAUNCH_RECEIPT_MISMATCH$'):
        delivery.exec_provider(row['launch_id'],'wrong' if bad=='nonce' else row['receipt_nonce'])
    assert effects==[] and child.outputs['launch'].read_bytes()==before


def test_missing_wrapper_does_not_retry_consumed_execution(launch_probe):
    client,delivery,calls,*_=launch_probe
    row=delivery.prepare_launch('claude',cwd=client.isolation)
    row.update(phase='gated',execution={'runner':{'pid':310,'process_start':'old-birth'},'outcome':{'state':'pending','returncode':None}})
    delivery.save(client.outputs['launch'],row,'launch_intent')
    state=delivery.status(row['launch_id']);before=client.outputs['launch'].read_bytes()
    with pytest.raises(api['ClientError'],match='^LAUNCH_OUTCOME_UNKNOWN$'):
        delivery.operator('launch-retry',{'launch_id':row['launch_id'],'expected_state_sha256':state['state_sha256']})
    assert not any('new-session' in argv for argv in calls)
    assert client.outputs['launch'].read_bytes()==before


@pytest.mark.parametrize('phase',['ended','planned'])
def test_old_standalone_end_does_not_consume_an_unrelated_launch(ending_child,phase):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['phase']=phase
    delivery.save(child.outputs['launch'],row,'launch_intent')
    slot=child.runtime_dir/'live-sessions/tb-ended.json'
    live=api['read_json'](slot)
    for key in ('launch_id','receipt_nonce'):live.pop(key,None)
    api['atomic_json'](slot,live)
    before=child.outputs['launch'].read_bytes()
    result=child.end_session(payload)
    assert child.outputs['launch'].read_bytes()==before
    if phase=='ended':
        assert result['end']=='last-session'
        assert api['read_json'](slot)['ended']
        assert child.inspect_owner()['retired_at'] is not None
    else:
        assert result=={'ok':True,'end':'unknown','changed':False}
        assert not api['read_json'](slot)['ended']
        assert child.inspect_owner()['retired_at'] is None


def test_actual_target_keeps_standalone_with_old_ended_launch(ending_child,monkeypatch,capsys):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['phase']='ended'
    row['execution']={'runner':{'pid':310,'process_start':'old-runner'},'outcome':{'state':'exited','returncode':0}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    slot=child.runtime_dir/'live-sessions/tb-ended.json';live=api['read_json'](slot)
    for key in ('launch_id','receipt_nonce'):live.pop(key,None)
    api['atomic_json'](slot,live)
    assert api['main'](['target','--agent-id',str(child.identity['agent_id']),'--context',str(parent.path),'--print'])==0
    reply=json.loads(capsys.readouterr().out)
    assert reply['target']['kind']=='standalone-process'
    assert reply['target']['pid']==live['pid'] and reply['target']['launch'] is None
    assert 'lifecycle' not in reply


def test_retire_raw_s2_only_fields_are_rejected_by_healthy_server(ending_child):
    parent,child,delivery,payload=ending_child
    args={**child.binding,'agent_id':child.identity['agent_id'],'registration_token':child.local_owner(),
          'expected_credential_generation':child.identity['credential_generation'],'request_id':str(uuid.uuid4())}
    with pytest.raises(api['ClientError'],match='^TOOL_REJECTED$'):
        api['result_value'](child.rpc('tools/call',{'name':'retire_agent','arguments':args}))
    assert child.inspect_owner()['retired_at'] is None
    assert child.call('retire_agent')['status']=='retired'


@pytest.mark.parametrize('collision',['before-create','after-create-case','lost-response'])
def test_creation_races_preserve_foreign_target_and_never_adopt(launch_probe,monkeypatch,collision):
    client,delivery,calls,occupied,_=launch_probe
    command=delivery.launch_command;created=[]
    def race(*args):
        if args[0]=='new-session':
            created.append(args)
            if collision=='before-create':
                occupied.append(client.identity['name'])
                return __import__('subprocess').CompletedProcess(args,1,'','duplicate session')
            if collision=='lost-response':
                raise __import__('subprocess').TimeoutExpired(args,5)
            occupied.extend([client.identity['name'],client.identity['name'].swapcase()])
        return command(*args)
    monkeypatch.setattr(type(delivery),'launch_command',lambda self,*args:race(*args))
    if collision=='after-create-case':
        delivery.start('claude',client.isolation,detached=True)
        row=delivery.launch()
        tmux=api['runtime_tmux_api']();scope=delivery.execute.__globals__
        fake=tmux['global_socket'](Path(client.config['agentstack_home']),client.binding['expected_server_instance_id'])
        monkeypatch.setenv('TMUX',str(fake)+',123,0');monkeypatch.setenv('TMUX_PANE','%1')
        monkeypatch.setitem(tmux,'panes',lambda *a,**kw:[{'pid':scope['os'].getpid(),'session':client.identity['name'],'pane_id':'%1','session_id':'$1'}])
        import socket
        with socket.socket(socket.AF_UNIX) as peer:
            peer.bind(str(fake));fake.chmod(0o600)
            with pytest.raises(api['ClientError'],match='^TMUX_SESSION_NAME_CONFLICT$'):delivery.execute(row['launch_id'],row['receipt_nonce'])
    else:
        reason='LAUNCH_OUTCOME_UNKNOWN' if collision=='lost-response' else 'TMUX_SESSION_NAME_CONFLICT'
        with pytest.raises(api['ClientError'],match='^'+reason+'$'):delivery.start('claude',client.isolation,detached=True)
    assert len(created)==1
    row=delivery.launch()
    assert row['phase']=='gated' and 'execution' not in row
    assert not any(x[3] in ('kill-session','kill-pane','send-keys','paste-buffer') for x in calls)
    assert client.inspect_owner()['retired_at'] is None


def test_actual_popen_failure_records_not_started_and_finishes_local_end(launch_probe,monkeypatch):
    client,delivery,*_=launch_probe
    row=delivery.prepare_launch('claude',cwd=client.isolation)
    delivery.bind_window();row=delivery.launch();row['phase']='gated'
    delivery.save(client.outputs['launch'],row,'launch_intent')
    module=delivery.execute.__globals__;subprocess=module['subprocess'];attempts=[]
    def cannot_spawn(*args,**kwargs):
        attempts.append(args);raise OSError('fixture actual spawn failure')
    import types
    monkeypatch.setitem(module,'subprocess',types.SimpleNamespace(**{**vars(subprocess),'Popen':cannot_spawn}))
    monkeypatch.setitem(api['RuntimeClient'].record_live.__globals__,'process_start',lambda *a,**kw:'fixture-runner-birth')
    with pytest.raises(OSError,match='fixture actual spawn failure'):
        delivery.execute(row['launch_id'],row['receipt_nonce'])
    assert len(attempts)==1
    saved=delivery.launch()
    assert saved['execution']['outcome']=={'state':'not-started','returncode':None}
    assert saved['phase']=='ended' and saved['termination']['phase']=='ended'
    assert client.inspect_owner()['retired_at'] is None


@pytest.mark.parametrize('caller',['owner','parent','sibling'])
def test_actual_replay_end_role_and_wrong_digest_keep_intent(ending_child,server,monkeypatch,capsys,caller):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'exited','returncode':0}}
    row['phase']='exec-unknown';delivery.save(child.outputs['launch'],row,'launch_intent')
    if caller=='sibling':
        prepared=parent.prepare_child({'child_client_name':'sibling','name':'SiblingChild',
            'prepare_only':True,'program':'claude-code','model':'fixture'})
        principal=api['RuntimeClient'](prepared['client_config'])
        assert principal.identity['agent_id'] not in (child.identity['agent_id'],parent.identity['agent_id'])
    else:principal=child if caller=='owner' else parent
    before=child.outputs['launch'].read_bytes()
    target_args=['target','--agent-id',str(child.identity['agent_id']),'--context',str(principal.path),'--print']
    status=api['main'](target_args);captured=capsys.readouterr()
    if caller=='sibling':
        assert status==2 and json.loads(captured.out)['reason']=='OWNER_REQUIRED'
        assert child.outputs['launch'].read_bytes()==before and child.inspect_owner()['retired_at'] is None
        return
    assert status==0,captured
    target=json.loads(captured.out)
    args=['replay-end','--agent-id',str(child.identity['agent_id']),'--context',str(principal.path),
          '--confirm-state-digest','0'*64,'--expected-intent-sha256','null']
    assert api['main'](args)==2
    assert json.loads(capsys.readouterr().out)['reason']=='RESOLUTION_DIGEST_MISMATCH'
    assert child.outputs['launch'].read_bytes()==before and child.inspect_owner()['retired_at'] is None
    args[args.index('--confirm-state-digest')+1]=target['state_sha256']
    assert api['main'](args)==0,capsys.readouterr()
    reply=json.loads(capsys.readouterr().out)
    assert reply['lifecycle']['phase']=='ended'
    assert child.inspect_owner()['retired_at'] is not None
    if caller=='owner':
        assert reply['next_operations']==[]
        assert api['main'](target_args)==2
        assert json.loads(capsys.readouterr().out)['reason']=='OWNER_REQUIRED'


def test_post_end_read_failure_is_unknown_after_retirement(ending_child,server,monkeypatch,capsys):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'exited','returncode':0}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    target=target_cli(child,parent,server,monkeypatch,capsys)
    original=type(child).end_session
    scope=type(child).management.__globals__
    exchange=scope['management_exchange']
    completed=False
    def end(self,payload):
        nonlocal completed
        result=original(self,payload);completed=True;return result
    def read(*args,**kw):
        if completed:raise api['ClientError']('OBSERVATION_INCOMPLETE')
        return exchange(*args,**kw)
    monkeypatch.setattr(type(child),'end_session',end)
    monkeypatch.setitem(scope,'management_exchange',read)
    assert api['main'](['replay-end','--agent-id',str(child.identity['agent_id']),'--context',str(parent.path),
        '--confirm-state-digest',target['state_sha256'],'--expected-intent-sha256','null'])==3
    reply=json.loads(capsys.readouterr().out)
    assert reply['effects']=='unknown' and reply['reason']=='TMUX_OPERATION_OUTCOME_UNKNOWN'
    completed=False
    assert child.inspect_owner()['retired_at'] is not None
    assert delivery.launch()['termination']['phase']=='ended'


@pytest.mark.parametrize('ancestor,in_process',[(400,False),(None,False),(None,True)])
def test_sdk_prefers_native_descendant_and_never_adopts_runner_pid(ending_child,monkeypatch,ancestor,in_process):
    import os,types,subprocess
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'pending','returncode':None}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    scope=type(child).record_live.__globals__
    monkeypatch.setitem(scope,'process_start',lambda pid,**kw:'runner-birth' if pid==310 else 'provider-birth')
    monkeypatch.setitem(scope,'provider_ancestor',lambda **kw:ancestor)
    monkeypatch.setenv('AGENTSTACK_PROVIDER_PID','400' if in_process else '310')
    monkeypatch.setenv('AGENTSTACK_LAUNCH_ID',row['launch_id'])
    monkeypatch.setenv('AGENTSTACK_LAUNCH_NONCE',row['receipt_nonce'])
    delivery_scope=type(delivery).session_start.__globals__
    monkeypatch.setitem(delivery_scope,'os',types.SimpleNamespace(**{**vars(os),'getpid':lambda:400}))
    process_api=api['runtime_tmux_api']()['module_api']('process_observation')
    ps=types.SimpleNamespace(run=lambda *a,**kw:subprocess.CompletedProcess(a,0,f'310 1 {os.getuid()}\n400 310 {os.getuid()}\n',''))
    monkeypatch.setitem(process_api['belongs_to'].__globals__,'subprocess',ps)
    before=child.outputs['launch'].read_bytes()
    args={key:value for key,value in payload.items() if key!='provider_pid'}
    if ancestor is None and not in_process:
        with pytest.raises(api['ClientError'],match='^SESSION_LIVENESS_UNKNOWN$'):
            delivery.session_start(args,trusted_receipt=(row['launch_id'],row['receipt_nonce']))
        assert child.outputs['launch'].read_bytes()==before
    else:
        delivery.session_start(args,**({} if in_process else {'trusted_receipt':(row['launch_id'],row['receipt_nonce'])}))
        live=api['read_json'](child.runtime_dir/'live-sessions/tb-ended.json')
        assert live['pid']==400 and live['process_start']=='provider-birth'
        assert delivery.launch()['execution']['runner']['pid']==310


@pytest.mark.parametrize('native_survives',[False,True])
def test_waiting_for_shim_never_proves_live_native_descendant_ended(launch_probe,monkeypatch,native_survives):
    import os,types,subprocess
    client,delivery,*_=launch_probe
    row=delivery.prepare_launch('claude',cwd=client.isolation)
    delivery.bind_window();row=delivery.launch();row['phase']='gated'
    delivery.save(client.outputs['launch'],row,'launch_intent')
    runner=os.getpid();state={'native_live':True}
    def birth(pid,**kw):
        if pid==runner:return 'runner-birth'
        if pid==500:return 'shim-birth'
        if pid==400:return 'native-birth' if state['native_live'] else 'absent'
        return 'absent'
    scope=type(client).record_live.__globals__
    monkeypatch.setitem(scope,'process_start',birth)
    proc_api=api['runtime_tmux_api']()['module_api']('process_observation')
    monkeypatch.setitem(proc_api['belongs_to'].__globals__,'subprocess',types.SimpleNamespace(run=lambda *a,**kw:subprocess.CompletedProcess(a,0,f'{runner} 1 {os.getuid()}\n500 {runner} {os.getuid()}\n400 500 {os.getuid()}\n','')))
    class Shim:
        pid=500
        def __init__(self,*args,**kw):
            delivery.session_start({'session_id':'shim-sdk','provider_pid':400,'cwd':str(client.isolation)},trusted_receipt=(row['launch_id'],row['receipt_nonce']))
        def wait(self,**kw):
            state['native_live']=native_survives
            return 0
    module=delivery.execute.__globals__;original=module['subprocess']
    monkeypatch.setitem(module,'subprocess',types.SimpleNamespace(**{**vars(original),'Popen':Shim}))
    assert delivery.execute(row['launch_id'],row['receipt_nonce'])==0
    saved=delivery.launch()
    if native_survives:
        assert saved['execution']['outcome']=={'state':'unknown','returncode':None}
        assert saved['phase']=='running' and 'termination' not in saved
        assert not api['read_json'](client.runtime_dir/'live-sessions/shim-sdk.json')['ended']
        assert delivery.status(row['launch_id'])['operator_required']
    else:
        assert saved['execution']['outcome']=={'state':'exited','returncode':0}
        assert saved['termination']['phase']=='ended'
    assert client.inspect_owner()['retired_at'] is None
