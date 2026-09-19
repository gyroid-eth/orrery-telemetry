"""Tool arguments never reach the server log or the client as raw values (#49).

FastMCP validates tool arguments with pydantic and, on failure, logs the whole
``ValidationError`` (``input_value='...'`` included) through
``fastmcp.tools.tool_manager`` before any code the server owns runs. A caller
that sends a credential under a name the tool does not accept therefore wrote
the credential into the server log. These tests pin the two layers that stop
it, and replay the registration helper's own call order (token first, then
without) against ``set_contact_policy``, whose published schema stays equal to
the frozen upstream contract.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from agentstack_mail import app, boundary, config, db
from fastmcp import Client

CANARY = "canary-secret-value-7f3a9c"
# Literal on purpose: the canary tests must also run (and fail) on a tree without the fix.
TOOL_MANAGER_LOGGER = "fastmcp.tools.tool_manager"


def _configure_isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    db.reset_database_state()
    config.clear_settings_cache()


def _payload(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        parts.append(str(getattr(block, "text", block)))
    if getattr(result, "structured_content", None) is not None:
        parts.append(repr(result.structured_content))
    return "\n".join(parts)


@pytest.fixture
def capture_tool_manager_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    # FastMCP's own logger does not propagate to the root logger, so attach the
    # capture handler where the validation record is created.
    logger = logging.getLogger(TOOL_MANAGER_LOGGER)
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger=TOOL_MANAGER_LOGGER)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    chunks = [caplog.text]
    for record in caplog.records:
        chunks.append(record.getMessage())
        if record.exc_text:
            chunks.append(record.exc_text)
        if record.exc_info and len(record.exc_info) > 1 and record.exc_info[1] is not None:
            chunks.append(repr(record.exc_info[1]))
    return "\n".join(chunks)


async def _register(client: Client, project: str, name: str, token: str | None) -> None:
    result = await client.call_tool("ensure_project", {"human_key": project}, raise_on_error=False)
    assert result.is_error is False, _payload(result)
    arguments: dict[str, Any] = {
        "project_key": project,
        "program": "test",
        "model": "test",
        "name": name,
    }
    if token is not None:
        arguments["registration_token"] = token
    result = await client.call_tool("register_agent", arguments, raise_on_error=False)
    assert result.is_error is False, _payload(result)


def _policy(tmp_path: Path, name: str) -> str:
    # ``whois`` does not project the contact policy, so read the durable row.
    connection = sqlite3.connect(tmp_path / "mail.sqlite3")
    try:
        row = connection.execute(
            "SELECT contact_policy FROM agents WHERE name = ?", (name,)
        ).fetchone()
    finally:
        connection.close()
    return "<absent>" if row is None else str(row[0])


def test_unknown_argument_is_rejected_by_name_without_echoing_its_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_tool_manager_log: pytest.LogCaptureFixture,
) -> None:
    _configure_isolated_runtime(monkeypatch, tmp_path)

    async def run() -> Any:
        async with Client(app.build_mcp_server()) as client:
            return await client.call_tool(
                "health_check", {"nonce": CANARY}, raise_on_error=False
            )

    try:
        result = asyncio.run(run())
    finally:
        db.reset_database_state()

    text = _payload(result)
    assert result.is_error is True
    assert "health_check" in text and "does not accept 1" in text
    assert "nonce" not in text
    assert CANARY not in text
    assert CANARY not in _log_text(capture_tool_manager_log)


def test_unknown_argument_named_like_a_secret_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_tool_manager_log: pytest.LogCaptureFixture,
) -> None:
    _configure_isolated_runtime(monkeypatch, tmp_path)

    async def run() -> Any:
        async with Client(app.build_mcp_server()) as client:
            return await client.call_tool("health_check", {CANARY: 0}, raise_on_error=False)

    try:
        result = asyncio.run(run())
    finally:
        db.reset_database_state()

    assert result.is_error is True
    assert CANARY not in _payload(result)
    assert CANARY not in _log_text(capture_tool_manager_log)


def test_summary_never_repeats_caller_supplied_keys_or_tool_names() -> None:
    from pydantic import TypeAdapter, ValidationError

    with pytest.raises(ValidationError) as info:
        TypeAdapter(dict[str, int]).validate_python({CANARY: "bad"})
    exc = info.value
    unverified = boundary.validation_error_summary(None, exc)
    verified = boundary.validation_error_summary("fetch_inbox", exc, {"limit"})
    assert CANARY not in unverified and "<tool>" in unverified
    assert CANARY not in verified and "<argument>" in verified
    sanitizer = boundary.ToolValidationLogSanitizer()
    record = logging.LogRecord(
        TOOL_MANAGER_LOGGER, logging.ERROR, __file__, 1,
        f"Error validating tool '{CANARY}': {exc}", (), (type(exc), exc, exc.__traceback__),
    )
    assert sanitizer.filter(record) is True
    assert CANARY not in record.getMessage()


def test_type_error_reports_the_field_path_but_not_the_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_tool_manager_log: pytest.LogCaptureFixture,
) -> None:
    _configure_isolated_runtime(monkeypatch, tmp_path)
    project = str(tmp_path / "project")

    async def run() -> Any:
        async with Client(app.build_mcp_server()) as client:
            await _register(client, project, "RedStone", None)
            return await client.call_tool(
                "fetch_inbox",
                {"project_key": project, "agent_name": "RedStone", "limit": CANARY},
                raise_on_error=False,
            )

    try:
        result = asyncio.run(run())
    finally:
        db.reset_database_state()

    text = _payload(result)
    assert result.is_error is True
    assert "limit" in text
    assert CANARY not in text
    assert CANARY not in _log_text(capture_tool_manager_log)


def test_nested_argument_values_stay_out_of_the_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_tool_manager_log: pytest.LogCaptureFixture,
) -> None:
    _configure_isolated_runtime(monkeypatch, tmp_path)
    project = str(tmp_path / "project")

    async def run() -> Any:
        async with Client(app.build_mcp_server()) as client:
            await _register(client, project, "RedStone", None)
            return await client.call_tool(
                "send_message",
                {
                    "project_key": project,
                    "sender_name": "RedStone",
                    "to": {"nested": CANARY},
                    "subject": "x",
                    "body_md": "y",
                },
                raise_on_error=False,
            )

    try:
        result = asyncio.run(run())
    finally:
        db.reset_database_state()

    assert result.is_error is True
    assert CANARY not in _payload(result)
    assert CANARY not in _log_text(capture_tool_manager_log)


def test_sanitizer_rewrites_a_validation_record_and_leaves_other_records_alone() -> None:
    from pydantic import TypeAdapter, ValidationError

    sanitizer = boundary.ToolValidationLogSanitizer()
    try:
        TypeAdapter(dict[str, int]).validate_python({"limit": CANARY})
    except ValidationError as exc:
        record = logging.LogRecord(
            TOOL_MANAGER_LOGGER,
            logging.ERROR,
            __file__,
            1,
            f"Error validating tool 'fetch_inbox': {exc}",
            (),
            (type(exc), exc, exc.__traceback__),
        )
    assert sanitizer.filter(record) is True
    assert record.exc_info is None and record.exc_text is None
    assert CANARY not in record.getMessage()
    # The logger cannot verify the tool key or field names, so it reports neither.
    assert "<tool>" in record.getMessage() and "limit" not in record.getMessage()
    assert "1 validation error" in record.getMessage()

    plain = logging.LogRecord(
        TOOL_MANAGER_LOGGER, logging.ERROR, __file__, 1, "Error calling tool 'x'", (), None
    )
    assert sanitizer.filter(plain) is True
    assert plain.getMessage() == "Error calling tool 'x'"


def test_sanitizer_is_installed_once_per_process() -> None:
    logger = logging.getLogger(TOOL_MANAGER_LOGGER)
    first = boundary.install_tool_validation_log_sanitizer()
    second = boundary.install_tool_validation_log_sanitizer()
    assert first is second
    assert sum(isinstance(f, boundary.ToolValidationLogSanitizer) for f in logger.filters) == 1


def test_helper_call_order_against_set_contact_policy_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture_tool_manager_log: pytest.LogCaptureFixture,
) -> None:
    """The helper sends the owner token first; that call must fail without
    echoing the token anywhere, and the token-less retry must still apply."""
    _configure_isolated_runtime(monkeypatch, tmp_path)
    project = str(tmp_path / "project")
    owner = "owner-token-" + CANARY

    async def run() -> tuple[Any, str, Any, str]:
        async with Client(app.build_mcp_server()) as client:
            await _register(client, project, "RedStone", owner)
            base = {"project_key": project, "agent_name": "RedStone"}
            with_token = await client.call_tool(
                "set_contact_policy",
                {**base, "policy": "open", "registration_token": owner},
                raise_on_error=False,
            )
            after_token = _policy(tmp_path, "RedStone")
            without = await client.call_tool(
                "set_contact_policy",
                {**base, "policy": "open"},
                raise_on_error=False,
            )
            after_without = _policy(tmp_path, "RedStone")
            return with_token, after_token, without, after_without

    try:
        with_token, after_token, without, after_without = asyncio.run(run())
    finally:
        db.reset_database_state()

    text = _payload(with_token)
    assert with_token.is_error is True
    assert "set_contact_policy" in text and "does not accept 1" in text
    assert "registration_token" not in text
    assert CANARY not in text
    assert after_token != "open"
    assert without.is_error is False, _payload(without)
    assert after_without == "open"
    assert CANARY not in _log_text(capture_tool_manager_log)
