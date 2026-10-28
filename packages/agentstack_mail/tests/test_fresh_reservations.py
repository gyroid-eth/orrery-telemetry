"""Real ORM/SQLite leases; simulated known FS rules and native evidence differ."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client
from sqlalchemy import select

from agentstack_mail import reservation_paths
from agentstack_mail.fresh_server import build_fresh_server
from agentstack_mail.models import FileReservation
from agentstack_mail.storage_origin_contract import B1Error
from test_fresh_mail import snapshot, state


@pytest.fixture
def known_rules(monkeypatch):
    # Backend semantics under known exact/case-sensitive rules. The native
    # Japanese test below intentionally does not use this evidence substitute.
    monkeypatch.setattr(reservation_paths, 'unicode_rule_at', lambda _: 'exact')
    monkeypatch.setattr(reservation_paths, 'case_insensitive_at', lambda _: False)


def arguments(runtime, people, actor=0, **changes):
    result = dict(expected_server_instance_id=runtime.config['instance_id'],
                  auth=people[actor]['auth'], request_id='lease-request')
    result.update(changes)
    return result


async def lease_rows(runtime):
    async with runtime.sessions() as session:
        return (await session.execute(select(FileReservation).order_by(FileReservation.id))).scalars().all()


def test_wire_absolute_conflicts_batch_atomicity_and_pure_check(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            path = str(tmp_path / 'shared.py')
            async with Client(build_fresh_server(core=runtime.core)) as client:
                granted = (await client.call_tool('reserve_files', arguments(runtime, people,
                    paths=[path], ttl_seconds=900))).data
                assert granted['reservations'][0]['path_pattern'] == path
                before = await snapshot(runtime)
                check = (await client.call_tool('check_reservations', dict(
                    expected_server_instance_id=runtime.config['instance_id'], auth=people[1]['auth'], paths=[path]))).data
                assert check['conflict_free'] is False
                assert check['conflicts'][0]['owner_agent_id'] == people[0]['agent_id']
                assert await snapshot(runtime) == before
                denied = await client.call_tool('reserve_files', arguments(runtime, people, actor=1,
                    request_id='batch', paths=[str(tmp_path / 'unrelated.py'), path]), raise_on_error=False)
                assert denied.is_error and 'RESERVATION_CONFLICT' in str(denied)
                assert len(await lease_rows(runtime)) == 1
                longer = (await client.call_tool('reserve_files', arguments(runtime, people,
                    request_id='two-days', paths=[str(tmp_path / 'longer.py')], ttl_seconds=172800))).data
                row = longer['reservations'][0]
                assert datetime.fromisoformat(row['expires_ts']) - datetime.fromisoformat(row['created_ts']) == timedelta(days=2)
    asyncio.run(run())


def test_two_simultaneous_exclusive_requests_commit_only_one(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            values = [arguments(runtime, people, actor=n, request_id=f'race-{n}',
                                paths_input=[str(tmp_path / 'race.py')]) for n in (0, 1)]
            results = await asyncio.gather(*(runtime.core.reservation('reserve', **value) for value in values),
                                           return_exceptions=True)
            assert sum(isinstance(result, dict) for result in results) == 1
            rejected = [result for result in results if isinstance(result, Exception)]
            assert len(rejected) == 1 and isinstance(rejected[0], B1Error)
            assert rejected[0].code == 'RESERVATION_CONFLICT'
            rows = await lease_rows(runtime)
            assert len(rows) == 1
            assert rows[0].agent_id == next(result['actor_agent_id'] for result in results if isinstance(result, dict))
    asyncio.run(run())


def test_alias_missing_glob_and_distinct_folder_scopes(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            folder = tmp_path / 'work';folder.mkdir()
            alias = tmp_path / 'alias';alias.symlink_to(folder, target_is_directory=True)
            original = str(folder / 'missing.py')
            await runtime.core.reservation('reserve', **arguments(runtime, people, paths_input=[original]))
            with pytest.raises(B1Error, match='RESERVATION_CONFLICT'):
                await runtime.core.reservation('reserve', **arguments(runtime, people, actor=1,
                    request_id='alias', paths_input=[str(alias / 'missing.py')]))
            other = tmp_path / 'other';other.mkdir()
            await runtime.core.reservation('reserve', **arguments(runtime, people, actor=1,
                request_id='different-folder', paths_input=[str(other / 'missing.py')]))
            await runtime.core.reservation('reserve', **arguments(runtime, people,
                request_id='glob', paths_input=[str(folder / '**' / '*.txt')]))
            with pytest.raises(B1Error, match='RESERVATION_CONFLICT'):
                await runtime.core.reservation('reserve', **arguments(runtime, people, actor=1,
                    request_id='glob-child', paths_input=[str(folder / 'nested' / 'new.txt')]))
    asyncio.run(run())


def test_renew_release_replay_owner_and_expiry_contract(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            reserved = await runtime.core.reservation('reserve', **arguments(runtime, people,
                paths_input=[str(tmp_path / 'lease.py')], ttl_seconds=900))
            lease = reserved['reservations'][0]
            renewed_args = arguments(runtime, people, request_id='renew',
                                      file_reservation_ids=[lease['id']], extend_seconds=60)
            renewed = await runtime.core.reservation('renew', **renewed_args)
            assert datetime.fromisoformat(renewed['reservations'][0]['expires_ts']) == (
                datetime.fromisoformat(lease['expires_ts']) + timedelta(seconds=60))
            assert await runtime.core.reservation('renew', **renewed_args) == renewed
            for operation in ('renew', 'release'):
                with pytest.raises(B1Error, match='LEASE_OWNER_REQUIRED'):
                    await runtime.core.reservation(operation, **arguments(runtime, people, actor=1,
                        request_id='foreign-' + operation, file_reservation_ids=[lease['id']]))
            released_args = arguments(runtime, people, request_id='release', file_reservation_ids=[lease['id']])
            released = await runtime.core.reservation('release', **released_args)
            assert released['reservations'][0]['released_ts'] is not None
            assert await runtime.core.reservation('release', **released_args) == released
            with pytest.raises(B1Error, match='LEASE_EXPIRED'):
                await runtime.core.reservation('renew', **dict(renewed_args, request_id='released-renew'))
            second = await runtime.core.reservation('reserve', **arguments(runtime, people,
                request_id='expiring', paths_input=[str(tmp_path / 'expired.py')]))
            async with runtime.sessions() as session:
                row = await session.get(FileReservation, second['reservations'][0]['id'])
                row.expires_ts = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()
            with pytest.raises(B1Error, match='LEASE_EXPIRED'):
                await runtime.core.reservation('renew', **arguments(runtime, people,
                    request_id='expired-renew', file_reservation_ids=[row.id]))
            check = await runtime.core.reservation('check', expected_server_instance_id=runtime.config['instance_id'],
                auth=people[1]['auth'], paths_input=[str(tmp_path / 'expired.py')])
            assert check['conflict_free'] is True
    asyncio.run(run())


def test_request_refusal_and_accepted_replay_are_not_new_grants(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            args = arguments(runtime, people, paths_input=[str(tmp_path / 'one.py')])
            first = await runtime.core.reservation('reserve', **args)
            assert await runtime.core.reservation('reserve', **args) == first
            with pytest.raises(B1Error, match='REQUEST_INTENT_CONFLICT'):
                await runtime.core.reservation('reserve', **dict(args, ttl_seconds=900))
            other = arguments(runtime, people, actor=1, request_id='other', paths_input=args['paths_input'])
            with pytest.raises(B1Error, match='RESERVATION_CONFLICT'):
                await runtime.core.reservation('reserve', **other)
            await runtime.core.reservation('release', **arguments(runtime, people,
                request_id='release', file_reservation_ids=[first['reservations'][0]['id']]))
            with pytest.raises(B1Error, match='RESERVATION_CONFLICT'):
                await runtime.core.reservation('reserve', **other)
            new = await runtime.core.reservation('reserve', **dict(other, request_id='new-attempt'))
            assert new['reservations'][0]['owner_agent_id'] == people[1]['agent_id']
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['reserve_files', 'renew_reservations', 'release_reservations', 'check_reservations'])
@pytest.mark.parametrize('bad', ['missing', 'wrong', 'old-I'])
def test_wire_lease_owner_negative_matrix_is_side_effect_free(tmp_path, known_rules, operation, bad):
    async def run():
        async with state(tmp_path) as (runtime, people):
            reserved = await runtime.core.reservation('reserve', **arguments(runtime, people,
                paths_input=[str(tmp_path / 'protected.py')]))
            args = arguments(runtime, people, request_id='negative')
            if operation in {'reserve_files', 'check_reservations'}:
                args['paths'] = [str(tmp_path / 'new.py')]
            else:
                args['file_reservation_ids'] = [reserved['reservations'][0]['id']]
            if operation == 'check_reservations':
                del args['request_id']
            if bad == 'missing':del args['auth']
            elif bad == 'wrong':args['auth'] = dict(args['auth'], registration_token='wrong-fixture-key')
            else:args['expected_server_instance_id'] = 'old-I'
            before = await snapshot(runtime)
            async with Client(build_fresh_server(core=runtime.core)) as client:
                result = await client.call_tool(operation, args, raise_on_error=False)
                assert result.is_error
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_invalid_paths_ttl_types_and_unknown_rules_never_grant(tmp_path, known_rules, monkeypatch):
    async def run():
        async with state(tmp_path) as (runtime, people):
            with pytest.raises(B1Error, match='ABSOLUTE_PATH_REQUIRED'):
                await runtime.core.reservation('reserve', **arguments(runtime, people, paths_input=['relative.py']))
            with pytest.raises(B1Error, match='TTL_OUT_OF_RANGE'):
                await runtime.core.reservation('renew', **arguments(runtime, people, request_id='bad-ttl', extend_seconds=59))
            async with Client(build_fresh_server(core=runtime.core)) as client:
                for change in ({'exclusive': 1}, {'ttl_seconds': True}, {'paths': [False]}, {'project_key': '/old'}):
                    result = await client.call_tool('reserve_files', dict(
                        arguments(runtime, people, paths=[str(tmp_path / 'new.py')]), **change), raise_on_error=False)
                    assert result.is_error
            monkeypatch.setattr(reservation_paths, 'unicode_rule_at', lambda _: None)
            with pytest.raises(B1Error, match='UNICODE_RULES_UNKNOWN'):
                await runtime.core.reservation('reserve', **arguments(runtime, people,
                    request_id='unknown-unicode', paths_input=[str(tmp_path / '課題.txt')]))
            assert await lease_rows(runtime) == []
    asyncio.run(run())


def test_native_japanese_rules_are_reported_as_grant_or_explicit_unknown(tmp_path):
    path = str(tmp_path / '課題.txt')
    native_rule = reservation_paths.unicode_rule_at(tmp_path)
    native_case = reservation_paths.case_insensitive_at(tmp_path)
    try:
        reservation_paths.normalize_paths([path])
        expected = None
    except reservation_paths.ReservationError as error:
        expected = str(error)
    if sys.platform == 'darwin' and native_rule == 'apfs':
        assert expected is None  # Native APFS must now grant this profile, not just report unknown.
    async def run():
        async with state(tmp_path) as (runtime, people):
            args = arguments(runtime, people, paths_input=[path])
            if expected is not None:
                with pytest.raises(B1Error) as error:
                    await runtime.core.reservation('reserve', **args)
                assert error.value.code == expected
                assert await lease_rows(runtime) == []
            else:
                result = await runtime.core.reservation('reserve', **args)
                assert len(result['reservations']) == 1
                # No FS-rule injection: race the same missing Japanese file.
                values = [arguments(runtime, people, actor=n, request_id=f'native-race-{n}',
                    paths_input=[str(tmp_path / '並行.md')]) for n in (0, 1)]
                outcomes = await asyncio.gather(*(runtime.core.reservation('reserve', **value)
                    for value in values), return_exceptions=True)
                assert sum(isinstance(value, dict) for value in outcomes) == 1
                refused = [value for value in outcomes if isinstance(value, B1Error)]
                assert len(refused) == 1 and refused[0].code == 'RESERVATION_CONFLICT'
                assert len(await lease_rows(runtime)) == 2
    asyncio.run(run())
    (tmp_path / 'native-japanese-result.json').write_text(json.dumps(
        {'native_japanese_result': expected or 'granted', 'platform': sys.platform,
         'unicode_rule': native_rule, 'case_insensitive': native_case}) + '\n')
