"""Children a forced shutdown stopped mid-run become resumable again.

162nd seminar: after each reboot the agents could not be resumed ("retired_at
is missing") until they were retired by hand. A child's state says "running"
until its normal exit retires it, and a forced shutdown skips that exit.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from dashboard import server
from hooks import child_resume
from test_claude_resume_mail import NAME, TOKEN, private, resume  # noqa: F401


def _active_child(runtime, registration, *, written_before_boot=True):
    state = {**registration, "registration_token": TOKEN}
    path = runtime / "child-agents" / f"{NAME}.json"
    private(path, json.dumps(state))
    child_resume.prepare_active_state(runtime, NAME, project_key=registration["project_key"])
    stamp = time.time() - (3600 if written_before_boot else 0)
    os.utime(path, (stamp, stamp))
    return path


def _boot():
    return time.time() - 60  # booted a minute ago


def _state(runtime):
    return json.loads((runtime / "child-agents" / f"{NAME}.json").read_text())


def test_a_child_a_reboot_stopped_is_refused_with_a_plain_reason_then_recovered(resume):
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    refused = server.do_resume(NAME)
    assert not refused["ok"] and "did not end normally" in refused["error"]
    recovered = child_resume.recover_after_reboot(runtime, is_live=lambda _n: False,
                                                  retention_days=30, boot=_boot())
    assert recovered == [(NAME, "retired")]
    assert _state(runtime)["retired_at"] and _state(runtime)["resume_expires_at"]
    assert server.do_resume(NAME)["ok"] and launches


@pytest.mark.parametrize("live", [True, None])
def test_a_live_or_unknown_session_is_left_alone(resume, live):
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    before = _state(runtime)
    assert child_resume.recover_after_reboot(runtime, is_live=lambda _n: live,
                                             retention_days=30, boot=_boot()) == []
    assert _state(runtime) == before


def test_state_written_since_the_boot_is_left_alone(resume):
    """A child being spawned now has running state before its tmux session exists."""
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration, written_before_boot=False)
    assert child_resume.recover_after_reboot(runtime, is_live=lambda _n: False,
                                             retention_days=30, boot=_boot()) == []
    assert _state(runtime)["retired_at"] is None


def test_an_unknown_boot_time_changes_nothing(resume, monkeypatch):
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    monkeypatch.setattr(child_resume, "boot_time", lambda: None)
    assert child_resume.recover_after_reboot(runtime, is_live=lambda _n: False, retention_days=30) == []


@pytest.mark.parametrize("damage", ["pending_registration", "other_token", "retention_off"])
def test_unsafe_or_disabled_cases_are_left_alone(resume, damage):
    runtime, registration, launches, calls = resume
    path = _active_child(runtime, registration)
    retention = 30
    if damage == "pending_registration":
        private(path.with_name(f".{NAME}.registration-pending.json"), "{}")
    elif damage == "other_token":
        private(runtime / f"agent_token_{NAME}", "another-owner")
    else:
        retention = 0
    os.utime(path, (time.time() - 3600,) * 2)
    assert child_resume.recover_after_reboot(runtime, is_live=lambda _n: False,
                                             retention_days=retention, boot=_boot()) == []
    assert _state(runtime)["retired_at"] is None


def test_a_resume_cut_off_by_a_reboot_is_cleared(resume):
    runtime, registration, launches, calls = resume
    path = _active_child(runtime, registration)
    child_resume.mark_retired(runtime, NAME, retention_days=30)
    assert server.do_resume(NAME)["ok"]
    assert _state(runtime)["resume_in_progress_at"]
    refused = server.do_resume(NAME)
    assert not refused["ok"] and "did not finish" in refused["error"]
    os.utime(path, (time.time() - 3600,) * 2)
    assert child_resume.recover_after_reboot(runtime, is_live=lambda _n: False,
                                             retention_days=30, boot=_boot()) == [(NAME, "resume_cleared")]
    assert "resume_in_progress_at" not in _state(runtime)
    assert server.do_resume(NAME)["ok"]


def test_a_lease_from_before_the_boot_does_not_count_as_alive(tmp_path):
    """A reboot reuses PIDs: an old lease may name an unrelated live process."""
    holder = tmp_path / "live-sessions" / NAME
    holder.mkdir(parents=True)
    lease = holder / str(os.getpid())  # a live PID
    lease.write_text("")
    os.utime(lease, (time.time() - 3600,) * 2)
    assert child_resume.live_lease(tmp_path, NAME, boot=_boot()) is False
    child_resume.prune_leases(tmp_path, boot=_boot())
    assert not holder.exists()
    holder.mkdir(parents=True)
    (holder / str(os.getpid())).write_text("")  # written since the boot
    assert child_resume.live_lease(tmp_path, NAME, boot=_boot()) is True


def test_the_lease_commands_answer_for_the_hooks(tmp_path):
    holder = tmp_path / "live-sessions" / NAME
    holder.mkdir(parents=True)
    (holder / str(os.getpid())).write_text("")
    helper = child_resume.__file__
    run = lambda *a: subprocess.run(["python3", helper, *a, "--runtime-dir", str(tmp_path)],  # noqa: E731
                                    capture_output=True, check=False).returncode
    assert run("live-lease", "--agent-name", NAME) == 0
    assert run("live-lease", "--agent-name", "OtherName") == 1
    assert run("prune-leases") == 0 and (holder / str(os.getpid())).exists()


@pytest.mark.parametrize("returncode,stderr,expected", [
    (0, "", True),
    (1, "can't find session: X", False),
    (1, "no server running on /tmp/tmux-501/default", False),
    (1, "something unexpected", None),
    (1, "error connecting to /tmp/tmux-501/default (No such file or directory)", False),
    (1, "error connecting to /tmp/tmux-501/default (Permission denied)", None),
])
def test_the_dashboard_tells_a_live_child_from_a_gone_one(monkeypatch, tmp_path, returncode, stderr, expected):
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(server, "HOOKS_DIR", os.path.dirname(child_resume.__file__))
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, "", stderr))
    assert server._child_session_live(NAME, _boot()) is expected


def test_the_dashboard_recovers_at_start(resume, monkeypatch):
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    # The dashboard loads its own instance of child_resume.py from HOOKS_DIR.
    monkeypatch.setattr(server._child_resume_module(), "boot_time", _boot)
    monkeypatch.setattr(server, "_child_session_live", lambda name, boot: False)
    server._recover_children_after_reboot()
    assert _state(runtime)["retired_at"]


def test_a_session_that_starts_while_recovery_waits_for_the_lock_is_left_alone(resume):
    """#182 review: liveness is checked again under the lock, before writing."""
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    answers = iter([False, True])  # gone before the lock, live by the time it writes
    assert child_resume.recover_after_reboot(runtime, is_live=lambda _n: next(answers),
                                             retention_days=30, boot=_boot()) == []
    assert _state(runtime)["retired_at"] is None


def _lease_lock_is_held(runtime) -> bool:
    import fcntl

    (runtime / "live-sessions").mkdir(exist_ok=True)
    fd = os.open(runtime / "live-sessions" / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def test_recovery_takes_its_last_look_and_writes_under_the_lease_lock(resume):
    """#182 review: a lease written by SessionStart cannot land between the
    last liveness check and the write, because the writer takes the same lock."""
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    held = []

    def is_live(_name):
        held.append(_lease_lock_is_held(runtime))
        return False

    assert child_resume.recover_after_reboot(runtime, is_live=is_live, retention_days=30,
                                             boot=_boot()) == [(NAME, "retired")]
    assert held == [False, True]  # the first look is outside, the last one inside


def test_prune_holds_the_lease_lock_while_it_judges_and_removes(tmp_path, monkeypatch):
    holder = tmp_path / "live-sessions" / NAME
    holder.mkdir(parents=True)
    (holder / "999999").write_text("")
    held = []
    original = child_resume._lease_alive
    monkeypatch.setattr(child_resume, "_lease_alive",
                        lambda path, boot: held.append(_lease_lock_is_held(tmp_path)) or original(path, boot))
    child_resume.prune_leases(tmp_path, boot=_boot())
    assert held == [True] and not holder.exists()


def test_a_lease_writer_waits_for_the_lock_then_writes_anyway(tmp_path):
    import threading

    with child_resume._LeaseLock(tmp_path):
        done = threading.Event()
        threading.Thread(target=lambda: (child_resume.write_lease(tmp_path, NAME, os.getpid()), done.set()),
                         daemon=True).start()
        assert not done.wait(0.3)  # blocked while the lock is held
    assert done.wait(5)
    assert (tmp_path / "live-sessions" / NAME / str(os.getpid())).exists()
    started = time.monotonic()
    with child_resume._LeaseLock(tmp_path):
        with child_resume._LeaseLock(tmp_path, wait=0.2) as late:
            assert late.descriptor is None  # gave up waiting: the caller still writes
    assert time.monotonic() - started < 2


def test_write_lease_command(tmp_path):
    helper = child_resume.__file__
    done = subprocess.run(["python3", helper, "write-lease", "--runtime-dir", str(tmp_path),
                           "--agent-name", NAME, "--pid", str(os.getpid())], capture_output=True, check=False)
    assert done.returncode == 0, done.stderr
    assert child_resume.live_lease(tmp_path, NAME, boot=_boot()) is True
    bad = subprocess.run(["python3", helper, "write-lease", "--runtime-dir", str(tmp_path),
                          "--agent-name", "../x", "--pid", "5"], capture_output=True, check=False)
    assert bad.returncode != 0 and not (tmp_path / "x").exists()
