"""Shared active-owner reader, not process/native/proxy admission."""
from copy import deepcopy

import pytest

from test_runtime_legacy_auth_binding import bound, configure_fixture, context_fixture
from runtime_intent_store import read_owner, write_owner, write_private_bytes
import runtime_legacy_execution as execution
from local_model_auth import LocalModelError


def read(bound):
    reader = getattr(execution, 'active_execution_snapshot', None)
    assert callable(reader), 'SHARED_ACTIVE_EXECUTION_READER_MISSING'
    return reader(bound.store.owner, bound.intent)


@pytest.mark.parametrize('phase', ['exec-unknown', 'running'])
def test_shared_reader_uses_only_the_same_registered_owner_without_effects(bound, phase):
    frame = read_owner(bound.store.owner)
    if phase == 'running':
        frame['local_execution']['phase'] = phase
        frame['local_execution']['session_receipt'] = {'session_id': 'fixture-session', 'phase': 'started'}
        write_owner(bound.store.owner, frame)
    before = (bound.store.owner.read_bytes(), bound.store.token.read_bytes())
    value = read(bound)
    assert value['owner'] == frame and value['row'] == frame['local_execution']
    assert (bound.store.owner.read_bytes(), bound.store.token.read_bytes()) == before
    # Private data remains private: this is NOT a status result or tool output.
    assert value['owner']['registration_token'] == before[1].decode()
    assert not bound.ws.tmp.joinpath('key-executed').exists()


@pytest.mark.parametrize('mutation', ['missing-registration', 'token-pair', 'intent', 'wrong-launch',
    'wrong-nonce', 'wrong-pid', 'unknown-outcome', 'planned', 'gated', 'ended', 'cancelled', 'resolution'])
def test_shared_reader_never_accepts_inactive_or_uncertain_state(bound, monkeypatch, mutation):
    frame = read_owner(bound.store.owner)
    row = frame['local_execution']
    if mutation == 'missing-registration': del frame['local_registration']
    elif mutation == 'token-pair': write_private_bytes(bound.store.token, b'other-owner-000000000000000000000000')
    elif mutation == 'intent':
        # A different caller expectation must not select this owner's state.
        bound.intent = deepcopy(bound.intent)
        bound.intent['revision'] += 1
    elif mutation == 'wrong-launch': monkeypatch.setenv('AGENTSTACK_LAUNCH_ID', 'c' * 32)
    elif mutation == 'wrong-nonce': monkeypatch.setenv('AGENTSTACK_LAUNCH_NONCE', 'c' * 43)
    elif mutation == 'wrong-pid': monkeypatch.setenv('AGENTSTACK_PROVIDER_PID', '99')
    elif mutation == 'unknown-outcome': row['execution']['outcome'] = {'state': 'unknown', 'returncode': None}
    elif mutation in ('planned', 'gated', 'cancelled'):
        fresh = execution.new_execution(bound.intent, launch_id=row['launch_id'], receipt_nonce=row['receipt_nonce'])
        if mutation == 'gated':
            fresh['phase'] = 'gated'; fresh['execution']['runner'] = row['execution']['runner']
        elif mutation == 'cancelled':
            fresh['phase'] = 'cancelled'; fresh['execution']['outcome'] = {'state': 'ended', 'returncode': None}
        frame['local_execution'] = fresh
    elif mutation == 'ended':
        row['phase'] = 'ended'; row['execution']['outcome'] = {'state': 'ended', 'returncode': 0}
    else:
        frame['local_resolution'] = {'phase': 'abandoned', 'previous_state_digest': 'a' * 64,
            'remote_effect': 'unknown', 'generation': None, 'launch_id': row['launch_id']}
    write_owner(bound.store.owner, frame)
    before = (bound.store.owner.read_bytes(), bound.store.token.read_bytes())
    with pytest.raises(LocalModelError): read(bound)
    assert (bound.store.owner.read_bytes(), bound.store.token.read_bytes()) == before


def test_shared_reader_cannot_use_a_copied_different_owner_path(bound):
    other = bound.store.owner.with_name('OtherChild.json')
    write_owner(other, read_owner(bound.store.owner))
    reader = getattr(execution, 'active_execution_snapshot', None)
    assert callable(reader), 'SHARED_ACTIVE_EXECUTION_READER_MISSING'
    with pytest.raises(LocalModelError): reader(other, bound.intent)


def test_shared_reader_rechecks_each_call_instead_of_caching_a_previous_success(bound):
    assert read(bound) == read(bound)
    frame = read_owner(bound.store.owner)
    frame['local_execution']['phase'] = 'ended'
    frame['local_execution']['execution']['outcome'] = {'state': 'ended', 'returncode': 0}
    write_owner(bound.store.owner, frame)
    with pytest.raises(LocalModelError): read(bound)


def test_active_reader_does_not_require_an_auth_runner_for_a_keyless_profile(configure_fixture, monkeypatch):
    from test_runtime_legacy_configure import plan
    from runtime_legacy_registration import Store
    client, ws, _, configure, _, _ = configure_fixture
    value = plan(configure_fixture)
    configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    intent = value['intent']; target = intent['legacy_context']
    assert intent['model']['resolved']['auth'] is None
    store = Store(target['runtime_dir'], target['name'])
    token = 'fixture-keyless-owner-0000000000000000000000'
    identity = {'id': 73, 'project_id': target['project_id'], 'name': target['name'],
        'program': 'claude-code', 'model': intent['model']['resolved']['model']}
    store.begin(expected_digest=store.status()['state_digest'], candidate_token=token)
    store.send_once(lambda *args: {**identity, 'registration_token': token},
        expected_digest=store.status()['state_digest'])
    store.finalize(lambda token: identity, expected_digest=store.status()['state_digest'])
    row = execution.new_execution(intent, launch_id='a' * 32, receipt_nonce='b' * 43)
    row['phase'] = 'exec-unknown'
    row['execution']['runner'] = {'pid': 11, 'process_start': 'fixture-runner'}
    row['execution']['provider'] = {'pid': 12, 'process_start': 'fixture-provider'}
    frame = read_owner(store.owner); frame['local_execution'] = row
    write_owner(store.owner, frame)
    for key, val in {'AGENTSTACK_LAUNCH_ID': row['launch_id'],
            'AGENTSTACK_LAUNCH_NONCE': row['receipt_nonce'], 'AGENTSTACK_PROVIDER_PID': '12'}.items():
        monkeypatch.setenv(key, val)
    before = (store.owner.read_bytes(), store.token.read_bytes())
    observed = execution.active_execution_snapshot(store.owner, intent)
    assert observed['row'] == row and observed['owner'] == frame
    assert (store.owner.read_bytes(), store.token.read_bytes()) == before
