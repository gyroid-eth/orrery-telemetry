"""Pure comparison fixtures; never proof of CLI/native permission observation."""
from copy import deepcopy
import importlib
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bin/lib'))
auth = importlib.import_module('local_model_auth')


def snapshot(provider='codex', version='fixture-v1', binary='a'*64):
    return {'provider':provider,
        'cli':{'path':'/fixture/install/bin/cli', 'uid':501, 'mode':0o500,
            'sha256':binary, 'version':version,
            'origin':{'path':'/fixture/install', 'uid':501}},
        'effective_scope':{'cli_version':version, 'read':['/fixture/task'],
            'edit':['/fixture/scratch'], 'bash':['fixture-a && fixture-b'],
            'mail_tools':['mcp__fixture__send_message'], 'built_in_read':['/fixture/cwd'],
            'sandbox':{'network':False}, 'hooks':{'reservation':'required'},
            'plugins':[], 'workspace_trust':'approved'},
        'resources':{'runner':'b'*64, 'dependencies':{'/fixture/dependency':'c'*64},
            'settings':{'/fixture/settings':'d'*64}, 'renderers':{'/fixture/renderer':'e'*64},
            'skills':{'/fixture/skill':'f'*64}},
        'derivation':{'model':'fixture-model', 'endpoint':'http://127.0.0.1:12345/v1',
            'auth':{'mechanism':'fixture-command', 'digest':'1'*64},
            'context':{'window':32768, 'output':4096}}}


@pytest.mark.parametrize('provider', ['codex','claude'])
@pytest.mark.parametrize('version,binary', [('fixture-v1','a'*64),
    ('fixture-v2','a'*64), ('fixture-v2','2'*64)])
def test_version_only_comparison_preserves_approval_and_reports_both_sides(provider, version, binary):
    approved, observed = snapshot(provider), snapshot(provider,version,binary)
    before = json.dumps(approved,sort_keys=True)
    result = auth.compare_cli_configurations(approved,observed)
    assert json.dumps(approved,sort_keys=True) == before
    assert result['provider'] == provider
    assert result['approved_cli'] == approved['cli'] and result['observed_cli'] == observed['cli']
    assert result['version_changed'] is (version != 'fixture-v1')
    assert result['bytes_changed'] is (binary != 'a'*64)
    # Independent literal golden semantic digest, not a product normalizer.
    semantic = {k:v for k,v in approved['effective_scope'].items() if k != 'cli_version'}
    import hashlib
    def digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
            ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    assert result['scope_sha256'] == digest(semantic)
    assert result['resources_sha256'] == digest(approved['resources'])
    assert result['derivation_sha256'] == digest(approved['derivation'])
    assert result['proof_sha256'] == digest({k:v for k,v in result.items() if k!='proof_sha256'})


@pytest.mark.parametrize('mutation', ['same-version-bytes','path','origin','uid','writable',
    'not-executable','relative','version-empty','unknown-cli-key','unknown-snapshot-key',
    'nan','bool-uid'])
def test_invalid_cli_observation_never_becomes_a_version_exception(mutation):
    approved, observed = snapshot(), snapshot(version='fixture-v2',binary='2'*64)
    if mutation == 'same-version-bytes':observed['cli']['version']='fixture-v1'
    elif mutation == 'path':observed['cli']['path']='/fixture/install/bin/other'
    elif mutation == 'origin':observed['cli']['origin']['path']='/other-install'
    elif mutation == 'uid':observed['cli']['uid']=502
    elif mutation == 'writable':observed['cli']['mode']=0o777
    elif mutation == 'not-executable':observed['cli']['mode']=0o600
    elif mutation == 'relative':observed['cli']['path']='relative-cli'
    elif mutation == 'version-empty':observed['cli']['version']=''
    elif mutation == 'unknown-cli-key':observed['cli']['verified']=True
    elif mutation == 'unknown-snapshot-key':observed['verified']=True
    elif mutation == 'nan':observed['effective_scope']['unknown']=float('nan')
    else:observed['cli']['uid']=True
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_CLI_CHANGED$'):
        auth.compare_cli_configurations(approved,observed)


@pytest.mark.parametrize('field', ['read','edit','bash','mail_tools','built_in_read',
    'sandbox','hooks','plugins','workspace_trust','new-native-effect'])
def test_one_scope_difference_is_not_erased_with_the_version(field):
    approved, observed = snapshot(), snapshot(version='fixture-v2',binary='2'*64)
    observed['effective_scope'][field] = ['different']
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE$'):
        auth.compare_cli_configurations(approved,observed)


@pytest.mark.parametrize('version', ['fixture-v1','fixture-v2'])
@pytest.mark.parametrize('resource', ['runner','dependencies','settings','renderers','skills'])
def test_other_resource_drift_is_not_relaxed_by_a_version_change(version,resource):
    approved, observed = snapshot(), snapshot(version=version)
    observed['resources'][resource] = 'changed'
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_AUTH_FAILED$'):
        auth.compare_cli_configurations(approved,observed)


@pytest.mark.parametrize('field', ['model','endpoint','auth','context'])
def test_model_endpoint_auth_and_context_derivation_still_have_to_match(field):
    approved, observed = snapshot(), snapshot(version='fixture-v2',binary='2'*64)
    observed['derivation'][field] = 'changed'
    with pytest.raises(auth.LocalModelError,match='^LOCAL_MODEL_INTENT_CHANGED$'):
        auth.compare_cli_configurations(approved,observed)


def test_comparison_does_not_interpret_or_reorder_native_bash_rules():
    approved, observed = snapshot(), snapshot(version='fixture-v2')
    approved['effective_scope']['bash']=['first','second']
    observed['effective_scope']['bash']=['second','first']
    with pytest.raises(auth.LocalModelError,match='PERMISSION_PLAN_UNSATISFIABLE'):
        auth.compare_cli_configurations(approved,observed)


def with_proposal(value):
    scope = value['effective_scope']
    proposal = {'schema_version':1, 'provider':value['provider'],
        'declared_cli_version':value['cli']['version'], 'requested_scope':deepcopy(scope),
        'native_settings':{'fixture-native-rules':['literal-rule']},
        'built_in_operations':{'status':'unverified'}, 'source_closure':{'status':'unverified'},
        'settings_sources':[], 'requires_native_validation':True, 'launch_admissible':False}
    proposal['plan_digest'] = hashlib.sha256(json.dumps(proposal,sort_keys=True,
        separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    scope['native_proposal'] = proposal
    return value


@pytest.mark.parametrize('mutation', [None,'native_settings','built_in_operations',
    'source_closure','new-native-effect','bad-digest'])
def test_only_nested_version_provenance_is_removed_not_native_effects(mutation):
    approved = with_proposal(snapshot())
    observed = with_proposal(snapshot(version='fixture-v2',binary='2'*64))
    if mutation is not None:
        proposal = observed['effective_scope']['native_proposal']
        proposal[mutation] = 'changed'
        if mutation != 'bad-digest':
            proposal['plan_digest'] = hashlib.sha256(json.dumps(
                {k:v for k,v in proposal.items() if k!='plan_digest'},sort_keys=True,
                separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        with pytest.raises(auth.LocalModelError,match='PERMISSION_PLAN_UNSATISFIABLE'):
            auth.compare_cli_configurations(approved,observed)
    else:
        result = auth.compare_cli_configurations(approved,observed)
        assert result['version_changed'] is True and result['bytes_changed'] is True


def test_comparison_never_grants_native_admission_or_executes_a_cli(monkeypatch):
    import subprocess
    import runtime_native_scope
    def forbidden(*a,**kw):pytest.fail('pure comparison executed an external command')
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    approved = with_proposal(snapshot())
    result = auth.compare_cli_configurations(approved,with_proposal(snapshot(version='fixture-v2')))
    assert 'verified' not in result and 'launch_admissible' not in result
    with pytest.raises(auth.LocalModelError,match='ADAPTER_UNSUPPORTED'):
        runtime_native_scope.require_native_binding(approved['effective_scope']['native_proposal'])


@pytest.mark.parametrize('dimension,reason', [
    ('scope','PERMISSION_PLAN_UNSATISFIABLE'), ('resource','AUTH_FAILED'),
    ('derivation','INTENT_CHANGED')])
def test_normalized_equality_does_not_conflate_boolean_integer_and_float(dimension,reason):
    approved, observed = snapshot(), snapshot(version='fixture-v2')
    if dimension == 'scope':observed['effective_scope']['sandbox']['network']=0
    elif dimension == 'resource':
        approved['resources']['flag']=False
        observed['resources']['flag']=0
    else:observed['derivation']['context']['window']=32768.0
    with pytest.raises(auth.LocalModelError,match=reason):
        auth.compare_cli_configurations(approved,observed)


def test_system_owned_cli_can_keep_its_operator_owned_install_origin():
    approved, observed = snapshot(), snapshot(version='fixture-v2',binary='2'*64)
    approved['cli']['uid'] = observed['cli']['uid'] = 0
    proof = auth.compare_cli_configurations(approved,observed)
    assert proof['approved_cli']['uid'] == proof['observed_cli']['uid'] == 0
    assert proof['observed_cli']['origin'] == {'path':'/fixture/install','uid':501}


@pytest.mark.parametrize('mutation', ['tuple','integer-object-key','proposal-pairs','negative-origin-uid'])
def test_non_json_shapes_and_invalid_origin_are_not_silently_normalized(mutation):
    approved = with_proposal(snapshot())
    observed = with_proposal(snapshot(version='fixture-v2'))
    if mutation == 'tuple':observed['effective_scope']['read']=tuple(observed['effective_scope']['read'])
    elif mutation == 'integer-object-key':
        approved['effective_scope']['extra']={'1':False}
        observed['effective_scope']['extra']={1:False}
    elif mutation == 'proposal-pairs':
        observed['effective_scope']['native_proposal']=list(observed['effective_scope']['native_proposal'].items())
    else:
        approved['cli']['origin']['uid']=observed['cli']['origin']['uid']=-1
    with pytest.raises(auth.LocalModelError):
        auth.compare_cli_configurations(approved,observed)
