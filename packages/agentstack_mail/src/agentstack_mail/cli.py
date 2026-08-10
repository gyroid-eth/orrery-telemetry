"""Console entry point for the isolated AgentStack Mail HTTP server."""

from __future__ import annotations

import ipaddress

from .app import build_mcp_server
from .config import get_settings


def _normalized_path(raw_path: str) -> str:
    path = raw_path.strip()
    if not path:
        raise RuntimeError("AGENTSTACK_MAIL_HTTP_PATH must not be empty")
    return path if path.startswith("/") else f"/{path}"


def _is_loopback_host(raw_host: str) -> bool:
    host = raw_host.strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def main() -> None:
    """Run the exact 22-tool MCP boundary on its isolated HTTP endpoint."""
    settings = get_settings()
    host = settings.http.host.strip()
    if not _is_loopback_host(host):
        raise RuntimeError(
            "the first AgentStack Mail HTTP entry point is loopback-only; "
            "set AGENTSTACK_MAIL_HTTP_HOST to 127.0.0.1, ::1, or localhost"
        )
    if settings.http.bearer_token or settings.http.jwt_enabled:
        raise RuntimeError(
            "HTTP bearer/JWT authentication is not wired into the first "
            "AgentStack Mail entry point; refusing to start with auth configured"
        )

    build_mcp_server().run(
        transport="streamable-http",
        host=host,
        port=settings.http.port,
        path=_normalized_path(settings.http.path),
        log_level=settings.log_level.lower(),
        json_response=True,
        stateless_http=True,
        uvicorn_config={"loop": "asyncio", "ws": "none"},
    )


if __name__ == "__main__":
    main()
