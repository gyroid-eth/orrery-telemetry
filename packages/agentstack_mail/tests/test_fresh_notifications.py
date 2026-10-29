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
from agentstack_mail import fresh_launch
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
    return dict(hook_event_name='SessionStart', source='startup', session_id=session_id, transcript_path=str(native))


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


@pytest.mark.parametrize('transcript_first', [True, False])
def test_real_regular_claude_entry_receives_hook_context_once_and_internal_tools_are_hidden(tmp_path, transcript_first):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            home, binary = prepare(client, 'claude', tmp_path)
            # The fake native CLI uses exactly the generated real hook commands.
            native = '''import json,os,pathlib,subprocess,sys,uuid
args=sys.argv[1:];home=pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
session=str(uuid.uuid4());transcript=home/(session+'.jsonl')
early=os.environ['TEST_TRANSCRIPT_FIRST']=='1'
if early:transcript.write_text('fixture')
settings=json.loads(pathlib.Path(args[args.index('--settings')+1]).read_text())
event=dict(hook_event_name='SessionStart',source='startup',session_id=session,transcript_path=str(transcript))
results=[]
for kind in ('SessionStart','PostToolUse','PostToolUse'):
    event['hook_event_name']=kind
    command=settings['hooks'][kind][0]['hooks'][0]['command']
    r=subprocess.run(['/bin/bash','-c',command],input=json.dumps(event),text=True,capture_output=True,timeout=30)
    base=pathlib.Path(os.environ['AGENTSTACK_FRESH_CLIENT_CONTEXT']).parent/'launch'
    run=base/'runs'/os.environ['AGENTSTACK_FRESH_LAUNCH_ID']
    results.append(dict(rc=r.returncode,stdout=r.stdout,stderr=r.stderr,
        receipt=(run/'session.json').exists(),candidate=(run/'session-start.json').exists()))
    # The file cannot exist until the SessionStart subprocess has returned.
    if kind=='SessionStart' and not early:transcript.write_text('fixture')
pathlib.Path(os.environ['TEST_TRACE']).write_text(json.dumps(results))
sys.exit(0 if all(r['rc']==0 for r in results) else 17)
'''
            binary.write_text('#!'+sys.executable+'\n'+native)
            await runtime.core.write_message('send', **send_args(runtime, people, auth=people[1]['auth'],
                recipients=[{'target_name':'Alpha'}], body_md='x'*4000, importance='high', ack_required=True))
            # A forged filename/envelope never becomes authority or a body.
            folder = runtime.root/'signals/agents'/str(people[0]['agent_id'])
            _write_private(folder/'999999.signal', {'mail_instance_id':'foreign','message_id':999999})
            result = await invoke(client, 'claude', binary, tmp_path,
                                  extra={'TEST_TRANSCRIPT_FIRST':'1' if transcript_first else '0'})
            assert result.returncode == 0, result.stderr
            observed = json.loads((tmp_path/'native-trace.json').read_text())
            delivered = 0 if transcript_first else 1
            assert 'Body preview:' in observed[delivered]['stdout'] and 'Fetch inbox' in observed[delivered]['stdout']
            assert all(row['stdout'] == '' for i,row in enumerate(observed) if i != delivered)
            assert observed[0]['receipt'] is transcript_first
            assert observed[0]['candidate'] is (not transcript_first)
            assert observed[1]['receipt'] and observed[2]['receipt']
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


@pytest.mark.parametrize('source', ['resume','clear','compact','future','',None,False,0,[],{},'STARTUP','missing'])
def test_nonstartup_start_rechecks_receipt_without_notification_handoff(tmp_path, monkeypatch, source):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            event = running(client, tmp_path, monkeypatch)
            await fresh_launch.record_session(client, event)
            if source == 'missing':event.pop('source')
            else:event['source'] = source
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            before = await rows(runtime)
            receipts = {p:p.read_bytes() for p in layout(client).rglob('*.json')}
            signals = {p:p.read_bytes() for p in runtime.root.rglob('*.signal')}
            calls, dispatch = [], client.dispatch
            async def observed(endpoint, tool, args):
                if tool in {'notification_pending','notification_complete'}:calls.append(tool)
                return await dispatch(endpoint, tool, args)
            client.dispatch = observed
            output = StringIO();await notifications.hook(client, event, ROOT, output=output)
            assert output.getvalue() == '' and calls == []
            assert await rows(runtime) == before
            assert {p:p.read_bytes() for p in layout(client).rglob('*.json')} == receipts
            assert {p:p.read_bytes() for p in runtime.root.rglob('*.signal')} == signals
    asyncio.run(run())


@pytest.mark.parametrize('source', ['missing',None,False])
@pytest.mark.parametrize('damage', ['nonce','current','ended','session','owner','outside'])
def test_nonstartup_source_never_bypasses_receipt_or_owner_gates(tmp_path, monkeypatch, source, damage):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            event = running(client, tmp_path, monkeypatch)
            await fresh_launch.record_session(client, event)
            directory = layout(client) / 'runs' / os.environ['AGENTSTACK_FRESH_LAUNCH_ID']
            receipts = {p:p.read_bytes() for p in (directory/'session.json',
                layout(client)/'sessions'/(event['session_id']+'.json'))}
            if source == 'missing':event.pop('source')
            else:event['source'] = source
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            if damage == 'nonce':monkeypatch.setenv('AGENTSTACK_FRESH_LAUNCH_NONCE','foreign')
            elif damage == 'current':
                (layout(client)/'current.json').write_text(canonical({'launch_id':str(uuid4())}))
            elif damage == 'ended':
                _write_private(directory/'ended.json', {'launch_id':directory.name,'exit_code':0})
            elif damage == 'session':event['session_id'] = str(uuid4())
            elif damage == 'owner':
                await runtime.core.recover(people[0]['agent_id'], expected_generation=people[0]['credential_generation'],
                    new_token='nonstartup-synthetic-recovery-key')
            else:
                outside = tmp_path/'outside.jsonl';outside.write_text('outside family HOME')
                event['transcript_path'] = str(outside)
            before = await rows(runtime)
            signals = {p:p.read_bytes() for p in runtime.root.rglob('*.signal')}
            calls, dispatch = [], client.dispatch
            async def observed(endpoint, tool, args):
                if tool in {'notification_pending','notification_complete'}:calls.append(tool)
                return await dispatch(endpoint, tool, args)
            client.dispatch = observed
            output = StringIO()
            with pytest.raises(B1Error):await notifications.hook(client, event, ROOT, output=output)
            assert output.getvalue() == '' and calls == [] and await rows(runtime) == before
            assert all(p.read_bytes() == value for p,value in receipts.items())
            assert {p:p.read_bytes() for p in runtime.root.rglob('*.signal')} == signals
    asyncio.run(run())


@pytest.mark.parametrize('source', ['resume','missing',None])
def test_nonstartup_candidate_and_same_receipt_leave_pending_until_regular_post(tmp_path, monkeypatch, source):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            event = running(client, tmp_path, monkeypatch)
            if source == 'missing':event.pop('source')
            else:event['source'] = source
            transcript = Path(event['transcript_path']);transcript.unlink()
            sent = await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            before = await rows(runtime)
            directory = layout(client)/'runs'/os.environ['AGENTSTACK_FRESH_LAUNCH_ID']
            output = StringIO();await notifications.hook(client, event, ROOT, output=output)
            assert output.getvalue() == '' and await rows(runtime) == before
            candidate = (directory/'session-start.json').read_bytes()
            assert not (directory/'session.json').exists()
            transcript.write_text('created only after SessionStart returns')
            # A regular Post publishes the admitted candidate and may hand off.
            post = StringIO()
            await notifications.hook(client, dict(event,hook_event_name='PostToolUse'), ROOT, output=post)
            assert 'ORRERY Mail notification:' in post.getvalue()
            receipt = (directory/'session.json').read_bytes()
            assert (directory/'session-start.json').read_bytes() == candidate
            second = await runtime.core.write_message('send', **send_args(runtime, people,
                request_id='after-post', auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            before = await rows(runtime)
            quiet = StringIO();await notifications.hook(client, event, ROOT, output=quiet)
            assert quiet.getvalue() == '' and await rows(runtime) == before
            assert (directory/'session.json').read_bytes() == receipt
            assert (directory/'session-start.json').read_bytes() == candidate
            facts,pairs = before
            assert facts[0]['message_id'] == sent['message_id'] and facts[0]['completed_ts']
            assert facts[1]['message_id'] == second['message_id'] and facts[1]['completed_ts'] is None
            assert all(read is None and ack is None for _,_,read,ack in pairs)
    asyncio.run(run())


async def deferred_start(client, tmp_path, monkeypatch):
    event = running(client, tmp_path, monkeypatch)
    transcript = Path(event['transcript_path']);transcript.unlink()
    output = StringIO();await notifications.hook(client, event, ROOT, output=output)
    run = layout(client) / 'runs' / os.environ['AGENTSTACK_FRESH_LAUNCH_ID']
    assert output.getvalue() == '' and (run / 'session-start.json').is_file()
    assert (run / 'session-start.json').stat().st_mode & 0o777 == 0o600
    assert not (run / 'session.json').exists() and not list((layout(client) / 'sessions').iterdir())
    return event, transcript, run


@pytest.mark.parametrize('post_path', [True, False])
def test_deferred_start_stays_unready_until_real_transcript_then_concurrent_post_handoff(tmp_path, monkeypatch, post_path):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            sent = await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            before = await rows(runtime)
            event, transcript, directory = await deferred_start(client, tmp_path, monkeypatch)
            candidate = (directory / 'session-start.json').read_bytes()
            # Identical simultaneous starts are idempotent, never notifications.
            await asyncio.gather(*(notifications.hook(client, event, ROOT, output=StringIO()) for _ in range(2)))
            post = dict(event, hook_event_name='PostToolUse')
            if not post_path:post.pop('transcript_path')
            quiet = StringIO();await notifications.hook(client, post, ROOT, output=quiet)
            assert quiet.getvalue() == '' and await rows(runtime) == before
            assert not (directory / 'session.json').exists()
            assert (directory / 'session-start.json').read_bytes() == candidate
            transcript.write_text('native creates this only after the start returns')
            outputs = [StringIO(), StringIO()]
            await asyncio.gather(*(notifications.hook(client, post, ROOT, output=out) for out in outputs))
            assert sum(bool(out.getvalue()) for out in outputs) == 1
            assert 'ORRERY Mail notification:' in ''.join(out.getvalue() for out in outputs)
            receipt = json.loads((directory / 'session.json').read_text())
            assert receipt['native_session_id'] == event['session_id']
            assert receipt['transcript_path'] == str(transcript.resolve(strict=True))
            assert {k:receipt[k] for k in identity(client)} == identity(client)
            facts, pairs = await rows(runtime)
            assert facts[0]['completed_ts'] and pairs == [(sent['message_id'],people[0]['agent_id'],None,None)]
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['absent-start','other-session','other-start','nonce','intent','current',
    'ended','generation','revoked','lineage','outside','relative','path-type','symlink','same-home-symlink',
    'resume','candidate-mode','candidate-conflict','candidate-schema'])
def test_deferred_session_gates_reject_without_receipts_or_notification_consumption(tmp_path, monkeypatch, damage):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            event, transcript, directory = await deferred_start(client, tmp_path, monkeypatch)
            saved = (directory / 'session-start.json').read_bytes()
            transcript.write_text('present after start')
            post = dict(event, hook_event_name='PostToolUse')
            intent_path = directory / 'intent.json';intent = json.loads(intent_path.read_text())
            if damage == 'absent-start':(directory / 'session-start.json').unlink()
            elif damage in {'other-session','other-start'}:
                post['session_id'] = str(uuid4())
                if damage == 'other-start':post['hook_event_name'] = 'SessionStart'
            elif damage == 'nonce':monkeypatch.setenv('AGENTSTACK_FRESH_LAUNCH_NONCE','foreign')
            elif damage in {'intent','generation','resume'}:
                if damage == 'intent':intent['wrapper_root'] = '/changed-intent'
                elif damage == 'resume':intent['resume_session_id'] = str(uuid4())
                else:intent['credential_generation'] += 1
                intent_path.write_text(canonical(intent))
            elif damage == 'current':
                (layout(client) / 'current.json').write_text(canonical({'launch_id':str(uuid4())}))
            elif damage == 'ended':_write_private(directory / 'ended.json', {'launch_id':directory.name,'exit_code':0})
            elif damage == 'revoked':
                await runtime.core.recover(people[0]['agent_id'], expected_generation=people[0]['credential_generation'],
                                           new_token='deferred-synthetic-recovery-key')
            elif damage == 'lineage':
                _write_private(client.path.parent / 'child.json', {'parent_context':str(client.path),'slot':'missing'})
            elif damage == 'outside':post['transcript_path'] = str(tmp_path / 'outside.jsonl')
            elif damage == 'relative':post['transcript_path'] = 'relative.jsonl'
            elif damage == 'path-type':post['transcript_path'] = None
            elif damage in {'symlink','same-home-symlink'}:
                other = (tmp_path if damage == 'symlink' else transcript.parent) / 'other.jsonl'
                other.write_text('changed target')
                transcript.unlink();transcript.symlink_to(other)
            elif damage == 'candidate-mode':(directory / 'session-start.json').chmod(0o644)
            else:
                path = directory / 'session-start.json';value = json.loads(path.read_text())
                if damage == 'candidate-schema':value.pop('transcript_path')
                else:value['intent_digest'] = 'changed'
                path.write_text(canonical(value))
            before = await rows(runtime)
            output = StringIO()
            with pytest.raises(B1Error):await notifications.hook(client, post, ROOT, output=output)
            assert output.getvalue() == '' and await rows(runtime) == before
            assert not (directory / 'session.json').exists()
            assert not list((directory.parent.parent / 'sessions').iterdir())
            if damage not in {'absent-start','candidate-conflict','candidate-schema'}:
                assert (directory / 'session-start.json').read_bytes() == saved
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['start','post'])
def test_transcript_permission_error_is_never_deferred_as_missing(tmp_path, monkeypatch, phase):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            if phase == 'post':
                event, transcript, directory = await deferred_start(client, tmp_path, monkeypatch)
                event['hook_event_name'] = 'PostToolUse'
            else:
                event = running(client, tmp_path, monkeypatch);transcript = Path(event['transcript_path'])
                directory = layout(client) / 'runs' / os.environ['AGENTSTACK_FRESH_LAUNCH_ID']
            resolve = Path.resolve
            def denied(path, *args, **kwargs):
                if path == transcript and kwargs.get('strict'):raise PermissionError('synthetic denied')
                return resolve(path, *args, **kwargs)
            monkeypatch.setattr(Path, 'resolve', denied)
            before = await rows(runtime)
            with pytest.raises(PermissionError):await notifications.hook(client, event, ROOT, output=StringIO())
            assert await rows(runtime) == before and not (directory / 'session.json').exists()
            if phase == 'start':assert not (directory / 'session-start.json').exists()
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['same-home','outside-home'])
def test_repeated_start_rejects_transcript_switch_between_candidate_and_strict_resolve(tmp_path, monkeypatch, boundary):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            event, transcript, directory = await deferred_start(client, tmp_path, monkeypatch)
            candidate_path = directory / 'session-start.json';saved = candidate_path.read_bytes()
            candidate = json.loads(saved)
            transcript.write_text('original reference from admitted start')
            replacement = (transcript.parent if boundary == 'same-home' else tmp_path) / 'replacement.jsonl'
            replacement.write_text('different transcript at the same native path')
            resolve = Path.resolve;observed = []
            def switched(path, *args, **kwargs):
                if path == transcript and kwargs.get('strict'):
                    # Reproduce an actual filesystem replacement after the
                    # non-strict candidate comparison, before the strict check.
                    transcript.unlink();transcript.symlink_to(replacement)
                result = resolve(path, *args, **kwargs)
                if path == transcript:observed.append((bool(kwargs.get('strict')),str(result)))
                return result
            monkeypatch.setattr(Path, 'resolve', switched)
            before = await rows(runtime);output = StringIO()
            with pytest.raises(B1Error, match='FRESH_NATIVE_SESSION_MISMATCH'):
                await notifications.hook(client, event, ROOT, output=output)
            assert observed == [(False,candidate['resolved_transcript_path']),
                                (True,str(resolve(replacement,strict=True)))]
            assert observed[0][1] != observed[1][1] and transcript.is_symlink()
            assert candidate_path.read_bytes() == saved
            assert not (directory / 'session.json').exists()
            assert not list((directory.parent.parent / 'sessions').iterdir())
            assert output.getvalue() == '' and await rows(runtime) == before
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['outside','relative','path-type','nonce','ended','codex-missing'])
def test_invalid_or_nonclaude_start_never_creates_deferred_candidate(tmp_path, monkeypatch, damage):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            event = running(client, tmp_path, monkeypatch)
            directory = layout(client) / 'runs' / os.environ['AGENTSTACK_FRESH_LAUNCH_ID']
            Path(event['transcript_path']).unlink()
            if damage == 'outside':event['transcript_path'] = str(tmp_path / 'missing.jsonl')
            elif damage == 'relative':event['transcript_path'] = 'missing.jsonl'
            elif damage == 'path-type':event['transcript_path'] = None
            elif damage == 'nonce':monkeypatch.setenv('AGENTSTACK_FRESH_LAUNCH_NONCE','foreign')
            elif damage == 'ended':_write_private(directory / 'ended.json', {'launch_id':directory.name,'exit_code':0})
            else:
                path = directory / 'intent.json';intent = json.loads(path.read_text());intent['provider'] = 'codex'
                path.write_text(canonical(intent))
                event['transcript_path'] = str(client.path.parent / 'providers/codex/missing.jsonl')
            before = await rows(runtime)
            with pytest.raises((B1Error, TypeError, FileNotFoundError)):
                await notifications.hook(client, event, ROOT, output=StringIO())
            assert not (directory / 'session-start.json').exists() and not (directory / 'session.json').exists()
            assert not list((directory.parent.parent / 'sessions').iterdir()) and await rows(runtime) == before
    asyncio.run(run())


@pytest.mark.parametrize('conflict', [False, True])
def test_partial_receipt_publication_preserves_pending_and_only_exact_retry_can_finish(tmp_path, monkeypatch, conflict):
    async def run():
        async with live(tmp_path) as (runtime, people, client, _):
            await runtime.core.write_message('send', **send_args(runtime, people,
                auth=people[1]['auth'], recipients=[{'target_name':'Alpha'}]))
            event, transcript, directory = await deferred_start(client, tmp_path, monkeypatch)
            transcript.write_text('now available');post = dict(event, hook_event_name='PostToolUse')
            real_write = fresh_launch._write_private
            def broken(path, value):
                if path == directory / 'session.json':raise OSError('synthetic second publication failure')
                return real_write(path, value)
            monkeypatch.setattr(fresh_launch, '_write_private', broken)
            before = await rows(runtime)
            output = StringIO()
            with pytest.raises(OSError, match='synthetic second publication failure'):
                await notifications.hook(client, post, ROOT, output=output)
            first = layout(client) / 'sessions' / (event['session_id'] + '.json')
            saved = first.read_bytes()
            assert output.getvalue() == '' and await rows(runtime) == before
            assert not (directory / 'session.json').exists()
            monkeypatch.setattr(fresh_launch, '_write_private', real_write)
            if conflict:
                value = json.loads(saved);value['native_session_id'] = str(uuid4());first.write_text(canonical(value))
                with pytest.raises(B1Error, match='FRESH_NATIVE_SESSION_MISMATCH'):
                    await notifications.hook(client, post, ROOT, output=output)
                assert output.getvalue() == '' and await rows(runtime) == before
                assert not (directory / 'session.json').exists()
            else:
                await notifications.hook(client, post, ROOT, output=output)
                assert 'ORRERY Mail notification:' in output.getvalue() and first.read_bytes() == saved
                assert (directory / 'session.json').is_file()
                facts, pairs = await rows(runtime)
                assert facts[0]['completed_ts'] and pairs[0][2:] == (None,None)
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
