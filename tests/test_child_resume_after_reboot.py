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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def test_dashboard_recovers_a_child_that_exits_between_maintenance_passes(resume, monkeypatch):
    runtime, registration, launches, calls = resume
    registration["retired_at"] = None  # its Mail row stayed active
    boot = time.time() - 3600
    monkeypatch.setattr(server._child_resume_module(), "boot_time", lambda: boot)
    monkeypatch.setattr(server._child_resume_module(), "child_process_live", lambda name: False)
    monkeypatch.setattr(server, "_child_session_live", lambda name, boot: False)
    monkeypatch.setattr(server, "_has_session", lambda name: False)
    monkeypatch.setattr(server, "_cached_claude_transcript_path",
                        lambda name: (True, server._transcript_path(name), None))

    def between_passes(seconds):
        assert seconds == 30
        if (runtime / "child-agents" / f"{NAME}.json").exists():
            raise StopIteration
        path = _active_child(runtime, registration, written_before_boot=False)
        os.utime(path, (time.time() - 120,) * 2)
        assert server._resume_capability_for_row(NAME, "claude-code", category="gone") == "config_unrestorable"

    monkeypatch.setattr(server.threading, "Thread", lambda *, target, **kwargs: SimpleNamespace(start=target))
    monkeypatch.setattr(server.time, "sleep", between_passes)
    with pytest.raises(StopIteration):
        server._start_child_resume_maintenance()

    assert _state(runtime)["retired_at"] and _state(runtime)["resume_expires_at"]
    assert server._resume_capability_for_row(NAME, "claude-code", category="gone") == "ready"
    assert not calls
    assert server.do_jump(NAME)["ok"] and launches
    assert [method for method, _ in calls] == ["register_agent", "unretire_agent"]


@pytest.mark.parametrize("liveness", ["tmux", "lease", "process", "lease_after_tmux", "unknown", "stopped"])
def test_hourly_expiry_purges_only_a_confirmed_stopped_child(resume, monkeypatch, liveness):
    runtime, registration, _, _ = resume
    state = _active_child(runtime, registration)
    token = runtime / f"agent_token_{NAME}"
    tombstone = runtime / "child-resume-tombstones" / f"{registration['agent_id']}.json"
    child_resume.mark_retired(runtime, NAME, retention_days=30,
                              now=datetime.now(timezone.utc) - timedelta(days=31))
    if liveness == "lease":
        child_resume.write_lease(runtime, NAME, os.getpid())
    module = server._child_resume_module()
    monkeypatch.setattr(module, "boot_time", _boot)

    def live_process(_name):
        if liveness == "lease_after_tmux":
            assert not _lease_lock_is_held(runtime)
            child_resume.write_lease(runtime, NAME, os.getpid())
        return liveness == "process"

    monkeypatch.setattr(module, "child_process_live", live_process)

    def tmux(args, **_kwargs):
        if liveness == "unknown":
            return subprocess.CompletedProcess(args, 1, "", "unavailable")
        return subprocess.CompletedProcess(args, 0, NAME if liveness == "tmux" else "", "")

    def stop(_seconds):
        raise StopIteration

    monkeypatch.setattr(server, "_tmux_run", tmux)
    monkeypatch.setattr(server.threading, "Thread", lambda *, target, **kw: SimpleNamespace(start=target))
    monkeypatch.setattr(server.time, "sleep", stop)
    with pytest.raises(StopIteration):
        server._start_child_resume_maintenance()

    stopped = liveness == "stopped"
    assert state.exists() == token.exists() == (not stopped)
    assert tombstone.exists() is stopped


def test_interrupted_codex_resume_clears_the_marker_and_keeps_retention(resume, monkeypatch):
    runtime, registration, _launches, _calls = resume
    registration = {**registration, "program": "codex"}
    path = _active_child(runtime, registration)
    now = datetime.now(timezone.utc)
    boot = now.timestamp() - 3600
    started = now - timedelta(minutes=2)
    identity = {key: registration[key] for key in ("agent_id", "project_key", "program")}
    assert child_resume.mark_retired(runtime, NAME, retention_days=30, now=started)
    child_resume.begin_resume(runtime, NAME, now=started, **identity)
    os.utime(path, (started.timestamp(),) * 2)
    monkeypatch.setattr(child_resume, "child_process_live", lambda name: False)

    before = _state(runtime)
    assert child_resume.recover_stopped_children(runtime, is_live=lambda name: False,
                                                 retention_days=30, boot=boot, now=now) == [(NAME, "resume_cleared")]
    assert child_resume.inspect_retained(runtime, NAME, now=now, **identity) == {
        key: value for key, value in before.items() if key != "resume_in_progress_at"
    }
    child_resume.begin_resume(runtime, NAME, now=now, **identity)
    assert _state(runtime)["resume_in_progress_at"]


@pytest.mark.parametrize("resuming", [False, True])
def test_startup_grace_protects_fresh_launches_and_resume_markers(resume, monkeypatch, resuming):
    runtime, registration, _launches, _calls = resume
    path = _active_child(runtime, registration, written_before_boot=False)
    if resuming:
        assert child_resume.mark_retired(runtime, NAME, retention_days=30)
        identity = {key: registration[key] for key in ("agent_id", "project_key", "program")}
        child_resume.begin_resume(runtime, NAME, **identity)
        os.utime(path, (time.time() - 120,) * 2)  # the timestamp still protects it
    monkeypatch.setattr(child_resume, "child_process_live", lambda name: pytest.fail("fresh child probed"))
    before = _state(runtime)
    assert child_resume.recover_stopped_children(runtime, is_live=lambda name: False,
                                                 retention_days=30, boot=time.time() - 3600) == []
    assert _state(runtime) == before


def test_a_live_lease_keeps_a_child_without_tmux_running(resume, monkeypatch):
    runtime, registration, _launches, _calls = resume
    path = _active_child(runtime, registration, written_before_boot=False)
    os.utime(path, (time.time() - 120,) * 2)
    child_resume.write_lease(runtime, NAME, os.getpid())
    monkeypatch.setattr(child_resume, "child_process_live", lambda name: False)
    assert child_resume.recover_stopped_children(runtime, is_live=lambda name: False,
                                                 retention_days=30, boot=time.time() - 3600) == []
    assert _state(runtime)["retired_at"] is None


def test_unknown_tmux_status_keeps_a_child_running(resume, monkeypatch):
    runtime, registration, _launches, _calls = resume
    path = _active_child(runtime, registration, written_before_boot=False)
    os.utime(path, (time.time() - 120,) * 2)
    monkeypatch.setattr(child_resume, "child_process_live", lambda name: pytest.fail("tmux status unknown"))
    assert child_resume.recover_stopped_children(runtime, is_live=lambda name: None,
                                                 retention_days=30, boot=time.time() - 3600) == []
    assert _state(runtime)["retired_at"] is None


@pytest.mark.parametrize("tty,command_line,recovered", [
    ("ttys001", f"/bin/codex AGENT_NAME={NAME}", False),
    ("ttys001", "(codex)", False),
    ("??", f"fixture-app AGENT_NAME={NAME}", True),
], ids=["attached", "environment-unknown", "detached"])
def test_dashboard_checks_process_tty_before_recovery(resume, monkeypatch, tty, command_line, recovered):
    runtime, registration, _launches, _calls = resume
    path = _active_child(runtime, {**registration, "program": "codex"}, written_before_boot=False)
    os.utime(path, (time.time() - 120,) * 2)
    module = server._child_resume_module()
    monkeypatch.setattr(module, "boot_time", lambda: time.time() - 3600)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(server, "_child_session_live", lambda name, boot: False)
    probes = []

    def process_list(argv, **kwargs):
        probes.append(argv)
        rows = f"123 S {tty} {command_line}\n456 R ?? /bin/ps AGENTSTACK_PS_PROBE=1\n"
        return subprocess.CompletedProcess(argv, 0, rows.encode(), b"")

    monkeypatch.setattr(module, "subprocess", SimpleNamespace(run=process_list, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(module.os, "kill", lambda pid, signal: None)
    before = _state(runtime)
    server._recover_stopped_children()

    assert len(probes) == 1 and probes[0][:2] == ["/bin/ps", "-Eww"]
    if recovered:
        assert _state(runtime)["retired_at"] and _state(runtime)["resume_expires_at"]
    else:
        assert _state(runtime) == before


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


@pytest.mark.parametrize("listed,has_session,expected", [
    ((0, f"Other\n{NAME}\n", ""), 0, True),
    ((0, f"{NAME}\n", ""), 1, None),  # listed but admission refuses: cannot tell
    ((0, "Other\n", ""), None, False),
    ((0, "", ""), None, False),
    ((1, "", "no server running on /tmp/tmux-501/default"), None, False),
    ((1, "", "something unexpected"), None, None),
    ((1, "", "error connecting to /tmp/tmux-501/default (No such file or directory)"), None, False),
    ((1, "", "error connecting to /tmp/tmux-501/default (Permission denied)"), None, None),
])
def test_the_dashboard_tells_a_live_child_from_a_gone_one(monkeypatch, tmp_path, listed, has_session, expected):
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(server, "HOOKS_DIR", os.path.dirname(child_resume.__file__))
    asked = []

    def run(args, **kwargs):
        asked.append(args[0])
        if args[0] == "list-sessions":
            return subprocess.CompletedProcess(args, *listed)
        assert has_session is not None, "has-session is asked only for a listed name"
        return subprocess.CompletedProcess(args, has_session, "", "")

    monkeypatch.setattr(server, "_tmux_run", run)
    assert server._child_session_live(NAME, _boot()) is expected
    assert asked[0] == "list-sessions"


@pytest.mark.parametrize("listed,expected", [({"$1": "Other"}, False), ({"$1": NAME}, True), ({}, False)])
def test_the_real_tmux_consumer_reads_a_missing_session_as_gone(monkeypatch, tmp_path, listed, expected):
    # 2026-10-16: through the real consumer, has-session on a missing target is
    # refused (TMUX_TARGET_UNAVAILABLE), not answered with "can't find session".
    # The probe read that as "cannot tell", so no child a reboot stopped was
    # ever recovered. Only the transport's OS evidence is synthetic here.
    from _legacy_dashboard_transport import install_legacy_transport
    monkeypatch.setattr(server, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(server, "HOOKS_DIR", os.path.dirname(child_resume.__file__))

    def invoke(argv, **kwargs):
        # Admission already resolved the name to its session id ($N).
        if argv[1] == "has-session" and argv[argv.index("-t") + 1] in listed:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected tmux call: {argv}")

    install_legacy_transport(monkeypatch, tmp_path, server, invoke, sessions=listed)
    assert server._child_session_live(NAME, _boot()) is expected


def test_the_dashboard_recovers_at_start(resume, monkeypatch):
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    # The dashboard loads its own instance of child_resume.py from HOOKS_DIR.
    monkeypatch.setattr(server._child_resume_module(), "boot_time", _boot)
    monkeypatch.setattr(server._child_resume_module(), "child_process_live", lambda name: pytest.fail("pre-boot child probed"))
    monkeypatch.setattr(server, "_child_session_live", lambda name, boot: False)
    server._recover_stopped_children()
    assert _state(runtime)["retired_at"]


def test_a_session_that_starts_while_recovery_waits_for_the_lock_is_left_alone(resume, monkeypatch):
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    live = set()
    original_lock = child_resume.tools_change_lock

    @contextmanager
    def after_jump(runtime_dir, name):
        with original_lock(runtime_dir, name):
            live.add(name)  # the jump published its session before releasing the lock
            yield

    monkeypatch.setattr(child_resume, "tools_change_lock", after_jump)
    assert child_resume.recover_after_reboot(runtime, is_live=lambda name: name in live,
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


def test_recovery_reads_the_leases_again_and_writes_under_the_lease_lock(resume, monkeypatch):
    """#182 review: a lease written by SessionStart cannot land between the
    last look and the write, because the writer takes the same lock. tmux is
    asked outside the lock, so the lock is held for milliseconds."""
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    tmux_held, lease_held = [], []

    def is_live(_name):
        tmux_held.append(_lease_lock_is_held(runtime))
        return False

    original = child_resume.live_lease
    monkeypatch.setattr(child_resume, "live_lease",
                        lambda *a, **k: lease_held.append(_lease_lock_is_held(runtime)) or original(*a, **k))
    assert child_resume.recover_after_reboot(runtime, is_live=is_live, retention_days=30,
                                             boot=_boot()) == [(NAME, "retired")]
    assert tmux_held == [False, False] and lease_held == [True]


def test_a_lease_written_just_before_the_last_look_stops_recovery(resume, monkeypatch):
    runtime, registration, launches, calls = resume
    _active_child(runtime, registration)
    looks = []

    def is_live(_name):
        looks.append(1)
        if len(looks) == 2:  # a session starts between the last tmux look and the lock
            child_resume.write_lease(runtime, NAME, os.getpid())
        return False

    assert child_resume.recover_after_reboot(runtime, is_live=is_live, retention_days=30,
                                             boot=_boot()) == []
    assert _state(runtime)["retired_at"] is None


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


def test_a_lease_writer_waits_for_the_lock_and_never_goes_around_it(tmp_path, capsys):
    import threading

    with child_resume._LeaseLock(tmp_path):
        done = threading.Event()
        threading.Thread(target=lambda: (child_resume.write_lease(tmp_path, NAME, os.getpid()), done.set()),
                         daemon=True).start()
        assert not done.wait(0.5)  # blocked while the lock is held
        assert not (tmp_path / "live-sessions" / NAME / str(os.getpid())).exists()
    assert done.wait(5)
    assert (tmp_path / "live-sessions" / NAME / str(os.getpid())).exists()


def test_a_long_wait_only_warns(tmp_path, capsys):
    import threading

    held = child_resume._LeaseLock(tmp_path).__enter__()
    try:
        got = threading.Event()

        def wait():
            with child_resume._LeaseLock(tmp_path, warn_after=0.1):
                got.set()

        threading.Thread(target=wait, daemon=True).start()
        assert not got.wait(0.5)  # still waiting, past the warning
    finally:
        held.__exit__()
    assert got.wait(5)
    assert "still waiting for" in capsys.readouterr().err


def test_write_lease_command(tmp_path):
    helper = child_resume.__file__
    done = subprocess.run(["python3", helper, "write-lease", "--runtime-dir", str(tmp_path),
                           "--agent-name", NAME, "--pid", str(os.getpid())], capture_output=True, check=False)
    assert done.returncode == 0, done.stderr
    assert child_resume.live_lease(tmp_path, NAME, boot=_boot()) is True
    bad = subprocess.run(["python3", helper, "write-lease", "--runtime-dir", str(tmp_path),
                          "--agent-name", "../x", "--pid", "5"], capture_output=True, check=False)
    assert bad.returncode != 0 and not (tmp_path / "x").exists()


def test_only_children_left_running_or_mid_resume_are_asked_about(resume, tmp_path):
    """The liveness check runs tmux: ask it only about candidates, never about
    proxy configs or children retired normally (#183: 273 tmux calls at start)."""
    runtime, registration, launches, calls = resume
    path = _active_child(runtime, registration)
    retired = runtime / "child-agents" / "RetiredCurie.json"
    private(retired, json.dumps({**json.loads(path.read_text()), "agent_name": "RetiredCurie",
                                 "retired_at": "2026-09-01T00:00:00Z",
                                 "resume_expires_at": "2026-10-01T00:00:00Z"}))
    private(runtime / "child-agents" / f"{NAME}.mcp.json", '{"mcpServers": {}}')
    for item in (retired, runtime / "child-agents" / f"{NAME}.mcp.json"):
        os.utime(item, (time.time() - 3600,) * 2)
    asked = []
    child_resume.recover_after_reboot(runtime, is_live=lambda name: asked.append(name) or True,
                                      retention_days=30, boot=_boot())
    assert asked == [NAME, ]


@pytest.mark.parametrize('reason',['GLOBAL_TARGET_OWNERSHIP_UNKNOWN','LEGACY_TARGET_IS_GLOBAL'])
def test_actual_dashboard_unknown_admission_keeps_reboot_liveness_unknown(monkeypatch,tmp_path,reason):
    monkeypatch.setattr(server,'RUNTIME_DIR',str(tmp_path))
    def unknown(*args,**kwargs):raise RuntimeError(reason)
    monkeypatch.setattr(server,'_tmux_run',unknown)
    assert server._child_session_live(NAME,_boot()) is None


@pytest.mark.parametrize("listed,expected", [({"$1": "Other"}, False), ({"$1": NAME}, True), ({}, False)])
def test_the_resume_button_reads_a_missing_session_as_gone(monkeypatch, tmp_path, listed, expected):
    # 2026-10-16: RESUME on any gone agent failed with "the dashboard's answer
    # was lost": do_jump's _has_session let the consumer's refusal of a missing
    # target escape as an exception instead of answering False.
    from _legacy_dashboard_transport import install_legacy_transport

    def invoke(argv, **kwargs):
        if argv[1] == "has-session" and argv[argv.index("-t") + 1] in listed:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected tmux call: {argv}")

    install_legacy_transport(monkeypatch, tmp_path, server, invoke, sessions=listed)
    assert server._has_session(NAME) is expected
