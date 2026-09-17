"""Fail-closed FastMCP publication boundary for ORRERY Mail.

The derived tool bodies live in :mod:`agentstack_mail.app`, but only the
versioned compatibility surface is publishable.  Keeping the publication
decision in the server decorator prevents a newly copied upstream tool or
resource from becoming reachable by accident.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import ValidationError

from .contract import COMPATIBILITY_TOOLS
from .tool_descriptions import COMPACT_TOOL_DESCRIPTIONS

_EXPECTED_UVICORN_VERSION = "0.52.1"


def _assert_uvicorn_signal_contract() -> None:
    actual = getattr(uvicorn, "__version__", "unknown")
    if actual != _EXPECTED_UVICORN_VERSION:
        raise RuntimeError(
            "ORRERY Mail requires Uvicorn "
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



# ---------------------------------------------------------------------------
# Tool argument boundary (#49)
#
# FastMCP validates tool arguments with a pydantic TypeAdapter and, on failure,
# logs the whole ValidationError (``input_value='...'`` included) through
# ``fastmcp.tools.tool_manager`` before the error reaches anything we own. A
# caller that sends a credential under an argument name the tool does not
# accept therefore writes that credential into the server log. Two layers stop
# that without weakening validation:
#
# 1. ``ToolArgumentBoundary`` (middleware) rejects unknown arguments before
#    FastMCP validation runs, reporting only how many there were, and re-raises
#    any remaining ValidationError as a ToolError that names the tool and the
#    fields only when they were verified against the published schema.
# 2. ``ToolValidationLogSanitizer`` (logging filter on the tool-manager logger)
#    rewrites the record FastMCP still emits for a ValidationError to a fixed
#    placeholder, a count and the error types; the logger cannot verify the
#    tool key or field names, so it repeats neither.
#
# The filter only touches records whose exception is a pydantic
# ValidationError; other tool errors keep their diagnostics.
# ---------------------------------------------------------------------------

TOOL_MANAGER_LOGGER = "fastmcp.tools.tool_manager"


def validation_error_summary(
    tool_name: str | None,
    exc: ValidationError,
    known_fields: frozenset[str] | set[str] | None = None,
) -> str:
    """Describe a ValidationError without any caller-supplied text.

    ``loc`` components can be caller-supplied dictionary keys, and the tool
    name FastMCP puts in its message is the requested key, so both are only
    reported when the caller of this function has verified them against the
    published schema. Everything else collapses to fixed placeholders.
    """
    try:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
    except TypeError:  # pragma: no cover - older pydantic signature
        errors = [
            {k: v for k, v in e.items() if k not in {"input", "url", "ctx"}}
            for e in exc.errors()
        ]
    types = sorted({str(e.get("type", "")) for e in errors if e.get("type")})
    paths: set[str] = set()
    if known_fields:
        for e in errors:
            loc = tuple(e.get("loc", ()))
            if loc and str(loc[0]) in known_fields:
                paths.add(str(loc[0]) + (".<...>" if len(loc) > 1 else ""))
            else:
                paths.add("<argument>")
    label = repr(tool_name) if tool_name is not None else "<tool>"
    text = f"Tool {label} rejected its arguments: {len(errors)} validation error(s)"
    if paths:
        text += f" at {', '.join(sorted(paths))}"
    if types:
        text += f" ({', '.join(types)})"
    return text


class ToolValidationLogSanitizer(logging.Filter):
    """Strip argument values from FastMCP's tool validation exception records."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        exc = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
        if not isinstance(exc, ValidationError):
            return True
        # The tool key in FastMCP's message is caller-supplied and the logger
        # has no schema to verify it against, so it is not repeated here.
        record.msg = validation_error_summary(None, exc)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def install_tool_validation_log_sanitizer() -> ToolValidationLogSanitizer:
    logger = logging.getLogger(TOOL_MANAGER_LOGGER)
    for existing in logger.filters:
        if isinstance(existing, ToolValidationLogSanitizer):
            return existing
    sanitizer = ToolValidationLogSanitizer()
    logger.addFilter(sanitizer)
    return sanitizer


class ToolArgumentBoundary(Middleware):
    """Reject unknown tool arguments and never echo caller-supplied text."""

    def __init__(self, server: FastMCP) -> None:
        self._server = server

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        message = context.message
        tool_name = str(getattr(message, "name", ""))
        arguments = getattr(message, "arguments", None) or {}
        schema = await self._parameter_schema(tool_name)
        known_fields: frozenset[str] = frozenset()
        verified_name: str | None = None
        if schema is not None:
            verified_name = tool_name
            properties = schema.get("properties")
            if isinstance(properties, dict):
                known_fields = frozenset(str(key) for key in properties)
                if not schema.get("additionalProperties", False):
                    unknown = len(set(arguments) - known_fields)
                    if unknown:
                        # Names are caller-supplied: report the count only.
                        raise ToolError(
                            f"Tool {tool_name!r} does not accept {unknown} of the supplied argument(s); "
                            "check the published schema"
                        )
        try:
            return await call_next(context)
        except ValidationError as exc:
            raise ToolError(validation_error_summary(verified_name, exc, known_fields)) from None

    async def _parameter_schema(self, tool_name: str) -> dict[str, Any] | None:
        manager = getattr(self._server, "_tool_manager", None)
        if manager is None:
            return None
        try:
            tool = await manager.get_tool(tool_name)
        except Exception:
            return None
        schema = getattr(tool, "parameters", None)
        return schema if isinstance(schema, dict) else None

class CompatibilityFastMCP(FastMCP):
    """FastMCP server that can publish only the frozen compatibility tools."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        install_tool_validation_log_sanitizer()
        self.add_middleware(ToolArgumentBoundary(self))
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
        # Publish the compact description instead of the upstream-derived
        # docstring: the docstrings are developer documentation measured in
        # kilobytes, and every connected agent session pays for tools/list.
        # The table wins even over an explicit description= at the decorator,
        # so the published surface has one source of truth. Tools absent from
        # the table (the two frozen read tools) keep their docstring text,
        # which the runtime contract pins to the fixture.
        compact = COMPACT_TOOL_DESCRIPTIONS.get(tool_name)
        if compact is not None:
            kwargs["description"] = compact
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
                "ORRERY Mail cannot inspect the FastMCP tool registry; "
                "refusing to construct a server without an exact boundary check"
            )

        actual = frozenset(registry)
        missing = COMPATIBILITY_TOOLS - actual
        extra = actual - COMPATIBILITY_TOOLS
        recorded_missing = COMPATIBILITY_TOOLS - self._agentstack_published_tools
        recorded_extra = self._agentstack_published_tools - COMPATIBILITY_TOOLS
        if missing or extra or recorded_missing or recorded_extra:
            raise RuntimeError(
                "ORRERY Mail tool boundary mismatch: "
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
