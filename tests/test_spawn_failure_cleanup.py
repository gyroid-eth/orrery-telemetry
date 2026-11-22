"""Normal (not preregistered) spawn failure, with test-owned Mail and tmux.

No installed service, real registration or native CLI is used. The synthetic
Mail transport can disappear independently at release, retire or whois.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

from test_spawn_child_embed_task import _fake_launch_env

ROOT = Path(__file__).resolve().parents[1]
NAME = "FailureFixture"


@contextmanager
def mail_fixture(fail_method):
    state = {"calls": [], "registered": False, "retired": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request["params"]["name"]
            args = request["params"]["arguments"]
            state["calls"].append(method)
            if state["registered"] and method == fail_method:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if method == "register_agent":
                state.update(registered=True, token=args["registration_token"], program=args["program"])
                data = {"id": 73, "name": NAME, "project_id": 1,
                        "registration_token": state["token"]}
            elif method == "whois":
                if not state["registered"]:
                    self.reply(request, error={"code": -32000, "message": "Agent not found"})
                    return
                data = {"name": NAME, "retired_at": "2026-01-01T00:00:00Z" if state["retired"] else None}
            elif method == "file_reservation_paths":
                data = {"granted": [{"path_pattern": "src/trial.txt", "expires_ts": "2099-01-01"}], "conflicts": []}
            elif method == "release_file_reservations":
                data = {"released": 1}
            elif method == "retire_agent":
                assert args["registration_token"] == state["token"]
                state["retired"] = True
                data = {"status": "retired", "agent_name": NAME, "project_key": "/shared/project"}
            elif method in {"health_check", "send_message"}:
                data = {"status": "ok"}
            else:
                self.reply(request, error={"code": -32601, "message": "Unexpected fixture method"})
                return
            self.reply(request, result={"structuredContent": data,
                       "content": [{"type": "text", "text": json.dumps(data)}]})

        def reply(self, request, **response):
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], **response}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/mcp", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def fail_after_registration(tmp_path, program, fail_method, *, creation="ack-exited", retention=None):
    env, work = _fake_launch_env(tmp_path, codex=program == "codex")
    env.update(AGENTSTACK_PYTHON=sys.executable, AGENTSTACK_CHILD_SHELL="/bin/bash",
               AGENTSTACK_MAIL_HTTP_BEARER_MODE="disabled", FAKE_CREATE_RESULT=creation)
    env.pop("AGENTSTACK_CHILD_RESUME_RETENTION_DAYS", None)
    if retention is not None:
        env["AGENTSTACK_CHILD_RESUME_RETENTION_DAYS"] = retention
    # ACK then exit proves this session is already gone; nonzero creation
    # leaves its outcome unknown, even if our synthetic server created it.
    tmux = tmp_path / "bin/tmux"
    injected = r'''if [[ "${1:-}" == new-session ]]; then
    case "$FAKE_CREATE_RESULT" in
        ack-exited) exit 0 ;;
        created-unconfirmed) : > "$FAKE_TMUX_ALIVE"; exit 1 ;;
        uncreated-unconfirmed) exit 1 ;;
    esac
fi
[[ "${1:-}" != capture-pane ]] || exit 1
if [[ "${1:-}" == has-session && ! -f "$FAKE_TMUX_ALIVE" ]]; then
    echo "can't find session: synthetic child" >&2
    exit 1
fi
'''
    tmux.write_text(tmux.read_text().replace('case "${1:-}" in\n', injected + 'case "${1:-}" in\n', 1))
    args = ["/bin/bash", str(ROOT / "hooks/spawn_child.sh")]
    if program == "codex":
        args += ["--codex", "--model", "gpt-5.5"]
        env["AGENTSTACK_CODEX_BIN"] = str(tmp_path / "bin/codex")
    else:
        args += ["--model", "claude-sonnet-4-6"]
    args += ["--resources", "src/trial.txt", "Synthetic failure task", str(work)]
    with mail_fixture(fail_method) as (url, state):
        env["AGENTSTACK_MCP_URL"] = url
        result = subprocess.run(args, env=env, cwd=ROOT, capture_output=True,
                                text=True, timeout=30)
    assert state["registered"], result.stderr
    assert "new-session" in Path(env["FAKE_TMUX_LOG"]).read_text()
    assert result.returncode != 0
    assert state["token"] not in result.stdout + result.stderr
    return result, state, Path(env["AGENTSTACK_RUNTIME_DIR"])


@pytest.mark.parametrize("program", ["claude", "codex"])
@pytest.mark.parametrize("fail_method", ["release_file_reservations", "retire_agent", "whois"])
def test_failed_spawn_rpc_disconnect_keeps_recovery_material(tmp_path, program, fail_method):
    result, state, runtime = fail_after_registration(tmp_path, program, fail_method)
    token = runtime / ("agent_token_" + NAME)
    child = runtime / "child-agents" / (NAME + ".json")
    assert token.is_file(), result.stderr
    assert token.read_text().strip() == state["token"]
    assert child.is_file(), result.stderr
    assert json.loads(child.read_text())["registration_token"] == state["token"]
    assert fail_method in state["calls"]


@pytest.mark.parametrize("program", ["claude", "codex"])
@pytest.mark.parametrize("retention", [None, "30"])
def test_confirmed_failed_spawn_cleanup_deletes_without_resume_retention(tmp_path, program, retention):
    _result, state, runtime = fail_after_registration(tmp_path, program, None, retention=retention)
    assert state["retired"]
    assert state["calls"][-3:] == ["release_file_reservations", "retire_agent", "whois"]
    assert not (runtime / ("agent_token_" + NAME)).exists()
    assert not (runtime / "child-agents" / (NAME + ".json")).exists()


@pytest.mark.parametrize("program", ["claude", "codex"])
@pytest.mark.parametrize("creation", ["uncreated-unconfirmed", "created-unconfirmed"])
def test_unconfirmed_creation_keeps_identity_without_killing_or_retiring(tmp_path, program, creation):
    result, state, runtime = fail_after_registration(tmp_path, program, None, creation=creation)
    assert "creation unconfirmed" in result.stderr
    assert (runtime / ("agent_token_" + NAME)).is_file()
    assert (runtime / "child-agents" / (NAME + ".json")).is_file()
    assert not {"release_file_reservations", "retire_agent"}.intersection(state["calls"])
    assert "kill-session" not in (tmp_path / "tmux.log").read_text()
    assert (tmp_path / "tmux.alive").exists() == (creation == "created-unconfirmed")


@pytest.mark.parametrize("scenario", ["pending", "unreachable", "kill_failed", "still_alive", "updating", "stopped"])
def test_started_child_cleanup_requires_stop_confirmation(tmp_path, scenario):
    """A failed tmux probe is not absence; no RPC or deletion may follow it."""
    import shlex

    source = (ROOT / "hooks/spawn_child.sh").read_text()
    begin = source.index("cleanup_on_failure() {")
    body = source[begin:source.index("\n}\n", begin) + 3]
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    for name in ("token", "state", "worktree"):
        (recovery / name).write_text("synthetic material")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "cleanup-child-agent.sh").write_text(
        'echo cleanup >> "$TEST_ROOT/calls"\nrm -rf "$TEST_ROOT/recovery"\n')
    script = r'''
export TEST_ROOT=ROOT_VALUE
HOOKS_DIR="$TEST_ROOT/hooks"
SPAWN_COMPLETED=false
CHILD_SESSION_STARTED=true
CHILD_NAME=SyntheticChild
USE_CODEX=true
SCENARIO=SCENARIO_VALUE
CHILD_SESSION_START_PENDING=false
if [[ "$SCENARIO" == pending ]]; then CHILD_SESSION_START_PENDING=true; CHILD_SESSION_STARTED=false; fi
warn_if_uninjected() { :; }
codex_self_update_on_screen() { [[ "$SCENARIO" == updating ]]; }
leave_updating_codex_child() { echo "updater:$*" >> "$TEST_ROOT/calls"; }
cleanup_worktree() { echo worktree >> "$TEST_ROOT/calls"; }
discard_claude_launch_record() { echo discard >> "$TEST_ROOT/calls"; }
tmux() {
    echo "$1" >> "$TEST_ROOT/calls"
    case "$1" in
        has-session)
            if [[ "$SCENARIO" == unreachable ]]; then echo 'error connecting: Permission denied' >&2; return 1; fi
            [[ ! -f "$TEST_ROOT/killed" ]] ;;
        kill-session)
            [[ "$SCENARIO" != kill_failed ]] || return 1
            [[ "$SCENARIO" == still_alive ]] || touch "$TEST_ROOT/killed" ;;
    esac
}
'''.replace("ROOT_VALUE", shlex.quote(str(tmp_path))).replace("SCENARIO_VALUE", scenario)
    result = subprocess.run(["/bin/bash", "-c", script + body + "\ncleanup_on_failure\n"],
                            capture_output=True, text=True, timeout=5)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    if scenario == "stopped":
        assert result.returncode == 0, result.stderr
        assert calls == ["has-session", "kill-session", "has-session", "cleanup", "worktree", "discard"]
        assert not recovery.exists()
    else:
        assert result.returncode != 0
        if scenario == "updating":
            assert "updater:SyntheticChild true" in calls
        assert "cleanup" not in calls
        assert "worktree" not in calls
        assert "discard" not in calls
        assert sorted(p.name for p in recovery.iterdir()) == ["state", "token", "worktree"]


@pytest.mark.parametrize("fail_method", [None, "release_file_reservations", "retire_agent", "whois"])
def test_direct_update_watch_confirms_mail_cleanup_before_removing_worktree(tmp_path, fail_method):
    """The actual shared cleanup receives the retained identity after updater stop."""
    import os
    import shlex

    runtime = tmp_path / "runtime"
    child_dir = runtime / "child-agents"
    child_dir.mkdir(parents=True)
    token = runtime / ("agent_token_" + NAME)
    token.write_text("synthetic-watch-token")
    child = child_dir / (NAME + ".json")
    child.write_text(json.dumps({"project_key": "/shared/project", "registration_token": token.read_text()}))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "kept.txt").write_text("synthetic work")
    home = tmp_path / "home"
    home.mkdir()
    source = (ROOT / "hooks/spawn_child.sh").read_text()
    start = source.index("codex_update_watch() {")
    body = source[start:source.index("\n}\n", start) + 3]
    script = r'''
TEST_ROOT=ROOT_VALUE
SPAWN_INCIDENT_LOG="$TEST_ROOT/incidents"
tmux() {
    case "$1" in
        has-session) [[ ! -f "$TEST_ROOT/killed" ]] ;;
        capture-pane) echo 'update finished' ;;
        kill-session) : > "$TEST_ROOT/killed" ;;
    esac
}
git() {
    echo "git $*" >> "$TEST_ROOT/git-calls"
    if [[ "$3" == worktree && "$4" == remove ]]; then rm -rf "$6"; fi
}
'''.replace("ROOT_VALUE", shlex.quote(str(tmp_path)))
    script += body + '\ncodex_update_watch ' + NAME + ' "$TEST_ROOT/worktree" /synthetic/source ' + NAME + ' 30 5 true\n'
    env = os.environ.copy()
    env.pop("AGENTSTACK_CHILD_RESUME_RETENTION_DAYS", None)
    env.update(HOME=str(home), HOOKS_DIR=str(ROOT / "hooks"), AGENTSTACK_HOOKS_DIR=str(ROOT / "hooks"),
               AGENTSTACK_RUNTIME_DIR=str(runtime), AGENTSTACK_PYTHON=sys.executable,
               AGENTSTACK_REGISTER_LIB=str(ROOT / "bin/lib/agentstack-register.sh"),
               AGENTSTACK_MAIL_HTTP_BEARER_MODE="disabled")
    with mail_fixture(fail_method) as (url, state):
        state.update(registered=True, token=token.read_text())
        env["AGENTSTACK_MCP_URL"] = url
        result = subprocess.run(["/bin/bash", "-c", script], env=env,
                                capture_output=True, text=True, timeout=20)
    notes = (tmp_path / "incidents").read_text()
    assert "registration was rolled back" not in notes
    assert (tmp_path / "killed").exists()
    if fail_method is None:
        assert result.returncode == 0, result.stderr
        assert state["calls"] == ["release_file_reservations", "retire_agent", "whois"]
        assert "retirement confirmed" in notes
        assert not token.exists() and not child.exists() and not worktree.exists()
        assert (tmp_path / "git-calls").exists()
    else:
        assert result.returncode != 0
        assert "Mail cleanup unknown" in notes
        assert "retirement confirmed" not in notes
        assert token.exists() and child.exists() and worktree.exists()
        assert not (tmp_path / "git-calls").exists()
