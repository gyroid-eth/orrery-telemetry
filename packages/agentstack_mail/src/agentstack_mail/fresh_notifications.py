"""Fresh notification facts and the existing Claude hook handoff pattern.

The database is authoritative. Signals are reconstructible hints, not owner
proof or delivery receipts. A flushed hook response is a handoff, not read/ACK
or evidence that the agent understood or completed its work. Output and the
database are separate effects: an uncertain completion may be notified again.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import runpy
import stat
import sys

from sqlalchemy import Column, ForeignKey, Integer, String, Table, insert, select, update
from sqlmodel import SQLModel

from .models import Message, MessageRecipient
from .storage_origin_contract import B1Error, integer

facts = Table('fresh_notification_facts', SQLModel.metadata,
    Column('message_id', Integer, ForeignKey('messages.id'), primary_key=True),
    Column('agent_id', Integer, ForeignKey('agents.id'), primary_key=True),
    Column('completed_ts', String, nullable=True))
IMPORTANCE = {'low': 0, 'normal': 1, 'high': 2, 'urgent': 3}


async def create(session, mid, recipients):
    for recipient in recipients:
        await session.execute(insert(facts).values(message_id=mid, agent_id=recipient['agent_id']))


def pending_where(aid):
    return (facts.c.agent_id == aid, facts.c.completed_ts.is_(None),
            MessageRecipient.read_ts.is_(None), MessageRecipient.ack_ts.is_(None))


def joined():
    return facts.join(MessageRecipient, (facts.c.message_id == MessageRecipient.message_id)
                      & (facts.c.agent_id == MessageRecipient.agent_id)).join(
                          Message, Message.id == facts.c.message_id)


async def apply(core, operation, *, expected_server_instance_id, auth, message_id=None, limit=50,
                min_importance='low'):
    if operation not in {'pending', 'complete'}:raise B1Error('OPERATION_UNPUBLISHED')
    integer(limit)
    if limit > 100:raise B1Error('INVALID_LIMIT')
    if not isinstance(min_importance, str) or min_importance not in IMPORTANCE:
        raise B1Error('INVALID_IMPORTANCE')
    if message_id is not None:integer(message_id)
    if operation == 'complete' and message_id is None:raise B1Error('INVALID_ARGUMENTS')
    async with core.owner_transaction(expected_server_instance_id, auth) as ctx:
        s, actor = ctx.session, ctx.actor
        ctx.assert_live(s)
        identity = dict(mail_instance_id=ctx.state['instance_id'], mail_agent_id=actor.id,
                        credential_generation=actor.credential_generation)
        if operation == 'pending':
            eligible = [value for value, rank in IMPORTANCE.items() if rank >= IMPORTANCE[min_importance]]
            query = select(Message).select_from(joined()).where(*pending_where(actor.id),
                                                              Message.importance.in_(eligible))
            if message_id is not None:query = query.where(Message.id == message_id)
            rows = (await s.execute(query.order_by(Message.id).limit(limit))).scalars().all()
            return dict(identity, messages=[dict(id=m.id, sender_id=m.sender_id,
                sender_name=ctx.records[m.sender_id].name, subject=m.subject, body_md=m.body_md[:2001],
                importance=m.importance, ack_required=m.ack_required, thread_id=m.thread_id)
                for m in rows])
        pair = await s.get(MessageRecipient, (message_id, actor.id))
        row = (await s.execute(select(facts).where(facts.c.message_id == message_id,
                facts.c.agent_id == actor.id))).mappings().first()
        if pair is None or row is None or await s.get(Message, message_id) is None:
            raise B1Error('NOT_A_RECIPIENT')
        stamp = row['completed_ts'] or datetime.now(timezone.utc).isoformat()
        await s.execute(update(facts).where(facts.c.message_id == message_id,
            facts.c.agent_id == actor.id).values(completed_ts=stamp))
        result = dict(identity, message_id=message_id, completed_ts=stamp)
    await reconcile(core, [(message_id, result['mail_agent_id'])])
    return result


def envelope(core, mid, aid):
    return dict(kind='orrery-fresh-message-signal-v1', version=1,
                mail_instance_id=core.instance_id, recipient_agent_id=aid, message_id=mid)


async def reconcile(core, pairs):
    """Materialize only committed pairs, under the same DB writer ordering.

    Used after send/read/ACK/completion, never by peek. Missing hint files do
    not hide pending facts: the hook queries its authenticated server index.
    """
    from .fresh_runtime import _directory, _read_private, _write_private
    root = core.output_root / 'signals'
    for path in (root, root / 'agents'):
        path.mkdir(mode=0o700, exist_ok=True);_directory(path)
    async with core._transaction(write=True) as session:
        for mid, aid in pairs:
            folder = root / 'agents' / str(aid)
            folder.mkdir(mode=0o700, exist_ok=True);_directory(folder)
            path, value = folder / (str(mid) + '.signal'), envelope(core, mid, aid)
            desired = (await session.execute(select(Message.id).select_from(joined()).where(
                facts.c.message_id == mid, *pending_where(aid)))).scalar_one_or_none() is not None
            exists = path.exists() or path.is_symlink()
            if exists and _read_private(path) != value:raise B1Error('FRESH_SIGNAL_CONFLICT')
            if desired and not exists:_write_private(path, value)
            elif not desired and exists:path.unlink()


@contextmanager
def consumer_lock(client):
    """One hook at a time per I/A, including distinct prepared client contexts."""
    from .fresh_runtime import _directory
    root = Path(client.context['root'])
    parent = root / 'notification-consumers'
    parent.mkdir(mode=0o700, exist_ok=True);_directory(parent)
    directory = parent / str(client.context['mail_agent_id'])
    directory.mkdir(mode=0o700, exist_ok=True);_directory(directory)
    fd = os.open(directory / 'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):raise B1Error('FRESH_PRIVATE_FILE_UNSAFE')
        try:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:yield True
    finally:os.close(fd)


def current_hook(client, payload):
    from .fresh_launch import identity, layout, native_id, pinned
    from .fresh_runtime import _directory, _read_private
    base = layout(client)
    launch_id = native_id(os.environ.get('AGENTSTACK_FRESH_LAUNCH_ID'))
    run = base / 'runs' / launch_id
    _directory(run)
    intent = _read_private(run / 'intent.json');pinned(client, intent)
    receipt = _read_private(run / 'session.json');pinned(client, receipt)
    if (intent['provider'] != 'claude' or receipt.get('provider') != 'claude'
            or receipt.get('launch_id') != launch_id
            or receipt.get('native_session_id') != payload.get('session_id')
            or intent.get('nonce') != os.environ.get('AGENTSTACK_FRESH_LAUNCH_NONCE')
            or _read_private(base / 'current.json') != {'launch_id': launch_id}
            or (run / 'ended.json').exists()):raise B1Error('FRESH_LAUNCH_RECEIPT_MISMATCH')
    return identity(client)


async def hook(client, payload, wrapper_root, *, output=None):
    from .fresh_launch import complete_pending_session, record_session, root_path
    from .fresh_proxy import verified_owner
    output = sys.stdout if output is None else output
    if not isinstance(payload, dict) or payload.get('hook_event_name') not in {'SessionStart', 'PostToolUse'}:
        raise B1Error('FRESH_SESSION_EVENT_INVALID')
    if payload['hook_event_name'] == 'SessionStart':
        if not await record_session(client, payload, defer_transcript=True):return
    elif not await complete_pending_session(client, payload):return
    expected = current_hook(client, payload)
    await verified_owner(client)
    # A resumed native session can accept hook context without starting a turn.
    # Keep its notification pending for the normal watcher wake; source is not
    # owner proof, so even unknown/missing sources pass the receipt gates first.
    if payload['hook_event_name'] == 'SessionStart':
        source = payload.get('source')
        if not isinstance(source, str) or source != 'startup':return
    root = root_path(wrapper_root)
    helpers = runpy.run_path(str(root / 'bin/lib/runtime_delivery.py'))
    with consumer_lock(client) as acquired:
        if not acquired:return
        if os.environ.get('AGENTSTACK_MAIL_DISABLED') == '1':return
        # Query the current server fact, not filenames or a cached send receipt.
        minimum = os.environ.get('AGENTSTACK_MAIL_NOTIFY_MIN_IMPORTANCE', 'low').lower()
        # Match the existing notification_eligible reader: unknown is normal.
        if minimum not in IMPORTANCE:minimum = 'normal'
        batch = await client.call('notification_pending', {'min_importance': minimum})
        if any(batch.get(k) != value for k, value in expected.items()):
            raise B1Error('FRESH_PROXY_BINDING_MISMATCH')
        messages = [m for m in batch['messages'] if helpers['notification_eligible'](m)]
        notices, selected = [], []
        for message in messages:
            latest = await client.call('notification_pending',
                {'message_id': message['id'], 'limit': 1, 'min_importance': minimum})
            if any(latest.get(k) != value for k, value in expected.items()):
                raise B1Error('FRESH_PROXY_BINDING_MISMATCH')
            if not latest['messages']:continue
            current_hook(client, payload)
            notices.append(helpers['format_notification'](latest['messages'][0]));selected.append(message['id'])
        if not notices:return
        result = {'hookSpecificOutput': {'hookEventName': payload['hook_event_name'],
                  'additionalContext': '\n'.join(notices)}}
        output.write(json.dumps(result, ensure_ascii=False) + '\n')
        output.flush()  # Never consume before a successful, explicit handoff.
        for mid in selected:
            current_hook(client, payload)
            await client.call('notification_complete', {'message_id': mid})
