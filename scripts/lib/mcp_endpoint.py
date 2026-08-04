#!/usr/bin/env python3
"""Whether two agent-mail MCP URLs reach the same server.

This lives on its own because three places need the same answer: the installer
decides whether rewriting `~/.claude.json` would be churn, doctor decides
whether to tell the user delegation is broken, and selftest decides whether to
fail the run. When only the installer knew that `/api/` and `/mcp` are the same
door, the other two reported a working configuration as missing and as a hard
failure.

agent-mail mounts its MCP app at both `/api` and `/mcp` no matter which one is
configured as the base ("compatibility aliases ... regardless of configured
base" in its http.py). Verified against a running server: POST to either
returns 200.
"""

from __future__ import annotations

import sys
import urllib.parse


INTERCHANGEABLE_MCP_PATHS = frozenset({"/api", "/mcp"})


def same_endpoint(left: str, right: str) -> bool:
    """True when both URLs address the same agent-mail MCP endpoint."""
    a, b = urllib.parse.urlsplit(left), urllib.parse.urlsplit(right)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        return False
    a_path, b_path = a.path.rstrip("/") or "/", b.path.rstrip("/") or "/"
    if a_path == b_path:
        return True
    return {a_path, b_path} <= INTERCHANGEABLE_MCP_PATHS


def main(argv: list[str]) -> int:
    """Exit 0 when the two URL arguments reach the same server, 1 otherwise."""
    if len(argv) != 2:
        print("usage: mcp_endpoint.py <url> <url>", file=sys.stderr)
        return 2
    return 0 if same_endpoint(argv[0], argv[1]) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
