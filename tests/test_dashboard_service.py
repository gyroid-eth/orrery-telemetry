"""Dashboard service supervision, diagnostics, and bounded log coverage."""

from __future__ import annotations

import json
import os
import pathlib
import plistlib
import pty
import re
import signal
import socket
import subprocess
import sys
import time


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
            "--dashboard-only", "--dry-run", "--install-dir", str(install_dir),
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
