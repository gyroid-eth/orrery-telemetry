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
RESOLVER = ROOT / "hooks" / "resolve-agent-name.sh"


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


class _OneShotZeroServer:
    """Return renewed=0 once, then make the retry fail at transport."""

    def __init__(self) -> None:
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._serve_once, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.listener.getsockname()
        return f"http://{host}:{port}/mcp"

    def _serve_once(self) -> None:
        connection, _address = self.listener.accept()
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            header, body = request.split(b"\r\n\r\n", 1)
            content_length = 0
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            while len(body) < content_length:
                body += connection.recv(4096)
            response = _mcp_result(0)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(response)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + response
            )
        self.listener.close()

    def __enter__(self) -> _OneShotZeroServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            self.listener.close()
        except OSError:
            pass
        self.thread.join(timeout=2)


def _mcp_result(renewed: Any) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "reservation-guard",
            "result": {"structuredContent": {"renewed": renewed}},
        }
    ).encode()


class ReservationHookTests(unittest.TestCase):
    def run_hook_with_unconfigured_product_environment(
        self, root: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run with no AgentStack product selectors and a definitive zero renewal."""
        home = root / "home"
        workspace = home / "workspace"
        workspace.mkdir(parents=True)
        workspace = workspace.resolve()

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            "if [ \"${1-}\" = \"-c\" ]; then\n"
            f"    exec {json.dumps(os.sys.executable)} \"$@\"\n"
            "fi\n"
            "cat >/dev/null\n"
            "printf '%s\\n' 'HOOK_RENEWED: 0'\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith("AGENTSTACK_") or name in {
                "PROJECT_KEY",
                "MCP_URL",
                "MCP_AGENT_MAIL_TOKEN",
                "FILE_RESERVATION_RENEW_SECONDS",
                "FILE_RESERVATION_RETRY_DELAY_SECONDS",
                "TMUX",
                "TMUX_PANE",
            }:
                env.pop(name, None)
        env.update(
            {
                "AGENT_NAME": "PluckyEinstein",
                "HOME": str(home),
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            }
        )
        payload = json.dumps(
            {"tool_input": {"file_path": str(workspace / "note.md")}}
        )
        return subprocess.run(
            ["/bin/bash", str(HOOK)],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            cwd=workspace,
            check=False,
            timeout=10,
        )

    def run_hook(
        self,
        url: str,
        root: Path,
        *,
        bearer: str = "",
        agent_name: str | None = "PluckyEinstein",
        tmux_pane: str | None = None,
        metadata_agent: str | None = None,
        install_resolver: bool = False,
        resolver_bytes: bytes | None = None,
        tmux_session_agent: str | None = None,
        file_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runtime = root / "runtime"
        hooks = root / "isolated-hooks"
        runtime.mkdir(exist_ok=True)
        hooks.mkdir(exist_ok=True)
        if install_resolver:
            (hooks / "resolve-agent-name.sh").write_bytes(
                RESOLVER.read_bytes() if resolver_bytes is None else resolver_bytes
            )
        (runtime / "agent_token_PluckyEinstein").write_text(
            "sentinel-owner-token\n", encoding="utf-8"
        )
        env = os.environ.copy()
        for name in ("AGENT_NAME", "TMUX", "TMUX_PANE"):
            env.pop(name, None)
        env.update(
            {
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
        if agent_name is not None:
            env["AGENT_NAME"] = agent_name
        if tmux_pane is not None:
            env["TMUX_PANE"] = tmux_pane
        if metadata_agent is not None:
            if tmux_pane is None:
                raise AssertionError("metadata_agent requires tmux_pane")
            pane_key = tmux_pane.replace("%", "_")
            (runtime / f"agent_name_{pane_key}").write_text(
                metadata_agent + "\n", encoding="utf-8"
            )
        if tmux_session_agent is not None:
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(exist_ok=True)
            fake_tmux = fake_bin / "tmux"
            fake_tmux.write_text(
                "#!/bin/sh\nprintf '%s\\n' " + tmux_session_agent + "\n",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        payload = json.dumps(
            {"tool_input": {"file_path": str(file_path or root / "note.md")}}
        )
        return subprocess.run(
            ["/bin/bash", str(HOOK)],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=10,
        )

    def test_unconfigured_defaults_protect_vault_and_block_missing_reservation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_hook_with_unconfigured_product_environment(
                Path(directory)
            )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("FILE RESERVATION REQUIRED", result.stderr)

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

    def test_transport_failure_after_definitive_zero_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _OneShotZeroServer() as server:
            result = self.run_hook(server.url, Path(directory))

        self.assertEqual(result.returncode, 2)
        self.assertIn("became unreachable", result.stderr)

    def test_unresolved_identity_blocks_without_using_ambient_tmux_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(
                server.url,
                Path(directory),
                agent_name=None,
                install_resolver=True,
                tmux_session_agent="WrongAgent",
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("AGENT IDENTITY REQUIRED", result.stderr)
        self.assertEqual(server.requests, [])

    def test_exact_pane_metadata_identity_can_confirm_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(
                server.url,
                Path(directory),
                agent_name=None,
                tmux_pane="%77",
                metadata_agent="PluckyEinstein",
                install_resolver=True,
                tmux_session_agent="PluckyEinstein",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = server.requests[0]["json"]["params"]["arguments"]
        self.assertEqual(arguments["agent_name"], "PluckyEinstein")

    def test_stale_pane_metadata_conflict_blocks_without_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(
                server.url,
                Path(directory),
                agent_name=None,
                tmux_pane="%77",
                metadata_agent="StaleAgent",
                install_resolver=True,
                tmux_session_agent="PluckyEinstein",
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("AGENT IDENTITY CONFLICT", result.stderr)
        self.assertEqual(server.requests, [])

    def test_exact_targeted_tmux_session_without_metadata_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(
                server.url,
                Path(directory),
                agent_name=None,
                tmux_pane="%77",
                install_resolver=True,
                tmux_session_agent="PluckyEinstein",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = server.requests[0]["json"]["params"]["arguments"]
        self.assertEqual(arguments["agent_name"], "PluckyEinstein")

    def test_placeholder_targeted_session_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(
                server.url,
                Path(directory),
                agent_name=None,
                tmux_pane="%77",
                install_resolver=True,
                tmux_session_agent="warm-123",
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("AGENT IDENTITY REQUIRED", result.stderr)
        self.assertEqual(server.requests, [])

    def test_all_environment_placeholders_block_before_http(self) -> None:
        for placeholder in (
            "pending-123",
            "warm-123",
            "claimed-123",
            "mail-watcher",
        ):
            with self.subTest(placeholder=placeholder), tempfile.TemporaryDirectory() as directory, _Server(
                lambda _count: (200, _mcp_result(1))
            ) as server:
                result = self.run_hook(
                    server.url,
                    Path(directory),
                    agent_name=placeholder,
                    install_resolver=True,
                )

            self.assertEqual(result.returncode, 2)
            self.assertIn("AGENT IDENTITY REQUIRED", result.stderr)
            self.assertEqual(server.requests, [])

    def test_placeholder_validator_mutation_is_observable(self) -> None:
        original = RESOLVER.read_bytes()
        mutated = original.replace(
            b'""|pending-*|warm-*|claimed-*|mail-watcher',
            b'""|pending-*|claimed-*|mail-watcher',
        )
        self.assertNotEqual(mutated, original)

        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            result = self.run_hook(
                server.url,
                Path(directory),
                agent_name="warm-123",
                install_resolver=True,
                resolver_bytes=mutated,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(server.requests), 1)

    def test_unresolved_identity_outside_protected_root_still_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (200, _mcp_result(1))
        ) as server:
            root = Path(directory)
            result = self.run_hook(
                server.url,
                root,
                agent_name=None,
                install_resolver=True,
                tmux_session_agent="WrongAgent",
                file_path=root.parent / f"outside-{root.name}.md",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.requests, [])

    def test_rejection_after_definitive_zero_still_blocks(self) -> None:
        def respond(count: int) -> tuple[int, bytes]:
            return (200, _mcp_result(0)) if count == 1 else (500, b'{"error":"down"}')

        with tempfile.TemporaryDirectory() as directory, _Server(respond) as server:
            result = self.run_hook(server.url, Path(directory))

        self.assertEqual(result.returncode, 2)
        self.assertIn("rejected", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
