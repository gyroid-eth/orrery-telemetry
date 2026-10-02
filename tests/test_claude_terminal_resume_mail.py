"""A Claude reopened from a terminal gets its Mail row back (#143 review, TRIAGE P5-b).

Only the dashboard resume unretired a row. Once #143 re-retires a token-only
Claude on exit, `AGENT_NAME=X claude --resume` from a terminal said "already
registered" while the row stayed retired. The SessionStart shell registration
now unretires on the same terms as the dashboard: the saved owner credential
authenticates, and retained child material is still within its period.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from dashboard import server
from hooks import child_resume
from claude_resume_harness import StandInMail, mail, run_resumed
from test_claude_resume_mail import NAME, ROOT, TOKEN, child, private, resume

RETIRED_AT = "2026-09-30T14:32:36Z"


def session_start(runtime, registration, cwd, source="resume", extra=None):
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.environ["HOME"],
        "AGENT_NAME": NAME,
        "AGENTSTACK_RESERVED_IDENTITY": "1",
        "AGENTSTACK_PROJECT_KEY": registration["project_key"],
        "AGENTSTACK_MCP_URL": os.environ["AGENTSTACK_MCP_URL"],
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
        "AGENTSTACK_REGISTER_LIB": str(ROOT / "bin/lib/agentstack-register.sh"),
        "AGENTSTACK_PYTHON": sys.executable,
        **(extra or {}),
    }
    os.makedirs(env["HOME"], exist_ok=True)
    payload = {"session_id": "sess-terminal-1", "hook_event_name": "SessionStart", "cwd": str(cwd)}
    if source is not None:
        payload["source"] = source
    result = subprocess.run(["/bin/bash", str(ROOT / "hooks/session-start-reminder.sh")],
                            input=json.dumps(payload), capture_output=True, text=True,
                            timeout=60, env=env, cwd=cwd)
    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout + result.stderr
    return result


def unretires(mail):
    return [arguments for method, arguments in mail if method == "unretire_agent"]


@pytest.mark.parametrize("source", ["startup", "resume"])
@pytest.mark.parametrize("kind", ["token_only", "retained_child"])
def test_terminal_resume_unretires_an_authenticated_retired_owner(resume, mail, tmp_path, kind, source):
    runtime, registration, launches, calls = resume
    if kind == "retained_child":
        child(runtime, registration)
    StandInMail.retired_at = RETIRED_AT
    session_start(runtime, registration, tmp_path, source=source)
    registers = [arguments for method, arguments in mail if method == "register_agent"]
    assert registers and registers[-1]["registration_token"] == TOKEN
    assert unretires(mail) == [{"project_key": registration["project_key"], "agent_name": NAME}]
    assert (runtime / f"agent_token_{NAME}").read_text().strip() == TOKEN


def test_terminal_resume_leaves_an_active_row_alone(resume, mail, tmp_path):
    runtime, registration, *_ = resume
    session_start(runtime, registration, tmp_path)
    assert [method for method, _ in mail if method == "register_agent"]
    assert unretires(mail) == []


@pytest.mark.parametrize("kind", ["expired", "legacy", "purged", "resume_pending", "no_token"])
def test_terminal_resume_never_revives_material_outside_its_period(resume, mail, tmp_path, kind):
    """An intentionally retired or expired identity stays retired."""
    runtime, registration, *_ = resume
    state_path = runtime / "child-agents" / f"{NAME}.json"
    token = runtime / f"agent_token_{NAME}"
    if kind in {"expired", "resume_pending"}:
        child(runtime, registration)
        state = json.loads(state_path.read_text())
        if kind == "expired":
            state["resume_expires_at"] = "2000-01-01T00:00:00Z"
        else:
            state["resume_in_progress_at"] = "2026-09-30T00:00:00Z"
        private(state_path, json.dumps(state))
    elif kind == "legacy":
        private(state_path, json.dumps({"agent_name": NAME, "project_key": registration["project_key"],
                                        "registration_token": TOKEN}))
    elif kind == "purged":
        child(runtime, registration)
        assert child_resume.purge_one(runtime, NAME, reason="retention_expired",
                                      now=datetime.now(timezone.utc) + timedelta(days=31))
        private(token, TOKEN)
    else:
        token.unlink()
    StandInMail.retired_at = RETIRED_AT
    session_start(runtime, registration, tmp_path)
    assert unretires(mail) == []


def test_exit_then_terminal_resume_round_trips_the_row(resume, mail, tmp_path):
    """The review's sequence (a): dashboard resume, /exit, then a terminal resume."""
    runtime, registration, launches, calls = resume
    assert server.do_resume(NAME, open_terminal=False)["ok"]
    process = run_resumed(launches, tmp_path, "exit 0\n")
    assert process.returncode == 0, process.stderr
    assert [method for method, _ in mail if method in {"retire_agent", "unretire_agent"}] == ["retire_agent"]
    StandInMail.retired_at = RETIRED_AT
    session_start(runtime, registration, tmp_path)
    assert [method for method, _ in mail if method in {"retire_agent", "unretire_agent"}] == [
        "retire_agent", "unretire_agent"]


@pytest.mark.parametrize("source", ["clear", "compact", None])
@pytest.mark.parametrize("kind", ["token_only", "retained_child"])
def test_clear_or_compaction_never_undoes_a_deliberate_retire(resume, mail, tmp_path, kind, source):
    """#152 review N-1: a running identity the user retired came back on the next /clear or compaction."""
    runtime, registration, *_ = resume
    if kind == "retained_child":
        child(runtime, registration)
    StandInMail.retired_at = RETIRED_AT
    result = session_start(runtime, registration, tmp_path, source=source)
    assert [method for method, _ in mail if method == "register_agent"]
    assert unretires(mail) == []
    assert "retired" in result.stdout


def test_session_start_sweeps_leases_of_ended_sessions(resume, mail, tmp_path):
    """#152 review P3-a: leases of sessions that have ended piled up under runtime/live-sessions."""
    runtime, registration, *_ = resume
    ended = subprocess.run(["/bin/sh", "-c", "echo $$"], capture_output=True, text=True).stdout.strip()
    stale = [runtime / "live-sessions" / NAME / ended, runtime / "live-sessions" / "OtherAgent" / ended]
    live = runtime / "live-sessions" / "OtherAgent" / str(os.getpid())
    for lease in (*stale, live):
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.write_text("")
    session_start(runtime, registration, tmp_path)
    assert not any(lease.exists() for lease in stale)
    assert live.exists()
    # This session's own lease (the pytest process runs the hook) is written.
    assert (runtime / "live-sessions" / NAME / str(os.getpid())).exists()


def test_a_failing_lease_helper_writes_nothing_instead_of_going_around_the_lock(resume, mail, tmp_path):
    """#182 review: only a missing helper falls back to a plain write. A helper
    that runs and fails (an older python3, say) must not write a lease outside
    the lock that recovery and pruning hold."""
    runtime, registration, *_ = resume
    failing = tmp_path / "failing_helper.py"
    failing.write_text("import sys\nsys.exit(3)\n")
    session_start(runtime, registration, tmp_path, extra={"AGENTSTACK_CHILD_RESUME_HELPER": str(failing)})
    assert not (runtime / "live-sessions" / NAME / str(os.getpid())).exists()
