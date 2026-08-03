"""Dashboard service supervision, diagnostics, and bounded log coverage."""

from __future__ import annotations

import json
import http.server
import os
import pathlib
import plistlib
import pty
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "dashboard" / "service_runner.py"
PLIST_TEMPLATE = ROOT / "dashboard" / "agentdashboard.plist.template"


def _wait_for(path: pathlib.Path, needle: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        if needle in text:
            return text
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} not found in {path}:\n{text}")


def _runner_env(tmp_path: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    runtime = tmp_path / "runtime"
    env.update({
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_DASHBOARD_LOG": str(runtime / "dashboard.log"),
        "AGENTSTACK_DASHBOARD_RUN_STATE": str(runtime / "dashboard-service.json"),
        "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": str(1024 * 1024),
        "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "2",
        "AGENTSTACK_DASHBOARD_RESTART_DELAY": "0",
    })
    return env


def _fake_python_39() -> str:
    return """#!/bin/sh
case "$2" in
  *"sys.version_info[:3]"*)
    echo 3.9.6
    exit 0
    ;;
  *"sys.version_info >= (3, 10)"*)
    exit 1
    ;;
esac
echo "fake Python 3.9 only supports version probes" >&2
exit 1
"""


def test_service_definitions_use_runner_runtime_log_and_restart_policy():
    with PLIST_TEMPLATE.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"][1] == "__INSTALL_DIR__/service_runner.py"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert plist["ThrottleInterval"] == 5
    assert plist["StandardOutPath"] == "__DASHBOARD_LOG__"
    assert plist["StandardErrorPath"] == "__DASHBOARD_LOG__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_DASHBOARD_LOG"] == "__DASHBOARD_LOG__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_LANG"] == "__LANG__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_MURMUR"] == "__MURMUR__"

    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert 'ExecStart={esc(\'$PYTHON_BIN\')} {esc(\'$DASHBOARD_DIR/service_runner.py\')}' in installer
    assert '"Restart=always"' in installer
    assert "AGENTSTACK_DASHBOARD_SELF_RESTART=1" in installer


def test_launchd_install_explicitly_kickstarts_before_checking_health(
    tmp_path,
):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Darwin\n",
        "uv": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_TERMINAL": "none",
    })
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts" / "install.sh"),
            "--dashboard-only", "--dry-run",
            "--install-dir", str(home / ".agentstack"),
            "--project-key", str(project),
            "--port", "18952",
            "--label-prefix", "org.agentstack.order-test",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    output = result.stdout
    bootstrap = output.index("launchctl bootstrap gui/")
    enable = output.index("launchctl enable gui/")
    kickstart = output.index("launchctl kickstart gui/")
    health = output.index("verify dashboard API responds")
    assert bootstrap < enable < kickstart < health


def test_doctor_rejects_loaded_but_not_running_launchd_job(tmp_path):
    home = tmp_path / "home"
    install_dir = home / ".agentstack"
    runtime = install_dir / "runtime"
    database = tmp_path / "storage.sqlite3"
    project = tmp_path / "project"
    fake_bin = tmp_path / "fake-bin"
    for directory in (
        install_dir / "dashboard",
        install_dir / "hooks",
        runtime,
        project,
        fake_bin,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for path in (
        install_dir / "dashboard" / "server.py",
        install_dir / "dashboard" / "service_runner.py",
        install_dir / "hooks" / "spawn_child.sh",
        runtime / "dashboard.log",
        database,
    ):
        path.touch()
    (install_dir / "hooks" / "spawn_child.sh").chmod(0o755)
    (install_dir / "env.sh").write_text(
        f"export AGENTSTACK_PYTHON={sys.executable}\n"
        f"export AGENTSTACK_MAIL_DB={database}\n"
        f"export AGENTSTACK_RUNTIME_DIR={runtime}\n"
        f"export AGENTSTACK_DASHBOARD_LOG={runtime / 'dashboard.log'}\n"
        f"export AGENTSTACK_PROJECT_KEY={project}\n",
        encoding="utf-8",
    )
    (install_dir / "install-state.json").write_text(
        json.dumps({
            "services": [{
                "kind": "launchd",
                "label": "org.agentstack.test-dashboard",
                "path": str(home / "Library" / "LaunchAgents" / "test.plist"),
            }],
        }),
        encoding="utf-8",
    )
    for name, body in {
        "launchctl": (
            "#!/bin/sh\n"
            "printf '%s\\n' '{' '    state = not running' '}'\n"
        ),
        "tmux": "#!/bin/sh\nexit 1\n",
        "uv": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
    })

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "doctor.sh"),
            "--install-dir",
            str(install_dir),
        ],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "launchd job is loaded but not running" in result.stdout
    assert "ok: dashboard service mode launchd" not in result.stdout

    (fake_bin / "launchctl").write_text(
        "#!/bin/sh\nprintf '%s\\n' '{' '    state = running' '    pid = 4321' '}'\n",
        encoding="utf-8",
    )
    running = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "doctor.sh"),
            "--install-dir",
            str(install_dir),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "dashboard service mode launchd" in running.stdout
    assert "running" in running.stdout


def test_installer_rejects_explicit_python_39_before_writing(tmp_path):
    python39 = tmp_path / "usr" / "bin" / "python3"
    python39.parent.mkdir(parents=True)
    python39.write_text(_fake_python_39(), encoding="utf-8")
    python39.chmod(0o755)
    install_dir = tmp_path / "install"
    env = os.environ.copy()
    env["AGENTSTACK_PYTHON"] = str(python39)

    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts" / "install.sh"),
            "--assume-yes", "--dashboard-only", "--dry-run",
            "--install-dir", str(install_dir),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "AGENTSTACK_PYTHON must be Python 3.10 or newer" in result.stderr
    assert "found 3.9.6" in result.stderr
    assert str(python39) in result.stderr
    assert not install_dir.exists()


def test_installer_skips_old_path_python_for_versioned_candidate(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    python39 = fake_bin / "python3"
    python39.write_text(_fake_python_39(), encoding="utf-8")
    python39.chmod(0o755)
    (fake_bin / "python3.10").symlink_to(sys.executable)
    for name in ("tmux", "uv"):
        command = fake_bin / name
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)

    env = os.environ.copy()
    env.pop("AGENTSTACK_PYTHON", None)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    result = subprocess.run(
        [
            "bash", str(ROOT / "scripts" / "install.sh"),
            "--dashboard-only", "--dry-run",
            "--install-dir", str(tmp_path / "install"),
            "--project-key", str(tmp_path),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    python_lines = [
        line for line in result.stdout.splitlines() if line.startswith("python: ")
    ]
    assert len(python_lines) == 1
    assert python_lines[0].startswith(f"python: {fake_bin / 'python3.10'} ")


def test_macos_launchd_bootstrap_failure_falls_back_and_finishes_install(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    commands = {
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Darwin\n",
        "uv": "#!/bin/sh\nexit 0\n",
        "launchctl": """#!/bin/sh
echo "$*" >> "$AGENTSTACK_TEST_LAUNCHCTL_LOG"
case "$1" in
  bootstrap)
    echo "Bootstrap failed: 125: Domain does not support specified action" >&2
    exit 125
    ;;
  print)
    exit 1
    ;;
esac
exit 0
""",
    }
    for name, body in commands.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    home = tmp_path / "home"
    install_dir = home / ".agentstack"
    project = tmp_path / "project"
    project.mkdir()
    mail_dir = home / "mcp_agent_mail"
    (mail_dir / ".git").mkdir(parents=True)
    (mail_dir / "storage.sqlite3").touch()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(mail_dir),
        "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
        "AGENTSTACK_PORT": str(port),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_TEST_LAUNCHCTL_LOG": str(launchctl_log),
    })

    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        ["bash", str(ROOT / "scripts" / "install.sh")],
        cwd=ROOT,
        env=env,
        stdin=slave_fd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(slave_fd)
    try:
        os.write(master_fd, b"yes\nyes\nyes\n")
        stdout, stderr = process.communicate(timeout=60)
    finally:
        os.close(master_fd)
    assert process.returncode == 0, stderr
    result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    try:
        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        assert manifest["services"] == [{
            "kind": "nohup",
            "pidfile": str(install_dir / "runtime" / "dashboard.pid"),
        }]
        assert "launchd could not bootstrap" in result.stderr
        assert "Service mode: supervised background" in result.stdout
        assert "dashboard healthy:" in result.stdout
        assert "bootstrap gui/" in launchctl_log.read_text(encoding="utf-8")
        assert not list((home / "Library" / "LaunchAgents").glob("*.plist"))
        assert "<!-- >>> claude-agent-stack (managed: agentstack-codex-setup) -->" in (
            home / ".codex" / "AGENTS.md"
        ).read_text(encoding="utf-8")
        assert "<!-- >>> claude-agent-stack (managed: agentstack-claude-setup) -->" in (
            project / "CLAUDE.md"
        ).read_text(encoding="utf-8")

        status = subprocess.run(
            ["bash", str(install_dir / "dashboard" / "agentctl.sh"), "status"],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "service mode: supervised-background" in status.stdout
        assert "http 200" in status.stdout

        restart = subprocess.run(
            ["bash", str(install_dir / "dashboard" / "agentctl.sh"), "restart"],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "started in supervised-background mode" in restart.stdout
        restarted_status = subprocess.run(
            ["bash", str(install_dir / "dashboard" / "agentctl.sh"), "status"],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "service mode: supervised-background" in restarted_status.stdout

        doctor = subprocess.run(
            ["bash", str(install_dir / "bin" / "agentstack-doctor"),
             "--install-dir", str(install_dir)],
            env=env,
            text=True,
            capture_output=True,
        )
        assert "dashboard service mode supervised-background (pid " in doctor.stdout

        installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        main = installer[installer.index("main() {"):]
        assert main.index("safe_managed_doc_setups") < main.index("start_service")
        assert main.index("start_service") < main.index("write_manifest")
    finally:
        subprocess.run(
            ["bash", str(install_dir / "dashboard" / "agentctl.sh"), "stop"],
            env=env,
            text=True,
            capture_output=True,
        )


def test_installer_reuses_existing_agent_mail_listener_database(tmp_path):
    home = tmp_path / "home"
    external_root = (
        home / ".local" / "share" / "mcp-agent-mail" / "git_mailbox_repo"
    )
    external_root.mkdir(parents=True)
    external_db = external_root / "storage.sqlite3"
    external_db.touch()

    class AgentMailHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(content_length)
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": "agentstack-installer-probe",
                "result": {
                    "content": [],
                    "structuredContent": {
                        "status": "ok",
                        "http_host": "127.0.0.1",
                        "http_port": self.server.server_port,
                        # Upstream's default is cwd-relative.  The installer
                        # must not reinterpret it below AGENTSTACK_MAIL_DIR.
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

    mail_server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        AgentMailHandler,
    )
    mail_thread = threading.Thread(target=mail_server.serve_forever, daemon=True)
    mail_thread.start()

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

    install_dir = home / ".agentstack"
    mail_dir = home / "new-clone-that-must-not-be-created"
    project = tmp_path / "project"
    project.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dashboard_port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(mail_dir),
        "AGENTSTACK_MAIL_HOME": str(home / ".mcp_agent_mail"),
        "AGENTSTACK_MCP_URL": (
            f"http://127.0.0.1:{mail_server.server_port}/mcp"
        ),
        "AGENTSTACK_PORT": str(dashboard_port),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
    })

    try:
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "install.sh"),
                "--dashboard-only",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        assert manifest["env"]["AGENTSTACK_MAIL_DB"] == str(external_db)
        assert "existing agent-mail listener detected" in result.stdout
        assert f"existing agent-mail database: {external_db}" in result.stdout
        assert "non-interactive install: using" in result.stdout
        assert "reuse existing agent-mail server" in result.stdout
        assert not mail_dir.exists()

        doctor = subprocess.run(
            [
                "bash",
                str(install_dir / "bin" / "agentstack-doctor"),
                "--install-dir",
                str(install_dir),
            ],
            env=env,
            text=True,
            capture_output=True,
        )
        assert f"ok: agent-mail database {external_db}" in doctor.stdout
    finally:
        subprocess.run(
            [
                "bash",
                str(install_dir / "dashboard" / "agentctl.sh"),
                "stop",
            ],
            env=env,
            text=True,
            capture_output=True,
        )
        mail_server.shutdown()
        mail_server.server_close()
        mail_thread.join(timeout=5)


def test_installer_refuses_to_record_an_unresolved_mail_database(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Linux\n",
        "uv": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    home = tmp_path / "home"
    install_dir = home / ".agentstack"
    project = tmp_path / "project"
    project.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dashboard_port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_MAIL_DIR": str(home / "not-installed"),
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
        "AGENTSTACK_PORT": str(dashboard_port),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
    })

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "install.sh"),
            "--assume-yes",
            "--dashboard-only",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "no existing SQLite database was found" in result.stderr
    assert "set AGENTSTACK_MAIL_DB" in result.stderr
    assert not (install_dir / "env.sh").exists()
    assert not (install_dir / "install-state.json").exists()


def test_mail_watcher_process_drives_health_and_agents_without_launchd(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text(
        """#!/bin/sh
case "$1" in
  list-sessions)
    printf 'mail-watcher\\0371700000000\\0371700000100\\n'
    ;;
  list-panes)
    printf 'mail-watcher\\03711\\037bash\\037mail-watcher\\n'
    ;;
esac
""",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    launchctl.chmod(0o755)

    watcher_script = tmp_path / "watch_agent_mail_signals.sh"
    watcher_script.write_text(
        "#!/bin/sh\n"
        'while :; do : > "$AGENTSTACK_MAIL_WATCHER_HEARTBEAT"; sleep 1; done\n',
        encoding="utf-8",
    )
    watcher_script.chmod(0o755)
    heartbeat = tmp_path / "watcher-heartbeat"
    watcher_env = os.environ.copy()
    watcher_env["AGENTSTACK_MAIL_WATCHER_HEARTBEAT"] = str(heartbeat)
    watcher = subprocess.Popen([str(watcher_script)], env=watcher_env)

    pidfile = tmp_path / "watcher.pid"
    pidfile.write_text(f"{watcher.pid}\n", encoding="utf-8")
    database = tmp_path / "storage.sqlite3"
    database.touch()
    runtime = tmp_path / "runtime"
    signals = tmp_path / "signals"
    project = tmp_path / "project"
    runtime.mkdir()
    signals.mkdir()
    project.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_PORT": str(port),
        "AGENTSTACK_MAIL_DB": str(database),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_SIGNALS_DIR": str(signals),
        "AGENTSTACK_MAIL_WATCHER_PIDFILE": str(pidfile),
        "AGENTSTACK_MAIL_WATCHER_HEARTBEAT": str(heartbeat),
        "AGENTSTACK_TERMINAL": "none",
    })
    dashboard = subprocess.Popen(
        [sys.executable, str(ROOT / "dashboard" / "server.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def get_json(path: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}",
                    timeout=1,
                ) as response:
                    return json.loads(response.read())
            except Exception as exc:  # server startup race
                last_error = exc
                time.sleep(0.05)
        raise AssertionError(f"dashboard endpoint did not start: {last_error}")

    try:
        health = get_json("/api/mail-watcher-health")
        assert health["watcher_running"] is True
        assert health["watcher_mode"] == "pidfile"
        assert health["watcher_pid"] == watcher.pid
        assert health["status"] == "green"

        agents = get_json("/api/agents")["agents"]
        watcher_card = next(row for row in agents if row["name"] == "mail-watcher")
        assert watcher_card["category"] == "infra"
        assert watcher_card["running"] is True
        assert watcher_card["live"] == "watcher: pidfile"

        watcher.terminate()
        watcher.wait(timeout=5)
        time.sleep(5.1)
        stale = get_json("/api/mail-watcher-health")
        assert stale["watcher_running"] is False
        assert stale["status"] == "red"
    finally:
        if watcher.poll() is None:
            watcher.terminate()
            watcher.wait(timeout=5)
        dashboard.terminate()
        dashboard.wait(timeout=10)


def test_mail_watcher_publishes_pidfile_and_live_heartbeat(tmp_path):
    watcher_lock = tmp_path / "watcher.lock"
    pidfile = watcher_lock / "watcher.pid"
    heartbeat = watcher_lock / "heartbeat"
    env = os.environ.copy()
    env.update({
        "AGENTSTACK_MAIL_HOME": str(tmp_path / "mail-home"),
        "AGENTSTACK_SIGNALS_DIR": str(tmp_path / "signals"),
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
        "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(watcher_lock),
    })
    watcher = subprocess.Popen(
        ["bash", str(ROOT / "hooks" / "watch_agent_mail_signals.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(pidfile, str(watcher.pid))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not heartbeat.exists():
            time.sleep(0.05)
        assert heartbeat.exists()
        first_mtime = heartbeat.stat().st_mtime_ns
        while (
            time.monotonic() < deadline
            and heartbeat.stat().st_mtime_ns == first_mtime
        ):
            time.sleep(0.1)
        assert heartbeat.stat().st_mtime_ns > first_mtime
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)

    assert not pidfile.exists()
    assert not heartbeat.exists()


def test_runner_records_sigkill_and_self_restarts_for_nohup(tmp_path):
    child = tmp_path / "crash_then_wait.py"
    counter = tmp_path / "attempts.txt"
    child.write_text(
        """import os
import pathlib
import signal
import time

counter = pathlib.Path(os.environ["DASHBOARD_TEST_COUNTER"])
try:
    attempt = int(counter.read_text()) + 1
except FileNotFoundError:
    attempt = 1
counter.write_text(str(attempt))
print(f"attempt={attempt}", flush=True)
if attempt == 1:
    os.kill(os.getpid(), signal.SIGKILL)
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    env = _runner_env(tmp_path)
    env["AGENTSTACK_DASHBOARD_SELF_RESTART"] = "1"
    env["DASHBOARD_TEST_COUNTER"] = str(counter)
    state_path = pathlib.Path(env["AGENTSTACK_DASHBOARD_RUN_STATE"])
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"supervisor_pid": 999, "started_at": "old"}), encoding="utf-8")
    log_path = pathlib.Path(env["AGENTSTACK_DASHBOARD_LOG"])

    runner = subprocess.Popen([sys.executable, str(RUNNER), str(child)], env=env)
    try:
        text = _wait_for(log_path, "server | attempt=2")
        assert "unclean supervisor exit detected" in text
        assert "dashboard server exited" in text
        assert "signal=SIGKILL(9)" in text
        assert "restarting dashboard server in 0 seconds" in text
    finally:
        runner.send_signal(signal.SIGTERM)
        runner.wait(timeout=10)

    assert runner.returncode == 0
    assert not state_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "supervisor received signal=SIGTERM(15)" in text
    assert "dashboard supervisor stopped after requested signal" in text


def test_runner_rotates_logs_and_leaves_restart_to_service_manager(tmp_path):
    child = tmp_path / "noisy_failure.py"
    child.write_text(
        """for number in range(100):
    print(f"line-{number:03d}-" + "x" * 180, flush=True)
raise RuntimeError("controlled dashboard crash")
""",
        encoding="utf-8",
    )
    env = _runner_env(tmp_path)
    env["AGENTSTACK_DASHBOARD_LOG_MAX_BYTES"] = "1024"
    env["AGENTSTACK_DASHBOARD_LOG_BACKUPS"] = "2"
    log_path = pathlib.Path(env["AGENTSTACK_DASHBOARD_LOG"])

    result = subprocess.run(
        [sys.executable, str(RUNNER), str(child)],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    logs = sorted(log_path.parent.glob("dashboard.log*"))
    assert [path.name for path in logs] == [
        "dashboard.log", "dashboard.log.1", "dashboard.log.2"
    ]
    assert all(path.stat().st_size <= 1400 for path in logs)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in logs)
    assert "controlled dashboard crash" in combined
    assert "exit_code=1" in combined
    assert "leaving restart to the service manager" in combined


def test_supervised_child_exits_if_runner_is_sigkilled(tmp_path):
    child = tmp_path / "watch_supervisor.py"
    child.write_text(
        """import signal
import time
from dashboard.server import _start_supervisor_watchdog

_start_supervisor_watchdog()
print("watchdog-ready", flush=True)
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    env = _runner_env(tmp_path)
    env["PYTHONPATH"] = str(ROOT)
    log_path = pathlib.Path(env["AGENTSTACK_DASHBOARD_LOG"])
    runner = subprocess.Popen([sys.executable, str(RUNNER), str(child)], env=env)
    child_pid = 0
    try:
        text = _wait_for(log_path, "server | watchdog-ready")
        matches = re.findall(r"dashboard server started child_pid=(\d+)", text)
        assert matches
        child_pid = int(matches[-1])
        os.kill(runner.pid, signal.SIGKILL)
        runner.wait(timeout=5)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"supervised child survived runner SIGKILL: {child_pid}")
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=5)
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    state = pathlib.Path(env["AGENTSTACK_DASHBOARD_RUN_STATE"])
    assert state.exists(), "SIGKILL must leave a marker for the next service-manager restart"
