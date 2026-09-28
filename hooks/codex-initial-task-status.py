#!/usr/bin/env python3
"""Has this Codex launch started its first task? Answer from its own rollout.

The launcher passes a cold Codex child its first task as the positional
[PROMPT]. Whether that task entered a turn is read here from the exact rollout
of this launch, never from the screen, where the task or an answer can quote
any dialog text, and never from a scan of the child's CODEX_HOME, whose
sessions/ and history.jsonl may be symlinks shared with other children.

The chain is: this launch's expectation (codex_launches/<agent_id>.json, with
the launch_id the launcher holds) -> the SessionStart receipt it produced
(session_index/<agent_id>.json, same launch_id, receipt_id and claimed
session) -> the transcript that receipt names, whose session_meta header must
carry that session id. Only that file is read, as JSON.

Prints one word on stdout:
  started  the rollout holds a turn start and a user message whose text is the
           canonical task (read from stdin)
  bound    the receipt and transcript are verified, but not both records yet
  unknown  anything else: no receipt, a stale or conflicting launch, a
           mismatched header, an unreadable file, or a shape this does not know

`unknown` and `bound` are not evidence that the task has not started: Codex
queues rollout writes and can continue after a write failure. The caller only
records a diagnostic for them; it never resends the task.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

MAX_RECORD_BYTES = 256 * 1024
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
TURN_STARTED = {"task_started", "turn_started"}
PROGRAMS = {"codex", "codex-cli"}


def _load_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_RECORD_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _verified_binding(launch_path: Path, launch_id: str, agent_name: str) -> tuple[dict, dict] | None:
    """The launch expectation and its receipt, when both belong to this launch."""
    if launch_path.parent.name != "codex_launches" or launch_path.suffix != ".json":
        return None
    launch = _load_object(launch_path)
    if launch is None:
        return None
    agent_id = launch.get("agent_id")
    origin = launch.get("launch_origin")
    profile = launch.get("codex_mcp_profile")
    # The same launch contract the SessionStart recorder accepts
    # (record-codex-session-index.py _valid_launch), narrowed to a claimed,
    # unconflicted cold start; anything it would not bind is unknown here.
    if not (
        launch.get("schema_version") == 1
        and launch.get("binding_expected") is True
        and launch.get("history_mode") == "enabled"
        and launch.get("provider") == "codex"
        and launch.get("program") in PROGRAMS
        and launch.get("launch_id") == launch_id
        and launch.get("launch_kind") == "startup"
        and launch.get("agent_name") == agent_name
        and type(agent_id) is int
        and agent_id > 0
        and launch_path.stem == str(agent_id)
        and _nonempty_str(launch.get("project_key"))
        and launch.get("binding_conflicted") is False
        and _nonempty_str(launch.get("claimed_session_id"))
        and _nonempty_str(launch.get("receipt_id"))
        and (
            (origin in (None, "standalone") and profile is None)
            or (origin == "child" and profile in {"inherit", "orrery-only"})
        )
    ):
        return None
    receipt = _load_object(launch_path.parent.parent / "session_index" / f"{agent_id}.json")
    if receipt is None:
        return None
    if not (
        receipt.get("schema_version") == 2
        and receipt.get("binding_kind") == "self"
        and receipt.get("provider") == "codex"
        and receipt.get("program") == launch.get("program")
        and type(receipt.get("agent_id")) is int
        and receipt["agent_id"] == agent_id
        and receipt.get("agent_name") == agent_name
        and receipt.get("registered_by") == agent_name
        and receipt.get("project_key") == launch.get("project_key")
        and receipt.get("launch_id") == launch_id
        and receipt.get("receipt_id") == launch.get("receipt_id")
        and receipt.get("session_id") == launch.get("claimed_session_id")
        and receipt.get("launch_kind") == "startup"
        and receipt.get("launch_origin") == origin
        and receipt.get("codex_mcp_profile") == profile
    ):
        return None
    return launch, receipt


def _complete_lines(path: Path) -> list[bytes] | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_TRANSCRIPT_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raw = raw[:MAX_TRANSCRIPT_BYTES]
    lines = raw.split(b"\n")
    # A line still being appended has no newline yet; wait for it.
    return lines[:-1]


def _user_texts(envelope: dict[str, Any]) -> list[str]:
    """User-authored text carried by one rollout line, in the shapes known here."""
    kind = envelope.get("type")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return []
    if kind == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        return [message] if isinstance(message, str) else []
    if kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        content = payload.get("content")
        if not isinstance(content, list):
            return []
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and part.get("type") == "input_text"
        ]
        if parts and all(isinstance(text, str) for text in parts):
            return ["".join(parts)]
    return []


def task_status(launch_path: Path, launch_id: str, agent_name: str, task: str) -> str:
    binding = _verified_binding(launch_path, launch_id, agent_name)
    if binding is None:
        return "unknown"
    launch, receipt = binding
    transcript = receipt.get("transcript_path")
    if not (isinstance(transcript, str) and os.path.isabs(transcript) and transcript.endswith(".jsonl")):
        return "unknown"
    lines = _complete_lines(Path(transcript))
    if not lines:
        return "unknown"
    try:
        header = json.loads(lines[0])
    except (ValueError, UnicodeDecodeError):
        return "unknown"
    meta = header.get("payload") if isinstance(header, dict) else None
    if not (
        isinstance(header, dict)
        and header.get("type") == "session_meta"
        and isinstance(meta, dict)
        and (meta.get("id") or meta.get("session_id")) == receipt.get("session_id")
    ):
        return "unknown"

    turn_started = False
    task_recorded = False
    wanted = task.strip()
    for raw in lines[1:]:
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            # A malformed line is not evidence either way; the records that
            # parse still count.
            continue
        if not isinstance(envelope, dict):
            continue
        payload = envelope.get("payload")
        if (
            envelope.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") in TURN_STARTED
        ):
            turn_started = True
        if any(text.strip() == wanted for text in _user_texts(envelope)):
            task_recorded = True
        if turn_started and task_recorded:
            break

    # The launch must still be the same generation after the read.
    if _verified_binding(launch_path, launch_id, agent_name) != binding:
        return "unknown"
    return "started" if turn_started and task_recorded else "bound"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-path", required=True)
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--agent-name", required=True)
    args = parser.parse_args()
    task = sys.stdin.read()
    if not task.strip():
        print("unknown")
        return 0
    try:
        print(task_status(Path(args.launch_path), args.launch_id, args.agent_name, task))
    except Exception:  # never let a diagnostic helper break the launcher
        print("unknown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
