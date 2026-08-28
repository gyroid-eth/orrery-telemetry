"""Transport-independent authorization catalog and shadow observations.

The catalog describes the published compatibility surface without
adding credentials to tool schemas. The retire rule records the current
loopback local-process boundary; other rules remain prospective and
non-binding. Shadow observations are diagnostic only: they never authorize or
deny the wrapped operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from collections.abc import Iterable, Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TypeAlias

from .contract import COMPATIBILITY_TOOLS

logger = logging.getLogger(__name__)

LOCAL_SINGLE_PRINCIPAL = "local-single-principal"
AUTHORIZATION_FIXTURE = "authorization-tools-v1.json"
AUTHORIZATION_FIXTURE_SHA256 = (
    "73df37ad341d5aa5e8d67d63dde0040b5a4a1b50909753c0c7ebcb3214ef61f3"
)


def _entry(
    *,
    subject: str,
    action: str,
    resource: str,
    required_arguments: tuple[str, ...],
    current_credential_arguments: tuple[str, ...] = (),
    authorization_rule: str,
) -> dict[str, object]:
    return {
        "subject": subject,
        "action": action,
        "resource": resource,
        "required_arguments": required_arguments,
        "current_credential_arguments": current_credential_arguments,
        "authorization_rule": authorization_rule,
    }


AUTHORIZATION_CATALOG: dict[str, dict[str, object]] = {
    "acknowledge_message": _entry(
        subject="agent:{agent_name}",
        action="acknowledge_received_message",
        resource="project:{project_key}/message:{message_id}/recipient:{agent_name}",
        required_arguments=("project_key", "agent_name", "message_id"),
        authorization_rule="recipient agent owner or project administrator",
    ),
    "ensure_project": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="ensure_project",
        resource="project:{human_key}",
        required_arguments=("human_key",),
        authorization_rule="bound local principal; project administrator after principal subdivision",
    ),
    "fetch_inbox": _entry(
        subject="agent:{agent_name}",
        action="read_agent_inbox",
        resource="project:{project_key}/agent:{agent_name}/inbox",
        required_arguments=("project_key", "agent_name"),
        authorization_rule="agent owner or project administrator",
    ),
    "fetch_summary": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="read_project_summaries",
        resource="project:{project_key}/summaries",
        required_arguments=("project_key",),
        authorization_rule="project member or project administrator",
    ),
    "fetch_topic": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="read_project_topic",
        resource="project:{project_key}/topic:{topic_name}",
        required_arguments=("project_key", "topic_name"),
        authorization_rule="project member or project administrator",
    ),
    "file_reservation_paths": _entry(
        subject="agent:{agent_name}",
        action="create_file_reservations",
        resource="project:{project_key}/agent:{agent_name}/file-reservations",
        required_arguments=("project_key", "agent_name", "paths"),
        authorization_rule="agent owner or project administrator",
    ),
    "health_check": _entry(
        subject="caller",
        action="read_service_health",
        resource="service",
        required_arguments=(),
        authorization_rule="public service read",
    ),
    "list_contacts": _entry(
        subject="agent:{agent_name}",
        action="read_agent_contacts",
        resource="project:{project_key}/agent:{agent_name}/contacts",
        required_arguments=("project_key", "agent_name"),
        authorization_rule="agent owner or project administrator",
    ),
    "macro_contact_handshake": _entry(
        subject="agent:{requester|agent_name}",
        action="request_accept_and_optionally_welcome_contact",
        resource="project:{project_key}/contact:{requester|agent_name}->{target|to_agent}",
        required_arguments=("project_key",),
        authorization_rule="requester agent owner; target mutation requires target owner or administrator",
    ),
    "macro_file_reservation_cycle": _entry(
        subject="agent:{agent_name}",
        action="create_and_optionally_release_file_reservations",
        resource="project:{project_key}/agent:{agent_name}/file-reservations",
        required_arguments=("project_key", "agent_name", "paths"),
        authorization_rule="agent owner or project administrator",
    ),
    "macro_start_session": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="bind_agent_start_session_reserve_and_read_inbox",
        resource="project:{human_key}/agent:{agent_name|generated}",
        required_arguments=("human_key", "program", "model"),
        authorization_rule="bound local principal may create or use only its agent; administrator otherwise",
    ),
    "mark_message_read": _entry(
        subject="agent:{agent_name}",
        action="mark_received_message_read",
        resource="project:{project_key}/message:{message_id}/recipient:{agent_name}",
        required_arguments=("project_key", "agent_name", "message_id"),
        authorization_rule="recipient agent owner or project administrator",
    ),
    "register_agent": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="create_or_refresh_agent_identity",
        resource="project:{project_key}/agent:{name|generated}",
        required_arguments=("project_key", "program", "model"),
        current_credential_arguments=("registration_token",),
        authorization_rule="bound local principal for new identity; agent owner or administrator for existing identity",
    ),
    "release_file_reservations": _entry(
        subject="agent:{agent_name}",
        action="release_file_reservations",
        resource="project:{project_key}/agent:{agent_name}/file-reservations",
        required_arguments=("project_key", "agent_name"),
        authorization_rule="reservation owner agent or project administrator",
    ),
    "renew_file_reservations": _entry(
        subject="agent:{agent_name}",
        action="renew_file_reservations",
        resource="project:{project_key}/agent:{agent_name}/file-reservations",
        required_arguments=("project_key", "agent_name"),
        authorization_rule="reservation owner agent or project administrator",
    ),
    "reply_message": _entry(
        subject="agent:{sender_name}",
        action="reply_as_agent",
        resource="project:{project_key}/message:{message_id}/reply",
        required_arguments=("project_key", "message_id", "sender_name", "body_md"),
        authorization_rule="sender agent owner or project administrator",
    ),
    "request_contact": _entry(
        subject="agent:{from_agent}",
        action="request_contact",
        resource="project:{project_key}/contact:{from_agent}->{to_project|project_key}:{to_agent}",
        required_arguments=("project_key", "from_agent", "to_agent"),
        authorization_rule="requesting agent owner or project administrator",
    ),
    "respond_contact": _entry(
        subject="agent:{to_agent}",
        action="accept_or_reject_contact_request",
        resource="project:{project_key}/contact:{from_project|project_key}:{from_agent}->{to_agent}",
        required_arguments=("project_key", "to_agent", "from_agent", "accept"),
        authorization_rule="target agent owner or project administrator",
    ),
    "retire_agent": _entry(
        subject="agent:{agent_name}",
        action="soft_retire_agent",
        resource="project:{project_key}/agent:{agent_name}",
        required_arguments=("project_key", "agent_name"),
        current_credential_arguments=("registration_token",),
        authorization_rule=(
            "bound loopback local principal may soft-retire any project target "
            "without its registration_token; retain the credential field for "
            "future project-administrator hardening"
        ),
    ),
    "unretire_agent": _entry(
        subject="agent:{agent_name}",
        action="restore_retired_agent",
        resource="project:{project_key}/agent:{agent_name}",
        required_arguments=("project_key", "agent_name"),
        current_credential_arguments=("registration_token",),
        authorization_rule=(
            "bound loopback local principal may restore any project target "
            "without its registration_token; retain the credential field for "
            "future project-administrator hardening, which should land on "
            "retire_agent and unretire_agent together"
        ),
    ),
    "search_messages": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="search_project_messages",
        resource="project:{project_key}/messages/search",
        required_arguments=("project_key", "query"),
        authorization_rule="project member or project administrator",
    ),
    "send_message": _entry(
        subject="agent:{sender_name}",
        action="send_message_as_agent",
        resource="project:{project_key}/outbox:{sender_name}/message:new",
        required_arguments=("project_key", "sender_name", "to", "subject", "body_md"),
        current_credential_arguments=("sender_token",),
        authorization_rule="sender agent owner or project administrator",
    ),
    "set_contact_policy": _entry(
        subject="agent:{agent_name}",
        action="set_agent_contact_policy",
        resource="project:{project_key}/agent:{agent_name}/contact-policy",
        required_arguments=("project_key", "agent_name", "policy"),
        authorization_rule="agent owner or project administrator",
    ),
    "summarize_thread": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="summarize_project_thread",
        resource="project:{project_key}/thread:{thread_id}",
        required_arguments=("project_key", "thread_id"),
        authorization_rule="project member or project administrator",
    ),
    "whois": _entry(
        subject=LOCAL_SINGLE_PRINCIPAL,
        action="read_agent_profile",
        resource="project:{project_key}/agent:{agent_name}/profile",
        required_arguments=("project_key", "agent_name"),
        authorization_rule="project member or project administrator",
    ),
}

ShadowDecision: TypeAlias = Literal["would_allow", "would_deny"]
ShadowAuthorizationPolicy: TypeAlias = Mapping[str, Mapping[str, str]]

# No policy is configured in the product yet. Missing, None, and empty policy
# all mean observe-only default allow; they must never introduce a denial.
SHADOW_AUTHORIZATION_POLICY: ShadowAuthorizationPolicy | None = None
_SHADOW_OBSERVATIONS: deque[dict[str, str]] = deque(maxlen=4096)
_ACTIVE_POLICY = object()
_CREDENTIAL_ARGUMENT_SUFFIXES = ("_token", "_secret", "_credential")
_SHADOW_POLICY_REASON_CODES = frozenset({"synthetic_test_denial"})


def record_shadow_authorization(
    tool_name: str,
    *,
    policy: ShadowAuthorizationPolicy | None | object = _ACTIVE_POLICY,
) -> dict[str, str]:
    """Record a future authorization result without enforcing it."""
    if tool_name not in AUTHORIZATION_CATALOG:
        raise KeyError(f"Tool {tool_name!r} is not in the authorization catalog")

    selected_policy = (
        SHADOW_AUTHORIZATION_POLICY if policy is _ACTIVE_POLICY else policy
    )
    override = selected_policy.get(tool_name) if selected_policy else None
    if not selected_policy:
        decision: ShadowDecision = "would_allow"
        reason = "policy_empty_default_allow"
    elif override is None:
        decision = "would_allow"
        reason = "policy_rule_missing_default_allow"
    else:
        raw_decision = override.get("decision")
        raw_reason = override.get("reason")
        if raw_decision not in {"would_allow", "would_deny"}:
            raise ValueError(
                f"Invalid shadow decision for {tool_name!r}: {raw_decision!r}"
            )
        if (
            not isinstance(raw_reason, str)
            or raw_reason not in _SHADOW_POLICY_REASON_CODES
        ):
            raise ValueError(
                f"Shadow decision for {tool_name!r} requires an approved reason code"
            )
        decision = raw_decision
        reason = raw_reason

    observation = {
        "principal_candidate": LOCAL_SINGLE_PRINCIPAL,
        "tool": tool_name,
        "decision": decision,
        "reason": reason,
    }
    _SHADOW_OBSERVATIONS.append(observation)
    logger.info("authorization_shadow", extra={"authorization_shadow": observation})
    return dict(observation)


def clear_shadow_authorization_observations() -> None:
    _SHADOW_OBSERVATIONS.clear()


def shadow_authorization_observations() -> tuple[dict[str, str], ...]:
    return tuple(dict(observation) for observation in _SHADOW_OBSERVATIONS)


def redact_tool_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return logger-safe arguments while preserving the real tool inputs."""
    return {
        name: (
            "<redacted>"
            if name.lower().endswith(_CREDENTIAL_ARGUMENT_SUFFIXES)
            else value
        )
        for name, value in arguments.items()
        if name != "ctx"
    }


def authorization_catalog_is_complete() -> bool:
    return frozenset(AUTHORIZATION_CATALOG) == COMPATIBILITY_TOOLS


def catalog_record_shape() -> frozenset[str]:
    return frozenset(
        {
            "subject",
            "action",
            "resource",
            "required_arguments",
            "current_credential_arguments",
            "authorization_rule",
        }
    )


def catalog_as_plain_data() -> dict[str, dict[str, Any]]:
    """Return a JSON-ready copy for audit/reporting without runtime objects."""
    return {
        tool_name: {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in record.items()
        }
        for tool_name, record in AUTHORIZATION_CATALOG.items()
    }


def assert_authorization_catalog_boundary(
    published_tool_names: Iterable[str],
) -> None:
    """Fail closed if source, fixture, or published tool names diverge."""
    expected_names = set(COMPATIBILITY_TOOLS)
    catalog_names = set(AUTHORIZATION_CATALOG)
    published_names = set(published_tool_names)
    if catalog_names != expected_names or published_names != expected_names:
        raise RuntimeError(
            "Authorization catalog boundary mismatch: "
            f"catalog_missing={sorted(expected_names - catalog_names)}, "
            f"catalog_extra={sorted(catalog_names - expected_names)}, "
            f"published_missing={sorted(expected_names - published_names)}, "
            f"published_extra={sorted(published_names - expected_names)}"
        )

    packaged_fixture = files("agentstack_mail").joinpath(
        f"fixtures/{AUTHORIZATION_FIXTURE}"
    )
    try:
        fixture_bytes = packaged_fixture.read_bytes()
    except FileNotFoundError:
        source_fixture = (
            Path(__file__).resolve().parents[2] / "fixtures" / AUTHORIZATION_FIXTURE
        )
        fixture_bytes = source_fixture.read_bytes()
    fixture_digest = hashlib.sha256(fixture_bytes).hexdigest()
    if fixture_digest != AUTHORIZATION_FIXTURE_SHA256:
        raise RuntimeError(
            "Authorization fixture digest mismatch: "
            f"expected={AUTHORIZATION_FIXTURE_SHA256}, actual={fixture_digest}"
        )
    fixture = json.loads(fixture_bytes)
    if fixture != {
        "catalog_version": 1,
        "default_policy": {
            "decision": "would_allow",
            "reason": "policy_empty_default_allow",
        },
        "default_principal_candidate": LOCAL_SINGLE_PRINCIPAL,
        "rule_status": (
            "current_loopback_retire_boundary_other_rules_prospective_non_binding"
        ),
        "tools": catalog_as_plain_data(),
    }:
        raise RuntimeError("Authorization source and canonical fixture diverged")
