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
    work = {"now": ({"agents": 2, "launching": 0, "cockpit": 0}, [])}
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
        work["now"] = ({"agents": 0, "launching": 0, "cockpit": 0}, [])
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
    controller = _controller(tmp_path, lambda: ({"agents": 0, "launching": 0, "cockpit": 0}, []), starter, clock)
    assert controller.tick()["state"] == "idle"
    assert starter.calls == []


def test_a_launch_or_a_cockpit_viewer_alone_is_work(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 1e9)
    for reasons in ({"launching": 1}, {"cockpit": 1}):
        directory = tmp_path / next(iter(reasons))
        clock, starter = Clock(), LocalStarter()
        controller = _controller(directory, lambda: (reasons, []), starter, clock)
        try:
            assert controller.tick()["state"] == "holding"
            assert len(starter.calls) == 1
        finally:
            starter.stop()


def test_an_anchor_that_dies_early_is_started_again(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 1e9)
    clock, starter = Clock(), LocalStarter()
    controller = _controller(tmp_path, lambda: ({"agents": 1}, []), starter, clock)
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
    controller = _controller(tmp_path, lambda: ({"agents": 1}, []), starter, clock)
    for _ in range(5):
        status = controller.tick()
        clock.now += 5
    assert status["state"] == "failed"
    assert "interop" in status["error"]
    assert len(starter.calls) == wsl_anchor.MAX_START_FAILURES
    clock.now += wsl_anchor.RETRY_AFTER_FAILURE_SECONDS
    controller.tick()
    assert len(starter.calls) == wsl_anchor.MAX_START_FAILURES + 1


def test_an_anchor_that_never_reports_ready_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(wsl_anchor, "READY_TIMEOUT_SECONDS", 0)
    clock = Clock()
    controller = _controller(tmp_path, lambda: ({"agents": 1}, []), lambda d, a: (7, ""), clock)
    status = controller.tick()
    assert status["state"] == "failed"
    assert "ready" in status["error"]


def test_viewer_leases_expire(tmp_path):
    leases = tmp_path / "leases"
    leases.mkdir()
    (leases / "cockpit").touch()
    now = time.time()
    assert wsl_anchor.fresh_leases(tmp_path, now) == 1
    assert wsl_anchor.fresh_leases(tmp_path, now + wsl_anchor.LEASE_SECONDS + 1) == 0


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

def test_work_counts_live_agent_processes_launches_and_cockpit_viewers(tmp_path, monkeypatch):
    sep = server.SEP
    panes = "\n".join([
        f"RedCurie{sep}100",       # claude running (maybe waiting for approval)
        f"BlueBohr{sep}200",       # codex running
        f"GreyDirac{sep}300",      # agent exited: only a shell left
        f"mail-watcher{sep}400",   # infra
        f"warm-opus{sep}500",      # warm-up pool
    ])
    names = {100: "bash", 101: "claude", 200: "bash", 201: "codex", 300: "bash",
             400: "bash", 401: "claude", 500: "bash", 501: "claude"}
    children = {100: [101], 200: [201], 400: [401], 500: [501]}

    def fake_tmux(args):
        if args[0] == "list-panes":
            return panes
        if args[0] == "list-clients":
            return "0\n1\n"
        return ""

    monkeypatch.setattr(server, "_tmux", fake_tmux)
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: (names, children))
    monkeypatch.setattr(wsl_anchor, "process_start_ticks", lambda pid: pid * 10)
    monkeypatch.setattr(server, "_wsl_anchor_module", lambda: wsl_anchor)
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setitem(server._SPAWN_LAUNCHES, "NewAgent", {"ts": time.time(), "started": time.time(),
                                                               "state": "launching", "result": {}})
    reasons, pids = server._wsl_anchor_work()
    assert reasons == {"agents": 2, "launching": 1, "cockpit": 1}
    assert sorted(pids) == [[101, 1010], [201, 2010]]


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
    view = _view({"state": "holding", "reasons": {"agents": 2, "cockpit": 1}})
    assert view["show"] and view["label"] == "WSL kept · 2 agents · cockpit open"
    view = _view({"state": "releasing", "reasons": {}, "grace_seconds_left": 42})
    assert view["show"] and "42s" in view["label"]
    view = _view({"state": "failed", "error": "powershell.exe is not reachable"})
    assert view["show"] and view["status"] == "red" and "powershell.exe" in view["title"]
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
