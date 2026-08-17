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
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "session-start-reminder.sh"


class _Handler(http.server.BaseHTTPRequestHandler):
    """A stand-in for the real server: MCP path only, no health route."""

    status_for_mcp = 406

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") in {"/mcp", "/api"}:
            self.send_response(self.status_for_mcp)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture()
def mail_like_server() -> object:
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


@pytest.mark.parametrize("mcp_status", (200, 405, 406))
def test_a_server_that_only_serves_mcp_is_reported_as_running(
    mail_like_server: http.server.HTTPServer,
    tmp_path: Path,
    mcp_status: int,
) -> None:
    _Handler.status_for_mcp = mcp_status
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "server is running" in output, output
    assert "not running" not in output, output


def test_nothing_listening_is_still_reported_as_not_running(tmp_path: Path) -> None:
    """The null case: the probe must still be able to say no.

    A probe that answers "running" unconditionally would pass the test above.
    """
    # Port 1 on loopback: nothing serves it, and connecting fails immediately.
    output = _run_hook("http://127.0.0.1:1/mcp", tmp_path)
    assert "not running" in output, output


def test_a_server_answering_only_404_is_reported_as_not_running(
    mail_like_server: http.server.HTTPServer,
    tmp_path: Path,
) -> None:
    """Something else on the port is not this service."""
    _Handler.status_for_mcp = 404
    host, port = mail_like_server.server_address[:2]
    output = _run_hook(f"http://{host}:{port}/mcp", tmp_path)
    assert "not running" in output, output
