"""Protected fresh caller through the real tools/ORM; no installed state."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from agentstack_mail import fresh_client
from agentstack_mail.fresh_client import FreshBoundClient, _http
from agentstack_mail.fresh_runtime import _read_private
from agentstack_mail.fresh_server import build_fresh_server
from agentstack_mail.models import FileReservation, Message
from agentstack_mail.storage_origin_contract import B1Error, canonical
from test_fresh_mail import snapshot, state
from test_fresh_reservations import known_rules

ENDPOINT = 'http://127.0.0.1:19876/mcp'


def test_startup_projection_is_finite_and_health_pins_instance_without_credentials(tmp_path,monkeypatch):
    from agentstack_mail.fresh_runtime import FreshRuntime
    async def scenario():
        runtime=await FreshRuntime.initialize(tmp_path/'prepared',mode='fresh')
        try:
            registered=await runtime.register_local(request_id=str(uuid4()),name='Host',program='fixture',model='fixture')
            client=await prepare(runtime,registered)
            before=await snapshot(runtime)
            result=fresh_client.startup_projection(client.path)
            assert set(result)=={'runtime_mode','mail_instance_id','operator_id','context_digest','context_path','server_config','endpoint'}
            assert result['server_config']==str(runtime.root/'config.json') and result['mail_instance_id']==runtime.config['instance_id']
            binding=_read_private(Path(registered['binding_path']))
            assert binding['registration_token'] not in json.dumps(result)
            async def wire(endpoint,tool,args):
                assert endpoint==ENDPOINT and tool=='health_check'
                assert args=={'expected_server_instance_id':runtime.config['instance_id']}
                return await runtime.core.health(**args)
            monkeypatch.setattr(fresh_client,'_http',wire)
            assert await fresh_client.startup_health(client.path)=={'ok':True,'mail_instance_id':runtime.config['instance_id']}
            async def wrong(*args):return {'mail_instance_id':'0'*32}
            monkeypatch.setattr(fresh_client,'_http',wrong)
            with pytest.raises(B1Error,match='FRESH_SERVICE_MISMATCH'):await fresh_client.startup_health(client.path)
            assert await snapshot(runtime)==before
        finally:await runtime.close()
    asyncio.run(scenario())


def test_startup_projection_refuses_unprepared_and_isolated_context(tmp_path):
    async def scenario():
        async with state(tmp_path) as (runtime,people):
            client=await prepare(runtime,people[0])
            with pytest.raises(B1Error,match='FRESH_MODE_REQUIRED'):fresh_client.startup_projection(client.path)
            with pytest.raises((B1Error,OSError)):fresh_client.startup_projection(tmp_path/'missing-context.json')
    asyncio.run(scenario())


async def prepare(runtime, person, *, dispatch=None, client_id=None):
    server = build_fresh_server(core=runtime.core)
    async def wire(endpoint, tool, args):
        assert endpoint == ENDPOINT
        return await _http(server, tool, args)
    return await FreshBoundClient.prepare(runtime.root / 'config.json', person['binding_path'],
        ENDPOINT, client_id=client_id or str(uuid4()), dispatch=dispatch or wire)


def message(**changes):
    args = dict(recipients=[{'target_name': 'Beta'}], subject='bound caller', body_md='private proof')
    args.update(changes)
    return args


def test_bound_send_reply_owner_read_ack_and_whois_use_same_proof(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            alpha, beta = [await prepare(runtime, p) for p in people[:2]]
            sent = await alpha.call('send_message', message(importance='high', ack_required=True))
            inbox = await beta.call('fetch_inbox', {})
            assert [row['id'] for row in inbox] == [sent['message_id']]
            await beta.call('mark_message_read', {'message_id': sent['message_id']})
            await beta.call('acknowledge_message', {'message_id': sent['message_id']})
            replied = await beta.call('reply_message', {'message_id': sent['message_id'], 'body_md': 'reply'})
            async with runtime.sessions() as session:
                row = await session.get(Message, replied['message_id'])
                assert row.sender_id == people[1]['agent_id']
                assert row.importance == 'high' and row.ack_required
            who = await alpha.call('whois', {'target_agent_id': people[1]['agent_id']})
            assert who['target_agent_id'] == people[1]['agent_id']
            assert (await alpha.call('reconnect', {}))['mail_agent_id'] == people[0]['agent_id']
            assert people[0]['auth']['registration_token'] not in canonical([sent, inbox, replied, who])
    asyncio.run(run())


@pytest.mark.parametrize('field', ['auth', 'actor_agent_id', 'registration_token',
                                  'expected_server_instance_id', 'sender_name', 'agent_name',
                                  'cwd', 'anchor', 'request_id', 'unknown'])
def test_model_identity_anchor_and_unknown_fields_rejected_before_transport(tmp_path, field):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            before = await snapshot(runtime)
            async def forbidden(*args):
                pytest.fail('rejected business input reached transport')
            client.dispatch = forbidden
            with pytest.raises(B1Error, match='UNKNOWN_ARGUMENTS'):
                await client.call('send_message', message(**{field: 'other'}))
            assert list((client.path.parent / 'requests').iterdir()) == []
            assert await snapshot(runtime) == before
    asyncio.run(run())


@pytest.mark.parametrize('change', [{'ack_required': 1}, {'body_md': False},
                                    {'recipients': [{'target_agent_id': True}]}, {'subject': None}])
def test_original_json_types_not_coerced_to_valid_business_values(tmp_path, change):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            before = await snapshot(runtime)
            with pytest.raises(B1Error):
                await client.call('send_message', message(**change))
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_only_project_key_is_ignored_and_does_not_select_actor_or_cwd(tmp_path, known_rules, monkeypatch):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            monkeypatch.chdir(tmp_path)
            sent = await client.call('send_message', message(project_key={'anything': ['ignored']}))
            grant = await client.call('reserve_files', {'paths': ['local.py'], 'project_key': '/wrong'})
            assert grant['reservations'][0]['path_pattern'] == str(tmp_path / 'local.py')
            async with runtime.sessions() as session:
                assert (await session.get(Message, sent['message_id'])).sender_id == people[0]['agent_id']
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['missing', 'unsafe-mode', 'symlink', 'wrong-key', 'wrong-A', 'wrong-g', 'wrong-I'])
def test_changed_or_unavailable_binding_never_falls_back(tmp_path, operation):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            path = runtime.root / 'bindings' / f"{people[0]['agent_id']}.json"
            value = json.loads(path.read_text())
            if operation == 'missing':path.unlink()
            elif operation == 'unsafe-mode':path.chmod(0o644)
            elif operation == 'symlink':
                path.unlink();path.symlink_to(people[1]['binding_path'])
            else:
                key = {'wrong-key': 'registration_token', 'wrong-A': 'mail_agent_id',
                       'wrong-g': 'credential_generation', 'wrong-I': 'mail_instance_id'}[operation]
                value[key] = 999 if key in {'mail_agent_id', 'credential_generation'} else 'changed-fixture'
                path.write_text(canonical(value))
            before = await snapshot(runtime)
            async def forbidden(*args):pytest.fail('invalid binding reached transport')
            client.dispatch = forbidden
            with pytest.raises(B1Error):await client.call('send_message', message())
            assert await snapshot(runtime) == before
    asyncio.run(run())


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:8765/mcp', 'https://127.0.0.1:19876/mcp',
                                     'http://example.invalid:19876/mcp', 'http://u:p@localhost:19876/mcp',
                                     'http://localhost:19876/mcp?q=1'])
def test_prepare_rejects_nonisolated_endpoints(tmp_path, endpoint):
    async def run():
        async with state(tmp_path) as (runtime, people):
            with pytest.raises(B1Error, match='FRESH_ENDPOINT_INVALID'):
                await FreshBoundClient.prepare(runtime.root / 'config.json', people[0]['binding_path'],
                                               endpoint, client_id=str(uuid4()))
            assert not (runtime.root / 'clients').exists()
    asyncio.run(run())


def test_prepare_checks_actual_backend_I_and_existing_context_cannot_be_retargeted(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            other = tmp_path / 'other';other.mkdir()
            async with state(other) as (wrong, _):
                server = build_fresh_server(core=wrong.core)
                async def wire(endpoint, tool, args):return await _http(server, tool, args)
                with pytest.raises(B1Error, match='INSTANCE_MISMATCH'):
                    await prepare(runtime, people[0], dispatch=wire)
            client_id = str(uuid4())
            client = await prepare(runtime, people[0], client_id=client_id)
            assert (await prepare(runtime, people[0], client_id=client_id)).path == client.path
            with pytest.raises(B1Error, match='FRESH_CLIENT_CONTEXT_CONFLICT'):
                await prepare(runtime, people[1], client_id=client_id)
            value = json.loads(client.path.read_text());value['endpoint'] = 'http://127.0.0.1:19877/mcp'
            client.path.write_text(canonical(value))
            with pytest.raises(B1Error, match='FRESH_CLIENT_CONTEXT_CHANGED'):
                await client.call('health_check', {})
    asyncio.run(run())


def test_refused_ambiguous_recipient_keeps_candidates_and_whole_batch_is_atomic(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            await runtime.register_local(request_id=str(uuid4()), name='beta', program='fixture', model='fixture')
            client = await prepare(runtime, people[0])
            before = await snapshot(runtime)
            request = str(uuid4())
            with pytest.raises(B1Error, match='TARGET_NAME_AMBIGUOUS') as error:
                await client.call('send_message', message(recipients=[{'target_name': 'Gamma'},
                                                                      {'target_name': 'Beta'}]), request_id=request)
            assert error.value.details['candidate_count'] == 2
            assert error.value.details['request_id'] == request
            async with runtime.sessions() as session:
                assert (await session.execute(select(Message))).scalars().all() == []
            # A terminal refusal intent is expected, but no Mail/recipient writes.
            after = await snapshot(runtime)
            assert after['messages'] == before['messages']
    asyncio.run(run())


def test_response_loss_replay_keeps_identical_arguments_and_one_message(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            real = client.dispatch
            seen = []
            async def lose_once(endpoint, tool, args):
                seen.append(canonical(args))
                result = await real(endpoint, tool, args)
                if len(seen) == 1:raise B1Error('FRESH_TRANSPORT_UNAVAILABLE')
                return result
            client.dispatch = lose_once
            request = str(uuid4())
            with pytest.raises(B1Error) as error:
                await client.call('send_message', message(), request_id=request)
            assert error.value.details['request_id'] == request
            result = await client.replay(request)
            assert len(seen) == 2 and seen[0] == seen[1]
            async with runtime.sessions() as session:
                assert len((await session.execute(select(Message))).scalars().all()) == 1
            with pytest.raises(B1Error, match='REQUEST_INTENT_CONFLICT'):
                await client.call('send_message', message(body_md='changed'), request_id=request)
            assert len(seen) == 2
            assert result['sender_id'] == people[0]['agent_id']
    asyncio.run(run())


def test_current_cwd_for_new_paths_and_saved_absolute_paths_for_replay(tmp_path, known_rules, monkeypatch):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            a, b = tmp_path / 'a', tmp_path / 'b';a.mkdir();b.mkdir()
            real = client.dispatch
            seen = []
            async def capture(endpoint, tool, args):
                seen.append(canonical(args))
                return await real(endpoint, tool, args)
            client.dispatch = capture
            request = str(uuid4())
            monkeypatch.chdir(a)
            first = await client.call('reserve_files', {'paths': ['same.py'], 'ttl_seconds': 900}, request_id=request)
            monkeypatch.chdir(b)
            assert await client.replay(request) == first
            assert seen[0] == seen[1]
            second = await client.call('reserve_files', {'paths': ['same.py']})
            assert second['reservations'][0]['path_pattern'] == str(b / 'same.py')
            assert first['reservations'][0]['path_pattern'] == str(a / 'same.py')
            await client.call('renew_reservations', {'paths': [str(a / 'same.py')], 'extend_seconds': 60})
            await client.call('release_reservations', {'paths': [str(a / 'same.py')]})
            check = await client.call('check_reservations', {'paths': [str(b / 'same.py')]})
            assert check['own_matching_ids'] == [second['reservations'][0]['id']]
            async with runtime.sessions() as session:
                assert len((await session.execute(select(FileReservation))).scalars().all()) == 2
    asyncio.run(run())


def test_long_saved_request_has_explicit_larger_limit_binding_default_stays_64k(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            request = str(uuid4())
            result = await client.call('send_message', message(body_md='x' * 70000), request_id=request)
            path = client.path.parent / 'requests' / (request + '.json')
            assert path.stat().st_size > 65536
            with pytest.raises(B1Error, match='FRESH_PRIVATE_FILE_UNAVAILABLE'):_read_private(path)
            assert await client.replay(request) == result
            for path in (client.path, client.path.parent / 'requests' / (request + '.json')):
                assert path.stat().st_mode & 0o777 == 0o600
    asyncio.run(run())


def test_network_exception_text_and_credentials_never_printed(tmp_path, capsys, monkeypatch):
    import io
    import sys
    async def setup():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            return client.path, people[0]['auth']['registration_token']
    context, secret = asyncio.run(setup())
    class BrokenClient:
        def __init__(self, *args, **kwargs):raise RuntimeError('leaked-' + secret)
    monkeypatch.setattr(fresh_client, 'Client', BrokenClient)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{}'))
    assert fresh_client.main(['call-json', '--context', str(context), '--tool', 'reconnect']) == 1
    # Supply a valid business document; CLI must not print transport tracebacks.
    output = capsys.readouterr().out
    assert secret not in output and 'leaked-' not in output
    assert json.loads(output)['code'] == 'FRESH_TRANSPORT_UNAVAILABLE'


def test_revoked_proof_does_not_downgrade_peek_or_return_cached_write_success(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            client = await prepare(runtime, people[0])
            request = str(uuid4())
            await client.call('send_message', message(), request_id=request)
            await runtime.core.recover(people[0]['agent_id'],
                expected_generation=people[0]['credential_generation'], new_token='new-fixture-private-key-123456789')
            before = await snapshot(runtime)
            for tool, args in [('fetch_inbox', {}), ('whois', {'target_name': 'Beta'}), ('reconnect', {})]:
                with pytest.raises(B1Error):await client.call(tool, args)
            with pytest.raises(B1Error):await client.replay(request)
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_real_http_prepare_bound_call_and_cli_keep_secrets_private_and_stop_process(tmp_path):
    import socket
    import subprocess
    import sys
    import time

    async def run():
        async with state(tmp_path) as (runtime, people):
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0));port = reservation.getsockname()[1]
            assert port not in {8765, 8770, 8781, 8791}
            log_path = tmp_path / 'bound-http.log'
            with log_path.open('w') as log:
                process = subprocess.Popen([sys.executable, '-m', 'agentstack_mail.fresh_cli',
                    'serve', '--config', str(runtime.root / 'config.json'), '--port', str(port)],
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        assert process.poll() is None, log_path.read_text()
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=0.1):break
                        except OSError:await asyncio.sleep(0.1)
                    else:pytest.fail('bound HTTP startup timed out')
                    endpoint = f'http://127.0.0.1:{port}/mcp'
                    prepared = await asyncio.to_thread(subprocess.run,
                        [sys.executable, '-m', 'agentstack_mail.fresh_client', 'prepare',
                         '--config', str(runtime.root / 'config.json'), '--binding', people[0]['binding_path'],
                         '--endpoint', endpoint, '--client-id', str(uuid4())],
                        capture_output=True, text=True, timeout=30)
                    assert prepared.returncode == 0, prepared.stdout
                    context = json.loads(prepared.stdout)['context_path']
                    client = FreshBoundClient(context)
                    sent = await client.call('send_message', message())
                    # CLI input is original business JSON; identity never enters stdin.
                    called = await asyncio.to_thread(subprocess.run,
                        [sys.executable, '-m', 'agentstack_mail.fresh_client', 'call-json',
                         '--context', context, '--tool', 'whois'],
                        input='{"target_name":"Beta"}', capture_output=True, text=True, timeout=30)
                    assert called.returncode == 0, called.stdout
                    assert json.loads(called.stdout)['target_agent_id'] == people[1]['agent_id']
                    assert sent['sender_id'] == people[0]['agent_id']
                    output = prepared.stdout + prepared.stderr + called.stdout + called.stderr + log_path.read_text()
                    assert all(person['auth']['registration_token'] not in output for person in people)
                finally:
                    if process.poll() is None:process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait(timeout=5)
                    assert process.poll() is not None
    asyncio.run(run())
