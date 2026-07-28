"""Every per-agent proxy call must authenticate (tester report, defect D).

The Bridge proxy holds each child's owner token out-of-band, but several tool
paths resolved that token and then dropped it, so a token-strict agent-mail
answered:

    fetch_inbox requires registration_token for agent 'White-Koch',
    unless this MCP session has already authenticated as that agent.

which is why a delegated child could not read its own inbox.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from agentstack_codex_app.agent_mail_client import AgentMailClient, AgentMailError


OWNER_TOKEN = "owner-token-value"
AGENT = "White-Koch"
PROJECT = "/workspace/example"

# Tools the stock server gates on the agent's own registration_token.
GATED_TOOLS = {
    "fetch_inbox",
    "whois",
    "acknowledge_message",
    "file_reservation_paths",
    "renew_file_reservations",
    "release_file_reservations",
}


class StrictTransport:
    """Stands in for a token-strict agent-mail server."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        params = payload["params"]
        tool = params["name"]
        arguments = params.get("arguments", {})
        self.seen.append((tool, arguments))

        if tool in GATED_TOOLS and not arguments.get("registration_token"):
            return {
                "result": {
                    "isError": True,
                    "content": [{
                        "type": "text",
                        "text": (
                            f"Error calling tool '{tool}': {tool} requires "
                            f"registration_token for agent '{AGENT}', unless this "
                            "MCP session has already authenticated as that agent."
                        ),
                    }],
                }
            }

        if tool == "fetch_inbox":
            body: Any = []
        elif tool == "whois":
            body = {"name": AGENT, "retired_at": None}
        else:
            body = {"ok": True}
        return {
            "result": {"content": [{"type": "text", "text": json.dumps(body)}]}
        }


@pytest.fixture()
def strict() -> tuple[AgentMailClient, StrictTransport]:
    transport = StrictTransport()
    return AgentMailClient(transport), transport


def test_fetch_inbox_authenticates(strict):
    client, transport = strict
    assert client.fetch_inbox(
        project_key=PROJECT, agent_name=AGENT, registration_token=OWNER_TOKEN
    ) == []
    assert transport.seen[-1][1]["registration_token"] == OWNER_TOKEN


def test_fetch_inbox_without_a_token_still_fails_loudly(strict):
    """The null case: the guard must be real, not satisfied by any argument."""
    client, _ = strict
    with pytest.raises(AgentMailError):
        client.fetch_inbox(project_key=PROJECT, agent_name=AGENT)


def test_whois_authenticates(strict):
    client, transport = strict
    profile = client.whois(
        project_key=PROJECT, agent_name=AGENT, registration_token=OWNER_TOKEN
    )
    assert profile["name"] == AGENT
    assert transport.seen[-1][1]["registration_token"] == OWNER_TOKEN


def test_every_gated_reservation_and_ack_path_authenticates(strict):
    client, transport = strict
    client.acknowledge_message(
        project_key=PROJECT, agent_name=AGENT, message_id=1,
        registration_token=OWNER_TOKEN,
    )
    client.reserve_files(
        project_key=PROJECT, agent_name=AGENT, paths=["a.py"],
        registration_token=OWNER_TOKEN,
    )
    client.renew_reservations(
        project_key=PROJECT, agent_name=AGENT, registration_token=OWNER_TOKEN,
    )
    client.release_reservations(
        project_key=PROJECT, agent_name=AGENT, registration_token=OWNER_TOKEN,
    )
    tools = [tool for tool, _ in transport.seen]
    assert tools == [
        "acknowledge_message",
        "file_reservation_paths",
        "renew_file_reservations",
        "release_file_reservations",
    ]
    for _, arguments in transport.seen:
        assert arguments["registration_token"] == OWNER_TOKEN


def test_proxy_passes_the_owner_token_it_resolves():
    """The proxy must not resolve the token and then discard it."""
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent
    text = (source / "src" / "agentstack_codex_app" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    # runtime_status makes no upstream call, so it alone may discard the token.
    assert text.count("binding, _ = self._resolve(") == 1, (
        "a per-agent proxy call is still dropping the owner token"
    )
    assert text.count("registration_token=owner_token") >= 6
