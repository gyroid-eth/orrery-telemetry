"""Real ORM notification facts, actual HTTP and Claude hook handoff."""
from __future__ import annotations

import asyncio
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import select

from agentstack_mail import fresh_notifications as notifications
from agentstack_mail.fresh_client import FreshBoundClient, _http
from agentstack_mail.fresh_launch import identity, layout
from agentstack_mail.fresh_runtime import _write_private
from agentstack_mail.fresh_server import build_fresh_server
from agentstack_mail.models import Message, MessageRecipient
from agentstack_mail.storage_origin_contract import B1Error, canonical
from test_fresh_guard import live
from test_fresh_launch import invoke, prepare
from test_fresh_mail import send_args, snapshot, state
from test_fresh_proxy import environment, proxy, refused

ROOT = Path(__file__).resolve().parents[3]


async def pending(runtime, people, actor=1, **changes):
    return await runtime.core.notification('pending', expected_server_instance_id=runtime.config['instance_id'],
                                            auth=people[actor]['auth'], **changes)


async def complete(runtime, people, mid, actor=1):
    return await runtime.core.notification('complete', expected_server_instance_id=runtime.config['instance_id'],
                                            auth=people[actor]['auth'], message_id=mid)


async def rows(runtime):
    async with runtime.sessions() as session:
        result = (await session.execute(select(notifications.facts).order_by(
            notifications.facts.c.message_id, notifications.facts.c.agent_id))).mappings().all()
        pairs = (await session.execute(select(MessageRecipient).order_by(
            MessageRecipient.message_id, MessageRecipient.agent_id))).scalars().all()
        return [dict(r) for r in result], [(r.message_id, r.agent_id, r.read_ts, r.ack_ts) for r in pairs]


def running(client, tmp_path, monkeypatch):
    """Trusted synthetic launch state; the integration test uses the real entry."""
    base = layout(client)
    launch_id, session_id, nonce = str(uuid4()), str(uuid4()), uuid4().hex
    run = base / 'runs' / launch_id;run.mkdir(mode=0o700)
    transcript = tmp_path / 'sessions' / (session_id + '.jsonl')
    transcript.parent.mkdir(mode=0o700, exist_ok=True);transcript.write_text('synthetic Claude transcript\n')
    home = client.path.parent / 'providers' / 'claude'
    home.parent.mkdir(mode=0o700, exist_ok=True);home.mkdir(mode=0o700, exist_ok=True)
    native = home / (session_id + '.jsonl');native.write_text(transcript.read_text())
    _write_private(run / 'intent.json', dict(kind='orrery-fresh-launch-v1', launch_id=launch_id,
        provider='claude', context_digest=client.context_digest, nonce=nonce,
        resume_session_id=None, wrapper_root=str(ROOT), **identity(client)))
    _write_private(base / 'current.json', {'launch_id':launch_id})
    monkeypatch.setenv('AGENTSTACK_FRESH_LAUNCH_ID', launch_id)
    monkeypatch.setenv('AGENTSTACK_FRESH_LAUNCH_NONCE', nonce)
    return dict(hook_event_name='SessionStart', session_id=session_id, transcript_path=str(native))


def test_message_and_pending_fact_commit_together_replay_and_signal_are_not_delivery(tmp_path, monkeypatch):
    async def run():
        async with state(tmp_path) as (runtime, people):
            args = send_args(runtime, people)
            sent = await runtime.core.write_message('send', **args)
            assert sent['delivery_status'] == 'notification_pending'
            assert await runtime.core.write_message('send', **args) == sent
            facts, pairs = await rows(runtime)
            assert facts == [dict(message_id=sent['message_id'], agent_id=people[1]['agent_id'], completed_ts=None)]
            assert pairs == [(sent['message_id'], people[1]['agent_id'], None, None)]
            signal = runtime.root / 'signals/agents' / str(people[1]['agent_id']) / (str(sent['message_id'])+'.signal')
            assert json.loads(signal.read_text()) == notifications.envelope(runtime.core, sent['message_id'], people[1]['agent_id'])
            assert signal.stat().st_mode & 0o077 == 0
            assert (await pending(runtime, people))['messages'][0]['id'] == sent['message_id']
            before = await snapshot(runtime)
            real_create = notifications.create
            async def fail(session, mid, recipients):
                await real_create(session, mid, recipients)
                raise B1Error('FIXTURE_ROLLBACK')
            monkeypatch.setattr(notifications, 'create', fail)
            with pytest.raises(B1Error, match='FIXTURE_ROLLBACK'):
                await runtime.core.write_message('send', **dict(args, request_id='rollback'))
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_peek_and_pending_are_pure_completion_is_distinct_and_idempotent(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            sent = await runtime.core.write_message('send', **send_args(runtime, people,
                recipients=[{'target_name':'Beta'}, {'target_name':'Gamma'}], body_md='x'*5000))
            before = await snapshot(runtime)
            files = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in runtime.root.rglob('*.signal')}
            first = await pending(runtime, people)
            assert len(first['messages'][0]['body_md']) == 2001
            await runtime.core.peek(expected_server_instance_id=runtime.config['instance_id'], target_name='Beta')
            assert await snapshot(runtime) == before
            assert files == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in runtime.root.rglob('*.signal')}
            result = await complete(runtime, people, sent['message_id'])
            assert await complete(runtime, people, sent['message_id']) == result
            facts, pairs = await rows(runtime)
            assert facts[0]['completed_ts'] is not None and facts[1]['completed_ts'] is None
            assert all(read is None and ack is None for _, _, read, ack in pairs)
            assert (await pending(runtime, people))['messages'] == []
            assert len((await pending(runtime, people, 2))['messages']) == 1
            assert len(list(runtime.root.rglob('*.signal'))) == 1
    asyncio.run(run())


def test_read_ack_consume_only_own_pending_without_inventing_notification_completion(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            first = await runtime.core.write_message('send', **send_args(runtime, people))
            second = await runtime.core.write_message('send', **send_args(runtime, people, request_id='two'))
            for operation, message in [('read',first), ('ack',second)]:
                args = dict(expected_server_instance_id=runtime.config['instance_id'], auth=people[1]['auth'],
                    request_id=str(uuid4()), message_id=message['message_id'], target_agent_id=people[1]['agent_id'])
                a = await runtime.core.owner(operation, **args)
                assert await runtime.core.owner(operation, **args) == a
            facts, pairs = await rows(runtime)
            assert all(row['completed_ts'] is None for row in facts)
            assert pairs[0][2] is not None and pairs[0][3] is None
            assert pairs[1][2] is not None and pairs[1][3] is not None
            assert (await pending(runtime, people))['messages'] == []
            assert list(runtime.root.rglob('*.signal')) == []
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['notification_pending','notification_complete'])
@pytest.mark.parametrize('damage', ['missing','wrong','old-I','stale-generation'])
def test_actual_internal_tools_reject_invalid_owner_without_side_effects(tmp_path, operation, damage):
    async def run():
        async with state(tmp_path) as (runtime, people):
            sent = await runtime.core.write_message('send', **send_args(runtime, people))
            args = dict(expected_server_instance_id=runtime.config['instance_id'], auth=people[1]['auth'],
                        message_id=sent['message_id'])
            if damage == 'missing':del args['auth']
            elif damage == 'wrong':args['auth'] = dict(args['auth'], registration_token='wrong-fixture')
            elif damage == 'old-I':args['expected_server_instance_id'] = 'old-instance'
            else:
                await runtime.core.recover(people[1]['agent_id'],
                    expected_generation=people[1]['credential_generation'], new_token='new-notify-private-fixture-key')
            before = await snapshot(runtime)
            signals = {str(p):p.read_bytes() for p in runtime.root.rglob('*.signal')}
            with pytest.raises(B1Error):await _http(build_fresh_server(core=runtime.core), operation, args)
            assert await snapshot(runtime) == before
            assert signals == {str(p):p.read_bytes() for p in runtime.root.rglob('*.signal')}
    asyncio.run(run())


def test_valid_nonrecipient_cannot_consume_and_pending_is_bound_to_actor(tmp_path):
    async def run():
        async with state(tmp_path) as (runtime, people):
            sent = await runtime.core.write_message('send', **send_args(runtime, people))
            before = await snapshot(runtime)
            assert (await pending(runtime, people, 2))['messages'] == []
            with pytest.raises(B1Error, match='NOT_A_RECIPIENT'):
                await complete(runtime, people, sent['message_id'], 2)
            assert await snapshot(runtime) == before
    asyncio.run(run())


def test_flush_failure_keeps_pending_and_concurrent_hook_is_not_a_second_handoff(tmp_path, monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            event = running(client, tmp_path, monkeypatch)
            sent = await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            class Broken(StringIO):
                def flush(self):raise OSError('fixture output refused')
            with pytest.raises(OSError, match='fixture output refused'):
                await notifications.hook(client, event, ROOT, output=Broken())
            assert len((await pending(runtime, people, 0))['messages']) == 1
            event['hook_event_name'] = 'PostToolUse'
            with notifications.consumer_lock(client) as acquired:
                assert acquired
                blocked = StringIO();await notifications.hook(client, event, ROOT, output=blocked)
                assert blocked.getvalue() == ''
            output = StringIO();await notifications.hook(client, event, ROOT, output=output)
            assert json.loads(output.getvalue())['hookSpecificOutput']['additionalContext'].startswith('ORRERY Mail notification:')
            assert (await pending(runtime, people, 0))['messages'] == []
            quiet = StringIO();await notifications.hook(client, event, ROOT, output=quiet)
            assert quiet.getvalue() == ''
            facts, pairs = await rows(runtime)
            assert facts[0]['completed_ts'] and pairs == [(sent['message_id'],people[0]['agent_id'],None,None)]
    asyncio.run(run())


@pytest.mark.parametrize('committed', [False,True])
def test_completion_response_loss_is_separate_from_stdout_handoff(tmp_path, monkeypatch, committed):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            event = running(client, tmp_path, monkeypatch)
            sent = await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            real = client.dispatch
            async def uncertain(endpoint, tool, args):
                if tool == 'notification_complete':
                    if committed:await real(endpoint, tool, args)
                    raise B1Error('FRESH_TRANSPORT_UNAVAILABLE')
                return await real(endpoint, tool, args)
            client.dispatch = uncertain
            first = StringIO()
            with pytest.raises(B1Error, match='FRESH_TRANSPORT_UNAVAILABLE'):
                await notifications.hook(client, event, ROOT, output=first)
            assert 'ORRERY Mail notification:' in first.getvalue()
            client.dispatch = real;second = StringIO()
            await notifications.hook(client, dict(event,hook_event_name='PostToolUse'), ROOT, output=second)
            assert bool(second.getvalue()) is (not committed)
            assert (await pending(runtime, people, 0))['messages'] == []
            facts, pairs = await rows(runtime)
            assert len(facts) == 1 and facts[0]['completed_ts'] and pairs[0][2:] == (None,None)
    asyncio.run(run())


def test_real_regular_claude_entry_receives_hook_context_once_and_internal_tools_are_hidden(tmp_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            home, binary = prepare(client, 'claude', tmp_path)
            # The fake native CLI uses exactly the generated real hook commands.
            native = '''import json,os,pathlib,subprocess,sys,uuid
args=sys.argv[1:];home=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
session=str(uuid.uuid4());transcript=home/(session+'.jsonl');transcript.write_text('fixture')
settings=json.loads(pathlib.Path(args[args.index('--settings')+1]).read_text())
event=dict(hook_event_name='SessionStart',session_id=session,transcript_path=str(transcript))
results=[]
for kind in ('SessionStart','PostToolUse','PostToolUse'):
    event['hook_event_name']=kind
    command=settings['hooks'][kind][0]['hooks'][0]['command']
    r=subprocess.run(['/bin/bash','-c',command],input=json.dumps(event),text=True,capture_output=True,timeout=30)
    results.append(dict(rc=r.returncode,stdout=r.stdout,stderr=r.stderr))
pathlib.Path(os.environ['TEST_TRACE']).write_text(json.dumps(results))
sys.exit(0 if all(r['rc']==0 for r in results) else 17)
'''
            binary.write_text('#!'+sys.executable+'\n'+native)
            await runtime.core.write_message('send', **send_args(runtime, people, auth=people[1]['auth'],
                recipients=[{'target_name':'Alpha'}], body_md='x'*4000, importance='high', ack_required=True))
            # A forged filename/envelope never becomes authority or a body.
            folder = runtime.root/'signals/agents'/str(people[0]['agent_id'])
            _write_private(folder/'999999.signal', {'mail_instance_id':'foreign','message_id':999999})
            result = await invoke(client, 'claude', binary, tmp_path)
            assert result.returncode == 0, result.stderr
            observed = json.loads((tmp_path/'native-trace.json').read_text())
            assert 'Body preview:' in observed[0]['stdout'] and 'Fetch inbox' in observed[0]['stdout']
            assert observed[1]['stdout'] == observed[2]['stdout'] == ''
            assert '999999' not in canonical(observed)
            assert all(p['auth']['registration_token'] not in canonical(observed) for p in people)
            facts, pairs = await rows(runtime)
            assert len(facts)==1 and facts[0]['completed_ts'] and pairs[0][2:] == (None,None)
            async with proxy(client,tmp_path) as peer:
                listing = await peer.rpc('tools/list')
                names = {row['name'] for row in listing['result']['tools']}
                for name in ('notification_pending','notification_complete'):
                    assert name not in names and refused(await peer.call(name,{})) == 'TOOL_UNPUBLISHED'
    asyncio.run(run())


def test_high_notification_is_not_starved_by_fifty_low_pending_facts(tmp_path, monkeypatch):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            event = running(client, tmp_path, monkeypatch)
            for number in range(50):
                await runtime.core.write_message('send', **send_args(runtime, people,
                    request_id='low-'+str(number), auth=people[1]['auth'],
                    recipients=[{'target_name':'Alpha'}], subject='low-'+str(number), importance='low'))
            high = await runtime.core.write_message('send', **send_args(runtime, people,
                request_id='high', auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}],
                subject='high-after-fifty', importance='high', ack_required=True))
            assert high['message_id'] == 51
            assert len((await pending(runtime, people, 0))['messages']) == 50
            filtered = await client.call('notification_pending', {'min_importance':'high'})
            assert [m['id'] for m in filtered['messages']] == [51]
            env = environment(client, AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE='high')
            async def call(payload):
                return await asyncio.to_thread(subprocess.run,
                    ['/bin/bash',str(ROOT/'bin/agentstack-runtime-client'),'notifications'],
                    input=json.dumps(payload), text=True,capture_output=True,env=env,cwd=tmp_path,timeout=30)
            first = await call(event)
            assert first.returncode == 0, first.stderr
            text = json.loads(first.stdout)['hookSpecificOutput']['additionalContext']
            assert 'high-after-fifty' in text and 'low-0' not in text
            second = await call(dict(event,hook_event_name='PostToolUse'))
            (tmp_path/'notify-high-hook.json').write_text(canonical({
                'first':dict(returncode=first.returncode,stdout=first.stdout,stderr=first.stderr),
                'second':dict(returncode=second.returncode,stdout=second.stdout,stderr=second.stderr)}))
            assert second.returncode == 0 and second.stdout == '', second.stderr
            facts, pairs = await rows(runtime)
            assert len(facts)==51 and all(f['completed_ts'] is None for f in facts[:50])
            assert facts[50]['completed_ts'] is not None
            assert all(read is None and ack is None for _,_,read,ack in pairs)
            assert [m['id'] for m in (await pending(runtime, people, 0))['messages']] == list(range(1,51))
            assert (await pending(runtime, people, 0, message_id=51))['messages'] == []
            assert len(list(runtime.root.rglob('*.signal'))) == 50
    asyncio.run(run())
