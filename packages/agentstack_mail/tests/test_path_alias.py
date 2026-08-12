"""The alias middleware must rewrite exactly the alias forms and nothing else.

The 2026-08-12 cutover served ``/api/`` only, and every caller defaulting to
``/mcp`` (spawn helpers, install.sh) 404ed silently. These tests pin the
restored contract: both well-known paths reach the one mounted app, and the
rewrite lands on the exact 200 form, not the 307-redirecting one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentstack_mail.path_alias import PathAliasMiddleware, expand_alias_paths


def test_expand_alias_paths_matches_both_slash_forms() -> None:
    assert expand_alias_paths("/api/", ["/mcp"]) == frozenset({"/mcp", "/mcp/"})


def test_expand_alias_paths_drops_the_canonical_itself() -> None:
    # "/api" is the canonical "/api/" modulo trailing slash: rewriting it would
    # be a no-op loop, and matching it must not shadow the real mount.
    assert expand_alias_paths("/api/", ["/api", "/api/"]) == frozenset()


def test_expand_alias_paths_normalizes_missing_leading_slash_and_blanks() -> None:
    assert expand_alias_paths("/api/", ["mcp", "", "  "]) == frozenset({"/mcp", "/mcp/"})


class _RecordingApp:
    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.scopes.append(scope)


def _run(middleware: PathAliasMiddleware, scope: dict[str, Any]) -> None:
    asyncio.run(middleware(scope, None, None))


def test_alias_request_is_rewritten_to_the_exact_canonical_form() -> None:
    app = _RecordingApp()
    middleware = PathAliasMiddleware(app, canonical="/api/", aliases=("/mcp", "/api"))

    for requested in ("/mcp", "/mcp/"):
        _run(middleware, {"type": "http", "path": requested, "raw_path": requested.encode()})

    assert [s["path"] for s in app.scopes] == ["/api/", "/api/"]
    assert [s["raw_path"] for s in app.scopes] == [b"/api/", b"/api/"]


def test_canonical_and_unrelated_requests_pass_through_unmodified() -> None:
    app = _RecordingApp()
    middleware = PathAliasMiddleware(app, canonical="/api/", aliases=("/mcp",))

    original = {"type": "http", "path": "/api/", "raw_path": b"/api/"}
    _run(middleware, original)
    unrelated = {"type": "http", "path": "/health", "raw_path": b"/health"}
    _run(middleware, unrelated)

    # Pass-through must hand over the SAME scope object: a copy would discard
    # mutations the downstream app makes for the server to observe.
    assert app.scopes[0] is original
    assert app.scopes[1] is unrelated


def test_non_http_scopes_are_never_touched() -> None:
    app = _RecordingApp()
    middleware = PathAliasMiddleware(app, canonical="/api/", aliases=("/mcp",))

    lifespan = {"type": "lifespan"}
    _run(middleware, lifespan)

    assert app.scopes == [lifespan]


def test_rewrite_does_not_mutate_the_caller_visible_scope() -> None:
    app = _RecordingApp()
    middleware = PathAliasMiddleware(app, canonical="/api/", aliases=("/mcp",))

    scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp"}
    _run(middleware, scope)

    assert scope["path"] == "/mcp"
    assert app.scopes[0]["path"] == "/api/"
