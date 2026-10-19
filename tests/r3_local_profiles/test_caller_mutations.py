"""Actual front-door mutations; not synthetic successful caller traces."""
import hashlib
import json

import pytest

from .caller_binding import assert_single_replacement
from .caller_mutations import emit_previous_capture
from .evidence import (CALLERS, prepare_caller_invocation, read_fresh_capture,
                       assert_runtime_capture, caller_client_root, snapshot_tree,
                       assert_tree_unchanged)


@pytest.mark.parametrize('entry', ['prepare-child','resume-child','operator','shell','dashboard'])
@pytest.mark.parametrize('mutation', ['no-op','stale-capture'])
def test_actual_connected_entry_cannot_reuse_previous_success(caller_probe,workspace,entry,mutation):
    owner, binary, previous, previous_nonce = prepare_caller_invocation(workspace,'prepare-child')
    previous_hash = hashlib.sha256(previous.with_suffix('.py').read_bytes()).hexdigest()
    caller_probe('prepare-child',workspace=workspace,real_entry=CALLERS['prepare-child'],
        provider_binary=binary,owner=owner,prepare_only=False,
        provider_session_id='native-fixture-session',task_delivery='mail',headless=False)
    successful = read_fresh_capture(previous,previous_nonce)
    assert_runtime_capture(successful,owner.parent/'runtime/launch.json',capture=previous,
        binary=binary,expected_script_sha256=previous_hash)
    previous_bytes = previous.read_bytes()
    approved_owner = owner.read_bytes()
    owner, binary, current, nonce = prepare_caller_invocation(workspace,entry,
        resume_owner=approved_owner if entry=='resume-child' else None)
    assert current != previous and nonce != previous_nonce and previous.is_file()
    driver_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    kwargs = dict(workspace=workspace,real_entry=CALLERS[entry],provider_binary=binary,
        owner=owner,prepare_only=(entry=='operator'),provider_session_id='native-fixture-session',
        task_delivery='mail',headless=False)
    if mutation == 'no-op':
        before = []
        kwargs.update(real_entry_mutation='no-op',
                      on_before_real_entry=lambda:before.append(snapshot_tree(caller_client_root(workspace))))
        reason = ('R3_REAL_SHELL_TASK_PREPARATION_MISSING' if entry=='shell' else
                  'R3_REAL_DASHBOARD_TASK_PREPARATION_MISSING' if entry=='dashboard' else
                  'R3_REAL_CHILD_PREPARATION_PHASE_MISMATCH' if entry=='resume-child'
                  else 'R3_REAL_CHILD_PREPARATION_MISSING')
        with pytest.raises(RuntimeError,match=reason) as refused:
            caller_probe(entry,**kwargs)
        assert str(refused.value).startswith('R3_REAL_CALLER_REJECTED: '+entry+':')
        records = [json.loads(line) for line in
                   (workspace.tmp/('mutation-'+entry+'.jsonl')).read_text().splitlines()]
        # Independent literal oracle, not the mutator's own target table.
        target = {'prepare-child':'RuntimeClient.prepare_child',
                  'resume-child':'RuntimeClient.resume_child',
                  'operator':'RuntimeOperator.spawn',
                  'shell':'runtime_client.entry_operation',
                  'dashboard':'dashboard.server.do_spawn'}[entry]
        expected_file = str(caller_probe.install/'dashboard/server.py') if entry=='dashboard' else str(
            caller_probe.install/'bin/lib'/('runtime_operator.py' if entry=='operator' else 'runtime_client.py'))
        assert [row['event'] for row in records] == ['installed','activated']
        assert all(row['target']==target and row['method_file']==expected_file for row in records)
        pids = {row['pid'] for row in caller_probe.last_entry_rows if row['event']=='bootstrap'}
        assert len({row['pid'] for row in records})==1 and records[0]['pid'] in pids
        if entry=='shell':
            assert all(row['method_name']=='entry_operation' and row['module']=='__main__'
                       and row['entry']=='spawn-child' for row in records)
            root_changes = [row for row in caller_probe.last_entry_rows if row['event']=='shell-entry-mutation']
            assert len(root_changes)==1
            assert root_changes[0]['pid']==records[0]['pid']
            assert root_changes[0]['replacements']==['runtime_client.entry_operation']
            assert root_changes[0]['replacement_count']==1
        if entry=='dashboard':
            assert all(row['method_name']=='do_spawn' and row['module']=='dashboard.server'
                       and row['replacements']==['dashboard.server.do_spawn']
                       and row['replacement_count']==1 for row in records)
            assert any(row['event']=='real-call' and row['pid']==records[0]['pid']
                       and row['file']==str(caller_probe.install/'dashboard/server.py')
                       and row['name']=='do_POST' for row in caller_probe.last_entry_rows)
        assert_single_replacement(caller_probe.last_entry_rows)
        assert len(before)==1
        assert_tree_unchanged(caller_client_root(workspace),before[0])
        assert not current.exists() and not current.with_suffix('.driver.json').exists()
    else:
        expected_script_hash = emit_previous_capture(binary,current,previous)
        caller_probe(entry,**kwargs)  # actual entry/R2 must succeed, not early-reject
        assert current.read_bytes() == previous_bytes
        with pytest.raises(AssertionError,match='capture belongs to an earlier caller invocation'):
            read_fresh_capture(current,nonce)
        assert hashlib.sha256(current.with_suffix('.py').read_bytes()).hexdigest() == expected_script_hash
        witness = json.loads(current.with_suffix('.driver.json').read_text())
        assert witness == {'pid':int(current.with_suffix('.stale-pid').read_text()),
            'driver':str(binary),'driver_sha256':driver_hash,
            'script':str(current.with_suffix('.py')),'script_sha256':expected_script_hash}
        assert_single_replacement(caller_probe.last_entry_rows)
    assert previous.read_bytes() == previous_bytes, 'mutation changed the successful seed'
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == driver_hash
