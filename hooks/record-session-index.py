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


# Outcomes the caller acts on. mark-agent-registered.sh creates the session's
# registration flag only for EXIT_BOUND: a flag that outlives a refused or
# failed binding tells the guards "registered" while leaving them unable to say
# who this is.
EXIT_BOUND = 0
EXIT_NOT_APPLICABLE = 3
EXIT_DELEGATED = 4
EXIT_CALLER_UNRESOLVED = 5
EXIT_WRITE_FAILED = 6

# Sources resolve-agent-name.sh reports that identify one agent. Anything else
# (identity-conflict, placeholder-env, unconfirmed-metafile) is a caller whose
# identity was refused, not an absent one.
_SOURCES_THAT_MAY_BIND = {"none", "env", "tmux-session", "metafile+tmux-session", "session-index"}


def _bindings_for(out_dir, session_id):
    """Names already bound to this session by an authoritative record."""
    names = set()
    try:
        entries = os.listdir(out_dir)
    except OSError:
        return names
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        path = os.path.join(out_dir, entry)
        if os.path.islink(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("session_id") != session_id:
            continue
        if record.get("schema_version") != 2 or record.get("binding_kind") != "self":
            continue
        name = record.get("agent_name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def main():
    try:
        d = json.loads(sys.stdin.read())
    except Exception:
        return EXIT_NOT_APPLICABLE

    session_id = d.get("session_id") or ""
    transcript_path = d.get("transcript_path") or ""
    cwd = d.get("cwd") or ""

    resp = d.get("tool_response")
    if resp is None:
        resp = d.get("tool_result")
    agent_id = _extract_id(resp)
    tool_input = d.get("tool_input") or {}
    agent_name = _extract_name(resp) or tool_input.get("name", "")

    # Agent names are project-local, so a binding is only meaningful together
    # with the project it was made in. resolve-agent-name.sh refuses a record
    # that cannot show it belongs to the project being enforced.
    project_key = tool_input.get("project_key") or ""
    if not isinstance(project_key, str):
        project_key = ""

    # Who called register_agent, as mark-agent-registered.sh resolved it. Both
    # halves are required: the name alone cannot distinguish "nobody claims this
    # session" from "the claim was refused", and a refused claim must not be
    # read as an anonymous self-registration.
    registered_by = os.environ.get("AGENTSTACK_REGISTERING_AGENT", "")
    # Absent means the writer was invoked directly rather than through
    # mark-agent-registered.sh: an anonymous caller, which may claim only a
    # session no other identity has claimed.
    caller_source = os.environ.get("AGENTSTACK_REGISTERING_SOURCE", "") or "none"

    # Need at least an id (the unique key) and a session_id to be useful.
    if agent_id is None or not session_id:
        return EXIT_NOT_APPLICABLE

    if caller_source not in _SOURCES_THAT_MAY_BIND:
        # An unresolved, conflicting or unconfirmed caller cannot be shown to be
        # this agent. Adding a third name to a session that already has two is
        # how a conflict becomes permanent.
        return EXIT_CALLER_UNRESOLVED

    # A registration made on somebody else's behalf is not a binding for this
    # session, and writing it anyway leaves a record every reader has to know
    # to distrust -- the dashboard used one to show a parent's transcript on a
    # child's card. Not writing it is the version that cannot be misread.
    if registered_by and agent_name and registered_by != agent_name:
        return EXIT_DELEGATED
    if caller_source != "none" and registered_by != agent_name:
        return EXIT_DELEGATED

    runtime_dir = os.path.expanduser(
        os.environ.get("AGENTSTACK_RUNTIME_DIR", "~/.agentstack/runtime")
    )
    out_dir = os.path.join(runtime_dir, "session_index")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return EXIT_WRITE_FAILED

    # An anonymous caller may only claim a session nobody else has claimed.
    if caller_source == "none":
        for existing in _bindings_for(out_dir, session_id):
            if existing != agent_name:
                return EXIT_CALLER_UNRESOLVED

    record = {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "project_key": project_key,
        "registered_by": registered_by,
        # Readers that treat this file as authority check these two, so a record
        # written by an older version is ignored rather than half-trusted.
        "schema_version": 2,
        "binding_kind": "self",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = os.path.join(out_dir, f"{agent_id}.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        # Swallowing this used to be harmless -- the index was a convenience for
        # the dashboard. It is now the identity the guards check, so a failure
        # has to reach the caller instead of leaving a flag with no binding.
        return EXIT_WRITE_FAILED
    return EXIT_BOUND


if __name__ == "__main__":
    try:
        raise SystemExit(main() or EXIT_BOUND)
    except SystemExit:
        raise
    except Exception:
        # A crash is not a binding either.
        raise SystemExit(EXIT_WRITE_FAILED)
