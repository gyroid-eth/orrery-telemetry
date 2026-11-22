"""Completed notification slots disappear without losing concurrent intent."""
from contextlib import contextmanager
import json
import select
from pathlib import Path
import subprocess
import sys

from test_global_runtime_delivery import server, launch, send, api, ROOT


def test_completed_messages_leave_no_slots_or_reconciliation_locks(server,launch,monkeypatch):
    client,delivery,row=launch
    for _ in range(8):
        mid=send(server);attempt=delivery.prepare(mid,'tmux')
        assert delivery.callback(mid,attempt['attempt_id'],confirmed=True)['state']=='complete'
    slots=delivery.consumer_agent()/'messages'
    calls=[];message=delivery.message
    @contextmanager
    def counted(mid):
        calls.append(mid)
        with message(mid) as path:yield path
    monkeypatch.setattr(delivery,'message',counted)
    delivery.reconcile_confirmed()
    assert list(slots.iterdir())==[]
    assert calls==[] and delivery.diagnostics()==[]


def test_legacy_empty_slots_are_removed_in_one_reconciliation(launch):
    client,delivery,row=launch
    slots=delivery.directory(delivery.consumer_agent()/'messages')
    for mid in (1001,1002,1003):delivery.directory(slots/str(mid))
    delivery.reconcile_confirmed()
    assert list(slots.iterdir())==[]
    assert delivery.intent_ids()==[] and delivery.diagnostics()==[]


def test_empty_slot_removal_serializes_with_other_process_prepare(server,launch,monkeypatch):
    client,delivery,row=launch;mid=send(server)
    directory=delivery.directory(delivery.consumer_agent()/'messages'/str(mid))
    remove=Path.rmdir;children=[];removed=[]
    script='''
from contextlib import contextmanager
import json,runpy,sys
api=runpy.run_path(sys.argv[1]);client=api['RuntimeClient'](sys.argv[2])
mutex=client.operation_mutex
@contextmanager
def waiting():
    print('waiting-for-operation-mutex',flush=True)
    with mutex():yield
client.operation_mutex=waiting
delivery=api['runtime_delivery'](client)
print('client-ready',flush=True)
assert sys.stdin.readline().strip()=='prepare'
result=delivery.prepare(int(sys.argv[3]),'tmux')
print(json.dumps(result),flush=True)
'''
    def racing_remove(path):
        if path==directory and not removed:
            child.stdin.write('prepare\n');child.stdin.flush()
            assert select.select([child.stdout],[],[],10)[0], 'prepare child did not reach mutex'
            assert child.stdout.readline().strip()=='waiting-for-operation-mutex'
            assert child.poll() is None and not (directory/'intent.json').exists()
            remove(path);removed.append(True)
        else:remove(path)
    monkeypatch.setattr(Path,'rmdir',racing_remove)
    try:
        child=subprocess.Popen([sys.executable,'-c',script,str(ROOT/'bin/lib/runtime_client.py'),str(client.path),str(mid)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        children.append(child)
        assert select.select([child.stdout],[],[],10)[0], 'prepare child did not initialize'
        assert child.stdout.readline().strip()=='client-ready'
        delivery.reconcile_confirmed()
        assert removed==[True]
        output,errors=children[0].communicate(timeout=20)
        assert children[0].returncode==0,errors
        result=json.loads(output.splitlines()[-1])
        assert result['state']=='handoff'
        with delivery.message(mid) as path:
            intent=delivery.local(path,'delivery_intent')
            assert intent['message_id']==mid and intent['attempt_id']==result['attempt_id']
        assert path.exists()
    finally:
        for child in children:
            if child.poll() is None:child.terminate()
            child.communicate(timeout=10)
