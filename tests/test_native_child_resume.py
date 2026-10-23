"""Native resume reads only the exact child's callback and retained identity."""
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from hooks import child_resume as resume
from dashboard import server
from hooks import harness_child as native


def child(tmp_path, name, harness='dsh', native_id='session-one'):
    runtime = tmp_path / 'runtime'
    directory = runtime / 'child-agents' / (name + '.harness') / ('a' * 32)
    state = dict(schema_version=1, launch_origin='child', provider=harness,
                 agent_name=name, agent_id=73 if name == 'FirstAgent' else 74,
                 project_key='/fixture/project', program=harness,
                 registration_token='fixture-token-' + name,
                 harness=harness, harness_backend='strata', harness_state_dir=str(directory),
                 retired_at='2026-10-19T00:00:00Z', resume_expires_at='2099-01-01T00:00:00Z')
    document = dict(agent_name=name, program=harness, backend_id='strata',
                    model='fixture-model', project_key=state['project_key'],
                    cwd=str(tmp_path), state_dir=str(directory), runtime_dir=str(runtime))
    event = dict(harness=harness, native_session_id=native_id,
                 created_at='2026-10-19T00:00:00Z', agent_name=name)
    for path, value in [(directory / 'native-session.json', event),
                        (directory / 'input.json', document),
                        (runtime / 'child-agents' / (name + '.json'), state)]:
        resume._atomic_json(path, value)
    resume._atomic_bytes(runtime / ('agent_token_' + name), state['registration_token'].encode())
    return runtime, state, document, event


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_two_children_resume_only_their_own_id(tmp_path, harness):
    runtime, first, _, _ = child(tmp_path, 'FirstAgent', harness, 'first-native-id')
    _, second, _, _ = child(tmp_path, 'SecondAgent', harness, 'second-native-id')
    for state, expected in [(first, 'first-native-id'), (second, 'second-native-id')]:
        retained = resume.inspect_retained(runtime, state['agent_name'],
            agent_id=state['agent_id'], project_key=state['project_key'], program=harness)
        assert resume.native_resume_input(runtime, retained)['resume_session_id'] == expected


@pytest.mark.parametrize('mismatch', ['directory', 'event_name', 'event_harness',
                                      'input_backend', 'input_name', 'input_project', 'unsafe_id'])
def test_foreign_or_invalid_native_binding_is_rejected(tmp_path, mismatch):
    runtime, state, document, event = child(tmp_path, 'FirstAgent')
    _, other, _, _ = child(tmp_path, 'SecondAgent')
    directory = resume.Path(state['harness_state_dir'])
    if mismatch == 'directory':
        state['harness_state_dir'] = other['harness_state_dir']
    elif mismatch == 'event_name':
        event['agent_name'] = 'SecondAgent'
    elif mismatch == 'event_harness':
        event['harness'] = 'hermes'
    elif mismatch == 'input_backend':
        document['backend_id'] = 'deepseek'
    elif mismatch == 'input_name':
        document['agent_name'] = 'SecondAgent'
    elif mismatch == 'input_project':
        document['project_key'] = '/fixture/foreign-project'
    else:
        event['native_session_id'] = '../latest'
    resume._atomic_json(directory / 'native-session.json', event)
    resume._atomic_json(directory / 'input.json', document)
    with pytest.raises(resume.ResumeStateError) as caught:
        resume.native_resume_input(runtime, state)
    assert caught.value.code == 'identity_mismatch'


def test_missing_callback_does_not_fall_back_to_latest(tmp_path):
    runtime, state, _, _ = child(tmp_path, 'FirstAgent')
    directory = resume.Path(state['harness_state_dir'])
    (directory / 'native-session.json').unlink()
    (directory / 'last-used.json').write_text(json.dumps({'session_id': 'foreign-latest'}))
    with pytest.raises(resume.ResumeStateError) as caught:
        resume.native_resume_input(runtime, state)
    assert caught.value.code == 'no_history'


def test_expired_native_identity_cannot_be_reactivated(tmp_path):
    runtime, state, _, _ = child(tmp_path, 'FirstAgent', 'hermes')
    state['resume_expires_at'] = '2026-01-01T00:00:00Z'
    resume._atomic_json(runtime / 'child-agents/FirstAgent.json', state)
    with pytest.raises(resume.ResumeStateError) as caught:
        resume.inspect_retained(runtime, 'FirstAgent', agent_id=73,
                                project_key=state['project_key'], program='hermes')
    assert caught.value.code == 'retention_expired'


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
@pytest.mark.parametrize('launch_ok', [True, False])
def test_native_dashboard_dispatch_and_failed_startup_rollback(tmp_path, monkeypatch, harness, launch_ok):
    runtime, state, _, _ = child(tmp_path, 'FirstAgent', harness, 'owned-session')
    registration = {key: state[key] for key in ('agent_name', 'agent_id', 'project_key', 'program')}
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, '_child_resume_module', lambda: resume)
    monkeypatch.setattr(server, '_agent_program', lambda _: harness)
    monkeypatch.setattr(server, '_resume_registration', lambda *a: registration)
    monkeypatch.setattr(server, '_native_resume_launcher_ready', lambda: True)
    monkeypatch.setattr(server, '_native_prepare_resume', lambda *a: None)
    monkeypatch.setattr(server, '_register_claude_resume', lambda *a, **k: True)
    mail, launches = [], []
    monkeypatch.setattr(server, '_set_claude_mail_retired', lambda *a, **k: mail.append(k['retired']))
    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return {'ok': launch_ok, 'adapter': 'detached'}
    monkeypatch.setattr(server, '_launch_claude_resume_tmux', launch)
    assert server._resume_capability('FirstAgent', harness, category='retired', open_terminal=False) == 'ready'
    answer = server.do_resume('FirstAgent', open_terminal=False, replace_husk=True)
    assert answer['ok'] is launch_ok
    argv, options = launches[0]
    assert argv[-1].endswith('--resume-native FirstAgent')
    assert options['replace_husk'] is True and options['open_terminal'] is False
    assert mail == ([False] if launch_ok else [False, True])
    current = resume._load_state(runtime / 'child-agents/FirstAgent.json')
    assert ('resume_in_progress_at' in current) is launch_ok


def test_native_missing_id_is_unavailable_before_mail_or_tmux(tmp_path, monkeypatch):
    runtime, state, _, _ = child(tmp_path, 'FirstAgent')
    (resume.Path(state['harness_state_dir']) / 'native-session.json').unlink()
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, '_child_resume_module', lambda: resume)
    monkeypatch.setattr(server, '_resume_registration', lambda *a: state)
    monkeypatch.setattr(server, '_register_claude_resume', lambda *a: pytest.fail('Mail called'))
    monkeypatch.setattr(server, '_launch_claude_resume_tmux', lambda *a, **k: pytest.fail('tmux called'))
    assert server._resume_capability('FirstAgent', 'dsh', category='retired', open_terminal=False) == 'no_history'
    assert server._do_resume_native('FirstAgent', 'dsh')['resume_capability'] == 'no_history'


def prepared_native(tmp_path, monkeypatch, harness='dsh'):
    runtime, state, document, _ = child(tmp_path, 'FirstAgent', harness, 'owned-session')
    directory = Path(state['harness_state_dir'])
    profile_key = 'DSH_HOME' if harness == 'dsh' else 'HERMES_HOME'
    profile = str(tmp_path / 'owned-profile')
    document.update(env={}, tools={'base': 'default', 'tools': {}},
                    manifest_file=str(tmp_path / 'manifest.json'))
    resume._atomic_json(directory / 'input.json', document)
    resume._atomic_json(directory / 'launch.json', {
        'argv': ['/bin/true'], 'env': {profile_key: profile}, 'cwd': str(tmp_path)})
    (directory / 'native-exit.status').write_text('0\n')
    generator = tmp_path / 'generator.py'
    generator.write_text('''import argparse,json,sys,os
def child_home(document):
    assert document['native_profile_env'] == {PROFILE_KEY:PROFILE}
if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--out');a=p.parse_args()
    s=json.load(open(a.input))
    plan={'argv':[sys.executable,'-c','import sys; print(sys.argv[1])',s['resume_session_id']],
          'env':{PROFILE_KEY:PROFILE},'cwd':s['cwd']}
    with open(a.out,'w') as f:json.dump(plan,f)
    os.chmod(a.out,0o600)
'''.replace('PROFILE_KEY', repr(profile_key)).replace('PROFILE', repr(profile)))
    monkeypatch.setenv('AGENTSTACK_CLAUDE_JSON', str(tmp_path / 'no-settings.json'))
    monkeypatch.setenv('AGENTSTACK_MCP_URL', 'http://127.0.0.1:9/mcp')
    monkeypatch.setattr(native, 'selection', lambda *a: {'python': sys.executable})
    monkeypatch.setattr(native, 'config_generator', lambda *a: generator)
    resume.begin_resume(runtime, 'FirstAgent', agent_id=73,
                        project_key=state['project_key'], program=harness)
    return runtime, state, directory, generator


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_prepare_and_foreground_resume_keep_exact_id_and_profile(tmp_path, monkeypatch, harness):
    runtime, state, directory, _ = prepared_native(tmp_path, monkeypatch, harness)
    assert native.prepare_resume(runtime, 'FirstAgent') == directory
    source = native.read(directory / 'input.json')
    assert source['resume_session_id'] == 'owned-session'
    assert Path(source['prompt_file']).read_text() == ''
    assert not (directory / 'native-exit.status').exists()
    proxy = native.read(source['mcp_config'])['mcpServers']['orrery-mail']['env']
    assert proxy['AGENTSTACK_PROXY_PROGRAM'] == harness
    assert proxy['AGENTSTACK_PROXY_TOKEN_FILE'] == str(runtime / 'agent_token_FirstAgent')
    invocation = native.resume_command(runtime, 'FirstAgent').split('; native_rc=$?', 1)[0]
    assert shlex.split(invocation)[-1] == 'owned-session'
    current = resume._load_state(runtime / 'child-agents/FirstAgent.json')
    assert current['retired_at'] is None and 'resume_in_progress_at' not in current
    assert current['harness_state_dir'] == str(directory)


def test_foreground_refuses_unprepared_or_canceled_attempt(tmp_path, monkeypatch):
    runtime, state, directory, _ = prepared_native(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='native_resume_not_prepared'):
        native.resume_command(runtime, 'FirstAgent')
    native.prepare_resume(runtime, 'FirstAgent')
    resume.cancel_resume(runtime, 'FirstAgent', agent_id=73, project_key=state['project_key'])
    with pytest.raises(ValueError, match='native_resume_not_prepared'):
        native.resume_command(runtime, 'FirstAgent')


def test_changed_profile_cannot_be_adopted_on_a_second_attempt(tmp_path, monkeypatch):
    runtime, state, directory, generator = prepared_native(tmp_path, monkeypatch)
    text = generator.read_text().replace('owned-profile', 'foreign-profile')
    generator.write_text(text)
    for _ in range(2):
        with pytest.raises(ValueError, match='native_resume_profile_changed'):
            native.prepare_resume(runtime, 'FirstAgent')
    assert resume._load_state(runtime / 'child-agents/FirstAgent.json')['retired_at'] is not None


def test_generation_failure_preserves_husk_and_retained_identity(tmp_path, monkeypatch):
    runtime, state, _, _ = child(tmp_path, 'FirstAgent')
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, '_child_resume_module', lambda: resume)
    monkeypatch.setattr(server, '_resume_registration', lambda *a: state)
    monkeypatch.setattr(server, '_native_resume_launcher_ready', lambda: True)
    def fail(*args):
        raise ValueError('generator_unavailable')
    monkeypatch.setattr(server, '_native_prepare_resume', fail)
    monkeypatch.setattr(server, '_register_claude_resume', lambda *a: pytest.fail('Mail called'))
    monkeypatch.setattr(server, '_launch_claude_resume_tmux', lambda *a, **k: pytest.fail('husk touched'))
    result = server._do_resume_native('FirstAgent', 'dsh', replace_husk=True)
    assert not result['ok']
    current = resume._load_state(runtime / 'child-agents/FirstAgent.json')
    assert current['retired_at'] == state['retired_at'] and 'resume_in_progress_at' not in current
    assert (runtime / 'agent_token_FirstAgent').read_text() == state['registration_token']


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_prepared_resume_cli_in_isolated_minimal_environment(tmp_path, monkeypatch, harness):
    runtime, state, directory, _ = prepared_native(tmp_path, monkeypatch, harness)
    native.prepare_resume(runtime, 'FirstAgent')
    script = Path(native.__file__).resolve()
    result = subprocess.run([sys.executable, '-I', str(script), 'resume',
        '--runtime-dir', str(runtime), '--agent-name', 'FirstAgent',
        '--project-key', state['project_key']],
        env={'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}, cwd='/',
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert shlex.split(result.stdout.split('; native_rc=$?', 1)[0])[-1] == 'owned-session'
    assert state['registration_token'] not in result.stdout + result.stderr
    current = resume._load_state(runtime / 'child-agents/FirstAgent.json')
    assert current['retired_at'] is None and 'resume_in_progress_at' not in current
    assert current['harness_state_dir'] == str(directory)


@pytest.mark.parametrize('changed_profile', [False, True, 'new_connection'])
def test_real_dsh_generator_round_trip_preserves_owned_native_session(tmp_path, monkeypatch, request, changed_profile):
    from test_dsh_thin_config import ThinTests
    from test_harness_child import staged
    fixture = ThinTests()
    fixture.setUp()
    request.addfinalizer(fixture.doCleanups)
    manifest = native.read(fixture.manifest)
    backend = 'strata-epyc' if changed_profile == 'new_connection' else 'strata'
    if backend != 'strata':
        manifest['backends'][backend] = {**manifest['backends']['strata'], 'kind': 'strata'}
        template = native.read(fixture.template)
        routes = native.read(template['backend_file'])
        routes['backends'][backend] = dict(routes['backends']['strata'])
        native.save(Path(template['backend_file']), routes)
    manifest['harnesses']['dsh']['python'] = sys.executable
    native.save(fixture.manifest, manifest)
    args = staged(tmp_path, 'dsh')
    args.manifest, args.model = str(fixture.manifest), 'a'
    args.backend = backend
    args.helper_model = 'b'
    mcp = native.read(args.mcp_config)
    mcp['mcpServers']['orrery-mail'].update(command='/fixture/proxy', args=[])
    native.save(args.mcp_config, mcp)
    monkeypatch.setenv('AGENTSTACK_CLAUDE_JSON', str(tmp_path / 'absent-settings.json'))
    monkeypatch.setenv('AGENTSTACK_MCP_URL', 'http://127.0.0.1:9/mcp')
    directory = native.prepare(args)
    first = native.read(directory / 'launch.json')
    runtime = Path(args.runtime_dir)
    resume.finish_registration(runtime, args.agent_name, generation=args.generation, rollback=False)
    resume._atomic_json(directory / 'native-session.json', {
        'harness': 'dsh', 'native_session_id': 'exact-owned-id',
        'created_at': '2026-10-19T00:00:00Z', 'agent_name': args.agent_name})
    assert resume.mark_retired(runtime, args.agent_name, retention_days=30)
    resume.begin_resume(runtime, args.agent_name, agent_id=7,
                        project_key=args.project_key, program='dsh')
    if changed_profile and changed_profile != 'new_connection':
        source = native.read(directory / 'input.json')
        source['native_profile_env'] = {'DSH_HOME': str(tmp_path / 'foreign-profile')}
        native.save(directory / 'input.json', source)
        original = (directory / 'dsh-config/native.patch.json').read_bytes()
        with pytest.raises(ValueError, match='native_resume_profile_changed'):
            native.prepare_resume(runtime, args.agent_name)
        assert (directory / 'dsh-config/native.patch.json').read_bytes() == original
        assert not (tmp_path / 'foreign-profile').exists()
        assert resume._load_state(runtime / 'child-agents' / (args.agent_name + '.json'))['retired_at']
        return
    assert native.prepare_resume(runtime, args.agent_name) == directory
    resumed = native.read(directory / 'launch.json')
    assert resumed['env']['DSH_HOME'] == first['env']['DSH_HOME']
    assert resumed['argv'][-2:] == ['--resume', 'exact-owned-id']
    source = native.read(directory / 'input.json')
    assert source['backend_id'] == backend
    assert Path(source['prompt_file']).read_text() == ''
    blocks = native.read(directory / 'dsh-config/native.patch.json')
    proxy = next(row for block in blocks for row in block.get('insert', [])
                 if row['id'] == 'orrery-mcp-orrery-mail')
    assert proxy['config']['env']['AGENTSTACK_PROXY_PROGRAM'] == 'dsh'
    assert proxy['config']['env']['AGENTSTACK_PROXY_TOKEN_FILE'] == str(runtime / ('agent_token_' + args.agent_name))
    assert next(row for row in blocks if row.get('id') == 'tool-subagent')['config']['agentOptions']['model'] == 'b'
    invocation = native.resume_command(runtime, args.agent_name).split('; native_rc=$?', 1)[0]
    assert shlex.split(invocation)[-2:] == ['--resume', 'exact-owned-id']
    # The synthetic node is deliberately invalid: this test generates a plan,
    # never executes a runtime, provider plugin, credential helper or Mail RPC.


@pytest.mark.parametrize('changed_profile', [False, True, 'legacy', 'missing_home', 'missing_pin', 'missing_history', 'unknown_reason', 'new_connection'])
def test_real_hermes_generator_round_trip_preserves_owned_native_session(tmp_path, monkeypatch, request, changed_profile):
    from shutil import copy2
    from unittest.mock import patch
    from test_hermes_thin_config import ThinConfig, config
    from test_harness_child import staged
    from test_managed_harness_install import CAFixture, fixture_stamp
    original_popen = subprocess.Popen
    fixture = ThinConfig()
    fixture.setUp()
    request.addfinalizer(fixture.doCleanups)
    # Synthetic CA inputs and runtime bytes must bind to the same private
    # product policy that the real static admission and generator will read.
    ca = CAFixture(None, fixture.base)
    request.addfinalizer(ca.stack.close)
    ca.source(fixture.source)
    ca.ca.apply(ca.repo, fixture.source)
    generation = Path(fixture.template_data['hermes_python']).parent.parent
    paths = ca.material(fixture.runtime, fixture.source, generation)
    stamp = fixture_stamp(fixture.source)
    fixture.template_data.update(ca_recipe=ca.ca.load(ca.repo)[2],
        ca_evidence=ca.ca.runtime_evidence(ca.repo, fixture.runtime, fixture.source,
            generation, ca.target, paths), **stamp)
    product = Path(native.__file__).resolve().parents[1]
    for relative in ('bin/lib/instruction_blocks.py', 'bin/lib/harness_instructions.md', 'codex/AGENTS.md'):
        destination = ca.repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy2(product / relative, destination)
    monkeypatch.setattr(native, 'ROOT', ca.repo)
    receipt = fixture.base / 'plain-receipt.json'
    fixture.save(receipt, {'variant': 'plain', 'thin_config_contract': 1,
        'harness': 'hermes', 'complete': True, 'details': fixture.template_data})
    fixture.save(fixture.template, {**fixture.template_data, 'generation_receipt': str(receipt)})
    manifest = {**fixture.manifest_data, 'enabled': True}
    backend = 'strata-epyc' if changed_profile == 'new_connection' else 'deepseek'
    if backend != 'deepseek':
        manifest['backends'][backend] = {**manifest['backends']['deepseek'], 'kind': 'strata'}
        template = native.read(fixture.template)
        template['profiles'][backend] = dict(template['profiles']['strata'])
        fixture.save(fixture.template, template)
    manifest['harnesses']['hermes']['python'] = sys.executable
    fixture.save(fixture.manifest, manifest)
    args = staged(tmp_path, 'hermes')
    args.manifest, args.model, args.backend = str(fixture.manifest), 'fixture-main', backend
    args.helper_model = 'fixture-helper'
    mcp = native.read(args.mcp_config)
    mcp['mcpServers']['orrery-mail'].update(command='/fixture/proxy', args=[])
    native.save(args.mcp_config, mcp)
    monkeypatch.setenv('AGENTSTACK_CLAUDE_JSON', str(tmp_path / 'absent-settings.json'))
    monkeypatch.setenv('AGENTSTACK_MCP_URL', 'http://127.0.0.1:9/mcp')
    # Permit the product validation/generator subprocesses. The native Hermes
    # executable remains invalid synthetic text and is never invoked.
    with patch.object(subprocess, 'Popen', original_popen):
        directory = native.prepare(args)
        first = native.read(directory / 'launch.json')
        memory = Path(first['env']['HERMES_HOME']) / 'memories/MEMORY.md'
        memory.parent.mkdir()
        memory.write_text('synthetic-memory-marker-distinct-from-conversation')
        (memory.parent.parent / 'state.db').write_bytes(b'synthetic history presence fixture')
        runtime = Path(args.runtime_dir)
        resume.finish_registration(runtime, args.agent_name, generation=args.generation, rollback=False)
        resume._atomic_json(directory / 'native-session.json', {
            'harness': 'hermes', 'native_session_id': 'exact-hermes-id',
            'created_at': '2026-10-19T00:00:00Z', 'agent_name': args.agent_name})
        assert resume.mark_retired(runtime, args.agent_name, retention_days=30)
        resume.begin_resume(runtime, args.agent_name, agent_id=7,
                            project_key=args.project_key, program='hermes')
        if changed_profile and changed_profile != 'new_connection':
            source = native.read(directory / 'input.json')
            reason = 'native_resume_profile_changed'
            if changed_profile == 'legacy':
                shared = fixture.runtime / 'profiles/deepseek'
                fixture.save(shared / 'config.yaml', {'synthetic': 'unchanged'})
                source['native_profile_env'] = {'HERMES_HOME': str(shared)}
                reason = 'native_resume_legacy_profile'
            elif changed_profile == 'missing_home':
                import shutil
                shutil.rmtree(Path(first['env']['HERMES_HOME']))
                reason = 'native_resume_home_missing'
            elif changed_profile == 'missing_pin':
                source['native_profile_env'] = None
                reason = 'native_resume_profile_missing'
            elif changed_profile == 'missing_history':
                (memory.parent.parent / 'state.db').unlink()
                reason = 'native_resume_history_missing'
            elif changed_profile == 'unknown_reason':
                reason = 'native_resume_profile_changed-untrusted-private-text'
                def unknown_home(document):
                    raise ValueError(reason)
                monkeypatch.setattr(native.runpy, 'run_path', lambda _: {'child_home': unknown_home})
            else:
                source['native_profile_env'] = {'HERMES_HOME': str(tmp_path / 'foreign-profile')}
            native.save(directory / 'input.json', source)
            original = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
            with pytest.raises(ValueError) as failure:
                native.prepare_resume(runtime, args.agent_name)
            if changed_profile == 'unknown_reason':
                assert not hasattr(failure.value, 'code')
            else:
                assert isinstance(failure.value, resume.ResumeStateError)
                assert failure.value.code == reason
            assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()} == original
            assert not (tmp_path / 'foreign-profile').exists()
            assert resume._load_state(runtime / 'child-agents' / (args.agent_name + '.json'))['retired_at']
            if changed_profile == 'legacy':
                assert native.read(shared / 'config.yaml') == {'synthetic': 'unchanged'}
            return
        assert native.prepare_resume(runtime, args.agent_name) == directory
        resumed = native.read(directory / 'launch.json')
        assert resumed['env']['HERMES_HOME'] == first['env']['HERMES_HOME']
        assert memory.read_text() == 'synthetic-memory-marker-distinct-from-conversation'
        assert resumed['argv'][-2:] == ['--resume', 'exact-hermes-id']
        source = native.read(directory / 'input.json')
        assert source['backend_id'] == backend
        assert Path(source['prompt_file']).read_text() == ''
        overlay = config.read(directory / 'overlay/config.yaml')
        assert overlay['delegation']['model'] == 'fixture-helper'
        proxy = overlay['mcp_servers']['orrery-mail']['env']
        assert proxy['AGENTSTACK_PROXY_PROGRAM'] == 'hermes'
        assert proxy['AGENTSTACK_PROXY_TOKEN_FILE'] == str(runtime / ('agent_token_' + args.agent_name))
        invocation = native.resume_command(runtime, args.agent_name).split('; native_rc=$?', 1)[0]
        assert shlex.split(invocation)[-2:] == ['--resume', 'exact-hermes-id']


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
@pytest.mark.parametrize('reason', [None, 'native_resume_profile_changed', 'native_resume_profile_missing',
                                  'native_resume_legacy_profile', 'native_resume_home_missing',
                                  'native_resume_history_missing'])
def test_jump_preserves_finished_native_husk_when_generation_fails(tmp_path, monkeypatch, harness, reason):
    runtime, state, _, _ = child(tmp_path, 'FirstAgent', harness)
    monkeypatch.setattr(server, 'RUNTIME_DIR', str(runtime))
    monkeypatch.setattr(server, '_agent_program', lambda _: harness)
    monkeypatch.setattr(server, '_has_session', lambda _: True)
    monkeypatch.setattr(server, 'lookup_agent', lambda _: {'category': 'finished'})
    monkeypatch.setattr(server, '_child_resume_module', lambda: resume)
    monkeypatch.setattr(server, '_resume_registration', lambda *a: state)
    monkeypatch.setattr(server, '_native_resume_launcher_ready', lambda: True)
    def fail(*args):
        if reason:
            raise resume.ResumeStateError(reason, 'Native resume HOME prerequisites are unavailable')
        raise ValueError('untrusted-private-exception-text')
    monkeypatch.setattr(server, '_native_prepare_resume', fail)
    monkeypatch.setattr(server, '_tmux_run', lambda *a, **k: pytest.fail('husk changed before config generation'))
    monkeypatch.setattr(server, '_register_claude_resume', lambda *a: pytest.fail('Mail called'))
    result = server.do_jump('FirstAgent', open_terminal=False)
    assert not result['ok']
    assert result['resume_capability'] == (reason or 'config_unrestorable')
    assert 'untrusted-private-exception-text' not in json.dumps(result)
    current = resume._load_state(runtime / 'child-agents/FirstAgent.json')
    assert current['retired_at'] == state['retired_at'] and 'resume_in_progress_at' not in current
    assert (runtime / 'agent_token_FirstAgent').read_text() == state['registration_token']
