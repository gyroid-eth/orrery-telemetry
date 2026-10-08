"""Explicit persistent candidate adapters; default app and published tools are untouched."""

from __future__ import annotations

import hmac
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .namespace_replies import CallerEvidence, MessageRecord, ReplyCatalog, ReplyError
from .namespace_state_io import StateMigrationError, connection, rows
from .namespace_transform import timestamp, iso
from .reservation_candidate import Lease, ReservationServer
from .reservation_paths import ReservationPath, normalize_path
from .utils import sanitize_agent_name


def generation(database: sqlite3.Connection) -> str:
    row = database.execute(
        "SELECT value FROM namespace_metadata WHERE key='generation'"
    ).fetchone()
    if row is None:
        raise StateMigrationError("CANDIDATE_SCHEMA_REQUIRED")
    return row[0]


def authority(database: sqlite3.Connection, expected: str) -> None:
    if not isinstance(expected, str) or generation(database) != expected:
        raise StateMigrationError("STALE_CANDIDATE_WRITER")


def changed(database: sqlite3.Connection) -> None:
    database.execute(
        "UPDATE namespace_metadata SET value=CAST(value AS INTEGER)+1 WHERE key='write_generation'"
    )


def authenticate(database: sqlite3.Connection, name: str, token: str) -> int:
    if not isinstance(name, str) or not isinstance(token, str):
        raise StateMigrationError("OWNER_REQUIRED")
    key = sanitize_agent_name(name).lower()
    row = database.execute(
        "SELECT id,registration_token,retired_at FROM agents WHERE lookup_key=?", (key,)
    ).fetchone()
    if (
        row is None
        or row["retired_at"] is not None
        or not isinstance(row["registration_token"], str)
        or not hmac.compare_digest(token, row["registration_token"])
    ):
        raise StateMigrationError("OWNER_REQUIRED")
    return row["id"]


def reply_catalog(database: sqlite3.Connection) -> ReplyCatalog:
    groups = {}
    for row in rows(database, "message_recipients"):
        groups.setdefault(row["message_id"], {}).setdefault(row["kind"], []).append(
            row["agent_id"]
        )
    return ReplyCatalog(
        [
            MessageRecord(
                row["id"],
                row["legacy_thread_source"] or "local",
                row["sender_id"],
                tuple(groups.get(row["id"], {}).get("to", ())),
                tuple(groups.get(row["id"], {}).get("cc", ())),
                tuple(groups.get(row["id"], {}).get("bcc", ())),
                row["reply_to"],
                row["legacy_thread_label"],
                row["topic"],
            )
            for row in rows(database, "messages")
        ]
    )


class CandidateMailStore:
    """Atomic parent participation/existence, insert and retention; explicit contact checker."""

    def __init__(
        self, path: Path, *, authorize_delivery: Callable[[int, tuple[int, ...]], bool]
    ):
        self.path = path
        self.authorize_delivery = authorize_delivery

    def reconnect(
        self,
        window_row_id: int,
        agent_id: int,
        name: str,
        token: str,
        *,
        expected_generation: str,
    ) -> dict:
        with connection(self.path) as database:
            authority(database, expected_generation)
            owner = authenticate(database, name, token)
            window = database.execute(
                "SELECT agent_id,window_uuid,display_name FROM window_identities WHERE id=?",
                (window_row_id,),
            ).fetchone()
            if owner != agent_id or window is None or window["agent_id"] != owner:
                raise StateMigrationError("WINDOW_OWNER_MISMATCH")
            return {
                "agent_id": owner,
                "window_uuid": window["window_uuid"],
                "agent_name": window["display_name"],
            }

    def reply(
        self,
        name: str,
        token: str,
        *,
        subject: str,
        body_md: str,
        recipients: dict[str, tuple[int, ...]],
        expected_generation: str,
        reply_to: int | None = None,
        thread_id: str | None = None,
        created_at: str | None = None,
    ) -> int:
        # BEGIN IMMEDIATE serializes purge/participation/parent insert in one transaction.
        with connection(self.path, write=True) as database:
            database.execute("BEGIN IMMEDIATE")
            authority(database, expected_generation)
            owner = authenticate(database, name, token)
            parent = reply_catalog(database).resolve(
                CallerEvidence(owner, True),
                reply_to=reply_to,
                legacy_thread_id=thread_id,
            )
            if (
                set(recipients) - {"to", "cc", "bcc"}
                or not isinstance(subject, str)
                or not isinstance(body_md, str)
            ):
                raise StateMigrationError("INVALID_NEW_MESSAGE")
            targets = tuple(item for values in recipients.values() for item in values)
            if (
                any(type(item) is not int or item <= 0 for item in targets)
                or not targets
                or len(set(targets)) != len(targets)
            ):
                raise StateMigrationError("INVALID_RECIPIENTS")
            if self.authorize_delivery(owner, targets) is not True:
                raise StateMigrationError("CONTACT_POLICY_REQUIRED")
            created = created_at or iso(datetime.now(timezone.utc))
            timestamp(created)
            key = database.execute(
                "INSERT INTO messages(legacy_project_id,sender_id,legacy_thread_id_raw,topic,subject,body_md,importance,ack_required,created_ts,attachments,reply_to,legacy_thread_label,legacy_thread_source) VALUES(NULL,?,NULL,NULL,?,?,'normal',0,?,'[]',?,NULL,NULL) RETURNING id",
                (owner, subject, body_md, created, parent),
            ).fetchone()[0]
            for kind, values in recipients.items():
                database.executemany(
                    "INSERT INTO message_recipients(message_id,agent_id,kind) VALUES(?,?,?)",
                    [(key, item, kind) for item in values],
                )
            changed(database)
            return key

    def purge(
        self,
        expired_ids: tuple[int, ...],
        *,
        expected_generation: str,
        dry_run: bool = False,
    ) -> dict:
        with connection(self.path, write=not dry_run) as database:
            database.execute("BEGIN IMMEDIATE" if not dry_run else "BEGIN")
            authority(database, expected_generation)
            catalog = reply_catalog(database)
            plan = catalog.plan_purge(expired_ids)
            if not dry_run:
                database.execute("PRAGMA defer_foreign_keys=ON")
                for key in plan.delete_ids:
                    database.execute(
                        "DELETE FROM namespace_attachment_map WHERE message_id=?",
                        (key,),
                    )
                    database.execute(
                        "DELETE FROM message_recipients WHERE message_id=?", (key,)
                    )
                    database.execute("DELETE FROM messages WHERE id=?", (key,))
                if plan.delete_ids:
                    changed(database)
            return {
                "candidates": plan.candidates,
                "delete_ids": plan.delete_ids,
                "held_ids": plan.held_ids,
                "dry_run": dry_run,
            }

    def inbox(
        self,
        name: str,
        token: str,
        *,
        expected_generation: str,
        mark_read: str | None = None,
    ) -> list[dict]:
        with connection(self.path, write=mark_read is not None) as database:
            if mark_read is not None:
                database.execute("BEGIN IMMEDIATE")
                timestamp(mark_read)
            authority(database, expected_generation)
            owner = authenticate(database, name, token)
            values = [
                dict(row)
                for row in database.execute(
                    "SELECT m.*,r.kind,r.read_ts,r.ack_ts FROM messages m JOIN message_recipients r ON r.message_id=m.id WHERE r.agent_id=? ORDER BY m.id",
                    (owner,),
                )
            ]
            if mark_read is not None:
                if database.execute(
                    "UPDATE message_recipients SET read_ts=? WHERE agent_id=? AND read_ts IS NULL",
                    (mark_read, owner),
                ).rowcount:
                    changed(database)
            # Other recipients' BCC identities and token columns are never selected.
            return values

    def acknowledge(
        self,
        name: str,
        token: str,
        message_id: int,
        *,
        expected_generation: str,
        at: str,
    ) -> None:
        timestamp(at)
        with connection(self.path, write=True) as database:
            database.execute("BEGIN IMMEDIATE")
            authority(database, expected_generation)
            owner = authenticate(database, name, token)
            if not database.execute(
                "UPDATE message_recipients SET ack_ts=COALESCE(ack_ts,?) WHERE message_id=? AND agent_id=?",
                (at, message_id, owner),
            ).rowcount:
                raise StateMigrationError("MESSAGE_UNAVAILABLE")
            changed(database)

    def search(
        self, name: str, token: str, query: str, *, expected_generation: str
    ) -> tuple[int, ...]:
        with connection(self.path) as database:
            authority(database, expected_generation)
            owner = authenticate(database, name, token)
            return tuple(
                row[0]
                for row in database.execute(
                    "SELECT m.id FROM fts_messages f JOIN messages m ON m.id=f.message_id WHERE fts_messages MATCH ? AND (m.sender_id=? OR EXISTS(SELECT 1 FROM message_recipients r WHERE r.message_id=m.id AND r.agent_id=?)) ORDER BY m.id",
                    (query, owner, owner),
                )
            )


class PersistentReservations:
    """PR2 semantics inside an SQLite write transaction; IDs and activity survive restart."""

    def __init__(self, path: Path, *, clock=time.time, probe=None):
        self.path = path
        self.clock = clock
        self.probe = probe

    def dispatch(self, tool: str, arguments: dict, *, expected_generation: str):
        with connection(self.path, write=True) as database:
            database.execute("BEGIN IMMEDIATE")
            authority(database, expected_generation)
            kwargs = {"clock": self.clock}
            if self.probe is not None:
                kwargs["probe"] = self.probe
            server = ReservationServer(
                lambda name, token: str(authenticate(database, name, token)), **kwargs
            )
            original = {row["id"]: row for row in rows(database, "file_reservations")}
            for row in original.values():
                try:
                    scope = (
                        normalize_path(row["path_pattern"])
                        if not row["path_unknown"]
                        else ReservationPath("filesystem", row["path_pattern"])
                    )
                except ValueError:
                    if (
                        row["released_ts"] is None
                        and timestamp(row["expires_ts"]).timestamp() > self.clock()
                    ):
                        raise StateMigrationError(
                            "ACTIVE_LEASE_RULES_UNKNOWN"
                        ) from None
                    scope = ReservationPath("filesystem", row["path_pattern"])
                server._leases[row["id"]] = Lease(
                    row["id"],
                    str(row["agent_id"]),
                    scope,
                    timestamp(row["created_ts"]).timestamp(),
                    timestamp(row["expires_ts"]).timestamp(),
                    bool(row["exclusive"]),
                    row["reason"],
                    row["released_ts"] is not None,
                    row["revision"],
                )
            server._next_id = max(original, default=0) + 1
            for row in rows(database, "agents"):
                mail = database.execute(
                    "SELECT MAX(m.created_ts) FROM messages m WHERE m.sender_id=? OR EXISTS(SELECT 1 FROM message_recipients r WHERE r.message_id=m.id AND r.agent_id=?)",
                    (row["id"], row["id"]),
                ).fetchone()[0]
                server._activity[str(row["id"])] = (
                    timestamp(row["last_active_ts"]).timestamp(),
                    timestamp(mail).timestamp() if mail else None,
                    0,
                )
            before = dict(server._leases)
            result = (
                server.collect(arguments["ids"])
                if tool == "_candidate_collect"
                else server.dispatch(tool, arguments)
            )
            for key, lease in server._leases.items():
                if before.get(key) == lease:
                    continue
                old = original.get(key)
                expires = iso(datetime.fromtimestamp(lease.expires, timezone.utc))
                released = (
                    (
                        old["released_ts"]
                        if old and old["released_ts"]
                        else iso(datetime.fromtimestamp(self.clock(), timezone.utc))
                    )
                    if lease.released
                    else None
                )
                if old:
                    database.execute(
                        "UPDATE file_reservations SET expires_ts=?,released_ts=?,revision=? WHERE id=?",
                        (expires, released, lease.revision, key),
                    )
                else:
                    database.execute(
                        "INSERT INTO file_reservations(id,legacy_project_id,agent_id,path_pattern,exclusive,reason,created_ts,expires_ts,released_ts,legacy_path_pattern,path_unknown,revision) VALUES (?,NULL,?,?,?,?,?,?,?,NULL,0,?)",
                        (
                            key,
                            int(lease.owner),
                            lease.path.value,
                            lease.exclusive,
                            lease.reason,
                            iso(datetime.fromtimestamp(lease.created, timezone.utc)),
                            expires,
                            released,
                            lease.revision,
                        ),
                    )
                changed(database)
            for owner, (active, mail, revision) in server._activity.items():
                if revision:
                    database.execute(
                        "UPDATE agents SET last_active_ts=? WHERE id=?",
                        (iso(datetime.fromtimestamp(active, timezone.utc)), int(owner)),
                    )
                    changed(database)
            return result

    def collect(self, ids: list[int], *, expected_generation: str):
        # Use the same locked import/flush path as tool dispatch.
        return self.dispatch(
            "_candidate_collect", {"ids": ids}, expected_generation=expected_generation
        )
