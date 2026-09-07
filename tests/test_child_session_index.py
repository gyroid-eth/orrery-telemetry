"""A pre-registered child gets a session index too.

The session index (`runtime/session_index/<agent_id>.json`) is what lets the
dashboard resume the exact transcript of an agent. It was written only by the
PostToolUse hook on register_agent, so a child that spawn_child.sh pre-registers
and that the SessionStart hook re-registers from the shell never had one, and
"resume" for a retired child fell back to guessing by name counts -- the
parent's transcript mentions the child as often as the child's own does (WSL2
Ubuntu, 2026-09-07: "会話ログが見つからず再開できません").
"""
from __future__ import annotations

import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "session-start-reminder.sh"
AGENT_ID = 77
CHILD = "CozyPlanck"


class _Mail(http.server.BaseHTTPRequestHandler):
    """health_check, ensure_project and register_agent, nothing else."""

    calls: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        params = req.get("params") or {}
        name = params.get("name")
        _Mail.calls.append(name)
        if name == "health_check":
            result = {"structuredContent": {"status": "ok"}}
        elif name == "ensure_project":
            result = {"structuredContent": {"id": 1}}
        elif name == "register_agent":
            args = params.get("arguments") or {}
            result = {"structuredContent": {
                "id": AGENT_ID, "name": args.get("name"),
                "registration_token": args.get("registration_token", ""),
            }}
        else:
            result = None
        body = json.dumps(
            {"jsonrpc": "2.0", "id": req.get("id"), "result": result}
            if result is not None else
            {"jsonrpc": "2.0", "id": req.get("id"),
             "error": {"code": -32601, "message": f"unknown tool {name!r}"}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(406)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture()
def mail() -> object:
    _Mail.calls = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Mail)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def _run_child_session_start(tmp_path: Path, mcp_url: str, *, with_transcript: bool = True):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # spawn_child.sh leaves the child's owner token here before the session starts.
    (runtime / f"agent_token_{CHILD}").write_text("tok-child\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "claude" / "projects" / "-p" / "abc123.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    payload = {"session_id": "sess-child-1", "hook_event_name": "SessionStart", "cwd": str(project)}
    if with_transcript:
        payload["transcript_path"] = str(transcript)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path / "home"),
        "AGENT_NAME": CHILD,
        "AGENTSTACK_RESERVED_IDENTITY": "1",
        "PROJECT_KEY": str(project),
        "AGENTSTACK_PROJECT_KEY": str(project),
        "AGENTSTACK_MCP_URL": mcp_url,
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_HOOKS_DIR": str(REPO_ROOT / "hooks"),
        "AGENTSTACK_REGISTER_LIB": str(REPO_ROOT / "bin" / "lib" / "agentstack-register.sh"),
    }
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        ["/bin/bash", str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    return result, runtime, transcript


def test_shell_registration_writes_the_session_index(mail: str, tmp_path: Path) -> None:
    result, runtime, transcript = _run_child_session_start(tmp_path, mail)
    assert "register_agent" in _Mail.calls, _Mail.calls
    assert "already registered" in result.stdout, result.stdout
    record = json.loads((runtime / "session_index" / f"{AGENT_ID}.json").read_text(encoding="utf-8"))
    assert record["agent_name"] == CHILD
    assert record["session_id"] == "sess-child-1"
    assert record["transcript_path"] == str(transcript)
    assert record["schema_version"] == 2 and record["binding_kind"] == "self"
    assert record["registered_by"] == CHILD, "the child bound itself, not a parent"


def test_the_index_is_exact_authority_for_the_dashboard(mail: str, tmp_path: Path, monkeypatch) -> None:
    _, runtime, transcript = _run_child_session_start(tmp_path, mail)
    import dashboard.server as server

    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(runtime / "session_index"))
    monkeypatch.setattr(server, "_agent_id_for_name", lambda name: AGENT_ID if name == CHILD else None)
    assert server._indexed_transcript(CHILD) == str(transcript)


def test_no_transcript_path_means_no_false_index(mail: str, tmp_path: Path) -> None:
    """The null case: a payload without a transcript writes a record that
    names no file, and the dashboard then falls back rather than resuming
    a path that does not exist."""
    _, runtime, _ = _run_child_session_start(tmp_path, mail, with_transcript=False)
    record = json.loads((runtime / "session_index" / f"{AGENT_ID}.json").read_text(encoding="utf-8"))
    assert record["transcript_path"] == ""
