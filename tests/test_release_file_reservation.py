#!/usr/bin/env python3
"""Behavior tests for the file-reservation release hook family."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CHECK_HOOK = ROOT / "hooks" / "check-file-reservation.sh"
RELEASE_HOOK = ROOT / "hooks" / "release-file-reservation.sh"
INVALIDATE_HOOK = ROOT / "hooks" / "invalidate-release-debounce.sh"
RELEASE_ALL_HOOK = ROOT / "hooks" / "release-all-reservations.sh"
WORKER = ROOT / "hooks" / "release-file-reservation-worker.py"
RESOLVER = ROOT / "hooks" / "resolve-agent-name.sh"
TEMPLATE = ROOT / "hooks" / "settings.template.json"


def _mcp_result(*, renewed: int | None = None, released: int | None = None) -> bytes:
    structured: dict[str, int] = {}
    if renewed is not None:
        structured["renewed"] = renewed
    if released is not None:
        structured["released"] = released
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "test",
            "result": {"isError": False, "structuredContent": structured},
        }
    ).encode()


class _Server:
    def __init__(self, responder: Callable[[int], tuple[int, bytes]] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder = responder or (lambda _count: (200, _mcp_result(released=1)))
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                parent.requests.append(
                    {
                        "headers": {
                            key.lower(): value for key, value in self.headers.items()
                        },
                        "json": json.loads(body),
                    }
                )
                status, response = parent.responder(len(parent.requests))
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


class ReleaseHookTests(unittest.TestCase):
    def make_environment(
        self,
        root: Path,
        url: str,
        *,
        agent_name: str | None = "PluckyEinstein",
        install_worker: bool = False,
        install_resolver: bool = False,
        grace: str = "0",
        bearer: str = "",
    ) -> tuple[dict[str, str], Path, Path]:
        project = root / "project"
        runtime = root / "runtime"
        hooks = root / "isolated-hooks"
        project.mkdir()
        runtime.mkdir()
        hooks.mkdir()
        if install_worker:
            shutil.copy2(WORKER, hooks / WORKER.name)
        if install_resolver:
            shutil.copy2(RESOLVER, hooks / RESOLVER.name)

        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith("AGENTSTACK_") or name in {
                "AGENT_NAME",
                "PROJECT_KEY",
                "MCP_URL",
                "MCP_AGENT_MAIL_TOKEN",
                "FILE_RESERVATION_RELEASE_GRACE_SECONDS",
                "TMUX",
                "TMUX_PANE",
            }:
                env.pop(name, None)
        env.update(
            {
                "HOME": str(root / "home"),
                "AGENTSTACK_PROJECT_KEY": str(project),
                "AGENTSTACK_PROTECTED_ROOTS": str(project),
                "AGENTSTACK_HOOKS_DIR": str(hooks),
                "AGENTSTACK_RUNTIME_DIR": str(runtime),
                "AGENTSTACK_MCP_URL": url,
                "AGENTSTACK_MAIL_HTTP_BEARER_MODE": (
                    "enabled" if bearer else "disabled"
                ),
                "AGENTSTACK_RELEASE_GRACE_SECONDS": grace,
            }
        )
        if agent_name is not None:
            env["AGENT_NAME"] = agent_name
        if bearer:
            env["MCP_AGENT_MAIL_TOKEN"] = bearer
        return env, project, runtime

    @staticmethod
    def payload(
        file_path: Path | str | None,
        *,
        session_id: str = "session-1",
        response: Any = None,
    ) -> str:
        document: dict[str, Any] = {
            "session_id": session_id,
            "tool_input": {},
            "tool_response": {"success": True} if response is None else response,
        }
        if file_path is not None:
            document["tool_input"]["file_path"] = str(file_path)
        return json.dumps(document)

    @staticmethod
    def run_hook(
        hook: Path,
        payload: str,
        env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(hook)],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            cwd=cwd,
            check=False,
            timeout=10,
        )

    def test_null_cases_do_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            root = Path(directory)
            env, project, runtime = self.make_environment(root, server.url)
            outside = root / "outside.md"
            for payload in (self.payload(None), self.payload(outside)):
                with self.subTest(payload=payload):
                    result = self.run_hook(RELEASE_HOOK, payload, env, project)
                    self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(server.requests, [])
            self.assertFalse((runtime / "release-failures.log").exists())

    def test_success_uses_the_same_project_and_paths_as_the_guard(self) -> None:
        def respond(count: int) -> tuple[int, bytes]:
            if count == 1:
                return 200, _mcp_result(renewed=1)
            return 200, _mcp_result(released=1)

        with tempfile.TemporaryDirectory() as directory, _Server(respond) as server:
            root = Path(directory)
            env, project, _runtime = self.make_environment(root, server.url)
            target = project / "notes" / "one.md"
            target.parent.mkdir()
            payload = self.payload(target)

            checked = self.run_hook(CHECK_HOOK, payload, env, project)
            released = self.run_hook(RELEASE_HOOK, payload, env, project)

        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(len(server.requests), 2)
        guard = server.requests[0]
        release = server.requests[1]
        self.assertEqual(guard["json"]["params"]["name"], "renew_file_reservations")
        self.assertEqual(release["json"]["params"]["name"], "release_file_reservations")
        guard_args = guard["json"]["params"]["arguments"]
        release_args = release["json"]["params"]["arguments"]
        self.assertEqual(release_args["project_key"], guard_args["project_key"])
        self.assertEqual(release_args["agent_name"], guard_args["agent_name"])
        self.assertEqual(release_args["paths"], guard_args["paths"])
        self.assertEqual(
            release["headers"]["accept"], "application/json, text/event-stream"
        )

    def test_failed_or_blocked_tool_never_releases(self) -> None:
        failures = (
            {"error": "edit failed"},
            {"status": "blocked"},
            "Error: write refused",
            {"success": False},
        )
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            root = Path(directory)
            env, project, _runtime = self.make_environment(root, server.url)
            target = project / "note.md"
            for response in failures:
                with self.subTest(response=response):
                    result = self.run_hook(
                        RELEASE_HOOK,
                        self.payload(target, response=response),
                        env,
                        project,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(server.requests, [])

    def test_rereservation_invalidates_the_sleeping_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            root = Path(directory)
            env, project, runtime = self.make_environment(
                root, server.url, install_worker=True, grace="1"
            )
            target = project / "note.md"
            armed = self.run_hook(RELEASE_HOOK, self.payload(target), env, project)
            self.assertEqual(armed.returncode, 0, armed.stderr)
            self.assertTrue(any((runtime / "file_release_debounce").iterdir()))

            reserve_payload = json.dumps(
                {
                    "session_id": "session-1",
                    "tool_input": {
                        "agent_name": "PluckyEinstein",
                        "project_key": str(project),
                        "paths": ["note.md"],
                    },
                }
            )
            invalidated = self.run_hook(
                INVALIDATE_HOOK, reserve_payload, env, project
            )
            self.assertEqual(invalidated.returncode, 0, invalidated.stderr)
            time.sleep(1.4)

            self.assertEqual(server.requests, [])
            self.assertEqual(list((runtime / "file_release_debounce").iterdir()), [])

    def test_missing_worker_falls_back_to_immediate_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            root = Path(directory)
            env, project, runtime = self.make_environment(
                root, server.url, install_worker=False, grace="90"
            )
            result = self.run_hook(
                RELEASE_HOOK, self.payload(project / "note.md"), env, project
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(server.requests), 1)
            log = (runtime / "release-failures.log").read_text(encoding="utf-8")
            self.assertIn("worker-missing", log)
            self.assertIn("fallback=immediate", log)

    def test_release_uses_the_same_legacy_bearer_decision_as_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            root = Path(directory)
            env, project, _runtime = self.make_environment(
                root, server.url, bearer="legacy-http-token"
            )
            result = self.run_hook(
                RELEASE_HOOK, self.payload(project / "note.md"), env, project
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                server.requests[0]["headers"]["authorization"],
                "Bearer legacy-http-token",
            )

    def test_session_index_supplies_the_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            root = Path(directory)
            env, project, runtime = self.make_environment(
                root,
                server.url,
                agent_name=None,
                install_resolver=True,
            )
            index = runtime / "session_index"
            index.mkdir()
            (index / "binding.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "binding_kind": "self",
                        "session_id": "desktop-session",
                        "agent_name": "IndexedCurie",
                        "registered_by": "IndexedCurie",
                        "project_key": str(project),
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_hook(
                RELEASE_HOOK,
                self.payload(project / "note.md", session_id="desktop-session"),
                env,
                project,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = server.requests[0]["json"]["params"]["arguments"]
            self.assertEqual(arguments["agent_name"], "IndexedCurie")

    def test_http_406_and_connection_failure_are_logged(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server(
            lambda _count: (406, b'{"error":"Accept required"}')
        ) as server:
            root = Path(directory)
            env, project, runtime = self.make_environment(root, server.url)
            result = self.run_hook(
                RELEASE_HOOK, self.payload(project / "note.md"), env, project
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log = (runtime / "release-failures.log").read_text(encoding="utf-8")
            self.assertIn("HTTP 406", log)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, project, runtime = self.make_environment(
                root, f"http://127.0.0.1:{port}/mcp"
            )
            result = self.run_hook(
                RELEASE_HOOK, self.payload(project / "note.md"), env, project
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log = (runtime / "release-failures.log").read_text(encoding="utf-8")
            self.assertIn("transport:", log)

    def test_session_end_releases_all_without_retiring_the_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            root = Path(directory)
            env, project, _runtime = self.make_environment(root, server.url)
            result = self.run_hook(
                RELEASE_ALL_HOOK,
                json.dumps({"session_id": "session-1"}),
                env,
                project,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(server.requests), 1)
            request = server.requests[0]["json"]
            self.assertEqual(request["params"]["name"], "release_file_reservations")
            self.assertEqual(
                request["params"]["arguments"],
                {"project_key": str(project), "agent_name": "PluckyEinstein"},
            )

    def test_template_wires_all_release_hooks(self) -> None:
        template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        hooks = template["hooks"]
        post = json.dumps(hooks["PostToolUse"])
        pre = json.dumps(hooks["PreToolUse"])
        end = json.dumps(hooks["SessionEnd"])
        self.assertIn("release-file-reservation.sh", post)
        self.assertIn('"matcher": "Edit|Write"', post)
        self.assertIn("invalidate-release-debounce.sh", pre)
        for prefix in (
            "mcp__orrery-mail__",
            "mcp__mcp-agent-mail__",
            "mcp__agent_mail__",
        ):
            for tool in (
                "file_reservation_paths",
                "macro_file_reservation_cycle",
                "renew_file_reservations",
            ):
                self.assertIn(prefix + tool, pre)
        self.assertIn("release-all-reservations.sh", end)


if __name__ == "__main__":
    unittest.main()
