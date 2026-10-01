"""`spawn_child.sh --pre-registered` brings a retired child back to active.

A child's cleanup retires its Mail identity at exit and keeps its state and
credential for a later resume. The dashboard's resume unretires it; relaunching
the same child with `spawn_child.sh --pre-registered <name>` did not. The child
then ran with a retired row, and ORRERY Mail refuses messages to a retired
agent (AGENT_RETIRED), so its parent could not reach it (found while
triaging the WSL2 report, 2026-10-01).

A disposable ORRERY Mail stand-in on a free port; temporary HOME and runtime.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_spawn_child_embed_task import (  # noqa: E402
    ROOT,
    SPAWN,
    _codex_handoff,
    _executable,
    _fake_launch_env,
)


NAME = "RetiredChild"
PROJECT = "/shared/project"
TOKEN = "retained-owner-token"


class FakeMail:
    def __init__(self, *, refuse: set[str] = frozenset()) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.retired = True
        self.refuse = set(refuse)
        mail = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802 - stdlib handler API
                envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = envelope["params"]["name"]
                arguments = envelope["params"]["arguments"]
                mail.calls.append((name, arguments))
                if name == "whois" and name not in mail.refuse:
                    data = {"id": 73, "name": arguments["agent_name"], "program": "claude-code"}
                    if mail.retired:
                        data["retired_at"] = "2026-09-30T14:32:36Z"
                    result = {"structuredContent": data, "content": [{"type": "text", "text": json.dumps(data)}]}
                elif name in mail.refuse or arguments.get("registration_token") != TOKEN:
                    result = {"isError": True, "content": [{"type": "text", "text": f"{name} refused"}]}
                else:
                    mail.retired = name == "retire_agent"
                    data = {"status": "retired" if mail.retired else "active",
                            "agent_name": arguments["agent_name"], "project_key": arguments["project_key"]}
                    result = {"structuredContent": data, "content": [{"type": "text", "text": json.dumps(data)}]}
                raw = json.dumps({"jsonrpc": "2.0", "id": envelope.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tools(self) -> list[str]:
        return [name for name, _ in self.calls]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mail_factory():
    servers: list[FakeMail] = []

    def make(**kwargs) -> FakeMail:
        server = FakeMail(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _retained_child(tmp_path: pathlib.Path, mail: FakeMail, *, codex: bool, retired: bool = True,
                    mail_retired: bool | None = None):
    """A child that ran before. ``retired`` is what its local state says;
    ``mail_retired`` what ORRERY Mail says (default: the same)."""
    from hooks import child_resume

    env, workdir = _fake_launch_env(tmp_path, codex=codex)
    env["AGENTSTACK_MCP_URL"] = mail.endpoint
    env["AGENTSTACK_MAIL_HTTP_BEARER_MODE"] = "disabled"
    env["AGENTSTACK_CODEX_BIN"] = str(tmp_path / "bin" / "codex")
    runtime = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"])
    token = runtime / f"agent_token_{NAME}"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(TOKEN)
    token.chmod(0o600)
    state = runtime / "child-agents" / f"{NAME}.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"agent_id": 73, "agent_name": NAME, "project_key": PROJECT,
                                 "program": "codex" if codex else "claude-code",
                                 "registration_token": TOKEN}))
    state.chmod(0o600)
    child_resume.prepare_active_state(runtime, NAME, project_key=PROJECT)
    if retired:
        # What cleanup-child-agent.sh leaves after the child exits.
        assert child_resume.mark_retired(runtime, NAME, retention_days=30)
    mail.retired = retired if mail_retired is None else mail_retired
    args = ["/bin/bash", str(SPAWN), "--pre-registered", NAME]
    if codex:
        args.append("--codex")
    return env, [*args, "relaunched task", str(workdir)], state


def _launched(env: dict) -> bool:
    log = pathlib.Path(env["FAKE_TMUX_LOG"])
    return log.exists() and "new-session" in log.read_text(encoding="utf-8")


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False)


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_relaunching_a_retired_child_makes_it_reachable_again(tmp_path, mail_factory, codex):
    mail = mail_factory()
    env, args, state = _retained_child(tmp_path, mail, codex=codex)

    result = _run(args, env)

    assert result.returncode == 0, result.stderr
    assert _launched(env)
    assert mail.tools() == ["whois", "unretire_agent"]
    assert mail.calls[1][1] == {"project_key": PROJECT, "agent_name": NAME, "registration_token": TOKEN}
    assert not mail.retired
    assert json.loads(state.read_text())["retired_at"] is None


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_an_active_child_is_only_looked_up(tmp_path, mail_factory, codex):
    mail = mail_factory()
    env, args, _state = _retained_child(tmp_path, mail, codex=codex, retired=False)

    result = _run(args, env)

    assert result.returncode == 0, result.stderr
    assert mail.tools() == ["whois"]


@pytest.mark.parametrize("codex", [False, True], ids=["claude", "codex"])
def test_a_child_retired_only_in_mail_is_made_active(tmp_path, mail_factory, codex):
    """Retired by the dashboard's retire button, a manual retire_agent, or a
    cleanup whose state write did not happen: the local state says nothing.
    ORRERY Mail is what refuses the parent's messages, so it decides."""
    mail = mail_factory()
    env, args, state = _retained_child(tmp_path, mail, codex=codex, retired=False, mail_retired=True)
    assert json.loads(state.read_text())["retired_at"] is None

    result = _run(args, env)

    assert result.returncode == 0, result.stderr
    assert mail.tools() == ["whois", "unretire_agent"]
    assert not mail.retired


def test_a_child_retired_only_in_its_state_is_not_unretired(tmp_path, mail_factory):
    mail = mail_factory()
    env, args, _state = _retained_child(tmp_path, mail, codex=False, retired=True, mail_retired=False)

    result = _run(args, env)

    assert result.returncode == 0, result.stderr
    assert mail.tools() == ["whois"]


def test_an_active_child_whose_mail_cannot_be_checked_still_starts(tmp_path, mail_factory):
    """Nothing says it is retired; do not make Mail a new requirement to start."""
    mail = mail_factory()
    env, args, _state = _retained_child(tmp_path, mail, codex=False, retired=False)
    mail.close()

    result = _run(args, env)

    assert result.returncode == 0, result.stderr
    assert _launched(env)
    assert "could not check" in result.stderr


def test_a_failed_start_of_an_active_child_leaves_everything_as_it_was(tmp_path, mail_factory):
    mail = mail_factory()
    env, args, state = _retained_child(tmp_path, mail, codex=False, retired=False)
    token = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"]) / f"agent_token_{NAME}"
    _executable(tmp_path / "bin" / "tmux", '#!/bin/bash\n[[ "$1" != new-session ]] || exit 1\nexit 0\n')
    before = {path: (path.read_bytes(), path.stat().st_mode) for path in (state, token)}

    result = _run(args, env)

    assert result.returncode != 0
    assert mail.tools() == ["whois"]
    for path, saved in before.items():
        assert (path.read_bytes(), path.stat().st_mode) == saved


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
def test_a_child_that_cannot_be_unretired_is_not_started(tmp_path, mail_factory, failure):
    """Starting it anyway leaves a child its parent cannot message."""
    mail = mail_factory(refuse={"unretire_agent"} if failure == "refused" else set())
    env, args, state = _retained_child(tmp_path, mail, codex=False)
    if failure == "unreachable":
        mail.close()
    before = state.read_bytes()

    result = _run(args, env)

    assert result.returncode != 0
    assert not _launched(env)
    assert state.read_bytes() == before
    assert "retired" in result.stderr
    assert not list(state.parent.glob(".*.registration-pending.json"))


def test_a_failed_start_retires_the_child_again(tmp_path, mail_factory):
    mail = mail_factory()
    env, args, state = _retained_child(tmp_path, mail, codex=False)
    _executable(tmp_path / "bin" / "tmux", '#!/bin/bash\n[[ "$1" != new-session ]] || exit 1\nexit 0\n')
    before = state.read_bytes()

    result = _run(args, env)

    assert result.returncode != 0
    assert mail.tools() == ["whois", "unretire_agent", "retire_agent"]
    assert mail.retired
    assert state.read_bytes() == before


def test_a_fresh_preregistered_child_needs_no_mail_call(tmp_path, mail_factory):
    mail = mail_factory()
    env, workdir = _fake_launch_env(tmp_path, codex=False)
    env["AGENTSTACK_MCP_URL"] = mail.endpoint
    handoff = _codex_handoff(tmp_path, "FreshChild")
    binding = handoff.with_name(handoff.name + ".binding.json")
    binding.write_text(json.dumps({**json.loads(binding.read_text()), "program": "claude-code"}))

    result = _run(["/bin/bash", str(SPAWN), "--pre-registered", "FreshChild", "--child-token-file", str(handoff),
                   "fresh task", str(workdir)], env)

    assert result.returncode == 0, result.stderr
    assert mail.tools() == []
