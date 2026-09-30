"""Retention lifecycle coverage for resumable Codex children (issue #59)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dashboard import server


ROOT = Path(__file__).resolve().parents[1]
AGENT = "RetainedCodex"
AGENT_ID = 73
PROJECT = "/fixture/project"
TOKEN = "fixture-owner-token"
SESSION_ID = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"


def _private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _seed_bound_child_receipt(
    runtime: Path,
    project: str,
    transcript: Path,
    *,
    session_id: str = SESSION_ID,
) -> None:
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": project},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _private(
        runtime / "session_index" / f"{AGENT_ID}.json",
        json.dumps(
            {
                "schema_version": 2,
                "binding_kind": "self",
                "provider": "codex",
                "program": "codex",
                "agent_id": AGENT_ID,
                "agent_name": AGENT,
                "project_key": project,
                "registered_by": AGENT,
                "launch_id": "prior-launch",
                "receipt_id": "prior-receipt",
                "launch_kind": "startup",
                "session_id": session_id,
                "transcript_path": str(transcript),
                "source": "startup",
                "recorded_at": "2026-09-23T00:00:00+00:00",
                "launch_origin": "child",
                "codex_mcp_profile": "orrery-only",
            }
        ),
    )


def _retained_state(*, expires_at: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "launch_origin": "child",
        "provider": "codex",
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": PROJECT,
        "program": "codex",
        "codex_mcp_profile": "orrery-only",
        "registration_token": TOKEN,
        "retired_at": "2026-09-21T00:00:00Z",
        "resume_expires_at": expires_at,
    }


def test_normal_cleanup_retains_codex_credential_but_removes_generated_home(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    mcp_config = runtime / "child-agents" / f"{AGENT}.mcp.json"
    child_home = runtime / "child-agents" / f"{AGENT}.codex-home"
    _private(
        state_file,
        json.dumps(
            {
                **_retained_state(expires_at="2026-10-21T00:00:00Z"),
                "retired_at": None,
                "resume_expires_at": None,
            }
        ),
    )
    _private(token_file, TOKEN)
    _private(mcp_config, "{}")
    child_home.mkdir(parents=True)
    _private(child_home / "config.toml", "# generated\n")

    env = os.environ.copy()
    env.update(
        {
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_MANAGED_AGENTS_FILE": str(runtime / "managed_agents.txt"),
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS": "30",
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "hooks" / "cleanup-child-agent.sh"), AGENT],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert state_file.is_file()
    assert token_file.is_file()
    retained = json.loads(state_file.read_text(encoding="utf-8"))
    assert retained["retired_at"].endswith("Z")
    assert retained["resume_expires_at"].endswith("Z")
    assert retained["registration_token"] == TOKEN
    assert not child_home.exists()
    assert not mcp_config.exists()


def test_zero_day_retention_keeps_the_historical_full_delete(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    _private(
        state_file,
        json.dumps(
            {
                **_retained_state(expires_at="2999-01-01T00:00:00Z"),
                "retired_at": None,
                "resume_expires_at": None,
            }
        ),
    )
    _private(token_file, TOKEN)
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "hooks" / "cleanup-child-agent.sh"), AGENT],
        cwd=ROOT,
        env={
            **os.environ,
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
            "AGENTSTACK_MANAGED_AGENTS_FILE": str(runtime / "managed_agents.txt"),
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:1/mcp",
            "AGENTSTACK_CHILD_RESUME_RETENTION_DAYS": "0",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not state_file.exists()
    assert not token_file.exists()


def test_expired_retained_state_is_a_fixed_fail_closed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    _private(state_file, json.dumps(_retained_state(expires_at="2026-09-20T00:00:00Z")))
    _private(token_file, TOKEN)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    registration = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": PROJECT,
        "program": "codex",
    }

    with pytest.raises(server._ResumeCapabilityError) as raised:
        server._codex_resume_child_home(AGENT, registration)

    assert raised.value.code == "retention_expired"


def test_explicit_purge_removes_only_private_resume_material(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    receipt = runtime / "session_index" / f"{AGENT_ID}.json"
    child_home = runtime / "child-agents" / f"{AGENT}.codex-home"
    _private(state_file, json.dumps(_retained_state(expires_at="2026-10-21T00:00:00Z")))
    _private(token_file, TOKEN)
    _private(receipt, '{"binding_kind":"self"}\n')
    child_home.mkdir(parents=True)
    _private(child_home / "config.toml", "# generated\n")
    command = ROOT / "bin" / "agentstack-purge-child-resume"

    assert command.is_file()
    result = subprocess.run(
        [str(command), "--runtime-dir", str(runtime), AGENT],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not state_file.exists()
    assert not token_file.exists()
    assert not child_home.exists()
    assert receipt.is_file()
    tombstone = json.loads(
        (runtime / "child-resume-tombstones" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert tombstone["reason"] == "purged"


def test_explicit_purge_refuses_non_child_state_and_preserves_credential(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    _private(
        state_file,
        json.dumps(
            {
                "agent_id": AGENT_ID,
                "agent_name": AGENT,
                "project_key": PROJECT,
                "program": "codex",
                "registration_token": TOKEN,
            }
        ),
    )
    _private(token_file, TOKEN)

    result = subprocess.run(
        [
            str(ROOT / "bin" / "agentstack-purge-child-resume"),
            "--runtime-dir",
            str(runtime),
            AGENT,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert state_file.is_file()
    assert token_file.is_file()


def test_expiry_maintenance_does_not_purge_a_resumed_child_in_progress(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    _private(
        state_file,
        json.dumps(
            {
                **_retained_state(expires_at="2026-09-20T00:00:00Z"),
                "resume_in_progress_at": "2026-09-19T23:59:59Z",
            }
        ),
    )
    _private(token_file, TOKEN)

    result = subprocess.run(
        [
            str(ROOT / "bin" / "agentstack-purge-child-resume"),
            "--runtime-dir",
            str(runtime),
            "--expired",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert state_file.is_file()
    assert token_file.is_file()

    explicit = subprocess.run(
        [
            str(ROOT / "bin" / "agentstack-purge-child-resume"),
            "--runtime-dir",
            str(runtime),
            AGENT,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert explicit.returncode != 0
    assert state_file.is_file()
    assert token_file.is_file()


@pytest.mark.parametrize("unretire_succeeds", [True, False])
def test_resume_bootstrap_preserves_child_provenance_before_unretire(
    tmp_path: Path, unretire_succeeds: bool
) -> None:
    install_home = tmp_path / "agentstack"
    bindir = install_home / "bin"
    hooks = install_home / "hooks"
    libdir = bindir / "lib"
    libdir.mkdir(parents=True)
    hooks.mkdir(parents=True)
    bootstrap = bindir / "agentstack-codex-bootstrap"
    shutil.copy2(ROOT / "bin" / "agentstack-codex-bootstrap", bootstrap)
    shutil.copy2(ROOT / "hooks" / "prepare-codex-session-binding.py", hooks)
    shutil.copy2(ROOT / "hooks" / "child_resume.py", hooks)
    call_log = tmp_path / "mcp-calls.log"
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() {\n"
        "  printf '%s\\n' \"$1\" >> \"$FAKE_MCP_CALL_LOG\"\n"
        "  if [[ \"$1\" == unretire_agent ]]; then\n"
        "    grep -q '\"launch_kind\":\"resume\"' \"$AGENTSTACK_RUNTIME_DIR/codex_launches/73.json\" || return 1\n"
        "    grep -q '\"launch_origin\":\"child\"' \"$AGENTSTACK_RUNTIME_DIR/codex_launches/73.json\" || return 1\n"
        "    grep -q '\"resume_in_progress_at\":' \"$AGENTSTACK_RUNTIME_DIR/child-agents/RetainedCodex.json\" || return 1\n"
        "    [[ \"${FAIL_UNRETIRE:-0}\" != 1 ]] || return 1\n"
        "  fi\n"
        "  printf '{\"result\":{\"structuredContent\":{\"id\":1}}}\\n'\n"
        "}\n"
        "ags_mcp_has_error() { return 1; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_register_session() {\n"
        "  printf 'register_agent\\n' >> \"$FAKE_MCP_CALL_LOG\"\n"
        "  AGS_REGISTERED_AGENT_NAME=RetainedCodex\n"
        "  AGS_REGISTERED_AGENT_ID=73\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    _private(
        state_file,
        json.dumps(_retained_state(expires_at="2999-01-01T00:00:00Z")),
    )
    _private(runtime / f"agent_token_{AGENT}", TOKEN)
    _seed_bound_child_receipt(runtime, PROJECT, tmp_path / "prior-rollout.jsonl")
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'source "{bootstrap}" "{tmp_path}"',
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "AGENTSTACK_PROJECT_KEY": PROJECT,
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_HOOKS_DIR": str(hooks),
            "AGENTSTACK_PYTHON": sys.executable,
            "AGENTSTACK_RESERVED_IDENTITY": "1",
            "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
            "AGENTSTACK_CODEX_RESUME_SESSION_ID": SESSION_ID,
            "AGENTSTACK_CODEX_CHILD_MCP_PROFILE": "orrery-only",
            "AGENT_NAME": AGENT,
            "FAKE_MCP_CALL_LOG": str(call_log),
            "FAIL_UNRETIRE": "0" if unretire_succeeds else "1",
            "TMUX": "",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert (result.returncode == 0) is unretire_succeeds, result.stderr
    launch = json.loads(
        (runtime / "codex_launches" / f"{AGENT_ID}.json").read_text(
            encoding="utf-8"
        )
    )
    assert launch["launch_origin"] == "child"
    assert launch["codex_mcp_profile"] == "orrery-only"
    assert launch["resume_session_id"] == SESSION_ID
    assert launch["fallback_launch_id"] == "prior-launch"
    assert call_log.read_text(encoding="utf-8").splitlines()[-1] == "unretire_agent"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if unretire_succeeds:
        assert state["resume_in_progress_at"].endswith("Z")
    else:
        assert "resume_in_progress_at" not in state
        assert state["retired_at"] == "2026-09-21T00:00:00Z"
        assert state["resume_expires_at"] == "2999-01-01T00:00:00Z"


def test_retained_state_permission_and_purge_are_fixed_fail_closed_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    _private(state_file, json.dumps(_retained_state(expires_at="2999-01-01T00:00:00Z")))
    _private(token_file, TOKEN)
    state_file.chmod(0o644)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    registration = {
        "agent_id": AGENT_ID,
        "agent_name": AGENT,
        "project_key": PROJECT,
        "program": "codex",
    }

    with pytest.raises(server._ResumeCapabilityError) as unsafe:
        server._codex_resume_child_home(AGENT, registration)
    assert unsafe.value.code == "credential_permission"

    state_file.chmod(0o600)
    purged = subprocess.run(
        [
            str(ROOT / "bin" / "agentstack-purge-child-resume"),
            "--runtime-dir",
            str(runtime),
            AGENT,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert purged.returncode == 0, purged.stderr
    with pytest.raises(server._ResumeCapabilityError) as removed:
        server._codex_resume_child_home(AGENT, registration)
    assert removed.value.code == "purged"


def test_failure_before_codex_exec_discards_home_but_keeps_retired_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    source_home = home / ".codex"
    source_home.mkdir(parents=True)
    _private(source_home / "config.toml", 'model = "gpt-5.6-terra"\n')
    rollout = tmp_path / "rollout.jsonl"
    session_id = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(project)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = {
        **_retained_state(expires_at="2999-01-01T00:00:00Z"),
        "project_key": str(project),
    }
    state_file = runtime / "child-agents" / f"{AGENT}.json"
    token_file = runtime / f"agent_token_{AGENT}"
    _private(state_file, json.dumps(state))
    _private(token_file, TOKEN)
    install_home = tmp_path / "agentstack"
    hooks = install_home / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(ROOT / "hooks" / "child_resume.py", hooks)
    bootstrap = install_home / "bin" / "agentstack-codex-bootstrap"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("return 17\n", encoding="utf-8")
    runner = (
        install_home
        / "integrations"
        / "codex_app"
        / "plugin"
        / "scripts"
        / "run-mcp.sh"
    )
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    marker = tmp_path / "codex-ran"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        f"#!/usr/bin/env bash\nprintf ran > {str(marker)!r}\n", encoding="utf-8"
    )
    fake_codex.chmod(0o755)
    launched: list[list[str]] = []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setenv("AGENTSTACK_PYTHON", sys.executable)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))
    monkeypatch.setattr(
        server,
        "_codex_registration",
        lambda _name: {
            "agent_id": AGENT_ID,
            "agent_name": AGENT,
            "project_key": str(project),
            "program": "codex",
        },
    )
    monkeypatch.setattr(
        server,
        "_open_terminal_tmux",
        lambda args, **_kwargs: launched.append(args)
        or {"ok": True, "adapter": "fixture"},
    )

    result = server._do_resume_codex(AGENT)
    assert result["ok"] is True
    child_home = runtime / "child-agents" / f"{AGENT}.codex-home"
    assert child_home.is_dir()
    command = subprocess.run(
        ["/bin/bash", "-c", launched[0][-1]],
        cwd=project,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert command.returncode == 17
    assert not marker.exists()
    assert not child_home.exists()
    assert state_file.is_file()
    assert token_file.is_file()


@pytest.mark.parametrize("mail_knows_agent", [True, False])
def test_real_resume_command_can_cleanup_and_resume_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mail_knows_agent: bool
) -> None:
    """Exercise the actual shell registration/prepare/cleanup command chain."""

    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    source_home = home / ".codex"
    sessions = source_home / "sessions"
    sessions.mkdir(parents=True)
    source_config = source_home / "config.toml"
    _private(source_config, 'profile_marker = "first"\n')
    session_id = SESSION_ID
    rollout = sessions / "rollout-fixture.jsonl"
    _seed_bound_child_receipt(
        runtime,
        str(project),
        rollout,
        session_id=session_id,
    )
    _private(
        runtime / "child-agents" / f"{AGENT}.json",
        json.dumps(
            {
                **_retained_state(expires_at="2999-01-01T00:00:00Z"),
                "project_key": str(project),
            }
        ),
    )
    _private(runtime / f"agent_token_{AGENT}", TOKEN)

    install_home = tmp_path / "agentstack"
    bindir = install_home / "bin"
    libdir = bindir / "lib"
    hooks = install_home / "hooks"
    libdir.mkdir(parents=True)
    hooks.mkdir(parents=True)
    for source, target in (
        (ROOT / "bin" / "agentstack-codex-bootstrap", bindir / "agentstack-codex-bootstrap"),
        (ROOT / "bin" / "lib" / "agentstack-register.sh", libdir / "agentstack-register.sh"),
        (ROOT / "bin" / "lib" / "agentstack-scientists.sh", libdir / "agentstack-scientists.sh"),
        (ROOT / "hooks" / "prepare-codex-session-binding.py", hooks / "prepare-codex-session-binding.py"),
        (ROOT / "hooks" / "child_resume.py", hooks / "child_resume.py"),
        (ROOT / "hooks" / "cleanup-child-agent.sh", hooks / "cleanup-child-agent.sh"),
        (ROOT / "hooks" / "project-context.sh", hooks / "project-context.sh"),
        (ROOT / "hooks" / "resolve-agent-name.sh", hooks / "resolve-agent-name.sh"),
    ):
        shutil.copy2(source, target)
    proxy = (
        install_home
        / "integrations"
        / "codex_app"
        / "plugin"
        / "scripts"
        / "run-mcp.sh"
    )
    proxy.parent.mkdir(parents=True)
    proxy.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    proxy.chmod(0o755)

    calls: list[tuple[str, dict[str, object]]] = []
    remote = {"retired": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            envelope = json.loads(self.rfile.read(length))
            name = envelope["params"]["name"]
            arguments = envelope["params"]["arguments"]
            calls.append((name, arguments))
            error = None
            if name == "ensure_project":
                result = {"id": 1, "human_key": str(project)}
            elif name == "register_agent":
                if arguments.get("registration_token") != TOKEN or not remote["retired"]:
                    error = "credential registration rejected"
                    result = {}
                else:
                    result = {
                        "id": AGENT_ID,
                        "name": AGENT,
                        "program": "codex",
                        "registration_token": TOKEN,
                    }
            elif name == "unretire_agent":
                launch = json.loads(
                    (runtime / "codex_launches" / f"{AGENT_ID}.json").read_text(
                        encoding="utf-8"
                    )
                )
                resume_state = json.loads(
                    (runtime / "child-agents" / f"{AGENT}.json").read_text(
                        encoding="utf-8"
                    )
                )
                if (
                    launch.get("launch_kind") != "resume"
                    or launch.get("launch_origin") != "child"
                    or launch.get("codex_mcp_profile") != "orrery-only"
                    or not resume_state.get("resume_in_progress_at")
                ):
                    error = "fresh child binding missing"
                else:
                    remote["retired"] = False
                result = {"id": AGENT_ID, "name": AGENT}
            elif name == "whois":
                # A reserved identity must already exist in the target project
                # before register_agent. Real Mail's whois also finds retired agents.
                if mail_knows_agent:
                    result = {"id": AGENT_ID, "name": AGENT, "program": "codex"}
                else:
                    error = f"Agent '{AGENT}' not found in project '{project}'."
                    result = {}
            elif name == "retire_agent":
                if arguments.get("registration_token") != TOKEN:
                    error = "owner credential missing"
                remote["retired"] = True
                result = {"id": AGENT_ID, "name": AGENT}
            else:
                result = {"id": 1, "status": "ok"}
            if error:
                payload = {
                    "jsonrpc": "2.0",
                    "id": envelope.get("id"),
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": error}],
                    },
                }
            else:
                payload = {
                    "jsonrpc": "2.0",
                    "id": envelope.get("id"),
                    "result": {
                        "structuredContent": result,
                        "content": [
                            {"type": "text", "text": json.dumps(result)}
                        ],
                    },
                }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    fixture_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=fixture_server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{fixture_server.server_port}/mcp"

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [[ \"${1:-}\" == new-session ]]; then\n"
        "  shift; session= cwd=\n"
        "  while [[ $# -gt 0 ]]; do\n"
        "    case \"$1\" in -A) shift;; -s) session=$2; shift 2;; -c) cwd=$2; shift 2;; *) break;; esac\n"
        "  done\n"
        "  export TMUX=/tmp/fixture TMUX_PANE=%0 FAKE_TMUX_SESSION=$session\n"
        "  cd \"$cwd\"\n"
        "  exec /bin/bash -c \"${@: -1}\"\n"
        "fi\n"
        "case \"${1:-}\" in\n"
        "  display-message) printf '%s\\n' \"$FAKE_TMUX_SESSION\";;\n"
        "  has-session) exit 1;;\n"
        "  *) exit 0;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "count=$(cat \"$FAKE_CODEX_COUNT\" 2>/dev/null || printf 0)\n"
        "count=$((count + 1)); printf '%s' \"$count\" > \"$FAKE_CODEX_COUNT\"\n"
        "cp \"$CODEX_HOME/config.toml\" \"$FAKE_CODEX_CAPTURE.$count.toml\"\n"
        "printf '%s\\n' \"$*\" > \"$FAKE_CODEX_CAPTURE.$count.args\"\n"
        "\"$AGENTSTACK_PYTHON\" \"$FAKE_RECORDER\" < \"$FAKE_HOOK_PAYLOAD\"\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    fake_codex.chmod(0o755)
    hook_payload = tmp_path / "hook.json"
    hook_payload.write_text(
        json.dumps(
            {
                "hook_event_name": "SessionStart",
                "source": "resume",
                "session_id": session_id,
                "transcript_path": str(rollout),
                "cwd": str(project),
                "model": "gpt-5.6-terra",
                "permission_mode": "dontAsk",
            }
        ),
        encoding="utf-8",
    )
    capture = tmp_path / "codex"
    count = tmp_path / "codex-count"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv("AGENTSTACK_HOME", str(install_home))
    monkeypatch.setenv("AGENTSTACK_PROJECT_KEY", str(project))
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("AGENTSTACK_HOOKS_DIR", str(hooks))
    monkeypatch.setenv("AGENTSTACK_MCP_URL", endpoint)
    monkeypatch.setenv("AGENTSTACK_MAIL_HTTP_BEARER_MODE", "disabled")
    monkeypatch.setenv("AGENTSTACK_PYTHON", sys.executable)
    monkeypatch.setenv("AGENTSTACK_CHILD_SHELL", "/bin/bash")
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_CODEX_COUNT", str(count))
    monkeypatch.setenv("FAKE_RECORDER", str(
        ROOT
        / "integrations"
        / "codex_app"
        / "plugin"
        / "scripts"
        / "record-codex-session-index.py"
    ))
    monkeypatch.setenv("FAKE_HOOK_PAYLOAD", str(hook_payload))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(server, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(server, "SESSION_INDEX_DIR", str(runtime / "session_index"))
    monkeypatch.setattr(server, "CODEX_LAUNCH_DIR", str(runtime / "codex_launches"))
    monkeypatch.setattr(server, "MAIL_ENV_PATH", str(tmp_path / "mail.env"))
    monkeypatch.setattr(server, "MAIL_HTTP_BEARER_MODE", "disabled")
    monkeypatch.setattr(server, "_codex_transcript_path", lambda _name: str(rollout))
    monkeypatch.setattr(
        server,
        "_codex_registration",
        lambda _name: {
            "agent_id": AGENT_ID,
            "agent_name": AGENT,
            "project_key": str(project),
            "program": "codex",
        },
    )

    def run_terminal(args, **_kwargs):
        completed = subprocess.run(
            [str(fake_tmux), *args[1:]],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        return {
            "ok": completed.returncode == 0,
            "adapter": "fixture",
            "error": completed.stderr or completed.stdout,
        }

    monkeypatch.setattr(server, "_open_terminal_tmux", run_terminal)
    try:
        first = server._do_resume_codex(AGENT)
        if not mail_knows_agent:
            assert first["ok"] is False, first
            assert "could not be registered" in first["error"], first
            methods = [name for name, _args in calls]
            assert "whois" in methods
            assert "register_agent" not in methods
            assert "unretire_agent" not in methods
            return
        assert first["ok"] is True, first
        assert remote["retired"] is True
        assert not (runtime / "child-agents" / f"{AGENT}.codex-home").exists()
        first_receipt = json.loads(
            (runtime / "session_index" / f"{AGENT_ID}.json").read_text(
                encoding="utf-8"
            )
        )
        assert first_receipt["binding_kind"] == "self"
        assert first_receipt["launch_origin"] == "child"
        assert first_receipt["codex_mcp_profile"] == "orrery-only"
        assert 'profile_marker = "first"' in Path(
            f"{capture}.1.toml"
        ).read_text(encoding="utf-8")
        retained = json.loads(
            (runtime / "child-agents" / f"{AGENT}.json").read_text(
                encoding="utf-8"
            )
        )
        assert "resume_in_progress_at" not in retained

        source_config.write_text('profile_marker = "second"\n', encoding="utf-8")
        source_config.chmod(0o600)
        second = server._do_resume_codex(AGENT)
        assert second["ok"] is True, second
        assert remote["retired"] is True
        second_receipt = json.loads(
            (runtime / "session_index" / f"{AGENT_ID}.json").read_text(
                encoding="utf-8"
            )
        )
        assert second_receipt["binding_kind"] == "self"
        assert second_receipt["launch_origin"] == "child"
        assert second_receipt["codex_mcp_profile"] == "orrery-only"
        retained = json.loads(
            (runtime / "child-agents" / f"{AGENT}.json").read_text(
                encoding="utf-8"
            )
        )
        assert "resume_in_progress_at" not in retained
        assert 'profile_marker = "second"' in Path(
            f"{capture}.2.toml"
        ).read_text(encoding="utf-8")
    finally:
        fixture_server.shutdown()
        fixture_server.server_close()
        server_thread.join(timeout=5)

    methods = [name for name, _args in calls]
    assert methods.count("register_agent") == 2
    assert methods.count("unretire_agent") == 2
    assert methods.count("retire_agent") == 2
    for method in ("register_agent", "unretire_agent", "retire_agent"):
        positions = [index for index, name in enumerate(methods) if name == method]
        assert len(positions) == 2
    first_register = methods.index("register_agent")
    first_unretire = methods.index("unretire_agent")
    first_retire = methods.index("retire_agent")
    assert first_register < first_unretire < first_retire
    second_register = methods.index("register_agent", first_retire + 1)
    second_unretire = methods.index("unretire_agent", first_retire + 1)
    second_retire = methods.index("retire_agent", first_retire + 1)
    assert second_register < second_unretire < second_retire
