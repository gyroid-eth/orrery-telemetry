"""Synthetic bindings; no production credential, mailbox, signal or service."""

import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integrations/codex_app/src"))
sys.path.insert(0, str(ROOT / "packages/agentstack_mail/src"))
from agentstack_codex_app.daemon_runtime import (
    DaemonRuntime,
    DaemonError,
    KIND,
    private_text,
    connection_at,
    profile_at,
    create_identity,
)
from agentstack_codex_app.agent_mail_client import AgentMailClient
from agentstack_mail.enrollment import credential_fingerprint

TOKEN = "fixture-owner-token-00000000000000000000000000"
INSTANCE = "c25d8404-e773-46c3-927d-3b0ce034bc72"


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def binding(tmp_path):
    tmp_path.chmod(0o700)
    connection = {
        "kind": "orrery-mail-connection-v1",
        "expected_server_instance_id": INSTANCE,
        "mcp_url": "http://127.0.0.1:12345/api/",
        "management_socket": str(tmp_path / "m.sock"),
        "runtime_dir": str(tmp_path),
        "http_bearer_mode": "disabled",
    }
    conn = write(tmp_path / "connection.json", connection)
    profile = {
        "kind": KIND,
        "name": "BlueLake",
        "agent_id": 1,
        "project_key": str(tmp_path / "project"),
        "connection": str(conn),
        "peer_name": "GreenCastle",
        "peer_id": 2,
        "pause_path": str(tmp_path / "paused.json"),
        "lock_path": str(tmp_path / "daemon.lock"),
    }
    path = write(tmp_path / "daemon.json", profile)
    inspected = {
        "ok": True,
        "server_instance_id": INSTANCE,
        "project_key": profile["project_key"],
        "agent_id": 1,
        "name": "BlueLake",
        "retired": False,
        "credential_state": "server-token",
        "credential_fingerprint": credential_fingerprint(TOKEN),
        "credential_generation": 0,
    }
    calls = []
    faults = {}

    def transport(payload):
        if payload["method"] == "tools/list":
            return {"result": {"tools": []}}
        name = payload["params"]["name"]
        args = payload["params"]["arguments"]
        calls.append((name, args.copy()))
        if name in faults:
            return {"result": {"structuredContent": faults[name]()}}
        if name == "health_check":
            value = {"status": "ok", "server_instance_id": INSTANCE}
        elif name == "whois":
            own = args["agent_name"] == "BlueLake"
            value = {
                "id": 1 if own else 2,
                "name": args["agent_name"],
                "project_id": 10,
                "program": "orrery-daemon" if own else "claude-code",
            }
        elif name == "fetch_inbox":
            value = [
                {"id": 301, "from": "GreenCastle", "sender_id": 2, "project_id": 10}
            ]
        elif name == "register_agent":
            value = {"name": args["name"], "id": 1}
        elif name == "acknowledge_message":
            value = {"ok": True}
        elif name == "send_message":
            value = {"deliveries": [{"payload": {"id": 302}}]}
        else:
            raise AssertionError(name)
        return {"result": {"structuredContent": value}}

    client = AgentMailClient(transport)
    runtime = DaemonRuntime(
        profile, connection, TOKEN, client=client, inspect=lambda: inspected.copy()
    )
    yield {
        "runtime": runtime,
        "profile": profile,
        "connection": connection,
        "path": path,
        "inspect": inspected,
        "calls": calls,
        "faults": faults,
        "client": client,
        "root": tmp_path,
    }
    runtime.close()


def test_binding_and_owner_fields_are_not_caller_arguments(binding):
    b = binding
    r = b["runtime"]
    assert r.call("runtime_status", {}) == {
        "name": "BlueLake",
        "project": b["profile"]["project_key"],
        "agent_id": 1,
        "instance": INSTANCE,
    }
    assert r.call("whois", {"agent_name": "GreenCastle"})["id"] == 2
    assert (
        r.call("fetch_inbox", {"limit": 1000, "include_bodies": True})[0]["id"] == 301
    )
    r.call("acknowledge_message", {"message_id": 301})
    r.call(
        "send_message",
        {
            "to": ["GreenCastle"],
            "subject": "synthetic",
            "body_md": "synthetic",
            "thread_id": str(uuid.uuid4()),
        },
    )
    sends = [args for name, args in b["calls"] if name == "send_message"]
    assert sends[0]["sender_name"] == "BlueLake" and sends[0]["sender_token"] == TOKEN
    assert all(
        "registration_token" not in args
        for name, args in b["calls"]
        if name != "send_message"
    )
    assert all(
        args["agent_name"] == "BlueLake"
        for name, args in b["calls"]
        if name == "fetch_inbox"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("server_instance_id", "different"),
        ("project_key", "/other"),
        ("agent_id", 4),
        ("agent_id", True),
        ("name", "OtherLake"),
        ("retired", True),
        ("credential_fingerprint", "wrong"),
        ("credential_generation", True),
    ],
)
def test_bad_enrollment_stops_before_http_or_mail(binding, key, value):
    binding["inspect"][key] = value
    with pytest.raises(DaemonError):
        binding["runtime"].call("fetch_inbox", {"include_bodies": True})
    assert binding["calls"] == []


def test_changed_generation_and_http_instance_are_rejected(binding):
    b = binding
    b["runtime"].call("runtime_status", {})
    b["inspect"]["credential_generation"] = 1
    with pytest.raises(DaemonError, match="credential-mismatch"):
        b["runtime"].call("acknowledge_message", {"message_id": 301})
    b["inspect"]["credential_generation"] = 0
    b["faults"]["health_check"] = lambda: {
        "status": "ok",
        "server_instance_id": "other",
    }
    with pytest.raises(DaemonError, match="http-instance-mismatch"):
        b["runtime"].call("fetch_inbox", {})
    assert not any(
        name in {"fetch_inbox", "acknowledge_message"} for name, _ in b["calls"]
    )


@pytest.mark.parametrize(
    "method,args",
    [
        ("register_agent", {}),
        ("recover", {}),
        ("fetch_inbox", {"agent_name": "OtherLake"}),
        ("send_message", {"registration_token": "synthetic"}),
        ("runtime_status", {"project_key": "/other"}),
    ],
)
def test_runtime_cannot_mutate_binding_or_supply_credentials(binding, method, args):
    with pytest.raises(DaemonError, match="call-denied"):
        binding["runtime"].call(method, args)
    assert binding["calls"] == []


@pytest.mark.parametrize(
    "field,value",
    [("sender_id", 3), ("sender_id", True), ("project_id", 11), ("from", "OtherLake")],
)
def test_foreign_inbox_is_rejected_without_ack(binding, field, value):
    binding["faults"]["fetch_inbox"] = lambda: [
        {"id": 1, "project_id": 10, "sender_id": 2, "from": "GreenCastle", field: value}
    ]
    with pytest.raises(DaemonError, match="message-denied"):
        binding["runtime"].call("fetch_inbox", {})
    assert not any(name == "acknowledge_message" for name, _ in binding["calls"])


def test_changed_peer_stops_send(binding):
    binding["faults"]["whois"] = lambda: {
        "name": "BlueLake",
        "id": 1,
        "project_id": 10,
        "program": "orrery-daemon",
    }
    with pytest.raises(DaemonError, match="peer-changed|operation-failed"):
        binding["runtime"].call(
            "send_message", {"to": ["GreenCastle"], "subject": "s", "body_md": "b"}
        )
    assert not any(name == "send_message" for name, _ in binding["calls"])


@pytest.mark.parametrize("point", ["entry", "inspect", "health", "own_whois"])
def test_pause_between_calls_stops_new_fetch_and_ack(binding, point):
    b = binding
    marker = Path(b["profile"]["pause_path"])
    if point == "entry":
        marker.write_text("{}")
    elif point == "inspect":

        def inspect():
            marker.write_text("{}")
            return b["inspect"].copy()

        b["runtime"]._inspect = inspect
    elif point == "health":

        def health():
            marker.write_text("{}")
            return {"status": "ok", "server_instance_id": INSTANCE}

        b["faults"]["health_check"] = health
    else:

        def whois():
            marker.write_text("{}")
            return {
                "name": "BlueLake",
                "id": 1,
                "project_id": 10,
                "program": "orrery-daemon",
            }

        b["faults"]["whois"] = whois
    with pytest.raises(DaemonError, match="paused"):
        b["runtime"].call("fetch_inbox", {})
    with pytest.raises(DaemonError, match="paused"):
        b["runtime"].call("acknowledge_message", {"message_id": 301})
    assert not any(
        name in {"fetch_inbox", "acknowledge_message"} for name, _ in b["calls"]
    )


def test_canonical_wait_uses_authenticated_metadata_and_close_cancels(binding):
    b = binding
    r = b["runtime"]
    # Consumption is separate; the helper never ACKs the Mail during its wait.
    assert r.wait_for_notification(0.1) is True
    r.call("fetch_inbox", {"include_bodies": True})
    assert r.wait_for_notification(0.01) is False
    started = threading.Event()
    original = r._wait

    def wait(*args, **kwargs):
        started.set()
        return original(*args, **kwargs)

    r._wait = wait
    result = []
    thread = threading.Thread(target=lambda: result.append(r.wait_for_notification(60)))
    thread.start()
    assert started.wait(1)
    at = time.monotonic()
    r.close()
    thread.join(1)
    assert not thread.is_alive() and result == [False]
    assert time.monotonic() - at < 1
    fetches = [args for name, args in b["calls"] if name == "fetch_inbox"]
    assert fetches and all(args["agent_name"] == "BlueLake" for args in fetches)
    assert not any(name == "acknowledge_message" for name, _ in b["calls"])


def test_create_is_operator_only_single_attempt_and_receipt_has_no_secret(binding):
    b = binding
    receipt = create_identity(
        b["profile"]["connection"],
        project_key=b["profile"]["project_key"],
        name="BlueLake",
        journal_path=b["root"] / "pending.json",
        client=b["client"],
    )
    assert receipt["agent_id"] == 1 and "token" not in json.dumps(receipt)
    assert (b["root"] / "agent_token_BlueLake").stat().st_mode & 0o777 == 0o600
    with pytest.raises(DaemonError, match="already-attempted"):
        create_identity(
            b["profile"]["connection"],
            project_key=b["profile"]["project_key"],
            name="BlueLake",
            journal_path=b["root"] / "pending.json",
            client=b["client"],
        )
    assert len([name for name, _ in b["calls"] if name == "register_agent"]) == 1


def test_close_during_binding_wait_completes_without_another_network_call(binding):
    b = binding
    started, finish = threading.Event(), threading.Event()

    def inspect():
        started.set()
        assert finish.wait(1)
        return b["inspect"].copy()

    b["runtime"]._inspect = inspect
    result = []
    thread = threading.Thread(
        target=lambda: result.append(b["runtime"].wait_for_notification(60))
    )
    thread.start()
    assert started.wait(1)
    timer = threading.Timer(0.1, finish.set)
    timer.start()
    try:
        b["runtime"].close()
        thread.join(1)
        assert not thread.is_alive() and result == [False]
        assert b["calls"] == []
    finally:
        finish.set()
        timer.join(1)
        thread.join(2)


def test_unknown_create_keeps_journal_and_does_not_register_again(binding):
    b = binding

    def failure():
        raise OSError("synthetic transport failure")

    b["faults"]["register_agent"] = failure
    kwargs = dict(
        project_key=b["profile"]["project_key"],
        name="BlueLake",
        journal_path=b["root"] / "pending.json",
        client=b["client"],
    )
    with pytest.raises(OSError):
        create_identity(b["profile"]["connection"], **kwargs)
    assert json.loads(private_text(kwargs["journal_path"]))["outcome"] == "unknown"
    with pytest.raises(DaemonError, match="already-attempted"):
        create_identity(b["profile"]["connection"], **kwargs)
    assert len([name for name, _ in b["calls"] if name == "register_agent"]) == 1


def test_private_profile_and_connection_fail_closed(binding):
    b = binding
    assert profile_at(b["path"])["agent_id"] == 1
    b["path"].chmod(0o644)
    with pytest.raises(DaemonError, match="unsafe"):
        profile_at(b["path"])
    link = b["root"] / "link"
    link.symlink_to(b["path"])
    with pytest.raises(OSError):
        private_text(link)
    connection = b["connection"].copy()
    connection["mcp_url"] = "https://example.test/mcp"
    path = write(b["root"] / "bad.json", connection)
    with pytest.raises(DaemonError, match="not-local"):
        connection_at(path)
