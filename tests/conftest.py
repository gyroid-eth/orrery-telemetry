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
import threading
import time

import pytest

from _env_isolation import (
    REAL_USER_INSTRUCTION_FILES,
    digest_files,
    is_inherited_stack_variable,
)

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
SAMPLE_SECONDS = 10.0


@pytest.fixture
def isolated_native_mcp_sources(tmp_path, monkeypatch):
    """Native generation/resume tests use only their own synthetic MCP inputs."""
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setenv('AGENTSTACK_CLAUDE_JSON', str(tmp_path / 'claude.json'))
    monkeypatch.delenv('AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY', raising=False)


@pytest.fixture(autouse=True)
def _close_synthetic_tmux_endpoints():
    """Fixture sockets do not accumulate across a full serial worker."""
    yield
    import sys
    module = sys.modules.get('_fake_tmux_transport')
    if module is not None:
        module._cleanup()


@pytest.fixture(autouse=True, scope="session")
def _no_inherited_agentstack_env():
    """Run the suite as CI does: with no AGENTSTACK_* in the environment.

    A shell that was started by the installed agent stack carries some forty
    AGENTSTACK_* variables (mail database, labels, spawn dirs, codex policy).
    The installer rehearsals inherit them through `os.environ` copies, and the
    live install's values then contradict the rehearsal's own: "AGENTSTACK_MAIL_DB
    must equal the native state database", spawn dirs from the real install
    reported where the test expected its own. On this machine that made 28
    tests fail in a full run that is green in CI, and every one of them was
    read as "environment-dependent" for two days instead of being fixed.

    The same applies to variables that point tools at the real user's config
    directories. A Codex child agent runs with CODEX_HOME set to a per-child
    directory whose AGENTS.md is a symlink to the real ~/.codex/AGENTS.md; an
    installer rehearsal that inherited it wrote the rehearsal's temporary project
    key through that symlink into every later Codex session's instructions (#73).
    """
    inherited = {
        key: value for key, value in os.environ.items() if is_inherited_stack_variable(key)
    }
    for key in inherited:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(inherited)


@pytest.fixture(autouse=True)
def _dashboard_quotas_are_offline_by_default(monkeypatch):
    """HTTP/browser fixtures must not read host credentials or start providers.

    HOME isolation does not cover Keychain. The optional provider entry loads a
    separate dashboard core, so bind both before any per-test HTTP fixture.
    Quota-specific tests may then install their own synthetic service.
    """
    from dashboard import server, provider_server
    from _dashboard_http_fixture import install_offline_quotas
    for dashboard in (server, provider_server):
        install_offline_quotas(dashboard, monkeypatch=monkeypatch)


@pytest.fixture(autouse=True)
def _checkout_dashboard_helpers(monkeypatch):
    """Workers must load this checkout's helpers, never an installed old copy."""
    from pathlib import Path
    from dashboard import server
    monkeypatch.setattr(server, "HOOKS_DIR", str(Path(__file__).resolve().parents[1] / "hooks"))
    # A tmux consumer kept by an earlier test would carry that test's doubles.
    server._reset_tmux_consumer_cache()
    yield
    server._reset_tmux_consumer_cache()


@pytest.fixture(autouse=True)
def _no_child_start_watcher(monkeypatch):
    """A Claude child launched by spawn_child.sh gets a background watcher that
    reports to its parent by Mail when the child does not start its task.
    Tests launch fake children; the tests of that watcher turn it back on.
    Per test, so nothing reaches suites that check for inherited AGENTSTACK_*.
    """
    monkeypatch.setenv("AGENTSTACK_CHILD_START_CHECK", "0")


@pytest.fixture
def isolated_provider_stall_runtime(tmp_path, monkeypatch):
    """Import-time dashboard constants must not retain launcher runtime paths."""
    from dashboard import server
    for key in list(os.environ):
        if is_inherited_stack_variable(key):
            monkeypatch.delenv(key)
    home = tmp_path / "provider-home"
    home.mkdir()
    runtime = home / ".agentstack" / "runtime"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("AGENTSTACK_MAIL_DB", str(home / "fixture.sqlite3"))
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "DB_PATH", str(home / "fixture.sqlite3"))
    monkeypatch.setattr(server, "_provider_monitor", None)
    monkeypatch.setattr(server, "_provider_thread", None)
    monkeypatch.setattr(server, "_provider_stop", threading.Event())
    yield runtime
    server._stop_provider_stall_monitor()

@pytest.fixture(autouse=True, scope="session")
def _the_suite_leaves_real_user_instructions_alone():
    """Fail the run if a test rewrote the real user's instruction files.

    Scrubbing known variables fixes the path we found; this catches the next
    one. The files are the ones the installers write managed blocks into.
    """
    before = digest_files(REAL_USER_INSTRUCTION_FILES)
    yield
    after = digest_files(REAL_USER_INSTRUCTION_FILES)
    changed = sorted(str(path) for path in before if before[path] != after[path])
    if changed:
        pytest.fail(
            "the test run modified the real user's instruction files: "
            + ", ".join(changed),
            pytrace=False,
        )


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


def _private_subprocess():
    """A second, unshared instance of the stdlib subprocess module.

    Same reasoning as LAUNCHCTL, one layer down: tests replace `run` and
    `Popen` on the shared module (tests/test_spawn_v2.py returns a stub with
    no `.stdout`; tests/test_reservation_activity.py wraps Popen to count
    concurrency). A watcher sampling through the shared module during those
    windows either crashed and reported the run as unwatched, or was counted
    as one of the probes it was watching (`assert 9 == 8`). Capturing `run`
    alone is not enough because `run` looks `Popen` up on its module at call
    time, so the instrument gets its own module object.
    """
    import importlib.util

    spec = importlib.util.find_spec("subprocess")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SUBPROCESS = _private_subprocess()
_RUN = _SUBPROCESS.run

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
        result = _RUN(
            [LAUNCHCTL, "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, _SUBPROCESS.SubprocessError):
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
        result = _RUN(
            [LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, _SUBPROCESS.SubprocessError):
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



# The real user's home, from the password database: tests set HOME to a temporary
# directory, and the guard below must keep looking at the real one.
def _real_home() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


REAL_LAUNCH_AGENTS = os.path.join(_real_home(), "Library", "LaunchAgents")
MOBILE_RELAY_LABEL = "org.agentstack.mobile-relay"


def _real_launch_agents() -> frozenset[str] | str:
    try:
        return frozenset(os.listdir(REAL_LAUNCH_AGENTS))
    except FileNotFoundError:
        return frozenset()
    except OSError:
        return UNKNOWN


def _mobile_relay_loaded() -> bool | str:
    """Whether launchd knows the phone reader job. Only `launchctl print`: reads."""
    if not LAUNCHCTL:
        return UNKNOWN
    try:
        result = _RUN([LAUNCHCTL, "print", f"gui/{os.getuid()}/{MOBILE_RELAY_LABEL}"],
                      capture_output=True, text=True, timeout=10)
    except (OSError, _SUBPROCESS.SubprocessError):
        return UNKNOWN
    return result.returncode == 0


@pytest.fixture(autouse=True, scope="session")
def _the_suite_leaves_real_launch_agents_alone():
    """Fail the run if the real ~/Library/LaunchAgents or the phone reader job changed.

    A test once ran scripts/uninstall.sh with the real HOME, and the uninstaller
    stopped the real phone reader and removed its plist. The service watcher
    above follows the install's own labels and missed it: the reader is created
    later by `agentstack-mobile`, not by the installer. This only reads (a
    directory listing and `launchctl print`); it never changes launchd.
    """
    before = (_real_launch_agents(), _mobile_relay_loaded())
    yield
    after = (_real_launch_agents(), _mobile_relay_loaded())
    problems = []
    if UNKNOWN not in (before[0], after[0]) and before[0] != after[0]:
        gone, new = sorted(before[0] - after[0]), sorted(after[0] - before[0])
        problems.append(f"{REAL_LAUNCH_AGENTS} changed (removed {gone}, added {new})")
    if UNKNOWN not in (before[1], after[1]) and before[1] != after[1]:
        problems.append(f"launchd job {MOBILE_RELAY_LABEL} went from "
                        f"{'loaded' if before[1] else 'absent'} to {'loaded' if after[1] else 'absent'}")
    if problems:
        pytest.fail("the test run changed the real user's launchd state (last test: "
                    f"{_CURRENT_TEST}): " + "; ".join(problems)
                    + ". Run uninstall/launchd code with a temporary HOME and a fake launchctl.",
                    pytrace=False)

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


def pytest_sessionstart(session):
    # Session fixtures run in every xdist worker, but not in its controller.
    # Own this monitor in the controller hook instead; workers use a no-op.
    if hasattr(session.config, 'workerinput') or os.environ.get('PYTEST_XDIST_WORKER'):
        return
    guard = _the_suite_leaves_real_services_alone()
    next(guard)
    session.config._orrery_service_watch_guard = guard


def pytest_sessionfinish(session, exitstatus):
    guard = getattr(session.config, '_orrery_service_watch_guard', None)
    if guard is None:
        return
    del session.config._orrery_service_watch_guard
    try:
        next(guard)
    except StopIteration:
        pass
    except pytest.fail.Exception as exc:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin('terminalreporter')
        if reporter is not None:
            reporter.write_sep('=', 'production-service monitor failed', red=True)
            reporter.write_line(str(exc), red=True)
