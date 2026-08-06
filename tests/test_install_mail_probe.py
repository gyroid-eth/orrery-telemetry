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
import time
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX, stop_dashboard  # noqa: E402


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
        "PATH": f"{_fake_bin(tmp_path)}:/usr/bin:/bin:/usr/sbin:/sbin",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(home / "clone-that-must-not-be-created"),
        "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
        # Never register under the label a real install uses.
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
    })
    env.update(extra_env)
    try:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "install.sh"), "--dashboard-only"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
    finally:
        stop_dashboard(home)
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


def test_fresh_install_clones_prepares_and_keeps_agent_mail_running(tmp_path):
    home = tmp_path / "home"
    upstream = tmp_path / "upstream-agent-mail"
    package = upstream / "src" / "mcp_agent_mail"
    package.mkdir(parents=True)
    (upstream / "pyproject.toml").write_text(
        "[project]\nname='mcp-agent-mail'\nversion='0.0.0'\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        """import http.server
import json
import pathlib
import sys

args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
pathlib.Path("storage.sqlite3").touch()

class Handler(http.server.BaseHTTPRequestHandler):
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
                    "database_url": "sqlite+aiosqlite:///./storage.sqlite3",
                },
                "isError": False,
            },
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
""",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "fresh-fake-bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    for name, body in {
        "git": """#!/bin/sh
if [ "$1" = "clone" ]; then
  cp -R "$2/." "$3/"
  mkdir -p "$3/.git"
  exit 0
fi
exit 0
""",
        "systemctl": "#!/bin/sh\nexit 1\n",
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Linux\n",
        "uv": """#!/bin/sh
printf '%s\n' "$*" >> "$AGENTSTACK_TEST_UV_LOG"
if [ "$1" = "--directory" ]; then
  directory="$2"
  shift 2
fi
case "$1" in
  sync)
    touch "$AGENTSTACK_TEST_UV_SYNCED"
    ;;
  run)
    shift
    while [ "$1" = "--no-dev" ] || [ "$1" = "--no-sync" ]; do shift; done
    cd "$directory" || exit 1
    PYTHONPATH="$directory/src" exec "$@"
    ;;
  *)
    exit 2
    ;;
esac
""",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    # install.sh starts the mail server as `uv run … python -m …`, and real uv
    # guarantees an interpreter named `python` inside the environment it
    # creates. The fake uv above only execs, so `python` had to come from the
    # host — which meant this test passed or failed on whether the machine
    # happened to carry a bare `python` on PATH. It does under conda and does
    # not under a plain macOS PATH, where it failed as "python: not found" and
    # read like a regression in whichever branch happened to run it.
    python_shim = fake_bin / "python"
    python_shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python_shim.chmod(0o755)

    install_dir = home / ".agentstack"
    mail_dir = home / "mcp_agent_mail"
    mail_home = home / ".mcp_agent_mail"
    project = tmp_path / "project"
    project.mkdir()
    mail_port = _free_port()
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(mail_dir),
        "AGENTSTACK_MAIL_HOME": str(mail_home),
        "AGENTSTACK_AGENT_MAIL_REPO": str(upstream),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
        # Never register under the label a real install uses.
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_TEST_UV_LOG": str(uv_log),
        "AGENTSTACK_TEST_UV_SYNCED": str(tmp_path / "uv-synced"),
    })
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install.sh"), "--dashboard-only"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    mail_pidfile = mail_home / "agent-mail.pid"
    dashboard_pidfile = install_dir / "runtime" / "dashboard.pid"
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (mail_dir / ".git").is_dir()
        assert (tmp_path / "uv-synced").is_file()
        uv_calls = uv_log.read_text(encoding="utf-8")
        assert "sync --no-dev" in uv_calls
        assert "run --no-dev --no-sync" in uv_calls
        assert (mail_dir / "storage.sqlite3").is_file()
        assert mail_pidfile.is_file()
        supervisor_pid = int(mail_pidfile.read_text(encoding="utf-8").strip())
        os.kill(supervisor_pid, 0)

        request = urllib.request.Request(
            f"http://127.0.0.1:{mail_port}/mcp",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": "fresh-install-check",
                "method": "tools/call",
                "params": {"name": "health_check", "arguments": {}},
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            health = json.loads(response.read())
        assert health["result"]["structuredContent"]["status"] == "ok"
        assert "agent-mail ready at" in result.stdout
        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        assert any(
            service.get("pidfile") == str(mail_pidfile)
            for service in manifest["services"]
        )
    finally:
        for pidfile in (dashboard_pidfile, mail_pidfile):
            try:
                pid = int(pidfile.read_text(encoding="utf-8").strip())
                os.kill(pid, 15)
            except (FileNotFoundError, ProcessLookupError, ValueError):
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", mail_port)) != 0:
                    break
            time.sleep(0.05)
