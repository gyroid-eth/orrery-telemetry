"""S2b acceptance: synthetic sources/private HOME and real HTTP/Unix socket."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import pytest
from fastmcp import Client

from agentstack_mail.global_server import GlobalError
from agentstack_mail.global_s2a import revision
from agentstack_mail.global_s2b_contract import TOOLS
from agentstack_mail.global_s2b import S2bRuntime
from agentstack_mail.global_prepare_s2b import prepare
from agentstack_mail.namespace_plan import FenceEvidence
from agentstack_mail.namespace_state_io import connection

spec = importlib.util.spec_from_file_location(
    "s2a_legacy", Path(__file__).parent / "fixtures/namespace_state.py"
)
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def fence(gen, roster):
    return FenceEvidence(
        gen, roster, {n: "stopped" for n in roster}, True, True, True, 0
    )


@pytest.fixture
def prepared(monkeypatch):
    root = Path(
        tempfile.mkdtemp(
            prefix="s2a-", dir="/private/tmp" if Path("/private/tmp").is_dir() else None
        )
    ).resolve()
    root.chmod(0o700)
    (root / "home").mkdir(mode=0o700)
    (root / "tmux").mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("TMUX_TMPDIR", str(root / "tmux"))
    source, choices = legacy.build(root / "source")
    # Unread historical message without any remaining hint/delivery row:
    # cutover must preserve its consumed state without inventing a timestamp.
    with connection(source.mail, write=True) as db:
        db.execute(
            "INSERT INTO messages(id,project_id,sender_id,subject,body_md,importance,ack_required,created_ts,attachments) VALUES(107,1,1,'historical','old','normal',0,'2026-10-08T10:00:00+00:00','[]')"
        )
        db.execute(
            "INSERT INTO message_recipients(message_id,agent_id,kind) VALUES(107,2,'to')"
        )
    request_id = str(uuid.uuid4())
    result = prepare(source, root, "one", choices, request_id=request_id, fence=fence)
    candidate = root / "s2b-candidates/one/candidate"
    cfg = json.loads((candidate / "server-config.json").read_text())
    state = {
        "root": root,
        "source": source,
        "choices": choices,
        "request_id": request_id,
        "receipt": result,
        "config": cfg,
        "config_path": candidate / "server-config.json",
    }
    try:
        yield state
    finally:
        shutil.rmtree(root)


def owner(state, aid=2, **extra):
    return {
        "expected_server_instance_id": state["config"]["mail_instance_id"],
        "candidate_generation": state["config"]["candidate_generation"],
        "authority_epoch": state["config"]["authority_epoch"],
        "agent_id": aid,
        "registration_token": {
            1: "fixture-owner-one",
            2: "fixture-owner-two",
            3: "fixture-owner-three",
        }[aid],
        "expected_credential_generation": {1: 7, 2: 9, 3: 2}[aid],
        **extra,
    }


def mutate(state, tool, aid=2, **extra):
    return S2bRuntime(state["config_path"]).apply(
        tool, owner(state, aid, request_id=str(uuid.uuid4()), **extra)
    )


def file_state(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def send(state, **extra):
    return owner(
        state,
        1,
        request_id=str(uuid.uuid4()),
        subject="hello",
        body_md="body",
        to_agent_ids=[2],
        **extra,
    )


def test_prepare_and_send_replay(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    args = send(prepared)
    first = runtime.apply("send_message", args)
    assert runtime.apply("send_message", args) == first
    assert first["message_id"] == 108
    with runtime.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 1
        )
        assert db.execute("SELECT count(*) FROM global_signal_dirty").fetchone()[0] == 1
    runtime.reconcile(batch_limit=100)
    path = runtime.runtime_root / "signals/agents/2/108.signal"
    assert path.exists()
    result = runtime.apply("search_messages", owner(prepared, 2, query="hello"))
    assert result["items"][0]["id"] == 108
    assert result["items"][0]["body_md"] is None
    runtime.apply(
        "acknowledge_message",
        owner(prepared, 2, message_id=108, request_id=str(uuid.uuid4())),
    )
    runtime.reconcile(batch_limit=100)
    assert not path.exists()


def test_imported_consumed_not_backlog_new_reply(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    with runtime.transaction() as db:
        assert runtime.signal_stats(db)["undelivered_count"] == 0
    result = runtime.apply(
        "reply_message",
        owner(
            prepared,
            2,
            message_id=101,
            body_md="new reply",
            request_id=str(uuid.uuid4()),
        ),
    )
    with runtime.transaction() as db:
        row = db.execute(
            "SELECT * FROM message_recipients WHERE message_id=?",
            (result["message_id"],),
        ).fetchone()
        assert row["notification_fact"] is None
        assert runtime.signal_stats(db)["undelivered_count"] == 1
    runtime.reconcile(batch_limit=100)
    assert (
        runtime.runtime_root / "signals/agents/1" / f"{result['message_id']}.signal"
    ).exists()


@pytest.mark.parametrize(
    "point",
    ["before_state", "after_state", "after_receipt", "before_commit", "after_commit"],
)
def test_write_interruption_same_uuid(prepared, point):
    runtime = S2bRuntime(prepared["config_path"])
    args = send(prepared)

    def fail(p):
        if p == point:
            raise RuntimeError("crash")

    runtime.fault = fail
    with pytest.raises(RuntimeError):
        runtime.apply("send_message", args)
    fresh = S2bRuntime(prepared["config_path"])
    result = fresh.apply("send_message", args)
    assert fresh.apply("send_message", args) == result
    with fresh.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT count(*) FROM messages WHERE id>107").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "point",
    [
        "signal_after_fact",
        "signal_after_write",
        "signal_after_fsync",
        "signal_after_rename",
        "signal_after_dir_fsync",
        "signal_after_dirty",
        "signal_before_commit",
        "signal_after_commit",
    ],
)
def test_signal_interruption_same_pair(prepared, point):
    runtime = S2bRuntime(prepared["config_path"])
    result = runtime.apply("send_message", send(prepared))

    def fail(p):
        if p == point:
            raise RuntimeError("crash")

    runtime.fault = fail
    with pytest.raises(RuntimeError):
        runtime.reconcile(batch_limit=100)
    fresh = S2bRuntime(prepared["config_path"])
    out = fresh.reconcile(batch_limit=100)
    assert out["blocked_count"] == 0
    assert (
        fresh.runtime_root / "signals/agents/2" / f"{result['message_id']}.signal"
    ).exists()


@pytest.fixture
def server(prepared):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("AGENTSTACK_", "AGS_"))
    }
    env.update(
        HOME=str(prepared["root"] / "home"),
        TMUX_TMPDIR=str(prepared["root"] / "tmux"),
        AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE="passthrough",
        AGENTSTACK_MAIL_ENV_FILE=str(prepared["root"] / "absent"),
    )
    prepared["url"] = f"http://127.0.0.1:{port}/mcp"
    with (prepared["root"] / "server.log").open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentstack_mail.cli",
                "--port",
                str(port),
                "--global-config",
                str(prepared["config_path"]),
            ],
            env=env,
            cwd=prepared["root"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 25
            last_error = "HTTP health has not answered"
            while True:
                remaining = deadline - time.monotonic()
                if proc.poll() is not None or remaining <= 0:
                    pytest.fail(
                        f"Server startup failed: {last_error}\n"
                        + (prepared["root"] / "server.log").read_text()
                    )
                if Path(prepared["config"]["management_socket"]).exists():
                    try:
                        health = asyncio.run(
                            asyncio.wait_for(
                                http(prepared, "health_check"),
                                timeout=min(1, remaining),
                            )
                        )
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                    else:
                        if (health.get("status"), health.get("runtime_profile")) == (
                            "ok",
                            "s2b-v1",
                        ):
                            break
                        last_error = f"Unexpected HTTP health: {health!r}"
                time.sleep(0.05)
            yield prepared
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            assert not Path(prepared["config"]["management_socket"]).exists()


async def http(state, name, args=None):
    async with Client(state["url"]) as client:
        if name == "tools":
            return await client.list_tools()
        response = await client.call_tool(name, args or {})
        return json.loads(next(p.text for p in response.content if hasattr(p, "text")))


def management(state, operation, **extra):
    req = {
        "version": 1,
        "operation": operation,
        **{
            k: v
            for k, v in owner(state).items()
            if k
            in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        },
        **extra,
    }
    with socket.socket(socket.AF_UNIX) as stream:
        stream.settimeout(5)
        stream.connect(state["config"]["management_socket"])
        stream.sendall(json.dumps(req).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            data += stream.recv(65536)
    return json.loads(data)


def test_actual_http_eight_tools_inbox_and_management(server):
    found = {t.name: t for t in asyncio.run(http(server, "tools"))}
    for name, contract in TOOLS.items():
        assert found[name].inputSchema == contract["input_schema"]
        assert found[name].outputSchema == contract["output_schema"]
    sent = asyncio.run(http(server, "send_message", send(server, topic="acceptance")))
    mid = sent["message_id"]
    inbox = asyncio.run(
        http(server, "fetch_inbox", owner(server, include_bodies=False))
    )
    assert any(r["id"] == mid and r["body_md"] is None for r in inbox)
    page = asyncio.run(http(server, "search_messages", owner(server, query="hello")))
    assert mid in [r["id"] for r in page["items"]]
    topic = asyncio.run(
        http(server, "fetch_topic", owner(server, topic_name="acceptance"))
    )
    assert [r["id"] for r in topic["items"]] == [mid]
    summary = asyncio.run(http(server, "fetch_summary", owner(server)))
    assert mid in [m for g in summary["items"] for m in g["source_message_ids"]]
    conversation = asyncio.run(
        http(
            server,
            "summarize_thread",
            owner(server, message_id=mid, include_examples=True),
        )
    )
    assert conversation["items"][0]["source_message_ids"] == [mid]
    done = management(
        server, "delivery_completed", recipient_agent_id=2, message_ids=[mid]
    )
    assert done["ok"] and done["items"][0]["state"] == "recorded"
    again = management(
        server, "delivery_completed", recipient_agent_id=2, message_ids=[mid]
    )
    assert again["items"][0]["notified_at"] == done["items"][0]["notified_at"]
    read = asyncio.run(
        http(
            server,
            "mark_message_read",
            owner(server, message_id=mid, request_id=str(uuid.uuid4())),
        )
    )
    ack = asyncio.run(
        http(
            server,
            "acknowledge_message",
            owner(server, message_id=mid, request_id=str(uuid.uuid4())),
        )
    )
    assert read["read_at"] == ack["read_at"]
    reply = asyncio.run(
        http(
            server,
            "reply_message",
            owner(
                server, message_id=mid, body_md="reply", request_id=str(uuid.uuid4())
            ),
        )
    )
    assert reply["reply_to"] == mid
    flushed = management(server, "reconcile_outputs", batch_limit=100)
    assert flushed["ok"]


def test_inbox_read_leaves_revision_hint_and_consumption_unchanged(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    mid = runtime.apply("send_message", send(prepared))["message_id"]
    runtime.reconcile()
    path = runtime.runtime_root / "signals/agents/2" / f"{mid}.signal"
    raw = path.read_bytes()
    with runtime.transaction() as db:
        before = revision(db)
    args = owner(
        prepared,
        limit=20,
        include_bodies=True,
        urgent_only=False,
        since_ts=None,
        before_id=None,
    )
    assert runtime.inbox(args)[0]["id"] == mid
    assert path.read_bytes() == raw
    with runtime.transaction() as db:
        assert revision(db) == before
        assert db.execute(
            "SELECT read_ts,notification_fact FROM message_recipients WHERE message_id=? AND agent_id=2",
            (mid,),
        ).fetchone()[:] == (None, None)


def test_visible_keyset_ignores_other_owner_and_survives_mutations(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    first = runtime.apply("search_messages", owner(prepared, query="", limit=1))
    maximum = first["max_message_id"]
    cursor = first["next_cursor"]
    assert cursor
    # A new message after the first page must never appear in its continuation.
    new = runtime.apply("send_message", send(prepared))["message_id"]
    runtime.reconcile()
    second = runtime.apply(
        "search_messages", owner(prepared, query="", limit=100, cursor=cursor)
    )
    assert second["max_message_id"] == maximum
    assert new not in [r["id"] for r in second["items"]]
    assert first["items"][0]["id"] not in [r["id"] for r in second["items"]]
    # No visible rows => no global maximum ID disclosure.
    with runtime.transaction(write=True) as db:
        db.execute("DELETE FROM message_recipients WHERE agent_id=3")
        db.execute("UPDATE messages SET sender_id=1 WHERE sender_id=3")
        from agentstack_mail.namespace_store import changed

        changed(db)
    empty = runtime.apply("search_messages", owner(prepared, 3, query=""))
    assert empty["max_message_id"] == 0 and empty["items"] == []


def test_attachment_blob_dedup_metadata_download_quota(prepared):
    import base64, hashlib

    content = b"private attachment"
    sha = hashlib.sha256(content).hexdigest()
    file = {
        "filename": "note.txt",
        "media_type": "text/plain",
        "content_base64": base64.b64encode(content).decode(),
    }
    runtime = S2bRuntime(prepared["config_path"])
    with runtime.transaction() as db:
        initial = runtime.attachment_usage(db, 1)
    runtime.attachment_limit = initial + len(content)
    args = send(prepared, attachments=[file])
    a = runtime.apply("send_message", args)
    b = runtime.apply("send_message", send(prepared, attachments=[file]))
    assert b["message_id"] > a["message_id"]
    with runtime.transaction() as db:
        assert runtime.attachment_usage(db, 1) == initial + len(content)
    result = runtime.apply(
        "search_messages", owner(prepared, query="", limit=1, attachment_sha256=sha)
    )
    assert base64.b64decode(result["attachment"]["content_base64"]) == content
    assert result["items"][0]["attachments"][0]["content_ref"]["sha256"] == sha
    file2 = {**file, "content_base64": base64.b64encode(b"different").decode()}
    with pytest.raises(GlobalError, match="ATTACHMENT_OWNER_CAPACITY_REACHED"):
        runtime.apply("send_message", send(prepared, attachments=[file2]))
    assert runtime.apply("send_message", args) == a


@pytest.mark.parametrize(
    "update,reason",
    [
        ({"thread_id": "old"}, "LEGACY_THREAD_WRITE_NOT_SUPPORTED"),
        ({"attachment_paths": ["/not/read"]}, "ATTACHMENT_PATHS_NOT_SUPPORTED"),
        ({"broadcast": True}, "BROADCAST_NOT_SUPPORTED"),
        ({"auto_contact_if_blocked": True}, "AUTO_CONTACT_NOT_SUPPORTED"),
        ({"convert_images": True}, "IMAGE_CONVERSION_NOT_SUPPORTED"),
        ({"body_md": "é" * 40000}, "PAYLOAD_TOO_LARGE"),
        (
            {
                "attachments": [
                    {
                        "filename": "../x",
                        "media_type": "text/plain",
                        "content_base64": "",
                    }
                ]
            },
            "ATTACHMENT_INVALID",
        ),
        (
            {
                "attachments": [
                    {"filename": "x", "media_type": "text/plain", "content_base64": "*"}
                ]
            },
            "ATTACHMENT_INVALID",
        ),
    ],
)
def test_new_write_rejections_do_not_commit(prepared, update, reason):
    runtime = S2bRuntime(prepared["config_path"])
    args = send(prepared)
    args.update(update)
    with runtime.transaction() as db:
        before = revision(db)
    with pytest.raises(GlobalError, match=reason):
        runtime.apply("send_message", args)
    with runtime.transaction() as db:
        assert revision(db) == before


def test_bcc_hidden_from_other_recipient_but_gets_signal(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    # Explicitly allow sender->3 in the common policy, as an operator fixture.
    with runtime.transaction(write=True) as db:
        db.execute("UPDATE agents SET contact_policy='open' WHERE id=3")
        from agentstack_mail.namespace_store import changed

        changed(db)
    mid = runtime.apply("send_message", send(prepared, bcc_agent_ids=[3]))["message_id"]
    runtime.reconcile()
    for aid in (2, 3):
        assert (
            runtime.runtime_root / "signals/agents" / str(aid) / f"{mid}.signal"
        ).exists()
    page = runtime.apply("search_messages", owner(prepared, query="", limit=1))
    assert page["items"][0]["bcc"] == []
    page = runtime.apply("search_messages", owner(prepared, 3, query="", limit=1))
    assert page["items"][0]["bcc"] == [{"agent_id": 3, "name": "Gamma"}]


def test_foreign_signal_blocks_one_pair_only(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    a = runtime.apply("send_message", send(prepared))["message_id"]
    b = runtime.apply("send_message", send(prepared))["message_id"]
    folder = runtime.runtime_root / "signals/agents/2"
    folder.mkdir(mode=0o700)
    bad = folder / f"{a}.signal"
    bad.write_text("foreign")
    bad.chmod(0o600)
    result = runtime.reconcile()
    assert result["blocked_count"] == 1
    assert bad.read_text() == "foreign" and (folder / f"{b}.signal").exists()
    assert runtime.health_details()["signal"]["status"] == "red"
    bad.unlink()
    runtime.reconcile(retry_blocked=True)
    assert bad.exists() and runtime.health_details()["signal"]["blocked_pairs"] == 0


def test_imported_consumed_management_already_completed_and_no_age(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    with runtime.transaction() as db:
        row = db.execute(
            "SELECT message_id,agent_id FROM message_recipients WHERE notification_fact='imported-consumed' LIMIT 1"
        ).fetchone()
    req = {
        "version": 1,
        "operation": "delivery_completed",
        **{
            k: v
            for k, v in owner(prepared).items()
            if k
            in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        },
        "recipient_agent_id": row["agent_id"],
        "message_ids": [row["message_id"]],
    }
    result = runtime._management_apply(req, os.getuid())
    assert result["items"][0] == {
        "message_id": row["message_id"],
        "state": "already-completed",
        "notified_at": None,
    }
    runtime.reconcile()
    assert runtime.health_details()["signal"]["undelivered_count"] == 0
    assert runtime.health_details()["signal"]["oldest_undelivered_at"] is None


def test_purge_holds_parent_then_clears_signal_blob_without_id_reuse(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    args = send(prepared)
    parent = runtime.apply("send_message", args)["message_id"]
    child = runtime.apply(
        "reply_message",
        owner(
            prepared,
            message_id=parent,
            body_md="new child",
            request_id=str(uuid.uuid4()),
        ),
    )["message_id"]
    runtime.reconcile()
    assert runtime.purge([parent])["held_ids"] == [parent]
    assert runtime.purge([parent, child])["delete_ids"] == [parent, child]
    runtime.reconcile()
    for aid, mid in ((2, parent), (1, child)):
        assert not (
            runtime.runtime_root / "signals/agents" / str(aid) / f"{mid}.signal"
        ).exists()
    fresh = S2bRuntime(prepared["config_path"])
    assert (
        fresh.apply("send_message", args)["message_id"] == parent
    )  # historical receipt, no resurrection
    assert fresh.apply("send_message", send(prepared))["message_id"] > child


def test_schema_load_checks_unselected_branch_and_array_constraints(
    tmp_path, monkeypatch
):
    from agentstack_mail import schema_contract as common

    with pytest.raises(ValueError, match="CAPABILITY_INVALID"):
        common.validate_schema_definition(
            {"anyOf": [{"type": "string"}, {"unknownKeyword": True}]}
        )
    schema = {
        "type": "array",
        "items": {"type": "integer"},
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": True,
    }
    for wrong in ([], [1, 1], [1, 2, 3], [True]):
        with pytest.raises(ValueError):
            common.validate_schema(wrong, schema)
    common.validate_schema([1, 2], schema)


def test_old_schema_root_without_receipt_is_unchanged_before_db_open(
    prepared, monkeypatch
):
    import agentstack_mail.global_server as module

    oldreceipt = prepared["config_path"].parent.parent / "preparation-receipt.json"
    oldreceipt.unlink()
    before = file_state(prepared["root"])

    def no_open(*a, **k):
        pytest.fail("database opened before receipt admission")

    monkeypatch.setattr(module, "connection", no_open)
    with pytest.raises(GlobalError, match="CANDIDATE_SCHEMA_REQUIRES_REPREPARE"):
        S2bRuntime(prepared["config_path"])
    assert file_state(prepared["root"]) == before


def test_notification_source_precedence_and_unresolved_pair(prepared):
    from agentstack_mail.global_prepare_s2b import initialize_notifications

    runtime = S2bRuntime(prepared["config_path"])
    with runtime.transaction(write=True) as db:
        # Existing unread historical pair: only a remaining hint makes it pending.
        signal = {"hint": {"payload": {"message_id": 107, "agent_id": 2}}}
        initialize_notifications(db, [], signal, {}, "2026-10-10T00:00:00+00:00")
        assert (
            db.execute(
                "SELECT notification_fact FROM message_recipients WHERE message_id=107"
            ).fetchone()[0]
            is None
        )
        assert runtime.signal_stats(db)["undelivered_count"] == 1
    with pytest.raises(GlobalError, match="NOTIFICATION_SOURCE_REQUIRES_RESOLUTION"):
        with runtime.transaction(write=True) as db:
            initialize_notifications(
                db,
                [{"message_id": 107, "agent_id": 2, "status": "leased"}],
                signal,
                {},
                "2026-10-10T00:00:00+00:00",
            )


@pytest.mark.parametrize(
    "point",
    [
        "signal_after_unlink",
        "signal_after_dir_fsync",
        "signal_after_dirty",
        "signal_before_commit",
        "signal_after_commit",
    ],
)
def test_clear_signal_interruption_does_not_reemit(prepared, point):
    runtime = S2bRuntime(prepared["config_path"])
    mid = runtime.apply("send_message", send(prepared))["message_id"]
    runtime.reconcile()
    runtime.apply(
        "mark_message_read",
        owner(prepared, message_id=mid, request_id=str(uuid.uuid4())),
    )

    def fail(p):
        if p == point:
            raise RuntimeError("crash")

    runtime.fault = fail
    with pytest.raises(RuntimeError):
        runtime.reconcile()
    fresh = S2bRuntime(prepared["config_path"])
    fresh.reconcile()
    assert not (fresh.runtime_root / "signals/agents/2" / f"{mid}.signal").exists()


def test_delivery_whole_batch_reject_and_unknown_cleanup(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    mid = runtime.apply("send_message", send(prepared))["message_id"]
    req = {
        "version": 1,
        "operation": "delivery_completed",
        **{
            k: v
            for k, v in owner(prepared).items()
            if k
            in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        },
        "recipient_agent_id": 1,
        "message_ids": [99999, mid],
    }
    with runtime.transaction() as db:
        before = revision(db)
    with pytest.raises(GlobalError, match="DELIVERY_RECIPIENT_MISMATCH"):
        runtime._management_apply(req, os.getuid())
    with runtime.transaction() as db:
        assert revision(db) == before
    req["message_ids"] = [99999]
    assert runtime._management_apply(req, os.getuid())["items"][0]["state"] == "unknown"
    assert runtime.reconcile()["pending_count"] == 0


def test_single_intent_resumes_bytes_after_request_and_commit_loss(prepared):
    from agentstack_mail.global_s2b_contract import (
        make_intent,
        resume_intent,
        complete_intent,
    )

    args = send(
        prepared,
        attachments=[
            {
                "filename": "x.bin",
                "media_type": "application/octet-stream",
                "content_base64": "YWJj",
            }
        ],
    )
    intent = make_intent("send_message", args)
    assert args["registration_token"] not in json.dumps(intent)
    assert intent["canonical_intent"]["body_md"] == args["body_md"]
    assert intent["canonical_intent"]["attachments"][0]["content_base64"] == "YWJj"
    owner_args = owner(prepared, 1)
    restored = resume_intent(json.loads(json.dumps(intent)), owner_args)
    runtime = S2bRuntime(prepared["config_path"])
    committed = runtime.apply("send_message", restored)
    assert runtime.apply("send_message", resume_intent(intent, owner_args)) == committed
    assert complete_intent(intent, committed, owner_args)["phase"] == "committed"
    wrong = {**committed, "committed_at": "invalid"}
    with pytest.raises(GlobalError, match="RESPONSE_INVALID"):
        complete_intent(intent, wrong, owner_args)
    with pytest.raises(GlobalError, match="MUTATION_PENDING_REQUIRES_RESOLUTION"):
        resume_intent(intent, {**owner_args, "authority_epoch": "other"})


def test_incident_inspection_and_quarantine_schema4(prepared):
    from agentstack_mail.global_incident import inspect_incident, quarantine

    runtime = S2bRuntime(prepared["config_path"])
    with connection(runtime.paths["database"], write=True) as db:
        db.execute(
            "UPDATE namespace_metadata SET value=CAST(value AS INTEGER)+1 WHERE key='write_generation'"
        )
    with pytest.raises(GlobalError, match="RUNTIME_WRITER_MISMATCH"):
        S2bRuntime(prepared["config_path"])
    inspection = inspect_incident(prepared["config_path"])
    assert not inspection["runtime_validated"]
    result = quarantine(
        prepared["config_path"],
        request_id=str(uuid.uuid4()),
        incident_digest=inspection["incident_digest"],
        tracked_revision=inspection["manifest"]["tracked_revision"],
        current_revision=inspection["manifest"]["current_revision"],
        confirm=True,
    )
    assert result["authority"]["root_status"] == "retired"


@pytest.mark.parametrize(
    "point",
    [
        "after_plan",
        "s2b_before_extension",
        *[f"s2b_after_ddl_{i}" for i in range(6)],
        "s2b_before_extension_commit",
        "s2b_after_extension_commit",
        "s2b_after_staged_control",
        "pr3_final_verified_after",
        "before_ready",
        "after_ready",
        "before_publish",
        "after_publish",
        "before_complete",
        "after_complete",
    ],
)
def test_fresh_schema4_preparation_interruption_restarts(prepared, point):
    request = str(uuid.uuid4())
    fired = False

    def fault(actual):
        nonlocal fired
        if actual == point and not fired:
            fired = True
            raise RuntimeError("synthetic crash")

    with pytest.raises(RuntimeError):
        prepare(
            prepared["source"],
            prepared["root"],
            "two",
            prepared["choices"],
            request_id=request,
            fence=fence,
            fault=fault,
        )
    assert fired
    cfg = prepared["root"] / "s2b-candidates/two/candidate/server-config.json"
    if cfg.exists() and point != "after_complete":
        with pytest.raises(GlobalError, match="PREPARATION_INCOMPLETE"):
            S2bRuntime(cfg)
    assert (
        prepare(
            prepared["source"],
            prepared["root"],
            "two",
            prepared["choices"],
            request_id=request,
            fence=fence,
        )["phase"]
        == "complete"
    )
    with S2bRuntime(cfg).transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_1001_chain_keyset_never_loses_tail(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    from agentstack_mail.namespace_store import changed

    with runtime.transaction(write=True) as db:
        parent = None
        for mid in range(1000, 2001):
            db.execute(
                "INSERT INTO messages(id,sender_id,subject,body_md,importance,ack_required,created_ts,attachments,reply_to) VALUES(?,1,'chain','body','normal',0,'2026-10-10T00:00:00+00:00','[]',?)",
                (mid, parent),
            )
            db.execute(
                "INSERT INTO message_recipients(message_id,agent_id,kind,notification_fact) VALUES(?,2,'to','imported-consumed')",
                (mid,),
            )
            parent = mid
        db.execute(
            "UPDATE namespace_metadata SET value='2000' WHERE key='message_id_high_water'"
        )
        changed(db)
    ids = []
    cursor = None
    while True:
        page = runtime.apply(
            "summarize_thread",
            owner(prepared, message_id=1000, per_thread_limit=100, cursor=cursor),
        )
        ids += [mid for group in page["items"] for mid in group["source_message_ids"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert ids == list(range(2000, 999, -1))


def test_large_legacy_metadata_and_small_response_budget(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    from agentstack_mail.namespace_store import changed
    import hashlib

    blob = b"x" * 300000
    sha = hashlib.sha256(blob).hexdigest()
    with runtime.transaction(write=True) as db:
        db.execute(
            "INSERT INTO global_attachment_blobs VALUES(?,?,?)", (sha, len(blob), blob)
        )
        db.execute(
            "INSERT INTO global_message_attachments VALUES(107,0,?,'historical.bin','application/octet-stream')",
            (sha,),
        )
        changed(db)
    fresh = S2bRuntime(prepared["config_path"])
    page = fresh.apply("search_messages", owner(prepared, query="", limit=1))
    assert page["items"][0]["attachments"][0]["byte_size"] == 300000
    download = fresh.apply(
        "search_messages", owner(prepared, query="", limit=1, attachment_sha256=sha)
    )
    assert download["attachment"]["byte_size"] == 300000
    # Complete rows only: a legacy body exceeding the final envelope budget
    # refuses inbox, but metadata remains readable and nothing is marked read.
    with fresh.transaction(write=True) as db:
        db.execute("UPDATE messages SET body_md=? WHERE id=107", ("x" * 600000,))
        changed(db)
    with pytest.raises(GlobalError, match="MESSAGE_RESPONSE_TOO_LARGE"):
        fresh.inbox(owner(prepared, limit=1, include_bodies=True, urgent_only=False))
    assert (
        fresh.inbox(owner(prepared, limit=1, include_bodies=False, urgent_only=False))[
            0
        ]["id"]
        == 107
    )


def test_retirement_and_future_backoff_do_not_starve_other_pairs(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    a = runtime.apply("send_message", send(prepared))["message_id"]
    b = runtime.apply("send_message", send(prepared))["message_id"]
    from agentstack_mail.namespace_store import changed

    with runtime.transaction(write=True) as db:
        db.execute(
            "UPDATE message_recipients SET notify_after_ts='2999-01-01T00:00:00+00:00' WHERE message_id=?",
            (a,),
        )
        changed(db)
    runtime.reconcile(batch_limit=1)
    assert (runtime.runtime_root / "signals/agents/2" / f"{b}.signal").exists()
    assert not (runtime.runtime_root / "signals/agents/2" / f"{a}.signal").exists()
    runtime.lifecycle(owner(prepared), True)
    runtime.reconcile()
    assert not (runtime.runtime_root / "signals/agents/2" / f"{b}.signal").exists()
    runtime.lifecycle(owner(prepared), False)
    runtime.reconcile()
    assert (runtime.runtime_root / "signals/agents/2" / f"{b}.signal").exists()


@pytest.mark.parametrize(
    "point", ["after_state", "after_receipt", "before_commit", "after_commit"]
)
def test_real_sigkill_write_replay_has_one_message(prepared, point):
    import signal

    args = send(prepared)
    script = """
import json,os,signal,sys
from agentstack_mail.global_s2b import S2bRuntime
runtime=S2bRuntime(sys.argv[1])
def fault(actual):
    if actual==sys.argv[2]:os.kill(os.getpid(),signal.SIGKILL)
runtime.fault=fault
runtime.apply('send_message',json.loads(sys.argv[3]))
"""
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("AGENTSTACK_", "AGS_"))
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(prepared["config_path"]),
            point,
            json.dumps(args),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    runtime = S2bRuntime(prepared["config_path"])
    receipt = runtime.apply("send_message", args)
    assert runtime.apply("send_message", args) == receipt
    with runtime.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM messages WHERE id>107").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "point",
    [
        "signal_after_write",
        "signal_after_rename",
        "signal_after_dirty",
        "signal_after_commit",
    ],
)
def test_real_sigkill_signal_reconciliation(prepared, point):
    import signal

    runtime = S2bRuntime(prepared["config_path"])
    mid = runtime.apply("send_message", send(prepared))["message_id"]
    script = """
import os,signal,sys
from agentstack_mail.global_s2b import S2bRuntime
runtime=S2bRuntime(sys.argv[1])
def fault(actual):
    if actual==sys.argv[2]:os.kill(os.getpid(),signal.SIGKILL)
runtime.fault=fault
runtime.reconcile()
"""
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("AGENTSTACK_", "AGS_"))
    }
    proc = subprocess.run(
        [sys.executable, "-c", script, str(prepared["config_path"]), point],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    fresh = S2bRuntime(prepared["config_path"])
    result = fresh.reconcile()
    assert result["blocked_count"] == 0
    assert (fresh.runtime_root / "signals/agents/2" / f"{mid}.signal").exists()


def test_signal_red_does_not_prevent_existing_4a_client_tools(server):
    module_path = (
        Path(__file__).resolve().parents[3] / "tests/test_global_runtime_client_s2a.py"
    )
    spec = importlib.util.spec_from_file_location(
        "existing_client_fixture", module_path
    )
    client_fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client_fixture)
    client = client_fixture.context(server)
    folder = Path(server["config"]["runtime_root"]) / "signals/agents/2"
    folder.mkdir(mode=0o700, exist_ok=True)
    foreign = folder / "108.signal"
    foreign.write_text("foreign")
    foreign.chmod(0o600)
    asyncio.run(http(server, "send_message", send(server)))
    deadline = time.monotonic() + 5
    while True:
        health = asyncio.run(http(server, "health_check"))
        if health["signal"]["status"] == "red":
            break
        assert time.monotonic() < deadline, health
        time.sleep(0.05)
    assert health["status"] == "ok"
    assert client.call("whois")["agent_id"] == 2
    assert client.call("fetch_inbox", {"include_bodies": False})[0]["id"] == 108


def test_server_fence_failure_cannot_report_ready(server):
    authority = Path(server["config"]["authority"])
    before = authority.read_bytes()
    value = json.loads(before)
    value["phase"] = "quiescing"
    authority.write_text(json.dumps(value))
    try:
        with pytest.raises(Exception, match="WRITER_FENCED"):
            asyncio.run(http(server, "health_check"))
    finally:
        authority.write_bytes(before)


def test_remote_catalog_rejects_unknown_unselected_schema_branch(server):
    module_path = (
        Path(__file__).resolve().parents[3] / "tests/test_global_runtime_client_s2a.py"
    )
    spec = importlib.util.spec_from_file_location("catalog_client_fixture", module_path)
    client_fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client_fixture)
    client = client_fixture.context(server)
    original = client.rpc

    def changed_catalog(method, params):
        reply = original(method, params)
        if method == "tools/list":
            for tool in reply["result"]["tools"]:
                if tool["name"] == "refresh_registration":
                    tool["outputSchema"] = {
                        "anyOf": [tool["outputSchema"], {"unknownKeyword": True}]
                    }
        return reply

    client.rpc = changed_catalog
    with pytest.raises(client_fixture.ClientError, match="CAPABILITY_INVALID"):
        client.call("whois")
    assert not client.outputs["mutation"].exists()


def test_sqlite_id_bounds_have_fixed_reason_and_do_not_reuse(prepared):
    runtime = S2bRuntime(prepared["config_path"])
    with pytest.raises(GlobalError, match="ARGUMENTS_UNSUPPORTED"):
        runtime.apply("send_message", {**send(prepared), "to_agent_ids": [2**64]})
    with pytest.raises(GlobalError, match="QUERY_INVALID"):
        runtime.apply("summarize_thread", owner(prepared, thread_id=str(2**64)))
    from agentstack_mail.namespace_store import changed

    with runtime.transaction(write=True) as db:
        db.execute(
            "UPDATE namespace_metadata SET value=? WHERE key='message_id_high_water'",
            (str(2**63 - 1),),
        )
        changed(db)
    with pytest.raises(GlobalError, match="PAYLOAD_TOO_LARGE"):
        runtime.apply("send_message", send(prepared))


def test_ten_thousand_absent_legacy_hints_do_not_create_backlog(prepared):
    from agentstack_mail.global_prepare_s2b import initialize_notifications
    from agentstack_mail.namespace_store import changed

    runtime = S2bRuntime(prepared["config_path"])
    with runtime.transaction(write=True) as db:
        db.executemany(
            "INSERT INTO messages(id,sender_id,subject,body_md,importance,ack_required,created_ts,attachments) VALUES(?,1,'history','old','normal',0,'2026-10-08T00:00:00+00:00','[]')",
            ((i,) for i in range(1000, 11000)),
        )
        db.executemany(
            "INSERT INTO message_recipients(message_id,agent_id,kind) VALUES(?,2,'to')",
            ((i,) for i in range(1000, 11000)),
        )
        proof = initialize_notifications(db, [], {}, {}, "2026-10-10T00:00:00+00:00")
        assert sum(r["dirty"] for r in proof["pairs"]) == 0
        assert runtime.signal_stats(db)["undelivered_count"] == 0
        assert db.execute("SELECT count(*) FROM global_signal_dirty").fetchone()[0] == 0
        db.execute(
            "UPDATE namespace_metadata SET value='10999' WHERE key='message_id_high_water'"
        )
        changed(db)
    assert runtime.reconcile()["items"] == []


def test_notification_resolution_requires_source_digest_and_preserves_backoff(prepared):
    from agentstack_mail.global_prepare_s2b import initialize_notifications
    from agentstack_mail.global_s2a_contract import digest

    runtime = S2bRuntime(prepared["config_path"])
    delivery = [{"message_id": 107, "agent_id": 2, "status": "leased"}]
    signals = {}
    resolutions = {
        "source_delivery_digest": digest(delivery),
        "source_signals_digest": digest(signals),
        "pairs": {
            "107:2": {"action": "retry_after", "at": "2999-01-01T00:00:00+00:00"}
        },
    }
    with runtime.transaction(write=True) as db:
        proof = initialize_notifications(
            db, delivery, signals, resolutions, "2026-10-10T00:00:00+00:00"
        )
        row = next(r for r in proof["pairs"] if r["message_id"] == 107)
        assert row["dirty"] and row["notify_after_ts"] == "2999-01-01T00:00:00+00:00"
        from agentstack_mail.namespace_store import changed

        changed(db)
    assert runtime.reconcile()["items"] == []
    with pytest.raises(GlobalError, match="NOTIFICATION_SOURCE_REQUIRES_RESOLUTION"):
        with runtime.transaction(write=True) as db:
            initialize_notifications(
                db,
                delivery,
                signals,
                {**resolutions, "source_delivery_digest": "wrong"},
                "2026-10-10T00:00:00+00:00",
            )


@pytest.mark.parametrize("delivered_at", [None, "not-a-date"])
@pytest.mark.parametrize("read", [False, True])
def test_invalid_legacy_delivery_timestamp_requires_resolution_unless_read(
    prepared, delivered_at, read
):
    from agentstack_mail.global_prepare_s2b import initialize_notifications

    runtime = S2bRuntime(prepared["config_path"])
    delivery = [
        {
            "message_id": 107,
            "agent_id": 2,
            "status": "delivered",
            "delivered_at": delivered_at,
        }
    ]
    with runtime.transaction(write=True) as db:
        if read:
            db.execute(
                "UPDATE message_recipients SET read_ts='2026-10-10T00:00:00+00:00' WHERE message_id=107"
            )
            proof = initialize_notifications(
                db, delivery, {}, {}, "2026-10-10T00:00:00+00:00"
            )
            row = next(r for r in proof["pairs"] if r["message_id"] == 107)
            assert row["notification_fact"] is None and row["dirty"] is False
        else:
            with pytest.raises(
                GlobalError, match="NOTIFICATION_SOURCE_REQUIRES_RESOLUTION"
            ):
                initialize_notifications(
                    db, delivery, {}, {}, "2026-10-10T00:00:00+00:00"
                )
