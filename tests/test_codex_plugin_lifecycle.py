from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('codex_plugin_trust', ROOT/'scripts/codex_plugin_trust.py')
trust = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trust)
OWN_SPEC = importlib.util.spec_from_file_location('codex_plugin_ownership', ROOT/'scripts/codex_plugin_ownership.py')
ownership = importlib.util.module_from_spec(OWN_SPEC)
OWN_SPEC.loader.exec_module(ownership)
PLUGIN_ID = 'agentstack-codex-app@agentstack-local'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import shutil
    root = tmp_path/'plugin'
    shutil.copytree(ROOT/'integrations/codex_app/plugin', root)
    home = tmp_path/'codex'; home.mkdir()
    cwd = tmp_path/'project'; cwd.mkdir()
    definitions = json.loads((root/'hooks/hooks.json').read_text())['hooks']
    hooks = []
    for n, (event, groups) in enumerate(definitions.items()):
        handler = groups[0]['hooks'][0]
        hooks.append({'key': f'owned:{n}', 'eventName': event,
                      'handler': {'type': 'command', 'command': handler['command'], 'async': handler['async']},
                      'isManaged': False, 'source': 'plugin', 'sourcePath': str(root/'hooks/hooks.json'),
                      'pluginId': PLUGIN_ID, 'timeoutSec': handler['timeoutSec'],
                      'currentHash': f'sha256:{n}', 'trustStatus': 'untrusted', 'enabled': False})
    state = {'hooks': hooks, 'config': {'other': {'trusted_hash': 'preserved', 'enabled': False}},
             'writes': [], 'requests': [], 'reject': False, 'verify': True}

    class Server:
        def __init__(self, binary, selected_home, selected_cwd):
            assert selected_home == home and selected_cwd == cwd
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, method, params):
            state['requests'].append(method)
            if method == 'hooks/list':
                return {'data': [{'cwd': str(cwd), 'hooks': copy.deepcopy(state['hooks']), 'errors': []}]}
            if method == 'config/read':
                return {'layers': [{'name': {'type': 'user'}, 'version': 'user-version'}]}
            assert method == 'config/batchWrite', 'no model/thread/login or other write requests'
            assert params['expectedVersion'] == 'user-version'
            if state['reject']: raise trust.TrustUnavailable('config/batchWrite rejected')
            state['writes'].append(params)
            edit = params['edits'][0]
            assert edit['keyPath'] == 'hooks.state' and edit['mergeStrategy'] == 'upsert'
            state['config'].update(edit['value'])
            if state['verify']:
                for h in state['hooks']:
                    if h['key'] in edit['value']:
                        h.update(enabled=True, trustStatus='trusted')
            return {'status': 'ok'}

    monkeypatch.setattr(trust, 'AppServer', Server)
    return root, home, cwd, state


def plan_for(runtime):
    root, home, cwd, state = runtime
    return trust.capture_plan('/fixture/codex', home, cwd, root, PLUGIN_ID)


def test_review_plan_is_not_consent(runtime):
    plan = plan_for(runtime)
    with pytest.raises(trust.TrustUnavailable, match='approval'):
        trust.apply_trust(plan)
    assert not runtime[3]['writes']


def test_only_reviewed_owned_hooks_are_enabled_and_trusted(runtime):
    plan = plan_for(runtime); plan['approved'] = True
    result = trust.apply_trust(plan)
    state = runtime[3]
    assert result['hook_trust'] == 'observed_current' and result['history'] == 'unobserved'
    assert len(state['writes']) == 1
    assert state['config']['other'] == {'trusted_hash': 'preserved', 'enabled': False}
    assert set(state['writes'][0]['edits'][0]['value']) == {h['key'] for h in plan['hooks']}


@pytest.mark.parametrize('field,value', [('currentHash', 'changed'), ('timeoutSec', 999),
                                         ('source', 'user'), ('pluginId', 'other@local'),
                                         ('sourcePath', '/unowned/hooks.json')])
def test_changed_or_unowned_definition_prevents_writes(runtime, field, value):
    plan = plan_for(runtime); plan['approved'] = True
    runtime[3]['hooks'][0][field] = value
    with pytest.raises(trust.TrustUnavailable): trust.apply_trust(plan)
    assert not runtime[3]['writes']


def test_script_change_with_same_hook_hash_prevents_trust(runtime):
    plan = plan_for(runtime); plan['approved'] = True
    (runtime[0]/'scripts/run-hook.sh').write_text('changed script')
    with pytest.raises(trust.TrustUnavailable, match='payload changed'): trust.apply_trust(plan)
    assert not runtime[3]['writes']


def test_policy_or_version_conflict_falls_back_without_claiming_trust(runtime):
    plan = plan_for(runtime); plan['approved'] = True; runtime[3]['reject'] = True
    with pytest.raises(trust.TrustUnavailable, match='rejected'): trust.apply_trust(plan)
    assert not runtime[3]['writes']


def test_write_acknowledgement_alone_is_not_verified_trust(runtime):
    plan = plan_for(runtime); plan['approved'] = True; runtime[3]['verify'] = False
    with pytest.raises(trust.TrustUnavailable, match='not observed'): trust.apply_trust(plan)


def test_library_source_has_no_side_effects_or_shell_option_changes(tmp_path):
    result = subprocess.run(['/bin/bash', '-c', 'set +u; source "$1"; '
                             'type agentstack_codex_app_plan >/dev/null; '
                             'type agentstack_codex_app_remove_registration >/dev/null; '
                             'echo "${UNSET_VALUE}source-only"', 'bash',
                             str(ROOT/'scripts/lib/codex-app-integration.sh')],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == 'source-only\n'
    assert list(tmp_path.iterdir()) == []


def test_remove_registration_failure_retains_payload_and_runtime(tmp_path):
    install = tmp_path/'integration'; install.mkdir()
    runtime = tmp_path/'runtime'; runtime.mkdir()
    binary = tmp_path/'codex'; binary.write_text('#!/bin/sh\nexit 1\n'); binary.chmod(0o755)
    manifest = {'tool': 'agentstack-codex-app', 'install_dir': str(install), 'runtime_dir': str(runtime),
                'shared_codex_home': str(tmp_path/'codex-home'), 'codex_binary': str(binary),
                'plugin': {'enabled': True, 'id': PLUGIN_ID, 'marketplace_name': 'agentstack-local'},
                'service': {'kind': 'disabled'}}
    (install/'install-state.json').write_text(json.dumps(manifest))
    result = subprocess.run([str(ROOT/'scripts/uninstall-codex-app-integration.sh'), '--install-dir', str(install)],
                            env=dict(os.environ, HOME=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode != 0
    assert (install/'install-state.json').exists() and runtime.exists()


def test_registration_only_removal_targets_saved_home_and_keeps_proxy(tmp_path):
    fixture = removal_fixture(tmp_path)
    install, runtime, state_path, log_path = fixture
    result = remove_fixture(fixture, tmp_path)
    assert result.returncode == 0, result.stderr
    assert (install/'run-mcp.sh').read_text() == 'core proxy' and runtime.exists()
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert {call['home'] for call in calls} == {str(tmp_path/'shared-home')}
    assert calls[0]['argv'] == ['plugin', 'list', '--json']
    assert calls[1]['argv'] == ['plugin', 'marketplace', 'list', '--json']
    assert json.loads(state_path.read_text())['installed'] == []
    assert json.loads((install/'install-state.json').read_text())['plugin']['enabled'] is False


def test_flat_metadata_from_cli_0161_is_reviewed_and_verified(runtime):
    for hook in runtime[3]['hooks']:
        handler = hook.pop('handler')
        hook.update(handlerType=handler['type'], command=handler['command'],
                    **{'async': handler['async']}, timeoutSec=600)
    plan = plan_for(runtime)
    assert all(h['timeoutSec'] == 600 for h in plan['hooks'])
    assert plan['declared_hooks']['hooks']['SessionStart'][0]['hooks'][0]['timeoutSec'] == 5
    plan['approved'] = True
    assert trust.apply_trust(plan)['hook_trust'] == 'observed_current'


def test_isolated_check_failure_still_produces_json_and_exit_code(tmp_path):
    broken = tmp_path/'codex'; broken.write_text('#!/bin/sh\nexit 7\n'); broken.chmod(0o755)
    output = tmp_path/'result.json'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/check-codex-plugin-isolated.py'),
                             '--codex-binary', str(broken), '--output', str(output),
                             '--timeout-seconds', '5'], capture_output=True, text=True, timeout=15)
    report = json.loads(output.read_text())
    assert result.returncode == report['exit_code'] == 1
    assert report['phase'] == 'version' and report['model_thread_login_requested'] is False
    assert report['history'] == 'unobserved'


def removal_fixture(tmp_path):
    install = tmp_path/'integration'; install.mkdir()
    runtime = tmp_path/'runtime'; runtime.mkdir()
    (install/'run-mcp.sh').write_text('core proxy')
    market = install/'marketplace'; market.mkdir()
    entry = {'pluginId': PLUGIN_ID, 'marketplaceName': 'agentstack-local',
             'marketplaceSource': {'sourceType': 'local', 'source': str(market)},
             'source': {'source': 'local', 'path': str(market/'plugins/agentstack-codex-app')}}
    state_path, log_path = tmp_path/'registry.json', tmp_path/'commands.jsonl'
    state_path.write_text(json.dumps({'installed': [entry], 'marketplaces': [
        {'name': 'agentstack-local', 'root': str(market), 'marketplaceSource': entry['marketplaceSource']}]}))
    binary = tmp_path/'codex'
    binary.write_text('#!'+sys.executable+'\n'
                     'import json, os, sys\nfrom pathlib import Path\n'
                     f'STATE=Path({str(state_path)!r})\nLOG=Path({str(log_path)!r})\n'+r'''
state=json.loads(STATE.read_text())
argv=sys.argv[1:]
with LOG.open('a') as stream:
    stream.write(json.dumps({'argv': argv, 'home': os.environ['CODEX_HOME']})+'\n')
if state.get('malformed'):
    print('invalid registry'); raise SystemExit(0)
if argv[:3] == ['plugin', 'marketplace', 'list']:
    print(json.dumps({'marketplaces': state['marketplaces']}))
elif argv[:2] == ['plugin', 'list']:
    print(json.dumps({'installed': state['installed']}))
elif argv[:2] == ['plugin', 'remove']:
    if state.get('fail_plugin'):
        raise SystemExit(6)
    state['installed']=[p for p in state['installed'] if p['pluginId'] != argv[2]]
elif argv[:3] == ['plugin', 'marketplace', 'remove']:
    if state.pop('fail_market_once', False):
        STATE.write_text(json.dumps(state)); raise SystemExit(7)
    state['marketplaces']=[m for m in state['marketplaces'] if m['name'] != argv[3]]
else:
    raise SystemExit(9)
STATE.write_text(json.dumps(state))
''')
    binary.chmod(0o755)
    manifest = {'tool': 'agentstack-codex-app', 'install_dir': str(install), 'runtime_dir': str(runtime),
                'shared_codex_home': str(tmp_path/'shared-home'), 'codex_binary': str(binary),
                'ownership': {'marketplace_root': str(market)},
                'plugin': {'enabled': True, 'id': PLUGIN_ID, 'marketplace_name': 'agentstack-local'},
                'service': {'kind': 'disabled'}}
    (install/'install-state.json').write_text(json.dumps(manifest))
    return install, runtime, state_path, log_path


def remove_fixture(fixture, tmp_path, registration_only=True):
    operation = 'agentstack_codex_app_remove_registration' if registration_only else 'agentstack_codex_app_remove'
    return subprocess.run(['/bin/bash', '-c', f'source "$1"; {operation} --install-dir "$2"',
                           'bash', str(ROOT/'scripts/lib/codex-app-integration.sh'), str(fixture[0])],
                          env=dict(os.environ, HOME=str(tmp_path), CODEX_HOME=str(tmp_path/'ambient-wrong-home')),
                          capture_output=True, text=True)


@pytest.mark.parametrize('bad_registry', ['foreign_root', 'duplicate_id', 'malformed'])
def test_current_unowned_or_unknown_registry_prevents_all_removals(tmp_path, bad_registry):
    fixture = removal_fixture(tmp_path)
    state = json.loads(fixture[2].read_text())
    if bad_registry == 'foreign_root':
        root = tmp_path/'foreign-marketplace'
        state['marketplaces'][0].update(root=str(root), marketplaceSource={'sourceType': 'local', 'source': str(root)})
        state['installed'][0].update(marketplaceSource={'sourceType': 'local', 'source': str(root)},
                                     source={'source': 'local', 'path': str(root/'plugins/agentstack-codex-app')})
    elif bad_registry == 'duplicate_id':
        state['installed'].append(copy.deepcopy(state['installed'][0]))
    else:
        state['malformed'] = True
    fixture[2].write_text(json.dumps(state))
    manifest_before = (fixture[0]/'install-state.json').read_bytes()
    result = remove_fixture(fixture, tmp_path, registration_only=False)
    assert result.returncode != 0
    calls = [json.loads(line)['argv'] for line in fixture[3].read_text().splitlines()]
    assert all('remove' not in call for call in calls)
    assert json.loads(fixture[2].read_text()) == state
    assert (fixture[0]/'install-state.json').read_bytes() == manifest_before
    assert fixture[1].exists()


def test_retry_after_partial_plugin_removal_keeps_payload_until_observed(tmp_path):
    fixture = removal_fixture(tmp_path)
    state = json.loads(fixture[2].read_text()); state['fail_market_once'] = True
    fixture[2].write_text(json.dumps(state))
    first = remove_fixture(fixture, tmp_path)
    assert first.returncode != 0 and (fixture[0]/'run-mcp.sh').exists() and fixture[1].exists()
    assert json.loads(fixture[2].read_text())['installed'] == []
    second = remove_fixture(fixture, tmp_path)
    assert second.returncode == 0, second.stderr
    calls = [json.loads(line)['argv'] for line in fixture[3].read_text().splitlines()]
    assert sum(call[:2] == ['plugin', 'remove'] for call in calls) == 1
    assert json.loads(fixture[2].read_text())['marketplaces'] == []


def test_owned_plugin_command_failure_retains_registry_payload_and_runtime(tmp_path):
    fixture = removal_fixture(tmp_path)
    state = json.loads(fixture[2].read_text()); state['fail_plugin'] = True
    fixture[2].write_text(json.dumps(state))
    manifest_before = (fixture[0]/'install-state.json').read_bytes()
    result = remove_fixture(fixture, tmp_path, registration_only=False)
    assert result.returncode != 0
    calls = [json.loads(line)['argv'] for line in fixture[3].read_text().splitlines()]
    assert any(call[:2] == ['plugin', 'remove'] for call in calls)
    assert not any(call[:3] == ['plugin', 'marketplace', 'remove'] for call in calls)
    assert json.loads(fixture[2].read_text()) == state
    assert (fixture[0]/'install-state.json').read_bytes() == manifest_before
    assert (fixture[0]/'run-mcp.sh').exists() and fixture[1].exists()


def test_shared_marketplace_retains_other_plugin_and_snapshot_payload(tmp_path):
    fixture = removal_fixture(tmp_path)
    state = json.loads(fixture[2].read_text())
    other = dict(state['installed'][0], pluginId='other@agentstack-local')
    state['installed'].append(other); fixture[2].write_text(json.dumps(state))
    result = remove_fixture(fixture, tmp_path, registration_only=False)
    assert result.returncode == 0, result.stderr
    final = json.loads(fixture[2].read_text())
    assert final['installed'] == [other] and final['marketplaces'] == state['marketplaces']
    report = json.loads(result.stdout.splitlines()[-1])
    assert report['marketplace_retained'] is True and report['payload_removed'] is False
    assert (fixture[0]/'run-mcp.sh').read_text() == 'core proxy'


@pytest.mark.parametrize('difference', ['uid', 'command', 'start', 'unknown', 'legacy'])
def test_unowned_or_unknown_pid_never_receives_a_stop_signal(tmp_path, monkeypatch, difference):
    runner = tmp_path/'bin/run-bridge'
    saved = {'pid': 424242, 'uid': os.getuid(), 'start': 'fixture-start', 'command': '/bin/bash '+str(runner)}
    current = dict(saved)
    if difference in {'uid', 'command', 'start'}:
        current[difference] = {'uid': os.getuid()+1, 'command': '/bin/bash /unrelated/runner', 'start': 'new-start'}[difference]
    def probe(pid):
        if difference == 'unknown': raise ownership.OwnershipUnknown('inspection unavailable')
        return current
    monkeypatch.setattr(ownership, 'process_identity', probe)
    signals = []; monkeypatch.setattr(ownership.os, 'kill', lambda pid, sig: signals.append(sig))
    pidfile = tmp_path/'supervisor.pid'; pidfile.write_text('424242\n')
    with pytest.raises(ownership.OwnershipUnknown):
        ownership.stop_supervisor(pidfile, runner, None if difference == 'legacy' else saved)
    assert signals == [] and pidfile.exists()


def test_verified_supervisor_stops_and_clears_only_its_owned_receipt(tmp_path, monkeypatch):
    import signal
    runner = tmp_path/'bin/run-bridge'
    saved = {'pid': 424242, 'uid': os.getuid(), 'start': 'fixture-start', 'command': '/bin/bash '+str(runner)}
    state = {'current': saved}
    monkeypatch.setattr(ownership, 'process_identity', lambda pid: state['current'])
    signals = []
    def send(pid, sig):
        signals.append((pid, sig)); state['current'] = None
    monkeypatch.setattr(ownership.os, 'kill', send)
    pidfile = tmp_path/'supervisor.pid'; pidfile.write_text('424242\n')
    identity_path = Path(str(pidfile)+'.identity.json'); identity_path.write_text(json.dumps(saved))
    ownership.stop_supervisor(pidfile, runner, saved)
    assert signals == [(424242, signal.SIGTERM)] and not pidfile.exists() and not identity_path.exists()


def test_pid_identity_changed_after_term_is_preserved_without_kill(tmp_path, monkeypatch):
    import signal
    runner = tmp_path/'bin/run-bridge'
    saved = {'pid': 424242, 'uid': os.getuid(), 'start': 'fixture-start', 'command': '/bin/bash '+str(runner)}
    state = {'current': dict(saved)}
    monkeypatch.setattr(ownership, 'process_identity', lambda pid: state['current'])
    signals = []
    def send(pid, sig):
        signals.append(sig); state['current']['start'] = 'replacement-start'
    monkeypatch.setattr(ownership.os, 'kill', send)
    pidfile = tmp_path/'supervisor.pid'; pidfile.write_text('424242\n')
    with pytest.raises(ownership.OwnershipUnknown):
        ownership.stop_supervisor(pidfile, runner, saved)
    assert signals == [signal.SIGTERM] and pidfile.exists()


@pytest.mark.parametrize('foreign', ['plist', 'loaded_job', 'unknown'])
def test_launchd_label_alone_does_not_authorize_stopping(tmp_path, monkeypatch, foreign):
    import plistlib
    from types import SimpleNamespace
    root = tmp_path/'integration'; root.mkdir()
    expected = ['/bin/bash', str(root/'bin/run-bridge')]
    definition = {'Label': 'fixture.bridge', 'ProgramArguments': expected}
    if foreign == 'plist': definition['ProgramArguments'] = ['/bin/bash', '/unrelated/runner']
    plist = tmp_path/'bridge.plist'; plist.write_bytes(plistlib.dumps(definition))
    args = ['/bin/bash', '/unrelated/runner'] if foreign == 'loaded_job' else expected
    result = SimpleNamespace(returncode=1 if foreign == 'unknown' else 0,
                             stderr='inspection unavailable', stdout='\n arguments = {\n'+'\n'.join(args)+'\n }\n')
    calls = []
    def run(argv, **kwargs):
        calls.append(argv); return result
    monkeypatch.setattr(ownership.subprocess, 'run', run)
    with pytest.raises(ownership.OwnershipUnknown):
        ownership.check_launchd({'label': 'fixture.bridge', 'path': str(plist)}, root)
    assert all(call[:2] == ['launchctl', 'print'] for call in calls) and plist.exists()


def test_verified_launchd_definition_and_observed_absence_are_distinct(tmp_path, monkeypatch):
    import plistlib
    from types import SimpleNamespace
    root = tmp_path/'integration'; root.mkdir()
    args = ['/bin/bash', str(root/'bin/run-bridge')]
    plist = tmp_path/'bridge.plist'
    plist.write_bytes(plistlib.dumps({'Label': 'fixture.bridge', 'ProgramArguments': args}))
    result = SimpleNamespace(returncode=0, stderr='', stdout='\n arguments = {\n'+'\n'.join(args)+'\n }\n')
    monkeypatch.setattr(ownership.subprocess, 'run', lambda *a, **kw: result)
    assert ownership.check_launchd({'label': 'fixture.bridge', 'path': str(plist)}, root) is True
    result.returncode = 113; result.stderr = 'Could not find service "fixture.bridge" in domain for user gui'
    assert ownership.check_launchd({'label': 'fixture.bridge', 'path': str(plist)}, root) is False
