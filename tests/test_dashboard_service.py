"""Dashboard service supervision, diagnostics, and bounded log coverage."""

from __future__ import annotations

import json
import http.server
import os
import pathlib
import plistlib
import pty
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = ROOT / "dashboard" / "service_runner.py"
PLIST_TEMPLATE = ROOT / "dashboard" / "agentdashboard.plist.template"


class _DashboardVersionHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/api/version":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.server.version_payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_dashboard_version_server():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _DashboardVersionHandler
    )
    server.version_payload = {
        "name": "claude-agent-stack", "version": "test", "api": 1,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


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


def _isolated_installer_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "installer-repo"
    repo.mkdir()
    for directory in ("bin", "dashboard", "scripts"):
        shutil.copytree(ROOT / directory, repo / directory)
    (repo / "hooks").mkdir()
    shutil.copy2(ROOT / "hooks" / "project-context.sh", repo / "hooks")
    shutil.copy2(ROOT / "VERSION", repo / "VERSION")
    return repo


def _write_marker_dashboard(path: pathlib.Path, marker: str) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import http.server
import json
import os

MARKER = %r

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps({"marker": MARKER}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

http.server.ThreadingHTTPServer(
    ("127.0.0.1", int(os.environ["AGENTSTACK_PORT"])), Handler
).serve_forever()
""" % marker,
        encoding="utf-8",
    )


def _wait_for_marker(port: int, marker: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/agents", timeout=0.5
            ) as response:
                payload = json.loads(response.read())
            if payload.get("marker") == marker:
                return
        except Exception as exc:  # The process may still be starting or replacing.
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(
        f"dashboard marker {marker!r} did not appear on port {port}: {last_error}"
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} did not exit")


def _installer_upgrade_env(
    tmp_path: pathlib.Path,
    fake_bin: pathlib.Path,
    port: int,
) -> tuple[dict[str, str], pathlib.Path]:
    home = tmp_path / "home"
    install_dir = home / ".agentstack"
    project = tmp_path / "project"
    project.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        mail_port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_MAIL_STATE_ROOT": str(install_dir / "mail"),
        "AGENTSTACK_MAIL_SERVICE_VENV": str(
            pathlib.Path(sys.executable).parent.parent
        ),
        "AGENTSTACK_MAIL_PACKAGE_SOURCE": str(ROOT / "packages" / "agentstack_mail"),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(port),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
    })
    return env, install_dir


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
    assert plist["EnvironmentVariables"]["AGENTSTACK_SPAWN_DIRS"] == "__SPAWN_DIRS__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_SPAWN_ROOTS"] == "__SPAWN_ROOTS__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_CODEX_CHILD_APPROVAL"] == "__CODEX_CHILD_APPROVAL__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_CODEX_NETWORK"] == "__CODEX_NETWORK__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_CODEX_ADD_DIRS"] == "__CODEX_ADD_DIRS__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_PORTRAITS_DIR"] == "__PORTRAITS_DIR__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_CUSTOM_PORTRAITS"] == "__CUSTOM_PORTRAITS__"
    assert plist["EnvironmentVariables"]["AGENTSTACK_CODEX_MODELS"] == "__CODEX_MODELS__"

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
        # Hermetic: nothing listens on port 1, so the installer plans a fresh
        # provision instead of detecting whatever mail server runs on this host.
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
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
    lines = result.stdout.splitlines()
    dashboard_lines = [
        line for line in lines if "org.agentstack.order-test.agentdashboard" in line
    ]
    enable = next(i for i, line in enumerate(dashboard_lines) if " enable " in line)
    bootout = next(i for i, line in enumerate(dashboard_lines) if " bootout " in line)
    wait = next(i for i, line in enumerate(dashboard_lines) if "wait for launchctl unload" in line)
    bootstrap = next(i for i, line in enumerate(dashboard_lines) if " bootstrap " in line)
    kickstart = next(i for i, line in enumerate(dashboard_lines) if " kickstart " in line)
    assert enable < bootout < wait < bootstrap < kickstart


def test_agentctl_enables_then_waits_for_bootout_before_bootstrap(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "launchctl.log"
    enabled = tmp_path / "enabled"
    pending = tmp_path / "pending-unload"
    count = tmp_path / "print-count"
    for name, body in {
        "uname": "#!/bin/sh\necho Darwin\n",
        "launchctl": """#!/bin/sh
echo "$1" >> "$AGENTSTACK_TEST_LAUNCHCTL_LOG"
case "$1" in
  enable)
    touch "$AGENTSTACK_TEST_ENABLED"
    ;;
  bootout)
    touch "$AGENTSTACK_TEST_PENDING"
    echo 0 > "$AGENTSTACK_TEST_PRINT_COUNT"
    ;;
  print)
    if [ -f "$AGENTSTACK_TEST_PENDING" ]; then
      current=$(cat "$AGENTSTACK_TEST_PRINT_COUNT")
      if [ "$current" -lt 2 ]; then
        echo $((current + 1)) > "$AGENTSTACK_TEST_PRINT_COUNT"
        exit 0
      fi
      rm -f "$AGENTSTACK_TEST_PENDING"
    fi
    exit 1
    ;;
  bootstrap)
    [ -f "$AGENTSTACK_TEST_ENABLED" ] || exit 5
    [ ! -f "$AGENTSTACK_TEST_PENDING" ] || exit 5
    ;;
esac
exit 0
""",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_ENV_FILE": str(tmp_path / "missing-env.sh"),
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.order",
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
            "AGENTSTACK_TEST_LAUNCHCTL_LOG": str(log),
            "AGENTSTACK_TEST_ENABLED": str(enabled),
            "AGENTSTACK_TEST_PENDING": str(pending),
            "AGENTSTACK_TEST_PRINT_COUNT": str(count),
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "dashboard" / "agentctl.sh"), "start"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "started in launchd mode" in result.stdout
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0:2] == ["enable", "bootout"], calls
    bootstrap = calls.index("bootstrap")
    polls = [i for i, call in enumerate(calls) if call == "print"]
    assert len(polls) >= 3 and max(polls) < bootstrap, calls


def test_launchd_in_place_upgrade_replaces_its_own_listener_with_new_code(
    tmp_path,
):
    repo = _isolated_installer_repo(tmp_path)
    _write_marker_dashboard(repo / "dashboard" / "server.py", "new-launchd")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    new_pidfile = tmp_path / "new-launchd.pid"
    for name, body in {
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Darwin\n",
        "uv": "#!/bin/sh\nexit 0\n",
        "launchctl": """#!/bin/sh
case "$*" in
  *agentdashboard*) ;;
  *) [ "$1" = print ] && exit 1; exit 0 ;;
esac
case "$1" in
  print)
    [ -f "$AGENTSTACK_TEST_LOADED_MARKER" ] || exit 1
    printf '{\n    pid = %s\n}\n' "$AGENTSTACK_TEST_OLD_PID"
    ;;
  bootout)
    if kill -0 "$AGENTSTACK_TEST_OLD_PID" 2>/dev/null; then
      kill "$AGENTSTACK_TEST_OLD_PID" 2>/dev/null || true
      attempts=0
      while kill -0 "$AGENTSTACK_TEST_OLD_PID" 2>/dev/null && [ "$attempts" -lt 50 ]; do
        sleep 0.1
        attempts=$((attempts + 1))
      done
    fi
    rm -f "$AGENTSTACK_TEST_LOADED_MARKER"
    ;;
  bootstrap)
    nohup "$AGENTSTACK_PYTHON" \
      "$AGENTSTACK_HOME/dashboard/service_runner.py" >/dev/null 2>&1 &
    echo $! > "$AGENTSTACK_TEST_NEW_PIDFILE"
    ;;
esac
exit 0
""",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env, install_dir = _installer_upgrade_env(tmp_path, fake_bin, port)
    old_server = tmp_path / "old-dashboard.py"
    _write_marker_dashboard(old_server, "old-launchd")
    old_process = subprocess.Popen(
        [sys.executable, str(old_server)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    loaded_marker = tmp_path / "launchd-loaded"
    loaded_marker.touch()
    env.update({
        "AGENTSTACK_TEST_OLD_PID": str(old_process.pid),
        "AGENTSTACK_TEST_NEW_PIDFILE": str(new_pidfile),
        "AGENTSTACK_TEST_LOADED_MARKER": str(loaded_marker),
    })
    try:
        _wait_for_marker(port, "old-launchd")
        result = subprocess.run(
            [
                "bash",
                str(repo / "scripts" / "install.sh"),
                "--dashboard-only",
            ],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        assert old_process.wait(timeout=5) == -signal.SIGTERM
        _wait_for_marker(port, "new-launchd")
        new_pid = int(new_pidfile.read_text(encoding="utf-8").strip())
        assert new_pid != old_process.pid
        assert _pid_is_alive(new_pid)
        assert "managed dashboard owns port" in result.stdout
        assert "replacing it during this install" in result.stdout
        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        assert manifest["services"][0]["kind"] == "launchd"
    finally:
        subprocess.run(
            [str(install_dir / "bin" / "agentstack-mailctl"), "stop"],
            env=env, text=True, capture_output=True, check=False,
        )
        if new_pidfile.exists():
            try:
                os.kill(int(new_pidfile.read_text().strip()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if _pid_is_alive(old_process.pid):
            old_process.terminate()
        old_process.wait(timeout=5)


def test_installer_still_rejects_an_unmanaged_listener(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    listener = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,sys,time; "
                "s=socket.socket(); s.bind(('127.0.0.1',int(sys.argv[1]))); "
                "s.listen(); time.sleep(30)"
            ),
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    else:
        raise AssertionError("foreign listener did not start")

    install_dir = tmp_path / "home" / ".agentstack"
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_PORT": str(port),
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.foreign-listener-test",
        "AGENTSTACK_PROJECT_KEY": str(tmp_path / "project"),
        "AGENTSTACK_TERMINAL": "none",
    })
    try:
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "install.sh"),
                "--assume-yes",
                "--dashboard-only",
                "--install-dir",
                str(install_dir),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 1
        assert f"port {port} is already in use" in result.stderr
        assert "managed dashboard owns port" not in result.stdout
        assert not install_dir.exists()
    finally:
        listener.terminate()
        listener.wait(timeout=5)


def test_supervised_in_place_upgrade_stops_old_pid_and_runs_new_code(tmp_path):
    repo = _isolated_installer_repo(tmp_path)
    _write_marker_dashboard(repo / "dashboard" / "server.py", "supervised-v1")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in {
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\necho Darwin\n",
        "uv": "#!/bin/sh\nexit 0\n",
        "launchctl": """#!/bin/sh
case "$1" in
  print) exit 1 ;;
  bootstrap) exit 125 ;;
esac
exit 0
""",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env, install_dir = _installer_upgrade_env(tmp_path, fake_bin, port)
    command = [
        "bash",
        str(repo / "scripts" / "install.sh"),
        "--dashboard-only",
    ]
    first = subprocess.run(
        command,
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    pidfile = install_dir / "runtime" / "dashboard.pid"
    old_pid = int(pidfile.read_text(encoding="utf-8").strip())
    try:
        assert "falling back to supervised background mode" in first.stderr
        _wait_for_marker(port, "supervised-v1")
        _write_marker_dashboard(repo / "dashboard" / "server.py", "supervised-v2")
        second = subprocess.run(
            command,
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        new_pid = int(pidfile.read_text(encoding="utf-8").strip())
        assert new_pid != old_pid
        _wait_for_pid_exit(old_pid)
        assert _pid_is_alive(new_pid)
        _wait_for_marker(port, "supervised-v2")
        assert "managed dashboard owns port" in second.stdout
        assert "stopping supervised background dashboard" in second.stdout
    finally:
        subprocess.run(
            [str(install_dir / "bin" / "agentstack-mailctl"), "stop"],
            env=env, text=True, capture_output=True, check=False,
        )
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_doctor_rejects_loaded_but_not_running_launchd_job(tmp_path):
    home = tmp_path / "home"
    install_dir = home / ".agentstack"
    runtime = install_dir / "runtime"
    database = tmp_path / "storage.sqlite3"
    project = tmp_path / "project"
    fake_bin = tmp_path / "fake-bin"
    version_server, version_thread = _start_dashboard_version_server()
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
        f"export AGENTSTACK_PORT={version_server.server_port}\n"
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

    try:
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
        assert "dashboard endpoint serving" in result.stdout
        assert "launchd job is loaded but not running" in result.stdout
        assert "actual mode is unmanaged-background" in result.stdout
        assert "ok: dashboard service mode launchd" not in result.stdout

        (fake_bin / "launchctl").write_text(
            "#!/bin/sh\nexit 1\n",
            encoding="utf-8",
        )
        unmanaged = subprocess.run(
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
        assert unmanaged.returncode == 1  # manager drift remains actionable
        assert "dashboard endpoint serving" in unmanaged.stdout
        assert "is not loaded" in unmanaged.stdout
        assert "actual mode is unmanaged-background" in unmanaged.stdout
        assert "dashboard endpoint is not serving" not in unmanaged.stdout

        (fake_bin / "launchctl").write_text(
            "#!/bin/sh\nprintf '%s\\n' '{' '    state = running' '    pid = 4321' '}'\n",
            encoding="utf-8",
        )
        version_server.version_payload = {
            "name": "some-other-service", "version": "test", "api": 1,
        }
        wrong_endpoint = subprocess.run(
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
        assert wrong_endpoint.returncode == 1
        assert "dashboard endpoint is not serving" in wrong_endpoint.stdout
        assert (
            "service manager is running but the dashboard endpoint is unavailable"
            in wrong_endpoint.stdout
        )

        version_server.version_payload = {
            "name": "claude-agent-stack", "version": "test", "api": 1,
        }
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
        )
        # This fixture isolates dashboard supervision and intentionally does
        # not start ORRERY Mail, so doctor remains non-zero for that separate
        # service while still reporting the dashboard state precisely.
        assert running.returncode == 1
        assert "dashboard endpoint serving" in running.stdout
        assert "dashboard service mode launchd" in running.stdout
        assert "running" in running.stdout
    finally:
        version_server.shutdown()
        version_thread.join()


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
            "--project-key", str(tmp_path),
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
    # Hermetic: nothing listens on port 1, so the installer plans a fresh
    # provision instead of detecting whatever mail server runs on this host.
    env["AGENTSTACK_MCP_URL"] = "http://127.0.0.1:1/mcp"
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
    # The contract is that the too-old `python3` sitting first on PATH is
    # rejected and something new enough is chosen instead. Asserting *which*
    # interpreter wins bakes in the maintainer's machine: here /usr/bin/python3
    # is 3.9, so the stub beside it was the only remaining candidate, while a
    # Linux CI runner also has /usr/bin/python3.12 and legitimately picks that.
    # Both obey the rule; only one satisfied the old assertion, and the test
    # had been failing on every CI run because of it.
    chosen = python_lines[0][len("python: "):]
    assert not chosen.startswith(f"{fake_bin / 'python3'} "), (
        f"installer selected the 3.9 stub it was supposed to skip: {chosen}"
    )
    version = re.search(r"\((\d+)\.(\d+)", chosen)
    assert version, f"no version reported in {chosen!r}"
    assert (int(version.group(1)), int(version.group(2))) >= (3, 10), chosen


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
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        mail_port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_HOME": str(install_dir),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_MAIL_STATE_ROOT": str(install_dir / "mail"),
        "AGENTSTACK_MAIL_SERVICE_VENV": str(pathlib.Path(sys.executable).parent.parent),
        "AGENTSTACK_PORT": str(port),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
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
        os.write(master_fd, b"yes\nyes\nyes\nyes\n")
        stdout, stderr = process.communicate(timeout=60)
    finally:
        os.close(master_fd)
    assert process.returncode == 0, stderr
    result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    try:
        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        assert manifest["services"] == [
            {
                "kind": "nohup",
                "pidfile": str(install_dir / "runtime" / "dashboard.pid"),
            },
            {
                "kind": "nohup",
                "pidfile": str(
                    install_dir / "mail-service/runtime/agentstack-mail.pid"
                ),
                "role": "agent-mail",
            },
        ]
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
            [str(install_dir / "bin" / "agentstack-mailctl"), "stop"],
            env=env,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            ["bash", str(install_dir / "dashboard" / "agentctl.sh"), "stop"],
            env=env,
            text=True,
            capture_output=True,
        )


# Foreign and unreadable listener adoption is covered by test_install_mail_probe.py.


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
