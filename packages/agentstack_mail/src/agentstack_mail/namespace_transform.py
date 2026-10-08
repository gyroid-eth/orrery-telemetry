"""Offline global Mail/delivery conversion. Explicit snapshots and resolutions only."""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .namespace_plan import (
    AgentRecord,
    WindowRecord,
    plan_agent_names,
    plan_windows,
    fingerprint,
)
from .namespace_replies import MessageRecord, ReplyCatalog, ReplyError
from .namespace_state_io import (
    SourceBundle,
    StateMigrationError,
    backup,
    connection,
    rows,
    tables,
    tree_manifest,
    atomic_json,
)
from .reservation_paths import normalize_path, paths_overlap
from .utils import sanitize_agent_name


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            result.replace(tzinfo=timezone.utc)
            if result.tzinfo is None
            else result.astimezone(timezone.utc)
        )
    except (ValueError, TypeError, AttributeError):
        raise StateMigrationError("INVALID_STATE_TIMESTAMP") from None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def mail_rows(path: Path) -> dict[str, list[dict]]:
    with connection(path) as database:
        if (
            database.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or database.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise StateMigrationError("SOURCE_DATABASE_INVALID")
        return {name: rows(database, name) for name in tables(database)}


def resolve_mail(
    source: SourceBundle, choices: dict, *, prior_windows: dict | None = None
) -> dict:
    original = mail_rows(source.mail)
    required = {
        "projects",
        "agents",
        "messages",
        "message_recipients",
        "file_reservations",
        "window_identities",
        "mail_instances",
    }
    if not required <= original.keys():
        raise StateMigrationError("UNSUPPORTED_MAIL_SCHEMA")
    instances = original["mail_instances"]
    if len(instances) != 1 or not instances[0]["instance_id"]:
        raise StateMigrationError("MAIL_INSTANCE_REQUIRED")
    projects = {row["id"]: row for row in original["projects"]}
    agents = {row["id"]: row for row in original["agents"]}
    rename = {int(key): value for key, value in choices.get("rename_map", {}).items()}
    name_plan = plan_agent_names(
        [
            AgentRecord(
                row["id"],
                projects[row["project_id"]]["slug"],
                row["name"],
                bool(row["retired_at"]),
            )
            for row in agents.values()
        ],
        rename,
    )
    if name_plan.conflicts:
        raise StateMigrationError("AGENT_NAME_COLLISION")
    window_choices = {
        int(key): value for key, value in choices.get("window_map", {}).items()
    }
    window_plan = plan_windows(
        [
            WindowRecord(
                row["id"],
                projects[row["project_id"]]["slug"],
                row["window_uuid"],
                row["display_name"],
            )
            for row in original["window_identities"]
        ],
        window_choices,
        prior_targets={int(key): value for key, value in (prior_windows or {}).items()},
    )
    if window_plan.unresolved:
        raise StateMigrationError("WINDOW_MAPPING_REQUIRED")
    window_agents = {}
    for row in original["window_identities"]:
        explicit = choices.get("window_agents", {}).get(str(row["id"]))
        matches = [
            agent["id"]
            for agent in agents.values()
            if agent["project_id"] == row["project_id"]
            and agent["name"] == row["display_name"]
        ]
        selected = (
            explicit
            if explicit is not None
            else (matches[0] if len(matches) == 1 else None)
        )
        if (
            selected not in agents
            or agents[selected]["project_id"] != row["project_id"]
        ):
            raise StateMigrationError("WINDOW_AGENT_UNRESOLVED")
        window_agents[str(row["id"])] = selected
    recipients = original["message_recipients"]
    reply_map = {}
    for row in original["messages"]:
        groups = {
            kind: tuple(
                item["agent_id"]
                for item in recipients
                if item["message_id"] == row["id"] and item["kind"] == kind
            )
            for kind in ("to", "cc", "bcc")
        }
        if any(
            item["kind"] not in groups
            for item in recipients
            if item["message_id"] == row["id"]
        ):
            raise StateMigrationError("INVALID_RECIPIENT_KIND")
        try:
            message = MessageRecord.from_legacy(
                message_id=row["id"],
                source=projects[row["project_id"]]["slug"],
                sender_id=row["sender_id"],
                thread_id=row["thread_id"],
                topic=row["topic"],
                **groups,
            )
        except ReplyError:
            if str(row["id"]) not in choices.get("reply_map", {}):
                raise
            message = MessageRecord(
                message_id=row["id"],
                source=projects[row["project_id"]]["slug"],
                sender_id=row["sender_id"],
                **groups,
            )
        reply_map[str(row["id"])] = {
            "reply_to": message.reply_to,
            "label": message.legacy_thread_label,
            "source": message.source,
        }
    repairs = choices.get("reply_map", {})
    if set(repairs) - set(reply_map):
        raise StateMigrationError("UNKNOWN_REPLY_REPAIR")
    for key, parent in repairs.items():
        if parent is not None and (type(parent) is not int or parent <= 0):
            raise StateMigrationError("INVALID_REPLY_REPAIR")
        reply_map[key]["reply_to"] = parent
    catalog = ReplyCatalog(
        [
            MessageRecord(
                row["id"],
                reply_map[str(row["id"])]["source"],
                row["sender_id"],
                reply_to=reply_map[str(row["id"])]["reply_to"],
                legacy_thread_label=reply_map[str(row["id"])]["label"],
            )
            for row in original["messages"]
        ]
    )
    # Construction validates missing references, self references and cycles.
    del catalog
    now = timestamp(choices["as_of"])
    lease_paths = {}
    active = []
    for row in original["file_reservations"]:
        key = str(row["id"])
        live = row["released_ts"] is None and timestamp(row["expires_ts"]) > now
        raw = row["path_pattern"]
        anchor = choices.get("lease_anchors", {}).get(key)
        try:
            value = normalize_path(raw, anchor=Path(anchor) if anchor else None)
        except ValueError:
            if live:
                raise StateMigrationError("LEASE_ANCHOR_OR_RULES_UNKNOWN") from None
            value = None
        lease_paths[key] = None if value is None else value.value
        if live:
            for previous, scope in active:
                if (
                    previous["agent_id"] != row["agent_id"]
                    and (previous["exclusive"] or row["exclusive"])
                    and paths_overlap(scope, value)
                ):
                    raise StateMigrationError("ABSOLUTE_LEASE_COLLISION")
            active.append((row, value))
    return {
        "names": {str(key): value for key, value in name_plan.names.items()},
        "windows": {str(key): value for key, value in window_plan.targets.items()},
        "window_agents": window_agents,
        "reply_map": reply_map,
        "lease_paths": lease_paths,
        "instance_id": instances[0]["instance_id"],
        "unresolved": [],
    }


def target_table(table: str) -> str:
    return {
        "projects": "legacy_projects",
        "project_sibling_suggestions": "legacy_project_sibling_suggestions",
    }.get(table, table)


def target_column(column: str) -> str:
    return "legacy_" + column if column.endswith("project_id") else column


def convert_mail(source: SourceBundle, target: Path, resolved: dict) -> None:
    backup(source.mail, target)
    original = mail_rows(source.mail)
    with connection(target, write=True) as database:
        database.execute("PRAGMA defer_foreign_keys=ON")
        for table in original:
            if target_table(table) != table:
                database.execute(
                    f"ALTER TABLE {quoted(table)} RENAME TO {quoted(target_table(table))}"
                )
        for table in original:
            mapped = target_table(table)
            columns = [
                row["name"]
                for row in database.execute("PRAGMA table_info(" + quoted(mapped) + ")")
            ]
            for name in columns:
                if target_column(name) != name:
                    database.execute(
                        f"ALTER TABLE {quoted(mapped)} RENAME COLUMN {quoted(name)} TO {quoted(target_column(name))}"
                    )
        database.execute("ALTER TABLE agents ADD COLUMN lookup_key TEXT")
        for key, name in resolved["names"].items():
            database.execute(
                "UPDATE agents SET name=?, lookup_key=? WHERE id=?",
                (name, sanitize_agent_name(name).lower(), int(key)),
            )
        database.execute(
            "CREATE UNIQUE INDEX namespace_agent_lookup ON agents(lookup_key)"
        )
        database.execute(
            "ALTER TABLE messages RENAME COLUMN thread_id TO legacy_thread_id_raw"
        )
        database.execute(
            "ALTER TABLE messages ADD COLUMN reply_to INTEGER REFERENCES messages(id) DEFERRABLE INITIALLY DEFERRED"
        )
        database.execute("ALTER TABLE messages ADD COLUMN legacy_thread_label TEXT")
        database.execute("ALTER TABLE messages ADD COLUMN legacy_thread_source TEXT")
        for key, row in resolved["reply_map"].items():
            database.execute(
                "UPDATE messages SET reply_to=?,legacy_thread_label=?,legacy_thread_source=? WHERE id=?",
                (row["reply_to"], row["label"], row["source"], int(key)),
            )
        database.execute("CREATE INDEX namespace_reply_parent ON messages(reply_to)")
        database.execute(
            "ALTER TABLE window_identities ADD COLUMN agent_id INTEGER REFERENCES agents(id)"
        )
        for key, uuid in resolved["windows"].items():
            agent = resolved["window_agents"][key]
            database.execute(
                "UPDATE window_identities SET window_uuid=?,display_name=?,agent_id=? WHERE id=?",
                (uuid, resolved["names"][str(agent)], agent, int(key)),
            )
        database.execute(
            "CREATE UNIQUE INDEX namespace_window_uuid ON window_identities(window_uuid)"
        )
        database.execute(
            "ALTER TABLE file_reservations ADD COLUMN legacy_path_pattern TEXT"
        )
        database.execute(
            "ALTER TABLE file_reservations ADD COLUMN path_unknown INTEGER NOT NULL DEFAULT 0"
        )
        database.execute(
            "ALTER TABLE file_reservations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )
        for row in original["file_reservations"]:
            path = resolved["lease_paths"][str(row["id"])]
            database.execute(
                "UPDATE file_reservations SET legacy_path_pattern=?,path_pattern=?,path_unknown=? WHERE id=?",
                (
                    row["path_pattern"],
                    path or row["path_pattern"],
                    path is None,
                    row["id"],
                ),
            )
        if "message_summaries" in original:
            database.execute(
                "ALTER TABLE message_summaries ADD COLUMN cache_valid INTEGER NOT NULL DEFAULT 0"
            )
        database.execute(
            "CREATE TABLE namespace_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        database.executemany(
            "INSERT INTO namespace_metadata VALUES (?,?)",
            [
                ("activation", "false"),
                ("generation", fingerprint(resolved)),
                ("write_generation", "0"),
                ("resolutions_digest", fingerprint(resolved)),
            ],
        )
        database.execute("PRAGMA user_version=2")
        # Triggers and every historical row survive backup/ALTER; refresh only FTS.
        database.execute("DELETE FROM fts_messages")
        database.execute(
            "INSERT INTO fts_messages(rowid,message_id,subject,body) SELECT id,id,subject,body_md FROM messages"
        )
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise StateMigrationError("CANDIDATE_FOREIGN_KEY_INVALID")
    nullable_provenance(target)


def retry_policy(config: dict, project: str, agent: str) -> dict:
    policy = config.get("delivery_policies", {}).get(delivery_key(project, agent))
    if (
        not isinstance(policy, dict)
        or not isinstance(policy.get("version"), str)
        or not policy["version"]
    ):
        raise StateMigrationError("DELIVERY_POLICY_REQUIRED")
    for name in ("coalesce_seconds", "base_backoff_seconds", "max_backoff_seconds"):
        value = policy.get(name)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= 86400
        ):
            raise StateMigrationError("INVALID_DELIVERY_POLICY")
    if policy["base_backoff_seconds"] > policy["max_backoff_seconds"]:
        raise StateMigrationError("INVALID_DELIVERY_POLICY")
    return policy


def due_at(row: dict, policy: dict) -> tuple[str, str]:
    delay = policy["base_backoff_seconds"]
    for _ in range(min(max(0, row["attempt_count"] - 1), 64)):
        delay = min(policy["max_backoff_seconds"], delay * 2)
    retry = timestamp(row["updated_at"]) + timedelta(seconds=delay)
    coalesce = timestamp(row["created_at"]) + timedelta(
        seconds=policy["coalesce_seconds"]
    )
    return iso(retry), iso(coalesce)


def expected_delivery(
    source: SourceBundle, resolved: dict, choices: dict
) -> list[dict]:
    mail = mail_rows(source.mail)
    config = json.loads(source.config.read_text())
    result, keys = [], set()
    with connection(source.delivery) as database:
        legacy = rows(database, "codex_app_delivery_state")
    for row in legacy:
        old_key = delivery_key(row["project_key"], row["agent_name"], row["message_id"])
        project_ids = {
            item["id"]
            for item in mail["projects"]
            if item["human_key"] == row["project_key"]
        }
        matches = [
            item["id"]
            for item in mail["agents"]
            if item["project_id"] in project_ids and item["name"] == row["agent_name"]
        ]
        explicit = choices.get("delivery_map", {}).get(old_key)
        if explicit is not None:
            if type(explicit) is not int or not any(
                item["id"] == explicit for item in mail["agents"]
            ):
                raise StateMigrationError("DELIVERY_MAPPING_MISMATCH")
            agent = explicit
        elif len(matches) == 1:
            agent = matches[0]
        else:
            raise StateMigrationError("DELIVERY_MAPPING_UNKNOWN")
        if not any(
            item["message_id"] == row["message_id"] and item["agent_id"] == agent
            for item in mail["message_recipients"]
        ):
            raise StateMigrationError("DELIVERY_MESSAGE_UNAVAILABLE")
        key = (resolved["instance_id"], agent, row["message_id"])
        if key in keys:
            raise StateMigrationError("DELIVERY_MAPPING_COLLISION")
        keys.add(key)
        if row["status"] == "leased" and old_key not in choices.get(
            "wake_recovery", []
        ):
            raise StateMigrationError("WAKE_COMPLETION_UNKNOWN")
        policy = retry_policy(config, row["project_key"], row["agent_name"])
        retry, coalesce = due_at(row, policy)
        result.append(
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in ("project_key", "agent_name")
                },
                "mail_instance_id": resolved["instance_id"],
                "agent_id": agent,
                "legacy_project_key": row["project_key"],
                "legacy_agent_name": row["agent_name"],
                "policy_json": json.dumps(policy, sort_keys=True),
                "next_attempt_at": retry,
                "coalesce_ready_at": coalesce,
            }
        )
    return result


DELIVERY_SQL = """CREATE TABLE codex_app_delivery_state(
mail_instance_id TEXT NOT NULL, agent_id INTEGER NOT NULL CHECK(agent_id>0), message_id INTEGER NOT NULL CHECK(message_id>0),
status TEXT NOT NULL CHECK(status IN ('pending','leased','delivered','failed','dead_letter')),
lease_owner TEXT, lease_expires_at TEXT, attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),last_error TEXT,
created_at TEXT NOT NULL,updated_at TEXT NOT NULL,delivered_at TEXT,legacy_project_key TEXT,legacy_agent_name TEXT,
policy_json TEXT NOT NULL,next_attempt_at TEXT NOT NULL,coalesce_ready_at TEXT NOT NULL,
PRIMARY KEY(mail_instance_id,agent_id,message_id),
CHECK((status='leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status<>'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)),
CHECK((status='delivered' AND delivered_at IS NOT NULL) OR (status<>'delivered' AND delivered_at IS NULL)))"""


def convert_delivery(
    source: SourceBundle, target: Path, resolved: dict, choices: dict
) -> None:
    expected = expected_delivery(source, resolved, choices)
    backup(source.delivery, target)
    with connection(target, write=True) as database:
        database.execute(
            "ALTER TABLE codex_app_delivery_state RENAME TO legacy_codex_app_delivery_state"
        )
        # Legacy transition trigger stays attached to its historical table.
        database.execute(DELIVERY_SQL)
        database.execute(DELIVERY_TRANSITION_SQL)
        for row in expected:
            database.execute(
                "INSERT INTO codex_app_delivery_state ("
                + ",".join(row)
                + ") VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                list(row.values()),
            )
        database.execute(
            "CREATE INDEX namespace_delivery_ready ON codex_app_delivery_state(status,next_attempt_at,lease_expires_at)"
        )
        database.execute(
            "CREATE TABLE namespace_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        database.executemany(
            "INSERT INTO namespace_metadata VALUES (?,?)",
            [
                ("activation", "false"),
                ("generation", fingerprint(resolved)),
                ("write_generation", "0"),
            ],
        )
        database.execute("PRAGMA user_version=2")


def transformed_files(source: SourceBundle, resolved: dict) -> dict[str, str]:
    """Immutable byte copies mapped by stable IDs; unknown layouts stop conversion."""
    mail = mail_rows(source.mail)
    projects = {row["slug"]: row["id"] for row in mail["projects"]}
    agents = {(row["project_id"], row["name"]): row["id"] for row in mail["agents"]}
    result = {}
    for category in ("archive", "signals", "history"):
        for relative in tree_manifest(getattr(source, category)):
            parts = Path(relative).parts
            if category == "signals":
                destination = "legacy-signals/" + relative
            elif category == "history" or parts[0] == ".git":
                destination = category + "/legacy/" + relative
            elif len(parts) >= 3 and parts[0] == "projects" and parts[1] in projects:
                tail = parts[2:]
                if tail[0] == "agents" and len(tail) >= 2:
                    legacy_name = tail[1].removesuffix(".signal")
                    agent = agents.get((projects[parts[1]], legacy_name))
                    if agent is None:
                        raise StateMigrationError("STATE_AGENT_MAPPING_UNKNOWN")
                    suffix = (
                        tail[2:]
                        if not tail[1].endswith(".signal")
                        else ("legacy.signal",)
                    )
                    destination = (
                        category + "/agents/" + str(agent) + "/" + "/".join(suffix)
                    )
                else:
                    destination = (
                        category
                        + "/sources/"
                        + str(projects[parts[1]])
                        + "/"
                        + "/".join(tail)
                    )
            elif category == "archive" and relative in (".gitattributes", ".gitignore"):
                destination = category + "/legacy/" + relative
            else:
                raise StateMigrationError("STATE_LAYOUT_UNKNOWN")
            if destination in result.values():
                raise StateMigrationError("STATE_PATH_COLLISION")
            result[category + "/" + relative] = destination
    return result


def convert_bundle(
    source: SourceBundle, target: Path, resolved: dict, choices: dict
) -> dict:
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    convert_mail(source, target / "mail.sqlite3", resolved)
    convert_delivery(source, target / "delivery.sqlite3", resolved, choices)
    mapping = transformed_files(source, resolved)
    envelopes = signal_envelopes(source, resolved)
    for old, new in mapping.items():
        category, relative = old.split("/", 1)
        destination = target / new
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(getattr(source, category) / relative, destination)
        destination.chmod(0o600)
    for envelope in envelopes.values():
        atomic_json(target / envelope["path"], envelope["payload"])
    bindings = expected_bindings(source, resolved)
    atomic_json(target / "bindings.json", bindings)
    shutil.copyfile(source.config, target / "legacy-config.json")
    (target / "legacy-config.json").chmod(0o600)
    with connection(target / "mail.sqlite3", write=True) as database:
        database.execute(
            "CREATE TABLE namespace_attachment_map(message_id INTEGER NOT NULL REFERENCES messages(id),legacy_path TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(message_id,legacy_path))"
        )
        for item in attachments(source, mapping):
            database.execute(
                "INSERT INTO namespace_attachment_map VALUES (?,?,?,?)",
                list(item.values()),
            )
    atomic_json(
        target / "mapping.json",
        {
            "files": mapping,
            "signals": envelopes,
            "resolved": resolved,
            "activation": False,
        },
    )
    return {
        "files": mapping,
        "signals": envelopes,
        "resolved": resolved,
        "activation": False,
    }


def expected_mail(
    original: dict[str, list[dict]], resolved: dict
) -> dict[str, list[dict]]:
    result = {}
    for table, records in original.items():
        target = target_table(table)
        result[target] = []
        for row in records:
            mapped = {target_column(key): value for key, value in row.items()}
            key = str(row.get("id"))
            if table == "agents":
                mapped["name"] = resolved["names"][key]
                mapped["lookup_key"] = sanitize_agent_name(mapped["name"]).lower()
            elif table == "messages":
                mapped["legacy_thread_id_raw"] = mapped.pop("thread_id")
                reference = resolved["reply_map"][key]
                mapped.update(
                    reply_to=reference["reply_to"],
                    legacy_thread_label=reference["label"],
                    legacy_thread_source=reference["source"],
                )
            elif table == "window_identities":
                mapped["agent_id"] = resolved["window_agents"][key]
                mapped["display_name"] = resolved["names"][str(mapped["agent_id"])]
                mapped["window_uuid"] = resolved["windows"][key]
            elif table == "file_reservations":
                path = resolved["lease_paths"][key]
                mapped.update(
                    legacy_path_pattern=row["path_pattern"],
                    path_pattern=path or row["path_pattern"],
                    path_unknown=int(path is None),
                    revision=0,
                )
            elif table == "message_summaries":
                mapped["cache_valid"] = 0
            result[target].append(mapped)
    return result


def expected_bindings(source: SourceBundle, resolved: dict) -> dict:
    bindings = json.loads(source.bindings.read_text())
    mail = mail_rows(source.mail)
    agents = {row["id"]: row for row in mail["agents"]}
    projects = {row["id"]: row for row in mail["projects"]}
    for row in bindings["bindings"]:
        agent = agents.get(row["agent_id"])
        if (
            agent is None
            or row["mail_instance_id"] != resolved["instance_id"]
            or row["project_key"] != projects[agent["project_id"]]["human_key"]
            or row["agent_name"] != agent["name"]
        ):
            raise StateMigrationError("BINDING_SOURCE_MISMATCH")
        if (
            row["registration_token"] != agent["registration_token"]
            or row["credential_generation"] != agent["credential_generation"]
        ):
            raise StateMigrationError("BINDING_CREDENTIAL_MISMATCH")
        window = str(row["window_row_id"])
        old_window = next(
            (item for item in mail["window_identities"] if str(item["id"]) == window),
            None,
        )
        if (
            old_window is None
            or old_window["window_uuid"] != row["window_uuid"]
            or resolved["window_agents"][window] != agent["id"]
        ):
            raise StateMigrationError("BINDING_WINDOW_MISMATCH")
        row["legacy_project_key"] = row.pop("project_key")
        row["agent_name"] = resolved["names"][str(agent["id"])]
        row["window_uuid"] = resolved["windows"][window]
    bindings["version"] = 2
    return bindings


def attachments(source: SourceBundle, mapping: dict) -> list[dict]:
    original = mail_rows(source.mail)
    projects = {row["id"]: row["slug"] for row in original["projects"]}
    files = tree_manifest(source.archive)
    result = []
    for message in original["messages"]:
        for attachment in json.loads(message["attachments"]):
            path = attachment.get("path")
            if path is None:
                continue
            if (
                not isinstance(path, str)
                or Path(path).is_absolute()
                or ".." in Path(path).parts
                or "\\" in path
            ):
                raise StateMigrationError("ATTACHMENT_PATH_INVALID")
            relative = "projects/" + projects[message["project_id"]] + "/" + path
            old = "archive/" + relative
            if relative not in files or old not in mapping:
                raise StateMigrationError("ATTACHMENT_MISSING")
            result.append(
                {
                    "message_id": message["id"],
                    "legacy_path": path,
                    "path": mapping[old],
                    "sha256": files[relative]["sha256"],
                }
            )
    return result


def same_rows(left: list[dict], right: list[dict]) -> bool:
    def ordered(values):
        return sorted(
            values,
            key=lambda row: json.dumps(
                row, sort_keys=True, default=lambda value: {"bytes": value.hex()}
            ),
        )

    return ordered(left) == ordered(right)


def validate_bundle(
    source: SourceBundle, target: Path, resolved: dict, choices: dict
) -> dict:
    # SQLite's integrity command checks the candidate FTS index, not just its content rows.
    # https://www.sqlite.org/fts5.html#the_integrity_check_command
    with connection(target / "mail.sqlite3", write=True) as database:
        try:
            database.execute(
                "INSERT INTO fts_messages(fts_messages) VALUES('integrity-check')"
            )
        except sqlite3.DatabaseError:
            raise StateMigrationError("FTS_INDEX_INVALID") from None
    expected = expected_mail(mail_rows(source.mail), resolved)
    with connection(target / "mail.sqlite3") as database:
        for table, values in expected.items():
            if not same_rows(rows(database, table), values):
                raise StateMigrationError("MAIL_ROWS_NOT_PRESERVED")
        if set(tables(database)) != set(expected) | {
            "namespace_metadata",
            "namespace_attachment_map",
        }:
            raise StateMigrationError("CANDIDATE_TABLES_CHANGED")
        if (
            database.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or database.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise StateMigrationError("CANDIDATE_DATABASE_INVALID")
        fts = [
            dict(row)
            for row in database.execute(
                "SELECT rowid,message_id,subject,body FROM fts_messages"
            )
        ]
        expected_fts = [
            {
                "rowid": row["id"],
                "message_id": row["id"],
                "subject": row["subject"],
                "body": row["body_md"],
            }
            for row in expected["messages"]
        ]
        if not same_rows(fts, expected_fts):
            raise StateMigrationError("FTS_ROWS_INVALID")
        if not same_rows(
            rows(database, "namespace_attachment_map"),
            attachments(source, transformed_files(source, resolved)),
        ):
            raise StateMigrationError("ATTACHMENT_MAPPING_INVALID")
        if dict(database.execute("SELECT key,value FROM namespace_metadata")) != {
            "activation": "false",
            "generation": fingerprint(resolved),
            "write_generation": "0",
            "resolutions_digest": fingerprint(resolved),
        }:
            raise StateMigrationError("CANDIDATE_METADATA_INVALID")
    # Validate source FTS too: a successful rebuild must not hide lost source rows.
    with connection(source.mail) as database:
        fts = [
            dict(row)
            for row in database.execute(
                "SELECT rowid,message_id,subject,body FROM fts_messages"
            )
        ]
        if not same_rows(fts, expected_fts):
            raise StateMigrationError("SOURCE_FTS_INVALID")
    with connection(target / "delivery.sqlite3") as database, connection(
        source.delivery
    ) as original:
        expected_tables = {
            (
                "legacy_codex_app_delivery_state"
                if table == "codex_app_delivery_state"
                else table
            )
            for table in tables(original)
        } | {"codex_app_delivery_state", "namespace_metadata"}
        if set(tables(database)) != expected_tables:
            raise StateMigrationError("DELIVERY_TABLES_CHANGED")
        if (
            database.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or database.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise StateMigrationError("DELIVERY_DATABASE_INVALID")
        if not same_rows(
            rows(database, "codex_app_delivery_state"),
            expected_delivery(source, resolved, choices),
        ):
            raise StateMigrationError("DELIVERY_ROWS_NOT_PRESERVED")
        for table in tables(original):
            candidate = (
                "legacy_codex_app_delivery_state"
                if table == "codex_app_delivery_state"
                else table
            )
            if not same_rows(rows(database, candidate), rows(original, table)):
                raise StateMigrationError("DELIVERY_HISTORY_NOT_PRESERVED")
        if dict(database.execute("SELECT key,value FROM namespace_metadata")) != {
            "activation": "false",
            "generation": fingerprint(resolved),
            "write_generation": "0",
        }:
            raise StateMigrationError("DELIVERY_METADATA_INVALID")
    manifest = json.loads((target / "mapping.json").read_text())
    if manifest != {
        "files": transformed_files(source, resolved),
        "signals": signal_envelopes(source, resolved),
        "resolved": resolved,
        "activation": False,
    }:
        raise StateMigrationError("STATE_MAPPING_CHANGED")
    for old, new in manifest["files"].items():
        category, relative = old.split("/", 1)
        if (getattr(source, category) / relative).read_bytes() != (
            target / new
        ).read_bytes():
            raise StateMigrationError("STATE_BYTES_NOT_PRESERVED")
    for envelope in manifest["signals"].values():
        if json.loads((target / envelope["path"]).read_text()) != envelope["payload"]:
            raise StateMigrationError("SIGNAL_MAPPING_INVALID")
    if json.loads((target / "bindings.json").read_text()) != expected_bindings(
        source, resolved
    ):
        raise StateMigrationError("BINDINGS_NOT_PRESERVED")
    if (target / "legacy-config.json").read_bytes() != source.config.read_bytes():
        raise StateMigrationError("CONFIG_NOT_PRESERVED")
    return {
        "mail": {key: len(value) for key, value in expected.items()},
        "delivery": {
            status: sum(
                row["status"] == status
                for row in expected_delivery(source, resolved, choices)
            )
            for status in ("pending", "leased", "delivered", "failed", "dead_letter")
        },
        "attachments": len(attachments(source, manifest["files"])),
        "state_files": len(manifest["files"]),
        "resolutions_digest": fingerprint(resolved),
        "activation": False,
    }


def nullable_provenance(path: Path) -> None:
    """Rebuild a candidate only; historical project columns cannot be required routing."""
    replacement = path.with_suffix(".global.sqlite3")
    replacement.unlink(missing_ok=True)
    with connection(path) as original, connection(replacement, write=True) as target:
        target.execute("PRAGMA foreign_keys=OFF")
        objects = [
            dict(row)
            for row in original.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND NOT (type='table' AND name GLOB 'fts_messages_*') ORDER BY type,name"
            )
        ]
        for item in objects:
            if item["type"] == "table":
                sql = re.sub(
                    r'((?:"legacy_[A-Za-z_]*project_id"|\blegacy_[A-Za-z_]*project_id\b)\s+INTEGER)\s+NOT NULL',
                    r"\1",
                    item["sql"],
                    flags=re.IGNORECASE,
                )
                target.execute(sql)
        for name in tables(original):
            values = rows(original, name)
            for row in values:
                target.execute(
                    "INSERT INTO "
                    + quoted(name)
                    + " ("
                    + ",".join(quoted(key) for key in row)
                    + ") VALUES ("
                    + ",".join("?" for _ in row)
                    + ")",
                    list(row.values()),
                )
        for row in original.execute("SELECT rowid,* FROM fts_messages"):
            target.execute(
                "INSERT INTO fts_messages(rowid,message_id,subject,body) VALUES (?,?,?,?)",
                tuple(row),
            )
        for item in objects:
            if item["type"] in ("index", "trigger"):
                target.execute(item["sql"])
        target.execute("PRAGMA user_version=2")
    replacement.chmod(0o600)
    replacement.replace(path)
    with connection(path) as candidate:
        if candidate.execute("PRAGMA foreign_key_check").fetchall():
            raise StateMigrationError("CANDIDATE_FOREIGN_KEY_INVALID")


def signal_envelopes(source: SourceBundle, resolved: dict) -> dict:
    mail = mail_rows(source.mail)
    projects = {row["slug"]: row["id"] for row in mail["projects"]}
    agents = {(row["project_id"], row["name"]): row["id"] for row in mail["agents"]}
    messages = {row["id"]: row for row in mail["messages"]}
    result = {}
    for relative in tree_manifest(source.signals):
        parts = Path(relative).parts
        if (
            len(parts) not in (4, 5)
            or parts[0] != "projects"
            or parts[2] != "agents"
            or parts[1] not in projects
        ):
            raise StateMigrationError("SIGNAL_LAYOUT_UNKNOWN")
        name = parts[3].removesuffix(".signal") if len(parts) == 4 else parts[3]
        agent = agents.get((projects[parts[1]], name))
        if agent is None:
            raise StateMigrationError("SIGNAL_AGENT_UNKNOWN")
        try:
            payload = json.loads((source.signals / relative).read_text())
        except (ValueError, UnicodeError):
            raise StateMigrationError("SIGNAL_PAYLOAD_INVALID") from None
        if payload.get("project") != parts[1] or payload.get("agent") != name:
            raise StateMigrationError("SIGNAL_IDENTITY_MISMATCH")
        message = (payload.get("message") or {}).get("id")
        metadata = None
        if message is not None:
            if (
                type(message) is not int
                or message <= 0
                or message not in messages
                or not any(
                    row["message_id"] == message and row["agent_id"] == agent
                    for row in mail["message_recipients"]
                )
            ):
                raise StateMigrationError("SIGNAL_MESSAGE_UNAVAILABLE")
            if len(parts) == 5 and parts[4] != str(message) + ".signal":
                raise StateMigrationError("SIGNAL_IDENTITY_MISMATCH")
            metadata = {
                **payload["message"],
                "sender_id": messages[message]["sender_id"],
                "from": resolved["names"][str(messages[message]["sender_id"])],
            }
        elif len(parts) == 5:
            raise StateMigrationError("SIGNAL_MESSAGE_UNAVAILABLE")
        filename = str(message) + ".signal" if message is not None else "legacy.signal"
        result[relative] = {
            "path": "signals/agents/" + str(agent) + "/" + filename,
            "payload": {
                "mail_instance_id": resolved["instance_id"],
                "agent_id": agent,
                "timestamp": payload.get("timestamp"),
                "message": metadata,
                "needs_inbox": message is None,
                "legacy_signal_path": "legacy-signals/" + relative,
            },
        }
    return result


def delivery_key(project: str, agent: str, message: int | None = None) -> str:
    return json.dumps(
        [project, agent] if message is None else [project, agent, message],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def plan_report(source: SourceBundle, choices: dict) -> dict:
    """Read-only resolution inventory, complete conflict groups and non-secret IDs."""
    mail = mail_rows(source.mail)
    projects = {row["id"]: row for row in mail["projects"]}
    agents = {row["id"]: row for row in mail["agents"]}
    names = plan_agent_names(
        [
            AgentRecord(
                row["id"],
                projects[row["project_id"]]["slug"],
                row["name"],
                bool(row["retired_at"]),
            )
            for row in agents.values()
        ],
        {int(key): value for key, value in choices.get("rename_map", {}).items()},
    )
    # Only unresolved row IDs are reported; this read-only inventory issues no UUID.
    windows = plan_windows(
        [
            WindowRecord(
                row["id"],
                projects[row["project_id"]]["slug"],
                row["window_uuid"],
                row["display_name"],
            )
            for row in mail["window_identities"]
        ],
        {
            int(key): ("proposed-" + str(key) if value == "generate" else value)
            for key, value in choices.get("window_map", {}).items()
        },
    )
    unknown_leases = []
    for row in mail["file_reservations"]:
        if row["released_ts"] is None and timestamp(row["expires_ts"]) > timestamp(
            choices["as_of"]
        ):
            try:
                anchor = choices.get("lease_anchors", {}).get(str(row["id"]))
                normalize_path(
                    row["path_pattern"], anchor=Path(anchor) if anchor else None
                )
            except ValueError:
                unknown_leases.append(
                    {
                        "lease_id": row["id"],
                        "agent_id": row["agent_id"],
                        "legacy_path": row["path_pattern"],
                    }
                )
    replies = {}
    reply_issues = []
    from .namespace_replies import numeric_legacy

    for row in mail["messages"]:
        try:
            parent = (
                numeric_legacy(row["thread_id"])
                if row["thread_id"] is not None
                else None
            )
            parent = choices.get("reply_map", {}).get(str(row["id"]), parent)
            replies[row["id"]] = parent
        except ReplyError:
            if str(row["id"]) in choices.get("reply_map", {}):
                replies[row["id"]] = choices["reply_map"][str(row["id"])]
            else:
                reply_issues.append(
                    {"message_id": row["id"], "reason": "INVALID_REPLY_ID"}
                )
    message_ids = {row["id"] for row in mail["messages"]}
    cycles = set()
    for key, parent in list(replies.items()):
        if parent is not None and (type(parent) is not int or parent <= 0):
            reply_issues.append({"message_id": key, "reason": "INVALID_REPLY_ID"})
            replies[key] = None
            continue
        if parent is not None and parent not in message_ids:
            reply_issues.append({"message_id": key, "reason": "DANGLING_REPLY"})
        path = []
        positions = {}
        current = key
        while current in replies and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = replies[current]
            if current is not None and type(current) is not int:
                break
        if isinstance(current, int) and current in positions:
            cycles.update(path[positions[current] :])
    reply_issues.extend(
        {"message_id": key, "reason": "REPLY_CYCLE"} for key in sorted(cycles)
    )
    delivery = []
    config = json.loads(source.config.read_text())
    with connection(source.delivery) as database:
        for row in rows(database, "codex_app_delivery_state"):
            key = delivery_key(row["project_key"], row["agent_name"], row["message_id"])
            matching_projects = {
                project["id"]
                for project in projects.values()
                if project["human_key"] == row["project_key"]
            }
            candidates = [
                agent["id"]
                for agent in agents.values()
                if agent["project_id"] in matching_projects
                and agent["name"] == row["agent_name"]
            ]
            reasons = []
            if key not in choices.get("delivery_map", {}) and len(candidates) != 1:
                reasons.append("DELIVERY_MAPPING_UNKNOWN")
            if row["status"] == "leased" and key not in choices.get(
                "wake_recovery", []
            ):
                reasons.append("WAKE_COMPLETION_UNKNOWN")
            try:
                retry_policy(config, row["project_key"], row["agent_name"])
            except StateMigrationError:
                reasons.append("DELIVERY_POLICY_REQUIRED")
            if reasons:
                delivery.append(
                    {
                        "legacy_key": key,
                        "candidate_agent_ids": candidates,
                        "status": row["status"],
                        "reasons": reasons,
                    }
                )
    conflicts = [
        [
            {
                "agent_id": key,
                "source": projects[agents[key]["project_id"]]["slug"],
                "name": agents[key]["name"],
                "retired": agents[key]["retired_at"] is not None,
            }
            for key in group
        ]
        for group in names.conflicts
    ]
    result = {
        "activation": False,
        "agent_conflicts": conflicts,
        "window_unresolved": list(windows.unresolved),
        "lease_unresolved": unknown_leases,
        "reply_issues": reply_issues,
        "delivery_unresolved": delivery,
    }
    result["needs_action"] = bool(
        conflicts or windows.unresolved or unknown_leases or reply_issues or delivery
    )
    return result


DELIVERY_TRANSITION_SQL = """CREATE TRIGGER namespace_delivery_status_transition
BEFORE UPDATE OF status ON codex_app_delivery_state
WHEN NOT (OLD.status=NEW.status
OR (OLD.status='pending' AND NEW.status IN ('leased','dead_letter'))
OR (OLD.status='leased' AND NEW.status IN ('pending','delivered','failed','dead_letter'))
OR (OLD.status='failed' AND NEW.status IN ('pending','leased','dead_letter')))
BEGIN SELECT RAISE(ABORT,'invalid candidate delivery transition'); END"""
