"""Explicit Mail-only MCP resource proposal, not a running/native proxy."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat

import pytest

from test_runtime_legacy_configure import prepared as configure_fixture, plan
from test_runtime_legacy_configure_cli import prepared as cli_fixture, invoke
from test_runtime_legacy_context import prepared as context_fixture
from test_runtime_configure_ownership import snapshot
from local_model_auth import LocalModelError


@pytest.fixture
def prepared(configure_fixture):
    # The common fixture now mirrors the installer's complete source package.
    return configure_fixture


def proxy_output(prepared, value):
    client = prepared[0]
    path = client.profile_path.with_suffix('.preview-home') / 'local-model-mcp.json'
    found = [row for row in value['output_plan']['outputs'] if row['path'] == str(path)]
    assert len(found) == 1, 'LEGACY_EXPLICIT_PROXY_OUTPUT_MISSING'
    return path, found[0]


@pytest.mark.parametrize('base', ['default', 'mail-only'])
def test_readonly_mail_only_proxy_resource_pins_python_and_existing_proxy_sources(prepared, monkeypatch, base):
    client, ws, args, _, _, _ = prepared
    ws.caller['base'] = base
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'fixture-ambient-secret-never-copy')
    before = snapshot(ws.tmp)
    value = plan(prepared)
    path, output = proxy_output(prepared, value)
    assert snapshot(ws.tmp) == before and not path.exists()
    root = Path(client.config['wrapper_root'])
    definition = {'command': args['python'], 'args': ['-I', '-B',
        str(root / 'bin/lib/runtime_legacy_preview_cli.py'), 'entry', 'proxy', 'FixtureChild']}
    assert json.loads(output['content']) == {'mcpServers': {
        'orrery-mail': definition, 'fixture_mail': definition}}
    assert output['kind'] == 'provider-settings' and output['mode'] == 0o600
    assert 'fixture-ambient-secret-never-copy' not in json.dumps(value)
    # The interpreter is pinned even for an unauthenticated local profile.
    assert value['intent']['model']['resolved']['auth'] is None
    proof = value['intent']['configuration_evidence']['runtime_python']
    info = Path(args['python']).lstat()
    assert proof == {'path': args['python'], 'uid': info.st_uid,
        'mode': stat.S_IMODE(info.st_mode), 'sha256': hashlib.sha256(Path(args['python']).read_bytes()).hexdigest()}
    sources = value['intent']['configuration_evidence']['renderer_sources']
    for filename in ('mcp_server.py', 'agent_mail_client.py', 'hook_entry.py', 'identity_store.py', 'snapshot.py'):
        source = root / 'integrations/codex_app/src/agentstack_codex_app' / filename
        assert sources[str(source)]['state'] == 'present'
        assert sources[str(source)]['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_proxy_settings_are_owned_by_the_same_explicit_manifest(prepared):
    client, ws, args, configure, _, _ = prepared
    value = plan(prepared)
    path, output = proxy_output(prepared, value)
    result = configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    assert result['launch_admissible'] is False
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    assert path.read_text() == output['content']
    manifest = json.loads((Path(client.config['wrapper_root']) / 'install-state.json').read_text())
    assert manifest['configure_owned'][str(path)] == output['after']
    assert str(path) in manifest['owned_files']
    before = snapshot(ws.tmp)
    assert configure.apply_local_configuration(client, value, approved_id=value['plan_id'])['changed'] is False
    assert snapshot(ws.tmp) == before


@pytest.mark.parametrize('tools', [{'mcp': ['fixture-extra']}, {'browser': True}, {'screen': 'operate'}])
def test_preview_nonempty_tools_refused_instead_of_dropped_before_outputs(prepared, tools):
    client, ws, _, _, _, _ = prepared
    ws.caller['tools'] = tools
    before = snapshot(ws.tmp)
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_ADAPTER_UNSUPPORTED$'):
        plan(prepared)
    assert snapshot(ws.tmp) == before


def test_real_operator_rejects_nonempty_tools_before_parent_mail_auth(cli_fixture):
    _, ws, original, calls, _ = cli_fixture
    payload = deepcopy(original)
    payload['caller']['tools'] = {'mcp': ['fixture-extra']}
    before = snapshot(ws.tmp)
    result = invoke(cli_fixture, payload, 'instruction-configure', 'operator', 'claude', 'local-plan')
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr.strip() == 'runtime-client: LOCAL_MODEL_ADAPTER_UNSUPPORTED'
    assert calls == [] and snapshot(ws.tmp) == before


@pytest.mark.parametrize('mutation', ['bytes', 'mode', 'symlink', 'missing-proof'])
def test_runtime_python_drift_stops_projection_even_without_profile_auth(prepared, mutation):
    client, ws, args, configure, _, _ = prepared
    value = plan(prepared)
    proxy_output(prepared, value)
    configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    binary = Path(args['python'])
    if mutation == 'bytes':binary.chmod(0o700); binary.write_text('# changed runtime\n'); binary.chmod(0o500)
    elif mutation == 'mode':binary.chmod(0o700)
    elif mutation == 'symlink':
        moved = binary.with_name('other-python'); binary.rename(moved); binary.symlink_to(moved)
    else:
        from local_model_auth import intent_digest
        value['intent']['configuration_evidence'].pop('runtime_python')
        value['intent']['intent_digest'] = intent_digest(value['intent'])
    api = runpy.run_path(str(Path(client.config['wrapper_root']) / 'bin/lib/runtime_legacy_mail.py'))
    with pytest.raises(LocalModelError):api['_registration_projection'](value['intent'], ws.caller)


@pytest.mark.parametrize('mutation', ['bytes', 'missing', 'symlink', 'mode', 'hardlink'])
def test_changed_proxy_settings_fail_before_registration(prepared, mutation):
    client, ws, _, configure, _, _ = prepared
    value = plan(prepared)
    path, _ = proxy_output(prepared, value)
    configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    api = runpy.run_path(str(Path(client.config['wrapper_root']) / 'bin/lib/runtime_legacy_mail.py'))
    assert api['_registration_projection'](value['intent'], ws.caller) == ws.context['legacy_context']
    if mutation == 'bytes':path.write_text('{}')
    elif mutation == 'missing':path.unlink()
    elif mutation == 'symlink':path.unlink(); path.symlink_to(ws.settings)
    elif mutation == 'mode':path.chmod(0o644)
    else:os.link(path, path.with_name('proxy-alias'))
    before = snapshot(ws.tmp)
    with pytest.raises(LocalModelError):api['_registration_projection'](value['intent'], ws.caller)
    assert snapshot(ws.tmp) == before and not list(ws.tmp.rglob('agent_token_*'))


@pytest.mark.parametrize('mutation', ['missing', 'symlink'])
def test_missing_or_aliased_existing_proxy_source_cannot_be_configured(prepared, mutation):
    client, ws, _, _, _, _ = prepared
    source = Path(client.config['wrapper_root']) / 'integrations/codex_app/src/agentstack_codex_app/mcp_server.py'
    if mutation == 'missing':source.unlink()
    else:
        moved = source.with_name('saved.py'); source.rename(moved); source.symlink_to(moved)
    before = snapshot(ws.tmp)
    with pytest.raises(LocalModelError):plan(prepared)
    assert snapshot(ws.tmp) == before


@pytest.mark.parametrize('field,value', [('sha256', '0' * 64), ('mode', True), ('uid', True), ('extra', 1)])
def test_common_resource_checker_checks_runtime_proof_independently(prepared, field, value):
    from local_model_auth import check_configuration_evidence
    planned = plan(prepared)
    evidence = deepcopy(planned['intent']['configuration_evidence'])
    check_configuration_evidence(evidence)
    evidence['runtime_python'][field] = value
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'):
        check_configuration_evidence(evidence)
