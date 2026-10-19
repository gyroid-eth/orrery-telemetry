"""Ordinary preview edits through real shared readers/core HTTP, fake peer/OS."""
import io
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

import pytest

from test_runtime_legacy_auth_binding import bound, configure_fixture, context_fixture, observation
from test_runtime_legacy_session_hooks import cases as sessions, record
from local_model_auth import LocalModelError
from runtime_intent_store import read_owner, write_owner


@pytest.fixture
def cases(sessions, monkeypatch):
    for case in (sessions, sessions.fresh):
        assert record(case)['ok']
        case.guard = case.api.get('guard_preview_edit')
        assert callable(case.guard), 'PREVIEW_EDIT_GUARD_MISSING'
        case.wire = []; case.renewed = 1; case.bad_resource = False; case.transport_failed = False
        case.rows = [lease('FixtureChild'), lease('AnotherAgent', 'different/**')]
        case.payload = {'session_id': 'fixture-session', 'cwd': case.bound.intent['cwd'],
            'hook_event_name': 'PreToolUse', 'tool_name': 'Write',
            'tool_input': {'file_path': str(Path(case.bound.intent['legacy_context']['project_key']) / 'note.md')}}
    current = {'case': sessions}
    class Opener:
        def __init__(self, handlers): self.handlers = handlers
        def open(self, request, timeout):
            assert len(self.handlers) == 2 and isinstance(self.handlers[0], urllib.request.ProxyHandler)
            assert self.handlers[0].proxies == {} and self.handlers[1].__class__.__name__ == '_NoRedirect'
            case = current['case']; target = case.bound.intent['legacy_context']
            assert request.full_url == target['mail_url'] and request.get_header('Authorization') is None
            value = json.loads(request.data); case.wire.append(value)
            if case.transport_failed: raise OSError('fixture transport failure')
            if value['method'] == 'tools/list':
                result = {'tools': [{'name': name, 'inputSchema': {'properties': {'registration_token': {}}}}
                    for name in ('renew_file_reservations', 'release_file_reservations')]}
            elif value['method'] == 'resources/read':
                assert value['params'] == {'uri': 'resource://file_reservations/' +
                    urllib.parse.quote(target['project_key'], safe='') + '?active_only=true&format=json'}
                result = {'contents': [] if case.bad_resource else [{'text': json.dumps(case.rows)}]}
            else:
                supplied = value['params']['arguments']
                assert supplied['project_key'] == target['project_key'] and supplied['agent_name'] == target['name']
                if value['params']['name'] == 'renew_file_reservations': result = {'structuredContent': {'renewed': case.renewed}}
                else:
                    assert value['params']['name'] == 'release_file_reservations'
                    case.rows[:] = [row for row in case.rows if row['agent'] != target['name']]
                    result = {'structuredContent': {'released': 1}}
            return io.BytesIO(json.dumps({'result': result}).encode())
    def build(*handlers):
        return Opener(handlers)
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    sessions.select = current
    sessions.fresh.select = current
    return sessions


def lease(agent, pattern='note.md'):
    return {'agent': agent, 'path_pattern': pattern, 'exclusive': True,
        'expires_ts': '2040-01-01T00:00:00Z', 'released_ts': None}


def guard(case, operation='pre-edit', payload=None, mutation=None):
    case.select['case'] = case
    observation(case.bound, case.patch, mutation)
    if payload is None:
        payload = {**case.payload, 'hook_event_name': 'PreToolUse' if operation == 'pre-edit' else 'PostToolUse'}
    return case.guard(case.bound.intent, operation, payload)


def test_renew_uses_same_owner_both_paths_and_ordinary_900_seconds(cases):
    assert guard(cases) == {'ok': True, 'protected': True, 'renewed': 1}
    call = next(row for row in cases.wire if row['method'] == 'tools/call')
    args = call['params']['arguments']; target = cases.bound.intent['legacy_context']
    assert args['paths'] == ['note.md', target['project_key'] + '/note.md']
    assert args['extend_seconds'] == 900
    assert cases.bound.store.token.read_text() == args['registration_token']
    assert len([row for row in cases.wire if row['method'] == 'resources/read']) == 1


@pytest.mark.parametrize('failure', ['foreign-exclusive', 'zero-renew', 'bool-renew',
    'missing-count', 'bad-resource', 'transport'])
def test_ordinary_lease_or_service_failures_refuse_not_success(cases, failure):
    assert guard(cases)['ok']
    case = cases.fresh
    if failure == 'foreign-exclusive': case.rows.append(lease('AnotherAgent'))
    elif failure == 'zero-renew': case.renewed = 0
    elif failure == 'bool-renew': case.renewed = True
    elif failure == 'missing-count': case.renewed = None
    elif failure == 'bad-resource': case.bad_resource = True
    else: case.transport_failed = True
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_RESERVATION_FAILED$'): guard(case)
    assert not any(row.get('params', {}).get('name') == 'release_file_reservations' for row in case.wire)


@pytest.mark.parametrize('failure', ['session', 'cwd', 'no-path', 'path-type', 'ended'])
def test_invalid_or_ended_hook_context_refuses_before_http(cases, failure):
    assert guard(cases)['ok']
    case = cases.fresh; payload = {**case.payload, 'tool_input': dict(case.payload['tool_input'])}
    if failure == 'session': payload['session_id'] = 'another-session'
    elif failure == 'cwd': payload['cwd'] = '/other'
    elif failure == 'no-path': payload['tool_input'] = {}
    elif failure == 'path-type': payload['tool_input']['file_path'] = False
    else:
        frame = read_owner(case.bound.store.owner)
        frame['local_execution']['session_receipt']['phase'] = 'ended'
        write_owner(case.bound.store.owner, frame)
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_RESERVATION_FAILED$'): guard(case, payload=payload)
    assert case.wire == []


@pytest.mark.parametrize('failure', [False, True])
def test_post_edit_does_not_automatically_release_even_after_failed_edit(cases, failure):
    payload = {**cases.payload, 'hook_event_name': 'PostToolUse', 'success': not failure}
    assert guard(cases, 'post-edit', payload) == {'ok': True, 'released': 0}
    assert cases.wire == [] and len(cases.rows) == 2


def test_mail_only_proxy_can_explicitly_release_own_leases_but_not_another_owner(cases):
    assert guard(cases)['ok']
    cases.select['case'] = cases
    observation(cases.bound, cases.patch)
    assert cases.bound.intent['tools'] == {}
    server = cases.api['configured_preview_proxy'](cases.bound.intent)
    result = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'release_reservations', 'arguments': {}}})['result']
    assert result['isError'] is False
    assert cases.rows == [lease('AnotherAgent', 'different/**')]


def test_payload_metadata_never_changes_endpoint_project_or_owner(cases):
    assert guard(cases)['ok']
    case = cases.fresh
    payload = {**case.payload, 'project_key': '/other', 'mail_url': 'http://elsewhere.invalid',
        'owner': str(cases.bound.store.owner), 'agent_name': 'OtherChild', 'registration_token': 'not-an-authority'}
    assert guard(case, payload=payload)['ok']
    assert 'not-an-authority' not in json.dumps(case.wire)


def test_relative_path_is_anchored_to_saved_cwd_not_the_hook_process(cases):
    payload = {**cases.payload, 'tool_input': {'file_path': os.path.relpath(
        cases.payload['tool_input']['file_path'], cases.bound.intent['cwd'])}}
    assert guard(cases, payload=payload) == {'ok': True, 'protected': True, 'renewed': 1}
    call = next(row for row in cases.wire if row['method'] == 'tools/call')
    assert call['params']['arguments']['paths'][0] == 'note.md'


def test_known_path_outside_protected_project_needs_no_mail_reservation(cases):
    assert guard(cases)['ok']
    case = cases.fresh
    payload = {**case.payload, 'tool_input': {'file_path': 'outside-project.md'}}
    assert guard(case, payload=payload) == {'ok': True, 'protected': False, 'renewed': 0}
    assert case.wire == []
