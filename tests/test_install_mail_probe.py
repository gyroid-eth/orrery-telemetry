"""The installer's agent-mail probe must not become a gate it cannot open."""

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
    """A listener that speaks HTTP but never answers a health check.

    This stands in for the servers the probe cannot read: a build predating
    ``database_url`` in its health response, or one that wants credentials the
    installer has no way to find. Either way it is still agent-mail, and it is
    still holding the operator's database.
    """

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _fake_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "systemctl": "#!/bin/sh\nexit 1\n",
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Linux\n",
        "uv": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    return fake_bin


def _run_installer(home, tmp_path, mail_port, extra_env):
    install_dir = home / ".agentstack"
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{_fake_bin(tmp_path)}:{env['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(home / "clone-that-must-not-be-created"),
        "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
    })
    env.update(extra_env)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install.sh"), "--dashboard-only"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    return result, install_dir


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_explicit_database_survives_a_listener_the_probe_cannot_read(tmp_path):
    """An operator who names the database must not be blocked by the probe."""
    home = tmp_path / "home"
    external_root = home / ".local" / "share" / "mcp-agent-mail" / "repo"
    external_root.mkdir(parents=True)
    external_db = external_root / "storage.sqlite3"
    external_db.touch()

    server = _serve(_SilentListener)
    try:
        result, install_dir = _run_installer(
            home,
            tmp_path,
            server.server_port,
            {
                "AGENTSTACK_MAIL_DB": str(external_db),
                "AGENTSTACK_ASSUME_YES": "1",
            },
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "assume-yes: approved existing agent-mail server at "
        f"http://127.0.0.1:{server.server_port}/mcp"
    ) in result.stdout
    manifest = json.loads(
        (install_dir / "install-state.json").read_text(encoding="utf-8")
    )
    assert manifest["env"]["AGENTSTACK_MAIL_DB"] == str(external_db)


def test_unreadable_listener_without_a_named_database_still_stops(tmp_path):
    """Without evidence or instruction, guessing is worse than stopping."""
    home = tmp_path / "home"
    (home / ".agentstack").mkdir(parents=True)

    server = _serve(_SilentListener)
    try:
        result, install_dir = _run_installer(home, tmp_path, server.server_port, {})
    finally:
        server.shutdown()

    assert result.returncode != 0
    assert "AGENTSTACK_MAIL_DB" in result.stdout + result.stderr
    assert not (install_dir / "install-state.json").exists()
