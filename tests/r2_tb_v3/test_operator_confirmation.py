"""Human wait is outside C/deadline; stdout remains the validated JSON wire."""
import io
import json
import sys
import threading
import time

import pytest
from test_lifecycle_v3_red import api,server,ending_child
from test_recovery_v3_red import target_cli


@pytest.mark.parametrize('changed',[False,True])
def test_actual_operator_replay_rechecks_after_human_wait(ending_child,server,monkeypatch,capsys,changed):
    parent,child,delivery,payload=ending_child
    row=delivery.launch();row['execution']={'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'exited','returncode':0}}
    delivery.save(child.outputs['launch'],row,'launch_intent')
    target=target_cli(child,parent,server,monkeypatch,capsys,operator=True)
    writes=[]
    class HumanInput(io.StringIO):
        def isatty(self):return True
        def readline(self,*args):
            def writer():
                with child.operation_mutex():
                    if changed:
                        saved=delivery.launch();saved['error']='Fixture update while operator reviews'
                        delivery.save(child.outputs['launch'],saved,'launch_intent')
                    writes.append(True)
            thread=threading.Thread(target=writer)
            thread.start();thread.join(timeout=3)
            assert not thread.is_alive(),'human prompt retained the operation mutex'
            if not changed:time.sleep(5.1)
            return super().readline(*args)
    monkeypatch.setattr(sys,'stdin',HumanInput('yes\n'))
    code=api['main'](['replay-end','--agent-id',str(child.identity['agent_id']),
        '--operator','--runtime-mode','global','--server-config',str(server['config_path']),
        '--confirm-state-digest',target['state_sha256'],'--expected-intent-sha256','null'])
    streams=capsys.readouterr();reply=json.loads(streams.out)
    assert writes==[True]
    assert 'Finish this saved terminal report and cleanup?' in streams.err and '/compact' not in streams.err
    if changed:
        assert code==2 and reply['reason']=='RESOLUTION_DIGEST_MISMATCH' and reply['effects']=='none'
        assert child.inspect_owner()['retired_at'] is None
        assert not child.outputs['mutation'].exists()
    else:
        assert code==0 and reply['lifecycle']['phase']=='ended'
        assert child.inspect_owner()['retired_at'] is not None
