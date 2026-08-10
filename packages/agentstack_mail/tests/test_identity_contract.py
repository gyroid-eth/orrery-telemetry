import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agentstack_mail import app, config, db
from agentstack_mail.app import _agent_to_dict, _resolve_registration_token
from agentstack_mail.contract import COMPATIBILITY_TOOLS
from agentstack_mail.models import Agent
from fastmcp import Client


TOKEN_FIELDS = frozenset({"registration_token", "sender_token", "agent_token"})


def test_caller_registration_token_is_retained() -> None:
    caller_token = "caller-owned-registration-token"

    assert _resolve_registration_token(None, caller_token) == caller_token
    assert _resolve_registration_token(caller_token, None) == caller_token


def test_same_registration_token_replay_is_idempotent() -> None:
    caller_token = "caller-owned-registration-token"

    assert _resolve_registration_token(caller_token, caller_token) == caller_token


def test_conflicting_registration_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match the existing token"):
        _resolve_registration_token("original-owner-token", "different-owner-token")


def test_registration_response_projection_does_not_expose_token() -> None:
    caller_token = "caller-owned-registration-token"
    agent = Agent(
        id=7,
        project_id=3,
        name="BackendHarmonizer",
        program="codex-app",
        model="gpt-5",
        registration_token=caller_token,
    )

    response = _agent_to_dict(agent)

    assert "registration_token" not in response
    assert caller_token not in json.dumps(response, sort_keys=True)


def _configure_isolated_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str | None,
) -> None:
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'mail.sqlite3'}",
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR",
        str(tmp_path / "signals"),
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_LOG_RICH_ENABLED", "false")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    if mode is None:
        monkeypatch.delenv(
            "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE",
            raising=False,
        )
    else:
        monkeypatch.setenv("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE", mode)
    db.reset_database_state()
    config.clear_settings_cache()


def _payload(result: Any) -> Any:
    value = result.structured_content
    if value is None:
        value = result.data
    while isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value


async def _ensure_project(client: Client[Any], project: str) -> None:
    result = await client.call_tool(
        "ensure_project",
        {"human_key": project, "format": "json"},
        raise_on_error=False,
    )
    assert result.is_error is False


def test_passthrough_public_registration_preserves_real_runtime_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_isolated_runtime(monkeypatch, tmp_path, mode="passthrough")
    project = str(tmp_path / "project")
    requested_names = ("ProOpus", "AirSonnet", "BiomatterBot", "SeminarBot")

    async def register_names() -> list[str]:
        async with Client(app.build_mcp_server()) as client:
            await _ensure_project(client, project)
            returned_names: list[str] = []
            for requested_name in requested_names:
                result = await client.call_tool(
                    "register_agent",
                    {
                        "project_key": project,
                        "program": "identity-contract",
                        "model": "fixture-model",
                        "name": requested_name,
                        "task_description": "exact cutover identity",
                        "registration_token": f"token-{requested_name}",
                        "format": "json",
                    },
                    raise_on_error=False,
                )
                assert result.is_error is False
                returned = _payload(result)
                assert returned["name"] == requested_name
                returned_names.append(returned["name"])
        await db.get_engine().dispose()
        return returned_names

    try:
        assert asyncio.run(register_names()) == list(requested_names)
    finally:
        db.reset_database_state()
        config.clear_settings_cache()


def test_default_coerce_exposes_substituted_name_in_public_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_isolated_runtime(monkeypatch, tmp_path, mode=None)
    project = str(tmp_path / "project")

    async def attempt_registration() -> Any:
        async with Client(app.build_mcp_server()) as client:
            await _ensure_project(client, project)
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": project,
                    "program": "identity-contract",
                    "model": "fixture-model",
                    "name": "ProOpus",
                    "task_description": "freeze default substitution visibility",
                    "registration_token": "explicit-name-token",
                    "format": "json",
                },
                raise_on_error=False,
            )
        await db.get_engine().dispose()
        return result

    try:
        assert config.get_settings().agent_name_enforcement_mode == "coerce"
        result = asyncio.run(attempt_registration())
        assert result.is_error is False
        returned = _payload(result)
        assert isinstance(returned["name"], str)
        assert returned["name"] != "ProOpus"
    finally:
        db.reset_database_state()
        config.clear_settings_cache()


def test_passthrough_keeps_frozen_name_sanitization_behavior(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_isolated_runtime(monkeypatch, tmp_path, mode="passthrough")
    project = str(tmp_path / "project")

    async def attempt_registration() -> Any:
        async with Client(app.build_mcp_server()) as client:
            await _ensure_project(client, project)
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": project,
                    "program": "identity-contract",
                    "model": "fixture-model",
                    "name": "Pro-Opus",
                    "registration_token": "normalization-token",
                },
                raise_on_error=False,
            )
        await db.get_engine().dispose()
        return result

    try:
        result = asyncio.run(attempt_registration())
        assert result.is_error is False
        assert _payload(result)["name"] == "ProOpus"
    finally:
        db.reset_database_state()
        config.clear_settings_cache()


def test_compatibility_tool_schemas_advertise_only_live_token_parameters() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "live-tools-list.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    advertised: dict[str, set[str]] = {}
    for tool in fixture["tools"]:
        name = tool["name"]
        if name not in COMPATIBILITY_TOOLS:
            continue
        properties = tool["inputSchema"].get("properties", {})
        token_fields = TOKEN_FIELDS.intersection(properties)
        if token_fields:
            advertised[name] = set(token_fields)

    assert advertised == {
        "register_agent": {"registration_token"},
        "retire_agent": {"registration_token"},
        "send_message": {"sender_token"},
    }
