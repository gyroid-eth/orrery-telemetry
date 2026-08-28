"""Namespace-independent lifecycle and macro differential scenario.

The worker supplies async ``call`` and ``checkpoint`` callbacks.  This module
deliberately imports neither server namespace, and caller-owned registration
tokens are used only as tool arguments; they are never checkpointed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

ToolResponse = Any
ToolCall = Callable[[str, Mapping[str, Any]], Awaitable[ToolResponse]]
Checkpoint = Callable[[str, ToolResponse], Awaitable[None]]

SCENARIO_TOOLS: frozenset[str] = frozenset(
    {
        "ensure_project",
        "fetch_summary",
        "health_check",
        "macro_contact_handshake",
        "macro_file_reservation_cycle",
        "macro_start_session",
        "register_agent",
        "retire_agent",
        "unretire_agent",
    }
)

_STARTER = "GreenCastle"
_PEER = "BlueLake"
_AGENT_NAMES = (_STARTER, _PEER)
_SESSION_PATH = "session/lifecycle/**"
_CYCLE_PATH = "src/lifecycle.py"


def _scenario_tokens(secrets: Mapping[str, str]) -> dict[str, str]:
    """Validate the caller-owned tokens without exposing values in errors."""

    try:
        tokens = {agent_name: secrets[agent_name] for agent_name in _AGENT_NAMES}
    except KeyError as exc:
        raise ValueError(
            "secrets must contain fake tokens for every lifecycle agent"
        ) from exc
    if any(not isinstance(token, str) or not token for token in tokens.values()):
        raise ValueError("lifecycle tokens must be non-empty strings")
    if len(set(tokens.values())) != len(tokens):
        raise ValueError("lifecycle tokens must be distinct")
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


def _assert_response_does_not_leak_tokens(
    response: Any,
    tokens: Sequence[str],
) -> None:
    if _contains_token(response, tokens):
        raise AssertionError("tool response leaked a lifecycle registration token")


async def run(
    call: ToolCall,
    checkpoint: Checkpoint,
    project_key: str,
    secrets: Mapping[str, str],
) -> None:
    """Run the ordered lifecycle chain against one isolated server."""

    tokens = _scenario_tokens(secrets)
    token_values = tuple(tokens.values())

    async def event(
        event_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResponse:
        if tool_name not in SCENARIO_TOOLS:
            raise AssertionError(f"scenario attempted undeclared tool {tool_name!r}")
        response = await call(tool_name, arguments)
        _assert_response_does_not_leak_tokens(response, token_values)
        await checkpoint(event_name, response)
        return response

    await event(
        "01_health_checked",
        "health_check",
        {"format": "json"},
    )
    await event(
        "02_project_ensured",
        "ensure_project",
        {"human_key": project_key, "format": "json"},
    )

    for index, agent_name in enumerate(_AGENT_NAMES, start=3):
        await event(
            f"{index:02d}_agent_registered_{agent_name}",
            "register_agent",
            {
                "project_key": project_key,
                "program": "differential-harness",
                "model": "fixture-model",
                "name": agent_name,
                "task_description": "lifecycle and macro differential",
                "registration_token": tokens[agent_name],
                "format": "json",
            },
        )

    # Reusing the caller-owned identity makes the macro exercise session setup
    # without replacing its registration token.  It also covers the macro's
    # optional reservation and inbox branches.
    await event(
        "05_session_started_with_reservation",
        "macro_start_session",
        {
            "human_key": project_key,
            "program": "differential-harness",
            "model": "fixture-model",
            "task_description": "active lifecycle differential session",
            "agent_name": _STARTER,
            "file_reservation_paths": [_SESSION_PATH],
            "file_reservation_reason": "lifecycle session setup",
            "file_reservation_ttl_seconds": 900,
            "inbox_limit": 5,
            "format": "json",
        },
    )
    await event(
        "06_reservation_cycle_auto_released",
        "macro_file_reservation_cycle",
        {
            "project_key": project_key,
            "agent_name": _STARTER,
            "paths": [_CYCLE_PATH],
            "ttl_seconds": 900,
            "exclusive": True,
            "reason": "lifecycle macro reservation cycle",
            "auto_release": True,
            "format": "json",
        },
    )
    await event(
        "07_contact_handshake_auto_accepted",
        "macro_contact_handshake",
        {
            "project_key": project_key,
            "requester": _STARTER,
            "target": _PEER,
            "reason": "lifecycle macro contact handshake",
            "ttl_seconds": 604800,
            "auto_accept": True,
            "register_if_missing": False,
            "format": "json",
        },
    )

    # A fresh isolated project has no stored summaries.  Retrieving that empty
    # collection is the deterministic, non-LLM success path for fetch_summary.
    await event(
        "08_empty_summary_collection_fetched",
        "fetch_summary",
        {
            "project_key": project_key,
            "since_hours": 24.0,
            "limit": 5,
            "format": "json",
        },
    )
    await event(
        "09_peer_retired",
        "retire_agent",
        {
            "project_key": project_key,
            "agent_name": _PEER,
            "registration_token": tokens[_PEER],
        },
    )
    await event(
        "10_peer_restored",
        "unretire_agent",
        {
            "project_key": project_key,
            "agent_name": _PEER,
            "registration_token": tokens[_PEER],
        },
    )
