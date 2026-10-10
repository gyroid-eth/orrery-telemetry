"""Isolated schema4 message runtime; the only external projection is a hint."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from datetime import timedelta
import hashlib
import os
import sqlite3
import time

from fastmcp.tools.tool import Tool, ToolResult
from mcp.types import TextContent
from pydantic import PrivateAttr

from .global_server import GlobalError, document, now
from .global_s2a import (
    S2aRuntime,
    build_s2a_server,
    revision,
    utc,
    iso,
    validate_runtime_candidate,
    validate_receipt as s2a_receipt,
)
from .global_s2a_contract import canonical, digest, unique_document
from .global_s2b_contract import (
    FIXTURE,
    TOOLS,
    arguments,
    attachments,
    preimage,
    preimage_schema,
    validate,
    ensure_budget,
)
from .namespace_store import changed

VISIBLE = "(m.sender_id=? OR EXISTS(SELECT 1 FROM message_recipients vr WHERE vr.message_id=m.id AND vr.agent_id=?))"
# Single SQL predicate used by worker, management and health. The fact itself
# is a tagged union: no invented timestamp and no parallel provenance column.
UNCONSUMED = "(r.notification_fact IS NULL AND r.read_ts IS NULL AND r.ack_ts IS NULL)"
PENDING = "(" + UNCONSUMED + " AND a.retired_at IS NULL)"


def notified_at(fact):
    if fact is None or fact == "imported-consumed":
        return None
    if not isinstance(fact, str) or not fact.startswith("delivered:"):
        raise GlobalError("RUNTIME_CANDIDATE_INVALID")
    return iso(fact[len("delivered:") :])


def dirty(db, mid, aid):
    db.execute(
        "INSERT INTO global_signal_dirty VALUES(?,?,'pending',NULL) ON CONFLICT(message_id,recipient_agent_id) DO UPDATE SET state='pending',last_reason=NULL WHERE state!='pending' OR last_reason IS NOT NULL",
        (mid, aid),
    )


def attachment_descriptors(db, mid):
    return [
        dict(r)
        for r in db.execute(
            "SELECT a.ordinal,a.sha256,b.byte_size,a.filename,a.media_type FROM global_message_attachments a JOIN global_attachment_blobs b USING(sha256) WHERE a.message_id=? ORDER BY ordinal",
            (mid,),
        )
    ]


def message_digest(db, row):
    return digest(
        {
            "id": row["id"],
            "sender_id": row["sender_id"],
            "subject": row["subject"],
            "body_sha256": hashlib.sha256(row["body_md"].encode()).hexdigest(),
            "body_byte_size": len(row["body_md"].encode()),
            "importance": row["importance"],
            "ack_required": bool(row["ack_required"]),
            "topic": row["topic"],
            "reply_to": row["reply_to"],
            "created_ts": iso(row["created_ts"]),
            "recipients": [
                list(r)
                for r in db.execute(
                    "SELECT agent_id,kind FROM message_recipients WHERE message_id=? ORDER BY kind,agent_id",
                    (row["id"],),
                )
            ],
            "attachments": attachment_descriptors(db, row["id"]),
        }
    )


def validate_receipt(
    db, row, instance, candidate, *, contracts=TOOLS, request_schema=preimage_schema
):
    if row["tool"] not in contracts:
        return s2a_receipt(db, row, instance, candidate)
    try:
        req = unique_document(row["canonical_request_json"])
        out = unique_document(row["receipt_json"])
        if contracts[row["tool"]]["operation"] != "write":
            raise ValueError
        validate(
            req["arguments"], request_schema(row["tool"]), "REQUEST_RECEIPT_INVALID"
        )
        if set(req) != {
            "version",
            "tool",
            "server_instance_id",
            "candidate_generation",
            "authority_epoch",
            "agent_id",
            "expected_credential_generation",
            "arguments",
        }:
            raise ValueError
        if (
            req["version"] != "orrery-global-operation-request-v1"
            or req["tool"] != row["tool"]
        ):
            raise ValueError
        for k, field in [
            ("server_instance_id", "server_instance_id"),
            ("candidate_generation", "candidate_generation"),
            ("authority_epoch", "authority_epoch"),
            ("agent_id", "agent_id"),
            ("expected_credential_generation", "credential_generation"),
        ]:
            if req[k] != row[field] or (
                field in contracts[row["tool"]]["output_schema"]["properties"]
                and out[field] != row[field]
            ):
                raise ValueError
        if (
            row["server_instance_id"] != instance
            or row["candidate_generation"] != candidate
        ):
            raise ValueError
        if (
            canonical(req) != row["canonical_request_json"]
            or hashlib.sha256(canonical(req).encode()).hexdigest()
            != row["request_hash"]
        ):
            raise ValueError
        if (
            canonical(out) != row["receipt_json"]
            or digest(out) != row["receipt_sha256"]
        ):
            raise ValueError
        if (
            out["request_id"] != row["request_id"]
            or out["committed_at"] != row["committed_at"]
            or out["mutation_revision"] != row["mutation_revision"]
            or not 1 <= row["mutation_revision"] <= revision(db)
        ):
            raise ValueError
        validate(
            out, contracts[row["tool"]]["output_schema"], "REQUEST_RECEIPT_INVALID"
        )
        if row["tool"] in ("send_message", "reply_message"):
            m = db.execute(
                "SELECT * FROM messages WHERE id=?", (out["message_id"],)
            ).fetchone()
            if m:
                if message_digest(db, m) != out["message_sha256"]:
                    raise ValueError
                if (
                    hashlib.sha256(m["body_md"].encode()).hexdigest()
                    != req["arguments"]["body_utf8_sha256"]
                ):
                    raise ValueError
                if (
                    len(m["body_md"].encode())
                    != req["arguments"]["body_utf8_byte_size"]
                ):
                    raise ValueError
                actual = [
                    {k: v for k, v in a.items() if k != "ordinal"}
                    for a in attachment_descriptors(db, m["id"])
                ]
                if actual != req["arguments"]["attachments"]:
                    raise ValueError
    except (KeyError, TypeError, ValueError, GlobalError):
        raise GlobalError("REQUEST_RECEIPT_INVALID") from None


class S2bRuntime(S2aRuntime):
    config_kind = "orrery-global-server-s2b-v1"
    profile = "s2b"
    receipt_validator = staticmethod(validate_receipt)
    schema_version = 4
    tools = S2aRuntime.tools | TOOLS.keys()
    health_fields = {
        "capabilities": sorted(
            S2aRuntime.health_fields["capabilities"]
            + list(FIXTURE["capabilities_added"])
        ),
        "runtime_profile": "s2b-v1",
    }

    def __init__(self, config):
        self.worker_task = None
        super().__init__(config)
        self.attachment_limit = self.config.get(
            "attachment_owner_byte_limit", 268435456
        )
        if type(self.attachment_limit) is not int or self.attachment_limit <= 0:
            raise GlobalError("ISOLATED_CONFIG_REQUIRED")

    def initial_preflight(self):
        path = self.runtime_root.parent / "preparation-receipt.json"
        if not path.exists() and not path.is_symlink():
            raise GlobalError("CANDIDATE_SCHEMA_REQUIRES_REPREPARE")
        receipt = document(path)
        if (
            receipt.get("kind") != "orrery-" + self.profile + "-preparation-receipt-v1"
            or receipt.get("phase") != "complete"
            or any(
                receipt.get(k) != self.config.get(v)
                for k, v in [
                    ("server_instance_id", "mail_instance_id"),
                    ("candidate_generation", "candidate_generation"),
                    ("authority_epoch", "authority_epoch"),
                ]
            )
        ):
            raise GlobalError("PREPARATION_INCOMPLETE")
        if (
            self.runtime_root.parent.parent
            != self.root / (self.profile + "-candidates")
            or self.runtime_root.name != "candidate"
            or self.paths["database"] != self.runtime_root / "mail.sqlite3"
        ):
            raise GlobalError("ISOLATED_CONFIG_REQUIRED")

    def validate_candidate(self, db, **options):
        validate_runtime_candidate(
            db,
            schema_version=self.schema_version,
            profile=self.profile + "-v1",
            receipt_validator=self.receipt_validator,
            **options,
        )
        if options.get("full"):
            for row in db.execute("SELECT notification_fact FROM message_recipients"):
                notified_at(row[0])
            seq = db.execute(
                "SELECT value FROM namespace_metadata WHERE key='message_id_high_water'"
            ).fetchone()
            if (
                not seq
                or not seq[0].isascii()
                or not seq[0].isdecimal()
                or not db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM messages"
                ).fetchone()[0]
                <= int(seq[0])
                <= 2**63 - 1
            ):
                raise GlobalError("RUNTIME_CANDIDATE_INVALID")
            for row in db.execute("SELECT * FROM global_attachment_blobs"):
                if (
                    hashlib.sha256(row["content"]).hexdigest() != row["sha256"]
                    or len(row["content"]) != row["byte_size"]
                ):
                    raise GlobalError("RUNTIME_CANDIDATE_INVALID")

    def validate_receipt(self, db, row):
        validate_receipt(db, row, self.instance, self.candidate_generation)

    def visible(self, db, mid, aid):
        return db.execute(
            "SELECT m.* FROM messages m WHERE m.id=? AND " + VISIBLE, (mid, aid, aid)
        ).fetchone()

    def attachment_usage(self, db, aid):
        return db.execute(
            "SELECT COALESCE(sum(byte_size),0) FROM global_attachment_blobs WHERE sha256 IN (SELECT DISTINCT x.sha256 FROM global_message_attachments x JOIN messages m ON m.id=x.message_id WHERE m.sender_id=?)",
            (aid,),
        ).fetchone()[0]

    def projection(self, db, row, aid, bodies=False):
        mid = row["id"]
        sender = db.execute(
            "SELECT id,name FROM agents WHERE id=?", (row["sender_id"],)
        ).fetchone()
        result = {
            "id": mid,
            "sender": {"agent_id": sender["id"], "name": sender["name"]},
            "to": [],
            "cc": [],
            "bcc": [],
            "subject": row["subject"],
            "body_md": row["body_md"] if bodies else None,
            "importance": row["importance"],
            "ack_required": bool(row["ack_required"]),
            "topic": row["topic"],
            "reply_to": row["reply_to"],
            "legacy_thread_label": row["legacy_thread_label"],
            "created_ts": iso(row["created_ts"]),
            "attachments": [],
            "read_at": None,
            "acknowledged_at": None,
            "history_incomplete": False,
        }
        for r in db.execute(
            "SELECT r.*,a.name FROM message_recipients r JOIN agents a ON a.id=r.agent_id WHERE r.message_id=? ORDER BY r.agent_id",
            (mid,),
        ):
            if r["kind"] != "bcc" or aid == row["sender_id"] or aid == r["agent_id"]:
                result[r["kind"]].append({"agent_id": r["agent_id"], "name": r["name"]})
            if aid == r["agent_id"]:
                result.update(
                    read_at=iso(r["read_ts"]), acknowledged_at=iso(r["ack_ts"])
                )
        if row["reply_to"] is not None and not self.visible(db, row["reply_to"], aid):
            result.update(reply_to=None, history_incomplete=True)
        for a in attachment_descriptors(db, mid):
            a.pop("ordinal")
            a["content_ref"] = {"kind": "db-blob", "sha256": a["sha256"]}
            result["attachments"].append(a)
        return result

    def publish(self, db, tool, owner, args, stamp):
        parent_id = (
            args.get("reply_to") if tool == "send_message" else args["message_id"]
        )
        parent = None
        if parent_id is not None:
            parent = self.visible(db, parent_id, owner["id"])
            if parent is None:
                raise GlobalError("MESSAGE_UNAVAILABLE")
        recipients = {}
        for kind in ("to", "cc", "bcc"):
            ids = args[kind + "_agent_ids"]
            if ids is None:
                ids = [parent["sender_id"]] if kind == "to" and parent else []
            for aid in ids:
                recipients.setdefault(aid, kind)
        if not recipients:
            raise GlobalError("RECIPIENTS_EMPTY")
        if len(recipients) > 100:
            raise GlobalError("PAYLOAD_TOO_LARGE")
        for aid in recipients:
            if not self.authorize_delivery(db, owner["id"], aid, stamp):
                raise GlobalError("CONTACT_BLOCKED")
        files = attachments(args["attachments"])
        owned = {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT x.sha256 FROM global_message_attachments x JOIN messages m ON m.id=x.message_id WHERE m.sender_id=?",
                (owner["id"],),
            )
        }
        added = {f["sha256"]: f["byte_size"] for f in files if f["sha256"] not in owned}
        if (
            added
            and self.attachment_usage(db, owner["id"]) + sum(added.values())
            > self.attachment_limit
        ):
            raise GlobalError("ATTACHMENT_OWNER_CAPACITY_REACHED")
        seq = db.execute(
            "SELECT value FROM namespace_metadata WHERE key='message_id_high_water'"
        ).fetchone()
        if not seq:
            raise GlobalError("RUNTIME_CANDIDATE_INVALID")
        mid = int(seq[0]) + 1
        if mid > 2**63 - 1:
            raise GlobalError("PAYLOAD_TOO_LARGE")
        db.execute(
            "UPDATE namespace_metadata SET value=? WHERE key='message_id_high_water'",
            (str(mid),),
        )
        subject = args.get("subject")
        if tool == "reply_message":
            prefix = args["subject_prefix"].strip()
            base = parent["subject"]
            subject = (
                base
                if prefix and base.lower().startswith(prefix.lower())
                else f"{prefix} {base}".strip()
            )
        db.execute(
            "INSERT INTO messages(id,sender_id,subject,body_md,importance,ack_required,topic,created_ts,reply_to) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                mid,
                owner["id"],
                subject,
                args["body_md"],
                args.get("importance", parent["importance"] if parent else "normal"),
                args.get("ack_required", parent["ack_required"] if parent else False),
                args.get("topic", parent["topic"] if parent else None),
                stamp.isoformat(),
                parent_id,
            ),
        )
        for aid, kind in recipients.items():
            db.execute(
                "INSERT INTO message_recipients(message_id,agent_id,kind) VALUES(?,?,?)",
                (mid, aid, kind),
            )
            dirty(db, mid, aid)
        for ordinal, f in enumerate(files):
            db.execute(
                "INSERT OR IGNORE INTO global_attachment_blobs VALUES(?,?,?)",
                (f["sha256"], f["byte_size"], f["content"]),
            )
            stored = db.execute(
                "SELECT content FROM global_attachment_blobs WHERE sha256=?",
                (f["sha256"],),
            ).fetchone()[0]
            if stored != f["content"]:
                raise GlobalError("RUNTIME_CANDIDATE_INVALID")
            db.execute(
                "INSERT INTO global_message_attachments VALUES(?,?,?,?,?)",
                (mid, ordinal, f["sha256"], f["filename"], f["media_type"]),
            )
        row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return {
            "message_id": mid,
            "reply_to": parent_id,
            "message_sha256": message_digest(db, row),
            "recipient_agent_ids": sorted(recipients),
            "durability": "db-committed",
            "outputs_as_of_commit": "pending",
        }

    mutation_contracts = TOOLS
    mutation_arguments = staticmethod(arguments)
    mutation_preimage = staticmethod(preimage)

    def mutate_domain(self, db, tool, owner, args, stamp):
        if tool in ("send_message", "reply_message"):
            return self.publish(db, tool, owner, args, stamp)
        r = db.execute(
            "SELECT * FROM message_recipients WHERE message_id=? AND agent_id=?",
            (args["message_id"], owner["id"]),
        ).fetchone()
        if r is None:
            raise GlobalError("MESSAGE_UNAVAILABLE")
        read = iso(r["read_ts"]) or stamp.isoformat()
        ack = iso(r["ack_ts"]) or stamp.isoformat()
        db.execute(
            "UPDATE message_recipients SET read_ts=?"
            + (",ack_ts=?" if tool == "acknowledge_message" else "")
            + " WHERE message_id=? AND agent_id=?",
            (
                (read, ack, args["message_id"], owner["id"])
                if tool == "acknowledge_message"
                else (read, args["message_id"], owner["id"])
            ),
        )
        dirty(db, args["message_id"], owner["id"])
        result = {"message_id": args["message_id"], "read_at": read}
        if tool == "mark_message_read":
            result["read"] = True
        else:
            result.update(acknowledged=True, acknowledged_at=ack)
        return result

    def apply(self, tool, supplied):
        if tool not in self.mutation_contracts:
            return super().apply(tool, supplied)
        args = self.mutation_arguments(tool, supplied)
        binding = tuple(
            args[k]
            for k in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        )
        write = self.mutation_contracts[tool]["operation"] == "write"
        with self.transaction(write=write, binding=binding) as db:
            owner = self.owner(db, args["agent_id"], args["registration_token"])
            if owner["credential_generation"] != args["expected_credential_generation"]:
                raise GlobalError("CREDENTIAL_GENERATION_MISMATCH")
            if not write:
                return self.query(db, tool, owner, args)
            raw, hashed = self.mutation_preimage(tool, args)
            old = db.execute(
                "SELECT * FROM global_operation_receipts WHERE server_instance_id=? AND agent_id=? AND request_id=?",
                (self.instance, owner["id"], args["request_id"]),
            ).fetchone()
            if old:
                if old["request_hash"] != hashed:
                    raise GlobalError("REQUEST_ID_CONFLICT")
                self.validate_receipt(db, old)
                return unique_document(old["receipt_json"])
            if self.receipts_used(db, owner["id"]) >= self.receipt_limit:
                raise GlobalError("REQUEST_RECEIPT_CAPACITY_REACHED")
            stamp = utc(now())
            self.fault("before_state")
            result = self.mutate_domain(db, tool, owner, args, stamp)
            self.fault("after_state")
            changed(db)
            result.update(
                **{
                    k: v
                    for k, v in self.identity(owner).items()
                    if k
                    in (
                        "agent_id",
                        "credential_generation",
                        "server_instance_id",
                        "candidate_generation",
                        "authority_epoch",
                    )
                    and k
                    in self.mutation_contracts[tool]["output_schema"]["properties"]
                },
                request_id=args["request_id"],
                mutation_revision=revision(db),
                result_as_of="commit",
                committed_at=stamp.isoformat(),
            )
            ensure_budget(result)
            validate(
                result,
                self.mutation_contracts[tool]["output_schema"],
                "RESPONSE_INVALID",
            )
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
            row = db.execute(
                "SELECT * FROM global_operation_receipts WHERE server_instance_id=? AND agent_id=? AND request_id=?",
                (self.instance, owner["id"], args["request_id"]),
            ).fetchone()
            self.validate_receipt(db, row)
            self.fault("after_receipt")
            self.fault("before_commit")
        self.fault("after_commit")
        return result

    def purge(self, message_ids, *, dry_run=False):
        """Operator/internal deletion with the shared ancestor-hold contract."""
        with self.transaction(write=not dry_run) as db:
            return self.purge_into(db, message_ids, dry_run=dry_run)

    def purge_into(self, db, message_ids, *, dry_run=False):
        from .namespace_store import reply_catalog

        plan = reply_catalog(db).plan_purge(message_ids)
        if not dry_run:
            db.execute("PRAGMA defer_foreign_keys=ON")
            for mid in plan.delete_ids:
                for row in db.execute(
                    "SELECT agent_id FROM message_recipients WHERE message_id=?",
                    (mid,),
                ).fetchall():
                    dirty(db, mid, row[0])
                db.execute(
                    "DELETE FROM global_message_attachments WHERE message_id=?",
                    (mid,),
                )
                db.execute("DELETE FROM message_recipients WHERE message_id=?", (mid,))
                db.execute("DELETE FROM messages WHERE id=?", (mid,))
            if plan.delete_ids:
                db.execute(
                    "DELETE FROM global_attachment_blobs WHERE sha256 NOT IN (SELECT sha256 FROM global_message_attachments)"
                )
                changed(db)
        return {
            "delete_ids": list(plan.delete_ids),
            "held_ids": list(plan.held_ids),
            "dry_run": dry_run,
        }

    def cursor(self, tool, owner, args):
        query = {
            k: v
            for k, v in args.items()
            if k
            not in {
                "cursor",
                "registration_token",
                "project_key",
                "agent_name",
                "format",
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
                "expected_credential_generation",
                "limit",
                "per_thread_limit",
            }
        }
        qhash = digest(query)
        if args.get("cursor") is None:
            return None, qhash
        try:
            token = args["cursor"]
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            value = unique_document(raw)
            validate(value, FIXTURE["keyset_cursor_schema"], "CURSOR_INVALID")
            if (
                value["agent_id"] != owner["id"]
                or value["tool"] != tool
                or value["query_sha256"] != qhash
            ):
                raise ValueError
            if (
                not 0 <= value["max_message_id"] <= 2**63 - 1
                or not 1 <= value["before_id"] <= value["max_message_id"]
            ):
                raise ValueError
            return value, qhash
        except (ValueError, GlobalError, KeyError):
            raise GlobalError("CURSOR_INVALID") from None

    def query(self, db, tool, owner, args):
        aid = owner["id"]
        saved, qhash = self.cursor(tool, owner, args)
        maximum = (
            saved["max_message_id"]
            if saved
            else db.execute(
                "SELECT COALESCE(MAX(m.id),0) FROM messages m WHERE " + VISIBLE,
                (aid, aid),
            ).fetchone()[0]
        )
        before = saved["before_id"] if saved else None
        cutoff = saved["since_cutoff"] if saved else None
        clause = VISIBLE + " AND m.id<=?"
        params = [aid, aid, maximum]
        if before is not None:
            clause += " AND m.id<?"
            params.append(before)
        if tool == "search_messages":
            if args["query"]:
                clause += " AND m.id IN (SELECT message_id FROM fts_messages WHERE fts_messages MATCH ?)"
                params.append(args["query"])
        elif tool == "fetch_topic":
            clause += " AND m.topic=?"
            params.append(args["topic_name"])
            cutoff = args["since_ts"]
        elif tool == "fetch_summary":
            if cutoff is None:
                cutoff = (utc(now()) - timedelta(hours=args["since_hours"])).isoformat()
        else:
            roots = []
            labels = []
            if args["message_id"] is not None:
                roots = [args["message_id"]]
            else:
                for part in args["thread_id"].split(","):
                    part = part.strip()
                    if part.isascii() and part.isdecimal():
                        if not 1 <= int(part) <= 2**63 - 1:
                            raise GlobalError("QUERY_INVALID")
                        roots.append(int(part))
                    else:
                        labels.append(part)
            terms = []
            rparams = []
            if roots:
                terms.append("v.id IN (" + ",".join("?" for _ in roots) + ")")
                rparams += roots
            if labels:
                terms.append(
                    "v.legacy_thread_label IN (" + ",".join("?" for _ in labels) + ")"
                )
                rparams += labels
            if not terms:
                raise GlobalError("QUERY_INVALID")
            # Only visible vertices participate. UNION provides cycle safety;
            # no persistent visited state or second conversation namespace.
            graph = (
                "WITH RECURSIVE v AS (SELECT m.id,m.reply_to FROM messages m WHERE "
                + VISIBLE
                + " AND m.id<=?), g(id) AS (SELECT v.id FROM v JOIN messages lm ON lm.id=v.id WHERE "
                + (
                    " OR ".join(
                        t.replace("v.legacy_thread_label", "lm.legacy_thread_label")
                        for t in terms
                    )
                )
                + " UNION SELECT v.id FROM v JOIN g ON v.reply_to=g.id OR v.id=(SELECT p.reply_to FROM v p WHERE p.id=g.id)) SELECT id FROM g"
            )
            clause += " AND m.id IN (" + graph + ")"
            params += [aid, aid, maximum, *rparams]
        if cutoff is not None:
            utc(cutoff)
            clause += " AND julianday(m.created_ts)>julianday(?)"
            params.append(cutoff)
        limit = args.get("limit", args.get("per_thread_limit", 20))
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        try:
            rows = list(
                db.execute(
                    "SELECT m.* FROM messages m WHERE "
                    + clause
                    + " ORDER BY m.id DESC LIMIT ?",
                    (*params, limit + 1),
                )
            )
        except sqlite3.OperationalError as exc:
            raise GlobalError(
                "QUERY_WORK_LIMIT_EXCEEDED"
                if "interrupt" in str(exc)
                else "QUERY_INVALID"
            ) from None
        finally:
            db.set_progress_handler(None, 0)
        selected = []
        result = None
        attachment = None
        if tool == "search_messages" and args["attachment_sha256"] is not None:
            blob = db.execute(
                "SELECT b.* FROM global_attachment_blobs b WHERE b.sha256=? AND EXISTS(SELECT 1 FROM global_message_attachments x JOIN messages m ON m.id=x.message_id WHERE x.sha256=b.sha256 AND "
                + VISIBLE
                + ")",
                (args["attachment_sha256"], aid, aid),
            ).fetchone()
            if blob is None:
                raise GlobalError("ATTACHMENT_UNAVAILABLE")
            attachment = {
                "sha256": blob["sha256"],
                "byte_size": blob["byte_size"],
                "content_base64": base64.b64encode(blob["content"]).decode(),
            }

        def envelope(chosen, more):
            nxt = None
            if more and chosen:
                value = {
                    "version": 1,
                    "tool": tool,
                    "agent_id": aid,
                    "query_sha256": qhash,
                    "max_message_id": maximum,
                    "before_id": chosen[-1]["id"],
                    "since_cutoff": cutoff,
                }
                nxt = (
                    base64.urlsafe_b64encode(canonical(value).encode())
                    .decode()
                    .rstrip("=")
                )
            projections = [
                self.projection(db, r, aid, args.get("include_bodies", False))
                for r in chosen
            ]
            if tool in ("fetch_summary", "summarize_thread"):
                groups = {}
                for r, p in zip(chosen, projections):
                    key = (
                        (r["legacy_thread_label"], r["legacy_thread_source"])
                        if r["legacy_thread_label"] is not None
                        else (None, None)
                    )
                    groups.setdefault(key, []).append((r, p))
                items = []
                for (label, source), group in groups.items():
                    people = {p["sender"]["agent_id"]: p["sender"] for r, p in group}
                    for r, p in group:
                        for kind in ("to", "cc", "bcc"):
                            for person in p[kind]:
                                people[person["agent_id"]] = person
                    items.append(
                        {
                            "origin": {
                                "kind": "legacy-label"
                                if label
                                else (
                                    "conversation"
                                    if tool == "summarize_thread"
                                    else "recent"
                                ),
                                "message_id": args.get("message_id"),
                                "legacy_label": label,
                                "source_provenance": source,
                            },
                            "kind": "visible-message-digest-v1",
                            "source_message_ids": [r["id"] for r, p in group],
                            "participants": list(people.values()),
                            "key_points": [r["subject"][:2048] for r, p in group],
                            "action_items": [],
                            "examples": [p for r, p in group]
                            if args.get("include_examples")
                            else [],
                            "history_incomplete": more
                            or any(p["history_incomplete"] for r, p in group),
                        }
                    )
            else:
                items = projections
            out = {
                "items": items,
                "next_cursor": nxt,
                "max_message_id": maximum,
                "as_of_revision": revision(db),
                "truncated": more,
            }
            if tool == "search_messages":
                out["attachment"] = attachment
            return out

        for r in rows[:limit]:
            candidate = envelope(selected + [r], len(rows) > len(selected) + 1)
            try:
                ensure_budget(candidate)
            except GlobalError:
                if not selected:
                    raise
                break
            selected.append(r)
        result = envelope(selected, len(rows) > len(selected))
        ensure_budget(result)
        validate(result, TOOLS[tool]["output_schema"], "RESPONSE_INVALID")
        return result

    inbox_extensions = ("before_id", "expected_credential_generation")

    def after_lifecycle(self, db, agent_id):
        for row in db.execute(
            "SELECT r.message_id FROM message_recipients r WHERE r.agent_id=? AND "
            + UNCONSUMED,
            (agent_id,),
        ).fetchall():
            dirty(db, row[0], agent_id)

    def inbox(self, args):
        limit = args["limit"]
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GlobalError("LIMIT_INVALID")
        before = args.get("before_id")
        if before is not None and (
            type(before) is not int or not 1 <= before <= 2**63 - 1
        ):
            raise GlobalError("ARGUMENTS_UNSUPPORTED")
        cutoff = args.get("since_ts")
        if cutoff is not None:
            utc(cutoff)
        binding = tuple(
            args[k]
            for k in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        )
        with self.transaction(binding=binding) as db:
            owner = self.owner(db, args["agent_id"], args["registration_token"])
            if (
                args.get("expected_credential_generation") is not None
                and owner["credential_generation"]
                != args["expected_credential_generation"]
            ):
                raise GlobalError("CREDENTIAL_GENERATION_MISMATCH")
            return self.inbox_snapshot(
                db,
                owner["id"],
                limit=limit,
                before=before,
                cutoff=cutoff,
                urgent_only=args["urgent_only"],
                include_bodies=args["include_bodies"],
            )

    def inbox_snapshot(
        self,
        db,
        aid,
        *,
        limit,
        before=None,
        cutoff=None,
        urgent_only=False,
        include_bodies=False,
    ):
        """Select and project inbox rows inside the caller's transaction."""
        clause = "r.agent_id=?"
        params = [aid]
        if before is not None:
            clause += " AND m.id<?"
            params.append(before)
        if cutoff is not None:
            clause += " AND julianday(m.created_ts)>julianday(?)"
            params.append(cutoff)
        if urgent_only:
            clause += " AND m.importance IN ('high','urgent')"
        rows = db.execute(
            "SELECT m.* FROM messages m JOIN message_recipients r ON r.message_id=m.id WHERE "
            + clause
            + " ORDER BY m.id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return ensure_budget(
            [self.projection(db, r, aid, include_bodies) for r in rows]
        )

    def signal_stats(self, db):
        row = db.execute(
            "SELECT count(*) FROM message_recipients r JOIN messages m ON m.id=r.message_id JOIN agents a ON a.id=r.agent_id WHERE "
            + PENDING
        ).fetchone()
        oldest = db.execute(
            "SELECT m.created_ts FROM message_recipients r JOIN messages m ON m.id=r.message_id JOIN agents a ON a.id=r.agent_id WHERE "
            + PENDING
            + " ORDER BY julianday(m.created_ts),m.id LIMIT 1"
        ).fetchone()
        states = dict(
            db.execute("SELECT state,count(*) FROM global_signal_dirty GROUP BY state")
        )
        stamp = iso(oldest[0]) if oldest else None
        reasons = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT last_reason,count(*) FROM global_signal_dirty WHERE state='blocked' GROUP BY last_reason"
            )
        }
        return {
            "status": "red"
            if states.get("blocked", 0)
            else ("degraded" if states.get("pending", 0) else "ok"),
            "undelivered_count": row[0],
            "oldest_undelivered_at": stamp,
            "oldest_undelivered_age_seconds": max(
                0, (utc(now()) - utc(stamp)).total_seconds()
            )
            if stamp
            else None,
            "pending_pairs": states.get("pending", 0),
            "blocked_pairs": states.get("blocked", 0),
            "blocked_reasons": reasons,
        }

    def health_details(self, db=None):
        if db is not None:
            return {"signal": self.signal_stats(db)}
        with self.transaction() as snapshot:
            return {"signal": self.signal_stats(snapshot)}

    def inspect(self, db, agent_id):
        result = super().inspect(db, agent_id)
        used = self.attachment_usage(db, agent_id)
        result.update(
            signal=self.signal_stats(db),
            attachment_capacity={
                "used": used,
                "limit": self.attachment_limit,
                "over_limit": used > self.attachment_limit,
            },
        )
        return result

    async def start(self):
        await super().start()

        async def run():
            while True:
                try:
                    self.reconcile(batch_limit=100)
                except GlobalError:
                    pass
                await asyncio.sleep(0.1)

        self.worker_task = asyncio.create_task(run())

    async def close(self):
        if self.worker_task:
            self.worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker_task
        await super().close()

    def reconcile(self, **options):
        from .global_signals import reconcile

        return reconcile(self, **options)

    def management(self, request, peer_uid):
        if not isinstance(request, dict) or request.get("operation") not in (
            "delivery_completed",
            "reconcile_outputs",
        ):
            return super().management(request, peer_uid)
        try:
            return self._management_apply(request, peer_uid)
        except GlobalError as exc:
            known = FIXTURE["management_contract"]["error_schema"]["properties"][
                "reason"
            ]["enum"]
            reason = str(exc)
            if reason not in known:
                reason = (
                    "ARGUMENTS_UNSUPPORTED"
                    if reason == "PEER_UID_REQUIRED"
                    else "WRITER_FENCED"
                )
            return {
                "version": 1,
                "ok": False,
                "operation": request["operation"],
                "reason": reason,
            }

    def _management_apply(self, request, peer_uid):
        if not isinstance(request, dict) or request.get("operation") not in (
            "delivery_completed",
            "reconcile_outputs",
        ):
            return super()._management_apply(request, peer_uid)
        if peer_uid != os.getuid():
            raise GlobalError("PEER_UID_REQUIRED")
        operation = request["operation"]
        contract = FIXTURE["management_contract"][operation]
        validate(request, contract["request_schema"])
        ids = [request.get("recipient_agent_id", 1), *request.get("message_ids", [])]
        for pair in request.get("pairs") or []:
            ids.extend((pair["message_id"], pair["recipient_agent_id"]))
        if any(value > 2**63 - 1 for value in ids):
            raise GlobalError("ARGUMENTS_UNSUPPORTED")
        binding = tuple(
            request[k]
            for k in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        )
        if operation == "reconcile_outputs":
            with self.transaction(binding=binding):
                pass
            result = self.reconcile(
                pairs=request.get("pairs"),
                batch_limit=request.get("batch_limit"),
                retry_blocked=True,
            )
        else:
            items = []
            with self.transaction(write=True, binding=binding) as db:
                self.peer(db, request["recipient_agent_id"])
                modified = False
                for mid in request["message_ids"]:
                    m = db.execute(
                        "SELECT id FROM messages WHERE id=?", (mid,)
                    ).fetchone()
                    r = db.execute(
                        "SELECT * FROM message_recipients WHERE message_id=? AND agent_id=?",
                        (mid, request["recipient_agent_id"]),
                    ).fetchone()
                    if m and not r:
                        raise GlobalError("DELIVERY_RECIPIENT_MISMATCH")
                    if r:
                        fact = r["notification_fact"]
                        stamp = notified_at(fact)
                        if fact is None:
                            stamp = now()
                            db.execute(
                                "UPDATE message_recipients SET notification_fact=? WHERE message_id=? AND agent_id=?",
                                (
                                    "delivered:" + stamp,
                                    mid,
                                    request["recipient_agent_id"],
                                ),
                            )
                            modified = True
                        state = "recorded" if fact is None else "already-completed"
                    else:
                        state = "unknown"
                        stamp = None
                    before = db.total_changes
                    dirty(db, mid, request["recipient_agent_id"])
                    modified |= db.total_changes > before
                    items.append(
                        {"message_id": mid, "state": state, "notified_at": stamp}
                    )
                if modified:
                    changed(db)
                result = {"items": items, "mutation_revision": revision(db)}
        result.update(
            version=1,
            ok=True,
            operation=operation,
            server_instance_id=self.instance,
            candidate_generation=self.candidate_generation,
            authority_epoch=self.epoch,
        )
        validate(result, contract["response_schema"], "RESPONSE_INVALID")
        return result


class S2bTool(Tool):
    _runtime: S2bRuntime = PrivateAttr()

    async def run(self, arguments):
        result = self._runtime.apply(self.name, arguments)
        return ToolResult(
            content=[TextContent(type="text", text=canonical(result))],
            structured_content=result,
        )


def build_s2b_server(config, *, runtime_class=S2bRuntime):
    captured = []

    class Runtime(runtime_class):
        def __init__(self, path):
            super().__init__(path)
            captured.append(self)

    server = build_s2a_server(config, runtime_class=Runtime)

    for name, contract in TOOLS.items():
        tool = S2bTool(
            name=name,
            description="Isolated global message capability.",
            parameters=contract["input_schema"],
            output_schema=contract["output_schema"],
        )
        tool._runtime = captured[0]
        server.add_tool(tool)
        server._agentstack_declared_tools.add(name)
        server._agentstack_published_tools.add(name)
    return server
