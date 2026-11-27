"""Only the live tmux server may corroborate an agent's pane name."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import pytest

from test_unmanaged_session_policy import (
    BASE_ENV, REGISTRATION_GUARD, REPO_ROOT, RESERVATION_GUARD, UNREACHABLE,
    _edit_payload, _record_registration, _registration_payload, _run,
)


@pytest.fixture
def tmux_server(tmp_path):
    binary = shutil.which("tmux")
    if not binary:
        pytest.skip("tmux unavailable")
    terminal_env = {**BASE_ENV, "HOME": str(tmp_path), "TERM": "xterm"}
    directory = "/private/tmp" if Path("/private/tmp").is_dir() else "/tmp"
    with tempfile.TemporaryDirectory(prefix="opane-", dir=directory) as folder:
        socket_path = Path(folder) / "private"

        def tmux(*args, check=True):
            return subprocess.run([binary, "-f", "/dev/null", "-S", str(socket_path), *args],
                                  env=terminal_env, capture_output=True, text=True, check=check, timeout=10)

        def start(name):
            tmux("new-session", "-d", "-s", name, "/bin/sleep 60")
            pid, pane = tmux("display-message", "-p", "-t", name, "#{pid}|#{pane_id}").stdout.strip().split("|")
            return {"TMUX": f"{socket_path},{pid},0", "TMUX_PANE": pane,
                    "PATH": f"{Path(binary).parent}:{BASE_ENV['PATH']}"}

        def stop(env):
            pid = env["TMUX"].rsplit(",", 2)[1]
            tmux("kill-server")
            deadline = time.monotonic() + 5
            while True:
                state = subprocess.run(["ps", "-p", pid, "-o", "stat="],
                                       capture_output=True, text=True, timeout=2).stdout.strip()
                if not state or state.startswith("Z"):
                    return
                assert time.monotonic() < deadline, "the isolated tmux server did not exit"
                time.sleep(.02)

        try:
            yield start, stop
        finally:
            tmux("kill-server", check=False)


def _write_name(tmp_path, name, pane):
    env = {**BASE_ENV, **pane, "HOME": str(tmp_path / "home"),
           "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime")}
    result = subprocess.run(["/bin/bash", str(REPO_ROOT / "hooks/set-ghostty-title.sh"), name],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("legacy", [False, True], ids=["recorded-server", "legacy-record"])
def test_restarted_server_reuses_pane_number_without_reusing_identity(tmp_path, tmux_server, legacy):
    start, stop = tmux_server
    before = start("PreviousAgent")
    runtime = tmp_path / "runtime"
    key = "agent_name_" + before["TMUX_PANE"].replace("%", "_")
    if legacy:
        runtime.mkdir()
        record = runtime / key
        record.write_text("PreviousAgent")
        previous = time.time() - 5
        os.utime(record, (previous, previous))
    else:
        _write_name(tmp_path, "PreviousAgent", before)
        record, = runtime.rglob(key)
    stop(before)
    after = start("CurrentAgent")
    assert before["TMUX_PANE"] == after["TMUX_PANE"]
    assert before["TMUX"] != after["TMUX"]

    target = tmp_path / "note.md"
    target.touch()
    for guard, payload in (
        (REGISTRATION_GUARD, _registration_payload("new-tmux-generation")),
        (RESERVATION_GUARD, _edit_payload("new-tmux-generation", target)),
    ):
        result = _run(guard, payload, tmp_path, **after, AGENT_NAME="CurrentAgent",
                      AGENTSTACK_MCP_URL=UNREACHABLE, AGENTSTACK_PROTECTED_ROOTS=str(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
    assert not record.exists()


def test_metadata_from_the_current_server_still_counts_as_a_conflicting_claim(tmp_path, tmux_server):
    start, _ = tmux_server
    pane = start("CurrentAgent")
    _write_name(tmp_path, "OtherAgent", pane)
    session = "live-tmux-generation"
    _record_registration(tmp_path, session, 41, "CurrentAgent", str(tmp_path))

    target = tmp_path / "note.md"
    target.touch()
    for guard, payload in (
        (REGISTRATION_GUARD, _registration_payload(session)),
        (RESERVATION_GUARD, _edit_payload(session, target)),
    ):
        result = _run(guard, payload, tmp_path, **pane, AGENT_NAME="CurrentAgent",
                      AGENTSTACK_PROJECT_KEY=str(tmp_path), AGENTSTACK_MCP_URL=UNREACHABLE,
                      AGENTSTACK_PROTECTED_ROOTS=str(tmp_path))
        assert result.returncode == 2 and "AGENT IDENTITY CONFLICT" in result.stderr
    assert list((tmp_path / "runtime").rglob("agent_name__0"))
