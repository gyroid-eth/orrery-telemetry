"""Ended history is not current liveness; unresolved sessions remain protected."""
import hashlib
import io
import json
from pathlib import Path

import pytest
from test_lifecycle_v3_red import api, server
from test_global_runtime_delivery import context


@pytest.mark.parametrize('principal',['owner','operator'])
@pytest.mark.parametrize('state',['absent','live','reused','unknown','ended-pending'])
def test_old_ended_pid_reuse_never_blocks_or_authorizes_cleanup(server,monkeypatch,capsys,principal,state):
    c=context(server,window=False)
    prepared=c.prepare_child({'child_client_name':'foreign','name':'ForeignLeaseOwner',
        'prepare_only':True,'program':'claude-code','model':'fixture'})
    foreign=api['RuntimeClient'](Path(prepared['client_config']))
    own=c.call('file_reservation_paths',{'paths':[str(c.isolation/'own.py')]})['granted'][0]['id']
    other=foreign.call('file_reservation_paths',{'paths':[str(c.isolation/'other.py')]})['granted'][0]['id']
    scope=type(c).record_live.__globals__
    monkeypatch.delenv('TMUX_PANE',raising=False)
    monkeypatch.setitem(scope,'process_start',lambda *a,**kw:'original-birth')
    c.record_live({'session_id':'a-closed','provider_pid':456})
    old=c.runtime_dir/'live-sessions/a-closed.json'
    api['atomic_json'](old,{**api['read_json'](old),'ended':True})
    old_bytes=old.read_bytes()
    c.record_live({'session_id':'current','provider_pid':123})
    slot=c.runtime_dir/'live-sessions/current.json'
    receipts=[];rpc=type(c).rpc;lost=False
    def observe(self,method,params=None):
        nonlocal lost
        result=rpc(self,method,params)
        if (params or {}).get('name')=='release_file_reservations':
            receipts.append(api['result_value'](result))
            if state=='ended-pending' and not lost:
                lost=True;raise api['ClientError']('TRANSPORT_FAILED')
        return result
    monkeypatch.setattr(type(c),'rpc',observe)
    if state=='ended-pending':
        with pytest.raises(api['ClientError'],match='^TRANSPORT_FAILED$'):c.end_session({'session_id':'current'})
        # Previously completed local evidence must not revive a numeric PID.
        api['atomic_json'](slot,{**api['read_json'](slot),'ended':True})
    observed=[];signals=[]
    def start(pid,**kwargs):
        observed.append(pid)
        if pid==456:return 'unrelated-reused-birth'
        return {'absent':'absent','live':'original-birth','reused':'new-birth',
                'unknown':None,'ended-pending':'unrelated-current-birth'}[state]
    monkeypatch.setitem(scope,'process_start',start)
    # Catch the shared signal boundary without replacing the global os module.
    # No recovery command should ever request a provider exit.
    monkeypatch.setitem(scope,'request_provider_exit',lambda **kw:signals.append(kw))
    pending=c.outputs['mutation'];pending_before=pending.read_bytes() if pending.exists() else None
    slot_before=slot.read_bytes()
    class Approved(io.StringIO):
        def isatty(self):return True
    monkeypatch.setattr(__import__('sys'),'stdin',Approved('yes\n'*4))
    def command(operation,**options):
        args=[operation,'--agent-id',str(c.identity['agent_id'])]
        if operation=='target':args.append('--print')
        if principal=='operator':args+=['--operator','--runtime-mode','global','--server-config',str(server['config_path'])]
        else:args+=['--context',str(c.path)]
        for key,value in options.items():args+=['--'+key.replace('_','-'),str(value)]
        capsys.readouterr();rc=api['main'](args);out=capsys.readouterr()
        return rc,json.loads(out.out),out.err
    rc,target,err=command('target')
    if state in ('absent','ended-pending'):
        assert rc==0,(target,err)
        assert target['target']['kind']=='standalone-lifecycle'
        assert 'replay-end' in target['next_operations']
        rc,result,err=command('replay-end',confirm_state_digest=target['state_sha256'],
            expected_intent_sha256=hashlib.sha256(pending_before).hexdigest() if pending_before else 'null')
        assert rc==0,(result,err)
        assert own not in {x['id'] for page in c.all_leases() for x in page}
        assert api['read_json'](slot)['ended'] and not pending.exists()
        assert 456 not in observed
        if state=='ended-pending':
            assert 123 not in observed
            assert len(receipts)==2
            assert receipts[0]['request_id']==receipts[1]['request_id']
            assert receipts[0]['mutation_revision']==receipts[1]['mutation_revision']
    else:
        if rc==0:assert 'replay-end' not in target['next_operations']
        rc,result,err=command('replay-end',confirm_state_digest=target.get('state_sha256','0'*64),
            expected_intent_sha256='null')
        assert rc==2,(result,err)
        assert slot.read_bytes()==slot_before
        assert (pending.read_bytes() if pending.exists() else None)==pending_before
        assert own in {x['id'] for page in c.all_leases() for x in page}
        assert receipts==[]
    assert old.read_bytes()==old_bytes and signals==[]
    assert other in {x['id'] for page in foreign.all_leases() for x in page}
    assert c.inspect_owner()['retired_at'] is None
