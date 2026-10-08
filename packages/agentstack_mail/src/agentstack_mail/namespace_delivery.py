"""Persistent global delivery candidate; old daemon still uses its unchanged v1 store."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .namespace_state_io import StateMigrationError, connection
from .namespace_store import authority, changed
from .namespace_transform import iso, timestamp, due_at


class CandidateDelivery:
    def __init__(self, path: Path, *, clock=lambda: datetime.now(timezone.utc)):
        self.path = path
        self.clock = clock

    @staticmethod
    def _key(instance: str, agent: int, message: int) -> tuple:
        if (
            not isinstance(instance, str)
            or not instance
            or type(agent) is not int
            or agent <= 0
            or type(message) is not int
            or message <= 0
        ):
            raise StateMigrationError("INVALID_DELIVERY_KEY")
        return instance, agent, message

    def observe(
        self,
        instance: str,
        agent: int,
        message: int,
        *,
        policy: dict,
        expected_generation: str,
    ) -> None:
        key = self._key(instance, agent, message)
        now = iso(self.clock())
        policy = dict(policy)
        if not isinstance(policy.get("version"), str) or not policy["version"]:
            raise StateMigrationError("DELIVERY_POLICY_REQUIRED")
        # Use the same validation as migration, without needing a legacy project key.
        from .namespace_transform import retry_policy, delivery_key

        retry_policy({"delivery_policies": {delivery_key("", "x"): policy}}, "", "x")
        retry, coalesce = due_at(
            {"created_at": now, "updated_at": now, "attempt_count": 0}, policy
        )
        with connection(self.path, write=True) as database:
            database.execute("BEGIN IMMEDIATE")
            authority(database, expected_generation)
            if database.execute(
                "INSERT OR IGNORE INTO codex_app_delivery_state(mail_instance_id,agent_id,message_id,status,attempt_count,created_at,updated_at,policy_json,next_attempt_at,coalesce_ready_at) VALUES(?,?,?,'pending',0,?,?,?,?,?)",
                (*key, now, now, json.dumps(policy, sort_keys=True), retry, coalesce),
            ).rowcount:
                changed(database)

    def ready(
        self, instance: str, agent: int, *, expected_generation: str
    ) -> tuple[int, ...]:
        self._key(instance, agent, 1)
        instant = self.clock()
        now = iso(instant)
        with connection(self.path, write=True) as database:
            database.execute("BEGIN IMMEDIATE")
            authority(database, expected_generation)
            recovered = False
            result = []
            for row in database.execute(
                "SELECT * FROM codex_app_delivery_state WHERE mail_instance_id=? AND agent_id=? ORDER BY message_id",
                (instance, agent),
            ).fetchall():
                status = row["status"]
                # Legacy timestamps retain their original spelling. Compare instants,
                # not TEXT: Z, UTC offsets and fractional precision can differ.
                if status == "leased" and timestamp(row["lease_expires_at"]) <= instant:
                    database.execute(
                        "UPDATE codex_app_delivery_state SET status='pending',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE mail_instance_id=? AND agent_id=? AND message_id=?",
                        (now, instance, agent, row["message_id"]),
                    )
                    status = "pending"
                    recovered = True
                if timestamp(row["coalesce_ready_at"]) <= instant and (
                    status == "pending"
                    or (
                        status == "failed"
                        and timestamp(row["next_attempt_at"]) <= instant
                    )
                ):
                    result.append(row["message_id"])
            if recovered:
                changed(database)
            return tuple(result)

    def acquire(
        self,
        instance: str,
        agent: int,
        message: int,
        *,
        owner: str,
        seconds: int,
        expected_generation: str,
    ) -> bool:
        key = self._key(instance, agent, message)
        if (
            not isinstance(owner, str)
            or not owner
            or type(seconds) is not int
            or not 1 <= seconds <= 86400
        ):
            raise StateMigrationError("INVALID_DELIVERY_LEASE")
        instant = self.clock()
        now = iso(instant)
        expires = iso(instant + timedelta(seconds=seconds))
        with connection(self.path, write=True) as database:
            database.execute("BEGIN IMMEDIATE")
            authority(database, expected_generation)
            row = database.execute(
                "SELECT * FROM codex_app_delivery_state WHERE mail_instance_id=? AND agent_id=? AND message_id=?",
                key,
            ).fetchone()
            if (
                row is None
                or timestamp(row["coalesce_ready_at"]) > instant
                or not (
                    row["status"] == "pending"
                    or (
                        row["status"] == "failed"
                        and timestamp(row["next_attempt_at"]) <= instant
                    )
                )
            ):
                return False
            database.execute(
                "UPDATE codex_app_delivery_state SET status='leased',lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE mail_instance_id=? AND agent_id=? AND message_id=?",
                (owner, expires, now, *key),
            )
            changed(database)
            return True

    def complete(
        self,
        instance: str,
        agent: int,
        message: int,
        *,
        owner: str,
        success: bool,
        expected_generation: str,
        error: str | None = None,
    ) -> bool:
        key = self._key(instance, agent, message)
        if type(success) is not bool or (
            error is not None and (not isinstance(error, str) or len(error) > 2048)
        ):
            raise StateMigrationError("INVALID_DELIVERY_RESULT")
        instant = self.clock()
        now = iso(instant)
        with connection(self.path, write=True) as database:
            database.execute("BEGIN IMMEDIATE")
            authority(database, expected_generation)
            row = database.execute(
                "SELECT * FROM codex_app_delivery_state WHERE mail_instance_id=? AND agent_id=? AND message_id=? AND status='leased' AND lease_owner=?",
                (*key, owner),
            ).fetchone()
            if row is None or timestamp(row["lease_expires_at"]) <= instant:
                return False
            policy = json.loads(row["policy_json"])
            retry, _ = due_at({**dict(row), "updated_at": now}, policy)
            database.execute(
                "UPDATE codex_app_delivery_state SET status=?,lease_owner=NULL,lease_expires_at=NULL,delivered_at=?,last_error=?,updated_at=?,next_attempt_at=? WHERE mail_instance_id=? AND agent_id=? AND message_id=?",
                (
                    "delivered" if success else "failed",
                    now if success else None,
                    error,
                    now,
                    retry,
                    *key,
                ),
            )
            changed(database)
            return True
