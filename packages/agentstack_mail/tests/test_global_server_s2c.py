"""Schema5 regression fixtures: isolated HOME and synthetic Mail only."""

import asyncio
import os
import uuid

import pytest
from agentstack_mail.global_prepare_s2c import prepare
from agentstack_mail.global_reservations import ACTIVE, lease_timestamp
from agentstack_mail.global_s2a import revision
from agentstack_mail.global_s2c import S2cRuntime
from agentstack_mail.global_s2c_contract import FIXTURE
from agentstack_mail.global_server import GlobalError
from agentstack_mail.namespace_state_io import connection
from agentstack_mail.namespace_store import changed
from agentstack_mail.schema_contract import definitive_rejection
from test_global_server_s2b import (
    fence,
    file_state,
    http,
    owner,
    prepared_for,
    server_for,
)


@pytest.fixture
def prepared(monkeypatch):
    yield from prepared_for(monkeypatch, prepare, "s2c")


def call(state, tool, aid=2, **args):
    return S2cRuntime(state["config_path"]).apply(
        tool, owner(state, aid, request_id=str(uuid.uuid4()), **args)
    )


def test_fresh_schema5_and_atomic_conflict(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    path = str(prepared["root"] / "new file.py")
    args = owner(prepared, request_id=str(uuid.uuid4()), paths=[path])
    first = runtime.apply("file_reservation_paths", args)
    assert first["granted"][0]["path_pattern"] == path
    assert runtime.apply("file_reservation_paths", args) == first
    with pytest.raises(GlobalError, match="RESERVATION_CONFLICT"):
        call(prepared, "file_reservation_paths", aid=1, paths=[path])
    released = call(
        prepared,
        "release_file_reservations",
        file_reservation_ids=[first["granted"][0]["id"]],
    )
    assert released["released"][0]["release_cause"] == "owner"


@pytest.fixture
def server(prepared):
    yield from server_for(prepared, "s2c")


def test_real_http_catalog_lifecycle(server):
    catalog = asyncio.run(http(server, "tools"))
    names = {tool.name for tool in catalog}
    assert len(names) == 31 and "macro_prepare_thread" not in names
    args = owner(
        server, request_id=str(uuid.uuid4()), paths=[str(server["root"] / "http.py")]
    )
    result = asyncio.run(http(server, "file_reservation_paths", args))
    key = result["granted"][0]["id"]
    renewal = asyncio.run(
        http(
            server,
            "renew_file_reservations",
            owner(server, request_id=str(uuid.uuid4()), file_reservation_ids=[key]),
        )
    )
    assert renewal["renewed"][0]["id"] == key
    release = asyncio.run(
        http(
            server,
            "release_file_reservations",
            owner(server, request_id=str(uuid.uuid4()), file_reservation_ids=[key]),
        )
    )
    assert release["released"][0]["release_cause"] == "owner"


def test_macro_start_session_is_atomic_composition(prepared):
    out = call(
        prepared,
        "macro_start_session",
        file_reservation_paths=[str(prepared["root"] / "session.py")],
    )
    assert out["agent"]["agent_id"] == 2
    assert out["file_reservations"] and out["inbox"]


def test_contact_welcome_has_no_deferred_queue(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    with runtime.transaction(write=True) as db:
        db.execute("DELETE FROM agent_links WHERE a_agent_id=1 AND b_agent_id=2")
        from agentstack_mail.namespace_store import changed

        changed(db)
    request = owner(
        prepared,
        1,
        request_id=str(uuid.uuid4()),
        to_agent_id=2,
        welcome_subject="hello",
        welcome_body="welcome",
    )
    pending = runtime.apply("macro_contact_handshake", request)
    assert pending["welcome"] is None
    runtime.apply(
        "respond_contact",
        owner(prepared, 2, request_id=str(uuid.uuid4()), from_agent_id=1, accept=True),
    )
    assert runtime.apply("macro_contact_handshake", request) == pending
    request["request_id"] = str(uuid.uuid4())
    delivered = runtime.apply("macro_contact_handshake", request)
    assert delivered["stage"] == "welcome-sent"
    assert runtime.apply("macro_contact_handshake", request) == delivered


def snapshot(runtime):
    with runtime.transaction() as db:
        return (
            revision(db),
            [
                tuple(r)
                for r in db.execute("SELECT * FROM file_reservations ORDER BY id")
            ],
            [
                tuple(r)
                for r in db.execute(
                    "SELECT * FROM global_operation_receipts ORDER BY request_id"
                )
            ],
        )


@pytest.mark.parametrize(
    "point",
    ["before_state", "after_state", "after_receipt", "before_commit", "after_commit"],
)
@pytest.mark.parametrize(
    "tool",
    [
        "file_reservation_paths",
        "macro_file_reservation_cycle",
        "macro_start_session",
        "macro_contact_handshake",
    ],
)
def test_mutation_interruption_replays_once(prepared, point, tool):
    runtime = S2cRuntime(prepared["config_path"])
    extra = {"paths": [str(prepared["root"] / "interrupted.py")]}
    if tool == "macro_start_session":
        extra = {"file_reservation_paths": extra["paths"]}
    elif tool == "macro_contact_handshake":
        extra = {"to_agent_id": 2, "welcome_subject": "welcome", "welcome_body": "body"}
    args = owner(prepared, 1, request_id=str(uuid.uuid4()), **extra)

    def fail(p):
        if p == point:
            raise RuntimeError("interruption")

    runtime.fault = fail
    with pytest.raises(RuntimeError, match="interruption"):
        runtime.apply(tool, args)
    fresh = S2cRuntime(prepared["config_path"])
    result = fresh.apply(tool, args)
    assert fresh.apply(tool, args) == result
    with fresh.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM global_operation_receipts").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "point",
    [
        "after_plan",
        "before_extension",
        "after_ddl_0",
        "before_extension_commit",
        "after_extension_commit",
        "s2b_before_extension",
        *[f"s2b_after_ddl_{i}" for i in range(6)],
        "s2b_before_extension_commit",
        "s2b_after_extension_commit",
        "s2b_after_staged_control",
        "s2b_after_provenance",
        "pr3_final_verified_after",
        "before_ready",
        "before_publish",
        "before_complete",
        "after_complete",
        "s2c_before_extension",
        "s2c_after_ddl",
        "s2c_before_extension_commit",
        "s2c_after_extension_commit",
        "s2c_after_staged_control",
        "after_ready",
        "after_publish",
    ],
)
def test_fresh_publication_interruption_is_recoverable(prepared, point):
    source_before = file_state(prepared["source"].mail.parent)

    def fail(p):
        if p == point:
            raise RuntimeError("interruption")

    request = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="interruption"):
        prepare(
            prepared["source"],
            prepared["root"],
            "retry",
            prepared["choices"],
            request_id=request,
            fence=fence,
            fault=fail,
        )
    result = prepare(
        prepared["source"],
        prepared["root"],
        "retry",
        prepared["choices"],
        request_id=request,
        fence=fence,
    )
    assert result["phase"] == "complete"
    assert file_state(prepared["source"].mail.parent) == source_before
    S2cRuntime(prepared["root"] / "s2c-candidates/retry/candidate/server-config.json")


@pytest.mark.parametrize("case", FIXTURE["rejection_expectations"])
def test_pending_phase_expectation(case):
    # The phase compiler and the existing classifier are the only authority.
    if case["operation"] != "write":
        assert case["action"] in (
            "unchanged",
            "no_pending_mutation",
            "no_agent_pending",
        )
        return
    expected = case["action"].startswith("clear")
    assert (
        definitive_rejection(
            case["reason"], existing_pending=case["existing_pending"], contract=FIXTURE
        )
        is expected
    )


def test_expired_history_is_not_selected_or_rewritten(prepared, monkeypatch):
    runtime = S2cRuntime(prepared["config_path"])
    path = str(prepared["root"] / "active.py")
    first = runtime.apply(
        "file_reservation_paths",
        owner(prepared, request_id=str(uuid.uuid4()), paths=[path]),
    )
    with runtime.transaction(write=True) as db:
        columns = "id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,released_ts,path_unknown,revision"
        db.executemany(
            "INSERT INTO file_reservations("
            + columns
            + ") VALUES(?,2,'tool://expired',1,'history',?,?,NULL,1,0)",
            (
                (
                    1000 + i,
                    lease_timestamp("2000-01-01Z"),
                    lease_timestamp("2000-01-02Z"),
                )
                for i in range(10000)
            ),
        )
        changed(db)
    before = snapshot(runtime)
    result = runtime.apply("list_file_reservations", owner(prepared, owner_agent_id=2))
    assert [r["id"] for r in result["items"]] == [first["granted"][0]["id"]]
    assert snapshot(runtime) == before
    result = call(prepared, "renew_file_reservations", extend_seconds=60)
    assert len(result["renewed"]) == 1
    with runtime.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM file_reservations WHERE id>=1000 AND released_ts IS NULL AND revision=0"
            ).fetchone()[0]
            == 10000
        )
        assert any(
            "SEARCH" in r[3]
            for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM file_reservations WHERE " + ACTIVE,
                {"now_utc": lease_timestamp("2026-11-01Z")},
            )
        )


def test_renew_expiry_and_overflow_replay(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    result = call(
        prepared, "file_reservation_paths", paths=[str(prepared["root"] / "future.py")]
    )
    key = result["granted"][0]["id"]
    with runtime.transaction(write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts=? WHERE id=?",
            (lease_timestamp("9999-12-31T23:59:59Z"), key),
        )
        changed(db)
    before = snapshot(runtime)
    with pytest.raises(GlobalError, match="TIMESTAMP_INVALID"):
        call(
            prepared,
            "renew_file_reservations",
            file_reservation_ids=[key],
            extend_seconds=60,
        )
    assert snapshot(runtime) == before
    # Historical acquisition skips the current filesystem and expiry checks.
    assert (
        runtime.apply(
            "file_reservation_paths",
            owner(
                prepared,
                request_id=result["request_id"],
                paths=[str(prepared["root"] / "future.py")],
            ),
        )
        == result
    )


@pytest.mark.parametrize(
    "paths,ids,reason",
    [
        ([], None, "LEASE_SELECTOR_EMPTY"),
        (None, [], "LEASE_SELECTOR_EMPTY"),
        (["tool://x"], [1], "LEASE_SELECTOR_CONFLICT"),
    ],
)
def test_selector_static_rejection(prepared, paths, ids, reason):
    runtime = S2cRuntime(prepared["config_path"])
    before = snapshot(runtime)
    with pytest.raises(GlobalError, match=reason):
        call(
            prepared, "release_file_reservations", paths=paths, file_reservation_ids=ids
        )
    assert snapshot(runtime) == before


def test_force_release_inspect_and_stale_recovery(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    lease = call(
        prepared, "file_reservation_paths", paths=[str(prepared["root"] / "force.py")]
    )["granted"][0]
    binding = {
        k: owner(prepared)[k]
        for k in (
            "expected_server_instance_id",
            "candidate_generation",
            "authority_epoch",
        )
    }
    inspect = {
        "version": 1,
        "action": "inspect",
        **binding,
        "agent_id": 2,
        "reservation_id": lease["id"],
    }
    observed = runtime.management(inspect, os.getuid())
    assert observed["reservation"] == lease
    force = {
        "version": 1,
        "operation": "force_release_reservation",
        **binding,
        "reservation_id": lease["id"],
        "expected_owner_agent_id": 2,
        "expected_lease_revision": lease["revision"],
        "confirm": True,
        "note": "operator",
    }
    result = runtime.management(force, os.getuid())
    assert result["changed"] and result["lease"]["release_cause"] == "operator"
    with pytest.raises(GlobalError, match="STALE_LEASE_REVISION"):
        runtime.management(force, os.getuid())
    current = runtime.management(inspect, os.getuid())["reservation"]
    force["expected_lease_revision"] = current["revision"]
    assert runtime.management(force, os.getuid())["changed"] is False


def test_cycle_releases_only_new_ids_and_rolls_back_conflicts(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    original = call(prepared, "file_reservation_paths", paths=["tool://reused"])[
        "granted"
    ][0]
    out = call(
        prepared,
        "macro_file_reservation_cycle",
        paths=["tool://reused", "tool://new"],
        auto_release=True,
    )
    assert out["released_ids"] == [out["granted"][1]["id"]]
    assert (
        out["granted"][0]["id"] == original["id"]
        and out["granted"][0]["released_ts"] is None
    )
    assert out["granted"][1]["release_cause"] == "owner"
    before = snapshot(runtime)
    with pytest.raises(GlobalError, match="RESERVATION_CONFLICT"):
        call(
            prepared,
            "macro_start_session",
            aid=1,
            file_reservation_paths=["tool://untouched", "tool://reused"],
        )
    assert snapshot(runtime) == before


@pytest.mark.parametrize("active_count", [0, 2, 101])
def test_implicit_selection_ignores_expired_rows(prepared, active_count):
    runtime = S2cRuntime(prepared["config_path"])
    with runtime.transaction(write=True) as db:
        db.executemany(
            "INSERT INTO file_reservations(id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,path_unknown,revision) VALUES(?,2,'tool://fixture',1,'fixture',?,?,0,0)",
            (
                (
                    1000 + i,
                    lease_timestamp("2000-01-01T00:00:00Z"),
                    lease_timestamp(
                        "9999-01-01T00:00:00Z"
                        if i < active_count
                        else "2000-01-02T00:00:00Z"
                    ),
                )
                for i in range(150)
            ),
        )
        changed(db)
    before = snapshot(runtime)
    if active_count > 100:
        with pytest.raises(GlobalError, match="LEASE_SELECTION_TOO_LARGE"):
            call(prepared, "release_file_reservations")
        assert snapshot(runtime) == before
    else:
        out = call(prepared, "release_file_reservations")
        assert len(out["released"]) == active_count
    with runtime.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM file_reservations WHERE id>=? AND released_ts IS NULL AND revision=0",
                (1000 + active_count,),
            ).fetchone()[0]
            == 150 - active_count
        )


def test_keyset_and_coverage_are_pure_reads(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    call(
        prepared,
        "file_reservation_paths",
        paths=["tool://one", "tool://two", "tool://three"],
    )
    args = owner(prepared, owner_agent_id=2, limit=1)
    first = runtime.apply("list_file_reservations", args)
    call(prepared, "file_reservation_paths", paths=["tool://later"])
    second = runtime.apply(
        "list_file_reservations", args | {"cursor": first["next_cursor"]}
    )
    assert second["max_reservation_id"] == first["max_reservation_id"]
    assert second["items"][0]["id"] > first["items"][0]["id"]
    with pytest.raises(GlobalError, match="CURSOR_INVALID"):
        runtime.apply(
            "list_file_reservations",
            args | {"owner_agent_id": 1, "cursor": first["next_cursor"]},
        )
    before = snapshot(runtime)
    checked = runtime.apply(
        "check_file_reservations",
        owner(prepared, paths=["tool://one", "tool://missing"]),
    )
    assert checked["covered"] is False and checked["items"][0]["covered"] is True
    assert snapshot(runtime) == before


def test_retirement_changes_only_active_rows_and_unretire_preserves_receipt(
    prepared, monkeypatch
):
    runtime = S2cRuntime(prepared["config_path"])
    old = call(
        prepared, "file_reservation_paths", paths=["tool://expired"], ttl_seconds=60
    )
    fresh = call(
        prepared, "file_reservation_paths", paths=["tool://active"], ttl_seconds=3600
    )
    from datetime import timedelta

    from agentstack_mail.global_s2a import utc

    boundary = (utc(old["committed_at"]) + timedelta(seconds=61)).isoformat()
    monkeypatch.setattr("agentstack_mail.global_s2c.now", lambda: boundary)
    runtime.lifecycle(owner(prepared), True)
    with runtime.transaction() as db:
        rows = {r["id"]: r for r in db.execute("SELECT * FROM file_reservations")}
        assert rows[old["granted"][0]["id"]]["released_ts"] is None
        assert rows[fresh["granted"][0]["id"]]["release_cause"] == "retired"
    args = owner(
        prepared,
        request_id=fresh["request_id"],
        paths=["tool://active"],
        ttl_seconds=3600,
    )
    with pytest.raises(GlobalError, match="OWNER_REQUIRED"):
        runtime.apply("file_reservation_paths", args)
    runtime.lifecycle(owner(prepared), False)
    assert runtime.apply("file_reservation_paths", args) == fresh


@pytest.mark.parametrize(
    "policy,status", [("block_all", "pending"), ("open", "blocked")]
)
def test_welcome_block_priority_rolls_back_all(prepared, policy, status):
    runtime = S2cRuntime(prepared["config_path"])
    with runtime.transaction(write=True) as db:
        db.execute("UPDATE agents SET contact_policy=? WHERE id=2", (policy,))
        db.execute(
            "UPDATE agent_links SET status=? WHERE a_agent_id=1 AND b_agent_id=2",
            (status,),
        )
        changed(db)
    before = snapshot(runtime)
    with pytest.raises(GlobalError, match="CONTACT_BLOCKED"):
        call(
            prepared,
            "macro_contact_handshake",
            aid=1,
            to_agent_id=2,
            welcome_subject="hello",
            welcome_body="body",
        )
    assert snapshot(runtime) == before


def test_receipt_required_before_sqlite_open(prepared, monkeypatch):
    path = prepared["config_path"].parent.parent / "preparation-receipt.json"
    path.unlink()
    before = file_state(prepared["root"])

    def forbidden(*args, **kwargs):
        raise AssertionError("SQLite opened before receipt admission")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    with pytest.raises(GlobalError, match="CANDIDATE_SCHEMA_REQUIRES_REPREPARE"):
        S2cRuntime(prepared["config_path"])
    assert file_state(prepared["root"]) == before


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00+09:00",
        "2026-01-01T00:00:00.1+00:00",
        "2026-01-01T25:00:00.000000+00:00",
        "2026-01-01T00:00:00.x00000+00:00",
    ],
)
def test_noncanonical_timestamp_sql_check(prepared, value):
    import sqlite3

    runtime = S2cRuntime(prepared["config_path"])
    before = snapshot(runtime)
    with pytest.raises(sqlite3.IntegrityError):
        with connection(runtime.paths["database"], write=True) as db:
            db.execute("UPDATE file_reservations SET expires_ts=? WHERE id=1", (value,))
    assert snapshot(runtime) == before


def test_direct_server_unknown_rules_refuse_without_state(prepared, monkeypatch):
    from agentstack_mail import reservation_paths

    runtime = S2cRuntime(prepared["config_path"])
    before = snapshot(runtime)
    monkeypatch.setattr(reservation_paths, "unicode_rule_at", lambda path: None)
    monkeypatch.setattr(reservation_paths, "case_insensitive_at", lambda path: False)
    with pytest.raises(GlobalError, match="ACTIVE_LEASE_RULES_UNKNOWN"):
        call(prepared, "file_reservation_paths", paths=[str(prepared["root"] / "é.py")])
    assert snapshot(runtime) == before


def test_legacy_long_reason_and_long_expiry_survive_renewal(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    with runtime.transaction(write=True) as db:
        db.execute(
            "UPDATE file_reservations SET reason=?,expires_ts=? WHERE id=1",
            ("r" * 1000, lease_timestamp("2030-01-01T00:00:00Z")),
        )
        changed(db)
    out = call(
        prepared,
        "renew_file_reservations",
        aid=1,
        file_reservation_ids=[1],
        extend_seconds=60,
    )
    assert out["renewed"][0]["reason"] == "r" * 1000
    assert out["renewed"][0]["expires_ts"] == lease_timestamp("2030-01-01T00:01:00Z")
    assert (
        call(prepared, "release_file_reservations", aid=1, file_reservation_ids=[1])[
            "released"
        ][0]["reason"]
        == "r" * 1000
    )


def test_actual_management_socket_force_and_purge(server):
    from test_global_server_s2b import management

    args = owner(server, request_id=str(uuid.uuid4()), paths=["tool://socket-force"])
    lease = asyncio.run(http(server, "file_reservation_paths", args))["granted"][0]
    released = management(
        server,
        "force_release_reservation",
        reservation_id=lease["id"],
        expected_owner_agent_id=2,
        expected_lease_revision=lease["revision"],
        confirm=True,
        note="test",
    )
    assert released["ok"] and released["lease"]["release_cause"] == "operator"
    after = 0
    while True:
        result = management(
            server,
            "purge_messages",
            cutoff_at="2026-10-08T10:01:00Z",
            after_id=after,
            limit=1,
            dry_run=True,
        )
        assert result["ok"]
        if not result["more"]:
            break
        assert result["next_after_id"] > after
        after = result["next_after_id"]


def test_schema5_incident_inspection_preserves_exit(prepared):
    from agentstack_mail.global_incident import inspect_incident, quarantine

    runtime = S2cRuntime(prepared["config_path"])
    with connection(runtime.paths["database"], write=True) as db:
        changed(db)
    with pytest.raises(GlobalError, match="RUNTIME_WRITER_MISMATCH"):
        S2cRuntime(prepared["config_path"])
    observed = inspect_incident(prepared["config_path"])
    assert not observed["runtime_validated"]
    result = quarantine(
        prepared["config_path"],
        request_id=str(uuid.uuid4()),
        incident_digest=observed["incident_digest"],
        tracked_revision=observed["manifest"]["tracked_revision"],
        current_revision=observed["manifest"]["current_revision"],
        confirm=True,
    )
    assert result["authority"]["root_status"] == "retired"


@pytest.mark.parametrize("operation", ["inspect", "purge_messages"])
def test_management_large_sql_id_is_structured_rejection(prepared, operation):
    runtime = S2cRuntime(prepared["config_path"])
    request = {
        k: owner(prepared)[k]
        for k in (
            "expected_server_instance_id",
            "candidate_generation",
            "authority_epoch",
        )
    }
    request["version"] = 1
    if operation == "inspect":
        request.update(action="inspect", agent_id=2, reservation_id=2**63)
    else:
        request.update(
            operation=operation,
            cutoff_at="2026-10-08T10:01:00Z",
            after_id=2**63,
            limit=1,
            dry_run=False,
        )
    before = snapshot(runtime)
    with pytest.raises(GlobalError, match="ARGUMENTS_UNSUPPORTED"):
        runtime.management(request, os.getuid())
    assert snapshot(runtime) == before


def test_imported_large_output_rolls_back_macro_and_bounds_read(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    with runtime.transaction(write=True) as db:
        # This imported row has no historical operation receipt. Keep it ACTIVE
        # so the macro reuses and projects its imported long reason.
        db.execute("UPDATE messages SET body_md=? WHERE id=107", ("x" * 1100000,))
        db.execute(
            "UPDATE file_reservations SET reason=?,expires_ts=? WHERE id=1",
            ("r" * 1100000, lease_timestamp("9999-01-01T00:00:00Z")),
        )
        path = db.execute(
            "SELECT path_pattern FROM file_reservations WHERE id=1"
        ).fetchone()[0]
        changed(db)
    before = snapshot(runtime)
    with pytest.raises(GlobalError, match="MESSAGE_RESPONSE_TOO_LARGE"):
        call(
            prepared,
            "macro_start_session",
            aid=1,
            file_reservation_paths=[path],
        )
    assert snapshot(runtime) == before
    with pytest.raises(GlobalError, match="MESSAGE_RESPONSE_TOO_LARGE"):
        runtime.apply("list_file_reservations", owner(prepared, active_only=False))
    assert snapshot(runtime) == before


def test_fresh_retired_owner_preserves_expired_null_history(prepared):
    with connection(prepared["source"].mail, write=True) as db:
        for key, expiry in [
            (10, "2026-10-08T18:59:59+09:00"),
            (11, "2026-10-08T19:00:00+09:00"),
            (12, "2026-10-08T19:00:01+09:00"),
        ]:
            db.execute(
                "INSERT INTO file_reservations(id,project_id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,released_ts) VALUES(?,1,4,?,1,'old','2026-10-07T10:00:00Z',?,NULL)",
                (key, str(prepared["root"] / f"retired-{key}"), expiry),
            )
    source_before = file_state(prepared["source"].mail.parent)
    prepare(
        prepared["source"],
        prepared["root"],
        "retired-history",
        prepared["choices"],
        request_id=str(uuid.uuid4()),
        fence=fence,
    )
    path = prepared["root"] / "s2c-candidates/retired-history/candidate/mail.sqlite3"
    with connection(path) as db:
        rows = {
            row["id"]: row
            for row in db.execute("SELECT * FROM file_reservations WHERE id>=10")
        }
        for key in (10, 11):
            assert (
                rows[key]["released_ts"] is None and rows[key]["release_cause"] is None
            )
        assert rows[12]["release_cause"] == "retired"
        retired = db.execute("SELECT retired_at FROM agents WHERE id=4").fetchone()[0]
        assert rows[12]["released_ts"] == lease_timestamp(retired)
    assert file_state(prepared["source"].mail.parent) == source_before


@pytest.mark.parametrize(
    "tool", ["renew_file_reservations", "release_file_reservations"]
)
@pytest.mark.parametrize(
    "history",
    ["shared", "foreign-expired", "foreign-released", "own-released", "101-history"],
)
def test_path_selector_uses_only_self_active(prepared, tool, history):
    runtime = S2cRuntime(prepared["config_path"])
    path = "tool://selector-history"
    old_owner = 2 if history in {"own-released", "101-history"} else 1
    old = call(
        prepared, "file_reservation_paths", aid=old_owner, paths=[path], exclusive=False
    )["granted"][0]["id"]
    if history != "shared":
        if history == "foreign-expired":
            with runtime.transaction(write=True) as db:
                db.execute(
                    "UPDATE file_reservations SET expires_ts=created_ts WHERE id=?",
                    (old,),
                )
                changed(db)
        else:
            call(
                prepared,
                "release_file_reservations",
                aid=old_owner,
                file_reservation_ids=[old],
            )
    if history == "101-history":
        with runtime.transaction(write=True) as db:
            for _ in range(100):
                db.execute(
                    "INSERT INTO file_reservations(agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,released_ts,path_unknown,revision,release_cause,release_note) SELECT agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,released_ts,path_unknown,revision,release_cause,release_note FROM file_reservations WHERE id=?",
                    (old,),
                )
            changed(db)
    current = call(prepared, "file_reservation_paths", paths=[path], exclusive=False)[
        "granted"
    ][0]["id"]
    with runtime.transaction() as db:
        history_before = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM file_reservations WHERE id<>? ORDER BY id", (current,)
            )
        ]
    result = call(prepared, tool, paths=[path])
    rows = result["renewed" if tool == "renew_file_reservations" else "released"]
    assert [row["id"] for row in rows] == [current]
    if tool == "release_file_reservations":
        assert result["already_released_ids"] == []
    with runtime.transaction() as db:
        assert history_before == [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM file_reservations WHERE id<>? ORDER BY id", (current,)
            )
        ]


@pytest.mark.parametrize(
    "tool", ["renew_file_reservations", "release_file_reservations"]
)
def test_path_selector_does_not_select_self_expired(prepared, tool):
    runtime = S2cRuntime(prepared["config_path"])
    path = "tool://selector-expired"
    key = call(prepared, "file_reservation_paths", paths=[path])["granted"][0]["id"]
    with runtime.transaction(write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts=created_ts WHERE id=?", (key,)
        )
        changed(db)
    before = snapshot(runtime)
    if tool == "renew_file_reservations":
        with pytest.raises(GlobalError, match="LEASE_NOT_FOUND"):
            call(prepared, tool, paths=[path])
        assert snapshot(runtime) == before
    else:
        result = call(prepared, tool, paths=[path])
        assert result["released"] == result["already_released_ids"] == []
        assert snapshot(runtime)[1] == before[1]
        assert (
            call(prepared, tool, file_reservation_ids=[key])["released"][0]["id"] == key
        )


def test_macro_inbox_large_bodies_shares_metadata_selection(server):
    from test_global_server_s2b import send

    prepared = server

    runtime = S2cRuntime(prepared["config_path"])
    for _ in range(9):
        runtime.apply("send_message", {**send(prepared), "body_md": "x" * 60000})
    before = asyncio.run(
        http(server, "fetch_inbox", owner(prepared, limit=9, include_bodies=False))
    )
    with runtime.transaction() as db:
        recipients = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM message_recipients ORDER BY message_id,agent_id"
            )
        ]
    result = call(prepared, "macro_start_session", inbox_limit=9)
    assert result["inbox"] == before
    assert len(result["inbox"]) == 9
    assert all(row["body_md"] is None for row in result["inbox"])
    with runtime.transaction() as db:
        assert recipients == [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM message_recipients ORDER BY message_id,agent_id"
            )
        ]
        receipt = db.execute(
            "SELECT receipt_json FROM global_operation_receipts WHERE tool='macro_start_session'"
        ).fetchone()[0]
        assert len(receipt.encode()) < 20000


@pytest.mark.parametrize("unknown_first", [True, False])
@pytest.mark.parametrize("known_covers", [True, False])
def test_coverage_known_witness_overrides_unknown(
    prepared, unknown_first, known_covers
):
    runtime = S2cRuntime(prepared["config_path"])
    path = "tool://coverage-target"
    patterns = [
        "tool://coverage-unknown",
        path if known_covers else "tool://coverage-other",
    ]
    if not unknown_first:
        patterns.reverse()
    rows = call(prepared, "file_reservation_paths", paths=patterns)["granted"]
    unknown_id = next(
        row["id"] for row in rows if row["path_pattern"] == "tool://coverage-unknown"
    )
    with runtime.transaction(write=True) as db:
        db.execute(
            "UPDATE file_reservations SET path_unknown=1 WHERE id=?", (unknown_id,)
        )
        changed(db)
    before = snapshot(runtime)
    if known_covers:
        result = runtime.apply("check_file_reservations", owner(prepared, paths=[path]))
        assert result["covered"]
        assert result["items"][0]["reservation_ids"] == [
            next(row["id"] for row in rows if row["path_pattern"] == path)
        ]
    else:
        with pytest.raises(GlobalError, match="ACTIVE_LEASE_RULES_UNKNOWN"):
            runtime.apply("check_file_reservations", owner(prepared, paths=[path]))
    assert snapshot(runtime) == before
    with runtime.transaction(write=True) as db:
        db.execute(
            "UPDATE file_reservations SET expires_ts=created_ts WHERE id=?",
            (unknown_id,),
        )
        changed(db)
    assert (
        runtime.apply("check_file_reservations", owner(prepared, paths=[path]))[
            "covered"
        ]
        is known_covers
    )


@pytest.mark.parametrize("omit_selector", [False, True])
def test_release_empty_selection_is_new_noop_receipt_not_replay(
    prepared, omit_selector
):
    runtime = S2cRuntime(prepared["config_path"])
    path = "tool://repeated-release"
    key = call(prepared, "file_reservation_paths", paths=[path])["granted"][0]["id"]
    selector = {} if omit_selector else {"paths": [path]}
    first_args = owner(prepared, request_id=str(uuid.uuid4()), **selector)
    first = runtime.apply("release_file_reservations", first_args)
    assert [row["id"] for row in first["released"]] == [key]
    before = snapshot(runtime)
    second_args = {**first_args, "request_id": str(uuid.uuid4())}
    second = runtime.apply("release_file_reservations", second_args)
    assert second["released"] == second["already_released_ids"] == []
    assert second["request_id"] != first["request_id"]
    assert second["mutation_revision"] > first["mutation_revision"]
    after = snapshot(runtime)
    assert after[1] == before[1]
    assert len(after[2]) == len(before[2]) + 1
    assert runtime.apply("release_file_reservations", second_args) == second
    assert runtime.apply("release_file_reservations", first_args) == first
    assert snapshot(runtime) == after


def test_release_paths_ignore_missing_and_foreign_history(prepared):
    runtime = S2cRuntime(prepared["config_path"])
    foreign = "tool://foreign-only-release"
    own = "tool://own-release"
    call(prepared, "file_reservation_paths", aid=1, paths=[foreign])
    key = call(prepared, "file_reservation_paths", paths=[own])["granted"][0]["id"]
    before = snapshot(runtime)
    result = call(
        prepared,
        "release_file_reservations",
        paths=[foreign, own, "tool://never-reserved"],
    )
    assert [row["id"] for row in result["released"]] == [key]
    with runtime.transaction() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM file_reservations WHERE id<>? ORDER BY id", (key,)
            )
        ] == [row for row in before[1] if row[0] != key]
    again = call(
        prepared,
        "release_file_reservations",
        paths=[foreign, own, "tool://never-reserved"],
    )
    assert again["released"] == again["already_released_ids"] == []


@pytest.mark.parametrize("foreign", [False, True])
def test_release_explicit_id_still_rejects_whole_batch(prepared, foreign):
    runtime = S2cRuntime(prepared["config_path"])
    own = call(prepared, "file_reservation_paths", paths=["tool://explicit-own"])[
        "granted"
    ][0]["id"]
    other = (
        call(
            prepared, "file_reservation_paths", aid=1, paths=["tool://explicit-foreign"]
        )["granted"][0]["id"]
        if foreign
        else 2**63 - 1
    )
    before = snapshot(runtime)
    with pytest.raises(
        GlobalError, match="LEASE_OWNER_REQUIRED" if foreign else "LEASE_NOT_FOUND"
    ):
        call(prepared, "release_file_reservations", file_reservation_ids=[own, other])
    assert snapshot(runtime) == before
