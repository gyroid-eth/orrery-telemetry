"""hooks/codex-initial-task-status.py: the first task's start, read from the
exact rollout of this launch.

The chain is the launch expectation -> the SessionStart receipt for the same
launch_id / receipt_id / session -> the transcript that receipt names (its
session_meta header must match) -> a turn start and a user message whose text
is the canonical task, parsed as JSON. Anything else is `bound` (verified
binding, records not there yet) or `unknown`; neither is a failure.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "hooks" / "codex-initial-task-status.py"

TASK = "You are Child, a standalone agent with no parent. Start it immediately:\n\nreply STARTED"
SESSION = "01a0e943-80d1-7d43-a35a-6ab1a56d4d9e"
LAUNCH_ID = "6e5760b4d00c8a52b19002f95b5f583a"


def _line(kind: str, payload: dict) -> str:
    return json.dumps({"timestamp": "2026-09-28T18:25:10.351Z", "type": kind, "payload": payload})


META = _line("session_meta", {"id": SESSION, "cwd": "/home/example"})
TURN = _line("event_msg", {"type": "task_started", "turn_id": "1"})
USER_ITEM = _line("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": TASK}]})
USER_EVENT = _line("event_msg", {"type": "user_message", "message": TASK})


def _setup(tmp_path: pathlib.Path, lines: list[str], *, launch: dict | None = None,
           receipt: dict | None = None, newline_at_end: bool = True) -> pathlib.Path:
    runtime = tmp_path / "runtime"
    launches = runtime / "codex_launches"
    index = runtime / "session_index"
    launches.mkdir(parents=True)
    index.mkdir()
    transcript = tmp_path / "sessions" / f"rollout-{SESSION}.jsonl"
    transcript.parent.mkdir()
    body = "\n".join(lines)
    transcript.write_text(body + ("\n" if newline_at_end and lines else ""), encoding="utf-8")
    # Shaped like a real launch record (BriskGalileo, WSL 2026-09-28), claimed.
    record = {
        "schema_version": 1, "binding_expected": True, "history_mode": "enabled",
        "provider": "codex", "program": "codex-cli", "agent_id": 46,
        "agent_name": "Child", "project_key": "/p", "launch_id": LAUNCH_ID,
        "launch_kind": "startup", "launch_origin": "child", "codex_mcp_profile": "inherit",
        "binding_conflicted": False, "claimed_session_id": SESSION, "receipt_id": "r1",
    }
    record.update(launch or {})
    (launches / "46.json").write_text(json.dumps(record), encoding="utf-8")
    if receipt is not False:
        data = {
            "schema_version": 2, "binding_kind": "self", "provider": "codex", "program": "codex-cli",
            "agent_id": 46, "agent_name": "Child", "registered_by": "Child", "project_key": "/p",
            "launch_id": LAUNCH_ID, "receipt_id": "r1", "launch_kind": "startup",
            "launch_origin": "child", "codex_mcp_profile": "inherit", "session_id": SESSION,
            "transcript_path": str(transcript),
        }
        data.update(receipt or {})
        (index / "46.json").write_text(json.dumps(data), encoding="utf-8")
    return launches / "46.json"


def _status(launch_path: pathlib.Path, *, launch_id: str = LAUNCH_ID, name: str = "Child", task: str = TASK) -> str:
    result = subprocess.run(
        [sys.executable, str(HELPER), "--launch-path", str(launch_path),
         "--launch-id", launch_id, "--agent-name", name],
        input=task, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("user", [USER_ITEM, USER_EVENT], ids=["response_item", "legacy_event"])
def test_turn_and_canonical_user_message_in_the_exact_rollout_is_started(tmp_path, user):
    assert _status(_setup(tmp_path, [META, TURN, user])) == "started"


@pytest.mark.parametrize("lines", [
    [META],
    [META, TURN],
    [META, USER_ITEM],
    # Another task's user message is not this one.
    [META, TURN, _line("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "another task"}]})],
    # Event JSON quoted inside a tool output is data, not an event.
    [META, _line("response_item", {"type": "function_call_output",
                                   "output": json.dumps({"type": "task_started"}) + TASK})],
    # The assistant repeating the task is not the user's message.
    [META, TURN, _line("response_item", {"type": "message", "role": "assistant",
                                         "content": [{"type": "output_text", "text": TASK}]})],
], ids=["meta-only", "turn-only", "user-only", "other-task", "quoted-in-tool-output", "assistant-echo"])
def test_a_verified_binding_without_both_records_is_bound(tmp_path, lines):
    assert _status(_setup(tmp_path, lines)) == "bound"


def test_a_line_still_being_written_is_not_read(tmp_path):
    assert _status(_setup(tmp_path, [META, TURN, USER_ITEM], newline_at_end=False)) == "bound"


def test_a_malformed_line_does_not_hide_the_records_that_parse(tmp_path):
    assert _status(_setup(tmp_path, [META, "{not json", TURN, USER_ITEM])) == "started"


@pytest.mark.parametrize(("launch", "receipt"), [
    ({}, False),                                            # no receipt (binding not installed)
    ({"claimed_session_id": None, "receipt_id": None}, {}),  # expectation never claimed
    ({"binding_conflicted": True}, {}),
    ({"launch_id": "older-launch"}, {}),                    # stale generation
    ({}, {"launch_id": "older-launch"}),
    ({}, {"receipt_id": "r0"}),
    ({}, {"session_id": "another-session"}),
    ({}, {"agent_name": "OtherChild", "registered_by": "OtherChild"}),
    ({}, {"schema_version": 1}),
    ({"schema_version": 99}, {}),
    ({"history_mode": "disabled", "binding_expected": False}, {}),
    ({"binding_expected": False}, {}),
    ({"program": "other"}, {"program": "other"}),
    ({"project_key": None}, {"project_key": None}),
    ({"launch_kind": "resume"}, {"launch_kind": "resume"}),
    ({"codex_mcp_profile": "everything"}, {"codex_mcp_profile": "everything"}),
    ({}, {"codex_mcp_profile": "orrery-only"}),
], ids=["no-receipt", "unclaimed", "conflicted", "stale-launch", "stale-receipt", "receipt-id",
        "receipt-session", "other-agent", "schema", "launch-schema", "history-disabled",
        "binding-not-expected", "program", "project-missing", "resume", "profile-invalid",
        "profile-mismatch"])
def test_any_break_in_the_chain_is_unknown(tmp_path, launch, receipt):
    assert _status(_setup(tmp_path, [META, TURN, USER_ITEM], launch=launch, receipt=receipt)) == "unknown"


def test_a_header_for_another_session_is_unknown(tmp_path):
    other = _line("session_meta", {"id": "ffffffff-0000-0000-0000-000000000000"})
    assert _status(_setup(tmp_path, [other, TURN, USER_ITEM])) == "unknown"


def test_the_launcher_supplied_launch_id_and_name_must_match(tmp_path):
    launch = _setup(tmp_path, [META, TURN, USER_ITEM])
    assert _status(launch, launch_id="another-launch") == "unknown"
    assert _status(launch, name="OtherChild") == "unknown"


def test_a_transcript_elsewhere_in_the_child_home_is_never_consulted(tmp_path):
    # The receipt names an empty rollout; a completed rollout for the same
    # task sits next to it (a shared sessions/ symlink). Only the named one counts.
    launch = _setup(tmp_path, [META])
    sibling = tmp_path / "sessions" / "rollout-sibling.jsonl"
    sibling.write_text("\n".join([META, TURN, USER_ITEM]) + "\n", encoding="utf-8")
    assert _status(launch) == "bound"


def test_an_empty_task_is_unknown(tmp_path):
    assert _status(_setup(tmp_path, [META, TURN, USER_ITEM]), task="  ") == "unknown"
