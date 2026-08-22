#!/usr/bin/env python3
"""Stop a dashboard an installer test started, and be sure it stopped.

A successful install leaves a supervised dashboard running. Tests that run the
installer therefore start one every time, and without this each run leaves
another behind — a suite that reports 348 passed while quietly accumulating
servers.

Three things make this harder than one kill:

* the supervisor restarts the server, so the supervisor goes first;
* it can fork a replacement between the signal and the sweep, so one sweep is
  not enough;
* under load the service can take seconds to appear, so an empty sweep does not
  mean "stopped" — it can mean "not started yet". That one passed in isolation
  and leaked a process per run in the full suite.

On macOS the installer may register a launchd agent instead, and launchd
restarts what you kill — which is why every test that runs the installer must
also pass a label of its own (see ``TEST_LABEL_PREFIX``) and have it booted out
here. Without a distinct label the test registers a service under the same name
a real install uses, on the developer's own machine.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import time

# Tests must pass AGENTSTACK_LABEL_PREFIX=<this> so a test run can never
# bootstrap, or boot out, the service belonging to a real install.
TEST_LABEL_PREFIX = "org.agentstack.test"


def _run_command(args, *, timeout=None):
    """Every external command this module runs goes through here.

    Tests replace this symbol. They used to assign to
    ``service_teardown.subprocess.run``, which is the shared stdlib module
    object -- the same attribute the production service watcher in conftest
    calls from its own thread. That watcher hit a test's fake, died with an
    exception pytest reported as a warning, and stopped watching for the rest
    of the run.
    """
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout)


def stop_recorded_supervisor(pidfile, home) -> bool:
    """Stop the process this install wrote down, without listing processes.

    Sandboxes exist where listing processes is unavailable -- this repo's
    own review environment answers `pgrep` with "Cannot get process list".
    A teardown that can only work by inventory fails there for reasons that
    have nothing to do with what it is cleaning up.

    The pid file holds exactly one pid: the service runner, which is its
    own supervisor (scripts/install.sh writes `$!` of service_runner.py).
    A pid alone is not provenance, though -- pids are reused, and a file
    can say anything. It is only acted on when the install's own state
    agrees that this pid is its supervisor and that the server it
    supervises lives under this home. Anything else is refused rather than
    killed: an unrelated process is a worse outcome than a failed cleanup.

    Scope: test homes, created for one test and thrown away. Both the pid file
    and the state survive a SIGKILL'd runner, so a pid reused by the operating
    system later would still match them -- the identity here is "what this
    install recorded", not "the process now holding that number". A fresh
    per-test home makes the window small enough to be worth the simplicity;
    the same helper must not be pointed at a long-lived home, where a stale
    pair can outlive the pid by hours. Closing that gap needs a live handle
    (a lock, a control socket) rather than files.
    """
    pidfile = pathlib.Path(pidfile)
    home = pathlib.Path(home)
    try:
        raw = pidfile.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    # The exact shape install.sh writes: digits and one optional newline.
    # Loosening this by a `.split()` accepted " 424242 \n" as strict, and by a
    # `.splitlines()` accepted a pid wrapped in blank lines.
    # ASCII digits only: `\d` also matches other scripts' digits, and int()
    # parses them, so a pid file could name a process in a form nothing that
    # wrote it would ever produce.
    match = re.fullmatch(r"([0-9]+)\n?", raw)
    if not match:
        raise RuntimeError(
            f"pid file is not a single pid, refusing to act on it: {raw!r} ({pidfile})"
        )
    pid = int(match.group(1))
    if pid <= 1:
        raise RuntimeError(f"pid file names pid {pid}, refusing to act on it ({pidfile})")

    state_path = home / ".agentstack" / "runtime" / "dashboard-service.json"

    def state_names_it() -> bool:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if state.get("supervisor_pid") != pid:
            return False
        server_path = state.get("server_path")
        if not isinstance(server_path, str):
            return False
        try:
            return home.resolve() in pathlib.Path(server_path).resolve().parents
        except OSError:
            return False

    def provenance_holds() -> bool:
        # One witness, and a structural one: the install's own state naming
        # this pid as its supervisor. A "does the process command line mention
        # this home" check was tried and removed -- it accepted any process
        # that merely had the path among its arguments.
        return state_names_it()

    def alive() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Somebody else's process owns that number now; not ours.
            return False
        return True

    # Nothing to stop, and nothing to prove: a pid file left behind by a
    # process that has already exited is just a stale file. Demanding
    # provenance for it turned an ordinary end-of-test state into a teardown
    # failure.
    if not alive():
        pidfile.unlink(missing_ok=True)
        return True

    if not provenance_holds():
        raise RuntimeError(
            f"pid {pid} is not this install's supervisor according to {state_path}; "
            "refusing to signal it"
        )

    for signal_to_send in (signal.SIGTERM, signal.SIGKILL):
        # Re-checked before every signal: between the first one and the
        # second, the pid can be gone and reused by something unrelated.
        if not alive():
            break
        if not provenance_holds():
            raise RuntimeError(
                f"pid {pid} no longer matches this install's supervisor; refusing to signal it"
            )
        try:
            os.kill(pid, signal_to_send)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not alive():
                break
            time.sleep(0.1)
        if not alive():
            break

    if alive():
        raise RuntimeError(
            f"dashboard process survived teardown: {pid} (pidfile {pidfile})"
        )
    pidfile.unlink(missing_ok=True)
    return True


def stop_dashboard(home, *, appear_timeout: float = 8.0,
                   label_prefix: str = TEST_LABEL_PREFIX) -> None:
    """Terminate the dashboard this install started, launchd or supervised."""
    home = pathlib.Path(home)
    # launchd is macOS only; on Linux the installer uses systemd or a plain
    # supervisor, and there is no launchctl to call.
    if label_prefix and shutil.which("launchctl"):
        _run_command(["launchctl", "bootout", f"gui/{os.getuid()}/{label_prefix}.agentdashboard"])
    marker = str(home.resolve() / ".agentstack" / "dashboard")
    pidfile = home / ".agentstack" / "runtime" / "dashboard.pid"

    if stop_recorded_supervisor(pidfile, home):
        return


    def running() -> list[int]:
        """Processes matching this install's dashboard.

        Raises when pgrep could not answer. Turning a tool failure into an
        empty list made "we could not look" identical to "nothing is there",
        so a teardown that never observed anything reported success -- the
        same collapse this module's own leak came from, one level down.
        """
        try:
            found = _run_command(["pgrep", "-f", marker], timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"could not look for dashboard processes: {exc}") from exc
        tokens = found.stdout.split()
        # Anything on stderr means the answer is qualified, whatever the exit
        # code says. A partial listing that killed what it could see would
        # otherwise return cleanly with processes still running.
        if found.stderr.strip():
            raise RuntimeError(
                f"pgrep could not answer cleanly ({found.returncode}): {found.stderr.strip()}"
            )
        # pgrep: 1 means no match, and it says nothing on either stream.
        if found.returncode == 1 and not tokens:
            return []
        if found.returncode != 0:
            raise RuntimeError(
                f"pgrep failed ({found.returncode}): {found.stderr.strip() or 'no output'}"
            )
        if not tokens or any(not token.isdigit() for token in tokens):
            raise RuntimeError(f"pgrep returned output that is not pids: {found.stdout!r}")
        return [int(token) for token in tokens]

    # Nothing was installed here, so there is nothing of ours to find. This is
    # a filesystem fact, not a failed lookup: asking for a process inventory
    # anyway made two tests that install nothing fail in a sandbox where
    # listing processes is unavailable.
    if not pathlib.Path(marker).exists():
        return

    def look_or_give_up():
        """Ask the inventory, unless there is nothing recorded to look for.

        An install that never wrote a pid file mostly never started a service:
        several suites install with a system manager stub that starts nothing.
        For those, a lookup that cannot answer says nothing about a leak, and
        raising made them fail wherever listing processes is unavailable. When
        a pid file does exist, the recorded path above has already handled it,
        so an unanswerable lookup there is still an error.
        """
        try:
            return running()
        except RuntimeError:
            state = home / ".agentstack" / "runtime" / "dashboard-service.json"
            if pidfile.exists() or state.exists():
                # Something here recorded a running service at some point, so
                # an unanswerable lookup leaves a real question open.
                raise
            return None

    # Wait for it to exist before concluding it does not.
    deadline = time.monotonic() + appear_timeout
    while time.monotonic() < deadline:
        found = look_or_give_up()
        if found is None:
            return
        if pidfile.exists() or found:
            break
        time.sleep(0.2)

    # The pid file can arrive during that wait -- the loop above exists for
    # exactly that case. It goes through the same provenance rule as one that
    # was there from the start; sending an unverified signal here was the last
    # way around it, and the comment above ("the service can take seconds to
    # appear") describes the normal path into it, not an edge case.
    if stop_recorded_supervisor(pidfile, home):
        return

    # Two consecutive empty sweeps: one can catch the gap between a supervisor
    # dying and its replacement being visible.
    empty = 0
    for _ in range(20):
        pids = look_or_give_up()
        if pids is None:
            return
        if not pids:
            empty += 1
            if empty >= 2:
                return
            time.sleep(0.3)
            continue
        empty = 0
        for pid in pids:
            # These pids carry their own provenance: they came from matching
            # this install's dashboard path in the process's command line, not
            # from a file that could name anything. (Audit scope: this module
            # and the fake service managers that call it. Other suites signal
            # pids too -- test_fresh_install_e2e, test_codex_app_packaging,
            # test_install_mail_probe -- and are not covered by this rule.)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.3)

    # Falling out of the loop means the sweeps ran out with processes still
    # alive. Returning here reported a clean teardown for a leak -- which is
    # how four dashboards from one test stayed up on a developer's machine
    # without any test failing.
    remaining = look_or_give_up()
    if remaining:
        raise RuntimeError(
            f"dashboard processes survived teardown: {remaining} (marker {marker})"
        )
