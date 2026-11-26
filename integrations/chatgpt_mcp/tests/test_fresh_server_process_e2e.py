"""Actual fresh MCP subprocess + SDK over private HTTP with isolated fresh Mail.

The ordinary in-process gateway fixture cannot prove the `fresh_server serve`
entrypoint starts, loads its policy, or answers an external MCP SDK client.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import socket
import subprocess
import sys
import threading
import time

from fastmcp import Client

from orrery_chatgpt_mcp.server import Config, pair, read_secret
from test_fresh_message_e2e import ROOT, live, stored
from test_readonly import FRESH


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _dashboard(instance: str):
    state = {"instance": instance, "reads": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["reads"].append(self.path)
            if self.path == "/api/version":
                payload = {**FRESH, "mail_instance_id": state["instance"]}
            elif self.path == "/api/agents":
                payload = {
                    "mail_instance_id": state["instance"],
                    "observation_truncated": False,
                    "agents": [],
                }
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_port, state
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def _startup_summary(process: subprocess.Popen, log_path: Path) -> str:
    """Report only fixed, non-secret signals from a failed private startup."""
    output = log_path.read_text(errors="replace")
    return (
        "private fresh MCP did not listen"
        f" (child_running={process.poll() is None},"
        f" startup_refused={'startup refused' in output},"
        f" listener_reported={'fresh message MCP listening' in output},"
        f" log_nonempty={bool(output)})"
    )


@contextmanager
def _fresh_process(case, dashboard_port: int):
    port = _unused_port()
    cfg = Config("fresh", f"http://127.0.0.1:{dashboard_port}", port,
                 case.root / "external-mcp-state")
    pair(cfg)
    private_url = f"http://127.0.0.1:{port}/mcp/{read_secret(cfg)}"
    log_path = case.root / "external-mcp.log"
    env = {**case.env,
           "ORRERY_MCP_MODE": "fresh",
           "ORRERY_MCP_ENABLE_MESSAGE": "1",
           "ORRERY_MCP_ENABLE_SPAWN": "0",
           "ORRERY_MCP_PORT": str(port),
           "ORRERY_MCP_STATE_DIR": str(cfg.state_dir),
           "ORRERY_MCP_FRESH_MESSAGE_POLICY": str(case.policy),
           "ORRERY_MCP_DASHBOARD_URL": cfg.dashboard_url}
    with log_path.open("w") as log:
        log_path.chmod(0o600)
        process = subprocess.Popen(
            [sys.executable, "-m", "orrery_chatgpt_mcp.fresh_server", "serve"],
            cwd=case.root, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert process.poll() is None, _startup_summary(process, log_path)
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise AssertionError(_startup_summary(process, log_path))
            yield private_url
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            assert process.poll() is not None
            output = log_path.read_text()
            assert read_secret(cfg) not in output
            assert case.args["body_md"] not in output
            assert "registration_token" not in output


def test_startup_diagnostics_redact_private_log(tmp_path):
    secret = "never-include-private-pairing-secret"
    body = "never-include-private-mail-body"
    log_path = tmp_path / "startup.log"
    log_path.write_text(
        f"{secret}\n{body}\nstartup refused\n",
        encoding="utf-8",
    )

    class FakeProcess:
        def poll(self):
            return None

    message = _startup_summary(FakeProcess(), log_path)
    assert secret not in message and body not in message
    assert "child_running=True" in message
    assert "startup_refused=True" in message
    assert "listener_reported=False" in message
    assert "log_nonempty=True" in message


def test_real_fresh_server_process_sdk_and_mail_receipt(live):
    with _dashboard(live.instance) as (dashboard_port, _):
        with _fresh_process(live, dashboard_port) as url:
            async def check():
                async with Client(url) as client:
                    tools = {tool.name: tool for tool in await client.list_tools()}
                    assert set(tools) == {
                        "orrery_status", "orrery_list_agents", "orrery_get_agent",
                        "orrery_message_preview", "orrery_message_commit",
                        "orrery_message_status",
                    }
                    assert tools["orrery_message_commit"].annotations.readOnlyHint is False
                    assert tools["orrery_message_status"].annotations.readOnlyHint is True
                    status = (await client.call_tool("orrery_status", {})).data
                    assert status["dashboard"]["mail_instance_id"] == live.instance
                    assert status["adapter"]["message_enabled_on_server"] is True
                    preview = (await client.call_tool(
                        "orrery_message_preview", live.args)).data
                    assert preview["message"]["body_md"] == live.args["body_md"]
                    assert stored(live)[0] == []
                    request = {key: preview[key] for key in ("preview_id", "content_digest")}
                    request["idempotency_key"] = live.args["idempotency_key"]
                    accepted = (await client.call_tool("orrery_message_commit", request)).data
                    checked = (await client.call_tool(
                        "orrery_message_status",
                        {"idempotency_key": live.args["idempotency_key"]})).data
                    assert accepted == checked
                    assert accepted["status"] == "accepted"
                    assert accepted["execution_status"] == "unobserved"
                    assert (await client.call_tool("orrery_message_commit", request)).data == accepted

            asyncio.run(check())
    rows, recipients = stored(live)
    assert len(rows) == 1
    assert rows[0][1:] == (
        live.people[0]["agent_id"], live.args["subject"], live.args["body_md"])
    assert recipients == [(rows[0][0], live.people[1]["agent_id"])]


def test_real_fresh_server_process_rejects_changed_dashboard_instance(live):
    with _dashboard(live.instance) as (dashboard_port, dashboard_state):
        with _fresh_process(live, dashboard_port) as url:
            async def check():
                async with Client(url) as client:
                    preview = (await client.call_tool(
                        "orrery_message_preview", live.args)).data
                    assert preview["status"] == "previewed"
                    dashboard_state["instance"] = "f" * 32
                    request = {key: preview[key] for key in ("preview_id", "content_digest")}
                    request["idempotency_key"] = live.args["idempotency_key"]
                    refused = await client.call_tool("orrery_message_commit", request, raise_on_error=False)
                    assert refused.is_error is True
                    assert refused.data["error"]["code"] == "message_dashboard_mismatch"
                    before = list(dashboard_state["reads"])
                    cached = await client.call_tool("orrery_message_status", {
                        "idempotency_key": live.args["idempotency_key"],
                    })
                    assert cached.data["status"] == "previewed"
                    assert dashboard_state["reads"] == before
            asyncio.run(check())
    assert stored(live) == ([], [])
