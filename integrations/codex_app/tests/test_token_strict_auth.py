"""Owner credentials follow the live name-scoped agent-mail schema.

The Bridge proxy resolves each agent's owner token out-of-band. Stock
agent-mail builds disagree on whether inbox, whois, acknowledgement, and
reservation tools accept ``registration_token``. The client must obey the
schema advertised by the server that actually answers.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from agentstack_codex_app.agent_mail_client import AgentMailClient


OWNER_TOKEN = "owner-token-value"
AGENT = "White-Koch"
PROJECT = "/workspace/example"

NAME_SCOPED_TOOLS = {
    "fetch_inbox",
    "whois",
    "acknowledge_message",
    "file_reservation_paths",
    "renew_file_reservations",
    "release_file_reservations",
}


class VersionedTransport:
    """Model either generation and reject calls that violate its schema."""

    def __init__(self, *, advertises_owner_token: bool = False) -> None:
        self.advertises_owner_token = advertises_owner_token
        self.seen: list[tuple[str, Mapping[str, Any]]] = []

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
        self.seen.append((tool, arguments))

        if (
            tool in NAME_SCOPED_TOOLS
            and self.advertises_owner_token
            and arguments.get("registration_token") != OWNER_TOKEN
        ):
            return {
                "result": {
                    "isError": True,
                    "content": [{
                        "type": "text",
                        "text": "registration_token required",
                    }],
                }
            }
        if (
            tool in NAME_SCOPED_TOOLS
            and not self.advertises_owner_token
            and "registration_token" in arguments
        ):
            return {
                "result": {
                    "isError": True,
                    "content": [{
                        "type": "text",
                        "text": "Unexpected keyword argument registration_token",
                    }],
                }
            }

        if tool == "fetch_inbox":
            body: Any = []
        elif tool == "whois":
            body = {"name": AGENT, "retired_at": None}
        elif tool == "register_agent":
            body = {"name": AGENT, "registration_token": OWNER_TOKEN}
        else:
            body = {"ok": True}
        return {
            "result": {"content": [{"type": "text", "text": json.dumps(body)}]}
        }


@pytest.mark.parametrize(
    "advertises_owner_token",
    [
        pytest.param(True, id="pinned-token-schema"),
        pytest.param(False, id="legacy-tokenless-schema"),
    ],
)
def test_name_scoped_paths_follow_the_advertised_owner_token_schema(
    advertises_owner_token
):
    transport = VersionedTransport(
        advertises_owner_token=advertises_owner_token
    )
    client = AgentMailClient(transport)

    assert client.fetch_inbox(
        project_key=PROJECT,
        agent_name=AGENT,
        registration_token=OWNER_TOKEN,
    ) == []
    assert client.whois(
        project_key=PROJECT,
        agent_name=AGENT,
        registration_token=OWNER_TOKEN,
    )["name"] == AGENT
    client.acknowledge_message(
        project_key=PROJECT,
        agent_name=AGENT,
        message_id=1,
        registration_token=OWNER_TOKEN,
    )
    client.reserve_files(
        project_key=PROJECT,
        agent_name=AGENT,
        paths=["a.py"],
        registration_token=OWNER_TOKEN,
    )
    client.renew_reservations(
        project_key=PROJECT,
        agent_name=AGENT,
        registration_token=OWNER_TOKEN,
    )
    client.release_reservations(
        project_key=PROJECT,
        agent_name=AGENT,
        registration_token=OWNER_TOKEN,
    )

    assert [tool for tool, _ in transport.seen] == [
        "fetch_inbox",
        "whois",
        "acknowledge_message",
        "file_reservation_paths",
        "renew_file_reservations",
        "release_file_reservations",
    ]
    for _, arguments in transport.seen:
        if advertises_owner_token:
            assert arguments["registration_token"] == OWNER_TOKEN
        else:
            assert "registration_token" not in arguments


def test_registration_send_and_retirement_keep_their_required_credentials():
    transport = VersionedTransport()
    client = AgentMailClient(transport)

    client.register_agent(
        project_key=PROJECT,
        model="gpt-example",
        registration_token=OWNER_TOKEN,
        agent_name=AGENT,
    )
    client.send_message(
        project_key=PROJECT,
        agent_name=AGENT,
        registration_token=OWNER_TOKEN,
        to=["Calm-Noether"],
        subject="done",
        body_md="finished",
    )
    client.retire_agent(
        project_key=PROJECT,
        agent_name=AGENT,
        registration_token=OWNER_TOKEN,
    )

    register_args = transport.seen[0][1]
    send_args = transport.seen[1][1]
    retire_args = transport.seen[2][1]
    assert register_args["registration_token"] == OWNER_TOKEN
    assert send_args["sender_token"] == OWNER_TOKEN
    assert retire_args["registration_token"] == OWNER_TOKEN


def test_proxy_resolves_owner_tokens_but_client_controls_upstream_schema():
    """Proxy resolution remains centralized without exposing identity inputs."""
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent
    text = (source / "src" / "agentstack_codex_app" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    assert text.count("binding, _ = self._resolve(") == 1
    assert text.count("registration_token=owner_token") >= 6
