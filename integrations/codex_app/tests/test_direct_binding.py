"""Direct (Bridge-less) proxy binding for tmux-spawned children — defect D, part 2.

The Codex App path learns its binding from the Bridge daemon's identity store.
A Claude Code child spawned into tmux has no Bridge, so it had no way to reach
ORRERY Mail as itself and hit:

    fetch_inbox requires registration_token for agent 'Red-Euler', ...

With a direct binding the launcher states the identity, the proxy loads the
child's owner token, and the child calls tools with no token in its context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from agentstack_codex_app.mcp_server import (
    AgentStackProxy,
    ProxyConfig,
    ProxyError,
    _dispatch,
    direct_token_path,
    load_direct_owner_token,
)
from agentstack_codex_app.agent_mail_client import AgentMailClient


AGENT = "Red-Euler"
PROJECT = "/workspace/example"
TOKEN = "child-owner-token"
NAME_SCOPED_TOOLS = {
    "fetch_inbox",
    "whois",
    "acknowledge_message",
    "file_reservation_paths",
    "renew_file_reservations",
    "release_file_reservations",
}


class RecordingTransport:
    def __init__(self, *, advertises_owner_token: bool) -> None:
        self.advertises_owner_token = advertises_owner_token
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if payload["method"] == "tools/list":
            properties = (
                {"registration_token": {}}
                if self.advertises_owner_token
                else {}
            )
            return {
                "result": {
                    "tools": [
                        {
                            "name": tool,
                            "inputSchema": {"properties": properties},
                        }
                        for tool in NAME_SCOPED_TOOLS
                    ]
                }
            }
        params = payload["params"]
        tool = params["name"]
        arguments = params.get("arguments", {})
        self.calls.append((tool, arguments))
        if tool == "send_message" and not arguments.get("sender_token"):
            return {"result": {"isError": True, "content": [{
                "type": "text",
                "text": f"Error calling tool '{tool}': {tool} requires sender_token",
            }]}}
        if (
            self.advertises_owner_token
            and tool in NAME_SCOPED_TOOLS
            and arguments.get("registration_token") != TOKEN
        ):
            return {"result": {"isError": True, "content": [{
                "type": "text",
                "text": f"Error calling tool '{tool}': owner token required",
            }]}}
        body: Any = [] if tool == "fetch_inbox" else {"ok": True}
        return {"result": {"content": [{"type": "text", "text": json.dumps(body)}]}}


def _proxy(tmp_path: Path) -> tuple[AgentStackProxy, RecordingTransport]:
    from agentstack_codex_app.identity_store import IdentityStore
    from agentstack_codex_app.snapshot import SnapshotStore

    # The installer-pinned server advertises owner tokens on name-scoped tools.
    transport = RecordingTransport(advertises_owner_token=True)
    proxy = AgentStackProxy(
        IdentityStore(tmp_path / "identity"),
        SnapshotStore(tmp_path / "snapshot.json"),
        AgentMailClient(transport),
    )
    proxy.bind_direct(agent_name=AGENT, project_key=PROJECT, owner_token=TOKEN)
    return proxy, transport


def test_config_reads_a_direct_binding_from_the_environment(tmp_path):
    config = ProxyConfig.from_env({
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_PROXY_AGENT_NAME": AGENT,
        "AGENTSTACK_PROJECT_KEY": PROJECT,
        "AGENTSTACK_PROXY_TOKEN_FILE": str(tmp_path / "tok"),
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
    })
    assert config.is_direct
    assert config.agent_name == AGENT
    assert config.token_file == tmp_path / "tok"


def test_bridge_mode_is_unchanged_when_no_direct_env_is_set(tmp_path):
    config = ProxyConfig.from_env({
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
    })
    assert not config.is_direct
    assert config.agent_name is None


def test_agent_name_without_a_project_key_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ProxyConfig.from_env({
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
            "AGENTSTACK_PROXY_AGENT_NAME": AGENT,
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
        })


def test_directly_bound_tools_need_no_bootstrap_and_no_session_id(tmp_path):
    proxy, transport = _proxy(tmp_path)
    # Exactly what a child does: ask for its own inbox, nothing else supplied.
    result = _dispatch(proxy, "fetch_inbox", {"limit": 5})
    assert result == []
    tool, arguments = transport.calls[-1]
    assert tool == "fetch_inbox"
    assert arguments["agent_name"] == AGENT
    assert arguments["registration_token"] == TOKEN


def test_an_unbound_proxy_still_refuses(tmp_path):
    """The null case: without a binding, tools must not work at all."""
    from agentstack_codex_app.identity_store import IdentityStore
    from agentstack_codex_app.snapshot import SnapshotStore

    proxy = AgentStackProxy(
        IdentityStore(tmp_path / "identity"),
        SnapshotStore(tmp_path / "snapshot.json"),
        AgentMailClient(RecordingTransport(advertises_owner_token=True)),
    )
    assert proxy.bound_session_id is None
    with pytest.raises((ProxyError, TypeError)):
        _dispatch(proxy, "fetch_inbox", {"limit": 5})


def test_a_child_cannot_reach_another_agents_binding(tmp_path):
    proxy, _ = _proxy(tmp_path)
    with pytest.raises(ProxyError):
        _dispatch(proxy, "fetch_inbox", {"session_id": "direct-Other-Bohr"})


# A real Codex thread id. The launcher binds the synthetic "direct-<name>", so
# these two can never be equal — which is why every call from a real child
# failed until this was fixed.
CODEX_THREAD_ID = "019fc57e-4f21-7c6a-9c3e-2b4d5e6f7a8b"


def test_a_child_that_passes_its_own_session_id_is_not_locked_out(tmp_path):
    """The tester's finding: a Codex child sends its real thread id.

    It is naming itself, not asking for another runtime, and a directly bound
    process serves one agent anyway. Before the fix this raised "requested
    runtime does not match the process binding" on every coordination call, so
    a real child could neither read its inbox nor report back.
    """
    proxy, transport = _proxy(tmp_path)
    result = _dispatch(proxy, "fetch_inbox", {
        "session_id": CODEX_THREAD_ID, "limit": 5,
    })
    assert result == []
    _, arguments = transport.calls[-1]
    assert arguments["agent_name"] == AGENT
    assert arguments["registration_token"] == TOKEN


def test_bootstrap_from_a_real_session_id_reports_the_binding(tmp_path):
    """The child's second symptom: rebinding failed too.

    Told its runtime did not match, a child reasonably tries `bootstrap`, and
    got "already bound to another runtime" — a dead end. A directly bound
    process should just report the binding it already has.
    """
    proxy, _ = _proxy(tmp_path)
    status = _dispatch(proxy, "bootstrap", {"session_id": CODEX_THREAD_ID})
    assert status["agent_name"] == AGENT
    assert status["project_key"] == PROJECT


def test_send_message_works_from_a_real_session_id(tmp_path):
    """Reading the inbox and reporting back are the two halves that were lost."""
    proxy, transport = _proxy(tmp_path)
    _dispatch(proxy, "send_message", {
        "session_id": CODEX_THREAD_ID,
        "to": ["Sturdy-Koch"],
        "subject": "done",
        "body_md": "finished",
    })
    tool, arguments = transport.calls[-1]
    assert tool == "send_message"
    assert arguments["sender_name"] == AGENT
    assert arguments["sender_token"] == TOKEN


def test_token_path_defaults_to_the_shared_runtime_layout(tmp_path):
    config = ProxyConfig.from_env({
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_PROXY_AGENT_NAME": AGENT,
        "AGENTSTACK_PROJECT_KEY": PROJECT,
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
    })
    # runtime_dir_from_env appends codex-app; the shell helpers write the token
    # one level up, next to the other agent_token_* files.
    assert direct_token_path(config) == tmp_path / "agent_token_Red-Euler"


def test_token_path_sanitises_the_name_like_the_shell_helper(tmp_path):
    """Must match ags_registration_token_file: tr -c 'A-Za-z0-9_.-' '_'."""
    config = ProxyConfig.from_env({
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_PROXY_AGENT_NAME": "Odd Name/With:Chars",
        "AGENTSTACK_PROJECT_KEY": PROJECT,
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
    })
    assert direct_token_path(config).name == "agent_token_Odd_Name_With_Chars"


def test_missing_or_empty_token_is_a_clear_error(tmp_path):
    config = ProxyConfig.from_env({
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_PROXY_AGENT_NAME": AGENT,
        "AGENTSTACK_PROJECT_KEY": PROJECT,
        "AGENTSTACK_PROXY_TOKEN_FILE": str(tmp_path / "absent"),
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
    })
    with pytest.raises(ProxyError):
        load_direct_owner_token(config)

    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    config_empty = ProxyConfig.from_env({
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_PROXY_AGENT_NAME": AGENT,
        "AGENTSTACK_PROJECT_KEY": PROJECT,
        "AGENTSTACK_PROXY_TOKEN_FILE": str(empty),
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
    })
    with pytest.raises(ProxyError):
        load_direct_owner_token(config_empty)


def test_documented_arguments_are_accepted_when_they_match(tmp_path):
    """Agents are taught to pass project_key/agent_name; that must not error.

    The tester hit "unexpected keyword argument" on a child's first call
    because the proxy takes identity from its binding instead.
    """
    proxy, transport = _proxy(tmp_path)
    result = _dispatch(proxy, "fetch_inbox", {
        "project_key": PROJECT, "agent_name": AGENT, "limit": 5,
    })
    assert result == []
    _, arguments = transport.calls[-1]
    # The binding still decides what goes upstream.
    assert arguments["agent_name"] == AGENT
    assert arguments["registration_token"] == TOKEN


def test_mismatched_identity_arguments_are_refused(tmp_path):
    """Accepting the arguments must not become a way to act as someone else."""
    proxy, _ = _proxy(tmp_path)
    with pytest.raises(ProxyError):
        _dispatch(proxy, "fetch_inbox", {"agent_name": "Other-Bohr"})
    with pytest.raises(ProxyError):
        _dispatch(proxy, "fetch_inbox", {"project_key": "/somewhere/else"})


def test_bootstrap_naming_itself_as_agent_id_reports_the_binding(tmp_path):
    """A Codex child called bootstrap with its own name as agent_id.

    That built a subagent id which never matched the direct binding, returned
    "MCP process is already bound to another runtime", and the child then
    declined to report to its parent (2026-09-25).
    """
    proxy, _ = _proxy(tmp_path)
    status = _dispatch(
        proxy, "bootstrap", {"session_id": CODEX_THREAD_ID, "agent_id": AGENT}
    )
    assert status["agent_name"] == AGENT


def test_send_message_naming_itself_as_agent_id_is_delivered(tmp_path):
    proxy, transport = _proxy(tmp_path)
    _dispatch(proxy, "send_message", {
        "session_id": CODEX_THREAD_ID,
        "agent_id": AGENT,
        "to": ["Sturdy-Koch"],
        "subject": "done",
        "body_md": "finished",
    })
    tool, arguments = transport.calls[-1]
    assert tool == "send_message"
    assert arguments["sender_name"] == AGENT


def test_agent_id_naming_another_agent_is_refused(tmp_path):
    proxy, _ = _proxy(tmp_path)
    with pytest.raises(ProxyError, match="names another agent"):
        _dispatch(
            proxy, "bootstrap", {"session_id": CODEX_THREAD_ID, "agent_id": "Other-Agent"}
        )


# --- the launcher's parent reaches lineage (WSL2 report, 2026-10-01) -------


def test_config_reads_the_parent_the_launcher_named(tmp_path):
    config = ProxyConfig.from_env({
        "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
        "AGENTSTACK_PROXY_AGENT_NAME": AGENT,
        "AGENTSTACK_PROJECT_KEY": PROJECT,
        "AGENTSTACK_PROXY_PARENT_AGENT": "Blue-Lake",
        "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
    })
    assert config.parent_agent == "Blue-Lake"


@pytest.mark.parametrize("environment", [
    {"AGENTSTACK_PROXY_PARENT_AGENT": "Parent Agent"},
    {"AGENTSTACK_PROXY_PARENT_AGENT": "../Other"},
    # A parent is only meaningful for a direct binding.
    {"AGENTSTACK_PROXY_AGENT_NAME": "", "AGENTSTACK_PROXY_PARENT_AGENT": "Blue-Lake"},
])
def test_a_parent_the_proxy_cannot_state_is_refused_at_startup(tmp_path, environment):
    with pytest.raises(ValueError):
        ProxyConfig.from_env({
            "AGENTSTACK_MCP_URL": "http://127.0.0.1:8765/mcp",
            "AGENTSTACK_PROXY_AGENT_NAME": AGENT,
            "AGENTSTACK_PROJECT_KEY": PROJECT,
            "AGENTSTACK_RUNTIME_DIR": str(tmp_path),
            **environment,
        })


def test_a_launched_child_reports_its_parent_not_a_root(tmp_path):
    from agentstack_codex_app.identity_store import IdentityStore
    from agentstack_codex_app.snapshot import SnapshotStore

    proxy = AgentStackProxy(
        IdentityStore(tmp_path / "identity"),
        SnapshotStore(tmp_path / "snapshot.json"),
        AgentMailClient(RecordingTransport(advertises_owner_token=True)),
    )
    proxy.bind_direct(agent_name=AGENT, project_key=PROJECT, owner_token=TOKEN, parent_agent="Blue-Lake")
    # Codex passes its own thread id to bootstrap; the answer is the same.
    booted = _dispatch(proxy, "bootstrap", {"session_id": "019a-codex-thread"})
    status = _dispatch(proxy, "runtime_status", {})
    assert booted["lineage"] == status["lineage"]
    assert status["lineage"]["kind"] == "child"
    assert status["lineage"]["parent_agent"] == "Blue-Lake"


def test_a_standalone_direct_binding_is_still_a_root(tmp_path):
    proxy, _transport = _proxy(tmp_path)
    lineage = _dispatch(proxy, "runtime_status", {})["lineage"]
    assert lineage["kind"] == "root"
    assert lineage["parent_agent"] is None


# --- a conversation that used raw ORRERY Mail, resumed on the proxy ---------
#
# WSL2 report, 2026-10-01, problem 2: a conversation first run against the raw
# orrery-mail server was resumed from the dashboard on this proxy. The model
# kept calling the same tool names the way it had before: send_message with
# `project_key` and `sender_name`, whois with the target in `agent_name`.
# fetch_inbox worked; send_message failed with
#   AgentStackProxy.send_message() got an unexpected keyword argument 'sender_name'
# and whois refused the target as a mismatched caller identity.


def _call(proxy, name, arguments):
    from agentstack_codex_app.mcp_server import StdioMcpServer

    return StdioMcpServer(proxy)._call_tool(name, arguments)


def test_raw_send_message_from_the_bound_agent_is_sent(tmp_path):
    proxy, transport = _proxy(tmp_path)
    result = _call(proxy, "send_message", {
        "project_key": PROJECT, "sender_name": AGENT, "to": ["Blue-Lake"],
        "subject": "report", "body_md": "done", "sender_token": "whatever-the-model-had",
    })
    assert not result["isError"], result
    sent = [arguments for tool, arguments in transport.calls if tool == "send_message"]
    assert sent and sent[0]["sender_name"] == AGENT and sent[0]["to"] == ["Blue-Lake"]
    # The proxy authenticates with its own credential, never one the model supplied.
    assert sent[0]["sender_token"] == TOKEN


def test_raw_send_message_as_someone_else_is_refused_with_the_reason(tmp_path):
    proxy, transport = _proxy(tmp_path)
    result = _call(proxy, "send_message", {
        "project_key": PROJECT, "sender_name": "Blue-Lake", "to": ["Green-Castle"],
        "subject": "s", "body_md": "b",
    })
    assert result["isError"]
    text = result["content"][0]["text"]
    assert "sender_name" in text and AGENT in text
    assert not [tool for tool, _ in transport.calls if tool == "send_message"]


def test_raw_whois_treats_agent_name_as_the_agent_to_look_up(tmp_path):
    proxy, transport = _proxy(tmp_path)
    recording = transport.__class__.__call__

    def answer_with_profile(self, payload):
        reply = recording(self, payload)
        if payload.get("params", {}).get("name") == "whois":
            profile = {"id": 9, "name": payload["params"]["arguments"]["agent_name"]}
            return {"result": {"content": [{"type": "text", "text": json.dumps(profile)}]}}
        return reply

    transport.__class__ = type("ProfileTransport", (transport.__class__,), {"__call__": answer_with_profile})
    result = _call(proxy, "whois", {
        "project_key": PROJECT, "agent_name": "Blue-Lake",
        "include_recent_commits": False, "commit_limit": 3,
    })
    assert not result["isError"], result
    looked_up = [arguments for tool, arguments in transport.calls if tool == "whois"]
    assert looked_up and looked_up[0]["agent_name"] == "Blue-Lake"


def test_raw_fetch_inbox_never_forwards_a_model_supplied_token(tmp_path):
    proxy, transport = _proxy(tmp_path)
    result = _call(proxy, "fetch_inbox", {
        "project_key": PROJECT, "agent_name": AGENT, "limit": 5,
        "registration_token": "stale-token-from-history",
    })
    assert not result["isError"], result
    fetched = [arguments for tool, arguments in transport.calls if tool == "fetch_inbox"]
    assert fetched and fetched[0]["registration_token"] == TOKEN


def test_an_unknown_argument_is_refused_with_the_accepted_list(tmp_path):
    proxy, transport = _proxy(tmp_path)
    result = _call(proxy, "send_message", {
        "to": ["Blue-Lake"], "subject": "s", "body_md": "b",
        "attachment_paths": ["/tmp/x.png"],
    })
    assert result["isError"]
    text = result["content"][0]["text"]
    assert "attachment_paths" in text
    assert "body_md" in text and "subject" in text  # what it does accept
    assert "unexpected keyword argument" not in text
    assert not [tool for tool, _ in transport.calls if tool == "send_message"]


def test_a_launcher_binding_does_not_claim_to_be_registering(tmp_path):
    """No Bridge snapshot ever exists for a direct binding, so the state it
    reported never changed from "registering" however long one waited."""
    proxy, _transport = _proxy(tmp_path)
    status = _dispatch(proxy, "runtime_status", {})
    assert status["state"] != "registering"
    assert status["state"] == "bound"


def test_whois_never_reads_agent_name_as_the_caller(tmp_path):
    proxy, transport = _proxy(tmp_path)
    result = _call(proxy, "whois", {"name": "Blue-Lake", "agent_name": "Green-Castle"})
    assert result["isError"]
    assert "name and agent_name" in result["content"][0]["text"]
    assert not [tool for tool, _ in transport.calls if tool == "whois"]
