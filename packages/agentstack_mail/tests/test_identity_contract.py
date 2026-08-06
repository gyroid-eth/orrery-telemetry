import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentstack_mail.app import (
    _agent_to_dict,
    _generate_unique_agent_name,
    _resolve_registration_token,
)
from agentstack_mail.contract import COMPATIBILITY_TOOLS
from agentstack_mail.models import Agent, Project


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


def test_passthrough_mode_accepts_a_descriptive_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentstack_mail import app

    async def fake_ensure_archive(settings: object, slug: str) -> SimpleNamespace:
        return SimpleNamespace(root=tmp_path / slug)

    async def fake_agent_name_exists(project: Project, candidate: str) -> bool:
        return False

    monkeypatch.setattr(app, "ensure_archive", fake_ensure_archive)
    monkeypatch.setattr(app, "_agent_name_exists", fake_agent_name_exists)
    project = Project(id=1, slug="identity-contract", human_key="/tmp/identity-contract")
    settings = SimpleNamespace(agent_name_enforcement_mode="passthrough")

    selected = asyncio.run(
        _generate_unique_agent_name(
            project,
            settings,
            name_hint="Backend Harmonizer",
        )
    )

    assert selected == "BackendHarmonizer"


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
