"""The production-service watcher must not report what it could not measure.

Three times in one day this project read "cannot tell" as "stopped": launchd's
loaded-versus-running, a monkeypatched `shutil.which` that hid launchctl from a
watcher thread, and a TCP accept counted as an answer. Each time the wrong
reading blamed something real -- a test, a supervisor, an outage -- and sent
someone looking for a cause that did not exist.

These tests drive the watcher's own helpers against tools that fail in the ways
real tools fail.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import threading
import time
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent


def _load_conftest():
    spec = importlib.util.spec_from_file_location("_watcher_conftest", TESTS_DIR / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_tool(tmp_path: Path, name: str, body: str) -> Path:
    tool = tmp_path / name
    tool.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    tool.chmod(0o755)
    return tool


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    [
        ("a diagnostic failure", 'echo "lsof: something went wrong" >&2\nexit 2\n', "unknown"),
        ("output that is not pids", 'echo "not-a-pid"\nexit 0\n', "unknown"),
        ("nothing listening", "exit 1\n", "empty"),
        ("a listener", "echo 4321\nexit 0\n", "pids"),
    ],
)
def test_the_listener_probe_separates_absence_from_failure(
    tmp_path: Path, label: str, body: str, expected: str
) -> None:
    module = _load_conftest()
    module.LSOF = str(_fake_tool(tmp_path, "lsof", body))
    result = module._listener_pids(8765)
    if expected == "unknown":
        assert result == module.UNKNOWN, label
    elif expected == "empty":
        assert result == (), label
    else:
        assert result == (4321,), label


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    [
        ("a diagnostic failure", 'echo "launchctl: broken" >&2\nexit 2\n', "unknown"),
        ("no such service", "exit 113\n", "absent"),
        ("running", 'echo "	state = running"\necho "	pid = 999"\nexit 0\n', "pid"),
        ("loaded but not running", 'echo "	state = waiting"\nexit 0\n', "absent"),
        ("a successful silence", "exit 0\n", "unknown"),
    ],
)
def test_the_job_probe_separates_absence_from_failure(
    tmp_path: Path, label: str, body: str, expected: str
) -> None:
    module = _load_conftest()
    module.LAUNCHCTL = str(_fake_tool(tmp_path, "launchctl", body))
    result = module._job_pid("org.example.whatever")
    if expected == "unknown":
        assert result == module.UNKNOWN, label
    elif expected == "absent":
        assert result is None, label
    else:
        assert result == 999, label


def test_a_missing_tool_is_not_a_stopped_service(tmp_path: Path) -> None:
    module = _load_conftest()
    module.LAUNCHCTL = str(tmp_path / "definitely-not-here")
    module.LSOF = str(tmp_path / "also-not-here")
    assert module._job_pid("org.example.whatever") == module.UNKNOWN
    assert module._listener_pids(8765) == module.UNKNOWN


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    [
        ("rc1 with a diagnostic", 'echo "lsof: WARNING" >&2\nexit 1\n', "unknown"),
        ("rc0 with no output", "exit 0\n", "unknown"),
    ],
)
def test_the_listener_probe_needs_a_positive_absence(
    tmp_path: Path, label: str, body: str, expected: str
) -> None:
    """"An exit code we tolerate" is not "the tool said nothing is there".

    lsof reports no match as rc1 with both streams empty, and uses rc1 with a
    diagnostic for its own troubles. Reading them the same way blamed whichever
    test was running for stopping the service.
    """
    module = _load_conftest()
    module.LSOF = str(_fake_tool(tmp_path, "lsof", body))
    assert module._listener_pids(8765) == module.UNKNOWN, label


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("running without a pid", 'echo "	state = running"\nexit 0\n'),
        ("a state this code does not know", 'echo "	state = spinning"\nexit 0\n'),
        ("output with no state at all", 'echo "	something = else"\nexit 0\n'),
    ],
)
def test_the_job_probe_needs_a_recognised_state(tmp_path: Path, label: str, body: str) -> None:
    module = _load_conftest()
    module.LAUNCHCTL = str(_fake_tool(tmp_path, "launchctl", body))
    assert module._job_pid("org.example.whatever") == module.UNKNOWN, label


def test_a_nested_pid_before_the_job_s_own_does_not_win(tmp_path: Path) -> None:
    """Depth decides, not the order fields happen to appear in.

    Anchoring on "the indent of the first relevant field" worked on this
    machine's output and would break the moment a nested block listed a pid
    first: the nested value would then define what top level meant.
    """
    module = _load_conftest()
    fixture = 'org.example = {\n\tspawn = {\n\t\tpid = 444\n\t\tstate = active\n\t}\n\tstate = running\n\tpid = 999\n}'
    tool = tmp_path / "launchctl"
    tool.write_text("#!/bin/bash\ncat <<'EOF'\n" + fixture + "\nEOF\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    module.LAUNCHCTL = str(tool)
    assert module._job_pid("org.example.whatever") == 999


def test_the_job_probe_reads_the_job_and_not_its_nested_blocks(tmp_path: Path) -> None:
    """Real `launchctl print` nests blocks that have their own state.

    Taking the last `state = ...` seen let a nested "active" overwrite the
    job's own "running", so the actual service on this machine measured as
    unmeasurable -- the watcher then skipped it silently, which is the failure
    mode this whole guard exists to prevent.
    """
    module = _load_conftest()
    module.LAUNCHCTL = str(
        _fake_tool(
            tmp_path,
            "launchctl",
            'printf "\\tstate = running\\n\\tpid = 999\\n\\t\\tstate = active\\n\\t\\tstate = active\\n"\nexit 0\n',
        )
    )
    assert module._job_pid("org.example.whatever") == 999


def test_an_unknown_top_level_state_is_still_unmeasurable(tmp_path: Path) -> None:
    """The null case: indentation awareness must not swallow the strictness."""
    module = _load_conftest()
    module.LAUNCHCTL = str(
        _fake_tool(
            tmp_path,
            "launchctl",
            'printf "\\tstate = spinning\\n\\t\\tstate = active\\n"\nexit 0\n',
        )
    )
    assert module._job_pid("org.example.whatever") == module.UNKNOWN


def test_output_that_nests_without_braces_is_still_read_correctly(tmp_path: Path) -> None:
    """Braces are the primary signal; indentation is the fallback.

    A launchctl that nested by indentation alone would otherwise present every
    line as the job's own -- so a nested state would decide the verdict again,
    the way it did before depth was considered at all.
    """
    module = _load_conftest()
    fixture = "\n".join(
        [
            "\tstate = running",
            "\tpid = 999",
            "\t\tstate = active",
            "\t\tpid = 444",
        ]
    )
    tool = tmp_path / "launchctl"
    tool.write_text("#!/bin/bash\ncat <<'EOF'\n" + fixture + "\nEOF\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    module.LAUNCHCTL = str(tool)
    assert module._job_pid("org.example.whatever") == 999


def test_a_brace_less_nested_block_listed_first_does_not_win(tmp_path: Path) -> None:
    """The job's own fields are the shallowest, wherever they appear.

    Anchoring on the first candidate line let an indent-nested block that came
    first define what top level meant -- the same shape as the bug this parser
    was rewritten to fix, one layer down.
    """
    module = _load_conftest()
    fixture = "\n".join(
        [
            "\t\tstate = active",
            "\t\tpid = 444",
            "\tstate = running",
            "\tpid = 999",
        ]
    )
    tool = tmp_path / "launchctl"
    tool.write_text("#!/bin/bash\ncat <<'EOF'\n" + fixture + "\nEOF\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    module.LAUNCHCTL = str(tool)
    assert module._job_pid("org.example.whatever") == 999


def test_teardown_reports_a_process_it_could_not_stop(tmp_path: Path) -> None:
    """A cleanup that gives up must not return the same way one that worked does.

    stop_dashboard swept twenty times and then returned success regardless, so
    a surviving dashboard looked exactly like a clean teardown -- four of them
    accumulated on this machine without any test failing.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    # The lookup only runs for a home that has an install in it.
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)

    # A process that matches the marker and cannot be killed: pretend the
    # sweeps never clear it.
    marker = str((home / ".agentstack" / "dashboard").resolve())
    original_run = service_teardown._run_command

    def fake_run(args, *rest, **kwargs):
        if args and args[0] == "pgrep" and marker in args:
            class _Result:
                returncode = 0
                stdout = "424242\n"
                stderr = ""

            return _Result()
        return original_run(args, *rest, **kwargs)

    original_kill = service_teardown.os.kill
    service_teardown._run_command = fake_run
    service_teardown.os.kill = lambda *_args: None
    try:
        with pytest.raises(RuntimeError, match="survived teardown"):
            service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown._run_command = original_run
        service_teardown.os.kill = original_kill


@pytest.mark.parametrize(
    ("label", "returncode", "stdout", "stderr"),
    [
        ("a diagnostic failure", 2, "", "pgrep: something is wrong"),
        ("success with no output", 0, "", ""),
        ("output that is not pids", 0, "not-a-pid\n", ""),
        ("no match, but with a warning", 1, "", "pgrep: warning"),
    ],
)
def test_teardown_refuses_to_read_a_failed_lookup_as_no_processes(
    tmp_path: Path, label: str, returncode: int, stdout: str, stderr: str
) -> None:
    """"We could not look" is not "nothing is there".

    This applies where there is something recorded to look for. With a pid file
    present, a lookup that cannot answer leaves the question open, and the
    honest outcome is an error rather than a clean teardown.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    # Evidence that a service ran here: a state file from a previous runner,
    # with no pid file to go with it. That is exactly when the inventory is the
    # only way left to find a process, so a lookup that cannot answer leaves a
    # real question open.
    (home / ".agentstack" / "runtime" / "dashboard-service.json").write_text(
        json.dumps({"supervisor_pid": 999999, "server_path": str(home)}), encoding="utf-8"
    )
    original_run = service_teardown._run_command

    def fake_run(args, *rest, **kwargs):
        if args and args[0] == "pgrep":
            class _Result:
                pass

            result = _Result()
            result.returncode = returncode
            result.stdout = stdout
            result.stderr = stderr
            return result
        return original_run(args, *rest, **kwargs)

    service_teardown._run_command = fake_run
    try:
        # The stale pid file is cleared first, then the inventory is consulted
        # for anything else, and it cannot answer.
        with pytest.raises(RuntimeError):
            service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown._run_command = original_run


def test_an_unanswerable_lookup_with_nothing_recorded_is_not_an_error(
    tmp_path: Path,
) -> None:
    """An install that never recorded a pid mostly never started anything.

    Several suites install with a system-manager stub that starts no service.
    For those there is nothing to look for, and a lookup that cannot answer
    says nothing about a leak -- raising made them fail wherever listing
    processes is unavailable, which is the environment this whole thread of
    review has been about.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "dashboard").mkdir(parents=True)
    original_run = service_teardown._run_command

    def cannot_answer(args, *rest, **kwargs):
        if args and args[0] == "pgrep":
            class _Result:
                returncode = 3
                stdout = ""
                stderr = "pgrep: Cannot get process list"

            return _Result()
        return original_run(args, *rest, **kwargs)

    service_teardown._run_command = cannot_answer
    try:
        service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown._run_command = original_run


def test_teardown_still_accepts_a_genuine_no_match(tmp_path: Path) -> None:
    """The null case: nothing to clean up must stay quiet."""
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    # The lookup only runs for a home that has an install in it.
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    original_run = service_teardown._run_command

    def fake_run(args, *rest, **kwargs):
        if args and args[0] == "pgrep":
            class _Result:
                returncode = 1
                stdout = ""
                stderr = ""

            return _Result()
        return original_run(args, *rest, **kwargs)

    service_teardown._run_command = fake_run
    try:
        service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown._run_command = original_run


def test_a_warning_on_stderr_disqualifies_a_successful_lookup(tmp_path: Path) -> None:
    """rc0 with a diagnostic is a qualified answer, not a complete one.

    A partial listing -- a permission warning, a truncated result -- would
    otherwise let teardown kill what it could see and report success while
    processes it never saw kept running.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    # The lookup only runs for a home that has an install in it.
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    # Evidence a runner recorded itself here, so the inventory is consulted.
    (home / ".agentstack" / "runtime" / "dashboard-service.json").write_text(
        json.dumps({"supervisor_pid": 999999, "server_path": str(home)}), encoding="utf-8"
    )
    original_run = service_teardown._run_command

    def fake_run(args, *rest, **kwargs):
        if args and args[0] == "pgrep":
            class _Result:
                returncode = 0
                stdout = "123\n"
                stderr = "pgrep: warning: partial results"

            return _Result()
        return original_run(args, *rest, **kwargs)

    service_teardown._run_command = fake_run
    try:
        with pytest.raises(RuntimeError, match="could not answer cleanly"):
            service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown._run_command = original_run


def test_a_real_pgrep_run_against_a_marker_nothing_matches(tmp_path: Path) -> None:
    """One case that actually runs the tool, so "portable" is not just fakes.

    On a machine where listing processes is unavailable this raises rather than
    reporting a clean teardown -- which is the intended behaviour, and the
    reason the module fixture no longer asks unless something was installed.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    # The lookup only runs for a home that has an install in it.
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    try:
        service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    except RuntimeError as exc:
        assert "pgrep" in str(exc), exc
        pytest.skip(f"process listing unavailable here: {exc}")


def _install_with_supervisor(home: Path, pid: int, server_path: Path) -> Path:
    runtime = home / ".agentstack" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "dashboard.pid").write_text(f"{pid}\n", encoding="utf-8")
    (runtime / "dashboard-service.json").write_text(
        json.dumps({"supervisor_pid": pid, "server_path": str(server_path)}),
        encoding="utf-8",
    )
    return runtime


def test_teardown_uses_the_recorded_pid_without_listing_processes(tmp_path: Path) -> None:
    """Cleanup must work where listing processes is unavailable.

    The review sandbox answers `pgrep` with "Cannot get process list", so a
    teardown that can only work by inventory fails there for reasons unrelated
    to what it is cleaning up.
    """
    import service_teardown

    home = tmp_path / "home"
    server_path = home / ".agentstack" / "dashboard" / "server.py"
    server_path.parent.mkdir(parents=True)
    server_path.touch()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    runtime = _install_with_supervisor(home, child.pid, server_path)
    # Reap in the background: a real dashboard is not this process's child, so
    # it disappears when it exits. Ours would linger as a zombie, which is
    # still a live pid to os.kill.
    reaper = threading.Thread(target=child.wait, daemon=True)
    reaper.start()

    def _refuse_to_list(*_args, **_kwargs):
        raise AssertionError("teardown listed processes when it did not need to")

    original_run = service_teardown._run_command
    service_teardown._run_command = _refuse_to_list
    try:
        service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown._run_command = original_run
        child.kill()
        reaper.join(timeout=10)

    assert not (runtime / "dashboard.pid").exists()


def test_teardown_refuses_a_pid_this_install_does_not_claim(tmp_path: Path) -> None:
    """A pid in a file is not provenance.

    The first version of this signalled whatever number it found, and its own
    test proved it by killing an unrelated sleeper -- specifying exactly the
    behaviour that makes a cleanup dangerous. The installer itself refuses to
    stop a pid its state does not name as the supervisor.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    # The lookup only runs for a home that has an install in it.
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (home / ".agentstack" / "runtime" / "dashboard.pid").write_text(
            f"{bystander.pid}\n", encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="not this install's supervisor"):
            service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
        assert bystander.poll() is None, "the bystander was killed"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_teardown_refuses_a_supervisor_that_serves_another_home(tmp_path: Path) -> None:
    """The null case for the check above, from the other side."""
    import service_teardown

    home = tmp_path / "home"
    elsewhere = tmp_path / "somebody-else" / "server.py"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.touch()
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _install_with_supervisor(home, bystander.pid, elsewhere)
        with pytest.raises(RuntimeError, match="not this install's supervisor"):
            service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


@pytest.mark.parametrize("contents", ["", "123 456\n", "not-a-pid\n", "1\n"])
def test_a_pid_file_that_is_not_one_pid_is_refused(tmp_path: Path, contents: str) -> None:
    """The file has a contract: exactly one pid. Anything else is not read."""
    import service_teardown

    home = tmp_path / "home"
    runtime = home / ".agentstack" / "runtime"
    runtime.mkdir(parents=True)
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    (runtime / "dashboard.pid").write_text(contents, encoding="utf-8")
    # An empty file is refused like any other malformed one: a teardown error
    # is the honest outcome when the record of what to stop is unreadable.
    with pytest.raises(RuntimeError, match="refusing to act on it"):
        service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")


def test_teardown_reports_a_recorded_process_it_cannot_stop(tmp_path: Path) -> None:
    """A survivor must still be reported, not assumed gone."""
    import service_teardown

    home = tmp_path / "home"
    server_path = home / ".agentstack" / "dashboard" / "server.py"
    server_path.parent.mkdir(parents=True)
    server_path.touch()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _install_with_supervisor(home, child.pid, server_path)
    reaper = threading.Thread(target=child.wait, daemon=True)
    reaper.start()

    original_kill = service_teardown.os.kill

    def kill_that_does_nothing(pid, sig):
        if sig == 0:
            return original_kill(pid, 0)
        return None

    service_teardown.os.kill = kill_that_does_nothing
    try:
        with pytest.raises(RuntimeError, match="survived teardown"):
            service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown.os.kill = original_kill
        child.kill()
        reaper.join(timeout=10)


def test_teardown_stops_a_real_runner_and_its_child(tmp_path: Path) -> None:
    """The graceful path, end to end: real runner, real child, no inventory.

    Everything above uses sleepers. That measures the pid bookkeeping and
    nothing else -- whether stopping the recorded supervisor actually ends the
    server it supervises is a separate claim, and it is the one the cleanup
    depends on.

    Scope: this is the graceful path. The runner receives SIGTERM and forwards
    it, and the log confirms that is what happened -- the watchdog never has to
    act. The other route, a SIGKILL'd supervisor leaving the child to notice
    the closed pipe, is covered by
    ``test_dashboard_service.py::test_supervised_child_exits_if_runner_is_sigkilled``.

    A stub that ignores SIGTERM was tried here to force that route and does not
    work: the watchdog ends the process by sending itself SIGTERM
    (``dashboard/server.py:4828``), so ignoring the signal defeats the very
    mechanism under test. Driving it needs the dedicated test's approach.
    """
    import re

    root = TESTS_DIR.parent
    runner = root / "dashboard" / "service_runner.py"
    home = tmp_path / "home"
    runtime = home / ".agentstack" / "runtime"
    runtime.mkdir(parents=True)
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)

    # Under the install, the way a real one is: the provenance check requires
    # the supervised server to live in this home, which is what makes a
    # recorded pid this install's rather than merely a number it wrote down.
    server = home / ".agentstack" / "dashboard" / "server_stub.py"
    server.parent.mkdir(parents=True, exist_ok=True)
    server.write_text(
        "import time\n"
        "from dashboard.server import _start_supervisor_watchdog\n"
        "_start_supervisor_watchdog()\n"
        "print('server-ready', flush=True)\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )

    log_path = runtime / "dashboard.log"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(root),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_DASHBOARD_LOG": str(log_path),
        "AGENTSTACK_DASHBOARD_RUN_STATE": str(runtime / "dashboard-service.json"),
        "AGENTSTACK_DASHBOARD_RESTART_DELAY": "0",
    })
    process = subprocess.Popen([sys.executable, str(runner), str(server)], env=env)
    reaper = threading.Thread(target=process.wait, daemon=True)
    reaper.start()
    child_pid = 0
    try:
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            if "server-ready" in text:
                break
            time.sleep(0.2)
        assert "server-ready" in text, text
        child_pid = int(re.findall(r"dashboard server started child_pid=(\d+)", text)[-1])

        # This is what an install records, written the way install.sh does.
        (runtime / "dashboard.pid").write_text(f"{process.pid}\n", encoding="utf-8")

        import service_teardown

        original_run = service_teardown._run_command

        def _refuse_to_list(*_args, **_kwargs):
            raise AssertionError("teardown listed processes when it did not need to")

        service_teardown._run_command = _refuse_to_list
        try:
            service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
        finally:
            service_teardown._run_command = original_run

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.2)
        else:
            raise AssertionError(f"the supervised server {child_pid} outlived its runner")
    finally:
        for pid in (child_pid, process.pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        reaper.join(timeout=10)


def test_the_harness_helper_also_refuses_an_unclaimed_pid(tmp_path: Path) -> None:
    """The unsafe kill must not simply move to another helper.

    It did once: after the shared teardown started checking provenance, the
    module's own fake-systemd cleanup was still killing whatever pid it found.
    Both callers go through the same rule now, and this pins that they do.
    """
    import test_runtime_defaults

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    # The lookup only runs for a home that has an install in it.
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pidfile = tmp_path / "dashboard-service.pid"
    try:
        pidfile.write_text(f"{bystander.pid}\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not this install's supervisor"):
            test_runtime_defaults._stop_pid_recorded_by_the_harness(pidfile, home)
        assert bystander.poll() is None, "the bystander was killed"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_a_pid_file_that_appears_during_the_wait_is_still_checked(tmp_path: Path) -> None:
    """The wait exists because the file can arrive late; so can the check.

    stop_dashboard asked for provenance once, before the appear loop, and then
    signalled the file's first token afterwards without asking again. A pid
    file written 50ms into that wait reached the unverified path -- and an
    unrelated sleeper named in one really did get terminated.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "runtime").mkdir(parents=True)
    # The lookup only runs for a home that has an install in it.
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pidfile = home / ".agentstack" / "runtime" / "dashboard.pid"

    def write_late():
        time.sleep(0.05)
        pidfile.write_text(f"{bystander.pid}\n", encoding="utf-8")

    writer = threading.Thread(target=write_late)
    original_run = service_teardown._run_command

    def no_match(args, *rest, **kwargs):
        if args and args[0] == "pgrep":
            class _Result:
                returncode = 1
                stdout = ""
                stderr = ""

            return _Result()
        return original_run(args, *rest, **kwargs)

    service_teardown._run_command = no_match
    writer.start()
    try:
        with pytest.raises(RuntimeError, match="not this install's supervisor"):
            service_teardown.stop_dashboard(home, appear_timeout=2.0, label_prefix="")
        assert bystander.poll() is None, "the late-arriving pid was signalled unchecked"
    finally:
        service_teardown._run_command = original_run
        writer.join(timeout=5)
        bystander.kill()
        bystander.wait(timeout=10)


def test_a_stale_pid_file_for_a_process_that_has_exited_is_not_an_error(
    tmp_path: Path,
) -> None:
    """The null case: nothing to stop is a clean teardown, not a refusal.

    Requiring provenance for a pid that no longer exists turned the ordinary
    end of a test into a teardown failure.
    """
    import service_teardown

    home = tmp_path / "home"
    runtime = home / ".agentstack" / "runtime"
    runtime.mkdir(parents=True)
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait(timeout=10)
    pidfile = runtime / "dashboard.pid"
    pidfile.write_text(f"{done.pid}\n", encoding="utf-8")

    service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    assert not pidfile.exists()


@pytest.mark.parametrize(
    "contents",
    [
        "\n123\n",
        "123\n456\n",
        "\n\n",
        " 424242 \n",
        "\t99\n",
        "99 \n",
        # Digits in another script: `\d` matches them and int() parses them,
        # so this reached the provenance check as a real pid.
        "\u0664\u0662\u0664\u0662\n",
        "１２３\n",
    ]
)
def test_a_pid_file_with_extra_lines_is_refused(tmp_path: Path, contents: str) -> None:
    """One line, one pid. Splitting on all whitespace accepted more than that."""
    import service_teardown

    home = tmp_path / "home"
    runtime = home / ".agentstack" / "runtime"
    runtime.mkdir(parents=True)
    (home / ".agentstack" / "dashboard").mkdir(parents=True, exist_ok=True)
    (runtime / "dashboard.pid").write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to act on it"):
        service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")


def test_the_fake_system_manager_does_not_kill_on_disable(tmp_path: Path) -> None:
    """Generated shell is part of the kill graph too.

    The audit that concluded "two kill sites" searched Python for os.kill and
    missed the `kill` in the shell this module writes -- a bystander named in
    the pid file really was terminated by `systemctl disable`.
    """
    import test_runtime_defaults

    fake = tmp_path / "systemctl"
    fake.write_text(test_runtime_defaults._fake_systemctl(), encoding="utf-8")
    fake.chmod(0o755)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pidfile = tmp_path / "service.pid"
    try:
        pidfile.write_text(f"{bystander.pid}\n", encoding="utf-8")
        subprocess.run(
            [str(fake), "--user", "disable", "whatever"],
            env={**os.environ, "AGENTSTACK_TEST_SERVICE_PID": str(pidfile)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        time.sleep(0.5)
        assert bystander.poll() is None, "systemctl disable killed a bystander"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


def test_a_home_with_no_install_is_not_a_lookup(tmp_path: Path) -> None:
    """Nothing installed means nothing to find, without asking anybody.

    Two tests that install nothing still called teardown, which fell through
    to the process inventory and failed in a sandbox where listing processes is
    unavailable. Whether this install exists is a filesystem fact; only whether
    its process is running needs a lookup.
    """
    import service_teardown

    original_run = service_teardown._run_command

    def _refuse_to_list(*_args, **_kwargs):
        raise AssertionError("teardown looked for processes for an install that is not there")

    service_teardown._run_command = _refuse_to_list
    try:
        service_teardown.stop_dashboard(tmp_path / "never-installed", appear_timeout=0.1,
                                        label_prefix="")
    finally:
        service_teardown._run_command = original_run


def test_an_installed_home_without_a_pid_file_still_looks(tmp_path: Path) -> None:
    """The null case: skipping the lookup entirely would hide a real leak.

    An install whose pid file is missing -- killed, or never written -- is
    exactly when the inventory is the only way left to find the process.
    """
    import service_teardown

    home = tmp_path / "home"
    (home / ".agentstack" / "dashboard").mkdir(parents=True)
    asked = []

    def record(args, *rest, **kwargs):
        asked.append(args)

        class _Result:
            returncode = 1
            stdout = ""
            stderr = ""

        return _Result()

    original_run = service_teardown._run_command
    service_teardown._run_command = record
    try:
        service_teardown.stop_dashboard(home, appear_timeout=0.1, label_prefix="")
    finally:
        service_teardown._run_command = original_run
    assert any(args and args[0] == "pgrep" for args in asked), asked
