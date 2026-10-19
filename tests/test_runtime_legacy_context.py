"""Literal target binding through the REAL common resolver, not server proof."""
from copy import deepcopy
import importlib
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from local_model_auth import LocalModelError, intent_digest
from runtime_launch_intent import resolve_launch_intent, materialize_launch_intent
from r3_local_profiles.conftest import workspace as profile_workspace
from test_runtime_preview_policy import policy


def module():
    try:return importlib.import_module('runtime_legacy_context')
    except ModuleNotFoundError:pytest.fail('LEGACY_CONTEXT_NOT_IMPLEMENTED')


@pytest.fixture
def prepared(tmp_path):
    tmp_path.chmod(0o700)
    for name in ('home','runtime','cwd','vault','task-dir'):(tmp_path/name).mkdir(mode=0o700)
    ws=profile_workspace.__wrapped__(tmp_path)
    install=tmp_path/'install';install.mkdir(mode=0o700)
    owner=tmp_path/'runtime/child-agents/FixtureChild.json'
    target={'schema_version':1,'install_root':str(install),'runtime_dir':str(tmp_path/'runtime'),
        'owner':str(owner),'name':'FixtureChild','project_key':str(tmp_path/'vault'),
        'project_id':7,'mail_url':'http://127.0.0.1:45679/mcp/',
        'parent':{'id':41,'name':'FixtureParent','program':'claude-code','model':'fixture-parent-model'}}
    ws.caller['provider']='claude'
    ws.context.update(mode='legacy',client_root=str(tmp_path/'runtime'),intent_owner=str(owner),
        runtime_client=str(install/'bin/lib/runtime_client.py'),execution_policy=policy(),legacy_context=target)
    return ws


def test_context_is_closed_data_not_a_parent_authentication_claim(prepared):
    m=module();ws=prepared;target=ws.context['legacy_context'];before=deepcopy(target)
    result=m.checked_legacy_context(target)
    assert result==target and result is not target
    result['parent']['id']=99
    assert target==before
    assert not {'authenticated','server_uuid','verified','registration_token'} & target.keys()


@pytest.mark.parametrize('key,value',[
    ('schema_version',True),('schema_version',2),('extra','ignored'),
    ('name','../Child'),('name','OtherChild'),('owner','/unrelated/FixtureChild.json'),
    ('runtime_dir','/unrelated-runtime'),('project_id',True),('project_id',0),
    ('project_key','relative'),('mail_url','http://user:secret@127.0.0.1/mcp/'),
    ('mail_url','http://127.0.0.1/mcp/?token=canary'),('mail_url','https://mail.invalid/mcp/#x'),
    ('mail_url','http://remote.invalid/mcp/'),('mail_url','file:///tmp/mail'),
    ('parent',{'id':41,'name':'FixtureParent','program':'claude-code','model':'x','token':'canary'}),
    ('parent',{'id':True,'name':'FixtureParent','program':'claude-code','model':'x'}),
    ('parent',{'id':41,'name':'../Other','program':'claude-code','model':'x'}),
])
def test_malformed_or_cross_target_context_is_rejected(prepared,key,value):
    m=module();target=deepcopy(prepared.context['legacy_context']);target[key]=value
    with pytest.raises(LocalModelError):m.checked_legacy_context(target)


def test_resolver_saves_policy_and_target_inside_the_one_existing_digest(prepared):
    module();ws=prepared;before=deepcopy(ws.context)
    value=resolve_launch_intent(ws.caller,ws.context)
    assert value['execution_policy']==policy()
    assert value['legacy_context']==ws.context['legacy_context']
    assert value['intent_digest']==intent_digest(value)
    for field in ('execution_policy','legacy_context'):
        changed=deepcopy(value);changed[field]['extra']='modified'
        assert intent_digest(changed)!=value['intent_digest']
    value['legacy_context']['parent']['id']=99
    assert ws.context==before


@pytest.mark.parametrize('mutation',['global','policy-missing','context-missing','policy-null',
    'caller-name','caller-parent','provider','foreground','worktree','chrome','task-mail',
    'owner','runtime','script','install-symlink'])
def test_resolver_does_not_ignore_legacy_or_preview_context(prepared,mutation):
    module();ws=prepared;caller,context=deepcopy(ws.caller),deepcopy(ws.context)
    if mutation=='global':context['mode']='global'
    elif mutation=='policy-missing':del context['execution_policy']
    elif mutation=='context-missing':del context['legacy_context']
    elif mutation=='policy-null':context['execution_policy']=None
    elif mutation=='caller-name':caller['name']='OtherChild'
    elif mutation=='caller-parent':caller['parent']='OtherParent'
    elif mutation=='provider':caller['provider']='codex'
    elif mutation=='foreground':caller['detached']=False
    elif mutation=='worktree':caller['worktree']=True
    elif mutation=='chrome':caller['chrome']=True
    elif mutation=='task-mail':caller['task_delivery']='mail'
    elif mutation=='owner':context['intent_owner']=str(ws.tmp/'other.json')
    elif mutation=='runtime':context['client_root']=str(ws.tmp/'other-runtime')
    elif mutation=='script':context['runtime_client']=str(ws.tmp/'other-client.py')
    else:
        alias=ws.tmp/'alias';alias.symlink_to(context['legacy_context']['install_root'])
        context['legacy_context']['install_root']=str(alias)
    with pytest.raises(LocalModelError):resolve_launch_intent(caller,context)


@pytest.mark.parametrize('field',['execution_policy','legacy_context'])
def test_materialization_rejects_changed_binding_even_if_new_context_is_well_formed(prepared,field):
    module();ws=prepared;value=resolve_launch_intent(ws.caller,ws.context)
    ctx=deepcopy(ws.context)
    if field=='execution_policy':ctx[field]['operator_decision']='operator:other'
    else:ctx[field]['parent']['id']=99
    with pytest.raises(LocalModelError):materialize_launch_intent(value,ctx,operation='spawn')


def test_new_context_does_not_enable_resume_or_native_admission(prepared):
    module();ws=prepared;value=resolve_launch_intent(ws.caller,ws.context)
    for operation in ('resume','headless-wake'):
        with pytest.raises(LocalModelError,match='ADAPTER_UNSUPPORTED'):
            materialize_launch_intent(value,ws.context,operation=operation)
    # These inputs lack a native proposal; successful pure resolution alone
    # must not accidentally bypass the normal gate or become exec approval.
    with pytest.raises(LocalModelError):
        materialize_launch_intent(value,ws.context,operation='spawn',_native_admission=True)


def test_ordinary_global_resolution_never_loads_legacy_only_policy_dependencies(prepared,monkeypatch):
    import builtins
    ws=prepared;context=deepcopy(ws.context);context['mode']='global'
    del context['legacy_context'];del context['execution_policy']
    original=builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.startswith('runtime_legacy_') or name=='runtime_preview_policy':
            pytest.fail('ordinary global path imported a legacy-only dependency')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    value=resolve_launch_intent(ws.caller,context)
    assert 'execution_policy' not in value and 'legacy_context' not in value
