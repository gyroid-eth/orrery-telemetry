"""Session-wide safety net: the suite must not stop the machine's own services.

tests/test_mail_autostart.py ran `agentstack-mailctl stop` without naming a
label. The controller defaults to the production one, so every full-suite run
booted out the developer's real mail service. Each occurrence was investigated
as an outage in its own right -- five in one day -- before the pattern showed
up, and one of those investigations reported the supervisor as broken for
failing to recover from a stop nothing had explained.

A textual rule ("every test that runs mailctl must set a label") is easy to
satisfy without being true: the offending file already contained a test label,
in an unrelated string. This watches the service instead.

Comparing before and after is not enough either -- a test that stops the
service and starts it again leaves the same end state while every agent on the
machine loses its coordination for the duration. So this samples throughout.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

import pytest

# Labels this machine's own services use. The mail service is the one the
# suite has actually stopped; the autostart wrapper is named per install, so
# it is read from the environment rather than written down here -- an operator
# name does not belong in a tracked file (test_no_personal_identifiers.py).
PRODUCTION_LABELS = tuple(
    label
    for label in (
        "org.orrery.mail",
        "org.agentstack.mail",
        os.environ.get("AGENTSTACK_MAIL_AUTOSTART_LABEL", ""),
    )
    if label
)
SAMPLE_SECONDS = 2.0

# Which test is running right now. A disturbance that only appears in a full
# run is an interaction between tests, and "somewhere in the suite" is not a
# lead -- naming the test that was running when the service went down is.
_CURRENT_TEST = "<none>"


def pytest_runtest_logstart(nodeid, location):  # noqa: ARG001 - pytest hook
    global _CURRENT_TEST
    _CURRENT_TEST = nodeid


# Resolved once, at import, before any test runs. A test in this suite replaces
# `shutil.which` on the shared stdlib module to simulate a machine without
# launchctl; a watcher that looked the tool up at sample time inherited that
# and read "no launchctl" as "the service stopped", reporting a production
# outage that never happened. The instrument has to be independent of what it
# is watching.
LAUNCHCTL = shutil.which("launchctl")

UNKNOWN = "unknown"


def _job_pid(label: str) -> int | str | None:
    """pid when running, None when stopped, UNKNOWN when unmeasurable.

    Loaded is not running: after a bootout the job can remain loaded with
    nothing behind it, so `launchctl print` succeeding answers a question
    nobody asked. An earlier version of this guard checked exactly that and
    watched the service die without noticing. Collapsing "cannot tell" into
    "stopped" is the same mistake pointing the other way.
    """
    if not LAUNCHCTL:
        return UNKNOWN
    try:
        result = subprocess.run(
            [LAUNCHCTL, "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if result.returncode != 0:
        # launchctl answers "no such service" with 113. Anything else is the
        # tool failing, not the service being absent.
        return None if result.returncode == 113 else UNKNOWN
    # Only the top-level fields. Real `launchctl print` nests blocks under the
    # job -- domain, endpoints, spawn state -- and several of them have their
    # own `state = ...`. Reading the last one seen meant the nested
    # "state = active" overwrote the job's own "state = running", so the real
    # service on this machine measured as unmeasurable and was never watched.
    # Two passes, because the job's own fields are the shallowest ones and a
    # nested block can appear before them. Reading in one pass anchored on
    # whatever came first, so an indent-nested block listed first defined what
    # "top level" meant.
    candidates = []
    depth = 0
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("}", "};"):
            depth = max(0, depth - 1)
            continue
        if stripped.endswith("{"):
            depth += 1
            continue
        if depth > 1:
            continue
        if not re.match(r"(state|pid) = ", stripped):
            continue
        candidates.append((len(line) - len(line.lstrip("\t ")), stripped))

    state = None
    pid = None
    if candidates:
        shallowest = min(indent for indent, _ in candidates)
        for indent, stripped in candidates:
            if indent != shallowest:
                continue
            match = re.match(r"state = (\S+)", stripped)
            if match and state is None:
                state = match.group(1)
            match = re.match(r"pid = (\d+)", stripped)
            if match and pid is None:
                pid = int(match.group(1))

    # Same rule as above: only a state this code recognises is an observation.
    # Output drift, or a "running" job with no pid, is the tool telling us
    # something we do not understand -- not the service being stopped.
    if state is None:
        return UNKNOWN
    if state == "running":
        return pid if pid is not None else UNKNOWN
    if state in ("waiting", "not", "stopped"):
        return None
    return UNKNOWN


LSOF = shutil.which("lsof") or "/usr/sbin/lsof"


def _listener_pids(port: int = 8765) -> tuple[int, ...] | str:
    """The processes actually serving the port, or UNKNOWN.

    The launchd job's pid is a wrapper; the service is its child. A wrapper
    that survives while the service under it is killed and restarted leaves
    the job "running" and every agent's coordination interrupted, so the job
    pid alone does not answer the question this watcher exists for.
    """
    if not os.path.exists(LSOF):
        return UNKNOWN
    try:
        result = subprocess.run(
            [LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    # lsof exits 1 for "nothing matched" and uses other nonzero codes for its
    # own failures. Reading both as "no listener" is the mistake this file is
    # about: a tool that could not answer would blame whichever test was running.
    tokens = result.stdout.split()
    # Absence has to be recognised positively. "Exit code we tolerate" is not
    # the same as "the tool told us there is nothing there": lsof reports no
    # match as rc1 with nothing on either stream, and uses rc1 with a
    # diagnostic for its own troubles.
    if result.returncode == 1 and not tokens and not result.stderr.strip():
        return ()
    if result.returncode != 0:
        return UNKNOWN
    if not tokens or any(not token.isdigit() for token in tokens):
        return UNKNOWN
    return tuple(sorted(int(token) for token in tokens))


class _Watcher(threading.Thread):
    def __init__(self, labels: tuple[str, ...]) -> None:
        super().__init__(daemon=True)
        self.labels = labels
        self.stop = threading.Event()
        self.baseline = {label: _job_pid(label) for label in labels}
        self.listener_baseline = _listener_pids()
        self.disturbed: dict[str, str] = {}
        self.crashed: BaseException | None = None

    def _check_listener(self) -> None:
        if self.listener_baseline == UNKNOWN or not self.listener_baseline:
            return
        if "mail listener" in self.disturbed:
            return
        current = _listener_pids()
        if current == UNKNOWN:
            return
        if not current:
            self.disturbed["mail listener"] = f"stopped serving 8765 while running {_CURRENT_TEST}"
        elif current != self.listener_baseline:
            self.disturbed["mail listener"] = (
                f"replaced while running {_CURRENT_TEST} "
                f"(pids {self.listener_baseline} -> {current})"
            )

    def run(self) -> None:
        try:
            self._watch()
        except BaseException as exc:  # noqa: BLE001 - re-raised at session end
            # A watcher that dies stops watching, and a thread exception is
            # only a warning: one test's monkeypatch killed this thread and the
            # rest of the suite ran unwatched, reporting success.
            self.crashed = exc

    def _watch(self) -> None:
        while not self.stop.wait(SAMPLE_SECONDS):
            self._check_listener()
            for label, baseline_pid in self.baseline.items():
                if baseline_pid is None or baseline_pid == UNKNOWN:
                    continue  # not this machine's, already down, or unmeasurable
                if label in self.disturbed:
                    continue
                current = _job_pid(label)
                if current == UNKNOWN:
                    continue
                if current is None:
                    self.disturbed[label] = f"stopped while running {_CURRENT_TEST}"
                elif current != baseline_pid:
                    self.disturbed[label] = (
                        f"replaced while running {_CURRENT_TEST} "
                        f"(pid {baseline_pid} -> {current})"
                    )


@pytest.fixture(scope="session", autouse=True)
def _the_suite_leaves_real_services_alone():
    watcher = _Watcher(PRODUCTION_LABELS)
    watcher.start()
    try:
        yield
    finally:
        watcher.stop.set()
        watcher.join(timeout=SAMPLE_SECONDS * 2)
        if watcher.is_alive():
            # A thread that hangs stops watching just as thoroughly as one that
            # dies, and leaves no exception to report.
            pytest.fail(
                "the production-service watcher did not finish; it stopped watching at "
                "some point during this run",
                pytrace=False,
            )
        watcher._check_listener()
        # One last look, for a disturbance in the final seconds.
        for label, baseline_pid in watcher.baseline.items():
            if baseline_pid is None or baseline_pid == UNKNOWN or label in watcher.disturbed:
                continue
            current = _job_pid(label)
            if current == UNKNOWN:
                continue
            if current is None:
                watcher.disturbed[label] = f"stopped by the end of the run (last test: {_CURRENT_TEST})"
            elif current != baseline_pid:
                watcher.disturbed[label] = (
                    f"replaced by the end of the run (pid {baseline_pid} -> {current})"
                )
    if watcher.crashed is not None:
        pytest.fail(
            "the production-service watcher stopped watching during this run: "
            f"{watcher.crashed!r}",
            pytrace=False,
        )
    if watcher.disturbed:
        pytest.fail(
            "the test run disturbed services belonging to this machine's install: "
            + "; ".join(f"{label}: {why}" for label, why in watcher.disturbed.items())
            + ". A test must act only on its own label "
            "(AGENTSTACK_MAIL_LAUNCHD_LABEL / AGENTSTACK_LABEL_PREFIX).",
            pytrace=False,
        )
