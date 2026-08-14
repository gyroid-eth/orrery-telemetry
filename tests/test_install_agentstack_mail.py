"""Opt-in AgentStack Mail installer wiring without changing the upstream default."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.request

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


def _service_env(path: pathlib.Path, state: pathlib.Path, port: int) -> None:
    path.write_text(
        "\n".join(
            (
                "AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1",
                f"AGENTSTACK_MAIL_HTTP_PORT={port}",
                "AGENTSTACK_MAIL_HTTP_PATH=/mcp",
                "AGENTSTACK_MAIL_HTTP_PATH_ALIASES=/mcp,/api",
                f"AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///{state / 'storage.sqlite3'}",
                f"AGENTSTACK_MAIL_STORAGE_ROOT={state / 'archive'}",
                "AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED=true",
                f"AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR={state / 'signals'}",
                "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough",
                "",
            )
        ),
        encoding="utf-8",
    )


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


def test_no_opt_in_keeps_upstream_dry_run(tmp_path):
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

    result = subprocess.run(
        ["/bin/bash", str(INSTALLER), "--dashboard-only", "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "installer will provision upstream agent-mail" in result.stdout
    assert "clone agent-mail upstream" in result.stdout
    assert "AgentStack Mail candidate" not in result.stdout
    assert not (home / ".agentstack").exists()


def test_opt_in_migrates_to_distinct_state_and_serves_health(tmp_path):
    # Keep the venv path itself: resolving its python symlink would select the
    # base interpreter directory, which does not contain the mail entrypoints.
    candidate_bin = pathlib.Path(sys.executable).parent
    server = candidate_bin / "agentstack-mail"
    migrate = candidate_bin / "agentstack-mail-migrate"
    service = candidate_bin / "agentstack-mail-service"
    assert server.is_file() and migrate.is_file() and service.is_file()

    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_linux_bin(tmp_path)
    env = _base_env(tmp_path, home, fake_bin)
    source_state = tmp_path / "legacy-state"
    source_env = tmp_path / "legacy.env"
    source_port = _free_port()
    source_url = f"http://127.0.0.1:{source_port}/mcp"
    _service_env(source_env, source_state, source_port)
    source_process = subprocess.Popen(
        [str(server)],
        cwd=ROOT,
        env={**env, "AGENTSTACK_MAIL_ENV_FILE": str(source_env)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        source_health = _wait_health(source_url)
        assert pathlib.Path(
            source_health["database_url"].removeprefix("sqlite+aiosqlite:///")
        ).resolve() == (source_state / "storage.sqlite3").resolve()
    finally:
        source_process.terminate()
        source_process.wait(timeout=15)

    # The migration contract treats the archive as a versioned working tree,
    # just like the upstream service does.  The native seed server creates the
    # directory but deliberately does not invent repository history for it.
    subprocess.run(["git", "init", "-q", str(source_state / "archive")], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source_state / "archive"),
            "config",
            "user.name",
            "Installer Test",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(source_state / "archive"),
            "config",
            "user.email",
            "installer@example.test",
        ],
        check=True,
    )
    (source_state / "archive" / ".keep").write_text("\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source_state / "archive"), "add", "."], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(source_state / "archive"),
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )

    destination_state = tmp_path / "native-state"
    mail_port = _free_port()
    dashboard_port = _free_port()
    mail_url = f"http://127.0.0.1:{mail_port}/mcp"
    env.update(
        {
            "AGENTSTACK_MAIL_PROVIDER": "agentstack",
            "AGENTSTACK_MAIL_STATE_ROOT": str(destination_state),
            "AGENTSTACK_MAIL_SERVICE_ROOT": str(
                home / ".agentstack" / "mail-service"
            ),
            "AGENTSTACK_MAIL_SERVICE_VENV": str(candidate_bin.parent),
            "AGENTSTACK_MAIL_MIGRATION_SOURCE_DB": str(
                source_state / "storage.sqlite3"
            ),
            "AGENTSTACK_MAIL_MIGRATION_SOURCE_ARCHIVE": str(
                source_state / "archive"
            ),
            "AGENTSTACK_MAIL_MIGRATION_SOURCE_SIGNALS": str(
                source_state / "signals"
            ),
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
        assert '"status": "copied"' in installed.stdout
        assert '"status": "verified"' in installed.stdout
        assert '"status": "reversible"' in installed.stdout
        assert "AgentStack Mail ready at" in installed.stdout

        health = _wait_health(mail_url)
        alias_health = _wait_health(f"http://127.0.0.1:{mail_port}/api/")
        source_db = (source_state / "storage.sqlite3").resolve()
        destination_db = (destination_state / "storage.sqlite3").resolve()
        health_db = pathlib.Path(
            health["database_url"].removeprefix("sqlite+aiosqlite:///")
        ).resolve()
        assert source_db != destination_db
        assert health_db == destination_db
        assert alias_health["status"] == "ok"
        assert alias_health["database_url"] == health["database_url"]
        assert source_db.is_file() and destination_db.is_file()

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
            item.get("role") == "agent-mail" for item in manifest["services"]
        )

        claude_mcp = json.loads(
            (home / ".claude.json").read_text(encoding="utf-8")
        )["mcpServers"]
        assert claude_mcp["mcp-agent-mail"] == {"type": "http", "url": mail_url}
        assert claude_mcp["unrelated"] == {"command": "keep-me"}
        assert "agentstack-mail" not in claude_mcp
        assert (home / ".agentstack" / "hooks" / "spawn_child.sh").is_file()
        dashboard_plist_template = (
            home / ".agentstack" / "dashboard" / "agentdashboard.plist.template"
        ).read_text(encoding="utf-8")
        assert "AGENTSTACK_MAIL_HTTP_BEARER_MODE" in dashboard_plist_template
        assert "__MAIL_HTTP_BEARER_MODE__" in dashboard_plist_template

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
        assert "AgentStack Mail transport uses owner tokens" in combined
        assert f"AgentStack Mail health serving {destination_db}" in combined
    finally:
        _stop_mail(home, destination_state, mail_port)
        stop_dashboard(home, label_prefix="")
