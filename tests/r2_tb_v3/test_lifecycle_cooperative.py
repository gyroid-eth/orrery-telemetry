"""Concurrent terminal callers and last-session/foreign-lease protection."""
import concurrent.futures
import threading
from pathlib import Path

import pytest
from test_lifecycle_v3_red import api,server,ending_child
from test_global_runtime_delivery import context


def test_hook_and_runner_concurrently_publish_one_report(ending_child,monkeypatch):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'exited','returncode':0}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    peer=api['RuntimeClient'](child.path);ready=threading.Barrier(2)
    original=type(child).call;retire=[]
    def count(self,tool,arguments=None):
        if tool=='retire_agent':retire.append(self.identity['agent_id'])
        return original(self,tool,arguments)
    monkeypatch.setattr(type(child),'call',count)
    def finish(client):
        ready.wait(timeout=5);return client.end_session(payload)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(finish,[child,peer]))
    assert sorted(x['changed'] for x in results)==[False,True]
    assert retire==[child.identity['agent_id']]
    messages=parent.call('fetch_inbox',{'limit':100})
    assert sum(x.get('sender',{}).get('agent_id')==child.identity['agent_id'] for x in messages)==1
    assert delivery.launch()['termination']['phase']=='ended'


@pytest.mark.parametrize('state',['live','unknown','foreign-nonce'])
def test_other_session_or_foreign_nonce_preserves_active_child(ending_child,monkeypatch,state):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'exited','returncode':0}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    slot=child.runtime_dir/'live-sessions/tb-ended.json';value=api['read_json'](slot)
    if state=='foreign-nonce':
        value['receipt_nonce']='another-nonce';api['atomic_json'](slot,value)
    else:
        value.update(session_id='another',pid=124)
        for key in ('launch_id','receipt_nonce'):value.pop(key,None)
        api['atomic_json'](slot.with_name('another.json'),value)
        monkeypatch.setitem(type(child).record_live.__globals__,'process_start',lambda pid,**kw:None if state=='unknown' and pid==124 else 'fixture-birth')
    before=child.outputs['launch'].read_bytes()
    if state=='foreign-nonce':
        with pytest.raises(api['ClientError'],match='^LAUNCH_RECEIPT_MISMATCH$'):child.end_session(payload)
    else:
        assert child.end_session(payload)['end']==('other-session-live' if state=='live' else 'unknown')
    assert child.outputs['launch'].read_bytes()==before
    assert not child.outputs['mutation'].exists()
    assert child.inspect_owner()['retired_at'] is None
    assert not api['read_json'](slot)['ended']


def test_last_standalone_releases_only_own_active_lease(server,monkeypatch):
    client=context(server,window=False)
    prepared=client.prepare_child({'child_client_name':'other','name':'OtherLeaseOwner',
        'prepare_only':True,'program':'claude-code','model':'fixture'})
    other=api['RuntimeClient'](Path(prepared['client_config']))
    owned=client.call('file_reservation_paths',{'paths':[str(client.isolation/'own.py')]})['granted'][0]['id']
    foreign=other.call('file_reservation_paths',{'paths':[str(client.isolation/'other.py')]})['granted'][0]['id']
    monkeypatch.setitem(type(client).record_live.__globals__,'process_start',lambda *a,**kw:'fixture-birth')
    monkeypatch.delenv('TMUX_PANE',raising=False)
    client.record_live({'session_id':'standalone','provider_pid':123})
    assert client.end_session({'session_id':'standalone'})['end']=='last-session'
    assert not client.outputs['launch'].exists()
    assert client.inspect_owner()['retired_at'] is None
    assert owned not in {x['id'] for page in client.all_leases() for x in page}
    assert foreign in {x['id'] for page in other.all_leases() for x in page}


@pytest.mark.parametrize('provider_present',[False,True])
def test_explicit_ended_cannot_contradict_a_live_provider_without_runner(ending_child,monkeypatch,provider_present):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'pending','returncode':None}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    monkeypatch.setitem(type(child).record_live.__globals__,'process_start',lambda pid,**kw:'fixture-birth' if provider_present and pid==123 else 'absent')
    request={'launch_id':row['launch_id'],'expected_state_sha256':delivery.status(row['launch_id'])['state_sha256'],
             'resolution':{'action':'confirmed-ended','operator_evidence':'Fixture operator explicitly observed execution stopped'}}
    before=child.outputs['launch'].read_bytes()
    if provider_present:
        with pytest.raises(api['ClientError'],match='^LAUNCH_OUTCOME_UNKNOWN$'):delivery.resolve_launch(request)
        assert child.outputs['launch'].read_bytes()==before
        assert not api['read_json'](child.runtime_dir/'live-sessions/tb-ended.json')['ended']
        assert child.inspect_owner()['retired_at'] is None
    else:
        assert delivery.resolve_launch(request)['phase']=='ended'
        child.end_session({})
        assert delivery.launch()['termination']['phase']=='ended'
