"""Actual consumer CLI and Targets.execute recheck their last observation."""
import contextlib
import io
from pathlib import Path
import runpy
import socket
import subprocess
import tempfile
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = runpy.run_path(str(ROOT / 'bin/lib/runtime_tmux.py'))


@pytest.mark.parametrize('appears', ['never', 'initial', 'before-admission', 'between-admissions'])
@pytest.mark.parametrize('global_owner', [False, True])
def test_actual_attach_or_create_refreshes_an_initially_absent_endpoint(monkeypatch, appears, global_owner):
    factory = runpy.run_path(str(ROOT / 'bin/lib/legacy_consumer.py'))['LegacyConsumer']
    with tempfile.TemporaryDirectory(prefix='r263ep-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory, socket.socket(socket.AF_UNIX) as stream:
        home = Path(directory).resolve()
        path = home / 's'
        reads, effects = [], []

        def bind():
            stream.bind(str(path))
            path.chmod(0o600)

        if appears == 'initial':
            bind()

        def run(argv, **kwargs):
            command = argv[3]
            if command == 'list-sessions':
                text = 'Alpha\n'
            elif '#{socket_path}|#{pid}' in argv:
                text = str(path) + '|123\n'
            elif '#{pid}' in argv:
                text = '123\n'
            else:
                text = '$1\tAlpha\t%1\t100\n'
            return subprocess.CompletedProcess(argv, 0, text, '')

        transport = API['Transport'](path, binary='fixture-tmux', run=run, allow_absent=True)
        if appears == 'initial':
            transport.server_pid = 123

        def proofs(*args, **kwargs):
            reads.append('proof')
            return [{'live': {'pane_id': '%1', 'pid': 100, 'process_start': 'start'}}] if global_owner else []

        consumer = factory(home, transport=transport, api={'process_start': lambda *a, **kw: 'start'}, proof_reader=proofs, transport_api=API)
        if appears == 'before-admission':
            bind()
        original = consumer.admitted_argv
        checks = []

        def admit(args):
            value = original(args)
            checks.append(value)
            if appears == 'between-admissions' and len(checks) == 1:
                bind()
            return value

        monkeypatch.setattr(consumer, 'admitted_argv', admit)
        cli = runpy.run_path(str(ROOT / 'bin/lib/runtime_tmux_consumer.py'))
        monkeypatch.setitem(cli['main'].__globals__, 'runpy', types.SimpleNamespace(run_path=lambda file: API if str(file).endswith('runtime_tmux.py') else {'LegacyConsumer': lambda *a, **kw: consumer}))

        class ExecObserved(BaseException):
            pass

        def observe_exec(binary, argv, env):
            effects.append((binary, argv, env))
            raise ExecObserved()

        monkeypatch.setattr(cli['os'], 'execvpe', observe_exec)
        stderr = io.StringIO()
        code = None
        with contextlib.redirect_stderr(stderr):
            try:
                code = cli['main'](['new-session', '-A', '-s', 'Alpha'])
            except ExecObserved:
                pass
        if global_owner and appears != 'never':
            assert code == 2
            assert stderr.getvalue() == 'LEGACY_TARGET_IS_GLOBAL\n'
            assert reads and effects == []
        else:
            assert len(effects) == 1
            assert effects[0][1] == ['fixture-tmux', '-S', str(path), 'new-session', '-A', '-s', 'Alpha']
            assert 'TMUX' not in effects[0][2] and 'TMUX_PANE' not in effects[0][2]
            assert bool(reads) == (appears != 'never')
            if appears != 'never':
                assert transport.server_pid == 123


@pytest.mark.parametrize('changed', [False, True])
def test_actual_capture_rechecks_pid_start_after_reading_returned_text(changed):
    target_api = runpy.run_path(str(ROOT / 'bin/lib/runtime_target.py'))
    reader = runpy.run_path(str(ROOT / 'dashboard/terminal_state.py'))
    state = {'start': 'old-start', 'captures': 0}
    observed = []

    class Transport:
        path = Path('/fixture/socket')
        argv = ['fixture-tmux', '-S', str(path)]

        def command(self, *args, **kwargs):
            if args[0] == 'capture-pane':
                state['captures'] += 1
                if state['captures'] == 3 and changed:
                    state['start'] = 'new-start'
                text = 'LATEST TEXT\n❯\nctx: 14% used' if state['captures'] == 3 else '❯\nctx: 14% used'
            elif '#{window_id}' in args:
                text = '@1'
            elif '#{session_id}\t#{session_name}\t#{pane_id}\t#{pane_pid}' in args:
                text = '$1\tBeta\t%1\t123\n'
            else:
                text = '99'
            return types.SimpleNamespace(returncode=0, stdout=text)

    transport = Transport()
    obj = object.__new__(target_api['Targets'])
    obj.deadline = time.monotonic() + 5
    obj.observations = types.SimpleNamespace(deadline=obj.deadline)
    obj.operator = types.SimpleNamespace(binding={'expected_server_instance_id': 'fixture', 'candidate_generation': 'gen', 'authority_epoch': 'epoch'})

    def process_start(*args, **kwargs):
        observed.append(state['start'])
        return state['start']

    obj.api = {'integer': lambda value, minimum=0: type(value) is int and value >= minimum, 'CLIENT_LAYOUT': {'metadata': 'registration-metadata.json'}, 'process_start': process_start}
    obj.tmux = {**API, 'compact_readiness': reader['compact_readiness'], 'compact_refusal': reader['compact_refusal']}
    obj.tmux['module_api'] = lambda _: {'unproven_app': lambda *a: False}
    value = {'launch': dict(provider='claude', launch_id='fixture-launch', receipt_nonce='fixture-nonce', tmux_transport='global-instance'), 'context': {}, 'lives': [dict(pid=123, process_start='old-start', pane_id='%1', launch_id='fixture-launch', receipt_nonce='fixture-nonce')], 'child': None}
    obj.snapshot = lambda *a, **kw: (Path('/fixture/client'), value, 'b' * 64, dict(name='Beta', credential_generation=0, retired_at=None))
    obj.principal = lambda *a, **kw: (dict(kind='operator', uid=501, agent_id=None), None)
    obj.endpoints = lambda *a: [('global-instance', transport)]
    if changed:
        with pytest.raises(API['TargetError'], match='^TMUX_TARGET_CHANGED$'):
            obj.execute('capture', 2, operator=True, lines=40)
        assert observed[-1] == 'new-start'
    else:
        result, schema = obj.execute('capture', 2, operator=True, lines=40)
        assert schema == 'capture.success'
        assert result['capture']['text'] == 'LATEST TEXT\n❯\nctx: 14% used'
        assert result['target']['process_start'] == 'old-start'
    assert state['captures'] == 3
