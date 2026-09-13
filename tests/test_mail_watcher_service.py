#!/usr/bin/env python3
"""The Mail watcher must run as a service, not only as an agent-start side effect.

hooks/watch_agent_mail_signals.sh is what turns a Mail signal file into a tmux
prompt injection. Until 2026-09-07 the only things that started it were
`bin/agent-start` and the Codex bootstrap (a detached tmux session), so a host
whose agents were all spawned from the dashboard delivered nothing: on WSL2,
after `wsl --shutdown`, the dashboard reported watcher_running=false with six
signals stuck while two live agents waited for mail that never came.

These assert that the installer registers a KeepAlive / Restart=always unit
for the watcher on both platforms, that a failed registration cleans up, that
the manifest records the unit for uninstall.sh, and that agent-start's tmux
fallback stands down when a watcher already holds the single-instance lock.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import plistlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"
REGISTER_LIB = ROOT / "bin" / "lib" / "agentstack-register.sh"
SERVER = ROOT / "dashboard" / "server.py"
LABEL_PREFIX = "org.agentstack.test.mail-watcher"


def _autostart_helpers():
    spec = importlib.util.spec_from_file_location(
        "test_mail_autostart_helpers", ROOT / "tests" / "test_mail_autostart.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_enable(tmp: pathlib.Path, platform: str, *, fail: bool = False) -> tuple[subprocess.CompletedProcess, str]:
    helpers = _autostart_helpers()
    home = tmp / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "systemd" / "user").mkdir(parents=True, exist_ok=True)
    hooks = home / ".agentstack" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "watch_agent_mail_signals.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    fake, log = helpers._fake_manager_bin(tmp, platform, fail=fail)
    # No tmux session to retire; has-session must fail, everything else logs.
    helpers._write_command(
        fake, "tmux",
        f'#!/bin/sh\nprintf "tmux %s\\n" "$*" >> {log}\n[ "$1" = has-session ] && exit 1\nexit 0\n',
    )
    script = tmp / "enable.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"export PATH={fake}\n"
        "DRY_RUN=false\n"
        f"HOME='{home}'\n"
        f"BIN_DIR='{home}/.agentstack/bin'\n"
        f"HOOKS_DIR='{hooks}'\n"
        f"RUNTIME_DIR='{home}/.agentstack/runtime'\n"
        f"MAIL_HOME='{home}/.agentstack/mail'\n"
        f"SIGNALS_DIR='{home}/.agentstack/mail/signals'\n"
        f"PYTHON_BIN='{sys.executable}'\n"
        "PATH_VALUE=/usr/bin:/bin\n"
        f"MAIL_WATCHER_LABEL={LABEL_PREFIX}.mail-watcher\n"
        "AGENT_MAIL_WATCHER_KIND=\n"
        "AGENT_MAIL_WATCHER_PATH=\n"
        "say() { printf 'SAY %s\\n' \"$*\"; }\n"
        "warn() { printf 'WARN %s\\n' \"$*\"; }\n"
        "plan() { :; }\n"
        f"eval \"$(sed -n '/^mail_watcher_environment()/,/^}} # end mail_watcher_environment/p' {INSTALLER})\"\n"
        f"eval \"$(sed -n '/^render_mail_watcher_unit()/,/^}} # end render_mail_watcher_unit/p' {INSTALLER})\"\n"
        f"eval \"$(sed -n '/^stop_tmux_mail_watcher()/,/^}} # end stop_tmux_mail_watcher/p' {INSTALLER})\"\n"
        f"eval \"$(sed -n '/^wait_for_launchd_unload()/,/^}} # end wait_for_launchd_unload/p' {INSTALLER})\"\n"
        f"eval \"$(sed -n '/^enable_mail_watcher()/,/^}} # end enable_mail_watcher/p' {INSTALLER})\"\n"
        "enable_mail_watcher\n"
        'printf "KIND=%s\\nPATH_OUT=%s\\n" "$AGENT_MAIL_WATCHER_KIND" "$AGENT_MAIL_WATCHER_PATH"\n',
        encoding="utf-8",
    )
    r = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=180)
    return r, (log.read_text(encoding="utf-8") if log.exists() else "")


def _path_out(stdout: str) -> pathlib.Path:
    return pathlib.Path(re.search(r"^PATH_OUT=(.*)$", stdout, re.M).group(1))


def test_launchd_unit_keeps_the_watcher_alive():
    with tempfile.TemporaryDirectory() as td:
        r, calls = _run_enable(pathlib.Path(td), "Darwin")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "KIND=launchd" in r.stdout, r.stdout
        plist = plistlib.loads(_path_out(r.stdout).read_bytes())
    assert plist["Label"] == f"{LABEL_PREFIX}.mail-watcher"
    # The watcher IS the long-running process (unlike mailctl, which exits), so
    # the unit must keep it alive and pace restarts.
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ThrottleInterval"] >= 1
    assert plist["ProgramArguments"][0] == "/bin/bash"
    assert plist["ProgramArguments"][1].endswith("hooks/watch_agent_mail_signals.sh")
    env = plist["EnvironmentVariables"]
    assert env["AGENTSTACK_SIGNALS_DIR"].endswith("mail/signals")
    assert env["AGENTSTACK_RUNTIME_DIR"].endswith(".agentstack/runtime")
    assert plist["StandardOutPath"].endswith("mail-watcher.log")
    assert f"launchctl bootstrap gui/" in calls and ".mail-watcher.plist" in calls, calls
    # Bootstrapped from a non-GUI context (ssh), RunAtLoad never fires: the Air
    # showed the job loaded with runs = 0. kickstart makes the start explicit.
    assert f"launchctl kickstart gui/" in calls, calls
    lines = calls.splitlines()
    bootstrap = next(i for i, line in enumerate(lines) if "launchctl bootstrap " in line)
    kickstart = next(i for i, line in enumerate(lines) if "launchctl kickstart " in line)
    assert bootstrap < kickstart, calls


def test_systemd_unit_restarts_the_watcher():
    with tempfile.TemporaryDirectory() as td:
        r, calls = _run_enable(pathlib.Path(td), "Linux")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "KIND=systemd-user" in r.stdout, r.stdout
        text = _path_out(r.stdout).read_text(encoding="utf-8")
    assert re.search(r"^Type=simple$", text, re.M), text
    assert re.search(r"^Restart=always$", text, re.M), text
    assert re.search(r'^ExecStart=/bin/bash ".*hooks/watch_agent_mail_signals\.sh"$', text, re.M), text
    assert re.search(r'^Environment=AGENTSTACK_SIGNALS_DIR=".*mail/signals"$', text, re.M), text
    assert re.search(r"^WantedBy=default\.target$", text, re.M), text
    assert f"systemctl --user enable --now {LABEL_PREFIX}.mail-watcher.service" in calls, calls
    # `enable --now` leaves an already-running service on the old unit file.
    assert f"systemctl --user restart {LABEL_PREFIX}.mail-watcher.service" in calls, calls


def test_a_failed_registration_cleans_up_and_reports():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        r, _ = _run_enable(tmp, "Darwin", fail=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "KIND=\n" in r.stdout and "PATH_OUT=\n" in r.stdout, r.stdout
        assert "WARN could not register the ORRERY Mail watcher unit" in r.stdout, r.stdout
        leftovers = list((tmp / "home" / "Library" / "LaunchAgents").glob("*.plist"))
    assert not leftovers, leftovers


def test_installer_registers_the_watcher_after_mail_and_records_it():
    text = INSTALLER.read_text(encoding="utf-8")
    main = text[text.index("\nmain() {"):]
    assert main.index("  enable_mail_autostart\n") < main.index("  enable_mail_watcher\n"), (
        "the watcher unit must be registered from main(), after the mail autostart"
    )
    block = text[text.index("owned_files = []"):text.index("owned_dirs =")]
    assert "owned_files.append(mail_watcher_path)" in block, (
        "the watcher unit file is not installer-owned, so uninstall leaves it behind"
    )
    assert '"role": "mail-watcher"' in text, (
        "the installer does not record the watcher unit in manifest['services']"
    )
    # uninstall.sh iterates services by kind; both kinds are emitted.
    assert re.search(r'"kind": "launchd", "label": mail_watcher_label', text)
    assert re.search(r'"kind": "systemd-user", "unit": f"\{mail_watcher_label\}\.service"', text)


def _run_tmux_fallback(tmp: pathlib.Path, pid: int) -> str:
    fake = tmp / "fake-bin"
    fake.mkdir()
    log = tmp / "tmux.log"
    tmux = fake / "tmux"
    tmux.write_text(
        f'#!/bin/sh\nprintf "tmux %s\\n" "$*" >> {log}\n[ "$1" = has-session ] && exit 1\nexit 0\n',
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    hooks = tmp / "hooks"
    hooks.mkdir()
    (hooks / "watch_agent_mail_signals.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    lock = tmp / "lock"
    lock.mkdir()
    (lock / "watcher.pid").write_text(f"{pid}\n", encoding="utf-8")
    script = tmp / "run.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"export AGENTSTACK_MAIL_WATCHER_PIDFILE='{lock}/watcher.pid'\n"
        f"eval \"$(sed -n '/^ags_start_mail_watcher()/,/^}}/p' {REGISTER_LIB})\"\n"
        f"ags_start_mail_watcher '{tmux}' '{hooks}'\n",
        encoding="utf-8",
    )
    r = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_agent_start_does_not_start_a_second_watcher_when_one_holds_the_lock():
    with tempfile.TemporaryDirectory() as td:
        calls = _run_tmux_fallback(pathlib.Path(td), os.getpid())
    assert "new-session" not in calls, (
        "a service-managed watcher holds the lock; a tmux copy would only exit as a duplicate\n" + calls
    )


def test_agent_start_still_starts_the_fallback_when_the_lock_is_stale():
    with tempfile.TemporaryDirectory() as td:
        # A pid that cannot be alive on this host: beyond the kernel's range.
        calls = _run_tmux_fallback(pathlib.Path(td), 2**31 - 2)
    assert "new-session -d -s mail-watcher" in calls, calls


def _load_server():
    env_backup = dict(os.environ)
    os.environ.setdefault("AGENTSTACK_TERMINAL", "none")
    try:
        spec = importlib.util.spec_from_file_location(
            f"agentstack_server_watcher_test_{id(SERVER)}", SERVER
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        module_name = spec.name
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            return module
        finally:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


def test_dashboard_health_counts_a_systemd_watcher():
    server = _load_server()
    calls = []

    class _Done:
        def __init__(self, stdout, returncode=0):
            self.stdout, self.returncode = stdout, returncode

    def fake_run(argv, **_):
        calls.append(argv)
        return _Done("active\n")

    original_run, original_platform = server.subprocess.run, server.sys.platform
    try:
        server.subprocess.run = fake_run
        server.sys.platform = "linux"
        assert server._systemd_user_unit_running("x.mail-watcher.service") is True
        server.sys.platform = "darwin"
        assert server._systemd_user_unit_running("x.mail-watcher.service") is False
    finally:
        server.subprocess.run, server.sys.platform = original_run, original_platform
    assert calls == [["systemctl", "--user", "is-active", "x.mail-watcher.service"]]
    health = SERVER.read_text(encoding="utf-8")
    assert "watcher_launchd or watcher_systemd or watcher_pidfile" in health
    assert 'result["watcher_mode"] = "systemd-user"' in health


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as error:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {error}")
    sys.exit(1 if failures else 0)


WATCHER = ROOT / "hooks" / "watch_agent_mail_signals.sh"


def _acquire_lock_against(lock: pathlib.Path, pid: int) -> "subprocess.CompletedProcess[str]":
    """Run the watcher's real acquire_lock() against a pre-populated lock dir."""
    funcs = " ".join(
        f"/^{name}()/,/^}}/p;" for name in ("log", "is_pid_running", "write_heartbeat", "acquire_lock")
    )
    script = lock.parent / "acquire.sh"
    script.write_text(
        "set -euo pipefail\n"
        "LOCK_ACQUIRED=0\n"
        f"WATCHER_LOCK_DIR='{lock}'\n"
        f"WATCHER_PIDFILE='{lock}/watcher.pid'\n"
        f"WATCHER_HEARTBEAT='{lock}/heartbeat'\n"
        f"eval \"$(sed -n '{funcs}' {WATCHER})\"\n"
        "acquire_lock\n"
        "echo acquired=$LOCK_ACQUIRED\n",
        encoding="utf-8",
    )
    return subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=60)


def test_watcher_takes_over_a_stale_lock_that_still_holds_a_heartbeat():
    # A watcher killed without its EXIT trap (SIGKILL, host crash, a deploy that
    # replaced the process) leaves heartbeat + pidfile inside the lock dir.
    # 2026-09-11: takeover did `rmdir || true` then `mkdir`, so the non-empty dir
    # made every restart exit 1 and launchd relaunched it every 5 s for 32 hours.
    # Mail kept landing in inboxes; nothing was injected into any tmux session.
    with tempfile.TemporaryDirectory() as td:
        lock = pathlib.Path(td) / "lock"
        lock.mkdir()
        (lock / "watcher.pid").write_text(f"{2**31 - 2}\n", encoding="utf-8")
        (lock / "heartbeat").write_text("", encoding="utf-8")
        r = _acquire_lock_against(lock, os.getpid())
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Stale watcher lock detected" in r.stdout
        assert "acquired=1" in r.stdout
        assert (lock / "watcher.pid").read_text(encoding="utf-8").strip().isdigit()
        assert (lock / "heartbeat").exists()


def test_watcher_still_yields_to_a_live_holder():
    with tempfile.TemporaryDirectory() as td:
        lock = pathlib.Path(td) / "lock"
        lock.mkdir()
        (lock / "watcher.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        (lock / "heartbeat").write_text("", encoding="utf-8")
        r = _acquire_lock_against(lock, os.getpid())
        assert r.returncode == 0, r.stdout + r.stderr
        assert "already running" in r.stdout
        assert (lock / "watcher.pid").read_text(encoding="utf-8").strip() == str(os.getpid())
