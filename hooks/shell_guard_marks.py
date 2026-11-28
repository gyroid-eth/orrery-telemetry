"""Shell guard evidence. Keep tool arguments and provider IDs out of the record."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def journal(runtime: Path, name: str, session: str) -> Path:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,127}", name) or not session or len(session) > 512:
        raise ValueError("invalid shell guard identity")
    return runtime / "shell-guard-marks" / name / (fingerprint(session) + ".jsonl")


def record(command, event, *, session=None):
    if not isinstance(command, str) or not command:
        return
    runtime = os.environ.get("AGENTSTACK_RUNTIME_DIR")
    name = os.environ.get("AGENT_NAME")
    session = session or event.get("session_id") or event.get("conversationId")
    if not runtime or not name or not isinstance(session, str):
        return
    call = event.get("tool_use_id")
    try:
        path = journal(Path(runtime), name, session)
        for directory in (path.parent.parent, path.parent):
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return
        entry = [time.time(), fingerprint(call) if isinstance(call, str) and call else None, fingerprint(command)]
        data = json.dumps(entry, separators=(",", ":")).encode() + b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
                os.write(fd, data)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        # A missing receipt remains visible in the independent transcript audit.
        return
