"""Notification lifecycle with an isolated Mail home and private tmux socket."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest
from _installed_client_payload import copy_client_library

ROOT=Path(__file__).resolve().parents[1]
SOURCE=(ROOT/'hooks/watch_agent_mail_signals.sh').read_text()


def function(name):
    match=re.search(rf'^{name}\(\) \{{\n.*?^\}}\n',SOURCE,re.M|re.S)
    assert match,name
    return match.group()


@pytest.fixture
def watcher(tmp_path):
    tmux=shutil.which('tmux')
    if tmux is None:pytest.skip('tmux is required')
    home=tmp_path/'home';install=home/'.agentstack';runtime=install/'runtime'
    agents=install/'mail/signals/projects/fixture/agents';agents.mkdir(parents=True)
    copy_client_library(install)
    runner=install/'hooks/watch-once.sh'
    runner.write_text(SOURCE[:SOURCE.index('if command -v fswatch &>/dev/null; then')]
                      +'\nprocess_existing_signals\nwait\n')
    commands=tmp_path/'bin';commands.mkdir();failure=tmp_path/'failure'
    wrapper=commands/'tmux'
    wrapper.write_text('#!'+shutil.which('python3')+'\n'+f'''
import os,sys,time
from pathlib import Path
failure=Path({str(failure)!r})
if 'list-sessions' in sys.argv and failure.exists():
    if failure.read_text()=='timeout':time.sleep(7)
    print('permission denied',file=sys.stderr);sys.exit(1)
os.execv({tmux!r},[{tmux!r},*sys.argv[1:]])
''');wrapper.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix='orws-',dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as socket_root:
        env={**os.environ,'HOME':str(home),'PATH':str(commands)+':'+os.environ['PATH'],
             'TMUX_TMPDIR':str(Path(socket_root).resolve()),'AGENTSTACK_HOME':str(install),
             'AGENTSTACK_MAIL_HOME':str(install/'mail'),'AGENTSTACK_SIGNALS_DIR':str(agents.parents[2]),
             'AGENTSTACK_RUNTIME_DIR':str(runtime),'AGENTSTACK_RUNTIME_MODE':'legacy',
             'AGENTSTACK_PERSISTENT_DELIVER':str(tmp_path/'no-headless-helper'),'TMUX_TIMEOUT':'5'}
        env.pop('TMUX',None);env.pop('TMUX_PANE',None)
        def call(*args):return subprocess.run([tmux,*args],env=env,capture_output=True,text=True,timeout=10,check=True)
        def signal(agent,number,legacy=False):
            path=agents/(agent+'.signal') if legacy else agents/agent/(str(number)+'.signal')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps({'message':{'id':number,'from':'FixtureSender','subject':'fixture'}}))
            return path
        def run():
            result=subprocess.run(['/bin/bash',str(runner)],env=env,capture_output=True,text=True,timeout=60)
            assert result.returncode==0,result.stdout+result.stderr
            return result
        try:
            call('new-session','-d','-s','Control','sleep 300')
            call('new-session','-d','-s','AbsentLonger','sleep 300')
            yield signal,run,runtime,failure,call
        finally:
            subprocess.run([tmux,'kill-server'],env=env,capture_output=True,timeout=10)


def test_absent_signals_are_discarded_once_and_live_delivery_continues(watcher):
    signal,run,runtime,failure,tmux=watcher
    paths=[signal('Absent',number) for number in range(1,25)]
    tmux('new-session','-d','-s','Living',"printf 'Claude\\n'; cat")
    live=signal('Living',100)
    first=run();state=json.loads((runtime/'notify-state.json').read_text())
    assert all(not path.exists() for path in paths)
    assert not live.exists(),(state.get('Living:100'),first.stdout,first.stderr)
    assert all(state[f'Absent:{number}']['terminal'] is True for number in range(1,25))
    assert state['Living:100']['last_result']=='success'
    assert first.stdout.count('Signal:')==25
    # Even an old saved result without the terminal marker is consumed once.
    old=signal('Absent',200);state['Absent:200']={'last_result':'session_not_found','last_attempt_epoch':0}
    (runtime/'notify-state.json').write_text(json.dumps(state))
    assert run().stdout.count('Signal:')==1 and not old.exists()
    assert 'Signal:' not in run().stdout
    assert not list((runtime/'notify-locks').glob('*.lock'))


@pytest.mark.parametrize('mode',['permission','timeout'])
def test_uncertain_session_lookup_retains_signal_and_can_retry(watcher,mode):
    signal,run,runtime,failure,tmux=watcher
    path=signal('Absent',1);failure.write_text(mode)
    run();state=json.loads((runtime/'notify-state.json').read_text())
    assert path.exists() and state['Absent:1']['last_result']=='session_lookup_failed'
    assert not state['Absent:1'].get('terminal')
    state['Absent:1']['last_attempt_epoch']=0
    (runtime/'notify-state.json').write_text(json.dumps(state));failure.unlink()
    run();assert not path.exists()


def test_new_legacy_message_is_not_blocked_by_a_previous_absent_result(watcher):
    signal,run,runtime,failure,tmux=watcher
    path=signal('Absent',1,legacy=True);run();assert not path.exists()
    path=signal('Absent',2,legacy=True);run();assert not path.exists()
    state=json.loads((runtime/'notify-state.json').read_text())
    assert state['Absent:1']['terminal'] and state['Absent:2']['terminal']


def test_headless_manifest_is_retained_when_its_helper_is_missing(watcher):
    signal,run,runtime,failure,tmux=watcher
    manifest=runtime/'persistent/Absent.json';manifest.parent.mkdir(parents=True)
    manifest.write_text('{}');path=signal('Absent',1)
    run();state=json.loads((runtime/'notify-state.json').read_text())
    assert path.exists() and state['Absent:1']['last_result']=='headless_reply_failed'
    assert not state['Absent:1'].get('terminal')


def test_refreshed_legacy_signal_is_preserved(tmp_path):
    path=tmp_path/'legacy.signal';path.write_text('first')
    st=path.stat();identity=f'{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}'
    path.write_text('a new notification')
    script=function('discard_absent_signal')+'\ndiscard_absent_signal "$1" "$2"\n'
    result=subprocess.run(['/bin/bash','-c',script,'fixture',str(path),identity],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert path.read_text()=='a new notification'


def test_guard_file_count_is_bounded_and_leases_are_released(tmp_path):
    script='set -euo pipefail\n'+function('acquire_delivery_lease')+function('release_delivery_lease')+'''
LEASE_DIR="$1";LEASE_TTL=330;mkdir -p "$LEASE_DIR"
for number in $(seq 1 270); do
 owner=$(acquire_delivery_lease FixtureAgent "$number")
 release_delivery_lease FixtureAgent "$number" "$owner"
done
'''
    result=subprocess.run(['/bin/bash','-c',script,'fixture',str(tmp_path/'leases')],
                          capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    files=list((tmp_path/'leases').iterdir())
    assert 0<len(files)<=256
    assert all(re.fullmatch(r'bucket-[0-9a-f]{2}\.guard',p.name) for p in files)


@pytest.mark.parametrize('append',[False,True])
def test_log_copy_truncate_keeps_service_descriptors_and_bounds_backup(tmp_path,append):
    logfile=tmp_path/'mail-watcher.log';logfile.write_bytes(b'x'*(6*1024*1024))
    script='STATE_DIR="$1"\n'+function('bound_watcher_log')+'''\n
bound_watcher_log
printf 'stdout after rotation\n'
printf 'stderr after rotation\n' >&2
'''
    with logfile.open('ab' if append else 'r+b') as output:
        output.seek(0,os.SEEK_END)
        result=subprocess.run(['/bin/bash','-c',script,'fixture',str(tmp_path)],
                              stdout=output,stderr=output,timeout=10)
    assert result.returncode==0
    assert logfile.read_text()=='stdout after rotation\nstderr after rotation\n'
    assert logfile.with_suffix('.log.1').stat().st_size==5*1024*1024
