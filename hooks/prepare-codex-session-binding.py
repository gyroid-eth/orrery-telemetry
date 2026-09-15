#!/usr/bin/env python3
"""Create the non-secret receipt expectation for one Codex CLI launch.

The caller must supply registration data returned by ORRERY Mail.  A fresh
``launch_id`` is generated while holding the same per-agent lock used by the
SessionStart recorder, so a callback from an older launch cannot become the
receipt for this one.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any


SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _registration(args: argparse.Namespace) -> dict[str, Any]:
    if args.registration_file:
        data = json.loads(Path(args.registration_file).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("registration file must contain an object")
        return data
    return {
        "agent_id": args.agent_id,
        "agent_name": args.agent_name,
        "project_key": args.project_key,
        "program": args.program,
    }


def prepare(
    runtime_dir: Path,
    registration: dict[str, Any],
    *,
    launch_kind: str,
    history_mode: str,
    now: float | None = None,
) -> tuple[Path, str]:
    agent_id = registration.get("agent_id")
    agent_name = registration.get("agent_name")
    project_key = registration.get("project_key")
    program = registration.get("program")
    if type(agent_id) is not int or agent_id <= 0:
        raise ValueError("registration has no positive numeric agent_id")
    if not isinstance(agent_name, str) or not SAFE_NAME.fullmatch(agent_name):
        raise ValueError("registration has no safe agent_name")
    if not isinstance(project_key, str) or not project_key:
        raise ValueError("registration has no project_key")
    if program not in {"codex", "codex-cli"}:
        raise ValueError("registration is not for Codex CLI")
    if launch_kind not in {"startup", "resume"}:
        raise ValueError("launch_kind must be startup or resume")
    if history_mode not in {"enabled", "disabled"}:
        raise ValueError("history_mode must be enabled or disabled")

    launches = runtime_dir / "codex_launches"
    launch_path = launches / f"{agent_id}.json"
    lock_path = launches / f"{agent_id}.lock"
    launch_id = secrets.token_hex(16)
    record = {
        "schema_version": 1,
        "binding_expected": history_mode == "enabled",
        "provider": "codex",
        "program": program,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "project_key": project_key,
        "launch_id": launch_id,
        "launch_kind": launch_kind,
        "history_mode": history_mode,
        "expected_at": time.time() if now is None else float(now),
        "claimed_session_id": None,
        "binding_conflicted": False,
        "receipt_id": None,
        "last_reason": None,
    }
    descriptor: int | None = None
    try:
        launches.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(launches, 0o700)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _atomic_json(launch_path, record)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return launch_path, launch_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--registration-file")
    parser.add_argument("--agent-id", type=int)
    parser.add_argument("--agent-name")
    parser.add_argument("--project-key")
    parser.add_argument("--program", default="codex")
    parser.add_argument("--launch-kind", choices=("startup", "resume"), required=True)
    parser.add_argument("--history-mode", choices=("enabled", "disabled"), default="enabled")
    args = parser.parse_args()
    try:
        launch_path, launch_id = prepare(
            Path(args.runtime_dir).expanduser(),
            _registration(args),
            launch_kind=args.launch_kind,
            history_mode=args.history_mode,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"prepare-codex-session-binding: {exc}", file=os.sys.stderr)
        return 1
    print(f"{launch_path}\t{launch_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
