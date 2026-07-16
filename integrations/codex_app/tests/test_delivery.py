from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from agentstack_codex_app.delivery import DeliveryManager


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += timedelta(seconds=seconds)


def _manager(tmp_path, clock):
    return DeliveryManager(tmp_path / "delivery.sqlite3", clock=clock)


def test_delivery_is_idempotent_and_delivered_rows_are_terminal(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    manager.observe("/workspace/example", "Calm-Noether", [7, 7])
    clock.advance(2)

    ready = manager.ready_ids(
        "/workspace/example",
        "Calm-Noether",
        [7],
        coalesce_seconds=2,
        base_backoff_seconds=2,
        max_backoff_seconds=30,
    )
    assert ready == [7]
    assert manager.acquire(
        "/workspace/example",
        "Calm-Noether",
        ready,
        lease_owner="wake-one",
        lease_seconds=30,
    ) == [7]
    manager.mark_delivered(
        "/workspace/example",
        "Calm-Noether",
        [7],
        lease_owner="wake-one",
    )
    manager.observe("/workspace/example", "Calm-Noether", [7])

    row = manager.rows()[0]
    assert row["status"] == "delivered"
    assert row["attempt_count"] == 1
    assert os.stat(manager.database_path).st_mode & 0o777 == 0o600


def test_expired_lease_and_failed_attempt_use_exponential_retry(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    manager.observe("/workspace/example", "Calm-Noether", [8])
    clock.advance(2)
    manager.acquire(
        "/workspace/example",
        "Calm-Noether",
        [8],
        lease_owner="wake-one",
        lease_seconds=1,
    )
    clock.advance(2)

    assert manager.ready_ids(
        "/workspace/example",
        "Calm-Noether",
        [8],
        coalesce_seconds=0,
        base_backoff_seconds=2,
        max_backoff_seconds=30,
    ) == [8]
    manager.acquire(
        "/workspace/example",
        "Calm-Noether",
        [8],
        lease_owner="wake-two",
        lease_seconds=30,
    )
    manager.mark_failed(
        "/workspace/example",
        "Calm-Noether",
        [8],
        lease_owner="wake-two",
        error_code="resume_failed",
        max_attempts=5,
    )

    assert manager.ready_ids(
        "/workspace/example",
        "Calm-Noether",
        [8],
        coalesce_seconds=0,
        base_backoff_seconds=2,
        max_backoff_seconds=30,
    ) == []
    clock.advance(4)
    assert manager.ready_ids(
        "/workspace/example",
        "Calm-Noether",
        [8],
        coalesce_seconds=0,
        base_backoff_seconds=2,
        max_backoff_seconds=30,
    ) == [8]


def test_signal_disappearance_marks_active_turn_delivery_complete(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    manager.observe("/workspace/example", "Calm-Noether", [9, 10])

    assert manager.reconcile_absent(
        "/workspace/example",
        "Calm-Noether",
        [10],
    ) == [9]
    rows = {row["message_id"]: row for row in manager.rows()}
    assert rows[9]["status"] == "delivered"
    assert rows[10]["status"] == "pending"


def test_exhausted_attempt_moves_to_dead_letter(tmp_path):
    clock = MutableClock()
    manager = _manager(tmp_path, clock)
    manager.observe("/workspace/example", "Calm-Noether", [11])
    manager.acquire(
        "/workspace/example",
        "Calm-Noether",
        [11],
        lease_owner="wake-one",
        lease_seconds=30,
    )
    manager.mark_failed(
        "/workspace/example",
        "Calm-Noether",
        [11],
        lease_owner="wake-one",
        error_code="resume_failed",
        max_attempts=1,
    )

    status = manager.status("/workspace/example", "Calm-Noether")
    assert status.pending_count == 0
    assert status.dead_letter_count == 1
    assert status.last_error == "resume_failed"
