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


@pytest.mark.parametrize("case", ["own_pane", "closed_pane", "dead_lease", "empty_inventory"])
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
        # No other pane is a confirmed empty observation, not an unreadable
        # server (the tmux_unreadable negative above covers that separately).
        env = {"FAKE_TMUX_PANES": ""}
    process = run_resumed(launches, tmp_path, "exit 0\n", env=env)
    assert process.returncode == 0, process.stderr
    # Reservations are the SessionEnd hook's job; the retire-only exit never releases them.
    assert exit_calls(mail) == ["retire_agent"], mail


@pytest.mark.parametrize("endpoint,retired", [("absent", True), ("stale", True), ("unsafe", False)])
def test_plain_terminal_exit_classifies_actual_tmux_endpoint(resume, mail, tmp_path, endpoint, retired):
    """Real absence retires; a socket rejected by admission keeps the row."""
    import os
    from pathlib import Path
    import shutil
    import socket
    import tempfile
    from test_claude_resume_mail import ROOT
    binary = shutil.which('tmux')
    if binary is None:
        pytest.skip('tmux unavailable')
    runtime, registration, _, _ = resume
    with tempfile.TemporaryDirectory(prefix='cleanup-endpoint-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory:
        base = Path(directory).resolve()
        parent = base / ('tmux-' + str(os.getuid()))
        parent.mkdir(mode=0o700)
        path = parent / 'default'
        if endpoint != 'absent':
            with socket.socket(socket.AF_UNIX) as stream:
                stream.bind(str(path))
                path.chmod(0o600 if endpoint == 'stale' else 0o666)
        env = {**os.environ, 'TMUX_TMPDIR': str(base), 'AGENTSTACK_RUNTIME_DIR': str(runtime),
               'AGENTSTACK_HOOKS_DIR': str(ROOT / 'hooks'), 'AGENTSTACK_RUNTIME_MODE': 'legacy',
               'AGENTSTACK_MAIL_HTTP_BEARER_MODE': 'disabled', 'AGENTSTACK_CLEANUP_RETIRE_ONLY': '1', 'PROJECT_KEY': registration['project_key']}
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        result = subprocess.run(['/bin/bash', str(ROOT / 'hooks/cleanup-child-agent.sh'), NAME],
                                env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert exit_calls(mail) == (['retire_agent'] if retired else []), (result.stderr, mail)
        assert (runtime / f'agent_token_{NAME}').read_text() == TOKEN


def test_permission_denied_endpoint_retains_identity_and_signal_inventory(resume, mail, tmp_path, monkeypatch):
    """A real EACCES is unknown for both cleanup and signal pruning."""
    import os
    from pathlib import Path
    import shutil
    import socket
    import tempfile
    from dashboard import mail_signals
    from test_claude_resume_mail import ROOT
    if shutil.which('tmux') is None:
        pytest.skip('tmux unavailable')
    runtime, registration, _, _ = resume
    token = runtime / f'agent_token_{NAME}'
    before = token.read_bytes()
    with tempfile.TemporaryDirectory(prefix='denied-endpoint-', dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory:
        base = Path(directory).resolve()
        parent = base / ('tmux-' + str(os.getuid()))
        parent.mkdir(mode=0o700)
        path = parent / 'default'
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind(str(path))
            path.chmod(0o600)
            monkeypatch.setenv('TMUX_TMPDIR', str(base))
            monkeypatch.delenv('TMUX', raising=False)
            monkeypatch.delenv('TMUX_PANE', raising=False)
            env = {**os.environ, 'AGENTSTACK_RUNTIME_DIR': str(runtime),
                   'AGENTSTACK_HOOKS_DIR': str(ROOT / 'hooks'), 'AGENTSTACK_RUNTIME_MODE': 'legacy',
                   'AGENTSTACK_MAIL_HTTP_BEARER_MODE': 'disabled', 'AGENTSTACK_CLEANUP_RETIRE_ONLY': '1',
                   'PROJECT_KEY': registration['project_key']}
            parent.chmod(0o000)
            try:
                with pytest.raises(PermissionError):
                    os.lstat(path)
                sessions = mail_signals.session_names()
                result = subprocess.run(['/bin/bash', str(ROOT / 'hooks/cleanup-child-agent.sh'), NAME],
                                        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stderr
                assert (sessions, exit_calls(mail), token.read_bytes()) == (None, [], before), (sessions, mail, result.stderr)
            finally:
                parent.chmod(0o700)
