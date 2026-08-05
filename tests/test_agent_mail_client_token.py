#!/usr/bin/env python3
"""Every authenticated call must carry the token to the server, not just accept it.

The proxy client took `registration_token` on nine methods and forwarded it on
three. The six that dropped it looked correct from the outside — the parameter
was there, callers passed it, nothing raised — and the server simply refused
every read and reservation call. A pre-registered child could `send_message`
but never `fetch_inbox`, so it never read its own task and stalled at startup.

Accepting a credential and not sending it is invisible to types, to linters and
to the caller. The only thing that catches it is asserting on what reaches the
wire, which is what this does: drive each method against a fake transport and
look at the arguments dict it produced.

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

TOKEN = "test-token-0123456789"
PROJECT = "/tmp/project"
AGENT = "TestAgent"

# The server field each method is expected to put the token in. send_message
# uses sender_token because it authenticates the sender rather than the caller.
TOKEN_FIELD = {
    "fetch_inbox": "registration_token",
    "acknowledge_message": "registration_token",
    "reserve_files": "registration_token",
    "renew_reservations": "registration_token",
    "release_reservations": "registration_token",
    "whois": "registration_token",
    "send_message": "sender_token",
    "retire_agent": "registration_token",
}


class _Recorder:
    """Stands in for the transport and keeps what each call was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        # Shapes the callers validate before returning.
        if name == "fetch_inbox":
            return []
        if name == "whois":
            return {"name": AGENT}
        return {}

    _call_tool_object = _call_tool


def _client() -> tuple[object, _Recorder]:
    client = client_mod.AgentMailClient.__new__(client_mod.AgentMailClient)
    recorder = _Recorder()
    client._call_tool = recorder._call_tool
    client._call_tool_object = recorder._call_tool_object
    return client, recorder


def _invoke(client, method: str) -> None:
    common = {"project_key": PROJECT, "agent_name": AGENT, "registration_token": TOKEN}
    extra = {
        "acknowledge_message": {"message_id": 1},
        "reserve_files": {"paths": ["a.py"]},
        "send_message": {"to": [AGENT], "subject": "s", "body_md": "b"},
    }.get(method, {})
    getattr(client, method)(**common, **extra)


def test_every_authenticated_method_sends_its_token():
    missing = []
    for method, field in TOKEN_FIELD.items():
        client, recorder = _client()
        _invoke(client, method)
        assert recorder.calls, f"{method} made no call"
        _, arguments = recorder.calls[-1]
        if arguments.get(field) != TOKEN:
            missing.append(f"{method} -> {field}={arguments.get(field)!r}")
    assert not missing, "token never reached the server for: " + ", ".join(missing)


def test_absent_token_is_omitted_rather_than_sent_as_none():
    """A missing token must not become an explicit null the server has to reject."""

    for method in ("fetch_inbox", "reserve_files", "release_reservations", "whois"):
        client, recorder = _client()
        common = {"project_key": PROJECT, "agent_name": AGENT}
        extra = {"reserve_files": {"paths": ["a.py"]}}.get(method, {})
        getattr(client, method)(**common, **extra)
        _, arguments = recorder.calls[-1]
        assert "registration_token" not in arguments, method


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
