"""Before a resume, a Claude row says whether Mail comes back with it (#145).

`resume_capability: ready` answers "can this conversation be resumed". It
covered conversation-only resumes too, so a caller could learn whether the
resumed agent would have Mail only from the `/api/jump` response. Rows now
carry the `resume_mode` that `/api/jump` will use, beside the unchanged code.
"""
from __future__ import annotations

import json

import pytest

from dashboard import server
from test_claude_resume_mail import NAME, TOKEN, child, private, resume

KINDS = {
    "token": ("mail", None),
    "child": ("mail", None),
    "missing": ("conversation_only", "credential_absent"),
    "expired": ("conversation_only", "retention_expired"),
    "old_schema": ("conversation_only", "mail_schema_unsupported"),
}


def prepare(kind, runtime, registration, monkeypatch):
    if kind in {"child", "expired"}:
        child(runtime, registration)
    if kind == "expired":
        state_path = runtime / "child-agents" / f"{NAME}.json"
        state = json.loads(state_path.read_text())
        state["resume_expires_at"] = "2000-01-01T00:00:00Z"
        private(state_path, json.dumps(state))
    elif kind == "missing":
        (runtime / f"agent_token_{NAME}").unlink()
    elif kind == "old_schema":
        private(runtime / "child-agents" / f"{NAME}.json", json.dumps({
            "agent_name": NAME, "project_key": registration["project_key"], "registration_token": TOKEN}))
        monkeypatch.setattr(server, "_legacy_claude_owned_registration", lambda _name, value: value)
        monkeypatch.setattr(server, "_mcp_tool_parameters", lambda _tool: {"name", "registration_token"})


def row_for(category="gone"):
    row = {"name": NAME, "program": "claude-code", "running": False}
    row["resume_capability"] = server._resume_capability_for_row(NAME, "claude-code", category=category)
    row.update(server._claude_row_mail_fields(row, category=category))
    return row


@pytest.mark.parametrize("verified", [True, False], ids=["ready", "verification_required"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_row_forecast_matches_what_jump_does(resume, monkeypatch, kind, verified):
    runtime, registration, launches, calls = resume
    prepare(kind, runtime, registration, monkeypatch)
    cached = (True, server._transcript_path(NAME), None) if verified else (False, None, None)
    monkeypatch.setattr(server, "_cached_claude_transcript_path", lambda _name: cached)
    server._RESUME_CAPABILITY_CACHE.clear()
    mode, reason = KINDS[kind]
    row = row_for()
    # The capability code keeps its meaning; the forecast is a separate field.
    assert row["resume_capability"] == ("ready" if verified else "verification_required")
    assert row["resume_mode"] == mode, row
    if reason:
        assert (row["mail_status"], row["mail_reason"]) == ("unavailable", reason), row
    else:
        assert "mail_status" not in row and "mail_reason" not in row, row
    monkeypatch.setattr(server, "_has_session", lambda _name: False)
    result = server.do_jump(NAME, open_terminal=False)
    assert result["ok"] and result["action"] == "resumed", result
    assert result["resume_mode"] == row["resume_mode"]
    assert result.get("mail_reason") == row.get("mail_reason")
    assert bool(calls) is (mode == "mail")


@pytest.mark.parametrize("damage", ["mismatch", "permissions"])
def test_rows_that_cannot_resume_carry_no_forecast(resume, damage):
    runtime, registration, launches, calls = resume
    child(runtime, registration)
    token = runtime / f"agent_token_{NAME}"
    if damage == "mismatch":
        private(token, "another-owner")
    else:
        token.chmod(0o644)
    server._RESUME_CAPABILITY_CACHE.clear()
    row = row_for()
    assert row["resume_capability"] not in {"ready", "verification_required"}
    assert "resume_mode" not in row and "mail_status" not in row


def test_running_and_codex_rows_carry_no_forecast(resume):
    running = {"name": NAME, "program": "claude-code", "running": True, "resume_capability": "not_required"}
    assert "resume_mode" not in server._claude_row_mail_fields(running, category="agent", session_state={})
    codex = {"name": NAME, "program": "codex-cli", "running": False, "resume_capability": "ready"}
    assert server._claude_row_mail_fields(codex, category="gone") == {}

