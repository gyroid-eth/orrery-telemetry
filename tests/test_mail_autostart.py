#!/usr/bin/env python3
"""AgentStack Mail must come back after a reboot.

`agentstack-mailctl start` hands the server to `nohup` and exits, so nothing
re-launched it at login. The dashboard has had a launchd plist / systemd user
unit since the first installer; mail never did, and the gap is invisible until
the machine actually reboots — which is how it survived unnoticed until
2026-08-16, when a reboot on the maintainer's Mac came back with a dashboard, no
mail server, and a stale legacy service holding the port instead.

These assert the installer now registers an autostart unit for mail, that the
unit is shaped correctly for a controller that exits (one-shot, not KeepAlive),
and that the uninstaller can find it again.

Runnable two ways (no third-party dependency required):
    python3 tests/test_mail_autostart.py   # plain script, prints PASS/FAIL
    pytest tests/test_mail_autostart.py     # under pytest if available
"""
from __future__ import annotations

import os
import pathlib
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall.sh"
MAILCTL = ROOT / "bin" / "agentstack-mailctl"
LABEL_PREFIX = "org.agentstack.test.mail-autostart"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_command(directory: pathlib.Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(body, encoding="utf-8")
    command.chmod(0o755)


def _dry_run(tmp: pathlib.Path, platform: str) -> subprocess.CompletedProcess:
    """Run the installer in dry-run mode pretending to be `platform`."""
    home = tmp / "home"
    home.mkdir()
    project = tmp / "project"
    project.mkdir()
    fake_bin = tmp / "fake-bin"
    fake_bin.mkdir()
    _write_command(fake_bin, "uname", f"#!/bin/sh\nprintf '%s\\n' {'Linux' if platform.startswith('Linux') else platform}\n")
    if platform == "Linux":
        _write_command(fake_bin, "systemctl", "#!/bin/sh\nexit 0\n")
    # platform == "LinuxNoSystemd": uname says Linux and no systemctl stub is
    # written, so `command -v systemctl` fails and the autostart has nowhere to
    # register. (An unknown kernel is rejected by preflight before this point.)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "PATH": f"{fake_bin}:{env['PATH']}",
        "AGENTSTACK_HOME": str(home / ".agentstack"),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_TERMINAL": "none",
        "AGENTSTACK_LABEL_PREFIX": LABEL_PREFIX,
        "AGENTSTACK_PYTHON": sys.executable,
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
        "AGENTSTACK_PORT": str(_free_port()),
    })
    return subprocess.run(
        ["/bin/bash", str(INSTALLER), "--dashboard-only", "--dry-run"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=600,
    )


def test_macos_install_plans_a_launchd_autostart_for_mail():
    with tempfile.TemporaryDirectory() as td:
        r = _dry_run(pathlib.Path(td), "Darwin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{LABEL_PREFIX}.mail.plist" in r.stdout, (
        "the installer did not plan a launchd autostart unit for AgentStack Mail; "
        "without it `mailctl start`'s nohup process is lost at reboot\n" + r.stdout
    )
    assert "AgentStack Mail autostart" in r.stdout, r.stdout
    assert f"launchctl bootstrap" in r.stdout, r.stdout


def test_linux_install_plans_a_systemd_autostart_for_mail():
    with tempfile.TemporaryDirectory() as td:
        r = _dry_run(pathlib.Path(td), "Linux")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{LABEL_PREFIX}.mail.service" in r.stdout, r.stdout


def test_a_host_without_a_service_manager_says_mail_will_not_restart():
    """A missing autostart must be announced, not silently skipped."""
    with tempfile.TemporaryDirectory() as td:
        r = _dry_run(pathlib.Path(td), "LinuxNoSystemd")
    combined = r.stdout + r.stderr
    assert r.returncode == 0, combined
    assert "will NOT restart after a reboot" in combined, combined


# --- the shape of the unit ---------------------------------------------------

def _render_unit(kind: str, tmp: pathlib.Path) -> pathlib.Path:
    """Call the installer's renderer directly, without running an install."""
    home = tmp / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    script = tmp / "render.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"AGENTSTACK_LABEL_PREFIX=\'{LABEL_PREFIX}\'\n"
        "DRY_RUN=false\n"
        f"HOME='{home}'\n"
        f"INSTALL_DIR='{home}/.agentstack'\n"
        f"BIN_DIR='{home}/.agentstack/bin'\n"
        f"PYTHON_BIN=\'{sys.executable}\'\n"
        "PATH_VALUE=/usr/bin:/bin\n"
        f"NATIVE_MAIL_SERVICE_ROOT='{home}/mail-service'\n"
        f"NATIVE_MAIL_ENV='{home}/mail-service/service.env'\n"
        f"NATIVE_MAIL_RUNNER='{home}/mail-service/run.sh'\n"
        f"NATIVE_MAIL_PIDFILE='{home}/mail-service/mail.pid'\n"
        f"NATIVE_MAIL_LOG='{home}/mail-service/mail.log'\n"
        f"MAIL_DB='{home}/mail-service/storage.sqlite3'\n"
        "MCP_URL=http://127.0.0.1:18765/mcp\n"
        f"MAIL_AUTOSTART_LABEL={LABEL_PREFIX}.mail\n"
        "AGENT_MAIL_AUTOSTART_PATH=\n"
        "AGENT_MAIL_AUTOSTART_SERVICE_PATH=\n"
        "plan() { :; }\n"
        # Pull in just the two functions under test.
        f"eval \"$(sed -n '/^mail_autostart_environment()/,/^}} # end mail_autostart_environment/p' {INSTALLER})\"\n"
        f"eval \"$(sed -n '/^render_mail_autostart_unit()/,/^}} # end render_mail_autostart_unit/p' {INSTALLER})\"\n"
        f"render_mail_autostart_unit {kind}\n"
        "printf '%s\\n%s\\n' \"$AGENT_MAIL_AUTOSTART_PATH\" "
        "\"${AGENT_MAIL_AUTOSTART_SERVICE_PATH:-}\"\n",
        encoding="utf-8",
    )
    out = subprocess.run(["/bin/bash", str(script)], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = [l for l in out.stdout.splitlines() if l.strip()]
    return pathlib.Path(lines[-2] if len(lines) >= 2 and lines[-1] else lines[-1])


def _render_systemd_pair(tmp: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """systemd needs two files: the oneshot service and the timer that re-runs it."""
    timer = _render_unit("systemd-user", tmp)
    service = timer.with_suffix(".service")
    return timer, service


def test_launchd_unit_is_one_shot_and_calls_mailctl():
    with tempfile.TemporaryDirectory() as td:
        path = _render_unit("launchd", pathlib.Path(td))
        plist = plistlib.loads(path.read_bytes())
    assert plist["Label"] == f"{LABEL_PREFIX}.mail", plist["Label"]
    assert plist["ProgramArguments"][-1] == "start", plist["ProgramArguments"]
    assert plist["ProgramArguments"][0].endswith("agentstack-mailctl"), plist["ProgramArguments"]
    assert plist["RunAtLoad"] is True, plist
    assert plist["KeepAlive"] is False, (
        "mailctl exits after handing the server to nohup; KeepAlive would respawn "
        "the controller in a loop instead of supervising the server"
    )
    interval = plist.get("StartInterval")
    assert isinstance(interval, int) and 60 <= interval <= 900, (
        f"StartInterval={interval!r} is not a liveness sweep; a year-long "
        "interval satisfies a truthiness check and supervises nothing"
    )
    assert interval, (
        "RunAtLoad alone only covers login. Without a periodic re-check a runner "
        "killed mid-session stays dead until the next reboot, and a port that was "
        "briefly contended at login is never retried. (systemd gets this from its "
        "timer unit.)"
    )
    env = plist["EnvironmentVariables"]
    # Enough for mailctl to find its own configuration; see
    # test_the_unit_lets_mailctl_read_env_sh for why nothing more belongs here.
    for key in ("HOME", "AGENTSTACK_HOME", "PATH"):
        assert key in env, (key, sorted(env))


def test_systemd_unit_is_one_shot_and_calls_mailctl():
    with tempfile.TemporaryDirectory() as td:
        _, service = _render_systemd_pair(pathlib.Path(td))
        text = service.read_text(encoding="utf-8")
    assert "Type=oneshot" in text, text
    assert "RemainAfterExit" not in text, (
        "a oneshot left active after exiting is never re-run by its timer"
    )
    assert re.search(r"^ExecStart=.*agentstack-mailctl\"? start$", text, re.M), text
    assert "After=default.target" not in text, (
        "`systemctl --user enable` installs this into default.target.wants/, and "
        "systemd completes that Wants with default.target After=<unit>. Ordering "
        "against default.target as well is an ordering cycle, which systemd "
        "resolves by dropping a job — so mail may simply not start."
    )
    assert re.search(r'^Environment=AGENTSTACK_HOME="?/', text, re.M), text


def test_the_unit_lets_mailctl_read_env_sh():
    """The unit must not bake the mail paths in.

    `agentstack-mailctl` resolves the service env, runner, pidfile and database
    from $AGENTSTACK_HOME/env.sh. A unit that hard-codes them instead would keep
    starting the previous render after a re-install — and because nothing reads
    the unit until the next reboot, the drift would surface days later as "mail
    came back pointing at the wrong database".
    """
    with tempfile.TemporaryDirectory() as td:
        path = _render_unit("launchd", pathlib.Path(td))
        plist = plistlib.loads(path.read_bytes())
    env = plist["EnvironmentVariables"]
    assert "AGENTSTACK_MAILCTL_SKIP_ENV" not in env, (
        "skipping env.sh makes the unit depend on values frozen at install time"
    )
    assert env.get("AGENTSTACK_HOME"), env
    assert env.get("HOME"), env
    # AGENTSTACK_MAILCTL_SWEEP is a mode flag, not configuration: it tells the
    # controller "this is the periodic sweep", which is how an explicit stop is
    # respected and how the sweep stays quiet. Everything else must come from
    # env.sh.
    baked = [k for k in env
             if k.startswith("AGENTSTACK_MAIL") and k != "AGENTSTACK_MAILCTL_SWEEP"]
    assert not baked, f"these belong in env.sh, not in the unit: {baked}"
    assert env.get("AGENTSTACK_MAILCTL_SWEEP") == "1", (
        "without the sweep flag the trigger would restart a server the operator "
        "deliberately stopped, and would log a line every five minutes"
    )


def test_autostart_is_registered_even_when_mail_is_already_running():
    """Every existing user re-running install.sh hits the already-running path.

    ensure_native_agentstack_mail() returns early when a healthy server is found,
    so registering the autostart there would have skipped exactly the population
    that needs it. It must be wired into the main flow instead.
    """
    installer = INSTALLER.read_text(encoding="utf-8")
    body = installer[installer.index("ensure_native_agentstack_mail() {"):]
    body = body[:body.index("\n}\n")]
    assert "enable_mail_autostart" not in body, (
        "enable_mail_autostart sits behind the early return for an existing "
        "server; users updating an existing install would never get it"
    )
    assert re.search(r"write_env_file\n(?:.*\n)*?\s*enable_mail_autostart", installer), (
        "enable_mail_autostart must run in the main flow, after write_env_file "
        "(the unit invokes mailctl, which reads env.sh)"
    )


def test_uninstall_removes_the_unit_file_not_just_the_job():
    """Booting the job out is not enough: the file re-registers at next login.

    uninstall.sh unlinks manifest["owned_files"] but its service loop only calls
    `launchctl bootout` / `systemctl disable`. The dashboard's own unit path is
    recorded in owned_files; the mail trigger has to be too. (Reproduced by
    review with a synthetic manifest: after uninstall the unit file was still
    on disk.)
    """
    installer = INSTALLER.read_text(encoding="utf-8")
    block = installer[installer.index("owned_files = []"):installer.index("owned_dirs =")]
    assert "owned_files.append(mail_autostart_path)" in block, (
        "the autostart unit file is not installer-owned, so uninstall leaves it behind"
    )


def test_uninstall_understands_the_autostart_service_entry():
    """uninstall.sh iterates manifest['services']; the new entry must fit it."""
    text = UNINSTALLER.read_text(encoding="utf-8")
    assert 'kind == "launchd"' in text and 'kind == "systemd-user"' in text, (
        "uninstall.sh no longer handles these service kinds; the autostart unit "
        "would be left behind after an uninstall"
    )
    installer = INSTALLER.read_text(encoding="utf-8")
    assert '"role": "agent-mail-autostart"' in installer, (
        "the installer does not record the autostart unit in the manifest, so "
        "uninstall.sh cannot remove it"
    )


# --- the already-running service must be adopted, not overwritten -------------

def _call_installer_function(body: str, tmp: pathlib.Path) -> subprocess.CompletedProcess:
    """Run a snippet with named installer functions pulled in via their sentinels."""
    script = tmp / "call.sh"
    script.write_text("set -euo pipefail\n" + body, encoding="utf-8")
    return subprocess.run(["/bin/bash", str(script)], capture_output=True,
                          text=True, timeout=120)


def test_an_existing_service_env_is_adopted_from_the_running_runner():
    """env.sh must describe the service that is running, not one this run planned.

    Reported by review (PeachEinstein, 2026-08-16) from an isolated full install
    against a healthy listener: install exited 0 and announced the login trigger,
    but the recorded env path did not exist and `agentstack-mailctl start` failed
    with "service env is missing".

    The pidfile here is written by agentstack-mailctl's own `write_pid`, not by a
    hand-rolled fixture. The first version of this test invented a one-line
    pidfile; the real one is two lines (pid, then runner), so the implementation
    it "proved" could never have worked in production. Generating the fixture
    with the real writer removes that whole class of lie.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        live = tmp / "renders" / "live"
        live.mkdir(parents=True)
        (live / "service.env").write_text("# live\n", encoding="utf-8")
        runner = live / "run-agentstack-mail.sh"
        runner.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
        runner.chmod(0o755)
        proc = subprocess.Popen(["/bin/bash", str(runner)])
        try:
            pidfile = tmp / "mail.pid"
            stale = tmp / "renders" / "planned" / "service.env"
            r = _call_installer_function(
                f"PIDFILE=\'{pidfile}\'\n"
                f"MAIL_RUNNER=\'{runner}\'\n"
                f"eval \"$(sed -n '/^write_pid()/,/^}}$/p' {MAILCTL})\"\n"
                f"write_pid {proc.pid}\n"
                f"NATIVE_MAIL_PIDFILE=\'{pidfile}\'\n"
                f"NATIVE_MAIL_ENV=\'{stale}\'\n"
                f"NATIVE_MAIL_RUNNER={stale.parent}/run-agentstack-mail.sh\n"
                f"MAIL_ENV=\'{stale}\'\n"
                f"INSTALL_DIR={tmp}/agentstack\n"
                "say() { :; }\n"
                f"eval \"$(sed -n '/^adopt_running_native_mail_render()/,"
                f"/^}} # end adopt_running_native_mail_render/p' {INSTALLER})\"\n"
                "adopt_running_native_mail_render\n"
                'printf "%s\\n" "$MAIL_ENV"\n',
                tmp,
            )
            pidfile_lines = pidfile.read_text(encoding="utf-8").splitlines()
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(pidfile_lines) == 2, (
        f"the controller's pidfile format changed: {pidfile_lines}"
    )
    adopted = r.stdout.strip().splitlines()[-1]
    assert adopted == str(live / "service.env"), (
        f"the running service env was not adopted; env.sh would record {adopted}"
    )


def test_no_trigger_is_registered_when_the_service_env_is_missing():
    """A unit that fails only at boot is worse than no unit at all."""
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        home = tmp / "home"
        home.mkdir()
        r = _call_installer_function(
            "DRY_RUN=false\n"
            f"HOME='{home}'\n"
            f"MAIL_ENV={tmp}/missing/service.env\n"
            f"BIN_DIR='{home}/.agentstack/bin'\n"
            f"MAIL_AUTOSTART_LABEL={LABEL_PREFIX}.mail\n"
            "AGENT_MAIL_AUTOSTART_KIND=\n"
            "AGENT_MAIL_AUTOSTART_PATH=\n"
            "warn() { printf 'WARN %s\\n' \"$*\"; }\n"
            "say() { :; }\n"
            "plan() { :; }\n"
            f"eval \"$(sed -n '/^enable_mail_autostart()/,/^}}$/p' {INSTALLER})\"\n"
            "enable_mail_autostart\n"
            'printf "KIND=%s\\n" "$AGENT_MAIL_AUTOSTART_KIND"\n',
            tmp,
        )
        # Check inside the context: after the `with` block the whole tree is
        # gone, so "no unit was written" would be true no matter what happened.
        # (Caught by review, 2026-08-16.)
        wrote_a_unit = (home / "Library" / "LaunchAgents").exists() or \
                       (home / ".config" / "systemd").exists()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "service env is missing" in r.stdout, r.stdout
    assert "KIND=" in r.stdout and "KIND=launchd" not in r.stdout, r.stdout
    assert not wrote_a_unit, "a login trigger was written despite the missing env"


# --- the registration must actually happen ------------------------------------
#
# Every test above passes against an implementation that sets
# AGENT_MAIL_AUTOSTART_KIND, prints "will restart at login" and returns without
# ever calling launchctl/systemctl — demonstrated by review with a mutant on
# 2026-08-16, 11/11 green. String plans and rendered shapes are not registration.

def _fake_manager_bin(tmp: pathlib.Path, platform: str, *, fail: bool = False) -> tuple[pathlib.Path, pathlib.Path]:
    """An isolated PATH holding only the commands the installer may call."""
    fake = tmp / "fake-bin"
    fake.mkdir()
    log = tmp / "manager.log"
    _write_command(fake, "uname", f"#!/bin/sh\nprintf '%s\\n' {platform}\n")
    for name in ("launchctl", "systemctl"):
        _write_command(fake, name,
            "#!/bin/sh\n"
            f'printf "%s %s\\n" {name} "$*" >> {log}\n'
            + ("exit 1\n" if fail else "exit 0\n"))
    # Real utilities the renderer needs, resolved from the host but reachable
    # only through this directory so nothing else leaks in.
    for name in ("sed", "cat", "mkdir", "rm", "dirname", "basename", "printf",
                 "id", "python3", "awk", "ps", "kill", "mv", "cp", "chmod"):
        real = shutil.which(name)
        if real:
            (fake / name).symlink_to(real)
    return fake, log


def _run_enable(tmp: pathlib.Path, platform: str, *, fail: bool = False) -> tuple[subprocess.CompletedProcess, str]:
    home = tmp / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "systemd" / "user").mkdir(parents=True, exist_ok=True)
    mail_env = tmp / "service.env"
    mail_env.write_text("# live\n", encoding="utf-8")
    fake, log = _fake_manager_bin(tmp, platform, fail=fail)
    script = tmp / "enable.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"export PATH={fake}\n"
        "DRY_RUN=false\n"
        f"HOME='{home}'\n"
        f"INSTALL_DIR='{home}/.agentstack'\n"
        f"BIN_DIR='{home}/.agentstack/bin'\n"
        f"PYTHON_BIN=\'{sys.executable}\'\n"
        "PATH_VALUE=/usr/bin:/bin\n"
        f"MAIL_ENV='{mail_env}'\n"
        f"NATIVE_MAIL_LOG='{tmp}/mail.log'\n"
        f"NATIVE_MAIL_SERVICE_ROOT='{tmp}/mail-service'\n"
        "AGENT_MAIL_AUTOSTART_SERVICE_PATH=\n"
        f"MAIL_AUTOSTART_LABEL={LABEL_PREFIX}.mail\n"
        "AGENT_MAIL_AUTOSTART_KIND=\n"
        "AGENT_MAIL_AUTOSTART_PATH=\n"
        "say() { printf 'SAY %s\\n' \"$*\"; }\n"
        "warn() { printf 'WARN %s\\n' \"$*\"; }\n"
        "plan() { :; }\n"
        f"eval \"$(sed -n '/^mail_autostart_environment()/,/^}} # end mail_autostart_environment/p' {INSTALLER})\"\n"
        f"eval \"$(sed -n '/^render_mail_autostart_unit()/,/^}} # end render_mail_autostart_unit/p' {INSTALLER})\"\n"
        f"eval \"$(sed -n '/^enable_mail_autostart()/,/^}}$/p' {INSTALLER})\"\n"
        "enable_mail_autostart\n"
        'printf "KIND=%s\\nPATH_OUT=%s\\n" "$AGENT_MAIL_AUTOSTART_KIND" "$AGENT_MAIL_AUTOSTART_PATH"\n',
        encoding="utf-8",
    )
    r = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=180)
    return r, (log.read_text(encoding="utf-8") if log.exists() else "")


def test_launchd_registration_actually_calls_launchctl():
    with tempfile.TemporaryDirectory() as td:
        r, calls = _run_enable(pathlib.Path(td), "Darwin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KIND=launchd" in r.stdout, r.stdout
    assert "launchctl bootstrap" in calls, f"the job was never registered:\n{calls}"
    assert f"launchctl enable gui/" in calls and f"{LABEL_PREFIX}.mail" in calls, (
        "the wrong label would register a job nobody triggers\n" + calls
    )


def test_systemd_registration_actually_calls_systemctl():
    with tempfile.TemporaryDirectory() as td:
        r, calls = _run_enable(pathlib.Path(td), "Linux")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KIND=systemd-user" in r.stdout, r.stdout
    assert "systemctl --user daemon-reload" in calls, calls
    # The unit *and* the activation both matter: enabling the service instead of
    # the timer, or enabling without --now, leaves nothing sweeping until the
    # next login. Both mutants passed an earlier version of this test.
    assert f"systemctl --user enable --now {LABEL_PREFIX}.mail.timer" in calls, (
        "the timer was not enabled-and-started\n" + calls
    )
    assert f"systemctl --user disable {LABEL_PREFIX}.mail.service" in calls, (
        "an older install enabled the service directly; that symlink must be "
        "cleared or it keeps firing alongside the timer\n" + calls
    )


def test_a_failed_registration_cleans_up_and_reports():
    """A half-registered trigger must not be left claiming success."""
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        r, calls = _run_enable(tmp, "Darwin", fail=True)
        leftover = list((tmp / "home" / "Library" / "LaunchAgents").glob("*.plist"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KIND=" in r.stdout and "KIND=launchd" not in r.stdout, r.stdout
    assert "WARN" in r.stdout and "will NOT restart" in r.stdout, r.stdout
    assert "launchctl bootout" in calls, calls
    assert not leftover, f"a unit file survived a failed registration: {leftover}"


def test_a_failed_registration_keeps_the_previous_trigger():
    """Re-registering must not cost the user the trigger they already had.

    The renderer overwrites the unit file before the manager is called, so a
    failed bootstrap would leave neither the old trigger nor the new one — and
    nothing would notice until the next reboot. (Raised by review, 2026-08-16.)
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        existing = tmp / "home" / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.mail.plist"
        existing.parent.mkdir(parents=True)
        existing.write_text("PREVIOUS-UNIT", encoding="utf-8")
        r, _ = _run_enable(tmp, "Darwin", fail=True)
        kept = existing.exists() and existing.read_text(encoding="utf-8") == "PREVIOUS-UNIT"
        residue = list(existing.parent.glob("*.prev"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert kept, "the previously working login trigger was destroyed by a failed re-registration"
    assert not residue, f"backup left behind: {residue}"


def test_the_unit_carries_the_real_paths_not_just_the_right_keys():
    """Asserting key names alone passes with HOME=/definitely/wrong."""
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        path = _render_unit("launchd", tmp)
        env = plistlib.loads(path.read_bytes())["EnvironmentVariables"]
    assert env["HOME"] == str(tmp / "home"), env
    assert env["AGENTSTACK_HOME"] == str(tmp / "home" / ".agentstack"), env


def test_both_platforms_log_to_the_dedicated_autostart_log():
    """`autostart_log` must actually be wired, on both platforms.

    Pointing it at /definitely/wrong/... passed the whole suite, and the systemd
    unit had no StandardOutput at all while the docs claimed a dedicated log.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        expected = str(tmp / "home" / "mail-service" / "runtime"
                       / "agentstack-mail-autostart.log")
        plist = plistlib.loads(_render_unit("launchd", tmp).read_bytes())
    with tempfile.TemporaryDirectory() as td2:
        tmp2 = pathlib.Path(td2)
        expected2 = str(tmp2 / "home" / "mail-service" / "runtime"
                        / "agentstack-mail-autostart.log")
        _, service = _render_systemd_pair(tmp2)
        unit_text = service.read_text(encoding="utf-8")
    assert plist["StandardOutPath"] == expected, (plist.get("StandardOutPath"), expected)
    assert plist["StandardErrorPath"] == expected, plist.get("StandardErrorPath")
    assert f"StandardOutput=append:{expected2}" in unit_text, unit_text
    assert f"StandardError=append:{expected2}" in unit_text, unit_text


def test_systemd_timer_re_runs_the_oneshot():
    """A oneshot service is never repeated on its own; the timer is the sweep."""
    with tempfile.TemporaryDirectory() as td:
        timer, service = _render_systemd_pair(pathlib.Path(td))
        text = timer.read_text(encoding="utf-8")
        # Inside the block: after it the tree is gone and exists() is always False.
        service_written = service.exists()
    assert timer.name.endswith(".timer"), timer
    assert service_written, service
    assert "OnBootSec=" in text, text
    assert "OnUnitActiveSec=" in text, (
        "without a repeat interval this is a boot trigger, not a liveness sweep\n" + text
    )
    assert "WantedBy=timers.target" in text, text


def test_systemd_unit_survives_a_path_containing_spaces():
    """systemd splits unquoted values on whitespace."""
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td) / "a path with spaces"
        tmp.mkdir()
        _, service = _render_systemd_pair(tmp)
        text = service.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("Environment=", "ExecStart=")) and " " in line:
            value = line.split("=", 1)[1]
            if line.startswith("ExecStart="):
                value = value.rsplit(" start", 1)[0]
            else:
                value = value.split("=", 1)[1]
            assert value.startswith('"') and value.endswith('"'), (
                f"unquoted value would be split by systemd: {line}"
            )


# --- the sweep must not undo a deliberate stop --------------------------------

MAILCTL = ROOT / "bin" / "agentstack-mailctl"


def _mailctl_env(tmp: pathlib.Path) -> dict:
    runtime = tmp / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    service_env = tmp / "service.env"
    service_env.write_text("# test\n", encoding="utf-8")
    runner = tmp / "run-agentstack-mail.sh"
    runner.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
    runner.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "AGENTSTACK_MAILCTL_SKIP_ENV": "1",
        "AGENTSTACK_MAIL_PROVIDER": "agentstack",
        "AGENTSTACK_MAIL_DIR": str(tmp),
        "AGENTSTACK_MAIL_ENV": str(service_env),
        "AGENTSTACK_MAIL_RUNNER": str(runner),
        "AGENTSTACK_MAIL_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_MCP_URL": f"http://127.0.0.1:{_free_port()}/mcp",
        "AGENTSTACK_PYTHON": sys.executable,
    })
    return env


def test_the_sweep_leaves_a_deliberately_stopped_server_alone():
    """Reported by review with a live end-to-end run:

        STOP_RESULT 0 AgentStack Mail stopped
        SIMULATED_SWEEP_START 0 AgentStack Mail started (pid 39426, ...)

    A trigger that runs `start` every five minutes silently undoes an operator's
    `stop`. The controller now records the intent and the sweep honours it.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        env = _mailctl_env(tmp)
        stop = subprocess.run(["/bin/bash", str(MAILCTL), "stop"],
                              capture_output=True, text=True, env=env, timeout=120)
        marker = tmp / "runtime" / "agentstack-mail.stopped"
        marker_written = marker.exists()
        sweep_env = dict(env)
        sweep_env["AGENTSTACK_MAILCTL_SWEEP"] = "1"
        sweep = subprocess.run(["/bin/bash", str(MAILCTL), "start"],
                               capture_output=True, text=True, env=sweep_env, timeout=120)
        # An operator's own `start` is intent too: it must clear the hold.
        manual = subprocess.run(["/bin/bash", str(MAILCTL), "start"],
                                capture_output=True, text=True, env=env, timeout=180)
        marker_cleared = not marker.exists()
    assert marker_written, (stop.stdout, stop.stderr)
    assert sweep.returncode == 0, sweep.stderr
    assert "held down by an explicit stop" in sweep.stderr, (sweep.stdout, sweep.stderr)
    assert "started" not in sweep.stdout, (
        "the sweep restarted a server the operator stopped\n" + sweep.stdout)
    assert marker_cleared, "an explicit start must release the hold"
    # `manual` may fail for lack of a real server; what matters is that it tried.
    assert "held down" not in manual.stderr, manual.stderr


def test_the_sweep_is_quiet_when_there_is_nothing_to_do():
    """It runs ~105k times a year; a line each time is a log that eats itself."""
    source = MAILCTL.read_text(encoding="utf-8")
    assert 'SWEEP" == "1" ]] || say "AgentStack Mail already running' in source, (
        "the 'already running' line is not suppressed for the periodic sweep"
    )


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
