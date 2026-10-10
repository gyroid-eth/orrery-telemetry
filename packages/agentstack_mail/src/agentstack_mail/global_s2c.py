"""Isolated schema5: SQL reservations and atomic contact welcome messages."""

import base64
import hashlib
import os

from .global_server import GlobalError, now
from .global_s2a import revision, utc
from .global_s2a_contract import canonical, digest, unique_document
from .global_s2b import (
    S2bRuntime,
    S2bTool,
    build_s2b_server,
    validate_receipt as base_receipt,
)
from .global_s2b_contract import (
    TOOLS as MESSAGE_TOOLS,
    arguments as message_arguments,
    preimage as message_preimage,
    ensure_budget,
)
from .global_s2c_contract import (
    FIXTURE,
    TOOLS,
    arguments,
    preimage,
    preimage_schema,
    validate,
)
from . import global_reservations as leases


def validate_receipt(db, row, instance, candidate):
    if row["tool"] not in TOOLS:
        return base_receipt(db, row, instance, candidate)
    base_receipt(
        db, row, instance, candidate, contracts=TOOLS, request_schema=preimage_schema
    )
    req = unique_document(row["canonical_request_json"])["arguments"]
    out = unique_document(row["receipt_json"])
    if row["tool"] == "macro_contact_handshake":
        contact = out["contact"]
        welcome = out["welcome"]
        if (
            contact["from_agent_id"] != row["agent_id"]
            or contact["to_agent_id"] != req["to_agent_id"]
        ):
            raise GlobalError("REQUEST_RECEIPT_INVALID")
        if welcome is not None:
            if (
                welcome["body_sha256"] != req["welcome_body_sha256"]
                or welcome["subject"] != req["welcome_subject"]
                or welcome["recipient_agent_id"] != req["to_agent_id"]
                or out["stage"] != "welcome-sent"
                or out["notification"] != "signal-pending"
            ):
                raise GlobalError("REQUEST_RECEIPT_INVALID")
            message = db.execute(
                "SELECT * FROM messages WHERE id=?", (welcome["message_id"],)
            ).fetchone()
            if message is not None and (
                message["subject"] != welcome["subject"]
                or hashlib.sha256(message["body_md"].encode()).hexdigest()
                != welcome["body_sha256"]
                or len(message["body_md"].encode()) != req["welcome_body_byte_size"]
                or message["sender_id"] != row["agent_id"]
                or [
                    tuple(r)
                    for r in db.execute(
                        "SELECT agent_id,kind FROM message_recipients WHERE message_id=?",
                        (message["id"],),
                    )
                ]
                != [(welcome["recipient_agent_id"], "to")]
            ):
                raise GlobalError("REQUEST_RECEIPT_INVALID")
        elif out["stage"] != "request-only" or out["notification"] != "not-emitted":
            raise GlobalError("REQUEST_RECEIPT_INVALID")
    else:
        for key in ("granted", "renewed", "released", "file_reservations"):
            for lease in out.get(key, []):
                if lease["owner_agent_id"] != row["agent_id"]:
                    raise GlobalError("REQUEST_RECEIPT_INVALID")
                current = db.execute(
                    "SELECT * FROM file_reservations WHERE id=?", (lease["id"],)
                ).fetchone()
                if (
                    current is None
                    or any(
                        current[k] != lease[k]
                        for k in ("path_pattern", "reason", "created_ts")
                    )
                    or current["agent_id"] != lease["owner_agent_id"]
                ):
                    raise GlobalError("REQUEST_RECEIPT_INVALID")


class S2cRuntime(S2bRuntime):
    config_kind = "orrery-global-server-s2c-v1"
    profile = "s2c"
    schema_version = 5
    receipt_validator = staticmethod(validate_receipt)
    tools = S2bRuntime.tools | TOOLS.keys()
    health_fields = {
        "capabilities": sorted(
            S2bRuntime.health_fields["capabilities"]
            + list(FIXTURE["capabilities_added"])
        ),
        "runtime_profile": "s2c-v1",
    }
    mutation_contracts = MESSAGE_TOOLS | TOOLS
    mutation_arguments = staticmethod(
        lambda tool, args: (
            arguments(tool, args) if tool in TOOLS else message_arguments(tool, args)
        )
    )
    mutation_preimage = staticmethod(
        lambda tool, args: (
            preimage(tool, args) if tool in TOOLS else message_preimage(tool, args)
        )
    )

    def validate_candidate(self, db, **options):
        super().validate_candidate(db, **options)
        if options.get("full"):
            for row in db.execute(
                "SELECT created_ts,expires_ts,released_ts FROM file_reservations"
            ):
                for value in row:
                    if value is not None and leases.lease_timestamp(value) != value:
                        raise GlobalError("RUNTIME_CANDIDATE_INVALID")

    def validate_receipt(self, db, row):
        validate_receipt(db, row, self.instance, self.candidate_generation)

    def after_lifecycle(self, db, agent_id):
        super().after_lifecycle(db, agent_id)
        owner = db.execute(
            "SELECT retired_at FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        if owner[0] is not None:
            stamp = leases.lease_timestamp(now())
            db.execute(
                "UPDATE file_reservations SET released_ts=:now_utc,release_cause='retired',revision=revision+1 WHERE agent_id=:owner AND "
                + leases.ACTIVE,
                {"now_utc": stamp, "owner": agent_id},
            )

    def mutate_domain(self, db, tool, owner, args, stamp):
        if tool not in TOOLS:
            return super().mutate_domain(db, tool, owner, args, stamp)
        aid = owner["id"]
        if tool == "macro_contact_handshake":
            if args["auto_accept"]:
                raise GlobalError("CONTACT_TARGET_CONSENT_REQUIRED")
            target = self.peer(db, args["to_agent_id"])
            link = db.execute(
                "SELECT status FROM agent_links WHERE a_agent_id=? AND b_agent_id=?",
                (aid, target["id"]),
            ).fetchone()
            if self.contact_is_blocked(target, link):
                raise GlobalError("CONTACT_BLOCKED")
            contact = self.contact(db, "request_contact", owner, args, stamp)
            contact.pop("notification", None)
            if contact["status"] == "blocked":
                raise GlobalError("CONTACT_BLOCKED")
            approved = contact["effective"]
            result = dict(
                contact=contact,
                stage="request-only",
                notification="not-emitted",
                approval_required=not approved,
                approval_required_from_agent_id=None
                if approved
                else args["to_agent_id"],
                welcome=None,
            )
            if approved and args["welcome_subject"] is not None:
                data = {
                    "subject": args["welcome_subject"],
                    "body_md": args["welcome_body"],
                    "to_agent_ids": [args["to_agent_id"]],
                    "cc_agent_ids": [],
                    "bcc_agent_ids": [],
                    "attachments": [],
                }
                message = self.publish(db, "send_message", owner, data, stamp)
                result.update(
                    stage="welcome-sent",
                    notification="signal-pending",
                    welcome={
                        "message_id": message["message_id"],
                        "recipient_agent_id": args["to_agent_id"],
                        "subject": args["welcome_subject"],
                        "body_sha256": hashlib.sha256(
                            args["welcome_body"].encode()
                        ).hexdigest(),
                    },
                )
            return result
        if tool in ("renew_file_reservations", "release_file_reservations"):
            rows = leases.selected(db, aid, args, stamp)
            if tool == "renew_file_reservations":
                return {
                    "renewed": [
                        leases.projection(r)
                        for r in leases.renew(db, rows, stamp, args["extend_seconds"])
                    ]
                }
            already = [r["id"] for r in rows if r["released_ts"] is not None]
            return {
                "released": [
                    leases.projection(r)
                    for r in leases.release(db, rows, stamp)
                    if r["id"] not in already
                ],
                "already_released_ids": already,
            }
        session = tool == "macro_start_session"
        rows, new_ids = leases.acquire(
            db,
            aid,
            args.get("file_reservation_paths") or [] if session else args["paths"],
            stamp,
            ttl_seconds=args["file_reservation_ttl_seconds"]
            if session
            else args["ttl_seconds"],
            reason=args["file_reservation_reason"] if session else args["reason"],
            exclusive=True if session else args["exclusive"],
        )
        if session:
            self.touch(db, owner, None, stamp)
            fresh = db.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
            agent = self.identity(fresh)
            agent.update(
                id=aid,
                program=fresh["program"],
                model=fresh["model"],
                retired_at=None,
                last_active_ts=fresh["last_active_ts"],
            )
            inbox = self.inbox_snapshot(db, aid, limit=args["inbox_limit"])
            return {
                "agent": agent,
                "file_reservations": [leases.projection(r) for r in rows],
                "inbox": inbox,
            }
        released_ids = []
        if tool == "macro_file_reservation_cycle" and args["auto_release"]:
            released_ids = new_ids
            leases.release(db, [r for r in rows if r["id"] in new_ids], stamp)
            rows = [
                db.execute(
                    "SELECT * FROM file_reservations WHERE id=?", (r["id"],)
                ).fetchone()
                for r in rows
            ]
        result = {"granted": [leases.projection(r) for r in rows]}
        if tool == "macro_file_reservation_cycle":
            result["released_ids"] = released_ids
        return result

    def query(self, db, tool, owner, args):
        if tool not in TOOLS:
            return super().query(db, tool, owner, args)
        stamp = leases.lease_timestamp(now())
        result = {
            k: v
            for k, v in self.identity(owner).items()
            if k in TOOLS[tool]["output_schema"]["properties"]
        }
        result["observed_revision"] = revision(db)
        if tool == "check_file_reservations":
            paths = leases.normalize(args["paths"])
            if any(p.is_glob for p in paths):
                raise GlobalError("CHECK_REQUIRES_CONCRETE_PATH")
            rows = leases.active_rows(db, stamp, owner["id"])
            items = []
            for path in paths:
                witness = None
                unknown = False
                for row in rows:
                    try:
                        covered = leases.overlap(path, leases.scope(row))
                    except GlobalError as exc:
                        if str(exc) != "ACTIVE_LEASE_RULES_UNKNOWN":
                            raise
                        unknown = True
                        continue
                    if covered:
                        witness = row["id"]
                        break
                if witness is None and unknown:
                    raise GlobalError("ACTIVE_LEASE_RULES_UNKNOWN")
                items.append(
                    {
                        "path": path.value,
                        "covered": witness is not None,
                        "reservation_ids": [] if witness is None else [witness],
                    }
                )
            result.update(covered=all(i["covered"] for i in items), items=items)
        else:
            query = {
                "owner_agent_id": args["owner_agent_id"],
                "active_only": args["active_only"],
            }
            qhash = digest(query)
            clause, params = "1", {"now_utc": stamp}
            if args["owner_agent_id"] is not None:
                clause += " AND agent_id=:owner"
                params["owner"] = args["owner_agent_id"]
            if args["active_only"]:
                clause += " AND " + leases.ACTIVE
            maximum = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM file_reservations WHERE " + clause,
                params,
            ).fetchone()[0]
            after = 0
            if args["cursor"] is not None:
                try:
                    token = args["cursor"]
                    saved = unique_document(
                        base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
                    )
                    validate(
                        saved, FIXTURE["reservation_cursor_schema"], "CURSOR_INVALID"
                    )
                    if (
                        saved["agent_id"] != owner["id"]
                        or saved["query_hash"] != qhash
                        or not 0
                        <= saved["after_id"]
                        <= saved["max_reservation_id"]
                        <= leases.MAX_ID
                    ):
                        raise ValueError
                    maximum, after = saved["max_reservation_id"], saved["after_id"]
                except (ValueError, GlobalError, KeyError):
                    raise GlobalError("CURSOR_INVALID") from None
            params.update(maximum=maximum, after=after, limit=args["limit"] + 1)
            rows = db.execute(
                "SELECT * FROM file_reservations WHERE "
                + clause
                + " AND id>:after AND id<=:maximum ORDER BY id LIMIT :limit",
                params,
            ).fetchall()
            more = len(rows) > args["limit"]
            rows = rows[: args["limit"]]
            cursor = None
            if more:
                cursor = (
                    base64.urlsafe_b64encode(
                        canonical(
                            {
                                "version": 1,
                                "tool": tool,
                                "agent_id": owner["id"],
                                "query_hash": qhash,
                                "max_reservation_id": maximum,
                                "after_id": rows[-1]["id"],
                            }
                        ).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            result.update(
                items=[leases.projection(r) for r in rows],
                max_reservation_id=maximum,
                next_cursor=cursor,
                more=more,
            )
        ensure_budget(result)
        validate(result, TOOLS[tool]["output_schema"], "RESPONSE_INVALID")
        return result

    def management(self, request, peer_uid):
        operation = request.get("operation") if isinstance(request, dict) else None
        inspect = (
            isinstance(request, dict)
            and request.get("action") == "inspect"
            and "reservation_id" in request
        )
        if (
            operation not in ("force_release_reservation", "purge_messages")
            and not inspect
        ):
            return super().management(request, peer_uid)
        if peer_uid != os.getuid():
            raise GlobalError("PEER_UID_REQUIRED")
        contract = FIXTURE["management_contract"][
            "inspect_extension"
            if inspect
            else "force_release"
            if operation == "force_release_reservation"
            else "purge"
        ]
        validate(request, contract["input_schema"])
        if any(
            type(value) is int and value > leases.MAX_ID
            for key, value in request.items()
            if key.endswith("_id")
        ):
            raise GlobalError("ARGUMENTS_UNSUPPORTED")
        binding = tuple(
            request[k]
            for k in (
                "expected_server_instance_id",
                "candidate_generation",
                "authority_epoch",
            )
        )
        with self.transaction(
            write=not inspect and not request.get("dry_run", False), binding=binding
        ) as db:
            if inspect or operation == "force_release_reservation":
                key = request["reservation_id"]
                row = db.execute(
                    "SELECT * FROM file_reservations WHERE id=?", (key,)
                ).fetchone()
                if row is None:
                    raise GlobalError("LEASE_NOT_FOUND")
                if (
                    row["agent_id"]
                    != request["agent_id" if inspect else "expected_owner_agent_id"]
                ):
                    raise GlobalError("LEASE_OWNER_REQUIRED")
                if inspect:
                    result = self.inspect(db, request["agent_id"])
                    fragment = {
                        "reservation": leases.projection(row),
                        "observed_revision": revision(db),
                    }
                    validate(
                        fragment,
                        contract["output_extension_schema"],
                        "RESPONSE_INVALID",
                    )
                    return result | fragment
                if row["revision"] != request["expected_lease_revision"]:
                    raise GlobalError("STALE_LEASE_REVISION")
                modified = row["released_ts"] is None
                row = leases.release(db, [row], now(), "operator", request["note"])[0]
                if modified:
                    from .namespace_store import changed

                    changed(db)
                result = {"lease": leases.projection(row), "changed": modified}
            else:
                cutoff = utc(request["cutoff_at"])
                ids = []
                # Old message timestamps have mixed offsets; compare instants.
                for row in db.execute(
                    "SELECT id,created_ts FROM messages WHERE id>? ORDER BY id",
                    (request["after_id"],),
                ):
                    if utc(row["created_ts"]) < cutoff:
                        ids.append(row["id"])
                        if len(ids) > request["limit"]:
                            break
                more = len(ids) > request["limit"]
                ids = ids[: request["limit"]]
                plan = self.purge_into(db, ids, dry_run=request["dry_run"])
                result = {
                    "deleted_ids": plan["delete_ids"],
                    "held_ids": plan["held_ids"],
                    "cutoff_at": request["cutoff_at"],
                    "more": more,
                    "next_after_id": ids[-1] if ids else request["after_id"],
                }
            result.update(
                ok=True, version=1, operation=operation, observed_revision=revision(db)
            )
            validate(result, contract["output_schema"], "RESPONSE_INVALID")
            return result


def build_s2c_server(config):
    captured = []

    class Runtime(S2cRuntime):
        def __init__(self, path):
            super().__init__(path)
            captured.append(self)

    server = build_s2b_server(config, runtime_class=Runtime)
    for name, contract in TOOLS.items():
        tool = S2bTool(
            name=name,
            description="Isolated global reservation/contact capability.",
            parameters=contract["input_schema"],
            output_schema=contract["output_schema"],
        )
        tool._runtime = captured[0]
        server.add_tool(tool)
        server._agentstack_declared_tools.add(name)
        server._agentstack_published_tools.add(name)
    return server
