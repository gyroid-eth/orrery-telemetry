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
    assert "name" not in call["params"]["arguments"]


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
    assert client.transport.payloads[0]["params"]["arguments"]["name"] == (
        "Quiet-Curie"
    )


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


def test_p2_methods_use_only_explicit_agent_mail_tools():
    responses = [
        {"result": {"structuredContent": {"result": [{"id": 7}]}}},
        {"result": {"structuredContent": {"count": 1}}},
        {"result": {"structuredContent": {"message_id": 7}}},
        {"result": {"structuredContent": {"granted": [], "conflicts": []}}},
        {"result": {"structuredContent": {"renewed": 1}}},
        {"result": {"structuredContent": {"released": 1}}},
    ]

    class SequenceTransport:
        def __init__(self):
            self.payloads = []

        def __call__(self, payload):
            self.payloads.append(payload)
            return responses[len(self.payloads) - 1]

    transport = SequenceTransport()
    client = AgentMailClient(transport)
    assert client.fetch_inbox(
        project_key="/workspace/example",
        agent_name="Calm-Noether",
    ) == [{"id": 7}]
    client.send_message(
        project_key="/workspace/example",
        agent_name="Calm-Noether",
        registration_token="owner-secret",
        to=["Quiet-Curie"],
        subject="Result",
        body_md="Done",
    )
    client.acknowledge_message(
        project_key="/workspace/example",
        agent_name="Calm-Noether",
        message_id=7,
    )
    client.reserve_files(
        project_key="/workspace/example",
        agent_name="Calm-Noether",
        paths=["src/*.py"],
    )
    client.renew_reservations(
        project_key="/workspace/example",
        agent_name="Calm-Noether",
        file_reservation_ids=[11],
    )
    client.release_reservations(
        project_key="/workspace/example",
        agent_name="Calm-Noether",
        file_reservation_ids=[11],
    )

    assert [call["params"]["name"] for call in transport.payloads] == [
        "fetch_inbox",
        "send_message",
        "acknowledge_message",
        "file_reservation_paths",
        "renew_file_reservations",
        "release_file_reservations",
    ]
    send_args = transport.payloads[1]["params"]["arguments"]
    assert send_args["sender_token"] == "owner-secret"


def test_retire_agent_uses_bridge_owner_token():
    transport = FakeTransport(
        {
            "result": {
                "structuredContent": {
                    "status": "retired",
                    "agent_name": "CalmNoether",
                    "project_key": "/workspace/example",
                }
            }
        }
    )
    client = AgentMailClient(transport)

    result = client.retire_agent(
        project_key="/workspace/example",
        agent_name="CalmNoether",
        registration_token="owner-secret",
    )

    assert result["status"] == "retired"
    call = transport.payloads[0]
    assert call["params"]["name"] == "retire_agent"
    assert call["params"]["arguments"] == {
        "project_key": "/workspace/example",
        "agent_name": "CalmNoether",
        "registration_token": "owner-secret",
    }


def test_whois_reads_profile_without_commit_history():
    transport = FakeTransport(
        {
            "result": {
                "structuredContent": {
                    "name": "CalmNoether",
                    "program": "codex-app",
                    "retired_at": "2026-07-16T12:00:00Z",
                }
            }
        }
    )
    client = AgentMailClient(transport)

    profile = client.whois(
        project_key="/workspace/example",
        agent_name="CalmNoether",
    )

    assert profile["retired_at"] == "2026-07-16T12:00:00Z"
    call = transport.payloads[0]
    assert call["params"]["name"] == "whois"
    assert call["params"]["arguments"] == {
        "project_key": "/workspace/example",
        "agent_name": "CalmNoether",
        "include_recent_commits": False,
    }
