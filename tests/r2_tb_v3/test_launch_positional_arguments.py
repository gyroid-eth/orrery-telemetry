"""Generated internal argv round-trips through the real main/argparse entry.

These are argument-boundary unit tests, not native/provider acceptance. Only
the final dispatcher and unrelated process/lifecycle effects are synthetic.
"""
from contextlib import nullcontext
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('boundary', ['execute-launch', 'provider-exec'])
@pytest.mark.parametrize('nonce', ['A' * 43, '-' + 'A' * 42, '--' + 'A' * 41],
                         ids=['ordinary', 'leading-dash', 'leading-double-dash'])
@pytest.mark.parametrize('session_id', [None, '-saved-provider-session'],
                         ids=['startup', 'resume'])
def test_generated_launch_arguments_reach_real_main_unchanged(
        tmp_path, monkeypatch, boundary, nonce, session_id):
    api = runpy.run_path(str(ROOT / 'bin/lib/runtime_client.py'))
    context = tmp_path / 'runtime-client.json'
    # Real selector reads this private fixture context; only configured owner
    # construction is replaced, since this unit test makes no authenticated RPC.
    api['atomic_json'](context, {'kind': api['KIND'], 'mode': 'global'})
    client = SimpleNamespace(mode='global', path=context, runtime_dir=tmp_path / 'runtime',
        profile_path=tmp_path / 'profile.json', outputs={'launch': tmp_path / 'launch.json'},
        identity={'name': 'ArgumentFixture', 'agent_id': 1},
        config={'wrapper_root': str(ROOT)}, endpoint='http://127.0.0.1:1/mcp',
        operation_mutex=nullcontext, observe=lambda: None)
    delivery = api['runtime_delivery'](client)
    row = {'launch_id': '00000000-0000-4000-8000-000000000001',
           'receipt_nonce': nonce, 'phase': 'gated', 'provider': 'claude',
           'cwd': str(tmp_path), 'launch_kind': 'resume' if session_id is not None else 'startup'}
    dispatched = []
    def dispatch(*args):
        dispatched.append(args)
        return 0
    sink = SimpleNamespace(execute=dispatch, exec_provider=dispatch)
    def configured(path, *, recovery, selection):
        assert path == str(context)
        assert selection['context'] == context and selection['mode'] == 'global'
        return client
    monkeypatch.setitem(api['main'].__globals__, 'configured', configured)
    monkeypatch.setitem(api['main'].__globals__, 'runtime_delivery', lambda value: sink)
    generated = []
    def round_trip(command):
        assert command[:2] == [sys.executable, str(ROOT / 'bin/lib/runtime_client.py')]
        generated.append(command)
        assert api['main'](command[2:]) == 0
        expected = (row['launch_id'], nonce)
        if boundary == 'execute-launch':
            if session_id is not None: expected += (session_id,)
        else:
            expected += (session_id,)
        assert dispatched == [expected]

    if boundary == 'execute-launch':
        def launch_command(*args):
            assert args[:6] == ('new-session', '-d', '-s', 'ArgumentFixture', '-c', str(tmp_path))
            command = shlex.split(args[6])
            assert command[0] == 'exec'
            round_trip(command[1:])
            return subprocess.CompletedProcess(args, 0, '', '')
        monkeypatch.setattr(delivery, 'launch_command', launch_command)
        delivery.create_wrapper(row, session_id)
    else:
        monkeypatch.setattr(delivery, 'launch', lambda: row)
        monkeypatch.setattr(delivery, 'save', lambda *args: None)
        monkeypatch.setattr(delivery, 'launch_task', lambda *args, **kwargs: '')
        monkeypatch.setattr(delivery, 'provider_command', lambda *args: ['/usr/bin/true'])
        monkeypatch.setattr(delivery, 'live_rows', lambda *args: [])
        ended = []
        monkeypatch.setattr(delivery, 'end_launch', lambda payload: ended.append(payload))
        monkeypatch.setitem(delivery.api, 'process_start', lambda pid: 'fixture-birth')
        def popen(command, *, cwd, env):
            assert cwd == str(tmp_path)
            assert env['AGENTSTACK_LAUNCH_NONCE'] == nonce
            round_trip(command)
            return SimpleNamespace(pid=123456, wait=lambda **kwargs: 0)
        # Namespace substitution does not alter shared subprocess.Popen.
        module = delivery.execute.__globals__
        monkeypatch.setitem(module, 'subprocess', SimpleNamespace(
            **{**vars(module['subprocess']), 'Popen': popen}))
        assert delivery.execute(row['launch_id'], nonce, session_id) == 0
        assert ended == [{'session_id': None}]
    assert len(generated) == 1
