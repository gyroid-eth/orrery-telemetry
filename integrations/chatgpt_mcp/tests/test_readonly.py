"""Isolated tests: no production Dashboard, Mail, tunnel or agents are touched."""
from __future__ import annotations

import contextlib
from copy import deepcopy
import http.client
import json
import os
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orrery_chatgpt_mcp.model import Dashboard, DashboardError, ToolError, invoke, snapshot
from orrery_chatgpt_mcp.server import Config, ConfigurationError, Gateway, Server, handler, pair, read_secret


INSTANCE = "0123456789abcdef0123456789abcdef"
PROJECT = "/fixture/selected"
FRESH = {
    "name": "orrery-telemetry", "version": "test", "api": 39, "runtime_mode": "fresh",
    "mail_instance_id": INSTANCE, "capabilities": {
        "roster": True, "mail": True, "spawn": True, "terminal": True
    },
}
LEGACY = {"name": "orrery-telemetry", "version": "test", "api": 39}
AGENTS_FRESH = {
    "mail_instance_id": INSTANCE, "observation_truncated": False, "agents": [
        {"mail_instance_id": INSTANCE, "mail_agent_id": 7, "name": "QuietBohr",
         "program": "claude-code", "provider": "claude", "model": "opus",
         "running": True, "state": "active",
         "observation": {"state": "observed", "age_ms": 125, "reason": None},
         "cmd": "do not expose", "token": "do not expose", "transcript_path": "/private"},
        {"mail_instance_id": INSTANCE, "mail_agent_id": 8, "name": "LivelyCurie",
         "program": "codex", "running": False, "state": "unknown",
         "observation": {"state": "unknown", "age_ms": None, "reason": "unobserved"}},
        {"mail_instance_id": INSTANCE, "mail_agent_id": 9, "name": "BrightTesla",
         "program": "claude-code", "running": False, "state": "finished",
         "observation": {"state": "observed", "age_ms": 43, "reason": None}},
    ],
}
AGENTS_LEGACY = {"history_days": 30.0, "project_key": PROJECT, "agents": [
    {"name": "QuietBohr", "running": True, "category": "agent",
     "task": "private task", "cmd": "private command", "project_key": PROJECT},
    {"name": "OtherUI", "running": False, "category": "not_agent", "project_key": None},
]}


class FakeDashboard:
    def __init__(self, version=None, agents=None):
        self.version = FRESH.copy() if version is None else version
        self.agents = AGENTS_FRESH.copy() if agents is None else agents
        self.calls = []

    def read(self, route):
        self.calls.append(route)
        if route == "/api/version":
            return self.version
        if route in ("/api/agents", "/api/agents?days=30"):
            return self.agents
        raise AssertionError("unexpected call " + route)


def test_fresh_identity_and_unknown_agents_survive_projection():
    data = snapshot(FakeDashboard(), "fresh")
    assert data.instance == INSTANCE
    assert data.capabilities["roster"] is True
    assert data.capabilities["worktree"] is None
    assert data.agents[0]["mail_agent_id"] == 7
    assert data.agents[1]["running"] is None  # Not observed is not proven stopped.
    assert data.agents[1]["observation"]["reason"] == "unobserved"
    assert "cmd" not in data.agents[0]
    assert "token" not in data.agents[0]
    assert "transcript_path" not in data.agents[0]
    assert "task" not in data.agents[0]


def test_list_defaults_to_all_not_only_running():
    result = invoke("orrery_list_agents", {}, FakeDashboard(), "fresh")
    assert result["total"] == 3
    assert result["count"] == 3
    assert any(a["running"] is None for a in result["agents"])
    running = invoke("orrery_list_agents", {"running_only": True}, FakeDashboard(), "fresh")
    assert [a["name"] for a in running["agents"]] == ["QuietBohr"]
    with pytest.raises(ToolError) as exc:
        invoke("orrery_list_agents", {"running_only": "true"}, FakeDashboard(), "fresh")
    assert exc.value.code == "invalid_arguments"


def test_get_by_exact_mail_identity_and_stale_name_rejected():
    got = invoke("orrery_get_agent", {"mail_instance_id": INSTANCE, "mail_agent_id": 8}, FakeDashboard(), "fresh")
    assert got["agent"]["name"] == "LivelyCurie"
    with pytest.raises(ToolError) as exc:
        invoke("orrery_get_agent",
               {"name": "QuietBohr", "mail_instance_id": INSTANCE, "mail_agent_id": 8},
               FakeDashboard(), "fresh")
    assert exc.value.code == "not_found_or_ambiguous"
    with pytest.raises(ToolError):
        invoke("orrery_get_agent", {"mail_agent_id": 8}, FakeDashboard(), "fresh")


def test_status_reports_mode_and_unknown_coverage():
    status = invoke("orrery_status", {}, FakeDashboard(), "fresh")
    assert status["dashboard"]["runtime_mode"] == "fresh"
    assert status["dashboard"]["mail_instance_id"] == INSTANCE
    assert status["observations"]["unknown_running"] == 1
    assert status["adapter"]["spawn_enabled_on_server"] is False
    assert status["adapter"]["message_enabled_on_server"] is False


@pytest.mark.parametrize("version,mode,roster", [
    (LEGACY, "fresh", AGENTS_FRESH),
    (FRESH, "legacy", AGENTS_FRESH),
    ({"name": "orrery-telemetry", "api": 34, "runtime_mode": "global"}, "legacy", AGENTS_LEGACY),
    ({"name": "orrery-telemetry", "api": 34, "runtime_mode": "global"}, "fresh", AGENTS_FRESH),
    ({**FRESH, "mail_instance_id": "other"}, "fresh", AGENTS_FRESH),
    (LEGACY, "legacy", AGENTS_FRESH),
])
def test_unrecognized_or_mismatched_mode_fails_closed(version, mode, roster):
    with pytest.raises(DashboardError):
        snapshot(FakeDashboard(version, roster), mode, project_key=PROJECT if mode == "legacy" else None)


@pytest.mark.parametrize("changes", [
    {"mail_agent_id": None},
    {"mail_agent_id": 0},
    {"mail_instance_id": "different"},
    {"observation": None},
    {"observation": {"state": "bad"}},
])
def test_invalid_fresh_agent_identity_or_observation_fails_closed(changes):
    rows = [dict(AGENTS_FRESH["agents"][0], **changes)]
    with pytest.raises(DashboardError):
        snapshot(FakeDashboard(agents={**AGENTS_FRESH, "agents": rows}), "fresh")


def test_legacy_requires_explicit_mode_and_does_not_invent_mail_ids():
    data = snapshot(FakeDashboard(LEGACY, AGENTS_LEGACY), "legacy", project_key=PROJECT)
    assert len(data.agents) == 1
    assert data.instance is None
    assert data.agents[0]["mail_agent_id"] is None
    assert data.capabilities["mail"] is None
    assert data.agents[0]["running"] is True
    with pytest.raises(DashboardError):
        snapshot(FakeDashboard(LEGACY, AGENTS_LEGACY), "global")


def test_unknown_tool_raises_before_any_dashboard_read():
    stub = FakeDashboard()
    with pytest.raises(ToolError) as exc:
        invoke("orrery_message_commit", {}, stub, "fresh")
    assert exc.value.code == "unknown_tool"
    assert stub.calls == []


@pytest.mark.parametrize("values", [
    {},
    {"ORRERY_MCP_MODE": "global"},
    {"ORRERY_MCP_MODE": "fresh", "ORRERY_MCP_ENABLE_MESSAGE": "1"},
    {"ORRERY_MCP_MODE": "legacy", "ORRERY_MCP_ENABLE_SPAWN": "1"},
    {"ORRERY_MCP_MODE": "fresh", "ORRERY_MCP_AUTH": "oauth"},
    {"ORRERY_MCP_MODE": "fresh", "ORRERY_MCP_DASHBOARD_URL": "https://example.com"},
    {"ORRERY_MCP_MODE": "fresh", "ORRERY_MCP_DASHBOARD_URL": "http://localhost:8770"},
    {"ORRERY_MCP_MODE": "fresh", "ORRERY_MCP_PORT": "0"},
])
def test_invalid_configuration_refused(values):
    with pytest.raises(ConfigurationError):
        Config.from_env(values)


def test_pair_private_file_and_explicit_rotation(tmp_path):
    config = Config("fresh", "http://127.0.0.1:8770", 8791, tmp_path / "state")
    pair(config)
    token = read_secret(config)
    assert len(token) == 43
    assert stat.S_IMODE((config.state_dir / "pairing.json").stat().st_mode) == 0o600
    with pytest.raises(ConfigurationError):
        pair(config)
    pair(config, rotate=True)
    assert read_secret(config) != token
    gateway = Gateway(config, FakeDashboard())
    assert gateway.path_allowed("/mcp/" + read_secret(config))
    assert not gateway.path_allowed("/mcp/" + token)


def test_pair_refuses_insecure_file_permissions(tmp_path):
    config = Config("legacy", "http://127.0.0.1:8770", 8791, tmp_path / "state")
    pair(config)
    file = config.state_dir / "pairing.json"
    file.chmod(0o644)
    with pytest.raises(ConfigurationError):
        read_secret(config)


def test_dashboard_client_get_only_and_redirect_rejected():
    with pytest.raises(DashboardError):
        Dashboard("http://localhost:8770")
    d = Dashboard("http://127.0.0.1:8770")
    with pytest.raises(DashboardError):
        d.read("/api/spawn")


@contextlib.contextmanager
def fake_http_server(payloads):
    class Api(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = payloads.get(self.path)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            raise AssertionError("adapter must never POST to dashboard")

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Api) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield service.server_address[1]
        finally:
            service.shutdown()
            thread.join(3)


@contextlib.contextmanager
def mcp_http_server(config):
    gateway = Gateway(config)
    with Server(("127.0.0.1", 0), handler(gateway)) as service:
        # Test config uses a port before startup; make it the server's actual port.
        address = service.server_address[1]
        gateway.config = Config(config.mode, config.dashboard_url, address, config.state_dir, config.project_key)
        gateway.hosts = {f"127.0.0.1:{address}", f"localhost:{address}"}
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield address, read_secret(config)
        finally:
            service.shutdown()
            thread.join(3)


def request(port, secret, payload, *, path=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        raw = json.dumps(payload).encode()
        base = {"Content-Type": "application/json", "Accept": "application/json"}
        base.update(headers or {})
        conn.request("POST", path or "/mcp/" + secret, body=raw, headers=base)
        res = conn.getresponse()
        body = res.read()
        return res.status, json.loads(body) if body else None
    finally:
        conn.close()


def test_http_protocol_read_only_and_unknown_commit(tmp_path):
    with fake_http_server({"/api/version": FRESH, "/api/agents": AGENTS_FRESH}) as backend:
        config = Config("fresh", f"http://127.0.0.1:{backend}", 8791, tmp_path / "state")
        pair(config)
        with mcp_http_server(config) as (port, secret):
            code, answer = request(port, secret, {"jsonrpc": "2.0", "id": 1,
                                                  "method": "initialize",
                                                  "params": {"protocolVersion": "2025-06-18"}})
            assert code == 200
            assert answer["result"]["protocolVersion"] == "2025-06-18"
            code, answer = request(port, secret, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert code == 200
            assert [x["name"] for x in answer["result"]["tools"]] == [
                "orrery_status", "orrery_list_agents", "orrery_get_agent"
            ]
            assert all(x["annotations"]["readOnlyHint"] is True for x in answer["result"]["tools"])
            code, answer = request(port, secret, {"jsonrpc": "2.0", "id": 3,
                "method": "tools/call", "params": {"name": "orrery_status", "arguments": {}}})
            assert code == 200
            assert answer["result"]["structuredContent"]["dashboard"]["mail_instance_id"] == INSTANCE
            assert answer["result"]["structuredContent"]["adapter"]["message_enabled_on_server"] is False
            code, answer = request(port, secret, {"jsonrpc": "2.0", "id": 4,
                "method": "tools/call", "params": {"name": "orrery_message_commit", "arguments": {}}})
            assert code == 200
            assert answer["result"]["isError"] is True
            assert answer["result"]["structuredContent"]["error"]["code"] == "unknown_tool"
            code, answer = request(port, secret, {"jsonrpc": "2.0", "id": 5,
                "method": "tools/call", "params": {"name": "orrery_list_agents", "arguments": {}}})
            assert answer["result"]["structuredContent"]["count"] == 3
            assert code == 200
            code, _ = request(port, secret + "wrong", {"jsonrpc": "2.0", "id": 6,
                                                   "method": "tools/list"})
            assert code == 404
            code, _ = request(port, secret, {"jsonrpc": "2.0", "id": 7,
                                            "method": "tools/list"}, headers={"Origin": "https://evil.test"})
            assert code == 403


def test_http_legacy_mode_and_mode_mismatch(tmp_path):
    with fake_http_server({"/api/version": LEGACY, "/api/agents?days=30": AGENTS_LEGACY}) as backend:
        config = Config("legacy", f"http://127.0.0.1:{backend}", 8791, tmp_path / "state", PROJECT)
        pair(config)
        with mcp_http_server(config) as (port, secret):
            code, value = request(port, secret, {"jsonrpc": "2.0", "id": "status",
                "method": "tools/call", "params": {"name": "orrery_status", "arguments": {}}})
            assert code == 200
            assert value["result"]["structuredContent"]["dashboard"]["runtime_mode"] == "legacy"
            gateway = Gateway(Config("fresh", config.dashboard_url, port, config.state_dir))
            response = gateway.rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                    "params": {"name": "orrery_status"}})
            assert response[1]["result"]["structuredContent"]["error"]["code"] == "dashboard_unavailable"


@pytest.mark.parametrize("api", [34, 35, 36, 37, 38, 39])
def test_supported_fresh_generations_still_require_fresh_contract(api):
    version = {**FRESH, "api": api}
    data = snapshot(FakeDashboard(version, AGENTS_FRESH), "fresh")
    assert data.scope["kind"] == "mail_instance"
    assert data.scope["includes_retired"] is True
    assert data.scope["includes_unobserved"] is True


@pytest.mark.parametrize("api", [999, 40, 33, True, "39", None])
def test_unknown_fresh_generations_are_rejected_before_roster_read(api):
    backend = FakeDashboard({**FRESH, "api": api}, AGENTS_FRESH)
    with pytest.raises(DashboardError):
        snapshot(backend, "fresh")
    assert backend.calls == ["/api/version"]


@pytest.mark.parametrize("mode", ["legacy", "fresh"])
@pytest.mark.parametrize("tool", ["orrery_status", "orrery_list_agents", "orrery_get_agent"])
def test_api38_annotations_and_tasks_never_extend_public_projection(mode, tool):
    version, roster = deepcopy((LEGACY, AGENTS_LEGACY) if mode == "legacy" else (FRESH, AGENTS_FRESH))
    for row in roster["agents"]:
        row.update(annot={"role": "private-marker-role", "emoji": "private-marker-emoji",
                          "group": "private-marker-group", "task": "private-marker-override"},
                   task="private-marker-registered-task", future_field="private-marker-future")
    backend = FakeDashboard(version, roster)
    args = {"name": "QuietBohr"} if tool == "orrery_get_agent" else {}
    result = invoke(tool, args, backend, mode, project_key=PROJECT if mode == "legacy" else None)
    assert "private-marker" not in json.dumps(result)
    agents = result.get("agents", [result["agent"]] if "agent" in result else [])
    for agent in agents:
        assert set(agent) == {"name", "mail_instance_id", "mail_agent_id", "provider", "program",
                              "model", "category", "running", "state", "retired", "observation", "last_active"}
        assert set(agent["observation"]) == {"state", "age_ms", "reason"}


@pytest.mark.parametrize("name", ["A" * 65, "A" * 64 + "/escape", "A" * 64 + "\n"])
def test_long_fresh_names_are_rejected_not_shortened(name):
    rows = [{**AGENTS_FRESH["agents"][0], "name": name}]
    with pytest.raises(DashboardError):
        snapshot(FakeDashboard(agents={**AGENTS_FRESH, "agents": rows}), "fresh")


@pytest.mark.parametrize('raw',['gpt-6.1-sol','gpt-7-nova-preview2'])
def test_model_projection_prefers_complete_raw_identifier_to_display(raw):
    roster=deepcopy(AGENTS_LEGACY)
    roster['agents'][0].update(model='GPT-'+raw[4:].title(),model_raw=raw)
    data=snapshot(FakeDashboard(LEGACY,roster),'legacy',project_key=PROJECT)
    assert data.agents[0]['model']==raw
