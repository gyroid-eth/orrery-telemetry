"""Current lease coverage through B1 and the actual shared/Codex hooks."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from agentstack_mail.fresh_client import FreshBoundClient
from agentstack_mail.fresh_guard import check
from agentstack_mail.fresh_server import build_fresh_server
from agentstack_mail.fresh_client import _http
from agentstack_mail.models import FileReservation
from agentstack_mail.storage_origin_contract import B1Error
from agentstack_mail import reservation_paths
from test_fresh_mail import snapshot, state
from test_fresh_reservations import arguments, known_rules, lease_rows

ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / 'hooks/check-file-reservation.sh'
CODEX = ROOT / 'hooks/check-codex-file-reservation.py'


async def grant(runtime, people, paths, actor=0, **changes):
    return await runtime.core.reservation('reserve', **arguments(runtime, people, actor=actor,
        paths_input=[str(p) for p in paths], request_id=str(uuid4()), **changes))


async def guard(runtime, people, paths, actor=0):
    return await runtime.core.reservation('guard', expected_server_instance_id=runtime.config['instance_id'],
        auth=people[actor]['auth'], paths_input=[str(p) for p in paths])


def test_current_guard_all_targets_and_no_partial_renewal(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            a, b = tmp_path / 'a.py', tmp_path / 'b.py'
            first = await grant(runtime, people, [a], ttl_seconds=60)
            before = await snapshot(runtime)
            # check's union and conflict_free are not a coverage certificate.
            checked = await runtime.core.reservation('check', expected_server_instance_id=runtime.config['instance_id'],
                auth=people[0]['auth'], paths_input=[str(a), str(b)])
            assert checked['conflict_free'] and checked['own_matching_ids']
            with pytest.raises(B1Error, match='RESERVATION_REQUIRED'):await guard(runtime, people, [a, b])
            assert await snapshot(runtime) == before
            await grant(runtime, people, [b], ttl_seconds=60)
            allowed = await guard(runtime, people, [a, b])
            assert allowed['covered'] is True and allowed['renewed'] == 2
            assert [item['path'] for item in allowed['items']] == [str(a), str(b)]
            row = next(row for row in await lease_rows(runtime) if row.id == first['reservations'][0]['id'])
            assert row.expires_ts == datetime.fromisoformat(first['reservations'][0]['expires_ts']) + timedelta(seconds=1800)
            assert (await guard(runtime, people, [a, b]))['renewed'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['expired', 'released', 'foreign', 'revoked'])
def test_past_grant_and_guard_success_never_authorize_after_state_change(tmp_path, known_rules, phase):
    async def run():
        async with state(tmp_path) as (runtime, people):
            path = tmp_path / 'current.py'
            granted = await grant(runtime, people, [path])
            assert (await guard(runtime, people, [path]))['covered']
            lease_id = granted['reservations'][0]['id']
            if phase == 'revoked':
                await runtime.core.recover(people[0]['agent_id'], expected_generation=people[0]['credential_generation'],
                                          new_token='guard-new-private-fixture-token')
            elif phase == 'expired':
                async with runtime.sessions() as session:
                    row = await session.get(FileReservation, lease_id)
                    row.expires_ts = datetime.now(timezone.utc) - timedelta(seconds=1)
                    await session.commit()
            else:
                await runtime.core.reservation('release', **arguments(runtime, people,
                    request_id=str(uuid4()), file_reservation_ids=[lease_id]))
                if phase == 'foreign':await grant(runtime, people, [path], actor=1)
            before = await snapshot(runtime)
            with pytest.raises(B1Error):await guard(runtime, people, [path])
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_literal_coverage_glob_lease_is_not_directory_or_glob_edit_permission(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            folder = tmp_path / 'work';folder.mkdir()
            await grant(runtime, people, [folder / '*.py'])
            assert (await guard(runtime, people, [folder / 'one.py']))['covered']
            with pytest.raises(B1Error, match='RESERVATION_REQUIRED'):
                await guard(runtime, people, [folder / 'one.txt'])
            with pytest.raises(B1Error, match='CHECK_REQUIRES_CONCRETE_PATH'):
                await guard(runtime, people, [folder / '*.py'])
            await grant(runtime, people, [folder])
            with pytest.raises(B1Error, match='CHECK_REQUIRES_FILE_PATH'):
                await guard(runtime, people, [folder])
            nested = folder / 'nested.py'
            # An exact directory lease does not cover its children.
            other = tmp_path / 'exact';other.mkdir()
            await grant(runtime, people, [other])
            with pytest.raises(B1Error, match='RESERVATION_REQUIRED'):
                await guard(runtime, people, [other / 'new.py'])
            assert not nested.exists()
    asyncio.run(run())


def test_foreign_shared_lease_blocks_write_even_with_own_shared_lease(tmp_path, known_rules):
    async def run():
        async with state(tmp_path) as (runtime, people):
            path = tmp_path / 'shared.py'
            await grant(runtime, people, [path], exclusive=False)
            await grant(runtime, people, [path], actor=1, exclusive=False)
            before = await snapshot(runtime)
            with pytest.raises(B1Error, match='RESERVATION_CONFLICT'):await guard(runtime, people, [path])
            assert await snapshot(runtime) == before
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['missing', 'wrong', 'old-I'])
def test_guard_actual_tool_owner_proof_negative_is_side_effect_free(tmp_path, known_rules, bad):
    async def run():
        async with state(tmp_path) as (runtime, people):
            path = tmp_path / 'proof.py'
            await grant(runtime, people, [path], ttl_seconds=60)
            args = dict(expected_server_instance_id=runtime.config['instance_id'], auth=people[0]['auth'], paths=[str(path)])
            if bad == 'missing':del args['auth']
            elif bad == 'wrong':args['auth'] = dict(args['auth'], registration_token='fixture-wrong')
            else:args['expected_server_instance_id'] = 'old-I'
            before = await snapshot(runtime)
            with pytest.raises(B1Error):await _http(build_fresh_server(core=runtime.core), 'guard_edit', args)
            assert await snapshot(runtime) == before
    asyncio.run(run())


@asynccontextmanager
async def live(tmp_path):
    async with state(tmp_path) as (runtime, people):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0));port = reservation.getsockname()[1]
        assert port not in {8765, 8770, 8781, 8791}
        log_path = tmp_path / 'guard-http.log'
        with log_path.open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'agentstack_mail.fresh_cli', 'serve',
                '--config', str(runtime.root / 'config.json'), '--port', str(port)], stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.1):break
                    except OSError:await asyncio.sleep(0.1)
                else:pytest.fail('guard HTTP startup timed out')
                client = await FreshBoundClient.prepare(runtime.root / 'config.json', people[0]['binding_path'],
                    f'http://127.0.0.1:{port}/mcp', client_id=str(uuid4()))
                yield runtime, people, client, process
            finally:
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)
                assert process.poll() is not None


async def hook(client, cwd, tool, inputs, *, codex=False, env_changes=None, payload_changes=None):
    env = dict(os.environ, AGENTSTACK_FRESH_CLIENT_CONTEXT=str(client.path), AGENTSTACK_PYTHON=sys.executable,
               AGENT_NAME='wrong-old-name', AGENTSTACK_PROJECT_KEY='/wrong-old-project',
               AGENTSTACK_PROTECTED_ROOTS='/not-this-folder', AGENTSTACK_MAIL_OUTAGE_POLICY='warn-open')
    env.update(env_changes or {})
    payload = dict(cwd=str(cwd), tool_name=tool, tool_input=inputs, session_id='fresh-guard-fixture')
    payload.update(payload_changes or {})
    return await asyncio.to_thread(subprocess.run,
        [sys.executable, str(CODEX)] if codex else ['/bin/bash', str(SHARED)],
        cwd=cwd, env=env, input=json.dumps(payload), capture_output=True, text=True, timeout=30)


def patch(*paths):
    return '*** Begin Patch\n' + ''.join(f'*** Add File: {p}\n+new\n' for p in paths) + '*** End Patch'


def test_real_shared_hook_protects_outside_old_project_and_rechecks_live_lease(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, process):
            folder = tmp_path / 'elsewhere';folder.mkdir()
            path = folder / 'note.py'
            refused = await hook(client, folder, 'Write', {'file_path': 'note.py'})
            assert refused.returncode == 2 and 'RESERVATION_REQUIRED' in refused.stderr
            granted = await grant(runtime, people, [path], ttl_seconds=60)
            allowed = await hook(client, folder, 'Write', {'file_path': 'note.py', 'actor_agent_id': 999, 'cwd': '/wrong'})
            assert allowed.returncode == 0, allowed.stderr
            assert not path.exists()  # A hook decides; it never performs the edit.
            row = (await lease_rows(runtime))[0]
            assert row.expires_ts == datetime.fromisoformat(granted['reservations'][0]['expires_ts']) + timedelta(seconds=1800)
            async with runtime.sessions() as session:
                expired = await session.get(FileReservation, row.id)
                expired.expires_ts = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()
            refused = await hook(client, folder, 'Edit', {'file_path': str(path)})
            assert refused.returncode == 2 and 'RESERVATION_REQUIRED' in refused.stderr
            await client.call('release_reservations', {'file_reservation_ids': [row.id]})
            refused = await hook(client, folder, 'Edit', {'file_path': str(path)})
            assert refused.returncode == 2 and 'RESERVATION_REQUIRED' in refused.stderr
            await grant(runtime, people, [path], actor=1)
            refused = await hook(client, folder, 'Edit', {'file_path': str(path)})
            assert refused.returncode == 2 and 'RESERVATION_CONFLICT' in refused.stderr
            binding = Path(people[0]['binding_path']);binding.unlink()
            refused = await hook(client, folder, 'Edit', {'file_path': str(path)})
            assert refused.returncode == 2 and 'FRESH_PRIVATE_FILE_UNAVAILABLE' in refused.stderr
            assert all(person['auth']['registration_token'] not in refused.stderr for person in people)
    asyncio.run(run())


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Mac APFS acceptance; other filesystems have separate evidence')
def test_native_apfs_japanese_missing_aliases_and_glob_through_http_and_shared_guard(tmp_path):
    folder = tmp_path / 'Obsidian_for_Xiaomi' / '21_Coding Projects' / 'orrery' / '日本語'
    folder.mkdir(parents=True)
    assert reservation_paths.unicode_rule_at(folder) == 'apfs'
    assert reservation_paths.case_insensitive_at(folder) is True
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            alias = 'か\u3099くａ.md'
            path = folder / 'がくＡ.md'
            denied = await hook(client, folder, 'Write', {'file_path': alias})
            assert denied.returncode == 2 and 'RESERVATION_REQUIRED' in denied.stderr
            await client.call('reserve_files', {'paths': [str(path)], 'ttl_seconds': 900})
            allowed = await hook(client, folder, 'Write', {'file_path': alias})
            assert allowed.returncode == 0, allowed.stderr
            assert not path.exists() and not (folder / alias).exists()
            beta = await FreshBoundClient.prepare(runtime.root / 'config.json', people[1]['binding_path'],
                client.context['endpoint'], client_id=str(uuid4()))
            with pytest.raises(B1Error, match='RESERVATION_CONFLICT'):
                await beta.call('reserve_files', {'paths': [str(folder / alias)]})
            # Witnesses belong only to this private fixture, never runtime detection.
            path.touch()
            assert path.samefile(folder / alias)
            await client.call('reserve_files', {'paths': [str(folder / '課題A.md')]})
            with pytest.raises(B1Error, match='RESERVATION_CONFLICT'):
                await beta.call('reserve_files', {'paths': [str(folder / '課題a.md')]})
            await beta.call('reserve_files', {'paths': [str(folder / '別件.md')]})
            other = folder.parent / '別のfolder';other.mkdir()
            await beta.call('reserve_files', {'paths': [str(other / path.name)]})
            await client.call('reserve_files', {'paths': [str(folder / '予約*.txt')]})
            matched = await hook(client, folder, 'Write', {'file_path': '予約一.txt'})
            assert matched.returncode == 0, matched.stderr
            not_matched = await hook(client, folder, 'Write', {'file_path': '対象外.txt'})
            assert not_matched.returncode == 2 and 'RESERVATION_REQUIRED' in not_matched.stderr
            before = await snapshot(runtime)
            with pytest.raises(B1Error, match='CASE_RULES_UNKNOWN'):
                await client.call('reserve_files', {'paths': [str(folder / 'É.md')]})
            assert await snapshot(runtime) == before
            (tmp_path / 'native-apfs-guard-result.json').write_text(json.dumps({
                'unicode_rule': 'apfs', 'case_insensitive': True, 'native_rules_injected': False,
                'missing_leaf_guard': 'allowed', 'nfc_nfd_fullwidth_alias': 'samefile',
                'ascii_case_alias_conflict': True, 'different_file_and_folder_granted': True,
                'glob_coverage_and_noncoverage': True, 'outside_profile_refused': True}) + '\n')
    asyncio.run(run())


def test_real_codex_adapter_checks_every_target_move_both_ends_and_symlink_entry(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            a, b = tmp_path / 'old.py', tmp_path / 'new.py'
            await grant(runtime, people, [a])
            refused = await hook(client, tmp_path, 'apply_patch', {'command': patch('old.py', 'new.py')}, codex=True)
            assert refused.returncode == 2 and 'RESERVATION_REQUIRED' in refused.stderr
            move = '*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n@@\n-old\n+new\n*** End Patch'
            refused = await hook(client, tmp_path, 'apply_patch', {'command': move}, codex=True)
            assert refused.returncode == 2
            await grant(runtime, people, [b])
            allowed = await hook(client, tmp_path, 'apply_patch', {'command': move}, codex=True)
            assert allowed.returncode == 0, allowed.stderr
            await client.call('release_reservations', {'paths': [str(a)]})
            refused = await hook(client, tmp_path, 'apply_patch', {'command': move}, codex=True)
            assert refused.returncode == 2 and 'RESERVATION_REQUIRED' in refused.stderr
            target = tmp_path / 'target.py';target.write_text('target')
            link = tmp_path / 'link.py';link.symlink_to(target)
            await grant(runtime, people, [target])
            refused = await hook(client, tmp_path, 'apply_patch',
                {'command': '*** Begin Patch\n*** Delete File: link.py\n*** End Patch'}, codex=True)
            assert refused.returncode == 2 and 'RESERVATION_ENTRY_UNSUPPORTED' in refused.stderr
            assert link.is_symlink() and target.read_text() == 'target'
            assert not a.exists() and not b.exists()
    asyncio.run(run())


def test_explicit_fresh_context_never_falls_through_on_corruption_or_transport(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, process):
            path = tmp_path / 'held.py';await grant(runtime, people, [path])
            for change in ({'AGENTSTACK_FRESH_CLIENT_CONTEXT': ''},
                           {'AGENTSTACK_FRESH_CLIENT_CONTEXT': str(tmp_path / 'missing.json')},
                           {'AGENTSTACK_CLIENT_CONFIG': '/old-global'},
                           {'AGENTSTACK_GLOBAL_SERVER_CONFIG': '/old-server'}):
                result = await hook(client, tmp_path, 'Edit', {'file_path': str(path)}, env_changes=change)
                assert result.returncode == 2
            mismatch = await hook(client, tmp_path, 'Edit', {'file_path': str(path)}, payload_changes={'cwd': '/'})
            assert mismatch.returncode == 2 and 'FRESH_HOOK_CWD_MISMATCH' in mismatch.stderr
            saved = client.path.read_bytes();client.path.write_text('{broken')
            corrupted = await hook(client, tmp_path, 'Edit', {'file_path': str(path)})
            assert corrupted.returncode == 2 and 'FRESH_PRIVATE_FILE_UNAVAILABLE' in corrupted.stderr
            client.path.write_bytes(saved)
            process.terminate();process.wait(timeout=5)
            result = await hook(client, tmp_path, 'Edit', {'file_path': str(path)})
            assert result.returncode == 2 and 'FRESH_TRANSPORT_UNAVAILABLE' in result.stderr
            assert people[0]['auth']['registration_token'] not in result.stderr
    asyncio.run(run())


def test_guard_rejects_incomplete_or_wrong_response_without_editing(tmp_path, monkeypatch):
    class Caller:
        context = {'mail_instance_id': 'I', 'mail_agent_id': 1}
        def __init__(self, path):pass
        def _business(self, tool, args):return args
        async def call(self, tool, args):
            return {'mail_instance_id': 'I', 'actor_agent_id': 2, 'covered': True,
                    'items': [{'path': str(tmp_path / 'a.py'), 'reservation_id': 1}]}
    from agentstack_mail import fresh_guard
    monkeypatch.setattr(fresh_guard, 'FreshBoundClient', Caller)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(B1Error, match='FRESH_GUARD_RESPONSE_INVALID'):
        asyncio.run(check('fixture', {'tool_name': 'Write', 'cwd': str(tmp_path),
                                     'tool_input': {'file_path': 'a.py'}}))
    assert not (tmp_path / 'a.py').exists()
