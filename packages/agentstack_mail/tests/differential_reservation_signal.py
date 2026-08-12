"""Namespace-independent reservation, archive, and signal scenario.

The differential worker supplies two async callbacks::

    result = await call(tool_name, arguments)
    await checkpoint(event_name, result)

``secrets`` maps the three deterministic agent names to fake caller-owned
registration tokens.  This module does not import either server package and
never returns, logs, or checkpoints the secret mapping itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

Call = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
Checkpoint = Callable[[str, Any], Awaitable[None]]

SCENARIO_TOOLS: frozenset[str] = frozenset(
    {
        "ensure_project",
        "fetch_inbox",
        "file_reservation_paths",
        "register_agent",
        "release_file_reservations",
        "renew_file_reservations",
        "send_message",
        "set_contact_policy",
    }
)

_AGENT_NAMES = ("GreenCastle", "BlueLake", "RedStone")
_NFD_RESERVATION_PATH = "src/cafe\u0301/**"
_NFC_RESERVATION_PATH = "src/caf\u00e9/**"
_OVERLAPPING_PATH = "src/caf\u00e9/app.py"


def _scenario_tokens(secrets: Mapping[str, str]) -> dict[str, str]:
    """Validate and copy fake tokens without including their values in errors."""
    try:
        tokens = {agent_name: secrets[agent_name] for agent_name in _AGENT_NAMES}
    except KeyError as exc:
        raise ValueError(
            "secrets must contain fake tokens for every scenario agent"
        ) from exc
    if any(not isinstance(token, str) or not token for token in tokens.values()):
        raise ValueError("scenario tokens must be non-empty strings")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("scenario tokens must be distinct")
    return tokens


def _contains_token(value: Any, tokens: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(token in value for token in tokens)
    if isinstance(value, Mapping):
        return any(
            _contains_token(key, tokens) or _contains_token(item, tokens)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return any(_contains_token(item, tokens) for item in value)
    return False


def _assert_response_does_not_leak_tokens(result: Any, tokens: Sequence[str]) -> None:
    if _contains_token(result, tokens):
        raise AssertionError("tool response leaked a scenario registration token")


async def run(
    call: Call,
    checkpoint: Checkpoint,
    project_key: str,
    secrets: Mapping[str, str],
) -> None:
    """Run the ordered public-tool scenario against one isolated server."""
    tokens = _scenario_tokens(secrets)

    async def event(
        event_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        if tool_name not in SCENARIO_TOOLS:
            raise AssertionError(f"scenario attempted undeclared tool {tool_name!r}")
        result = await call(tool_name, arguments)
        _assert_response_does_not_leak_tokens(result, tuple(tokens.values()))
        await checkpoint(event_name, result)
        return result

    await event(
        "01_project_ensured",
        "ensure_project",
        {"human_key": project_key, "format": "json"},
    )

    for index, agent_name in enumerate(_AGENT_NAMES, start=2):
        await event(
            f"{index:02d}_agent_registered_{agent_name}",
            "register_agent",
            {
                "project_key": project_key,
                "program": "differential-harness",
                "model": "fixture-model",
                "name": agent_name,
                "task_description": "reservation/archive/signal differential",
                "registration_token": tokens[agent_name],
                "format": "json",
            },
        )

    # Keep contact behavior out of this storage-focused scenario.  Both recipients
    # must accept the later to/bcc deliveries regardless of server environment.
    await event(
        "05_contact_policy_open_BlueLake",
        "set_contact_policy",
        {
            "project_key": project_key,
            "agent_name": "BlueLake",
            "policy": "open",
            "format": "json",
        },
    )
    await event(
        "06_contact_policy_open_RedStone",
        "set_contact_policy",
        {
            "project_key": project_key,
            "agent_name": "RedStone",
            "policy": "open",
            "format": "json",
        },
    )

    await event(
        "07_reservation_created_nfd",
        "file_reservation_paths",
        {
            "project_key": project_key,
            "agent_name": "GreenCastle",
            "paths": [_NFD_RESERVATION_PATH],
            "ttl_seconds": 300,
            "exclusive": True,
            "reason": "differential reservation lifecycle",
            "format": "json",
        },
    )
    await event(
        "08_reservation_reacquired_nfc_same_agent",
        "file_reservation_paths",
        {
            "project_key": project_key,
            "agent_name": "GreenCastle",
            "paths": [_NFC_RESERVATION_PATH],
            "ttl_seconds": 900,
            "exclusive": True,
            "reason": "differential reservation lifecycle",
            "format": "json",
        },
    )
    await event(
        "09_reservation_conflict_other_agent",
        "file_reservation_paths",
        {
            "project_key": project_key,
            "agent_name": "BlueLake",
            "paths": [_OVERLAPPING_PATH],
            "ttl_seconds": 300,
            "exclusive": True,
            "reason": "differential conflict probe",
            "format": "json",
        },
    )
    await event(
        "10_reservation_renewed_by_overlap",
        "renew_file_reservations",
        {
            "project_key": project_key,
            "agent_name": "GreenCastle",
            "extend_seconds": 600,
            "paths": [_OVERLAPPING_PATH],
            "format": "json",
        },
    )
    await event(
        "11_reservation_released_by_overlap",
        "release_file_reservations",
        {
            "project_key": project_key,
            "agent_name": "GreenCastle",
            "paths": [_OVERLAPPING_PATH],
            "format": "json",
        },
    )
    await event(
        "12_reservation_acquired_after_release",
        "file_reservation_paths",
        {
            "project_key": project_key,
            "agent_name": "BlueLake",
            "paths": [_OVERLAPPING_PATH],
            "ttl_seconds": 300,
            "exclusive": True,
            "reason": "differential post-release acquisition",
            "format": "json",
        },
    )

    common_message_arguments: dict[str, Any] = {
        "project_key": project_key,
        "sender_name": "GreenCastle",
        "to": ["BlueLake"],
        "bcc": ["RedStone"],
        "importance": "high",
        "ack_required": True,
        "sender_token": tokens["GreenCastle"],
        "format": "json",
    }
    await event(
        "13_message_sent_first",
        "send_message",
        {
            **common_message_arguments,
            "subject": "Differential message one",
            "body_md": "First reservation/archive/signal differential message.",
        },
    )
    await event(
        "14_message_sent_second",
        "send_message",
        {
            **common_message_arguments,
            "subject": "Differential message two",
            "body_md": "Second reservation/archive/signal differential message.",
        },
    )
    await event(
        "15_blue_inbox_fetched",
        "fetch_inbox",
        {
            "project_key": project_key,
            "agent_name": "BlueLake",
            "limit": 20,
            "include_bodies": True,
            "format": "json",
        },
    )
