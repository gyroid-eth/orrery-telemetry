#!/usr/bin/env python3
"""record-session-index.py

Called from mark-agent-registered.sh (PostToolUse on register_agent).

Records a precise, unambiguous mapping so the agent-dashboard can resume the
EXACT Claude session belonging to an agent-mail row — instead of guessing via
name self-reference scoring + an mtime activity window (which mis-fires when
`last_active_ts` is stuck at inception, or when a name is reused across
projects; see logs/ in the dashboard project).

Key = agent-mail `id` (global PRIMARY KEY → unique per session). For each
register_agent we write:

    ~/.agentstack/runtime/session_index/<agent_id>.json
        {agent_id, agent_name, session_id, transcript_path, cwd, ts}

Idempotent re-registration / resume keeps the same id and session_id, so we
simply overwrite (keeps the entry fresh). The dashboard reads this first and
only falls back to the heuristic for old sessions registered before this hook
existed.

Reads the PostToolUse hook payload (JSON) on stdin. Never raises — a failure
here must not disturb registration.
"""
import json
import os
import sys
import time


def _extract_id(v):
    """Pull agent-mail numeric id out of the register_agent tool_response,
    which may be a JSON string, a dict, or the MCP content-block wrapper."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return None
    if isinstance(v, dict):
        if isinstance(v.get("id"), int):
            return v["id"]
        content = v.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    got = _extract_id(blk.get("text", ""))
                    if got is not None:
                        return got
    return None


def _extract_name(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return ""
    if isinstance(v, dict):
        if v.get("name"):
            return v["name"]
        content = v.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    got = _extract_name(blk.get("text", ""))
                    if got:
                        return got
    return ""


def main():
    try:
        d = json.loads(sys.stdin.read())
    except Exception:
        return

    session_id = d.get("session_id") or ""
    transcript_path = d.get("transcript_path") or ""
    cwd = d.get("cwd") or ""

    resp = d.get("tool_response")
    if resp is None:
        resp = d.get("tool_result")
    agent_id = _extract_id(resp)
    agent_name = _extract_name(resp) or (d.get("tool_input") or {}).get("name", "")

    # Need at least an id (the unique key) and a session_id to be useful.
    if agent_id is None or not session_id:
        return

    runtime_dir = os.path.expanduser(
        os.environ.get("AGENTSTACK_RUNTIME_DIR", "~/.agentstack/runtime")
    )
    out_dir = os.path.join(runtime_dir, "session_index")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return

    record = {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = os.path.join(out_dir, f"{agent_id}.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
