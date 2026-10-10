"""S2a acceptance: synthetic sources/private HOME and real HTTP/Unix socket."""

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

from agentstack_mail.global_server import GlobalError, GlobalRuntime
from agentstack_mail.global_s2a import S2aRuntime, revision, validate_runtime_candidate
from agentstack_mail.global_s2a_contract import FIXTURE, TOOLS
from agentstack_mail.global_prepare import prepare
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
    request_id = str(uuid.uuid4())
    result = prepare(source, root, "one", choices, request_id=request_id, fence=fence)
    candidate = root / "s2a-candidates/one/candidate"
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
    return S2aRuntime(state["config_path"]).apply(
        tool, owner(state, aid, request_id=str(uuid.uuid4()), **extra)
    )


def file_state(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_fresh_preparation_and_restart(prepared):
    state = prepared
    runtime = S2aRuntime(state["config_path"])
    with runtime.transaction() as db:
        assert revision(db) == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        validate_runtime_candidate(db, full=True)
    assert (
        prepare(
            state["source"],
            state["root"],
            "one",
            state["choices"],
            request_id=state["request_id"],
            fence=fence,
        )
        == state["receipt"]
    )
    with pytest.raises(GlobalError):
        GlobalRuntime(state["config_path"])


def test_receipt_preimage_replay_and_conflict(prepared):
    runtime = S2aRuntime(prepared["config_path"])
    args = owner(prepared, request_id=str(uuid.uuid4()), task_description="")
    first = runtime.apply("refresh_registration", args)
    assert first["program"] == first["model"] == "fixture"
    assert (
        runtime.apply("refresh_registration", {**args, "project_key": "ignored"})
        == first
    )
    with pytest.raises(GlobalError, match="REQUEST_ID_CONFLICT"):
        runtime.apply("refresh_registration", {**args, "task_description": None})
    with runtime.transaction() as db:
        assert revision(db) == 2
        row = db.execute("SELECT * FROM global_operation_receipts").fetchone()
        assert (
            json.loads(row["canonical_request_json"])["arguments"]["task_description"]
            == ""
        )
        validate_runtime_candidate(db, full=True)


def test_contact_target_consent_and_pagination(prepared):
    runtime = S2aRuntime(prepared["config_path"])
    requested = runtime.apply(
        "request_contact",
        owner(prepared, 3, to_agent_id=2, request_id=str(uuid.uuid4())),
    )
    assert requested["status"] == "pending"
    approved = runtime.apply(
        "respond_contact",
        owner(prepared, 2, from_agent_id=3, accept=True, request_id=str(uuid.uuid4())),
    )
    assert approved["id"] == requested["id"]
    assert approved["effective"]
    noop = runtime.apply(
        "request_contact",
        owner(prepared, 3, to_agent_id=2, request_id=str(uuid.uuid4())),
    )
    assert noop["expires_ts"] == approved["expires_ts"]
    blocked = runtime.apply(
        "respond_contact",
        owner(prepared, 2, from_agent_id=3, accept=False, request_id=str(uuid.uuid4())),
    )
    assert blocked["status"] == "blocked" and blocked["expires_ts"] is None
    macro = runtime.apply(
        "macro_contact_handshake",
        owner(prepared, 3, to_agent_id=2, request_id=str(uuid.uuid4())),
    )
    assert macro["approval_required"] and macro["contact"]["status"] == "blocked"
    for tool, args in [
        ("request_contact", {"to_agent_id": 99}),
        ("macro_contact_handshake", {"to_agent_id": 2, "auto_accept": True}),
    ]:
        with pytest.raises(GlobalError):
            runtime.apply(
                tool, owner(prepared, 3, request_id=str(uuid.uuid4()), **args)
            )
    listing = runtime.apply("list_contacts", owner(prepared, 3, limit=1))
    assert listing["items"][0]["to_identity"]["name"] == "Beta"
    with runtime.transaction() as db:
        assert not runtime.authorize_delivery(db, 3, 2)


@pytest.mark.parametrize(
    "point",
    ["before_state", "after_state", "after_receipt", "before_commit", "after_commit"],
)
def test_mutation_interrupt_replay(prepared, point):
    runtime = S2aRuntime(prepared["config_path"])
    args = owner(prepared, policy="auto", request_id=str(uuid.uuid4()))

    def fail(actual):
        if actual == point:
            raise OSError("synthetic interruption")

    runtime.fault = fail
    with pytest.raises((OSError, GlobalError)):
        runtime.apply("set_contact_policy", args)
    runtime = S2aRuntime(prepared["config_path"])
    result = runtime.apply("set_contact_policy", args)
    assert result["policy"] == "auto"
    with runtime.transaction() as db:
        assert (
            revision(db) == 2
            and db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[
                0
            ]
            == 1
        )


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
                            "s2a-v1",
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


def test_server_fixture_waits_for_http_after_management_socket(tmp_path, monkeypatch):
    management = tmp_path / "management.sock"
    management.touch()
    state = {
        "root": tmp_path,
        "config": {"management_socket": str(management)},
        "config_path": tmp_path / "server-config.json",
    }
    calls = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            management.unlink()

        def wait(self, timeout):
            return 0

    async def delayed_http(state, name, args=None):
        calls.append(name)
        if len(calls) < 3:
            raise ConnectionRefusedError("synthetic HTTP startup delay")
        return {"status": "ok", "runtime_profile": "s2a-v1"}

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setitem(globals(), "http", delayed_http)
    fixture = server.__wrapped__(state)
    try:
        assert next(fixture) is state
        assert calls == ["health_check"] * 3
    finally:
        fixture.close()


async def http(state, name, args=None):
    async with Client(state["url"]) as client:
        if name == "tools":
            return await client.list_tools()
        response = await client.call_tool(name, args or {})
        return json.loads(next(p.text for p in response.content if hasattr(p, "text")))


def test_actual_http_catalog_contact_and_socket(server):
    tools = asyncio.run(http(server, "tools"))
    found = {t.name: t for t in tools}
    for name, contract in TOOLS.items():
        assert found[name].inputSchema == contract["input_schema"]
        assert found[name].outputSchema == contract["output_schema"]
    health = asyncio.run(http(server, "health_check"))
    assert health["runtime_profile"] == "s2a-v1" and health["capabilities"] == sorted(
        FIXTURE["capabilities_added"]
    )
    response = asyncio.run(
        http(
            server,
            "set_contact_policy",
            owner(server, policy="auto", request_id=str(uuid.uuid4())),
        )
    )
    assert response["policy"] == "auto"
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(server["config"]["management_socket"])
        s.sendall(
            json.dumps(
                {
                    "version": 1,
                    "action": "inspect",
                    **{
                        k: v
                        for k, v in owner(server).items()
                        if k
                        in (
                            "expected_server_instance_id",
                            "candidate_generation",
                            "authority_epoch",
                            "agent_id",
                        )
                    },
                }
            ).encode()
            + b"\n"
        )
        data = b""
        while not data.endswith(b"\n"):
            data += s.recv(16384)
    inspect = json.loads(data)
    assert inspect["ok"] and inspect["receipt_capacity"]["owner"]["used"] == 1


@pytest.mark.parametrize("suffix", ["clean-wal", "with-sidecars", "s1-written"])
def test_schema2_refusal_never_opens_sqlite_or_changes_files(
    prepared, suffix, monkeypatch
):
    from agentstack_mail.namespace_migration import NamespaceMigration

    root = prepared["root"]
    migration = NamespaceMigration(
        prepared["source"], root / "old-schema2", prepared["choices"]
    )
    migration.resume(fence=fence)
    dbpath = migration.workspace / "candidate/mail.sqlite3"
    with connection(dbpath, write=True) as db:
        db.execute("PRAGMA journal_mode=WAL")
        if suffix == "s1-written":
            db.execute("UPDATE agents SET task_description='S1-only value' WHERE id=1")
            db.execute(
                "UPDATE namespace_metadata SET value='1' WHERE key='write_generation'"
            )
    held = None
    if suffix == "with-sidecars":
        import sqlite3

        held = sqlite3.connect(dbpath)
        held.execute("SELECT * FROM agents").fetchone()
    cfg = {
        **prepared["config"],
        "database": str(dbpath),
        "runtime_root": str(dbpath.parent),
    }
    bad = root / "reject-config.json"
    bad.write_text(json.dumps(cfg))
    bad.chmod(0o600)
    before = file_state(root)
    original_open = os.open

    def refuse_database_open(path, *args, **kwargs):
        if Path(path) == dbpath:
            pytest.fail("missing receipt must refuse before opening the database file")
        return original_open(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", refuse_database_open)
            with pytest.raises(
                GlobalError, match="CANDIDATE_SCHEMA_REQUIRES_REPREPARE"
            ):
                S2aRuntime(bad)
        assert file_state(root) == before
        from dataclasses import replace

        with pytest.raises(GlobalError, match="CANDIDATE_SCHEMA_REQUIRES_REPREPARE"):
            prepare(
                replace(prepared["source"], mail=dbpath),
                root,
                "bad",
                prepared["choices"],
                request_id=str(uuid.uuid4()),
                fence=fence,
            )
        assert file_state(root) == before
    finally:
        if held:
            held.close()


@pytest.mark.parametrize(
    "point",
    [
        "after_plan",
        "after_ddl_0",
        "after_ddl_1",
        "after_ddl_2",
        "before_extension_commit",
        "after_extension_commit",
        "after_staged_control",
        "pr3_final_verified_after",
        "before_ready",
        "after_ready",
        "before_publish",
        "after_publish",
        "before_complete",
        "after_complete",
    ],
)
def test_fresh_preparation_interruption_is_recoverable(prepared, point):
    root = prepared["root"]
    request = str(uuid.uuid4())
    fired = False

    def fault(actual):
        nonlocal fired
        if not fired and actual == point:
            fired = True
            raise RuntimeError("synthetic preparation crash")

    with pytest.raises(RuntimeError):
        prepare(
            prepared["source"],
            root,
            "two",
            prepared["choices"],
            request_id=request,
            fence=fence,
            fault=fault,
        )
    assert fired
    path = root / "s2a-candidates/two/candidate/server-config.json"
    if path.exists() and point != "after_complete":
        with pytest.raises(GlobalError, match="PREPARATION_INCOMPLETE"):
            S2aRuntime(path)
    receipt = prepare(
        prepared["source"],
        root,
        "two",
        prepared["choices"],
        request_id=request,
        fence=fence,
    )
    assert receipt["phase"] == "complete"
    runtime = S2aRuntime(path)
    with runtime.transaction() as db:
        assert revision(db) == 1


@pytest.mark.parametrize(
    "point", ["after_incident", "after_fence", "after_retire", "after_receipt"]
)
def test_incident_quarantine_interrupt_and_retained_evidence(prepared, point):
    from agentstack_mail.global_incident import inspect_incident, quarantine

    dbpath = Path(prepared["config"]["database"])
    with connection(dbpath, write=True) as db:
        db.execute(
            "UPDATE namespace_metadata SET value='2' WHERE key='write_generation'"
        )
    with pytest.raises(GlobalError, match="RUNTIME_WRITER_MISMATCH"):
        S2aRuntime(prepared["config_path"])
    inspection = inspect_incident(prepared["config_path"])
    before = dbpath.read_bytes()
    m = inspection["manifest"]
    request = str(uuid.uuid4())
    args = {
        "request_id": request,
        "incident_digest": inspection["incident_digest"],
        "tracked_revision": m["tracked_revision"],
        "current_revision": m["current_revision"],
        "confirm": True,
    }

    def fault(actual):
        if actual == point:
            raise RuntimeError("synthetic incident crash")

    with pytest.raises(RuntimeError):
        quarantine(prepared["config_path"], **args, fault=fault)
    result = quarantine(prepared["config_path"], **args)
    assert (
        result["status"] == "retired-evidence-retained"
        and dbpath.read_bytes() == before
    )
    assert quarantine(prepared["config_path"], **args) == result
    with pytest.raises(GlobalError, match="WRITER_FENCED"):
        S2aRuntime(prepared["config_path"])


def test_quota_is_per_owner_replay_and_touch_survive(prepared):
    path = prepared["config_path"]
    cfg = prepared["config"]
    cfg["operation_receipt_limit"] = 1
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    runtime = S2aRuntime(path)
    args = owner(prepared, policy="auto", request_id=str(uuid.uuid4()))
    result = runtime.apply("set_contact_policy", args)
    assert runtime.apply("set_contact_policy", args) == result
    with pytest.raises(GlobalError, match="REQUEST_RECEIPT_CAPACITY_REACHED"):
        runtime.apply(
            "set_contact_policy",
            owner(prepared, policy="open", request_id=str(uuid.uuid4())),
        )
    runtime.apply(
        "set_contact_policy",
        owner(prepared, 1, policy="open", request_id=str(uuid.uuid4())),
    )
    with runtime.transaction() as db:
        window = db.execute(
            "SELECT * FROM window_identities WHERE agent_id=2"
        ).fetchone()
    args = owner(
        prepared, window_row_id=window["id"], window_uuid=window["window_uuid"]
    )
    runtime.apply("touch_window_identity", args)
    assert runtime.apply("verify_window_identity", args)["ready"]
    with runtime.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    "column",
    ["canonical_request_json", "request_hash", "receipt_sha256", "authority_epoch"],
)
def test_candidate_only_receipt_validation_rejects_corruption(prepared, column):
    mutate(prepared, "refresh_registration", task_description=None)
    dbpath = Path(prepared["config"]["database"])
    with connection(dbpath, write=True) as db:
        db.execute(
            "UPDATE global_operation_receipts SET " + column + "=?", ("corrupt",)
        )
    with pytest.raises(GlobalError, match="REQUEST_RECEIPT_INVALID"):
        S2aRuntime(prepared["config_path"])


@pytest.mark.parametrize(
    "tool,extra",
    [
        ("refresh_registration", {}),
        ("verify_window_identity", {"window": True}),
        ("touch_window_identity", {"window": True}),
        ("request_contact", {"to_agent_id": 3}),
        ("respond_contact", {"from_agent_id": 3, "accept": True}),
        ("list_contacts", {}),
        ("set_contact_policy", {"policy": "auto"}),
        ("macro_contact_handshake", {"to_agent_id": 3}),
        ("resolve_agent_identity", {"target_name": "Gamma"}),
    ],
)
def test_generation_zero_accepted_by_all_nine_tools(prepared, tool, extra):
    dbpath = Path(prepared["config"]["database"])
    with connection(dbpath, write=True) as db:
        db.execute("UPDATE agents SET credential_generation=0 WHERE id=2")
        window = db.execute(
            "SELECT id,window_uuid FROM window_identities WHERE agent_id=2"
        ).fetchone()
    args = owner(prepared, expected_credential_generation=0)
    extra = dict(extra)
    if extra.pop("window", False):
        args.update(window_row_id=window["id"], window_uuid=window["window_uuid"])
    args.update(extra)
    if TOOLS[tool]["operation"] == "write":
        args["request_id"] = str(uuid.uuid4())
    result = S2aRuntime(prepared["config_path"]).apply(tool, args)
    assert result["mutation_revision"] >= 1
    with connection(dbpath) as db:
        assert (
            db.execute(
                "SELECT credential_generation FROM agents WHERE id=2"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "tool,extra",
    [
        ("request_contact", {"to_agent_id": 3, "ttl_seconds": True}),
        ("request_contact", {"to_agent_id": 3, "ttl_seconds": -1}),
        ("request_contact", {"to_agent_id": 3, "ttl_seconds": 10**30}),
        ("set_contact_policy", {"policy": "unknown"}),
        ("macro_contact_handshake", {"to_agent_id": 3, "welcome_body": ""}),
        ("request_contact", {"to_agent_id": 2}),
        ("refresh_registration", {"task_description": "x" * 2049}),
        ("request_contact", {"to_agent_id": 3, "unknown": "value"}),
    ],
)
def test_invalid_arguments_never_create_receipt_or_increment_revision(
    prepared, tool, extra
):
    runtime = S2aRuntime(prepared["config_path"])
    with pytest.raises(GlobalError):
        runtime.apply(tool, owner(prepared, request_id=str(uuid.uuid4()), **extra))
    with runtime.transaction() as db:
        assert revision(db) == 1
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 0
        )


def test_clock_rollback_touch_and_register_never_regress(prepared, monkeypatch):
    import agentstack_mail.global_s2a as s2a
    import agentstack_mail.global_server as s1

    dbpath = Path(prepared["config"]["database"])
    stamp = "2060-01-01T10:00:00+09:00"
    with connection(dbpath, write=True) as db:
        db.execute("UPDATE agents SET last_active_ts=? WHERE id=2", (stamp,))
        db.execute(
            "UPDATE window_identities SET last_active_ts=?,expires_ts=? WHERE agent_id=2",
            (stamp, "2061-01-01T00:00:00+00:00"),
        )
        window = db.execute(
            "SELECT * FROM window_identities WHERE agent_id=2"
        ).fetchone()
    monkeypatch.setattr(s2a, "now", lambda: "2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(s1, "now", lambda: "2020-01-01T00:00:00+00:00")
    runtime = S2aRuntime(prepared["config_path"])
    args = owner(
        prepared, window_row_id=window["id"], window_uuid=window["window_uuid"]
    )
    assert runtime.apply("touch_window_identity", args)["mutation_revision"] == 1
    runtime.register(
        tuple(
            args[k]
            for k in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        ),
        2,
        None,
        "fixture",
        "fixture",
        "",
        args["registration_token"],
        window["id"],
        window["window_uuid"],
    )
    with runtime.transaction() as db:
        assert (
            db.execute("SELECT last_active_ts FROM agents WHERE id=2").fetchone()[0]
            == stamp
        )
        actual = db.execute(
            "SELECT * FROM window_identities WHERE id=?", (window["id"],)
        ).fetchone()
        assert (
            actual["last_active_ts"] == stamp
            and actual["expires_ts"] == window["expires_ts"]
        )
        assert revision(db) == 2


def test_read_snapshot_does_not_report_false_gap_during_normal_commit(
    prepared, monkeypatch
):
    import threading
    import agentstack_mail.global_server as s1

    dbpath = Path(prepared["config"]["database"])
    with connection(dbpath, write=True) as db:
        db.execute("PRAGMA journal_mode=WAL")
    runtime = S2aRuntime(prepared["config_path"])
    old = s1.generation
    completed = threading.Event()
    errors = []
    once = False

    def generation(db):
        nonlocal once
        value = old(db)
        if threading.current_thread() is threading.main_thread() and not once:
            once = True

            def writer():
                try:
                    mutate(prepared, "set_contact_policy", policy="auto")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    completed.set()

            thread = threading.Thread(target=writer)
            thread.start()
            assert completed.wait(10)
            thread.join()
        return value

    monkeypatch.setattr(s1, "generation", generation)
    result = runtime.apply("list_contacts", owner(prepared))
    assert not errors and result["mutation_revision"] == 1
    assert runtime.apply("list_contacts", owner(prepared))["mutation_revision"] == 2


def test_delivery_primitive_block_priority_and_offset_exchange(prepared):
    from datetime import datetime, timezone

    runtime = S2aRuntime(prepared["config_path"])
    stamp = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    with runtime.transaction(write=True) as db:
        db.execute("UPDATE agents SET contact_policy='open' WHERE id=2")
        db.execute(
            "UPDATE agent_links SET status='blocked',expires_ts='2000-01-01T00:00:00+00:00' WHERE a_agent_id=1 AND b_agent_id=2"
        )
        assert not runtime.authorize_delivery(db, 1, 2, stamp)
        db.execute(
            "UPDATE agent_links SET status='approved' WHERE a_agent_id=1 AND b_agent_id=2"
        )
        db.execute("UPDATE agents SET contact_policy='block_all' WHERE id=2")
        assert not runtime.authorize_delivery(db, 1, 2, stamp)
        assert runtime.authorize_delivery(db, 2, 2, stamp)
        db.execute("UPDATE agents SET contact_policy='contacts_only' WHERE id=2")
        assert runtime.authorize_delivery(
            db, 1, 2, stamp
        )  # approved expiry is informational
        db.execute("DELETE FROM agent_links WHERE a_agent_id=1 AND b_agent_id=2")
        assert not runtime.authorize_delivery(db, 1, 2, stamp)
        db.execute("UPDATE agents SET contact_policy='auto' WHERE id=2")
        # 08:00 -03:00 is more recent than the lexically later 15:00 +09:00.
        db.execute(
            "UPDATE messages SET sender_id=1,created_ts='2026-10-09T08:00:00-03:00'"
        )
        db.execute("UPDATE message_recipients SET agent_id=2")
        assert runtime.authorize_delivery(db, 1, 2, stamp)
        db.execute(
            "UPDATE messages SET sender_id=2,created_ts='2026-10-08T01:00:00+09:00'"
        )
        db.execute("UPDATE message_recipients SET agent_id=1")
        assert not runtime.authorize_delivery(db, 1, 2, stamp)
        db.execute("UPDATE messages SET created_ts='2026-10-09T20:00:00+09:00'")
        assert runtime.authorize_delivery(
            db, 1, 2, stamp
        )  # reverse exchange also counts


def test_operator_prepare_cli_uses_explicit_source_fence_and_completed_config(prepared):
    from dataclasses import asdict
    from agentstack_mail.namespace_plan import fingerprint

    source = prepared["source"]
    roster = tuple(json.loads(source.config.read_text())["expected_writers"])
    evidence = fence(fingerprint(source.manifest()), roster)
    plan = {
        "kind": "orrery-s2a-preparation-plan-v1",
        "activation_enabled": False,
        "isolation_root": str(prepared["root"]),
        "candidate_name": "operator-cli",
        "request_id": str(uuid.uuid4()),
        "source_paths": {key: str(value) for key, value in asdict(source).items()},
        "choices": prepared["choices"],
        "fence_evidence": asdict(evidence),
    }
    path = prepared["root"] / "operator-plan.json"
    path.write_text(json.dumps(plan))
    path.chmod(0o600)
    command = [
        sys.executable,
        "-c",
        "from agentstack_mail.global_prepare import main; main()",
        "--plan",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    receipt = json.loads(result.stdout)
    assert receipt["phase"] == "complete" and receipt["activation_enabled"] is False
    config = (
        prepared["root"] / "s2a-candidates/operator-cli/candidate/server-config.json"
    )
    with S2aRuntime(config).transaction() as db:
        assert revision(db) == 1
    replay = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert replay.returncode == 0 and json.loads(replay.stdout) == receipt


def test_legacy_source_format_does_not_depend_on_user_version(prepared):
    with connection(prepared["source"].mail, write=True) as db:
        db.execute("PRAGMA user_version=2")
    result = prepare(
        prepared["source"],
        prepared["root"],
        "legacy-version-two",
        prepared["choices"],
        request_id=str(uuid.uuid4()),
        fence=fence,
    )
    assert result["phase"] == "complete"


def test_normal_write_and_replay_do_not_scan_history(prepared, monkeypatch):
    import agentstack_mail.global_s2a as implementation

    runtime = S2aRuntime(prepared["config_path"])

    def refuse_scan(*args, **kwargs):
        raise AssertionError("full receipt scan on the normal path")

    monkeypatch.setattr(implementation, "validate_receipts", refuse_scan)
    monkeypatch.setattr(implementation, "integrity", refuse_scan)
    args = owner(prepared, request_id=str(uuid.uuid4()), policy="auto")
    result = runtime.apply("set_contact_policy", args)
    assert runtime.apply("set_contact_policy", args) == result
    with runtime.transaction() as db:
        window = db.execute(
            "SELECT id,window_uuid FROM window_identities WHERE agent_id=2"
        ).fetchone()
    runtime.apply(
        "touch_window_identity",
        owner(prepared, window_row_id=window["id"], window_uuid=window["window_uuid"]),
    )


def test_full_read_integrity_never_duplicates_database_in_memory(prepared, monkeypatch):
    import agentstack_mail.global_s2a as implementation

    original = implementation.sqlite3.connect

    def connect(path, *args, **kwargs):
        assert str(path) != ":memory:", "whole database must not be copied into RAM"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(implementation.sqlite3, "connect", connect)
    S2aRuntime(prepared["config_path"])


def test_full_inspect_and_matched_replay_reject_corrupt_receipt(prepared):
    runtime = S2aRuntime(prepared["config_path"])
    args = owner(prepared, request_id=str(uuid.uuid4()), policy="auto")
    runtime.apply("set_contact_policy", args)
    with connection(Path(prepared["config"]["database"]), write=True) as db:
        db.execute("UPDATE global_operation_receipts SET receipt_sha256='broken'")
    with pytest.raises(GlobalError, match="REQUEST_RECEIPT_INVALID"):
        runtime.apply("set_contact_policy", args)
    with runtime.transaction() as db:
        with pytest.raises(GlobalError, match="REQUEST_RECEIPT_INVALID"):
            runtime.inspect(db, 2)
    with pytest.raises(GlobalError, match="REQUEST_RECEIPT_INVALID"):
        S2aRuntime(prepared["config_path"])


@pytest.mark.parametrize("unsafe", ["lock-replace", "database-mode"])
def test_incident_uses_common_exclusive_fence_before_authority_write(prepared, unsafe):
    from agentstack_mail.global_incident import inspect_incident, quarantine

    with connection(Path(prepared["config"]["database"]), write=True) as db:
        db.execute(
            "UPDATE namespace_metadata SET value='2' WHERE key='write_generation'"
        )
    inspected = inspect_incident(prepared["config_path"])
    authority = Path(prepared["config"]["authority"])
    before = authority.read_bytes()

    def fault(point):
        if point == "after_incident":
            if unsafe == "lock-replace":
                path = Path(prepared["config"]["authority_lock"])
                replacement = path.with_suffix(".replacement")
                replacement.touch(mode=0o600)
                replacement.replace(path)
            else:
                Path(prepared["config"]["database"]).chmod(0o644)

    with pytest.raises(GlobalError, match="AUTHORITY_LOCK_REPLACED|DATABASE_UNSAFE"):
        quarantine(
            prepared["config_path"],
            request_id=str(uuid.uuid4()),
            incident_digest=inspected["incident_digest"],
            tracked_revision=inspected["manifest"]["tracked_revision"],
            current_revision=inspected["manifest"]["current_revision"],
            confirm=True,
            fault=fault,
        )
    assert authority.read_bytes() == before


@pytest.mark.parametrize("entry", ["startup", "inspect", "incident", "prepare"])
@pytest.mark.parametrize("fail_fts", [False, True])
def test_fts_snapshot_stays_inside_candidate_and_cleans_up(
    prepared, monkeypatch, tmp_path, entry, fail_fts
):
    import agentstack_mail.global_s2a as implementation
    from agentstack_mail.global_incident import inspect_incident
    import stat

    runtime = S2aRuntime(prepared["config_path"])
    if entry == "incident":
        with connection(Path(prepared["config"]["database"]), write=True) as db:
            db.execute(
                "UPDATE namespace_metadata SET value='2' WHERE key='write_generation'"
            )
    outside = tmp_path / "system-tmp"
    outside.mkdir(mode=0o700)
    # Preserve the admitted temporary root, including on Linux where there is
    # no /private/tmp fallback. Catch any default-directory snapshot in a
    # separate monitored sink instead of changing tempfile.gettempdir().
    temporary_root = implementation.tempfile.gettempdir()
    original_directory = implementation.tempfile.TemporaryDirectory

    def monitored_directory(*args, **kwargs):
        if kwargs.get("dir") is None:
            kwargs["dir"] = outside
        return original_directory(*args, **kwargs)

    monkeypatch.setattr(
        implementation.tempfile, "TemporaryDirectory", monitored_directory
    )
    original = implementation.sqlite3.connect
    checked = []

    # Native subclass preserves SQLite backup()'s target type and observes
    # the actual FTS command after the copied private file exists.
    class NativeChecker(implementation.sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "integrity-check" in sql:
                path = Path(super().execute("PRAGMA database_list").fetchone()[2])
                assert prepared["root"] in path.parents
                assert path.parent.parent.name == ".fts-validation"
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
                assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
                assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
                assert not list(outside.iterdir())
                checked.append(path)
                if fail_fts:
                    raise implementation.sqlite3.DatabaseError("synthetic FTS failure")
            return super().execute(sql, *args, **kwargs)

    def connect(path, *args, **kwargs):
        if Path(str(path)).name == "check.sqlite3":
            kwargs["factory"] = NativeChecker
        return original(path, *args, **kwargs)

    monkeypatch.setattr(implementation.sqlite3, "connect", connect)

    def run():
        if entry == "startup":
            return S2aRuntime(prepared["config_path"])
        if entry == "inspect":
            with runtime.transaction() as db:
                return runtime.inspect(db, 2)
        if entry == "incident":
            return inspect_incident(prepared["config_path"])
        return prepare(
            prepared["source"],
            prepared["root"],
            "fts-second",
            prepared["choices"],
            request_id=str(uuid.uuid4()),
            fence=fence,
        )

    if fail_fts:
        with pytest.raises(GlobalError, match="RUNTIME_CANDIDATE_INVALID"):
            run()
    else:
        run()
    assert checked
    assert implementation.tempfile.gettempdir() == temporary_root
    assert not list(outside.iterdir())
    assert all(not path.exists() and not path.parent.exists() for path in checked)
    assert all(
        not list(path.iterdir()) for path in prepared["root"].rglob(".fts-validation")
    )


@pytest.mark.parametrize("unsafe", ["symlink", "public-mode"])
def test_fts_snapshot_refuses_unsafe_fixed_directory(prepared, tmp_path, unsafe):
    directory = Path(prepared["config"]["database"]).parent / ".fts-validation"
    directory.rmdir()  # preparation leaves only the empty fixed private directory
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if unsafe == "symlink":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir(mode=0o755)
    with pytest.raises(GlobalError, match="ISOLATION_ROOT_UNSAFE"):
        S2aRuntime(prepared["config_path"])
    assert not list(outside.iterdir())
    if unsafe == "public-mode":
        assert not list(directory.iterdir())


def test_fts_recovers_only_own_stale_snapshots_without_following_symlinks(
    prepared, tmp_path
):
    directory = Path(prepared["config"]["database"]).parent / ".fts-validation"
    stale = directory / "snapshot-dead0001"
    stale.mkdir(mode=0o700)
    (stale / "check.sqlite3").write_bytes(b"private stale fixture token and body")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged outside data")
    (stale / "external-link").symlink_to(outside, target_is_directory=True)
    alias = directory / "snapshot-link0001"
    alias.symlink_to(outside, target_is_directory=True)
    unrelated = directory / "keep-note"
    unrelated.write_bytes(b"unrelated private data")
    foreign = directory / "snapshot-mode0001"
    foreign.mkdir(mode=0o755)
    (foreign / "keep").write_bytes(b"not an own private snapshot")
    S2aRuntime(prepared["config_path"])
    assert not stale.exists()
    assert sentinel.read_bytes() == b"unchanged outside data"
    assert alias.is_symlink()
    assert unrelated.read_bytes() == b"unrelated private data"
    assert (foreign / "keep").read_bytes() == b"not an own private snapshot"
    assert {path.name for path in directory.iterdir()} == {
        alias.name,
        unrelated.name,
        foreign.name,
    }


def test_fts_snapshot_recovery_waits_for_same_private_directory_lock(
    prepared, monkeypatch
):
    import fcntl
    import stat
    import threading
    import agentstack_mail.global_s2a as implementation

    directory = Path(prepared["config"]["database"]).parent / ".fts-validation"
    stale = directory / "snapshot-dead0001"
    stale.mkdir(mode=0o700)
    (stale / "check.sqlite3").write_bytes(b"stale private snapshot")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    original = fcntl.flock
    original(fd, fcntl.LOCK_EX)
    entered = threading.Event()
    results, errors = [], []

    def flock(candidate_fd, operation):
        if stat.S_ISDIR(os.fstat(candidate_fd).st_mode) and operation == fcntl.LOCK_EX:
            entered.set()
        return original(candidate_fd, operation)

    monkeypatch.setattr(implementation.fcntl, "flock", flock)

    def inspect():
        try:
            results.append(S2aRuntime(prepared["config_path"]))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=inspect)
    thread.start()
    try:
        assert entered.wait(10), errors
        assert thread.is_alive()
        assert stale.exists()  # a parallel reader cannot recover while locked
        assert list(directory.iterdir()) == [stale]
    finally:
        original(fd, fcntl.LOCK_UN)
        os.close(fd)
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    assert len(results) == 1
    assert not list(directory.iterdir())


def test_source_admission_never_opens_raw_database_descriptor(prepared, monkeypatch):
    from agentstack_mail.global_prepare import require_legacy_source

    source = prepared["source"].mail
    original = os.open

    def guarded(path, *args, **kwargs):
        assert Path(path) != source, "source admission closes SQLite's POSIX locks"
        return original(path, *args, **kwargs)

    with connection(source) as held:
        held.execute("SELECT id FROM agents LIMIT 1").fetchall()
        monkeypatch.setattr(os, "open", guarded)
        require_legacy_source(source)
