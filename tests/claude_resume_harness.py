"""A local Mail stand-in and a runner for the login-shell body do_resume builds.

The tests execute the real command tmux would run, with a synthetic Claude CLI,
so their assertions are about what it does, not about its text.
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


class StandInMail(http.server.BaseHTTPRequestHandler):
    calls: list[tuple[str, dict]] = []
    # What register_agent reports as the row's retirement (None: active).
    retired_at: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        params = request.get("params") or {}
        name, arguments = params.get("name"), params.get("arguments") or {}
        StandInMail.calls.append((name, dict(arguments)))
        if name == "health_check":
            result = {"structuredContent": {"status": "ok"}}
        elif name == "ensure_project":
            result = {"structuredContent": {"id": 1}}
        elif name == "register_agent":
            result = {"structuredContent": {"id": 73, "name": arguments.get("name"),
                                            "registration_token": arguments.get("registration_token", ""),
                                            "retired_at": StandInMail.retired_at}}
        elif name in {"retire_agent", "unretire_agent"}:
            result = {"structuredContent": {
                "status": "retired" if name == "retire_agent" else "active",
                "agent_name": arguments.get("agent_name"), "project_key": arguments.get("project_key")}}
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
    StandInMail.calls = []
    StandInMail.retired_at = None
    httpd = http.server.HTTPServer(("127.0.0.1", 0), StandInMail)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[:2]
    monkeypatch.setenv("AGENTSTACK_MCP_URL", f"http://{host}:{port}/mcp")
    monkeypatch.setattr(server, "MAIL_HTTP_BEARER_MODE", "disabled")
    try:
        yield StandInMail.calls
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_resumed(launches, tmp_path, cli_body, env=None):
    """Execute the produced login-shell body with a synthetic Claude CLI."""
    cli = Path(server.ABS_CLAUDE)
    cli.write_text("#!/bin/bash\n" + cli_body, encoding="utf-8")
    cli.chmod(0o700)
    # The login shell puts ~/.local/bin first. A stand-in tmux there keeps the
    # command away from any real tmux server; it lists FAKE_TMUX_PANES,
    # or fails when FAKE_TMUX_FAIL is set.
    bin_dir = tmp_path / "empty-home" / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_tmux = bin_dir / "tmux"
    fake_tmux.write_text('#!/bin/bash\n[[ -n "${FAKE_TMUX_FAIL:-}" ]] && exit 1\n'
                         '[[ "$1" == list-panes ]] && printf "%s" "${FAKE_TMUX_PANES:-}"\nexit 0\n',
                         encoding="utf-8")
    fake_tmux.chmod(0o700)
    # A tmux server's global environment can carry another agent's model.
    environment = {**os.environ, "CLAUDE_CHILD_MODEL": "ambient-child-model",
                   "AGENTSTACK_CLAUDE_MODEL": "ambient-model"}
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    environment.update(env or {})
    return subprocess.run(["/bin/bash", "-c", launches[-1][-1]], cwd=tmp_path, env=environment,
                          capture_output=True, text=True, timeout=60)
