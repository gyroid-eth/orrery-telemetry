"""Real runtime wrapper -> stdio proxy -> isolated HTTP -> B1/ORM."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from sqlalchemy import select

from agentstack_mail.fresh_client import FreshBoundClient, MAX_REQUEST_BYTES
from agentstack_mail.models import Message, MessageRecipient
from agentstack_mail.storage_origin_contract import canonical
from test_fresh_client import message
from test_fresh_guard import live
from test_fresh_mail import snapshot

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / 'bin/agentstack-runtime-client'


def environment(client, **changes):
    env = dict(os.environ, AGENTSTACK_PYTHON=sys.executable,
               AGENTSTACK_FRESH_CLIENT_CONTEXT=str(client.path), AGENTSTACK_RUNTIME_MODE='legacy',
               AGENT_NAME='wrong-old-identity', AGENTSTACK_PROJECT_KEY='/ignored-old-project')
    env.pop('AGENTSTACK_CLIENT_CONFIG', None)
    env.pop('AGENTSTACK_GLOBAL_SERVER_CONFIG', None)
    env.update(changes)
    return env


class Peer:
    def __init__(self, process):
        self.process, self.sequence, self.responses = process, 0, []

    async def rpc(self, method, params=None):
        self.sequence += 1
        request = dict(jsonrpc='2.0', id=self.sequence, method=method)
        if params is not None:request['params'] = params
        self.process.stdin.write((canonical(request) + '\n').encode())
        await self.process.stdin.drain()
        raw = await asyncio.wait_for(self.process.stdout.readline(), timeout=30)
        assert raw, f'proxy terminated with {self.process.returncode}'
        result = json.loads(raw)
        assert result['id'] == self.sequence
        self.responses.append(result)
        return result

    async def call(self, name, args=None):
        return await self.rpc('tools/call', {'name': name, 'arguments': {} if args is None else args})

    async def value(self, name, args=None):
        reply = await self.call(name, args)
        assert 'error' not in reply and reply['result']['isError'] is False, reply
        return json.loads(reply['result']['content'][0]['text'])


def refused(reply):
    if 'error' in reply:return reply['error']['message']
    assert reply['result']['isError'] is True
    return json.loads(reply['result']['content'][0]['text'])['code']


@asynccontextmanager
async def proxy(client, tmp_path, label='proxy'):
    log_path = tmp_path / (label + '.log')
    with log_path.open('w') as log:
        process = await asyncio.create_subprocess_exec('/bin/bash', str(WRAPPER), 'proxy',
            cwd=tmp_path, env=environment(client), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=log, limit=MAX_REQUEST_BYTES)
        peer = Peer(process)
        try:
            initialized = await peer.rpc('initialize', {'protocolVersion': '2025-06-18',
                'clientInfo': {'name': 'isolated-probe', 'version': '1'}, 'capabilities': {}})
            assert initialized['result']['serverInfo']['name'] == 'orrery-mail'
            yield peer
        finally:
            if process.stdin is not None:process.stdin.close()
            try:await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                try:await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill();await asyncio.wait_for(process.wait(), timeout=5)
            assert process.returncode is not None
            (tmp_path / (label + '-exit.json')).write_text(canonical({'pid': process.pid,
                'returncode': process.returncode, 'responses': len(peer.responses)}) + '\n')
            (tmp_path / (label + '-responses.json')).write_text(canonical(peer.responses) + '\n')


def test_real_proxy_schema_and_all_tools_reject_model_proof_anchor_uuid_and_unknown(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            async with proxy(client, tmp_path) as peer:
                tools = (await peer.rpc('tools/list'))['result']['tools']
                definitions = {tool['name']: tool for tool in tools}
                assert {'send_message', 'reply_message', 'fetch_inbox', 'whois', 'runtime_status'} <= definitions.keys()
                assert not {'guard_edit', 'register_agent', 'ensure_project', 'reconnect', 'health_check'} & definitions.keys()
                forbidden = {'auth', 'actor_agent_id', 'registration_token', 'expected_credential_generation',
                             'expected_server_instance_id', 'request_id', 'anchor', 'cwd', 'project_key'}
                samples = {
                    'send_message': message(), 'reply_message': {'message_id': 1, 'body_md': 'reply'},
                    'mark_message_read': {'message_id': 1}, 'acknowledge_message': {'message_id': 1},
                    'reserve_files': {'paths': ['note.py']}, 'renew_reservations': {},
                    'release_reservations': {}, 'check_reservations': {'paths': ['note.py']},
                    'fetch_inbox': {}, 'whois': {'target_name': 'Beta'},
                    'list_candidates': {'target_name': 'Beta'}, 'runtime_status': {},
                }
                assert definitions.keys() == samples.keys()
                before = await snapshot(runtime)
                for name, definition in definitions.items():
                    schema = definition['inputSchema']
                    assert schema['additionalProperties'] is False
                    assert not forbidden & set(schema['properties'])
                    assert not forbidden & set(schema.get('required', []))
                    for field in forbidden | {'sender_name', 'agent_name', 'unknown'}:
                        reply = await peer.call(name, dict(samples[name], **{field: people[0]['auth']['registration_token']}))
                        assert refused(reply) == 'INVALID_ARGUMENTS', (name, field, reply)
                for name in ('guard_edit', 'register_agent', 'ensure_project'):
                    assert refused(await peer.call(name, {})) == 'TOOL_UNPUBLISHED'
                assert await snapshot(runtime) == before
                assert list((client.path.parent / 'requests').iterdir()) == []
                output = canonical(peer.responses) + (tmp_path / 'proxy.log').read_text() + (tmp_path / 'guard-http.log').read_text()
                assert all(person['auth']['registration_token'] not in output for person in people)
    asyncio.run(run())


def test_real_proxy_send_peek_reply_read_ack_bind_same_numeric_owner(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, alpha, _):
            beta = await FreshBoundClient.prepare(runtime.root / 'config.json', people[1]['binding_path'],
                alpha.context['endpoint'], client_id=str(uuid4()))
            async with proxy(alpha, tmp_path, 'alpha') as a, proxy(beta, tmp_path, 'beta') as b:
                status = await a.value('runtime_status')
                assert status == dict(mail_instance_id=runtime.config['instance_id'], mail_agent_id=people[0]['agent_id'],
                    credential_generation=people[0]['credential_generation'], agent_name='Alpha')
                sent = await a.value('send_message', message(importance='high', ack_required=True))
                before = await snapshot(runtime)
                inbox = await b.value('fetch_inbox')
                assert [row['id'] for row in inbox] == [sent['message_id']]
                assert await snapshot(runtime) == before
                await b.value('mark_message_read', {'message_id': sent['message_id']})
                await b.value('acknowledge_message', {'message_id': sent['message_id']})
                replied = await b.value('reply_message', {'message_id': sent['message_id'], 'body_md': 'bound reply'})
                who = await a.value('whois', {'target_agent_id': people[1]['agent_id']})
                assert who['target_agent_id'] == people[1]['agent_id']
                async with runtime.sessions() as session:
                    original = await session.get(Message, sent['message_id'])
                    reply = await session.get(Message, replied['message_id'])
                    recipient = await session.get(MessageRecipient, (original.id, people[1]['agent_id']))
                    assert (original.sender_id, reply.sender_id) == (people[0]['agent_id'], people[1]['agent_id'])
                    assert reply.thread_id == original.thread_id and reply.importance == 'high' and reply.ack_required
                    assert recipient.read_ts is not None and recipient.ack_ts is not None
                output = canonical(a.responses + b.responses)
                assert all(person['auth']['registration_token'] not in output for person in people)
    asyncio.run(run())


def test_real_proxy_ambiguous_candidates_mixed_batch_refusal_and_explicit_id(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            duplicate = await runtime.register_local(request_id=str(uuid4()), name='beta', program='fixture', model='fixture')
            await runtime.core.retire(duplicate['agent_id'])
            async with proxy(client, tmp_path) as peer:
                reply = await peer.call('send_message', message(recipients=[{'target_name': 'Gamma'}, {'target_name': 'Beta'}]))
                assert refused(reply) == 'TARGET_NAME_AMBIGUOUS'
                details = json.loads(reply['result']['content'][0]['text'])
                assert details['candidate_count'] == 2 and details['request_id']
                candidates = await peer.value('list_candidates', {'target_name': 'Beta'})
                assert {row['target_agent_id'] for row in candidates['candidates']} == {people[1]['agent_id'], duplicate['agent_id']}
                async with runtime.sessions() as session:
                    assert (await session.execute(select(Message))).scalars().all() == []
                    assert (await session.execute(select(MessageRecipient))).scalars().all() == []
                result = await peer.value('send_message', message(recipients=[{'target_agent_id': people[1]['agent_id']}]))
                assert result['recipients'][0]['agent_id'] == people[1]['agent_id']
    asyncio.run(run())


def test_real_proxy_preserves_invalid_original_json_types_and_omission_semantics(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            async with proxy(client, tmp_path) as peer:
                before = await snapshot(runtime)
                for name, args in [('send_message', message(ack_required=1)),
                                   ('send_message', message(body_md=False)),
                                   ('send_message', message(subject=None)),
                                   ('reply_message', {'message_id': True, 'body_md': 'reply'}),
                                   ('reply_message', {'message_id': 1, 'body_md': 'reply', 'ack_required': None}),
                                   ('reserve_files', {'paths': [False]}),
                                   ('reserve_files', {'paths': ['a.py'], 'ttl_seconds': True}),
                                   ('fetch_inbox', {'limit': True}), ('runtime_status', None)]:
                    if name == 'runtime_status':
                        assert refused(await peer.rpc('tools/call', {'name': name, 'arguments': None})) == 'INVALID_ARGUMENTS'
                    else:assert refused(await peer.call(name, args)) == 'INVALID_ARGUMENTS'
                assert await snapshot(runtime) == before
                assert list((client.path.parent / 'requests').iterdir()) == []
    asyncio.run(run())


def test_running_proxy_rejects_revoked_proof_and_changed_context_no_anonymous_fallback(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            async with proxy(client, tmp_path) as peer:
                await runtime.core.recover(people[0]['agent_id'], expected_generation=people[0]['credential_generation'],
                                          new_token='fresh-proxy-new-private-fixture')
                before = await snapshot(runtime)
                for name, args in [('fetch_inbox', {}), ('send_message', message()), ('runtime_status', {})]:
                    assert refused(await peer.call(name, args))
                assert 'error' in await peer.rpc('tools/list')
                assert await snapshot(runtime) == before
                saved = client.path.read_bytes();client.path.write_text('{broken')
                assert refused(await peer.call('whois', {'target_name': 'Beta'})) == 'FRESH_PRIVATE_FILE_UNAVAILABLE'
                client.path.write_bytes(saved)
    asyncio.run(run())


def test_running_proxy_transport_failure_never_reports_success(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, _, client, server):
            async with proxy(client, tmp_path) as peer:
                server.terminate();server.wait(timeout=5)
                assert refused(await peer.call('whois', {'target_name': 'Beta'})) == 'FRESH_TRANSPORT_UNAVAILABLE'
                assert refused(await peer.rpc('tools/list')) == 'FRESH_TRANSPORT_UNAVAILABLE'
    asyncio.run(run())


def test_explicit_runtime_wrapper_selection_missing_context_never_uses_legacy(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            envs = [environment(client, AGENTSTACK_FRESH_CLIENT_CONTEXT=''),
                    environment(client, AGENTSTACK_FRESH_CLIENT_CONTEXT=str(tmp_path / 'absent.json')),
                    environment(client, AGENTSTACK_RUNTIME_MODE='global'),
                    environment(client, AGENTSTACK_CLIENT_CONFIG='/old-global')]
            marker_only = environment(client, AGENTSTACK_RUNTIME_MODE='fresh')
            marker_only.pop('AGENTSTACK_FRESH_CLIENT_CONTEXT');envs.append(marker_only)
            corrupt = tmp_path / 'corrupt.json';corrupt.write_text('{broken');corrupt.chmod(0o600)
            envs.append(environment(client, AGENTSTACK_FRESH_CLIENT_CONTEXT=str(corrupt)))
            for env in envs:
                result = await asyncio.to_thread(subprocess.run, ['/bin/bash', str(WRAPPER), 'proxy'],
                    env=env, cwd=tmp_path, input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n',
                    capture_output=True, text=True, timeout=30)
                assert result.returncode == 2 and not result.stdout
                assert 'FRESH_' in result.stderr and 'Traceback' not in result.stderr
                assert people[0]['auth']['registration_token'] not in result.stderr
            # With no fresh marker/context the original wrapper still dispatches
            # to its existing stdlib CLI; help never opens a database or service.
            legacy = dict(os.environ)
            for key in list(legacy):
                if key.startswith('AGENTSTACK_'):legacy.pop(key)
            result = await asyncio.to_thread(subprocess.run, ['/bin/bash', str(WRAPPER), '--help'],
                env=legacy, cwd=tmp_path, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0 and '--runtime-mode {legacy,global}' in result.stdout
    asyncio.run(run())


def test_actual_fastmcp_stdio_client_understands_schema_results_and_disconnects(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            transport = StdioTransport('/bin/bash', [str(WRAPPER), 'proxy'], env=environment(client),
                cwd=str(tmp_path), keep_alive=False, log_file=tmp_path / 'sdk-proxy.log')
            try:
                async with Client(transport, timeout=30) as model:
                    assert 'guard_edit' not in {tool.name for tool in await model.list_tools()}
                    status = (await model.call_tool('runtime_status', {})).data
                    assert status['mail_agent_id'] == people[0]['agent_id']
                    sent = (await model.call_tool('send_message', message())).data
                    inbox = (await model.call_tool('fetch_inbox', {'target_name': 'Beta'})).data
                    assert [row['id'] for row in inbox] == [sent['message_id']]
            finally:
                await transport.close()
            assert transport._connect_task is None
            assert people[0]['auth']['registration_token'] not in (tmp_path / 'sdk-proxy.log').read_text()
    asyncio.run(run())
