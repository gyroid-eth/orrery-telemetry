"""Recorded isolated schema3 retirement; never adopts a mismatched tracker."""

from __future__ import annotations

import argparse
import json
import re
import uuid

from .global_server import GlobalError, GlobalRuntime, document
from .global_s2a import S2aRuntime, revision, schema_objects
from .global_s2a_contract import canonical, digest
from .global_prepare import safe_directory, write_role
from .namespace_state_io import rows, tables


class IncidentRuntime(S2aRuntime):
    """Only tracker equality is waived, not schema/integrity/private files."""

    def __init__(self, config):
        self.fault = lambda point: None
        GlobalRuntime.__init__(self, config)

    def before_operation(self, db, *, write):
        if write:
            raise GlobalError("RUNTIME_WRITER_MISMATCH")
        self.initial_preflight()
        self.validate_candidate(db, allow_gap=True)

    def after_operation(self, db, *, write):
        if write:
            raise GlobalError("RUNTIME_WRITER_MISMATCH")

    def authority_states(self):
        states = super().authority_states()
        path = self.runtime_root / "global-runtime/incident.json"
        if not path.exists() and not path.is_symlink():
            return states
        record = document(path)
        if (
            record.get("kind") != "orrery-global-runtime-incident-v1"
            or record.get("old_authority") != states[0]
            or not isinstance(record.get("new_epoch"), str)
            or not record["new_epoch"]
        ):
            raise GlobalError("WRITER_FENCED")
        fenced = {
            **states[0],
            "phase": "quiescing",
            "authority_epoch": record["new_epoch"],
        }
        return (*states, fenced, {**fenced, "root_status": "retired"})

    def authority_transition(self, before, after, *, exclusive):
        # Only an EX operator transaction can advance its own recorded retirement.
        if not exclusive:
            return super().authority_transition(before, after, exclusive=exclusive)
        states = self.authority_states()
        return (
            before in states
            and after in states
            and states.index(after) >= states.index(before)
        )


def incident_runtime(config):
    if document(config).get("kind") == "orrery-global-server-s2b-v1":
        from .global_s2b import S2bRuntime

        class MessageIncidentRuntime(IncidentRuntime, S2bRuntime):
            config_kind = S2bRuntime.config_kind
            schema_version = S2bRuntime.schema_version
            initial_preflight = S2bRuntime.initial_preflight
            validate_candidate = S2bRuntime.validate_candidate

        return MessageIncidentRuntime(config)
    return IncidentRuntime(config)


def manifest(db):
    contract = db.execute(
        "SELECT last_validated_revision FROM global_runtime_contract WHERE id=1"
    ).fetchone()
    return {
        "tracked_revision": contract[0],
        "current_revision": revision(db),
        "schema_digest": digest(schema_objects(db)),
        "tables": {
            name: {
                "count": db.execute(
                    'SELECT count(*) FROM "' + name.replace('"', '""') + '"'
                ).fetchone()[0],
                "digest": digest(
                    json.loads(
                        json.dumps(rows(db, name), default=lambda v: {"bytes": v.hex()})
                    )
                ),
            }
            for name in tables(db)
        },
    }


def inspect_incident(config):
    runtime = incident_runtime(config)
    with runtime.transaction() as db:
        runtime.validate_candidate(db, full=True, allow_gap=True)
        current = manifest(db)
        value = {
            "kind": "orrery-global-runtime-incident-inspection-v1",
            "server_instance_id": runtime.instance,
            "candidate_generation": runtime.candidate_generation,
            "authority_epoch": runtime.epoch,
            "manifest": current,
            "actual_authority": runtime.authority(),
            "runtime_validated": current["tracked_revision"]
            == current["current_revision"],
        }
        value["incident_digest"] = digest(value)
        return value


def quarantine(
    config,
    *,
    request_id,
    incident_digest,
    tracked_revision,
    current_revision,
    confirm=False,
    fault=None,
):
    fault = fault or (lambda point: None)
    if not confirm:
        raise GlobalError("QUARANTINE_CONFIRMATION_REQUIRED")
    if (
        not isinstance(request_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", request_id
        )
        is None
    ):
        raise GlobalError("REQUEST_ID_INVALID")
    runtime = incident_runtime(config)
    directory = runtime.runtime_root / "global-runtime"
    safe_directory(directory, create=True)
    incident = directory / "incident.json"
    receipt = directory / "quarantine.json"
    for path, kind in [
        (incident, "orrery-global-runtime-incident-v1"),
        (receipt, "orrery-global-runtime-quarantine-v1"),
    ]:
        if path.exists() or path.is_symlink():
            if document(path).get("kind") != kind:
                raise GlobalError("OUTPUT_SCHEMA_INVALID")
    with runtime.transaction(exclusive=True) as db:
        runtime.validate_candidate(db, full=True, allow_gap=True)
        current = manifest(db)
        actual = runtime.authority()
        if incident.exists():
            record = document(incident)
            if (
                record.get("request_id") != request_id
                or record.get("incident_digest") != incident_digest
                or record.get("tracked_revision") != tracked_revision
                or record.get("current_revision") != current_revision
            ):
                raise GlobalError("INCIDENT_DIGEST_MISMATCH")
            if record.get("manifest") != current:
                raise GlobalError("INCIDENT_DIGEST_MISMATCH")
            old = record["old_authority"]
        else:
            value = {
                "kind": "orrery-global-runtime-incident-inspection-v1",
                "server_instance_id": runtime.instance,
                "candidate_generation": runtime.candidate_generation,
                "authority_epoch": runtime.epoch,
                "manifest": current,
                "actual_authority": actual,
                "runtime_validated": current["tracked_revision"]
                == current["current_revision"],
            }
            if (
                digest(value) != incident_digest
                or current["tracked_revision"] != tracked_revision
                or current["current_revision"] != current_revision
                or tracked_revision == current_revision
            ):
                raise GlobalError("INCIDENT_DIGEST_MISMATCH")
            old = actual
            record = {
                "kind": "orrery-global-runtime-incident-v1",
                "request_id": request_id,
                "incident_digest": incident_digest,
                "tracked_revision": tracked_revision,
                "current_revision": current_revision,
                "manifest": current,
                "old_authority": old,
                "new_epoch": str(uuid.uuid4()),
            }
        # Validate every persistent role before changing any authority.
        runtime.check_fence_files()
        if runtime.authority() != actual:
            raise GlobalError("WRITER_FENCED")
        if receipt.exists():
            saved = document(receipt)
            if (
                saved.get("request_id") != request_id
                or saved.get("incident_digest") != incident_digest
            ):
                raise GlobalError("INCIDENT_DIGEST_MISMATCH")
            if (
                saved.get("authority") != actual
                or actual.get("root_status") != "retired"
            ):
                raise GlobalError("WRITER_FENCED")
            return saved
        write_role(incident, record, record["kind"])
        fault("after_incident")
        runtime.check_fence_files()
        fenced = {
            **old,
            "phase": "quiescing",
            "authority_epoch": record["new_epoch"],
        }
        if actual not in (old, fenced, {**fenced, "root_status": "retired"}):
            raise GlobalError("WRITER_FENCED")
        if actual == old:
            write_role(runtime.paths["authority"], fenced, "orrery-global-authority-v1")
        fault("after_fence")
        runtime.check_fence_files()
        retired = {**fenced, "root_status": "retired"}
        write_role(runtime.paths["authority"], retired, "orrery-global-authority-v1")
        fault("after_retire")
        runtime.check_fence_files()
        result = {
            "kind": "orrery-global-runtime-quarantine-v1",
            "request_id": request_id,
            "incident_digest": incident_digest,
            "authority": retired,
            "manifest": current,
            "status": "retired-evidence-retained",
            "activation_enabled": False,
        }
        write_role(receipt, result, result["kind"])
        fault("after_receipt")
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect or retire an isolated schema3 tracker-gap candidate."
    )
    parser.add_argument("action", choices=["inspect", "quarantine"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--request-id")
    parser.add_argument("--incident-digest")
    parser.add_argument("--tracked-revision", type=int)
    parser.add_argument("--current-revision", type=int)
    parser.add_argument("--confirm-quarantine", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect":
            result = inspect_incident(args.config)
        else:
            result = quarantine(
                args.config,
                request_id=args.request_id,
                incident_digest=args.incident_digest,
                tracked_revision=args.tracked_revision,
                current_revision=args.current_revision,
                confirm=args.confirm_quarantine,
            )
        print(canonical(result))
    except (GlobalError, ValueError, TypeError, OSError, KeyError) as exc:
        print(
            canonical(
                {
                    "ok": False,
                    "reason": (
                        str(exc)
                        if isinstance(exc, GlobalError)
                        else "INCIDENT_INPUT_INVALID"
                    ),
                }
            )
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
