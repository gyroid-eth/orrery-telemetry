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

import os
import pathlib
import signal
import subprocess
import time

# Tests must pass AGENTSTACK_LABEL_PREFIX=<this> so a test run can never
# bootstrap, or boot out, the service belonging to a real install.
TEST_LABEL_PREFIX = "org.agentstack.test"


def stop_dashboard(home, *, appear_timeout: float = 8.0,
                   label_prefix: str = TEST_LABEL_PREFIX) -> None:
    """Terminate the dashboard this install started, launchd or supervised."""
    home = pathlib.Path(home)
    if label_prefix:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label_prefix}.agentdashboard"],
            capture_output=True,
        )
    marker = str(home.resolve() / ".agentstack" / "dashboard")
    pidfile = home / ".agentstack" / "runtime" / "dashboard.pid"

    def running() -> list[int]:
        found = subprocess.run(
            ["pgrep", "-f", marker], text=True, capture_output=True)
        pids = []
        for token in found.stdout.split():
            try:
                pids.append(int(token))
            except ValueError:
                pass
        return pids

    # Wait for it to exist before concluding it does not.
    deadline = time.monotonic() + appear_timeout
    while time.monotonic() < deadline:
        if pidfile.exists() or running():
            break
        time.sleep(0.2)

    try:
        os.kill(int(pidfile.read_text(encoding="utf-8").split()[0]), signal.SIGTERM)
    except (OSError, ValueError, IndexError):
        pass

    # Two consecutive empty sweeps: one can catch the gap between a supervisor
    # dying and its replacement being visible.
    empty = 0
    for _ in range(20):
        pids = running()
        if not pids:
            empty += 1
            if empty >= 2:
                return
            time.sleep(0.3)
            continue
        empty = 0
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.3)
