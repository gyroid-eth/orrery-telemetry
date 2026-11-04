"""Small, opt-in Streamable HTTP MCP server. It never implements write tools."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .model import Dashboard, DashboardError, TOOL_DEFINITIONS, ToolError, invoke, snapshot

SECRET_CHARS = 43
MAX_BODY = 128 * 1024
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    mode: str
    dashboard_url: str
    port: int
    state_dir: Path

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> "Config":
        env = dict(os.environ if values is None else values)
        mode = env.get("ORRERY_MCP_MODE")
        if mode not in ("fresh", "legacy"):
            raise ConfigurationError("set ORRERY_MCP_MODE explicitly to fresh or legacy")
        for opt in ("ORRERY_MCP_ENABLE_SPAWN", "ORRERY_MCP_ENABLE_MESSAGE"):
            if env.get(opt, "0").lower() not in ("0", "false"):
                raise ConfigurationError(opt + " is unsupported in the read-only adapter")
        if env.get("ORRERY_MCP_AUTH", "pairing") != "pairing":
            raise ConfigurationError("this adapter supports pairing only")
        try:
            port = int(env.get("ORRERY_MCP_PORT", "8791"))
        except ValueError:
            raise ConfigurationError("invalid port") from None
        if not 1 <= port <= 65535:
            raise ConfigurationError("invalid port")
        url = env.get("ORRERY_MCP_DASHBOARD_URL", "http://127.0.0.1:8770")
        try:
            Dashboard(url)
        except (DashboardError, ValueError):
            raise ConfigurationError("dashboard must be a literal loopback URL") from None
        state = Path(env.get("ORRERY_MCP_STATE_DIR", str(Path.home() / ".config" / "orrery-mcp-readonly")))
        if not state.is_absolute():
            raise ConfigurationError("state directory must be absolute")
        return cls(mode, url, port, state)


def _file(config: Config) -> Path:
    return config.state_dir / "pairing.json"


def _check_private(path: Path, *, directory: bool) -> None:
    item = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o077:
        raise ConfigurationError("pairing state is not owner-private")
    if not directory and item.st_nlink != 1:
        raise ConfigurationError("pairing file has multiple links")


def _prepare_state(config: Config) -> None:
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private(config.state_dir, directory=True)


def read_secret(config: Config) -> str:
    try:
        _check_private(config.state_dir, directory=True)
        path = _file(config)
        _check_private(path, directory=False)
        data = json.loads(path.read_bytes())
        secret = data.get("secret")
        if not isinstance(secret, str) or len(secret) != SECRET_CHARS or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in secret):
            raise ConfigurationError("invalid pairing secret")
        return secret
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError("pairing unavailable; run pair first") from None


def pair(config: Config, *, rotate: bool = False) -> None:
    _prepare_state(config)
    target = _file(config)
    if target.exists() and not rotate:
        read_secret(config)
        raise ConfigurationError("already paired; use pair --rotate to revoke the old URL")
    if target.is_symlink():
        raise ConfigurationError("refusing a symlink")
    if target.exists():
        read_secret(config)
    secret = secrets.token_urlsafe(32)
    if len(secret) != SECRET_CHARS:
        raise ConfigurationError("failed to create a pairing secret")
    temp = config.state_dir / (".pair-" + secrets.token_hex(8))
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps({"secret": secret}).encode() + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def _result(identity: Any, value: dict) -> dict:
    return {"jsonrpc": "2.0", "id": identity, "result": value}


def _error(identity: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message}}


class Gateway:
    def __init__(self, config: Config, dashboard: Dashboard | None = None):
        self.config = config
        self.dashboard = dashboard or Dashboard(config.dashboard_url)
        self.hosts = {f"127.0.0.1:{config.port}", f"localhost:{config.port}"}

    def path_allowed(self, path: str) -> bool:
        try:
            target = "/mcp/" + read_secret(self.config)
        except ConfigurationError:
            return False
        return hmac.compare_digest(path.encode(), target.encode())

    def rpc(self, request: Any) -> tuple[int, dict | None]:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return 400, _error(None, -32600, "invalid JSON-RPC request")
        if "id" not in request:
            return 202, None
        identity = request["id"]
        if type(identity) not in (str, int):
            return 400, _error(None, -32600, "invalid JSON-RPC id")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return 200, _error(identity, -32602, "params must be an object")
        method = request["method"]
        if method == "initialize":
            version = params.get("protocolVersion")
            if version not in PROTOCOLS:
                version = PROTOCOLS[0]
            return 200, _result(identity, {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "orrery-chatgpt-mcp", "version": __version__},
                "instructions": "Read-only. All writes, spawn, message commit and configuration changes are unavailable.",
            })
        if method == "ping":
            return 200, _result(identity, {})
        if method == "tools/list":
            return 200, _result(identity, {"tools": TOOL_DEFINITIONS})
        if method != "tools/call":
            return 200, _error(identity, -32601, "unsupported method")
        name = params.get("name")
        if not isinstance(name, str):
            return 200, _error(identity, -32602, "tool name required")
        try:
            body = invoke(name, params.get("arguments", {}), self.dashboard, self.config.mode)
            output = {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
                      "structuredContent": body, "isError": False}
        except ToolError as exc:
            item = {"error": {"code": exc.code, "message": str(exc)}}
            output = {"content": [{"type": "text", "text": json.dumps(item)}],
                      "structuredContent": item, "isError": True}
        except Exception:
            item = {"error": {"code": "internal_error", "message": "read-only request failed"}}
            output = {"content": [{"type": "text", "text": json.dumps(item)}],
                      "structuredContent": item, "isError": True}
        return 200, _result(identity, output)


def handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "orrery-mcp-readonly"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, fmt, *args):
            # No request paths, secret URLs, headers or payloads in logs.
            return

        def _send(self, status: int, body: dict | None = None, extra: dict[str, str] | None = None):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
            if status >= 400:
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if self.close_connection:
                self.send_header("Connection", "close")
            for key, val in (extra or {}).items():
                self.send_header(key, val)
            self.end_headers()
            if raw and self.command != "HEAD":
                self.wfile.write(raw)

        def _admit(self) -> bool:
            if self.headers.get_all("Transfer-Encoding") or len(self.headers.get_all("Content-Length") or []) > 1:
                self._send(400, {"error": "ambiguous_framing"})
                return False
            hosts = self.headers.get_all("Host") or []
            if len(hosts) != 1 or hosts[0].strip().lower() not in gateway.hosts:
                self._send(403, {"error": "untrusted_host"})
                return False
            if self.headers.get_all("Origin"):
                self._send(403, {"error": "cross_origin_not_allowed"})
                return False
            if self.command != "POST" and self.headers.get("Content-Length", "0").strip() not in ("", "0"):
                self._send(400, {"error": "unexpected_body"})
                return False
            return True

        def do_GET(self):
            if not self._admit():
                return
            if self.path == "/healthz":
                self._send(200, {"ok": True, "read_only": True})
            elif gateway.path_allowed(self.path):
                self._send(405, {"error": "method_not_allowed"}, {"Allow": "POST"})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self):
            if not self._admit():
                return
            if not gateway.path_allowed(self.path):
                self._send(404, {"error": "not_found"})
                return
            length = self.headers.get("Content-Length", "").strip()
            if not length.isdigit():
                self._send(411, {"error": "length_required"})
                return
            amount = int(length)
            if amount > MAX_BODY:
                self._send(413, {"error": "too_large"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send(415, {"error": "unsupported_media_type"})
                return
            accept = self.headers.get("Accept", "*/*").lower()
            if "application/json" not in accept and "*/*" not in accept:
                self._send(406, {"error": "not_acceptable"})
                return
            protocol = self.headers.get("MCP-Protocol-Version")
            if protocol is not None and protocol not in PROTOCOLS:
                self._send(400, {"error": "unsupported_protocol"})
                return
            try:
                data = self.rfile.read(amount)
                if len(data) != amount:
                    raise ValueError("short body")
                request = json.loads(data)
            except (UnicodeError, ValueError):
                self._send(400, _error(None, -32700, "parse error"))
                return
            code, response = gateway.rpc(request)
            self._send(code, response)

        def do_HEAD(self):
            self.do_GET()

        def do_DELETE(self):
            self._send(405, {"error": "method_not_allowed"}, {"Allow": "POST"})

        do_PUT = do_DELETE
        do_PATCH = do_DELETE
        do_OPTIONS = do_DELETE

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="Optional read-only ORRERY MCP adapter")
    sub = cli.add_subparsers(dest="cmd", required=True)
    pairing = sub.add_parser("pair", help="create one local pairing URL")
    pairing.add_argument("--rotate", action="store_true", help="revoke old URL")
    pairing.add_argument("--show-url", action="store_true", help="display the private URL on the local terminal")
    sub.add_parser("check", help="read-only compatibility preflight")
    sub.add_parser("serve", help="run until interrupted (Ctrl+C)")
    args = cli.parse_args(argv)
    try:
        config = Config.from_env()
        if args.cmd == "pair":
            pair(config, rotate=args.rotate)
            print("pairing created; secret is stored in the private state directory")
            if args.show_url:
                print(f"http://127.0.0.1:{config.port}/mcp/{read_secret(config)}")
            return 0
        read_secret(config)
        if args.cmd == "check":
            view = snapshot(Dashboard(config.dashboard_url), config.mode)
            print(json.dumps({"mode": view.mode, "version": view.version,
                              "mail_instance_id": view.instance, "agents": len(view.agents),
                              "unknown_running": sum(a["running"] is None for a in view.agents),
                              "read_only": True}, ensure_ascii=False))
            return 0
        # An incompatible dashboard must fail before any MCP listener is opened.
        snapshot(Dashboard(config.dashboard_url), config.mode)
        with Server(("127.0.0.1", config.port), handler(Gateway(config))) as service:
            print(f"read-only MCP listening on 127.0.0.1:{config.port}", flush=True)
            try:
                service.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt:
                pass
        return 0
    except (ConfigurationError, DashboardError, OSError) as exc:
        print(f"orrery-mcp-readonly: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
