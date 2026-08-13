"""Token-efficiency seams around message delivery.

Two independent behaviors, both measured on 2026-08-13:

- The send/reply tool result echoed the full body back to the sender at ~2.5×
  the body's size (content text + structuredContent both carry it).
  ``AGENTSTACK_MAIL_COMPACT_SEND_RESULT=true`` drops the echo; default stays
  parity-compatible.
- Notification signals carried no body, so every recipient paid a fetch_inbox
  round trip even for a one-word message — the dominant share of
  notification→reply latency. Signals now carry a 400-character snippet.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from agentstack_mail import app, config, db


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> None:
    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'mail.sqlite3'}",
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR", str(tmp_path / "signals")
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_TOOLS_LOG_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    db.reset_database_state()
    config.clear_settings_cache()


async def _send(tmp_path: Path, body_md: str) -> dict[str, Any]:
    project_key = str(tmp_path / "project")
    async with Client(app.build_mcp_server()) as client:
        await client.call_tool("ensure_project", {"human_key": project_key})
        for name in ("BlueLake", "GreenCastle"):
            await client.call_tool(
                "register_agent",
                {
                    "project_key": project_key,
                    "program": "claude-code",
                    "model": "test-model",
                    "name": name,
                    "task_description": "compact-send-result test",
                },
            )
        result = await client.call_tool(
            "send_message",
            {
                "project_key": project_key,
                "sender_name": "BlueLake",
                "to": ["GreenCastle"],
                "subject": "probe",
                "body_md": body_md,
            },
        )
    return result.structured_content or result.data


def test_default_keeps_the_parity_compatible_body_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    result = asyncio.run(_send(tmp_path, "hello parity"))
    payload = result["deliveries"][0]["payload"]
    assert payload["body_md"] == "hello parity"
    assert "body_omitted" not in payload


def test_compact_flag_drops_the_body_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch, tmp_path, AGENTSTACK_MAIL_COMPACT_SEND_RESULT="true"
    )
    body = "x" * 2000
    result = asyncio.run(_send(tmp_path, body))
    payload = result["deliveries"][0]["payload"]
    assert "body_md" not in payload
    assert payload["body_omitted"] is True
    # Identifying fields the sender needs must survive the trim.
    assert payload["subject"] == "probe"
    assert payload["to"] == ["GreenCastle"]
    assert payload["id"]


def _read_signal(tmp_path: Path) -> dict[str, Any]:
    signal_root = tmp_path / "signals"
    files = sorted(signal_root.rglob("*.signal"))
    assert files, "no signal file was written"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def test_signal_stays_metadata_only_without_the_body_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The frozen differential behavior writes metadata-only signals; the
    # snippet must not appear unless explicitly enabled.
    _configure(
        monkeypatch, tmp_path, AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true"
    )
    asyncio.run(_send(tmp_path, "しりとり: すいか"))
    message = _read_signal(tmp_path)["message"]
    assert "body_snippet" not in message
    assert "body_truncated" not in message


def test_signal_carries_the_full_body_of_a_short_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true",
        AGENTSTACK_MAIL_NOTIFICATIONS_INCLUDE_BODY="true",
    )
    asyncio.run(_send(tmp_path, "しりとり: すいか"))
    message = _read_signal(tmp_path)["message"]
    assert message["body_snippet"] == "しりとり: すいか"
    assert message["body_truncated"] is False


def test_signal_truncates_a_long_body_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true",
        AGENTSTACK_MAIL_NOTIFICATIONS_INCLUDE_BODY="true",
    )
    asyncio.run(_send(tmp_path, "y" * 1000))
    message = _read_signal(tmp_path)["message"]
    assert message["body_snippet"] == "y" * 400
    assert message["body_truncated"] is True
