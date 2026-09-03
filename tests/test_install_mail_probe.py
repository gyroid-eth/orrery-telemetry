"""Native ORRERY Mail listener-adoption guards."""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import plistlib
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


class _RelativeDatabaseListener(http.server.BaseHTTPRequestHandler):
    """The legacy service reports a DB relative to its private working dir."""

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


def _run_legacy_dry_run(
    tmp_path: pathlib.Path,
    mail_port: int,
    *,
    loaded_legacy: bool,
    retire: bool,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    state_root = tmp_path / "native-state"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    label = "com.test.mcp-agent-mail"
    loaded_marker = tmp_path / "legacy-loaded"
    launchctl_log = tmp_path / "launchctl.log"
    if loaded_legacy:
        loaded_marker.touch()
        plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True)
        executable = tmp_path / "mcp_agent_mail" / ".venv" / "bin" / "serve-http"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        with plist.open("wb") as handle:
            plistlib.dump(
                {"Label": label, "ProgramArguments": [str(executable)]}, handle
            )
    for name, body in {
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\nprintf '%s\\n' Darwin\n",
        "launchctl": f"""#!/bin/sh
printf '%s\\n' "$*" >> '{launchctl_log}'
case "$1" in
  print)
    [ "$2" = "gui/{os.getuid()}/{label}" ] && [ -f '{loaded_marker}' ]
    ;;
  bootout)
    rm -f '{loaded_marker}'
    ;;
esac
""",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("AGENTSTACK_") or name == "PROJECT_KEY":
            env.pop(name, None)
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_HOME": str(home / ".agentstack"),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.retire-order",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_MAIL_STATE_ROOT": str(state_root),
        "AGENTSTACK_MAIL_SERVICE_VENV": str(pathlib.Path(sys.executable).parent.parent),
        "AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": label,
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
    })
    args = [
        "/bin/bash",
        str(ROOT / "scripts" / "install.sh"),
        "--scoped",
        "--dry-run",
    ]
    if retire:
        args.append("--retire-legacy-mail")
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    ), state_root


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


def test_retire_flag_plans_legacy_retirement_before_listener_reuse_probe(tmp_path):
    server = _serve(_RelativeDatabaseListener)
    try:
        result, _state_root = _run_legacy_dry_run(
            tmp_path, server.server_port, loaded_legacy=True, retire=True
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stdout + result.stderr
    retire = result.stdout.index(
        "DRY-RUN would retire legacy mail service: com.test.mcp-agent-mail"
    )
    skip_probe = result.stdout.index("is planned for retirement; skipping reuse probe")
    provision = result.stdout.index("installer will provision AgentStack Mail")
    assert retire < skip_probe < provision
    assert "unsupported database URL" not in result.stderr


def test_legacy_listener_without_retire_flag_fails_with_actionable_label(tmp_path):
    server = _serve(_RelativeDatabaseListener)
    try:
        result, _state_root = _run_legacy_dry_run(
            tmp_path, server.server_port, loaded_legacy=True, retire=False
        )
    finally:
        server.shutdown()

    assert result.returncode != 0
    assert "com.test.mcp-agent-mail" in result.stderr
    assert "--retire-legacy-mail" in result.stderr
    assert "is holding" in result.stderr
    assert "unsupported database URL" not in result.stderr


def test_normal_reinstall_without_legacy_target_still_reuses_native_listener(tmp_path):
    state_root = tmp_path / "native-state"
    state_root.mkdir()
    expected_db = state_root / "storage.sqlite3"
    expected_db.touch()
    _ForeignDatabaseListener.database = expected_db
    server = _serve(_ForeignDatabaseListener)
    try:
        result, _state_root = _run_legacy_dry_run(
            tmp_path, server.server_port, loaded_legacy=False, retire=False
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"existing AgentStack Mail database: {expected_db}" in result.stdout
    assert "retire legacy mail service" not in result.stdout
    assert "installer will provision AgentStack Mail" not in result.stdout
