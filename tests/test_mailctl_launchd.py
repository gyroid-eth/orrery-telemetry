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
  bootstrap)
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
    program.write_text("#!/bin/sh\n", encoding="utf-8")
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
        "SERVING_MARKER": str(serving),
        "AGENTSTACK_MAILCTL_SKIP_ENV": "1",
        "AGENTSTACK_MAIL_PROVIDER": "agentstack",
        "AGENTSTACK_MAIL_ENV": str(tmp_path / "service" / "env"),
        "AGENTSTACK_MAIL_DB": str(tmp_path / "storage.sqlite3"),
        "AGENTSTACK_MAIL_RUNTIME_DIR": str(tmp_path / "runtime"),
        "AGENTSTACK_MCP_URL": f"http://{host}:{port}/mcp",
        "AGENTSTACK_MAIL_LAUNCHD_LABEL": LABEL,
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "env").write_text("", encoding="utf-8")
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
