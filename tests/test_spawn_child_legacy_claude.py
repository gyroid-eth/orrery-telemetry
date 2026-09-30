"""`spawn_child.sh --pre-registered` and a Claude child from an earlier version.

A Claude child spawned before 2026.09.30.3 left a three-field state file
(`agent_name`, `project_key`, `registration_token`) and a token. The dashboard's
resume recognises that shape and moves it to the current one after ORRERY Mail
confirms the saved credential (#140/#141). The launcher did not: it refused the
child as "belongs to another registration" and told the operator to
preregister again, which registers a *new* name and leaves two agents in the
same role (WSL2 report, 2026-10-01, problem 3).

These tests start a disposable ORRERY Mail stand-in on a free port and use a
temporary HOME and runtime; nothing here reaches a running service.
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
    _executable,
    _fake_launch_env,
)


NAME = "AshPlanck"
PROJECT = "/shared/project"
# The earlier launcher minted secrets.token_urlsafe(32): 43 characters.
TOKEN = "legacy-owner-token-0123456789abcdefghijklmn"
AGENT_ID = 41
PROJECT_ID = 7
LEGACY = {"agent_name": NAME, "project_key": PROJECT, "registration_token": TOKEN}


class FakeMail:
    """Answers the three calls the launcher may make, and records them."""

    def __init__(self, *, advertises_existing_owner: bool = True,
                 accepts_owner: bool = True, program: str = "claude-code") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.advertises_existing_owner = advertises_existing_owner
        self.accepts_owner = accepts_owner
        self.program = program
        self.retired = False
        mail = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802 - stdlib handler API
                envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                result, error = mail.answer(envelope)
                if error:
                    result = {"isError": True, "content": [{"type": "text", "text": error}]}
                raw = json.dumps({"jsonrpc": "2.0", "id": envelope.get("id"), "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def answer(self, envelope: dict) -> tuple[dict, str | None]:
        if envelope["method"] == "tools/list":
            self.calls.append(("tools/list", {}))
            properties = {name: {} for name in ("project_key", "name", "program", "registration_token")}
            if self.advertises_existing_owner:
                properties["existing_agent_id"] = {}
            return {"tools": [{"name": "register_agent", "inputSchema": {"properties": properties}},
                              {"name": "whois", "inputSchema": {"properties": {}}}]}, None
        name = envelope["params"]["name"]
        arguments = envelope["params"]["arguments"]
        self.calls.append((name, arguments))
        profile = {"id": AGENT_ID, "name": NAME, "program": self.program, "project_id": PROJECT_ID}
        if self.retired:
            profile["retired_at"] = "2026-09-30T14:32:36Z"
        if name == "unretire_agent" and arguments.get("registration_token") == TOKEN:
            self.retired = False
            data = {"status": "active", "agent_name": NAME, "project_key": PROJECT}
            return {"structuredContent": data, "content": [{"type": "text", "text": json.dumps(data)}]}, None
        if name == "whois":
            if arguments.get("agent_name") != NAME or arguments.get("project_key") != PROJECT:
                return {}, "agent not found"
            return {"structuredContent": profile, "content": [{"type": "text", "text": json.dumps(profile)}]}, None
        if name == "register_agent":
            if (not self.accepts_owner or arguments.get("registration_token") != TOKEN
                    or arguments.get("existing_agent_id") != AGENT_ID
                    or arguments.get("name") != NAME or arguments.get("program") != self.program):
                return {}, "Existing-owner identity does not match"
            return {"structuredContent": profile, "content": [{"type": "text", "text": json.dumps(profile)}]}, None
        return {}, f"unexpected tool {name}"

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


def _legacy_child(tmp_path: pathlib.Path, mail: FakeMail) -> tuple[dict, pathlib.Path, pathlib.Path, pathlib.Path]:
    env, workdir = _fake_launch_env(tmp_path, codex=False)
    env["AGENTSTACK_MCP_URL"] = mail.endpoint
    # The stand-in has no bearer; never consult a Keychain from a test.
    env["AGENTSTACK_MAIL_HTTP_BEARER_MODE"] = "disabled"
    env["PARENT_AGENT"] = "ParentAgent"
    runtime = pathlib.Path(env["AGENTSTACK_RUNTIME_DIR"])
    (runtime / "child-agents").mkdir(parents=True)
    runtime.chmod(0o700)
    (runtime / "child-agents").chmod(0o700)
    token = runtime / f"agent_token_{NAME}"
    token.write_text(TOKEN, encoding="utf-8")
    token.chmod(0o600)
    state = runtime / "child-agents" / f"{NAME}.json"
    # Byte for byte what the earlier adopt_child_token_file wrote (json.dump,
    # default separators). The reporter's file was 131 B of exactly this shape.
    state.write_text(json.dumps(LEGACY), encoding="utf-8")
    state.chmod(0o600)
    return env, workdir, state, token


def _spawn(env: dict, workdir: pathlib.Path, tmp_path: pathlib.Path) -> subprocess.CompletedProcess:
    task = tmp_path / "task.md"
    task.write_text("legacy child task", encoding="utf-8")
    return subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", NAME, "--embed-task",
         "--task-file", str(task), str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False,
    )


def _launched(env: dict) -> bool:
    log = pathlib.Path(env["FAKE_TMUX_LOG"])
    return log.exists() and "new-session" in log.read_text(encoding="utf-8")


def test_a_legacy_claude_child_is_verified_and_relaunched_under_its_own_name(tmp_path, mail_factory):
    mail = mail_factory()
    env, workdir, state, token = _legacy_child(tmp_path, mail)
    assert set(json.loads(state.read_text(encoding="utf-8"))) == set(LEGACY)

    result = _spawn(env, workdir, tmp_path)

    # Before the fix this stopped at "child state belongs to another
    # registration" and advised preregistering, i.e. a second name.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == NAME
    assert _launched(env)
    # The owner was confirmed with the saved credential, the same way the
    # dashboard's resume does it; nothing registered a new identity.
    registers = [arguments for name, arguments in mail.calls if name == "register_agent"]
    assert registers == [{
        "project_key": PROJECT, "name": NAME, "program": "claude-code",
        "registration_token": TOKEN, "existing_agent_id": AGENT_ID,
    }]
    migrated = json.loads(state.read_text(encoding="utf-8"))
    assert migrated["agent_id"] == AGENT_ID
    assert migrated["agent_name"] == NAME
    assert migrated["project_key"] == PROJECT
    assert migrated["program"] == "claude-code"
    assert migrated["registration_token"] == TOKEN
    assert token.read_text(encoding="utf-8") == TOKEN
    assert not list(state.parent.glob(".*.registration-pending.json"))


def test_the_migrated_state_keeps_the_program_mail_recorded(tmp_path, mail_factory):
    """Resume compares the state's program with Mail's row exactly."""
    mail = mail_factory(program="claude")
    env, workdir, state, _token = _legacy_child(tmp_path, mail)

    result = _spawn(env, workdir, tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8"))["program"] == "claude"


@pytest.mark.parametrize("failure", ["owner_rejected", "mail_too_old", "mail_unreachable"])
def test_an_unverified_legacy_child_is_left_untouched_and_not_sent_to_preregister(
    tmp_path, mail_factory, failure,
):
    mail = mail_factory(accepts_owner=failure != "owner_rejected",
                        advertises_existing_owner=failure != "mail_too_old")
    env, workdir, state, token = _legacy_child(tmp_path, mail)
    if failure == "mail_unreachable":
        mail.close()
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in (state, token)}

    result = _spawn(env, workdir, tmp_path)

    assert result.returncode != 0
    assert not _launched(env)
    for path, saved in before.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == saved
    assert not list(state.parent.glob(".*.registration-pending.json"))
    # The advice that produced a second agent in the same role is gone, and the
    # route that does work is named.
    assert "Re-run agentstack-preregister-child" not in result.stderr
    assert "earlier version" in result.stderr
    assert "dashboard" in result.stderr
    if failure == "mail_too_old":
        assert "docs/agentstack-mail-update.md" in result.stderr
        assert not [name for name, _ in mail.calls if name == "register_agent"]


def test_a_failed_start_restores_the_legacy_state_exactly(tmp_path, mail_factory):
    mail = mail_factory()
    env, workdir, state, token = _legacy_child(tmp_path, mail)
    _executable(tmp_path / "bin" / "tmux", '#!/bin/bash\n[[ "$1" != new-session ]] || exit 1\nexit 0\n')
    before = {path: (path.read_bytes(), path.stat().st_mode) for path in (state, token)}

    result = _spawn(env, workdir, tmp_path)

    assert result.returncode != 0
    for path, saved in before.items():
        assert (path.read_bytes(), path.stat().st_mode) == saved
    assert not list(state.parent.glob(".*.registration-pending.json"))


def test_staging_never_promotes_a_legacy_state_without_an_authenticated_owner(tmp_path):
    from hooks import child_resume

    runtime = tmp_path / "runtime"
    (runtime / "child-agents").mkdir(parents=True)
    (runtime / f"agent_token_{NAME}").write_text(TOKEN)
    (runtime / f"agent_token_{NAME}").chmod(0o600)
    state = runtime / "child-agents" / f"{NAME}.json"
    state.write_text(json.dumps(LEGACY))
    state.chmod(0o600)

    with pytest.raises(child_resume.ResumeStateError):
        child_resume.stage_registration(runtime, NAME, project_key=PROJECT, program="claude-code",
                                        generation="a" * 32)
    # An owner confirmed for another project is not this child's owner.
    with pytest.raises(child_resume.ResumeStateError):
        child_resume.stage_registration(runtime, NAME, project_key="/other/project", program="claude-code",
                                        generation="a" * 32, legacy_agent_id=AGENT_ID)
    # Codex never had the three-field shape; do not accept it as one.
    with pytest.raises(child_resume.ResumeStateError):
        child_resume.stage_registration(runtime, NAME, project_key=PROJECT, program="codex",
                                        generation="a" * 32, legacy_agent_id=AGENT_ID)
    assert json.loads(state.read_text()) == LEGACY
    assert not list(state.parent.glob(".*.registration-pending.json"))


def test_a_legacy_child_retired_in_mail_is_adopted_and_made_active(tmp_path, mail_factory):
    """#150 review P2-2: the three-field state says nothing about retirement,
    so ORRERY Mail decides, after the owner is confirmed and the state moved."""
    mail = mail_factory()
    mail.retired = True
    env, workdir, state, _token = _legacy_child(tmp_path, mail)

    result = _spawn(env, workdir, tmp_path)

    assert result.returncode == 0, result.stderr
    assert [name for name, _ in mail.calls if name != "tools/list"] == [
        "whois", "register_agent", "whois", "unretire_agent"]
    assert not mail.retired
    assert json.loads(state.read_text(encoding="utf-8"))["agent_id"] == AGENT_ID
