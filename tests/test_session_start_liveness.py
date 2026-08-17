"""The session-start hook must not call a healthy mail server dead.

Reported by a tester on 2026-08-17 and reproduced on the maintainer's machine:
the hook probed ``<base>/health/liveness``, a route AgentStack Mail does not
serve -- it answers on its MCP path and the configured aliases and nothing else.
``curl -sf`` fails on any non-2xx, so the probe failed on every healthy install.

The cost was not just a misleading line. Every session was told to skip
registration, so no agent ever refreshed ``last_active_ts``; that timestamp is
what keeps the staleness sweep off an agent's file reservations, and 5,058 of
12,699 reservations in the maintainer's database had been released within 120s
of being granted.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "session-start-reminder.sh"


class _Handler(http.server.BaseHTTPRequestHandler):
    """A stand-in for the real server: MCP over POST, no health route."""

    speaks_mcp = True
    get_status = 406
    reply: object = None          # override the reply document
    reply_status = 200            # override the HTTP status
    as_sse = False                # answer as an SSE stream
    last_request: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        try:
            _Handler.last_request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            _Handler.last_request = {}
        if not self.speaks_mcp:
            # Something else on the port. Plenty of services reject POST.
            self.send_response(405)
            self.end_headers()
            return
        # Answer the call that was made, not whatever was asked. A fake that
        # always reports health lets a probe calling the wrong tool pass.
        called = (self.last_request.get("params") or {}).get("name")
        if called != "health_check":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.last_request.get("id"),
                    "error": {"code": -32601, "message": f"unknown tool {called!r}"},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        document = self.reply
        if document is None:
            document = {
                "jsonrpc": "2.0",
                "id": self.last_request.get("id"),
                "result": {"structuredContent": {"status": "ok"}},
            }
        if self.as_sse:
            body = f"event: message\ndata: {json.dumps(document)}\n\n".encode()
            self.send_response(self.reply_status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(document).encode()
        self.send_response(self.reply_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") in {"/mcp", "/api"}:
            self.send_response(self.get_status)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture()
def mail_like_server() -> object:
    _Handler.speaks_mcp = True
    _Handler.get_status = 406
    _Handler.reply = None
    _Handler.reply_status = 200
    _Handler.as_sse = False
    _Handler.last_request = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _run_hook(mcp_url: str, tmp_path: Path) -> str:
    result = subprocess.run(
        ["/bin/bash", str(HOOK)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path),
            "AGENTSTACK_MCP_URL": mcp_url,
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path / "runtime"),
            "AGENTSTACK_HOOKS_DIR": str(REPO_ROOT / "hooks"),
        },
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_server_that_only_serves_mcp_is_reported_as_running(
    mail_like_server: http.server.HTTPServer,
    tmp_path: Path,
) -> None:
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "server is running" in output, output
    assert "not running" not in output, output


def test_nothing_listening_is_still_reported_as_not_running(tmp_path: Path) -> None:
    """The null case: the probe must still be able to say no.

    A probe that answers "running" unconditionally would pass the test above.
    """
    output = _run_hook("http://127.0.0.1:1/mcp", tmp_path)
    assert "not running" in output, output


def test_a_different_service_on_the_port_is_not_mistaken_for_mail(
    mail_like_server: http.server.HTTPServer,
    tmp_path: Path,
) -> None:
    """Answering HTTP is not the same as being this server.

    Rejecting a bare GET with 405/406 says only that some HTTP server is
    listening -- most services do that. The probe has to ask what it is.
    """
    _Handler.speaks_mcp = False
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "not running" in output, output


def test_a_server_answering_only_404_is_reported_as_not_running(
    mail_like_server: http.server.HTTPServer,
    tmp_path: Path,
) -> None:
    """Nothing at that path at all."""
    _Handler.speaks_mcp = False
    _Handler.get_status = 404
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "not running" in output, output


def test_the_probe_calls_health_check(
    mail_like_server: http.server.HTTPServer, tmp_path: Path
) -> None:
    """Pins which tool is called; the server answers only that one."""
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "server is running" in output
    assert (_Handler.last_request.get("params") or {}).get("name") == "health_check"


def test_an_sse_reply_is_understood(
    mail_like_server: http.server.HTTPServer, tmp_path: Path
) -> None:
    """The endpoint may stream its answer; that is still a healthy answer."""
    _Handler.as_sse = True
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "server is running" in output, output


@pytest.mark.parametrize(
    ("label", "document", "status"),
    [
        (
            "an error object that happens to say ok",
            {
                "jsonrpc": "2.0",
                "id": "session-start",
                "error": {"code": -1, "status": "error", "message": "ok"},
            },
            200,
        ),
        (
            "a healthy-looking body behind a 503",
            {
                "jsonrpc": "2.0",
                "id": "session-start",
                "result": {"structuredContent": {"status": "ok"}},
            },
            503,
        ),
        (
            "a bare status document with no JSON-RPC envelope",
            {"status": "ok"},
            200,
        ),
        (
            "a reply to somebody else's request",
            {
                "jsonrpc": "2.0",
                "id": "another-caller",
                "result": {"structuredContent": {"status": "ok"}},
            },
            200,
        ),
    ],
)
def test_replies_that_only_look_healthy_are_rejected(
    mail_like_server: http.server.HTTPServer,
    tmp_path: Path,
    label: str,
    document: dict,
    status: int,
) -> None:
    _Handler.reply = document
    _Handler.reply_status = status
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "not running" in output, f"{label}: {output}"
