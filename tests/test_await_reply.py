#!/usr/bin/env python3
"""agentstack-await-reply must return the right message, in order, or time out.

Both 2026-08-13 waiting incidents came from agents improvising their own
watchers; this tool is the blessed primitive, so its contract is pinned here:
sender filter, after-id filter, oldest-first hand-over, timeout exit 124, and
unreachable-server exit 1.

Runnable two ways (no third-party dependency required):
    python3 tests/test_await_reply.py
    pytest tests/test_await_reply.py
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import pathlib
import sys
import threading

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL = REPO / "bin" / "agentstack-await-reply"

spec = importlib.util.spec_from_loader("await_reply", loader=None)
await_reply = importlib.util.module_from_spec(spec)
await_reply.__file__ = str(TOOL)
exec(compile(TOOL.read_text(encoding="utf-8"), str(TOOL), "exec"), await_reply.__dict__)


def test_default_project_key_is_the_invocation_directory():
    assert await_reply.DEFAULT_PROJECT_KEY == os.getcwd()


def _serve(batches):
    """One-shot HTTP server: each POST answers with the next inbox batch."""

    class Handler(http.server.BaseHTTPRequestHandler):
        calls = 0

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            index = min(Handler.calls, len(batches) - 1)
            Handler.calls += 1
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "1",
                    "result": {"structuredContent": {"result": batches[index]}},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/api/"


def _run(url, argv, monkeyenv=None):
    import os

    old = os.environ.get("AGENTSTACK_MCP_URL")
    os.environ["AGENTSTACK_MCP_URL"] = url
    try:
        return await_reply.main(argv)
    finally:
        if old is None:
            os.environ.pop("AGENTSTACK_MCP_URL", None)
        else:
            os.environ["AGENTSTACK_MCP_URL"] = old


def test_returns_the_oldest_unseen_message_from_the_sender(capsys=None):
    inbox = [
        {"id": 12, "from": "BlueLake", "subject": "s3", "body_md": "third"},
        {"id": 11, "from": "GreenCastle", "subject": "noise", "body_md": "x"},
        {"id": 10, "from": "BlueLake", "subject": "s1", "body_md": "first"},
    ]
    server, url = _serve([inbox])
    try:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = _run(url, [
                "--agent-name", "Me", "--from", "BlueLake",
                "--after-id", "9", "--timeout", "5", "--interval", "0.2",
            ])
        assert code == 0, code
        message = json.loads(out.getvalue())
        assert message["id"] == 10, message  # oldest unseen, not newest
    finally:
        server.shutdown()


def test_after_id_skips_already_processed_messages():
    inbox = [{"id": 10, "from": "BlueLake", "subject": "s1", "body_md": "seen"}]
    later = [
        {"id": 13, "from": "BlueLake", "subject": "s2", "body_md": "fresh"},
        {"id": 10, "from": "BlueLake", "subject": "s1", "body_md": "seen"},
    ]
    server, url = _serve([inbox, later])
    try:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = _run(url, [
                "--agent-name", "Me", "--from", "BlueLake",
                "--after-id", "10", "--timeout", "5", "--interval", "0.1",
            ])
        assert code == 0, code
        assert json.loads(out.getvalue())["id"] == 13
    finally:
        server.shutdown()


def test_times_out_with_exit_124_when_nothing_matches():
    server, url = _serve([[]])
    try:
        code = _run(url, [
            "--agent-name", "Me", "--from", "BlueLake",
            "--timeout", "0.5", "--interval", "0.1",
        ])
        assert code == 124, code
    finally:
        server.shutdown()


def test_unreachable_server_exits_1():
    code = _run("http://127.0.0.1:9/api/", [
        "--agent-name", "Me", "--timeout", "0.5", "--interval", "0.1",
    ])
    assert code == 1, code


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
