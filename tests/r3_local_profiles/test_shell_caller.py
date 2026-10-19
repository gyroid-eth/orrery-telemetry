"""Actual preregister/shell/task/R2 delivery, not detached/native acceptance."""
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
def test_actual_full_shell_matches_prepared_child_delivery(caller_probe, workspace, nonempty):
    tools = {'mcp':['fixture_extra']} if nonempty else {}
    workspace.caller['tools'] = tools
    captures, intents = [], []
    for entry in ('prepare-child', 'shell'):
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
def test_actual_shell_prepare_only_publishes_task_without_execution(caller_probe, workspace, nonempty):
    workspace.caller['tools'] = {'mcp':['fixture_extra']} if nonempty else {}
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'shell')
    result = caller_probe('shell', workspace=workspace, real_entry=CALLERS['shell'],
        provider_binary=binary, owner=owner, prepare_only=True, inspect_prepare_only=True,
        provider_session_id='native-fixture-session', task_delivery='mail', headless=False,
        fixture_mcp={'name':'fixture_extra', 'definition':{'command':'/usr/bin/true','args':[]}}
            if nonempty else None)
    assert result.returncode == 0
    assert json.loads(result.stdout)['prepared'] is True
    assert json.loads(result.stdout)['runtime_ready'] is False
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    assert not (owner.parent/'runtime/launch.json').exists()
    state = json.loads((owner.parent/'runtime/child-state.json').read_text())
    assert state['phase'] == 'prepared' and state['tools_selection'] == {
        'base':'mail-only','tools':workspace.caller['tools']}
    assert (owner.parent/'runtime/task.md').read_bytes() == workspace.task.read_bytes()
    assert caller_probe.last_shell_preparation['intent'] == json.loads(owner.read_text())['launch_intent']


@pytest.mark.parametrize('field', [
    'provider','model','cwd','task','name','parent','tools','base','clear_tools',
    'detached','resources','worktree','chrome','effort','strict_output_cap','task_delivery'])
def test_actual_shell_rejects_each_different_incoming_literal(caller_probe, workspace, field):
    changed = {'provider':'claude', 'model':'local:other', 'cwd':str(workspace.tmp/'vault'),
        'task':str(workspace.handoff), 'name':'OtherChild', 'parent':'OtherParent',
        'tools':{'mcp':['fixture_extra']}, 'base':'default', 'clear_tools':True,
        'detached':False, 'resources':[], 'worktree':True, 'chrome':True,
        'effort':'', 'strict_output_cap':False, 'task_delivery':'mail'}
    assert field not in workspace.caller or workspace.caller[field] != changed[field]
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'shell')
    before, saved = [], []
    def snapshot():
        before.append(snapshot_tree(caller_client_root(workspace)))
        saved.append(owner.read_bytes())
    reason = ('CHILD_PARENT_MISMATCH' if field == 'parent' else
              'LOCAL_MODEL_IDENTITY_MISMATCH' if field in ('provider','cwd') else 'LOCAL_MODEL_INTENT_CHANGED')
    with pytest.raises(RuntimeError, match=reason) as refused:
        caller_probe('shell', workspace=workspace, real_entry=CALLERS['shell'],
            provider_binary=binary, owner=owner, prepare_only=True, inspect_prepare_only=True,
            shell_caller_overrides={field:changed[field]}, on_before_real_entry=snapshot,
            provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: shell:')
    assert str(refused.value).rstrip().endswith(': '+reason)
    assert len(before) == len(saved) == 1
    assert_tree_unchanged(caller_client_root(workspace), before[0])
    assert owner.read_bytes() == saved[0]
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    calls = {(row.get('file'),row.get('name')) for row in caller_probe.last_entry_rows
             if row['event']=='real-call'}
    for file, name in [('runtime_client.py','entry_operation'),
                       ('runtime_client.py','_shell_caller_input')]:
        assert (str(caller_probe.install/'bin/lib'/file), name) in calls


def test_actual_shell_R2_rejects_stale_digest_after_task_preparation(caller_probe, workspace):
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'shell')
    with pytest.raises(RuntimeError, match='LOCAL_MODEL_INTENT_CHANGED') as refused:
        caller_probe('shell', workspace=workspace, real_entry=CALLERS['shell'],
            provider_binary=binary, owner=owner, prepare_only=False, shell_bad_pin=True,
            provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: shell:')
    intent = json.loads(owner.read_text())['launch_intent']
    assert_intent_digest(intent, intent['intent_digest'])
    assert intent['intent_digest'] != '0'*64
    assert (owner.parent/'runtime/task.md').read_bytes() == workspace.task.read_bytes()
    assert not (owner.parent/'runtime/launch.json').exists()
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    calls = {(row.get('file'),row.get('name')) for row in caller_probe.last_entry_rows
             if row['event']=='real-call'}
    assert (str(caller_probe.install/'bin/lib/runtime_delivery.py'),'prepare_launch') in calls


def test_actual_shell_root_noop_is_rejected_before_any_R2_helper(caller_probe, workspace):
    # Unlike the previous-success matrix, this control needs no seed delivery:
    # a root no-op must be observable before process/native execution at all.
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'shell')
    before = []
    def snapshot():before.append(snapshot_tree(caller_client_root(workspace)))
    with pytest.raises(RuntimeError, match='R3_REAL_SHELL_TASK_PREPARATION_MISSING') as refused:
        caller_probe('shell', workspace=workspace, real_entry=CALLERS['shell'],
            provider_binary=binary, owner=owner, prepare_only=True, inspect_prepare_only=True,
            real_entry_mutation='no-op', on_before_real_entry=snapshot,
            provider_session_id='native-fixture-session', task_delivery='mail', headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: shell:')
    records = [json.loads(line) for line in (workspace.tmp/'mutation-shell.jsonl').read_text().splitlines()]
    assert [row['event'] for row in records] == ['installed','activated']
    assert all(row['target']=='runtime_client.entry_operation'
               and row['method_file']==str(caller_probe.install/'bin/lib/runtime_client.py')
               and row['method_name']=='entry_operation' and row['module']=='__main__'
               and row['entry']=='spawn-child' for row in records)
    assert len({row['pid'] for row in records}) == 1
    changes = [row for row in caller_probe.last_entry_rows if row['event']=='shell-entry-mutation']
    assert len(changes)==1 and changes[0]['pid']==records[0]['pid']
    assert changes[0]['replacements']==['runtime_client.entry_operation'] and changes[0]['replacement_count']==1
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
