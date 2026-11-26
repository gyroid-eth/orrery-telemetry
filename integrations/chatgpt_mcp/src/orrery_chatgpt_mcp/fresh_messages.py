"""Fresh-only delegated sends through a prepared, fixed protected Mail client."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import hmac
import json
import math
from pathlib import Path
import re
import time
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .message_state import MessageState, read_private_json
from .model import AGENT_NAME, ToolError, _schema

INSTANCE = re.compile(r'[0-9a-f]{32}')
KEY = re.compile(r'[A-Za-z0-9_.:-]{1,128}')
PREVIEW_SECONDS = 300
MAX_INTENTS = 10000
MAX_RECIPIENTS = 16
NOTE = ('Mail acceptance is not proof of notification delivery or agent execution. '
        'Status only reads saved local evidence; unknown results stay unknown and are never resent automatically. '
        'Chat confirmation is not server-verified human approval; the operator delegates send authority.')


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def positive(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def bounded_text(value: object, size: int, *, multiline: bool = False) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        if len(value.encode('utf-8')) > size:
            return False
    except UnicodeError:
        return False
    return not any((ord(c) < 32 and (not multiline or c not in '\n\t')) or ord(c) == 127 for c in value)


class BoundMail:
    """No context, credential, module or endpoint is accepted from tools."""
    def __init__(self, context: Path):
        from agentstack_mail.fresh_client import FreshBoundClient
        self.client = FreshBoundClient(context)
        self.check()
        self.instance = self.client.context['mail_instance_id']
        self.sender_id = self.client.context['mail_agent_id']
        self.binding_digest = self.client.context_digest

    def check(self) -> None:
        context, _ = self.client._load()
        if context['server_mode'] != 'fresh':
            raise ValueError('fresh mode required')

    def call(self, tool: str, args: dict, request_id: str | None = None) -> dict:
        return asyncio.run(self.client.call(tool, args, request_id=request_id))


PREVIEW_FIELDS = {
    'idempotency_key': {'type': 'string', 'pattern': '^' + KEY.pattern + '$'},
    'mail_instance_id': {'type': 'string', 'pattern': '^' + INSTANCE.pattern + '$'},
    'target_agent_id': {'type': 'integer', 'minimum': 1},
    'expected_target_name': {'type': 'string'},
    'subject': {'type': 'string', 'maxLength': 200},
    'body_md': {'type': 'string', 'maxLength': 65536},
}
TOOL_DEFINITIONS = [
    {'name': 'orrery_message_preview', 'title': 'Preview a fresh Mail instruction',
     'description': 'Persist the exact body and one allowed recipient without sending. ' + NOTE,
     'inputSchema': _schema(PREVIEW_FIELDS, list(PREVIEW_FIELDS)),
     'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'orrery_message_commit', 'title': 'Send the previewed fresh Mail instruction',
     'description': 'One durable send claim; requires the exact preview ID and digest. ' + NOTE,
     'inputSchema': _schema({'idempotency_key': PREVIEW_FIELDS['idempotency_key'],
                             'preview_id': {'type': 'string'}, 'content_digest': {'type': 'string'}},
                            ['idempotency_key', 'preview_id', 'content_digest']),
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'orrery_message_status', 'title': 'Read saved fresh Mail send evidence',
     'description': 'Read only the saved local receipt; never contact Mail, send, replay or change the ledger. ' + NOTE,
     'inputSchema': _schema({'idempotency_key': PREVIEW_FIELDS['idempotency_key']}, ['idempotency_key']),
     'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
]
for definition in TOOL_DEFINITIONS:
    definition['securitySchemes'] = [{'type': 'noauth'}]


class FreshMessages:
    def __init__(self, policy_path: Path, state_dir: Path):
        self.path = policy_path
        self.policy = read_private_json(policy_path)
        self._validate_policy(self.policy)
        self.policy_digest = digest(self.policy)
        self.mail = BoundMail(Path(self.policy['context_path']))
        if (self.mail.instance != self.policy['mail_instance_id']
                or self.mail.sender_id != self.policy['sender_agent_id']):
            raise ValueError('fresh sender and instance do not match the fixed policy')
        self.binding = digest({'policy': self.policy_digest, 'context': self.mail.binding_digest})
        self.state = MessageState(state_dir)

    @staticmethod
    def _validate_policy(policy: dict) -> None:
        required = {'kind', 'context_path', 'mail_instance_id', 'sender_agent_id', 'recipients'}
        if (set(policy) != required or policy['kind'] != 'orrery-mcp-fresh-message-policy-v1'
                or not isinstance(policy['mail_instance_id'], str)
                or not INSTANCE.fullmatch(policy['mail_instance_id']) or not positive(policy['sender_agent_id'])
                or not isinstance(policy['context_path'], str)
                or not Path(policy['context_path']).is_absolute()
                or Path(policy['context_path']).resolve() != Path(policy['context_path'])):
            raise ValueError('invalid fixed fresh message policy')
        recipients = policy['recipients']
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= MAX_RECIPIENTS:
            raise ValueError('an explicit allowlist of 1 to 16 recipients is required')
        seen = set()
        for recipient in recipients:
            if (not isinstance(recipient, dict)
                    or set(recipient) != {'target_agent_id', 'expected_target_name', 'kind'}
                    or not positive(recipient['target_agent_id'])
                    or not isinstance(recipient['expected_target_name'], str)
                    or not AGENT_NAME.fullmatch(recipient['expected_target_name'])
                    or recipient['kind'] != 'to' or recipient['target_agent_id'] in seen):
                raise ValueError('invalid or duplicate allowlisted recipient')
            seen.add(recipient['target_agent_id'])

    def check(self) -> None:
        try:
            if digest(read_private_json(self.path)) != self.policy_digest:
                raise ValueError('changed policy')
            self.mail.check()
        except Exception:
            raise ToolError('message_configuration_changed', 'fixed message authority is unavailable; no send attempted') from None

    def public_policy(self) -> dict:
        return {'mail_instance_id': self.mail.instance, 'sender_agent_id': self.mail.sender_id,
                'recipients': json.loads(canonical(self.policy['recipients'])),
                'approval_mode': 'operator_delegated', 'unknown_recovery': 'operator_review_no_automatic_resend'}

    def _envelope(self, args: dict) -> dict:
        if (args['mail_instance_id'] != self.mail.instance or not positive(args['target_agent_id'])
                or not isinstance(args['expected_target_name'], str)):
            raise ToolError('recipient_not_allowed', 'recipient does not match the fixed authority')
        recipient = {'target_agent_id': args['target_agent_id'],
                     'expected_target_name': args['expected_target_name'], 'kind': 'to'}
        if recipient not in self.policy['recipients']:
            raise ToolError('recipient_not_allowed', 'recipient does not match the fixed authority')
        if (not bounded_text(args['subject'], 200)
                or not bounded_text(args['body_md'], 65536, multiline=True)):
            raise ToolError('invalid_arguments', 'subject or body is empty, unsafe or too large')
        return {'recipients': [recipient], 'subject': args['subject'], 'body_md': args['body_md']}

    def invoke(self, name: str, args: object) -> dict:
        definition = next((d for d in TOOL_DEFINITIONS if d['name'] == name), None)
        if definition is None:
            raise ToolError('unknown_tool', 'unknown message tool')
        if not isinstance(args, dict) or set(args) != set(definition['inputSchema']['required']):
            raise ToolError('invalid_arguments', 'supply exactly the documented fields')
        key = args.get('idempotency_key')
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ToolError('invalid_arguments', 'invalid idempotency key')
        key_hash = digest(key)
        if name == 'orrery_message_status':
            return self._public(self._record(key_hash))
        self.check()
        if name == 'orrery_message_preview':
            return self.preview(key_hash, self._envelope(args))
        if (not isinstance(args['preview_id'], str) or not isinstance(args['content_digest'], str)
                or len(args['preview_id']) != 36 or not re.fullmatch(r'[a-f0-9]{64}', args['content_digest'])):
            raise ToolError('invalid_arguments', 'invalid preview proof')
        return self.commit(key_hash, args['preview_id'], args['content_digest'])

    def _request_id(self, key_hash: str) -> str:
        return str(uuid5(NAMESPACE_URL, 'urn:orrery:mcp:message:' + canonical(
            [self.mail.instance, self.mail.sender_id, key_hash])))

    def _bound(self, record: dict, key_hash: str) -> dict:
        if (not isinstance(record, dict) or set(record) != {'binding', 'preview_id', 'request_id',
                'content_digest', 'envelope', 'created_at', 'expires_at', 'state', 'receipt'}):
            raise ToolError('message_state_invalid', 'saved message evidence is inconsistent; preserve state and do not retry')
        if record.get('binding') != self.binding:
            raise ToolError('message_binding_changed', 'request belongs to a different fixed authority')
        try:
            envelope = record['envelope']
            if (not isinstance(envelope, dict) or set(envelope) != {'recipients', 'subject', 'body_md'}
                    or not isinstance(envelope['recipients'], list) or len(envelope['recipients']) != 1):
                raise ValueError
            recipient = envelope['recipients'][0]
            if (not isinstance(recipient, dict)
                    or set(recipient) != {'target_agent_id', 'expected_target_name', 'kind'}
                    or recipient['kind'] != 'to'):
                raise ValueError
            self._envelope(dict(mail_instance_id=self.mail.instance,
                target_agent_id=recipient['target_agent_id'], expected_target_name=recipient['expected_target_name'],
                subject=envelope['subject'], body_md=envelope['body_md']))
            expected_digest = digest({'instance': self.mail.instance, 'sender': self.mail.sender_id,
                                      'message': envelope})
            if (record['content_digest'] != expected_digest or record['request_id'] != self._request_id(key_hash)
                    or str(UUID(record['preview_id'])) != record['preview_id']
                    or record['state'] not in ('previewed', 'sending', 'unknown', 'accepted')
                    or any(type(record[key]) not in (int, float) or not math.isfinite(record[key])
                           for key in ('created_at', 'expires_at'))
                    or record['expires_at'] != record['created_at'] + PREVIEW_SECONDS):
                raise ValueError
            if record['state'] == 'accepted':
                verified = self._receipt_projection(record['receipt'])
                if verified is None or verified != record['receipt']:
                    raise ValueError
            elif record['receipt'] is not None:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError, ToolError):
            raise ToolError('message_state_invalid', 'saved message evidence is inconsistent; preserve state and do not retry') from None
        return record

    def _record(self, key_hash: str) -> dict:
        return self._bound(self.state.read(key_hash), key_hash)

    def preview(self, key_hash: str, envelope: dict) -> dict:
        content_digest = digest({'instance': self.mail.instance, 'sender': self.mail.sender_id, 'message': envelope})
        with self.state.transaction() as db:
            old = self.state.get(db, key_hash)
            if old is not None:
                self._bound(old, key_hash)
                if old['content_digest'] != content_digest:
                    raise ToolError('idempotency_conflict', 'this key already belongs to different content')
                return self._public(old, preview=True)
        recipient = envelope['recipients'][0]
        try:
            target = self.mail.call('whois', {k: v for k, v in recipient.items() if k != 'kind'})
            if (not isinstance(target, dict) or target.get('mail_instance_id') != self.mail.instance
                    or type(target.get('target_agent_id')) is not int
                    or target['target_agent_id'] != recipient['target_agent_id']
                    or target.get('name') != recipient['expected_target_name'] or target.get('state') != 'active'):
                raise ValueError('unverified target')
        except Exception:
            raise ToolError('recipient_unavailable', 'Mail could not verify the active recipient; nothing sent') from None
        now = time.time()
        record = {'binding': self.binding, 'preview_id': str(uuid4()),
                  'request_id': self._request_id(key_hash),
                  'content_digest': content_digest, 'envelope': envelope,
                  'created_at': now, 'expires_at': now + PREVIEW_SECONDS, 'state': 'previewed', 'receipt': None}
        with self.state.transaction() as db:
            old = self.state.get(db, key_hash)
            if old is not None:
                self._bound(old, key_hash)
                if old['content_digest'] != content_digest:
                    raise ToolError('idempotency_conflict', 'this key already belongs to different content')
                record = old
            else:
                if db.execute('SELECT count(*) FROM intents').fetchone()[0] >= MAX_INTENTS:
                    raise ToolError('message_ledger_full', 'local ledger is full; do not delete unresolved send evidence')
                self.state.put(db, key_hash, record)
        return self._public(record, preview=True)

    def commit(self, key_hash: str, preview_id: str, content_digest: str) -> dict:
        with self.state.transaction() as db:
            record = self.state.get(db, key_hash)
            if record is None:
                raise ToolError('not_found', 'preview not found')
            self._bound(record, key_hash)
            if (not hmac.compare_digest(record['preview_id'], preview_id)
                    or not hmac.compare_digest(record['content_digest'], content_digest)):
                raise ToolError('preview_mismatch', 'commit must match the exact preview')
            if record['state'] != 'previewed':
                return self._public(record)
            if not record['created_at'] <= time.time() < record['expires_at']:
                raise ToolError('preview_expired', 'preview expired or clock moved backwards; nothing sent')
            record['state'] = 'sending'
            self.state.put(db, key_hash, record)
        # The claim is durable before transport. A crash keeps it unknown.
        try:
            self.check()
            receipt = self.mail.call('send_message', record['envelope'], record['request_id'])
            projected = self._receipt(receipt, record)
        except Exception:
            projected = None
        record['receipt'] = projected
        record['state'] = 'accepted' if projected is not None else 'unknown'
        with self.state.transaction() as db:
            current = self._bound(self.state.get(db, key_hash), key_hash)
            if current['state'] == 'accepted':
                record = current  # Preserve any already persisted acceptance.
            else:
                self.state.put(db, key_hash, record)
        return self._public(record)

    def _receipt_projection(self, value: object) -> dict | None:
        if (not isinstance(value, dict) or value.get('mail_instance_id') != self.mail.instance
                or not positive(value.get('sender_id')) or value['sender_id'] != self.mail.sender_id
                or not positive(value.get('message_id')) or value.get('delivery_status') != 'notification_pending'
                or not bounded_text(value.get('thread_id'), 512) or not bounded_text(value.get('created_ts'), 128)):
            return None
        return {key: value[key] for key in ('mail_instance_id', 'message_id', 'sender_id', 'thread_id',
                                           'created_ts', 'delivery_status')}

    def _receipt(self, value: object, record: dict) -> dict | None:
        projected = self._receipt_projection(value)
        if projected is None or value.get('decision_only') is not False:
            return None
        targets = record['envelope']['recipients']
        rows = value.get('recipients')
        if (not isinstance(rows, list) or len(rows) != len(targets)
                or any(not isinstance(row, dict) or not positive(row.get('agent_id'))
                       or row['agent_id'] != target['target_agent_id'] or row.get('kind') != target['kind']
                       for row, target in zip(rows, targets))):
            return None
        return projected

    def _public(self, record: dict, *, preview: bool = False) -> dict:
        state = 'unknown' if record['state'] == 'sending' else record['state']
        result = {'status': state, 'preview_id': record['preview_id'], 'content_digest': record['content_digest'],
                  'request_id': record['request_id'], 'mail_instance_id': self.mail.instance,
                  'sender_agent_id': self.mail.sender_id, 'expires_at': record['expires_at'],
                  'recipients': json.loads(canonical(record['envelope']['recipients'])),
                  'receipt': record['receipt'], 'execution_status': 'unobserved',
                  'automatic_resend': False, 'note': NOTE}
        if state == 'previewed' and not record['created_at'] <= time.time() < record['expires_at']:
            result['status'] = 'expired'
        if preview:
            result['message'] = json.loads(canonical(record['envelope']))
        return result
