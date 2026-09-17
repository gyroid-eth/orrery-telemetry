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
    ProxyConfig,
    ProxyError,
    StdioMcpServer,
    TOOL_DEFINITIONS,
    _dispatch,
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

    def whois(self, **kwargs):
        return self._record(
            "whois", kwargs, {"name": kwargs["agent_name"], "program": "claude-code"}
        )


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


def test_bridge_binding_wins_over_misleading_shell_identity(tmp_path):
    config = ProxyConfig.from_env(
        {
            "AGENTSTACK_MCP_URL": "http://agent-mail.invalid/api/",
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
            "AGENT_NAME": "CocoaPasteur",
            "TMUX": "/tmp/tmux/default,1,0",
            "TMUX_PANE": "%99",
        }
    )
    proxy, _ = _proxy(tmp_path)

    status = proxy.bootstrap("session-example")

    assert not config.is_direct
    assert status["agent_name"] == "Calm-Noether"
    assert "CocoaPasteur" not in json.dumps(status)


def test_missing_bridge_binding_stops_without_guessing_a_name(tmp_path):
    proxy = AgentStackProxy(
        IdentityStore(tmp_path / "identity"),
        SnapshotStore(tmp_path / "snapshot.json"),
        FakeAgentMail(),
        bootstrap_wait_seconds=0,
    )

    with pytest.raises(ProxyError, match="identity is unavailable") as failure:
        proxy.bootstrap("missing-session")

    assert proxy.bound_binding is None
    assert "agent" not in str(failure.value).lower()


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


def test_session_bound_surface_needs_no_caller_identity_after_bootstrap(tmp_path):
    proxy, mail = _proxy(tmp_path)
    proxy.bootstrap("session-example")

    status = _dispatch(proxy, "runtime_status", {})
    inbox = _dispatch(proxy, "fetch_inbox", {"include_bodies": True})
    sent = _dispatch(
        proxy,
        "send_message",
        {"to": ["Quiet-Curie"], "subject": "Result", "body_md": "Done"},
    )
    reserved = _dispatch(proxy, "reserve_files", {"paths": ["src/*.py"]})

    assert status["agent_name"] == "Calm-Noether"
    assert inbox[0]["id"] == 7
    assert sent == {"count": 1}
    assert reserved == {"granted": [], "conflicts": []}
    assert {arguments["agent_name"] for _, arguments in mail.calls} == {
        "Calm-Noether"
    }


def test_unbound_status_fails_closed_without_identity_candidates(tmp_path):
    proxy = AgentStackProxy(
        IdentityStore(tmp_path / "identity"),
        SnapshotStore(tmp_path / "snapshot.json"),
        FakeAgentMail(),
    )

    with pytest.raises(ProxyError, match="bootstrap") as failure:
        _dispatch(proxy, "runtime_status", {})

    assert "CocoaPasteur" not in str(failure.value)


def test_bridge_tools_reject_caller_supplied_identity_after_bootstrap(tmp_path):
    proxy, _ = _proxy(tmp_path)
    proxy.bootstrap("session-example")

    for supplied in (
        {"session_id": "session-example"},
        {"agent_id": None},
        {"project_key": "/workspace/example"},
        {"agent_name": "Calm-Noether"},
    ):
        with pytest.raises(ProxyError, match="caller-supplied runtime identity"):
            _dispatch(proxy, "fetch_inbox", supplied)


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
        "whois",
    }
    assert names == {tool["name"] for tool in TOOL_DEFINITIONS}

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "retire_agent", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    assert "allowlisted" in response["result"]["content"][0]["text"]


@pytest.mark.parametrize("management_name", ("inspect_enrollment", "claim_agent", "recover_agent", "agentstack_enroll"))
def test_operator_enrollment_is_absent_from_proxy_catalog_and_dispatch(tmp_path, management_name):
    proxy, _ = _proxy(tmp_path)
    server = StdioMcpServer(proxy)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert management_name not in {tool["name"] for tool in listed["result"]["tools"]}

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": management_name, "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    assert "allowlisted" in response["result"]["content"][0]["text"]


def test_only_bootstrap_accepts_caller_supplied_runtime_identity():
    by_name = {tool["name"]: tool for tool in TOOL_DEFINITIONS}

    assert by_name["bootstrap"]["inputSchema"]["required"] == ["session_id"]
    assert "session_id" in by_name["bootstrap"]["inputSchema"]["properties"]
    for name, tool in by_name.items():
        if name == "bootstrap":
            continue
        properties = tool["inputSchema"]["properties"]
        assert "session_id" not in properties, name
        assert "agent_id" not in properties, name


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
    assert len(responses[1]["result"]["tools"]) == 9
    status = responses[2]["result"]["structuredContent"]
    assert status["external_id"] == "codex:session-example"
    assert "owner-secret" not in result.stdout


def test_whois_lets_a_child_confirm_a_recipient_name(tmp_path):
    proxy, mail = _proxy(tmp_path)
    proxy.bootstrap("session-example")

    profile = _dispatch(proxy, "whois", {"name": "BraveFaraday"})

    assert profile["name"] == "BraveFaraday"
    name, kwargs = mail.calls[-1]
    assert name == "whois"
    assert kwargs["agent_name"] == "BraveFaraday"
    assert kwargs["project_key"] == "/workspace/example"
    # The owner token travels to the server, never back to the model.
    assert kwargs["registration_token"] == "owner-secret"
    assert "owner-secret" not in json.dumps(profile)
    with pytest.raises(ProxyError):
        _dispatch(proxy, "whois", {"name": ""})


def test_server_error_text_reaches_the_model_with_secrets_redacted():
    from agentstack_codex_app.agent_mail_client import (
        AgentMailError,
        _decode_tool_response,
    )

    unknown = {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": (
                "Error calling tool 'send_message': Unknown recipient "
                "'baravefaraday': looks like a Unix username.\nsecond line"
            )}],
        }
    }
    with pytest.raises(AgentMailError) as excinfo:
        _decode_tool_response(unknown)
    message = str(excinfo.value)
    # The reason is what lets the child correct the name instead of giving up.
    assert "Unknown recipient 'baravefaraday'" in message
    assert "second line" not in message

    token = "A" * 24 + "b8c9d0e1f2a3b4c5d6e7f8a9"
    leaky = {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": f"bad bearer {token} rejected"}],
        }
    }
    with pytest.raises(AgentMailError) as excinfo:
        _decode_tool_response(leaky)
    assert token not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)

    empty = {"result": {"isError": True, "content": []}}
    with pytest.raises(AgentMailError, match="^ORRERY Mail tool call failed$"):
        _decode_tool_response(empty)
