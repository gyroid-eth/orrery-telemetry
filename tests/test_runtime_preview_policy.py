"""Pure preview metadata/explicit-request checks; no native admission proof."""
from copy import deepcopy
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from local_model_auth import LocalModelError, canonical_digest, intent_digest


def module():
    try:return importlib.import_module('runtime_preview_policy')
    except ModuleNotFoundError:pytest.fail('LOCAL_PREVIEW_POLICY_NOT_IMPLEMENTED')


def policy():
    return {'kind':'local-preview-v1','native_scope':'unverified','native_isolation':'unverified',
            'operator_decision':'operator:review-001'}


def intent(*,with_policy=True,provider='claude'):
    # Only the fields this pure boundary consumes. This is NOT a complete
    # runnable native intent/owner/credential or an accepted configuration.
    value={'schema_version':1,'revision':1,'provider':provider,
        'model':{'kind':'local-profile','ref':'local:example'},'task':'/fixture/task.md'}
    if with_policy:value['execution_policy']=policy()
    value['intent_digest']=intent_digest(value)
    return value


def redigest(value):
    value['intent_digest']=intent_digest(value);return value


def test_absent_policy_keeps_strict_and_explicit_claim_cannot_create_policy():
    m=module();value=intent(with_policy=False);before=deepcopy(value)
    assert m.check_preview_request(value,preview_requested=False) is None
    assert m.preview_notice(value) is None
    with pytest.raises(LocalModelError,match='LOCAL_MODEL_PREVIEW_POLICY_REQUIRED'):
        m.check_preview_request(value,preview_requested=True)
    assert value==before


def test_saved_policy_needs_an_explicit_boolean_request_and_is_returned_without_alias():
    m=module();value=intent();before=deepcopy(value)
    for claim in (False,None,0,1,'true',{},[]):
        with pytest.raises(LocalModelError):m.check_preview_request(value,preview_requested=claim)
    result=m.check_preview_request(value,preview_requested=True)
    assert result==policy();result['native_scope']='verified';assert value==before


@pytest.mark.parametrize('field,bad',[
    ('kind','strict'),('kind','unknown'),('native_scope','verified'),('native_scope',True),
    ('native_isolation','verified'),('native_isolation',False),('operator_decision',''),
    ('operator_decision','operator\nraw'),('operator_decision','https://secret.invalid/token'),
    ('operator_decision','x'*97),('extra',True),
])
def test_unknown_or_verified_claims_do_not_become_preview(field,bad):
    m=module();value=intent();value['execution_policy'][field]=bad;redigest(value)
    with pytest.raises(LocalModelError,match='LOCAL_MODEL_PREVIEW_POLICY_INVALID'):
        m.check_preview_request(value,preview_requested=True)


@pytest.mark.parametrize('value',[None,False,True,[],{},'preview'])
def test_present_null_or_malformed_policy_is_not_absent_or_strict(value):
    m=module();row=intent();row['execution_policy']=value;redigest(row)
    for claim in (False,True):
        with pytest.raises(LocalModelError):m.check_preview_request(row,preview_requested=claim)
    with pytest.raises(LocalModelError):m.preview_notice(row)


@pytest.mark.parametrize('provider',['codex','gemini','daemon','unknown'])
def test_preview_is_not_adopted_for_other_providers(provider):
    m=module();value=intent(provider=provider)
    with pytest.raises(LocalModelError,match='LOCAL_MODEL_ADAPTER_UNSUPPORTED'):
        m.check_preview_request(value,preview_requested=True)


def test_policy_is_in_existing_intent_digest_and_stale_digest_cannot_be_projected():
    m=module();value=intent();original=value['intent_digest']
    value['execution_policy']['operator_decision']='operator:review-002'
    assert intent_digest(value)!=original
    for operation in (lambda:m.check_preview_request(value,preview_requested=True),lambda:m.preview_notice(value)):
        with pytest.raises(LocalModelError,match='LOCAL_MODEL_INTENT_CHANGED'):operation()


def test_catalog_is_not_preview_and_does_not_change_to_local():
    m=module();value=intent();value['model']={'kind':'catalog','requested':'sonnet','id':'fixture-model'};redigest(value)
    with pytest.raises(LocalModelError,match='LOCAL_MODEL_PREVIEW_POLICY_INVALID'):
        m.check_preview_request(value,preview_requested=True)
    del value['execution_policy'];redigest(value)
    assert m.check_preview_request(value,preview_requested=False) is None


def test_notice_is_closed_nonsecret_unverified_data_not_cli_or_scope_approval():
    m=module();value=intent()
    value.update(endpoint='PRIVATE_ENDPOINT',auth_command=['PRIVATE_HELPER'],env={'TOKEN':'PRIVATE_CANARY'})
    redigest(value)
    notice=m.preview_notice(value)
    assert notice=={'mode':'preview','native_scope_verified':False,'isolation_verified':False,
        'execution_policy_sha256':canonical_digest(policy())}
    assert all(secret not in str(notice) for secret in ('PRIVATE_ENDPOINT','PRIVATE_HELPER','PRIVATE_CANARY','operator:review-001'))
    assert 'launch_admissible' not in notice and 'cli_acceptance' not in notice


def test_existing_launch_reference_must_match_digest_and_exact_preview_projection():
    m=module();value=intent();notice=m.preview_notice(value)
    row={'intent_digest':value['intent_digest'],'preview':notice,'phase':'planned'}
    assert m.check_preview_launch_reference(value,row)==notice
    assert row['phase']=='planned'  # Data validation never advances execution.
    for changed in ({**row,'intent_digest':'0'*64},{'intent_digest':value['intent_digest']},
            {**row,'preview':{**notice,'isolation_verified':True}},
            {**row,'preview':{**notice,'extra':True}},{**row,'preview':None}):
        with pytest.raises(LocalModelError,match='LOCAL_MODEL_INTENT_CHANGED'):
            m.check_preview_launch_reference(value,changed)


def test_strict_launch_does_not_inherit_a_preview_notice():
    m=module();value=intent(with_policy=False)
    assert m.check_preview_launch_reference(value,{'intent_digest':value['intent_digest']}) is None
    for notice in (None,{},m.preview_notice(intent())):
        with pytest.raises(LocalModelError,match='LOCAL_MODEL_INTENT_CHANGED'):
            m.check_preview_launch_reference(value,{'intent_digest':value['intent_digest'],'preview':notice})


def test_pure_policy_does_not_enable_the_real_native_gate():
    m=module();native=importlib.import_module('runtime_native_scope')
    value=intent();m.check_preview_request(value,preview_requested=True)
    with pytest.raises(LocalModelError,match='LOCAL_MODEL_ADAPTER_UNSUPPORTED'):
        native.require_native_binding(value['execution_policy'])
