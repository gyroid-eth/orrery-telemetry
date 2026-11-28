import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from dashboard import shell_guard_watch as watch

ROOT = Path(__file__).resolve().parents[1]


def row_for(program, command, when, call_id):
    if program == "claude":
        return {"timestamp": when, "type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "id": call_id, "input": {"command": command}}]}}
    if program == "codex":
        return {"timestamp": when, "type": "response_item", "payload": {
            "type": "function_call", "name": "new_native_shell", "namespace": "functions",
            "call_id": call_id, "arguments": json.dumps({"cmd": command})}}
    if program == "dsh":
        return {"time": int(when * 1000), "type": "tool/call", "data": {
            "name": "bash", "callId": call_id, "arguments": json.dumps({"command": command})}}
    return {"created_at": when, "step_index": 1, "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": command}}]}


@pytest.mark.parametrize("program", ["claude", "codex", "dsh", "hermes", "antigravity"])
def test_passive_shell_observation_matches_the_current_launch(tmp_path, monkeypatch, program):
    now = time.time()
    monkeypatch.setenv("AGENTSTACK_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_NAME", "FixtureChild")
    path = tmp_path / ("state.db" if program == "hermes" else "transcript.jsonl")
    binding = {"history_binding": "bound", "history_session_id": "test-session",
               "transcript_path": str(path)}
    command = "printf '%s' private-tool-arguments"
    if program == "hermes":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE messages (id INTEGER, session_id TEXT, timestamp REAL, tool_calls TEXT)")
            db.execute("INSERT INTO messages VALUES (?,?,?,?)", (
                1, "other-session", now - 25, json.dumps([{"id": "other", "function": {
                    "name": "terminal", "arguments": {"command": command}}}]),
            ))
            db.execute("INSERT INTO messages VALUES (?,?,?,?)", (
                2, "test-session", now - 25, json.dumps([{"id": "call-1", "function": {
                    "name": "terminal", "arguments": {"command": command}}}]),
            ))
    else:
        path.write_text(json.dumps(row_for(program, command, now - 25, "call-1")) + "\n")
    status = lambda: watch.receipt_status(tmp_path, "FixtureChild", program, binding,
                                          created=now - 100, now=now)
    assert status() == "missing"
    mark = watch.MARKS["record"]
    monkeypatch.setattr(mark.__globals__["time"], "time", lambda: now - 24)
    mark(command, {"session_id": "other-session", "tool_use_id": "call-1"})
    assert status() == "missing"
    mark(command, {"session_id": "test-session", "tool_use_id": "call-1"})
    assert status() == "observed"
    assert command not in watch.MARKS["journal"](tmp_path, "FixtureChild", "test-session").read_text()
    assert watch.receipt_status(tmp_path, "FixtureChild", program, binding,
                                created=now - 10, now=now) == "pending"


def test_malformed_or_unavailable_evidence_is_never_healthy(tmp_path):
    assert watch.receipt_status(tmp_path, "FixtureChild", "codex", {}, created=1, now=100) == "unverified"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps(row_for("codex", "printf new", 95, "new")) + "\n")
    binding = {"history_binding": "bound", "history_session_id": "test-session",
               "transcript_path": str(transcript)}
    assert watch.receipt_status(tmp_path, "FixtureChild", "codex", binding, created=1, now=100) == "pending"
    assert watch.receipt_status(tmp_path, "FixtureChild", "codex", binding, created=1, now=120) == "missing"


def test_missing_proof_notifies_once_across_restarts(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    now = time.time()
    transcript.write_text(json.dumps(row_for("codex", "printf real", now - 25, "call-1")) + "\n")
    binding = {"history_binding": "bound", "history_session_id": "test-session",
               "transcript_path": str(transcript)}
    rows = [{"name": "FixtureChild", "created": now - 30, "program": "codex", "binding": binding,
             "project_key": "fixture", "agent_id": 1, "generation": "launch-1"}]
    alerts = []
    watch.Monitor(tmp_path, alerts.append).tick(rows, now)
    monitor = watch.Monitor(tmp_path, alerts.append)
    monitor.tick(rows, now + 1)
    assert alerts == rows
    assert monitor.view("FixtureChild", "launch-1") == "missing"
    assert monitor.view("FixtureChild", "different") is None


def test_common_guard_does_not_dispatch_mcp_and_marks_supported_shell(tmp_path):
    guard = ROOT / "hooks/deny-llm-key-read.py"
    env = {**os.environ, "AGENTSTACK_RUNTIME_DIR": str(tmp_path), "AGENT_NAME": "FixtureChild"}
    env.pop("TMUX", None)
    def call(name, arguments):
        return subprocess.run([sys.executable, str(guard)], input=json.dumps({
            "session_id": "test-session", "tool_use_id": "call-1", "tool_name": name, "tool_input": arguments,
        }), env=env, capture_output=True, text=True)
    assert call("mcp__orrery_mail__send_message", {"cmd": "security find-generic-password -w"}).returncode == 0
    assert not (tmp_path / "shell-guard-marks").exists()
    assert call("future_native_shell", {"cmd": "printf harmless", "login": False}).returncode == 0
    assert watch.MARKS["journal"](tmp_path, "FixtureChild", "test-session").is_file()


def test_namespaced_mcp_arguments_are_not_shell_observations(tmp_path):
    path = tmp_path / "rollout.jsonl"
    event = row_for("codex", "credential-looking MCP field", 50, "mcp-call")
    event["payload"].update(namespace="mcp__orrery_fixture", name="fetch_inbox")
    path.write_text(json.dumps(event) + "\n")
    binding = {"history_binding": "bound", "history_session_id": "session", "transcript_path": str(path)}
    assert watch.receipt_status(tmp_path, "FixtureChild", "codex", binding,
                                created=1, now=100) == "pending"
