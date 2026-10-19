"""Explicit fixed-client resource publication, not native acceptance."""
from copy import deepcopy
import importlib
import hashlib
import json
import runpy
import shlex
from pathlib import Path
import sys

import pytest

from test_global_runtime_client_s2a import server, context, api
from r3_local_profiles.conftest import workspace as profile_workspace
from test_runtime_configure_ownership import snapshot
from _installed_client_payload import copy_client_library

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
configure = importlib.import_module('runtime_local_configure')


def test_shared_claude_hook_renderer_preserves_existing_lifecycle_literal():
    from runtime_delivery import claude_hook_settings
    python,script,context = '/approved/runtime python','/approved/client helper.py','/approved/client/context.json'
    observed = claude_hook_settings(python,script,context)
    matcher = 'Edit|Write|MultiEdit|NotebookEdit'
    expected = {'SessionStart':[(None,'session-start')],
                'PreToolUse':[(matcher,'pre-edit')],
                'PostToolUse':[(matcher,'post-edit'),(None,'notifications')],
                'SessionEnd':[(None,'end-session')]}
    assert set(observed) == {'hooks'} and set(observed['hooks']) == set(expected)
    for event,groups in expected.items():
        assert len(observed['hooks'][event]) == len(groups)
        for group,(pattern,operation) in zip(observed['hooks'][event],groups):
            assert set(group) == ({'hooks','matcher'} if pattern else {'hooks'})
            assert group.get('matcher') == pattern
            assert len(group['hooks']) == 1
            hook = group['hooks'][0]
            assert set(hook) == {'type','command'} and hook['type'] == 'command'
            assert shlex.split(hook['command']) == [python,script,'entry',operation,'--context',context]


@pytest.fixture
def prepared(server,monkeypatch):
    c = context(server,identity=False,window=False)
    root = server['root']/'install'
    root.mkdir(mode=0o700)
    manifest = root/'install-state.json'
    api['atomic_json'](manifest,{'schema_version':1,'install_dir':str(root),
        'owned_files':[str(manifest)],'owned_dirs':[str(root)],'skill_links':[]})
    api['atomic_json'](c.path,{**c.config,'wrapper_root':str(root)})
    copy_client_library(root)
    installed = runpy.run_path(str(root/'bin/lib/runtime_client.py'))
    c = installed['RuntimeClient'](c.path)
    c._fixture_api = installed
    work = server['root']/'local-profile-fixture';work.mkdir(mode=0o700)
    for name in ('home','tmp','runtime','cwd','vault','task-dir','bin'):
        (work/name).mkdir(mode=0o700)
    ws = profile_workspace.__wrapped__(work)
    # This new configure producer deliberately supports only the small native
    # settings subset. Keep #266's original primitive fixture unchanged; its
    # unknown sentinel is NOT silently discarded by production admission.
    ws.settings.write_text('')
    ws.context['approved_settings_sha256'] = hashlib.sha256(ws.settings.read_bytes()).hexdigest()
    for name in ('cli','python','auth'):
        binary = server['root']/('approved-'+name)
        binary.write_text('#!/bin/sh\necho NO > must-not-run\n');binary.chmod(0o500)
    ws.profile.write_text(ws.profile.read_text().replace('/fixture/keychain-ref',str(server['root']/'approved-auth')))
    def forbidden(*args,**kwargs):pytest.fail('configuration unexpectedly used network/credential/native execution')
    monkeypatch.setattr(c,'observe',forbidden)
    monkeypatch.setattr(c,'call',forbidden)
    args = {'python':str(server['root']/'approved-python'),'cli':str(server['root']/'approved-cli'),
            'declared_cli_version':'fixture-pinned','settings_paths':[str(ws.settings),str(work/'absent-managed.json')],
            'native_sources':[{'path':str(ws.settings),'layer':'user'},
                              {'path':str(work/'absent-managed.json'),'layer':'managed'}],
            'skill_sources':[]}
    return c,ws,args


def plan(prepared):
    c,ws,args = prepared
    return configure.plan_local_configuration(c,ws.caller,ws.context,**args)


def test_plan_and_unconfirmed_apply_are_readonly_without_registration(prepared):
    c,ws,args = prepared
    before = snapshot(Path(c.isolation))
    value = plan(prepared)
    assert snapshot(Path(c.isolation)) == before
    with pytest.raises(configure.LocalModelError,match='CONFIRMATION_REQUIRED'):
        configure.apply_local_configuration(c,value,approved_id=None)
    assert snapshot(Path(c.isolation)) == before
    assert not c.identity and not c.credential.exists() and not c.outputs['registration'].exists()


def test_apply_uses_only_profile_owner_and_manifest_and_replays_lost_return(prepared):
    c,ws,args = prepared
    value = plan(prepared)
    result = configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    assert result['requires_native_validation'] is True
    assert result['launch_admissible'] is False
    saved = json.loads(c.profile_path.read_text())
    assert saved['launch_intent'] == value['intent']
    assert saved['configuration_stage'] == 'prepared' and 'agent_id' not in saved
    assert saved['launch_intent']['task_sha256']
    assert c._fixture_api['RuntimeClient'](c.path).identity is None
    manifest = json.loads((Path(c.config['wrapper_root'])/'install-state.json').read_text())
    assert str(c.profile_path) not in manifest['owned_files']
    assert 'launch_intent' not in manifest and not manifest.get('configure_pending')
    assert not c.credential.exists() and not c.outputs['registration'].exists()
    before = snapshot(Path(c.isolation))
    again = configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    assert again['changed'] is False and snapshot(Path(c.isolation)) == before
    assert not list(Path(c.isolation).rglob('must-not-run'))


@pytest.mark.parametrize('mutation',['owner','context','task','source','cli','pending'])
def test_apply_rejects_drift_before_output_publication(prepared,mutation):
    c,ws,args = prepared
    value = plan(prepared)
    if mutation == 'owner':api['atomic_json'](c.profile_path,{'unapproved':True})
    elif mutation == 'context':api['atomic_json'](c.path,{**c.config,'activation_enabled':True})
    elif mutation == 'task':ws.task.write_text('changed task\n')
    elif mutation == 'source':ws.settings.write_text('{"changed":true}')
    elif mutation == 'cli':
        binary = Path(args['cli']);binary.chmod(0o600);binary.write_text('# changed\n');binary.chmod(0o500)
    else:api['atomic_json'](c.outputs['registration'],{
        'kind':c._fixture_api['REG_PENDING_KIND'],**c.binding,'name':ws.caller['name'],
        'program':'codex','model':'fixture-glm-model','task_description':'fixture','registration_token':'fixture-token'})
    before = snapshot(Path(c.isolation))
    with pytest.raises((configure.LocalModelError,c._fixture_api['ClientError'])):
        configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    assert snapshot(Path(c.isolation)) == before


def test_manifest_publication_interruption_before_intent_resumes_same_plan(prepared):
    c,ws,args = prepared
    value = plan(prepared)
    def stop(stage):
        if stage == 'before-intent-save':raise OSError('fixture interruption')
    with pytest.raises(OSError,match='fixture interruption'):
        configure.apply_local_configuration(c,value,approved_id=value['plan_id'],checkpoint=stop)
    assert not c.profile_path.exists()
    changed = deepcopy(value);changed['inputs']['caller']['name'] = 'OtherChild'
    before = snapshot(Path(c.isolation))
    with pytest.raises(configure.LocalModelError,match='PLAN_CHANGED'):
        configure.apply_local_configuration(c,changed,approved_id=changed['plan_id'])
    assert snapshot(Path(c.isolation)) == before
    configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    assert json.loads(c.profile_path.read_text())['launch_intent'] == value['intent']


def test_context_change_while_planning_is_rejected_without_outputs(prepared,monkeypatch):
    c,ws,args = prepared
    original = configure.resources.auth_source
    def change(*values,**options):
        result = original(*values,**options)
        api['atomic_json'](c.path,{**c.config,'fixture_change':'during-plan'})
        return result
    monkeypatch.setattr(configure.resources,'auth_source',change)
    with pytest.raises(configure.LocalModelError,match='PLAN_CHANGED'):plan(prepared)
    assert not c.profile_path.exists() and not (Path(c.config['wrapper_root'])/'libexec').exists()


@pytest.mark.parametrize('mutation',['task','retained-task','original-removed'])
def test_materialization_uses_task_hash_and_fixed_retained_copy(prepared,mutation):
    c,ws,args = prepared
    value = plan(prepared)
    configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    intent,ctx,_,_ = configure._data(c,value['inputs'])
    if mutation == 'task':ws.task.write_text('changed source\n')
    else:
        c.outputs['task'].write_bytes(ws.task.read_bytes());c.outputs['task'].chmod(0o600)
        if mutation == 'retained-task':c.outputs['task'].write_text('changed retained copy\n')
        else:ws.task.unlink()
    from runtime_launch_intent import materialize_launch_intent
    def check():return materialize_launch_intent(intent,ctx,operation='resume')
    if mutation == 'original-removed':assert check()['intent_digest'] == intent['intent_digest']
    else:
        with pytest.raises(configure.LocalModelError,match='TASK_CHANGED'):check()


@pytest.mark.parametrize('mutation',['name','provider','task-hash','native-claim','fake-agent-id',
                                     'null-evidence','string-evidence','list-evidence'])
def test_staging_profile_is_typed_and_does_not_forge_registered_identity(prepared,mutation):
    c,ws,args = prepared
    value = plan(prepared)
    configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    row = json.loads(c.profile_path.read_text())
    intent = row['launch_intent']
    if mutation == 'name':intent['name'] = ''
    elif mutation == 'provider':intent['provider'] = 'unsupported'
    elif mutation == 'task-hash':intent['task_sha256'] = 'not-a-hash'
    elif mutation == 'native-claim':intent['configuration_evidence']['requires_native_validation'] = False
    elif mutation == 'null-evidence':intent['configuration_evidence'] = None
    elif mutation == 'string-evidence':intent['configuration_evidence'] = 'not-evidence'
    elif mutation == 'list-evidence':intent['configuration_evidence'] = []
    else:row['agent_id'] = 1
    from local_model_auth import intent_digest
    intent['intent_digest'] = intent_digest(intent)
    api['atomic_json'](c.profile_path,row)
    with pytest.raises(c._fixture_api['ClientError'],match='OUTPUT_SCHEMA_INVALID'):
        c._fixture_api['RuntimeClient'](c.path)


def test_same_configuration_plan_is_readonly_and_does_not_increment_revision(prepared):
    c,ws,args = prepared
    value = plan(prepared)
    configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    before = snapshot(Path(c.isolation))
    again = plan(prepared)
    assert again['intent'] == value['intent']
    assert snapshot(Path(c.isolation)) == before
    assert configure.apply_local_configuration(c,again,approved_id=again['plan_id'])['changed'] is False
    assert snapshot(Path(c.isolation)) == before


def test_changed_configuration_increments_revision_only_after_explicit_apply(prepared):
    c,ws,args = prepared
    first = plan(prepared)
    configure.apply_local_configuration(c,first,approved_id=first['plan_id'])
    ws.caller['chrome'] = True
    changed = plan(prepared)
    assert changed['intent']['revision'] == first['intent']['revision'] + 1
    assert json.loads(c.profile_path.read_text())['launch_intent'] == first['intent']
    configure.apply_local_configuration(c,changed,approved_id=changed['plan_id'])
    assert json.loads(c.profile_path.read_text())['launch_intent'] == changed['intent']


@pytest.mark.parametrize('provider',['codex','claude'])
def test_explicit_configuration_carries_native_proposal_but_cannot_promote_native_binding(prepared,provider):
    c,ws,args = prepared
    ws.caller['provider'] = provider
    if provider == 'claude':ws.settings.write_text('{}')
    ws.context['approved_settings_sha256'] = hashlib.sha256(ws.settings.read_bytes()).hexdigest()
    value = plan(prepared)
    native = value['intent']['effective_scope']['native_proposal']
    script = str(Path(c.config['wrapper_root'])/'bin/lib/runtime_client.py')
    assert script in value['intent']['configuration_evidence']['renderer_sources']
    assert native['requires_native_validation'] is True and native['launch_admissible'] is False
    result = configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    assert result['launch_admissible'] is False
    if provider == 'claude':
        settings = json.loads((c.runtime_dir/'provider-home/claude/local-model-settings.json').read_text())
        assert settings['permissions'] == native['native_settings']['permissions']
        assert 'apiKeyHelper' not in settings, 'authless profile must not invent a credential helper'
        hooks = settings['hooks']
        assert set(hooks) == {'SessionStart','PreToolUse','PostToolUse','SessionEnd'}
        assert len(hooks['PostToolUse']) == 2
        for operation in ('session-start','pre-edit','post-edit','notifications','end-session'):
            assert 'entry '+operation in json.dumps(hooks)
    from runtime_native_scope import require_native_binding
    with pytest.raises(configure.LocalModelError,match='ADAPTER_UNSUPPORTED'):require_native_binding(native)


def test_unknown_native_settings_fail_before_outputs_and_are_not_rewritten(prepared):
    c,ws,args = prepared
    ws.caller['provider'] = 'claude'
    ws.settings.write_text('{"hooks":{"SessionStart":[]}}')
    ws.context['approved_settings_sha256'] = hashlib.sha256(ws.settings.read_bytes()).hexdigest()
    before = snapshot(Path(c.isolation))
    with pytest.raises(configure.LocalModelError,match='ADAPTER_UNSUPPORTED'):plan(prepared)
    assert snapshot(Path(c.isolation)) == before


@pytest.mark.parametrize('provider',['codex','claude'])
def test_authenticated_configure_pins_source_and_publishes_shared_runner_without_executing_it(prepared,provider):
    c,ws,args = prepared
    ws.caller['provider'] = provider
    if provider == 'claude':ws.settings.write_text('{}')
    ws.context['approved_settings_sha256'] = hashlib.sha256(ws.settings.read_bytes()).hexdigest()
    command = [str(Path(args['cli']).with_name('approved-auth')),'fixture-account']
    ws.profile.write_text(ws.profile.read_text().replace('['+provider+']',
        '['+provider+']\nauth_command = '+json.dumps(command)))
    value = plan(prepared)
    auth = value['intent']['model']['resolved']['auth']
    assert auth['source']['executable']['path'] == command[0]
    assert auth['runner']['python']['path'] == args['python']
    target = Path(auth['runner']['path'])
    assert not target.exists()
    configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    assert target.stat().st_mode & 0o777 == 0o500
    assert hashlib.sha256(target.read_bytes()).hexdigest() == auth['runner']['sha256']
    assert target.read_bytes().startswith(('#!'+args['python']+' -I\n').encode())
    if provider == 'claude':
        settings = json.loads((c.runtime_dir/'provider-home/claude/local-model-settings.json').read_text())
        assert str(c.profile_path) in settings['apiKeyHelper']
        assert value['intent']['intent_digest'] in settings['apiKeyHelper']
        assert 'permissions' in settings
        assert settings['hooks'] == value['intent']['effective_scope']['framework_settings']['hooks']
    assert not list(Path(c.isolation).rglob('must-not-run'))


@pytest.mark.parametrize('mutation',['hook-command','hook-missing','foreign-context','foreign-script'])
def test_materialization_rejects_replaced_framework_hooks_even_after_redigest(prepared,mutation):
    c,ws,args = prepared
    ws.caller['provider'] = 'claude';ws.settings.write_text('{}')
    ws.context['approved_settings_sha256'] = hashlib.sha256(ws.settings.read_bytes()).hexdigest()
    value = plan(prepared)
    configure.apply_local_configuration(c,value,approved_id=value['plan_id'])
    intent,ctx,_,_ = configure._data(c,value['inputs'])
    scope = intent['effective_scope']
    if mutation == 'hook-command':scope['framework_settings']['hooks']['SessionEnd'][0]['hooks'][0]['command'] = '/wrong/exit'
    elif mutation == 'hook-missing':scope['framework_settings']['hooks'].pop('PreToolUse')
    elif mutation == 'foreign-context':
        hooks = scope['framework_settings']['hooks']
        hooks['SessionStart'][0]['hooks'][0]['command'] = hooks['SessionStart'][0]['hooks'][0]['command'].replace(str(c.path),'/foreign/runtime-client.json')
    else:ctx['runtime_client'] = str(c.isolation/'foreign/runtime_client.py')
    ctx['effective_scope'] = deepcopy(scope)
    from local_model_auth import intent_digest
    intent['intent_digest'] = intent_digest(intent)
    from runtime_launch_intent import materialize_launch_intent
    with pytest.raises(configure.LocalModelError,match='PERMISSION_PLAN_UNSATISFIABLE'):
        materialize_launch_intent(intent,ctx,operation='spawn')
