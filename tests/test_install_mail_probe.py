"""Native ORRERY Mail listener-adoption guards."""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading


ROOT = pathlib.Path(__file__).resolve().parent.parent


class _SilentListener(http.server.BaseHTTPRequestHandler):
    """An occupied port that is not a healthy ORRERY Mail endpoint."""

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _ForeignDatabaseListener(http.server.BaseHTTPRequestHandler):
    """A healthy endpoint whose database is outside native isolated state."""

    database: pathlib.Path

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": "agentstack-installer-probe",
            "result": {
                "content": [],
                "structuredContent": {
                    "status": "ok",
                    "database_url": f"sqlite+aiosqlite:///{self.database}",
                },
                "isError": False,
            },
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_installer(tmp_path: pathlib.Path, mail_port: int):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "systemctl": "#!/bin/sh\nexit 1\n",
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Linux\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(home / ".agentstack"),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test",
    })
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "install.sh"), "--dashboard-only"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_non_mail_listener_is_rejected_without_guessing_a_database(tmp_path):
    server = _serve(_SilentListener)
    try:
        result = _run_installer(tmp_path, server.server_port)
    finally:
        server.shutdown()

    assert result.returncode != 0
    assert "did not answer an AgentStack Mail health check" in result.stderr
    assert not (tmp_path / "home/.agentstack/install-state.json").exists()


def test_healthy_listener_with_foreign_database_is_rejected(tmp_path):
    foreign = tmp_path / "foreign.sqlite3"
    foreign.touch()
    _ForeignDatabaseListener.database = foreign
    server = _serve(_ForeignDatabaseListener)
    try:
        result = _run_installer(tmp_path, server.server_port)
    finally:
        server.shutdown()

    assert result.returncode != 0
    assert "expected isolated database" in result.stderr
    assert str(foreign) in result.stderr
