"""Minimal, injectable agent-mail registration client.

Only ``register_agent`` is exposed in P1. Inbox and message operations remain
outside this client until the allowlisted P2 MCP proxy is implemented.
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
    """Register and refresh Codex App identities through agent-mail."""

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
        result = self._call_tool("register_agent", arguments)
        returned_name = result.get("name") or result.get("agent_name")
        if not isinstance(returned_name, str) or not returned_name:
            raise AgentMailError("register_agent response did not include an agent name")
        returned_token = result.get("registration_token")
        if returned_token is not None and returned_token != registration_token:
            raise AgentMailError("register_agent returned a conflicting owner token")
        return Registration(returned_name, registration_token)

    def _call_tool(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
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
        if isinstance(structured, dict):
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
                if isinstance(decoded, dict):
                    return decoded
        raise AgentMailError("agent-mail tool result has an unexpected shape")
