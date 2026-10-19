"""Core stdio proxy/HTTP parser with fake HTTP and OS, not native acceptance."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import runpy
import socket
import sys
from types import SimpleNamespace
import urllib.request

import pytest

from test_runtime_legacy_auth_binding import bound, configure_fixture, context_fixture, observation, TOKEN
from runtime_intent_store import read_owner, write_owner, write_private_bytes
from local_model_auth import LocalModelError


@pytest.fixture
def use_auth(request): return getattr(request, 'param', True)


def keyless(configure_fixture, monkeypatch):
    from test_runtime_legacy_configure import plan
    from runtime_legacy_registration import Store
    from runtime_legacy_execution import new_execution
    client, ws, _, configure, _, _ = configure_fixture
    value = plan(configure_fixture); intent = value['intent']; target = intent['legacy_context']
    configure.apply_local_configuration(client, value, approved_id=value['plan_id'])
    assert intent['model']['resolved']['auth'] is None
    store = Store(target['runtime_dir'], target['name'])
    identity = {'id': 73, 'project_id': target['project_id'], 'name': target['name'],
        'program': 'claude-code', 'model': intent['model']['resolved']['model']}
    store.begin(expected_digest=store.status()['state_digest'], candidate_token=TOKEN)
    store.send_once(lambda *a: {**identity, 'registration_token': TOKEN}, expected_digest=store.status()['state_digest'])
    store.finalize(lambda token: identity, expected_digest=store.status()['state_digest'])
    row = new_execution(intent, launch_id='a' * 32, receipt_nonce='b' * 43)
    row['phase'] = 'exec-unknown'
    row['execution']['runner'] = {'pid': 11, 'process_start': 'Sat Oct 17 12:00:00 2026'}
    row['execution']['provider'] = {'pid': 12, 'process_start': 'Sat Oct 17 12:00:00 2026'}
    frame = read_owner(store.owner); frame['local_execution'] = row; write_owner(store.owner, frame)
    for key, value in {'PATH': '/usr/bin:/bin', 'AGENTSTACK_LAUNCH_ID': row['launch_id'],
            'AGENTSTACK_LAUNCH_NONCE': row['receipt_nonce'], 'AGENTSTACK_PROVIDER_PID': '12'}.items():
        monkeypatch.setenv(key, value)
    return SimpleNamespace(ws=ws, intent=intent, store=store, row=row)


@pytest.fixture
def prepared(request, use_auth, monkeypatch):
    item = (request.getfixturevalue('bound') if use_auth
            else keyless(request.getfixturevalue('configure_fixture'), monkeypatch))
    script = Path(item.intent['legacy_context']['install_root']) / 'bin/lib/runtime_legacy_proxy.py'
    assert script.is_file(), 'PINNED_PREVIEW_PROXY_CONSUMER_MISSING'
    api = runpy.run_path(str(script))
    assert callable(api.get('configured_preview_proxy')), 'PINNED_PREVIEW_PROXY_FACTORY_MISSING'
    calls = observation(item, monkeypatch)
    wire = []; builders = []
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == item.intent['legacy_context']['mail_url']
            assert request.get_header('Authorization') is None
            payload = json.loads(request.data); wire.append(payload)
            if payload['method'] == 'tools/list':
                names = ['fetch_inbox', 'file_reservation_paths', 'renew_file_reservations', 'release_file_reservations']
                return io.BytesIO(json.dumps({'result': {'tools': [
                    {'name': name, 'inputSchema': {'properties': {'registration_token': {}}}} for name in names]}}).encode())
            name = payload['params']['name']
            result = [] if name == 'fetch_inbox' else {'fixture_effect': name}
            return io.BytesIO(json.dumps({'result': {'structuredContent': result}}).encode())
    def build(*handlers): builders.append(handlers); return Opener()
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('real network attempted'))
    return SimpleNamespace(bound=item, api=api, calls=calls, wire=wire, builders=builders)


def make(prepared): return prepared.api['configured_preview_proxy'](prepared.bound.intent)


def call(server, name='runtime_status', **arguments):
    response = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments}})
    return response['result']


@pytest.mark.parametrize('use_auth', [True, False], indirect=True)
@pytest.mark.parametrize('tool,args,raw', [
    ('fetch_inbox', {}, 'fetch_inbox'),
    ('send_message', {'to': ['FixtureParent'], 'subject': 'Fixture', 'body_md': 'Fixture'}, 'send_message'),
    ('reserve_files', {'paths': ['scratch.md'], 'ttl_seconds': 600}, 'file_reservation_paths'),
    ('renew_reservations', {'extend_seconds': 600}, 'renew_file_reservations'),
    ('release_reservations', {}, 'release_file_reservations'),
])
def test_real_core_dispatch_supplies_only_the_bound_owner_and_uses_shared_transport(prepared, tool, args, raw):
    server = make(prepared)
    previous = len(prepared.calls)
    result = call(server, tool, **args)
    assert result['isError'] is False and len(prepared.calls) > previous
    request = prepared.wire[-1]
    assert request['params']['name'] == raw
    supplied = request['params']['arguments']; target = prepared.bound.intent['legacy_context']
    assert supplied['project_key'] == target['project_key']
    assert supplied['sender_name' if tool == 'send_message' else 'agent_name'] == target['name']
    assert supplied['sender_token' if tool == 'send_message' else 'registration_token'] == TOKEN
    assert TOKEN not in json.dumps(result)
    assert len(prepared.builders[-1]) == 2
    assert isinstance(prepared.builders[-1][0], urllib.request.ProxyHandler)
    assert prepared.builders[-1][0].proxies == {}
    assert prepared.builders[-1][1].__class__.__name__ == '_NoRedirect'
    assert server.__class__.__name__ == 'StdioMcpServer'
    assert server.proxy.__class__.__mro__[1].__name__ == 'AgentStackProxy'
    # The SAME actual dispatch must refuse every Mail/reservation tool after
    # its bound execution ends, not just one convenient proxy method.
    sent = len(prepared.wire)
    frame = read_owner(prepared.bound.store.owner)
    frame['local_execution']['phase'] = 'ended'
    frame['local_execution']['execution']['outcome'] = {'state': 'ended', 'returncode': 0}
    write_owner(prepared.bound.store.owner, frame)
    assert call(server, tool, **args)['isError'] is True
    assert len(prepared.wire) == sent


def test_direct_status_and_bootstrap_create_no_second_identity_or_snapshot_store(prepared):
    from test_runtime_configure_ownership import snapshot
    before = snapshot(prepared.bound.ws.tmp)
    server = make(prepared)
    status = call(server)
    assert status['isError'] is False
    assert status['structuredContent']['agent_name'] == 'FixtureChild'
    assert status['structuredContent']['lineage']['parent_agent'] == prepared.bound.intent['parent']
    assert call(server, 'bootstrap')['isError'] is False
    assert snapshot(prepared.bound.ws.tmp) == before and prepared.wire == []
    assert TOKEN not in json.dumps(status)


@pytest.mark.parametrize('mutation', ['unknown', 'absent', 'saved-provider', 'reused-provider', 'older-provider',
    'other-uid', 'other-runner', 'reparent', 'row-change', 'owner-change', 'token-change', 'profile-change', 'task-change'])
def test_uncertain_process_or_in_observation_drift_refuses_before_any_mail(prepared, monkeypatch, mutation):
    assert call(make(prepared))['isError'] is False
    observation(prepared.bound, monkeypatch, mutation)
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'): make(prepared)
    assert prepared.wire == []


@pytest.mark.parametrize('mutation', ['ended', 'owner', 'token', 'nonce', 'instructions', 'native-settings',
    'proxy-settings', 'proxy-source', 'process-source', 'profile', 'task'])
def test_every_call_rechecks_live_owner_and_resources_not_previous_success(prepared, mutation):
    server = make(prepared)
    assert call(server)['isError'] is False
    b = prepared.bound; frame = read_owner(b.store.owner); root = Path(b.intent['legacy_context']['install_root'])
    if mutation == 'ended':
        frame['local_execution']['phase'] = 'ended'
        frame['local_execution']['execution']['outcome'] = {'state': 'ended', 'returncode': 0}
    elif mutation == 'owner': frame['agent_id'] += 1
    elif mutation == 'nonce': frame['local_execution']['receipt_nonce'] = 'c' * 43
    elif mutation == 'token':
        frame['registration_token'] = 'rotated-fixture-owner-0000000000000000000'
        write_private_bytes(b.store.token, frame['registration_token'].encode())
    elif mutation in ('instructions', 'native-settings', 'proxy-settings'):
        filename = {'instructions': 'CLAUDE.md', 'native-settings': 'local-model-settings.json',
            'proxy-settings': 'local-model-mcp.json'}[mutation]
        (b.store.owner.with_suffix('.preview-home') / filename).write_text('changed output')
    elif mutation == 'proxy-source':
        (root / 'integrations/codex_app/src/agentstack_codex_app/mcp_server.py').write_text('changed source')
    elif mutation == 'process-source': (root / 'bin/lib/process_observation.py').write_text('changed source')
    elif mutation == 'profile': b.ws.profile.write_text(b.ws.profile.read_text() + '\n[unexpected]\nx=true\n')
    else: b.ws.task.write_text('changed task')
    write_owner(b.store.owner, frame)
    result = call(server, 'fetch_inbox')
    assert result['isError'] is True and prepared.wire == []
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize('arguments', [{'session_id': 'direct-OtherChild'}, {'agent_id': 'OtherChild'}, {'project_key': '/other'}])
def test_existing_core_refuses_another_binding_without_mail_effect(prepared, arguments):
    server = make(prepared)
    assert call(server)['isError'] is False
    assert call(server, 'fetch_inbox', **arguments)['isError'] is True
    assert prepared.wire == []


def test_loader_ignores_cached_product_and_proxy_modules(prepared, monkeypatch):
    def poisoned(*a, **k): pytest.fail('unverified module cache used')
    for name in ('runtime_legacy_execution', 'runtime_legacy_configuration', 'runtime_launch_intent',
                 'agentstack_codex_app.agent_mail_client', 'agentstack_codex_app.mcp_server'):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(active_execution_snapshot=poisoned,
            check_legacy_provider_outputs=poisoned, AgentStackProxy=poisoned, AgentMailClient=poisoned))
    assert call(make(prepared))['isError'] is False


@pytest.mark.parametrize('source', ['agent_mail_client.py', 'mcp_server.py', 'identity_store.py'])
def test_all_sources_are_frozen_and_verified_before_any_code_is_executed(prepared, source):
    assert call(make(prepared))['isError'] is False
    root = Path(prepared.bound.intent['legacy_context']['install_root'])
    marker = prepared.bound.ws.tmp / 'unverified-proxy-code-ran'
    script = root / 'integrations/codex_app/src/agentstack_codex_app' / source
    script.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ").write_text('BAD')\n")
    with pytest.raises(LocalModelError): make(prepared)
    assert not marker.exists() and prepared.wire == []


@pytest.mark.parametrize('source', ['agent_mail_client.py', 'hook_entry.py', 'identity_store.py',
    'snapshot.py', 'mcp_server.py', 'local_model_auth.py', 'process_observation.py'])
def test_replaced_read_bytes_cannot_execute_even_if_path_evidence_is_unchanged(prepared, monkeypatch, source):
    assert call(make(prepared))['isError'] is False
    marker = prepared.bound.ws.tmp / 'unverified-read-bytes-ran'
    original = prepared.api['secure_read']; substituted = []
    def changed(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if Path(path).name == source:
            substituted.append(str(path))
            return b'from pathlib import Path\nPath(' + repr(str(marker)).encode() + b").write_text('BAD')\n"
        return value
    monkeypatch.setitem(prepared.api['configured_preview_proxy'].__globals__, 'secure_read', changed)
    with pytest.raises(LocalModelError, match='^LOCAL_MODEL_AUTH_FAILED$'): make(prepared)
    assert substituted and not marker.exists() and prepared.wire == []


def test_same_bound_proxy_accepts_legitimate_session_start_not_a_new_launch(prepared):
    server = make(prepared)
    assert call(server, 'fetch_inbox')['isError'] is False
    frame = read_owner(prepared.bound.store.owner)
    frame['local_execution']['phase'] = 'running'
    frame['local_execution']['session_receipt'] = {'session_id': 'fixture-session', 'phase': 'started'}
    write_owner(prepared.bound.store.owner, frame)
    assert call(server, 'fetch_inbox')['isError'] is False
    assert len([r for r in prepared.wire if r['method'] == 'tools/call']) == 2
