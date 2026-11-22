"""Fresh schema4 preparation using the one existing publication state machine."""

import argparse
import hashlib
import json
from dataclasses import replace
import os
from pathlib import Path

from .global_server import GlobalError, document, now
from .global_prepare import (
    FreshMigration,
    prepare as prepare_fresh,
    write_role,
    contents,
)
from .global_s2a import base_schema, extension_schema, validate_runtime_candidate, utc
from .global_s2a_contract import digest, canonical
from .global_s2b import PENDING, dirty, validate_receipt
from .global_s2b_contract import DDL, FIXTURE
from .namespace_state_io import (
    connection,
    tree_manifest,
    SourceBundle,
    StateMigrationError,
)


def initialize_notifications(db, delivery, signals, resolutions, stamp):
    if resolutions and (
        resolutions.get("source_delivery_digest") != digest(delivery)
        or resolutions.get("source_signals_digest") != digest(signals)
    ):
        raise GlobalError("NOTIFICATION_SOURCE_REQUIRES_RESOLUTION")
    hints = set()
    unknown = []
    for old, envelope in signals.items():
        value = envelope["payload"]
        mid = value["message_id"]
        aid = value["agent_id"]
        if (
            type(mid) is not int
            or not db.execute(
                "SELECT 1 FROM message_recipients WHERE message_id=? AND agent_id=?",
                (mid, aid),
            ).fetchone()
        ):
            choice = resolutions.get("hints", {}).get(old)
            if not isinstance(choice, dict):
                unknown.append(old)
                continue
            if choice.get("action") == "discard":
                continue
            if choice.get("action") != "map":
                unknown.append(old)
                continue
            mid = choice.get("message_id")
            if (
                type(mid) is not int
                or not db.execute(
                    "SELECT 1 FROM message_recipients WHERE message_id=? AND agent_id=?",
                    (mid, aid),
                ).fetchone()
            ):
                unknown.append(old)
                continue
        hints.add((mid, aid))
    if unknown:
        raise GlobalError("NOTIFICATION_SOURCE_REQUIRES_RESOLUTION")
    rows = {(r["message_id"], r["agent_id"]): r for r in delivery}
    # PR3 rejects orphan deliveries before this initializer is reached.
    if any(
        not db.execute(
            "SELECT 1 FROM message_recipients WHERE message_id=? AND agent_id=?",
            (r["message_id"], r["agent_id"]),
        ).fetchone()
        for r in delivery
    ):
        raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
    proof = []
    for row in db.execute(
        "SELECT r.*,a.retired_at FROM message_recipients r JOIN agents a ON a.id=r.agent_id ORDER BY r.message_id,r.agent_id"
    ).fetchall():
        key = (row["message_id"], row["agent_id"])
        entry = rows.get(key)
        fact = None
        after = None
        delivered = None
        if entry and entry["status"] == "delivered":
            try:
                delivered = utc(entry.get("delivered_at")).isoformat()
            except GlobalError:
                # Read/ack wins; unread inconsistent completion needs evidence.
                pass
        if row["read_ts"] is not None or row["ack_ts"] is not None:
            if delivered is not None:
                fact = "delivered:" + delivered
        elif entry:
            if delivered is not None:
                fact = "delivered:" + delivered
            elif entry["status"] in ("pending", "failed"):
                times = [
                    utc(entry[k])
                    for k in ("next_attempt_at", "coalesce_ready_at")
                    if entry.get(k) is not None
                ]
                after = max(times).isoformat() if times else None
            else:
                choice = resolutions.get("pairs", {}).get(
                    str(key[0]) + ":" + str(key[1])
                )
                if not isinstance(choice, dict) or choice.get("action") not in (
                    "delivered",
                    "retry_after",
                ):
                    raise GlobalError("NOTIFICATION_SOURCE_REQUIRES_RESOLUTION")
                if choice["action"] == "delivered":
                    if not choice.get("evidence"):
                        raise GlobalError("NOTIFICATION_SOURCE_REQUIRES_RESOLUTION")
                    fact = "delivered:" + utc(choice["at"]).isoformat()
                else:
                    after = utc(choice["at"]).isoformat()
        elif key not in hints:
            fact = "imported-consumed"
        db.execute(
            "UPDATE message_recipients SET notification_fact=?,notify_after_ts=? WHERE message_id=? AND agent_id=?",
            (fact, after, *key),
        )
        desired = bool(
            db.execute(
                "SELECT "
                + PENDING
                + " FROM message_recipients r JOIN agents a ON a.id=r.agent_id WHERE r.message_id=? AND r.agent_id=?",
                key,
            ).fetchone()[0]
        )
        if desired:
            dirty(db, *key)
        proof.append(
            {
                "message_id": key[0],
                "recipient_agent_id": key[1],
                "notification_fact": fact,
                "notify_after_ts": after,
                "dirty": desired,
                "source_hint": key in hints,
                "delivery_status": entry["status"] if entry else None,
            }
        )
    return {
        "prepared_at": stamp,
        "source_delivery_digest": digest(delivery),
        "source_signals_digest": digest(signals),
        "resolutions": resolutions,
        "pairs": proof,
        "needs_action": False,
    }


class S2bFreshMigration(FreshMigration):
    profile = "s2b"
    config_kind = "orrery-global-server-s2b-v1"

    def _candidate_bundle(self, root):
        original = super()._candidate_bundle(root)
        saved = root / "provenance"
        return replace(
            original,
            delivery=saved / "delivery.sqlite3",
            archive=saved / "archive",
            history=saved / "history",
            bindings=saved / "bindings.json",
            config=saved / "legacy-config.json",
        )

    def _build(self, snapshot, receipt, generation):
        # First obtain the unchanged PR3+S2a preservation proof. These are not
        # published or activated intermediates; all changes happen in staging.
        report = super()._build(snapshot, receipt, generation)
        target = self.workspace / "candidate"
        mapping = document(target / "mapping.json")
        with connection(target / "delivery.sqlite3") as db:
            delivery = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM codex_app_delivery_state ORDER BY message_id,agent_id"
                )
            ]
        provenance = target / "provenance"
        provenance.mkdir(mode=0o700)
        self.fault_hook("s2b_before_extension")
        with connection(target / "mail.sqlite3", write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            before = contents(db)
            for i, ddl in enumerate(DDL):
                db.execute(ddl)
                self.fault_hook("s2b_after_ddl_" + str(i))
            # There is one active descriptor relation; old JSON/map is retained
            # solely as provenance and never consulted by runtime reads.
            for item in db.execute(
                "SELECT * FROM namespace_attachment_map ORDER BY message_id,legacy_path"
            ).fetchall():
                path = target / item["path"]
                if path.is_symlink() or target.resolve() not in path.resolve().parents:
                    raise GlobalError("OUTPUT_SLOT_CONFLICT")
                content = path.read_bytes()
                sha = hashlib.sha256(content).hexdigest()
                if sha != item["sha256"]:
                    raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO global_attachment_blobs VALUES(?,?,?)",
                        (sha, len(content), content),
                    )
                except Exception:
                    raise GlobalError("SOURCE_ATTACHMENT_NOT_REPRESENTABLE") from None
                ordinal = db.execute(
                    "SELECT count(*) FROM global_message_attachments WHERE message_id=?",
                    (item["message_id"],),
                ).fetchone()[0]
                filename = Path(item["legacy_path"]).name
                message = next(
                    r for r in before["messages"] if r["id"] == item["message_id"]
                )
                metadata = next(
                    (
                        a
                        for a in json.loads(message["attachments"])
                        if a.get("path") == item["legacy_path"]
                    ),
                    {},
                )
                media_type = metadata.get("media_type")
                if not isinstance(media_type, str) or not media_type:
                    media_type = "application/octet-stream"
                db.execute(
                    "INSERT INTO global_message_attachments VALUES(?,?,?,?,?)",
                    (
                        item["message_id"],
                        ordinal,
                        sha,
                        filename,
                        media_type,
                    ),
                )
            attachment_provenance = {
                "kind": "orrery-s2b-legacy-attachments-v1",
                "attachment_map": before["namespace_attachment_map"],
                "message_attachments": [
                    {"id": r["id"], "attachments": r["attachments"]}
                    for r in before["messages"]
                ],
            }
            write_role(
                provenance / "attachments.json",
                attachment_provenance,
                attachment_provenance["kind"],
            )
            for statement in FIXTURE["db_extensions"]["provenance_sql"]:
                db.execute(statement)
            notifications = initialize_notifications(
                db,
                delivery,
                mapping["signals"],
                self.choices.get("notification_resolutions", {}),
                now(),
            )
            maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[
                0
            ]
            db.execute(
                "INSERT INTO namespace_metadata VALUES('message_id_high_water',?)",
                (str(maximum),),
            )
            contract = dict(
                db.execute("SELECT * FROM global_runtime_contract").fetchone()
            )
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='global_runtime_contract'"
            ).fetchone()[0]
            db.execute("DROP TABLE global_runtime_contract")
            db.execute(
                ddl.replace("schema_version='s2a-v1'", "schema_version='s2b-v1'")
            )
            db.execute(
                "INSERT INTO global_runtime_contract VALUES(?,?,?,?,?,?)",
                (
                    1,
                    "s2b-v1",
                    digest(base_schema(db)),
                    digest(extension_schema(db)),
                    contract["initialized_revision"],
                    contract["last_validated_revision"],
                ),
            )
            db.execute("PRAGMA user_version=4")
            self.fault_hook("s2b_before_extension_commit")
        self.fault_hook("s2b_after_extension_commit")
        with connection(target / "mail.sqlite3") as db:
            validate_runtime_candidate(
                db,
                full=True,
                schema_version=4,
                profile="s2b-v1",
                receipt_validator=validate_receipt,
            )
            after = contents(db)
            # Check every legacy row and table, excluding only the explicit
            # initialization delta, rather than blessing arbitrary new content.
            restored = json.loads(
                json.dumps(after, default=lambda v: {"bytes": v.hex()})
            )
            original = json.loads(
                json.dumps(before, default=lambda v: {"bytes": v.hex()})
            )
            for key in (
                "global_attachment_blobs",
                "global_message_attachments",
                "global_signal_dirty",
            ):
                restored.pop(key)
            for row in restored["message_recipients"]:
                for key in ("notification_fact", "notify_after_ts"):
                    row.pop(key)
            restored["namespace_metadata"] = [
                r
                for r in restored["namespace_metadata"]
                if r["key"] != "message_id_high_water"
            ]
            saved_attachments = document(provenance / "attachments.json")
            restored["namespace_attachment_map"] = saved_attachments["attachment_map"]
            old_json = {
                r["id"]: r["attachments"]
                for r in saved_attachments["message_attachments"]
            }
            for row in restored["messages"]:
                row["attachments"] = old_json[row["id"]]
            restored["messages"].sort(key=lambda row: json.dumps(row, sort_keys=True))
            restored["global_runtime_contract"] = original["global_runtime_contract"]
            if restored != original:
                raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
        preserved = provenance / "preserved-signals"
        os.rename(target / "signals", preserved)
        for name in (
            "archive",
            "delivery.sqlite3",
            "legacy-signals",
            "history",
            "bindings.json",
            "legacy-config.json",
        ):
            old = target / name
            if old.exists():
                os.rename(old, provenance / name)
        self.fault_hook("s2b_after_provenance")
        (target / "signals").mkdir(mode=0o700)
        (target / "signals/agents").mkdir(mode=0o700)
        write_role(
            target / "notification-proof.json",
            {"kind": "orrery-s2b-notification-proof-v1", **notifications},
            "orrery-s2b-notification-proof-v1",
        )
        cfg = document(target / "server-config.json")
        cfg["kind"] = self.config_kind
        write_role(target / "server-config.json", cfg, cfg["kind"])
        report.update(
            runtime_profile="s2b-v1",
            notification_proof_digest=digest(notifications),
            preserved_signals=tree_manifest(preserved),
            s2b_canonicalization_digest=digest(
                json.loads(json.dumps(after, default=lambda v: {"bytes": v.hex()}))
            ),
        )
        self.fault_hook("s2b_after_staged_control")
        return report


def prepare(*args, **kwargs):
    return prepare_fresh(*args, migration_class=S2bFreshMigration, **kwargs)


def main(argv=None, *, profile="s2b", prepare_function=prepare):
    parser = argparse.ArgumentParser(
        description="Prepare a fresh isolated "
        + profile
        + " candidate, never upgrade existing roots."
    )
    parser.add_argument("--plan", required=True)
    args = parser.parse_args(argv)
    try:
        plan = document(args.plan)
        if (
            plan.get("kind") != "orrery-" + profile + "-preparation-plan-v1"
            or plan.get("activation_enabled") is not False
        ):
            raise GlobalError("ISOLATED_CONFIG_REQUIRED")
        from .namespace_plan import FenceEvidence

        source = SourceBundle(**{k: Path(v) for k, v in plan["source_paths"].items()})
        result = prepare_function(
            source,
            Path(plan["isolation_root"]),
            plan["candidate_name"],
            plan["choices"],
            request_id=plan["request_id"],
            fence=FenceEvidence(**plan["fence_evidence"]),
        )
        print(
            canonical(
                {
                    k: result[k]
                    for k in (
                        "phase",
                        "server_instance_id",
                        "candidate_generation",
                        "authority_epoch",
                        "activation_enabled",
                    )
                }
            )
        )
    except (GlobalError, StateMigrationError, ValueError, KeyError, OSError) as exc:
        print(
            canonical(
                {
                    "ok": False,
                    "reason": str(exc)
                    if isinstance(exc, (GlobalError, StateMigrationError))
                    else "PREPARATION_PLAN_CONFLICT",
                }
            )
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
