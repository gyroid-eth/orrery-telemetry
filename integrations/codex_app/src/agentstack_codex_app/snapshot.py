"""Sanitized, atomic runtime snapshots for the dashboard provider."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SNAPSHOT_FIELDS = frozenset({"schema_version", "generated_at", "runtimes"})
RUNTIME_FIELDS = frozenset(
    {
        "external_id",
        "surface",
        "session_id",
        "agent_id",
        "parent_external_id",
        "agent_name",
        "project_key",
        "program",
        "model",
        "cwd",
        "state",
        "last_seen_at",
        "capabilities",
    }
)
ALLOWED_STATES = frozenset(
    {"registering", "working", "waiting", "blocked", "dormant", "degraded"}
)


class SnapshotError(ValueError):
    """Raised when a snapshot contains non-allowlisted or invalid data."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_record(
    binding: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    state: str,
    last_seen_at: str | None = None,
) -> dict[str, Any]:
    """Build an allowlisted dashboard record from binding and hook metadata."""

    if state not in ALLOWED_STATES:
        raise SnapshotError(f"invalid runtime state: {state}")
    record = {
        "external_id": binding["external_id"],
        "surface": "codex-app",
        "session_id": binding["session_id"],
        "agent_id": binding["agent_id"],
        "parent_external_id": binding["parent_external_id"],
        "agent_name": binding["agent_name"],
        "project_key": binding["project_key"],
        "program": "codex-app",
        "model": event.get("model"),
        "cwd": event.get("cwd"),
        "state": state,
        "last_seen_at": last_seen_at or binding["last_seen_at"],
        "capabilities": ["open"],
    }
    return validate_runtime(record)


def validate_runtime(record: Mapping[str, Any]) -> dict[str, Any]:
    if set(record) != RUNTIME_FIELDS:
        raise SnapshotError("runtime snapshot fields do not match the allowlist")
    normalized = dict(record)
    if normalized["surface"] != "codex-app" or normalized["program"] != "codex-app":
        raise SnapshotError("runtime surface/program must be codex-app")
    if normalized["state"] not in ALLOWED_STATES:
        raise SnapshotError("invalid runtime state")
    capabilities = normalized["capabilities"]
    if capabilities != ["open"]:
        raise SnapshotError("P1 Codex App runtimes may advertise only open")
    for key in ("external_id", "session_id", "agent_name", "project_key", "last_seen_at"):
        if not isinstance(normalized[key], str) or not normalized[key]:
            raise SnapshotError(f"runtime field {key} must be non-empty")
    if not Path(normalized["project_key"]).is_absolute():
        raise SnapshotError("runtime project_key must be absolute")
    if normalized["cwd"] is not None and not isinstance(normalized["cwd"], str):
        raise SnapshotError("runtime cwd must be a string or null")
    if normalized["model"] is not None and not isinstance(normalized["model"], str):
        raise SnapshotError("runtime model must be a string or null")
    return normalized


def read_snapshot(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and validate a snapshot; a missing file is an empty inventory."""

    snapshot_path = Path(path).expanduser()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "generated_at": None, "runtimes": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError("unable to read runtime snapshot") from exc
    if not isinstance(payload, dict) or set(payload) != SNAPSHOT_FIELDS:
        raise SnapshotError("snapshot envelope fields are invalid")
    if payload["schema_version"] != 1 or not isinstance(payload["runtimes"], list):
        raise SnapshotError("unsupported runtime snapshot")
    payload["runtimes"] = [validate_runtime(item) for item in payload["runtimes"]]
    return payload


def write_snapshot(
    path: str | os.PathLike[str], runtimes: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Atomically write an allowlisted snapshot with mode 0600."""

    snapshot_path = Path(path).expanduser()
    records = sorted(
        (validate_runtime(record) for record in runtimes),
        key=lambda item: item["external_id"],
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "runtimes": records,
    }
    _atomic_write(
        snapshot_path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return payload


class SnapshotStore:
    """Read-modify-write helper for a single daemon-owned snapshot file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()

    def get(self, external_id: str) -> dict[str, Any] | None:
        current = read_snapshot(self.path)
        for item in current.get("runtimes", []):
            if item["external_id"] == external_id:
                return item
        return None

    def upsert(self, record: Mapping[str, Any]) -> dict[str, Any]:
        current = read_snapshot(self.path)
        runtimes = {
            item["external_id"]: item for item in current.get("runtimes", [])
        }
        validated = validate_runtime(record)
        runtimes[validated["external_id"]] = validated
        return write_snapshot(self.path, list(runtimes.values()))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
