"""Delayed Claude registration binds the captured session, never an old receipt."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from dashboard.claude_history import ReceiptIndex, history_binding
from test_reregister_diagnostics import AGENT_NAME, REREGISTER, ROOT, _fixture_env, _write_executable
from test_child_session_index import mail  # noqa: F401 - isolated MCP fixture

SID = "125687ab-1111-4222-8333-123456789abc"
OLD_SID = "125687ab-1111-4222-8333-123456789def"


@pytest.fixture
def recovery(tmp_path):
    env, log = _fixture_env(tmp_path)
    env.update(AGENT_NAME=AGENT_NAME, TMUX="", AGENTSTACK_MAIL_HTTP_BEARER_MODE="disabled")
    env.pop("TMUX_PANE", None)
    hook_bin = tmp_path / "hook-bin"
    hook_bin.mkdir()
    _write_executable(hook_bin / "tmux", '#!/bin/bash\nprintf "%s\\n" "${FIXTURE_TMUX_NAME:-FixtureAgent}"\n')
    env["PATH"] = f"{hook_bin}:{env['PATH']}"
    project = tmp_path / "project"
    project.mkdir()
    env["AGENTSTACK_PROJECT_KEY"] = str(project)
    projects = tmp_path / "claude" / "projects"
    directory = projects / "-fixture-project"
    directory.mkdir(parents=True)
    for sid in (SID, OLD_SID):
        (directory / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "sessionId": sid}) + "\n")
    payload = {"session_id": SID, "transcript_path": str(directory / f"{SID}.jsonl"),
               "cwd": str(project), "source": "startup", "hook_event_name": "SessionStart"}
    envfile = tmp_path / "session-env.sh"
    env["CLAUDE_ENV_FILE"] = str(envfile)
    path = Path(env["AGENTSTACK_RUNTIME_DIR"]) / "session_index" / "41.json"
    path.parent.mkdir()
    old = {"schema_version": 2, "binding_kind": "self", "agent_id": 41,
           "agent_name": AGENT_NAME, "registered_by": AGENT_NAME, "project_key": str(project),
           "session_id": OLD_SID, "transcript_path": str(directory / f"{OLD_SID}.jsonl"),
           "cwd": str(project), "ts": "2000-01-01T00:00:00"}
    path.write_text(json.dumps(old))
    return dict(env=env, log=log, path=path, payload=payload, envfile=envfile,
                old=old, projects=projects, project=project, hook_bin=hook_bin)


def start(recovery, *, mail_url="http://127.0.0.1:1/mcp", **updates):
    env = dict(recovery["env"], AGENTSTACK_MCP_URL=mail_url,
               PATH=f"{recovery['hook_bin']}:/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(updates)
    result = subprocess.run(["/bin/bash", str(ROOT / "hooks/session-start-reminder.sh")],
                            input=json.dumps(recovery["payload"]), text=True, capture_output=True,
                            cwd=recovery["project"], env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    return result


def recover(recovery, *, name=AGENT_NAME, program="claude-code", **updates):
    env = dict(recovery["env"])
    env.update(updates)
    return subprocess.run(
        ["/bin/bash", "-c", '. "$1"; shift; exec "$@"', "recovery",
         str(recovery["envfile"]), "/bin/bash", str(REREGISTER), name, program],
        cwd=recovery["project"], env=env, text=True, capture_output=True, timeout=30,
    )


def binding(recovery, session_created):
    return history_binding(AGENT_NAME, {"agent_id": 41, "program": "claude-code",
                           "project_key": str(recovery["project"])},
                           projects=str(recovery["projects"]),
                           index=ReceiptIndex(str(recovery["path"].parent)),
                           session_created=session_created)


def test_outage_then_reregister_replaces_stale_receipt_with_exact_current_session(recovery):
    # Bare Claude in a named pane has no launcher-exported AGENT_NAME.
    recovery["env"].pop("AGENT_NAME")
    recovery["env"]["TMUX_PANE"] = "%42"
    created = int(time.time())
    assert binding(recovery, created)["history_binding_reason_code"] == "receipt_predates_session"
    result = start(recovery)
    assert "not running" in result.stdout
    assert json.loads(recovery["path"].read_text()) == recovery["old"]
    result = recover(recovery)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    record = json.loads(recovery["path"].read_text())
    for key in ("session_id", "transcript_path", "cwd"):
        assert record[key] == recovery["payload"][key]
    assert record["registered_by"] == record["agent_name"] == AGENT_NAME
    assert record["schema_version"] == 2 and record["binding_kind"] == "self"
    assert binding(recovery, created)["history_binding"] == "bound"
    calls = [json.loads(line)["tool"] for line in recovery["log"].read_text().splitlines()]
    assert calls == ["ensure_project", "register_agent"]  # no receipt recovery RPC


@pytest.mark.parametrize("mode", ["transport", "tool", "identity"])
def test_failed_registration_keeps_old_receipt(recovery, mode):
    start(recovery)
    result = recover(recovery, FAKE_REGISTER_MODE=mode)
    assert result.returncode != 0
    assert json.loads(recovery["path"].read_text()) == recovery["old"]


@pytest.mark.parametrize("change", ["another-name", "another-project", "unresolved-caller", "conflict", "malformed-context"])
def test_successful_remote_registration_cannot_bind_another_session(recovery, change):
    start(recovery)
    updates = {}
    if change == "another-name":
        updates["AGENT_NAME"] = "DifferentAgent"
    elif change == "another-project":
        updates["AGENTSTACK_PROJECT_KEY"] = str(recovery["project"]) + "-other"
    elif change == "unresolved-caller":
        updates.update(AGENT_NAME="", TMUX_PANE="")
    elif change == "conflict":
        updates.update(AGENT_NAME="", TMUX_PANE="%42", FIXTURE_TMUX_NAME="DifferentAgent")
        runtime = Path(recovery["env"]["AGENTSTACK_RUNTIME_DIR"])
        (runtime / "agent_name__42").write_text(AGENT_NAME)
    else:
        recovery["envfile"].write_text("export AGENTSTACK_CLAUDE_SESSION_CONTEXT='[]'\n")
    result = recover(recovery, **updates)
    assert result.returncode == 0  # remote registration is still reported truthfully
    assert "stage=session-receipt reason=binding-not-written" in result.stderr
    assert json.loads(recovery["path"].read_text()) == recovery["old"]


def test_registering_another_identity_does_not_write_its_receipt(recovery):
    start(recovery)
    result = recover(recovery, name="DifferentAgent", CHILD_REGISTRATION_TOKEN="fixture-token")
    assert result.returncode == 0
    assert "binding-not-written" in result.stderr
    assert json.loads(recovery["path"].read_text()) == recovery["old"]


def test_no_credential_remains_fail_closed_then_operator_recovery_can_bind(recovery, mail):
    token = Path(recovery["env"]["AGENTSTACK_RUNTIME_DIR"]) / f"agent_token_{AGENT_NAME}"
    token.unlink()
    result = start(recovery, mail_url=mail)
    assert "local credential がありません" in result.stdout
    result = recover(recovery)
    assert result.returncode != 0
    assert "reason=credential-unavailable" in result.stderr
    assert not recovery["log"].exists()
    assert json.loads(recovery["path"].read_text()) == recovery["old"]
    # Enrollment itself remains an operator task outside this repair.
    token.write_text("operator-restored-fixture-token")
    assert recover(recovery).returncode == 0
    assert json.loads(recovery["path"].read_text())["session_id"] == SID


def test_codex_reregister_does_not_consume_claude_context(recovery):
    start(recovery)
    result = recover(recovery, program="codex")
    assert result.returncode == 0 and not result.stderr
    assert json.loads(recovery["path"].read_text()) == recovery["old"]


def test_latest_start_context_wins_and_shell_metacharacters_are_data(recovery):
    start(recovery)
    value = str(recovery["project"] / "'`touch SHOULD_NOT_EXIST`$(touch ALSO_NOT_EXIST)\n")
    sid = "125687ab-1111-4222-8333-123456789aaa"
    transcript = Path(recovery["payload"]["transcript_path"]).with_name(f"{sid}.jsonl")
    transcript.write_text(json.dumps({"type": "user", "sessionId": sid}) + "\n")
    recovery["payload"].update(cwd=value, session_id=sid, transcript_path=str(transcript), source="clear")
    start(recovery)
    result = recover(recovery)
    assert result.returncode == 0 and not result.stderr
    record = json.loads(recovery["path"].read_text())
    assert record["cwd"] == value
    assert record["session_id"] == sid
    assert record["transcript_path"] == str(transcript)
    assert not (recovery["project"] / "SHOULD_NOT_EXIST").exists()
    assert not (recovery["project"] / "ALSO_NOT_EXIST").exists()


def test_healthy_start_clears_inherited_recovery_context(recovery, mail):
    start(recovery)
    assert "agent_name" in recovery["envfile"].read_text()
    result = start(recovery, mail_url=mail)
    assert "already registered" in result.stdout
    assert recovery["envfile"].read_text().splitlines()[-1] == "export AGENTSTACK_CLAUDE_SESSION_CONTEXT=''"


def test_missing_env_file_keeps_existing_startup_behavior(recovery):
    result = start(recovery, CLAUDE_ENV_FILE="")
    assert "not running" in result.stdout
    assert not recovery["envfile"].exists()
    assert json.loads(recovery["path"].read_text()) == recovery["old"]
    recovery["envfile"].write_text("export AGENTSTACK_CLAUDE_SESSION_CONTEXT=''\n")
    result = recover(recovery)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(recovery["path"].read_text()) == recovery["old"]


def test_receipt_io_failure_is_reported_without_claiming_remote_registration_failed(recovery):
    start(recovery)
    recovery["path"].with_suffix(".json.tmp").mkdir()
    result = recover(recovery)
    assert result.returncode == 0
    assert result.stdout == f"agentstack-reregister: registered {AGENT_NAME}\n"
    assert "stage=session-receipt reason=binding-not-written" in result.stderr
    assert json.loads(recovery["path"].read_text()) == recovery["old"]
