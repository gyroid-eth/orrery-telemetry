"""Same configure producer, private legacy owner; fake parent auth only.

The installed settings reader is real. No Mail listener, provider CLI, key,
model, global client or manufactured registered child is used by this fixture.
"""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
from local_model_auth import LocalModelError
from runtime_intent_store import read_owner, write_owner
from test_runtime_legacy_context import prepared as context_fixture
from test_runtime_configure_ownership import snapshot
from _installed_client_payload import ROOT, copy_client_library


@pytest.fixture
def prepared(context_fixture):
    try:importlib.import_module('runtime_legacy_configuration')
    except ModuleNotFoundError:pytest.fail('LEGACY_CONFIGURE_ADAPTER_NOT_IMPLEMENTED')
    ws=context_fixture;target=ws.context['legacy_context'];root=Path(target['install_root'])
    # Staging's private runtime layout exists before read-only configure. This
    # directory is not a derived provider output or a registered child owner.
    Path(target['owner']).parent.mkdir(mode=0o700)
    copy_client_library(root)
    shutil.copy2(ROOT/'hooks/child_tools.py',root/'hooks/child_tools.py')
    # The installer copies the complete core hooks tree. These are the SAME
    # parser/matcher the ordinary reservation hook uses, not fixture stubs.
    for name in ('check-reservation-conflicts.py','reservation-patterns.py'):
        shutil.copy2(ROOT/'hooks'/name,root/'hooks'/name)
    # The real installer copies this complete provider template tree. Explicit
    # local configure reads its fixed template, never the user's CLAUDE.md.
    assert 'copy_tree "$REPO_ROOT/claude" "$INSTALL_DIR/claude"' in (ROOT/'scripts/install.sh').read_text()
    (root/'claude').mkdir(exist_ok=True)
    shutil.copy2(ROOT/'claude/CLAUDE.md',root/'claude/CLAUDE.md')
    # Core install_child_mcp_proxy copies the source package, not only its
    # client file. Keep the installed layout complete without running it.
    shared=Path('integrations/codex_app/src/agentstack_codex_app')
    shutil.copytree(ROOT/shared,root/shared,dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.pyo'))
    write_owner(root/'install-state.json',{'schema_version':1,'install_dir':str(root),
        'owned_files':[str(root/'install-state.json')],'owned_dirs':[str(root)],'skill_links':[]})
    env=root/'env.sh'
    env.write_text(''.join('export '+k+'='+shlex.quote(v)+'\n' for k,v in {
        'AGENTSTACK_RUNTIME_MODE':'legacy','AGENTSTACK_PROJECT_KEY':target['project_key'],
        'AGENTSTACK_MCP_URL':target['mail_url']}.items()));env.chmod(0o600)
    ws.settings.write_text('{}')
    import hashlib
    ws.context['approved_settings_sha256']=hashlib.sha256(ws.settings.read_bytes()).hexdigest()
    for name in ('cli','python'):
        binary=ws.tmp/name;binary.write_text('#!/bin/sh\nexit 91\n');binary.chmod(0o500)
    parent_auth=[]
    def authenticate():
        parent_auth.append('authenticated-with-fake-transport')
        return {**target['parent'],'project_id':target['project_id']}
    api=runpy.run_path(str(root/'bin/lib/runtime_legacy_configuration.py'))
    client=api['LegacyConfigurationClient'](target,_authenticate_parent=authenticate)
    args={'python':str(ws.tmp/'python'),'cli':str(ws.tmp/'cli'),'declared_cli_version':'fixture-pinned',
        'settings_paths':[str(ws.settings)],'native_sources':[{'path':str(ws.settings),'layer':'user'}],
        'skill_sources':[]}
    configure=importlib.import_module('runtime_local_configure')
    return client,ws,args,configure,parent_auth,api


def plan(prepared):
    client,ws,args,configure,_,_=prepared
    return configure.plan_local_configuration(client,ws.caller,ws.context,**args)


def test_plan_is_readonly_and_uses_no_global_identity_or_profile(prepared):
    client,ws,args,configure,calls,_=prepared
    before=snapshot(ws.tmp)
    value=plan(prepared)
    assert snapshot(ws.tmp)==before
    assert value['owner']==str(ws.tmp/'runtime/child-agents/FixtureChild.json')
    assert value['intent']['legacy_context']==ws.context['legacy_context']
    assert value['intent']['execution_policy']==ws.context['execution_policy']
    assert calls==['authenticated-with-fake-transport']
    assert client.identity is None and client.mode=='legacy'
    assert not list(ws.tmp.rglob('profile.json')) and not list(ws.tmp.rglob('runtime-client.json'))


def test_apply_uses_the_same_manifest_and_same_child_state_without_registration(prepared):
    client,ws,args,configure,calls,_=prepared;value=plan(prepared)
    result=configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    assert result['configured'] is True and result['launch_admissible'] is False
    assert result['requires_native_validation'] is True
    state=read_owner(client.profile_path)
    assert state=={'configuration_stage':'prepared','launch_intent':value['intent']}
    assert not {'agent_id','registration_token','local_execution','local_registration'} & state.keys()
    home=ws.tmp/'runtime/child-agents/FixtureChild.preview-home'
    settings=json.loads((home/'local-model-settings.json').read_text())
    assert settings['permissions']['defaultMode']=='dontAsk'
    assert settings['hooks']==value['intent']['effective_scope']['framework_settings']['hooks']
    assert value['intent']['configuration_evidence']['runtime_python']['path']==args['python']
    assert args['python']!=sys.executable  # independent declared executable, not implicit caller Python
    for groups in settings['hooks'].values():
        for group in groups:
            for hook in group['hooks']:
                parts=shlex.split(hook['command'])
                assert parts[:2]==[args['python'],str(Path(client.config['wrapper_root'])/'bin/lib/runtime_legacy_preview_cli.py')]
                assert parts[2]=='entry' and parts[-1]=='FixtureChild'
                assert 'runtime-client.json' not in hook['command']
    assert not list(ws.tmp.rglob('agent_token_*'))
    assert calls==['authenticated-with-fake-transport']*2
    before=snapshot(ws.tmp)
    again=configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    assert again['changed'] is False and snapshot(ws.tmp)==before


@pytest.mark.parametrize('mutation', ['caller-python', 'foreign-python', 'missing-python-pin'])
def test_hook_reader_rederives_from_declared_python_not_callers_interpreter(prepared, mutation):
    client, _, args, configure, _, _ = prepared
    value = plan(prepared)
    intent, context, _, _ = configure._data(client, value['inputs'])
    from runtime_launch_intent import _scope_evidence
    scope = deepcopy(intent['effective_scope'])
    expected = _scope_evidence(scope, context, provider='claude')
    assert expected == intent['scope_evidence']
    if mutation == 'missing-python-pin': context['configuration_evidence'].pop('runtime_python')
    else:
        hook = scope['framework_settings']['hooks']['SessionStart'][0]['hooks'][0]
        argv = shlex.split(hook['command'])
        assert argv[0] == args['python'] and argv[0] != sys.executable
        argv[0] = sys.executable if mutation == 'caller-python' else '/foreign/python'
        hook['command'] = shlex.join(argv)
    with pytest.raises(LocalModelError, match='PERMISSION_PLAN_UNSATISFIABLE'):
        _scope_evidence(scope, context, provider='claude')


@pytest.mark.parametrize('mutation',['project','url','mode','env-bytes','reader','parent-auth'])
def test_target_drift_refuses_before_outputs(prepared,mutation):
    client,ws,args,configure,_,_=prepared;value=plan(prepared)
    env=Path(client.config['wrapper_root'])/'env.sh'
    if mutation=='parent-auth':
        def denied():raise OSError('fake failure must not leak')
        client._authenticate_parent=denied
    elif mutation=='reader':
        reader=Path(client.config['wrapper_root'])/'hooks/project-context.sh'
        reader.write_text(reader.read_text()+'\n# changed source\n')
    else:
        raw=env.read_text()
        if mutation=='project':raw=raw.replace(ws.context['legacy_context']['project_key'],'/other-project')
        elif mutation=='url':raw=raw.replace('45679','45680')
        elif mutation=='mode':raw=raw.replace('=legacy','=global')
        else:raw+='\n# drift\n'
        env.write_text(raw)
    before=snapshot(ws.tmp)
    with pytest.raises(LocalModelError):configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    assert snapshot(ws.tmp)==before and not client.profile_path.exists()


@pytest.mark.parametrize('mutation',['parent-id','project-id','extra-token'])
def test_authenticated_parent_result_must_match_exact_closed_identity(prepared,mutation):
    client,ws,args,configure,_,_=prepared;target=ws.context['legacy_context']
    identity={**target['parent'],'project_id':target['project_id']}
    if mutation=='parent-id':identity['id']+=1
    elif mutation=='project-id':identity['project_id']+=1
    else:identity['registration_token']='fixture-response-secret'
    client._authenticate_parent=lambda:identity
    before=snapshot(ws.tmp)
    with pytest.raises(LocalModelError):plan(prepared)
    assert snapshot(ws.tmp)==before


def test_existing_cloud_owner_cannot_be_adopted_as_prepared_local(prepared):
    client,ws,args,configure,_,_=prepared
    write_owner(client.profile_path,{'agent_id':33,'agent_name':'FixtureChild','registration_token':'fixture-cloud'})
    before=snapshot(ws.tmp)
    with pytest.raises(LocalModelError):plan(prepared)
    assert snapshot(ws.tmp)==before


@pytest.mark.parametrize('action',['plan','apply'])
@pytest.mark.parametrize('material',['credential','credential-symlink'])
def test_orphan_canonical_credential_cannot_be_adopted_by_configure(prepared,action,material):
    client,ws,args,configure,_,_=prepared
    value=plan(prepared) if action=='apply' else None
    token=client.runtime_dir/'agent_token_FixtureChild'
    if material=='credential':token.write_text('fixture-existing-credential');token.chmod(0o600)
    else:token.symlink_to(client.runtime_dir/'missing-credential')
    before=snapshot(ws.tmp)
    with pytest.raises(LocalModelError,match='LOCAL_MODEL_INTENT_CHANGED'):
        if action=='plan':plan(prepared)
        else:configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    assert snapshot(ws.tmp)==before


def test_explicit_preview_metadata_cannot_be_supplied_by_caller(prepared):
    client,ws,args,configure,_,_=prepared
    caller={**ws.caller,'execution_policy':ws.context['execution_policy']}
    before=snapshot(ws.tmp)
    with pytest.raises(LocalModelError):configure.plan_local_configuration(client,caller,ws.context,**args)
    assert snapshot(ws.tmp)==before


def test_parent_auth_is_outside_name_lock_and_cannot_be_replaced_with_a_bool(prepared):
    client,ws,args,configure,_,api=prepared
    calls=[]
    def authenticate():
        import fcntl
        lock=client.profile_path.parent/'.FixtureChild.resume.lock'
        if lock.exists():
            with lock.open('rb') as stream:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        calls.append('outside-lock')
        return {**ws.context['legacy_context']['parent'],'project_id':7}
    client._authenticate_parent=authenticate
    value=plan(prepared)
    configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    assert calls==['outside-lock','outside-lock']
    with pytest.raises(LocalModelError):api['LegacyConfigurationClient'](ws.context['legacy_context'],_authenticate_parent=True)


def test_provider_home_and_owner_are_fixed_not_operator_context_overrides(prepared):
    client,ws,args,configure,_,_=prepared
    context=deepcopy(ws.context);context['intent_owner']=str(ws.tmp/'other.json')
    before=snapshot(ws.tmp)
    with pytest.raises(LocalModelError):configure.plan_local_configuration(client,ws.caller,context,**args)
    assert snapshot(ws.tmp)==before


@pytest.mark.parametrize('key',['id','project_id'])
def test_parent_auth_boolean_ids_are_not_integer_one(prepared,key):
    _,ws,args,configure,_,api=prepared
    target=deepcopy(ws.context['legacy_context'])
    if key=='id':target['parent']['id']=1
    else:target['project_id']=1
    ws.context['legacy_context']=target
    identity={**target['parent'],'project_id':target['project_id']};identity[key]=True
    client=api['LegacyConfigurationClient'](target,_authenticate_parent=lambda:identity)
    with pytest.raises(LocalModelError):configure.plan_local_configuration(client,ws.caller,ws.context,**args)


def test_target_change_immediately_after_authentication_is_not_adopted_by_plan(prepared):
    client,ws,args,configure,_,_=prepared;original=client.authenticate_target
    def change_after_authentication():
        result=original()
        client.path.write_text(client.path.read_text()+'\n# changed after observation\n')
        return result
    client.authenticate_target=change_after_authentication
    with pytest.raises(LocalModelError):plan(prepared)
    assert not client.profile_path.exists() and not client.provider_home('claude').exists()


@pytest.mark.parametrize('mutation',['name','provider','verified-policy'])
def test_invalid_prepared_candidate_is_not_silently_repaired_by_new_configure(prepared,mutation):
    from local_model_auth import intent_digest
    client,ws,args,configure,_,_=prepared;value=plan(prepared)
    configure.apply_local_configuration(client,value,approved_id=value['plan_id'])
    frame=read_owner(client.profile_path);intent=frame['launch_intent']
    if mutation=='name':intent['name']='OtherChild'
    elif mutation=='provider':intent['provider']='codex'
    else:intent['execution_policy']['native_scope']='verified'
    intent['intent_digest']=intent_digest(intent);write_owner(client.profile_path,frame)
    before=snapshot(ws.tmp)
    with pytest.raises(LocalModelError):plan(prepared)
    assert snapshot(ws.tmp)==before
