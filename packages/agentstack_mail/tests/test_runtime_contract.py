"""Runtime contract gates for the AgentStack-owned mail core.

These tests intentionally fail when a required core API has not been ported yet.
Do not weaken them with skips or expected failures: they are the extraction gate.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from agentstack_mail.contract import COMPATIBILITY_TOOLS, ISOLATION_DEFAULTS

LEGACY_ENV = {
    "PORT": "8765",
    "HTTP_PORT": "8765",
    "DATABASE_URL": "sqlite+aiosqlite:///./storage.sqlite3",
    "STORAGE_ROOT": "~/.mcp_agent_mail_git_mailbox_repo",
    "NOTIFICATIONS_SIGNALS_DIR": "~/.mcp_agent_mail/signals",
}

AGENTSTACK_ENV = {
    "AGENTSTACK_MAIL_HTTP_PORT": "28765",
    "AGENTSTACK_MAIL_DATABASE_URL": "sqlite+aiosqlite:////tmp/agentstack-mail.sqlite3",
    "AGENTSTACK_MAIL_STORAGE_ROOT": "/tmp/agentstack-mail-archive",
    "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": "/tmp/agentstack-mail-signals",
}


def _require_module(name: str) -> ModuleType:
    """Import a required runtime module, reporting a contract failure explicitly."""
    try:
        return importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - exercised only by incomplete ports
        pytest.fail(
            f"runtime contract requires importable module {name!r}: {exc}",
            pytrace=False,
        )


def _require_attr(module: ModuleType, name: str) -> Any:
    """Resolve a required runtime API, reporting a contract failure explicitly."""
    try:
        return getattr(module, name)
    except AttributeError:
        pytest.fail(
            f"runtime contract requires {module.__name__}.{name}",
            pytrace=False,
        )


def _sqlite_path(url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    assert url.startswith(prefix), f"expected an aiosqlite URL, got {url!r}"
    return Path(url.removeprefix(prefix)).expanduser().resolve()


def _load_settings() -> tuple[ModuleType, Any]:
    config = _require_module("agentstack_mail.config")
    clear_settings_cache = _require_attr(config, "clear_settings_cache")
    get_settings = _require_attr(config, "get_settings")
    clear_settings_cache()
    return config, get_settings()


def test_isolated_defaults_ignore_all_legacy_environment_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "HTTP_PORT=8765\nDATABASE_URL=sqlite+aiosqlite:///./storage.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    for name in AGENTSTACK_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in LEGACY_ENV.items():
        monkeypatch.setenv(name, value)

    config, settings = _load_settings()

    try:
        assert settings.http.port == ISOLATION_DEFAULTS.port
        assert _sqlite_path(settings.database.url) == Path(
            ISOLATION_DEFAULTS.database
        ).expanduser().resolve()
        assert Path(settings.storage.root).expanduser().resolve() == Path(
            ISOLATION_DEFAULTS.archive
        ).expanduser().resolve()
        assert Path(settings.notifications.signals_dir).expanduser().resolve() == Path(
            ISOLATION_DEFAULTS.signals
        ).expanduser().resolve()
    finally:
        _require_attr(config, "clear_settings_cache")()


def test_only_agentstack_prefixed_environment_names_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    for name, value in LEGACY_ENV.items():
        monkeypatch.setenv(name, value)
    for name, value in AGENTSTACK_ENV.items():
        monkeypatch.setenv(name, value)

    config, settings = _load_settings()

    try:
        assert settings.http.port == int(
            AGENTSTACK_ENV["AGENTSTACK_MAIL_HTTP_PORT"]
        )
        assert settings.database.url == AGENTSTACK_ENV["AGENTSTACK_MAIL_DATABASE_URL"]
        assert settings.storage.root == AGENTSTACK_ENV["AGENTSTACK_MAIL_STORAGE_ROOT"]
        assert (
            settings.notifications.signals_dir
            == AGENTSTACK_ENV["AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR"]
        )
    finally:
        _require_attr(config, "clear_settings_cache")()


def test_mcp_server_exposes_exactly_the_22_compatibility_tools_and_no_resources() -> None:
    app = _require_module("agentstack_mail.app")
    build_mcp_server = _require_attr(app, "build_mcp_server")

    async def inspect_server() -> tuple[set[str], Any]:
        mcp = build_mcp_server()
        tools = await mcp.get_tools()
        resources = await mcp.get_resources()
        return set(tools), resources

    tool_names, resources = asyncio.run(inspect_server())

    assert len(tool_names) == 22
    assert tool_names == COMPATIBILITY_TOOLS
    assert not resources


def test_actual_tool_schemas_match_the_frozen_live_contract() -> None:
    app = _require_module("agentstack_mail.app")
    build_mcp_server = _require_attr(app, "build_mcp_server")
    fixture_path = Path(__file__).parents[1] / "fixtures" / "live-tools-list.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected = {
        tool["name"]: {
            "inputSchema": tool["inputSchema"],
            "outputSchema": tool["outputSchema"],
            "_meta": tool["_meta"],
        }
        for tool in fixture["tools"]
        if tool["name"] in COMPATIBILITY_TOOLS
    }

    async def inspect_server() -> dict[str, dict[str, Any]]:
        tools = await build_mcp_server().get_tools()
        return {
            name: {
                "inputSchema": dumped["inputSchema"],
                "outputSchema": dumped["outputSchema"],
                "_meta": dumped["_meta"],
            }
            for name, tool in tools.items()
            if (
                dumped := tool.to_mcp_tool().model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                )
            )
        }

    actual = asyncio.run(inspect_server())

    assert actual == expected


def test_published_tool_descriptions_do_not_reference_suppressed_resources() -> None:
    app = _require_module("agentstack_mail.app")
    build_mcp_server = _require_attr(app, "build_mcp_server")

    async def descriptions() -> list[str]:
        tools = await build_mcp_server().get_tools()
        assert {
            "register_agent",
            "macro_start_session",
            "list_contacts",
            "whois",
            "send_message",
        } <= set(tools)
        return [tool.description or "" for tool in tools.values()]

    assert all("resource://agents" not in text for text in asyncio.run(descriptions()))


def test_boundary_checks_the_actual_registry_after_mutation() -> None:
    app = _require_module("agentstack_mail.app")
    build_mcp_server = _require_attr(app, "build_mcp_server")
    mcp = build_mcp_server()

    mcp.remove_tool("whois")

    assert mcp.published_tool_names == COMPATIBILITY_TOOLS
    with pytest.raises(RuntimeError, match=r"missing=\['whois'\]"):
        mcp.assert_contract_boundary()


def test_boundary_rejects_a_tool_added_through_the_fastmcp_base_class() -> None:
    from fastmcp import FastMCP

    app = _require_module("agentstack_mail.app")
    build_mcp_server = _require_attr(app, "build_mcp_server")
    mcp = build_mcp_server()

    def rogue() -> None:
        return None

    FastMCP.tool(mcp, rogue, name="rogue")

    with pytest.raises(RuntimeError, match=r"extra=\['rogue'\]"):
        mcp.assert_contract_boundary()


def test_subset_tool_filter_fails_server_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_FILTER_ENABLED", "true")
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_FILTER_PROFILE", "minimal")
    config = _require_module("agentstack_mail.config")
    clear_settings_cache = _require_attr(config, "clear_settings_cache")
    app = _require_module("agentstack_mail.app")
    build_mcp_server = _require_attr(app, "build_mcp_server")
    clear_settings_cache()

    try:
        with pytest.raises(RuntimeError, match="tool boundary mismatch"):
            build_mcp_server()
    finally:
        clear_settings_cache()


def test_signals_are_per_message_debounced_by_message_id_and_fully_cleared(
    tmp_path: Path,
) -> None:
    storage = _require_module("agentstack_mail.storage")
    emit_notification_signal = _require_attr(storage, "emit_notification_signal")
    clear_notification_signal = _require_attr(storage, "clear_notification_signal")
    signal_debounce = _require_attr(storage, "_SIGNAL_DEBOUNCE")
    signal_debounce.clear()

    settings = SimpleNamespace(
        notifications=SimpleNamespace(
            enabled=True,
            signals_dir=str(tmp_path),
            include_metadata=True,
            debounce_ms=60_000,
        )
    )
    project_slug = "runtime-contract"
    agent_name = "BlueLake"
    agents_dir = tmp_path / "projects" / project_slug / "agents"
    per_message_dir = agents_dir / agent_name
    legacy_path = agents_dir / f"{agent_name}.signal"

    async def emit_signals() -> tuple[bool, bool, bool, bool]:
        first = await emit_notification_signal(
            settings,
            project_slug,
            agent_name,
            {"id": 101, "from": "GreenCastle", "subject": "first"},
        )
        second = await emit_notification_signal(
            settings,
            project_slug,
            agent_name,
            {"id": 102, "from": "GreenCastle", "subject": "second"},
        )
        duplicate = await emit_notification_signal(
            settings,
            project_slug,
            agent_name,
            {"id": 101, "from": "GreenCastle", "subject": "duplicate"},
        )
        legacy = await emit_notification_signal(
            settings,
            project_slug,
            agent_name,
        )
        return first, second, duplicate, legacy

    try:
        first, second, duplicate, legacy = asyncio.run(emit_signals())

        assert (first, second, duplicate, legacy) == (True, True, False, True)
        assert (per_message_dir / "101.signal").is_file()
        assert (per_message_dir / "102.signal").is_file()
        assert legacy_path.is_file()

        cleared = asyncio.run(
            clear_notification_signal(settings, project_slug, agent_name)
        )

        assert cleared is True
        assert not (per_message_dir / "101.signal").exists()
        assert not (per_message_dir / "102.signal").exists()
        assert not per_message_dir.exists()
        assert not legacy_path.exists()
    finally:
        signal_debounce.clear()
