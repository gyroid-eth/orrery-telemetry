"""Lifecycle recovery through actual target/CLI and isolated S2c HTTP.

New record extensions are proposals: b6d4720 rejects their schema. That is
an explicit missing-contract red, not proof that any native flow worked.
"""
import hashlib
import io
import sys
import json
from pathlib import Path
import uuid

import pytest

from test_lifecycle_v3_red import ending_child, server, api
from r2_tb_v3_oracles import SPEC, target_schema


def pending_case(case):
    return case=="report-pending-provider-and-runner-absent"


def stable_id(child, launch):
    fields=[child.binding['expected_server_instance_id'],child.binding['candidate_generation'],
        child.binding['authority_epoch'],child.identity['agent_id'],launch['launch_id'],launch['receipt_nonce'],'terminal','declined']
    return str(uuid.uuid5(uuid.NAMESPACE_OID,json.dumps(fields,sort_keys=True,ensure_ascii=False,separators=(',',':'))))


def target_cli(child,parent,server,monkeypatch,capsys,*,operator=False):
    # The actual public dispatcher calls Targets.emit and its canonical schema.
    # No target resolver, emit function, or product schema is patched here.
    monkeypatch.setitem(api['RuntimeClient'].record_live.__globals__,'process_start',lambda *a,**kw:'absent')
    argv=['target','--agent-id',str(child.identity['agent_id']),'--print']
    if operator:argv+=['--operator','--runtime-mode','global','--server-config',str(server['config_path'])]
    else:argv+=['--context',str(parent.path)]
    capsys.readouterr()
    status=api['main'](argv)
    captured=capsys.readouterr()
    assert status==0,(captured.out,captured.err)
    target=json.loads(captured.out)
    api['wire_contract']()['validate_schema'](target,target_schema())
    return target


def test_F5_08_absent_pid_still_has_readonly_lifecycle_digest(ending_child,server,monkeypatch,capsys):
    parent,child,delivery,payload=ending_child
    value=target_cli(child,parent,server,monkeypatch,capsys)
    assert value['lifecycle']['phase']=='unknown'
    assert 'launch-resolve' in value['next_operations']
    assert not {'attach','capture','send-compact'} & set(value['next_operations'])
    assert len(value['state_sha256'])==64


@pytest.mark.parametrize('case',list(SPEC['recovery_cases']))
def test_F5_08_09_target_digest_returns_to_replay_or_resolution(ending_child,server,monkeypatch,case,capsys):
    parent,child,delivery,payload=ending_child
    expected=SPEC['recovery_cases'][case]
    launch=delivery.launch()
    monkeypatch.setitem(api['RuntimeClient'].record_live.__globals__,'process_start',lambda *a,**kw:'absent')
    receipt=None
    request={'to_agent_ids':[parent.identity['agent_id']],'subject':'Terminal fixture',
             'body_md':'Original terminal bytes','request_id':stable_id(child,launch)}
    if case=='report-pending-provider-and-runner-absent':
        original=child.rpc
        def lose(method,params):
            value=original(method,params)
            if params.get('name')=='send_message':
                committed=api['result_value'](value)
                assert committed['request_id']==request['request_id'] and committed['message_id']>0
                raise api['ClientError']('TRANSPORT_FAILED')
            return value
        monkeypatch.setattr(child,'rpc',lose)
        with pytest.raises(api['ClientError'],match='^TRANSPORT_FAILED$'):child.call('send_message',request)
        monkeypatch.setattr(child,'rpc',original)
        delivered=parent.call('fetch_inbox',{'limit':100})
        assert sum(r.get('sender',{}).get('agent_id')==child.identity['agent_id'] for r in delivered)==1
        phase='report-pending'
    elif case=='retired-cleanup-local-incomplete':
        receipt=child.call('send_message',request)
        child.call('retire_agent')
        phase='cleanup-pending'
    else:
        phase='report-required' if expected['provider_outcome']=='exited' else 'unknown'
        launch['phase']='exec-unknown'
        (child.runtime_dir/'live-sessions/tb-ended.json').unlink()
        (child.runtime_dir/'session_index/self.json').unlink(missing_ok=True)
    execution={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{
        'state':expected['provider_outcome'],'returncode':1 if expected['provider_outcome']=='exited' else None}}
    termination={'phase':phase,'report_kind':'declined' if receipt is not None or pending_case(case) else 'unverified','receipt_ref':None if receipt is None else {
        'request_id':receipt['request_id'],'message_id':receipt['message_id'],'mutation_revision':receipt['mutation_revision']}}
    for key,value in [('launch_execution',execution),('launch_termination',termination)]:
        api['wire_contract']()['validate_schema'](value,SPEC['record_deltas'][key])
    api['atomic_json'](child.outputs['launch'],{**launch,'execution':execution,'termination':termination})
    target=target_cli(child,parent,server,monkeypatch,capsys,operator=expected['principal']=='operator')
    assert target['lifecycle']['phase']==phase
    assert expected['next'] in target['next_operations']
    assert not {'attach','capture','send-compact'} & set(target['next_operations'])
    assert target['target']['kind']=='launch-lifecycle'
    assert target['target']['launch']=={key:launch[key] for key in ('launch_id','receipt_nonce')}
    assert target['target']['pid'] is None and target['target']['pane_id'] is None
    pending=child.outputs['mutation']
    pending_digest=hashlib.sha256(pending.read_bytes()).hexdigest() if pending.exists() else None
    assert target['lifecycle']['pending_sha256']==pending_digest
    if expected['next']=='launch-resolve':
        assert target['lifecycle']['provider_outcome']=='unknown'
        return
    # This is the real CLI dispatcher, not a response manufactured by a test adapter.
    argv=['replay-end','--agent-id',str(child.identity['agent_id']),
          '--confirm-state-digest',target['state_sha256'],'--expected-intent-sha256',pending_digest or 'null']
    if expected['principal']=='operator':argv+=['--operator','--runtime-mode','global','--server-config',str(server['config_path'])]
    else:argv+=['--context',str(parent.path)]
    class ApprovedInput(io.StringIO):
        def isatty(self):return True
    monkeypatch.setattr(sys,'stdin',ApprovedInput('yes\n'))
    result=api['main'](argv)
    assert result==0,capsys.readouterr().err
    assert not pending.exists()
    assert delivery.launch()['termination']['phase']=='ended'
    rows=parent.call('fetch_inbox',{'include_bodies':True,'limit':100})
    assert sum(r.get('sender',{}).get('agent_id')==child.identity['agent_id'] for r in rows)==1
