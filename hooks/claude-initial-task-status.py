#!/usr/bin/env python3
"""Did a Claude child start the task it was launched with?

spawn_child.sh gives a Claude child its first prompt and then watches for the
answer, because a child can receive the prompt and still not act on it: on
2026-10-01 Sonnet 5 children outside the vault answered the launch prompt with
a question to the user instead of the task, and nothing told the parent.

The answer is read from Claude Code's own transcript, never from the screen and
never from what the model says about itself: the first turn after the launch
prompt either calls a tool (the child started) or ends with text alone
(declined, or asked someone to confirm). That holds for every model.

Usage:
  claude-initial-task-status.py PROJECTS_DIR SINCE_EPOCH PROMPT_HEAD

An empty PROJECTS_DIR means Claude Code's own: $CLAUDE_CONFIG_DIR/projects,
else ~/.claude/projects.

Prints one JSON object: {"status": "started" | "declined" | "pending",
"transcript": path or "", "text": the first turn's text (declined only)}.
Never raises: a transcript that cannot be read is "pending".
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# A turn ends with any of these; "tool_use" means it continues with a tool.
_TURN_ENDS = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
_TEXT_LIMIT = 2000
# Enough of the launch prompt's first line to name the child (it carries the
# child's name) without depending on the rest of the wording.
_HEAD_CHARS = 80
# Transcripts are read in full; a first turn is near the top of a small file.
_MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _rows(path: Path) -> list[dict]:
    try:
        if path.stat().st_size > _MAX_TRANSCRIPT_BYTES:
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a line still being written
        if isinstance(row, dict):
            rows.append(row)
    return rows


def classify(rows: list[dict], prompt_head: str) -> tuple[str, str] | None:
    """(status, text) for a transcript whose launch prompt matches, else None."""
    start = None
    for index, row in enumerate(rows):
        message = row.get("message")
        if row.get("type") == "user" and not row.get("isMeta") and isinstance(message, dict):
            if prompt_head in _content_text(message.get("content")):
                start = index
            break  # only the conversation's first user turn is the launch prompt
    if start is None:
        return None
    # Claude Code writes each block of a message as its own row, and every row
    # of the message carries the message's stop_reason (the thinking row of a
    # text-only answer already says end_turn). So read the whole turn, up to
    # the next user row, before deciding.
    texts: list[str] = []
    ended = False
    for row in rows[start + 1:]:
        message = row.get("message")
        if row.get("type") == "user" and not row.get("isMeta"):
            break
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in content
        ):
            return "started", ""
        texts.append(_content_text(content))
        ended = ended or message.get("stop_reason") in _TURN_ENDS
    if ended:
        text = "\n".join(part for part in texts if part).strip()
        return "declined", text[-_TEXT_LIMIT:]
    return "pending", ""


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: claude-initial-task-status.py PROJECTS_DIR SINCE_EPOCH PROMPT_HEAD", file=sys.stderr)
        return 2
    projects = Path(argv[1]) if argv[1] else (
        Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    )
    since_raw, prompt_head = argv[2], argv[3].strip()[:_HEAD_CHARS]
    result = {"status": "pending", "transcript": "", "text": ""}
    try:
        since = float(since_raw)
    except ValueError:
        since = 0.0
    if prompt_head.strip():
        try:
            candidates = [
                path for path in projects.glob("*/*.jsonl")
                if path.stat().st_mtime >= since - 5
            ]
        except OSError:
            candidates = []
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
            found = classify(_rows(path), prompt_head)
            if found is None:
                continue
            result = {"status": found[0], "transcript": str(path), "text": found[1]}
            break
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - a watcher must never crash the launcher
        print(json.dumps({"status": "pending", "transcript": "", "text": ""}))
        raise SystemExit(0)
