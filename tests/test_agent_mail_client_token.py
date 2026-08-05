#!/usr/bin/env python3
"""Whether a call carries the owner token is the server's decision, not ours.

The proxy client took `registration_token` on nine methods and forwarded it on
three. On the pinned upstream that meant every read and reservation call was
unauthenticated and refused: a pre-registered child could `send_message` but
never `fetch_inbox`, so it never read its own task and stalled at startup.

Sending it unconditionally is not the fix. Builds disagree — the pinned
upstream declares `registration_token` on these tools, older and locally
patched builds reject that same argument — so a client that hardcodes either
answer breaks the other. `scripts/selftest.py` had already settled this by
reading the advertised schema; the client now does the same.

Two fixtures, one per server generation, and the assertions run against what
reaches the transport rather than against the method signature. Accepting a
credential and not sending it is invisible to types, to linters and to the
caller — the wire is the only place it shows.

Runnable two ways:
    python3 tests/test_agent_mail_client_token.py
    pytest tests/test_agent_mail_client_token.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "integrations"
    / "codex_app"
    / "src"
    / "agentstack_codex_app"
    / "agent_mail_client.py"
)

_spec = importlib.util.spec_from_file_location("agent_mail_client", MODULE_PATH)
client_mod = importlib.util.module_from_spec(_spec)
# Register before exec: the module defines a slots dataclass, and @dataclass
# resolves annotations through sys.modules[cls.__module__] while the class body
# is being processed.
sys.modules[_spec.name] = client_mod
_spec.loader.exec_module(client_mod)

TOKEN = "owner-secret-that-must-not-escape"
PROJECT = "/workspace/example"
AGENT = "CalmNoether"

# The six name-scoped tools whose token handling differs between builds.
NAME_SCOPED = (
    "fetch_inbox",
    "acknowledge_message",
    "reserve_files",
    "renew_reservations",
    "release_reservations",
    "whois",
)
# Method name -> the tool name it calls on the server.
SERVER_TOOL = {
    "fetch_inbox": "fetch_inbox",
    "acknowledge_message": "acknowledge_message",
    "reserve_files": "file_reservation_paths",
    "renew_reservations": "renew_file_reservations",
    "release_reservations": "release_file_reservations",
    "whois": "whois",
}
CALL_ARGS = {
    "acknowledge_message": {"message_id": 7},
    "reserve_files": {"paths": ["src/*.py"]},
}


class _Server:
    """Answers tools/list with one generation's schema and records tools/call."""

    def __init__(self, *, advertises_token: bool) -> None:
        self.advertises_token = advertises_token
        self.listings = 0
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, payload):
        if payload.get("method") == "tools/list":
            self.listings += 1
            base = ["project_key", "agent_name"]
            extra = ["registration_token"] if self.advertises_token else []
            return {
                "result": {
                    "tools": [
                        {
                            "name": tool,
                            "inputSchema": {
                                "properties": {k: {} for k in base + extra}
                            },
                        }
                        for tool in SERVER_TOOL.values()
                    ]
                }
            }
        tool = payload["params"]["name"]
        self.calls.append((tool, dict(payload["params"]["arguments"])))
        result = {"result": []} if tool == "fetch_inbox" else {"ok": True}
        if tool == "whois":
            result = {"name": AGENT}
        return {"result": {"structuredContent": result}}


def _drive(server) -> object:
    client = client_mod.AgentMailClient(server)
    for method in NAME_SCOPED:
        getattr(client, method)(
            project_key=PROJECT,
            agent_name=AGENT,
            registration_token=TOKEN,
            **CALL_ARGS.get(method, {}),
        )
    return client


def test_a_server_that_advertises_the_token_field_receives_it():
    server = _Server(advertises_token=True)
    _drive(server)
    assert len(server.calls) == len(NAME_SCOPED)
    missing = [
        tool for tool, args in server.calls if args.get("registration_token") != TOKEN
    ]
    assert not missing, "token never reached the server for: " + ", ".join(missing)


def test_a_server_that_does_not_advertise_it_is_never_sent_one():
    server = _Server(advertises_token=False)
    _drive(server)
    assert len(server.calls) == len(NAME_SCOPED)
    leaked = [tool for tool, args in server.calls if "registration_token" in args]
    assert not leaked, "token sent to a server that rejects it: " + ", ".join(leaked)


def test_the_schema_is_read_once_no_matter_how_many_calls_follow():
    server = _Server(advertises_token=True)
    _drive(server)
    assert server.listings == 1, server.listings


def test_an_unusable_listing_fails_before_any_tool_is_called():
    class Broken:
        def __init__(self):
            self.calls = []

        def __call__(self, payload):
            if payload.get("method") == "tools/list":
                return {"result": {"tools": "not-a-list"}}
            self.calls.append(payload)
            return {"result": {"structuredContent": {"result": []}}}

    server = Broken()
    client = client_mod.AgentMailClient(server)
    try:
        client.fetch_inbox(
            project_key=PROJECT, agent_name=AGENT, registration_token=TOKEN
        )
    except client_mod.AgentMailError as exc:
        assert TOKEN not in str(exc), "the token must not appear in the error"
    else:
        raise AssertionError("a broken listing must not fall through to tools/call")
    assert not server.calls, "no tool may run on an undiscovered schema"


def test_no_token_means_no_listing_and_no_token_argument():
    """Without a credential there is nothing to shape, so do not go ask."""

    server = _Server(advertises_token=True)
    client = client_mod.AgentMailClient(server)
    client.fetch_inbox(project_key=PROJECT, agent_name=AGENT)
    assert server.listings == 0
    _, arguments = server.calls[-1]
    assert "registration_token" not in arguments


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
