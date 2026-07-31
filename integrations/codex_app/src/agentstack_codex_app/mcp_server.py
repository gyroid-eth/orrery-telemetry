"""Narrow, session-bound MCP proxy surface for Codex App agents.

This module deliberately implements a small stdio JSON-RPC server instead of a
generic agent-mail passthrough. The first successful ``bootstrap`` fixes the
process to one Bridge-observed external ID. Project, agent name, and owner token
are then resolved from the private identity store for every allowlisted call.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agentstack_codex_app.agent_mail_client import (  # type: ignore
        AgentMailClient,
        AgentMailError,
        HttpJsonRpcTransport,
    )
    from agentstack_codex_app.hook_entry import runtime_dir_from_env  # type: ignore
    from agentstack_codex_app.identity_store import (  # type: ignore
        IdentityStore,
        external_id_for,
    )
    from agentstack_codex_app.snapshot import SnapshotStore  # type: ignore
else:
    from .agent_mail_client import (
        AgentMailClient,
        AgentMailError,
        HttpJsonRpcTransport,
    )
    from .hook_entry import runtime_dir_from_env
    from .identity_store import IdentityStore, external_id_for
    from .snapshot import SnapshotStore


SERVER_NAME = "agentstack"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_AGENT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,127}")
_IMPORTANCE = frozenset({"low", "normal", "high", "urgent"})


class ProxyError(RuntimeError):
    """Raised when a proxy request violates the session or tool boundary."""


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    runtime_dir: Path
    endpoint: str
    bearer_token: str | None = None
    bootstrap_wait_seconds: float = 1.0
    # Direct binding (no Codex App Bridge). A launcher that already knows which
    # agent this process serves names it here; this is what gives a
    # tmux-spawned Claude Code child an authenticated connection of its own
    # without the child ever seeing its token.
    agent_name: str | None = None
    project_key: str | None = None
    token_file: Path | None = None
    program: str = "claude-code"

    @property
    def is_direct(self) -> bool:
        return bool(self.agent_name and self.project_key)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProxyConfig":
        env = os.environ if environ is None else environ
        endpoint = (env.get("AGENTSTACK_MCP_URL") or "").strip()
        if not endpoint:
            raise ValueError("AGENTSTACK_MCP_URL must be configured")
        direct_agent = (env.get("AGENTSTACK_PROXY_AGENT_NAME") or "").strip() or None
        direct_project = (env.get("AGENTSTACK_PROJECT_KEY") or "").strip() or None
        token_file_value = (env.get("AGENTSTACK_PROXY_TOKEN_FILE") or "").strip()
        direct_token_file = (
            Path(token_file_value).expanduser() if token_file_value else None
        )
        direct_program = (env.get("AGENTSTACK_PROXY_PROGRAM") or "claude-code").strip()
        if direct_agent and not direct_project:
            raise ValueError(
                "AGENTSTACK_PROXY_AGENT_NAME requires AGENTSTACK_PROJECT_KEY"
            )
        wait_value = (env.get("AGENTSTACK_CODEX_APP_BOOTSTRAP_WAIT") or "1").strip()
        try:
            wait_seconds = float(wait_value)
        except ValueError as exc:
            raise ValueError(
                "AGENTSTACK_CODEX_APP_BOOTSTRAP_WAIT must be numeric"
            ) from exc
        if not 0 <= wait_seconds <= 5:
            raise ValueError(
                "AGENTSTACK_CODEX_APP_BOOTSTRAP_WAIT must be between 0 and 5"
            )
        return cls(
            runtime_dir=runtime_dir_from_env(env),
            endpoint=endpoint,
            bearer_token=(env.get("MCP_AGENT_MAIL_TOKEN") or None),
            bootstrap_wait_seconds=wait_seconds,
            agent_name=direct_agent,
            project_key=direct_project,
            token_file=direct_token_file,
            program=direct_program,
        )


class AgentStackProxy:
    """One-process/one-binding implementation of the P2 tool allowlist."""

    def __init__(
        self,
        identities: IdentityStore,
        snapshots: SnapshotStore,
        agent_mail: AgentMailClient,
        *,
        bootstrap_wait_seconds: float = 1.0,
        poll_interval: float = 0.05,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.identities = identities
        self.snapshots = snapshots
        self.agent_mail = agent_mail
        self.bootstrap_wait_seconds = bootstrap_wait_seconds
        self.poll_interval = poll_interval
        self.sleeper = sleeper
        self._binding: dict[str, Any] | None = None
        self._owner_token: str | None = None

    def bootstrap(
        self, session_id: str, agent_id: str | None = None
    ) -> dict[str, Any]:
        """Fix this MCP process to one Bridge-observed root or child binding."""

        external_id = external_id_for(session_id, agent_id)
        if self._binding is not None:
            if self._binding["external_id"] != external_id:
                raise ProxyError("MCP process is already bound to another runtime")
            return self.runtime_status(session_id, agent_id)

        deadline = time.monotonic() + self.bootstrap_wait_seconds
        binding = self.identities.resolve(external_id)
        while binding is None and time.monotonic() < deadline:
            self.sleeper(
                min(self.poll_interval, max(0.0, deadline - time.monotonic()))
            )
            binding = self.identities.resolve(external_id)
        if binding is None:
            raise ProxyError("Bridge has not observed this runtime identity")

        if agent_id is not None:
            parent = self.identities.resolve(binding["parent_external_id"])
            if parent is None or parent["session_id"] != session_id:
                raise ProxyError("Bridge has not observed the parent lineage")

        owner_token = self.identities.load_owner_token(external_id)
        if owner_token is None:
            raise ProxyError("identity_auth_required")
        self._binding = binding
        self._owner_token = owner_token
        return self.runtime_status(session_id, agent_id)

    def fetch_inbox(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        limit: int = 20,
        urgent_only: bool = False,
        include_bodies: bool = False,
        since_ts: str | None = None,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        binding, owner_token = self._resolve(session_id, agent_id)
        if not _is_integer(limit) or not 1 <= limit <= 100:
            raise ProxyError("limit must be between 1 and 100")
        _optional_text(since_ts, "since_ts", 128)
        _optional_text(topic, "topic", 64)
        return self.agent_mail.fetch_inbox(
            project_key=binding["project_key"],
            agent_name=binding["agent_name"],
            registration_token=owner_token,
            limit=limit,
            urgent_only=_boolean(urgent_only, "urgent_only"),
            include_bodies=_boolean(include_bodies, "include_bodies"),
            since_ts=since_ts,
            topic=topic,
        )

    def send_message(
        self,
        session_id: str,
        *,
        to: list[str],
        subject: str,
        body_md: str,
        agent_id: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        importance: str = "normal",
        ack_required: bool = False,
        thread_id: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        binding, owner_token = self._resolve(session_id, agent_id)
        recipients = _agent_names(to, "to", required=True)
        carbon_copy = _agent_names(cc, "cc")
        blind_copy = _agent_names(bcc, "bcc")
        _required_text(subject, "subject", 500)
        _required_text(body_md, "body_md", 100_000)
        _optional_text(thread_id, "thread_id", 128)
        _optional_text(topic, "topic", 64)
        if importance not in _IMPORTANCE:
            raise ProxyError("importance must be low, normal, high, or urgent")
        return self.agent_mail.send_message(
            project_key=binding["project_key"],
            agent_name=binding["agent_name"],
            registration_token=owner_token,
            to=recipients,
            subject=subject,
            body_md=body_md,
            cc=carbon_copy,
            bcc=blind_copy,
            importance=importance,
            ack_required=_boolean(ack_required, "ack_required"),
            thread_id=thread_id,
            topic=topic,
        )

    def acknowledge_message(
        self,
        session_id: str,
        message_id: int,
        *,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        binding, owner_token = self._resolve(session_id, agent_id)
        _positive_ids([message_id], "message_id")
        return self.agent_mail.acknowledge_message(
            project_key=binding["project_key"],
            agent_name=binding["agent_name"],
            registration_token=owner_token,
            message_id=message_id,
        )

    def reserve_files(
        self,
        session_id: str,
        paths: list[str],
        *,
        agent_id: str | None = None,
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        binding, owner_token = self._resolve(session_id, agent_id)
        safe_paths = _reservation_paths(paths, required=True)
        if not _is_integer(ttl_seconds) or not 60 <= ttl_seconds <= 86_400:
            raise ProxyError("ttl_seconds must be between 60 and 86400")
        _optional_text(reason, "reason", 500)
        return self.agent_mail.reserve_files(
            project_key=binding["project_key"],
            agent_name=binding["agent_name"],
            registration_token=owner_token,
            paths=safe_paths,
            ttl_seconds=ttl_seconds,
            exclusive=_boolean(exclusive, "exclusive"),
            reason=reason,
        )

    def renew_reservations(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        extend_seconds: int = 1800,
        paths: list[str] | None = None,
        file_reservation_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        binding, owner_token = self._resolve(session_id, agent_id)
        if not _is_integer(extend_seconds) or not 60 <= extend_seconds <= 86_400:
            raise ProxyError("extend_seconds must be between 60 and 86400")
        safe_paths = _reservation_paths(paths)
        ids = _positive_ids(file_reservation_ids, "file_reservation_ids")
        return self.agent_mail.renew_reservations(
            project_key=binding["project_key"],
            agent_name=binding["agent_name"],
            registration_token=owner_token,
            extend_seconds=extend_seconds,
            paths=safe_paths or None,
            file_reservation_ids=ids or None,
        )

    def release_reservations(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        paths: list[str] | None = None,
        file_reservation_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        binding, owner_token = self._resolve(session_id, agent_id)
        safe_paths = _reservation_paths(paths)
        ids = _positive_ids(file_reservation_ids, "file_reservation_ids")
        return self.agent_mail.release_reservations(
            project_key=binding["project_key"],
            agent_name=binding["agent_name"],
            registration_token=owner_token,
            paths=safe_paths or None,
            file_reservation_ids=ids or None,
        )

    def runtime_status(
        self, session_id: str, agent_id: str | None = None
    ) -> dict[str, Any]:
        binding, _ = self._resolve(session_id, agent_id)
        snapshot = self.snapshots.get(binding["external_id"])
        lineage = {
            "kind": "subagent" if binding["agent_id"] is not None else "root",
            "root_external_id": external_id_for(binding["session_id"]),
            "parent_external_id": binding["parent_external_id"],
        }
        return {
            "external_id": binding["external_id"],
            "surface": binding["surface"],
            "session_id": binding["session_id"],
            "agent_id": binding["agent_id"],
            "parent_external_id": binding["parent_external_id"],
            "agent_name": binding["agent_name"],
            "project_key": binding["project_key"],
            "program": binding["program"],
            "state": snapshot["state"] if snapshot is not None else "registering",
            "last_seen_at": (
                snapshot["last_seen_at"]
                if snapshot is not None
                else binding["last_seen_at"]
            ),
            "lineage": lineage,
        }

    def bind_direct(
        self,
        *,
        agent_name: str,
        project_key: str,
        owner_token: str,
        program: str = "claude-code",
    ) -> dict[str, Any]:
        """Bind this process to an agent the launcher already registered.

        The Codex App path discovers its binding through the Bridge daemon's
        identity store. A tmux-spawned child has no Bridge, but its parent
        pre-registered it and holds its owner token, so the launcher can state
        the binding outright. The token stays in this process; the agent on the
        other end of stdio never sees it.
        """

        if not agent_name or not project_key or not owner_token:
            raise ProxyError("direct binding needs agent_name, project_key and token")
        # external_id_for rejects colons, so the synthetic id uses a dash.
        session_id = f"direct-{agent_name}"
        self._binding = {
            "external_id": external_id_for(session_id),
            "surface": "direct",
            "session_id": session_id,
            "agent_id": None,
            "parent_external_id": None,
            "agent_name": agent_name,
            "project_key": project_key,
            "program": program,
            "last_seen_at": None,
        }
        self._owner_token = owner_token
        return dict(self._binding)

    @property
    def bound_session_id(self) -> str | None:
        return None if self._binding is None else self._binding["session_id"]

    @property
    def bound_binding(self) -> dict[str, Any] | None:
        return None if self._binding is None else dict(self._binding)

    def _resolve(
        self, session_id: str, agent_id: str | None
    ) -> tuple[dict[str, Any], str]:
        if self._binding is None or self._owner_token is None:
            raise ProxyError("call bootstrap before using coordination tools")
        requested = external_id_for(
            session_id,
            self._binding["agent_id"] if agent_id is None else agent_id,
        )
        if requested != self._binding["external_id"]:
            raise ProxyError("requested runtime does not match the process binding")
        return self._binding, self._owner_token


class StdioMcpServer:
    """Small MCP stdio server with an explicit tool dispatch table."""

    def __init__(self, proxy: AgentStackProxy) -> None:
        self.proxy = proxy

    def serve_forever(self) -> None:
        for line in sys.stdin.buffer:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._write(_rpc_error(None, -32700, "Parse error"))
                continue
            if not isinstance(request, dict):
                self._write(_rpc_error(None, -32600, "Invalid request"))
                continue
            response = self.handle(request)
            if response is not None:
                self._write(response)

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return _rpc_error(request_id, -32600, "Invalid request")
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            params = request.get("params")
            requested = (
                params.get("protocolVersion")
                if isinstance(params, dict)
                else None
            )
            return _rpc_result(
                request_id,
                {
                    "protocolVersion": (
                        requested if isinstance(requested, str)
                        else DEFAULT_PROTOCOL_VERSION
                    ),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            )
        if method == "ping":
            return _rpc_result(request_id, {})
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": TOOL_DEFINITIONS})
        if method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict):
                return _rpc_error(request_id, -32602, "Invalid params")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _rpc_error(request_id, -32602, "Invalid params")
            return _rpc_result(request_id, self._call_tool(name, arguments))
        return _rpc_error(request_id, -32601, "Method not found")

    def _call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            result = _dispatch(self.proxy, name, arguments)
        except (ProxyError, AgentMailError, TypeError, ValueError) as exc:
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        except Exception:
            return {
                "content": [{"type": "text", "text": "internal proxy error"}],
                "isError": True,
            }
        structured = result if isinstance(result, dict) else {"result": result}
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "structuredContent": structured,
            "isError": False,
        }

    @staticmethod
    def _write(response: Mapping[str, Any]) -> None:
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _dispatch(
    proxy: AgentStackProxy, name: str, arguments: Mapping[str, Any]
) -> Any:
    handlers: dict[str, Callable[..., Any]] = {
        "bootstrap": proxy.bootstrap,
        "fetch_inbox": proxy.fetch_inbox,
        "send_message": proxy.send_message,
        "acknowledge_message": proxy.acknowledge_message,
        "reserve_files": proxy.reserve_files,
        "renew_reservations": proxy.renew_reservations,
        "release_reservations": proxy.release_reservations,
        "runtime_status": proxy.runtime_status,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ProxyError("tool is not allowlisted")
    call_arguments = dict(arguments)
    # A directly bound process serves exactly one agent, so the caller does not
    # have to know (or be able to spoof) a session id.
    if "session_id" not in call_arguments and proxy.bound_session_id is not None:
        call_arguments["session_id"] = proxy.bound_session_id
    # Agents are told (by CLAUDE.md / AGENTS.md and by habit) to call agent-mail
    # with project_key and agent_name. The proxy takes those from its binding
    # instead, but rejecting the arguments outright turns documented usage into
    # "unexpected keyword argument" on a child's very first call. Accept them
    # when they agree with the binding, and refuse only a real mismatch — which
    # is someone trying to act as another agent or project.
    binding = proxy.bound_binding
    for field in ("project_key", "agent_name"):
        if field not in call_arguments:
            continue
        supplied = call_arguments.pop(field)
        if supplied is None or binding is None:
            continue
        if str(supplied) != str(binding[field]):
            raise ProxyError(
                f"{field} does not match this connection's binding: "
                f"this process serves {binding['agent_name']!r}"
            )
    return handler(**call_arguments)


def _required_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProxyError(f"{field} must be a bounded non-empty string")
    if "\x00" in value:
        raise ProxyError(f"{field} contains an invalid character")
    return value


def _optional_text(value: Any, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, maximum)


def _agent_names(
    values: Any, field: str, *, required: bool = False
) -> list[str] | None:
    if values is None:
        if required:
            raise ProxyError(f"{field} is required")
        return None
    if not isinstance(values, list) or len(values) > 50:
        raise ProxyError(f"{field} must be a bounded list")
    if required and not values:
        raise ProxyError(f"{field} must not be empty")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or _AGENT_NAME.fullmatch(value) is None:
            raise ProxyError(f"{field} contains an invalid agent name")
        normalized.append(value)
    return normalized


def _reservation_paths(
    values: Any, *, required: bool = False
) -> list[str]:
    if values is None:
        if required:
            raise ProxyError("paths is required")
        return []
    if not isinstance(values, list) or len(values) > 100:
        raise ProxyError("paths must be a non-empty bounded list")
    if not values:
        if required:
            raise ProxyError("paths must be a non-empty bounded list")
        return []
    normalized: list[str] = []
    for value in values:
        _required_text(value, "path", 1024)
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise ProxyError("reservation paths must stay project-relative")
        normalized.append(value)
    return normalized


def _positive_ids(values: Any, field: str) -> list[int]:
    if values is None:
        return []
    if _is_integer(values):
        values = [values]
    if not isinstance(values, list) or len(values) > 100:
        raise ProxyError(f"{field} must be a bounded integer list")
    if not all(_is_integer(value) and value > 0 for value in values):
        raise ProxyError(f"{field} must contain positive integers")
    return list(values)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProxyError(f"{field} must be a boolean")
    return value


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: Any, code: int, message: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


_SESSION = {
    "type": "string",
    "minLength": 1,
    "description": "Codex App session_id for this task.",
}
_AGENT = {
    "type": ["string", "null"],
    "minLength": 1,
    "description": "Subagent agent_id; omit for a root task.",
}


def _schema(
    properties: Mapping[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": required,
    }


TOOL_DEFINITIONS = [
    {
        "name": "bootstrap",
        "description": "Bind this MCP process to one Bridge-observed Codex runtime.",
        "inputSchema": _schema(
            {"session_id": _SESSION, "agent_id": _AGENT},
            ["session_id"],
        ),
    },
    {
        "name": "fetch_inbox",
        "description": "Fetch this bound agent's inbox without exposing credentials.",
        "inputSchema": _schema(
            {
                "session_id": _SESSION,
                "agent_id": _AGENT,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "urgent_only": {"type": "boolean"},
                "include_bodies": {"type": "boolean"},
                "since_ts": {"type": ["string", "null"]},
                "topic": {"type": ["string", "null"]},
            },
            ["session_id"],
        ),
    },
    {
        "name": "send_message",
        "description": "Send an agent-mail message as this bound agent.",
        "inputSchema": _schema(
            {
                "session_id": _SESSION,
                "agent_id": _AGENT,
                "to": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cc": {"type": ["array", "null"], "items": {"type": "string"}},
                "bcc": {"type": ["array", "null"], "items": {"type": "string"}},
                "subject": {"type": "string", "minLength": 1},
                "body_md": {"type": "string", "minLength": 1},
                "importance": {"type": "string", "enum": sorted(_IMPORTANCE)},
                "ack_required": {"type": "boolean"},
                "thread_id": {"type": ["string", "null"]},
                "topic": {"type": ["string", "null"]},
            },
            ["session_id", "to", "subject", "body_md"],
        ),
    },
    {
        "name": "acknowledge_message",
        "description": "Acknowledge an inbox message for this bound agent.",
        "inputSchema": _schema(
            {
                "session_id": _SESSION,
                "agent_id": _AGENT,
                "message_id": {"type": "integer", "minimum": 1},
            },
            ["session_id", "message_id"],
        ),
    },
    {
        "name": "reserve_files",
        "description": "Reserve project-relative files for this bound agent.",
        "inputSchema": _schema(
            {
                "session_id": _SESSION,
                "agent_id": _AGENT,
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
                "exclusive": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            ["session_id", "paths"],
        ),
    },
    {
        "name": "renew_reservations",
        "description": "Renew selected reservations owned by this bound agent.",
        "inputSchema": _schema(
            {
                "session_id": _SESSION,
                "agent_id": _AGENT,
                "extend_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 86400,
                },
                "paths": {"type": ["array", "null"], "items": {"type": "string"}},
                "file_reservation_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            ["session_id"],
        ),
    },
    {
        "name": "release_reservations",
        "description": "Release selected or all reservations owned by this bound agent.",
        "inputSchema": _schema(
            {
                "session_id": _SESSION,
                "agent_id": _AGENT,
                "paths": {"type": ["array", "null"], "items": {"type": "string"}},
                "file_reservation_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            ["session_id"],
        ),
    },
    {
        "name": "runtime_status",
        "description": "Return sanitized runtime identity, state, and parent lineage.",
        "inputSchema": _schema(
            {"session_id": _SESSION, "agent_id": _AGENT},
            ["session_id"],
        ),
    },
]


def serve() -> None:
    """Start the allowlisted MCP proxy over stdio."""

    config = ProxyConfig.from_env()
    identities = IdentityStore(config.runtime_dir / "identity")
    snapshots = SnapshotStore(config.runtime_dir / "snapshot.json")
    transport = HttpJsonRpcTransport(
        config.endpoint,
        bearer_token=config.bearer_token,
    )
    proxy = AgentStackProxy(
        identities,
        snapshots,
        AgentMailClient(transport),
        bootstrap_wait_seconds=config.bootstrap_wait_seconds,
    )
    if config.is_direct:
        proxy.bind_direct(
            agent_name=config.agent_name or "",
            project_key=config.project_key or "",
            owner_token=load_direct_owner_token(config),
            program=config.program,
        )
    StdioMcpServer(proxy).serve_forever()


def direct_token_path(config: ProxyConfig) -> Path:
    """Where a directly bound proxy reads its agent's owner token."""

    if config.token_file is not None:
        return config.token_file
    # Same layout the shell helpers write: ags_registration_token_file().
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", config.agent_name or "")
    base = config.runtime_dir
    if base.name == "codex-app":
        base = base.parent
    return base / f"agent_token_{key}"


def load_direct_owner_token(config: ProxyConfig) -> str:
    path = direct_token_path(config)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProxyError(
            f"cannot read the owner token for {config.agent_name} at {path}"
        ) from exc
    if not token:
        raise ProxyError(f"owner token file is empty: {path}")
    return token


if __name__ == "__main__":
    serve()
