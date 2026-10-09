"""Isolated schema3 owner/window/contact server. No installed cutover."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
from pathlib import Path

from fastmcp.tools.tool import Tool, ToolResult
from pydantic import PrivateAttr

from .global_server import (
    GlobalRuntime,
    ENROLLMENT_DDL,
    GlobalError,
    TOOLS as S1_TOOLS,
    build_global_server,
    document,
    integer,
    now,
    safe_directory,
)
from .global_s2a_contract import (
    FIXTURE,
    TOOLS,
    arguments,
    canonical,
    digest,
    preimage,
    unique_document,
    validate,
)
from .namespace_store import changed

EXTENSION_NAMES = {
    "global_runtime_contract",
    "global_operation_receipts",
    "uq_global_contact_pair",
}


def revision(db):
    return int(
        db.execute(
            "SELECT value FROM namespace_metadata WHERE key='write_generation'"
        ).fetchone()[0]
    )


def utc(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            stamp.replace(tzinfo=timezone.utc)
            if stamp.tzinfo is None
            else stamp.astimezone(timezone.utc)
        )
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise GlobalError("TIMESTAMP_INVALID") from None


def iso(value):
    return utc(value).isoformat() if value is not None else None


def schema_objects(db):
    return [
        list(r)
        for r in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]


def base_schema(db):
    return [
        r
        for r in schema_objects(db)
        if r[1] not in EXTENSION_NAMES and r[1] not in ENROLLMENT_DDL
    ]


def extension_schema(db):
    return [r for r in schema_objects(db) if r[1] in EXTENSION_NAMES]


def integrity(db, *, writable=False):
    if (
        db.execute("PRAGMA quick_check").fetchone()[0] != "ok"
        or db.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise GlobalError("RUNTIME_CANDIDATE_INVALID")
    # Keep the established read snapshot (including WAL) and bound RAM use.
    # SQLite's FTS integrity command needs a writable DB; only this private
    # disposable disk backup receives that command, never the source snapshot.
    try:
        if writable:
            db.execute(
                "INSERT INTO fts_messages(fts_messages) VALUES('integrity-check')"
            )
        else:
            main = next(
                row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"
            )
            if not main or not Path(main).is_absolute():
                raise GlobalError("RUNTIME_CANDIDATE_INVALID")
            parent = Path(main).parent / ".fts-validation"
            # The source database is already admitted inside the isolated
            # candidate. Never let the system TMPDIR receive its private data.
            safe_directory(parent, create=True)
            with tempfile.TemporaryDirectory(
                prefix="snapshot-", dir=parent
            ) as directory:
                path = Path(directory) / "check.sqlite3"
                path.touch(mode=0o600)
                with closing(sqlite3.connect(path)) as checker:
                    db.backup(checker)
                    checker.execute(
                        "INSERT INTO fts_messages(fts_messages) VALUES('integrity-check')"
                    )
    except sqlite3.Error:
        raise GlobalError("RUNTIME_CANDIDATE_INVALID") from None


def validate_receipt(db, row, instance, candidate):
    try:
        request = unique_document(row["canonical_request_json"])
        response = unique_document(row["receipt_json"])
        tool = row["tool"]
        if tool not in TOOLS or TOOLS[tool]["operation"] != "write":
            raise ValueError
        if set(request) != set(
            FIXTURE["candidate_validation_contract"]["request_preimage"][
                "exact_top_level_keys"
            ]
        ):
            raise ValueError
        supplied = {
            **request["arguments"],
            "expected_server_instance_id": request["server_instance_id"],
            **{
                k: request[k]
                for k in (
                    "candidate_generation",
                    "authority_epoch",
                    "agent_id",
                    "expected_credential_generation",
                )
            },
            "registration_token": "validation-only",
            "request_id": row["request_id"],
        }
        normalized = arguments(tool, supplied)
        text, request_hash = preimage(tool, normalized)
        if (
            text != row["canonical_request_json"]
            or request_hash != row["request_hash"]
            or request["server_instance_id"] != row["server_instance_id"]
            or row["server_instance_id"] != instance
            or request["candidate_generation"] != row["candidate_generation"]
            or row["candidate_generation"] != candidate
            or request["authority_epoch"] != row["authority_epoch"]
            or request["agent_id"] != row["agent_id"]
            or request["expected_credential_generation"] != row["credential_generation"]
            or response["request_id"] != row["request_id"]
            or response["committed_at"] != row["committed_at"]
            or response["mutation_revision"] != row["mutation_revision"]
            or not 1 <= row["mutation_revision"] <= revision(db)
            or canonical(response) != row["receipt_json"]
            or digest(response) != row["receipt_sha256"]
        ):
            raise ValueError
        validate(response, TOOLS[tool]["output_schema"], "REQUEST_RECEIPT_INVALID")
        if tool in ("refresh_registration", "set_contact_policy"):
            if (
                any(
                    response[k] != request[k]
                    for k in (
                        "server_instance_id",
                        "candidate_generation",
                        "authority_epoch",
                        "agent_id",
                    )
                )
                or response["credential_generation"]
                != request["expected_credential_generation"]
            ):
                raise ValueError
        if tool in (
            "request_contact",
            "respond_contact",
            "macro_contact_handshake",
        ):
            contact = (
                response["contact"] if tool == "macro_contact_handshake" else response
            )
            source = (
                request["arguments"]["from_agent_id"]
                if tool == "respond_contact"
                else request["agent_id"]
            )
            target = (
                request["agent_id"]
                if tool == "respond_contact"
                else request["arguments"]["to_agent_id"]
            )
            if contact["from_agent_id"] != source or contact["to_agent_id"] != target:
                raise ValueError
        if (
            tool == "set_contact_policy"
            and response["policy"] != request["arguments"]["policy"]
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError, GlobalError):
        raise GlobalError("REQUEST_RECEIPT_INVALID") from None


def validate_receipts(db, instance, candidate):
    for row in db.execute("SELECT * FROM global_operation_receipts"):
        validate_receipt(db, row, instance, candidate)


def validate_runtime_candidate(db, *, full=False, writable=False, allow_gap=False):
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise GlobalError("RUNTIME_SCHEMA_UNSUPPORTED")
        contracts = list(db.execute("SELECT * FROM global_runtime_contract"))
        if len(contracts) != 1 or contracts[0]["schema_version"] != "s2a-v1":
            raise GlobalError("RUNTIME_SCHEMA_UNSUPPORTED")
        contract = contracts[0]
        if (
            digest(base_schema(db)) != contract["base_schema_digest"]
            or digest(extension_schema(db)) != contract["extension_schema_digest"]
        ):
            raise GlobalError("RUNTIME_SCHEMA_UNSUPPORTED")
        for name, ddl in ENROLLMENT_DDL.items():
            found = db.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()
            if found and found[0] != ddl:
                raise GlobalError("RUNTIME_SCHEMA_UNSUPPORTED")
        if not allow_gap and revision(db) != contract["last_validated_revision"]:
            raise GlobalError("RUNTIME_WRITER_MISMATCH")
        if full:
            integrity(db, writable=writable)
            instance = db.execute("SELECT instance_id FROM mail_instances").fetchone()[
                0
            ]
            candidate = db.execute(
                "SELECT value FROM namespace_metadata WHERE key='generation'"
            ).fetchone()[0]
            validate_receipts(db, instance, candidate)
    except sqlite3.Error:
        raise GlobalError("RUNTIME_SCHEMA_UNSUPPORTED") from None


class S2aRuntime(GlobalRuntime):
    config_kind = "orrery-global-server-s2a-v1"
    schema_version = 3
    tools = S1_TOOLS | TOOLS.keys()
    health_fields = {
        "capabilities": sorted(FIXTURE["capabilities_added"]),
        "runtime_profile": "s2a-v1",
    }

    def __init__(self, config):
        self.fault = lambda point: None
        super().__init__(config)
        self.receipt_limit = self.config.get("operation_receipt_limit", 100000)
        self.window_ttl = self.config.get("window_ttl_seconds", 2592000)
        self.auto_ttl = self.config.get("contact_auto_ttl_seconds", 86400)
        if not all(
            integer(x) for x in (self.receipt_limit, self.window_ttl, self.auto_ttl)
        ):
            raise GlobalError("ISOLATED_CONFIG_REQUIRED")
        if self.config.get("contact_enforcement", True) is not True:
            raise GlobalError("CONFIG_CONTACT_ENFORCEMENT_UNSUPPORTED")
        with self.transaction() as db:
            validate_runtime_candidate(db, full=True)

    def initial_preflight(self):
        # Publication is the single admission boundary, before even opening the
        # main database file. No SQLite/header probe of an old root is needed.
        receipt_path = self.runtime_root.parent / "preparation-receipt.json"
        if not receipt_path.exists() and not receipt_path.is_symlink():
            raise GlobalError("CANDIDATE_SCHEMA_REQUIRES_REPREPARE")
        receipt = document(receipt_path)
        if (
            receipt.get("kind") != "orrery-s2a-preparation-receipt-v1"
            or receipt.get("phase") != "complete"
            or receipt.get("server_instance_id") != self.config.get("mail_instance_id")
            or receipt.get("candidate_generation")
            != self.config.get("candidate_generation")
            or receipt.get("authority_epoch") != self.config.get("authority_epoch")
        ):
            raise GlobalError("PREPARATION_INCOMPLETE")
        if (
            self.runtime_root.parent.parent != self.root / "s2a-candidates"
            or self.runtime_root.name != "candidate"
            or self.paths["database"] != self.runtime_root / "mail.sqlite3"
        ):
            raise GlobalError("ISOLATED_CONFIG_REQUIRED")

    def before_operation(self, db, *, write):
        self.initial_preflight()
        validate_runtime_candidate(db)
        registering = getattr(self, "register_window", None)
        if registering and registering[0] is not None:
            old = db.execute(
                "SELECT last_active_ts FROM agents WHERE id=?", (registering[0],)
            ).fetchone()
            self.register_previous_activity = old[0] if old else None

    def receipts_used(self, db, agent_id):
        # Use the existing receipt PK prefix instead of scanning other owners.
        # No duplicate persistent quota counter is maintained.
        return db.execute(
            "SELECT count(*) FROM global_operation_receipts WHERE server_instance_id=? AND agent_id=?",
            (self.instance, agent_id),
        ).fetchone()[0]

    def inspect(self, db, agent_id):
        validate_runtime_candidate(db, full=True)
        result = super().inspect(db, agent_id)
        cursor = getattr(self, "capacity_cursor", None)
        limit = getattr(self, "capacity_limit", 100)

        def owner_count(aid):
            used = self.receipts_used(db, aid)
            return {
                "agent_id": aid,
                "used": used,
                "remaining": max(0, self.receipt_limit - used),
                "new_requests_allowed": used < self.receipt_limit,
            }

        ids = [
            r[0]
            for r in db.execute(
                "SELECT id FROM agents WHERE id>? ORDER BY id LIMIT ?",
                (cursor or 0, limit + 1),
            )
        ]
        result["receipt_capacity"] = {
            "per_owner_limit": self.receipt_limit,
            "owner": owner_count(agent_id),
            "total_used": db.execute(
                "SELECT count(*) FROM global_operation_receipts"
            ).fetchone()[0],
            "owners": [owner_count(i) for i in ids[:limit]],
            "next_owner_id": ids[limit - 1] if len(ids) > limit else None,
            "retention": "candidate-lifetime-no-auto-delete",
        }
        return result

    def _management_apply(self, request, peer_uid):
        # The event loop invokes management synchronously; no await occurs while
        # these scoped pagination values exist. Not caller authentication data.
        if isinstance(request, dict) and request.get("action") == "inspect":
            cursor = request.get("capacity_after_agent_id")
            limit = request.get("capacity_limit", 100)
            if (
                (cursor is not None and not integer(cursor))
                or not integer(limit)
                or limit > 1000
            ):
                raise GlobalError("LIMIT_INVALID")
            self.capacity_cursor, self.capacity_limit = cursor, limit
            try:
                return super()._management_apply(request, peer_uid)
            finally:
                self.capacity_cursor, self.capacity_limit = None, 100
        return super()._management_apply(request, peer_uid)

    def window(self, db, aid, row_id, window_uuid):
        row = db.execute(
            "SELECT * FROM window_identities WHERE id=?", (row_id,)
        ).fetchone()
        if row is None or row["agent_id"] != aid or row["window_uuid"] != window_uuid:
            raise GlobalError("WINDOW_OWNER_MISMATCH")
        return row

    def touch(self, db, owner, window, stamp):
        changes = False
        old = utc(owner["last_active_ts"])
        if stamp > old:
            db.execute(
                "UPDATE agents SET last_active_ts=? WHERE id=?",
                (stamp.isoformat(), owner["id"]),
            )
            changes = True
        if window is not None:
            active = window["last_active_ts"]
            expiry = window["expires_ts"]
            if stamp > utc(active):
                active = stamp.isoformat()
            if expiry is not None:
                proposed = stamp + timedelta(seconds=self.window_ttl)
                if proposed > utc(expiry):
                    expiry = proposed.isoformat()
            if active != window["last_active_ts"] or expiry != window["expires_ts"]:
                db.execute(
                    "UPDATE window_identities SET last_active_ts=?,expires_ts=? WHERE id=?",
                    (active, expiry, window["id"]),
                )
                changes = True
        return changes

    def register(
        self,
        binding,
        agent_id,
        name,
        program,
        model,
        task,
        token,
        window_row,
        window_uuid,
    ):
        # S1 signature is unchanged. Its existing write already increments once;
        # the exact window's touch joins that same transaction via this hook.
        self.register_window = (agent_id, window_row, window_uuid)
        try:
            return super().register(
                binding,
                agent_id,
                name,
                program,
                model,
                task,
                token,
                window_row,
                window_uuid,
            )
        finally:
            self.register_window = None

    def after_operation(self, db, *, write):
        if write:
            binding = getattr(self, "register_window", None)
            if binding and binding[0] is not None:
                old = getattr(self, "register_previous_activity", None)
                current = db.execute(
                    "SELECT last_active_ts FROM agents WHERE id=?", (binding[0],)
                ).fetchone()
                if old and current and utc(old) > utc(current[0]):
                    db.execute(
                        "UPDATE agents SET last_active_ts=? WHERE id=?",
                        (old, binding[0]),
                    )
            if binding and binding[1] is not None:
                owner = db.execute(
                    "SELECT * FROM agents WHERE id=?", (binding[0],)
                ).fetchone()
                self.touch(db, owner, self.window(db, *binding), utc(now()))
            db.execute(
                "UPDATE global_runtime_contract SET last_validated_revision=? WHERE id=1",
                (revision(db),),
            )
            validate_runtime_candidate(db)

    def peer(self, db, aid):
        row = db.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
        if row is None or row["retired_at"] is not None:
            raise GlobalError("TARGET_UNAVAILABLE")
        return row

    def contact_value(self, db, row):
        def peer(aid):
            r = db.execute(
                "SELECT id,name,retired_at FROM agents WHERE id=?", (aid,)
            ).fetchone()
            return {
                "agent_id": r["id"],
                "name": r["name"],
                "retired_at": iso(r["retired_at"]),
            }

        return {
            "id": row["id"],
            "from_agent_id": row["a_agent_id"],
            "to_agent_id": row["b_agent_id"],
            "from_identity": peer(row["a_agent_id"]),
            "to_identity": peer(row["b_agent_id"]),
            "status": row["status"],
            "reason": row["reason"],
            **{k: iso(row[k]) for k in ("created_ts", "updated_ts", "expires_ts")},
            "effective": row["status"] == "approved",
            "approval_expiry_mode": "informational-compatible",
            "notification": "not-supported-until-s2b",
        }

    def expiry(self, stamp, seconds):
        try:
            return (stamp + timedelta(seconds=seconds)).isoformat()
        except (OverflowError, ValueError):
            raise GlobalError("CONTACT_TTL_INVALID") from None

    def contact(self, db, tool, owner, args, stamp):
        respond = tool == "respond_contact"
        aid = args["from_agent_id"] if respond else owner["id"]
        bid = owner["id"] if respond else args["to_agent_id"]
        if aid == bid:
            raise GlobalError("SELF_CONTACT_NOT_SUPPORTED")
        self.peer(db, aid if respond else bid)
        if tool == "macro_contact_handshake":
            if args["auto_accept"]:
                raise GlobalError("CONTACT_TARGET_CONSENT_REQUIRED")
            if args["welcome_subject"] is not None or args["welcome_body"] is not None:
                raise GlobalError("CONTACT_MESSAGE_REQUIRES_S2B")
        if not respond or args["accept"]:
            self.expiry(stamp, args["ttl_seconds"])
        row = db.execute(
            "SELECT * FROM agent_links WHERE a_agent_id=? AND b_agent_id=?", (aid, bid)
        ).fetchone()
        write = (
            respond
            or row is None
            or (
                row["status"] == "pending"
                and row["expires_ts"] is not None
                and utc(row["expires_ts"]) <= stamp
            )
        )
        if write:
            status = (
                ("approved" if args["accept"] else "blocked") if respond else "pending"
            )
            expiry = (
                None if status == "blocked" else self.expiry(stamp, args["ttl_seconds"])
            )
            reason = (
                row["reason"] if respond and row is not None else args.get("reason", "")
            )
            if row is None:
                db.execute(
                    "INSERT INTO agent_links(a_agent_id,b_agent_id,status,reason,created_ts,updated_ts,expires_ts) VALUES(?,?,?,?,?,?,?)",
                    (
                        aid,
                        bid,
                        status,
                        reason,
                        stamp.isoformat(),
                        stamp.isoformat(),
                        expiry,
                    ),
                )
            else:
                db.execute(
                    "UPDATE agent_links SET status=?,reason=?,updated_ts=?,expires_ts=? WHERE id=?",
                    (status, reason, stamp.isoformat(), expiry, row["id"]),
                )
            row = db.execute(
                "SELECT * FROM agent_links WHERE a_agent_id=? AND b_agent_id=?",
                (aid, bid),
            ).fetchone()
        value = self.contact_value(db, row)
        if tool == "macro_contact_handshake":
            return {
                "contact": value,
                "stage": "request-only",
                "notification": "not-supported-until-s2b",
                "approval_required": value["status"] != "approved",
                "approval_required_from_agent_id": (
                    bid if value["status"] != "approved" else None
                ),
            }
        return value

    def apply(self, tool, supplied):
        args = arguments(tool, supplied)
        writes = TOOLS[tool]["operation"].startswith("write")
        receipt = TOOLS[tool]["operation"] == "write"
        binding = tuple(
            args[k]
            for k in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        )
        with self.transaction(write=writes, binding=binding) as db:
            owner = self.owner(db, args["agent_id"], args["registration_token"])
            if owner["credential_generation"] != args["expected_credential_generation"]:
                raise GlobalError("CREDENTIAL_GENERATION_MISMATCH")
            if receipt:
                raw, hashed = preimage(tool, args)
                prior = db.execute(
                    "SELECT * FROM global_operation_receipts WHERE server_instance_id=? AND agent_id=? AND request_id=?",
                    (self.instance, owner["id"], args["request_id"]),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != hashed:
                        raise GlobalError("REQUEST_ID_CONFLICT")
                    validate_receipt(
                        db, prior, self.instance, self.candidate_generation
                    )
                    return unique_document(prior["receipt_json"])
                if self.receipts_used(db, owner["id"]) >= self.receipt_limit:
                    raise GlobalError("REQUEST_RECEIPT_CAPACITY_REACHED")
            stamp = utc(now())
            self.fault("before_state")
            if tool in (
                "request_contact",
                "respond_contact",
                "macro_contact_handshake",
            ):
                result = self.contact(db, tool, owner, args, stamp)
            elif tool == "set_contact_policy":
                db.execute(
                    "UPDATE agents SET contact_policy=? WHERE id=?",
                    (args["policy"], owner["id"]),
                )
                result = {**self.identity(owner), "policy": args["policy"]}
            elif tool in (
                "refresh_registration",
                "verify_window_identity",
                "touch_window_identity",
            ):
                row_id = args.get("window_row_id")
                window_uuid = args.get("window_uuid")
                if (row_id is None) != (window_uuid is None):
                    raise GlobalError("WINDOW_INPUT_REQUIRED")
                window = (
                    self.window(db, owner["id"], row_id, window_uuid)
                    if row_id is not None
                    else None
                )
                if tool == "verify_window_identity":
                    if (
                        window["expires_ts"] is not None
                        and utc(window["expires_ts"]) <= stamp
                    ):
                        raise GlobalError("WINDOW_EXPIRED")
                else:
                    modified = self.touch(db, owner, window, stamp)
                    if tool == "touch_window_identity" and modified:
                        changed(db)
                    if (
                        tool == "refresh_registration"
                        and args["task_description"] is not None
                    ):
                        db.execute(
                            "UPDATE agents SET task_description=? WHERE id=?",
                            (args["task_description"], owner["id"]),
                        )
                owner = db.execute(
                    "SELECT * FROM agents WHERE id=?", (owner["id"],)
                ).fetchone()
                window = (
                    self.window(db, owner["id"], row_id, window_uuid)
                    if row_id is not None
                    else None
                )
                output = (
                    None
                    if window is None
                    else {
                        "window_row_id": row_id,
                        "window_uuid": window_uuid,
                        "last_active_ts": iso(window["last_active_ts"]),
                        "expires_ts": iso(window["expires_ts"]),
                    }
                )
                result = self.identity(owner)
                if tool == "refresh_registration":
                    result.update(
                        program=owner["program"],
                        model=owner["model"],
                        last_active_ts=iso(owner["last_active_ts"]),
                        registration_updated=True,
                        window=output,
                    )
                else:
                    result.update(
                        **output,
                        ready=True,
                        verification=(
                            "server-verified-read-only"
                            if tool == "verify_window_identity"
                            else "server-verified-by-touch"
                        ),
                    )
            elif tool == "resolve_agent_identity":
                row = db.execute(
                    "SELECT id,name,retired_at FROM agents WHERE lookup_key=?",
                    (args["target_name"].lower(),),
                ).fetchone()
                if row is None:
                    raise GlobalError("TARGET_UNAVAILABLE")
                result = {
                    "target": {
                        "agent_id": row["id"],
                        "name": row["name"],
                        "retired_at": iso(row["retired_at"]),
                    }
                }
            elif tool == "list_contacts":
                direction = args["direction"]
                aid = owner["id"]
                clause = {
                    "incoming": "b_agent_id=?",
                    "outgoing": "a_agent_id=?",
                    "both": "(a_agent_id=? OR b_agent_id=?)",
                }[direction]
                params = [aid, aid] if direction == "both" else [aid]
                values = list(
                    db.execute(
                        "SELECT * FROM agent_links WHERE "
                        + clause
                        + " AND id>? ORDER BY id LIMIT ?",
                        (*params, args["after_id"] or 0, args["limit"] + 1),
                    )
                )
                result = {
                    "items": [
                        self.contact_value(db, r) for r in values[: args["limit"]]
                    ],
                    "next_after_id": (
                        values[args["limit"] - 1]["id"]
                        if len(values) > args["limit"]
                        else None
                    ),
                }
            else:
                raise GlobalError("ARGUMENTS_UNSUPPORTED")
            self.fault("after_state")
            if receipt:
                changed(db)
            result.update(
                mutation_revision=revision(db),
                result_as_of="commit" if writes else "read",
                **(
                    {"committed_at": stamp.isoformat()}
                    if writes
                    else {"observed_at": stamp.isoformat()}
                ),
            )
            if receipt:
                result["request_id"] = args["request_id"]
            validate(result, TOOLS[tool]["output_schema"], "RESPONSE_INVALID")
            if receipt:
                db.execute(
                    "INSERT INTO global_operation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.instance,
                        owner["id"],
                        args["request_id"],
                        tool,
                        hashed,
                        raw,
                        self.epoch,
                        self.candidate_generation,
                        owner["credential_generation"],
                        stamp.isoformat(),
                        revision(db),
                        canonical(result),
                        digest(result),
                    ),
                )
                # Inspect exactly the stored new row, including output/preimage
                # and digest, inside this transaction before it can commit.
                inserted = db.execute(
                    "SELECT * FROM global_operation_receipts WHERE server_instance_id=? AND agent_id=? AND request_id=?",
                    (self.instance, owner["id"], args["request_id"]),
                ).fetchone()
                validate_receipt(db, inserted, self.instance, self.candidate_generation)
                self.fault("after_receipt")
            self.fault("before_commit")
        self.fault("after_commit")
        return result

    def authorize_delivery(self, db, sender_id, target_id, stamp=None):
        """Shared S2b primitive; caller owns the same authenticated transaction."""
        stamp = stamp or utc(now())
        self.peer(db, sender_id)
        target = self.peer(db, target_id)
        if sender_id == target_id:
            return True
        link = db.execute(
            "SELECT status FROM agent_links WHERE a_agent_id=? AND b_agent_id=?",
            (sender_id, target_id),
        ).fetchone()
        if (link and link["status"] == "blocked") or target[
            "contact_policy"
        ] == "block_all":
            return False
        if target["contact_policy"] == "open" or (
            link and link["status"] == "approved"
        ):
            return True
        if target["contact_policy"] == "contacts_only":
            return False
        if target["contact_policy"] != "auto":
            raise GlobalError("CONTACT_POLICY_INVALID")
        # Offset-aware Python comparison, not TEXT/MAX timestamps.
        cutoff = stamp - timedelta(seconds=self.auto_ttl)
        for row in db.execute(
            "SELECT m.created_ts FROM messages m JOIN message_recipients r ON r.message_id=m.id WHERE (m.sender_id=? AND r.agent_id=?) OR (m.sender_id=? AND r.agent_id=?)",
            (sender_id, target_id, target_id, sender_id),
        ):
            if utc(row[0]) >= cutoff:
                return True
        return False


class S2aTool(Tool):
    _runtime: S2aRuntime = PrivateAttr()

    async def run(self, arguments):
        result = self._runtime.apply(self.name, arguments)
        return ToolResult(content=result, structured_content=result)


def build_s2a_server(config):
    captured = []

    class Runtime(S2aRuntime):
        def __init__(self, path):
            super().__init__(path)
            captured.append(self)

    server = build_global_server(config, runtime_class=Runtime)
    for name, contract in TOOLS.items():
        tool = S2aTool(
            name=name,
            description="Isolated global S2a capability.",
            parameters=contract["input_schema"],
            output_schema=contract["output_schema"],
        )
        tool._runtime = captured[0]
        server.add_tool(tool)
        server._agentstack_declared_tools.add(name)
        server._agentstack_published_tools.add(name)
    return server
