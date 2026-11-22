"""Public lifecycle/receipt shapes and their wrong-implementation controls."""
import hashlib
import json
from pathlib import Path

import pytest
from test_lifecycle_v3_red import api, server, ending_child
from test_global_runtime_delivery import context


def cli(client,capsys,operation,**options):
    argv=[operation,'--context',str(client.path),'--agent-id',str(client.identity['agent_id'])]
    if operation=='target':argv.append('--print')
    for key,value in options.items():argv.extend(['--'+key.replace('_','-'),str(value)])
    capsys.readouterr();rc=api['main'](argv);output=capsys.readouterr()
    return rc,json.loads(output.out) if output.out else {},output.err


@pytest.mark.parametrize('old_launch',[False,True,'history'])
@pytest.mark.parametrize('lost_release',[False,True])
def test_absent_standalone_cli_replays_own_release_without_retire(server,monkeypatch,capsys,old_launch,lost_release):
    c=context(server,window=False)
    prepared=c.prepare_child({'child_client_name':'foreign','name':'ForeignOwner','prepare_only':True,'program':'claude-code','model':'fixture'})
    foreign=api['RuntimeClient'](Path(prepared['client_config']))
    owned=c.call('file_reservation_paths',{'paths':[str(c.isolation/'owned.py')]})['granted'][0]['id']
    other=foreign.call('file_reservation_paths',{'paths':[str(c.isolation/'foreign.py')]})['granted'][0]['id']
    scope=type(c).record_live.__globals__
    monkeypatch.setitem(scope,'process_start',lambda *a,**kw:'birth')
    monkeypatch.delenv('TMUX_PANE',raising=False)
    c.record_live({'session_id':'standalone','provider_pid':123})
    if old_launch:
        monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN','/usr/bin/true')
        d=api['runtime_delivery'](c);row=d.prepare_launch('claude',cwd=c.isolation)
        d.save(c.outputs['launch'],{**row,'phase':'ended'},'launch_intent')
        if old_launch=='history':
            saved=api['read_json'](c.runtime_dir/'live-sessions/standalone.json')
            api['atomic_json'](c.runtime_dir/'live-sessions/previous-launch.json',
                {**saved,'session_id':'previous-launch','pid':456,'ended':True,
                 'launch_id':row['launch_id'],'receipt_nonce':row['receipt_nonce']})
    monkeypatch.setitem(scope,'process_start',lambda *a,**kw:'absent')
    calls=[];rpc=type(c).rpc;lost=False
    def observe(self,method,params=None):
        nonlocal lost
        result=rpc(self,method,params)
        if (params or {}).get('name')=='release_file_reservations':
            calls.append(api['result_value'](result))
            if lost_release and not lost:
                lost=True
                raise api['ClientError']('TRANSPORT_FAILED')
        return result
    monkeypatch.setattr(type(c),'rpc',observe)
    if lost_release:
        with pytest.raises(api['ClientError'],match='^TRANSPORT_FAILED$'):c.end_session({'session_id':'standalone'})
        assert len(calls)==1
    pending=c.outputs['mutation'];raw=pending.read_bytes() if pending.exists() else None
    rc,target,err=cli(c,capsys,'target')
    assert rc==0,(json.dumps(target),err)
    assert target['target']['kind']=='standalone-lifecycle' and target['target']['launch'] is None
    assert target['target']['pid'] is None and target['target']['pane_id'] is None
    assert 'replay-end' in target['next_operations']
    assert not set(target['next_operations']) & {'capture','attach','send-compact','end','launch-resolve'}
    intent=hashlib.sha256(raw).hexdigest() if raw else 'null'
    rc,result,err=cli(c,capsys,'replay-end',confirm_state_digest='0'*64,expected_intent_sha256=intent)
    assert rc==2 and result['reason']=='RESOLUTION_DIGEST_MISMATCH'
    assert (pending.read_bytes() if pending.exists() else None)==raw
    rc,rejected,err=cli(c,capsys,'replay-end',confirm_state_digest=target['state_sha256'],expected_intent_sha256='0'*64)
    assert rc==2 and rejected['reason']=='MUTATION_PENDING_CONFLICT'
    assert (pending.read_bytes() if pending.exists() else None)==raw
    rc,result,err=cli(c,capsys,'replay-end',confirm_state_digest=target['state_sha256'],expected_intent_sha256=intent)
    assert rc==0,(result,err)
    assert result['target']['kind']=='standalone-lifecycle'
    assert not pending.exists() and api['read_json'](c.runtime_dir/'live-sessions/standalone.json')['ended']
    assert c.inspect_owner()['retired_at'] is None
    assert owned not in {x['id'] for page in c.all_leases() for x in page}
    assert other in {x['id'] for page in foreign.all_leases() for x in page}
    assert c.outputs['launch'].exists()==bool(old_launch)
    assert len(calls)==(2 if lost_release else 1)
    assert calls[-1]['agent_id']==c.identity['agent_id']
    if lost_release:
        assert calls[0]['request_id']==calls[1]['request_id']
        assert calls[0]['mutation_revision']==calls[1]['mutation_revision']


@pytest.mark.parametrize('shape',['string','text-block','error','bad-json','wrong-recipient'])
def test_global_parent_receipt_normalization_controls_terminal_report(ending_child,monkeypatch,shape):
    parent,c,d,payload=ending_child
    # This receipt is a real server success, including the actual parent ID.
    receipt=c.call('send_message',{'to_agent_ids':[parent.identity['agent_id']],'subject':'Already reported','body_md':'done'})
    rows=[{'type':'user','message':{'content':'Delegated child agent'}},
          {'type':'assistant','message':{'id':'tool','stop_reason':'tool_use','content':[
              {'type':'tool_use','id':'report','name':'mcp__orrery-mail__send_message','input':{'to_agent_ids':[parent.identity['agent_id']]}}]}}]
    if shape=='wrong-recipient':receipt={**receipt,'recipient_agent_ids':[99999]}
    text='broken' if shape=='bad-json' else json.dumps(receipt)
    content=text if shape=='string' else [{'type':'text','text':text}]
    rows.extend([{'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'report','is_error':shape=='error','content':content}]}},
                 {'type':'assistant','message':{'id':'end','stop_reason':'end_turn','content':[{'type':'text','text':'Done.'}]}}])
    Path(payload['transcript_path']).write_text(''.join(json.dumps(x)+'\n' for x in rows))
    row=d.launch();row['execution']={'runner':{'pid':310,'process_start':'runner'},'outcome':{'state':'exited','returncode':0}}
    d.save(c.outputs['launch'],row,'launch_intent')
    monkeypatch.setitem(type(c).record_live.__globals__,'process_start',lambda *a,**kw:'absent')
    assert d.terminal_kind(row,payload)==('reported' if shape in ('string','text-block') else 'report_failed')
    c.end_session(payload)
    messages=parent.call('fetch_inbox',{'limit':100})
    assert sum(x.get('sender',{}).get('agent_id')==c.identity['agent_id'] for x in messages)==(1 if shape in ('string','text-block') else 2)
    assert d.launch()['termination']['phase']=='ended'


@pytest.mark.parametrize('state',['live','unknown','unrelated-pending'])
def test_standalone_recovery_preserves_other_live_unknown_and_other_intent(server,monkeypatch,capsys,state):
    c=context(server,window=False);scope=type(c).record_live.__globals__
    monkeypatch.setitem(scope,'process_start',lambda *a,**kw:'birth')
    monkeypatch.delenv('TMUX_PANE',raising=False)
    c.record_live({'session_id':'standalone','provider_pid':123})
    owned=c.call('file_reservation_paths',{'paths':[str(c.isolation/'owned.py')]})['granted'][0]['id']
    if state=='unrelated-pending':
        rpc=c.rpc
        def lose(method,params):
            result=rpc(method,params)
            if params.get('name')=='set_contact_policy':raise api['ClientError']('TRANSPORT_FAILED')
            return result
        monkeypatch.setattr(c,'rpc',lose)
        with pytest.raises(api['ClientError'],match='^TRANSPORT_FAILED$'):c.call('set_contact_policy',{'policy':'open'})
        monkeypatch.setattr(c,'rpc',rpc)
    else:c.record_live({'session_id':'other','provider_pid':124})
    monkeypatch.setitem(scope,'process_start',lambda pid,**kw:('birth' if state=='live' else None) if pid==124 else 'absent')
    pending=c.outputs['mutation'];before=pending.read_bytes() if pending.exists() else None
    rc,target,err=cli(c,capsys,'target')
    if rc==0:assert 'replay-end' not in target['next_operations']
    rc,result,err=cli(c,capsys,'replay-end',confirm_state_digest=target.get('state_sha256','0'*64),expected_intent_sha256=hashlib.sha256(before).hexdigest() if before else 'null')
    assert rc==2,(result,err)
    assert (pending.read_bytes() if pending.exists() else None)==before
    assert owned in {x['id'] for page in c.all_leases() for x in page}
    assert c.inspect_owner()['retired_at'] is None
    assert not api['read_json'](c.runtime_dir/'live-sessions/standalone.json')['ended']


def test_inventory_carries_the_actual_launch_to_legacy_runner_admission(ending_child):
    import subprocess,types
    parent,c,d,payload=ending_child
    row=d.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'pending','returncode':None}}
    d.save(c.outputs['launch'],row,'launch_intent')
    slot=c.runtime_dir/'live-sessions/tb-ended.json'
    api['atomic_json'](slot,{**api['read_json'](slot),'pane_id':'%4'})
    tmux=api['runtime_tmux_api']()
    inventory=tmux['global_inventory'](Path(c.config['agentstack_home']),api=api)
    assert inventory['global_complete'],inventory['diagnostics']
    proof=next(x for x in inventory['proofs'] if x['agent_id']==c.identity['agent_id'])
    assert proof['launch']['execution']['runner']==row['execution']['runner']
    # The inventory is real; only the OS command boundary is synthetic.
    channel=types.SimpleNamespace(path=Path('/fixture/socket'),argv=['tmux','-S','/fixture/socket'],
        command=lambda *a,**kw:subprocess.CompletedProcess(a,0,'$1\tChildAlpha\t%4\t310\n',''))
    with pytest.raises(tmux['TargetError'],match='^LEGACY_TARGET_IS_GLOBAL$'):
        tmux['legacy_admission'](channel,Path(c.config['agentstack_home']),pane='%4',operation='input',
            api={'process_start':lambda pid,**kw:'runner-birth' if pid==310 else 'absent'},
            proof_reader=lambda *a,**kw:inventory['proofs'])
