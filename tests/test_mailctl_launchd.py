"""`agentstack-mailctl` must be able to manage a launchd-supervised server.

Reported by a tester on 2026-08-17: `status`, `stop` and `restart` all died with
"endpoint is occupied without a live managed pid" against a healthy server. The
plist runs `agentstack-mail-service foreground ...` directly, while the
controller only recognises a process started through `run-agentstack-mail.sh`,
so its ownership guard could never match the launchd instance and the documented
CLI was unusable -- operators had to fall back to raw launchctl.

The controller now defers to launchd where launchd is the supervisor. Signalling
launchd's child behind its back is what made "who supervises this" ambiguous in
the first place.
"""

from __future__ import annotations

import http.server
import json
import os
import plistlib
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAILCTL = REPO_ROOT / "bin" / "agentstack-mailctl"
LABEL = "org.orrery.mail"

# The job reports the pid that actually holds the endpoint, because that is
# what ownership means. A fake that invents a pid would let the controller
# claim a server it does not supervise -- which is what the first version of
# these tests did.
FAKE_LAUNCHCTL = """#!/bin/bash
echo "$@" >> "$LAUNCHCTL_LOG"
case "$1" in
  print)
    if [[ -n "${APPEAR_AFTER_N_PRINTS:-}" ]]; then
      count=$(( $(cat "$PRINT_COUNT" 2>/dev/null || echo 0) + 1 ))
      echo "$count" > "$PRINT_COUNT"
      if (( count >= APPEAR_AFTER_N_PRINTS )) && [[ ! -f "$LOADED_MARKER" ]]; then
        touch "$LOADED_MARKER"
        [[ -n "${REAPPEAR_SERVING:-}" ]] && touch "$SERVING_MARKER"
      fi
      if [[ ! -f "$LOADED_MARKER" ]]; then exit 113; fi
    fi
    if [[ -n "${APPEAR_AFTER_FIRST_PRINT:-}" && ! -f "$LOADED_MARKER" ]]; then
      # The job loads between the controller's two questions.
      touch "$LOADED_MARKER"
      [[ -n "${REAPPEAR_SERVING:-}" ]] && touch "$SERVING_MARKER"
      exit 113
    fi
    if [[ -f "$PENDING_UNLOAD" ]]; then
      # bootout is asynchronous: the job outlives the call for a moment.
      rm -f "$PENDING_UNLOAD" "$LOADED_MARKER" "$SERVING_MARKER"
      echo "	path = $PLIST_PATH"
      echo "	program = $SERVICE_PROGRAM"
      echo "	pid = $(cat "$LISTENER_PID_FILE")"
      exit 0
    fi
    if [[ -f "$LOADED_MARKER" ]]; then
      [[ -z "${SUPPRESS_PLIST_PATH:-}" ]] && echo "	path = $PLIST_PATH"
      echo "	program = ${JOB_PROGRAM:-$SERVICE_PROGRAM}"
      if [[ -n "${JOB_ARGUMENT:-}" ]]; then
        echo "	arguments = {"
        echo "		0 = ${JOB_PROGRAM:-$SERVICE_PROGRAM}"
        echo "		1 = $JOB_ARGUMENT"
        echo "	}"
      fi
      echo "	pid = ${FORCED_JOB_PID:-$(cat "$LISTENER_PID_FILE")}"
      exit 0
    fi
    exit 113
    ;;
  bootout)
    if [[ -n "${BOOTOUT_IS_ASYNC:-}" ]]; then
      touch "$PENDING_UNLOAD"
      exit 0
    fi
    rm -f "$LOADED_MARKER" "$SERVING_MARKER"
    exit 0
    ;;
  enable)
    touch "$ENABLED_MARKER"
    exit 0
    ;;
  bootstrap)
    if [[ -n "${REQUIRE_ENABLE_BEFORE_BOOTSTRAP:-}" && ! -f "$ENABLED_MARKER" ]]; then
      exit 5
    fi
    touch "$LOADED_MARKER" "$SERVING_MARKER"
    exit 0
    ;;
  kickstart)
    touch "$SERVING_MARKER"
    exit 0
    ;;
esac
exit 0
"""


class _MailHandler(http.server.BaseHTTPRequestHandler):
    serving_marker: Path

    database_path: Path

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self.serving_marker.exists():
            self.send_response(503)
            self.end_headers()
            return
        # health_ok checks the reported database, not just the status.
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "status": "ok",
                    "database_url": f"sqlite+aiosqlite:///{self.database_path}",
                }
            },
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(406 if self.serving_marker.exists() else 503)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture()
def harness(tmp_path: Path):
    serving = tmp_path / "serving"
    serving.touch()
    _MailHandler.serving_marker = serving
    _MailHandler.database_path = tmp_path / "storage.sqlite3"
    server = http.server.HTTPServer(("127.0.0.1", 0), _MailHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    launchctl = fakebin / "launchctl"
    launchctl.write_text(FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o755)

    loaded = tmp_path / "loaded"
    loaded.touch()
    log = tmp_path / "launchctl.log"
    host, port = server.server_address[:2]
    # This process holds the listening socket, so it is the honest answer for
    # "which pid owns the endpoint".
    listener_pid_file = tmp_path / "listener.pid"
    listener_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    # A real plist: the controller reads Label and ProgramArguments through
    # plutil, so a placeholder document would exercise nothing.
    program = tmp_path / "agentstack-mail-service"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    plist = tmp_path / "org.orrery.mail.plist"
    plistlib.dump(
        {"Label": LABEL, "ProgramArguments": [str(program), "foreground"]},
        plist.open("wb"),
    )

    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "LAUNCHCTL_LOG": str(log),
        "LOADED_MARKER": str(loaded),
        "LISTENER_PID_FILE": str(listener_pid_file),
        "PLIST_PATH": str(plist),
        "SERVICE_PROGRAM": str(program),
        "PENDING_UNLOAD": str(tmp_path / "pending-unload"),
        "ENABLED_MARKER": str(tmp_path / "enabled"),
        "PRINT_COUNT": str(tmp_path / "print-count"),
        "SERVING_MARKER": str(serving),
        "AGENTSTACK_MAILCTL_SKIP_ENV": "1",
        "AGENTSTACK_MAIL_ENV": str(tmp_path / "service" / "env"),
        "AGENTSTACK_MAIL_DB": str(tmp_path / "storage.sqlite3"),
        "AGENTSTACK_MAIL_RUNTIME_DIR": str(tmp_path / "runtime"),
        "AGENTSTACK_MCP_URL": f"http://{host}:{port}/mcp",
        "AGENTSTACK_MAIL_LAUNCHD_LABEL": LABEL,
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "env").write_text("", encoding="utf-8")
    # A runner has to exist for start to reach the launchd checks at all;
    # without it the command dies earlier, for an unrelated reason.
    runner = tmp_path / "service" / "run-agentstack-mail.sh"
    runner.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    runner.chmod(0o755)
    (tmp_path / "storage.sqlite3").write_text("", encoding="utf-8")
    try:
        yield env, loaded, serving, log, server
    finally:
        server.shutdown()
        server.server_close()


def _log(log: Path) -> str:
    return log.read_text() if log.exists() else ""


def _mailctl(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(MAILCTL), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_status_recognises_the_launchd_supervised_server(harness) -> None:
    env, _loaded, _serving, _log, _server = harness
    result = _mailctl(env, "status")
    assert result.returncode == 0, result.stderr
    assert "running under launchd" in result.stdout
    assert LABEL in result.stdout
    assert "occupied without a live managed pid" not in result.stderr


def test_stop_asks_launchd_instead_of_signalling_its_child(harness) -> None:
    env, _loaded, _serving, log, _server = harness
    result = _mailctl(env, "stop")
    assert result.returncode == 0, result.stderr
    assert f"bootout gui/" in log.read_text()
    assert LABEL in log.read_text()
    assert "stopped" in result.stdout


def test_restart_is_one_launchctl_call(harness) -> None:
    env, _loaded, _serving, log, _server = harness
    result = _mailctl(env, "restart")
    assert result.returncode == 0, result.stderr
    calls = [line for line in log.read_text().splitlines() if line.startswith("kickstart")]
    assert any("-k" in call for call in calls), calls
    # A bootout would leave the job unloaded if the start half failed.
    assert "bootout" not in log.read_text()


def test_start_does_not_add_a_second_server_next_to_launchds(harness) -> None:
    env, _loaded, _serving, log, _server = harness
    result = _mailctl(env, "start")
    assert result.returncode == 0, result.stderr
    assert "already running under launchd" in result.stdout
    assert "run-agentstack-mail" not in log.read_text()


def test_without_launchd_the_controller_still_refuses_a_foreign_endpoint(
    harness,
) -> None:
    """The null case: the deference must not become a blanket 'assume it's fine'.

    With no launchd job loaded, an occupied endpoint is still someone else's
    process and the controller must say so rather than claim ownership.
    """
    env, loaded, _serving, _log, _server = harness
    loaded.unlink()
    result = _mailctl(env, "status")
    assert result.returncode != 0
    assert "occupied without a live managed pid" in result.stderr


def test_stop_then_start_puts_the_same_supervisor_back(harness) -> None:
    """A stop must not quietly demote the install to an unsupervised runner.

    After `stop` boots the job out, a `start` that falls through to the nohup
    path leaves a server launchd does not know about -- and launchd starts its
    own at the next login, so the machine ends up with two.
    """
    env, loaded, _serving, log, _server = harness
    stop = _mailctl(env, "stop")
    assert stop.returncode == 0, stop.stderr
    assert not loaded.exists()

    start = _mailctl(env, "start")
    assert start.returncode == 0, start.stderr
    assert "launchd" in start.stdout, start.stdout
    assert loaded.exists(), "the launchd job was not restored"
    assert "bootstrap" in _log(log)
    assert "run-agentstack-mail" not in _log(log)


def test_memo_restore_enables_a_disabled_label_before_bootstrap(harness) -> None:
    env, _loaded, _serving, log, _server = harness
    stop = _mailctl(env, "stop")
    assert stop.returncode == 0, stop.stderr
    env = {**env, "REQUIRE_ENABLE_BEFORE_BOOTSTRAP": "1"}

    start = _mailctl(env, "start")

    assert start.returncode == 0, start.stderr
    calls = _log(log).splitlines()
    enable = next(i for i, line in enumerate(calls) if line.startswith("enable "))
    bootstrap = next(i for i, line in enumerate(calls) if line.startswith("bootstrap "))
    assert enable < bootstrap, calls


def test_stop_boots_out_a_loaded_job_that_is_not_currently_serving(harness) -> None:
    """A loaded job with a closed endpoint is exactly what `stop` is for.

    Gating the bootout on an open port recorded the operator's intent and then
    left the job loaded, so the next login started it again.
    """
    env, loaded, serving, log, server = harness
    serving.unlink()
    # Really close the socket: with the port still open, gating the bootout on
    # an open endpoint would look identical to not gating it at all.
    server.shutdown()
    server.server_close()
    result = _mailctl(env, "stop")
    assert result.returncode == 0, result.stderr
    assert not loaded.exists(), "stop marker was written but the launchd job stayed loaded"
    assert "bootout" in _log(log)



def test_a_job_that_does_not_own_the_endpoint_is_not_claimed(harness) -> None:
    """A loaded label and an open port are two independent facts.

    Treating their coincidence as ownership lets the controller act on a server
    it does not supervise, whenever an unrelated job carries the expected label.
    """
    env, _loaded, _serving, _log, _server = harness
    env = {**env, "FORCED_JOB_PID": "1"}  # launchd reports a pid that is not the listener
    result = _mailctl(env, "status")
    assert result.returncode != 0
    assert "occupied without a live managed pid" in result.stderr


def test_stop_waits_for_an_asynchronous_bootout(harness) -> None:
    """`bootout` returns before the job is gone; one re-check is not enough."""
    env, loaded, _serving, log, _server = harness
    env = {**env, "BOOTOUT_IS_ASYNC": "1"}
    result = _mailctl(env, "stop")
    assert result.returncode == 0, result.stderr
    assert not loaded.exists()


@pytest.mark.parametrize("command", ("start", "stop", "restart", "status"))
def test_no_command_acts_on_a_job_that_is_not_ours(harness, command: str) -> None:
    """The guard belongs on every command, not only on `status`.

    A job carrying the expected label may belong to something else; `stop` would
    boot out a stranger's service and `restart` would kickstart it.
    """
    env, loaded, _serving, log, _server = harness
    env = {**env, "JOB_PROGRAM": "/Applications/Editor.app/Contents/MacOS/editor"}
    result = _mailctl(env, command)
    assert "bootout" not in _log(log), f"{command} booted out a foreign job"
    assert "kickstart" not in _log(log), f"{command} kickstarted a foreign job"
    assert loaded.exists(), f"{command} removed a foreign job"


def test_ownership_fails_closed_when_it_cannot_be_checked(harness) -> None:
    """No lsof means no proof. Refuse rather than assume."""
    env, _loaded, _serving, _log, _server = harness
    env = {**env, "PATH": str(Path(env["PATH"].split(":")[0])) + ":/usr/bin:/bin",
           "FORCED_JOB_PID": "1"}
    result = _mailctl(env, "status")
    assert result.returncode != 0, result.stdout


def test_a_tampered_restore_receipt_is_refused(harness, tmp_path: Path) -> None:
    """The receipt is an instruction to load a launchd job. Verify it first."""
    env, loaded, _serving, log, _server = harness
    stop = _mailctl(env, "stop")
    assert stop.returncode == 0, stop.stderr

    # Point the receipt at a plist that carries the right label but runs
    # something else entirely.
    intruder = tmp_path / "intruder.plist"
    plistlib.dump(
        {"Label": LABEL, "ProgramArguments": ["/Applications/Editor.app/Contents/MacOS/editor"]},
        intruder.open("wb"),
    )
    memo = Path(env["AGENTSTACK_MAIL_RUNTIME_DIR"]) / "agentstack-mail.launchd-plist"
    memo.write_text(f"label={LABEL}\nplist={intruder}\n", encoding="utf-8")

    result = _mailctl(env, "start")
    assert result.returncode != 0
    assert "does not define" in result.stderr
    assert "bootstrap" not in _log(log)


def test_start_refuses_when_a_foreign_job_holds_the_label(harness) -> None:
    """Two supervisors for one endpoint is a collision, not a free slot.

    Falling through to the unsupervised runner leaves the foreign job loaded;
    the conflict then surfaces at the next login instead of here.
    """
    env, loaded, serving, log, server = harness
    env = {**env, "JOB_PROGRAM": "/Applications/Editor.app/Contents/MacOS/editor"}
    serving.unlink()
    server.shutdown()
    server.server_close()
    result = _mailctl(env, "start")
    assert result.returncode != 0, result.stdout
    assert "not this service" in result.stderr
    assert loaded.exists()


def test_stop_refuses_when_launchd_reports_no_plist_path(harness) -> None:
    """No verified way back means no bootout."""
    env, loaded, _serving, log, _server = harness
    env = {**env, "SUPPRESS_PLIST_PATH": "1"}
    result = _mailctl(env, "stop")
    assert result.returncode != 0
    assert "refusing to stop" in result.stderr
    assert "bootout" not in _log(log)
    assert loaded.exists()


def test_stop_refuses_when_the_recorded_plist_is_gone(harness, tmp_path: Path) -> None:
    """A path that no longer exists cannot restore anything."""
    env, loaded, _serving, log, _server = harness
    (tmp_path / "org.orrery.mail.plist").unlink()
    result = _mailctl(env, "stop")
    assert result.returncode != 0
    assert "bootout" not in _log(log)
    assert loaded.exists()


def test_an_argument_mentioning_the_service_does_not_make_a_job_ours(harness) -> None:
    """launchd reports the program and its arguments; only the program counts.

    An editor started with --note=agentstack-mail-service had its job booted
    out when the arguments were folded into the match.
    """
    env, loaded, serving, log, server = harness
    env = {
        **env,
        "JOB_PROGRAM": "/Applications/Editor.app/Contents/MacOS/editor",
        "JOB_ARGUMENT": "--note=agentstack-mail-service",
    }
    serving.unlink()
    server.shutdown()
    server.server_close()
    result = _mailctl(env, "stop")
    assert "bootout" not in _log(log), "an editor's job was booted out for its argument"
    assert loaded.exists()
    assert "is not this service" in (result.stdout + result.stderr), (
        "a foreign job holding the label was passed over in silence"
    )


def test_a_foreign_job_appearing_mid_start_still_stops_the_start(harness) -> None:
    """The check and the act are separate moments; both need the guard.

    Reading "is a job loaded?" and "is it ours?" as two questions collapses
    absent and foreign into one answer, and a job that loads between them is
    then treated as absent -- so a foreign job survived while an unsupervised
    runner started next to it.
    """
    env, loaded, serving, log, server = harness
    loaded.unlink()          # absent when start looks
    serving.unlink()
    server.shutdown()
    server.server_close()
    env = {
        **env,
        "JOB_PROGRAM": "/Applications/Editor.app/Contents/MacOS/editor",
        "APPEAR_AFTER_FIRST_PRINT": "1",
    }
    result = _mailctl(env, "start")
    assert result.returncode != 0, result.stdout
    assert "not this service" in result.stderr


def test_a_name_shaped_lookalike_is_not_this_service(harness, tmp_path: Path) -> None:
    """Exact names. "not-agentstack-mail-service-backup" is not this service."""
    env, loaded, serving, log, server = harness
    lookalike = tmp_path / "not-agentstack-mail-service-backup"
    lookalike.write_text("#!/bin/sh\n", encoding="utf-8")
    env = {**env, "JOB_PROGRAM": str(lookalike)}
    serving.unlink()
    server.shutdown()
    server.server_close()
    result = _mailctl(env, "stop")
    assert "bootout" not in _log(log), "a lookalike's job was booted out"
    assert loaded.exists()
    assert "is not this service" in (result.stdout + result.stderr)


def test_a_name_shaped_symlink_is_not_this_service(harness, tmp_path: Path) -> None:
    """Follow the name to what it really runs."""
    env, loaded, serving, log, server = harness
    # A different directory: the fixture's own honest executable already owns
    # this name in tmp_path.
    elsewhere = tmp_path / "impostor"
    elsewhere.mkdir()
    lookalike = elsewhere / "agentstack-mail-service"
    lookalike.symlink_to("/bin/echo")
    env = {**env, "JOB_PROGRAM": str(lookalike)}
    serving.unlink()
    server.shutdown()
    server.server_close()
    result = _mailctl(env, "stop")
    assert "bootout" not in _log(log), "a symlink to /bin/echo was treated as this service"
    assert loaded.exists()


def test_our_own_job_appearing_mid_start_hands_back_to_launchd(harness) -> None:
    """Every state matters at the last moment, not only the foreign one.

    If launchd loaded our job while start was probing, launchd is bringing the
    server up; starting a second one from here collides with it.
    """
    env, loaded, serving, log, server = harness
    loaded.unlink()
    serving.unlink()
    env = {**env, "APPEAR_AFTER_FIRST_PRINT": "1", "REAPPEAR_SERVING": "1"}
    result = _mailctl(env, "start")
    assert result.returncode == 0, result.stderr
    assert "launchd" in result.stdout, result.stdout
    assert "pid" not in result.stdout.split("launchd")[0], result.stdout


def test_a_service_binary_nobody_can_run_is_not_this_service(
    harness, tmp_path: Path
) -> None:
    env, loaded, serving, log, server = harness
    (tmp_path / "agentstack-mail-service").chmod(0o644)
    serving.unlink()
    server.shutdown()
    server.server_close()
    result = _mailctl(env, "stop")
    assert "bootout" not in _log(log), "a job whose binary is not executable was booted out"
    assert loaded.exists()


def test_a_differently_named_link_to_the_service_is_recognised(
    harness, tmp_path: Path
) -> None:
    """Follow the link and judge the target, in both directions."""
    env, loaded, _serving, log, _server = harness
    entry = tmp_path / "mail-entry"
    entry.symlink_to(tmp_path / "agentstack-mail-service")
    env = {**env, "JOB_PROGRAM": str(entry)}
    result = _mailctl(env, "status")
    assert result.returncode == 0, result.stderr
    assert "running under launchd" in result.stdout


def test_no_function_is_defined_twice() -> None:
    """A later definition silently wins in bash.

    An edit left two `executable_is_this_service` definitions in place; the
    stale one was 30 lines further down, so every call used the version the
    fix had replaced -- and the tests for the fix failed for reasons that
    looked like the fix not working.
    """
    import collections
    import re

    names = re.findall(r"^([a-z_][a-z0-9_]*)\(\) \{", MAILCTL.read_text(), re.MULTILINE)
    duplicates = [name for name, count in collections.Counter(names).items() if count > 1]
    assert not duplicates, f"defined more than once: {duplicates}"


def test_the_last_thing_before_the_spawn_is_the_launchd_check() -> None:
    """The guard has to sit immediately before the spawn, not before the probes.

    This one is structural on purpose. The harm -- a job loading during the
    endpoint and health probes, leaving two supervisors for one endpoint -- was
    measured on a revision where the check sat earlier, but a fake cannot
    express it: removing the check also removes the `launchctl print` that
    would reveal the job, so a behavioural test passes either way. What can be
    pinned is that nothing observable happens between the check and the spawn.
    """
    source = MAILCTL.read_text()
    spawn = source.index('nohup /bin/bash "$MAIL_RUNNER"')
    guard = source.rindex('case "$(launchd_state)" in', 0, spawn)
    between = source[source.index("esac", guard) + len("esac") : spawn]
    for probe in ("endpoint_port_open", "health_ok", "wait_for_health", "launchctl"):
        assert probe not in between, (
            f"{probe} runs between the launchd check and the spawn; the window it "
            "opens is exactly the one this check exists to close"
        )
    assert between.strip() == "", f"unexpected statements before the spawn: {between!r}"


def test_a_foreign_job_appearing_after_the_probes_still_stops_the_spawn(
    harness,
) -> None:
    """The guard has to sit immediately before the spawn.

    Checking before endpoint_port_open and health_ok leaves the probes' worth of
    time in the window, and a job loading there ends with two supervisors for
    one endpoint -- measured on the previous revision, which reported
    "ORRERY Mail started (pid ...)" with the foreign job still loaded.
    """
    env, loaded, serving, log, server = harness
    loaded.unlink()
    serving.unlink()
    server.shutdown()
    server.server_close()
    env = {
        **env,
        "JOB_PROGRAM": "/Applications/Editor.app/Contents/MacOS/editor",
        "APPEAR_AFTER_N_PRINTS": "2",  # after the first state read
    }
    result = _mailctl(env, "start")
    assert result.returncode != 0, result.stdout
    assert "not this service" in result.stderr
    assert loaded.exists()


def test_no_test_is_defined_twice() -> None:
    """A duplicated test is a test that never runs.

    pytest collects the later definition and silently discards the earlier one,
    so the earlier one's assertions stop being checked without anything saying
    so. This file had two copies of the duplicate-shell-function test, which is
    the same mistake it exists to catch, one level up.
    """
    import collections
    import re

    for path in (
        Path(__file__),
        Path(__file__).with_name("test_legacy_mail_retirement.py"),
        Path(__file__).with_name("test_session_start_liveness.py"),
    ):
        names = re.findall(r"^def (test_[a-z0-9_]+)\(", path.read_text(), re.MULTILINE)
        duplicates = [n for n, c in collections.Counter(names).items() if c > 1]
        assert not duplicates, f"{path.name}: defined more than once: {duplicates}"



# --- cold boot: a runner that is still starting has not failed ---------------
#
# The controller used to allow one fixed window after spawning the runner --
# 150 probes, each of which spawns a Python interpreter, so about 48 s while
# the port is closed on the machine this was measured on -- and then kill the
# runner it had just started. A server whose start takes longer than that is killed
# seconds before it would listen. These tests drive the nohup path with a
# runner that opens its port only after a delay. The old controller's kill is
# reproduced with a runner that has not opened its port when the grace ends,
# not by racing a delay against the old window: that window's length depends
# on interpreter start-up time (48 s measured idle, longer under load).

import socket
import time

SLOW_RUNNER = """#!/bin/sh
# Every process this runner starts is recorded in the harness's pid ledger so
# cleanup can signal and confirm them without a process listing: pgrep/pkill
# are refused in some sandboxes (sysmond unavailable), and a cleanup that
# treats "cannot list" as "nothing left" leaves runners behind.
echo $$ >> {ledger}
python3 -c 'import sys, time; time.sleep(float(sys.argv[1]))' {delay} {marker} &
echo $! >> {ledger}
wait $!
python3 - {port} {database} {health} <<'PY' &
import http.server
import json
import sys

port, database, health = int(sys.argv[1]), sys.argv[2], sys.argv[3]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if health != "ok":
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps({{
            "jsonrpc": "2.0",
            "id": 1,
            "result": {{"structuredContent": {{
                "status": "ok",
                "database_url": "sqlite+aiosqlite:///" + database,
            }}}},
        }}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
PY
echo $! >> {ledger}
wait
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _cold_boot(harness, *, delay: float, health: str = "ok", script: str | None = None):
    """Point the controller at a closed port and a runner that opens it late.

    No launchd job is loaded, so `start` takes the nohup path -- the one the
    login-time trigger takes on a machine whose plist runs the controller.
    The runner records its own pid and its children's in the ledger under
    the harness tmp_path (see _ledger), which is what _reap keys on.
    """
    env, loaded, _serving, _log, _server = harness
    loaded.unlink()
    port = _free_port()
    env = dict(env, AGENTSTACK_MCP_URL=f"http://127.0.0.1:{port}/mcp")
    runner = Path(env["AGENTSTACK_MAIL_ENV"]).with_name("run-agentstack-mail.sh")
    runner.write_text(
        script
        if script is not None
        else SLOW_RUNNER.format(
            delay=delay,
            port=port,
            database=env["AGENTSTACK_MAIL_DB"],
            health=health,
            marker=_marker(env),
            ledger=_ledger(env),
        ),
        encoding="utf-8",
    )
    runner.chmod(0o755)
    return env, Path(env["AGENTSTACK_MAIL_RUNTIME_DIR"]) / "agentstack-mail.pid"


def _runner_pid(pidfile: Path) -> int:
    return int(pidfile.read_text(encoding="utf-8").splitlines()[0])


def _alive(pid: int) -> bool:
    """False only when the kernel says there is no such process. Any other
    failure (EPERM, a sandbox refusing the query) is raised: reading it as
    "gone" would make cleanup report success over a runner it cannot see."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _marker(env: dict[str, str]) -> str:
    """The harness tmp tree. Every process the runner starts -- the runner's
    shell (script path), the delay (explicit argument), the fake server
    (database path) -- carries it in argv, which is what cleanup keys on."""
    return str(Path(env["AGENTSTACK_MAIL_ENV"]).parent.parent)


def _ledger(env: dict[str, str]) -> str:
    """Where the runner records the pid of itself and of each process it
    starts. Cleanup reads this instead of listing processes."""
    return str(Path(_marker(env)) / "runner-pids")


def _ledger_pids(env: dict[str, str]) -> list[int]:
    ledger = Path(_ledger(env))
    if not ledger.exists():
        return []
    return [int(line) for line in ledger.read_text().split() if line.isdigit()]


def _owned_processes(env: dict[str, str]) -> list[int]:
    """Every recorded process that is still alive. A process listing is not
    required: kill(pid, 0) answers for a pid we already know."""
    return [pid for pid in _ledger_pids(env) if _alive(pid)]


def _reap(env: dict[str, str]) -> None:
    """Terminate the runner and everything it started, and confirm they are
    gone, whichever path the test took to get here (pass, assert, timeout)."""
    for signal_number in (15, 9):
        for pid in _owned_processes(env):
            try:
                os.kill(pid, signal_number)
            except ProcessLookupError:
                pass  # exited between the liveness check and the signal
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _owned_processes(env):
                return
            time.sleep(0.1)
    assert not _owned_processes(env), "runner processes survived cleanup"


@pytest.fixture()
def cold_boot(harness):
    """Yield the harness for a nohup-path test and always reap afterwards."""
    state: dict[str, dict[str, str]] = {}

    def start(**kwargs):
        env, pidfile = _cold_boot(harness, **kwargs)
        state["env"] = env
        return env, pidfile

    try:
        yield start
    finally:
        if "env" in state:
            _reap(state["env"])


def test_port_grace_is_separate_from_health_grace(cold_boot) -> None:
    """Health is allowed only 2 s once the port is open -- the shape of a single
    short window -- while the runner takes 6 s to listen. The port grace is
    what carries the start across that gap. This is not the reproduction of
    the old kill (see test_start_leaves_a_still_starting_runner_alive...): the
    old window's length depends on interpreter start-up time under load, so a
    delay chosen to exceed it is not reliable evidence either way."""
    env, pidfile = cold_boot(delay=6)
    env.update(AGENTSTACK_MAIL_START_GRACE="40", AGENTSTACK_MAIL_HEALTH_GRACE="2")
    began = time.monotonic()
    result = _mailctl(env, "start")
    assert result.returncode == 0, result.stderr
    assert "ORRERY Mail started" in result.stdout
    assert "ready after" in result.stdout
    assert time.monotonic() - began >= 6
    pid = _runner_pid(pidfile)
    assert _alive(pid), "the controller killed the runner it had started"
    status = _mailctl(env, "status")
    assert status.returncode == 0, status.stderr


def test_health_grace_is_a_time_bound_not_a_probe_count(cold_boot) -> None:
    """A port that opens but never answers health must not hold the controller
    for probe-count x probe-timeout. HEALTH_GRACE=2 means about 2 s plus the
    probe that is in flight when the deadline passes."""
    env, pidfile = cold_boot(delay=0, health="fail")
    env.update(AGENTSTACK_MAIL_START_GRACE="10", AGENTSTACK_MAIL_HEALTH_GRACE="2")
    began = time.monotonic()
    result = _mailctl(env, "start")
    elapsed = time.monotonic() - began
    assert result.returncode != 0
    assert "endpoint port open" in result.stderr, result.stderr
    assert elapsed < 8, f"health wait ran {elapsed:.1f}s for a 2 s grace"
    assert pidfile.exists()
    assert _alive(_runner_pid(pidfile))


def test_start_leaves_a_still_starting_runner_alive_when_grace_runs_out(
    cold_boot,
) -> None:
    """The reproduction of the old kill, and the fix. A runner that has not
    opened its port when the grace runs out is exactly what the old controller
    killed (after its 150 probes) before removing the pidfile; against the old
    controller this test fails on `_alive(pid)` and on the missing pidfile.
    Running out of patience is not evidence of failure: the runner stays, the
    pidfile stays, and the message says how long it waited and where it got
    to."""
    env, pidfile = cold_boot(delay=60)
    env.update(AGENTSTACK_MAIL_START_GRACE="2", AGENTSTACK_MAIL_HEALTH_GRACE="1")
    result = _mailctl(env, "start")
    assert result.returncode != 0
    # Behaviour first, wording second: against the old controller these two
    # are what fail (the pidfile is gone and the runner is dead).
    assert pidfile.exists(), "the pidfile was discarded"
    pid = _runner_pid(pidfile)
    assert _alive(pid), "the controller killed the runner it had started"
    assert "still starting after" in result.stderr, result.stderr
    assert "endpoint port closed" in result.stderr
    assert "left running" in result.stderr
    status = _mailctl(env, "status")
    assert status.returncode != 0
    assert "not listening yet" in status.stderr, status.stderr


def test_a_later_start_finds_the_runner_instead_of_racing_a_second_one(
    cold_boot,
) -> None:
    """The timer's next run lands while the first runner is still coming up. It
    must not spawn another runner against the same port, and it must not hold
    the lifecycle lock for another full grace either: it reports and returns,
    and once the runner listens a later start says so."""
    env, pidfile = cold_boot(delay=12)
    first_env = dict(env, AGENTSTACK_MAIL_START_GRACE="1", AGENTSTACK_MAIL_HEALTH_GRACE="1")
    first = _mailctl(first_env, "start")
    assert first.returncode != 0
    pid = _runner_pid(pidfile)
    sweep_env = dict(
        env,
        AGENTSTACK_MAIL_START_GRACE="30",
        AGENTSTACK_MAIL_HEALTH_GRACE="5",
        AGENTSTACK_MAILCTL_SWEEP="1",
    )
    began = time.monotonic()
    sweep = _mailctl(sweep_env, "start")
    assert sweep.returncode != 0
    assert "not listening yet" in sweep.stderr, sweep.stderr
    assert time.monotonic() - began < 5, "the sweep waited a full grace on a foreign start"
    assert _runner_pid(pidfile) == pid, "a second runner replaced the first"
    # The runner appends its own pid to the ledger once per start: a second
    # spawn would show as a second shell pid there. (The delay and the fake
    # server append theirs too, so count runner shells, not lines.)
    runner_shells = [pid for pid in _ledger_pids(env) if pid == _runner_pid(pidfile)]
    assert len(_ledger_pids(env)) <= 3 and runner_shells == [pid], _ledger_pids(env)
    time.sleep(12)
    # Diagnose before asserting the outcome: if the runner died in the
    # meantime the later start legitimately spawns a new one, and a pid
    # mismatch alone would read as a controller bug.
    assert _alive(pid), "the first runner died before the later start"
    later = _mailctl(sweep_env, "start")
    assert later.returncode == 0, later.stderr
    assert _runner_pid(pidfile) == pid, "the later start replaced a live runner"


def test_start_reports_a_runner_that_exits_before_listening(cold_boot) -> None:
    """A runner that dies is the one case where giving up is right, and the
    message must say that it exited rather than that health never came."""
    env, pidfile = cold_boot(delay=0, script="#!/bin/sh\nexit 7\n")
    env.update(AGENTSTACK_MAIL_START_GRACE="20", AGENTSTACK_MAIL_HEALTH_GRACE="1")
    result = _mailctl(env, "start")
    assert result.returncode != 0
    assert "exited after" in result.stderr, result.stderr
    assert not pidfile.exists(), "a dead runner's pidfile was kept"


def test_a_non_numeric_grace_is_refused(cold_boot) -> None:
    env, _pidfile = cold_boot(delay=0)
    env.update(AGENTSTACK_MAIL_START_GRACE="soon")
    result = _mailctl(env, "status")
    assert result.returncode != 0
    assert "AGENTSTACK_MAIL_START_GRACE" in result.stderr


def test_alive_reports_dead_only_for_a_missing_process(monkeypatch) -> None:
    """The null case for cleanup: an unanswerable query is not a dead runner."""

    def refuse(_pid: int, _sig: int) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "kill", refuse)
    with pytest.raises(PermissionError):
        _alive(12345)

    def missing(_pid: int, _sig: int) -> None:
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(os, "kill", missing)
    assert _alive(12345) is False


def test_reap_does_not_swallow_a_refused_signal(monkeypatch, tmp_path: Path) -> None:
    env = {"AGENTSTACK_MAIL_ENV": str(tmp_path / "service" / "env")}
    (tmp_path / "runner-pids").write_text("12345\n", encoding="utf-8")
    calls: list[tuple[int, int]] = []

    def refuse(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == 0:
            return
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "kill", refuse)
    with pytest.raises(PermissionError):
        _reap(env)
    assert (12345, 15) in calls
