"""Dashboard provider for sanitized Codex App Bridge snapshots."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .base import ActionResult, RuntimeProvider, RuntimeSnapshot


OpenAdapter = Callable[[], Mapping[str, Any]]
_RUNTIME_FIELDS = frozenset(
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
        "delivery",
    }
)
_DELIVERY_FIELDS = frozenset(
    {
        "pending_count",
        "wake_status",
        "failed_count",
        "dead_letter_count",
        "last_error",
        "parent_external_id",
    }
)


class CodexAppRuntimeProvider(RuntimeProvider):
    """Read Bridge telemetry and expose the safe App activation action."""

    provider_name = "codex-app"

    def __init__(
        self,
        snapshot_path: str | os.PathLike[str] | None = None,
        *,
        open_adapter: OpenAdapter | None = None,
    ) -> None:
        self.snapshot_path = (
            Path(snapshot_path).expanduser()
            if snapshot_path is not None
            else _default_snapshot_path()
        )
        self._open_adapter = open_adapter or _open_chatgpt

    def list_runtimes(self) -> list[RuntimeSnapshot]:
        runtimes = _read_runtimes(self.snapshot_path)
        snapshots: list[RuntimeSnapshot] = []
        for record in runtimes:
            capabilities = frozenset(record.get("capabilities") or [])
            snapshots.append(
                RuntimeSnapshot(
                    external_id=record["external_id"],
                    provider=self.provider_name,
                    present=True,
                    state=record["state"],
                    live="",
                    capabilities=capabilities,
                    metadata=record,
                )
            )
        return snapshots

    def perform(self, external_id: str, action: str) -> ActionResult:
        if action != "open":
            return ActionResult(
                ok=False,
                external_id=external_id,
                action=action,
                error=f"unsupported Codex App action: {action}",
            )
        known = {item.external_id for item in self.list_runtimes()}
        if external_id not in known:
            return ActionResult(
                ok=False,
                external_id=external_id,
                action=action,
                error="unknown Codex App runtime",
            )
        try:
            details = dict(self._open_adapter())
        except Exception as exc:  # provider boundary: keep dashboard alive
            return ActionResult(
                ok=False,
                external_id=external_id,
                action=action,
                error=str(exc),
            )
        return ActionResult(
            ok=bool(details.get("ok")),
            external_id=external_id,
            action=action,
            error=str(details.get("error") or ""),
            details=details,
        )


def _default_snapshot_path() -> Path:
    explicit = os.environ.get("AGENTSTACK_CODEX_APP_SNAPSHOT", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    runtime = os.environ.get("AGENTSTACK_CODEX_APP_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime).expanduser() / "snapshot.json"
    base = os.environ.get("AGENTSTACK_RUNTIME_DIR", "").strip()
    if base:
        return Path(base).expanduser() / "codex-app" / "snapshot.json"
    return Path("~/.agentstack/runtime/codex-app/snapshot.json").expanduser()


def _read_runtimes(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return []
    runtimes = payload.get("runtimes")
    if not isinstance(runtimes, list):
        return []
    safe: list[dict[str, Any]] = []
    for record in runtimes:
        if not isinstance(record, dict) or set(record) != _RUNTIME_FIELDS:
            continue
        if (
            isinstance(record.get("external_id"), str)
            and record.get("surface") == "codex-app"
            and record.get("program") == "codex-app"
            and isinstance(record.get("state"), str)
            and record.get("capabilities") == ["open"]
            and _valid_delivery(record.get("delivery"))
        ):
            safe.append(dict(record))
    return safe


def _valid_delivery(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _DELIVERY_FIELDS:
        return False
    if value.get("wake_status") not in {
        "idle",
        "pending",
        "waking",
        "wake_failed",
        "blocked",
        "dead_letter",
        "identity_auth_required",
    }:
        return False
    for field in ("pending_count", "failed_count", "dead_letter_count"):
        count = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
    error = value.get("last_error")
    valid_error = error is None or (
        isinstance(error, str)
        and 0 < len(error) <= 64
        and all(
            character.isascii()
            and (character.isalnum() or character in "_-")
            for character in error
        )
    )
    parent = value.get("parent_external_id")
    valid_parent = parent is None or (
        isinstance(parent, str) and parent.startswith("codex:")
    )
    return valid_error and valid_parent


def _open_chatgpt() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"ok": False, "error": "Codex App activation is supported on macOS"}
    process = subprocess.run(
        ["open", "-a", "ChatGPT"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if process.returncode != 0:
        return {
            "ok": False,
            "error": (process.stderr or process.stdout or "App activation failed").strip(),
        }
    return {"ok": True, "adapter": "macos-app-activate"}
