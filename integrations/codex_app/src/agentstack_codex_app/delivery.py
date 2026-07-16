"""SQLite-backed at-least-once delivery leases for Codex App cold wake."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class DeliveryStatus:
    pending_count: int
    leased_count: int
    failed_count: int
    dead_letter_count: int
    last_error: str | None


class DeliveryManager:
    """Apply the fixed migration and coordinate idempotent message delivery."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        migration_path: str | os.PathLike[str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.migration_path = (
            Path(migration_path).expanduser()
            if migration_path is not None
            else _default_migration_path()
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def observe(
        self,
        project_key: str,
        agent_name: str,
        message_ids: Iterable[int],
    ) -> None:
        """Insert newly observed message IDs without reopening terminal rows."""

        rows = [
            (project_key, agent_name, message_id)
            for message_id in sorted(set(message_ids))
            if _positive_int(message_id)
        ]
        if not rows:
            return
        now = _timestamp(self.clock())
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO codex_app_delivery_state
                    (project_key, agent_name, message_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(*row, now, now) for row in rows],
            )

    def reconcile_absent(
        self,
        project_key: str,
        agent_name: str,
        present_message_ids: Iterable[int],
    ) -> list[int]:
        """Mark formerly observed signals delivered after inbox consumption."""

        present = {item for item in present_message_ids if _positive_int(item)}
        now_dt = self.clock()
        now = _timestamp(now_dt)
        lease_expiry = _timestamp(now_dt + timedelta(seconds=1))
        delivered: list[int] = []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT message_id, status
                FROM codex_app_delivery_state
                WHERE project_key = ? AND agent_name = ?
                  AND status IN ('pending', 'failed')
                """,
                (project_key, agent_name),
            ).fetchall()
            for row in rows:
                message_id = int(row["message_id"])
                if message_id in present:
                    continue
                if row["status"] == "failed":
                    connection.execute(
                        """
                        UPDATE codex_app_delivery_state
                        SET status = 'pending', last_error = NULL, updated_at = ?
                        WHERE project_key = ? AND agent_name = ? AND message_id = ?
                          AND status = 'failed'
                        """,
                        (now, project_key, agent_name, message_id),
                    )
                connection.execute(
                    """
                    UPDATE codex_app_delivery_state
                    SET status = 'leased', lease_owner = 'signal-consumed',
                        lease_expires_at = ?, updated_at = ?
                    WHERE project_key = ? AND agent_name = ? AND message_id = ?
                      AND status = 'pending'
                    """,
                    (lease_expiry, now, project_key, agent_name, message_id),
                )
                changed = connection.execute(
                    """
                    UPDATE codex_app_delivery_state
                    SET status = 'delivered', lease_owner = NULL,
                        lease_expires_at = NULL, delivered_at = ?, updated_at = ?
                    WHERE project_key = ? AND agent_name = ? AND message_id = ?
                      AND status = 'leased' AND lease_owner = 'signal-consumed'
                    """,
                    (now, now, project_key, agent_name, message_id),
                ).rowcount
                if changed:
                    delivered.append(message_id)
        return delivered

    def ready_ids(
        self,
        project_key: str,
        agent_name: str,
        present_message_ids: Iterable[int],
        *,
        coalesce_seconds: float,
        base_backoff_seconds: float,
        max_backoff_seconds: float,
    ) -> list[int]:
        """Return one coalesced batch after recovering leases and due retries."""

        present = sorted(
            {item for item in present_message_ids if _positive_int(item)}
        )
        if not present:
            return []
        now_dt = self.clock()
        now = _timestamp(now_dt)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE codex_app_delivery_state
                SET status = 'pending', lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE project_key = ? AND agent_name = ? AND status = 'leased'
                  AND lease_expires_at <= ?
                """,
                (now, project_key, agent_name, now),
            )
            failed = connection.execute(
                """
                SELECT message_id, attempt_count, updated_at
                FROM codex_app_delivery_state
                WHERE project_key = ? AND agent_name = ? AND status = 'failed'
                """,
                (project_key, agent_name),
            ).fetchall()
            for row in failed:
                delay = min(
                    max_backoff_seconds,
                    base_backoff_seconds
                    * (2 ** max(0, int(row["attempt_count"]) - 1)),
                )
                if _parse_timestamp(row["updated_at"]) + timedelta(
                    seconds=delay
                ) <= now_dt:
                    connection.execute(
                        """
                        UPDATE codex_app_delivery_state
                        SET status = 'pending', last_error = NULL, updated_at = ?
                        WHERE project_key = ? AND agent_name = ? AND message_id = ?
                          AND status = 'failed'
                        """,
                        (now, project_key, agent_name, int(row["message_id"])),
                    )
            placeholders = ",".join("?" for _ in present)
            rows = connection.execute(
                f"""
                SELECT message_id, created_at
                FROM codex_app_delivery_state
                WHERE project_key = ? AND agent_name = ? AND status = 'pending'
                  AND message_id IN ({placeholders})
                ORDER BY message_id
                """,
                (project_key, agent_name, *present),
            ).fetchall()
        if not rows:
            return []
        oldest = min(_parse_timestamp(row["created_at"]) for row in rows)
        if oldest + timedelta(seconds=coalesce_seconds) > now_dt:
            return []
        return [int(row["message_id"]) for row in rows]

    def acquire(
        self,
        project_key: str,
        agent_name: str,
        message_ids: Iterable[int],
        *,
        lease_owner: str,
        lease_seconds: float,
    ) -> list[int]:
        """Acquire a batch lease and increment attempts exactly once."""

        ids = sorted({item for item in message_ids if _positive_int(item)})
        if not ids:
            return []
        now_dt = self.clock()
        now = _timestamp(now_dt)
        expires = _timestamp(now_dt + timedelta(seconds=lease_seconds))
        acquired: list[int] = []
        with self._connect() as connection:
            for message_id in ids:
                changed = connection.execute(
                    """
                    UPDATE codex_app_delivery_state
                    SET status = 'leased', lease_owner = ?, lease_expires_at = ?,
                        attempt_count = attempt_count + 1, last_error = NULL,
                        updated_at = ?
                    WHERE project_key = ? AND agent_name = ? AND message_id = ?
                      AND status = 'pending'
                    """,
                    (
                        lease_owner,
                        expires,
                        now,
                        project_key,
                        agent_name,
                        message_id,
                    ),
                ).rowcount
                if changed:
                    acquired.append(message_id)
        return acquired

    def mark_delivered(
        self,
        project_key: str,
        agent_name: str,
        message_ids: Iterable[int],
        *,
        lease_owner: str,
    ) -> None:
        now = _timestamp(self.clock())
        with self._connect() as connection:
            for message_id in set(message_ids):
                connection.execute(
                    """
                    UPDATE codex_app_delivery_state
                    SET status = 'delivered', lease_owner = NULL,
                        lease_expires_at = NULL, delivered_at = ?, updated_at = ?
                    WHERE project_key = ? AND agent_name = ? AND message_id = ?
                      AND status = 'leased' AND lease_owner = ?
                    """,
                    (
                        now,
                        now,
                        project_key,
                        agent_name,
                        message_id,
                        lease_owner,
                    ),
                )

    def mark_failed(
        self,
        project_key: str,
        agent_name: str,
        message_ids: Iterable[int],
        *,
        lease_owner: str,
        error_code: str,
        max_attempts: int,
        terminal: bool = False,
    ) -> None:
        """Fail a lease, promoting exhausted or blocked attempts to dead-letter."""

        now = _timestamp(self.clock())
        with self._connect() as connection:
            for message_id in set(message_ids):
                row = connection.execute(
                    """
                    SELECT attempt_count
                    FROM codex_app_delivery_state
                    WHERE project_key = ? AND agent_name = ? AND message_id = ?
                      AND status = 'leased' AND lease_owner = ?
                    """,
                    (project_key, agent_name, message_id, lease_owner),
                ).fetchone()
                if row is None:
                    continue
                status = (
                    "dead_letter"
                    if terminal or int(row["attempt_count"]) >= max_attempts
                    else "failed"
                )
                connection.execute(
                    """
                    UPDATE codex_app_delivery_state
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                        last_error = ?, updated_at = ?
                    WHERE project_key = ? AND agent_name = ? AND message_id = ?
                      AND status = 'leased' AND lease_owner = ?
                    """,
                    (
                        status,
                        _error_code(error_code),
                        now,
                        project_key,
                        agent_name,
                        message_id,
                        lease_owner,
                    ),
                )

    def status(self, project_key: str, agent_name: str) -> DeliveryStatus:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM codex_app_delivery_state
                WHERE project_key = ? AND agent_name = ?
                GROUP BY status
                """,
                (project_key, agent_name),
            ).fetchall()
            error = connection.execute(
                """
                SELECT last_error
                FROM codex_app_delivery_state
                WHERE project_key = ? AND agent_name = ?
                  AND last_error IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (project_key, agent_name),
            ).fetchone()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return DeliveryStatus(
            pending_count=sum(
                counts.get(status, 0)
                for status in ("pending", "leased", "failed")
            ),
            leased_count=counts.get("leased", 0),
            failed_count=counts.get("failed", 0),
            dead_letter_count=counts.get("dead_letter", 0),
            last_error=str(error["last_error"]) if error is not None else None,
        )

    def rows(self) -> list[dict[str, Any]]:
        """Return test/diagnostic rows without exposing message bodies."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT project_key, agent_name, message_id, status, lease_owner,
                       lease_expires_at, attempt_count, last_error, created_at,
                       updated_at, delivered_at
                FROM codex_app_delivery_state
                ORDER BY project_key, agent_name, message_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.database_path.parent, 0o700)
        migration = self.migration_path.read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.executescript(migration)
        os.chmod(self.database_path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _default_migration_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "migrations"
        / "001_delivery_state.sql"
    )


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _error_code(value: str) -> str:
    safe = "".join(
        character
        for character in str(value).lower()
        if character.isascii() and (character.isalnum() or character in "_-")
    )
    return (safe or "wake_failed")[:64]
