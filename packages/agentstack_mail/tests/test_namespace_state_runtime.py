from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest

from agentstack_mail.namespace_migration import NamespaceMigration
from agentstack_mail.namespace_plan import FenceEvidence
from agentstack_mail.namespace_state_io import StateMigrationError, connection, rows
from agentstack_mail.namespace_store import (
    CandidateMailStore,
    PersistentReservations,
    generation,
    reply_catalog,
)
from agentstack_mail.namespace_delivery import CandidateDelivery
from agentstack_mail.namespace_replies import ReplyError, CallerEvidence
from agentstack_mail.namespace_transform import timestamp, iso
from agentstack_mail.reservation_paths import ReservationError
from agentstack_mail.reservation_activity import Activity

spec = importlib.util.spec_from_file_location(
    "runtime_fixture", Path(__file__).parent / "fixtures/namespace_state.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source, choices = fixture.build(tmp_path / "source")
    run = NamespaceMigration(source, tmp_path / "migration", choices)
    run.resume(
        fence=lambda gen, roster: FenceEvidence(
            gen, roster, {key: "stopped" for key in roster}, True, True, True, 0
        )
    )
    root = run.workspace / "candidate"
    with connection(root / "mail.sqlite3") as database:
        current = generation(database)
    return source, choices, run, root, current


def store(root, authorize=lambda owner, targets: True):
    return CandidateMailStore(root / "mail.sqlite3", authorize_delivery=authorize)


def test_window_reconnect_preserves_id_token_and_distinguishes_resolved_uuid(migrated):
    source, choices, run, root, current = migrated
    mail = store(root)
    one = mail.reconnect(
        11, 1, "Alpha", "fixture-owner-one", expected_generation=current
    )
    two = mail.reconnect(
        22, 2, "Beta", "fixture-owner-two", expected_generation=current
    )
    assert (
        one["agent_id"] == 1
        and two["agent_id"] == 2
        and one["window_uuid"] != two["window_uuid"]
    )
    with pytest.raises(StateMigrationError, match="WINDOW_OWNER_MISMATCH"):
        mail.reconnect(22, 1, "Alpha", "fixture-owner-one", expected_generation=current)
    with pytest.raises(StateMigrationError, match="OWNER_REQUIRED"):
        mail.reconnect(11, 1, "Alpha", "wrong-token", expected_generation=current)
    assert run.status()["rollback_allowed"]


def test_reply_permission_is_per_message_and_new_edge_is_direct(migrated):
    source, choices, run, root, current = migrated
    mail = store(root)
    with connection(root / "mail.sqlite3") as database:
        catalog = reply_catalog(database)
        assert catalog.resolve(CallerEvidence(2, True), reply_to=101) == 101
        assert catalog.resolve(CallerEvidence(3, True), reply_to=102) == 102
        with pytest.raises(ReplyError, match="MESSAGE_UNAVAILABLE"):
            catalog.resolve(CallerEvidence(3, True), reply_to=101)
    with pytest.raises(ReplyError, match="MESSAGE_UNAVAILABLE"):
        mail.reply(
            "Beta",
            "fixture-owner-two",
            subject="forbidden",
            body_md="fixture",
            recipients={"to": (1,)},
            expected_generation=current,
            reply_to=102,
        )
    key = mail.reply(
        "Gamma",
        "fixture-owner-three",
        subject="direct reply",
        body_md="runtime search proof",
        recipients={"to": (1,)},
        expected_generation=current,
        reply_to=102,
    )
    with connection(root / "mail.sqlite3") as database:
        row = database.execute(
            "SELECT reply_to,legacy_project_id,legacy_thread_id_raw FROM messages WHERE id=?",
            (key,),
        ).fetchone()
        assert tuple(row) == (102, None, None)
    assert key in mail.search(
        "Alpha", "fixture-owner-one", "runtime", expected_generation=current
    )
    assert key not in mail.search(
        "Beta", "fixture-owner-two", "runtime", expected_generation=current
    )
    assert not run.status()["rollback_allowed"]
    with pytest.raises(StateMigrationError, match="ROLLBACK_AFTER_NEW_WRITES_REFUSED"):
        run.rollback()


def test_named_legacy_thread_and_conflicting_input_remain_rejected(migrated):
    _, _, _, root, current = migrated
    mail = store(root)
    for values, reason in [
        ({"thread_id": "review"}, "LEGACY_THREAD_READ_ONLY"),
        ({"reply_to": 102, "thread_id": "101"}, "REPLY_INPUT_CONFLICT"),
    ]:
        with pytest.raises(ReplyError, match=reason):
            mail.reply(
                "Alpha",
                "fixture-owner-one",
                subject="fixture",
                body_md="fixture",
                recipients={"to": (2,)},
                expected_generation=current,
                **values,
            )
    denied = store(root, lambda owner, targets: False)
    with pytest.raises(StateMigrationError, match="CONTACT_POLICY_REQUIRED"):
        denied.reply(
            "Alpha",
            "fixture-owner-one",
            subject="fixture",
            body_md="fixture",
            recipients={"to": (2,)},
            expected_generation=current,
            reply_to=101,
        )


def test_retention_holds_ancestors_and_removes_complete_expired_sets(migrated):
    _, _, _, root, current = migrated
    mail = store(root)
    dry = mail.purge((101,), expected_generation=current, dry_run=True)
    assert dry["delete_ids"] == () and dry["held_ids"] == (101,)
    assert mail.purge((101,), expected_generation=current)["held_ids"] == (101,)
    child = mail.reply(
        "Alpha",
        "fixture-owner-one",
        subject="deep",
        body_md="fixture",
        recipients={"to": (2,)},
        expected_generation=current,
        reply_to=102,
    )
    assert mail.purge((101, 102), expected_generation=current)["held_ids"] == (101, 102)
    deleted = mail.purge((101, 102, child), expected_generation=current)
    assert set(deleted["delete_ids"]) == {101, 102, child}
    with connection(root / "mail.sqlite3") as database:
        assert not database.execute("PRAGMA foreign_key_check").fetchall()
        assert not database.execute(
            "SELECT 1 FROM messages WHERE id IN (101,102,?)", (child,)
        ).fetchall()
    with pytest.raises(ReplyError, match="MESSAGE_UNAVAILABLE"):
        mail.reply(
            "Alpha",
            "fixture-owner-one",
            subject="late",
            body_md="fixture",
            recipients={"to": (2,)},
            expected_generation=current,
            reply_to=101,
        )


def test_reply_first_serializes_purge_in_the_same_write_transaction(migrated):
    _, _, _, root, current = migrated
    # First remove the existing child, leaving root101 otherwise purgeable.
    store(root).purge((102,), expected_generation=current)
    entered, release = Event(), Event()

    def policy(owner, targets):
        entered.set()
        assert release.wait(3)
        return True

    mail = store(root, policy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reply = pool.submit(
            mail.reply,
            "Alpha",
            "fixture-owner-one",
            subject="race",
            body_md="fixture",
            recipients={"to": (2,)},
            expected_generation=current,
            reply_to=101,
        )
        assert entered.wait(3)
        purge = pool.submit(store(root).purge, (101,), expected_generation=current)
        release.set()
        key = reply.result(timeout=5)
        assert purge.result(timeout=5)["held_ids"] == (101,)
    with connection(root / "mail.sqlite3") as database:
        assert (
            database.execute(
                "SELECT reply_to FROM messages WHERE id=?", (key,)
            ).fetchone()[0]
            == 101
        )
        assert not database.execute("PRAGMA foreign_key_check").fetchall()


def test_read_ack_and_bcc_are_retained_without_cross_recipient_leak(migrated):
    _, _, _, root, current = migrated
    mail = store(root)
    inbox = mail.inbox(
        "Gamma",
        "fixture-owner-three",
        expected_generation=current,
        mark_read="2026-10-08T13:00:00+00:00",
    )
    assert [row["id"] for row in inbox] == [102]
    assert inbox[0]["kind"] == "bcc" and "registration_token" not in inbox[0]
    mail.acknowledge(
        "Gamma",
        "fixture-owner-three",
        102,
        expected_generation=current,
        at="2026-10-08T13:00:00+00:00",
    )
    with connection(root / "mail.sqlite3") as database:
        assert (
            database.execute(
                "SELECT ack_ts FROM message_recipients WHERE message_id=102"
            ).fetchone()[0]
            == "2026-10-08T13:00:00+00:00"
        )
    with pytest.raises(StateMigrationError, match="MESSAGE_UNAVAILABLE"):
        mail.acknowledge(
            "Beta",
            "fixture-owner-two",
            102,
            expected_generation=current,
            at="2026-10-08T13:00:00+00:00",
        )


def test_persistent_reservations_preserve_imported_ids_and_owner_on_restart(migrated):
    _, choices, run, root, current = migrated
    now = [timestamp(choices["as_of"]).timestamp()]
    first = PersistentReservations(root / "mail.sqlite3", clock=lambda: now[0])
    target = str(Path(choices["lease_anchors"]["1"]) / "src/a.py")
    assert first.dispatch(
        "check_reservations",
        {
            "agent_name": "Alpha",
            "registration_token": "fixture-owner-one",
            "paths": [target],
        },
        expected_generation=current,
    )
    renewed = first.dispatch(
        "renew_file_reservations",
        {
            "agent_name": "Alpha",
            "registration_token": "fixture-owner-one",
            "file_reservation_ids": [1],
            "extend_seconds": 600,
        },
        expected_generation=current,
    )
    assert renewed[0].id == 1
    second = PersistentReservations(root / "mail.sqlite3", clock=lambda: now[0])
    with pytest.raises(ReservationError, match="LEASE_OWNER_REQUIRED"):
        second.dispatch(
            "release_file_reservations",
            {
                "agent_name": "Beta",
                "registration_token": "fixture-owner-two",
                "file_reservation_ids": [1],
            },
            expected_generation=current,
        )
    second.dispatch(
        "release_file_reservations",
        {
            "agent_name": "Alpha",
            "registration_token": "fixture-owner-one",
            "file_reservation_ids": [1],
        },
        expected_generation=current,
    )
    leases = second.dispatch(
        "file_reservation_paths",
        {
            "agent_name": "Beta",
            "registration_token": "fixture-owner-two",
            "paths": [target],
        },
        expected_generation=current,
    )
    assert leases[0].id > 3 and leases[0].owner == "2"
    with connection(root / "mail.sqlite3") as database:
        assert database.execute(
            "SELECT released_ts FROM file_reservations WHERE id=1"
        ).fetchone()[0]
        assert (
            database.execute(
                "SELECT agent_id FROM file_reservations WHERE id=?", (leases[0].id,)
            ).fetchone()[0]
            == 2
        )
    assert not run.status()["rollback_allowed"]


def test_persistent_lease_unknown_does_not_release_early_but_ttl_still_expires(
    migrated,
):
    _, choices, _, root, current = migrated
    now = [timestamp(choices["as_of"]).timestamp() + 1000]
    leases = PersistentReservations(
        root / "mail.sqlite3",
        clock=lambda: now[0],
        probe=lambda path: Activity(False, reason="fixture_unknown"),
    )
    assert leases.collect([1], expected_generation=current) == {1: "activity_unknown"}
    now[0] += 8000
    assert leases.collect([1], expected_generation=current) == {1: "ttl_expired"}


def test_delivery_terminal_observe_preserves_status_attempts_and_backoff(migrated):
    _, choices, run, root, current = migrated
    now = [timestamp(choices["as_of"])]
    delivery = CandidateDelivery(root / "delivery.sqlite3", clock=lambda: now[0])
    policy = {
        "version": "retry-v2",
        "coalesce_seconds": 1,
        "base_backoff_seconds": 3,
        "max_backoff_seconds": 60,
    }
    with connection(root / "delivery.sqlite3") as database:
        before = rows(database, "codex_app_delivery_state")
    for key in (101, 106, 103, 104, 105):
        delivery.observe(
            "fixture-instance", 2, key, policy=policy, expected_generation=current
        )
    with connection(root / "delivery.sqlite3") as database:
        assert rows(database, "codex_app_delivery_state") == before
    assert run.status()["rollback_allowed"]
    assert 104 not in delivery.ready("fixture-instance", 2, expected_generation=current)
    now[0] += timedelta(seconds=9)
    assert 104 in delivery.ready("fixture-instance", 2, expected_generation=current)
    assert delivery.acquire(
        "fixture-instance",
        2,
        104,
        owner="new-wake",
        seconds=30,
        expected_generation=current,
    )
    assert delivery.complete(
        "fixture-instance",
        2,
        104,
        owner="new-wake",
        success=False,
        error="fixture retry",
        expected_generation=current,
    )
    with connection(root / "delivery.sqlite3") as database:
        failed = dict(
            database.execute(
                "SELECT * FROM codex_app_delivery_state WHERE message_id=104"
            ).fetchone()
        )
        assert failed["attempt_count"] == 4
        assert timestamp(failed["next_attempt_at"]) == now[0] + timedelta(seconds=16)
        assert json.loads(failed["policy_json"])["version"] == "retry-v1"


def test_delivery_expired_lease_recovers_without_restarting_old_wake(migrated):
    _, choices, _, root, current = migrated
    now = [timestamp(choices["as_of"])]
    delivery = CandidateDelivery(root / "delivery.sqlite3", clock=lambda: now[0])
    assert 105 not in delivery.ready("fixture-instance", 2, expected_generation=current)
    now[0] += timedelta(seconds=121)
    assert 105 in delivery.ready("fixture-instance", 2, expected_generation=current)
    assert not delivery.complete(
        "fixture-instance",
        2,
        105,
        owner="fixture-wake",
        success=True,
        expected_generation=current,
    )
    assert delivery.acquire(
        "fixture-instance",
        2,
        105,
        owner="new-wake",
        seconds=30,
        expected_generation=current,
    )
    assert not delivery.complete(
        "fixture-instance",
        2,
        105,
        owner="fixture-wake",
        success=True,
        expected_generation=current,
    )
    assert delivery.complete(
        "fixture-instance",
        2,
        105,
        owner="new-wake",
        success=True,
        expected_generation=current,
    )


def test_same_message_id_is_independent_in_another_mail_instance(migrated):
    _, choices, _, root, current = migrated
    now = timestamp(choices["as_of"])
    delivery = CandidateDelivery(root / "delivery.sqlite3", clock=lambda: now)
    policy = {
        "version": "retry-v1",
        "coalesce_seconds": 0,
        "base_backoff_seconds": 2,
        "max_backoff_seconds": 60,
    }
    delivery.observe(
        "other-fixture-instance", 2, 101, policy=policy, expected_generation=current
    )
    assert 101 in delivery.ready(
        "other-fixture-instance", 2, expected_generation=current
    )
    assert 101 not in delivery.ready("fixture-instance", 2, expected_generation=current)


def test_old_writer_generation_cannot_update_mail_leases_or_delivery(migrated):
    _, _, _, root, current = migrated
    with pytest.raises(StateMigrationError, match="STALE_CANDIDATE_WRITER"):
        store(root).reply(
            "Alpha",
            "fixture-owner-one",
            subject="late",
            body_md="fixture",
            recipients={"to": (2,)},
            expected_generation="old-generation",
        )
    with pytest.raises(StateMigrationError, match="STALE_CANDIDATE_WRITER"):
        PersistentReservations(root / "mail.sqlite3").dispatch(
            "release_file_reservations",
            {
                "agent_name": "Alpha",
                "registration_token": "fixture-owner-one",
                "file_reservation_ids": [1],
            },
            expected_generation="old-generation",
        )
    with pytest.raises(StateMigrationError, match="STALE_CANDIDATE_WRITER"):
        CandidateDelivery(root / "delivery.sqlite3").complete(
            "fixture-instance",
            2,
            105,
            owner="fixture-wake",
            success=True,
            expected_generation="old-generation",
        )


@pytest.mark.parametrize(
    "spelling",
    [
        "2026-10-08T10:02:00Z",
        "2026-10-08T12:02:00+02:00",
        "2026-10-08T10:02:00.000000+00:00",
    ],
)
def test_delivery_legacy_expiry_compares_instants_at_boundary(migrated, spelling):
    _, _, _, root, current = migrated
    deadline = timestamp(spelling)
    now = [deadline - timedelta(microseconds=1)]
    with connection(root / "delivery.sqlite3", write=True) as database:
        database.execute(
            "UPDATE codex_app_delivery_state SET lease_expires_at=? WHERE message_id=105",
            (spelling,),
        )
    delivery = CandidateDelivery(root / "delivery.sqlite3", clock=lambda: now[0])
    assert 105 not in delivery.ready("fixture-instance", 2, expected_generation=current)
    now[0] = deadline
    assert not delivery.complete(
        "fixture-instance",
        2,
        105,
        owner="fixture-wake",
        success=True,
        expected_generation=current,
    )
    assert 105 in delivery.ready("fixture-instance", 2, expected_generation=current)


def test_delivery_terminal_transition_is_enforced_by_database(migrated):
    import sqlite3

    _, _, _, root, _ = migrated
    with connection(root / "delivery.sqlite3", write=True) as database:
        with pytest.raises(sqlite3.IntegrityError):
            database.execute(
                "UPDATE codex_app_delivery_state SET status='pending' WHERE message_id=101"
            )


@pytest.mark.parametrize("kind", ["sent", "received"])
@pytest.mark.parametrize(
    "earlier,recent",
    [
        ("2026-10-08T23:00:00+14:00", "2026-10-08T09:59:00Z"),
        ("2026-10-08T09:00:00+00:00", "2026-10-07T23:59:00-10:00"),
    ],
)
def test_reservation_latest_mail_is_selected_by_utc_instant(
    migrated, kind, earlier, recent
):
    _, _, _, root, current = migrated
    path = root / "mail.sqlite3"
    with connection(path, write=True) as database:
        database.execute("UPDATE messages SET created_ts='2026-10-08T08:00:00+00:00'")
    mail = store(root)
    for created in (earlier, recent):
        mail.reply(
            "Alpha" if kind == "sent" else "Beta",
            "fixture-owner-one" if kind == "sent" else "fixture-owner-two",
            subject="fixture activity",
            body_md="fixture",
            recipients={"to": (2 if kind == "sent" else 1,)},
            expected_generation=current,
            created_at=created,
        )
    service = PersistentReservations(
        path,
        clock=lambda: timestamp("2026-10-08T10:00:00Z").timestamp(),
        probe=lambda scope: Activity(True, matched=True),
    )
    assert service.collect([1], expected_generation=current) == {1: "active"}
    with connection(path) as database:
        assert (
            database.execute(
                "SELECT released_ts FROM file_reservations WHERE id=1"
            ).fetchone()[0]
            is None
        )
