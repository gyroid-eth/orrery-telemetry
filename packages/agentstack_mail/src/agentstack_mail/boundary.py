"""Fail-closed FastMCP publication boundary for AgentStack Mail.

The derived tool bodies live in :mod:`agentstack_mail.app`, but only the
versioned compatibility surface is publishable.  Keeping the publication
decision in the server decorator prevents a newly copied upstream tool or
resource from becoming reachable by accident.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import uvicorn
from fastmcp import FastMCP

from .contract import COMPATIBILITY_TOOLS

_EXPECTED_UVICORN_VERSION = "0.52.1"


def _assert_uvicorn_signal_contract() -> None:
    actual = getattr(uvicorn, "__version__", "unknown")
    if actual != _EXPECTED_UVICORN_VERSION:
        raise RuntimeError(
            "AgentStack Mail requires Uvicorn "
            f"{_EXPECTED_UVICORN_VERSION}, found {actual}; the SIGTERM re-raise "
            "suppression is pinned because it depends on that version's "
            "internal signal-capture behavior"
        )


class _AgentStackUvicornServer(uvicorn.Server):
    """Allow FastMCP's outer lifespan to finish after a graceful SIGTERM."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Delegate handler installation and restoration to the pinned Uvicorn
        # implementation. Before its context exits and re-raises captured
        # signals, suppress only TERM so FastMCP's outer database lifespan can
        # finish; preserve Ctrl-C/SIGINT's conventional exit 130 behavior.
        with super().capture_signals():
            try:
                yield
            finally:
                self._captured_signals[:] = [
                    captured
                    for captured in self._captured_signals
                    if captured != signal.SIGTERM
                ]


class CompatibilityFastMCP(FastMCP):
    """FastMCP server that can publish only the frozen compatibility tools."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._agentstack_declared_tools: set[str] = set()
        self._agentstack_published_tools: set[str] = set()
        self._agentstack_declared_resources: set[str] = set()

    async def run_http_async(self, *args: Any, **kwargs: Any) -> None:
        """Run HTTP with a TERM path that reaches the FastMCP lifespan exit."""

        _assert_uvicorn_signal_contract()
        original_server = uvicorn.Server
        uvicorn.Server = _AgentStackUvicornServer
        try:
            await super().run_http_async(*args, **kwargs)
        finally:
            uvicorn.Server = original_server

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
        """Fail if the actual FastMCP registry is not the exact contract.

        The decorator bookkeeping is useful for provenance, but it is not an
        enforcement boundary: FastMCP can add or remove tools after decoration.
        Inspect the pinned FastMCP registry itself so post-registration filters
        and direct base-class registration cannot bypass the exact-24 contract.
        """

        manager = getattr(self, "_tool_manager", None)
        registry = getattr(manager, "_tools", None)
        if not isinstance(registry, Mapping):
            raise RuntimeError(
                "AgentStack Mail cannot inspect the FastMCP tool registry; "
                "refusing to construct a server without an exact boundary check"
            )

        actual = frozenset(registry)
        missing = COMPATIBILITY_TOOLS - actual
        extra = actual - COMPATIBILITY_TOOLS
        recorded_missing = COMPATIBILITY_TOOLS - self._agentstack_published_tools
        recorded_extra = self._agentstack_published_tools - COMPATIBILITY_TOOLS
        if missing or extra or recorded_missing or recorded_extra:
            raise RuntimeError(
                "AgentStack Mail tool boundary mismatch: "
                f"missing={sorted(missing)}, extra={sorted(extra)}, "
                f"recorded_missing={sorted(recorded_missing)}, "
                f"recorded_extra={sorted(recorded_extra)}"
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
