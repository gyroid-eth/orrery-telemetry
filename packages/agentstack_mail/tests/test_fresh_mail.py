"""Fresh isolation slice: real ORM, real B1 validators and published handlers."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastmcp import Client
from sqlalchemy import select, text

from agentstack_mail.fresh_runtime import FreshRuntime
from agentstack_mail.fresh_server import build_fresh_server
from agentstack_mail.models import Agent, MailInstance, Message, MessageRecipient
from agentstack_mail.storage_origin_contract import B1Error


@asynccontextmanager
async def state(tmp_path, count=3):
    runtime = await FreshRuntime.initialize(tmp_path / 'fresh')
    people = []
    try:
        for name in ('Alpha', 'Beta', 'Gamma')[:count]:
            result = await runtime.register_local(request_id=str(uuid4()), name=name,
                                                  program='fixture', model='fixture')
            binding = json.loads((runtime.root / 'bindings' / f"{result['agent_id']}.json").read_text())
            people.append(dict(result, auth={
                'actor_agent_id': binding['mail_agent_id'],
                'registration_token': binding['registration_token'],
                'expected_credential_generation': binding['credential_generation']}))
        yield runtime, people
    finally:
        await runtime.close()


def send_args(runtime, people, **changes):
    args = dict(expected_server_instance_id=runtime.config['instance_id'], auth=people[0]['auth'],
                request_id='send-one', recipients=[{'target_name': 'Beta'}],
                subject='real stored mail', body_md='fixture body')
    args.update(changes)
    return args


async def snapshot(runtime):
    async with runtime.sessions() as session:
        tables = (await session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ))).scalars().all()
        return {table: [tuple(row) for row in (await session.execute(
            text('SELECT * FROM "' + table + '" ORDER BY 1,2'))).all()] for table in tables}


def test_init_is_explicit_empty_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTSTACK_MAIL_DATABASE_URL', 'sqlite+aiosqlite:////forbidden/live.sqlite3')

    async def run():
        runtime = await FreshRuntime.initialize(tmp_path / 'fresh')
        try:
            health = await runtime.core.health()
            assert health['mail_instance_id'] == runtime.config['instance_id']
            async with runtime.sessions() as session:
                assert (await session.execute(select(Agent))).scalars().all() == []
                assert (await session.execute(select(Message))).scalars().all() == []
            before = await snapshot(runtime)
            with pytest.raises(B1Error, match='FRESH_DESTINATION_EXISTS'):
                await FreshRuntime.initialize(runtime.root)
            assert await snapshot(runtime) == before
            with pytest.raises(B1Error, match='INSTANCE_MISMATCH'):
                await runtime.core.health('old-instance')
        finally:
            await runtime.close()

    asyncio.run(run())


def test_local_registration_replays_and_keeps_key_private(tmp_path, capsys, monkeypatch):
    from agentstack_mail.fresh_cli import main
    from agentstack_mail import fresh_runtime
    root = tmp_path / 'cli'
    main(['init', '--root', str(root)])
    request = str(uuid4())
    command = ['register', '--config', str(root / 'config.json'), '--request-id', request,
               '--name', 'Alpha', '--program', 'fixture', '--model', 'fixture']
    real_canonical = fresh_runtime.canonical
    def fail_binding_once(value):
        if value.get('kind') == 'orrery-fresh-owner-v1':
            monkeypatch.setattr(fresh_runtime, 'canonical', real_canonical)
            raise OSError('fixture publication failure')
        return real_canonical(value)
    monkeypatch.setattr(fresh_runtime, 'canonical', fail_binding_once)
    with pytest.raises(OSError, match='fixture publication failure'):
        main(command)
    assert list((root / 'bindings').iterdir()) == []
    async def committed_registration():
        runtime = await FreshRuntime.open(root / 'config.json')
        try:
            async with runtime.sessions() as session:
                agents = (await session.execute(select(Agent))).scalars().all()
                assert len(agents) == 1
                return agents[0].id, agents[0].registration_token, agents[0].credential_generation
        finally:
            await runtime.close()
    committed = asyncio.run(committed_registration())
    main(command)
    first = json.loads(capsys.readouterr().out.splitlines()[-1])
    main(command)
    second_output = capsys.readouterr().out
    assert json.loads(second_output) == first
    binding_path = root / 'bindings' / f"{first['agent_id']}.json"
    binding = json.loads(binding_path.read_text())
    assert binding['registration_token'] not in second_output
    assert binding_path.stat().st_mode & 0o077 == 0
    assert root.stat().st_mode & 0o077 == 0
    assert 'registration_token' not in first
    assert (first['agent_id'], binding['registration_token'], binding['credential_generation']) == committed
    assert asyncio.run(committed_registration()) == committed


def test_minimal_reply_inherits_flags_and_explicit_override_remains_explicit(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            async with Client(build_fresh_server(core=runtime.core)) as client:
                parent = (await client.call_tool('send_message', send_args(runtime, people,
                    importance='high', ack_required=True))).data
                args = dict(expected_server_instance_id=runtime.config['instance_id'], auth=people[1]['auth'],
                            message_id=parent['message_id'], body_md='reply')
                inherited = (await client.call_tool('reply_message', dict(args, request_id='inherit'))).data
                overridden = (await client.call_tool('reply_message', dict(args, request_id='override',
                    importance='normal', ack_required=False))).data
                async with runtime.sessions() as session:
                    first = await session.get(Message, inherited['message_id'])
                    second = await session.get(Message, overridden['message_id'])
                    assert (first.importance, first.ack_required) == ('high', True)
                    assert (second.importance, second.ack_required) == ('normal', False)
                for field, value in [('importance', None), ('ack_required', None),
                                     ('importance', 1), ('ack_required', 'false')]:
                    result = await client.call_tool('reply_message', dict(args, request_id='invalid',
                        **{field: value}), raise_on_error=False)
                    assert result.is_error
    asyncio.run(run())


def test_registration_checks_configured_instance_in_its_transaction(tmp_path):
    async def run():
        runtime = await FreshRuntime.initialize(tmp_path / 'fresh')
        try:
            async with runtime.sessions() as session:
                instance = await session.get(MailInstance, 1)
                instance.instance_id = 'substituted-instance'
                await session.commit()
            with pytest.raises(B1Error, match='INSTANCE_MISMATCH'):
                await runtime.register_local(request_id=str(uuid4()), name='Alpha',
                                             program='fixture', model='fixture')
            async with runtime.sessions() as session:
                assert (await session.execute(select(Agent))).scalars().all() == []
        finally:
            await runtime.close()
    asyncio.run(run())


def test_wire_send_reply_and_peek_persist_real_rows(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            async with Client(build_fresh_server(core=runtime.core)) as client:
                tools = {tool.name for tool in await client.list_tools()}
                assert 'register_agent' not in tools and 'ensure_project' not in tools
                health = await client.call_tool('health_check', {})
                assert health.data['mail_instance_id'] == runtime.config['instance_id']
                sent = (await client.call_tool('send_message', send_args(runtime, people))).data
                assert sent['decision_only'] is False and sent['delivery_status'] == 'notification_pending'
                before = await snapshot(runtime)
                peek = (await client.call_tool('fetch_inbox', dict(
                    expected_server_instance_id=runtime.config['instance_id'], target_name='Beta'))).data
                assert [(m['id'], m['body_md'], m['from']) for m in peek] == [
                    (sent['message_id'], 'fixture body', 'Alpha')]
                assert await snapshot(runtime) == before
                reply = (await client.call_tool('reply_message', dict(
                    expected_server_instance_id=runtime.config['instance_id'], auth=people[1]['auth'],
                    request_id='reply-one', message_id=sent['message_id'], body_md='actual reply'))).data
                assert reply['thread_id'] == sent['thread_id']
                assert reply['sender_id'] == people[1]['agent_id']
            async with runtime.sessions() as session:
                rows = (await session.execute(select(Message).order_by(Message.id))).scalars().all()
                assert [(row.sender_id, row.body_md) for row in rows] == [
                    (people[0]['agent_id'], 'fixture body'), (people[1]['agent_id'], 'actual reply')]
                assert rows[1].subject == 'Re: real stored mail'
                assert await session.get(MessageRecipient, (reply['message_id'], people[0]['agent_id']))
    asyncio.run(run())


@pytest.mark.parametrize('tool', ['send_message', 'reply_message', 'mark_message_read',
                                 'acknowledge_message', 'reconnect'])
@pytest.mark.parametrize('bad', ['missing', 'wrong', 'null', 'old-instance'])
def test_owner_wire_negative_matrix_has_no_effect(tmp_path, tool, bad):
    async def run():
        async with state(tmp_path) as (runtime, people):
            sent = await runtime.core.write_message('send', **send_args(runtime, people))
            common = dict(expected_server_instance_id=runtime.config['instance_id'], auth=people[1]['auth'])
            if tool == 'send_message':
                args = send_args(runtime, people, request_id='negative-send')
            elif tool == 'reply_message':
                args = dict(common, request_id='negative-reply', message_id=sent['message_id'], body_md='reply')
            elif tool == 'reconnect':
                args = common
            else:
                args = dict(common, request_id='negative-receipt', message_id=sent['message_id'],
                            target_agent_id=people[1]['agent_id'])
            if bad == 'missing':
                del args['auth']
            elif bad == 'null':
                args['auth'] = None
            elif bad == 'wrong':
                args['auth'] = dict(args['auth'], registration_token='wrong-fixture-secret')
            else:
                args['expected_server_instance_id'] = 'old-instance'
            before = await snapshot(runtime)
            async with Client(build_fresh_server(core=runtime.core)) as client:
                result = await client.call_tool(tool, args, raise_on_error=False)
                assert result.is_error
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_read_ack_first_times_and_other_recipient_are_preserved(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            sent = await runtime.core.write_message('send', **send_args(runtime, people,
                recipients=[{'target_name': 'Beta'}, {'target_name': 'Gamma', 'kind': 'cc'}]))
            args = dict(expected_server_instance_id=runtime.config['instance_id'], auth=people[1]['auth'],
                        target_agent_id=people[1]['agent_id'], message_id=sent['message_id'])
            read = await runtime.core.owner('read', request_id='read', **args)
            async with runtime.sessions() as session:
                pair = await session.get(MessageRecipient, (sent['message_id'], people[1]['agent_id']))
                assert pair.read_ts is not None and pair.ack_ts is None
            ack = await runtime.core.owner('ack', request_id='ack', **args)
            assert read['read_ts'] == ack['read_ts'] and ack['ack_ts'] is not None
            assert await runtime.core.owner('ack', request_id='ack', **args) == ack
            assert await runtime.core.owner('ack', request_id='ack-new-request', **args) == ack
            async with runtime.sessions() as session:
                beta = await session.get(MessageRecipient, (sent['message_id'], people[1]['agent_id']))
                gamma = await session.get(MessageRecipient, (sent['message_id'], people[2]['agent_id']))
                assert beta.read_ts.isoformat() == ack['read_ts']
                assert beta.ack_ts.isoformat() == ack['ack_ts']
                assert gamma.read_ts is None and gamma.ack_ts is None
            for operation in ('read', 'ack'):
                with pytest.raises(B1Error, match='NOT_A_RECIPIENT'):
                    await runtime.core.owner(operation, **dict(args, auth=people[0]['auth'],
                        target_agent_id=people[0]['agent_id'], request_id='not-recipient-' + operation))
                with pytest.raises(B1Error, match='TARGET_OWNER_MISMATCH'):
                    await runtime.core.owner(operation, **dict(args, target_agent_id=people[2]['agent_id'],
                        request_id='not-owner-' + operation))
    asyncio.run(run())


def test_mixed_recipients_refuse_whole_message_and_keep_terminal_result(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            args = send_args(runtime, people, recipients=[{'target_name': 'Beta'}, {'target_name': 'Later'}])
            with pytest.raises(B1Error, match='TARGET_NAME_NOT_FOUND'):
                await runtime.core.write_message('send', **args)
            async with runtime.sessions() as session:
                assert (await session.execute(select(Message))).scalars().all() == []
                assert (await session.execute(select(MessageRecipient))).scalars().all() == []
            await runtime.register_local(request_id=str(uuid4()), name='Later', program='fixture', model='fixture')
            with pytest.raises(B1Error, match='TARGET_NAME_NOT_FOUND'):
                await runtime.core.write_message('send', **args)
            sent = await runtime.core.write_message('send', **dict(args, request_id='new-intent'))
            assert len(sent['recipients']) == 2
            with pytest.raises(B1Error, match='REQUEST_INTENT_CONFLICT'):
                await runtime.core.write_message('send', **dict(args, body_md='different'))
    asyncio.run(run())


def test_ambiguous_names_include_retired_and_never_choose_latest(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            other = await runtime.register_local(request_id=str(uuid4()), name='beta',
                                                 program='fixture', model='fixture')
            await runtime.core.retire(other['agent_id'])
            with pytest.raises(B1Error, match='TARGET_NAME_AMBIGUOUS') as error:
                await runtime.core.write_message('send', **send_args(runtime, people))
            assert error.value.details['candidate_count'] == 2
            result = await runtime.core.write_message('send', **send_args(runtime, people,
                request_id='explicit-id', recipients=[{'target_agent_id': people[1]['agent_id']}]))
            assert result['recipients'][0]['agent_id'] == people[1]['agent_id']
    asyncio.run(run())


def test_response_loss_reopen_replays_same_message_without_target_reresolution(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            args = send_args(runtime, people)
            first = await runtime.core.write_message('send', **args)
            await runtime.core.retire(people[1]['agent_id'])
            reopened = await FreshRuntime.open(runtime.root / 'config.json')
            try:
                assert await reopened.core.write_message('send', **args) == first
                async with reopened.sessions() as session:
                    assert len((await session.execute(select(Message))).scalars().all()) == 1
                    assert len((await session.execute(select(MessageRecipient))).scalars().all()) == 1
            finally:
                await reopened.close()
    asyncio.run(run())


def test_passive_open_does_not_repair_missing_directory(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, _):
            async with runtime.engine.begin() as connection:
                await connection.execute(text('DROP TABLE b1_directory_meta'))
            with pytest.raises(B1Error, match='NOT_PREPARED'):
                await FreshRuntime.open(runtime.root / 'config.json')
            async with runtime.sessions() as session:
                assert (await session.execute(text(
                    "SELECT name FROM sqlite_master WHERE name='b1_directory_meta'"))).first() is None
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [('sender_name', 'Beta'), ('actor_agent_id', 2),
                                       ('project_key', '/old'), ('auth', None),
                                       ('ack_required', 1)])
def test_raw_boundary_rejects_actor_override_and_coercion(tmp_path, field, value):
    async def run():
        async with state(tmp_path) as (runtime, people):
            before = await snapshot(runtime)
            async with Client(build_fresh_server(core=runtime.core)) as client:
                result = await client.call_tool('send_message', dict(send_args(runtime, people),
                    **{field: value}), raise_on_error=False)
                assert result.is_error
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_explicit_cli_http_process_config_health_send_peek_and_shutdown(tmp_path):
    import socket
    import subprocess
    import sys
    import time

    async def run():
        async with state(tmp_path) as (runtime, people):
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            assert port not in {8765, 8770, 8781, 8791}
            log_path = tmp_path / 'fresh-http.log'
            with log_path.open('w') as log:
                process = subprocess.Popen([sys.executable, '-m', 'agentstack_mail.fresh_cli',
                    'serve', '--config', str(runtime.root / 'config.json'), '--port', str(port)],
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        assert process.poll() is None, log_path.read_text()
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                                break
                        except OSError:
                            await asyncio.sleep(0.1)
                    else:
                        pytest.fail('isolated HTTP startup timed out: ' + log_path.read_text())
                    async with Client(f'http://127.0.0.1:{port}/mcp') as client:
                        health = (await client.call_tool('health_check', {})).data
                        assert health['mail_instance_id'] == runtime.config['instance_id']
                        sent = (await client.call_tool('send_message', send_args(runtime, people))).data
                        inbox = (await client.call_tool('fetch_inbox', dict(
                            expected_server_instance_id=health['mail_instance_id'], target_name='Beta'))).data
                        assert [(m['id'], m['body_md']) for m in inbox] == [(sent['message_id'], 'fixture body')]
                        assert sent['delivery_status'] == 'notification_pending'
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    assert process.poll() is not None
    asyncio.run(run())
