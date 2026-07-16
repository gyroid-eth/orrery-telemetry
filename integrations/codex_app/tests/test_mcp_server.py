from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentstack_codex_app.identity_store import IdentityStore, build_binding
from agentstack_codex_app.mcp_server import (
    AgentStackProxy,
    ProxyError,
    StdioMcpServer,
    TOOL_DEFINITIONS,
)
from agentstack_codex_app.snapshot import SnapshotStore, runtime_record


class FakeAgentMail:
    def __init__(self):
        self.calls = []

    def _record(self, name, kwargs, result):
        self.calls.append((name, kwargs))
        return result

    def fetch_inbox(self, **kwargs):
        return self._record("fetch_inbox", kwargs, [{"id": 7, "subject": "Review"}])

    def send_message(self, **kwargs):
        return self._record("send_message", kwargs, {"count": 1})

    def acknowledge_message(self, **kwargs):
        return self._record(
            "acknowledge_message", kwargs, {"message_id": kwargs["message_id"]}
        )

    def reserve_files(self, **kwargs):
        return self._record("reserve_files", kwargs, {"granted": [], "conflicts": []})

    def renew_reservations(self, **kwargs):
        return self._record("renew_reservations", kwargs, {"renewed": 1})

    def release_reservations(self, **kwargs):
        return self._record("release_reservations", kwargs, {"released": 1})


def _save_identity(
    store: IdentityStore,
    *,
    session_id="session-example",
    agent_id=None,
    agent_name="Calm-Noether",
):
    binding = build_binding(
        session_id=session_id,
        agent_id=agent_id,
        agent_name=agent_name,
        project_key="/workspace/example",
    )
    store.save(binding)
    store.store_owner_token(binding["external_id"], "owner-secret")
    return binding


def _proxy(tmp_path):
    identities = IdentityStore(tmp_path / "identity")
    binding = _save_identity(identities)
    snapshots = SnapshotStore(tmp_path / "snapshot.json")
    snapshots.upsert(
        runtime_record(
            binding,
            {"model": "gpt-example", "cwd": "/workspace/example"},
            state="working",
        )
    )
    mail = FakeAgentMail()
    return AgentStackProxy(identities, snapshots, mail), mail


def test_bootstrap_binds_process_and_runtime_status_never_exposes_token(tmp_path):
    proxy, _ = _proxy(tmp_path)
    status = proxy.bootstrap("session-example")

    assert status["external_id"] == "codex:session-example"
    assert status["state"] == "working"
    assert status["lineage"] == {
        "kind": "root",
        "root_external_id": "codex:session-example",
        "parent_external_id": None,
    }
    assert "owner-secret" not in json.dumps(status)
    with pytest.raises(ProxyError, match="already bound"):
        proxy.bootstrap("other-session")


def test_subagent_bootstrap_waits_for_bridge_observed_pair_and_parent(tmp_path):
    identities = IdentityStore(tmp_path / "identity")
    _save_identity(identities)
    snapshots = SnapshotStore(tmp_path / "snapshot.json")
    mail = FakeAgentMail()
    created = False

    def bridge_catches_up(_seconds):
        nonlocal created
        if not created:
            _save_identity(
                identities,
                agent_id="child-example",
                agent_name="Quiet-Curie",
            )
            created = True

    proxy = AgentStackProxy(
        identities,
        snapshots,
        mail,
        bootstrap_wait_seconds=0.2,
        sleeper=bridge_catches_up,
    )
    status = proxy.bootstrap("session-example", "child-example")
    assert status["parent_external_id"] == "codex:session-example"
    assert status["lineage"]["kind"] == "subagent"


def test_subagent_bootstrap_rejects_unobserved_agent_id(tmp_path):
    proxy, _ = _proxy(tmp_path)
    proxy.bootstrap_wait_seconds = 0
    with pytest.raises(ProxyError, match="has not observed"):
        proxy.bootstrap("session-example", "invented-child")


def test_allowlisted_tools_inject_bound_identity_and_owner_token(tmp_path):
    proxy, mail = _proxy(tmp_path)
    proxy.bootstrap("session-example")

    assert proxy.fetch_inbox("session-example", include_bodies=True)[0]["id"] == 7
    assert proxy.send_message(
        "session-example",
        to=["Quiet-Curie"],
        subject="Result",
        body_md="Done",
    ) == {"count": 1}
    proxy.acknowledge_message("session-example", 7)
    proxy.reserve_files("session-example", ["src/*.py"], reason="P2")
    proxy.renew_reservations("session-example", file_reservation_ids=[11])
    proxy.release_reservations("session-example")

    for _, arguments in mail.calls:
        assert arguments["project_key"] == "/workspace/example"
        assert arguments["agent_name"] == "Calm-Noether"
    send = next(args for name, args in mail.calls if name == "send_message")
    assert send["registration_token"] == "owner-secret"


def test_proxy_rejects_cross_binding_and_unsafe_reservation_paths(tmp_path):
    proxy, _ = _proxy(tmp_path)
    proxy.bootstrap("session-example")
    with pytest.raises(ProxyError, match="does not match"):
        proxy.fetch_inbox("session-example", agent_id="invented-child")
    with pytest.raises(ProxyError, match="project-relative"):
        proxy.reserve_files("session-example", ["/private/file"])
    with pytest.raises(ProxyError, match="project-relative"):
        proxy.reserve_files("session-example", ["src/../private"])


def test_stdio_server_lists_only_allowlisted_tools_and_rejects_passthrough(tmp_path):
    proxy, _ = _proxy(tmp_path)
    server = StdioMcpServer(proxy)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {
        "bootstrap",
        "fetch_inbox",
        "send_message",
        "acknowledge_message",
        "reserve_files",
        "renew_reservations",
        "release_reservations",
        "runtime_status",
    }
    assert names == {tool["name"] for tool in TOOL_DEFINITIONS}

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "whois", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    assert "allowlisted" in response["result"]["content"][0]["text"]


def test_mcp_server_script_runs_directly_without_plugin_root_env(tmp_path):
    runtime = tmp_path / "runtime"
    identities = IdentityStore(runtime / "identity")
    _save_identity(identities)
    script = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agentstack_codex_app"
        / "mcp_server.py"
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "bootstrap",
                "arguments": {"session_id": "session-example"},
            },
        },
    ]
    environment = os.environ.copy()
    environment.pop("PLUGIN_ROOT", None)
    environment.pop("PLUGIN_DATA", None)
    environment["AGENTSTACK_CODEX_APP_RUNTIME_DIR"] = str(runtime)
    environment["AGENTSTACK_MCP_URL"] = "http://agent-mail.invalid/api/"
    result = subprocess.run(
        [sys.executable, str(script)],
        input="\n".join(json.dumps(item) for item in requests) + "\n",
        text=True,
        capture_output=True,
        env=environment,
        timeout=3,
    )
    assert result.returncode == 0
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "agentstack"
    assert len(responses[1]["result"]["tools"]) == 8
    status = responses[2]["result"]["structuredContent"]
    assert status["external_id"] == "codex:session-example"
    assert "owner-secret" not in result.stdout
