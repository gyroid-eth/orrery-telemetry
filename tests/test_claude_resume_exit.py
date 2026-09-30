"""A resumed Claude leaves its Mail row as it found it (#143).

The login-shell body that do_resume hands tmux is executed here with a
synthetic CLI and a local Mail stand-in, so the assertions are about what the
real command does after the conversation ends, not about its text.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from dashboard import server
from test_claude_resume_mail import NAME, TOKEN, resume


class _Mail(http.server.BaseHTTPRequestHandler):
    calls: list[tuple[str, dict]] = []

    def do_POST(self) -> None:  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        params = request.get("params") or {}
        name, arguments = params.get("name"), params.get("arguments") or {}
        _Mail.calls.append((name, dict(arguments)))
        if name == "health_check":
            result = {"structuredContent": {"status": "ok"}}
        elif name == "ensure_project":
            result = {"structuredContent": {"id": 1}}
        elif name == "register_agent":
            result = {"structuredContent": {"id": 73, "name": arguments.get("name"),
                                            "registration_token": arguments.get("registration_token", "")}}
        else:
            result = {"structuredContent": {}}
        body = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def mail(monkeypatch):
    _Mail.calls = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Mail)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[:2]
    monkeypatch.setenv("AGENTSTACK_MCP_URL", f"http://{host}:{port}/mcp")
    monkeypatch.setattr(server, "MAIL_HTTP_BEARER_MODE", "disabled")
    try:
        yield _Mail.calls
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_resumed(launches, tmp_path, cli_body):
    """Execute the produced login-shell body with a synthetic Claude CLI."""
    cli = Path(server.ABS_CLAUDE)
    cli.write_text("#!/bin/bash\n" + cli_body, encoding="utf-8")
    cli.chmod(0o700)
    (tmp_path / "empty-home").mkdir(exist_ok=True)
    # A tmux server's global environment can carry another agent's model.
    env = {**os.environ, "CLAUDE_CHILD_MODEL": "ambient-child-model",
           "AGENTSTACK_CLAUDE_MODEL": "ambient-model"}
    return subprocess.run(["/bin/bash", "-c", launches[-1][-1]], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("status", [0, 3])
def test_token_only_exit_retires_the_row_resume_unretired(resume, mail, tmp_path, status):
    """#143: without child state nothing re-retired the row after `/exit`."""
    runtime, registration, launches, calls = resume
    token = runtime / f"agent_token_{NAME}"
    before = (token.read_bytes(), token.stat().st_mode)
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    assert [method for method, _ in calls] == ["register_agent", "unretire_agent"]
    process = run_resumed(launches, tmp_path, f"exit {status}\n")
    assert process.returncode == status, process.stderr
    retires = [arguments for method, arguments in mail if method == "retire_agent"]
    assert retires == [{"project_key": registration["project_key"], "agent_name": NAME,
                        "registration_token": TOKEN}], mail
    # Exit restores the retirement only: the owner credential stays for the next resume.
    assert (token.read_bytes(), token.stat().st_mode) == before
    assert not (runtime / "child-agents" / f"{NAME}.json").exists()
    assert TOKEN not in process.stdout + process.stderr + json.dumps(launches)


def test_token_only_exit_leaves_a_row_that_was_active_before_resume(resume, mail, tmp_path):
    runtime, registration, launches, calls = resume
    registration["retired_at"] = None
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    process = run_resumed(launches, tmp_path, "exit 0\n")
    assert process.returncode == 0, process.stderr
    assert "retire_agent" not in [method for method, _ in mail]
    assert (runtime / f"agent_token_{NAME}").read_text() == TOKEN
