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
    """Simulate a profile read and the effect of re-registering its name."""

    calls: list[str] = []
    profile: dict

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        _Mail.calls.append(name)
        if name == "health_check":
            result = {"structuredContent": {"status": "ok"}}
        elif name == "whois" and _Mail.profile:
            assert args["include_recent_commits"] is False
            result = {"structuredContent": {"name": args["agent_name"], **_Mail.profile}}
        elif name == "ensure_project":
            result = {"structuredContent": {"id": 1}}
        elif name == "register_agent":
            _Mail.register_args.append(dict(args))
            _Mail.profile.update({key: args[key] for key in ("program", "model", "task_description")})
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
    _Mail.register_args = []
    _Mail.profile = {"program": "claude-code"}
    server = http.server.HTTPServer(("127.0.0.1", 0), _Mail)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def _run_child_session_start(
    tmp_path: Path, mcp_url: str, *, with_transcript: bool = True, source: str = "startup",
    extra_env: dict | None = None, ancestry: tuple[tuple[str, str], ...] | None = None,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    # spawn_child.sh leaves the child's owner token here before the session starts.
    (runtime / f"agent_token_{CHILD}").write_text("tok-child\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text(f"#!/bin/bash\necho {CHILD}\n", encoding="utf-8")
    tmux.chmod(0o755)
    if ancestry is not None:
        ps = fake_bin / "ps"
        ps.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if sys.argv[1:3] != ['-ww', '-p']:\n"
            "    os.execv('/bin/ps', ['/bin/ps', *sys.argv[1:]])\n"
            f"ancestry = {ancestry!r}\n"
            "pid = int(sys.argv[3])\n"
            "level = pid - 5000001 if pid >= 5000002 else 0\n"
            "if level >= len(ancestry):\n"
            "    raise SystemExit(1)\n"
            "comm, args = ancestry[level]\n"
            "if not comm:\n"
            "    raise SystemExit(1)\n"
            "parent = 5000002 + level if level + 1 < len(ancestry) else 1\n"
            "print(args if sys.argv[5] == 'args=' else f'{parent} {comm}')\n",
            encoding="utf-8",
        )
        ps.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    transcript = tmp_path / "claude" / "projects" / "-p" / "abc123.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    payload = {"session_id": "sess-child-1", "hook_event_name": "SessionStart", "cwd": str(project), "source": source}
    if with_transcript:
        payload["transcript_path"] = str(transcript)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
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
        "CLAUDECODE": "1",
        "TMUX_PANE": "%42",
    }
    env.update(extra_env or {})
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        ["/bin/bash", str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stderr
    return result, runtime, transcript


def test_shell_registration_keeps_the_model_the_parent_chose(mail: str, tmp_path: Path) -> None:
    """spawn_child.sh passes CLAUDE_CHILD_MODEL; the re-registration must not
    replace it with the program name. With "claude-code" as the model the
    dashboard derives no provider: no logo, and a chip reading CLAUDE-CODE
    (seen on WSL2, where no pane model is parsed to rescue it)."""
    _run_child_session_start(tmp_path, mail, extra_env={"CLAUDE_CHILD_MODEL": "claude-sonnet-5"})
    assert _Mail.register_args and _Mail.register_args[-1]["model"] == "claude-sonnet-5", _Mail.register_args


def test_shell_registration_without_a_handed_model_still_names_the_program(mail: str, tmp_path: Path) -> None:
    _run_child_session_start(tmp_path, mail)
    assert _Mail.register_args and _Mail.register_args[-1]["model"] == "claude-code", _Mail.register_args


@pytest.mark.parametrize("source", ["startup", "resume"])
def test_shell_registration_writes_the_session_index(mail: str, tmp_path: Path, source: str) -> None:
    result, runtime, transcript = _run_child_session_start(tmp_path, mail, source=source, ancestry=(
        ("/bin/sh", ""), ("claude", ""), ("/bin/zsh", ""), ("tmux: server", ""),
    ))
    assert "register_agent" in _Mail.calls, _Mail.calls
    assert "already registered" in result.stdout, result.stdout
    assert (runtime / f"agent_token_{CHILD}").read_bytes() == b"tok-child"
    record = json.loads((runtime / "session_index" / f"{AGENT_ID}.json").read_text(encoding="utf-8"))
    assert record["agent_name"] == CHILD
    assert record["session_id"] == "sess-child-1"
    assert record["transcript_path"] == str(transcript)
    assert record["schema_version"] == 2 and record["binding_kind"] == "self"
    assert record["registered_by"] == CHILD, "the child bound itself, not a parent"


def test_nested_claude_keeps_the_parent_token_and_session_index(mail: str, tmp_path: Path) -> None:
    index = tmp_path / "runtime" / "session_index" / f"{AGENT_ID}.json"
    index.parent.mkdir(parents=True)
    parent_record = b'{"session_id":"parent-session"}\n'
    index.write_bytes(parent_record)

    result, runtime, _ = _run_child_session_start(tmp_path, mail, with_transcript=False, ancestry=(
        ("/bin/bash", ""),
        ("2.1.300", "/home/example/.local/share/claude/versions/2.1.300 -p"),
        ("/bin/zsh", ""),
        ("/home/example/.local/share/claude/versions/2.1.299", ""),
    ))

    assert _Mail.calls == []
    assert (runtime / f"agent_token_{CHILD}").read_bytes() == b"tok-child\n"
    assert index.read_bytes() == parent_record
    assert result.stdout == ""


def test_claude_nested_in_agy_preserves_the_live_parent(mail: str, tmp_path: Path, monkeypatch) -> None:
    from dashboard import server

    parent = {"name": CHILD, "program": "antigravity", "model": "gemini-fixture",
              "task_description": "Parent task", "last_active": 0}
    _Mail.profile = parent.copy()
    index = tmp_path / "runtime" / "session_index" / f"{AGENT_ID}.json"
    index.parent.mkdir(parents=True)
    index.write_bytes(b'{"session_id":"parent-session"}\n')
    env_file = tmp_path / "claude.env"
    result, runtime, _ = _run_child_session_start(tmp_path, mail, ancestry=(
        ("/bin/sh", ""), ("claude", ""), ("/bin/zsh", ""), ("agy", ""),
    ), extra_env={"CLAUDE_ENV_FILE": str(env_file)})

    assert _Mail.calls == ["health_check", "whois"]
    assert _Mail.profile == parent
    assert (runtime / f"agent_token_{CHILD}").read_bytes() == b"tok-child\n"
    assert index.read_bytes() == b'{"session_id":"parent-session"}\n'
    assert not (runtime / "live-sessions").exists()
    assert not env_file.exists()
    assert result.stdout == ""

    pane = {"cmd": "agy", "title": "", "pane_pid": 123}
    tree = ({123: "agy"}, {})
    _, category, running, _ = server._tmux_row_state(CHILD, pane, _Mail.profile, tree)
    assert server._is_live_roster_row({"category": category, "running": running})

    monkeypatch.setattr(server, "_raw_graph", lambda: {"nodes": [_Mail.profile.copy()]})
    monkeypatch.setattr(server, "tmux_state", lambda: {CHILD: pane})
    monkeypatch.setattr(server, "_process_tree_snapshot", lambda: tree)
    for attr in ("_codex_app_runtimes", "_deliverables_index", "_annotations", "_name_substitutions", "_persistent_profiles"):
        monkeypatch.setattr(server, attr, dict)
    monkeypatch.setattr(server, "_agent_runtime", lambda *_: {})
    monkeypatch.setattr(server, "_provider_stall_view", lambda *_: None)
    graph = server.graph_payload(days=7, show_all=False)
    assert [(node["name"], node["state"]) for node in graph["nodes"]] == [(CHILD, "run")]


def test_unconfirmed_provider_skips_registration(mail: str, tmp_path: Path) -> None:
    _Mail.profile = {}
    result, runtime, _ = _run_child_session_start(tmp_path, mail)
    assert _Mail.calls == ["health_check", "whois"]
    assert "provider を確認できなかった" in result.stdout
    assert not (runtime / "session_index").exists()


def test_mail_disabled_skips_profile_lookup_and_registration(mail: str, tmp_path: Path) -> None:
    result, runtime, _ = _run_child_session_start(tmp_path, mail, source="resume",
                                                  extra_env={"AGENTSTACK_MAIL_DISABLED": "1"})
    assert _Mail.calls == []
    assert result.stdout == ""
    assert not (runtime / "session_index").exists()


def test_claude_nested_in_codex_skips_the_global_entry(mail: str, tmp_path: Path) -> None:
    result, runtime, _ = _run_child_session_start(tmp_path, mail, ancestry=(
        ("node", "node /opt/npm/node_modules/@anthropic-ai/claude-code/cli.js -p"),
        ("/bin/bash", ""), ("codex", ""),
    ), extra_env={"AGENTSTACK_CLIENT_CONFIG": str(tmp_path / "missing-client-config.json")})

    assert result.stdout == ""
    assert _Mail.calls == []
    assert (runtime / f"agent_token_{CHILD}").read_bytes() == b"tok-child\n"
    assert not (runtime / "session_index").exists()


def test_unavailable_ancestry_preserves_shell_registration(mail: str, tmp_path: Path) -> None:
    _, runtime, _ = _run_child_session_start(tmp_path, mail, ancestry=(("claude", ""), ("", "")))

    assert "register_agent" in _Mail.calls
    record = json.loads((runtime / "session_index" / f"{AGENT_ID}.json").read_text())
    assert record["session_id"] == "sess-child-1"


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
