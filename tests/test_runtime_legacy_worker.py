"""Real owner/projection/phase consumer; fake P/Q/exec only, not native proof."""
from copy import deepcopy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin/lib'))
from local_model_auth import LocalModelError
from runtime_intent_store import read_owner, write_owner
from test_runtime_legacy_auth_binding import bound, configure_fixture, context_fixture, TOKEN

STAMP = 'Sat Oct 17 12:00:00 2026'


class Transferred(BaseException): pass


class Process:
    def __init__(self): self.current = 11; self.parent_pid = 1; self.births = {11: STAMP, 12: STAMP}
    def pid(self): return self.current
    def parent(self): return self.parent_pid
    def start(self, pid): return self.births.get(pid, 'absent')


def build_case(bound, monkeypatch):
    b = bound; frame = read_owner(b.store.owner); frame.pop('local_execution'); write_owner(b.store.owner, frame)
    path = Path(b.intent['legacy_context']['install_root']) / 'bin/lib/runtime_legacy_worker.py'
    assert path.is_file(), 'PREVIEW_PQ_CONSUMER_MISSING'
    api = runpy.run_path(str(path)); process = Process(); captures = []; spawned = []
    assert callable(api.get('run_preview')) and callable(api.get('execute_provider'))
    chains = 0
    def observe(argv, **kw):
        nonlocal chains
        if argv[:2] == ['ps', '-p']:
            stamp = process.start(int(argv[2]))
            if stamp == 'absent': return subprocess.CompletedProcess(argv, 1, '', '')
            if stamp is None: return subprocess.CompletedProcess(argv, 1, '', 'fixture denied')
            if kw.get('env', {}).get('TZ') == 'UTC0': stamp = stamp.replace('12:00:', '03:00:')
            if state.mutation == 'older-provider' and int(argv[2]) == 12 and kw.get('env', {}).get('TZ') == 'UTC0':
                stamp = 'Sat Oct 17 02:59:59 2026'
            return subprocess.CompletedProcess(argv, 0, stamp + '\n', '')
        assert argv == ['ps', '-axo', 'pid=,ppid=,uid=']
        chains += 1
        uid = os.getuid() + (state.mutation == 'different-uid')
        parent = 99 if state.mutation == 'reparent' and chains > 1 else process.parent_pid
        return subprocess.CompletedProcess(argv, 0, f'12 {parent} {uid}\n11 1 {os.getuid()}\n', '')
    for key in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'PARENT_SECRET_CANARY'):
        monkeypatch.setenv(key, 'fixture-ambient-never-inherit')
    monkeypatch.setenv('TZ', 'UTC0')
    state = SimpleNamespace(bound=b, api=api, process=process, captures=captures, spawned=spawned,
        mutation=None, returncode=7, waited=False, wait_count=0, observe=observe)
    def execve(binary, argv, env):
        row = read_owner(b.store.owner)['local_execution']
        assert row['execution']['provider'] == {'pid': 12, 'process_start': STAMP}
        assert row['execution']['outcome'] == {'state': 'pending', 'returncode': None}
        captures.append((binary, argv, env, deepcopy(row)))
        if state.mutation == 'exec-error': raise OSError('fixture exec failed')
        raise Transferred()
    def popen(argv, *, env, cwd, close_fds):
        row = read_owner(b.store.owner)['local_execution']
        assert row['phase'] == 'exec-unknown' and row['execution']['provider'] is None
        assert row['execution']['outcome'] == {'state': 'unknown', 'returncode': None}
        assert close_fds is True and cwd == b.intent['cwd']
        spawned.append((argv, env, cwd))
        if state.mutation == 'popen-error': raise OSError('fixture spawn failed')
        if state.mutation == 'task-after-unknown': b.ws.task.write_text('changed task')
        prewait = state.mutation in ('token-before-wait', 'state-before-wait', 'birth-before-wait')
        if state.mutation == 'token-before-wait': b.store.token.write_text('changed token')
        if state.mutation == 'state-before-wait':
            frame = read_owner(b.store.owner)
            frame['local_execution']['receipt_nonce'] = 'c' * 43
            write_owner(b.store.owner, frame)
        if state.mutation == 'birth-before-wait': process.births[11] = None
        state.spawn_owner_bytes = b.store.owner.read_bytes()
        class Child:
            pid = 12
            def wait(self):
                # Match real Popen.wait: repeated wait after reap is harmless,
                # and never starts a second provider or replays its effects.
                if state.waited: return state.returncode
                state.wait_count += 1
                if prewait:
                    # This actual-child stand-in exits even when saved state
                    # cannot authorize Q. Reaping must not require that state.
                    state.waited = True; process.births[12] = 'absent'
                    return state.returncode
                state.waited = True; process.current = 12; process.parent_pid = 11
                if state.mutation == 'wrong-parent': process.parent_pid = 99
                if state.mutation == 'runner-reused': process.births[11] = 'different runner'
                if state.mutation == 'provider-unknown': process.births[12] = None
                launch = row['launch_id']; nonce = row['receipt_nonce']
                if state.mutation == 'wrong-nonce': nonce = 'c' * 43
                try:
                    with pytest.raises(Transferred):
                        api['execute_provider'](b.intent, launch_id=launch, receipt_nonce=nonce,
                            _process=process, _execve=execve, _chdir=lambda path: captures.append(('cwd', path)))
                finally:
                    process.current = 11; process.parent_pid = 1
                if state.mutation == 'live-after-wait': process.births[12] = STAMP
                elif state.mutation == 'unknown-after-wait': process.births[12] = None
                else: process.births[12] = 'absent'
                return True if state.mutation == 'bool-wait' else state.returncode
        return Child()
    state.popen = popen
    return state


@pytest.fixture
def ready(bound, monkeypatch, tmp_path):
    first = build_case(bound, monkeypatch)
    # A second independently configured/registered owner, never restored bytes
    # of a consumed execution. Both are prepared BEFORE fake OS interception.
    second_root = tmp_path / 'independent-negative'; second_root.mkdir(mode=0o700)
    ws = context_fixture.__wrapped__(second_root)
    configured = configure_fixture.__wrapped__(ws)
    second_bound = globals()['bound'].__wrapped__(configured, monkeypatch)
    first.fresh = build_case(second_bound, monkeypatch)
    first.patch = first.fresh.patch = monkeypatch
    return first


def run(ready, **changes):
    ready.patch.setattr(subprocess, 'run', ready.observe)
    b = ready.bound; generation = read_owner(b.store.owner)['local_registration']['generation']
    options = {'intent_digest': b.intent['intent_digest'], 'registration_generation': generation,
        '_process': ready.process, '_popen': ready.popen}
    return ready.api['run_preview'](b.intent, **{**options, **changes})


def test_real_store_unknown_precedes_spawn_and_same_pid_claim_precedes_exec_and_wait(ready):
    row = run(ready)
    assert ready.waited and row['phase'] == 'ended'
    assert row['execution']['outcome'] == {'state': 'ended', 'returncode': 7}
    assert row['termination'] == {'phase': 'pending', 'lease_release': 'pending', 'retire': 'pending'}
    assert row['session_receipt'] is None  # wait alone is not readiness/cleanup.
    assert len(ready.spawned) == 1 and len(ready.captures) == 2
    target = ready.bound.intent['legacy_context']; command, qenv, cwd = ready.spawned[0]
    assert command[:6] == [ready.bound.intent['configuration_evidence']['runtime_python']['path'],
        '-I', '-B', str(Path(target['install_root']) / 'bin/lib/runtime_legacy_preview_cli.py'), 'entry', 'provider']
    assert command[6] == target['name']
    assert command[7:] == ['--launch-id=' + row['launch_id'], '--receipt-nonce=' + row['receipt_nonce']]
    binary, argv, env, claimed = ready.captures[-1]
    assert ready.captures[0] == ('cwd', cwd)
    assert binary == ready.bound.intent['configuration_evidence']['cli']['path'] and argv[0] == binary
    assert '--strict-mcp-config' in argv and '--settings' in argv
    assert argv[-1].endswith(ready.bound.ws.task.read_text()) and '--embed-task' in argv[-1]
    assert json.loads(argv[argv.index('--mcp-config') + 1]) == json.loads(
        (ready.bound.store.owner.with_suffix('.preview-home') / 'local-model-mcp.json').read_text())
    assert env['AGENTSTACK_LAUNCH_ID'] == row['launch_id'] and env['AGENTSTACK_LAUNCH_NONCE'] == row['receipt_nonce']
    assert env['AGENTSTACK_PROVIDER_PID'] == '12'
    for candidate in (env, qenv):
        assert candidate['PATH'] == '/usr/bin:/bin' and candidate['LANG'] == candidate['LC_ALL'] == 'C'
        assert 'TZ' not in candidate and TOKEN not in json.dumps(candidate)
        assert 'fixture-ambient-never-inherit' not in json.dumps(candidate)
        assert candidate['HOME'] == candidate['CLAUDE_CONFIG_DIR'] == str(ready.bound.store.owner.with_suffix('.preview-home'))
    assert env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'
    assert os.environ.get('TZ') is None and os.environ['LC_ALL'] == 'C'


@pytest.mark.parametrize('mutation', ['popen-error', 'task-after-unknown', 'wrong-parent', 'runner-reused',
    'provider-unknown', 'wrong-nonce', 'live-after-wait', 'unknown-after-wait', 'bool-wait',
    'different-uid', 'older-provider', 'reparent', 'exec-error'])
def test_interruption_or_uncertainty_preserves_unknown_not_a_success(ready, mutation):
    assert run(ready)['phase'] == 'ended'
    ready = ready.fresh
    ready.mutation = mutation
    with pytest.raises(LocalModelError): run(ready)
    frame = read_owner(ready.bound.store.owner)
    assert frame['local_execution']['phase'] == 'exec-unknown'
    assert frame['local_execution']['termination']['phase'] == 'pending'
    if mutation not in ('live-after-wait', 'unknown-after-wait', 'bool-wait', 'exec-error'):
        assert not [capture for capture in ready.captures if len(capture) == 4]
    before = ready.bound.store.owner.read_bytes()
    with pytest.raises(LocalModelError): run(ready)
    assert ready.bound.store.owner.read_bytes() == before and len(ready.spawned) == 1


@pytest.mark.parametrize('mutation', ['token-before-wait', 'state-before-wait', 'birth-before-wait'])
def test_spawned_child_is_reaped_even_when_pre_wait_validation_fails(ready, mutation):
    # Same path succeeds first, with a separately prepared owner for rejection.
    assert run(ready)['phase'] == 'ended' and ready.wait_count == 1
    ready = ready.fresh; ready.mutation = mutation
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_EXECUTION_FAILED$'): run(ready)
    assert ready.waited and ready.wait_count == 1 and ready.process.births[12] == 'absent'
    assert ready.bound.store.owner.read_bytes() == ready.spawn_owner_bytes
    row = read_owner(ready.bound.store.owner)['local_execution']
    assert row['phase'] == 'exec-unknown'
    assert row['execution']['outcome'] == {'state': 'unknown', 'returncode': None}
    assert row['termination'] == {'phase': 'pending', 'lease_release': 'pending', 'retire': 'pending'}
    before = ready.bound.store.owner.read_bytes()
    with pytest.raises(LocalModelError): run(ready)
    assert ready.bound.store.owner.read_bytes() == before
    assert len(ready.spawned) == 1 and ready.wait_count == 1 and ready.captures == []


@pytest.mark.parametrize('mutation', ['intent', 'generation', 'task', 'cli', 'token', 'settings'])
def test_invalid_resources_or_stale_handoff_stop_before_plan_or_spawn(ready, mutation):
    assert run(ready)['phase'] == 'ended'
    ready = ready.fresh
    b = ready.bound; changes = {}
    if mutation == 'intent': changes['intent_digest'] = 'c' * 64
    elif mutation == 'generation': changes['registration_generation'] = 'c' * 32
    elif mutation == 'task': b.ws.task.write_text('changed task')
    elif mutation == 'cli':
        path = Path(b.intent['configuration_evidence']['cli']['path'])
        path.chmod(0o700); path.write_text('changed CLI'); path.chmod(0o500)
    elif mutation == 'token': b.store.token.write_text('changed token')
    else: (b.store.owner.with_suffix('.preview-home') / 'local-model-settings.json').write_text('{}')
    before = b.store.owner.read_bytes()
    with pytest.raises(LocalModelError): run(ready, **changes)
    assert b.store.owner.read_bytes() == before and ready.spawned == [] and ready.captures == []
