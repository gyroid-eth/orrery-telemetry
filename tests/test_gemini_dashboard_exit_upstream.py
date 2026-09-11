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
        lambda history_days=None: [
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
        lambda history_days=None: [
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


def test_antigravity_resume_fails_closed_without_claude_lookup(monkeypatch) -> None:
    monkeypatch.setattr(server, "_agent_program", lambda _name: "antigravity")

    def forbidden_lookup(_session):
        raise AssertionError("Antigravity resume must not search Claude transcripts")

    monkeypatch.setattr(server, "_transcript_path", forbidden_lookup)
    result = server.do_resume("GrayHopper")

    assert result["ok"] is False
    assert "Antigravity" in result["error"]
    assert "resume" in result["error"]


def test_antigravity_history_fails_closed_without_cross_provider_lookup(monkeypatch) -> None:
    monkeypatch.setattr(server, "_agent_program", lambda _name: "antigravity")

    def forbidden_lookup(_session):
        raise AssertionError("Antigravity history must not search another provider transcript")

    monkeypatch.setattr(server, "_transcript_path", forbidden_lookup)
    monkeypatch.setattr(server, "_codex_transcript_path", forbidden_lookup)
    result = server.history_payload("GrayHopper", 20)

    assert result["ok"] is False
    assert "Antigravity" in result["error"]
    assert "history" in result["error"]


def test_jump_preserves_finished_antigravity_shell_instead_of_resuming(monkeypatch) -> None:
    opened: list[list[str]] = []

    monkeypatch.setattr(server, "_agent_program", lambda _name: "antigravity")
    monkeypatch.setattr(server, "_terminal_adapter", lambda: "terminal")
    monkeypatch.setattr(server, "_focus_existing_terminal", lambda _name: False)
    monkeypatch.setattr(
        server,
        "build_agents",
        lambda history_days=None: [{"name": "GrayHopper", "category": "finished"}],
    )

    def forbidden_resume(_session):
        raise AssertionError("finished Antigravity shell must remain attachable")

    monkeypatch.setattr(server, "do_resume", forbidden_resume)

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["tmux", "has-session"]:
            return _Done()
        if argv[:2] == ["tmux", "kill-session"]:
            raise AssertionError("finished Antigravity shell must not be killed on jump")
        raise AssertionError(f"unexpected command: {argv}")

    def fake_open(argv, title):
        opened.append(argv)
        return {"ok": True, "adapter": "terminal"}

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server, "_open_terminal_tmux", fake_open)

    result = server.do_jump("GrayHopper")

    assert result["ok"] is True
    assert opened == [["tmux", "attach", "-d", "-t", "=GrayHopper"]]
