"""Outside-sandbox native OS evidence, synthetic providers, dedicated tmux only."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from test_lifecycle_v3_red import api,server
from test_global_runtime_delivery import context


@pytest.mark.parametrize('kill_runner',[False,True])
def test_dedicated_wait_runner_keeps_native_roles_and_unknown_recovery(server,monkeypatch,capsys,kill_runner):
    client=context(server,window=False)
    worker=client.isolation/'native-provider-fixture.py'
    evidence=client.isolation/'native-evidence.json';stop=client.isolation/'native-stop'
    worker.write_text('#!'+sys.executable+'\n'+'''import json,os,runpy,sys,time
from pathlib import Path
api=runpy.run_path(os.environ['FIXTURE_RUNTIME_CLIENT'])
c=api['RuntimeClient'](Path(os.environ['AGENTSTACK_CLIENT_CONFIG']))
d=api['runtime_delivery'](c)
d.session_start({'session_id':'native_fixture','provider_pid':os.getpid(),'cwd':os.getcwd()},trusted_receipt=(os.environ['AGENTSTACK_LAUNCH_ID'],os.environ['AGENTSTACK_LAUNCH_NONCE']))
Path(os.environ['FIXTURE_EVIDENCE']).write_text(json.dumps({'pid':os.getpid(),'parent_pid':os.getppid(),'pgid':os.getpgrp(),'tty':os.isatty(0)}))
end=time.monotonic()+30
while not Path(os.environ['FIXTURE_STOP']).exists() and time.monotonic()<end:time.sleep(.05)
''')
    worker.chmod(0o700)
    monkeypatch.setenv('AGENTSTACK_CLAUDE_BIN',str(worker));monkeypatch.delenv('TMUX',raising=False);monkeypatch.delenv('TMUX_PANE',raising=False)
    monkeypatch.setenv('FIXTURE_RUNTIME_CLIENT',str(Path(__file__).resolve().parents[2]/'bin/lib/runtime_client.py'))
    monkeypatch.setenv('FIXTURE_EVIDENCE',str(evidence));monkeypatch.setenv('FIXTURE_STOP',str(stop))
    tmux=api['runtime_tmux_api']();path=tmux['global_socket'](Path(client.config['agentstack_home']),client.binding['expected_server_instance_id'])
    expected_parent=Path('/tmp').resolve()/('orrery-tmux-'+str(os.getuid()))
    assert path.parent==expected_parent and path.name.endswith('.sock')
    name=client.identity['name'];delivery=api['runtime_delivery'](client)
    def launch_snapshot():
        with client.operation_mutex():
            return delivery.launch()
    def wait_for(predicate):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            value=predicate()
            if value:return value
            time.sleep(.1)
        pytest.fail('native fixture deadline exceeded')
    try:
        delivery.start('claude',client.isolation,detached=True)
        wait_for(lambda:evidence.exists())
        data=json.loads(evidence.read_text());launch=launch_snapshot();runner=launch['execution']['runner']
        assert runner['pid']!=data['pid'] and runner['pid']==data['parent_pid']
        assert data['tty'] is True and data['pgid']==os.getpgid(runner['pid'])
        live=api['read_json'](client.runtime_dir/'live-sessions/native_fixture.json')
        assert live['pid']==data['pid'] and live['process_start']==api['process_start'](data['pid'])
        channel=tmux['Transport'](path)
        panes=tmux['panes'](channel,session=name)
        assert len(panes)==1 and panes[0]['session']==name and panes[0]['pid']==runner['pid']
        assert tmux['live_target'](live,client.config,launch,api=api) is not None
        assert delivery.status(launch['launch_id'])['runtime_ready'] is True
        if kill_runner:
            assert api['process_start'](runner['pid'])==runner['process_start']
            os.kill(runner['pid'],signal.SIGKILL)
            wait_for(lambda:api['process_start'](runner['pid'])=='absent')
            state=delivery.status(launch['launch_id'])
            assert state['operator_required'] and 'launch-resolve' in state['next_operations']
            assert launch_snapshot()['execution']['outcome']['state']=='pending'
            assert not api['read_json'](client.runtime_dir/'live-sessions/native_fixture.json')['ended']
        else:
            stop.touch()
            wait_for(lambda:launch_snapshot()['phase']=='ended')
            assert launch_snapshot()['execution']['outcome']=={'state':'exited','returncode':0}
            assert api['read_json'](client.runtime_dir/'live-sessions/native_fixture.json')['ended']
            assert client.inspect_owner()['retired_at'] is None  # standalone never retires
    finally:
        stop.touch(exist_ok=True)
        # The only teardown target is this exact fixture session/endpoint.
        assert path.parent==expected_parent
        subprocess.run(['tmux','-S',str(path),'kill-session','-t','='+name],capture_output=True,timeout=5)


def test_actual_spawn_and_resume_shells_use_the_same_canonical_transport(server,monkeypatch):
    root=Path(__file__).resolve().parents[2]
    parent=context(server,window=False)
    parent.call('set_contact_policy',{'policy':'open'})
    cwd=parent.isolation/'cwd with space';cwd.mkdir(mode=0o700)
    prepared=parent.prepare_child({'child_client_name':'shell_child','name':'ShellChild',
        'prepare_only':True,'program':'claude-code','model':'fixture','workdir':str(cwd)})
    child=api['RuntimeClient'](prepared['client_config'])
    api['runtime_delivery'](child).instruction_configure('claude')
    evidence=child.isolation/'shell-evidence.json';stop=child.isolation/'shell-stop'
    worker=child.isolation/'shell-provider.py'
    worker.write_text('#!'+sys.executable+'\n'+'''import json,os,runpy,sys,time
from pathlib import Path
api=runpy.run_path(os.environ['FIXTURE_RUNTIME_CLIENT'])
c=api['RuntimeClient'](Path(os.environ['AGENTSTACK_CLIENT_CONFIG']))
api['runtime_delivery'](c).session_start({'session_id':'shell_sdk','provider_pid':os.getpid(),'cwd':os.getcwd()},trusted_receipt=(os.environ['AGENTSTACK_LAUNCH_ID'],os.environ['AGENTSTACK_LAUNCH_NONCE']))
Path(os.environ['FIXTURE_EVIDENCE']).write_text(json.dumps({'pid':os.getpid(),'argv':sys.argv,'cwd':os.getcwd(),'config':str(c.path),'pgid':os.getpgrp(),'tty':os.isatty(0)}))
end=time.monotonic()+25
while not Path(os.environ['FIXTURE_STOP']).exists() and time.monotonic()<end:time.sleep(.05)
''');worker.chmod(0o700)
    env={k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE')}
    env.update(AGENTSTACK_CLIENT_CONFIG=str(parent.path),AGENTSTACK_PYTHON=sys.executable,
        AGENTSTACK_CLAUDE_BIN=str(worker),FIXTURE_RUNTIME_CLIENT=str(root/'bin/lib/runtime_client.py'),
        FIXTURE_EVIDENCE=str(evidence),FIXTURE_STOP=str(stop))
    tmux=api['runtime_tmux_api']();path=tmux['global_socket'](Path(child.config['agentstack_home']),child.binding['expected_server_instance_id'])
    expected_parent=Path('/tmp').resolve()/('orrery-tmux-'+str(os.getuid()))
    name=child.identity['name'];assert path.parent==expected_parent
    def wait_for(predicate):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.1)
        pytest.fail('shell lifecycle deadline exceeded')
    try:
        for operation in ('spawn','resume'):
            stop.unlink(missing_ok=True);evidence.unlink(missing_ok=True)
            command=(["/bin/bash",str(root/'hooks/spawn_child.sh')] if operation=='spawn' else
                [sys.executable,str(root/'bin/agentstack-resume')])
            result=subprocess.run([*command,'--child-client-name','shell_child'],env=env,cwd=cwd,capture_output=True,text=True,timeout=20)
            assert result.returncode==0,(result.stdout,result.stderr)
            admitted=json.loads(result.stdout)
            assert admitted['phase']=='gated' and admitted['runtime_ready'] is False
            wait_for(evidence.exists)
            sample=json.loads(evidence.read_text())
            assert sample['cwd']==str(cwd) and sample['config']==str(child.path)
            assert ('--resume' in sample['argv'])==(operation=='resume')
            if operation=='resume':assert sample['argv'][sample['argv'].index('--resume')+1]=='shell_sdk'
            current=api['RuntimeClient'](child.path);delivery=api['runtime_delivery'](current);row=delivery.launch()
            assert row['launch_kind']==('startup' if operation=='spawn' else 'resume')
            channel=tmux['Transport'](path);panes=tmux['panes'](channel,session=name)
            assert len(panes)==1 and panes[0]['session']==name
            assert panes[0]['pid']==row['execution']['runner']['pid']!=sample['pid']
            assert sample['tty'] is True and sample['pgid']==os.getpgid(panes[0]['pid'])
            assert delivery.status(row['launch_id'])['runtime_ready']
            stop.touch();wait_for(lambda:delivery.launch()['phase']=='ended')
            assert delivery.launch()['termination']['phase']=='ended'
            assert current.inspect_owner()['retired_at'] is not None
    finally:
        stop.touch(exist_ok=True)
        assert path.parent==expected_parent
        subprocess.run(['tmux','-S',str(path),'kill-session','-t','='+name],capture_output=True,timeout=5)
        remaining=subprocess.run(['tmux','-S',str(path),'has-session','-t','='+name],capture_output=True,timeout=5)
        assert remaining.returncode!=0
