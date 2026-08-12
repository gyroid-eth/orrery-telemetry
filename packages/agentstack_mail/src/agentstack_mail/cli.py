"""Console entry point for the isolated AgentStack Mail HTTP server."""

from __future__ import annotations

import argparse
import ipaddress
from collections.abc import Sequence
from typing import Any

from .config import get_settings


_GRACEFUL_SHUTDOWN_SECONDS = 8.0


def _normalized_path(raw_path: str) -> str:
    path = raw_path.strip()
    if not path:
        raise RuntimeError("AgentStack Mail HTTP path must not be empty")
    return path if path.startswith("/") else f"/{path}"


def _is_loopback_host(raw_host: str) -> bool:
    host = raw_host.strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentstack-mail",
        description="Run the loopback-only AgentStack Mail MCP server.",
    )
    parser.add_argument("--host", metavar="HOST", help="loopback bind host")
    parser.add_argument("--port", metavar="PORT", type=int, help="HTTP listen port")
    parser.add_argument("--path", metavar="PATH", help="MCP HTTP path")
    return parser


def _build_mcp_server() -> Any:
    # Keep the FastMCP/application import behind argument parsing so --help is
    # fast, warning-free, and unable to initialize runtime state.
    from .app import build_mcp_server

    return build_mcp_server()


def main(argv: Sequence[str] | None = None) -> None:
    """Run the exact 24-tool MCP boundary on its isolated HTTP endpoint."""
    args = _parser().parse_args(argv)
    settings = get_settings()
    host = (
        args.host if args.host is not None else settings.http.host
    ).strip()
    port = args.port if args.port is not None else settings.http.port
    path = args.path if args.path is not None else settings.http.path
    if settings.agent_name_enforcement_mode != "passthrough":
        raise RuntimeError(
            "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough is required"
        )
    if not _is_loopback_host(host):
        raise RuntimeError(
            "the first AgentStack Mail HTTP entry point is loopback-only; "
            "use --host or set AGENTSTACK_MAIL_HTTP_HOST to 127.0.0.1, ::1, "
            "or localhost"
        )
    if settings.http.bearer_token or settings.http.jwt_enabled:
        raise RuntimeError(
            "HTTP bearer/JWT authentication is not wired into the first "
            "AgentStack Mail entry point; refusing to start with auth configured"
        )

    _build_mcp_server().run(
        transport="streamable-http",
        host=host,
        port=port,
        path=_normalized_path(path),
        log_level=settings.log_level.lower(),
        json_response=True,
        stateless_http=True,
        # FastMCP 2.13 otherwise supplies zero seconds, which immediately
        # cancels lifespan cleanup and leaves SQLite WAL/SHM sidecars behind.
        # Keep this below the service supervisor's 10-second TERM grace.
        uvicorn_config={
            "loop": "asyncio",
            "ws": "none",
            "timeout_graceful_shutdown": _GRACEFUL_SHUTDOWN_SECONDS,
        },
    )


if __name__ == "__main__":
    main()
