"""Browser provenance admission before MCP routing, aliases and tool calls."""
from __future__ import annotations

from typing import Any


def request_from_own_page(scope: dict[str, Any]) -> bool:
    server = scope.get("server")
    if not server or not isinstance(server[1], int):
        return False
    port = server[1]
    allowed = {f"{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")}
    headers: dict[bytes, list[str]] = {}
    for name, value in scope.get("headers", []):
        headers.setdefault(name.lower(), []).append(value.decode("latin-1").lower())
    if len(headers.get(b"host", [])) != 1 or headers[b"host"][0] not in allowed:
        return False
    for name in (b"origin", b"sec-fetch-site", b"sec-fetch-mode"):
        if len(headers.get(name, [])) > 1:
            return False
    origins = headers.get(b"origin", [])
    # No Origin is the established CLI/hook transport. An empty or null
    # browser Origin is not a local process and must fail closed.
    if origins and origins != ["http://" + headers[b"host"][0]]:
        return False
    return (headers.get(b"sec-fetch-site") != ["cross-site"]
            or headers.get(b"sec-fetch-mode") == ["navigate"])


class LocalPageMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in {"http", "websocket"} and not request_from_own_page(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                body = b'{"error":"request_denied"}'
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
