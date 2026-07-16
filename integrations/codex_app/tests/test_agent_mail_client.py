from __future__ import annotations

import json

import pytest

from agentstack_codex_app.agent_mail_client import AgentMailClient, AgentMailError


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        return self.response


def test_register_agent_uses_injected_transport_and_caller_owner_token():
    transport = FakeTransport(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"name": "Calm-Noether"}),
                    }
                ]
            },
        }
    )
    client = AgentMailClient(transport)
    registration = client.register_agent(
        project_key="/workspace/example",
        model="gpt-example",
        registration_token="owner-secret",
    )

    assert registration.agent_name == "Calm-Noether"
    assert registration.registration_token == "owner-secret"
    call = transport.payloads[0]
    assert call["method"] == "tools/call"
    assert call["params"]["name"] == "register_agent"
    assert call["params"]["arguments"]["program"] == "codex-app"
    assert call["params"]["arguments"]["registration_token"] == "owner-secret"


def test_register_agent_accepts_structured_content():
    client = AgentMailClient(
        FakeTransport(
            {
                "result": {
                    "structuredContent": {
                        "name": "Quiet-Curie",
                        "registration_token": "owner-secret",
                    }
                }
            }
        )
    )
    result = client.register_agent(
        project_key="/workspace/example",
        model="gpt-example",
        registration_token="owner-secret",
        agent_name="Quiet-Curie",
    )
    assert result.agent_name == "Quiet-Curie"


def test_register_agent_rejects_conflicting_returned_token():
    client = AgentMailClient(
        FakeTransport(
            {
                "result": {
                    "structuredContent": {
                        "name": "Quiet-Curie",
                        "registration_token": "different-secret",
                    }
                }
            }
        )
    )
    with pytest.raises(AgentMailError, match="conflicting"):
        client.register_agent(
            project_key="/workspace/example",
            model="gpt-example",
            registration_token="owner-secret",
        )
