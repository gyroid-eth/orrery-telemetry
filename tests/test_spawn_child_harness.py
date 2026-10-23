"""Real standard launcher, synthetic preregistration/pane; no provider or Mail."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_spawn_child_embed_task import _fake_launch_env, _codex_handoff, _executable, SPAWN, ROOT


def fixture(tmp_path, *, gone=False):
    env,work=_fake_launch_env(tmp_path,codex=False)
    token=_codex_handoff(tmp_path,'HarnessChild')
    binding=Path(str(token)+'.binding.json')
    data=json.loads(binding.read_text());data['program']='dsh';binding.write_text(json.dumps(data));binding.chmod(0o600)
    proxy=tmp_path/'product-proxy.sh';_executable(proxy,'#!/bin/sh\nexit 0\n')
    env['AGENTSTACK_MCP_PROXY']=str(proxy)
    fake=tmp_path/'harness-fixture.py'
    generator=tmp_path/'generator.py'
    generator.write_text("import argparse,json,os\np=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--out');a=p.parse_args();v=json.load(open(a.input));json.dump({'argv':['/usr/bin/true'],'env':{},'cwd':v['cwd']},open(a.out,'w'));os.chmod(a.out,0o600)\n")
    fake.write_text('import sys,runpy\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1]);script=Path(sys.argv.pop(1))/"harness_child.py"\nif sys.argv[1]=="validate":raise SystemExit(0) # Synthetic static admission only.\nm=runpy.run_path(str(script));g=m["prepare"].__globals__;g["selection"]=lambda *a:{"python":sys.executable};g["config_generator"]=lambda *a:Path('+repr(str(generator))+')\nraise SystemExit(m["main"]())\n')
    wrapper=tmp_path/'helper-wrapper.py'
    wrapper.write_text('import os,sys\nos.execv(sys.executable,[sys.executable,'+repr(str(fake))+','+repr(str(ROOT/'hooks'))+',*sys.argv[1:]])\n')
    env['AGENTSTACK_HARNESS_CHILD_HELPER']=str(wrapper)
    env['AGENTSTACK_PYTHON']=sys.executable
    manifest=tmp_path/'manifest.json';manifest.write_text('{}')
    env['AGENTSTACK_MANAGED_SPAWN_CONFIG']=str(manifest)
    tmux=tmp_path/'bin/tmux';text=tmux.read_text()
    if gone:
        text=text.replace('has-session) [[ -f "$FAKE_TMUX_ALIVE" ]]', "has-session) printf \"can't find session: HarnessChild\\n\" >&2; exit 1")
    tmux.write_text(text)
    for program in ('claude','codex'):
        _executable(tmp_path/'bin'/program,'#!/bin/sh\nprintf called > '+str(tmp_path/'forbidden-native-probe')+'\nexit 91\n')
    return env,work,token


@pytest.mark.parametrize('standalone',[False,True])
@pytest.mark.parametrize('backend',['strata','strata-epyc'])
def test_standard_preregistered_harness_reuses_binding_proxy_and_real_workdir(tmp_path,standalone,backend):
    env,work,token=fixture(tmp_path)
    helper=tmp_path/'harness-fixture.py'
    helper.write_text(helper.read_text().replace('{"python":sys.executable}',
        '{"python":sys.executable,"connection_kind":"strata"}'))
    env['AGENTSTACK_CHILD_RESUME_RETENTION_DAYS']='0'
    args=['/bin/bash',str(SPAWN),'--pre-registered','HarnessChild','--child-token-file',str(token),
          '--harness','dsh','--backend',backend,'--model','formal-model']
    if standalone:args+=['--standalone','',str(work)]
    else:args+=['--embed-task','Literal $(must-not-run) `task`',str(work)]
    result=subprocess.run(args,env=env,cwd=ROOT,text=True,capture_output=True,timeout=25)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='HarnessChild'
    state=json.loads((Path(env['AGENTSTACK_RUNTIME_DIR'])/'child-agents/HarnessChild.json').read_text())
    assert state['program']=='dsh' and state['provider']=='dsh'
    h=json.loads((Path(state['harness_state_dir'])/'input.json').read_text())
    assert h['backend_id']==state['harness_backend']==backend
    assert h['backend_kind']==state['harness_backend_kind']=='strata'
    assert h['cwd']==str(work.resolve())
    assert h['parent_agent']==(None if standalone else 'ParentAgent')
    prompt=Path(h['prompt_file']).read_text()
    assert (prompt=='' if standalone else 'Literal $(must-not-run) `task`' in prompt)
    assert Path(h['instructions_file']).is_file()
    proxy_config=json.loads(Path(h['mcp_config']).read_text())
    assert {row['env']['AGENTSTACK_PROXY_PROGRAM'] for row in proxy_config['mcpServers'].values()}=={'dsh'}
    assert not (tmp_path/'forbidden-native-probe').exists()
    assert not token.exists() and not Path(str(token)+'.binding.json').exists()
    assert not (Path(env['AGENTSTACK_RUNTIME_DIR'])/'child-agents/.HarnessChild.registration-pending.json').exists()
    assert '/usr/bin/true' in Path(env['FAKE_TMUX_LOG']).read_text()
    assert 'AGENTSTACK_CHILD_RESUME_RETENTION_DAYS=0' in Path(env['FAKE_TMUX_LOG']).read_text()


def test_failed_harness_start_keeps_identity_and_handoff_when_cleanup_unconfirmed(tmp_path):
    env,work,token=fixture(tmp_path,gone=True)
    result=subprocess.run(['/bin/bash',str(SPAWN),'--pre-registered','HarnessChild','--child-token-file',str(token),
        '--harness','dsh','--backend','strata','--model','formal-model','--standalone','',str(work)],
        env=env,cwd=ROOT,text=True,capture_output=True,timeout=25)
    assert result.returncode!=0
    assert token.exists() and Path(str(token)+'.binding.json').exists()
    state=Path(env['AGENTSTACK_RUNTIME_DIR'])/'child-agents/HarnessChild.json'
    assert state.exists() and json.loads(state.read_text())['harness_state_dir']
    assert 'cleanup unconfirmed' in result.stderr
    assert 'kill-session' not in Path(env['FAKE_TMUX_LOG']).read_text()


@pytest.mark.parametrize('flags',[
 ['--harness','dsh'], ['--backend','strata'], ['--harness','unknown','--backend','strata'],
 ['--harness','dsh','--backend','strata','--codex'],
 ['--harness','dsh','--backend','strata','--tools','browser'],
])
def test_bad_harness_flags_stop_before_any_registration_or_tmux(tmp_path,flags):
    env,work=_fake_launch_env(tmp_path,codex=False)
    result=subprocess.run(['/bin/bash',str(SPAWN),*flags,'--model','formal-model','--unsafe-no-resources','task',str(work)],
        env=env,cwd=ROOT,text=True,capture_output=True,timeout=10)
    assert result.returncode!=0
    assert not Path(env['FAKE_TMUX_LOG']).exists()


def test_harness_does_not_silently_drop_installed_default_tools(tmp_path):
    env,work,token=fixture(tmp_path)
    env['AGENTSTACK_CHILD_DEFAULT_TOOLS']='browser'
    result=subprocess.run(['/bin/bash',str(SPAWN),'--pre-registered','HarnessChild',
        '--child-token-file',str(token),'--harness','dsh','--backend','strata',
        '--model','formal-model','--standalone','',str(work)],env=env,cwd=ROOT,
        text=True,capture_output=True,timeout=10)
    assert result.returncode!=0 and 'native_default_tools_unsupported' in result.stderr
    assert token.exists() and not Path(env['FAKE_TMUX_LOG']).exists()


@pytest.mark.parametrize('stop_fails,start_pending', [(False, False), (True, False), (False, True)])
def test_interrupted_native_stops_before_remote_cleanup_and_keeps_unknown(tmp_path, stop_fails, start_pending):
    import shlex
    text = SPAWN.read_text()
    start = text.index('\ncleanup_harness_failure() {') + 1
    function = text[start:text.index('\n}\n', start) + 3]
    hooks = tmp_path / 'hooks'; hooks.mkdir()
    trace = tmp_path / 'trace'
    _executable(hooks / 'cleanup-child-agent.sh', '#!/bin/bash\nprintf "rpc\\n" >> "$TRACE"\nexit 1\n')
    native = subprocess.Popen(['/bin/sleep', '30'])
    script = function + '''
CHILD_NAME=HarnessChild
SPAWN_TRAP_SESSION=HarnessChild
PRE_REGISTERED_ADOPTION_PENDING=false
cleanup_worktree() { printf 'worktree\n' >> "$TRACE"; }
tmux() {
 case "$1" in
 kill-session)
   printf 'stop\n' >> "$TRACE"
   [[ "$STOP_FAILS" == false ]] || return 1
   kill "$NATIVE_PID" || return 1
   return 0 ;;
 has-session) [[ -f "$TRACE" ]] && return 1; return 0 ;;
 esac
}
trap cleanup_harness_failure EXIT
trap 'exit 143' TERM
kill -TERM $$
'''
    environment = {**os.environ, 'TRACE': str(trace), 'HOOKS_DIR': str(hooks),
                   'NATIVE_PID': str(native.pid), 'STOP_FAILS': str(stop_fails).lower(),
                   'HARNESS_START_PENDING': str(start_pending).lower()}
    try:
        result = subprocess.run(['/bin/bash', '-c', script], env=environment,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 143
        if start_pending:
            assert not trace.exists()
            assert native.poll() is None
            assert 'start/stop unconfirmed' in result.stderr
        elif stop_fails:
            assert trace.read_text().splitlines() == ['stop']
            assert native.poll() is None
            assert 'stop unconfirmed' in result.stderr
        else:
            assert native.wait(timeout=5) != 0
            assert trace.read_text().splitlines() == ['stop', 'rpc']
        assert not trace.exists() or 'worktree' not in trace.read_text()
    finally:
        if native.poll() is None:
            native.terminate()
        native.wait(timeout=5)


def test_native_early_exit_is_not_ready_while_cleanup_is_still_running(tmp_path):
    import time
    env, work, token = fixture(tmp_path)
    generator = tmp_path / 'generator.py'
    generator.write_text(generator.read_text().replace("['/usr/bin/true']", "['/bin/sh','-c','exit 17']"))
    product = tmp_path / 'pane-product'; (product / 'hooks').mkdir(parents=True)
    cleanup_started = tmp_path / 'cleanup-started'; cleanup_done = tmp_path / 'cleanup-done'
    cleanup_release = tmp_path / 'cleanup-release'
    _executable(product / 'hooks/cleanup-child-agent.sh',
        '#!/bin/bash\ntouch ' + str(cleanup_started) + '\n'
        'for ((i=0; i<3000; i++)); do [[ -f \"' + str(cleanup_release) + '\" ]] && break; /bin/sleep 0.01; done\n'
        'touch ' + str(cleanup_done) + '\nexit 1\n')
    helper = tmp_path / 'harness-fixture.py'
    helper.write_text(helper.read_text().replace('raise SystemExit(m["main"]())',
        'if sys.argv[1]=="command":g["ROOT"]=Path(' + repr(str(product)) + ')\nraise SystemExit(m["main"]())'))
    tmux = tmp_path / 'bin/tmux'; text = tmux.read_text()
    # Execute the real generated pane command, including its early-exit marker
    # and deliberately slow cleanup; this remains a fake tmux, not a server.
    text = text.replace('case "${1:-}" in\n',
        'if [[ "${1:-}" == new-session ]]; then\n'
        ' /bin/bash -c "${!#}" >/dev/null 2>&1 &\n'
        ' for ((i=0; i<200; i++)); do [[ -e ' + str(cleanup_started) + ' ]] && break; /bin/sleep 0.01; done\n'
        'fi\ncase "${1:-}" in\n', 1)
    tmux.write_text(text)
    try:
        result = subprocess.run(['/bin/bash', str(SPAWN), '--pre-registered', 'HarnessChild',
            '--child-token-file', str(token), '--harness', 'dsh', '--backend', 'strata',
            '--model', 'formal-model', '--standalone', '', str(work)],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=15)
        assert result.returncode != 0 and 'native exited during startup' in result.stderr
        state = json.loads((Path(env['AGENTSTACK_RUNTIME_DIR']) / 'child-agents/HarnessChild.json').read_text())
        assert (Path(state['harness_state_dir']) / 'native-exit.status').read_text().strip() == '17'
        assert cleanup_started.exists() and not cleanup_done.exists()
        assert token.exists()
    finally:
        cleanup_release.touch()
        deadline = time.monotonic() + 5
        while not cleanup_done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert cleanup_done.exists()


@pytest.mark.parametrize('case', ['connection-denied', 'session-absent', 'native-ended', 'other-error'])
def test_failure_cleanup_distinguishes_absent_session_from_unknown(tmp_path, case):
    import shlex
    text = SPAWN.read_text()
    start = text.index('\ncleanup_harness_failure() {') + 1
    function = text[start:text.index('\n}\n', start) + 3]
    hooks = tmp_path / 'hooks'; hooks.mkdir()
    trace = tmp_path / 'trace'
    _executable(hooks / 'cleanup-child-agent.sh', '#!/bin/bash\nprintf "rpc\\n" >> "$TRACE"\n')
    state = tmp_path / 'native-state'; state.mkdir()
    if case == 'native-ended':
        (state / 'native-exit.status').write_text('17\n')
    script = function + '''
CHILD_NAME=HarnessChild
SPAWN_TRAP_SESSION=HarnessChild
HARNESS_START_PENDING=false
PRE_REGISTERED_ADOPTION_PENDING=true
finish_child_registration() { printf 'finish\n' >> "$TRACE"; }
cleanup_worktree() { printf 'worktree\n' >> "$TRACE"; }
tmux() {
 if [[ "$1" == has-session ]]; then
   if [[ "$CASE" == session-absent ]]; then printf "can't find session: HarnessChild\n" >&2
   else printf 'error connecting to /fixture/tmux.sock (Permission denied)\n' >&2; fi
   [[ "$CASE" != other-error ]] || return 2
   return 1
 fi
 printf 'unexpected kill\n' >> "$TRACE"; return 1
}
cleanup_harness_failure
'''
    result = subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True,
        timeout=10, env={**os.environ, 'TRACE': str(trace), 'HOOKS_DIR': str(hooks),
                         'CASE': case, 'HARNESS_STATE': str(state)})
    if case in ('session-absent', 'native-ended'):
        assert result.returncode == 0
        assert trace.read_text().splitlines() == ['finish', 'rpc', 'worktree']
    else:
        assert result.returncode != 0
        assert not trace.exists()
        assert 'stop unconfirmed' in result.stderr
