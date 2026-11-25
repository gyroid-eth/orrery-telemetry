"""Real delivery and shared target proof with private tmux, no provider input."""
import json
import runpy
import subprocess
from pathlib import Path
import pytest
from test_runtime_target_http import target, server, TMUX

ROOT=Path(__file__).resolve().parents[1]
DELIVERY=runpy.run_path(str(ROOT/'bin/lib/runtime_delivery.py'))

@pytest.mark.parametrize('target',[True],indirect=True)
@pytest.mark.parametrize('barrier',['prepare','load-buffer','paste-buffer',None])
def test_actual_delivery_worker_rechecks_after_each_handoff_barrier(target,monkeypatch,barrier):
    subprocess.run([*target['command'],'rename-session','-t',target['session'],'orrery-fixture'],env=target['env'],check=True,capture_output=True)
    live={**target['live'],'launch_id':'fixture','receipt_nonce':'fixture-proof'}
    launch=dict(phase='running',launch_id='fixture',receipt_nonce='fixture-proof',tmux_transport='legacy-default')
    state={'start':'fixture-start'};effects=[];callbacks=[]
    original=TMUX['Transport'].command
    runtime_api={**target['targets'].api,'pane_evidence':lambda *a,**kw:('live',None,''),
        'runtime_tmux_api':lambda:TMUX,'process_start':lambda *a,**kw:state['start']}
    worker=object.__new__(DELIVERY['RuntimeDelivery'])
    worker.api=runtime_api;worker.client=target['client']
    worker.launch=lambda:launch;worker.live_rows=lambda _:[(None,live)]
    def prepare(*args,**kwargs):
        assert kwargs['admissible'] is True
        if barrier=='prepare':state['start']='absent'
        return dict(state='handoff',attempt_id='fixture-attempt',notification='fixture mail')
    worker.prepare=prepare
    worker.callback=lambda *args,**kw:callbacks.append(kw) or kw
    def command(self,*args,**kw):
        result=original(self,*args,**kw)
        if args[0] in ('load-buffer','paste-buffer','send-keys'):
            effects.append(args[0])
            if args[0]==barrier:state['start']='absent'
        return result
    monkeypatch.setattr(TMUX['Transport'],'command',command)
    result=worker.deliver_tmux(99)
    expected={
        'prepare': [],
        'load-buffer': ['load-buffer'],
        'paste-buffer': ['load-buffer','paste-buffer'],
        None: ['load-buffer','paste-buffer','send-keys'],
    }[barrier]
    assert effects==expected
    assert len(callbacks)==1
    assert result==dict(confirmed=barrier is None,known_failure=barrier in ('prepare','load-buffer'))
    if barrier is None:
        assert [json.loads(line) for line in target['received'].read_text().splitlines()]==['fixture mail']
    else:
        assert not target['received'].exists()
