"""Unpublished B1 contracts. No settings, transport, bootstrap, or activation.

Proofs identify a Mail agent; names only select a target in a transaction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .utils import sanitize_agent_name

RULESET = 'storage-origin-sprime-v1'
MAX_INT = 2**63 - 1
ABSENT = object()


class B1Error(ValueError):
    """Fixed diagnostic codes; never interpolate caller values or credentials."""
    def __init__(self, code: str, **details: Any):
        super().__init__(code)
        self.code, self.details = code, details


def integer(value: Any, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_INT:
        raise B1Error('INVALID_INTEGER')
    return value


def string(value: Any, maximum: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise B1Error('INVALID_STRING')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise B1Error('INVALID_STRING') from None
    return value


def token(value: Any, *, fresh: bool = False) -> str:
    try:
        value = string(value, 64)
    except B1Error:
        raise B1Error('AUTH_PROOF_INCOMPLETE') from None
    if fresh and len(value) < 20:
        raise B1Error('INVALID_NEW_CREDENTIAL')
    return value


def fingerprint(value: str) -> str:
    return hashlib.sha256(token(value).encode('utf-8')).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def name_keys(value: Any) -> tuple[str, ...]:
    value = string(value)
    cleaned = sanitize_agent_name(value)
    if not cleaned:
        raise B1Error('INVALID_TARGET_NAME')
    return tuple(sorted({value.lower(), cleaned.lower()}))


@dataclass(frozen=True)
class Auth:
    actor_agent_id: int
    registration_token: str = field(repr=False)
    expected_credential_generation: int

    def __post_init__(self):
        integer(self.actor_agent_id)
        token(self.registration_token)
        integer(self.expected_credential_generation, minimum=0)

    @classmethod
    def parse(cls, value: Any) -> 'Auth':
        fields = {'actor_agent_id', 'registration_token', 'expected_credential_generation'}
        if not isinstance(value, dict) or set(value) != fields:
            raise B1Error('AUTH_PROOF_INCOMPLETE')
        return cls(integer(value['actor_agent_id']), token(value['registration_token']),
                   integer(value['expected_credential_generation'], minimum=0))


@dataclass(frozen=True)
class OwnerBinding:
    mail_instance_id: str
    mail_agent_id: int
    credential_generation: int
    owner_storage_project_id: int

    def __post_init__(self):
        string(self.mail_instance_id, 64)
        integer(self.mail_agent_id)
        integer(self.credential_generation, minimum=0)
        integer(self.owner_storage_project_id)


@dataclass(frozen=True)
class ConsumptionRequest:
    mail_instance_id: str
    auth: Auth = field(repr=False)
    target_agent_id: int
    message_ids: tuple[int, ...]
    request_id: str

    def __post_init__(self):
        string(self.mail_instance_id, 64)
        if not isinstance(self.auth, Auth):
            raise B1Error('AUTH_PROOF_INCOMPLETE')
        integer(self.target_agent_id)
        string(self.request_id)
        if type(self.message_ids) is not tuple or not 1 <= len(self.message_ids) <= 100:
            raise B1Error('INVALID_MESSAGE_IDS')
        for item in self.message_ids:
            integer(item)
        if len(set(self.message_ids)) != len(self.message_ids):
            raise B1Error('INVALID_MESSAGE_IDS')


@dataclass(frozen=True)
class DeliveryTarget:
    """Worker target descriptor, not an owner proof or worker authorization.

    B5 must authenticate a private worker channel before accepting a D fact.
    No model-facing tool accepts this descriptor in B1.
    """
    mail_instance_id: str
    recipient_agent_id: int
    message_id: int
    delivery_epoch: str
    binding_receipt_id: str

    def __post_init__(self):
        string(self.mail_instance_id, 64)
        integer(self.recipient_agent_id)
        integer(self.message_id)
        string(self.delivery_epoch)
        string(self.binding_receipt_id)


# Audit the old surface. This is not a promise to publish/port all 25 wires.
POLICY = {
    **dict.fromkeys(('health_check', 'whois', 'fetch_inbox', 'fetch_topic', 'fetch_summary',
                    'search_messages', 'summarize_thread', 'list_contacts'), 'pure_read'),
    'ensure_project': 'private_bootstrap', 'register_agent': 'private_registration',
    **dict.fromkeys(('send_message', 'reply_message'), 'owner_decision'),
    **dict.fromkeys(('mark_message_read', 'acknowledge_message'), 'owner_recipient'),
    **dict.fromkeys(('file_reservation_paths', 'renew_file_reservations',
                    'release_file_reservations'), 'owner_lease_b3'),
    **dict.fromkeys(('set_contact_policy', 'request_contact', 'respond_contact'), 'owner_contact_b4'),
    'retire_agent': 'owner_identity', 'unretire_agent': 'private_management',
    **dict.fromkeys(('macro_start_session', 'macro_file_reservation_cycle',
                    'macro_contact_handshake'), 'unpublished_compound'),
}

AUTH_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'actor_agent_id': {'type': 'integer', 'minimum': 1, 'maximum': MAX_INT},
                   'registration_token': {'type': 'string', 'minLength': 1, 'maxLength': 64},
                   'expected_credential_generation': {'type': 'integer', 'minimum': 0, 'maximum': MAX_INT}},
    'required': ['actor_agent_id', 'registration_token', 'expected_credential_generation'],
}
