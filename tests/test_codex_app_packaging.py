from __future__ import annotations

import json
import os
import plistlib
import pytest
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-codex-app-integration.sh"
EXPORTER = ROOT / "scripts" / "export-component.sh"
PROXY_TOOLS = (
    "bootstrap",
    "fetch_inbox",
    "send_message",
    "acknowledge_message",
    "reserve_files",
    "renew_reservations",
    "release_reservations",
    "runtime_status",
)


# Unix socket paths are capped near 104 bytes, so these tests need a *short*
# temp directory. macOS puts the real one at /private/tmp; Linux has no such
# path at all, and pointing at a directory that does not exist made every one
# of these fail there with FileNotFoundError. None means "use the platform
# default", which on Linux is /tmp and is already short.
SHORT_TMP_DIR = "/private/tmp" if os.path.isdir("/private/tmp") else None

def _environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment["CODEX_HOME"] = str(home / ".codex")
    environment.pop("AGENTSTACK_CODEX_APP_INSTALL_DIR", None)
    environment.pop("AGENTSTACK_CODEX_APP_RUNTIME_DIR", None)
    return environment


# Some of these tests only need a path to hand the installer; others drive the
# real `codex plugin` registry and mean nothing without it. Keep the two apart:
# stub the first group so it runs everywhere, and skip the second with a reason
# that names what is missing. A test that quietly asserts nothing is worse than
# one that says it did not run.
HAVE_CODEX_CLI = shutil.which("codex") is not None
needs_codex_cli = pytest.mark.skipif(
    not HAVE_CODEX_CLI,
    reason="requires the Codex CLI on PATH (it drives `codex plugin`)",
)


def _stub_codex(home: Path) -> str:
    """A stand-in `codex` for machines that do not have the CLI installed.

    These tests only need a path to hand the installer, which records it and
    (in these dry runs) asks it for a version at most. Requiring the real CLI
    made five of them fail on every CI run with `assert None is not None` —
    a machine-shaped requirement stated as an assertion about nothing.
    """
    stub = home / "stub-bin" / "codex"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/bin/sh\necho 'codex-cli 0.0.0-stub'\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)


def _install_args(
    home: Path,
    *,
    runtime_dir: Path | None = None,
    agent_mail_url: str = "http://127.0.0.1:8765/api/",
    service: bool = False,
) -> list[str]:
    codex_binary = shutil.which("codex") or _stub_codex(home)
    args = [
        str(INSTALLER),
        "--project-key",
        str(home / "project"),
        "--agent-mail-url",
        agent_mail_url,
        "--agent-mail-env",
        str(home / "mcp_agent_mail" / ".env"),
        "--signals-dir",
        str(home / ".mcp_agent_mail" / "signals"),
        "--codex-bin",
        codex_binary,
    ]
    if not service:
        args.insert(1, "--no-service")
    if runtime_dir is not None:
        args.extend(["--runtime-dir", str(runtime_dir)])
    return args


def _prepare_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / "project").mkdir()
    mail_env = home / "mcp_agent_mail" / ".env"
    mail_env.parent.mkdir()
    mail_env.write_text(
        "HTTP_BEARER_" + "TOKEN=example-secret-value\n",
        encoding="utf-8",
    )
    return home


def _plugin_list(home: Path) -> dict:
    result = subprocess.run(
        ["codex", "plugin", "list", "--json"],
        env=_environment(home),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _read_generated_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("export "):
            continue
        assignment = shlex.split(line)[1]
        key, separator, value = assignment.partition("=")
        assert separator == "="
        values[key] = value
    return values


@needs_codex_cli
def test_installer_dry_run_does_not_write_clean_home(tmp_path):
    home = _prepare_home(tmp_path)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    result = subprocess.run(
        [*_install_args(home), "--dry-run"],
        env=_environment(home),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Dry-run complete: no files were written." in result.stdout
    assert not install_dir.exists()
    assert _plugin_list(home)["installed"] == []


def test_installer_skip_git_check_is_explicit_and_persisted(tmp_path):
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    subprocess.run(
        [*_install_args(home), "--no-plugin", "--skip-git-check"],
        env=environment,
        check=True,
    )

    generated_env = _read_generated_env(install_dir / "env.sh")
    assert generated_env["AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK"] == "1"

    subprocess.run(
        [
            str(install_dir / "bin" / "uninstall-codex-app-integration"),
            "--purge-data",
        ],
        env=environment,
        check=True,
    )


def test_launchd_bootstrap_failure_uses_supervised_background(tmp_path):
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    runtime_dir = Path(tempfile.mkdtemp(prefix="cas-codex-app-", dir=SHORT_TMP_DIR))
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    for name, body in {
        "uname": "#!/bin/sh\necho Darwin\n",
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
    }.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8")
        command.chmod(0o755)
    environment.update({
        "PATH": f"{fake_bin}:{environment['PATH']}",
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_CODEX_APP_RESTART_DELAY": "0",
        "AGENTSTACK_TEST_LAUNCHCTL_LOG": str(launchctl_log),
    })

    result = subprocess.run(
        [
            *_install_args(
                home,
                runtime_dir=runtime_dir,
                service=True,
            ),
            "--no-plugin",
        ],
        env=environment,
        text=True,
        capture_output=True,
    )

    try:
        assert result.returncode == 0, result.stderr
        manifest = json.loads(
            (install_dir / "install-state.json").read_text(encoding="utf-8")
        )
        pidfile = Path(manifest["service"]["pidfile"])
        supervisor_pid = int(pidfile.read_text(encoding="utf-8").strip())
        os.kill(supervisor_pid, 0)
        assert manifest["service"]["kind"] == "nohup"
        assert manifest["launchd"]["enabled"] is False
        live_plist = (
            home
            / "Library"
            / "LaunchAgents"
            / f'{manifest["launchd"]["label"]}.plist'
        )
        assert not live_plist.exists()
        assert "falling back to supervised background" in result.stderr
        assert "Service mode: supervised background" in result.stdout

        socket_path = runtime_dir / "bridge.sock"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not socket_path.exists():
            time.sleep(0.05)
        assert socket_path.is_socket()

        child_pidfile = runtime_dir / "bridge-child.pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not child_pidfile.exists():
            time.sleep(0.05)
        first_child_pid = int(child_pidfile.read_text(encoding="utf-8").strip())
        os.kill(first_child_pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        restarted_child_pid = first_child_pid
        while time.monotonic() < deadline:
            try:
                restarted_child_pid = int(
                    child_pidfile.read_text(encoding="utf-8").strip()
                )
            except (FileNotFoundError, ValueError):
                pass
            if restarted_child_pid != first_child_pid and socket_path.is_socket():
                break
            time.sleep(0.05)
        assert restarted_child_pid != first_child_pid
        os.kill(supervisor_pid, 0)

        doctor = subprocess.run(
            [str(install_dir / "bin" / "doctor-codex-app-integration")],
            env=environment,
            text=True,
            capture_output=True,
        )
        assert doctor.returncode == 0, doctor.stderr
        assert "Bridge service mode supervised-background" in doctor.stdout

        subprocess.run(
            [
                str(install_dir / "bin" / "uninstall-codex-app-integration"),
                "--purge-data",
            ],
            env=environment,
            check=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(supervisor_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("background Bridge supervisor survived uninstall")
    finally:
        manifest_path = install_dir / "install-state.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pidfile_value = manifest.get("service", {}).get("pidfile")
            if pidfile_value:
                try:
                    os.kill(int(Path(pidfile_value).read_text().strip()), signal.SIGTERM)
                except (FileNotFoundError, ProcessLookupError, ValueError):
                    pass


@needs_codex_cli
def test_clean_home_install_uninstall_reinstall(tmp_path):
    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    runtime_dir = Path(tempfile.mkdtemp(prefix="cas-codex-app-", dir=SHORT_TMP_DIR))

    subprocess.run(
        _install_args(home, runtime_dir=runtime_dir),
        env=environment,
        check=True,
    )

    env_file = install_dir / "env.sh"
    assert env_file.is_file()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "example-secret-value" not in env_file.read_text(encoding="utf-8")
    generated_env = _read_generated_env(env_file)
    codex_binary = generated_env["AGENTSTACK_CODEX_BINARY"]
    assert Path(codex_binary).is_absolute()
    resolved = shutil.which("codex")
    if resolved is not None:
        assert Path(codex_binary).samefile(resolved)
    else:
        assert Path(codex_binary).name == "codex" and Path(codex_binary).exists()
    assert generated_env["AGENTSTACK_CODEX_APP_PLUGIN_ID"] == (
        "agentstack-codex-app@agentstack-local"
    )
    assert generated_env["AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK"] == "0"
    assert generated_env["AGENTSTACK_CODEX_APP_STALE_AFTER_SECONDS"] == "3600"
    assert generated_env["AGENTSTACK_CODEX_APP_RETRY_MAX_ATTEMPTS"] == "12"
    assert generated_env["AGENTSTACK_CODEX_APP_RETRY_MAX_AGE_SECONDS"] == "3600"
    assert generated_env["AGENTSTACK_CODEX_APP_RETRY_MAX_BACKOFF_SECONDS"] == "300"
    manifest = json.loads(
        (install_dir / "install-state.json").read_text(encoding="utf-8")
    )
    assert manifest["plugin"]["id"] == "agentstack-codex-app@agentstack-local"
    with (install_dir / "launchd" / "org.agentstack.codex-app-bridge.plist").open(
        "rb"
    ) as handle:
        plist = plistlib.load(handle)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        str(install_dir / "bin" / "run-bridge"),
    ]
    assert plist["StandardOutPath"] == str(runtime_dir / "bridge.stdout.log")
    assert plist["StandardErrorPath"] == str(runtime_dir / "bridge.stderr.log")

    installed = _plugin_list(home)["installed"]
    assert [item["pluginId"] for item in installed] == [
        "agentstack-codex-app@agentstack-local"
    ]
    proxy_script = install_dir / "plugin" / "scripts" / "run-mcp.sh"
    approval_args = [
        "-c",
        (
            "plugins.agentstack-codex-app@agentstack-local."
            "mcp_servers.agentstack.enabled=false"
        ),
        "-c",
        'mcp_servers.agentstack.command="/bin/bash"',
        "-c",
        (
            "mcp_servers.agentstack.args="
            f"[{json.dumps(str(proxy_script))}]"
        ),
    ]
    for tool_name in PROXY_TOOLS:
        approval_args.extend(
            [
                "-c",
                (
                    f"mcp_servers.agentstack.tools.{tool_name}."
                    'approval_mode="approve"'
                ),
            ]
        )
    strict_config = subprocess.run(
        [
            codex_binary,
            "exec",
            "resume",
            "--strict-config",
            *approval_args,
            "--skip-git-repo-check",
            "00000000-0000-0000-0000-000000000000",
            "configuration probe",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert strict_config.returncode != 0
    assert "no rollout found" in strict_config.stderr
    assert "configuration" not in strict_config.stderr.lower()
    cached = (
        home
        / ".codex"
        / "plugins"
        / "cache"
        / "agentstack-local"
        / "agentstack-codex-app"
        / "0.1.0"
    )
    assert (cached / "src" / "agentstack_codex_app" / "mcp_server.py").is_file()
    assert (cached / "schemas" / "migrations" / "001_delivery_state.sql").is_file()
    assert (cached / "scripts" / "run-mcp.sh").is_file()
    mcp = subprocess.run(
        [str(cached / "scripts" / "run-mcp.sh")],
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        + "\n",
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(mcp.stdout)["result"]["serverInfo"]["name"] == "agentstack"

    doctor = subprocess.run(
        [
            str(install_dir / "bin" / "doctor-codex-app-integration"),
            "--allow-stopped",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ok: Codex plugin registered" in doctor.stdout
    assert "example-secret-value" not in doctor.stdout + doctor.stderr
    assert not list((install_dir / "src").rglob("__pycache__"))

    stdout_log = runtime_dir / "bridge.stdout.log"
    stderr_log = runtime_dir / "bridge.stderr.log"
    with stdout_log.open("w", encoding="utf-8") as stdout_handle, stderr_log.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        bridge = subprocess.Popen(
            [str(install_dir / "bin" / "run-bridge")],
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        socket_path = runtime_dir / "bridge.sock"
        deadline = time.monotonic() + 5
        while (
            bridge.poll() is None
            and not socket_path.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        try:
            assert bridge.poll() is None, stderr_log.read_text(encoding="utf-8")
            assert socket_path.is_socket()
            live_doctor = subprocess.run(
                [str(install_dir / "bin" / "doctor-codex-app-integration")],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            assert "ok: Bridge startup diagnostic" in live_doctor.stdout
            assert "ok: no stale spool drains" in live_doctor.stdout
        finally:
            bridge.terminate()
            bridge.wait(timeout=5)
            socket_path.unlink(missing_ok=True)
    bridge_stderr = stderr_log.read_text(encoding="utf-8")
    assert '"event":"bridge_launcher_start"' in bridge_stderr
    assert '"event":"bridge_start"' in bridge_stderr

    diagnostic_environment = dict(
        environment,
        PYTHONPATH=str(install_dir / "src"),
    )
    identity_probe = subprocess.run(
        [
            generated_env["AGENTSTACK_PYTHON"],
            "-c",
            """
import json
from pathlib import Path
import sys
from agentstack_codex_app.agent_mail_client import Registration
from agentstack_codex_app.daemon import BridgeConfig, BridgeDaemon

class FakeAgentMail:
    def __init__(self):
        self.calls = []

    def register_agent(self, **kwargs):
        self.calls.append(kwargs)
        names = ["BlueLake", "GreenCastle"]
        return Registration(names[len(self.calls) - 1], kwargs["registration_token"])

runtime = Path(sys.argv[1])
project_key = sys.argv[2]
config = BridgeConfig(
    runtime_dir=runtime,
    socket_path=runtime / "bridge.sock",
    spool_path=runtime / "hook-events.jsonl",
    retry_path=runtime / "registration-retry.jsonl",
    snapshot_path=runtime / "snapshot.json",
    project_key=project_key,
    agent_mail_endpoint="http://agent-mail.invalid/api/",
    enforce_surface_eligibility=False,
    cold_wake_enabled=False,
)
mail = FakeAgentMail()
daemon = BridgeDaemon(config, mail)
root = daemon.process_event({
    "schema_version": 1,
    "session_id": "packaging-session",
    "agent_id": None,
    "cwd": project_key,
    "model": "gpt-example",
    "hook_event_name": "SessionStart",
    "turn_id": None,
})
child = daemon.process_event({
    "schema_version": 1,
    "session_id": "packaging-session",
    "agent_id": "child-1",
    "cwd": project_key,
    "model": "gpt-example",
    "hook_event_name": "SubagentStart",
    "turn_id": None,
})
assert all("agent_name" not in call for call in mail.calls)
assert daemon.identities.resolve(root)["agent_name"] == "BlueLake"
assert daemon.identities.resolve(child)["agent_name"] == "GreenCastle"
print(json.dumps({"root": "BlueLake", "child": "GreenCastle"}))
""",
            str(runtime_dir / "packaging-identity-probe"),
            str(home / "project"),
        ],
        env=diagnostic_environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(identity_probe.stdout) == {
        "root": "BlueLake",
        "child": "GreenCastle",
    }
    subprocess.run(
        [
            generated_env["AGENTSTACK_PYTHON"],
            "-c",
            """
from pathlib import Path
import sys
from agentstack_codex_app.delivery import DeliveryManager
from agentstack_codex_app.identity_store import build_binding
from agentstack_codex_app.snapshot import SnapshotStore, runtime_record

runtime = Path(sys.argv[1])
binding = build_binding(
    session_id="session-requeue",
    agent_id=None,
    agent_name="ExampleAgent",
    project_key=sys.argv[2],
)
SnapshotStore(runtime / "snapshot.json").upsert(
    runtime_record(
        binding,
        {"cwd": sys.argv[2], "model": "gpt-example"},
        state="blocked",
    )
)
delivery = DeliveryManager(runtime / "delivery.sqlite3")
delivery.observe(sys.argv[2], "ExampleAgent", [77])
delivery.acquire(
    sys.argv[2],
    "ExampleAgent",
    [77],
    lease_owner="wake-test",
    lease_seconds=30,
)
delivery.mark_failed(
    sys.argv[2],
    "ExampleAgent",
    [77],
    lease_owner="wake-test",
    error_code=(
        "untrusted_workspace exit=0 output="
        "Not inside a trusted directory"
    ),
    max_attempts=5,
    terminal=True,
)
""",
            str(runtime_dir),
            str(home / "project"),
        ],
        env=diagnostic_environment,
        check=True,
    )
    requeue = subprocess.run(
        [
            str(install_dir / "bin" / "doctor-codex-app-integration"),
            "--allow-stopped",
            "--requeue-message",
            "77",
            "--agent-name",
            "ExampleAgent",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "requeued delivery message 77 for ExampleAgent" in requeue.stdout
    assert "untrusted_workspace exit=0" in requeue.stderr
    snapshot = json.loads((runtime_dir / "snapshot.json").read_text(encoding="utf-8"))
    runtime = next(
        item for item in snapshot["runtimes"] if item["agent_name"] == "ExampleAgent"
    )
    assert runtime["state"] == "waiting"
    assert runtime["delivery"]["wake_status"] == "pending"
    delivery_state = subprocess.run(
        [
            generated_env["AGENTSTACK_PYTHON"],
            "-c",
            """
import json
import sys
from agentstack_codex_app.delivery import DeliveryManager
print(json.dumps(DeliveryManager(sys.argv[1]).rows()))
""",
            str(runtime_dir / "delivery.sqlite3"),
        ],
        env=diagnostic_environment,
        capture_output=True,
        text=True,
        check=True,
    )
    row = json.loads(delivery_state.stdout)[0]
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["last_error"] is None

    subprocess.run(
        [str(install_dir / "bin" / "uninstall-codex-app-integration")],
        env=environment,
        check=True,
    )
    assert not install_dir.exists()
    assert runtime_dir.is_dir()
    assert _plugin_list(home)["installed"] == []

    subprocess.run(
        _install_args(home, runtime_dir=runtime_dir),
        env=environment,
        check=True,
    )
    assert install_dir.is_dir()
    assert len(_plugin_list(home)["installed"]) == 1
    subprocess.run(
        [
            str(install_dir / "bin" / "uninstall-codex-app-integration"),
            "--purge-data",
        ],
        env=environment,
        check=True,
    )
    assert not install_dir.exists()
    assert not runtime_dir.exists()
    assert _plugin_list(home)["installed"] == []


def test_doctor_cleanup_retires_orphan_before_local_purge(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            calls.append(payload)
            tool_name = payload["params"]["name"]
            if tool_name == "whois":
                structured = {
                    "name": "CalmNoether",
                    "program": "codex-app",
                }
            else:
                structured = {
                    "status": "retired",
                    "agent_name": "CalmNoether",
                    "project_key": str(home / "project"),
                }
            response = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "structuredContent": structured,
                },
            }
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    home = _prepare_home(tmp_path)
    environment = _environment(home)
    install_dir = home / ".agentstack" / "integrations" / "codex_app"
    runtime_dir = home / ".agentstack" / "runtime" / "codex-app"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/api/"
    try:
        subprocess.run(
            [
                *_install_args(
                    home,
                    runtime_dir=runtime_dir,
                    agent_mail_url=endpoint,
                ),
                "--no-service",
                "--no-plugin",
            ],
            env=environment,
            check=True,
        )
        diagnostic_environment = dict(
            environment,
            PYTHONPATH=str(install_dir / "src"),
        )
        subprocess.run(
            [
                str(Path(_read_generated_env(install_dir / "env.sh")["AGENTSTACK_PYTHON"])),
                "-c",
                """
from agentstack_codex_app.identity_store import IdentityStore, build_binding
from agentstack_codex_app.snapshot import SnapshotStore, runtime_record
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1])
project_key = sys.argv[2]
store = IdentityStore(runtime / "identity")
binding = store.save(
    build_binding(
        session_id="session-orphan",
        agent_id=None,
        agent_name="CalmNoether",
        project_key=project_key,
    )
)
store.store_owner_token(binding["external_id"], "owner-token")
SnapshotStore(runtime / "snapshot.json").upsert(
    runtime_record(binding, {}, state="waiting")
)
""",
                str(runtime_dir),
                str(home / "project"),
            ],
            env=diagnostic_environment,
            check=True,
        )

        doctor = subprocess.run(
            [
                str(install_dir / "bin" / "doctor-codex-app-integration"),
                "--allow-stopped",
                "--cleanup-orphan-bindings",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )

        assert "cleanup complete: 1 cleaned, 0 failed" in doctor.stdout
        assert len(calls) == 2
        assert [call["params"]["name"] for call in calls] == [
            "whois",
            "retire_agent",
        ]
        assert calls[1]["params"]["arguments"]["registration_token"] == "owner-token"
        assert not list((runtime_dir / "identity" / "bindings").glob("*.json"))
        assert not list((runtime_dir / "identity" / "secrets").glob("*.token"))
        snapshot = json.loads(
            (runtime_dir / "snapshot.json").read_text(encoding="utf-8")
        )
        assert snapshot["runtimes"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if install_dir.exists():
            subprocess.run(
                [
                    str(install_dir / "bin" / "uninstall-codex-app-integration"),
                    "--purge-data",
                ],
                env=environment,
                check=True,
            )


def test_export_gate_builds_allowlisted_token_free_artifact(tmp_path):
    home = _prepare_home(tmp_path)
    destination = tmp_path / "exported"
    environment = _environment(home)
    environment["AGENTSTACK_CODEX_APP_INSTALL_DIR"] = str(
        home / "no-live-integration"
    )
    result = subprocess.run(
        [
            str(EXPORTER),
            "codex-app",
            str(destination),
            "--skip-tests",
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    # `check=True` raises CalledProcessError, which prints the argv and hides
    # both streams — so this failing on Linux CI told us only "exit status 1"
    # and nothing about why. A test that captures output owes the reader that
    # output when it fails.
    assert result.returncode == 0, (
        f"export-component.sh exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    assert "Export complete:" in result.stdout
    assert (
        destination
        / "integrations"
        / "codex_app"
        / "src"
        / "agentstack_codex_app"
        / "daemon.py"
    ).is_file()
    exported_env = destination / "integrations" / "codex_app" / "env.sh"
    assert exported_env.stat().st_mode & 0o777 == 0o600
    assert "/workspace/example" in exported_env.read_text(encoding="utf-8")
    assert not list(destination.rglob("*.sqlite3"))
    assert not list(destination.rglob("__pycache__"))
