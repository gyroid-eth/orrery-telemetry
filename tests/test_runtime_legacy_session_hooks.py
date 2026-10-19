"""Session receipts in the real owner; fake OS only, never remote cleanup."""
import fcntl
from pathlib import Path
import runpy
import socket
import subprocess
from types import SimpleNamespace
import urllib.request

import pytest

from test_runtime_legacy_auth_binding import bound, configure_fixture, context_fixture, observation
from runtime_intent_store import read_owner, write_owner
from local_model_auth import LocalModelError


@pytest.fixture
def cases(bound, monkeypatch, tmp_path):
    root = tmp_path / 'independent-negative'; root.mkdir(mode=0o700)
    ws = context_fixture.__wrapped__(root)
    configured = configure_fixture.__wrapped__(ws)
    second = globals()['bound'].__wrapped__(configured, monkeypatch)
    def build(item):
        script = Path(item.intent['legacy_context']['install_root']) / 'bin/lib/runtime_legacy_proxy.py'
        api = runpy.run_path(str(script))
        assert callable(api.get('record_preview_session')), 'PREVIEW_SESSION_RECEIPT_CONSUMER_MISSING'
        return SimpleNamespace(bound=item, api=api, patch=monkeypatch)
    first = build(bound); first.fresh = build(second)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a, **k: pytest.fail('session receipt sent HTTP'))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('real network attempted'))
    return first


def record(case, operation='session-start', payload=None, mutation=None):
    observation(case.bound, case.patch, mutation)
    if payload is None:
        payload = {'session_id': 'fixture-session', 'cwd': case.bound.intent['cwd'],
            'hook_event_name': 'SessionStart' if operation == 'session-start' else 'SessionEnd'}
    return case.api['record_preview_session'](case.bound.intent, operation, payload)


def test_session_start_and_end_are_idempotent_receipts_not_wait_or_cleanup(cases):
    assert record(cases) == {'ok': True, 'recorded': True}
    before = cases.bound.store.owner.read_bytes()
    assert record(cases) == {'ok': True, 'recorded': True}
    assert cases.bound.store.owner.read_bytes() == before
    row = read_owner(cases.bound.store.owner)['local_execution']
    assert row['phase'] == 'running' and row['session_receipt'] == {'session_id': 'fixture-session', 'phase': 'started'}
    assert record(cases, 'end-session') == {'ok': True, 'recorded': True}
    before = cases.bound.store.owner.read_bytes()
    assert record(cases, 'end-session') == {'ok': True, 'recorded': True}
    assert cases.bound.store.owner.read_bytes() == before
    row = read_owner(cases.bound.store.owner)['local_execution']
    assert row['phase'] == 'running' and row['session_receipt'] == {'session_id': 'fixture-session', 'phase': 'ended'}
    assert row['execution']['outcome'] == {'state': 'pending', 'returncode': None}
    assert row['termination'] == {'phase': 'pending', 'lease_release': 'pending', 'retire': 'pending'}


@pytest.mark.parametrize('mutation', ['session-missing', 'session-bool', 'session-foreign-format',
    'session-too-long', 'cwd-missing', 'cwd-foreign', 'event-foreign', 'payload-list'])
def test_invalid_session_payload_never_selects_or_mutates_an_owner(cases, mutation):
    assert record(cases)['ok']
    case = cases.fresh; before = case.bound.store.owner.read_bytes()
    payload = {'session_id': 'fixture-session', 'cwd': case.bound.intent['cwd'], 'hook_event_name': 'SessionStart'}
    if mutation == 'session-missing': payload.pop('session_id')
    elif mutation == 'session-bool': payload['session_id'] = True
    elif mutation == 'session-foreign-format': payload['session_id'] = '../another-owner'
    elif mutation == 'session-too-long': payload['session_id'] = 'a' * 129
    elif mutation == 'cwd-missing': payload.pop('cwd')
    elif mutation == 'cwd-foreign': payload['cwd'] = '/foreign'
    elif mutation == 'event-foreign': payload['hook_event_name'] = 'SessionEnd'
    else: payload = []
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_SESSION_FAILED$'): record(case, payload=payload)
    assert case.bound.store.owner.read_bytes() == before


@pytest.mark.parametrize('mutation', ['end-before-start', 'different-session', 'restart-after-end'])
def test_session_transition_cannot_rescue_or_rebind_an_execution(cases, mutation):
    assert record(cases)['ok'] and record(cases, 'end-session')['ok']
    case = cases.fresh
    if mutation != 'end-before-start': assert record(case)['ok']
    if mutation == 'restart-after-end': assert record(case, 'end-session')['ok']
    before = case.bound.store.owner.read_bytes()
    operation = 'end-session' if mutation == 'end-before-start' else 'session-start'
    payload = {'session_id': 'foreign-session' if mutation == 'different-session' else 'fixture-session',
        'cwd': case.bound.intent['cwd']}
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_SESSION_FAILED$'): record(case, operation, payload)
    assert case.bound.store.owner.read_bytes() == before


@pytest.mark.parametrize('mutation', ['unknown', 'other-uid', 'other-runner', 'reused-provider',
    'reparent', 'row-change', 'owner-change', 'token-change', 'profile-change', 'task-change'])
def test_shared_active_proof_is_required_before_session_receipt(cases, mutation):
    assert record(cases)['ok']
    case = cases.fresh
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_SESSION_FAILED$'): record(case, mutation=mutation)
    row = read_owner(case.bound.store.owner)['local_execution']
    assert row['session_receipt'] is None and row['phase'] == 'exec-unknown'
    assert row['termination'] == {'phase': 'pending', 'lease_release': 'pending', 'retire': 'pending'}


@pytest.mark.parametrize('mutation', ['unknown-process', 'changed-owner'])
def test_failed_post_save_proof_is_not_rescued_or_rolled_back(cases, mutation):
    assert record(cases)['ok']
    case = cases.fresh; observation(case.bound, case.patch); observed = subprocess.run
    def after_save(argv, **kwargs):
        frame = read_owner(case.bound.store.owner)
        if frame['local_execution']['phase'] == 'running':
            if mutation == 'changed-owner':
                frame['agent_id'] = 74; write_owner(case.bound.store.owner, frame)
            elif argv[:3] == ['ps', '-p', '12']:
                return subprocess.CompletedProcess(argv, 1, '', 'fixture observation unavailable')
        return observed(argv, **kwargs)
    case.patch.setattr(subprocess, 'run', after_save)
    payload = {'session_id': 'fixture-session', 'cwd': case.bound.intent['cwd']}
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_SESSION_FAILED$'):
        case.api['record_preview_session'](case.bound.intent, 'session-start', payload)
    row = read_owner(case.bound.store.owner)['local_execution']
    assert row['phase'] == 'running' and row['session_receipt']['phase'] == 'started'
    assert row['execution']['outcome'] == {'state': 'pending', 'returncode': None}
    assert row['termination'] == {'phase': 'pending', 'lease_release': 'pending', 'retire': 'pending'}


def test_real_owner_cas_rejects_state_replaced_at_the_write_lock(cases):
    assert record(cases)['ok']
    case = cases.fresh; lock = fcntl.flock; changed = []
    def before_write(fd, mode):
        result = lock(fd, mode)
        if mode == fcntl.LOCK_EX and not changed:
            frame = read_owner(case.bound.store.owner)
            frame['local_execution']['receipt_nonce'] = 'c' * 43
            write_owner(case.bound.store.owner, frame); changed.append(True)
        return result
    case.patch.setattr(fcntl, 'flock', before_write)
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_SESSION_FAILED$'): record(case)
    row = read_owner(case.bound.store.owner)['local_execution']
    assert changed == [True] and row['receipt_nonce'] == 'c' * 43
    assert row['session_receipt'] is None and row['phase'] == 'exec-unknown'


def test_hook_metadata_cannot_choose_another_owner_or_echo_credentials(cases):
    assert record(cases)['ok']
    case = cases.fresh; before = cases.bound.store.owner.read_bytes()
    payload = {'session_id': 'fixture-session', 'cwd': case.bound.intent['cwd'],
        'owner': str(cases.bound.store.owner), 'agent_id': 999, 'provider_pid': 99,
        'registration_token': 'fixture-not-an-authority', 'transcript_path': '/metadata/not-read'}
    assert record(case, payload=payload) == {'ok': True, 'recorded': True}
    assert cases.bound.store.owner.read_bytes() == before
    assert read_owner(case.bound.store.owner)['agent_id'] == 73
