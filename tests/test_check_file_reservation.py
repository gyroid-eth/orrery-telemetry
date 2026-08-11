#!/usr/bin/env python3
"""Behavior tests for the fail-closed file-reservation hook boundary."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "check-file-reservation.sh"


class _Server:
    def __init__(self, responder: Callable[[int], tuple[int, bytes]]) -> None:
        self.requests: list[dict[str, Any]] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                parent.requests.append(
                    {
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "json": json.loads(body),
                    }
                )
                status, response = responder(len(parent.requests))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/mcp"

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()


def _mcp_result(renewed: Any) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "reservation-guard",
            "result": {"structuredContent": {"renewed": renewed}},
        }
    ).encode()


class ReservationHookTests(unittest.TestCase):
    def run_hook(
        self, url: str, root: Path, *, bearer: str = ""
    ) -> subprocess.CompletedProcess[str]:
        runtime = root / "runtime"
        hooks = root / "isolated-hooks"
        runtime.mkdir(exist_ok=True)
        hooks.mkdir(exist_ok=True)
        (runtime / "agent_token_PluckyEinstein").write_text(
            "sentinel-owner-token\n", encoding="utf-8"
        )
        env = os.environ.copy()
        env.update(
            {
                "AGENT_NAME": "PluckyEinstein",
                "AGENTSTACK_PROJECT_KEY": str(root),
                "AGENTSTACK_PROTECTED_ROOTS": str(root),
                "AGENTSTACK_HOOKS_DIR": str(hooks),
                "AGENTSTACK_RUNTIME_DIR": str(runtime),
                "AGENTSTACK_MCP_URL": url,
                "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled" if not bearer else "enabled",
                "MCP_AGENT_MAIL_TOKEN": bearer,
                "FILE_RESERVATION_RETRY_DELAY_SECONDS": "0",
            }
        )
        payload = json.dumps({"tool_input": {"file_path": str(root / "note.md")}})
        return subprocess.run(
            ["/bin/bash", str(HOOK)],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=10,
        )

    def test_existing_reservation_passes_with_accept_and_without_owner_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(server.url, Path(directory), bearer="legacy-http-token")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(
            request["headers"]["accept"], "application/json, text/event-stream"
        )
        self.assertEqual(request["headers"]["authorization"], "Bearer legacy-http-token")
        arguments = request["json"]["params"]["arguments"]
        self.assertNotIn("registration_token", arguments)
        self.assertEqual(request["json"]["params"]["name"], "renew_file_reservations")

    def test_reachable_server_without_bearer_can_confirm_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(server.url, Path(directory))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("authorization", server.requests[0]["headers"])

    def test_missing_reservation_blocks_and_never_auto_acquires(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(0))
        ) as server:
            result = self.run_hook(server.url, Path(directory))

        self.assertEqual(result.returncode, 2)
        self.assertIn("FILE RESERVATION REQUIRED", result.stderr)
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(
            {request["json"]["params"]["name"] for request in server.requests},
            {"renew_file_reservations"},
        )
        self.assertTrue(
            all(
                "registration_token" not in request["json"]["params"]["arguments"]
                for request in server.requests
            )
        )

    def test_http_rejection_blocks_instead_of_failing_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (406, b'{"error":"Accept required"}')
        ) as server:
            result = self.run_hook(server.url, Path(directory))

        self.assertEqual(result.returncode, 2)
        self.assertIn("HTTP 406", result.stderr)

    def test_mcp_rejection_blocks_instead_of_failing_open(self) -> None:
        response = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "reservation-guard",
                "error": {"code": -32602, "message": "unexpected argument"},
            }
        ).encode()
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, response)
        ) as server:
            result = self.run_hook(server.url, Path(directory))

        self.assertEqual(result.returncode, 2)
        self.assertIn("MCP error", result.stderr)

    def test_is_error_result_and_schema_mismatch_block(self) -> None:
        responses = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "reservation-guard",
                    "result": {"isError": True, "structuredContent": {"renewed": 1}},
                }
            ).encode(),
            *[
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "reservation-guard",
                        "result": {
                            "isError": invalid_is_error,
                            "structuredContent": {"renewed": 1},
                        },
                    }
                ).encode()
                for invalid_is_error in ("true", 1, {"truthy": True}, None)
            ],
            _mcp_result("1"),
            b"not-json",
        ]
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory, _Server(
                lambda _count, response=response: (200, response)
            ) as server:
                result = self.run_hook(server.url, Path(directory))
            self.assertEqual(result.returncode, 2)

    def test_initial_transport_unreachable_is_the_only_fail_open(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_hook(f"http://127.0.0.1:{port}/mcp", Path(directory))

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejection_after_definitive_zero_still_blocks(self) -> None:
        def respond(count: int) -> tuple[int, bytes]:
            return (200, _mcp_result(0)) if count == 1 else (500, b'{"error":"down"}')

        with tempfile.TemporaryDirectory() as directory, _Server(respond) as server:
            result = self.run_hook(server.url, Path(directory))

        self.assertEqual(result.returncode, 2)
        self.assertIn("rejected", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
