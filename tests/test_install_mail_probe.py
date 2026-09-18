"""Native ORRERY Mail listener-adoption guards."""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import plistlib
import shlex
import socket
import subprocess
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX, stop_dashboard


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
    pin_native_venv: bool | None = None,
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
    if pin_native_venv is None:
        pin_native_venv = loaded_legacy
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
        "AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS": label,
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
    })
    if pin_native_venv:
        # The explicit candidate pin belongs to the legacy-retirement fixture.
        # A normal native-listener reinstall must exercise the unpinned reuse
        # contract instead of inheriting that unrelated input.
        env["AGENTSTACK_MAIL_SERVICE_VENV"] = str(
            pathlib.Path(sys.executable).parent.parent
        )
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


def _run_unknown_listener_install(
    tmp_path: pathlib.Path, mail_port: int
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path, pathlib.Path]:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    state_root = tmp_path / "native-state"
    state_root.mkdir(exist_ok=True)
    (state_root / "storage.sqlite3").touch()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    label_prefix = f"{TEST_LABEL_PREFIX}.mail-probe"
    mail_label = f"{label_prefix}.mail"
    systemctl_log = tmp_path / "systemctl.log"
    registration_marker = tmp_path / "existing-mail-trigger-registered"
    registration_marker.touch()
    for name, body in {
        "systemctl": f"""#!/bin/sh
printf '%s\\n' "$*" >> '{systemctl_log}'
case " $* " in
  *" {mail_label}.timer "*|*" {mail_label}.service "*)
    rm -f '{registration_marker}'
    ;;
esac
exit 1
""",
        "tmux": "#!/bin/sh\nexit 0\n",
        "uname": "#!/bin/sh\nprintf '%s\\n' Linux\n",
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)

    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    timer = unit_dir / f"{mail_label}.timer"
    service = unit_dir / f"{mail_label}.service"
    timer.write_bytes(b"EXISTING-TIMER\n")
    service.write_bytes(b"EXISTING-SERVICE\n")

    stale_marker = tmp_path / "stale-enroll-invoked"
    stale_enroll = tmp_path / "stale" / "agentstack-enroll"
    stale_enroll.parent.mkdir()
    stale_enroll.write_text(
        f"#!/bin/sh\ntouch '{stale_marker}'\nexit 97\n", encoding="utf-8"
    )
    stale_enroll.chmod(0o755)

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
        "AGENTSTACK_LABEL_PREFIX": label_prefix,
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_MAIL_STATE_ROOT": str(state_root),
        "AGENTSTACK_MAIL_SERVICE_ROOT": str(home / ".agentstack" / "mail-service"),
        "AGENTSTACK_MAIL_ENROLL_BIN": str(stale_enroll),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{mail_port}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
    })
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "install.sh"), "--dashboard-only"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    return result, home, registration_marker, stale_marker


def test_non_mail_listener_is_rejected_without_guessing_a_database(tmp_path):
    server = _serve(_SilentListener)
    try:
        result = _run_installer(tmp_path, server.server_port)
    finally:
        server.shutdown()

    assert result.returncode != 0
    assert "did not answer an ORRERY Mail health check" in result.stderr
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
    provision = result.stdout.index("installer will provision ORRERY Mail")
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
    assert f"existing ORRERY Mail database: {expected_db}" in result.stdout
    assert "retire legacy mail service" not in result.stdout
    assert "installer will provision ORRERY Mail" not in result.stdout


def test_unknown_listener_with_explicit_candidate_pin_fails_without_stopping_it(tmp_path):
    state_root = tmp_path / "native-state"
    state_root.mkdir()
    expected_db = state_root / "storage.sqlite3"
    expected_db.touch()
    _ForeignDatabaseListener.database = expected_db
    server = _serve(_ForeignDatabaseListener)
    try:
        result, _state_root = _run_legacy_dry_run(
            tmp_path,
            server.server_port,
            loaded_legacy=False,
            retire=False,
            pin_native_venv=True,
        )
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=2):
            listener_still_running = True
    finally:
        server.shutdown()

    assert result.returncode != 0
    assert "cannot verify explicit AGENTSTACK_MAIL_SERVICE_VENV" in result.stderr
    assert "service env is not identifiable" in result.stderr
    assert listener_still_running


def test_real_install_reuses_unknown_listener_without_claiming_deployment_or_trigger(
    tmp_path,
):
    state_root = tmp_path / "native-state"
    state_root.mkdir()
    expected_db = state_root / "storage.sqlite3"
    expected_db.touch()
    _ForeignDatabaseListener.database = expected_db
    server = _serve(_ForeignDatabaseListener)
    home = tmp_path / "home"
    try:
        result, home, registration_marker, stale_marker = (
            _run_unknown_listener_install(tmp_path, server.server_port)
        )
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=2):
            listener_still_running = True

        label_prefix = f"{TEST_LABEL_PREFIX}.mail-probe"
        mail_label = f"{label_prefix}.mail"
        unit_dir = home / ".config" / "systemd" / "user"
        timer = unit_dir / f"{mail_label}.timer"
        service = unit_dir / f"{mail_label}.service"
        generated = _generated_env(home) if result.returncode == 0 else {}
        manifest_path = home / ".agentstack" / "install-state.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {}
        )
        systemctl_log = tmp_path / "systemctl.log"
        systemctl_calls = (
            systemctl_log.read_text(encoding="utf-8").splitlines()
            if systemctl_log.exists()
            else []
        )
        launcher = home / ".agentstack" / "bin" / "agentstack-enroll"
        launcher_result = (
            subprocess.run(
                [str(launcher), "inspect", "--help"],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "AGENTSTACK_HOME": str(home / ".agentstack"),
                    "AGENTSTACK_LABEL_PREFIX": f"{TEST_LABEL_PREFIX}.mail-probe",
                    "AGENTSTACK_MAIL_ENROLL_BIN": str(
                        tmp_path / "stale" / "agentstack-enroll"
                    ),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            if launcher.exists()
            else None
        )
    finally:
        dashboard_pidfile = home / ".agentstack" / "runtime" / "dashboard.pid"
        if dashboard_pidfile.exists():
            stop_dashboard(home, label_prefix=f"{TEST_LABEL_PREFIX}.mail-probe")
        server.shutdown()

    assert result.returncode == 0, result.stdout + result.stderr
    assert listener_still_running
    assert generated["AGENTSTACK_MAIL_ENV"] == ""
    assert generated["AGENTSTACK_MAIL_ENROLL_BIN"] == ""
    assert generated["AGENTSTACK_MAIL_MANAGEMENT_SOCKET"] == ""
    assert str(ROOT) not in generated["AGENTSTACK_MAIL_ENV"]
    assert manifest["agent_mail"]["deployment_identified"] is False
    assert manifest["agent_mail"]["enrollment_available"] is False
    assert manifest["agent_mail"]["autostart_managed_by_this_install"] is False
    assert manifest["agent_mail"]["candidate_venv"] == ""
    assert manifest["agent_mail"]["service_env"] == ""
    assert manifest["agent_mail"]["deployment_metadata"] == ""
    assert manifest["agent_mail"]["enroll_bin"] == ""
    assert "" not in manifest["retained_paths"]
    assert not any(
        service_entry.get("role") == "agent-mail-autostart"
        for service_entry in manifest["services"]
    )
    assert timer.read_bytes() == b"EXISTING-TIMER\n"
    assert service.read_bytes() == b"EXISTING-SERVICE\n"
    assert registration_marker.exists()
    assert not any(
        f"{mail_label}.timer" in call or f"{mail_label}.service" in call
        for call in systemctl_calls
    )
    assert not (home / ".agentstack" / "connections" / "local.json").exists()
    assert launcher_result is not None
    assert launcher_result.returncode != 0
    assert "installed Mail enrollment executable is unavailable" in launcher_result.stderr
    assert not stale_marker.exists()
    assert "reusing the healthy ORRERY Mail listener without changing it" in result.stderr
    assert "existing trigger files and registrations were left untouched" in result.stderr
