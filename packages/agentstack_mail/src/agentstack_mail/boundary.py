"""Fail-closed FastMCP publication boundary for AgentStack Mail.

The derived tool bodies live in :mod:`agentstack_mail.app`, but only the
versioned compatibility surface is publishable.  Keeping the publication
decision in the server decorator prevents a newly copied upstream tool or
resource from becoming reachable by accident.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from .contract import COMPATIBILITY_TOOLS


class CompatibilityFastMCP(FastMCP):
    """FastMCP server that can publish only the frozen compatibility tools."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._agentstack_declared_tools: set[str] = set()
        self._agentstack_published_tools: set[str] = set()
        self._agentstack_declared_resources: set[str] = set()

    def tool(
        self,
        name_or_fn: str | Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Register a compatibility tool or retain a non-published body."""

        if callable(name_or_fn):
            function = name_or_fn
            tool_name = name or getattr(function, "__name__", type(function).__name__)
            return self._publish_or_retain_tool(function, tool_name, name=name, **kwargs)

        positional_name = name_or_fn if isinstance(name_or_fn, str) else None

        def decorator(function: Callable[..., Any]) -> Any:
            tool_name = name or positional_name or getattr(function, "__name__", type(function).__name__)
            return self._publish_or_retain_tool(function, tool_name, name=tool_name, **kwargs)

        return decorator

    def _publish_or_retain_tool(
        self,
        function: Callable[..., Any],
        tool_name: str,
        **kwargs: Any,
    ) -> Any:
        self._agentstack_declared_tools.add(tool_name)
        if tool_name not in COMPATIBILITY_TOOLS:
            return function
        self._agentstack_published_tools.add(tool_name)
        # Pass the callable directly to the base implementation. Calling the
        # decorator form would create a partial bound to ``self.tool`` and
        # recurse through this publication guard.
        return FastMCP.tool(self, function, **kwargs)

    def resource(self, uri: str, **_kwargs: Any) -> Any:
        """Retain resource bodies without publishing an unversioned API."""

        self._agentstack_declared_resources.add(uri)

        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            return function

        return decorator

    def assert_contract_boundary(self) -> None:
        """Fail server construction if any compatibility tool is missing."""

        missing = COMPATIBILITY_TOOLS - self._agentstack_published_tools
        extra = self._agentstack_published_tools - COMPATIBILITY_TOOLS
        if missing or extra:
            raise RuntimeError(
                "AgentStack Mail tool boundary mismatch: "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )

    @property
    def declared_tool_names(self) -> frozenset[str]:
        return frozenset(self._agentstack_declared_tools)

    @property
    def published_tool_names(self) -> frozenset[str]:
        return frozenset(self._agentstack_published_tools)

    @property
    def suppressed_resource_uris(self) -> frozenset[str]:
        return frozenset(self._agentstack_declared_resources)
