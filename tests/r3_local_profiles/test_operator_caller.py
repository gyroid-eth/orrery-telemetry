"""Actual operator full caller delivery, not native admission evidence."""
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
def test_operator_full_form_matches_actual_prepared_child(caller_probe, workspace, nonempty):
    expected_tools = {'mcp':['fixture_extra']} if nonempty else {}
    workspace.caller['tools'] = expected_tools
    captured = []
    intents = []
    for entry in ('prepare-child', 'operator'):
        owner, binary, capture, nonce = prepare_caller_invocation(workspace, entry)
        script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
        caller_probe(entry, workspace=workspace, real_entry=CALLERS[entry],
            provider_binary=binary, owner=owner, prepare_only=(entry == 'operator'),
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
        assert observed['argv'][-1] == (CALLER_MCP_STARTUP_PROMPT if nonempty else CALLER_STARTUP_PROMPT)
        assert_caller_tool_configuration(observed, owner, expected_tools)
        intent = json.loads(owner.read_text())['launch_intent']
        assert_intent_digest(intent, intent['intent_digest'])
        for field in ('cwd','task','name','parent','base','tools','clear_tools','resources','detached','worktree','chrome'):
            assert intent[field] == workspace.caller[field]
        assert_single_owner(caller_client_root(workspace), owner)
        if captured:
            assert_caller_capture(capture, captured[0])
            assert intent == intents[0]
        captured.append(observed)
        intents.append(intent)


@pytest.mark.parametrize('nonempty', [False, True])
def test_operator_full_prepare_only_does_not_start_provider(caller_probe, workspace, nonempty):
    workspace.caller['tools'] = {'mcp':['fixture_extra']} if nonempty else {}
    owner, binary, capture, nonce = prepare_caller_invocation(workspace, 'operator')
    caller_probe('operator', workspace=workspace, real_entry=CALLERS['operator'],
        provider_binary=binary, owner=owner, prepare_only=True, inspect_prepare_only=True,
        provider_session_id='native-fixture-session', task_delivery='mail', headless=False,
        fixture_mcp={'name':'fixture_extra','definition':{'command':'/usr/bin/true','args':[]}}
            if nonempty else None)
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    assert not (owner.parent/'runtime/launch.json').exists()
    state = json.loads((owner.parent/'runtime/child-state.json').read_text())
    assert state['phase'] == 'prepared' and state['tools_selection'] == {
        'base':'mail-only','tools':workspace.caller['tools']}
    assert (owner.parent/'runtime/task.md').read_bytes() == workspace.task.read_bytes()


def test_operator_full_execution_uses_real_R2_without_fixture_execute_helper(caller_probe,workspace):
    workspace.caller['detached'] = False
    owner, binary, capture, nonce = prepare_caller_invocation(workspace,'operator')
    script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
    caller_probe('operator',workspace=workspace,real_entry=CALLERS['operator'],
        provider_binary=binary,owner=owner,prepare_only=False,operator_execute=True,
        provider_session_id='native-fixture-session',task_delivery='mail',headless=False)
    observed = read_fresh_capture(capture,nonce)
    assert_runtime_capture(observed,owner.parent/'runtime/launch.json',capture=capture,
                           binary=binary,expected_script_sha256=script_hash)
    intent = json.loads(owner.read_text())['launch_intent']
    assert intent['detached'] is False
    assert_intent_digest(intent,intent['intent_digest'])
    assert json.loads((owner.parent/'runtime/launch.json').read_text())['intent_digest'] == intent['intent_digest']
    assert observed['cwd'] == workspace.caller['cwd']
    argv = observed['argv']
    models = [value.split('=',1)[1] for value in argv if value.startswith('--model=')]
    models += [argv[i+1] for i,value in enumerate(argv[:-1]) if value in ('--model','-m')]
    assert models == ['fixture-glm-model']
    assert observed['env']['HOME'] == str(owner.parent/'runtime/provider-home/codex')
    assert observed['argv'][-1] == CALLER_STARTUP_PROMPT
    assert_caller_tool_configuration(observed,owner,{})


def test_real_R2_rejects_stale_admitted_digest_before_window_or_launch(caller_probe,workspace):
    owner, binary, capture, nonce = prepare_caller_invocation(workspace,'operator')
    with pytest.raises(RuntimeError,match='LOCAL_MODEL_INTENT_CHANGED') as refused:
        caller_probe('operator',workspace=workspace,real_entry=CALLERS['operator'],
            provider_binary=binary,owner=owner,prepare_only=True,operator_bad_pin=True,
            provider_session_id='native-fixture-session',task_delivery='mail',headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: operator:')
    intent = json.loads(owner.read_text())['launch_intent']
    assert_intent_digest(intent,intent['intent_digest'])
    assert intent['intent_digest'] != '0'*64
    state = json.loads((owner.parent/'runtime/child-state.json').read_text())
    assert state['phase']=='prepared'  # actual successful operator prerequisite
    assert (owner.parent/'runtime/task.md').read_bytes() == workspace.task.read_bytes()
    identity = json.loads((owner.parent/'runtime-client.json').read_text())['identity']
    assert identity and 'window_row_id' not in identity and 'window_uuid' not in identity
    assert not (owner.parent/'runtime/launch.json').exists()
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    calls = {(row.get('file'),row.get('name')) for row in caller_probe.last_entry_rows
             if row['event']=='real-call'}
    assert (str(caller_probe.install/'bin/lib/runtime_delivery.py'),'prepare_launch') in calls
    assert (str(caller_probe.install/'bin/lib/runtime_operator.py'),'spawn') in calls


@pytest.mark.parametrize('field', [
    'provider','model','cwd','task','name','parent','tools','base','clear_tools',
    'detached','resources','worktree','chrome','effort','strict_output_cap','task_delivery'])
def test_actual_operator_rejects_each_different_incoming_literal(caller_probe,workspace,field):
    # Independent valid inputs, not an expected value rebuilt by a resolver.
    # Optional absent vs empty/false/default is deliberately NOT equivalent.
    changed = {'provider':'claude','model':'local:other', 'cwd':str(workspace.tmp/'vault'),
        'task':str(workspace.handoff),'name':'OtherChild','parent':'OtherParent',
        'tools':{'mcp':['fixture_extra']},'base':'default','clear_tools':True,
        'detached':False,'resources':[],'worktree':True,'chrome':True,
        'effort':'','strict_output_cap':False,'task_delivery':'mail'}
    assert field not in workspace.caller or workspace.caller[field] != changed[field]
    owner,binary,capture,nonce = prepare_caller_invocation(workspace,'operator')
    before = []
    saved = []
    def snapshot():
        before.append(snapshot_tree(caller_client_root(workspace)))
        saved.append(owner.read_bytes())
    reason = ('LOCAL_MODEL_IDENTITY_MISMATCH' if field in ('provider','cwd')
              else 'LOCAL_MODEL_INTENT_CHANGED')
    with pytest.raises(RuntimeError,match=reason) as refused:
        caller_probe('operator',workspace=workspace,real_entry=CALLERS['operator'],
            provider_binary=binary,owner=owner,prepare_only=True,inspect_prepare_only=True,
            operator_caller_overrides={field:changed[field]},on_before_real_entry=snapshot,
            provider_session_id='native-fixture-session',task_delivery='mail',headless=False)
    assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: operator:')
    assert str(refused.value).rstrip().endswith(': '+reason)
    assert len(before)==len(saved)==1
    assert_tree_unchanged(caller_client_root(workspace),before[0])
    assert owner.read_bytes()==saved[0]
    assert not capture.exists() and not capture.with_suffix('.driver.json').exists()
    calls = {(row.get('file'),row.get('name')) for row in caller_probe.last_entry_rows
             if row['event']=='real-call'}
    for file,name in [('runtime_operator.py','spawn'),
                      ('runtime_local_launch.py','admit_caller_request')]:
        assert (str(caller_probe.install/'bin/lib'/file),name) in calls
    assert (str(caller_probe.install/'bin/lib/runtime_client.py'),'prepare_child') not in calls
    intent=json.loads(owner.read_text())['launch_intent']
    assert_intent_digest(intent,intent['intent_digest'])
