"""ASGI path-alias rewriting for the AgentStack Mail HTTP endpoint.

The retired third-party server answered the MCP endpoint on both ``/mcp`` and
``/api/``, and shell helpers, ``install.sh``, and MCP client configs split
between the two. Serving only one path turned every caller on the other path
into a silent 404 (the 2026-08-12 cutover broke child spawning exactly this
way). This middleware restores the old contract: well-known alias paths are
rewritten onto the canonical endpoint before routing, so the FastMCP app keeps
a single mount and the lifespan/SIGTERM contract in :mod:`boundary` is
untouched.

The rewrite targets the canonical path exactly as configured. That matters
because the underlying Starlette mount is trailing-slash exact: with
``path=/api/`` the app serves 200 on ``/api/`` but 307 on ``/api``, and a
plain ``curl`` caller does not follow redirects — so rewriting an alias to the
redirecting form would trade a 404 for an empty-body 307 and stay broken.
"""

from __future__ import annotations

from typing import Any


def _strip_slash(path: str) -> str:
    return path[:-1] if path.endswith("/") and len(path) > 1 else path


def expand_alias_paths(canonical: str, aliases: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Return the exact request paths that must rewrite to ``canonical``.

    Each alias matches with and without a trailing slash. An alias that is the
    canonical path itself (in either form) is dropped rather than rewritten,
    so a config that lists the canonical among the aliases stays harmless.
    """

    canonical_stem = _strip_slash(canonical)
    expanded: set[str] = set()
    for alias in aliases:
        alias = alias.strip()
        if not alias:
            continue
        if not alias.startswith("/"):
            alias = f"/{alias}"
        stem = _strip_slash(alias)
        if stem == canonical_stem:
            continue
        expanded.add(stem)
        expanded.add(f"{stem}/")
    return frozenset(expanded)


class PathAliasMiddleware:
    """Rewrite alias request paths onto the canonical MCP endpoint."""

    def __init__(self, app: Any, canonical: str, aliases: list[str] | tuple[str, ...]) -> None:
        self._app = app
        self._canonical = canonical
        self._alias_paths = expand_alias_paths(canonical, aliases)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") in self._alias_paths:
            scope = dict(scope)
            scope["path"] = self._canonical
            scope["raw_path"] = self._canonical.encode("ascii")
        await self._app(scope, receive, send)
