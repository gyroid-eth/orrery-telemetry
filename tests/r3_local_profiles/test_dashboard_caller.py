"""Actual HTTP prepare/task and separate TEST R2, not detached/native acceptance."""
import hashlib
import json

import pytest

from .evidence import (CALLERS, CALLER_STARTUP_PROMPT, CALLER_MCP_STARTUP_PROMPT,
                       prepare_caller_invocation, read_fresh_capture,
                       assert_runtime_capture, assert_caller_capture,
                       assert_caller_tool_configuration, assert_single_owner,
                       caller_client_root, assert_intent_digest, snapshot_tree,
                       assert_tree_unchanged)


@pytest.mark.parametrize('nonempty', [False, True])
def test_actual_full_dashboard_matches_prepared_child_delivery(caller_probe, workspace, nonempty):
    tools = {'mcp':['fixture_extra']} if nonempty else {}
    workspace.caller['tools'] = tools
    captures, intents = [], []
    for entry in ('prepare-child', 'dashboard'):
        owner, binary, capture, nonce = prepare_caller_invocation(workspace, entry)
        script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
        caller_probe(entry, workspace=workspace, real_entry=CALLERS[entry],
            provider_binary=binary, owner=owner, prepare_only=False,
            provider_session_id='native-fixture-session', task_delivery='mail', headless=False,
            fixture_mcp={'name':'fixture_extra', 'definition':{'command':'/usr/bin/true','args':[]}}
                if nonempty else None)
        observed = read_fresh_capture(capture, nonce)
        assert_runtime_capture(observed, owner.parent/'runtime/launch.json', capture=capture,
                               binary=binary, expected_script_sha256=script_hash)
        assert observed['cwd'] == workspace.caller['cwd']
        argv = observed['argv']
        models = [value.split('=',1)[1] for value in argv if value.startswith('--model=')]
        models += [argv[i+1] for i,value in enumerate(argv[:-1]) if value in ('--model','-m')]
        assert models == ['fixture-glm-model']
        assert argv[-1] == (CALLER_MCP_STARTUP_PROMPT if nonempty else CALLER_STARTUP_PROMPT)
        assert_caller_tool_configuration(observed, owner, tools)
        intent = json.loads(owner.read_text())['launch_intent']
        assert_intent_digest(intent, intent['intent_digest'])
        for field in ('cwd','task','name','parent','base','tools','clear_tools','resources','detached','worktree','chrome'):
            assert intent[field] == workspace.caller[field]
        assert_single_owner(caller_client_root(workspace), owner)
        if captures:
            assert_caller_capture(capture, captures[0])
            assert intent == intents[0]
        captures.append(observed)
        intents.append(intent)


@pytest.mark.parametrize('nonempty', [False, True])
def test_actual_dashboard_prepare_only_publishes_task_without_execution(caller_probe, workspace, nonempty):
    workspace.caller['tools'] = {'mcp':['fixture_extra']} if nonempty else {}
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'dashboard')
    result = caller_probe('dashboard', workspace=workspace, real_entry=CALLERS['dashboard'],
        provider_binary=binary, owner=owner, prepare_only=True, inspect_prepare_only=True,
        provider_session_id='native-fixture-session', task_delivery='mail', headless=False,
        fixture_mcp={'name':'fixture_extra', 'definition':{'command':'/usr/bin/true','args':[]}}
            if nonempty else None)
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response['status'] == 200 and response['body']['ok'] is True
    assert caller_probe.last_dashboard_http == response
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    assert not (owner.parent/'runtime/launch.json').exists()
    state = json.loads((owner.parent/'runtime/child-state.json').read_text())
    assert state['phase'] == 'prepared' and state['tools_selection'] == {
        'base':'mail-only','tools':workspace.caller['tools']}
    assert (owner.parent/'runtime/task.md').read_bytes() == workspace.task.read_bytes()
    assert caller_probe.last_dashboard_preparation['intent'] == json.loads(owner.read_text())['launch_intent']


@pytest.mark.parametrize('field', [
    'provider','model','cwd','task','name','parent','tools','base','clear_tools',
    'detached','resources','worktree','chrome','effort','strict_output_cap','task_delivery'])
def test_actual_dashboard_rejects_each_different_incoming_literal(caller_probe, workspace, field):
    changed = {'provider':'claude', 'model':'local:other', 'cwd':str(workspace.tmp/'vault'),
        'task':str(workspace.handoff), 'name':'OtherChild', 'parent':'OtherParent',
        'tools':{'mcp':['fixture_extra']}, 'base':'default', 'clear_tools':True,
        'detached':False, 'resources':[], 'worktree':True, 'chrome':True,
        'effort':'', 'strict_output_cap':False, 'task_delivery':'mail'}
    assert field not in workspace.caller or workspace.caller[field] != changed[field]
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'dashboard')
    before, saved = [], []
    def snapshot():
        before.append(snapshot_tree(caller_client_root(workspace)))
        saved.append(owner.read_bytes())
    reason = ('LOCAL_MODEL_IDENTITY_MISMATCH' if field in ('provider','cwd')
              else 'LOCAL_MODEL_INTENT_CHANGED')
    with pytest.raises(RuntimeError, match=reason) as refused:
        caller_probe('dashboard', workspace=workspace, real_entry=CALLERS['dashboard'],
            provider_binary=binary, owner=owner, prepare_only=True, inspect_prepare_only=True,
            dashboard_caller_overrides={field:changed[field]}, on_before_real_entry=snapshot,
            provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: dashboard:')
    assert str(refused.value).rstrip().endswith(': '+reason)
    assert len(before) == len(saved) == 1
    assert_tree_unchanged(caller_client_root(workspace), before[0])
    assert owner.read_bytes() == saved[0]
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    calls = {(row.get('file'),row.get('name')) for row in caller_probe.last_entry_rows
             if row['event']=='real-call'}
    for file, name in [('dashboard/server.py','do_spawn'),
                       ('dashboard/global_backend.py','spawn'),
                       ('bin/lib/runtime_local_launch.py','admit_caller_request')]:
        assert (str(caller_probe.install/file), name) in calls
    assert caller_probe.last_dashboard_http == {'status':409, 'body':{'ok':False, 'reason':reason}}


def test_actual_dashboard_R2_rejects_stale_digest_after_task_preparation(caller_probe, workspace):
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'dashboard')
    with pytest.raises(RuntimeError, match='LOCAL_MODEL_INTENT_CHANGED') as refused:
        caller_probe('dashboard', workspace=workspace, real_entry=CALLERS['dashboard'],
            provider_binary=binary, owner=owner, prepare_only=False, dashboard_bad_pin=True,
            provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: dashboard:')
    intent = json.loads(owner.read_text())['launch_intent']
    assert_intent_digest(intent, intent['intent_digest'])
    assert intent['intent_digest'] != '0'*64
    assert (owner.parent/'runtime/task.md').read_bytes() == workspace.task.read_bytes()
    assert not (owner.parent/'runtime/launch.json').exists()
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    calls = {(row.get('file'),row.get('name')) for row in caller_probe.last_entry_rows
             if row['event']=='real-call'}
    assert (str(caller_probe.install/'bin/lib/runtime_delivery.py'),'prepare_launch') in calls


def test_actual_dashboard_root_noop_is_rejected_before_any_R2_helper(caller_probe, workspace):
    # Unlike the previous-success matrix, this control needs no seed delivery:
    # a root no-op must be observable before process/native execution at all.
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'dashboard')
    before = []
    def snapshot():before.append(snapshot_tree(caller_client_root(workspace)))
    with pytest.raises(RuntimeError, match='R3_REAL_DASHBOARD_TASK_PREPARATION_MISSING') as refused:
        caller_probe('dashboard', workspace=workspace, real_entry=CALLERS['dashboard'],
            provider_binary=binary, owner=owner, prepare_only=True, inspect_prepare_only=True,
            real_entry_mutation='no-op', on_before_real_entry=snapshot,
            provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: dashboard:')
    records = [json.loads(line) for line in (workspace.tmp/'mutation-dashboard.jsonl').read_text().splitlines()]
    assert [row['event'] for row in records] == ['installed','activated']
    assert all(row['target']=='dashboard.server.do_spawn'
               and row['method_file']==str(caller_probe.install/'dashboard/server.py')
               and row['method_name']=='do_spawn' and row['module']=='dashboard.server'
               and row['replacements']==['dashboard.server.do_spawn']
               and row['replacement_count']==1 for row in records)
    assert len({row['pid'] for row in records}) == 1
    assert any(row['event']=='real-call' and row['pid']==records[0]['pid']
               and row['file']==str(caller_probe.install/'dashboard/server.py')
               and row['name']=='do_POST' for row in caller_probe.last_entry_rows)
    assert any(row['event']=='bootstrap' and row['pid']==records[0]['pid']
               and row['replacements']==['runtime_native_scope.require_native_binding']
               and row['replacement_count']==1 for row in caller_probe.last_entry_rows)
    assert len(before)==1
    assert_tree_unchanged(caller_client_root(workspace), before[0])
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    assert not (owner.parent/'runtime/task.md').exists() and not (owner.parent/'runtime/launch.json').exists()
    assert not any(row['event']=='real-call' and row['name'] in ('start','execute','exec_provider')
                   and row['file']==str(caller_probe.install/'bin/lib/runtime_delivery.py')
                   for row in caller_probe.last_entry_rows)
