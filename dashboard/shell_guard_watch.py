"""Check bound tool histories against independent guard evidence."""
from __future__ import annotations

from collections import deque
from contextlib import closing
from functools import lru_cache
from datetime import datetime
import json
import os
from pathlib import Path
import re
import runpy
import sqlite3
import stat
import subprocess

MARKS = runpy.run_path(str(Path(__file__).resolve().parents[1] / "hooks/shell_guard_marks.py"))
PROGRAMS = {
    "claude": "claude", "claude-code": "claude",
    "codex": "codex", "codex-cli": "codex",
    "dsh": "dsh", "hermes": "hermes", "antigravity": "antigravity",
}
MAX_READ = 4 * 1024 * 1024
GRACE = 20
SHELL_TOOLS = {"bash", "exec_command", "shell", "shell_command", "terminal", "pwsh"}


def _timestamp(value):
    if type(value) in (int, float):
        return float(value) / 1000 if value > 10**11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _call(ts, name, args, identity=None, *, field=None, codex_shape=False):
    if not isinstance(name, str) or name.lower().startswith(("mcp__", "mcp.")) or ts is None:
        return None
    args = _object(args)
    if name.lower() not in SHELL_TOOLS | {"run_command"} and not (
        codex_shape and (isinstance(args.get("cmd"), str) or (
            isinstance(args.get("command"), str) and any(
                key in args for key in ("login", "sandbox_permissions", "yield_time_ms", "max_output_tokens")
            )
        ))
    ):
        return None
    command = args.get(field) if field else args.get("command", args.get("cmd"))
    if isinstance(command, str) and command:
        return (ts, identity if isinstance(identity, str) else None, MARKS["fingerprint"](command))
    return None


def _calls(program, row):
    ts = _timestamp(row.get("timestamp") or row.get("time") or row.get("created_at"))
    if program == "claude" and row.get("type") == "assistant":
        for block in _items(_object(row.get("message")).get("content")):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield _call(ts, block.get("name"), block.get("input"), block.get("id"))
    elif program == "codex" and row.get("type") == "response_item":
        value = _object(row.get("payload"))
        namespace = value.get("namespace")
        mcp = isinstance(namespace, str) and namespace.startswith(("mcp__", "mcp."))
        if value.get("type") == "function_call" and not mcp:
            yield _call(ts, value.get("name"), value.get("arguments"), value.get("call_id"), codex_shape=True)
    elif program == "dsh" and row.get("type") == "tool/call":
        value = _object(row.get("data"))
        yield _call(ts, value.get("name"), value.get("arguments"), value.get("callId"))
    elif program == "antigravity" and row.get("type") == "PLANNER_RESPONSE":
        for index, block in enumerate(_items(row.get("tool_calls"))):
            if isinstance(block, dict):
                identity = f"{row['step_index']}:{index}" if "step_index" in row else None
                yield _call(ts, block.get("name"), block.get("args"), identity, field="CommandLine")


def _valid_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unreadable record")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _jsonl(path):
    with _valid_file(path) as source:
        if path.suffix == ".zstd":
            with subprocess.Popen(["zstd", "-dc"], stdin=source, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL) as proc:
                try:
                    data = proc.stdout.read(MAX_READ + 1)
                    if len(data) > MAX_READ or proc.wait(timeout=2):
                        raise ValueError("compressed shell history unavailable")
                finally:
                    if proc.poll() is None:
                        proc.kill()
        else:
            size = source.seek(0, os.SEEK_END)
            start = max(0, size - MAX_READ)
            source.seek(start)
            if start:
                source.readline(MAX_READ)
            data = source.read(MAX_READ)
    # Ignore a line still being written by the provider.
    for line in data.split(b"\n")[:-1]:
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _hermes_calls(path, session, since):
    with _valid_file(path) as source, closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: 1, 100_000)
        rows = db.execute(
            "SELECT timestamp, tool_calls FROM messages WHERE session_id=? AND id IS NOT NULL "
            "AND tool_calls IS NOT NULL AND timestamp>=? ORDER BY id DESC LIMIT 128", (session, since),
        )
        for ts, raw in rows:
            if not isinstance(raw, str) or len(raw) > MAX_READ:
                continue
            for block in _items(json.loads(raw)):
                if isinstance(block, dict):
                    function = _object(block.get("function"))
                    yield _call(_timestamp(ts), function.get("name"), function.get("arguments"), block.get("id"))
        with _valid_file(path) as after:
            if not os.path.samestat(os.fstat(source.fileno()), os.fstat(after.fileno())):
                raise ValueError("Hermes history replaced during observation")


@lru_cache(maxsize=128)
def _jsonl_calls(path, program, revision):
    calls = deque((call for obj in _jsonl(path) if isinstance(obj, dict)
                   for call in _calls(program, obj) if call is not None), maxlen=512)
    return tuple(calls)


def _observed(path, program, session, since):
    if program == "hermes":
        return _hermes_calls(path, session, since)
    info = path.lstat()
    return _jsonl_calls(path, program, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))


def receipt_status(runtime, name, program, binding, *, created, now):
    """Return pending, observed, missing or unverified for one live launch."""
    program = PROGRAMS.get(program)
    if not program or not created or binding.get("history_binding") != "bound":
        return "unverified"
    session = binding.get("history_session_id")
    path = binding.get("transcript_path")
    if not isinstance(session, str) or not isinstance(path, str) or not Path(path).is_absolute():
        return "unverified"
    try:
        calls = list(dict.fromkeys(call for call in _observed(Path(path), program, session, created)
                                   if call and created <= call[0] <= now))
        if not calls:
            return "pending"
        try:
            markers = [row for row in _jsonl(MARKS["journal"](Path(runtime), name, session))
                       if isinstance(row, list) and len(row) == 3 and type(row[0]) in (int, float)
                       and created <= row[0] <= now and row[2] and isinstance(row[2], str)]
        except FileNotFoundError:
            markers = []
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        return "unverified"
    pending = False
    for ts, identity, command in sorted(calls, key=lambda row: row[0]):
        call_id = MARKS["fingerprint"](identity) if identity else None
        match = next((i for i, mark in enumerate(markers)
                      if call_id and mark[1] == call_id and mark[2] == command), None)
        if match is None:
            # Some native adapters expose no shared call ID. Use each nearby
            # command mark at most once, including repeated identical commands.
            match = next((i for i, mark in enumerate(markers)
                          if mark[2] == command and abs(mark[0] - ts) <= GRACE), None)
        if match is not None:
            markers.pop(match)
        elif ts <= now - GRACE:
            return "missing"
        else:
            pending = True

    return "pending" if pending else "observed"


class Monitor:
    """Publish launch-scoped roster status and claim each missing-proof alert once."""

    def __init__(self, runtime, send):
        self.runtime = Path(runtime)
        self.path = self.runtime / "shell-guard-alerts.json"
        self.send = send
        self.views = {}

    def tick(self, samples, now):
        try:
            from .provider_stalls import locked_events, save_events
        except ImportError:
            from provider_stalls import locked_events, save_events

        statuses = {}
        missing = {}
        claims = []
        for row in samples:
            key = (row["name"], row["generation"])
            state = receipt_status(self.runtime, row["name"], row["program"], row["binding"],
                                   created=row["created"], now=now)
            statuses[key] = state
            if state == "missing":
                missing[json.dumps([row["project_key"], row["agent_id"], row["generation"]])] = row
        self.views = statuses
        if not missing:
            return
        with locked_events(self.path) as (events, previous):
            for key, row in missing.items():
                if key not in events:
                    events[key] = {"kind": "shell_guard_missing", "since": now, "name": row["name"],
                                   "program": row["program"], "notification_status": "delivery_unknown"}
                    claims.append((key, row))
            # Preserve dedupe across ordinary monitor restarts with a bounded journal.
            for key in sorted(events, key=lambda key: events[key]["since"])[:-2048]:
                del events[key]
            save_events(self.path, events, previous)
        outcomes = {}
        for key, row in claims:
            try:
                result = self.send(row)
            except Exception:
                result = "delivery_unknown"
            if result in {"sent", "failed", "parent_unverified", "delivery_unknown"}:
                outcomes[key] = result
        if outcomes:
            with locked_events(self.path) as (events, previous):
                for key, result in outcomes.items():
                    if key in events:
                        events[key]["notification_status"] = result
                save_events(self.path, events, previous)

    def view(self, name, generation):
        return self.views.get((name, generation))
