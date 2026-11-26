"""Fail closed on bounded authority and inconsistent private send evidence."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from agentstack_mail.fresh_runtime import FreshRuntime
from agentstack_mail.models import Message
from orrery_chatgpt_mcp import fresh_messages as messages
from orrery_chatgpt_mcp.model import ToolError
from test_fresh_messages import setup, preview, commit, status
from test_fresh_message_e2e import live, stored, unwrap, commit_args


@pytest.mark.parametrize('count', [16, 17])
def test_policy_recipient_limit_is_explicit_and_never_truncates(setup, count):
    manager, _, path, policy = setup
    policy['recipients'] = [
        {'target_agent_id': 100 + i, 'expected_target_name': f'Recipient{i}', 'kind': 'to'}
        for i in range(count)
    ]
    path.write_text(json.dumps(policy))
    before = path.read_bytes()
    if count == 16:
        checked = messages.FreshMessages(path, manager.state.root.parent)
        assert checked.public_policy()['recipients'] == policy['recipients']
        assert checked.mail.calls == []
    else:
        with pytest.raises(ValueError, match='16'):
            messages.FreshMessages(path, manager.state.root.parent)
    assert path.read_bytes() == before


@pytest.mark.parametrize('operation', ['preview', 'commit', 'status'])
@pytest.mark.parametrize('change', [
    'body', 'subject', 'recipient', 'extra_field', 'envelope_shape',
    'digest', 'request_id', 'preview_id', 'state', 'expiry', 'nonfinite_time',
])
def test_inconsistent_ledger_never_reaches_mail(setup, operation, change):
    manager, args, path, _ = setup
    first = preview(manager, args)
    key_hash = messages.digest(args['idempotency_key'])
    with manager.state.transaction() as db:
        record = manager.state.get(db, key_hash)
        if change in ('body', 'subject'):
            record['envelope']['body_md' if change == 'body' else 'subject'] = 'changed private content'
        elif change == 'recipient':
            record['envelope']['recipients'][0]['target_agent_id'] = 9
            record['content_digest'] = messages.digest({
                'instance': manager.mail.instance, 'sender': manager.mail.sender_id,
                'message': record['envelope'],
            })
        elif change == 'extra_field':
            record['envelope']['endpoint'] = 'http://untrusted'
        elif change == 'envelope_shape':
            record['envelope'] = []
        elif change == 'digest':
            record['content_digest'] = '0' * 64
        elif change == 'request_id':
            record['request_id'] = str(uuid4())
        elif change == 'preview_id':
            record['preview_id'] = 'invalid-preview'
        elif change == 'state':
            record['state'] = 'invented-success'
        elif change == 'expiry':
            record['expires_at'] += 3600
        else:
            record['created_at'] = float('inf')
        manager.state.put(db, key_hash, record)
    before = deepcopy(record)
    restarted = messages.FreshMessages(path, manager.state.root.parent)
    with pytest.raises(ToolError) as error:
        if operation == 'preview':
            preview(restarted, args)
        elif operation == 'commit':
            commit(restarted, args, first)
        else:
            status(restarted, args)
    assert error.value.code == 'message_state_invalid'
    assert 'changed private content' not in str(error.value)
    assert restarted.mail.calls == []
    assert restarted.state.read(key_hash) == before


def test_stored_mail_corruption_cannot_be_accepted_by_a_new_adapter_state(live):
    first_preview = unwrap(live.rpc('orrery_message_preview', live.args))
    accepted = unwrap(live.rpc('orrery_message_commit', commit_args(live, first_preview)))

    async def corrupt_fixture():
        runtime = await FreshRuntime.open(live.root / 'mail/config.json')
        try:
            async with runtime.sessions() as session:
                message = await session.get(Message, accepted['receipt']['message_id'])
                message.body_md = 'deliberately changed fixture body'
                await session.commit()
        finally:
            await runtime.close()

    asyncio.run(corrupt_fixture())
    before = stored(live)
    other = messages.FreshMessages(live.policy, live.root / 'other-state')
    again = preview(other, live.args)
    assert again['request_id'] == first_preview['request_id']
    result = commit(other, live.args, again)
    assert result['status'] == 'unknown' and result['receipt'] is None
    checked = status(other, live.args)
    assert checked['status'] == 'unknown'
    assert checked == result
    assert commit(other, live.args, again) == checked
    assert stored(live) == before


@pytest.mark.parametrize('operation', ['commit', 'status'])
@pytest.mark.parametrize('change', [
    'missing_receipt', 'wrong_instance', 'wrong_sender', 'invalid_message_id',
    'private_receipt_field', 'receipt_on_unknown', 'unsupported_state',
])
def test_inconsistent_terminal_evidence_is_not_success_or_public_data(setup, operation, change):
    manager, args, path, _ = setup
    first = preview(manager, args)
    assert commit(manager, args, first)['status'] == 'accepted'
    key_hash = messages.digest(args['idempotency_key'])
    with manager.state.transaction() as db:
        record = manager.state.get(db, key_hash)
        if change == 'missing_receipt':
            record['receipt'] = None
        elif change == 'wrong_instance':
            record['receipt']['mail_instance_id'] = 'f' * 32
        elif change == 'wrong_sender':
            record['receipt']['sender_id'] += 1
        elif change == 'invalid_message_id':
            record['receipt']['message_id'] = True
        elif change == 'private_receipt_field':
            record['receipt']['body_md'] = 'private-fixture-text'
        elif change == 'receipt_on_unknown':
            record['state'] = 'unknown'
        else:
            record.update(state='unsupported', receipt=None)
        manager.state.put(db, key_hash, record)
    restarted = messages.FreshMessages(path, manager.state.root.parent)
    with pytest.raises(ToolError) as error:
        if operation == 'commit':
            commit(restarted, args, first)
        else:
            status(restarted, args)
    assert error.value.code == 'message_state_invalid'
    assert 'private-fixture-text' not in str(error.value)
    assert restarted.mail.calls == []
    assert restarted.state.read(key_hash) == record
