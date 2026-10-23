"""Connection identity must survive the public catalog and real native generators.

Synthetic runtimes only; their executables and credential commands never run.
"""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import subprocess
from shutil import copy2
from unittest.mock import patch

import pytest

import test_dsh_thin_config as dsh_fixture
import test_hermes_thin_config as hermes_fixture
from dashboard.managed_catalog import catalog_providers
from hooks import harness_child, child_resume, llm_secret_reader, native_connections
from dashboard import harness_runtime, server, provider_server
from test_spawn_dry_run import spawn_http, preview_env
import test_native_child_resume as resume_fixture


MODEL = 'shared-wire-model'
HOSTS = {
    'strata': ('https://original.invalid', 'original-key', 131072, 4096),
    'strata-epyc': ('https://second.invalid', 'second-key', 262144, 8192),
}


@contextmanager
def configured(harness):
    case = dsh_fixture.ThinTests() if harness == 'dsh' else hermes_fixture.ThinConfig()
    case.setUp()
    try:
        module = dsh_fixture.thin if harness == 'dsh' else hermes_fixture.config
        root = case.root if harness == 'dsh' else case.base
        document = case.doc if harness == 'dsh' else case.document
        manifest = module.read(case.manifest)
        manifest['backends'] = {
            key: {'model': MODEL, 'models': [MODEL, key + '-helper'],
                  **({'kind': 'strata', 'label': 'Second local'} if key != 'strata' else {})}
            for key in HOSTS
        }
        module.save(case.manifest, manifest) if harness == 'dsh' else case.save(case.manifest, manifest)
        if harness == 'dsh':
            template = module.read(case.template)
            module.save(template['backend_file'], {'backends': {
                key: {'base_url': url, 'model': MODEL,
                      'keychain': {'service': credential, 'account': 'api-key'},
                      'model_definition': {'id': MODEL, 'contextWindow': context, 'maxTokens': maximum}}
                for key, (url, credential, context, maximum) in HOSTS.items()
            }})
        else:
            template = module.read(case.template)
            template['profiles'] = {
                key: {'provider': 'fixture-' + key, 'model': MODEL,
                      'base_url': url + '/v1', 'api_mode': 'chat_completions',
                      'context_length': context,
                      'key_cmd': '/fixture/no-execution --ref ' + credential}
                for key, (url, credential, context, _) in HOSTS.items()
            }
            case.save(case.template, template)
        yield module, case, root, document
    finally:
        case.doCleanups()


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_same_wire_model_keeps_selected_endpoint_credential_and_limits(harness):
    with configured(harness) as (module, case, root, document):
        seen = []
        with patch('subprocess.run', side_effect=AssertionError('No executable or credential invocation')):
            for index, (connection, (url, credential, context, maximum)) in enumerate(HOSTS.items()):
                state = root / ('selection-' + str(index))
                state.mkdir()
                plan = module.generate({**document, 'backend_id': connection, 'model': MODEL,
                    'agent_name': 'SyntheticHost' + str(index), 'state_dir': str(state)})
                if harness == 'dsh':
                    rows = module.read(Path(plan['config_dir']) / 'native.patch.json')
                    provider = next(row['config'] for row in rows if row.get('id') == 'llm-deepseek')
                    auth = next(row['config'] for group in rows for row in group.get('insert', [])
                                if row['id'] == 'orrery-keychain-credentials')
                    assert provider['baseURL'] == url
                    assert provider['models'] == [{'id': MODEL, 'contextWindow': context, 'maxTokens': maximum}]
                    assert (auth['service'], auth['account']) == (credential, 'api-key')
                    assert next(row['config']['model'] for row in rows if row.get('id') == 'agent-default-model') == MODEL
                else:
                    overlay = module.read(Path(plan['config_dir']) / 'config.yaml')
                    provider = overlay['providers'][plan['argv'][3]]
                    assert provider['base_url'] == url + '/v1'
                    assert provider['context_length'] == context
                    auth = module.read(plan['env']['AGENTSTACK_LLM_KEY_DEFINITIONS'])
                    assert auth['backends']['hermes']['auth_command'] == ['/fixture/no-execution', '--ref', credential]
                    assert plan['argv'][plan['argv'].index('--model') + 1] == MODEL
                seen.append(plan)
        assert seen[0]['config_dir'] != seen[1]['config_dir']


def test_catalog_exposes_declared_connection_without_leaking_routes():
    backends = {
        'deepseek': {'model': 'cloud-model'},
        'strata': {'model': MODEL},
        'strata-epyc': {'kind': 'strata', 'label': 'Second local', 'server': 'Second host',
                        'model': MODEL, 'base_url': 'https://must-stay-private.invalid'},
    }
    availability = {key: {h: {'available': True} for h in ('dsh', 'hermes')} for key in backends}
    rows = catalog_providers(backends, availability, mode='legacy', enabled=True)
    assert [row['id'] for row in rows] == ['deepseek', 'strata', 'strata-epyc']
    assert rows[-1]['kind'] == 'strata'
    assert rows[-1]['label'] == 'Second local'
    assert rows[-1]['locality'] == 'local'
    assert rows[-1]['models'][0]['id'] == MODEL
    assert 'must-stay-private' not in json.dumps(rows)


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_legacy_generator_control_and_foreign_helper_refusal(harness):
    with configured(harness) as (module, case, root, document):
        doc = {**document, 'backend_id': 'strata', 'model': MODEL}
        assert module.validate_input(doc)
        original = copy.deepcopy(module.read(case.manifest))
        with pytest.raises(ValueError, match='invalid_helper_model'):
            module.validate_input({**doc, 'helper_model': 'strata-epyc-helper'})
        assert module.read(case.manifest) == original


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_invalid_connection_cannot_fall_back_to_existing_route(harness):
    with configured(harness) as (module, case, root, document):
        baseline = module.read(case.manifest)
        cases = [
            ('unlisted', baseline),
            ('STRATA-EPYC', baseline),
            ('strata-epyc', {**baseline, 'backends': {'strata-epyc': {'model': MODEL}}}),
            ('strata-epyc', {**baseline, 'backends': {'strata-epyc': {'kind': 'other', 'model': MODEL}}}),
            ('strata', {**baseline, 'backends': {'strata': {'kind': 'deepseek', 'model': MODEL}}}),
            ('codex', {**baseline, 'backends': {'codex': {'kind': 'strata', 'model': MODEL}}}),
        ]
        for connection, manifest in cases:
            case.manifest.write_text(json.dumps(manifest))
            with pytest.raises(ValueError):
                module.generate({**document, 'backend_id': connection, 'model': MODEL})
        case.manifest.write_text('{"backends":{"strata":{},"strata":{}}}')
        with pytest.raises(ValueError, match='ambiguous_connection_configuration'):
            native_connections.read_manifest(case.manifest)
        with pytest.raises(ValueError):
            module.validate_input({**document, 'model': MODEL})


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_http_preview_retains_exact_connection_and_harness(preview_env, spawn_http, monkeypatch, harness):
    rows = catalog_providers({key: {'kind': 'strata', 'model': MODEL} for key in HOSTS},
        {key: {harness: {'available': True}} for key in HOSTS}, mode='legacy', enabled=True)
    monkeypatch.setattr(server, '_spawn_catalog_providers', lambda: rows)
    monkeypatch.setattr(provider_server, '_spawn_catalog_providers', lambda: rows)
    for connection in HOSTS:
        payload = {'standalone': True, 'dry_run': True, 'task': 'Synthetic only',
                   'dir': str(preview_env[0]), 'provider': connection, 'harness': harness, 'model': MODEL}
        code, answer = spawn_http(payload)
        assert code == 200 and answer['ok'], answer
        assert answer['provider'] == connection
        assert answer['argv'][answer['argv'].index('--backend') + 1] == connection
        assert answer['argv'][answer['argv'].index('--harness') + 1] == harness
    for changes in ({'provider': 'undefined'}, {'provider': ' Strata-epyc '},
                    {'model': 'unlisted'}, {'helper_model': 'foreign-helper'}, {'harness': 'codex'}):
        code, answer = spawn_http({**payload, **changes})
        assert code == 400 and not answer['ok']


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_resume_and_runtime_metadata_keep_connection_identity(tmp_path, harness):
    runtime, state, document, _ = resume_fixture.child(tmp_path, 'FirstAgent', harness, 'owned-session')
    state.update(harness_backend='strata-epyc', harness_backend_kind='strata')
    document.update(backend_id='strata-epyc', backend_kind='strata')
    directory = Path(state['harness_state_dir'])
    child_resume._atomic_json(directory / 'input.json', document)
    child_resume._atomic_json(runtime / 'child-agents/FirstAgent.json', state)
    retained = child_resume.native_resume_input(runtime, state)
    assert (retained['backend_id'], retained['backend_kind'], retained['resume_session_id']) == (
        'strata-epyc', 'strata', 'owned-session')
    assert harness_runtime.metadata(runtime, 'FirstAgent', harness)['provider'] == 'strata-epyc'
    for changes in ({'backend_id': 'strata'}, {'backend_kind': 'deepseek'}):
        child_resume._atomic_json(directory / 'input.json', {**document, **changes})
        with pytest.raises(child_resume.ResumeStateError, match='Native input belongs'):
            child_resume.native_resume_input(runtime, state)
    state.pop('harness_backend_kind')
    with pytest.raises(child_resume.ResumeStateError, match='Native connection identity'):
        child_resume.native_resume_input(runtime, state)


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_duplicate_route_entries_cannot_choose_the_last_host(harness):
    with configured(harness) as (module, case, root, document):
        if harness == 'dsh':
            target = Path(module.read(case.template)['backend_file'])
            target.write_text('{"backends":{"strata":{},"strata":{}}}')
        else:
            case.template.write_text('{"profiles":{"strata":{},"strata":{}}}')
        with pytest.raises(ValueError, match='ambiguous_connection_configuration'):
            module.generate({**document, 'backend_id': 'strata', 'model': MODEL})


def test_linux_reference_resolution_is_connection_specific(tmp_path):
    references = {key: {'store': 'secret-service', 'attributes': {'service': key, 'account': 'api-key'}}
                  for key in HOSTS}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'backends': {key: {'kind': 'strata', 'credential_ref': ref}
                                                for key, ref in references.items()}}))
    with patch('subprocess.run', side_effect=AssertionError('No key lookup')):
        for key in HOSTS:
            assert llm_secret_reader.manifest_ref(manifest, key) == references[key]
            assert llm_secret_reader.command(Path(__file__).resolve().parents[1], manifest, key, sys.executable)[-1] == key
        with pytest.raises(ValueError, match='invalid_credential_ref'):
            llm_secret_reader.manifest_ref(manifest, 'unknown')


@pytest.mark.parametrize('harness', ['dsh', 'hermes'])
def test_real_static_admission_checks_each_connection_without_runtime_execution(monkeypatch, harness):
    from test_managed_harness_install import CAFixture, fixture_stamp
    real_popen = subprocess.Popen
    with configured(harness) as (module, case, root, document):
        product = Path(document['product_root'])
        template = module.read(case.template)
        ca = None
        try:
            if harness == 'hermes':
                ca = CAFixture(None, root)
                ca.source(case.source)
                ca.ca.apply(ca.repo, case.source)
                generation = Path(template['hermes_python']).parent.parent
                paths = ca.material(case.runtime, case.source, generation)
                stamp = fixture_stamp(case.source)
                template.update(ca_recipe=ca.ca.load(ca.repo)[2],
                    ca_evidence=ca.ca.runtime_evidence(ca.repo, case.runtime, case.source,
                        generation, ca.target, paths), **stamp)
                product = ca.repo
                receipt = root / 'plain-receipt.json'
                case.save(receipt, {'harness': harness, 'variant': 'plain', 'thin_config_contract': 1,
                                   'complete': True, 'details': dict(template)})
                template['generation_receipt'] = str(receipt)
                case.save(case.template, template)
            else:
                target = product / 'integrations/dsh/thin_config.py'
                copy2(Path(module.__file__), target)
                (product / 'hooks').mkdir()
                copy2(Path(native_connections.__file__), product / 'hooks/native_connections.py')
            manifest = module.read(case.manifest)
            manifest['enabled'] = True
            manifest['harnesses'][harness]['python'] = sys.executable
            case.manifest.write_text(json.dumps(manifest))
            monkeypatch.setattr(harness_child, 'ROOT', product)
            harness_child._VALIDATION_CACHE.clear()
            # The fixture forbids native execution. Permit only the admission
            # subprocess here: its argv is fixed to this Python + validate_input.
            with patch('subprocess.Popen', real_popen):
                for connection in HOSTS:
                    result = harness_child.selection(str(case.manifest), harness, connection, MODEL)
                    assert result['connection_kind'] == 'strata'
            with pytest.raises(ValueError, match='model_mismatch'):
                harness_child.selection(str(case.manifest), harness, 'strata-epyc', 'strata-helper')
        finally:
            if ca:
                ca.stack.close()
