"""Failure-path regression coverage for the installed functional self-test."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import socket
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "agentstack_selftest_under_test", ROOT / "scripts" / "selftest.py"
)
assert SPEC and SPEC.loader
SELFTEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELFTEST)


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_installed_env(
    tmp_path: pathlib.Path,
    *,
    mcp_url: str,
    dashboard_port: int,
    claude_mcp: bool = True,
) -> pathlib.Path:
    install_dir = tmp_path / ".agentstack"
    install_dir.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    claude_json = tmp_path / ".claude.json"
    if claude_mcp:
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "mcp-agent-mail": {"type": "http", "url": mcp_url}
            }
        }))
    (install_dir / "env.sh").write_text(
        "\n".join(
            (
                f"export AGENTSTACK_MCP_URL='{mcp_url}'",
                f"export AGENTSTACK_PROJECT_KEY='{project}'",
                f"export AGENTSTACK_PORT='{dashboard_port}'",
                f"export AGENTSTACK_CLAUDE_JSON='{claude_json}'",
                "",
            )
        ),
        encoding="utf-8",
    )
    return install_dir


def _run_main(monkeypatch, install_dir: pathlib.Path) -> int:
    monkeypatch.setattr(
        sys, "argv", ["agentstack-selftest", "--install-dir", str(install_dir)]
    )
    return SELFTEST.main()


def _stub_healthy_mail_flow(monkeypatch) -> list[str]:
    pair = ["ProbeSender", "ProbeRecipient"]

    class HealthyMail:
        def __init__(self, _url: str, _token: str = "") -> None:
            pass

        def call(self, tool: str, _arguments: dict):
            if tool == "health_check":
                return {"status": "healthy"}
            if tool == "ensure_project":
                return {"id": 1}
            raise AssertionError(f"unexpected agent-mail call: {tool}")

    monkeypatch.setattr(SELFTEST, "AgentMail", HealthyMail)
    monkeypatch.setattr(
        SELFTEST, "register_pair", lambda _mail, _project, _report: pair
    )
    monkeypatch.setattr(
        SELFTEST, "exchange", lambda _mail, _project, _pair, _report: None
    )
    monkeypatch.setattr(
        SELFTEST, "reservations", lambda _mail, _project, _pair, _report: None
    )
    monkeypatch.setattr(
        SELFTEST, "cleanup", lambda _mail, _project, _pair, _report: None
    )
    return pair


def test_missing_agent_mail_makes_selftest_fail(tmp_path, monkeypatch, capsys):
    port = _unused_port()
    install_dir = _write_installed_env(
        tmp_path,
        mcp_url=f"http://127.0.0.1:{port}/mcp",
        dashboard_port=_unused_port(),
    )

    assert _run_main(monkeypatch, install_dir) == 1
    assert "self-test failed" in capsys.readouterr().err


def test_missing_claude_mcp_registration_makes_selftest_fail(
    tmp_path, monkeypatch, capsys
):
    install_dir = _write_installed_env(
        tmp_path,
        mcp_url="http://agent-mail.invalid/mcp",
        dashboard_port=_unused_port(),
        claude_mcp=False,
    )

    assert _run_main(monkeypatch, install_dir) == 1
    stderr = capsys.readouterr().err
    assert "Claude MCP registration is missing" in stderr
    assert "agentstack-doctor" in stderr


def test_missing_dashboard_makes_selftest_fail(tmp_path, monkeypatch, capsys):
    _stub_healthy_mail_flow(monkeypatch)
    install_dir = _write_installed_env(
        tmp_path,
        mcp_url="http://agent-mail.invalid/mcp",
        dashboard_port=_unused_port(),
    )

    assert _run_main(monkeypatch, install_dir) == 1
    stderr = capsys.readouterr().err
    assert "dashboard API" in stderr
    assert "self-test failed" in stderr


def test_dashboard_on_different_database_makes_selftest_fail(
    tmp_path, monkeypatch, capsys
):
    pair = _stub_healthy_mail_flow(monkeypatch)
    install_dir = _write_installed_env(
        tmp_path,
        mcp_url="http://agent-mail.invalid/mcp",
        dashboard_port=8770,
    )

    # Model the regression exactly: agent-mail registered the pair, but the
    # dashboard successfully answers from another database and lists neither.
    monkeypatch.setattr(
        SELFTEST,
        "dashboard",
        lambda _url, path: {"agents": [{"name": "UnrelatedAgent"}]}
        if path == "/api/agents"
        else {"nodes": [], "edges": []},
    )

    assert _run_main(monkeypatch, install_dir) == 1
    stderr = capsys.readouterr().err
    assert all(name in stderr for name in pair)
    assert "different database" in stderr
    assert "self-test failed" in stderr
