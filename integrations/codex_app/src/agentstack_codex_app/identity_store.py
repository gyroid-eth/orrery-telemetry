"""Durable, token-safe Codex App identity bindings.

Public binding metadata and owner credentials are stored in separate 0600
files. Filenames are hashes of external IDs, so hook-controlled identifiers
cannot escape the runtime directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "external_id",
        "surface",
        "session_id",
        "agent_id",
        "parent_external_id",
        "agent_name",
        "project_key",
        "program",
        "created_at",
        "last_seen_at",
    }
)
IMMUTABLE_BINDING_FIELDS = BINDING_FIELDS - {"last_seen_at"}


class IdentityStoreError(ValueError):
    """Raised when a binding is invalid or attempts to change identity."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def external_id_for(session_id: str, agent_id: str | None = None) -> str:
    """Build the canonical root or subagent external ID."""

    if not isinstance(session_id, str) or not session_id or ":" in session_id:
        raise IdentityStoreError("session_id must be a non-empty colon-free string")
    if agent_id is None:
        return f"codex:{session_id}"
    if not isinstance(agent_id, str) or not agent_id or ":" in agent_id:
        raise IdentityStoreError("agent_id must be a non-empty colon-free string")
    return f"codex:{session_id}:sub:{agent_id}"


def build_binding(
    *,
    session_id: str,
    agent_id: str | None,
    agent_name: str,
    project_key: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Construct a binding-record-v1 mapping for a root or subagent."""

    timestamp = now or utc_now()
    external_id = external_id_for(session_id, agent_id)
    return {
        "schema_version": 1,
        "external_id": external_id,
        "surface": "codex-app",
        "session_id": session_id,
        "agent_id": agent_id,
        "parent_external_id": (
            external_id_for(session_id) if agent_id is not None else None
        ),
        "agent_name": agent_name,
        "project_key": project_key,
        "program": "codex-app",
        "created_at": timestamp,
        "last_seen_at": timestamp,
    }


def validate_binding(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the security-relevant binding-record-v1 invariants."""

    if set(record) != BINDING_FIELDS:
        missing = sorted(BINDING_FIELDS - set(record))
        extra = sorted(set(record) - BINDING_FIELDS)
        raise IdentityStoreError(f"binding fields mismatch: missing={missing}, extra={extra}")
    normalized = dict(record)
    if normalized["schema_version"] != 1:
        raise IdentityStoreError("unsupported binding schema_version")
    if normalized["surface"] != "codex-app" or normalized["program"] != "codex-app":
        raise IdentityStoreError("binding surface and program must be codex-app")
    expected_external_id = external_id_for(
        normalized["session_id"], normalized["agent_id"]
    )
    if normalized["external_id"] != expected_external_id:
        raise IdentityStoreError("external_id does not match session_id/agent_id")
    expected_parent = (
        external_id_for(normalized["session_id"])
        if normalized["agent_id"] is not None
        else None
    )
    if normalized["parent_external_id"] != expected_parent:
        raise IdentityStoreError("parent_external_id does not match binding identity")
    agent_name = normalized["agent_name"]
    if not isinstance(agent_name, str) or re.fullmatch(
        r"[A-Za-z][A-Za-z0-9-]*", agent_name
    ) is None:
        raise IdentityStoreError("agent_name has an invalid format")
    project_key = normalized["project_key"]
    if not isinstance(project_key, str) or not Path(project_key).is_absolute():
        raise IdentityStoreError("project_key must be an absolute path")
    for field in ("created_at", "last_seen_at"):
        value = normalized[field]
        if not isinstance(value, str):
            raise IdentityStoreError(f"{field} must be a date-time string")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IdentityStoreError(f"invalid {field}") from exc
    return normalized


class IdentityStore:
    """Persist immutable bindings and separately protected owner tokens."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser()
        self.bindings_dir = self.root / "bindings"
        self.secrets_dir = self.root / "secrets"
        _ensure_private_dir(self.root)
        _ensure_private_dir(self.bindings_dir)
        _ensure_private_dir(self.secrets_dir)

    def resolve(self, external_id: str) -> dict[str, Any] | None:
        """Return a binding copy or ``None`` when it has not been created."""

        path = self._binding_path(external_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise IdentityStoreError(f"unable to read binding {external_id}") from exc
        if not isinstance(raw, dict):
            raise IdentityStoreError("binding file must contain an object")
        record = validate_binding(raw)
        if record["external_id"] != external_id:
            raise IdentityStoreError("binding filename/content mismatch")
        return record

    def resolve_event(
        self, session_id: str, agent_id: str | None = None
    ) -> dict[str, Any] | None:
        return self.resolve(external_id_for(session_id, agent_id))

    def save(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Create or refresh a binding without allowing identity reassignment."""

        candidate = validate_binding(record)
        current = self.resolve(candidate["external_id"])
        if current is not None:
            changed = {
                field
                for field in IMMUTABLE_BINDING_FIELDS
                if current[field] != candidate[field]
            }
            if changed:
                raise IdentityStoreError(
                    f"refusing to change immutable binding fields: {sorted(changed)}"
                )
            if candidate["last_seen_at"] < current["last_seen_at"]:
                candidate["last_seen_at"] = current["last_seen_at"]
        _atomic_write(
            self._binding_path(candidate["external_id"]),
            json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n",
        )
        return candidate

    def touch(self, external_id: str, *, now: str | None = None) -> dict[str, Any]:
        """Refresh ``last_seen_at`` while preserving all identity fields."""

        record = self.resolve(external_id)
        if record is None:
            raise IdentityStoreError(f"unknown external_id: {external_id}")
        record["last_seen_at"] = now or utc_now()
        return self.save(record)

    def store_owner_token(self, external_id: str, token: str) -> None:
        """Write an owner token separately from public binding metadata."""

        if self.resolve(external_id) is None:
            raise IdentityStoreError("cannot store a token without a binding")
        if not isinstance(token, str) or not token:
            raise IdentityStoreError("owner token must be non-empty")
        _atomic_write(self._secret_path(external_id), token)

    def load_owner_token(self, external_id: str) -> str | None:
        """Read an owner token without exposing it through binding APIs."""

        try:
            token = self._secret_path(external_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise IdentityStoreError("unable to read owner token") from exc
        return token or None

    def _binding_path(self, external_id: str) -> Path:
        return self.bindings_dir / f"{_key(external_id)}.json"

    def _secret_path(self, external_id: str) -> Path:
        return self.secrets_dir / f"{_key(external_id)}.token"


def _key(external_id: str) -> str:
    if not isinstance(external_id, str) or not external_id:
        raise IdentityStoreError("external_id must be non-empty")
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _atomic_write(path: Path, content: str) -> None:
    _ensure_private_dir(path.parent)
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
