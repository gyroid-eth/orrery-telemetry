"""A resumed Claude leaves its Mail row as it found it (#143).

The login-shell body that do_resume hands tmux is executed here with a
synthetic CLI and a local Mail stand-in, so the assertions are about what the
real command does after the conversation ends, not about its text.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from dashboard import server
from claude_resume_harness import mail, run_resumed
from test_claude_resume_mail import NAME, TOKEN, resume


@pytest.mark.parametrize("status", [0, 3])
def test_token_only_exit_retires_the_row_resume_unretired(resume, mail, tmp_path, status):
    """#143: without child state nothing re-retired the row after `/exit`."""
    runtime, registration, launches, calls = resume
    token = runtime / f"agent_token_{NAME}"
    before = (token.read_bytes(), token.stat().st_mode)
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    assert [method for method, _ in calls] == ["register_agent", "unretire_agent"]
    process = run_resumed(launches, tmp_path, f"exit {status}\n")
    assert process.returncode == status, process.stderr
    retires = [arguments for method, arguments in mail if method == "retire_agent"]
    assert retires == [{"project_key": registration["project_key"], "agent_name": NAME,
                        "registration_token": TOKEN}], mail
    # Exit restores the retirement only: the owner credential stays for the next resume.
    assert (token.read_bytes(), token.stat().st_mode) == before
    assert not (runtime / "child-agents" / f"{NAME}.json").exists()
    assert TOKEN not in process.stdout + process.stderr + json.dumps(launches)


def test_token_only_exit_leaves_a_row_that_was_active_before_resume(resume, mail, tmp_path):
    runtime, registration, launches, calls = resume
    registration["retired_at"] = None
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    process = run_resumed(launches, tmp_path, "exit 0\n")
    assert process.returncode == 0, process.stderr
    assert "retire_agent" not in [method for method, _ in mail]
    assert (runtime / f"agent_token_{NAME}").read_text() == TOKEN


def exit_calls(mail):
    return [method for method, _ in mail if method in {"release_file_reservations", "retire_agent"}]


@pytest.mark.parametrize("case", ["live_pane", "live_lease", "tmux_unreadable"])
def test_exit_leaves_mail_alone_while_the_identity_lives_elsewhere(resume, mail, tmp_path, case):
    """#143 review (b): exiting one copy released and retired a copy still in use."""
    runtime, registration, launches, calls = resume
    assert server.do_resume(NAME, open_terminal=False)["ok"]
    env = {"TMUX": "/tmp/fixture-tmux,1,0", "TMUX_PANE": "%1", "FAKE_TMUX_PANES": "%1\n%99\n"}
    sleeper = None
    if case == "live_pane":
        (runtime / "agent_name__99").write_text(NAME)
    elif case == "live_lease":
        sleeper = subprocess.Popen(["/bin/sleep", "60"])
        lease = runtime / "live-sessions" / NAME / str(sleeper.pid)
        lease.parent.mkdir(parents=True)
        lease.write_text("")
    else:
        env["FAKE_TMUX_FAIL"] = "1"
    try:
        process = run_resumed(launches, tmp_path, "exit 0\n", env=env)
    finally:
        if sleeper:
            sleeper.kill()
            sleeper.wait()
    assert process.returncode == 0, process.stderr
    assert exit_calls(mail) == [], mail
    assert NAME in process.stderr
    assert (runtime / f"agent_token_{NAME}").read_text() == TOKEN


@pytest.mark.parametrize("case", ["own_pane", "closed_pane", "dead_lease", "no_tmux"])
def test_exit_retires_when_no_other_copy_is_alive(resume, mail, tmp_path, case):
    runtime, registration, launches, calls = resume
    assert server.do_resume(NAME, open_terminal=False)["ok"]
    env = {"TMUX": "/tmp/fixture-tmux,1,0", "TMUX_PANE": "%1", "FAKE_TMUX_PANES": "%1\n"}
    if case == "own_pane":
        (runtime / "agent_name__1").write_text(NAME)
    elif case == "closed_pane":
        (runtime / "agent_name__99").write_text(NAME)
    elif case == "dead_lease":
        lease = runtime / "live-sessions" / NAME / "999999"
        lease.parent.mkdir(parents=True)
        lease.write_text("")
    else:
        env = {"FAKE_TMUX_FAIL": "1"}
    process = run_resumed(launches, tmp_path, "exit 0\n", env=env)
    assert process.returncode == 0, process.stderr
    # Reservations are the SessionEnd hook's job; the retire-only exit never releases them.
    assert exit_calls(mail) == ["retire_agent"], mail
