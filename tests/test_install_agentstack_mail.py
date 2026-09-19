"""Default ORRERY Mail installer wiring."""

from __future__ import annotations

import json
import os
import pathlib
import pty
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX, stop_dashboard


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


def _generated_env(home: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (home / ".agentstack" / "env.sh").read_text(
        encoding="utf-8"
    ).splitlines():
        if not line.startswith("export ") or "=" not in line:
            continue
        key, raw = line.removeprefix("export ").split("=", 1)
        parsed = shlex.split(raw, comments=True, posix=True)
        assert len(parsed) == 1, (key, raw)
        values[key] = parsed[0]
    return values


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

        service_env_path = next(
            (home / ".agentstack" / "mail-service" / "renders").glob(
                "*/service.env"
            )
        )
        service_env = service_env_path.read_text(encoding="utf-8")
        assert "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough" in service_env
        management_socket_line = next(
            line for line in service_env.splitlines()
            if line.startswith("AGENTSTACK_MAIL_MANAGEMENT_SOCKET=")
        )
        management_socket = management_socket_line.split("=", 1)[1]
        assert management_socket.startswith(f"/tmp/orrery-mail-{os.getuid()}/")
        assert "HTTP_BEARER_TOKEN" not in service_env
        generated_env = _generated_env(home)
        assert generated_env["AGENTSTACK_MAIL_ENROLL_BIN"] == str(
            candidate_bin.parent / "bin" / "agentstack-enroll"
        )
        deployment_path = service_env_path.with_name("deployment.json")
        deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
        assert deployment == {
            "kind": "orrery-mail-deployment-v1",
            "source_id": deployment["source_id"],
            "candidate_venv": str(candidate_bin.parent),
            "enroll_bin": str(candidate_bin.parent / "bin" / "agentstack-enroll"),
            "service_env": str(service_env_path),
            "state_root": str(destination_state.resolve()),
            "mcp_url": mail_url,
        }
        assert deployment_path.stat().st_mode & 0o777 == 0o600

        connection_profile_path = home / ".agentstack" / "connections" / "local.json"
        connection_profile = json.loads(connection_profile_path.read_text(encoding="utf-8"))
        assert connection_profile == {
            "kind": "orrery-mail-connection-v1",
            "management_socket": management_socket,
            "runtime_dir": str(home / ".agentstack" / "runtime"),
            "mcp_url": mail_url,
            "mail_env": str(service_env_path),
            "http_bearer_mode": "disabled",
        }
        assert connection_profile_path.stat().st_mode & 0o777 == 0o600

        pinned_instance = "11111111-2222-4333-8444-555555555555"
        connection_profile["expected_server_instance_id"] = pinned_instance
        connection_profile_path.write_text(
            json.dumps(connection_profile) + "\n", encoding="utf-8"
        )
        connection_profile_path.chmod(0o600)
        reinstalled = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--assume-yes"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        assert reinstalled.returncode == 0, reinstalled.stdout + reinstalled.stderr
        assert (
            json.loads(connection_profile_path.read_text(encoding="utf-8"))[
                "expected_server_instance_id"
            ]
            == pinned_instance
        )

        manifest = json.loads(
            (home / ".agentstack" / "install-state.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["agent_mail"]["provider"] == "agentstack"
        assert manifest["agent_mail"]["candidate_venv"] == str(candidate_bin.parent)
        assert manifest["agent_mail"]["service_env"] == str(service_env_path)
        assert manifest["agent_mail"]["deployment_metadata"] == str(deployment_path)
        assert manifest["agent_mail"]["enroll_bin"] == generated_env[
            "AGENTSTACK_MAIL_ENROLL_BIN"
        ]
        assert manifest["env"]["AGENTSTACK_MAIL_DB"] == str(destination_db)
        assert manifest["env"]["AGENTSTACK_MAIL_HTTP_BEARER_MODE"] == "disabled"
        assert manifest["env"]["AGENTSTACK_MAIL_ENROLL_BIN"] == generated_env[
            "AGENTSTACK_MAIL_ENROLL_BIN"
        ]
        assert manifest["env"]["AGENTSTACK_PERSISTENT_PROFILES_DIR"] == str(
            home / ".agentstack" / "profiles"
        )
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
        assert "AGENTSTACK_PERSISTENT_PROFILES_DIR" in dashboard_plist_template
        assert "__PERSISTENT_PROFILES_DIR__" in dashboard_plist_template

        mailctl = home / ".agentstack" / "bin" / "agentstack-mailctl"
        assert mailctl.is_file() and os.access(mailctl, os.X_OK)
        enroll = home / ".agentstack" / "bin" / "agentstack-enroll"
        assert enroll.is_file() and os.access(enroll, os.X_OK)
        enroll_help = subprocess.run(
            [str(enroll), "--help"],
            env={
                **env,
                "AGENTSTACK_HOME": str(home / ".agentstack"),
                "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert enroll_help.returncode == 0, enroll_help.stdout + enroll_help.stderr
        assert "inspect -> claim" in enroll_help.stdout
        persistent = home / ".agentstack" / "bin" / "agentstack-persistent"
        persistent_implementation = persistent.with_suffix(".py")
        persistent_deliver = (
            home / ".agentstack" / "bin" / "agentstack-persistent-deliver"
        )
        assert persistent.is_file() and os.access(persistent, os.X_OK)
        assert persistent.read_text(encoding="utf-8").startswith("#!/bin/bash\n")
        assert persistent_implementation.is_file()
        assert "import tomllib" in persistent_implementation.read_text(
            encoding="utf-8"
        )
        assert persistent_deliver.is_file() and os.access(persistent_deliver, os.X_OK)
        assert (home / ".agentstack" / "profiles").stat().st_mode & 0o777 == 0o700
        ambient_bin = tmp_path / "ambient-old-python"
        ambient_bin.mkdir()
        ambient_marker = tmp_path / "ambient-python-ran"
        _write_command(
            ambient_bin,
            "python3",
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(ambient_marker))}\n"
            "exit 91\n",
        )
        persistent_env = {
            **env,
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
            "PATH": f"{ambient_bin}:/usr/bin:/bin",
        }
        persistent_env.pop("AGENTSTACK_PYTHON", None)
        persistent_help = subprocess.run(
            [str(persistent), "--help"],
            env=persistent_env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert persistent_help.returncode == 0
        assert "agentstack-enroll inspect" in persistent_help.stdout
        assert "agentstack-persistent run --profile" in persistent_help.stdout
        assert not ambient_marker.exists()

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


def test_reinstall_adopts_the_running_candidate_enroll_cli_and_wrapper_inspects(
    tmp_path,
):
    """A source update must not publish a CLI from an unbuilt candidate.

    The first install starts a real isolated Mail server from an old managed
    candidate. The second install has a different source id and no candidate at
    all. It must preserve the listener, recover the old candidate through the
    immutable render association, publish that candidate's enroll CLI, and let
    the installed persistent wrapper use it without an operator override.
    """

    actual_venv = pathlib.Path(sys.executable).parent.parent.resolve()
    actual_bin = actual_venv / "bin"
    for name in (
        "agentstack-mail",
        "agentstack-mail-service",
        "agentstack-mail-migrate",
    ):
        assert (actual_bin / name).is_file(), name

    home = tmp_path / "home"
    home.mkdir()
    fake_bin = _fake_linux_bin(tmp_path)
    env = _base_env(tmp_path, home, fake_bin)
    service_root = home / ".agentstack" / "mail-service"
    old_source = "fixture-old-source"
    new_source = "fixture-new-source"
    old_venv = service_root / "candidates" / old_source / "venv"
    old_bin = old_venv / "bin"
    old_bin.mkdir(parents=True)
    for name in (
        "agentstack-mail",
        "agentstack-mail-service",
        "agentstack-mail-migrate",
    ):
        (old_bin / name).symlink_to(actual_bin / name)

    project = pathlib.Path(env["AGENTSTACK_PROJECT_KEY"])
    inspect_payload = {
        "ok": True,
        "agent_id": 41,
        "project_key": str(project),
        "name": "PersistentFixture",
        "credential_state": "server-token",
        "connection_pinned": True,
        "local_credential_state": "present",
        "server_instance_id": "mail-instance-fixture",
        "credential_generation": 3,
        "credential_fingerprint": "0123456789abcdef",
    }
    _write_command(
        old_bin,
        "agentstack-enroll",
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if len(sys.argv) < 2 or sys.argv[1] != 'inspect':\n"
        "    raise SystemExit(2)\n"
        f"print(json.dumps({inspect_payload!r}))\n",
    )

    state_root = tmp_path / "mail-state"
    mail_port = _free_port()
    dashboard_port = _free_port()
    mail_url = f"http://127.0.0.1:{mail_port}/mcp"
    env.update(
        {
            "AGENTSTACK_MAIL_STATE_ROOT": str(state_root),
            "AGENTSTACK_MAIL_SERVICE_ROOT": str(service_root),
            "AGENTSTACK_MAIL_CANDIDATE_ID": old_source,
            "AGENTSTACK_MCP_URL": mail_url,
            "AGENTSTACK_PORT": str(dashboard_port),
            "AGENTSTACK_LABEL_PREFIX": (
                f"{TEST_LABEL_PREFIX}.native-mail-reuse-{mail_port}"
            ),
        }
    )

    try:
        first = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--assume-yes"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        assert first.returncode == 0, first.stdout + first.stderr
        _wait_health(mail_url)
        pidfile = service_root / "runtime" / "agentstack-mail.pid"
        first_pid = int(pidfile.read_text(encoding="utf-8").split()[0])

        second_env = dict(env)
        second_env["AGENTSTACK_MAIL_CANDIDATE_ID"] = new_source
        second = subprocess.run(
            ["/bin/bash", str(INSTALLER), "--assume-yes"],
            cwd=ROOT,
            env=second_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        assert "adopted the running ORRERY Mail deployment" in second.stdout
        assert int(pidfile.read_text(encoding="utf-8").split()[0]) == first_pid
        assert not (service_root / "candidates" / new_source).exists()

        generated = _generated_env(home)
        assert generated["AGENTSTACK_MAIL_ENROLL_BIN"] == str(
            old_bin / "agentstack-enroll"
        )
        assert pathlib.Path(generated["AGENTSTACK_MAIL_ENV"]).is_file()

        connection_path = home / ".agentstack" / "connections" / "local.json"
        connection = json.loads(connection_path.read_text(encoding="utf-8"))
        connection["expected_server_instance_id"] = "mail-instance-fixture"
        connection_path.write_text(json.dumps(connection) + "\n", encoding="utf-8")
        connection_path.chmod(0o600)
        workdir = tmp_path / "persistent-work"
        workdir.mkdir()
        claude = tmp_path / "claude"
        claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        claude.chmod(0o755)
        profile_path = home / ".agentstack" / "profiles" / "fixture.json"
        profile_path.write_text(
            json.dumps(
                {
                    "kind": "orrery-persistent-agent-v1",
                    "name": "PersistentFixture",
                    "agent_id": 41,
                    "project_key": str(project),
                    "connection": str(connection_path),
                    "provider": "claude",
                    "parentless": True,
                    "lifecycle": "persistent",
                    "interaction": "interactive",
                    "state_dir": str(tmp_path / "persistent-state"),
                    "working_directory": str(workdir),
                    "command": [
                        str(claude),
                        "--channels",
                        "plugin:telegram@claude-plugins-official",
                    ],
                    "environment": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        profile_path.chmod(0o600)

        wrapper_env = {**second_env, **generated}
        wrapper_env.pop("AGENTSTACK_ENROLL_BIN", None)
        inspected = subprocess.run(
            [
                str(home / ".agentstack" / "bin" / "agentstack-persistent"),
                "inspect",
                "--profile",
                str(profile_path),
            ],
            env=wrapper_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert inspected.returncode == 0, inspected.stdout + inspected.stderr
        assert json.loads(inspected.stdout)["ready"] is True
    finally:
        _stop_mail(home, state_root, mail_port)
        stop_dashboard(home, label_prefix=env["AGENTSTACK_LABEL_PREFIX"])


def _copy_persistent_launcher(stack: pathlib.Path) -> pathlib.Path:
    bin_dir = stack / "bin"
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / "agentstack-persistent"
    shutil.copy2(
        ROOT / "scripts" / "lib" / "agentstack-persistent-launcher.sh",
        launcher,
    )
    launcher.chmod(0o755)
    return launcher


def test_persistent_launcher_preserves_pty_stdin_argv_and_exit_status(tmp_path):
    stack = tmp_path / "stack"
    launcher = _copy_persistent_launcher(stack)
    implementation = launcher.with_suffix(".py")
    implementation.write_text(
        "import json, os, sys\n"
        "value = {\n"
        "    'argv': sys.argv[1:],\n"
        "    'stdin': sys.stdin.readline().rstrip('\\r\\n'),\n"
        "    'stdin_isatty': os.isatty(0),\n"
        "    'stdout_isatty': os.isatty(1),\n"
        "}\n"
        "print(json.dumps(value), flush=True)\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    selected_dir = tmp_path / "selected python"
    selected_dir.mkdir()
    selected_python = selected_dir / "python with spaces"
    selected_python.symlink_to(pathlib.Path(sys.executable).resolve())
    (stack / "env.sh").write_text(
        f"export AGENTSTACK_PYTHON={shlex.quote(str(selected_python))}\n",
        encoding="utf-8",
    )
    ambient_bin = tmp_path / "ambient"
    ambient_bin.mkdir()
    ambient_marker = tmp_path / "ambient-ran"
    _write_command(
        ambient_bin,
        "python3",
        "#!/bin/sh\n"
        f"touch {shlex.quote(str(ambient_marker))}\n"
        "exit 91\n",
    )
    env = {
        "HOME": str(tmp_path),
        "AGENTSTACK_HOME": str(stack),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        # This inherited value is intentionally wrong. The installed env is
        # authoritative and must replace it before the exec.
        "AGENTSTACK_PYTHON": str(ambient_bin / "python3"),
        "PATH": f"{ambient_bin}:/usr/bin:/bin",
    }

    master, slave = pty.openpty()
    os.set_blocking(master, False)
    process = subprocess.Popen(
        [str(launcher), "alpha", "two words"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    chunks: list[bytes] = []
    try:
        os.write(master, b"payload over pty\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            if process.poll() is not None:
                while True:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)
                break
        returncode = process.wait(timeout=2)
    finally:
        os.close(master)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    output = b"".join(chunks).decode("utf-8", errors="replace")
    payload = next(
        json.loads(line.rstrip("\r"))
        for line in output.splitlines()
        if line.lstrip().startswith("{")
    )
    assert returncode == 23
    assert payload == {
        "argv": ["alpha", "two words"],
        "stdin": "payload over pty",
        "stdin_isatty": True,
        "stdout_isatty": True,
    }
    assert not ambient_marker.exists()


def test_persistent_launcher_fails_closed_when_installed_python_is_unavailable(
    tmp_path,
):
    for case, env_body, expected in (
        (
            "missing-assignment",
            "export UNRELATED=value\n",
            "installed Python is unavailable; re-run install.sh",
        ),
        (
            "missing-executable",
            "export AGENTSTACK_PYTHON=/definitely/missing/python\n",
            "installed Python is unavailable; re-run install.sh",
        ),
        (
            "relative-executable",
            "export AGENTSTACK_PYTHON=python3\n",
            "installed Python path is not absolute; re-run install.sh",
        ),
        (
            "env-load-failure",
            "false\n",
            "could not load installed environment",
        ),
    ):
        stack = tmp_path / case
        launcher = _copy_persistent_launcher(stack)
        launcher.with_suffix(".py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (stack / "env.sh").write_text(env_body, encoding="utf-8")
        run_cwd = None
        run_path = "/usr/bin:/bin"
        execution_markers: list[pathlib.Path] = []
        if case == "relative-executable":
            run_cwd = tmp_path / f"{case}-cwd"
            run_cwd.mkdir()
            local_marker = tmp_path / f"{case}-local-ran"
            _write_command(
                run_cwd,
                "python3",
                "#!/bin/sh\n"
                f"touch {shlex.quote(str(local_marker))}\n"
                "exit 41\n",
            )
            ambient_bin = tmp_path / f"{case}-ambient"
            ambient_bin.mkdir()
            ambient_marker = tmp_path / f"{case}-ambient-ran"
            _write_command(
                ambient_bin,
                "python3",
                "#!/bin/sh\n"
                f"touch {shlex.quote(str(ambient_marker))}\n"
                "exit 42\n",
            )
            run_path = f"{ambient_bin}:/usr/bin:/bin"
            execution_markers = [local_marker, ambient_marker]
        result = subprocess.run(
            [str(launcher), "inspect"],
            env={
                "HOME": str(tmp_path),
                "AGENTSTACK_HOME": str(stack),
                "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
                # A valid inherited value must not mask missing/broken env.
                "AGENTSTACK_PYTHON": sys.executable,
                "PATH": run_path,
            },
            cwd=run_cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 1, (case, result.stdout, result.stderr)
        assert expected in result.stderr, (case, result.stderr)
        assert not any(marker.exists() for marker in execution_markers), case


def test_bundled_watcher_reads_agentstack_per_message_signal(tmp_path):
    """Exercise the producer-shaped per-message layout through the real watcher."""

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
  capture-pane) printf '%s\n' 'Claude Code' ;;
  send-keys) exit 0 ;;
  *) exit 1 ;;
esac
""",
    )
    signals = tmp_path / "signals"
    runtime = tmp_path / "runtime"
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
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_TMUX_LOG": str(tmux_log),
            "AGENTSTACK_MAIL_HOME": str(tmp_path / "mail-home"),
            "AGENTSTACK_SIGNALS_DIR": str(signals),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(lock),
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
            if (
                state.get("BreezyMaxwell:42", {}).get("last_result") == "success"
                and not signal_file.exists()
            ):
                break
            time.sleep(0.1)
        assert state.get("BreezyMaxwell:42", {}).get("last_result") == "success"
        assert not signal_file.exists()
        calls = tmux_log.read_text(encoding="utf-8")
        assert "has-session -t BreezyMaxwell" in calls
        assert "capture-pane -t BreezyMaxwell" in calls
        assert "message from ProOpus [high]: per-message verification" in calls
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)
