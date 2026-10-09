"""Fresh, unpublished schema3 preparation. Existing runtime roots are never upgraded."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid

from .global_server import GlobalError, document, private
from .global_s2a import (
    base_schema,
    extension_schema,
    validate_runtime_candidate,
)
from .global_s2a_contract import DDL, canonical, digest
from .namespace_migration import NamespaceMigration
from .namespace_plan import MigrationReceipt
from .namespace_state_io import (
    SourceBundle,
    StateMigrationError,
    connection,
    rows,
    tables,
    tree_manifest,
)


def require_legacy_source(path):
    """Validate source format without a second runtime/schema-version contract.

    Immutable inspection cannot create/checkpoint WAL or SHM. PR3 candidates
    publish their namespace schema in the main file; runtime-only writes are
    deliberately not imported by this new-source preparation entrance.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise GlobalError("DATABASE_UNSAFE")
        try:
            db = sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
            try:
                names = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                columns = {r[1] for r in db.execute("PRAGMA table_info(agents)")}
                if "namespace_metadata" in names or "lookup_key" in columns:
                    raise GlobalError("CANDIDATE_SCHEMA_REQUIRES_REPREPARE")
                if "projects" not in names or "project_id" not in columns:
                    raise GlobalError("SOURCE_DATABASE_INVALID")
            finally:
                db.close()
        except sqlite3.Error:
            raise GlobalError("SOURCE_DATABASE_INVALID") from None
    finally:
        os.close(fd)


def safe_directory(path, *, create=False):
    if not path.exists() and create:
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise GlobalError("ISOLATION_ROOT_UNSAFE")


def write_role(path, value, kind):
    """Owned fixed role only: O_EXCL temp, typed existing slot, fsync+rename."""
    if value.get("kind") != kind:
        raise GlobalError("OUTPUT_SCHEMA_INVALID")
    if path.exists() or path.is_symlink():
        if document(path).get("kind") != kind:
            raise GlobalError("OUTPUT_SCHEMA_INVALID")
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            if document(path).get("kind") != kind:
                raise GlobalError("OUTPUT_SCHEMA_INVALID")
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def contents(db):
    return {name: rows(db, name) for name in tables(db)}


class FreshMigration(NamespaceMigration):
    def __init__(
        self, source, workspace, choices, final_root, isolation_root, epoch, fault
    ):
        super().__init__(source, workspace, choices)
        self.final_root = final_root
        self.isolation_root = isolation_root
        self.epoch = epoch
        self.fault_hook = fault

    def _build(self, snapshot, receipt, generation):
        report = super()._build(snapshot, receipt, generation)
        target = self.workspace / "candidate"
        with connection(target / "mail.sqlite3") as db:
            preserved = contents(db)
            base = base_schema(db)
            duplicate = db.execute(
                "SELECT a_agent_id,b_agent_id FROM agent_links GROUP BY a_agent_id,b_agent_id HAVING count(*)>1"
            ).fetchone()
            if duplicate:
                raise GlobalError("CONTACT_PAIR_DUPLICATE")
            for table, columns in [
                ("window_identities", ("created_ts", "last_active_ts", "expires_ts")),
                ("agent_links", ("created_ts", "updated_ts", "expires_ts")),
                ("agents", ("inception_ts", "last_active_ts", "retired_at")),
            ]:
                from .global_s2a import utc

                for row in db.execute("SELECT * FROM " + table):
                    for column in columns:
                        if row[column] is not None:
                            utc(row[column])
        self.fault_hook("before_extension")
        with connection(target / "mail.sqlite3", write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            for index, ddl in enumerate(DDL):
                db.execute(ddl)
                self.fault_hook("after_ddl_" + str(index))
            db.execute(
                "UPDATE namespace_metadata SET value='1' WHERE key='write_generation'"
            )
            db.execute(
                "INSERT INTO global_runtime_contract VALUES(?,?,?,?,?,?)",
                (1, "s2a-v1", digest(base), digest(extension_schema(db)), 1, 1),
            )
            db.execute("PRAGMA user_version=3")
            self.fault_hook("before_extension_commit")
        self.fault_hook("after_extension_commit")
        with connection(target / "mail.sqlite3") as db:
            current = contents(db)
            current.pop("global_runtime_contract")
            current.pop("global_operation_receipts")
            for row in current["namespace_metadata"]:
                if row["key"] == "write_generation":
                    row["value"] = "0"
            if current != preserved or base_schema(db) != base:
                raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
            validate_runtime_candidate(db, full=True)
            metadata = dict(db.execute("SELECT key,value FROM namespace_metadata"))
            instance = db.execute("SELECT instance_id FROM mail_instances").fetchone()[
                0
            ]
        cfg = {
            "kind": "orrery-global-server-s2a-v1",
            "activation_enabled": False,
            "isolation_root": str(self.isolation_root),
            "runtime_root": str(self.final_root),
            "database": str(self.final_root / "mail.sqlite3"),
            "mail_instance_id": instance,
            "candidate_generation": metadata["generation"],
            "authority_epoch": self.epoch,
            "authority": str(self.final_root / "authority.json"),
            "authority_lock": str(self.final_root / "authority.lock"),
            "management_socket": str(self.final_root / "control.sock"),
        }
        write_role(
            target / "authority.json",
            {
                "kind": "orrery-global-authority-v1",
                "phase": "active",
                "root_status": "active",
                "runtime_root": str(self.final_root),
                "mail_instance_id": instance,
                "candidate_generation": metadata["generation"],
                "authority_epoch": self.epoch,
            },
            "orrery-global-authority-v1",
        )
        lock = os.open(
            target / "authority.lock",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.close(lock)
        write_role(target / "server-config.json", cfg, "orrery-global-server-s2a-v1")
        report["runtime_profile"] = "s2a-v1"
        report["base_preservation_digest"] = digest(
            json.loads(json.dumps(preserved, default=lambda v: {"bytes": v.hex()}))
        )
        report["base_schema_digest"] = digest(base)
        report["initialized_revision"] = 1
        self.fault_hook("after_staged_control")
        return report


def prepare(
    source: SourceBundle,
    isolation_root: Path,
    candidate_name: str,
    choices: dict,
    *,
    request_id: str,
    fence,
    fault=None,
):
    """Explicit source/fence collector API. No ambient discovery or live reads."""
    fault = fault or (lambda point: None)
    if (
        not isinstance(candidate_name, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", candidate_name) is None
    ):
        raise GlobalError("PREPARATION_PLAN_CONFLICT")
    if (
        not isinstance(request_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", request_id
        )
        is None
    ):
        raise GlobalError("REQUEST_ID_INVALID")
    choices = json.loads(canonical(choices))
    root = Path(isolation_root)
    temporary = [Path(tempfile.gettempdir()).resolve(), Path("/private/tmp").resolve()]
    if (
        not root.is_absolute()
        or root.is_symlink()
        or root != root.resolve()
        or not any(t == root or t in root.parents for t in temporary)
    ):
        raise GlobalError("ISOLATION_ROOT_INVALID")
    safe_directory(root)
    require_legacy_source(source.mail)
    parent = root / "s2a-candidates"
    workspace = parent / candidate_name
    final = workspace / "candidate"
    for path in asdict(source).values():
        original = Path(path)
        if (
            original.is_symlink()
            or root == original.resolve()
            or workspace == original.resolve()
            or workspace in original.resolve().parents
            or original.resolve() in workspace.parents
        ):
            raise GlobalError("SOURCE_WORKSPACE_OVERLAP")
    plan = {
        "kind": "orrery-s2a-preparation-owner-v1",
        "request_id": request_id,
        "source_paths": {k: str(v.resolve()) for k, v in asdict(source).items()},
        "choices": choices,
        "candidate_name": candidate_name,
        "isolation_root": str(root),
    }
    # No user-controlled output role path. Existing workspace must identify the
    # exact same plan before any slot/lock is opened for write.
    if parent.exists() or parent.is_symlink():
        safe_directory(parent)
    if workspace.exists() or workspace.is_symlink():
        safe_directory(workspace)
        if document(workspace / "preparation-owner.json") != plan:
            raise GlobalError("PREPARATION_PLAN_CONFLICT")
    else:
        safe_directory(parent, create=True)
        workspace.mkdir(mode=0o700)
        write_role(workspace / "preparation-owner.json", plan, plan["kind"])
    owner = workspace / "preparation-owner.json"
    receipt_path = workspace / "preparation-receipt.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        r = document(receipt_path)
        if r.get("kind") != "orrery-s2a-preparation-receipt-v1" or r.get(
            "plan_digest"
        ) != digest(plan):
            raise GlobalError("PREPARATION_PLAN_CONFLICT")
    else:
        r = None
    lock_path = workspace / "preparation.lock"
    if lock_path.exists() or lock_path.is_symlink():
        private(lock_path)
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if document(owner) != plan:
            raise GlobalError("PREPARATION_PLAN_CONFLICT")
        if receipt_path.exists():
            r = document(receipt_path)
        if r and r.get("phase") == "complete":
            return r
        if not r:
            r = {
                "kind": "orrery-s2a-preparation-receipt-v1",
                "request_id": request_id,
                "plan_digest": digest(plan),
                "phase": "planned",
                "authority_epoch": str(uuid.uuid4()),
                "activation_enabled": False,
            }
            write_role(receipt_path, r, r["kind"])
            fault("after_plan")
        staging = workspace / "staging"
        candidate = staging / "candidate"
        if r["phase"] != "ready":
            migration = FreshMigration(
                source, staging, choices, final, root, r["authority_epoch"], fault
            )
            migration.resume(
                fence=fence,
                fault=lambda phase, point: fault("pr3_" + phase + "_" + point),
            )
            # PR3 final gate now includes schema3/extensions/control files. Check
            # it AGAIN at publication; a stale source/fence cannot publish.
            mr = migration._load()
            snapshot = migration._snapshot()
            evidence = migration._fence(fence, snapshot, mr)
            manifest = migration._candidate_manifest(mr)
            planner = MigrationReceipt.from_json(mr["planner"])
            if not planner.gate(snapshot, manifest, evidence).ready:
                raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
            cfg = document(candidate / "server-config.json")
            r.update(
                phase="ready",
                candidate_generation=cfg["candidate_generation"],
                server_instance_id=cfg["mail_instance_id"],
                final_manifest=manifest,
                artifact_manifest=tree_manifest(candidate),
                source_generation=snapshot.generation,
                report=mr["report"],
            )
            fault("before_ready")
            write_role(receipt_path, r, r["kind"])
            fault("after_ready")
        # A ready staging cannot become stale while waiting to rename.
        if candidate.exists():
            migration = FreshMigration(
                source, staging, choices, final, root, r["authority_epoch"], fault
            )
            mr = migration._load()
            snapshot = migration._snapshot()
            evidence = migration._fence(fence, snapshot, mr)
            if (
                not MigrationReceipt.from_json(mr["planner"])
                .gate(snapshot, migration._candidate_manifest(mr), evidence)
                .ready
            ):
                raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
            if final.exists() or final.is_symlink():
                raise GlobalError("PREPARATION_PLAN_CONFLICT")
            if tree_manifest(candidate) != r["artifact_manifest"]:
                raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
            fault("before_publish")
            os.rename(candidate, final)
            d = os.open(workspace, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
            fault("after_publish")
        if (
            not final.is_dir()
            or final.is_symlink()
            or tree_manifest(final) != r["artifact_manifest"]
        ):
            raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
        r["phase"] = "complete"
        fault("before_complete")
        write_role(receipt_path, r, r["kind"])
        fault("after_complete")
        return r
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Prepare a new isolated schema3 candidate; never upgrade existing state."
    )
    parser.add_argument("--plan", required=True)
    args = parser.parse_args(argv)
    try:
        plan = document(args.plan)
        if (
            plan.get("kind") != "orrery-s2a-preparation-plan-v1"
            or plan.get("activation_enabled") is not False
        ):
            raise GlobalError("ISOLATED_CONFIG_REQUIRED")
        from .namespace_plan import FenceEvidence

        source = SourceBundle(**{k: Path(v) for k, v in plan["source_paths"].items()})
        ev = plan["fence_evidence"]
        evidence = FenceEvidence(**ev)
        result = prepare(
            source,
            Path(plan["isolation_root"]),
            plan["candidate_name"],
            plan["choices"],
            request_id=plan["request_id"],
            fence=evidence,
        )
        print(
            canonical(
                {
                    "phase": result["phase"],
                    "activation_enabled": False,
                    "server_instance_id": result["server_instance_id"],
                    "candidate_generation": result["candidate_generation"],
                    "authority_epoch": result["authority_epoch"],
                }
            )
        )
    except (GlobalError, StateMigrationError, ValueError, KeyError, OSError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, (GlobalError, StateMigrationError))
            else "PREPARATION_PLAN_CONFLICT"
        )
        print(canonical({"ok": False, "reason": reason}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
