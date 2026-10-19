"""Native settings proposals, never fake CLI permission/skill evidence."""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin/lib'))
native = importlib.import_module('runtime_native_scope')


@pytest.fixture
def inputs(tmp_path):
    cwd = tmp_path/'work';cwd.mkdir()
    task = tmp_path/'task.md';task.write_text('fixture task')
    settings = tmp_path/'settings.json';settings.write_text('{}')
    scope = {'read':[str(cwd),str(task)],'edit':[str(cwd)],'built_in_read':[str(cwd)],
             'bash':['git status && git diff | head -10'],
             'mail_tools':['mcp__fixture__'+name for name in ('fetch_inbox','send_message',
                 'reserve_files','renew_reservations','release_reservations')],
             'sandbox_auto_allow':False,'inherited_bash_allow':[],
             'reservation_hook':'enabled','settings_sources':[str(settings)],
             'cli_version':'unobserved-fixture-version'}
    sources = [{'path':str(settings),'layer':'user'},
               {'path':str(tmp_path/'absent-managed.json'),'layer':'managed'}]
    return scope,sources,settings


@pytest.mark.parametrize('provider',['claude','codex'])
def test_plan_is_readonly_declares_unknown_builtins_and_never_grants_launch(inputs,provider):
    scope,sources,settings = inputs
    if provider == 'codex':settings.write_text('')
    before = {str(p):(p.stat().st_ino,p.stat().st_mtime_ns,p.read_bytes())
              for p in settings.parent.rglob('*') if p.is_file()}
    value = native.plan_native_scope(provider,scope,sources)
    assert value['requested_scope'] == scope
    assert value['declared_cli_version'] == 'unobserved-fixture-version'
    assert value['built_in_operations']['status'] == 'unverified'
    assert value['source_closure']['status'] == 'unverified'
    assert value['requires_native_validation'] is True and value['launch_admissible'] is False
    assert value['settings_sources'][1]['evidence'] == {'state':'absent'}
    assert value == native.plan_native_scope(provider,scope,sources)
    after = {str(p):(p.stat().st_ino,p.stat().st_mtime_ns,p.read_bytes())
             for p in settings.parent.rglob('*') if p.is_file()}
    assert after == before
    with pytest.raises(native.LocalModelError,match='ADAPTER_UNSUPPORTED'):
        native.require_native_binding(value)


def test_claude_native_rule_strings_are_literal_not_a_bash_parser(inputs):
    scope,sources,_ = inputs
    value = native.plan_native_scope('claude',scope,sources)
    settings = value['native_settings']
    assert settings['permissions']['defaultMode'] == 'dontAsk'
    assert settings['sandbox']['autoAllowBashIfSandboxed'] is False
    allow = settings['permissions']['allow']
    assert 'Bash(git status && git diff | head -10)' in allow
    assert 'Read(//'+scope['read'][1].lstrip('/')+')' in allow
    assert 'Edit(//'+scope['edit'][0].lstrip('/')+'/**)' in allow
    assert all(tool in allow for tool in scope['mail_tools'])
    assert 'Skill' in allow
    assert settings['permissions']['deny'] == settings['permissions']['ask'] == []


@pytest.mark.parametrize('content',[
    '{"permissions":{"allow":["Bash(*)"]}}',
    '{"permissions":{"allow":["Read(*)"]}}',
    '{"permissions":{"deny":["Read(*)"]}}',
    '{"permissions":{"ask":["mcp__fixture__send_message"]}}',
    '{"permissions":{"defaultMode":"bypassPermissions"}}',
    '{"sandbox":{"autoAllowBashIfSandboxed":true}}',
    '{"hooks":{}}','{"enabledPlugins":{}}','{"env":{}}',
    '{"apiKeyHelper":"some-helper"}', '{"fixture_sentinel":"unknown"}',
])
def test_unknown_or_inherited_native_effects_are_not_erased_or_treated_as_empty(inputs,content):
    scope,sources,settings = inputs;settings.write_text(content)
    with pytest.raises(native.LocalModelError,match='ADAPTER_UNSUPPORTED'):
        native.plan_native_scope('claude',scope,sources)
    assert settings.read_text() == content


@pytest.mark.parametrize('content',[
    'approval_policy="on-request"','sandbox_mode="danger-full-access"',
    '[sandbox_workspace_write]\nwritable_roots=["/outside"]',
    '[features]\nsome_unknown_feature=true','[mcp_servers.unknown]\ncommand="unknown"',
    'model="ambient-model"','[sandbox_workspace_write]\nnetwork_access=true',
])
def test_codex_unknown_or_wider_ambient_settings_are_unsupported(inputs,content):
    scope,sources,settings = inputs;settings.write_text(content)
    with pytest.raises(native.LocalModelError,match='ADAPTER_UNSUPPORTED'):
        native.plan_native_scope('codex',scope,sources)


@pytest.mark.parametrize('content',['{"permissions":{},"permissions":{}}','[]','{','{"permissions":null}'])
def test_malformed_or_duplicate_settings_fail_closed(inputs,content):
    scope,sources,settings = inputs;settings.write_text(content)
    with pytest.raises(native.LocalModelError):native.plan_native_scope('claude',scope,sources)


@pytest.mark.parametrize('mutation',['source-missing','source-duplicate','layer-duplicate','unknown-layer',
                                     'symlink','wildcard-path','new-scope-key','permission-auto-allow',
                                     'mail-missing','mail-alias','mail-pattern'])
def test_source_and_scope_admission_has_no_unknown_fallback(inputs,mutation):
    scope,sources,settings = inputs
    if mutation == 'source-missing':sources.pop(0)
    elif mutation == 'source-duplicate':sources[1]['path'] = sources[0]['path']
    elif mutation == 'layer-duplicate':sources[1]['layer'] = 'user'
    elif mutation == 'unknown-layer':sources[0]['layer'] = 'unrecognized'
    elif mutation == 'symlink':
        link = settings.with_name('linked.json');link.symlink_to(settings)
        scope['settings_sources'] = [str(link)];sources[0]['path'] = str(link)
    elif mutation == 'wildcard-path':scope['read'][0] += '/wildcard*'
    elif mutation == 'new-scope-key':scope['pretend_verified'] = True
    elif mutation == 'mail-missing':scope['mail_tools'].pop()
    elif mutation == 'mail-alias':scope['mail_tools'][0] = 'mcp__foreign__fetch_inbox'
    elif mutation == 'mail-pattern':scope['mail_tools'][0] = 'mcp__fixture__*'
    else:scope['sandbox_auto_allow'] = True
    with pytest.raises(native.LocalModelError):native.plan_native_scope('claude',scope,sources)


@pytest.mark.parametrize('mutation',['builtin','source','rules','version'])
def test_proposal_digest_covers_all_scope_and_native_evidence(inputs,mutation):
    scope,sources,_ = inputs
    value = native.plan_native_scope('claude',scope,sources)
    changed = deepcopy(value)
    if mutation == 'builtin':changed['built_in_operations']['declared_read'] = ['/']
    elif mutation == 'source':changed['settings_sources'][1]['evidence'] = {'state':'present'}
    elif mutation == 'rules':changed['native_settings']['permissions']['allow'] += ['Bash(*)']
    else:changed['declared_cli_version'] = 'another-version'
    assert native.canonical_digest({k:v for k,v in changed.items() if k!='plan_digest'}) != value['plan_digest']
    changed['requires_native_validation'] = False;changed['launch_admissible'] = True
    with pytest.raises(native.LocalModelError,match='ADAPTER_UNSUPPORTED'):
        native.require_native_binding(changed)
