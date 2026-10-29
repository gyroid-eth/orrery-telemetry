"""B1 acceptance tests: real legacy ORM, explicit candidate, no live settings.

Send/reply test decisions only; final writer/proxy/consumer wiring is B4/B5.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastmcp import Client
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from agentstack_mail.models import Agent, MailInstance, Message, MessageRecipient, Project
from agentstack_mail.storage_origin_core import StorageOriginCore
from agentstack_mail.storage_origin_contract import B1Error, POLICY, Auth
from agentstack_mail.storage_origin_candidate import build_candidate_server

I = 'fixture-mail-instance'
TOKENS = {n: f'fixture-owner-{n}-secret-unique' for n in (11, 22, 33, 44)}


def auth(n=11):
    return {'actor_agent_id': n, 'registration_token': TOKENS[n],
            'expected_credential_generation': 0}


def test_host_retained_target_is_transaction_scoped_without_retired_mail_bypass(state_factory):
    async def run():
        async with state_factory() as (core, sessions, engine, root):
            async with core.owner_transaction(I,auth()) as owner_context:
                first=await core.host_retire(owner_context,auth(22),22)
                assert first['changed']
                assert not (await core.host_retire(owner_context,auth(22),22))['changed']
                with pytest.raises(B1Error,match='ACTOR_RETIRED'):
                    core._verify(auth(22),owner_context.records,owner_context.keys)
                assert core.host_target(owner_context,auth(22),22).id==22
                for proof,aid in [(auth(22),33),(dict(auth(22),expected_credential_generation=9),22),
                                  (dict(auth(22),registration_token='wrong-proof'),22)]:
                    with pytest.raises(B1Error):core.host_target(owner_context,proof,aid)
                with pytest.raises(B1Error,match='FRESH_OPERATOR_RETIRE_FORBIDDEN'):
                    await core.host_retire(owner_context,auth(),11)
            with pytest.raises(B1Error,match='OWNER_CONTEXT_EXPIRED'):
                core.host_target(owner_context,auth(22),22)
            async with core.owner_transaction(I,auth()) as current:
                assert core.host_target(current,auth(22),22).retired_at is not None
            with pytest.raises(B1Error,match='ACTOR_RETIRED'):
                async with core.owner_transaction(I,auth(22)):pass
    asyncio.run(run())


@asynccontextmanager
async def _state(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "candidate.sqlite"}',
                                 connect_args={'timeout': 5})
    async with engine.begin() as c:
        await c.execute(text('PRAGMA journal_mode=WAL'))
        await c.run_sync(SQLModel.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        s.add_all([Project(id=n, slug=f'origin-{n}', human_key=f'/fixture/origin-{n}')
                   for n in (1, 2, 3)])
        s.add(MailInstance(instance_id=I))
        for n, origin, name in [(11, 1, 'Alpha'), (22, 2, 'Beta'),
                                 (33, 3, 'Gamma'), (44, 2, 'Delta')]:
            s.add(Agent(id=n, project_id=origin, name=name, program='codex-app',
                        model='fixture', contact_policy='open', registration_token=TOKENS[n]))
        await s.commit()
        s.add_all([Message(id=900, project_id=3, sender_id=33, thread_id='parent-thread',
                          topic='topic-a', subject='old unread', body_md='old body'),
                   Message(id=901, project_id=2, sender_id=22, thread_id='other-thread',
                          topic='topic-b', subject='other unread', body_md='other body')])
        await s.commit()
        s.add_all([MessageRecipient(message_id=900, agent_id=11),
                   MessageRecipient(message_id=900, agent_id=22),
                   MessageRecipient(message_id=901, agent_id=44)])
        await s.commit()
    core = StorageOriginCore(sessions)
    await core.prepare(default_project_id=2)
    try:
        yield core, sessions, engine, tmp_path
    finally:
        await engine.dispose()


@pytest.fixture
def state_factory(tmp_path):
    """Open/close the async case inside each test's single event loop."""
    return lambda: _state(tmp_path)


async def snapshot(sessions):
    async with sessions() as s:
        return {t: (await s.execute(text(f'SELECT * FROM "{t}" ORDER BY 1,2'))).all()
                for t in (await s.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))).scalars()
                if not t.startswith('sqlite_')}


async def owner(core, operation, **changes):
    args = dict(expected_server_instance_id=I, auth=auth(), target_agent_id=11,
                message_id=900, request_id=f'request-{operation}')
    args.update(changes)
    return await core.owner(operation, **args)


@pytest.mark.parametrize('operation', ['send', 'reply', 'read', 'ack', 'reconnect'])
@pytest.mark.parametrize('bad', ['missing', 'wrong'])
def test_owner_entry_matrix(state_factory, operation, bad):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            before = await snapshot(sessions)
            proof = None if bad == 'missing' else dict(auth(), registration_token='wrong-fixture-secret')
            with pytest.raises(B1Error) as e:
                await owner(core, operation, auth=proof)
            assert e.value.code in {'AUTH_PROOF_INCOMPLETE', 'OWNER_CREDENTIAL_MISMATCH'}
            assert await snapshot(sessions) == before

    asyncio.run(run())


@pytest.mark.parametrize('operation', ['read', 'ack', 'consume'])
def test_target_and_recipient_are_not_actor(state_factory, operation):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            with pytest.raises(B1Error, match='TARGET_OWNER_MISMATCH'):
                await owner(core, operation, target_agent_id=22)
            with pytest.raises(B1Error, match='NOT_A_RECIPIENT'):
                await owner(core, operation, message_id=901, request_id='not-recipient')
            async with sessions() as s:
                row = await s.get(MessageRecipient, (900, 22))
                assert row.read_ts is None and row.ack_ts is None

    asyncio.run(run())


def test_read_ack_persist_first_timestamp_and_other_pairs(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            read = await owner(core, 'read')
            async with sessions() as s:
                row = await s.get(MessageRecipient, (900, 11))
                assert row.read_ts is not None and row.ack_ts is None
                first = row.read_ts
            ack = await owner(core, 'ack')
            await asyncio.sleep(.01)
            assert await owner(core, 'read') == read
            assert await owner(core, 'ack') == ack
            await owner(core, 'ack', request_id='another-ack')
            async with sessions() as s:
                row = await s.get(MessageRecipient, (900, 11))
                peer = await s.get(MessageRecipient, (900, 22))
                assert row.read_ts == first and row.ack_ts is not None
                assert row.ack_ts.isoformat() == ack['ack_ts']
                assert peer.read_ts is None and peer.ack_ts is None

    asyncio.run(run())


def test_peek_reads_real_all_origin_rows_without_effect(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, root = state
            (root / 'signal').write_text('pending')
            (root / 'archive').write_text('old archive')
            before = await snapshot(sessions)
            result = await core.peek(expected_server_instance_id=I, target_agent_id=11,
                                     topic='topic-a', limit=1)
            assert [(r['id'], r['body_md'], r['sender_id'], r['storage_project_id']) for r in result] == [
                (900, 'old body', 33, 3)]
            assert await core.peek(expected_server_instance_id=I, target_agent_id=11,
                                   auth=auth(22)) == result
            assert await snapshot(sessions) == before
            assert (root / 'signal').read_text() == 'pending'
            assert (root / 'archive').read_text() == 'old archive'

    asyncio.run(run())


def test_actual_whois_returns_self_peer_and_retired_without_effect(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, root = state
            await core.retire(33)
            (root / 'signal').write_text('pending')
            (root / 'archive').write_text('old archive')
            before = await snapshot(sessions)
            async with Client(build_candidate_server(core)) as client:
                for selector, proof, expected in [
                    ({'target_agent_id': 11}, None,
                     {'target_agent_id': 11, 'name': 'Alpha', 'state': 'active', 'source_label': 'origin-1'}),
                    ({'target_name': 'Beta'}, auth(),
                     {'target_agent_id': 22, 'name': 'Beta', 'state': 'active', 'source_label': 'origin-2'}),
                    ({'target_agent_id': 33, 'expected_target_name': 'Gamma'}, auth(22),
                     {'target_agent_id': 33, 'name': 'Gamma', 'state': 'retired', 'source_label': 'origin-3'}),
                ]:
                    args = dict(expected_server_instance_id=I, **selector)
                    if proof is not None:
                        args['auth'] = proof
                    result = await client.call_tool('whois', args)
                    assert result.data == dict(expected, mail_instance_id=I)
            assert await snapshot(sessions) == before
            assert (root / 'signal').read_text() == 'pending'
            assert (root / 'archive').read_text() == 'old archive'

    asyncio.run(run())


@pytest.mark.parametrize('change,code', [
    ({'auth': dict(auth(), registration_token='wrong-whois-secret')}, 'OWNER_CREDENTIAL_MISMATCH'),
    ({'expected_server_instance_id': 'other-instance'}, 'INSTANCE_MISMATCH'),
    ({'expected_target_name': 'old-name'}, 'TARGET_NAME_STALE'),
    ({'target_agent_id': 999}, 'TARGET_NOT_FOUND'),
])
def test_actual_whois_rejects_wrong_proof_instance_and_target(state_factory, change, code):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            before = await snapshot(sessions)
            async with Client(build_candidate_server(core)) as client:
                args = {'expected_server_instance_id': I, 'target_agent_id': 22, 'auth': auth(), **change}
                result = await client.call_tool('whois', args, raise_on_error=False)
                assert result.is_error and code in str(result)
            assert await snapshot(sessions) == before

    asyncio.run(run())


def test_actual_whois_ambiguity_keeps_retired_candidate_and_explicit_id(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            duplicate = await core.register_new(request_id='whois-duplicate', name='Alpha',
                token='whois-duplicate-fresh-secret', program='fixture', model='fixture')
            await core.retire(duplicate['agent_id'])
            before = await snapshot(sessions)
            async with Client(build_candidate_server(core)) as client:
                result = await client.call_tool('whois', {'expected_server_instance_id': I,
                    'target_name': 'Alpha'}, raise_on_error=False)
                assert result.is_error
                error = json.loads(result.content[0].text)
                assert error['code'] == 'TARGET_NAME_AMBIGUOUS' and error['candidate_count'] == 2
                assert {(x['target_agent_id'], x['state']) for x in error['candidates']} == {
                    (11, 'active'), (duplicate['agent_id'], 'retired')}
                explicit = await client.call_tool('whois', {'expected_server_instance_id': I,
                    'target_agent_id': 11})
                assert explicit.data == {'target_agent_id': 11, 'name': 'Alpha', 'state': 'active',
                                         'source_label': 'origin-1', 'mail_instance_id': I}
            assert await snapshot(sessions) == before

    asyncio.run(run())


def test_send_reply_decisions_keep_actor_origins_kinds(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            before = await snapshot(sessions)
            targets = [{'target_agent_id': 22, 'kind': 'to'},
                       {'target_agent_id': 33, 'kind': 'cc'},
                       {'target_agent_id': 44, 'kind': 'bcc'}]
            result = await owner(core, 'send', recipients=targets)
            assert result == {'mail_instance_id': I, 'directory_revision': 1,
                              'actor_agent_id': 11, 'actor_storage_project_id': 1,
                              'storage_project_id': 1, 'parent_message_id': None, 'thread_id': None,
                              'recipients': [{'agent_id': 22, 'storage_project_id': 2, 'kind': 'to'},
                                             {'agent_id': 33, 'storage_project_id': 3, 'kind': 'cc'},
                                             {'agent_id': 44, 'storage_project_id': 2, 'kind': 'bcc'}],
                              'decision_only': True}
            reply = await owner(core, 'reply')
            assert reply == dict(result, storage_project_id=3, parent_message_id=900,
                                 thread_id='parent-thread',
                                 recipients=[{'agent_id': 33, 'storage_project_id': 3, 'kind': 'to'}])
            explicit = await owner(core, 'reply', recipients=targets)
            assert explicit == dict(result, storage_project_id=3, parent_message_id=900,
                                    thread_id='parent-thread')
            assert await snapshot(sessions) == before  # a decision is not a committed send
            await core.retire(44)
            after_management = await snapshot(sessions)
            current = await owner(core, 'send', recipients=[targets[0]])
            assert current == dict(result, directory_revision=2, recipients=[result['recipients'][0]])
            assert await snapshot(sessions) == after_management

    asyncio.run(run())


def test_name_candidates_include_retired_and_all_origins(state_factory):
    async def run():
        async with state_factory() as state:
            core, _, _, _ = state
            extra = await core.register_new(request_id='duplicate', name='Alpha',
                                            token='fresh-duplicate-owner-secret', program='fixture', model='fixture')
            with pytest.raises(B1Error, match='TARGET_NAME_AMBIGUOUS') as e:
                await owner(core, 'read', target_agent_id=None, target_name='Alpha')
            assert {x['target_agent_id'] for x in e.value.details['candidates']} == {11, extra['agent_id']}
            await core.retire(extra['agent_id'])
            with pytest.raises(B1Error, match='TARGET_NAME_AMBIGUOUS'):
                await owner(core, 'read', target_agent_id=None, target_name='Alpha', request_id='retired-one')
            await owner(core, 'read', request_id='explicit-id')
            await core.retire(33)
            with pytest.raises(B1Error, match='TARGET_RETIRED'):
                await owner(core, 'send', recipients=[{'target_name': 'Gamma', 'kind': 'to'}])

    asyncio.run(run())


def test_normalized_candidates_do_not_prioritize_exact(state_factory):
    async def run():
        async with state_factory() as state:
            core, _, _, _ = state
            a = await core.register_new(request_id='name-1', name='Blue-Lake',
                                        token='fresh-owner-one-secret-unique', program='fixture', model='fixture')
            b = await core.register_new(request_id='name-2', name='BlueLake',
                                        token='fresh-owner-two-secret-unique', program='fixture', model='fixture')
            found = await core.directory(expected_server_instance_id=I, target_name='Blue-Lake')
            assert found['candidate_count'] == 2
            assert {x['target_agent_id'] for x in found['candidates']} == {a['agent_id'], b['agent_id']}
            unique = await core.directory(expected_server_instance_id=I, target_name='Beta')
            assert unique['candidate_count'] == 1

    asyncio.run(run())


@pytest.mark.parametrize('recipients', [
    [{'target_name': 'Beta', 'kind': 'to'}, {'target_name': 'Alpha', 'kind': 'to'}],
    [{'target_name': 'Alpha', 'kind': 'to'}, {'target_name': 'Beta', 'kind': 'to'}],
    [{'target_name': 'Beta', 'kind': 'to'}, {'target_name': 'Alpha', 'kind': 'cc'}],
    [{'target_name': 'Beta', 'kind': 'to'}, {'target_name': 'Gamma', 'kind': 'bcc'}],
    [{'target_name': 'Gamma', 'kind': 'bcc'}, {'target_name': 'Beta', 'kind': 'to'}],
    [{'target_name': 'Alpha', 'kind': 'cc'}, {'target_name': 'Beta', 'kind': 'to'}],
])
def test_mixed_recipients_reject_atomically(state_factory, recipients):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            await core.register_new(request_id='dup', name='Alpha', token='duplicate-owner-secret-fresh',
                                    program='fixture', model='fixture')
            await core.retire(33)
            before = await snapshot(sessions)
            with pytest.raises(B1Error) as e:
                await owner(core, 'send', recipients=recipients)
            assert e.value.code in {'TARGET_NAME_AMBIGUOUS', 'TARGET_RETIRED'}
            # decision-only send does not persist any outcome; final writer belongs to B4.
            assert await snapshot(sessions) == before

    asyncio.run(run())


def test_shared_key_partial_recovery_never_revives(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            async with sessions() as s:
                b = await s.get(Agent, 22); b.registration_token = TOKENS[11]; await s.commit()
            await core.prepare(default_project_id=2)
            with pytest.raises(B1Error, match='CREDENTIAL_BURNED'):
                await owner(core, 'read')
            await core.recover(11, expected_generation=0, new_token='fresh-owner-alpha-recovered')
            with pytest.raises(B1Error, match='CREDENTIAL_BURNED'):
                await owner(core, 'read', auth=dict(auth(22), registration_token=TOKENS[11]),
                            target_agent_id=22)
            await core.recover(22, expected_generation=0, new_token='fresh-owner-beta-recovered')
            restarted = StorageOriginCore(sessions)
            assert await owner(restarted, 'reconnect', auth=dict(auth(), registration_token='fresh-owner-alpha-recovered',
                                                               expected_credential_generation=1))
            for n in (11, 22):
                with pytest.raises(B1Error):
                    await owner(restarted, 'reconnect', auth=dict(auth(n), registration_token=TOKENS[11],
                                                                 expected_credential_generation=1))
            with pytest.raises(B1Error, match='CREDENTIAL_UNAVAILABLE'):
                await restarted.recover(22, expected_generation=1, new_token=TOKENS[11])

    asyncio.run(run())


@pytest.mark.parametrize('change', ['token', 'name', 'generation', 'retired', 'origin', 'insert', 'index'])
def test_other_row_or_index_changes_fail_closed(state_factory, change):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            async with sessions() as s:
                b = await s.get(Agent, 22)
                if change == 'token': b.registration_token = TOKENS[11]
                elif change == 'name': b.name = 'Alpha'
                elif change == 'generation': b.credential_generation = 1
                elif change == 'retired': b.retired_at = datetime.now(timezone.utc)
                elif change == 'origin': b.project_id = 3
                elif change == 'insert':
                    s.add(Agent(project_id=2, name='NewAgent', program='fixture', model='fixture',
                                registration_token='new-untracked-owner-secret'))
                else: await s.execute(text("UPDATE b1_credential_keys SET state='burned'"))
                await s.commit()
            with pytest.raises(B1Error, match='DIRECTORY_STALE'):
                await owner(core, 'reconnect')

    asyncio.run(run())


def test_reprepare_cannot_recycle_name_or_id(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            async with sessions() as s:
                (await s.get(Agent, 22)).name = 'Reassigned'; await s.commit()
            with pytest.raises(B1Error, match='IDENTITY_HISTORY_MISMATCH'):
                await core.prepare(default_project_id=2)

    asyncio.run(run())


def test_refusal_survives_lost_response_and_registration(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            request = dict(target_agent_id=None, target_name='Absent', request_id='lost-refusal')
            with pytest.raises(B1Error, match='TARGET_NAME_NOT_FOUND'):
                await owner(core, 'read', **request)
            await core.register_new(request_id='now-present', name='Absent', token='new-absent-owner-secret-unique',
                                    program='fixture', model='fixture')
            with pytest.raises(B1Error, match='TARGET_NAME_NOT_FOUND'):
                await owner(core, 'read', **request)
            async with sessions() as s:
                assert (await s.get(MessageRecipient, (900, 11))).read_ts is None

    asyncio.run(run())


def test_accepted_target_retry_never_re_resolves(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            args = dict(target_agent_id=None, target_name='Alpha', request_id='fixed-target')
            original = await owner(core, 'read', **args)
            await core.register_new(request_id='dup', name='Alpha', token='new-alpha-owner-secret-unique',
                                    program='fixture', model='fixture')
            assert await owner(core, 'read', **args) == original
            with pytest.raises(B1Error, match='REQUEST_INTENT_CONFLICT'):
                await owner(core, 'read', **dict(args, target_name='Beta'))
            async with sessions() as s:
                await s.execute(delete(MessageRecipient).where(MessageRecipient.agent_id == 11))
                await s.commit()
            with pytest.raises(B1Error, match='RESULT_REFERENCE_MISSING'):
                await owner(core, 'read', **args)

    asyncio.run(run())


def test_intent_removal_is_not_a_fresh_request(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            await owner(core, 'read')
            async with sessions() as s:
                await s.execute(text('DELETE FROM b1_operation_intents')); await s.commit()
            with pytest.raises(B1Error, match='INTENT_STORE_STALE'):
                await owner(core, 'read')

    asyncio.run(run())


def test_cursor_binds_query_instance_revision(state_factory):
    async def run():
        async with state_factory() as state:
            core, _, _, _ = state
            for n in range(3):
                # different punctuation gives same sanitized key without violating per-origin UNIQUE
                await core.register_new(request_id=f'page-{n}', name='Blue' + '-' * (n+1) + 'Lake',
                                        token=f'fresh-page-owner-{n}-secret-unique', program='fixture', model='fixture')
            page = await core.directory(expected_server_instance_id=I, target_name='BlueLake', limit=1)
            cursor = page['next_cursor']; assert cursor and page['candidate_count'] == 3
            with pytest.raises(B1Error, match='CURSOR_MISMATCH'):
                await core.directory(expected_server_instance_id=I, target_name='Beta', cursor=cursor)
            ids = {page['candidates'][0]['target_agent_id']}
            while cursor:
                page = await core.directory(expected_server_instance_id=I, target_name='BlueLake', limit=1, cursor=cursor)
                ids.update(c['target_agent_id'] for c in page['candidates']); cursor=page['next_cursor']
            assert len(ids) == 3
            old = await core.directory(expected_server_instance_id=I, target_name='BlueLake', limit=1)
            await core.retire(33)
            with pytest.raises(B1Error, match='CURSOR_MISMATCH'):
                await core.directory(expected_server_instance_id=I, target_name='BlueLake', cursor=old['next_cursor'])

    asyncio.run(run())


def test_upgrade_receiptless_v1_resolves_same_mail_id(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            before = await snapshot(sessions)
            v1 = {'external_id': 'codex:fixture-session', 'agent_id': None, 'agent_name': 'Alpha',
                  'project_key': '/fixture/origin-1', 'program': 'codex-app'}
            receipt = await core.resolve_v1(v1, TOKENS[11])
            assert receipt == {'external_id': 'codex:fixture-session', 'mail_instance_id': I,
                               'mail_agent_id': 11, 'credential_generation': 0,
                               'owner_storage_project_id': 1, 'agent_name': 'Alpha'}
            assert await snapshot(sessions) == before
            with pytest.raises(B1Error, match='NEEDS_RESOLUTION'):
                await core.resolve_v1(v1, None)

    asyncio.run(run())


def test_v1_resolve_requires_prepared_history_without_classifying_owner(state_factory):
    async def run():
        async with state_factory() as state:
            from agentstack_mail.storage_origin_directory import metadata
            core, sessions, engine, _ = state
            v1 = {'external_id': 'codex:fixture-session', 'agent_id': None, 'agent_name': 'Alpha',
                  'project_key': '/fixture/origin-1', 'program': 'codex-app'}
            async with engine.begin() as c:
                await c.run_sync(metadata.drop_all)
            before = await snapshot(sessions)
            with pytest.raises(B1Error, match='^NOT_PREPARED$'):
                await core.resolve_v1(v1, TOKENS[11])
            assert await snapshot(sessions) == before  # no implicit DDL/backfill/recovery
            await core.prepare(default_project_id=2)
            prepared = await snapshot(sessions)
            receipt = await core.resolve_v1(v1, TOKENS[11])
            assert receipt == {'external_id': 'codex:fixture-session', 'mail_instance_id': I,
                'mail_agent_id': 11, 'credential_generation': 0,
                'owner_storage_project_id': 1, 'agent_name': 'Alpha'}
            assert await snapshot(sessions) == prepared
            assert {k: v for k, v in prepared.items() if not k.startswith('b1_')} == before

    asyncio.run(run())


@pytest.mark.parametrize('column,code', [('registry_digest', 'DIRECTORY_STALE'),
                                       ('intent_digest', 'INTENT_STORE_STALE')])
def test_v1_index_integrity_error_stops_phase_without_classifying_owner(state_factory, column, code):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            async with sessions() as s:
                await s.execute(text(f"UPDATE b1_directory_meta SET {column} = 'damaged'"))
                await s.commit()
            before = await snapshot(sessions)
            v1 = {'external_id': 'codex:fixture-session', 'agent_name': 'Alpha',
                  'project_key': '/fixture/origin-1', 'program': 'codex-app'}
            with pytest.raises(B1Error, match=f'^{code}$'):
                await core.resolve_v1(v1, TOKENS[11])
            assert await snapshot(sessions) == before

    asyncio.run(run())


def test_consume_validates_before_backend_not_ready(state_factory):
    async def run():
        async with state_factory() as state:
            core, _, _, _ = state
            with pytest.raises(B1Error, match='OWNER_CREDENTIAL_MISMATCH'):
                await owner(core, 'consume', auth=dict(auth(), registration_token='bad'))
            with pytest.raises(B1Error, match='NOTIFICATION_CONSUMPTION_NOT_READY'):
                await owner(core, 'consume')

    asyncio.run(run())


def test_actual_candidate_wire_presence_types_and_secret_redaction(state_factory, caplog):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            server = build_candidate_server(core)
            base = {'expected_server_instance_id': I, 'target_agent_id': 11}
            async with Client(server) as client:
                good = await client.call_tool('fetch_inbox', base)
                assert good.data[0]['id'] == 900
                for proof in [None, {}, {'actor_agent_id': 11}, dict(auth(), actor_agent_id=True),
                              dict(auth(), actor_agent_id='11'), dict(auth(), expected_credential_generation=True),
                              dict(auth(), registration_token='')]:
                    args = dict(base, auth=proof)
                    result = await client.call_tool('fetch_inbox', args, raise_on_error=False)
                    assert result.is_error
                result = await client.call_tool('fetch_inbox', dict(base, sender_token='synthetic-secret-canary'),
                                                raise_on_error=False)
                assert result.is_error
                assert 'synthetic-secret-canary' not in str(result) + caplog.text
                health = await client.call_tool('health_check', {})
                assert health.data['mail_instance_id'] == I
                bad = await client.call_tool('health_check', {'expected_server_instance_id': None}, raise_on_error=False)
                assert bad.is_error
            before = await snapshot(sessions)
            with pytest.raises(B1Error, match='INSTANCE_MISMATCH'):
                await core.peek(**dict(base, expected_server_instance_id='other-instance'))
            assert await snapshot(sessions) == before

    asyncio.run(run())


def test_policy_covers_every_legacy_tool():
    from agentstack_mail.contract import COMPATIBILITY_TOOLS
    assert set(POLICY) == COMPATIBILITY_TOOLS
    assert not any(value == 'allow' for value in POLICY.values())


def test_owner_context_cannot_escape_transaction_or_session(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            async with core.owner_transaction(I, auth()) as ctx:
                ctx.assert_live(ctx.session)
                async with sessions() as different:
                    with pytest.raises(B1Error, match='OWNER_CONTEXT_EXPIRED'):
                        ctx.assert_live(different)
                assert TOKENS[11] not in repr(ctx)
            with pytest.raises(B1Error, match='OWNER_CONTEXT_EXPIRED'):
                ctx.assert_live(ctx.session)

    asyncio.run(run())


def test_resolution_prelock_interleaving(state_factory, monkeypatch):
    async def run():
        async with state_factory() as state:
            """If a mutant resolves before lock, force a healthy registration to commit in that gap."""
            core, sessions, engine, _ = state
            trace = []
            register_attempted = asyncio.Event()
            original = core._candidates
            registration = None

            @event.listens_for(engine.sync_engine, 'before_cursor_execute')
            def record_begin(connection, cursor, statement, parameters, context, executemany):
                if statement == 'BEGIN IMMEDIATE':
                    who = asyncio.current_task().get_name()
                    trace.append(('lock', who))
                    if who == 'b1-register': register_attempted.set()

            async def register():
                result = await core.register_new(request_id='race-register', name='Alpha',
                    token='race-register-owner-secret-fresh', program='fixture', model='fixture')
                trace.append(('commit', 'b1-register'))
                return result

            async def observe(s, records, name):
                nonlocal registration
                result = await original(s, records, name)
                if name == 'Alpha' and registration is None:
                    trace.append(('resolved', 'b1-owner'))
                    registration = asyncio.create_task(register(), name='b1-register')
                    await asyncio.wait_for(register_attempted.wait(), 2)
                    if ('lock', 'b1-owner') not in trace:
                        # Only the prelock mutant can enter here. Its read snapshot is WAL;
                        # the normal writer updates Agent, credential index AND digest.
                        await asyncio.wait_for(asyncio.shield(registration), 3)
                        trace.append(('prelock_registration_committed', 'b1-register'))
                    else:
                        # Normal resolver holds the real SQLite write lock; do not wait
                        # for the other writer's lock before releasing this one.
                        await asyncio.sleep(.03)
                        assert not registration.done()
                return result

            monkeypatch.setattr(core, '_candidates', observe)
            async def request():
                try:
                    result = await owner(core, 'read', target_agent_id=None, target_name='Alpha', request_id='race-read')
                    trace.append(('commit', 'b1-owner'))
                    return result
                except B1Error as e:
                    trace.append(('refused', e.code))
                    return e
            try:
                outcome = await asyncio.create_task(request(), name='b1-owner')
                if registration: await asyncio.wait_for(registration, 5)
                print('B1_RACE_TRACE=' + json.dumps(trace))
                if ('prelock_registration_committed', 'b1-register') in trace:
                    assert isinstance(outcome, B1Error) and outcome.code == 'TARGET_NAME_AMBIGUOUS'
                else:
                    assert isinstance(outcome, dict) and outcome['agent_id'] == 11
                    assert trace.index(('commit', 'b1-owner')) < trace.index(('commit', 'b1-register'))
                # After the healthy registration both source and registry are prepared.
                with pytest.raises(B1Error, match='TARGET_NAME_AMBIGUOUS'):
                    await owner(core, 'read', target_agent_id=None, target_name='Alpha', request_id='later-race-read')
            finally:
                if registration and not registration.done():
                    registration.cancel()
                    await asyncio.gather(registration, return_exceptions=True)
                event.remove(engine.sync_engine, 'before_cursor_execute', record_begin)

    asyncio.run(run())


@pytest.mark.parametrize('change', ['recover', 'retire'])
def test_owner_write_linearizes_before_management(state_factory, monkeypatch, change):
    async def run():
        async with state_factory() as state:
            from agentstack_mail import storage_origin_directory as d
            core, sessions, _, _ = state
            acquired, resume = asyncio.Event(), asyncio.Event()
            original = core._context
            first = True
            async def paused(s, expected):
                nonlocal first
                result = await original(s, expected)
                if first:
                    first = False; acquired.set(); await resume.wait()
                return result
            monkeypatch.setattr(core, '_context', paused)
            operation = asyncio.create_task(owner(core, 'ack'))
            await asyncio.wait_for(acquired.wait(), 2)
            management = asyncio.create_task(core.recover(11, expected_generation=0,
                new_token='linearized-fresh-owner-secret') if change == 'recover' else core.retire(11))
            await asyncio.sleep(.03)
            assert not management.done()
            resume.set()
            try:
                assert (await asyncio.wait_for(operation, 5))['ack_ts']
                await asyncio.wait_for(management, 5)
                with pytest.raises(B1Error): await owner(core, 'read', request_id='after-management')
            finally:
                resume.set()
                for task in (operation, management):
                    if not task.done(): task.cancel()
                await asyncio.gather(operation, management, return_exceptions=True)

    asyncio.run(run())


def test_registration_replay_conflict_and_parallel_claim(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            args = dict(request_id='register-once', name='Once', token='once-fresh-owner-secret-unique',
                        program='fixture', model='fixture')
            one, two = await asyncio.gather(core.register_new(**args), core.register_new(**args))
            assert one == two
            with pytest.raises(B1Error, match='REQUEST_INTENT_CONFLICT'):
                await core.register_new(**dict(args, name='Changed'))
            other = dict(args, request_id='another-registration', name='Another')
            with pytest.raises(B1Error, match='CREDENTIAL_UNAVAILABLE'):
                await core.register_new(**other)
            async with sessions() as s:
                assert len((await s.execute(select(Agent).where(Agent.name == 'Once'))).scalars().all()) == 1
                assert not (await s.execute(select(Agent).where(Agent.name == 'Another'))).scalars().all()

    asyncio.run(run())


def test_read_ack_exception_rolls_back_all_state(state_factory, monkeypatch):
    async def run():
        async with state_factory() as state:
            from agentstack_mail import storage_origin_directory as d
            core, sessions, _, _ = state
            before = await snapshot(sessions)
            async def fail_after_effect(session, **kwargs):
                raise RuntimeError('fixture interrupted before commit')
            monkeypatch.setattr(d, 'refresh', fail_after_effect)
            with pytest.raises(RuntimeError): await owner(core, 'ack')
            assert await snapshot(sessions) == before

    asyncio.run(run())


@pytest.mark.parametrize('value', ['s', '非ASCIIの資格', ' ', 'x' * 64])
def test_existing_short_and_unicode_credentials_are_byte_exact(state_factory, value):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            async with sessions() as s:
                (await s.get(Agent, 11)).registration_token=value; await s.commit()
            await core.prepare(default_project_id=2)
            assert await owner(core, 'reconnect', auth=dict(auth(), registration_token=value))
            with pytest.raises(B1Error):
                await owner(core, 'reconnect', auth=dict(auth(), registration_token=value + ' '))

    asyncio.run(run())


@pytest.mark.parametrize('operation,tool', [('send','send_message'), ('reply','reply_message'),
    ('read','mark_message_read'), ('ack','acknowledge_message'), ('reconnect','reconnect')])
@pytest.mark.parametrize('bad', ['missing','wrong'])
def test_actual_owner_tools_reject_missing_or_wrong_proof(state_factory, operation, tool, bad):
    async def run():
        async with state_factory() as state:
            core, sessions, _, _ = state
            args={'expected_server_instance_id':I,'target_agent_id':11,'message_id':900,'request_id':'wire-matrix',
                  'recipients':[{'target_agent_id':22,'kind':'to'}]}
            if operation in {'send','reply','reconnect'}: args.pop('target_agent_id')
            if operation in {'send','reconnect'}: args.pop('message_id')
            if operation not in {'send','reply'}: args.pop('recipients')
            if operation=='reconnect': args.pop('request_id')
            if bad=='wrong': args['auth']=dict(auth(),registration_token='wire-wrong-secret-canary')
            before=await snapshot(sessions)
            async with Client(build_candidate_server(core)) as c:
                result=await c.call_tool(tool,args,raise_on_error=False)
                assert result.is_error
                assert ('AUTH_PROOF_INCOMPLETE' if bad=='missing' else 'OWNER_CREDENTIAL_MISMATCH') in str(result)
                assert 'wire-wrong-secret-canary' not in str(result)
            assert await snapshot(sessions)==before

    asyncio.run(run())


def test_unprepared_health_never_creates_candidate_schema(state_factory):
    async def run():
        async with state_factory() as state:
            core, sessions, engine, _ = state
            from agentstack_mail.storage_origin_directory import metadata
            async with engine.begin() as c: await c.run_sync(metadata.drop_all)
            async with sessions() as s:
                before=(await s.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))).all()
            with pytest.raises(B1Error,match='NOT_PREPARED'): await core.health()
            async with sessions() as s:
                after=(await s.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))).all()
            assert before==after and not any(x[0].startswith('b1_') for x in after)

    asyncio.run(run())


def test_cold_default_after_candidate_import_has_no_activation_or_ddl(tmp_path):
    import os, subprocess, sys, tempfile
    from pathlib import Path
    script=r'''
import asyncio,json
from sqlalchemy import text
from fastmcp import Client
from agentstack_mail import storage_origin_candidate,storage_origin_core,storage_origin_directory
from agentstack_mail import config,db,app
from agentstack_mail.contract import COMPATIBILITY_TOOLS
async def main():
    assert not any(t.startswith('b1_') for t in storage_origin_directory.metadata.tables if t in __import__('sqlmodel').SQLModel.metadata.tables)
    await db.ensure_schema()
    async with db.get_session() as s:
        names=(await s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).scalars().all()
        assert not any(t.startswith('b1_') for t in names)
    async with Client(app.build_mcp_server()) as c:
        tools=await c.list_tools()
        assert {t.name for t in tools}==COMPATIBILITY_TOOLS
        for t in tools:
            assert 'auth' not in t.inputSchema.get('properties',{})
        result=await c.call_tool('consume_notification',{},raise_on_error=False)
        assert result.is_error
    print('DEFAULT_BOUNDARY_OK')
asyncio.run(main())
'''
    env=dict(os.environ)
    home=tmp_path/'cold-home';home.mkdir()
    env.update(HOME=str(home),AGENTSTACK_MAIL_DATABASE_URL=f'sqlite+aiosqlite:///{tmp_path / "cold.sqlite"}',
               AGENTSTACK_MAIL_STORAGE_ROOT=str(tmp_path/'archive'),
               AGENTSTACK_MAIL_LOG_RICH_ENABLED='false')
    # Isolated management socket, with a short path below the Unix limit.
    with tempfile.TemporaryDirectory(prefix='o213-b1-', dir='/private/tmp') as management:
        env['AGENTSTACK_MAIL_MANAGEMENT_SOCKET']=str(Path(management)/'mail.sock')
        result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'DEFAULT_BOUNDARY_OK' in result.stdout


@pytest.mark.parametrize('proof', [dict(auth(),actor_agent_id=True),dict(auth(),actor_agent_id='11'),
    dict(auth(),expected_credential_generation=-1),dict(auth(),expected_credential_generation=1),
    dict(auth(),registration_token='x'*65),dict(auth(),registration_token='\ud800'),
    dict(auth(22),registration_token=TOKENS[11])])
def test_strict_owner_proof_and_generation(state_factory,proof):
    async def run():
        async with state_factory() as state:
            core,sessions,_,_=state
            before=await snapshot(sessions)
            with pytest.raises(B1Error): await owner(core,'reconnect',auth=proof)
            assert await snapshot(sessions)==before

    asyncio.run(run())


def test_peek_wrong_complete_proof_never_becomes_anonymous(state_factory,caplog):
    async def run():
        async with state_factory() as state:
            core,sessions,_,_=state
            before=await snapshot(sessions)
            args=dict(expected_server_instance_id=I,target_agent_id=11,
                      auth=dict(auth(),registration_token='peek-secret-canary-wrong'))
            with pytest.raises(B1Error,match='OWNER_CREDENTIAL_MISMATCH'):
                await core.peek(**args)
            async with Client(build_candidate_server(core)) as c:
                result=await c.call_tool('fetch_inbox',args,raise_on_error=False)
                assert result.is_error
                assert 'peek-secret-canary-wrong' not in str(result)+caplog.text
            assert await snapshot(sessions)==before

    asyncio.run(run())


def test_management_commits_before_owner_validates(state_factory,monkeypatch):
    async def run():
        async with state_factory() as state:
            from agentstack_mail import storage_origin_directory as d
            core,sessions,_,_=state
            ready,resume=asyncio.Event(),asyncio.Event()
            original=d.refresh
            async def paused(s,**kwargs):
                await original(s,**kwargs)
                if asyncio.current_task().get_name()=='b1-recovery':
                    ready.set();await resume.wait()
            monkeypatch.setattr(d,'refresh',paused)
            recovery=asyncio.create_task(core.recover(11,expected_generation=0,
                new_token='recovery-first-secret-unique'),name='b1-recovery')
            await asyncio.wait_for(ready.wait(),2)
            operation=asyncio.create_task(owner(core,'ack'))
            await asyncio.sleep(.03)
            assert not operation.done()
            resume.set()
            try:
                await asyncio.wait_for(recovery,5)
                with pytest.raises(B1Error,match='OWNER_CREDENTIAL_MISMATCH'):
                    await asyncio.wait_for(operation,5)
                async with sessions() as s:
                    pair=await s.get(MessageRecipient,(900,11))
                    assert pair.read_ts is None and pair.ack_ts is None
            finally:
                resume.set()
                for task in (recovery,operation):
                    if not task.done():task.cancel()
                await asyncio.gather(recovery,operation,return_exceptions=True)

    asyncio.run(run())


def test_register_failure_has_no_orphan_agent_key_or_intent(state_factory,monkeypatch):
    async def run():
        async with state_factory() as state:
            from agentstack_mail import storage_origin_directory as d
            core,sessions,_,_=state
            before=await snapshot(sessions)
            async def fail(*args,**kwargs):raise RuntimeError('fixture registry failure')
            monkeypatch.setattr(d,'refresh',fail)
            with pytest.raises(RuntimeError):
                await core.register_new(request_id='fails',name='Orphan',token='orphan-owner-secret-fresh',
                                        program='fixture',model='fixture')
            assert await snapshot(sessions)==before

    asyncio.run(run())


def test_reprepare_cannot_erase_private_credential_history(state_factory):
    async def run():
        async with state_factory() as state:
            from agentstack_mail.storage_origin_contract import fingerprint
            core,sessions,_,_=state
            async with sessions() as s:
                (await s.get(Agent,22)).registration_token=TOKENS[11];await s.commit()
            await core.prepare(default_project_id=2)
            await core.recover(11,expected_generation=0,new_token='new-alpha-after-shared-history')
            async with sessions() as s:
                await s.execute(text("UPDATE b1_credential_keys SET state='live',owner_id=22,members='[22]' WHERE fingerprint=:fp"),
                                {'fp':fingerprint(TOKENS[11])})
                await s.commit()
            with pytest.raises(B1Error,match='DIRECTORY_STALE'):
                await core.prepare(default_project_id=2)

    asyncio.run(run())


def test_missing_result_message_is_not_reexecuted(state_factory):
    async def run():
        async with state_factory() as state:
            core,sessions,_,_=state
            await owner(core,'read')
            async with sessions() as s:
                await s.execute(delete(Message).where(Message.id==900));await s.commit()
            with pytest.raises(B1Error,match='RESULT_REFERENCE_MISSING'):
                await owner(core,'read')

    asyncio.run(run())


def test_cursor_also_binds_instance_and_ruleset(state_factory):
    async def run():
        async with state_factory() as state:
            import base64
            core,_,_,_=state
            await core.register_new(request_id='cursor-dup',name='Alpha',token='cursor-dup-secret-owner-fresh',program='fixture',model='fixture')
            first=await core.directory(expected_server_instance_id=I,target_name='Alpha',limit=1)
            for field in ('instance','ruleset','revision'):
                data=json.loads(base64.urlsafe_b64decode(first['next_cursor']))
                data[field]='different' if field!='revision' else data[field]+1
                changed=base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
                with pytest.raises(B1Error,match='CURSOR_MISMATCH'):
                    await core.directory(expected_server_instance_id=I,target_name='Alpha',cursor=changed)

    asyncio.run(run())


def test_v1_from_actual_identity_store_has_no_mail_id_receipt(state_factory,monkeypatch):
    async def run():
        async with state_factory() as state:
            from pathlib import Path
            core,sessions,_,root=state
            monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]/'integrations/codex_app/src'))
            from agentstack_codex_app.identity_store import IdentityStore,build_binding
            store=IdentityStore(root/'old-binding-store')
            binding=build_binding(session_id='real-v1-fixture',agent_id=None,agent_name='Alpha',project_key='/fixture/origin-1')
            store.save(binding);store.store_owner_token(binding['external_id'],TOKENS[11])
            assert 'mail_agent_id' not in binding and 'credential_generation' not in binding
            result=await core.resolve_v1(store.resolve(binding['external_id']),store.load_owner_token(binding['external_id']))
            assert result['mail_agent_id']==11 and result['credential_generation']==0
            assert store.resolve(binding['external_id'])==binding
            assert store.load_owner_token(binding['external_id'])==TOKENS[11]

    asyncio.run(run())


@pytest.mark.parametrize('kind',['missing','wrong','null','shared','retired','origin','name'])
def test_v1_unrecoverable_stays_needs_resolution(state_factory,kind):
    async def run():
        async with state_factory() as state:
            core,sessions,_,_=state
            binding={'external_id':'codex:old-fixture','agent_id':None,'agent_name':'Alpha',
                     'project_key':'/fixture/origin-1','program':'codex-app'}
            key=TOKENS[11]
            if kind=='missing':key=None
            if kind=='wrong':key='wrong-v1-fixture-secret'
            if kind=='origin':binding['project_key']='/fixture/not-the-origin'
            if kind=='name':binding['agent_name']='not-the-agent'
            if kind in {'null','shared'}:
                async with sessions() as s:
                    if kind=='null':(await s.get(Agent,11)).registration_token=None
                    else:(await s.get(Agent,22)).registration_token=TOKENS[11]
                    await s.commit()
                await core.prepare(default_project_id=2)
            if kind=='retired':await core.retire(11)
            before=await snapshot(sessions)
            with pytest.raises(B1Error,match='NEEDS_RESOLUTION'):await core.resolve_v1(binding,key)
            assert await snapshot(sessions)==before

    asyncio.run(run())


def test_candidate_schema_matches_explicit_contract_fixture(state_factory):
    async def run():
        async with state_factory() as state:
            from pathlib import Path
            core,_,_,_=state
            spec=json.loads((Path(__file__).parents[1]/'fixtures/storage-origin-b1.json').read_text())
            assert spec['activation_enabled'] is False
            async with Client(build_candidate_server(core)) as c:
                tools=await c.list_tools()
                assert {t.name for t in tools}==set(spec['tools'])
                for tool in tools:
                    schema=tool.inputSchema;expected=spec['tools'][tool.name]
                    assert sorted(schema['properties'])==expected['properties']
                    assert sorted(schema.get('required',[]))==expected['required']
                    assert schema['additionalProperties'] is False
                    if 'auth' in schema['properties']:
                        a=schema['properties']['auth']
                        assert a['type']=='object' and a['additionalProperties'] is False
                        assert 'anyOf' not in a

    asyncio.run(run())


def test_bound_identity_and_delivery_types_do_not_use_names(state_factory):
    async def run():
        async with state_factory() as state:
            from agentstack_mail.storage_origin_contract import OwnerBinding,DeliveryTarget
            core,sessions,_,_=state
            binding=OwnerBinding(I,11,0,1)
            assert (await core.check_binding(binding,auth()))['mail_agent_id']==11
            with pytest.raises(B1Error,match='BINDING_OWNER_MISMATCH'):
                await core.check_binding(OwnerBinding(I,22,0,2),auth())
            before=await snapshot(sessions)
            fact=DeliveryTarget(I,11,900,'delivery-epoch','binding-receipt')
            with pytest.raises(B1Error,match='DELIVERY_CONFIRMATION_NOT_READY'):
                await core.confirm_delivery(fact)
            assert await snapshot(sessions)==before

    asyncio.run(run())


def test_consumption_batch_has_full_proof_and_all_recipient_checks(state_factory):
    async def run():
        async with state_factory() as state:
            core,sessions,_,_=state
            before=await snapshot(sessions)
            args={'expected_server_instance_id':I,'auth':auth(),'target_agent_id':11,
                  'message_ids':[900],'request_id':'consume-batch'}
            async with Client(build_candidate_server(core)) as c:
                good=await c.call_tool('consume_notification',args,raise_on_error=False)
                assert good.is_error and 'NOTIFICATION_CONSUMPTION_NOT_READY' in str(good)
                wrong=await c.call_tool('consume_notification',dict(args,auth=dict(auth(),registration_token='bad-batch-secret')),raise_on_error=False)
                assert wrong.is_error and 'OWNER_CREDENTIAL_MISMATCH' in str(wrong)
                other=await c.call_tool('consume_notification',dict(args,message_ids=[900,901]),raise_on_error=False)
                assert other.is_error and 'NOT_A_RECIPIENT' in str(other)
                malformed=await c.call_tool('consume_notification',dict(args,message_ids=[True]),raise_on_error=False)
                assert malformed.is_error and 'INVALID_INTEGER' in str(malformed)
            assert await snapshot(sessions)==before

    asyncio.run(run())
