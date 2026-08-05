"""Raw-free Codex App launch-to-coordination regression."""

from __future__ import annotations

import json

from agentstack_codex_app.agent_mail_client import AgentMailClient
from agentstack_codex_app.daemon import BridgeConfig, BridgeDaemon
from agentstack_codex_app.hook_entry import is_codex_desktop_payload, normalize_event
from agentstack_codex_app.mcp_server import AgentStackProxy, StdioMcpServer


SESSION_ID = "session-e2e"
PROJECT_KEY = "/workspace/example"
NAME_SCOPED_TOOLS = {
    "fetch_inbox",
    "whois",
    "acknowledge_message",
    "file_reservation_paths",
    "renew_file_reservations",
    "release_file_reservations",
}


class RecordingAgentMailTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, payload):
        if payload["method"] == "tools/list":
            return {
                "result": {
                    "tools": [
                        {
                            "name": tool,
                            "inputSchema": {
                                "properties": {"registration_token": {}}
                            },
                        }
                        for tool in NAME_SCOPED_TOOLS
                    ]
                }
            }
        tool = payload["params"]["name"]
        arguments = dict(payload["params"]["arguments"])
        self.calls.append((tool, arguments))
        if tool == "register_agent":
            result = {"name": "BoundNoether"}
        elif tool == "fetch_inbox":
            result = {
                "result": [
                    {
                        "id": 7,
                        "from": "ParentOpus",
                        "body_md": "E2E-V2-ACK BoundNoether",
                    }
                ]
            }
        elif tool == "send_message":
            result = {"deliveries": [{"payload": {"id": 8}}]}
        elif tool == "file_reservation_paths":
            result = {"granted": [{"id": 11}], "conflicts": []}
        else:  # pragma: no cover - the E2E allowlist below is intentionally exact
            raise AssertionError(f"unexpected agent-mail tool: {tool}")
        return {"result": {"structuredContent": result}}


def _call(server: StdioMcpServer, request_id: int, name: str, arguments: dict):
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is False, result["content"]
    return result["structuredContent"]


def test_codex_app_session_start_reaches_agentstack_without_raw_agent_mail(
    tmp_path,
):
    codex_home = tmp_path / ".codex"
    transcript = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "04"
        / f"rollout-test-{SESSION_ID}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": SESSION_ID,
                    "originator": "Codex Desktop",
                    "source": "vscode",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    raw_start = {
        "session_id": SESSION_ID,
        "cwd": PROJECT_KEY,
        "model": "gpt-example",
        "hook_event_name": "SessionStart",
        "turn_id": None,
        "transcript_path": str(transcript),
    }
    assert is_codex_desktop_payload(raw_start, codex_home=codex_home)

    runtime = tmp_path / "runtime"
    config = BridgeConfig(
        runtime_dir=runtime,
        socket_path=runtime / "bridge.sock",
        spool_path=runtime / "hook-events.jsonl",
        retry_path=runtime / "registration-retry.jsonl",
        snapshot_path=runtime / "snapshot.json",
        project_key=PROJECT_KEY,
        agent_mail_endpoint="http://agent-mail.invalid/api/",
        codex_sessions_root=codex_home / "sessions",
        enforce_surface_eligibility=True,
        cold_wake_enabled=False,
    )
    transport = RecordingAgentMailTransport()
    agent_mail = AgentMailClient(transport)
    daemon = BridgeDaemon(config, agent_mail)
    external_id = daemon.process_event(normalize_event(raw_start))

    binding = daemon.identities.resolve(external_id)
    assert binding is not None
    assert binding["agent_name"] == "BoundNoether"
    assert binding["session_id"] == SESSION_ID

    proxy = AgentStackProxy(daemon.identities, daemon.snapshots, agent_mail)
    server = StdioMcpServer(proxy)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    tool_names = {tool["name"] for tool in listed["result"]["tools"]}
    assert tool_names == {
        "bootstrap",
        "fetch_inbox",
        "send_message",
        "acknowledge_message",
        "reserve_files",
        "renew_reservations",
        "release_reservations",
        "runtime_status",
    }

    bootstrap = _call(
        server,
        2,
        "bootstrap",
        {"session_id": SESSION_ID, "agent_id": None},
    )
    status = _call(server, 3, "runtime_status", {})
    inbox = _call(server, 4, "fetch_inbox", {"include_bodies": True})["result"]
    sent = _call(
        server,
        5,
        "send_message",
        {
            "to": ["ParentOpus"],
            "subject": "E2E v2",
            "body_md": "session-bound source proxy",
        },
    )
    reserved = _call(
        server,
        6,
        "reserve_files",
        {"paths": ["integrations/codex_app/tests/test_session_bound_e2e.py"]},
    )

    assert bootstrap["agent_name"] == status["agent_name"] == "BoundNoether"
    assert status["lineage"]["kind"] == "root"
    assert inbox[0]["body_md"] == "E2E-V2-ACK BoundNoether"
    assert sent["deliveries"][0]["payload"]["id"] == 8
    assert reserved == {"granted": [{"id": 11}], "conflicts": []}

    calls = {tool: arguments for tool, arguments in transport.calls}
    assert set(calls) == {
        "register_agent",
        "fetch_inbox",
        "send_message",
        "file_reservation_paths",
    }
    owner_token = calls["register_agent"]["registration_token"]
    assert calls["fetch_inbox"]["registration_token"] == owner_token
    assert calls["file_reservation_paths"]["registration_token"] == owner_token
    assert "sender_token" in calls["send_message"]
    assert all(
        "session_id" not in arguments and "agent_id" not in arguments
        for tool, arguments in transport.calls
        if tool != "register_agent"
    )
