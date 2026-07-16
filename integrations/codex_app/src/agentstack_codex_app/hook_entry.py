"""Fast, fail-open Codex lifecycle hook entry point.

The hook forwards only versioned, allowlisted lifecycle metadata to the local
Bridge socket. If the Bridge is unavailable, the same sanitized event is
appended to a private spool. Prompt text and tool input/output never cross this
boundary.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Mapping


EVENT_NAMES = frozenset(
    {
        "SessionStart",
        "SubagentStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "SubagentStop",
    }
)
EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "agent_id",
        "cwd",
        "model",
        "hook_event_name",
        "turn_id",
    }
)
MAX_EVENT_BYTES = 64 * 1024


class HookEventError(ValueError):
    """Raised for an unsupported or malformed hook payload."""


def runtime_dir_from_env(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve writable state without depending on plugin-only variables."""

    env = os.environ if environ is None else environ
    explicit = (env.get("AGENTSTACK_CODEX_APP_RUNTIME_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = (env.get("AGENTSTACK_RUNTIME_DIR") or "").strip()
    if base:
        return Path(base).expanduser() / "codex-app"
    return Path("~/.agentstack/runtime/codex-app").expanduser()


def socket_path_from_env(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = (env.get("AGENTSTACK_CODEX_APP_SOCKET") or "").strip()
    return Path(explicit).expanduser() if explicit else runtime_dir_from_env(env) / "bridge.sock"


def spool_path_from_env(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = (env.get("AGENTSTACK_CODEX_APP_SPOOL") or "").strip()
    return Path(explicit).expanduser() if explicit else runtime_dir_from_env(env) / "hook-events.jsonl"


def normalize_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop all non-schema hook fields and validate identity metadata."""

    event = {
        "schema_version": 1,
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id"),
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "hook_event_name": payload.get("hook_event_name"),
        "turn_id": payload.get("turn_id"),
    }
    return validate_event(event)


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate runtime-event-v1 without importing optional dependencies."""

    if set(event) != EVENT_FIELDS or event.get("schema_version") != 1:
        raise HookEventError("invalid runtime event envelope")
    normalized = dict(event)
    _require_text(normalized, "session_id", 1024)
    _require_text(normalized, "cwd", 4096)
    if not Path(normalized["cwd"]).is_absolute():
        raise HookEventError("cwd must be absolute")
    for field in ("agent_id", "model", "turn_id"):
        if normalized[field] is not None:
            _require_text(normalized, field, 1024)
    hook_name = normalized["hook_event_name"]
    if hook_name not in EVENT_NAMES:
        raise HookEventError("unsupported hook event")
    if hook_name in {"SubagentStart", "SubagentStop"} and normalized["agent_id"] is None:
        raise HookEventError("subagent event requires agent_id")
    encoded = json.dumps(normalized, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise HookEventError("runtime event is too large")
    return normalized


def forward_event(
    event: Mapping[str, Any],
    socket_path: str | os.PathLike[str],
    *,
    timeout: float = 0.15,
) -> bool:
    """Forward one event and return whether the Bridge accepted it."""

    payload = json.dumps(validate_event(event), separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(os.fspath(socket_path))
            connection.sendall(payload)
            response = _recv_line(connection, MAX_EVENT_BYTES)
    except (OSError, TimeoutError):
        return False
    try:
        decoded = json.loads(response)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(decoded, dict) and decoded.get("ok") is True


def append_spool(event: Mapping[str, Any], path: str | os.PathLike[str]) -> None:
    """Atomically append one sanitized event to a private JSONL spool."""

    payload = json.dumps(validate_event(event), separators=(",", ":")) + "\n"
    spool_path = Path(path).expanduser()
    spool_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(spool_path.parent, 0o700)
    descriptor = os.open(
        spool_path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload.encode("utf-8"))
    finally:
        os.close(descriptor)


def handle_payload(
    payload: Mapping[str, Any],
    *,
    socket_path: str | os.PathLike[str] | None = None,
    spool_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Sanitize and deliver a hook payload, spooling on Bridge failure."""

    event = normalize_event(payload)
    target_socket = Path(socket_path) if socket_path is not None else socket_path_from_env()
    if forward_event(event, target_socket):
        return True
    target_spool = Path(spool_path) if spool_path is not None else spool_path_from_env()
    append_spool(event, target_spool)
    return False


def main() -> int:
    """Read one hook payload from stdin and fail open for the Codex task."""

    try:
        raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        handle_payload(payload)
    except (HookEventError, OSError, json.JSONDecodeError):
        # Lifecycle telemetry must never prevent the user's Codex turn.
        return 0
    return 0


def _recv_line(connection: socket.socket, limit: int) -> str:
    chunks = bytearray()
    while len(chunks) <= limit:
        block = connection.recv(min(4096, limit + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if b"\n" in block:
            break
    if len(chunks) > limit:
        raise OSError("Bridge response too large")
    return bytes(chunks).split(b"\n", 1)[0].decode("utf-8")


def _require_text(payload: Mapping[str, Any], field: str, maximum: int) -> None:
    value = payload.get(field)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise HookEventError(f"{field} must be a bounded non-empty string")


if __name__ == "__main__":
    raise SystemExit(main())
