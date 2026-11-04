"""Exact legacy Claude transcript bindings from hook-written self receipts.

No database, tmux, process, content-name or cwd discovery takes place here.
The dashboard supplies a project-scoped registration and existing tmux time.
"""
from __future__ import annotations

import collections
from datetime import datetime
import json
import math
import os
import re
import stat


def _history_regular_json(path: str) -> dict | None:
    """Read bounded hook metadata, without following a final symlink or FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            raw = handle.read(65537)
        if len(raw) > 65536:
            return None
        record = json.loads(raw)
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


class ReceiptIndex:
    """One receipt snapshot per roster, including competing self claims.

    This is deliberately independent of the resume/selfref caches. Replacing
    an index on SessionStart must invalidate an exported binding immediately.
    """

    def __init__(self, directory: str):
        self.directory = directory
        self.records = {}
        self.owners = collections.defaultdict(set)
        try:
            entries = os.listdir(self.directory)
        except OSError:
            entries = []
        for entry in entries:
            if not re.fullmatch(r"[0-9]+\.json", entry):
                continue
            aid = int(entry[:-5])
            record = _history_regular_json(os.path.join(self.directory, entry))
            self.records[aid] = record
            if not record or record.get("schema_version") != 2 or record.get("binding_kind") != "self":
                continue
            if record.get("provider") not in (None, "claude") or record.get("program") not in (None, "claude", "claude-code"):
                continue
            name = record.get("agent_name")
            if not isinstance(name, str) or not name or record.get("registered_by") not in ("", name):
                continue
            sid = record.get("session_id")
            path = record.get("transcript_path")
            if isinstance(sid, str) and sid:
                self.owners[("session", sid)].add(aid)
            if isinstance(path, str) and os.path.isabs(path):
                try:
                    self.owners[("path", os.path.realpath(path))].add(aid)
                except (OSError, ValueError):
                    pass


_CLAUDE_BINDING_REASONS = {
    "invalid_binding": "Claude history has no valid project-scoped self-registration receipt.",
    "binding_missing": "Claude history has no SessionStart or self-registration receipt.",
    "ambiguous_binding": "More than one agent claims this Claude conversation.",
    "no_transcript": "The Claude receipt does not name a usable transcript in Claude projects.",
    "id_mismatch": "The Claude session ID, transcript filename and native metadata do not agree.",
    "receipt_predates_session": "This Claude receipt predates the current tmux session.",
}


def _claude_transcript_matches(path: str, session_id: str) -> bool:
    """Check only native top-level metadata in a finite JSONL prefix.

    Claude has no Codex-style session_meta header. Metadata records may precede
    its first user/assistant record; a missing ID or an oversized prefix fails
    closed. Neither message text nor tool arguments supply identity evidence.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return False
            remaining = 262144
            for _ in range(64):
                line = handle.readline(remaining + 1)
                remaining -= len(line)
                if remaining < 0 or not line.endswith(b"\n"):
                    return False
                try:
                    record = json.loads(line)
                except ValueError:
                    return False
                if not isinstance(record, dict):
                    return False
                if record.get("type") in ("user", "assistant") and "sessionId" in record:
                    return record["sessionId"] == session_id
        return False
    except OSError:
        return False


def history_binding(
    session: str, registration: dict | None, *, projects: str, index: ReceiptIndex,
    session_created: float | None = None,
) -> dict:
    """Export an exact legacy Claude receipt, never the heuristic resolver."""
    def unbound(reason):
        return {"history_binding": "unbound",
                "history_binding_reason": _CLAUDE_BINDING_REASONS[reason],
                "history_binding_reason_code": reason,
                "transcript_path": None, "history_session_id": None,
                "history_receipt_ts": None}

    if registration is None:
        return unbound("invalid_binding")
    aid = registration["agent_id"]
    if aid not in index.records:
        return unbound("binding_missing")
    record = index.records[aid]
    if not record or not (
        record.get("schema_version") == 2 and record.get("binding_kind") == "self"
        and type(record.get("agent_id")) is int and record["agent_id"] == aid
        and record.get("agent_name") == session
        and record.get("project_key") == registration["project_key"]
        and record.get("registered_by") in ("", session)
        and record.get("provider") in (None, "claude")
        and record.get("program") in (None, registration["program"])
    ):
        return unbound("invalid_binding")
    sid = record.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", sid):
        return unbound("invalid_binding")
    path = record.get("transcript_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return unbound("no_transcript")
    root = os.path.realpath(projects)
    try:
        real_path = os.path.realpath(path)
    except (OSError, ValueError):
        return unbound("no_transcript")
    # Only a top-level native transcript: not a subagent file, arbitrary path,
    # symlink alias, or a session inferred from the receipt's cwd.
    relative = os.path.relpath(real_path, root)
    if (real_path != os.path.abspath(path) or relative.startswith(".." + os.sep)
            or len(relative.split(os.sep)) != 2 or not os.path.isfile(real_path)):
        return unbound("no_transcript")
    if os.path.basename(real_path) != sid + ".jsonl":
        return unbound("id_mismatch")
    if (index.owners[("session", sid)] != {aid}
            or index.owners[("path", real_path)] != {aid}):
        return unbound("ambiguous_binding")
    if not _claude_transcript_matches(real_path, sid):
        return unbound("id_mismatch")
    # Do not publish a snapshot whose own receipt changed while we validated
    # the transcript (resume, /clear, failed rewrite, or explicit removal).
    if _history_regular_json(os.path.join(index.directory, f"{aid}.json")) != record:
        return unbound("invalid_binding")
    receipt_ts = record.get("ts")
    receipt_epoch = None
    try:
        # The legacy writer uses time.strftime: no offset, dashboard host time.
        # Preserve that value for display; do not label it as UTC.
        if not isinstance(receipt_ts, str) or len(receipt_ts) > 64:
            raise ValueError("no receipt timestamp")
        receipt_epoch = datetime.fromisoformat(receipt_ts).timestamp()
    except (ValueError, OverflowError, OSError):
        receipt_ts = None
    metadata = {"history_session_id": sid, "history_receipt_ts": receipt_ts}
    if (receipt_epoch is not None and type(session_created) in (int, float)
            and math.isfinite(session_created) and 0 < session_created
            and receipt_epoch < session_created):
        result = unbound("receipt_predates_session")
        result.update(metadata, history_binding="stale")
        return result
    return {"history_binding": "bound", "history_binding_reason": "",
            "history_binding_reason_code": "bound", "transcript_path": real_path,
            **metadata}
