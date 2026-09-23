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
SAFE_SESSION_ID = re.compile(r"^[0-9A-Fa-f-]{8,}$")
MAX_RECEIPT_BYTES = 256 * 1024


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


def _transcript_header_session_id(path: Path) -> str | None:
    if not path.is_absolute() or path.suffix != ".jsonl":
        return None
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return None
        with resolved.open("rb") as handle:
            raw = handle.readline(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            return None
        envelope = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(envelope, dict) or envelope.get("type") != "session_meta":
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("id") or payload.get("session_id")
    return value if isinstance(value, str) and value else None


def _resume_fallback(
    runtime_dir: Path,
    registration: dict[str, Any],
    resume_session_id: str,
    launch_origin: str | None,
    codex_mcp_profile: str | None,
) -> tuple[str, str] | None:
    """Return the exact prior receipt nonces for this resume target.

    This runs under the agent launch lock.  Merely finding an old index is not
    enough: its registered identity, requested session id, and rollout header
    must all agree before a new expectation may preserve it as a fallback.
    """

    path = runtime_dir / "session_index" / f"{registration['agent_id']}.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
        if len(raw) > MAX_RECEIPT_BYTES:
            return None
        receipt = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None
    provenance_matches = (
        launch_origin is None
        and receipt.get("launch_origin") is None
        and receipt.get("codex_mcp_profile") is None
    ) or (
        launch_origin == "standalone"
        and receipt.get("launch_origin") == "standalone"
        and receipt.get("codex_mcp_profile") is None
    ) or (
        launch_origin == "child"
        and receipt.get("launch_origin") == "child"
        and receipt.get("codex_mcp_profile") == codex_mcp_profile
    )
    identity_matches = (
        receipt.get("schema_version") == 2
        and receipt.get("binding_kind") == "self"
        and receipt.get("provider") == "codex"
        and receipt.get("program") == registration["program"]
        and type(receipt.get("agent_id")) is int
        and receipt["agent_id"] == registration["agent_id"]
        and receipt.get("agent_name") == registration["agent_name"]
        and receipt.get("project_key") == registration["project_key"]
        and receipt.get("registered_by") == registration["agent_name"]
        and receipt.get("session_id") == resume_session_id
        and provenance_matches
    )
    launch_id = receipt.get("launch_id")
    receipt_id = receipt.get("receipt_id")
    transcript_path = receipt.get("transcript_path")
    if not (
        identity_matches
        and isinstance(launch_id, str)
        and bool(launch_id)
        and isinstance(receipt_id, str)
        and bool(receipt_id)
        and isinstance(transcript_path, str)
        and _transcript_header_session_id(Path(transcript_path).expanduser())
        == resume_session_id
    ):
        return None
    return launch_id, receipt_id


def prepare(
    runtime_dir: Path,
    registration: dict[str, Any],
    *,
    launch_kind: str,
    history_mode: str,
    launch_origin: str | None = None,
    codex_mcp_profile: str | None = None,
    resume_session_id: str | None = None,
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
    if launch_kind == "resume":
        if not isinstance(resume_session_id, str) or not SAFE_SESSION_ID.fullmatch(
            resume_session_id
        ):
            raise ValueError("resume launch requires a valid resume_session_id")
    elif resume_session_id is not None:
        raise ValueError("resume_session_id is only valid for a resume launch")
    if history_mode not in {"enabled", "disabled"}:
        raise ValueError("history_mode must be enabled or disabled")
    if launch_origin not in {None, "child", "standalone"}:
        raise ValueError("launch_origin must be child or standalone when supplied")
    if launch_origin == "child":
        if not isinstance(codex_mcp_profile, str) or codex_mcp_profile not in {
            "inherit",
            "orrery-only",
        }:
            raise ValueError(
                "child launch requires codex_mcp_profile inherit or orrery-only"
            )
    elif codex_mcp_profile is not None:
        raise ValueError("codex_mcp_profile requires launch_origin child")

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
    if launch_origin is not None:
        record["launch_origin"] = launch_origin
    if launch_origin == "child":
        record["codex_mcp_profile"] = codex_mcp_profile
    if launch_kind == "resume":
        record["resume_session_id"] = resume_session_id
    descriptor: int | None = None
    try:
        launches.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(launches, 0o700)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if launch_kind == "resume":
            fallback = _resume_fallback(
                runtime_dir,
                registration,
                resume_session_id,
                launch_origin,
                codex_mcp_profile,
            )
            if fallback is None:
                raise ValueError(
                    "resume target does not match a verified prior receipt"
                )
            record["fallback_launch_id"], record["fallback_receipt_id"] = fallback
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
    parser.add_argument("--launch-origin", choices=("child", "standalone"))
    parser.add_argument("--codex-mcp-profile", choices=("inherit", "orrery-only"))
    parser.add_argument("--resume-session-id")
    args = parser.parse_args()
    try:
        launch_path, launch_id = prepare(
            Path(args.runtime_dir).expanduser(),
            _registration(args),
            launch_kind=args.launch_kind,
            history_mode=args.history_mode,
            launch_origin=args.launch_origin,
            codex_mcp_profile=args.codex_mcp_profile,
            resume_session_id=args.resume_session_id,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"prepare-codex-session-binding: {exc}", file=os.sys.stderr)
        return 1
    print(f"{launch_path}\t{launch_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
