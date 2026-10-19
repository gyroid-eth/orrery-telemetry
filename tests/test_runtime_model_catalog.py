"""Catalog resolution delegates existing policy, not local endpoint heuristics."""
from copy import deepcopy
import sys
import subprocess
import os
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
import runtime_model_catalog as catalog
from dashboard import codex_models, claude_models
from r3_local_profiles.conftest import workspace, isolated_local_profile_home

ROOT=Path(__file__).resolve().parents[1]

# Literal golden from the old shell normalizer, independent of the new table.
PINNED = [
    ('claude-opus-5-5',('opus-5-5','opus55','opus5-5')),
    ('claude-opus-5',('opus-5','opus5')),
    ('claude-opus-4-8[1m]',('opus-1m','opus1m','opus[1m]','claude-opus-4-8-1m')),
    ('claude-opus-4-8',('opus-200k','opus200k')),
    ('claude-opus-5[1m]',('opus-5-1m','opus51m','opus-5[1m]','opus5[1m]')),
    ('claude-opus-5-5[1m]',('opus-5-5-1m','opus551m','opus-5-5[1m]','opus55[1m]','opus5-5[1m]')),
    ('claude-sonnet-5-5',('sonnet-5-5','sonnet55','sonnet5-5',' Sonnet 5.5 ')),
    ('claude-sonnet-5',('sonnet-5','sonnet5')),
    ('claude-sonnet-4-6',('sonnet-4-6','sonnet46')),
    ('claude-sonnet-4-6[1m]',('sonnet-1m','sonnet1m','sonnet[1m]')),
    ('claude-haiku-4-5-20251001',('haiku-4-5','haiku45','haiku4-5','claude-haiku-4-5')),
    ('claude-fable-5-1',('fable-5-1','fable51','fable5-1')),
]


@pytest.fixture(autouse=True)
def no_real_launcher(monkeypatch):
    monkeypatch.setattr(codex_models,'resolve_launcher',lambda:codex_models.LauncherPolicy(
        binary='/fixture/codex',version=(0,160,0)))
    monkeypatch.setattr(codex_models,'_capture',lambda *a,**kw:pytest.fail('real CLI probe'))
    monkeypatch.setattr(claude_models,'_probe_cli_version',lambda *a:None)


@pytest.mark.parametrize('provider,raw,effort,model,resolved_effort',[
    ('codex','luna','high','gpt-6-luna','high'),
    ('codex','gpt-6-astra','low','gpt-6-astra','low'),
    ('claude','sonnet',None,'claude-sonnet-5-5',None),
    ('claude','claude-future-9',None,'claude-future-9',None)])
def test_explicit_catalog_intent_retains_caller_and_uses_existing_policy(workspace,provider,raw,effort,model,resolved_effort):
    caller={**workspace.caller,'provider':provider,'model':raw,'effort':effort}
    intent=catalog.resolve_catalog_intent(caller,workspace.context)
    assert intent['model']=={'kind':'catalog','requested':raw,'id':model,'effort':resolved_effort}
    assert intent['intent_digest']==catalog.intent_digest(intent)
    for key,value in caller.items():
        if key!='model':assert intent[key]==value
    assert not {'effective_scope','local_models_root','configuration_evidence'} & intent.keys()
    before=deepcopy(intent)
    plan=catalog.materialize_catalog_intent(intent,workspace.context,provider_session_id='saved-session')
    assert intent==before
    assert plan['execution']['argv'][0]=='--model='+model
    assert plan['inherit_env'] is True and plan['warm_pool'] is True
    assert plan['execution']['env']=={}
    assert plan['provider_session_id']=='saved-session'


@pytest.mark.parametrize('provider,raw,effort',[
    ('codex','claude-opus-5-5',''),('codex','local:fixture',''),
    ('codex','gpt-6-luna','invented'),('claude','gpt-6-luna',''),
    ('claude','claude-future-9','high'),('claude','LOCAL:fixture','')])
def test_invalid_catalog_selection_never_becomes_local_or_native_fallback(workspace,provider,raw,effort):
    with pytest.raises(catalog.LocalModelError,match='MODEL_SELECTION_INVALID'):
        catalog.resolve_catalog_intent({**workspace.caller,'provider':provider,'model':raw,'effort':effort},workspace.context)


def test_catalog_resume_does_not_reresolve_alias_to_new_model(workspace,monkeypatch):
    intent=catalog.resolve_catalog_intent({**workspace.caller,'model':'luna','effort':'high'},workspace.context)
    monkeypatch.setitem(codex_models.ALIASES,'luna','gpt-new-model')
    assert catalog.materialize_catalog_intent(intent,workspace.context)['model']=='gpt-6-luna'


@pytest.mark.parametrize('requested',[{}, {'effort':None}, {'effort':''}])
def test_omitted_catalog_effort_stays_pinned_after_policy_default_changes(workspace,monkeypatch,requested):
    import runtime_launch_intent
    caller={**workspace.caller,'model':'gpt-6-luna',**requested}
    intent=catalog.resolve_catalog_intent(caller,workspace.context)
    assert intent['model']['effort']=='xhigh'
    assert ('effort' in intent)==('effort' in requested)
    if 'effort' in requested:assert intent['effort']==requested['effort']
    spawn=runtime_launch_intent.materialize_launch_intent(intent,workspace.context,operation='spawn')
    policy=codex_models.BUNDLED_EFFORTS['gpt-6-luna']
    monkeypatch.setitem(codex_models.BUNDLED_EFFORTS,'gpt-6-luna',
                        codex_models.EffortPolicy(policy.supported,'low'))
    assert catalog.resolve_catalog_intent(caller,workspace.context)['model']['effort']=='low'
    resume=runtime_launch_intent.materialize_launch_intent(intent,workspace.context,operation='resume')
    assert resume['execution']['argv']==spawn['execution']['argv']==[
        '--model=gpt-6-luna','-c','model_reasoning_effort="xhigh"']
    assert intent['intent_digest']==catalog.intent_digest(intent)


@pytest.mark.parametrize('requested',[{}, {'effort':None}, {'effort':''}])
def test_unknown_catalog_effort_keeps_cli_delegation_on_resume(workspace,monkeypatch,requested):
    import runtime_launch_intent
    model='gpt-fixture-future-9'
    caller={**workspace.caller,'model':model,**requested}
    intent=catalog.resolve_catalog_intent(caller,workspace.context)
    assert intent['model']=={'kind':'catalog','requested':model,'id':model,'effort':''}
    assert ('effort' in intent)==('effort' in requested)
    if 'effort' in requested:assert intent['effort']==requested['effort']
    spawn=runtime_launch_intent.materialize_launch_intent(intent,workspace.context,operation='spawn')
    monkeypatch.setitem(codex_models.BUNDLED_EFFORTS,model,
                        codex_models.EffortPolicy(('low','high'),'high'))
    assert catalog.resolve_catalog_intent(caller,workspace.context)['model']['effort']=='high'
    resume=runtime_launch_intent.materialize_launch_intent(intent,workspace.context,operation='resume')
    assert resume['execution']['argv']==spawn['execution']['argv']==['--model='+model]
    missing=deepcopy(intent)
    del missing['model']['effort']
    missing['intent_digest']=catalog.intent_digest(missing)
    with pytest.raises(catalog.LocalModelError,match='MODEL_SELECTION_INVALID'):
        runtime_launch_intent.materialize_launch_intent(missing,workspace.context,operation='resume')


@pytest.mark.parametrize('requested',[{}, {'effort':None}, {'effort':''}])
def test_claude_catalog_resolved_effort_is_none_without_collapsing_input(workspace,requested):
    intent=catalog.resolve_catalog_intent(
        {**workspace.caller,'provider':'claude','model':'sonnet',**requested},workspace.context)
    assert intent['model']=={'kind':'catalog','requested':'sonnet','id':'claude-sonnet-5-5','effort':None}
    assert ('effort' in intent)==('effort' in requested)
    if 'effort' in requested:assert intent['effort']==requested['effort']
    assert catalog.materialize_catalog_intent(intent,workspace.context)['execution']['argv']==[
        '--model=claude-sonnet-5-5']


@pytest.mark.parametrize('model',[
    {'kind':'catalog','ref':'luna','resolved':{'model':'gpt-6-luna','effort':'high'}},
    {'kind':'catalog','requested':'local:fixture','id':'gpt-6-luna','effort':'high'}])
def test_catalog_rejects_a_second_tag_shape_or_a_retagged_local_selector(workspace,model):
    intent=catalog.resolve_catalog_intent({**workspace.caller,'model':'luna','effort':'high'},workspace.context)
    intent['model']=model
    intent['intent_digest']=catalog.intent_digest(intent)
    with pytest.raises(catalog.LocalModelError,match='MODEL_SELECTION_INVALID'):
        catalog.materialize_catalog_intent(intent,workspace.context)


def test_common_model_readers_only_read_the_designed_tags():
    from runtime_launch_intent import model_reference,resolved_model_id
    for model,requested,formal in (
        ({'kind':'catalog','requested':'removed-alias','id':'gpt-6-luna','effort':'high'},
         'removed-alias','gpt-6-luna'),
        ({'kind':'local-profile','ref':'local:fixture','resolved':{'model':'opaque-local-id'}},
         'local:fixture','opaque-local-id')):
        assert model_reference({'model':model})==requested
        assert resolved_model_id({'model':model})==formal


@pytest.mark.parametrize('provider,model,setting',[
    ('codex','gpt-6-luna','AGENTSTACK_CODEX_MODELS'),
    ('claude','opus','AGENTSTACK_CLAUDE_MODELS')])
def test_existing_catalog_allowlist_remains_authoritative(workspace,monkeypatch,provider,model,setting):
    monkeypatch.setenv(setting,'gpt-other-9' if provider=='codex' else 'claude-other-9')
    with pytest.raises(catalog.LocalModelError,match='MODEL_SELECTION_INVALID'):
        catalog.resolve_catalog_intent({**workspace.caller,'provider':provider,'model':model},workspace.context)


def test_fixed_catalog_policy_loads_without_cwd_or_caller_selected_root(workspace,monkeypatch):
    monkeypatch.chdir(workspace.tmp)
    assert catalog.provider_policy('codex') is codex_models
    assert catalog.provider_policy('claude') is claude_models
    with pytest.raises(catalog.LocalModelError):catalog.provider_policy('gemini')


@pytest.mark.parametrize('model,raw',[(model,raw) for model,spellings in PINNED for raw in (*spellings,model)])
def test_moved_claude_aliases_keep_existing_literal_selection(model,raw):
    assert claude_models.normalize_model(raw)==model


@pytest.mark.parametrize('raw,model',[
    ('opus5-5','claude-opus-5-5'),('Sonnet 5.5','claude-sonnet-5-5'),
    ('opus[1m]','claude-opus-4-8[1m]'),('haiku45','claude-haiku-4-5-20251001'),
    ('fable51','claude-fable-5-1'),('claude-future-9','claude-future-9')])
def test_actual_bash_normalizer_delegates_existing_policy(raw,model):
    source=(ROOT/'hooks/spawn_child.sh').read_text()
    start=source.index('normalize_claude_model() {')
    body=source[start:source.index('\nnormalize_codex_model() {',start)]
    command=body+'\nnormalize_claude_model "$1"'
    env={**os.environ,'AGENTSTACK_PYTHON':sys.executable,'CLAUDE_MODEL_HELPER':str(ROOT/'dashboard/claude_models.py'),
         'CLAUDE_ALIAS_OPUS':'claude-captured-opus-9','CLAUDE_ALIAS_SONNET':'claude-captured-sonnet-9',
         'CLAUDE_ALIAS_HAIKU':'claude-captured-haiku-9','CLAUDE_ALIAS_FABLE':'claude-captured-fable-9'}
    result=subprocess.run(['/bin/bash','-eu','-c',command,'fixture',raw],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==model


def test_captured_shell_aliases_do_not_reresolve_during_normalization():
    aliases={'opus':'claude-captured-opus-9','sonnet':'claude-captured-sonnet-9',
             'haiku':'claude-captured-haiku-9','fable':'claude-captured-fable-9'}
    assert claude_models.normalize_model('',aliases=aliases)=='claude-captured-opus-9'
    assert claude_models.normalize_model('sonnet',aliases=aliases)=='claude-captured-sonnet-9'


@pytest.mark.parametrize('raw',['','opus','sonnet','haiku','fable'])
def test_unloaded_captured_aliases_keep_existing_loading_diagnostic(raw):
    aliases={family:'' for family in ('opus','sonnet','haiku','fable')}
    with pytest.raises(ValueError,match='load_claude_aliases must run first'):
        claude_models.normalize_model(raw,aliases=aliases)


@pytest.mark.parametrize('mutation',['partial-empty','invalid-model','missing-family'])
def test_corrupted_captured_aliases_are_not_misreported_as_unloaded(mutation):
    aliases={'opus':'claude-captured-opus-9','sonnet':'claude-captured-sonnet-9',
             'haiku':'claude-captured-haiku-9','fable':'claude-captured-fable-9'}
    if mutation=='partial-empty':aliases['sonnet']=''
    elif mutation=='invalid-model':aliases['sonnet']='invalid-captured-model'
    else:aliases.pop('sonnet')
    with pytest.raises(ValueError,match='^invalid captured Claude aliases$'):
        claude_models.normalize_model('sonnet',aliases=aliases)


@pytest.mark.parametrize('raw,model',[
    ('sonnet-5-5','claude-sonnet-5-5'),('claude-future-9','claude-future-9')])
def test_formal_and_versioned_models_do_not_require_family_alias_loading(raw,model):
    aliases={family:'' for family in ('opus','sonnet','haiku','fable')}
    assert claude_models.normalize_model(raw,aliases=aliases)==model


@pytest.mark.parametrize('raw',['','opus','sonnet','haiku','fable'])
def test_actual_bash_unloaded_aliases_keep_loading_error_without_model_output(raw):
    source=(ROOT/'hooks/spawn_child.sh').read_text()
    start=source.index('normalize_claude_model() {')
    command=source[start:source.index('\nnormalize_codex_model() {',start)]
    command+='\nnormalize_claude_model "$1"'
    env={**os.environ,'AGENTSTACK_PYTHON':sys.executable,
         'CLAUDE_MODEL_HELPER':str(ROOT/'dashboard/claude_models.py'),
         'CLAUDE_ALIAS_OPUS':'','CLAUDE_ALIAS_SONNET':'',
         'CLAUDE_ALIAS_HAIKU':'','CLAUDE_ALIAS_FABLE':''}
    result=subprocess.run(['/bin/bash','-eu','-c',command,'fixture',raw],env=env,
                          capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and result.stdout==''
    assert 'load_claude_aliases must run first' in result.stderr


def test_common_resolver_uses_catalog_without_local_profile_or_auth_lookup(workspace,monkeypatch):
    import runtime_launch_intent
    monkeypatch.setattr(runtime_launch_intent,'load_profile',lambda *a,**kw:pytest.fail('catalog consulted local root'))
    caller={**workspace.caller,'model':'luna','effort':'high'}
    value=runtime_launch_intent.resolve_launch_intent(caller,workspace.context)
    assert value['model']['kind']=='catalog'
    assert runtime_launch_intent.materialize_launch_intent(value,workspace.context,operation='resume')['model']=='gpt-6-luna'
