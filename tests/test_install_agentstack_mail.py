"""Default ORRERY Mail installer wiring."""

from __future__ import annotations

import json
import http.server
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import stop_dashboard


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_command(directory: pathlib.Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(body, encoding="utf-8")
    command.chmod(0o755)


class _ProjectResourceServer:
    def __init__(self, slug: str, human_key: str) -> None:
        self.requests: list[dict] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                owner.requests.append(request)
                uri = request.get("params", {}).get("uri", "")
                descriptor = {
                    "id": 1,
                    "slug": slug,
                    "human_key": human_key,
                    "agents": [],
                }
                response = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "application/json",
                                "text": json.dumps(descriptor),
                            }
                        ]
                    },
                }
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/mcp"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


def _fake_linux_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_command(fake_bin, "uname", "#!/bin/sh\nprintf '%s\\n' Linux\n")
    _write_command(fake_bin, "systemctl", "#!/bin/sh\nexit 1\n")
    return fake_bin


def _health(url: str) -> dict:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "agentstack-mail-installer-test",
            "method": "tools/call",
            "params": {"name": "health_check", "arguments": {}},
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read().decode("utf-8", errors="replace")
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    return json.loads(raw)["result"]["structuredContent"]


def _wait_health(url: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _health(url)
        except Exception as exc:  # noqa: BLE001 - readiness retry boundary
            last_error = exc
            time.sleep(0.2)
    raise AssertionError(f"mail health did not become ready: {last_error}")


def _stop_mail(home: pathlib.Path, state_root: pathlib.Path, port: int) -> None:
    """Stop only the isolated service proven by its pidfile/open state path."""

    pidfile = home / ".agentstack" / "mail-service" / "runtime" / "agentstack-mail.pid"
    try:
        supervisor = int(pidfile.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        supervisor = 0
    if supervisor:
        try:
            os.kill(supervisor, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.2)

    # A killed supervisor can leave the deliberately start_new_session child.
    # Resolve the exact listener and prove its open files name this test state
    # before sending a signal; never sweep by executable name.
    lsof = pathlib.Path("/usr/sbin/lsof")
    if not lsof.is_file():
        raise AssertionError(f"isolated mail listener remained on port {port}")
    listeners = subprocess.run(
        [str(lsof), "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.split()
    assert listeners, f"port {port} stayed open without a resolvable listener"
    for raw_pid in listeners:
        pid = int(raw_pid)
        open_files = subprocess.run(
            [str(lsof), "-a", "-p", str(pid), "-Fn"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        assert str(state_root) in open_files
        os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.2)
    raise AssertionError(f"isolated mail listener did not stop on port {port}")


def _base_env(
    tmp_path: pathlib.Path, home: pathlib.Path, fake_bin: pathlib.Path
) -> dict[str, str]:
    project = tmp_path / "project"
    project.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_PROJECT_KEY": str(project),
            "AGENTSTACK_TERMINAL": "none",
            "AGENTSTACK_LABEL_PREFIX": "org.agentstack.test.native-mail",
            "AGENTSTACK_PYTHON": sys.executable,
        }
    )
    return env


def _provider_dry_run(
    tmp_path: pathlib.Path, provider: str | None
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_linux_bin(tmp_path)
    env = _base_env(tmp_path, home, fake_bin)
    mail_port = _free_port()
    env.update(
        {
            "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
            "AGENTSTACK_PORT": str(_free_port()),
        }
    )
    if provider is not None:
        env["AGENTSTACK_MAIL_PROVIDER"] = provider

    result = subprocess.run(
        ["/bin/bash", str(INSTALLER), "--dashboard-only", "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, home


def test_default_uses_agentstack_dry_run(tmp_path):
    result, home = _provider_dry_run(tmp_path, None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "installer will provision ORRERY Mail" in result.stdout
    assert "create immutable ORRERY Mail candidate venv" in result.stdout
    assert "clone ORRERY Mail upstream" not in result.stdout
    assert not (home / ".agentstack").exists()


def test_obsolete_provider_env_cannot_change_the_native_dry_run(tmp_path):
    result, home = _provider_dry_run(tmp_path, "agentstack")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "installer will provision ORRERY Mail" in result.stdout
    assert "create immutable ORRERY Mail candidate venv" in result.stdout
    assert "clone ORRERY Mail upstream" not in result.stdout
    assert not (home / ".agentstack").exists()


def test_obsolete_upstream_value_no_longer_selects_a_clone_path(tmp_path):
    result, home = _provider_dry_run(tmp_path, "upstream")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "installer will provision ORRERY Mail" in result.stdout
    assert "create immutable ORRERY Mail candidate venv" in result.stdout
    assert "clone ORRERY Mail upstream" not in result.stdout
    assert not (home / ".agentstack").exists()


def test_automatic_migration_inputs_are_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_linux_bin(tmp_path)
    env = _base_env(tmp_path, home, fake_bin)
    env["AGENTSTACK_MAIL_MIGRATION_SOURCE_DB"] = "/legacy/storage.sqlite3"

    result = subprocess.run(
        ["/bin/bash", str(INSTALLER), "--dashboard-only", "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "automatic mail migration is not part of install.sh" in result.stderr
    assert "agentstack-mail-migrate copy and verify manually" in result.stderr


def test_default_provisions_isolated_state_and_serves_health(tmp_path):
    # Keep the venv path itself: resolving its python symlink would select the
    # base interpreter directory, which does not contain the mail entrypoints.
    candidate_bin = pathlib.Path(sys.executable).parent
    migrate = candidate_bin / "agentstack-mail-migrate"
    service = candidate_bin / "agentstack-mail-service"
    assert migrate.is_file() and service.is_file()

    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_linux_bin(tmp_path)
    env = _base_env(tmp_path, home, fake_bin)
    destination_state = tmp_path / "native-state"
    mail_port = _free_port()
    dashboard_port = _free_port()
    mail_url = f"http://127.0.0.1:{mail_port}/mcp"
    env.update(
        {
            "AGENTSTACK_MAIL_STATE_ROOT": str(destination_state),
            "AGENTSTACK_MAIL_SERVICE_ROOT": str(
                home / ".agentstack" / "mail-service"
            ),
            "AGENTSTACK_MAIL_SERVICE_VENV": str(candidate_bin.parent),
            "AGENTSTACK_MCP_URL": mail_url,
            "AGENTSTACK_PORT": str(dashboard_port),
        }
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcp-agent-mail": {
                        "type": "http",
                        "url": "http://127.0.0.1:9/mcp",
                        "headers": {"Authorization": "Bearer legacy-test-only"},
                    },
                    "unrelated": {"command": "keep-me"},
                }
            }
        ),
        encoding="utf-8",
    )

    try:
        installed = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--assume-yes"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        assert installed.returncode == 0, installed.stdout + installed.stderr
        assert "ORRERY Mail ready at" in installed.stdout

        health = _wait_health(mail_url)
        alias_health = _wait_health(f"http://127.0.0.1:{mail_port}/api/")
        destination_db = (destination_state / "storage.sqlite3").resolve()
        health_db = pathlib.Path(
            health["database_url"].removeprefix("sqlite+aiosqlite:///")
        ).resolve()
        assert health_db == destination_db
        assert alias_health["status"] == "ok"
        assert alias_health["database_url"] == health["database_url"]
        assert destination_db.is_file()

        service_env = next(
            (home / ".agentstack" / "mail-service" / "renders").glob(
                "*/service.env"
            )
        ).read_text(encoding="utf-8")
        assert "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough" in service_env
        assert "HTTP_BEARER_TOKEN" not in service_env

        manifest = json.loads(
            (home / ".agentstack" / "install-state.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["agent_mail"]["provider"] == "agentstack"
        assert manifest["env"]["AGENTSTACK_MAIL_DB"] == str(destination_db)
        assert manifest["env"]["AGENTSTACK_MAIL_HTTP_BEARER_MODE"] == "disabled"
        assert any(
            item.get("role") == "ORRERY Mail" for item in manifest["services"]
        )

        claude_mcp = json.loads(
            (home / ".claude.json").read_text(encoding="utf-8")
        )["mcpServers"]
        assert claude_mcp["orrery-mail"] == {"type": "http", "url": mail_url}
        assert claude_mcp["mcp-agent-mail"]["url"] == "http://127.0.0.1:9/mcp"
        assert claude_mcp["unrelated"] == {"command": "keep-me"}
        assert "agentstack-mail" not in claude_mcp
        installed_spawn = (
            home / ".agentstack" / "hooks" / "spawn_child.sh"
        ).read_text(encoding="utf-8")
        assert 'claimed = ["orrery-mail"]' in installed_spawn
        assert "AGENTSTACK_MCP_URL=mcp_url" in installed_spawn
        dashboard_plist_template = (
            home / ".agentstack" / "dashboard" / "agentdashboard.plist.template"
        ).read_text(encoding="utf-8")
        assert "AGENTSTACK_MAIL_HTTP_BEARER_MODE" in dashboard_plist_template
        assert "__MAIL_HTTP_BEARER_MODE__" in dashboard_plist_template

        mailctl = home / ".agentstack" / "bin" / "agentstack-mailctl"
        assert mailctl.is_file() and os.access(mailctl, os.X_OK)

        def run_mailctl(action: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(mailctl), action],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )

        status = run_mailctl("status")
        assert status.returncode == 0, status.stdout + status.stderr
        pidfile = (
            home
            / ".agentstack"
            / "mail-service"
            / "runtime"
            / "agentstack-mail.pid"
        )
        first_pid = int(pidfile.read_text(encoding="utf-8").split()[0])

        duplicate = run_mailctl("start")
        assert duplicate.returncode == 0, duplicate.stdout + duplicate.stderr
        assert "already running" in duplicate.stdout
        assert int(pidfile.read_text(encoding="utf-8").split()[0]) == first_pid

        # Kill only the listener proven to have this isolated state open. The
        # rendered runner must keep its PID and restore health after its
        # five-second crash-recovery delay.
        lsof = pathlib.Path("/usr/sbin/lsof")
        if lsof.is_file():
            listeners = subprocess.run(
                [str(lsof), "-nP", f"-iTCP:{mail_port}", "-sTCP:LISTEN", "-t"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.split()
            assert len(listeners) == 1
            listener = int(listeners[0])
            open_files = subprocess.run(
                [str(lsof), "-a", "-p", str(listener), "-Fn"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            assert str(destination_state) in open_files
            os.kill(listener, signal.SIGKILL)
            recovered = _wait_health(mail_url, timeout=20)
            assert recovered["database_url"] == health["database_url"]
            assert int(pidfile.read_text(encoding="utf-8").split()[0]) == first_pid

        restarted = run_mailctl("restart")
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        assert "ORRERY Mail stopped" in restarted.stdout
        assert "ORRERY Mail started" in restarted.stdout
        _wait_health(mail_url)
        assert int(pidfile.read_text(encoding="utf-8").split()[0]) != first_pid

        stopped = run_mailctl("stop")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        stopped_status = run_mailctl("status")
        assert stopped_status.returncode == 3
        assert "ORRERY Mail stopped" in stopped_status.stdout

        started = run_mailctl("start")
        assert started.returncode == 0, started.stdout + started.stderr
        _wait_health(mail_url)

        doctor = subprocess.run(
            [
                "/bin/bash",
                str(home / ".agentstack" / "bin" / "agentstack-doctor"),
                "--install-dir",
                str(home / ".agentstack"),
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        combined = doctor.stdout + doctor.stderr
        assert "ORRERY Mail transport uses owner tokens" in combined
        assert f"ORRERY Mail health serving {destination_db}" in combined
    finally:
        _stop_mail(home, destination_state, mail_port)
        stop_dashboard(home, label_prefix="")


def test_bundled_watcher_reads_agentstack_per_message_signal(tmp_path):
    """Exercise the producer-shaped per-message layout through the real watcher."""

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"
    wrong_python_log = tmp_path / "wrong-python.log"
    _write_command(
        fake_bin,
        "python3",
        "#!/bin/sh\nprintf '%s\\n' called > \"$WRONG_PYTHON_LOG\"\nexit 97\n",
    )
    _write_command(
        fake_bin,
        "tmux",
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) exit 0 ;;
  show-environment) printf '%s\n' "-${4:-UNKNOWN}"; exit 0 ;;
  capture-pane) printf '%s\n' 'Claude Code' ;;
  send-keys) exit 0 ;;
  *) exit 1 ;;
esac
""",
    )
    signals = tmp_path / "signals"
    runtime = tmp_path / "runtime"
    child_state = runtime / "child-agents" / "BreezyMaxwell.json"
    child_state.parent.mkdir(parents=True)
    child_state.write_text(
        json.dumps(
            {
                "agent_name": "BreezyMaxwell",
                "project_key": "/isolated/project",
            }
        ),
        encoding="utf-8",
    )
    lock = tmp_path / "watcher.lock"
    signal_file = (
        signals
        / "projects"
        / "isolated-project"
        / "agents"
        / "BreezyMaxwell"
        / "42.signal"
    )
    signal_file.parent.mkdir(parents=True)
    signal_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-15T00:00:00+00:00",
                "project": "isolated-project",
                "agent": "BreezyMaxwell",
                "message": {
                    "id": 42,
                    "from": "ProOpus",
                    "subject": "per-message verification",
                    "importance": "high",
                },
            }
        ),
        encoding="utf-8",
    )
    project_resource = _ProjectResourceServer(
        "isolated-project", "/isolated/project"
    )
    project_resource.start()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_TMUX_LOG": str(tmux_log),
            "WRONG_PYTHON_LOG": str(wrong_python_log),
            "AGENTSTACK_MAIL_HOME": str(tmp_path / "mail-home"),
            "AGENTSTACK_SIGNALS_DIR": str(signals),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(lock),
            "AGENTSTACK_MCP_URL": project_resource.url,
            "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "TMUX_TIMEOUT": "2",
        }
    )
    watcher = subprocess.Popen(
        ["/bin/bash", str(ROOT / "hooks" / "watch_agent_mail_signals.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        state_file = runtime / "notify-state.json"
        deadline = time.monotonic() + 10
        state: dict[str, dict[str, object]] = {}
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            successful = [
                entry for entry in state.values()
                if isinstance(entry, dict) and entry.get("last_result") == "success"
            ]
            if len(successful) == 1 and not signal_file.exists():
                break
            time.sleep(0.1)
        successful = [
            entry for entry in state.values()
            if isinstance(entry, dict) and entry.get("last_result") == "success"
        ]
        assert len(successful) == 1, state
        assert not signal_file.exists()
        calls = tmux_log.read_text(encoding="utf-8")
        assert "has-session -t =BreezyMaxwell" in calls
        assert "show-environment" in calls
        assert "capture-pane -t =BreezyMaxwell" in calls
        assert "send-keys -t =BreezyMaxwell" in calls
        assert "message from ProOpus [high]: per-message verification" in calls
        assert any(
            request.get("method") == "resources/read"
            and request.get("params", {}).get("uri")
            == "resource://project/isolated-project"
            for request in project_resource.requests
        )
        assert not wrong_python_log.exists()
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)
        project_resource.close()


def test_watcher_refuses_same_name_tmux_session_from_another_project(tmp_path):
    """A project-A dirty bit must never type into project-B's same-named pane."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"
    _write_command(
        fake_bin,
        "tmux",
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) exit 0 ;;
  show-environment)
    case "$*" in
      *AGENTSTACK_PROJECT_KEY*) printf '%s\n' 'AGENTSTACK_PROJECT_KEY=/isolated/project-b' ;;
      *PROJECT_KEY*) printf '%s\n' 'PROJECT_KEY=/isolated/project-b' ;;
    esac
    exit 0 ;;
  capture-pane) printf '%s\n' 'Claude Code' ;;
  send-keys) exit 0 ;;
  *) exit 1 ;;
esac
""",
    )
    signals = tmp_path / "signals"
    runtime = tmp_path / "runtime"
    signal_file = (
        signals / "projects" / "project-a-slug" / "agents"
        / "SharedCurie" / "73.signal"
    )
    signal_file.parent.mkdir(parents=True)
    signal_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-12T00:00:00+00:00",
                "project": "project-a-slug",
                "project_key": "/isolated/project-a",
                "agent": "SharedCurie",
                "message": {
                    "id": 73,
                    "from": "ParentCurie",
                    "subject": "must stay in project A",
                    "importance": "high",
                },
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("AGENTSTACK_") or name in {"PROJECT_KEY", "TMUX", "TMUX_PANE"}:
            env.pop(name, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_TMUX_LOG": str(tmux_log),
            "AGENTSTACK_MAIL_HOME": str(tmp_path / "mail-home"),
            "AGENTSTACK_SIGNALS_DIR": str(signals),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(tmp_path / "watcher.lock"),
            "TMUX_TIMEOUT": "2",
        }
    )
    watcher = subprocess.Popen(
        ["/bin/bash", str(ROOT / "hooks" / "watch_agent_mail_signals.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        state_file = runtime / "notify-state.json"
        deadline = time.monotonic() + 10
        state: dict[str, dict[str, object]] = {}
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if state:
                break
            time.sleep(0.1)
        results = [
            str(entry.get("last_result", ""))
            for entry in state.values()
            if isinstance(entry, dict)
        ]
        assert any("project" in value and "mismatch" in value for value in results), state
        assert signal_file.exists(), "a rejected delivery remains fetchable"
        calls = tmux_log.read_text(encoding="utf-8")
        assert "show-environment" in calls
        assert "capture-pane" not in calls
        assert "send-keys" not in calls
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)


def test_watcher_keeps_legacy_signal_pending_without_durable_session_project(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    tmux_log = tmp_path / "tmux.log"
    _write_command(
        fake_bin,
        "tmux",
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) exit 0 ;;
  show-environment) exit 1 ;;
  capture-pane) printf '%s\n' 'Claude Code' ;;
  send-keys) exit 0 ;;
  *) exit 1 ;;
esac
""",
    )
    signals = tmp_path / "signals"
    runtime = tmp_path / "runtime"
    signal_file = (
        signals / "projects" / "legacy-project-a" / "agents"
        / "AmbiguousCurie" / "74.signal"
    )
    signal_file.parent.mkdir(parents=True)
    signal_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-12T00:00:00+00:00",
                "project": "legacy-project-a",
                "agent": "AmbiguousCurie",
                "message": {
                    "id": 74,
                    "from": "ParentCurie",
                    "subject": "ambiguous legacy target",
                    "importance": "high",
                },
            }
        ),
        encoding="utf-8",
    )
    project_resource = _ProjectResourceServer(
        "legacy-project-a", "/isolated/project-a"
    )
    project_resource.start()
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("AGENTSTACK_") or name in {"PROJECT_KEY", "TMUX", "TMUX_PANE"}:
            env.pop(name, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_TMUX_LOG": str(tmux_log),
            "AGENTSTACK_MAIL_HOME": str(tmp_path / "mail-home"),
            "AGENTSTACK_SIGNALS_DIR": str(signals),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(tmp_path / "watcher.lock"),
            "AGENTSTACK_MCP_URL": project_resource.url,
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "TMUX_TIMEOUT": "2",
        }
    )
    watcher = subprocess.Popen(
        ["/bin/bash", str(ROOT / "hooks" / "watch_agent_mail_signals.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        state_file = runtime / "notify-state.json"
        deadline = time.monotonic() + 10
        state: dict[str, dict[str, object]] = {}
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if state:
                break
            time.sleep(0.1)
        results = [
            str(entry.get("last_result", ""))
            for entry in state.values()
            if isinstance(entry, dict)
        ]
        assert any(
            "project" in value and (
                "unknown" in value or "unresolved" in value or "mismatch" in value
            )
            for value in results
        ), state
        assert signal_file.exists()
        calls = tmux_log.read_text(encoding="utf-8")
        assert "show-environment" in calls
        assert "capture-pane" not in calls
        assert "send-keys" not in calls
        assert any(
            request.get("method") == "resources/read"
            for request in project_resource.requests
        )
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)
        project_resource.close()


@pytest.mark.parametrize(
    ("session_location", "expect_delivery", "use_project_alias"),
    [("other", False, False), ("linked", True, False), ("main", True, True)],
    ids=[
        "stale-key-cross-repository",
        "same-repository-linked-worktree",
        "filesystem-key-alias",
    ],
)
def test_watcher_corroborates_matching_legacy_key_with_live_repository(
    tmp_path, session_location, expect_delivery, use_project_alias,
):
    def git(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    main = tmp_path / "watcher-repo-main"
    other = tmp_path / "watcher-repo-other"
    linked = tmp_path / "watcher-repo-linked"
    main.mkdir()
    other.mkdir()
    for repository in (main, other):
        git(repository, "init", "-q")
        git(
            repository,
            "-c", "user.name=Issue16 Test",
            "-c", "user.email=issue16@example.invalid",
            "commit", "--allow-empty", "-q", "-m", "initial",
        )
    git(main, "worktree", "add", "-q", "-b", "watcher-linked", str(linked))
    alias = tmp_path / "watcher-repo-alias"
    alias.symlink_to(main, target_is_directory=True)
    session_cwd = {
        "other": other,
        "linked": linked,
        "main": main,
    }[session_location]
    session_project_key = str(alias) if use_project_alias else str(main.resolve())

    fake_bin = tmp_path / f"fake-bin-{session_location}"
    fake_bin.mkdir()
    tmux_log = tmp_path / f"tmux-{session_location}.log"
    _write_command(
        fake_bin,
        "tmux",
        """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_TMUX_LOG"
case "$1" in
  has-session) exit 0 ;;
  show-environment)
    case "${4:-}" in
      AGENTSTACK_PROJECT_KEY) printf '%s\n' "AGENTSTACK_PROJECT_KEY=$ISSUE16_PROJECT_KEY" ;;
      PROJECT_KEY) printf '%s\n' "PROJECT_KEY=$ISSUE16_PROJECT_KEY" ;;
      *) printf '%s\n' "-${4:-UNKNOWN}" ;;
    esac
    exit 0 ;;
  display-message) printf '%s\n' "$ISSUE16_SESSION_CWD" ;;
  capture-pane) printf '%s\n' 'Claude Code' ;;
  send-keys) exit 0 ;;
  *) exit 1 ;;
esac
""",
    )
    signals = tmp_path / f"signals-{session_location}"
    runtime = tmp_path / f"runtime-{session_location}"
    if use_project_alias:
        (runtime / "child-agents").mkdir(parents=True)
        (runtime / "name-bindings").mkdir(parents=True)
        (runtime / "agent_token_SharedCurie.project").write_text(
            f"{alias}\n", encoding="utf-8"
        )
        (runtime / "child-agents" / "SharedCurie.json").write_text(
            json.dumps(
                {"agent_name": "SharedCurie", "project_key": str(main.resolve())}
            ),
            encoding="utf-8",
        )
        (runtime / "name-bindings" / "sharedcurie.json").write_text(
            json.dumps(
                {"agent_name": "SharedCurie", "project_key": str(alias)}
            ),
            encoding="utf-8",
        )
    elif session_location == "other":
        # Truly distinct durable evidence remains ambiguous/fail-closed even
        # after filesystem aliases are normalized before uniqueness.
        (runtime / "child-agents").mkdir(parents=True)
        (runtime / "agent_token_SharedCurie.project").write_text(
            f"{main.resolve()}\n", encoding="utf-8"
        )
        (runtime / "child-agents" / "SharedCurie.json").write_text(
            json.dumps(
                {"agent_name": "SharedCurie", "project_key": str(other.resolve())}
            ),
            encoding="utf-8",
        )
    message_id = 81 if expect_delivery else 80
    signal_file = (
        signals / "projects" / "project-a" / "agents"
        / "SharedCurie" / f"{message_id}.signal"
    )
    signal_file.parent.mkdir(parents=True)
    signal_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-12T00:00:00+00:00",
                "project": "project-a",
                "project_key": str(main.resolve()),
                "agent": "SharedCurie",
                "message": {
                    "id": message_id,
                    "from": "ParentCurie",
                    "subject": "repository corroboration",
                    "importance": "high",
                },
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("AGENTSTACK_") or name in {
            "PROJECT_KEY", "TMUX", "TMUX_PANE", "GIT_DIR", "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
        }:
            env.pop(name, None)
    env.update(
        {
            "HOME": str(tmp_path / f"home-{session_location}"),
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_TMUX_LOG": str(tmux_log),
            "ISSUE16_PROJECT_KEY": session_project_key,
            "ISSUE16_SESSION_CWD": str(session_cwd.resolve()),
            "AGENTSTACK_MAIL_HOME": str(tmp_path / f"mail-{session_location}"),
            "AGENTSTACK_SIGNALS_DIR": str(signals),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(
                tmp_path / f"watcher-{session_location}.lock"
            ),
            "TMUX_TIMEOUT": "2",
        }
    )
    watcher = subprocess.Popen(
        ["/bin/bash", str(ROOT / "hooks" / "watch_agent_mail_signals.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        state_file = runtime / "notify-state.json"
        deadline = time.monotonic() + 10
        state = {}
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            results = [
                str(entry.get("last_result", ""))
                for entry in state.values()
                if isinstance(entry, dict)
            ]
            if (expect_delivery and "success" in results and not signal_file.exists()) or (
                not expect_delivery and "project_mismatch" in results
            ):
                break
            time.sleep(0.1)
        calls = tmux_log.read_text(encoding="utf-8")
        assert "display-message -t =SharedCurie" in calls
        if expect_delivery:
            assert any(
                entry.get("last_result") == "success"
                for entry in state.values()
                if isinstance(entry, dict)
            ), state
            assert not signal_file.exists()
            assert "capture-pane -t =SharedCurie" in calls
            assert "send-keys -t =SharedCurie" in calls
        else:
            assert any(
                entry.get("last_result") == "project_mismatch"
                for entry in state.values()
                if isinstance(entry, dict)
            ), state
            assert signal_file.exists()
            assert "capture-pane" not in calls
            assert "send-keys" not in calls
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)
