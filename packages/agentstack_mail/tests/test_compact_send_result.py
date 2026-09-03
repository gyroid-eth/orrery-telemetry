"""Token-efficiency seams around message delivery.

Two independent behaviors, both measured on 2026-08-13:

- The send/reply tool result echoed the full body back to the sender at ~2.5×
  the body's size (content text + structuredContent both carry it).
  ``AGENTSTACK_MAIL_COMPACT_SEND_RESULT=true`` drops the echo and is the
  product default; an explicit false retains the compatibility shape.
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


def test_default_drops_the_body_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    result = asyncio.run(_send(tmp_path, "hello compact"))
    payload = result["deliveries"][0]["payload"]
    assert "body_md" not in payload
    assert payload["body_omitted"] is True


def test_explicit_false_keeps_the_body_echo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch, tmp_path, AGENTSTACK_MAIL_COMPACT_SEND_RESULT="false"
    )
    body = "x" * 2000
    result = asyncio.run(_send(tmp_path, body))
    payload = result["deliveries"][0]["payload"]
    assert payload["body_md"] == body
    assert "body_omitted" not in payload


def _read_signal(tmp_path: Path) -> dict[str, Any]:
    signal_root = tmp_path / "signals"
    files = sorted(signal_root.rglob("*.signal"))
    assert files, "no signal file was written"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def test_signal_carries_body_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch, tmp_path, AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true"
    )
    asyncio.run(_send(tmp_path, "しりとり: すいか"))
    message = _read_signal(tmp_path)["message"]
    assert message["body_snippet"] == "しりとり: すいか"
    assert message["body_truncated"] is False


def test_explicit_false_keeps_signal_metadata_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true",
        AGENTSTACK_MAIL_NOTIFICATIONS_INCLUDE_BODY="false",
    )
    asyncio.run(_send(tmp_path, "しりとり: すいか"))
    message = _read_signal(tmp_path)["message"]
    assert "body_snippet" not in message
    assert "body_truncated" not in message


async def _fetch(tmp_path: Path, agent: str) -> None:
    project_key = str(tmp_path / "project")
    async with Client(app.build_mcp_server()) as client:
        await client.call_tool(
            "fetch_inbox", {"project_key": project_key, "agent_name": agent}
        )


def _signal_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "signals").rglob("*.signal"))


def test_default_grace_lets_a_fresh_signal_survive_a_racing_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch, tmp_path, AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true"
    )
    asyncio.run(_send(tmp_path, "fresh"))
    assert _signal_files(tmp_path)
    asyncio.run(_fetch(tmp_path, "GreenCastle"))
    assert _signal_files(tmp_path)


def test_explicit_zero_fetch_clears_a_fresh_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true",
        AGENTSTACK_MAIL_SIGNAL_CLEAR_GRACE_SECONDS="0",
    )
    asyncio.run(_send(tmp_path, "fresh"))
    assert _signal_files(tmp_path)
    asyncio.run(_fetch(tmp_path, "GreenCastle"))
    assert not _signal_files(tmp_path)


def test_grace_window_lets_a_fresh_signal_survive_a_racing_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The 2026-08-13 race: recipient's own poll fetched (clearing signals)
    # in the window before the watcher injected, and the push notification
    # vanished. With a grace window the fresh signal survives the fetch.
    _configure(
        monkeypatch,
        tmp_path,
        AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true",
        AGENTSTACK_MAIL_SIGNAL_CLEAR_GRACE_SECONDS="30",
    )
    asyncio.run(_send(tmp_path, "racing"))
    assert _signal_files(tmp_path)
    asyncio.run(_fetch(tmp_path, "GreenCastle"))
    assert _signal_files(tmp_path), "fresh signal must survive the racing fetch"


def test_grace_window_still_clears_signals_older_than_the_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os as _os
    import time as _time

    _configure(
        monkeypatch,
        tmp_path,
        AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED="true",
        AGENTSTACK_MAIL_SIGNAL_CLEAR_GRACE_SECONDS="30",
    )
    asyncio.run(_send(tmp_path, "old"))
    files = _signal_files(tmp_path)
    assert files
    aged = _time.time() - 120
    for f in files:
        _os.utime(f, (aged, aged))
    asyncio.run(_fetch(tmp_path, "GreenCastle"))
    assert not _signal_files(tmp_path), "aged signals are leftovers and must clear"


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
