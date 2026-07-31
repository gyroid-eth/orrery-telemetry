"""Direct (Bridge-less) proxy binding for tmux-spawned children — defect D, part 2.

The Codex App path learns its binding from the Bridge daemon's identity store.
A Claude Code child spawned into tmux has no Bridge, so it had no way to reach
agent-mail as itself and hit:

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


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        params = payload["params"]
        tool = params["name"]
        arguments = params.get("arguments", {})
        self.calls.append((tool, arguments))
        if not arguments.get("registration_token"):
            return {"result": {"isError": True, "content": [{
                "type": "text",
                "text": f"Error calling tool '{tool}': {tool} requires registration_token",
            }]}}
        body: Any = [] if tool == "fetch_inbox" else {"ok": True}
        return {"result": {"content": [{"type": "text", "text": json.dumps(body)}]}}


def _proxy(tmp_path: Path) -> tuple[AgentStackProxy, RecordingTransport]:
    from agentstack_codex_app.identity_store import IdentityStore
    from agentstack_codex_app.snapshot import SnapshotStore

    transport = RecordingTransport()
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
        AgentMailClient(RecordingTransport()),
    )
    assert proxy.bound_session_id is None
    with pytest.raises((ProxyError, TypeError)):
        _dispatch(proxy, "fetch_inbox", {"limit": 5})


def test_a_child_cannot_reach_another_agents_binding(tmp_path):
    proxy, _ = _proxy(tmp_path)
    with pytest.raises(ProxyError):
        _dispatch(proxy, "fetch_inbox", {"session_id": "direct-Other-Bohr"})


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
