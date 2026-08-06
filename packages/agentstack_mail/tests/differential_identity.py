"""Ordered identity, contact, messaging, and receipt differential scenario.

This module deliberately depends only on the worker callbacks.  In particular,
it must remain importable without importing either server namespace.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

ToolResponse = Any
ToolCall = Callable[[str, Mapping[str, Any]], Awaitable[ToolResponse]]
Checkpoint = Callable[[str, ToolResponse], Awaitable[None]]

SCENARIO_TOOLS: frozenset[str] = frozenset(
    {
        "acknowledge_message",
        "ensure_project",
        "fetch_inbox",
        "fetch_topic",
        "list_contacts",
        "mark_message_read",
        "register_agent",
        "reply_message",
        "request_contact",
        "respond_contact",
        "send_message",
        "set_contact_policy",
        "whois",
    }
)

_GREEN = "GreenCastle"
_BLUE = "BlueLake"
_TOPIC = "identity-differential"


def _caller_token(secrets: Mapping[str, str], agent_name: str) -> str:
    token = secrets.get(agent_name)
    if not isinstance(token, str) or not token:
        raise ValueError(f"missing non-empty caller token for {agent_name}")
    return token


def _contains_secret(value: Any, caller_tokens: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(token in value for token in caller_tokens)
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, caller_tokens)
            or _contains_secret(item, caller_tokens)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret(item, caller_tokens) for item in value)
    return False


def _assert_no_caller_token(value: Any, caller_tokens: Sequence[str]) -> None:
    if _contains_secret(value, caller_tokens):
        raise AssertionError("tool response disclosed a caller token")


def _message_id(response: Any) -> int:
    """Extract the created message id without rewriting the response."""

    if not isinstance(response, Mapping):
        raise TypeError("send_message response must be an object")

    deliveries = response.get("deliveries")
    if isinstance(deliveries, Sequence) and deliveries:
        first = deliveries[0]
        if isinstance(first, Mapping):
            payload = first.get("payload")
            if isinstance(payload, Mapping):
                message_id = payload.get("id")
                if isinstance(message_id, int) and not isinstance(message_id, bool):
                    return message_id

    for wrapper in ("result", "structuredContent"):
        nested = response.get(wrapper)
        if isinstance(nested, Mapping):
            try:
                return _message_id(nested)
            except AssertionError:
                pass

    message_id = response.get("id")
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        return message_id
    raise AssertionError("send_message response did not contain a message id")


async def run(
    call: ToolCall,
    checkpoint: Checkpoint,
    project_key: str,
    secrets: Mapping[str, str],
) -> None:
    """Run the fresh-database differential chain in contract order."""

    green_token = _caller_token(secrets, _GREEN)
    blue_token = _caller_token(secrets, _BLUE)
    caller_tokens = (green_token, blue_token)

    async def record(
        label: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResponse:
        response = await call(tool_name, arguments)
        _assert_no_caller_token(response, caller_tokens)
        await checkpoint(label, response)
        return response

    await record(
        "01_ensure_project",
        "ensure_project",
        {"human_key": project_key, "format": "json"},
    )

    green_registration = {
        "project_key": project_key,
        "program": "differential-harness",
        "model": "fixture-model",
        "name": _GREEN,
        "task_description": "Differential sender",
        "registration_token": green_token,
        "format": "json",
    }
    await record(
        "02_register_green",
        "register_agent",
        green_registration,
    )
    await record(
        "03_register_blue",
        "register_agent",
        {
            "project_key": project_key,
            "program": "differential-harness",
            "model": "fixture-model",
            "name": _BLUE,
            "task_description": "Differential recipient",
            "registration_token": blue_token,
            "format": "json",
        },
    )
    await record(
        "04_reregister_green_same_token",
        "register_agent",
        green_registration,
    )
    await record(
        "05_whois_green",
        "whois",
        {
            "project_key": project_key,
            "agent_name": _GREEN,
            "include_recent_commits": False,
            "format": "json",
        },
    )

    await record(
        "06_blue_contacts_only",
        "set_contact_policy",
        {
            "project_key": project_key,
            "agent_name": _BLUE,
            "policy": "contacts_only",
            "format": "json",
        },
    )
    await record(
        "07_request_contact",
        "request_contact",
        {
            "project_key": project_key,
            "from_agent": _GREEN,
            "to_agent": _BLUE,
            "reason": "Differential contact approval",
            "register_if_missing": False,
            "ttl_seconds": 604800,
            "format": "json",
        },
    )
    await record(
        "08_list_contacts_pending",
        "list_contacts",
        {
            "project_key": project_key,
            "agent_name": _GREEN,
            "format": "json",
        },
    )
    await record(
        "09_respond_contact_accept",
        "respond_contact",
        {
            "project_key": project_key,
            "to_agent": _BLUE,
            "from_agent": _GREEN,
            "accept": True,
            "ttl_seconds": 604800,
            "format": "json",
        },
    )
    await record(
        "10_list_contacts_approved",
        "list_contacts",
        {
            "project_key": project_key,
            "agent_name": _GREEN,
            "format": "json",
        },
    )

    sent = await record(
        "11_send_message",
        "send_message",
        {
            "project_key": project_key,
            "sender_name": _GREEN,
            "sender_token": green_token,
            "to": [_BLUE],
            "subject": "Differential identity message",
            "body_md": "Please verify the identity differential chain.",
            "importance": "high",
            "ack_required": True,
            "topic": _TOPIC,
            "format": "json",
        },
    )
    message_id = _message_id(sent)

    await record(
        "12_fetch_blue_inbox",
        "fetch_inbox",
        {
            "project_key": project_key,
            "agent_name": _BLUE,
            "limit": 20,
            "include_bodies": True,
            "topic": _TOPIC,
            "format": "json",
        },
    )
    await record(
        "13_fetch_topic",
        "fetch_topic",
        {
            "project_key": project_key,
            "topic_name": _TOPIC,
            "limit": 50,
            "include_bodies": True,
            "format": "json",
        },
    )
    await record(
        "14_mark_message_read",
        "mark_message_read",
        {
            "project_key": project_key,
            "agent_name": _BLUE,
            "message_id": message_id,
            "format": "json",
        },
    )
    acknowledgement = {
        "project_key": project_key,
        "agent_name": _BLUE,
        "message_id": message_id,
        "format": "json",
    }
    await record(
        "15_acknowledge_message",
        "acknowledge_message",
        acknowledgement,
    )
    await record(
        "16_acknowledge_message_replay",
        "acknowledge_message",
        acknowledgement,
    )
    await record(
        "17_reply_message",
        "reply_message",
        {
            "project_key": project_key,
            "message_id": message_id,
            "sender_name": _BLUE,
            "body_md": "Identity differential received and acknowledged.",
            "format": "json",
        },
    )
    await record(
        "18_fetch_green_inbox",
        "fetch_inbox",
        {
            "project_key": project_key,
            "agent_name": _GREEN,
            "limit": 20,
            "include_bodies": True,
            "topic": _TOPIC,
            "format": "json",
        },
    )
