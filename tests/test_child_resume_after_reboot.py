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


def test_prune_keeps_a_lease_rewritten_after_it_was_judged(tmp_path, monkeypatch):
    """#182 review: SessionStart may rewrite a stale lease path (a reused PID)."""
    holder = tmp_path / "live-sessions" / NAME
    holder.mkdir(parents=True)
    lease = holder / "999999"  # no such process: judged dead
    lease.write_text("")
    os.utime(lease, (time.time() - 3600,) * 2)
    original = child_resume._lease_alive

    def judged_then_rewritten(path, boot):
        verdict = original(path, boot)
        path.write_text("rewritten")  # a session claims the path meanwhile
        return verdict

    monkeypatch.setattr(child_resume, "_lease_alive", judged_then_rewritten)
    child_resume.prune_leases(tmp_path, boot=_boot())
    assert lease.exists()
