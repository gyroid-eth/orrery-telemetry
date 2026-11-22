"""Shared live_target, using only an external endpoint/OS fixture boundary."""
import json
from pathlib import Path
import runpy
import subprocess
import types
import os

import pytest

ROOT=Path(__file__).resolve().parents[2]
TMUX=runpy.run_path(str(ROOT/'bin/lib/runtime_tmux.py'))
CLIENT=runpy.run_path(str(ROOT/'bin/lib/runtime_client.py'))
SPEC=json.loads((ROOT/'tests/fixtures/r2-tb-lifecycle-v3.json').read_text())


@pytest.mark.parametrize('case',SPEC['role_cases'],ids=lambda c:c['id'])
def test_F4_07_shared_reader_separates_runner_and_provider(monkeypatch,case):
    live={'pid':400,'process_start':'provider-birth','pane_id':'%1','session_id':'sdk',
          'launch_id':'launch','receipt_nonce':'nonce'}
    launch={'launch_id':'launch','receipt_nonce':'nonce','tmux_transport':'global-instance',
            'execution':{'runner':{'pid':310,'process_start':'runner-birth'},'outcome':{'state':'pending','returncode':None}}}
    context={'agentstack_home':'/synthetic/client-home','expected_server_instance_id':'fixture-instance',
             'identity':{'name':'Alpha'}}
    class Peer:
        def command(self,*args,**kwargs):
            out='310' if args[-1]=='#{pane_pid}' else '300'
            return subprocess.CompletedProcess(args,0,out+'\n','')
    peer=Peer()
    scope=TMUX['live_target'].__globals__
    monkeypatch.setitem(scope,'endpoint_candidates',lambda *a:[('global-instance',peer)])
    monkeypatch.setitem(scope,'panes',lambda *a,**kw:[{'pid':999 if case['id']=='different-pane' else 310,'pane_id':'%1','session':'Alpha','session_id':'$1'}])
    def birth(pid,**kw):
        role='runner' if pid==310 else 'provider'
        state=case[role]
        return ('absent' if state=='absent' else 'reused-birth' if state=='reused' else role+'-birth')
    def ps(argv, **kw):
        assert argv[0]=='ps'
        parent=310 if case['valid_parentage'] else 999
        include_uid=any('uid' in str(a) for a in argv)
        uid=(' '+str(os.getuid())) if include_uid else ''
        text='310 1'+uid+' python\n400 '+str(parent)+uid+' claude\n'
        return subprocess.CompletedProcess(argv,0,text,'')
    monkeypatch.setitem(scope,'subprocess',types.SimpleNamespace(run=ps))
    process_api=TMUX['module_api']('process_observation')
    monkeypatch.setitem(process_api['process_start'].__globals__,'subprocess',types.SimpleNamespace(run=ps))
    api={**CLIENT,'process_start':birth}
    found=TMUX['live_target'](live,context,launch,api=api)
    if case['purpose']=='runtime':
        assert found is not None, 'a live provider below a live pane runner is the same launch'
    else:
        assert found is None, 'invalid/ended provider must not become a runtime target'
