"""Antigravity uses the shared process matcher and preserves child cleanup on EXIT."""
from __future__ import annotations

import signal

from dashboard import server


ROOT = 73000
TREE = (
    {ROOT: "bash", 73010: "agy"},
    {ROOT: [73010]},
)


def test_antigravity_process_pid_uses_shared_program_matcher() -> None:
    assert server._agent_process_pid(ROOT, TREE, "antigravity") == 73010
    assert server._agent_process_pid(ROOT, TREE, "gemini") is None


def test_dashboard_exit_interrupts_only_delegated_antigravity_runtime(monkeypatch) -> None:
    killed: list[tuple[int, signal.Signals]] = []
    sent: list[list[str]] = []

    monkeypatch.setattr(
        server,
        "build_agents",
        lambda: [
            {
                "name": "GrayKepler",
                "category": "agent",
                "attached": False,
            }
        ],
    )
    monkeypatch.setattr(server, "_has_session", lambda _name: True)
    monkeypatch.setattr(server, "_agent_program", lambda _name: "antigravity")
    monkeypatch.setattr(
        server,
        "_tmux_session_env",
        lambda _name, key: "1" if key == "AGENTSTACK_RESERVED_IDENTITY" else "",
    )
    monkeypatch.setattr(server, "_fresh_agent_process_pid", lambda *_args: 73010)
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["tmux", "send-keys"]:
            sent.append(argv)
        return _Done()

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.do_exit("GrayKepler")

    assert result["ok"] is True
    assert killed == [(73010, signal.SIGINT)]
    assert not sent
    assert "interrupt-sent" in result["actions"]
    assert "provider-headless:antigravity" in result["actions"]


def test_interactive_antigravity_keeps_existing_slash_exit_path(monkeypatch) -> None:
    sent: list[list[str]] = []

    monkeypatch.setattr(
        server,
        "build_agents",
        lambda: [
            {
                "name": "GrayHopper",
                "category": "agent",
                "attached": False,
            }
        ],
    )
    monkeypatch.setattr(server, "_has_session", lambda _name: True)
    monkeypatch.setattr(server, "_agent_program", lambda _name: "antigravity")
    monkeypatch.setattr(server, "_tmux_session_env", lambda *_args: "")
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    class _Done:
        returncode = 0
        stderr = ""

        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["tmux", "display-message"]:
            return _Done("agy\n")
        if argv[:2] == ["tmux", "send-keys"]:
            sent.append(argv)
            return _Done()
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.do_exit("GrayHopper")

    assert result["ok"] is True
    assert "exit-sent" in result["actions"]
    assert any("/exit" in argv for argv in sent)
