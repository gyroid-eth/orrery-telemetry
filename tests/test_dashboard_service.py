"""Dashboard service supervision, diagnostics, and bounded log coverage."""

from __future__ import annotations

import json
import os
import pathlib
import plistlib
import re
import signal
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

    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert 'ExecStart={esc(\'$PYTHON_BIN\')} {esc(\'$DASHBOARD_DIR/service_runner.py\')}' in installer
    assert '"Restart=always"' in installer
    assert "AGENTSTACK_DASHBOARD_SELF_RESTART=1" in installer


def test_launchd_install_enables_before_bootstrap_then_kickstarts_and_checks_health(
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
    enable = output.index("launchctl enable gui/")
    bootstrap = output.index("launchctl bootstrap gui/")
    kickstart = output.index("launchctl kickstart gui/")
    health = output.index("verify dashboard API responds")
    assert enable < bootstrap < kickstart < health


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
