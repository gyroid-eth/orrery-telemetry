"""The mail watcher's recovery scan, and what it may treat as a delivered signal.

On Linux, fswatch's inotify backend adds recursive watches itself: a signal
written into a recipient folder that was just created is reported late or not
at all (inotify(7), "Limitations and caveats"). The watcher removes that folder
after each delivery, so on WSL nearly every message waited for the 30-second
periodic scan (2026-09-29). The scan is now 2 seconds on Linux and stays 30 on
macOS.

The end-to-end cases run the real hooks/watch_agent_mail_signals.sh against a
fake `uname` (so either OS path runs on any CI host), a fake fswatch that
writes signals and prints the events it chooses, and a fake delivery helper.
Nothing touches a real Mail server, tmux or runtime. The last test uses the
real fswatch on Linux only.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WATCHER = ROOT / "hooks" / "watch_agent_mail_signals.sh"
# A 2-second scan delivers in 2-3 seconds; the old 30-second scan took ~30.
# The bound leaves room for a loaded runner (each scan starts several python
# processes) while still failing the old behaviour by a wide margin.
FAST_ENOUGH = 8.0


def _executable(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fake_uname(bin_dir: pathlib.Path, system: str) -> None:
    _executable(bin_dir / "uname", f"#!/bin/sh\necho {system}\n")


# --- the scan interval --------------------------------------------------------

def _scan_interval(tmp_path: pathlib.Path, system: str, override: str | None = None) -> str:
    source = WATCHER.read_text(encoding="utf-8")
    start = source.index('case "$(uname -s 2>/dev/null || true)" in')
    end = source.index("RETRY_COOLDOWN=30", start)
    bin_dir = tmp_path / f"bin-{system}"
    bin_dir.mkdir(exist_ok=True)
    _fake_uname(bin_dir, system)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    if override is not None:
        env["AGENTSTACK_MAIL_WATCHER_SCAN_INTERVAL"] = override
    result = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + source[start:end] + 'printf "%s" "$SCAN_INTERVAL"\n'],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_linux_scans_every_two_seconds_and_macos_keeps_thirty(tmp_path):
    assert _scan_interval(tmp_path, "Linux") == "2"
    assert _scan_interval(tmp_path, "Darwin") == "30"


@pytest.mark.parametrize(("override", "expected"), [
    ("5", "5"), ("08", "8"), ("0", "2"), ("abc", "2"), ("", "2"), ("-3", "2"),
])
def test_the_scan_interval_override_accepts_only_positive_whole_seconds(tmp_path, override, expected):
    assert _scan_interval(tmp_path, "Linux", override) == expected


def test_the_retry_cooldown_is_not_shortened_with_the_scan():
    assert re.search(r"^RETRY_COOLDOWN=30\b", WATCHER.read_text(encoding="utf-8"), re.M)


# --- the watcher, end to end ---------------------------------------------------

# The fake fswatch creates the signal the scenario describes and prints the
# events a racy inotify backend would. It never exits by itself.
FAKE_FSWATCH = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["AGENTSTACK_SIGNALS_DIR"])
out = pathlib.Path(os.environ["PROBE_ROOT"])
scenario = os.environ["PROBE_SCENARIO"]
agents = root / "projects" / "p" / "agents"
recipient = agents / "ProbeAgent"
target = recipient / "777.signal"
payload = json.dumps({"message": {"id": 777, "from": "ProbeSender",
                                  "subject": "probe subject", "importance": "normal"}})
def emit(path):
    print(str(path), flush=True)
def written():
    (out / "written.json").write_text(json.dumps({"at": time.monotonic()}))
time.sleep(0.3)
if scenario == "new-recipient":          # the folder event only, as on WSL
    recipient.mkdir(parents=True); target.write_text(payload); written(); emit(recipient)
elif scenario == "new-hierarchy":        # a new project: only its top folder is reported
    target.parent.mkdir(parents=True); target.write_text(payload); written(); emit(root / "projects" / "p")
elif scenario == "late-file":            # the folder is reported before the file exists
    recipient.mkdir(parents=True); emit(recipient); time.sleep(0.6); target.write_text(payload); written()
elif scenario == "burst":                # the same message reported again and again
    recipient.mkdir(parents=True); target.write_text(payload); written()
    for _ in range(8):
        emit(recipient); emit(target)
elif scenario == "partial-json":         # a reader catches the write half done
    recipient.mkdir(parents=True); target.write_text('{"message": {"id": 777, "fr'); emit(target)
    time.sleep(1.0); target.write_text(payload); written(); emit(target)
time.sleep(120)
'''

# Stands in for the persistent headless delivery helper: records what it was
# handed and reports success, so the watcher marks success and removes the file.
FAKE_DELIVER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
signal_file = pathlib.Path(args[args.index("--signal-file") + 1])
try:
    subject = json.loads(signal_file.read_text())["message"]["subject"]
except Exception as exc:
    subject = f"unreadable: {exc}"
with (pathlib.Path(os.environ["PROBE_ROOT"]) / "deliveries.jsonl").open("a") as fh:
    fh.write(json.dumps({"at": time.monotonic(), "subject": subject}) + "\n")
'''


def _start_watcher(folder: pathlib.Path, system: str, *, fswatch: str | None, scenario: str = ""):
    bin_dir = folder / "bin"
    bin_dir.mkdir()
    signals_dir = folder / "signals"
    (signals_dir / "projects").mkdir(parents=True)
    _fake_uname(bin_dir, system)
    if fswatch is None:
        _executable(bin_dir / "fswatch", FAKE_FSWATCH)
    else:
        os.symlink(fswatch, bin_dir / "fswatch")
    _executable(bin_dir / "deliver", FAKE_DELIVER)
    _executable(bin_dir / "tmux", "#!/bin/sh\nexit 91\n")
    env = {
        **os.environ,
        "HOME": str(folder / "home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AGENTSTACK_SIGNALS_DIR": str(signals_dir),
        "AGENTSTACK_RUNTIME_DIR": str(folder / "runtime"),
        "AGENTSTACK_MAIL_WATCHER_LOCK_DIR": str(folder / "lock"),
        "AGENTSTACK_MAIL_WATCHER_PIDFILE": str(folder / "lock" / "pid"),
        "AGENTSTACK_MAIL_WATCHER_HEARTBEAT": str(folder / "lock" / "heartbeat"),
        "AGENTSTACK_PERSISTENT_DELIVER": str(bin_dir / "deliver"),
        "PROBE_ROOT": str(folder),
        "PROBE_SCENARIO": scenario,
    }
    env.pop("AGENTSTACK_MAIL_WATCHER_SCAN_INTERVAL", None)
    log = (folder / "watcher.log").open("w")
    process = subprocess.Popen(["/bin/bash", str(WATCHER)], env=env, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    return process, log, signals_dir


def _stop(process, log) -> None:
    alive = process.poll() is None
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()
    log.close()
    # The watcher runs until it is stopped; exiting on its own is a failure.
    assert alive, "the watcher exited by itself"


def _deliveries(folder: pathlib.Path, wait: float) -> list[dict]:
    path = folder / "deliveries.jsonl"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.05)
    time.sleep(1.5)  # let any duplicate arrive before counting
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("scenario", ["new-recipient", "new-hierarchy", "late-file", "burst"])
def test_linux_delivers_a_signal_its_file_event_never_reported_within_seconds(tmp_path, scenario):
    process, log, _ = _start_watcher(tmp_path, "Linux", fswatch=None, scenario=scenario)
    try:
        delivered = _deliveries(tmp_path, wait=15)
    finally:
        _stop(process, log)
    assert len(delivered) == 1, (tmp_path / "watcher.log").read_text()
    written = json.loads((tmp_path / "written.json").read_text())["at"]
    latency = delivered[0]["at"] - written
    print(f"{scenario}: delivered {latency:.2f}s after the write")
    assert latency < FAST_ENOUGH, delivered
    assert delivered[0]["subject"] == "probe subject"
    state = json.loads((tmp_path / "runtime" / "notify-state.json").read_text())
    assert state["ProbeAgent:777"]["last_result"] == "success"


def test_a_half_written_signal_is_left_for_the_next_scan_not_delivered_empty(tmp_path):
    process, log, _ = _start_watcher(tmp_path, "Linux", fswatch=None, scenario="partial-json")
    try:
        delivered = _deliveries(tmp_path, wait=15)
    finally:
        _stop(process, log)
    assert [d["subject"] for d in delivered] == ["probe subject"], (tmp_path / "watcher.log").read_text()


def test_macos_still_waits_for_its_thirty_second_scan(tmp_path):
    # The same folder-only event on macOS is not picked up within a few
    # seconds: its behaviour is unchanged by this fix.
    process, log, signals_dir = _start_watcher(tmp_path, "Darwin", fswatch=None, scenario="new-recipient")
    try:
        delivered = _deliveries(tmp_path, wait=5)
        assert delivered == []
        assert (signals_dir / "projects" / "p" / "agents" / "ProbeAgent" / "777.signal").exists()
    finally:
        _stop(process, log)
    assert "recovery scan every 30s" in (tmp_path / "watcher.log").read_text()


# --- the lease gap -------------------------------------------------------------

def _function(source: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", source, re.M | re.S)
    assert match, name
    return match.group(0)


def test_a_lease_freed_by_a_finished_worker_does_not_start_a_second_one(tmp_path):
    """The previous worker records success and frees the lease between this
    handler's state check and its lease: the handler must re-check and stand
    down instead of starting another worker for the same message."""
    source = WATCHER.read_text(encoding="utf-8")
    functions = "".join(_function(source, name) for name in (
        "importance_rank", "importance_at_least", "read_signal_meta", "state_should_attempt",
        "state_mark_result", "release_delivery_lease", "handle_signal_file"))
    functions += _function(source, "acquire_delivery_lease").replace("acquire_delivery_lease()", "real_acquire()")
    signal_file = tmp_path / "signals" / "agents" / "ProbeAgent" / "777.signal"
    signal_file.parent.mkdir(parents=True)
    signal_file.write_text(json.dumps({"message": {"id": 777, "from": "Sender", "subject": "gap"}}))
    leases = tmp_path / "leases"
    script = (
        "set -euo pipefail\n" + functions
        + f"STATE_FILE={shlex.quote(str(tmp_path / 'state.json'))}\n"
        + f"LEASE_DIR={shlex.quote(str(leases))}\n"
        + "RETRY_COOLDOWN=30\nLEASE_TTL=330\nNOTIFY_MIN_IMPORTANCE=low\nMAX_WORKERS=12\n"
        + 'mkdir -p "$LEASE_DIR"\nlog() { :; }\n'
        + "OLD_OWNER=$(real_acquire ProbeAgent 777)\n"
        + "acquire_delivery_lease() {\n"
        + "    state_mark_result ProbeAgent 777 success prior-worker\n"
        + '    release_delivery_lease ProbeAgent 777 "$OLD_OWNER"\n'
        + '    real_acquire "$@"\n'
        + "}\n"
        + f"deliver_worker() {{ echo worker >> {shlex.quote(str(tmp_path / 'workers'))}; }}\n"
        + f"handle_signal_file {shlex.quote(str(signal_file))}\nwait\n"
    )
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "workers").exists(), "a second worker started for a delivered message"
    assert not (leases / "ProbeAgent-777.lock").exists(), "the handler kept a lease it did not use"


# --- real fswatch on Linux -------------------------------------------------------

def _inotify_watches(pid: int) -> set[int]:
    inodes: set[int] = set()
    for info in pathlib.Path(f"/proc/{pid}/fdinfo").iterdir():
        try:
            text = info.read_text()
        except OSError:
            continue
        inodes.update(int(m.group(1), 16) for m in re.finditer(r"^inotify wd:\S+ ino:([0-9a-f]+)", text, re.M))
    return inodes


def _child_fswatch(watcher_pid: int, deadline: float) -> int:
    while time.monotonic() < deadline:
        result = subprocess.run(["pgrep", "-P", str(watcher_pid), "fswatch"], capture_output=True, text=True)
        if result.stdout.strip():
            return int(result.stdout.split()[0])
        time.sleep(0.05)
    raise TimeoutError("the watcher did not start fswatch")


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("fswatch") is None,
                    reason="real inotify via fswatch: Linux with fswatch only")
def test_real_fswatch_a_new_recipient_folder_is_delivered_within_seconds():
    # fswatch is paused while the folder and its signal are created, so its
    # watch for the new folder is certainly not in place when the file lands.
    with tempfile.TemporaryDirectory(prefix="orrery-watcher-inotify-", dir="/tmp") as td:
        folder = pathlib.Path(td)
        process, log, signals_dir = _start_watcher(folder, "Linux", fswatch=shutil.which("fswatch"))
        try:
            agents = signals_dir / "projects" / "p" / "agents"
            agents.mkdir(parents=True)
            deadline = time.monotonic() + 10
            fswatch_pid = _child_fswatch(process.pid, deadline)
            while agents.stat().st_ino not in _inotify_watches(fswatch_pid):
                assert time.monotonic() < deadline, "fswatch never watched the agents folder"
                time.sleep(0.05)
            os.kill(fswatch_pid, signal.SIGSTOP)
            try:
                target = agents / "ProbeAgent" / "777.signal"
                target.parent.mkdir()
                target.write_text(json.dumps({"message": {"id": 777, "from": "ProbeSender",
                                                          "subject": "probe subject"}}))
                written = time.monotonic()
            finally:
                os.kill(fswatch_pid, signal.SIGCONT)
            delivered = _deliveries(folder, wait=15)
        finally:
            _stop(process, log)
        assert len(delivered) == 1, (folder / "watcher.log").read_text()
        latency = delivered[0]["at"] - written
        print(f"real fswatch: delivered {latency:.2f}s after the write")
        assert latency < FAST_ENOUGH, delivered
