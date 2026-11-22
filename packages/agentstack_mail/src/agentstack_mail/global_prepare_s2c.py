"""Fresh schema5 preparation; schema2/3/4 candidates are never upgraded."""


from .global_prepare import prepare as prepare_fresh, contents, write_role
from .global_prepare_s2b import S2bFreshMigration
from .global_server import GlobalError, document
from .global_s2a import base_schema, extension_schema, validate_runtime_candidate
from .global_s2a_contract import digest
from .global_reservations import lease_timestamp
from .global_s2c_contract import FIXTURE
from .namespace_state_io import connection


class S2cFreshMigration(S2bFreshMigration):
    profile = "s2c"
    config_kind = "orrery-global-server-s2c-v1"

    def _build(self, snapshot, receipt, generation):
        report = super()._build(snapshot, receipt, generation)
        target = self.workspace / "candidate"
        self.fault_hook("s2c_before_extension")
        with connection(target / "mail.sqlite3", write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            before = contents(db)
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='file_reservations'"
            ).fetchone()[0]
            indices = [
                r[0]
                for r in db.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='file_reservations' AND type='index' AND sql IS NOT NULL"
                )
            ]
            template = FIXTURE["timestamp_check_template"]
            checks = []
            for column, nullable in template["columns"].items():
                check = template["sql"].format(column=column)
                checks.append(
                    "CHECK("
                    + (column + " IS NULL OR (" + check + ")" if nullable else check)
                    + ")"
                )
            sql = (
                ddl[: ddl.rindex(")")] + "," + ",".join(checks) + ddl[ddl.rindex(")") :]
            )
            sql = sql.replace("file_reservations", "s2c_reservations_build", 1)
            db.execute(sql)
            old_rows = db.execute(
                "SELECT * FROM file_reservations ORDER BY id"
            ).fetchall()
            columns = (
                list(old_rows[0].keys())
                if old_rows
                else [r[1] for r in db.execute("PRAGMA table_info(file_reservations)")]
            )
            expected = []
            for old in old_rows:
                row = dict(old)
                for name in template["columns"]:
                    if row[name] is not None:
                        row[name] = lease_timestamp(row[name])
                expected.append(row)
                db.execute(
                    "INSERT INTO s2c_reservations_build("
                    + ",".join(columns)
                    + ") VALUES("
                    + ",".join("?" for _ in columns)
                    + ")",
                    [row[c] for c in columns],
                )
            db.execute("DROP TABLE file_reservations")
            db.execute("ALTER TABLE s2c_reservations_build RENAME TO file_reservations")
            for sql in indices + FIXTURE["reservation_contract"]["schema_extension"]:
                db.execute(sql)
            self.fault_hook("s2c_after_ddl")
            boundary = lease_timestamp(self.choices["as_of"])
            for row in expected:
                retired = db.execute(
                    "SELECT retired_at FROM agents WHERE id=?", (row["agent_id"],)
                ).fetchone()[0]
                cause = "legacy" if row["released_ts"] is not None else None
                if (
                    retired is not None
                    and row["released_ts"] is None
                    and row["expires_ts"] > boundary
                ):
                    row["released_ts"] = lease_timestamp(retired)
                    cause = "retired"
                row.update(release_cause=cause, release_note="")
                db.execute(
                    "UPDATE file_reservations SET released_ts=?,release_cause=? WHERE id=?",
                    (row["released_ts"], cause, row["id"]),
                )
            contract = dict(
                db.execute("SELECT * FROM global_runtime_contract").fetchone()
            )
            ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='global_runtime_contract'"
            ).fetchone()[0]
            db.execute("DROP TABLE global_runtime_contract")
            db.execute(
                ddl.replace("schema_version='s2b-v1'", "schema_version='s2c-v1'")
            )
            db.execute(
                "INSERT INTO global_runtime_contract VALUES(?,?,?,?,?,?)",
                (
                    1,
                    "s2c-v1",
                    digest(base_schema(db)),
                    digest(extension_schema(db)),
                    contract["initialized_revision"],
                    contract["last_validated_revision"],
                ),
            )
            db.execute("PRAGMA user_version=5")
            after = contents(db)
            actual = sorted(after["file_reservations"], key=lambda r: r["id"])
            if actual != expected:
                raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
            for key in before.keys() - {"file_reservations", "global_runtime_contract"}:
                if before[key] != after[key]:
                    raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise GlobalError("PREPARATION_MANIFEST_MISMATCH")
            self.fault_hook("s2c_before_extension_commit")
        self.fault_hook("s2c_after_extension_commit")
        from .global_s2c import validate_receipt

        with connection(target / "mail.sqlite3") as db:
            validate_runtime_candidate(
                db,
                full=True,
                schema_version=5,
                profile="s2c-v1",
                receipt_validator=validate_receipt,
            )
        proof = {
            "kind": "orrery-s2c-reservation-proof-v1",
            "source_as_of": boundary,
            "before": before["file_reservations"],
            "after": actual,
        }
        write_role(target / "reservation-proof.json", proof, proof["kind"])
        cfg = document(target / "server-config.json")
        cfg["kind"] = self.config_kind
        write_role(target / "server-config.json", cfg, cfg["kind"])
        report.update(runtime_profile="s2c-v1", reservation_proof_digest=digest(proof))
        self.fault_hook("s2c_after_staged_control")
        return report


def prepare(*args, **kwargs):
    return prepare_fresh(*args, migration_class=S2cFreshMigration, **kwargs)


def main(argv=None):
    # The plan has one canonical shape; only its profile and preparation
    # function vary, rather than maintaining another command implementation.
    from .global_prepare_s2b import main as prepare_main

    return prepare_main(argv, profile="s2c", prepare_function=prepare)
