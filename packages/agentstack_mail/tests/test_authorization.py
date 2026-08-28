"""Exact gates for the published authorization catalog and shadow observer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from agentstack_mail import app, authorization, config, db
from agentstack_mail.contract import COMPATIBILITY_TOOLS
from fastmcp import Client

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FIELDS = {
    "subject",
    "action",
    "resource",
    "required_arguments",
    "current_credential_arguments",
    "authorization_rule",
}
EXPECTED_CURRENT_CREDENTIAL_ARGUMENTS = {
    "register_agent": ("registration_token",),
    "retire_agent": ("registration_token",),
    # Recovery carries the same credential as the retirement it reverses.
    "unretire_agent": ("registration_token",),
    "send_message": ("sender_token",),
}
AUTHORIZATION_FIXTURE_PATH = (
    PACKAGE_ROOT / "fixtures" / authorization.AUTHORIZATION_FIXTURE
)


def _live_required_arguments() -> dict[str, tuple[str, ...]]:
    fixture = json.loads(
        (PACKAGE_ROOT / "fixtures" / "live-tools-list.json").read_text(encoding="utf-8")
    )
    return {
        tool["name"]: tuple(tool["inputSchema"].get("required", ()))
        for tool in fixture["tools"]
        if tool["name"] in COMPATIBILITY_TOOLS
    }


def _live_input_properties() -> dict[str, set[str]]:
    fixture = json.loads(
        (PACKAGE_ROOT / "fixtures" / "live-tools-list.json").read_text(encoding="utf-8")
    )
    return {
        tool["name"]: set(tool["inputSchema"].get("properties", {}))
        for tool in fixture["tools"]
        if tool["name"] in COMPATIBILITY_TOOLS
    }


def _configure_isolated_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tools_log_enabled: bool,
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
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_TOOLS_LOG_ENABLED",
        str(tools_log_enabled).lower(),
    )
    db.reset_database_state()
    config.clear_settings_cache()


def test_authorization_catalog_exactly_matches_the_published_contract() -> None:
    catalog = authorization.AUTHORIZATION_CATALOG
    fixture_bytes = AUTHORIZATION_FIXTURE_PATH.read_bytes()
    fixture = json.loads(fixture_bytes)

    assert authorization.authorization_catalog_is_complete()
    assert authorization.catalog_record_shape() == EXPECTED_FIELDS
    assert set(catalog) == COMPATIBILITY_TOOLS
    assert len(catalog) == 25  # 24 at cutover + unretire_agent (2026-08-28)
    # The rule text is the published claim about who may call this. It drifted
    # once already: the code was changed to trust the loopback caller while the
    # catalog still told operators a token was required.
    assert catalog["unretire_agent"]["authorization_rule"] == (
        "bound loopback local principal may restore any project target "
        "without its registration_token; retain the credential field for "
        "future project-administrator hardening, which should land on "
        "retire_agent and unretire_agent together"
    )
    assert set(asyncio.run(app.build_mcp_server().get_tools())) == set(catalog)
    assert hashlib.sha256(fixture_bytes).hexdigest() == (
        authorization.AUTHORIZATION_FIXTURE_SHA256
    )
    assert fixture == {
        "catalog_version": 1,
        "default_policy": {
            "decision": "would_allow",
            "reason": "policy_empty_default_allow",
        },
        "default_principal_candidate": authorization.LOCAL_SINGLE_PRINCIPAL,
        "rule_status": (
            "current_loopback_retire_unretire_boundary_other_rules_prospective_non_binding"
        ),
        "tools": authorization.catalog_as_plain_data(),
    }

    expected_required = _live_required_arguments()
    input_properties = _live_input_properties()
    for tool_name, record in catalog.items():
        assert set(record) == EXPECTED_FIELDS
        assert record["required_arguments"] == expected_required[tool_name]
        credential_arguments = record["current_credential_arguments"]
        assert isinstance(credential_arguments, tuple)
        assert set(credential_arguments) <= input_properties[tool_name]
        for field in ("subject", "action", "resource", "authorization_rule"):
            assert isinstance(record[field], str)
            assert record[field].strip()
        template = f"{record['subject']} {record['resource']}"
        for placeholder in re.findall(r"{([^{}]+)}", template):
            alternatives = placeholder.split("|")
            assert any(
                alternative in input_properties[tool_name] or alternative == "generated"
                for alternative in alternatives
            ), (tool_name, placeholder)


def test_server_construction_fails_if_catalog_loses_a_tool() -> None:
    whois = authorization.AUTHORIZATION_CATALOG.pop("whois")
    try:
        with pytest.raises(RuntimeError, match="catalog_missing=.*whois"):
            app.build_mcp_server()
    finally:
        authorization.AUTHORIZATION_CATALOG["whois"] = whois


def test_catalog_does_not_add_in_schema_credentials() -> None:
    actual = {
        tool_name: record["current_credential_arguments"]
        for tool_name, record in authorization.AUTHORIZATION_CATALOG.items()
        if record["current_credential_arguments"]
    }

    assert actual == EXPECTED_CURRENT_CREDENTIAL_ARGUMENTS
    assert authorization.AUTHORIZATION_CATALOG["retire_agent"][
        "authorization_rule"
    ] == (
        "bound loopback local principal may soft-retire any project target without "
        "its registration_token; retain the credential field for future "
        "project-administrator hardening"
    )


@pytest.mark.parametrize("policy", (None, {}))
def test_missing_or_empty_shadow_policy_denies_nothing(
    policy: authorization.ShadowAuthorizationPolicy | None,
) -> None:
    authorization.clear_shadow_authorization_observations()

    observations = [
        authorization.record_shadow_authorization(tool_name, policy=policy)
        for tool_name in sorted(COMPATIBILITY_TOOLS)
    ]

    assert len(observations) == 25  # one per published tool
    assert {item["decision"] for item in observations} == {"would_allow"}
    assert {item["reason"] for item in observations} == {"policy_empty_default_allow"}
    assert not [item for item in observations if item["decision"] == "would_deny"]


def test_nonempty_policy_with_no_tool_rule_defaults_to_allow() -> None:
    observation = authorization.record_shadow_authorization(
        "health_check",
        policy={
            "send_message": {
                "decision": "would_deny",
                "reason": "synthetic_other_tool_denial",
            }
        },
    )

    assert observation == {
        "principal_candidate": authorization.LOCAL_SINGLE_PRINCIPAL,
        "tool": "health_check",
        "decision": "would_allow",
        "reason": "policy_rule_missing_default_allow",
    }


def test_shadow_would_deny_fires_without_enforcement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_keys = {"principal_candidate", "tool", "decision", "reason"}
    _configure_isolated_runtime(monkeypatch, tmp_path, tools_log_enabled=False)

    async def call_health() -> Any:
        async with Client(app.build_mcp_server()) as client:
            result = await client.call_tool(
                "health_check",
                {},
                raise_on_error=False,
            )
        assert result.is_error is False
        return result.structured_content or result.data

    async def exercise_shadow_paths() -> None:
        authorization.clear_shadow_authorization_observations()
        monkeypatch.setattr(authorization, "SHADOW_AUTHORIZATION_POLICY", None)
        normal_result = await call_health()
        normal = authorization.shadow_authorization_observations()
        assert len(normal) == 1
        assert normal[0] == {
            "principal_candidate": authorization.LOCAL_SINGLE_PRINCIPAL,
            "tool": "health_check",
            "decision": "would_allow",
            "reason": "policy_empty_default_allow",
        }
        assert not [item for item in normal if item["decision"] == "would_deny"]

        synthetic_policy = {
            "health_check": {
                "decision": "would_deny",
                "reason": "synthetic_test_denial",
            }
        }
        authorization.clear_shadow_authorization_observations()
        monkeypatch.setattr(
            authorization,
            "SHADOW_AUTHORIZATION_POLICY",
            synthetic_policy,
        )
        denied_result = await call_health()
        denied = authorization.shadow_authorization_observations()
        assert len(denied) == 1
        assert set(denied[0]) == expected_keys
        assert denied[0] == {
            "principal_candidate": authorization.LOCAL_SINGLE_PRINCIPAL,
            "tool": "health_check",
            "decision": "would_deny",
            "reason": "synthetic_test_denial",
        }
        assert denied_result == normal_result

        credential_canary = "credential-must-not-become-a-reason"
        authorization.clear_shadow_authorization_observations()
        monkeypatch.setattr(
            authorization,
            "SHADOW_AUTHORIZATION_POLICY",
            {
                "health_check": {
                    "decision": "would_deny",
                    "reason": credential_canary,
                }
            },
        )
        invalid_policy_result = await call_health()
        assert invalid_policy_result == normal_result
        assert authorization.shadow_authorization_observations() == ()
        assert credential_canary not in repr(
            authorization.shadow_authorization_observations()
        )

    try:
        asyncio.run(exercise_shadow_paths())
    finally:
        db.reset_database_state()
        config.clear_settings_cache()


def test_shadow_and_rich_log_records_do_not_copy_credential_values() -> None:
    canary = "sensitive-canary-value"
    redacted = authorization.redact_tool_arguments(
        {
            "ctx": object(),
            "registration_token": canary,
            "sender_token": canary,
            "api_secret": canary,
            "owner_credential": canary,
            "body_md": "ordinary value",
        }
    )
    observation = authorization.record_shadow_authorization(
        "register_agent",
        policy={},
    )

    assert redacted == {
        "registration_token": "<redacted>",
        "sender_token": "<redacted>",
        "api_secret": "<redacted>",
        "owner_credential": "<redacted>",
        "body_md": "ordinary value",
    }
    assert canary not in repr(redacted)
    assert set(observation) == {
        "principal_candidate",
        "tool",
        "decision",
        "reason",
    }
    assert canary not in repr(observation)


def test_instrumentation_redacts_credential_before_rich_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "instrumentation-sensitive-canary"
    started: list[Any] = []
    _configure_isolated_runtime(monkeypatch, tmp_path, tools_log_enabled=True)
    monkeypatch.setattr(app.rich_logger, "log_tool_call_start", started.append)
    monkeypatch.setattr(app.rich_logger, "log_tool_call_end", lambda _context: None)

    async def call_register() -> None:
        async with Client(app.build_mcp_server()) as client:
            project = await client.call_tool(
                "ensure_project",
                {"human_key": str(tmp_path / "project")},
                raise_on_error=False,
            )
            assert project.is_error is False
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": str(tmp_path / "project"),
                    "program": "test",
                    "model": "test",
                    "name": "RedStone",
                    "registration_token": canary,
                },
                raise_on_error=False,
            )
            assert result.is_error is False

    try:
        authorization.clear_shadow_authorization_observations()
        asyncio.run(call_register())
        register_log = next(
            context for context in started if context.tool_name == "register_agent"
        )
        assert register_log.kwargs["registration_token"] == "<redacted>"
        assert canary not in repr(register_log.kwargs)
        register_shadow = next(
            item
            for item in authorization.shadow_authorization_observations()
            if item["tool"] == "register_agent"
        )
        assert canary not in repr(register_shadow)
    finally:
        db.reset_database_state()
        config.clear_settings_cache()
