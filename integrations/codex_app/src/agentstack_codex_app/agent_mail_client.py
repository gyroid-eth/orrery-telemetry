"""Injectable agent-mail client used by the Bridge and P2 MCP proxy.

The client exposes only the operations explicitly allowlisted by the Codex App
integration. Identity, project, and owner credentials are supplied by the
server-side binding; callers never provide them through the proxy tool surface.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class AgentMailError(RuntimeError):
    """Raised when the agent-mail transport or response is invalid."""


class JsonRpcTransport(Protocol):
    """Injectable JSON-RPC transport used by :class:`AgentMailClient`."""

    def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Registration:
    agent_name: str
    registration_token: str


class HttpJsonRpcTransport:
    """POST JSON-RPC to a configured agent-mail HTTP endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        bearer_token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not endpoint:
            raise ValueError("agent-mail endpoint must be configured")
        self.endpoint = endpoint
        self.bearer_token = bearer_token
        self.timeout = timeout

    def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "close",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise AgentMailError("agent-mail HTTP request failed") from exc
        if not isinstance(body, dict):
            raise AgentMailError("agent-mail returned a non-object response")
        return body


class AgentMailClient:
    """Perform the allowlisted Codex App operations through agent-mail."""

    def __init__(self, transport: JsonRpcTransport) -> None:
        self.transport = transport
        self._request_id = 0

    def register_agent(
        self,
        *,
        project_key: str,
        model: str,
        registration_token: str,
        agent_name: str | None = None,
        task_description: str = "Codex App task",
    ) -> Registration:
        """Register a fresh or existing identity idempotently."""

        if not project_key or not registration_token:
            raise ValueError("project_key and registration_token are required")
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "program": "codex-app",
            "model": model or "unknown",
            "task_description": task_description,
            "registration_token": registration_token,
        }
        if agent_name is not None:
            arguments["name"] = agent_name
        result = self._call_tool_object("register_agent", arguments)
        returned_name = result.get("name") or result.get("agent_name")
        if not isinstance(returned_name, str) or not returned_name:
            raise AgentMailError("register_agent response did not include an agent name")
        returned_token = result.get("registration_token")
        if returned_token is not None and returned_token != registration_token:
            raise AgentMailError("register_agent returned a conflicting owner token")
        return Registration(returned_name, registration_token)

    def fetch_inbox(
        self,
        *,
        project_key: str,
        agent_name: str,
        registration_token: str | None = None,
        limit: int = 20,
        urgent_only: bool = False,
        include_bodies: bool = False,
        since_ts: str | None = None,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "agent_name": agent_name,
            "limit": limit,
            "urgent_only": urgent_only,
            "include_bodies": include_bodies,
        }
        _put_optional(arguments, "registration_token", registration_token)
        _put_optional(arguments, "since_ts", since_ts)
        _put_optional(arguments, "topic", topic)
        value = self._call_tool("fetch_inbox", arguments)
        if isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise AgentMailError("fetch_inbox returned an unexpected result")
        return [dict(item) for item in value]

    def send_message(
        self,
        *,
        project_key: str,
        agent_name: str,
        registration_token: str,
        to: list[str],
        subject: str,
        body_md: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        importance: str = "normal",
        ack_required: bool = False,
        thread_id: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "sender_name": agent_name,
            "sender_token": registration_token,
            "to": to,
            "subject": subject,
            "body_md": body_md,
            "importance": importance,
            "ack_required": ack_required,
        }
        _put_optional(arguments, "cc", cc)
        _put_optional(arguments, "bcc", bcc)
        _put_optional(arguments, "thread_id", thread_id)
        _put_optional(arguments, "topic", topic)
        return self._call_tool_object("send_message", arguments)

    def acknowledge_message(
        self,
        *,
        project_key: str,
        agent_name: str,
        message_id: int,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "agent_name": agent_name,
            "message_id": message_id,
        }
        _put_optional(arguments, "registration_token", registration_token)
        return self._call_tool_object("acknowledge_message", arguments)

    def reserve_files(
        self,
        *,
        project_key: str,
        agent_name: str,
        paths: list[str],
        ttl_seconds: int = 3600,
        exclusive: bool = True,
        reason: str = "",
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "agent_name": agent_name,
            "paths": paths,
            "ttl_seconds": ttl_seconds,
            "exclusive": exclusive,
            "reason": reason,
        }
        _put_optional(arguments, "registration_token", registration_token)
        return self._call_tool_object("file_reservation_paths", arguments)

    def renew_reservations(
        self,
        *,
        project_key: str,
        agent_name: str,
        extend_seconds: int = 1800,
        paths: list[str] | None = None,
        file_reservation_ids: list[int] | None = None,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "agent_name": agent_name,
            "extend_seconds": extend_seconds,
        }
        _put_optional(arguments, "registration_token", registration_token)
        _put_optional(arguments, "paths", paths)
        _put_optional(arguments, "file_reservation_ids", file_reservation_ids)
        return self._call_tool_object("renew_file_reservations", arguments)

    def release_reservations(
        self,
        *,
        project_key: str,
        agent_name: str,
        paths: list[str] | None = None,
        file_reservation_ids: list[int] | None = None,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "agent_name": agent_name,
        }
        _put_optional(arguments, "registration_token", registration_token)
        _put_optional(arguments, "paths", paths)
        _put_optional(arguments, "file_reservation_ids", file_reservation_ids)
        return self._call_tool_object("release_file_reservations", arguments)

    def retire_agent(
        self,
        *,
        project_key: str,
        agent_name: str,
        registration_token: str,
    ) -> dict[str, Any]:
        """Retire one Bridge-owned agent using its persisted owner token."""

        if not project_key or not agent_name or not registration_token:
            raise ValueError(
                "project_key, agent_name, and registration_token are required"
            )
        return self._call_tool_object(
            "retire_agent",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "registration_token": registration_token,
            },
        )

    def whois(
        self,
        *,
        project_key: str,
        agent_name: str,
        registration_token: str | None = None,
    ) -> dict[str, Any]:
        """Read one agent profile without archive commit history."""

        if not project_key or not agent_name:
            raise ValueError("project_key and agent_name are required")
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "agent_name": agent_name,
            "include_recent_commits": False,
        }
        _put_optional(arguments, "registration_token", registration_token)
        profile = self._call_tool_object("whois", arguments)
        returned_name = profile.get("name")
        if returned_name != agent_name:
            raise AgentMailError("whois returned a mismatched agent identity")
        return profile

    def _call_tool_object(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = self._call_tool(tool_name, arguments)
        if not isinstance(value, dict):
            raise AgentMailError(f"{tool_name} returned a non-object result")
        return dict(value)

    def _call_tool(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> Any:
        self._request_id += 1
        response = self.transport(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": dict(arguments)},
            }
        )
        if response.get("error"):
            raise AgentMailError("agent-mail JSON-RPC call failed")
        rpc_result = response.get("result")
        if not isinstance(rpc_result, dict):
            raise AgentMailError("agent-mail response is missing result")
        if rpc_result.get("isError") is True:
            raise AgentMailError("agent-mail tool call failed")

        structured = rpc_result.get("structuredContent")
        if isinstance(structured, (dict, list)):
            return structured
        content = rpc_result.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, (dict, list)):
                    return decoded
        raise AgentMailError("agent-mail tool result has an unexpected shape")


def _put_optional(arguments: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        arguments[key] = value
