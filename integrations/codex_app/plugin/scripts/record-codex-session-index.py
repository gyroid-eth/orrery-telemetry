#!/usr/bin/env python3
"""Fail-open SessionStart recorder for product-launched Codex CLI sessions.

The hook payload's ``session_id`` is the only source of the runtime identity.
Launcher registration and launch identity come from the dedicated binding file
and launch-id environment installed immediately before exec.  Ambient
``AGENT_NAME`` and ``CODEX_*`` values are deliberately unused.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MAX_PAYLOAD = 256 * 1024
SUPPORTED_SOURCES = frozenset({"startup", "resume", "compact", "clear"})


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


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


def _header_session_id(path: Path) -> str | None:
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode):
            return None
        with resolved.open("rb") as handle:
            raw = handle.readline(MAX_PAYLOAD + 1)
        if len(raw) > MAX_PAYLOAD:
            return None
        envelope = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(envelope, dict) or envelope.get("type") != "session_meta":
        return None
    metadata_payload = envelope.get("payload")
    if not isinstance(metadata_payload, dict):
        return None
    observed = metadata_payload.get("id") or metadata_payload.get("session_id")
    return observed if isinstance(observed, str) and observed else None


def _valid_launch(record: Mapping[str, Any], launch_id: str) -> bool:
    claimed_session_id = record.get("claimed_session_id")
    receipt_id = record.get("receipt_id")
    return (
        record.get("schema_version") == 1
        and record.get("binding_expected") is True
        and record.get("provider") == "codex"
        and record.get("program") in {"codex", "codex-cli"}
        and type(record.get("agent_id")) is int
        and record.get("agent_id", 0) > 0
        and isinstance(record.get("agent_name"), str)
        and bool(record.get("agent_name"))
        and isinstance(record.get("project_key"), str)
        and bool(record.get("project_key"))
        and record.get("launch_id") == launch_id
        and record.get("launch_kind") in {"startup", "resume"}
        and record.get("history_mode") == "enabled"
        and type(record.get("binding_conflicted")) is bool
        and (
            claimed_session_id is None
            or (isinstance(claimed_session_id, str) and bool(claimed_session_id))
        )
        and (
            receipt_id is None
            or (isinstance(receipt_id, str) and bool(receipt_id))
        )
    )


def _mark_reason(launch_path: Path, launch: dict[str, Any], reason: str) -> None:
    updated = dict(launch)
    updated["last_reason"] = reason
    _atomic_json(launch_path, updated)


def _invalidate_current(index_path: Path) -> None:
    """Best-effort removal; receipt nonce validation is the hard boundary."""

    try:
        index_path.unlink()
    except OSError:
        pass


def _poison_binding(launch_path: Path, index_path: Path | None = None) -> None:
    """Remove authority before data when a state transition cannot commit."""

    for path in (launch_path, index_path):
        if path is None:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _index_path_for_launch_path(launch_path: Path) -> Path | None:
    if launch_path.parent.name != "codex_launches":
        return None
    try:
        agent_id = int(launch_path.stem)
    except ValueError:
        return None
    if agent_id <= 0:
        return None
    return launch_path.parent.parent / "session_index" / f"{agent_id}.json"


def _transition_launch(
    launch_path: Path,
    launch: dict[str, Any],
    *,
    reason: str,
    claimed_session_id: str | None = None,
    conflict: bool = False,
) -> dict[str, Any]:
    """Invalidate every earlier receipt and optionally claim the first ID."""

    updated = dict(launch)
    if updated.get("claimed_session_id") is None and claimed_session_id is not None:
        updated["claimed_session_id"] = claimed_session_id
    if conflict:
        updated["binding_conflicted"] = True
    updated["receipt_id"] = secrets.token_hex(16)
    updated["last_reason"] = reason
    _atomic_json(launch_path, updated)
    return updated


def record_payload(
    payload: Mapping[str, Any],
    *,
    launch_path: Path,
    launch_id: str,
) -> str:
    """Record one payload, returning a short observable outcome code."""

    # Built-in subagents share the root session id.  They are not separate CLI
    # processes and must never claim the root's launch binding.
    if payload.get("hook_event_name") != "SessionStart" or payload.get("agent_id") not in (None, ""):
        return "ignored"

    agent_id_hint = launch_path.stem
    lock_path = launch_path.with_suffix(".lock")
    index_path = _index_path_for_launch_path(launch_path)
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            launch = _load_object(launch_path)
        except (OSError, ValueError, json.JSONDecodeError):
            _poison_binding(launch_path, index_path)
            return "invalid_binding"
        if not _valid_launch(launch, launch_id):
            return "stale_launch"
        if str(launch["agent_id"]) != agent_id_hint:
            return "invalid_binding"

        # The expectation path itself is launcher-scoped. Derive its runtime
        # root instead of trusting AGENTSTACK_RUNTIME_DIR: login-shell startup
        # files may replace that ambient variable before Codex runs a hook.
        runtime_dir = launch_path.parent.parent
        if launch_path.parent.name != "codex_launches":
            return "invalid_binding"
        index_path = runtime_dir / "session_index" / f"{launch['agent_id']}.json"

        def reject(
            reason: str, claim: str | None = None, *, conflict: bool = False
        ) -> str:
            try:
                _transition_launch(
                    launch_path,
                    launch,
                    reason=reason,
                    claimed_session_id=claim,
                    conflict=conflict,
                )
            except OSError:
                _poison_binding(launch_path, index_path)
                return "write_failed"
            _invalidate_current(index_path)
            return reason

        source = payload.get("source")
        if source not in SUPPORTED_SOURCES:
            return reject("unsupported_source")

        session_id = payload.get("session_id")
        transcript = payload.get("transcript_path")
        if launch.get("binding_conflicted") is True:
            return reject("id_mismatch", conflict=True)
        if not isinstance(session_id, str) or not session_id:
            return reject("id_mismatch")

        # The first runtime ID presented by this launch is a one-way claim.
        # A second CLI can inherit the launch pair, and `/clear` may present a
        # new ID, but neither may turn a rejected ID into authority later.
        claimed_session_id = launch.get("claimed_session_id")
        if claimed_session_id is not None and claimed_session_id != session_id:
            return reject("id_mismatch", conflict=True)
        if not isinstance(transcript, str) or not transcript:
            return reject("no_transcript", session_id)
        transcript_path = Path(transcript).expanduser()
        try:
            real_transcript_path = transcript_path.resolve(strict=True)
        except OSError:
            return reject("no_transcript", session_id)
        if _header_session_id(real_transcript_path) != session_id:
            return reject("id_mismatch", session_id)

        # Commit a fresh nonce into launch authority before writing its receipt.
        # If the receipt write fails, an old index cannot validate against it.
        try:
            transitioned = _transition_launch(
                launch_path,
                launch,
                reason="write_failed",
                claimed_session_id=session_id,
            )
        except OSError:
            _poison_binding(launch_path, index_path)
            return "write_failed"

        receipt = {
            "schema_version": 2,
            "binding_kind": "self",
            "provider": "codex",
            "program": launch["program"],
            "agent_id": launch["agent_id"],
            "agent_name": launch["agent_name"],
            "project_key": launch["project_key"],
            "registered_by": launch["agent_name"],
            "launch_id": launch_id,
            "receipt_id": transitioned["receipt_id"],
            "launch_kind": launch["launch_kind"],
            "session_id": session_id,
            # A child-specific CODEX_HOME links sessions to the shared Codex
            # home, then normal cleanup removes that child home.  Persist the
            # regular file instead of the disposable symlink route.
            "transcript_path": str(real_transcript_path),
            "source": source,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _atomic_json(index_path, receipt)
        except OSError:
            return "write_failed"
        try:
            _mark_reason(launch_path, transitioned, "bound")
        except OSError:
            # The index is the sole success receipt. Updating the diagnostic
            # hint is secondary and must not turn a committed receipt into an
            # apparent recorder failure.
            pass
        return "bound"
    except (OSError, ValueError, json.JSONDecodeError):
        _poison_binding(launch_path, index_path)
        return "invalid_binding"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def main() -> int:
    """Read a hook payload and always fail open for the Codex process."""

    launch_value = os.environ.get("AGENTSTACK_CODEX_LAUNCH_BINDING", "").strip()
    launch_id = os.environ.get("AGENTSTACK_CODEX_LAUNCH_ID", "").strip()
    if not launch_value or not launch_id:
        return 0
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
        if len(raw) > MAX_PAYLOAD:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        result = record_payload(
            payload,
            launch_path=Path(launch_value).expanduser(),
            launch_id=launch_id,
        )
        if result not in {"bound", "ignored"}:
            print(f"record-codex-session-index: {result}", file=sys.stderr)
    except (OSError, ValueError, json.JSONDecodeError):
        print("record-codex-session-index: invalid_payload", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
