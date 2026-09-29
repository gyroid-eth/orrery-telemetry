"""WSL: keep the distro running while agents work, and only then.

WSL stops a distro about 15 s after its last Windows-side client exits, with
tmux, the dashboard and every agent inside it (WSL 2.7.13, 2026-09-29). The
dashboard keeps one hidden Windows `wsl.exe` running a foreground anchor while
there is work, and lets it end once the work is gone, so WSL's own idle stop
applies again and nobody has to `wsl --shutdown`. The Windows .wslconfig is
never written.

Nothing here reaches Windows: `powershell.exe`, `cmd.exe` and `wslpath` are
fakes, the anchor runs as a local subprocess where `wsl.exe` would start it,
and every state directory is temporary.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

import pytest

from dashboard import server, wsl_anchor

ROOT = pathlib.Path(__file__).resolve().parent.parent
ANCHOR = ROOT / "dashboard" / "wsl_anchor.py"
WSLCONFIG = ROOT / "scripts" / "lib" / "wslconfig.py"
INSTALL = ROOT / "scripts" / "install.sh"
DOCTOR = ROOT / "scripts" / "doctor.sh"
INDEX = ROOT / "dashboard" / "index.html"
HAS_PROC = pathlib.Path("/proc/self/stat").exists()


def _script(path: pathlib.Path, body: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _start_hold(directory: pathlib.Path, nonce: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ANCHOR), "hold", str(directory), nonce],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _want(directory: pathlib.Path, **data) -> None:
    wsl_anchor._write_json(directory / "wanted.json", data)


@pytest.fixture(autouse=True)
def _fast_anchor(monkeypatch):
    monkeypatch.setattr(wsl_anchor, "ANCHOR_POLL_SECONDS", 0.05)


# --- the anchor (runs inside the hidden wsl.exe) -------------------------------

def test_anchor_holds_while_wanted_and_ends_by_itself(tmp_path):
    _want(tmp_path, hold=True, until=time.time() + 60, pids=[])
    proc = _start_hold(tmp_path, "n1")
    try:
        assert _wait(lambda: wsl_anchor._read_json(tmp_path / "anchor.json").get("nonce") == "n1")
        assert wsl_anchor.anchor_running(tmp_path)
        # A second anchor for the same distro gives way at once.
        second = subprocess.run([sys.executable, str(ANCHOR), "hold", str(tmp_path), "n2"], timeout=10)
        assert second.returncode == 0
        assert wsl_anchor._read_json(tmp_path / "anchor.json")["nonce"] == "n1"
        # Release: it ends on its own; nothing kills it.
        _want(tmp_path, hold=False, until=time.time(), pids=[])
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not wsl_anchor.anchor_running(tmp_path)
    assert not (tmp_path / "anchor.json").exists()


def test_an_expired_lease_ends_the_anchor(tmp_path):
    _want(tmp_path, hold=True, until=time.time() - 1, pids=[])
    proc = _start_hold(tmp_path, "n1")
    assert proc.wait(timeout=10) == 0


@pytest.mark.skipif(not HAS_PROC, reason="process start ticks come from /proc (Linux, as in WSL)")
def test_without_the_controller_the_anchor_holds_while_recorded_agents_live(tmp_path):
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ticks = wsl_anchor.process_start_ticks(agent.pid)
        # The dashboard died: its lease has run out, but the agent it saw lives.
        _want(tmp_path, hold=True, until=time.time() - 1, pids=[[agent.pid, ticks]])
        proc = _start_hold(tmp_path, "n1")
        time.sleep(0.5)
        assert proc.poll() is None
        agent.kill()
        agent.wait()
        assert proc.wait(timeout=10) == 0
    finally:
        if agent.poll() is None:
            agent.kill()


@pytest.mark.skipif(not HAS_PROC, reason="process start ticks come from /proc (Linux, as in WSL)")
def test_a_recycled_pid_does_not_count(tmp_path):
    pid = os.getpid()
    ticks = wsl_anchor.process_start_ticks(pid)
    assert wsl_anchor.process_matches(pid, ticks)
    assert not wsl_anchor.process_matches(pid, ticks + 1)


# --- the controller (a thread in the dashboard) --------------------------------

class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class LocalStarter:
    """Where wsl.exe would run the anchor, run it as a local subprocess."""

    def __init__(self, fail=None):
        self.calls = []
        self.procs = []
        self.fail = fail

    def __call__(self, distro, argv):
        self.calls.append((distro, argv))
        if self.fail:
            return None, self.fail
        self.procs.append(subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        return 4242, ""

    def stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _controller(tmp_path, work, starter, clock):
    return wsl_anchor.Controller(
        work, tmp_path, "Ubuntu",
        lambda nonce: [sys.executable, str(ANCHOR), "hold", str(tmp_path), nonce],
        starter=starter, clock=clock, sleep_ready=lambda s: time.sleep(0.05),
    )


def test_hold_while_agents_run_then_release_after_the_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 1e9)
    clock, starter = Clock(), LocalStarter()
    work = {"now": ({"agents": 2}, [], True)}
    controller = _controller(tmp_path, lambda: work["now"], starter, clock)
    try:
        status = controller.tick()
        assert status["state"] == "holding"
        assert status["reasons"] == {"agents": 2}
        assert status["windows_pid"] == 4242
        assert starter.calls[0][0] == "Ubuntu"
        # Still working: no second anchor.
        clock.now += 5
        assert controller.tick()["state"] == "holding"
        assert len(starter.calls) == 1
        # Every agent exited: keep holding through the grace period.
        work["now"] = ({"agents": 0}, [], True)
        clock.now += 5
        status = controller.tick()
        assert status["state"] == "releasing" and status["grace_seconds_left"] == 55
        assert wsl_anchor.anchor_running(tmp_path)
        clock.now += wsl_anchor.GRACE_SECONDS
        status = controller.tick()
        assert status["state"] == "idle"
        assert wsl_anchor._read_json(tmp_path / "wanted.json")["hold"] is False
        starter.procs[0].wait(timeout=10)  # the anchor ended by itself
        assert not wsl_anchor.anchor_running(tmp_path)
        assert controller.tick()["state"] == "idle"
        assert len(starter.calls) == 1
    finally:
        starter.stop()


def test_nothing_is_started_without_work(tmp_path):
    clock, starter = Clock(), LocalStarter()
    controller = _controller(tmp_path, lambda: ({"agents": 0}, [], True), starter, clock)
    assert controller.tick()["state"] == "idle"
    assert starter.calls == []


def test_a_launch_reservation_or_a_visible_page_alone_is_work(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 1e9)
    for kind in ("reservation", "page"):
        directory = tmp_path / kind
        if kind == "reservation":
            wsl_anchor.reserve(directory, "spawn-1")
        else:
            wsl_anchor.touch_page_lease(directory, "page-1")
        clock, starter = Clock(), LocalStarter()
        controller = _controller(directory, lambda: ({"agents": 0}, [], True), starter, clock)
        try:
            status = controller.tick()
            assert status["state"] == "holding"
            assert status["reasons"] == ({"launching": 1} if kind == "reservation" else {"pages": 1})
            assert len(starter.calls) == 1
        finally:
            starter.stop()
    # Released reservations and closed pages no longer count.
    wsl_anchor.release_reservation(tmp_path / "reservation", "spawn-1")
    wsl_anchor.touch_page_lease(tmp_path / "page", "page-1", release=True)
    assert wsl_anchor.fresh_reservations(tmp_path / "reservation") == 0
    assert wsl_anchor.fresh_leases(tmp_path / "page") == 0


def test_unobservable_work_keeps_the_last_confirmed_hold_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 1e9)
    clock, starter = Clock(), LocalStarter()
    work = {"now": ({"agents": 1}, [[4321, 99]], True)}
    controller = _controller(tmp_path, lambda: work["now"], starter, clock)
    try:
        controller.tick()
        # tmux / ps stop answering for longer than the grace period.
        work["now"] = ({}, [], False)
        for _ in range(3):
            clock.now += wsl_anchor.GRACE_SECONDS
            status = controller.tick()
            assert status["state"] == "unknown"
            wanted = wsl_anchor._read_json(tmp_path / "wanted.json")
            assert wanted["hold"] is True and wanted["pids"] == [[4321, 99]]
        assert wsl_anchor.anchor_running(tmp_path)
        # Observation is back and the agent has really exited: normal release.
        work["now"] = ({"agents": 0}, [], True)
        clock.now += 5
        assert controller.tick()["state"] == "releasing"
        clock.now += wsl_anchor.GRACE_SECONDS
        assert controller.tick()["state"] == "idle"
        starter.procs[0].wait(timeout=10)
    finally:
        starter.stop()


def test_a_failing_tick_is_reported_not_hidden(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("ps exploded")

    controller = _controller(tmp_path, broken, LocalStarter(), Clock())
    monkeypatch.setattr(wsl_anchor, "TICK_SECONDS", 0.01)

    def stop_after_one(timeout):
        raise SystemExit

    monkeypatch.setattr(controller.wake, "wait", stop_after_one)
    with pytest.raises(SystemExit):
        controller.run_forever()
    status = wsl_anchor._read_json(tmp_path / "status.json")
    assert status["state"] == "error" and "ps exploded" in status["error"]


def test_a_reservation_waits_for_a_decision_in_progress(tmp_path):
    import threading

    done = threading.Event()
    with wsl_anchor._locked(tmp_path):
        thread = threading.Thread(target=lambda: (wsl_anchor.reserve(tmp_path, "spawn-2"), done.set()))
        thread.start()
        time.sleep(0.3)
        assert not done.is_set()  # blocked while the controller decides
    thread.join(timeout=5)
    assert done.is_set() and wsl_anchor.fresh_reservations(tmp_path) == 1


def test_an_anchor_that_dies_early_is_started_again(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 1e9)
    clock, starter = Clock(), LocalStarter()
    controller = _controller(tmp_path, lambda: ({"agents": 1}, [], True), starter, clock)
    try:
        controller.tick()
        starter.procs[0].kill()
        starter.procs[0].wait()
        clock.now += 5
        assert controller.tick()["state"] == "holding"
        assert len(starter.calls) == 2
    finally:
        starter.stop()


def test_a_failed_start_is_reported_and_retries_are_bounded(tmp_path):
    clock, starter = Clock(), LocalStarter(fail="powershell.exe is not reachable (Windows interop off?)")
    controller = _controller(tmp_path, lambda: ({"agents": 1}, [], True), starter, clock)
    for _ in range(5):
        status = controller.tick()
        clock.now += 5
    assert status["state"] == "failed"
    assert "interop" in status["error"]
    assert len(starter.calls) == wsl_anchor.MAX_START_FAILURES
    clock.now += wsl_anchor.RETRY_AFTER_FAILURE_SECONDS
    controller.tick()
    assert len(starter.calls) == wsl_anchor.MAX_START_FAILURES + 1


def test_the_windows_pid_is_shown_even_when_ready_came_late(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 0)  # ready "times out"
    clock, starter = Clock(), LocalStarter()
    controller = _controller(tmp_path, lambda: ({"agents": 1}, [], True), starter, clock)
    try:
        controller.tick()
        assert _wait(lambda: wsl_anchor.anchor_running(tmp_path))
        clock.now += 5
        status = controller.tick()
        assert status["state"] == "holding" and status["windows_pid"] == 4242
        # A new dashboard finds the same client on record.
        fresh = _controller(tmp_path, lambda: ({"agents": 1}, [], True), starter, clock)
        assert fresh.tick()["windows_pid"] == 4242
    finally:
        starter.stop()


def test_an_anchor_that_never_reports_ready_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 0)
    clock = Clock()
    controller = _controller(tmp_path, lambda: ({"agents": 1}, [], True), lambda d, a: (7, ""), clock)
    status = controller.tick()
    assert status["state"] == "failed"
    assert "ready" in status["error"]


def test_page_leases_and_reservations_expire(tmp_path):
    wsl_anchor.touch_page_lease(tmp_path, "page-x")
    wsl_anchor.reserve(tmp_path, "spawn-x")
    now = time.time()
    assert wsl_anchor.fresh_leases(tmp_path, now) == 1
    assert wsl_anchor.fresh_leases(tmp_path, now + wsl_anchor.PAGE_LEASE_SECONDS + 1) == 0
    assert wsl_anchor.fresh_reservations(tmp_path, now) == 1
    assert wsl_anchor.fresh_reservations(tmp_path, now + wsl_anchor.RESERVATION_SECONDS + 1) == 0
    assert not wsl_anchor.touch_page_lease(tmp_path, "../escape")


def test_the_anchor_keeps_the_last_readable_wishes_and_unknown_processes():
    now = time.time()
    original = wsl_anchor.process_state
    try:
        wsl_anchor.process_state = lambda pid, ticks: "unknown"
        assert wsl_anchor.keep_holding({"hold": False, "until": now, "pids": [[1, 2]]}, now)
        wsl_anchor.process_state = lambda pid, ticks: "dead"
        assert not wsl_anchor.keep_holding({"hold": False, "until": now, "pids": [[1, 2]]}, now)
    finally:
        wsl_anchor.process_state = original


def test_an_unreadable_wanted_file_does_not_end_the_anchor(tmp_path):
    _want(tmp_path, hold=True, until=time.time() + 60, pids=[])
    proc = _start_hold(tmp_path, "n1")
    try:
        assert _wait(lambda: wsl_anchor._read_json(tmp_path / "anchor.json").get("nonce") == "n1")
        (tmp_path / "wanted.json").write_text("{not json", encoding="utf-8")
        time.sleep(2.5)  # more than one poll of the subprocess
        assert proc.poll() is None
        _want(tmp_path, hold=False, until=time.time(), pids=[])
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


# --- starting the Windows client ------------------------------------------------

def test_the_powershell_starts_a_hidden_foreground_wsl_exe():
    script = wsl_anchor.start_script("Ubuntu", ["/usr/bin/python3", "/a/wsl_anchor.py", "hold", "/s", "n"])
    assert "Start-Process" in script and "-WindowStyle Hidden" in script and "-PassThru" in script
    assert "-Wait" not in script and "-NoNewWindow" not in script
    assert "--exec" in script and "wsl.exe" in script
    payload = re.search(r"FromBase64String\('([^']+)'\)", script).group(1)
    assert json.loads(base64.b64decode(payload)) == ["Ubuntu", "/usr/bin/python3", "/a/wsl_anchor.py", "hold", "/s", "n"]
    for forbidden in ("--shutdown", "--terminate", ".wslconfig"):
        assert forbidden not in script


def test_start_windows_client_reads_the_pid_and_refuses_unsafe_arguments(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "powershell.exe", 'echo "{\\"pid\\":31337}"\n')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert wsl_anchor.start_windows_client("Ubuntu", ["/usr/bin/python3", "x"]) == (31337, "")
    pid, error = wsl_anchor.start_windows_client("Ubuntu", ["/home/a b/python3"])
    assert pid is None and "spaces or quotes" in error
    _script(bin_dir / "powershell.exe", "echo boom >&2\nexit 1\n")
    pid, error = wsl_anchor.start_windows_client("Ubuntu", ["x"])
    assert pid is None and "boom" in error


def test_interop_falls_back_to_a_live_socket_when_the_inherited_one_is_gone(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "123_interop").touch()
    monkeypatch.setenv("WSL_INTEROP", str(run / "99_interop"))  # its window closed
    assert wsl_anchor.interop_env(str(run))["WSL_INTEROP"] == str(run / "123_interop")
    (run / "99_interop").touch()
    assert wsl_anchor.interop_env(str(run))["WSL_INTEROP"] == str(run / "99_interop")


# --- what counts as work, in the dashboard --------------------------------------

def _fake_run(outputs):
    """subprocess.run stand-in for `tmux ...`: outputs maps argv[1] to a result."""
    from types import SimpleNamespace

    def run(argv, **kwargs):
        result = outputs[argv[1]]
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(**result)

    return run


def test_work_counts_live_agent_processes_not_recorders(monkeypatch):
    sep = server.SEP
    panes = "\n".join([
        f"RedCurie{sep}100",       # claude running (maybe waiting for approval)
        f"BlueBohr{sep}200",       # codex running
        f"GreyDirac{sep}300",      # agent exited: only a shell left (a recorder may be attached)
        f"mail-watcher{sep}400",   # infra
        f"warm-opus{sep}500",      # warm-up pool
    ])
    names = {100: "bash", 101: "claude", 200: "bash", 201: "codex", 300: "bash",
             400: "bash", 401: "claude", 500: "bash", 501: "claude"}
    children = {100: [101], 200: [201], 400: [401], 500: [501]}
    monkeypatch.setattr(server.subprocess, "run", _fake_run({"list-panes": {"returncode": 0, "stdout": panes, "stderr": ""}}))
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: (names, children))
    monkeypatch.setattr(wsl_anchor, "process_start_ticks", lambda pid: pid * 10)
    monkeypatch.setattr(server, "_wsl_anchor_module", lambda: wsl_anchor)
    reasons, pids, known = server._wsl_anchor_work()
    assert known and reasons == {"agents": 2}
    assert sorted(pids) == [[101, 1010], [201, 2010]]
    assert "list-clients" not in server._wsl_anchor_work.__code__.co_consts


def test_work_that_cannot_be_observed_is_unknown_not_empty(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(server, "_wsl_anchor_module", lambda: wsl_anchor)
    sep = server.SEP
    # No tmux server: nothing can be running in it (known, empty).
    for err in ("no server running on /tmp/tmux-1000/default\n",
                "error connecting to /tmp/tmux-1000/default (No such file or directory)\n"):
        monkeypatch.setattr(server.subprocess, "run", _fake_run({"list-panes": {
            "returncode": 1, "stdout": "", "stderr": err}}))
        assert server._wsl_anchor_work() == ({"agents": 0}, [], True)
    # A socket that cannot be opened, or tmux missing right now: unknown.
    monkeypatch.setattr(server.subprocess, "run", _fake_run({"list-panes": {
        "returncode": 1, "stdout": "", "stderr": "error connecting to /tmp/tmux-1000/default (Permission denied)\n"}}))
    assert server._wsl_anchor_work()[2] is False
    monkeypatch.setattr(server.subprocess, "run", _fake_run({"list-panes": FileNotFoundError("tmux")}))
    assert server._wsl_anchor_work()[2] is False
    # tmux fails or hangs: unknown.
    monkeypatch.setattr(server.subprocess, "run", _fake_run({"list-panes": {
        "returncode": 1, "stdout": "", "stderr": "lost server"}}))
    assert server._wsl_anchor_work()[2] is False
    monkeypatch.setattr(server.subprocess, "run", _fake_run({"list-panes": sp.TimeoutExpired("tmux", 5)}))
    assert server._wsl_anchor_work()[2] is False
    # tmux answers but ps does not: unknown.
    monkeypatch.setattr(server.subprocess, "run", _fake_run({"list-panes": {
        "returncode": 0, "stdout": f"RedCurie{sep}100", "stderr": ""}}))
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: None)
    assert server._wsl_anchor_work()[2] is False


class FakeController:
    def __init__(self, directory):
        self.dir = directory
        self.wake = __import__("threading").Event()
        self.settled_calls = []

    answer = {"state": "holding"}

    def settled(self, since, timeout):
        # The reservation exists before the controller is asked to decide.
        self.settled_calls.append(wsl_anchor.fresh_reservations(self.dir))
        return self.answer


def test_a_dashboard_launch_is_reserved_before_it_starts_and_released_after(tmp_path, monkeypatch):
    fake = FakeController(tmp_path)
    monkeypatch.setitem(server._WSL_ANCHOR, "controller", fake)
    monkeypatch.setattr(server, "_wsl_anchor_module", lambda: wsl_anchor)
    seen = []

    def sync_spawn(payload):
        seen.append(wsl_anchor.fresh_reservations(tmp_path))
        return {"ok": True, "name": "RedCurie"}

    monkeypatch.setattr(server, "do_spawn", sync_spawn)
    result = server.do_spawn_held({})
    assert result["ok"] and result["wsl_hold"] == {"ok": True, "state": "holding"}
    assert fake.settled_calls == [1] and seen == [1]
    assert wsl_anchor.fresh_reservations(tmp_path) == 0
    # A failed launch releases it too.
    monkeypatch.setattr(server, "do_spawn", lambda payload: {"ok": False, "error": "boom"})
    server.do_spawn_held({})
    assert wsl_anchor.fresh_reservations(tmp_path) == 0


def test_a_hold_that_did_not_start_is_reported_with_the_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_wsl_anchor_module", lambda: wsl_anchor)
    monkeypatch.setattr(server, "do_spawn", lambda payload: {"ok": True, "child_name": "RedCurie"})
    for answer in ({"state": "failed", "error": "powershell.exe is not reachable"},
                   {"state": "timeout", "error": "the WSL hold did not start in time"}):
        fake = FakeController(tmp_path)
        fake.answer = answer
        monkeypatch.setitem(server._WSL_ANCHOR, "controller", fake)
        hold = server.do_spawn_held({})["wsl_hold"]
        assert hold["ok"] is False and hold["state"] == answer["state"]
        assert answer["error"] in hold["error"] and "Ubuntu window" in hold["hint"]


def test_a_status_from_before_the_launch_is_not_its_answer(tmp_path):
    wsl_anchor._write_json(tmp_path / "status.json", {"state": "holding", "updated": time.time() - 30})
    assert wsl_anchor.wait_settled(tmp_path, time.time(), 0.3)["state"] == "timeout"
    since = time.time()
    wsl_anchor._write_json(tmp_path / "status.json", {"state": "failed", "error": "x", "updated": since + 0.01})
    assert wsl_anchor.wait_settled(tmp_path, since, 1)["state"] == "failed"


def test_a_real_async_launch_keeps_its_reservation_until_it_settles(tmp_path, monkeypatch):
    """The real do_spawn: only Mail, Popen and the launcher are stand-ins."""
    import threading
    from types import SimpleNamespace
    from unittest import mock

    fake = FakeController(tmp_path / "state")
    launcher = _script(tmp_path / "spawn_child.sh", "exit 0\n")
    release, done = threading.Event(), threading.Event()

    class Proc:
        pid = 4321

        def wait(self, timeout=None):
            release.wait(5)
            return 0

        def poll(self):
            return 0 if release.is_set() else None

    def mcp(method, args, timeout=15):
        if method == "register_agent":
            return {"ok": True, "data": {"id": 73, "name": "QuietCurie", "registration_token": "fake"}}
        return {"ok": True, "data": {}}

    real_record = server._spawn_launch_record

    def record(name, result):
        real_record(name, result)
        if not result.get("pending"):
            done.set()

    monkeypatch.setattr(server, "_wsl_anchor_module", lambda: wsl_anchor)
    monkeypatch.setitem(server._WSL_ANCHOR, "controller", fake)
    with mock.patch.multiple(server, SPAWN_SCRIPT=str(launcher), RUNTIME_DIR=str(tmp_path / "runtime"),
                             HERE=str(tmp_path), ANNOT_PATH=str(tmp_path / "annotations.json"),
                             LEGACY_ANNOT_PATH=str(tmp_path / "legacy.json")), \
            mock.patch.object(server, "_project_key", return_value="/project"), \
            mock.patch.object(server, "_spawn_name_status", return_value="available"), \
            mock.patch.object(server, "_mcp_call", side_effect=mcp), \
            mock.patch.object(server.subprocess, "Popen", return_value=Proc()), \
            mock.patch.object(server.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
            mock.patch.object(server, "_spawn_launch_record", side_effect=record), \
            mock.patch.dict(server._SPAWN_LAUNCHES, {}, clear=True):
        result = server.do_spawn_held({"standalone": True, "name": "QuietCurie", "task": "work",
                                       "dir": str(tmp_path), "async": True})
        assert result.get("pending") is True and result.get("child_name") == "QuietCurie", result
        assert fake.settled_calls == [1]
        # The launcher is still running: the reservation must still be there.
        assert wsl_anchor.fresh_reservations(fake.dir) == 1
        release.set()
        assert done.wait(10)
    assert wsl_anchor.fresh_reservations(fake.dir) == 0


def test_page_lease_endpoint(tmp_path, monkeypatch):
    fake = FakeController(tmp_path)
    monkeypatch.setitem(server._WSL_ANCHOR, "controller", fake)
    monkeypatch.setattr(server, "_wsl_anchor_module", lambda: wsl_anchor)
    assert server.wsl_anchor_page_lease({"id": "abc123"})["ok"]
    assert wsl_anchor.fresh_leases(tmp_path) == 1 and fake.wake.is_set()
    assert server.wsl_anchor_page_lease({"id": "abc123", "release": True})["ok"]
    assert wsl_anchor.fresh_leases(tmp_path) == 0
    assert not server.wsl_anchor_page_lease({"id": "../x"})["ok"]
    monkeypatch.setitem(server._WSL_ANCHOR, "controller", None)
    assert server.wsl_anchor_page_lease({"id": "abc123"}) == {"ok": True, "active": False}


def test_shell_launchers_reserve_before_starting():
    for name, marker in (("agent-start", 'SESSION="pending-$$"'), ("agent-start-codex", 'SESSION="codex-pending-$$"')):
        text = (ROOT / "bin" / name).read_text(encoding="utf-8")
        assert text.index("ags_wsl_reserve_launch agent-start") > text.index(marker)
        assert text.index("ags_wsl_reserve_launch agent-start") < text.index("new-session")


def _reserve_cli(state, *extra):
    return subprocess.run([sys.executable, str(ANCHOR), "reserve", str(state), "tok-1", *extra],
                          capture_output=True, text=True, timeout=30)


def test_reserve_cli_waits_for_the_controller_and_reports_failures(tmp_path):
    import threading

    # No dashboard: say so at once, keep the reservation.
    result = _reserve_cli(tmp_path, "--wait", "5")
    assert result.returncode == 1 and "dashboard is not running" in result.stderr
    assert wsl_anchor.fresh_reservations(tmp_path) == 1
    # A controller that decides after the reservation: held.
    wsl_anchor._write_json(tmp_path / "status.json", {"state": "idle", "updated": time.time()})
    timer = threading.Timer(0.5, lambda: wsl_anchor._write_json(
        tmp_path / "status.json", {"state": "holding", "updated": time.time()}))
    timer.start()
    assert _reserve_cli(tmp_path, "--wait", "5").returncode == 0
    timer.join()
    # A controller that cannot hold: the reason is shown.
    wsl_anchor._write_json(tmp_path / "status.json", {"state": "idle", "updated": time.time()})
    timer = threading.Timer(0.5, lambda: wsl_anchor._write_json(
        tmp_path / "status.json", {"state": "failed", "error": "interop off", "updated": time.time()}))
    timer.start()
    result = _reserve_cli(tmp_path, "--wait", "5")
    timer.join()
    assert result.returncode == 1 and "interop off" in result.stderr
    # A reservation waits for a decision in progress (same lock).
    with wsl_anchor._locked(tmp_path):
        proc = subprocess.Popen([sys.executable, str(ANCHOR), "reserve", str(tmp_path), "tok-2"])
        time.sleep(0.5)
        assert proc.poll() is None
    assert proc.wait(timeout=10) == 0
    # An unwritable state directory is reported, not ignored.
    blocked = tmp_path / "file"
    blocked.write_text("x")
    result = _reserve_cli(blocked / "sub")
    assert result.returncode == 1 and "cannot reserve" in result.stderr


def test_the_shell_helper_uses_the_cli_and_never_blocks_the_launch(tmp_path):
    proc_version = tmp_path / "version"
    proc_version.write_text("Linux version 6.6 (microsoft-standard-WSL2)\n")
    lib = ROOT / "bin" / "lib" / "agentstack-launch.sh"
    script = f"set -euo pipefail\nAGS_PROG=agent-start\n. {shlex.quote(str(lib))}\nags_wsl_reserve_launch agent-start\necho launched\n"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "AGS_WSL_PROC_VERSION": str(proc_version),
           "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"), "AGENTSTACK_PYTHON": sys.executable,
           "AGENTSTACK_WSL_ANCHOR_HELPER": str(ANCHOR), "AGS_WSL_RESERVE_WAIT": "2"}
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and result.stdout.strip() == "launched"
    assert "dashboard is not running" in result.stderr and "continuing without a WSL hold" in result.stderr
    assert wsl_anchor.fresh_reservations(tmp_path / "runtime" / "wsl-anchor") == 1
    # Off, or outside WSL: nothing happens.
    for extra in ({"AGENTSTACK_WSL_ANCHOR": "0"}, {"AGS_WSL_PROC_VERSION": str(tmp_path / "none")}):
        other = tmp_path / ("r" + next(iter(extra.values()))[-4:])
        result = subprocess.run(["/bin/bash", "-c", script], env={**env, **extra, "AGENTSTACK_RUNTIME_DIR": str(other)},
                                capture_output=True, text=True, timeout=30)
        assert result.stdout.strip() == "launched" and result.stderr == ""
        assert not other.exists()


def test_a_known_agent_counts_until_its_death_is_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 1e9)
    clock, starter = Clock(), LocalStarter()
    work = {"now": ({"agents": 1}, [[4321, 99]], True)}
    controller = _controller(tmp_path, lambda: work["now"], starter, clock)
    states = {"v": "alive"}
    monkeypatch.setattr(wsl_anchor, "process_state", lambda pid, ticks: states["v"])
    try:
        controller.tick()
        # tmux says nothing runs, but the process is still there.
        work["now"] = ({"agents": 0}, [], True)
        clock.now += wsl_anchor.GRACE_SECONDS + 5
        status = controller.tick()
        assert status["state"] == "holding" and status["reasons"] == {"agents": 1}
        states["v"] = "dead"
        clock.now += 5
        assert controller.tick()["state"] == "releasing"
    finally:
        starter.stop()


def test_the_page_renews_its_lease_only_while_visible():
    html = INDEX.read_text(encoding="utf-8")
    assert "/api/wsl-anchor/lease" in html
    assert "setInterval(()=>{ if(!document.hidden) wslPageLease(false); },15000);" in html
    assert "window.addEventListener('pagehide',()=>wslPageLease(true));" in html


def test_status_is_unsupported_outside_wsl(monkeypatch):
    monkeypatch.setattr(server, "_is_wsl", lambda: False)
    assert server.wsl_anchor_status()["state"] == "unsupported"
    monkeypatch.setattr(server, "_is_wsl", lambda: True)
    monkeypatch.setenv("AGENTSTACK_WSL_ANCHOR", "0")
    assert server.wsl_anchor_status()["state"] == "disabled"


def test_the_anchor_is_not_started_outside_wsl_or_when_off(monkeypatch):
    monkeypatch.setitem(server._WSL_ANCHOR, "controller", None)
    monkeypatch.setattr(server, "_is_wsl", lambda: False)
    server._start_wsl_anchor()
    assert server._WSL_ANCHOR["controller"] is None
    monkeypatch.setattr(server, "_is_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("AGENTSTACK_WSL_ANCHOR", "0")
    server._start_wsl_anchor()
    assert server._WSL_ANCHOR["controller"] is None


# --- the header ------------------------------------------------------------------

def _view(status: dict) -> dict:
    html = INDEX.read_text(encoding="utf-8")
    block = re.search(r"function wslAnchorView\(d\)\{.*?\n\}\n", html, re.DOTALL).group(0)
    script = block + f"\nconsole.log(JSON.stringify(wslAnchorView({json.dumps(status)})));\n"
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_header_shows_why_wsl_is_kept_and_hides_once_released():
    view = _view({"state": "holding", "reasons": {"agents": 2, "pages": 1}, "age": 1})
    assert view["show"] and view["label"] == "WSL kept · 2 agents · page open"
    view = _view({"state": "releasing", "reasons": {}, "grace_seconds_left": 42, "age": 1})
    assert view["show"] and view["label"] == "WSL hold ends in 42s"
    assert "stops only if no window" in view["title"]
    view = _view({"state": "unknown", "reasons": {"unknown": 1}, "age": 1})
    assert view["show"] and view["label"] == "WSL kept · state unknown"
    view = _view({"state": "failed", "error": "powershell.exe is not reachable", "age": 1})
    assert view["show"] and view["status"] == "red" and "powershell.exe" in view["title"]
    view = _view({"state": "error", "error": "RuntimeError: x", "age": 1})
    assert view["show"] and view["label"] == "WSL hold error"
    view = _view({"state": "holding", "reasons": {"agents": 1}, "age": 120})
    assert view["label"] == "WSL status stale" and view["status"] == "red"
    for state in ("idle", "unsupported", "disabled", "stopped"):
        assert _view({"state": state})["show"] is False
    html = INDEX.read_text(encoding="utf-8")
    assert '<div id="wsl-anchor" class="mail-health" hidden' in html
    assert "#wsl-anchor[hidden]{display:none}" in html


# --- .wslconfig is read, never written --------------------------------------------

def _parse(text: str) -> dict:
    spec = __import__("importlib.util").util.spec_from_file_location("wslconfig", WSLCONFIG)
    module = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse(text)


def test_wslconfig_reader_understands_only_what_wsl_reads():
    assert _parse("[general]\ninstanceIdleTimeout=-1 # keep\n[wsl2]\nvmIdleTimeout=-1\n") == {
        "instanceIdleTimeout": {"state": "set", "value": -1},
        "vmIdleTimeout": {"state": "set", "value": -1},
    }
    assert _parse("[wsl2]\nmemory=8GB\n")["instanceIdleTimeout"]["state"] == "unset"
    assert _parse("[general]\ninstanceIdleTimeout=15000\n")["instanceIdleTimeout"]["value"] == 15000
    # Each of these would be read differently by WSL: never "set".
    for text in (
        "[general]\ninstanceIdleTimeout=-1 ; keep\n",
        "[ general ]\ninstanceIdleTimeout=-1\n",
        "[general]\ninstanceIdleTimeout=-2147483649\n",
        '[general]\ninstanceIdleTimeout="-1"\n',
        "[General]\ninstanceIdleTimeout=-1\n",
        "[general]\ninstanceIdleTimeout=-1\ninstanceIdleTimeout=-1\n",
        "[general]\ninstanceIdleTimeout=-08\n",   # invalid octal for WSL
        "[general]\ninstanceIdleTimeout=-010\n",  # octal -8 for WSL
    ):
        assert _parse(text)["instanceIdleTimeout"]["state"] == "unknown", text
    # A key under another section is not the setting.
    assert _parse("[wsl2]\ninstanceIdleTimeout=-1\n")["instanceIdleTimeout"]["state"] == "unset"


def test_nothing_writes_the_windows_wslconfig():
    source = WSLCONFIG.read_text(encoding="utf-8")
    assert "write_bytes" not in source and "write_text" not in source and "os.replace" not in source


def test_windows_profile_survives_a_japanese_name_and_an_ampersand(tmp_path):
    profile = tmp_path / "c" / "Users" / "利用者 & co"
    profile.mkdir(parents=True)
    raw = "USERPROFILE=C:\\Users\\利用者 & co\r\n".encode("utf-16-le")
    (tmp_path / "raw").write_bytes(raw)
    cmd = _script(tmp_path / "bin" / "cmd.exe",
                  f'[ "$1 $2 $3 $4" = "/d /u /c set USERPROFILE" ] || exit 9\ncat {shlex.quote(str(tmp_path / "raw"))}\n')
    wslpath = _script(tmp_path / "bin" / "wslpath",
                      f'[ "$2" = "C:\\\\Users\\\\利用者 & co" ] || exit 1\necho {shlex.quote(str(profile))}\n')
    spec = __import__("importlib.util").util.spec_from_file_location("wslconfig", WSLCONFIG)
    module = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.windows_profile(str(cmd), str(wslpath)) == profile
    (tmp_path / "raw").write_bytes(b"\xff")  # odd length: not UTF-16
    assert module.windows_profile(str(cmd), str(wslpath)) is None


# --- installer and doctor ----------------------------------------------------------

def test_installer_records_the_choice_and_rejects_other_values(tmp_path):
    source = INSTALL.read_text(encoding="utf-8")
    env_block = source[source.index("write_env_file() {"):source.index("stop_new_agent_mail() {")]
    assert '"AGENTSTACK_WSL_ANCHOR": "$WSL_ANCHOR_SETTING"' in env_block
    assert "wslconfig.py\" ensure" not in source and "instanceIdleTimeout=-1 to" not in source
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["/bin/bash", str(INSTALL), "--dry-run", "--install-dir", str(tmp_path / "stack"),
         "--project-key", str(tmp_path)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "AGENTSTACK_WSL_ANCHOR": "yes"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "AGENTSTACK_WSL_ANCHOR must be 0 or 1" in result.stderr


def _block(path: pathlib.Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8")
    a = text.index(start)
    return text[a:text.index(end, a) + len(end)]


def _doctor(tmp_path, *, status=None, wslconfig=None, setting=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime = tmp_path / "runtime"
    if status is not None:
        wsl_anchor._write_json(runtime / "wsl-anchor" / "status.json", status)
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    if wslconfig is not None:
        (profile / ".wslconfig").write_bytes(wslconfig)
    raw = tmp_path / "raw"
    raw.write_bytes("USERPROFILE=C:\\Users\\tester\r\n".encode("utf-16-le"))
    bin_dir = tmp_path / "bin"
    _script(bin_dir / "cmd.exe", f"cat {shlex.quote(str(raw))}\n")
    _script(bin_dir / "wslpath", f"echo {shlex.quote(str(profile))}\n")
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={shlex.quote(str(ROOT / 'scripts'))}\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        f"INSTALL_DIR={shlex.quote(str(tmp_path))}\n"
        "running_under_wsl() { return 0; }\n"
        + _block(DOCTOR, "# --- WSL anchor", "# --- end WSL anchor ---")
        + "\nreport_wsl_anchor\n"
    )
    env = {"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin", "AGENTSTACK_RUNTIME_DIR": str(runtime)}
    if setting is not None:
        env["AGENTSTACK_WSL_ANCHOR"] = setting
    return subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)


def test_doctor_reports_the_anchor(tmp_path):
    now = time.time()
    out = _doctor(tmp_path / "hold", status={"state": "holding", "reasons": {"agents": 2},
                                             "windows_pid": 77, "updated": now}).stdout
    assert "ok: WSL anchor: keeping WSL running for agents 2 (Windows wsl.exe PID 77)" in out
    out = _doctor(tmp_path / "fail", status={"state": "failed", "error": "powershell.exe is not reachable",
                                             "updated": now}).stdout
    assert "warn: WSL anchor: cannot keep WSL running for the agents: powershell.exe is not reachable" in out
    out = _doctor(tmp_path / "stale", status={"state": "holding", "updated": now - 600}).stdout
    assert "warn: WSL anchor: status is" in out
    out = _doctor(tmp_path / "none").stdout
    assert "warn: WSL anchor: no status yet" in out
    out = _doctor(tmp_path / "off", setting="0").stdout
    assert "note: WSL anchor is off" in out and "warn:" not in out


def test_doctor_explains_a_global_idle_setting_and_how_to_undo_it(tmp_path):
    status = {"state": "idle", "updated": time.time()}
    out = _doctor(tmp_path / "set", status=status,
                  wslconfig=b"[wsl2]\r\nvmIdleTimeout=-1\r\n\r\n[general]\r\ninstanceIdleTimeout=-1\r\n").stdout
    assert "instanceIdleTimeout=-1: every distro keeps running until 'wsl --shutdown'" in out
    assert "vmIdleTimeout=-1: the WSL VM keeps running" in out
    assert "to undo: delete that line" in out
    out = _doctor(tmp_path / "odd", status=status, wslconfig=b"[general]\r\ninstanceIdleTimeout=-1 ; x\r\n").stdout
    assert "cannot tell how WSL reads instanceIdleTimeout" in out
    out = _doctor(tmp_path / "plain", status=status, wslconfig=b"[wsl2]\r\nmemory=8GB\r\n").stdout
    assert "note:" not in out
