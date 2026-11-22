"""4c server boundary with synthetic candidates and real HTTP."""
import asyncio
import os
import uuid
import pytest
from test_global_server_s2c import prepared, server
from test_global_server_s2b import owner, http, mutate, send
from agentstack_mail.global_s2c import S2cRuntime
from agentstack_mail.global_server import GlobalError


def test_bind_window_replay_and_other_owner(server):
    args = owner(server, request_id=str(uuid.uuid4()), window_uuid=str(uuid.uuid4()))
    first = asyncio.run(http(server, "bind_window_identity", args))
    assert asyncio.run(http(server, "bind_window_identity", args)) == first
    runtime = S2cRuntime(server["config_path"])
    with runtime.transaction() as db:
        assert db.execute("SELECT count(*) FROM window_identities WHERE window_uuid=?", (args["window_uuid"],)).fetchone()[0] == 1
    with pytest.raises(GlobalError, match="WINDOW_OWNER_MISMATCH"):
        runtime.apply("bind_window_identity", owner(server, 1, request_id=str(uuid.uuid4()), window_uuid=args["window_uuid"]))
    # Verify the new row through the existing read-only owner wire.
    result = asyncio.run(http(server, "verify_window_identity", owner(server, window_row_id=first["window_row_id"], window_uuid=first["window_uuid"])))
    assert result["ready"]


def test_pending_filter_and_completion_generation(server):
    sent = asyncio.run(http(server, "send_message", send(server)))
    mid = sent["message_id"]
    args = owner(server, limit=1, message_ids=[mid], notification_pending_only=True)
    assert [row["id"] for row in asyncio.run(http(server, "fetch_inbox", args))] == [mid]
    runtime = S2cRuntime(server["config_path"])
    request = {"version":1, "operation":"delivery_completed", **{k:owner(server)[k] for k in ("expected_server_instance_id", "candidate_generation", "authority_epoch")}, "recipient_agent_id":2, "expected_credential_generation":8, "message_ids":[mid]}
    with pytest.raises(GlobalError, match="CREDENTIAL_GENERATION_MISMATCH"):
        runtime._management_apply(request, os.getuid())
    assert len(runtime.inbox({**args, "include_bodies":True, "urgent_only":False})) == 1
    request["expected_credential_generation"] = 9
    assert runtime._management_apply(request, os.getuid())["items"][0]["state"] == "recorded"
    assert asyncio.run(http(server, "fetch_inbox", args)) == []
    assert len(asyncio.run(http(server, "fetch_inbox", {**args,"notification_pending_only":False}))) == 1


def test_completion_after_retirement_preserves_fact_across_unretire(server):
    mid = asyncio.run(http(server, "send_message", send(server)))["message_id"]
    runtime = S2cRuntime(server["config_path"])
    binding = {k: owner(server)[k] for k in (
        "expected_server_instance_id", "candidate_generation", "authority_epoch")}
    request = {"version": 1, "operation": "delivery_completed", **binding,
               "recipient_agent_id": 2, "expected_credential_generation": 9,
               "message_ids": [mid]}
    asyncio.run(http(server, "retire_agent", {k:v for k,v in owner(server).items() if k != "expected_credential_generation"}))
    # Retirement continues to prohibit new delivery from public tools.
    with pytest.raises(GlobalError, match="TARGET_UNAVAILABLE"):
        runtime.apply("send_message", send(server))
    for changes, reason in (
        ({"expected_credential_generation": 8}, "CREDENTIAL_GENERATION_MISMATCH"),
        ({"recipient_agent_id": 3, "expected_credential_generation": 2}, "DELIVERY_RECIPIENT_MISMATCH"),
        ({"authority_epoch": "foreign"}, "STALE_RUNTIME_BINDING"),
    ):
        with pytest.raises(GlobalError, match=reason):
            runtime._management_apply({**request, **changes}, os.getuid())
    with pytest.raises(GlobalError, match="PEER_UID_REQUIRED"):
        runtime._management_apply(request, os.getuid() + 1)
    first = runtime._management_apply(request, os.getuid())["items"][0]
    assert first["state"] == "recorded"
    again = runtime._management_apply(request, os.getuid())["items"][0]
    assert again == {**first, "state": "already-completed"}
    asyncio.run(http(server, "unretire_agent", {k:v for k,v in owner(server).items() if k != "expected_credential_generation"}))
    assert asyncio.run(http(server, "fetch_inbox", owner(
        server, message_ids=[mid], notification_pending_only=True, limit=1))) == []
    with runtime.transaction() as db:
        row = db.execute("SELECT notification_fact FROM message_recipients WHERE message_id=? AND agent_id=2", (mid,)).fetchone()
        assert row[0] == "delivered:" + first["notified_at"]
        assert db.execute("SELECT retired_at FROM agents WHERE id=2").fetchone()[0] is None
