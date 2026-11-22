"""Opt-in real tunnel-client transport test; no hosted tunnel or API key.

Set ORRERY_MCP_TEST_TUNNEL_CLIENT to the binary to run its isolated dev proxy.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

import pytest

from orrery_chatgpt_mcp.server import Config, pair
from test_readonly import AGENTS_FRESH, FRESH, fake_http_server, mcp_http_server, request


def post(url, method, params=None, identity=1):
    target = urlsplit(url)
    assert target.scheme == "http" and target.hostname == "127.0.0.1"
    call = {"jsonrpc": "2.0", "method": method}
    if identity is not None:
        call["id"] = identity
    if params is not None:
        call["params"] = params
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
    try:
        connection.request("POST", target.path, json.dumps(call), {
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        })
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None
    finally:
        connection.close()


def test_real_tunnel_client_initialize_discovery_and_read_only(tmp_path):
    binary = os.environ.get("ORRERY_MCP_TEST_TUNNEL_CLIENT")
    if not binary:
        pytest.skip("set ORRERY_MCP_TEST_TUNNEL_CLIENT for an isolated real tunnel-client test")
    assert Path(binary).is_file(), "configured tunnel-client binary does not exist"
    with fake_http_server({"/api/version": FRESH, "/api/agents": AGENTS_FRESH}) as backend:
        config = Config("fresh", f"http://127.0.0.1:{backend}", 8791, tmp_path / "state")
        pair(config)
        with mcp_http_server(config) as (port, secret):
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CONTROL_PLANE_", "MCP_", "TUNNEL_", "ORRERY_MCP_"))}
            env["HOME"] = str(tmp_path / "home")
            record = tmp_path / "connection.json"
            # Only ephemeral fixture secrets can appear in the child process's log.
            with (tmp_path / "proxy.log").open("w") as log:
                child = subprocess.Popen([
                    binary, "dev", "proxy", "--backend", "go", "--listen", "127.0.0.1:0",
                    "--mcp-server-url", f"http://127.0.0.1:{port}/mcp/{secret}",
                    "--profile-dir", str(tmp_path / "profiles"), "--url-file", str(record),
                    "--readiness-timeout", "15s", "--duration", "45s",
                ], stdout=log, stderr=subprocess.STDOUT, env=env)
                try:
                    deadline = time.monotonic() + 20
                    while not record.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    assert record.exists(), "isolated tunnel-client did not become ready"
                    url = json.loads(record.read_text())["mcp_url"]
                    status, answer = post(url, "initialize", {
                        "protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "orrery-readonly-regression", "version": "1"},
                    })
                    assert status == 200 and answer["result"]["protocolVersion"] == "2025-11-25"
                    assert post(url, "notifications/initialized", identity=None) == (202, None)
                    status, answer = post(url, "tools/list", identity=2)
                    assert status == 200
                    assert {tool["name"] for tool in answer["result"]["tools"]} == {
                        "orrery_status", "orrery_list_agents", "orrery_get_agent",
                    }
                    status, answer = post(url, "tools/call", {"name": "orrery_status", "arguments": {}}, 3)
                    assert status == 200
                    view = answer["result"]["structuredContent"]
                    assert view["adapter"] == {"read_only": True, "spawn_enabled_on_server": False,
                                               "message_enabled_on_server": False}
                    assert view["observations"]["unknown_running"] == 1
                    status, answer = post(url, "tools/call", {"name": "orrery_message_commit", "arguments": {}}, 4)
                    assert status == 200 and answer["result"]["isError"] is True
                    assert answer["result"]["structuredContent"]["error"]["code"] == "unknown_tool"
                    # The real forwarding succeeded with local Host and the full secret path.
                    # Direct untrusted Host, Origin and missing-path requests still fail.
                    rpc = {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
                    assert request(port, secret, rpc, headers={"Host": "external.invalid"})[0] == 403
                    assert request(port, secret, rpc, headers={"Origin": "https://external.invalid"})[0] == 403
                    assert request(port, secret, rpc, path="/mcp")[0] == 404
                finally:
                    if child.poll() is None:
                        child.terminate()
                    try:
                        child.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
