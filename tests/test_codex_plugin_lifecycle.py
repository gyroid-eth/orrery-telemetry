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
    install = tmp_path/'integration'; install.mkdir()
    runtime = tmp_path/'runtime'; runtime.mkdir()
    proxy = install/'run-mcp.sh'; proxy.write_text('core proxy')
    binary = tmp_path/'codex'; seen = tmp_path/'seen'
    binary.write_text(f'#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME" >> "{seen}"\nif [ "$2" = list ]; then echo \'{{"installed": []}}\'; fi\nexit 0\n'); binary.chmod(0o755)
    manifest = {'tool': 'agentstack-codex-app', 'install_dir': str(install), 'runtime_dir': str(runtime),
                'shared_codex_home': str(tmp_path/'shared-home'), 'codex_binary': str(binary),
                'plugin': {'enabled': True, 'id': PLUGIN_ID, 'marketplace_name': 'agentstack-local'},
                'service': {'kind': 'disabled'}}
    (install/'install-state.json').write_text(json.dumps(manifest))
    result = subprocess.run(['/bin/bash', '-c', 'source "$1"; agentstack_codex_app_remove_registration --install-dir "$2"',
                             'bash', str(ROOT/'scripts/lib/codex-app-integration.sh'), str(install)],
                            env=dict(os.environ, HOME=str(tmp_path), CODEX_HOME=str(tmp_path/'ambient-wrong-home')),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert proxy.read_text() == 'core proxy' and runtime.exists()
    assert seen.read_text().splitlines() == [str(tmp_path/'shared-home')]*3
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
