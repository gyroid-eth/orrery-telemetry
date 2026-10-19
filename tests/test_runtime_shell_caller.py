"""Actual shell caller admission with catalog fixture data, never native CLI."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

from test_global_child_client import api, client, shell, server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin/lib'))
from runtime_model_catalog import resolve_catalog_intent
from runtime_intent_store import save_launch_intent
from r3_local_profiles.evidence import snapshot_tree, assert_tree_unchanged


@pytest.fixture
def prepared_caller(server):
    parent = client(server)
    cwd = parent.isolation / 'caller-cwd'
    cwd.mkdir(mode=0o700)
    task = cwd / 'task.md'
    task.write_text('Catalog-only shell preparation fixture.\n')
    task.chmod(0o600)
    caller = dict(provider='codex', model='gpt-fixture-future-9', cwd=str(cwd),
        task=str(task), name='ShellCallerChild', parent=parent.identity['name'],
        tools={}, base='mail-only', clear_tools=False, detached=True,
        resources=[str(cwd / 'Scratch.md')], worktree=False, chrome=False)
    result = parent.prepare_child(dict(child_client_name='child_01', name=caller['name'],
        program='codex', model=caller['model'], workdir=caller['cwd'],
        base=caller['base'], parsed_tools={}, prepare_only=True))
    child = api['RuntimeClient'](result['client_config'])
    child.save_profile()
    intent = resolve_catalog_intent(caller, {'operator_uid':os.getuid()})
    with child.operation_mutex(), child.fence():
        save_launch_intent(child.profile_path, intent, mode='global', locked=True,
            writer=lambda path, value: child.write_output(path, value, 'profile'))
    return parent, child, caller, intent


def command(caller):
    args = ['--child-client-name', 'child_01', '--name', caller['name'],
        '--model', caller['model'], '--parent', caller['parent'],
        '--base', caller['base'], '--task-file', caller['task'],
        '--resources', ','.join(caller['resources']), '--prepare-only']
    args += ['--codex'] if caller['provider'] == 'codex' else ['--program', 'claude-code']
    args.append('--detached' if caller['detached'] else '--no-detached')
    if caller['clear_tools']: args.append('--clear-tools')
    if caller['worktree']: args.append('--worktree')
    if caller['chrome']: args.append('--chrome')
    for value in caller['tools'].get('mcp', []): args += ['--tools', 'mcp:' + value]
    for key, flag in [('effort', '--effort'), ('task_delivery', '--task-delivery')]:
        if key in caller: args += [flag, caller[key]]
    if 'strict_output_cap' in caller:
        args.append('--strict-output-cap' if caller['strict_output_cap'] else '--no-strict-output-cap')
    return [*args, caller['cwd']]


def test_actual_full_shell_publishes_task_and_preserves_sole_catalog_intent(prepared_caller):
    parent, child, caller, intent = prepared_caller
    before = child.profile_path.read_bytes()
    result = shell(parent, 'hooks/spawn_child.sh', command(caller))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['prepared'] is True
    assert child.outputs['task'].read_bytes() == Path(caller['task']).read_bytes()
    assert child.profile_path.read_bytes() == before
    assert api['read_json'](child.profile_path)['launch_intent'] == intent
    assert not child.outputs['launch'].exists()


@pytest.mark.parametrize('field', ['provider', 'model', 'cwd', 'task', 'name', 'parent', 'tools',
    'base', 'clear_tools', 'detached', 'resources', 'worktree', 'chrome',
    'effort', 'strict_output_cap', 'task_delivery'])
def test_actual_full_shell_rejects_different_incoming_literal_before_effects(prepared_caller, field):
    parent, child, caller, intent = prepared_caller
    changed = {'provider':'claude', 'model':'gpt-other-fixture-9', 'cwd':str(parent.isolation),
        'task':str(parent.isolation / 'different.md'), 'name':'OtherChild',
        'parent':'OtherParent', 'tools':{'mcp':['fixture_extra']}, 'base':'default',
        'clear_tools':True, 'detached':False, 'resources':[], 'worktree':True,
        'chrome':True, 'effort':'', 'strict_output_cap':False, 'task_delivery':'mail'}
    incoming = {**deepcopy(caller), field:changed[field]}
    before = snapshot_tree(child.client_root)
    result = shell(parent, 'hooks/spawn_child.sh', command(incoming))
    reason = ('CHILD_PARENT_MISMATCH' if field == 'parent' else
              'LOCAL_MODEL_IDENTITY_MISMATCH' if field in ('provider', 'cwd') else 'LOCAL_MODEL_INTENT_CHANGED')
    assert result.returncode == 2 and result.stderr.rstrip().endswith(reason), result.stderr
    assert_tree_unchanged(child.client_root, before)
    assert not child.outputs['task'].exists() and not child.outputs['launch'].exists()


@pytest.mark.parametrize('field', ['--name', '--model', '--task-file', 'cwd'])
def test_full_shell_does_not_fill_missing_input_from_saved_owner(prepared_caller, field):
    parent, child, caller, intent = prepared_caller
    args = command(caller)
    if field == 'cwd': args.pop()
    else:
        index = args.index(field)
        del args[index:index + 2]
    before = snapshot_tree(child.client_root)
    result = shell(parent, 'hooks/spawn_child.sh', args)
    assert result.returncode == 2 and result.stderr.rstrip().endswith('LOCAL_MODEL_CALLER_INVALID'), result.stderr
    assert_tree_unchanged(child.client_root, before)


@pytest.mark.parametrize('tool', ['mail', 'shell'])
def test_actual_full_shell_keeps_existing_tools_grammar(prepared_caller, tool):
    parent, child, caller, intent = prepared_caller
    args = command(caller)
    args[-1:-1] = ['--tools', tool]
    before = snapshot_tree(child.client_root)
    result = shell(parent, 'hooks/spawn_child.sh', args)
    assert result.returncode == 2 and result.stderr.rstrip().endswith('CHILD_TOOLS_INVALID'), result.stderr
    assert_tree_unchanged(child.client_root, before)


@pytest.mark.parametrize('changed', [False, True])
def test_minimal_task_reference_preserves_intent_and_checks_supplied_task(prepared_caller, changed):
    parent, child, caller, intent = prepared_caller
    args = ['--child-client-name', 'child_01', '--prepare-only', '--task-file',
            str(parent.isolation / 'other.md') if changed else caller['task']]
    before = snapshot_tree(child.client_root)
    result = shell(parent, 'hooks/spawn_child.sh', args)
    if changed:
        assert result.returncode == 2 and result.stderr.rstrip().endswith('LOCAL_MODEL_INTENT_CHANGED'), result.stderr
        assert_tree_unchanged(child.client_root, before)
    else:
        assert result.returncode == 0, result.stderr
        assert child.outputs['task'].read_bytes() == Path(caller['task']).read_bytes()
    assert api['read_json'](child.profile_path)['launch_intent'] == intent


@pytest.mark.parametrize('detached', [False, True])
def test_full_shell_passes_under_C_admitted_digest_to_existing_R2(prepared_caller, monkeypatch, detached):
    # Argument handoff only: stop at the real R2.start boundary, never claim a
    # fake process/transport response as native execution acceptance.
    parent, child, caller, intent = prepared_caller
    if not detached:
        from local_model_auth import intent_digest
        intent = {**intent, 'detached':False, 'revision':2}
        intent['intent_digest'] = intent_digest(intent)
        caller = {**caller, 'detached':False}
        with child.operation_mutex(), child.fence():
            save_launch_intent(child.profile_path, intent, mode='global', locked=True,
                writer=lambda path, value: child.write_output(path, value, 'profile'))
    class Forwarded(Exception): pass
    observed = []
    delivery = api['runtime_delivery'](child)
    def start(self, provider, cwd, **kwargs):
        held = getattr(api['RuntimeClient'].operation_mutex.__wrapped__.__globals__['_OPERATION_HELD'], 'paths', set())
        assert str(self.client.outputs['operation_lock']) not in held, 'R2 called while child C is held'
        observed.append((str(self.client.path), provider, cwd, kwargs))
        raise Forwarded()
    monkeypatch.setattr(type(delivery), 'start', start)
    args = command(caller)
    args.remove('--prepare-only')
    with pytest.raises(Forwarded):
        api['entry_operation']('spawn-child', ['--context', str(parent.path), *args])
    assert observed == [(str(child.path), 'codex', caller['cwd'], {
        'detached':detached, 'expected_intent_digest':intent['intent_digest'],
        'launch_kind':'startup', 'provider_session_id':None})]
    assert not child.outputs['launch'].exists()


def test_bad_full_request_has_no_RPC_or_parent_child_writes(prepared_caller, monkeypatch):
    parent, child, caller, intent = prepared_caller
    snapshots = [snapshot_tree(value.client_root) for value in (parent, child)]
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('rejected caller reached authenticated RPC')
    monkeypatch.setattr(api['RuntimeClient'], 'rpc', forbidden)
    with pytest.raises(api['ClientError'], match='^LOCAL_MODEL_INTENT_CHANGED$'):
        api['entry_operation']('spawn-child', ['--context', str(parent.path),
            *command({**caller, 'name':'OtherChild'})])
    assert calls == []
    for value, before in zip((parent, child), snapshots):
        assert_tree_unchanged(value.client_root, before)


@pytest.mark.parametrize('entry', ['preregister-child', 'resume-child', 'reregister'])
def test_other_shared_parser_entries_cannot_silently_ignore_spawn_only_fields(prepared_caller, monkeypatch, entry):
    parent, child, caller, intent = prepared_caller
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('wrong-entry field reached RPC')
    monkeypatch.setattr(api['RuntimeClient'], 'rpc', forbidden)
    before = snapshot_tree(parent.client_root)
    with pytest.raises(api['ClientError'], match='^ARGUMENTS_UNSUPPORTED$'):
        api['entry_operation'](entry, ['--context', str(parent.path), '--resources', caller['resources'][0]])
    assert calls == []
    assert_tree_unchanged(parent.client_root, before)


def test_full_shell_rechecks_initial_digest_inside_existing_child_C(prepared_caller, monkeypatch):
    parent, child, caller, intent = prepared_caller
    import runtime_local_launch
    from local_model_auth import intent_digest
    original = runtime_local_launch.admit_caller_request
    calls = []
    changed = {**intent, 'revision':2}
    changed['intent_digest'] = intent_digest(changed)
    def admit(target, request, **kwargs):
        held = getattr(api['RuntimeClient'].operation_mutex.__wrapped__.__globals__['_OPERATION_HELD'], 'paths', set())
        calls.append((kwargs.get('expected_digest'), str(target.outputs['operation_lock']) in held))
        result = original(target, request, **kwargs)
        if len(calls) == 1:
            with child.operation_mutex(), child.fence():
                save_launch_intent(child.profile_path, changed, mode='global', locked=True,
                    writer=lambda path, value: child.write_output(path, value, 'profile'))
        return result
    monkeypatch.setattr(runtime_local_launch, 'admit_caller_request', admit)
    with pytest.raises(api['ClientError'], match='^LOCAL_MODEL_INTENT_CHANGED$'):
        api['entry_operation']('spawn-child', ['--context', str(parent.path), *command(caller)])
    assert calls == [(None, False), (intent['intent_digest'], True)]
    assert api['read_json'](child.profile_path)['launch_intent'] == changed
    assert not child.outputs['task'].exists() and not child.outputs['launch'].exists()


def test_full_shell_rejects_changed_task_before_publication(prepared_caller):
    parent, child, caller, intent = prepared_caller
    Path(caller['task']).write_text('Changed after explicit intent approval.\n')
    before = snapshot_tree(child.client_root)
    result = shell(parent, 'hooks/spawn_child.sh', command(caller))
    assert result.returncode == 2 and result.stderr.rstrip().endswith('LOCAL_MODEL_TASK_CHANGED'), result.stderr
    assert_tree_unchanged(child.client_root, before)


def test_task_cannot_change_between_admission_and_actual_publication(prepared_caller, monkeypatch):
    parent, child, caller, intent = prepared_caller
    original = api['RuntimeClient']._prepare_child_launch
    called = []
    def change_then_publish(self, *args, **kwargs):
        called.append(True)
        Path(caller['task']).write_text('Changed at the real publication boundary.\n')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(api['RuntimeClient'], '_prepare_child_launch', change_then_publish)
    before = snapshot_tree(child.client_root)
    with pytest.raises(api['ClientError'], match='^LOCAL_MODEL_TASK_CHANGED$'):
        api['entry_operation']('spawn-child', ['--context', str(parent.path), *command(caller)])
    assert called == [True]
    assert_tree_unchanged(child.client_root, before)


@pytest.mark.parametrize('field', ['working_directory', 'tools_selection'])
def test_full_shell_rejects_child_compatibility_projection_drift(prepared_caller, field):
    parent, child, caller, intent = prepared_caller
    state = api['read_json'](child.outputs['child'])
    state[field] = (str(parent.isolation) if field == 'working_directory'
                    else {'base':'default', 'tools':{}})
    with child.operation_mutex(), child.fence():
        child.write_output(child.outputs['child'], state, 'child')
    before = snapshot_tree(child.client_root)
    result = shell(parent, 'hooks/spawn_child.sh', command(caller))
    reason = 'LOCAL_MODEL_IDENTITY_MISMATCH' if field == 'working_directory' else 'LOCAL_MODEL_INTENT_CHANGED'
    assert result.returncode == 2 and result.stderr.rstrip().endswith(reason), result.stderr
    assert_tree_unchanged(child.client_root, before)


def invoke_task_boundary(prepared_caller, shape):
    parent, child, caller, intent = prepared_caller
    if shape == 'helper':
        # Exercise the shared task writer directly as well as admission: a
        # permissive admission must not hide a later operator/prepare regression.
        return child._prepare_child_launch(caller['task'])
    args = (command(caller) if shape == 'full' else
            ['--child-client-name','child_01','--prepare-only','--task-file',caller['task']])
    return api['entry_operation']('spawn-child', ['--context',str(parent.path),*args])


@pytest.mark.parametrize('mode', [0o600, 0o644], ids=['0600','0644'])
@pytest.mark.parametrize('shape', ['full','minimal','helper'])
def test_source_task_keeps_canonical_read_contract(prepared_caller, shape, mode):
    parent, child, caller, intent = prepared_caller
    source = Path(caller['task'])
    source.chmod(mode)
    from local_model_auth import secure_read
    assert secure_read(source, os.getuid()) == source.read_bytes()
    before = child.profile_path.read_bytes()
    invoke_task_boundary(prepared_caller, shape)
    assert child.outputs['task'].read_bytes() == source.read_bytes()
    assert source.stat().st_mode & 0o777 == mode, 'reader changed caller source permissions'
    assert child.outputs['task'].stat().st_mode & 0o077 == 0
    assert child.profile_path.read_bytes() == before
    assert not child.outputs['launch'].exists()


@pytest.mark.parametrize('mutation', ['group-writable','world-writable','symlink','changed'])
@pytest.mark.parametrize('shape', ['full','minimal','helper'])
def test_source_task_still_rejects_unsafe_or_changed_bytes(prepared_caller, shape, mutation):
    parent, child, caller, intent = prepared_caller
    source = Path(caller['task'])
    if mutation == 'group-writable':source.chmod(0o660)
    elif mutation == 'world-writable':source.chmod(0o602)
    elif mutation == 'symlink':
        target = source.with_name('same-task-bytes.md')
        source.rename(target)
        source.symlink_to(target)
    else:source.write_text('Changed approved source bytes.\n')
    before = snapshot_tree(child.client_root)
    with pytest.raises(api['ClientError'], match='^LOCAL_MODEL_TASK_CHANGED$'):
        invoke_task_boundary(prepared_caller, shape)
    assert_tree_unchanged(child.client_root, before)
    assert not child.outputs['task'].exists() and not child.outputs['launch'].exists()


@pytest.mark.parametrize('delivery', ['mail','embedded'])
@pytest.mark.parametrize('mutation', ['missing-source','changed-source','changed-retained',
                                    'unsafe-retained','symlink-retained'])
def test_minimal_omitted_source_checks_only_retained_approved_task(prepared_caller, delivery, mutation):
    parent, child, caller, intent = prepared_caller
    if delivery == 'embedded':
        from local_model_auth import intent_digest
        intent = {**intent, 'task_delivery':'embedded', 'revision':2}
        intent['intent_digest'] = intent_digest(intent)
        with child.operation_mutex(), child.fence():
            save_launch_intent(child.profile_path, intent, mode='global', locked=True,
                writer=lambda path,value:child.write_output(path,value,'profile'))
    child._prepare_child_launch(caller['task'], embed_task=delivery=='embedded')
    retained = child.outputs['task']
    approved_bytes = retained.read_bytes()
    source = Path(caller['task'])
    if mutation == 'changed-source':source.write_text('Original source no longer matches.\n')
    else:source.unlink()
    if mutation == 'changed-retained':retained.write_text('Different retained bytes.\n')
    elif mutation == 'unsafe-retained':retained.chmod(0o660)
    elif mutation == 'symlink-retained':
        target = source.with_name('same-retained-bytes.md')
        target.write_bytes(approved_bytes)
        target.chmod(0o600)
        retained.unlink()
        retained.symlink_to(target)
    before = snapshot_tree(child.client_root)
    def invoke():
        return api['entry_operation']('spawn-child', ['--context',str(parent.path),
            '--child-client-name','child_01','--prepare-only'])
    if mutation in ('missing-source','changed-source'):
        assert invoke() == 0
        assert retained.read_bytes() == approved_bytes
    else:
        # Existing fixed-output guards reject unsafe copies before model
        # admission; do not weaken private state to match the source contract.
        reason = 'LOCAL_MODEL_TASK_CHANGED' if mutation=='changed-retained' else 'OUTPUT_FILE_UNSAFE'
        with pytest.raises(api['ClientError'], match='^'+reason+'$'):invoke()
    assert_tree_unchanged(child.client_root, before)
    assert api['read_json'](child.profile_path)['launch_intent'] == intent
    assert api['read_json'](child.outputs['child'])['task_delivery'] == delivery
    assert not child.outputs['launch'].exists()


@pytest.mark.parametrize('mutation', ['missing-source','changed-source'])
def test_explicit_source_cannot_borrow_an_earlier_retained_copy(prepared_caller, mutation):
    parent, child, caller, intent = prepared_caller
    child._prepare_child_launch(caller['task'])
    source = Path(caller['task'])
    if mutation == 'missing-source':source.unlink()
    else:source.write_text('Explicitly requested source changed.\n')
    before = snapshot_tree(child.client_root)
    with pytest.raises(api['ClientError'], match='^LOCAL_MODEL_TASK_CHANGED$'):
        api['entry_operation']('spawn-child', ['--context',str(parent.path),
            '--child-client-name','child_01','--prepare-only','--task-file',caller['task']])
    assert_tree_unchanged(child.client_root, before)
