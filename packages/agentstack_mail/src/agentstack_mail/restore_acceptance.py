"""One-process observer for the production-backup restore acceptance gate.

The public observer reads the live production database twice, read-only, while
all destructive rehearsal work is confined to a caller-selected absent run
directory.  It publishes no terminal receipt unless every isolation, restore,
runtime, and live-traffic invariant passes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import selectors
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Final
from urllib.parse import quote

from fastmcp import Client

from . import migration
from .contract import COMPATIBILITY_TOOLS


SCHEMA_VERSION: Final[int] = 1
KIND: Final[str] = "production-backup-restore-acceptance"
PRODUCTION_PORT: Final[int] = 8765
MESSAGE_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "project_id",
    "sender_id",
    "thread_id",
    "topic",
    "subject",
    "body_md",
    "importance",
    "ack_required",
    "created_ts",
    "attachments",
)
MESSAGE_CANONICALIZATION: Final[dict[str, Any]] = {
    "encoding": "UTF-8",
    "json": {
        "ensure_ascii": False,
        "sort_keys": True,
        "separators": [",", ":"],
    },
    "row_order": "ORDER BY id",
    "typed_values": {
        "null": '["null",null]',
        "blob": '["blob",SHA256(bytes),len(bytes)]',
        "integer": '["integer",value]',
        "real": '["real",float.hex()]',
        "text": '["text",value]',
    },
}
_WORKER_READY: Final[str] = "RESTORE_ACCEPTANCE_WORKER_READY"
RAW_FAMILY_ROLES: Final[tuple[str, ...]] = ("main", "wal", "shm")
ABSENT_PIN: Final[str] = "ABSENT"
PUBLISH_FAULT_PHASES: Final[tuple[str, ...]] = (
    "before_receipt_prepare",
    "after_receipt_prepare",
    "before_pin_prepare",
    "after_pin_prepare",
    "before_receipt_chmod",
    "after_receipt_chmod",
    "before_pin_chmod",
    "after_pin_chmod",
    "before_pin_link",
    "after_pin_link",
    "before_pin_directory_fsync",
    "after_pin_directory_fsync",
    "before_receipt_link",
    "after_receipt_link",
    "before_receipt_directory_fsync",
    "after_receipt_directory_fsync",
    "before_receipt_temporary_unlink",
    "after_receipt_temporary_unlink",
    "before_pin_temporary_unlink",
    "after_pin_temporary_unlink",
    "before_cleanup_directory_fsync",
    "after_cleanup_directory_fsync",
    "before_final_receipt_verification",
    "after_final_receipt_verification",
    "before_final_pin_verification",
    "after_final_pin_verification",
)


class RestoreAcceptanceError(RuntimeError):
    """The bounded restore acceptance observation failed closed."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_absolute(path: Path, *, label: str) -> Path:
    raw = os.fspath(path.expanduser())
    absolute = Path(os.path.abspath(raw))
    if not path.is_absolute() or os.path.normpath(raw) != raw or str(absolute) != raw:
        raise RestoreAcceptanceError(f"{label} must be a canonical absolute path")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise RestoreAcceptanceError(
                f"{label} contains a symbolic path component: {current}"
            )
    return absolute


def _family_paths(database: Path) -> dict[str, Path]:
    return {
        "main": database,
        "wal": Path(f"{database}-wal"),
        "shm": Path(f"{database}-shm"),
    }


def _family_identity(database: Path, *, label: str) -> dict[str, Any]:
    database = _canonical_absolute(database, label=label)
    members: dict[str, dict[str, Any]] = {}
    for role, path in _family_paths(database).items():
        _canonical_absolute(path, label=f"{label}.{role}")
        try:
            info = path.lstat()
        except FileNotFoundError:
            if role == "main":
                raise RestoreAcceptanceError(f"{label} main database is absent: {path}")
            members[role] = {
                "state": "ABSENT",
                "canonical_path": str(path),
                "no_symlink": True,
            }
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RestoreAcceptanceError(f"{label}.{role} is not a regular file")
        if info.st_nlink != 1:
            raise RestoreAcceptanceError(f"{label}.{role} must have nlink=1")
        members[role] = {
            "state": "PRESENT",
            "canonical_path": str(path),
            "device": int(info.st_dev),
            "inode": int(info.st_ino),
            "mode": stat.S_IMODE(info.st_mode),
            "nlink": int(info.st_nlink),
            "no_symlink": True,
        }
    return {
        "database": str(database),
        "existence_set": sorted(
            role for role, record in members.items() if record["state"] == "PRESENT"
        ),
        "members": members,
    }


def _raw_family_pin(
    *, path: Path, state: str, sha256: str, size: int, mode: str
) -> dict[str, Any]:
    path = _canonical_absolute(path, label="raw backup family member")
    if state not in {"PRESENT", "ABSENT"}:
        raise RestoreAcceptanceError("raw backup state must be PRESENT or ABSENT")
    if state == "ABSENT":
        if sha256 != ABSENT_PIN or size != -1 or mode != ABSENT_PIN:
            raise RestoreAcceptanceError(
                "ABSENT raw backup requires SHA=ABSENT, size=-1, mode=ABSENT"
            )
        return {
            "path": str(path),
            "state": state,
            "sha256": sha256,
            "size": size,
            "mode": mode,
        }
    _require_sha256(sha256, label="raw backup member SHA-256")
    if size < 0:
        raise RestoreAcceptanceError("PRESENT raw backup size cannot be negative")
    if len(mode) != 4 or mode[0] != "0" or set(mode) - set("01234567"):
        raise RestoreAcceptanceError(
            "PRESENT raw backup mode must be four octal digits such as 0644"
        )
    return {
        "path": str(path),
        "state": state,
        "sha256": sha256,
        "size": size,
        "mode": mode,
    }


def _open_pinned_raw_family(
    pins: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], tuple[int, ...]]:
    if set(pins) != set(RAW_FAMILY_ROLES):
        raise RestoreAcceptanceError("raw backup descriptor must name main/WAL/SHM")
    if pins["main"]["state"] != "PRESENT":
        raise RestoreAcceptanceError("raw backup main must be PRESENT")
    main_path = Path(pins["main"]["path"])
    expected_sidecars = {
        "wal": Path(f"{main_path}-wal"),
        "shm": Path(f"{main_path}-shm"),
    }
    for role, expected in expected_sidecars.items():
        if Path(pins[role]["path"]) != expected:
            raise RestoreAcceptanceError(
                f"raw backup {role} path is not the sidecar of raw backup main"
            )
    opened: dict[str, dict[str, Any]] = {}
    descriptors: list[int] = []
    try:
        for role in RAW_FAMILY_ROLES:
            pin = pins[role]
            path = _canonical_absolute(Path(pin["path"]), label=f"raw backup {role}")
            if pin["state"] == "ABSENT":
                try:
                    path.lstat()
                except FileNotFoundError:
                    opened[role] = {**pin, "no_symlink": True, "fd": None}
                    continue
                raise RestoreAcceptanceError(
                    f"raw backup {role} is present but pinned ABSENT"
                )
            try:
                before = path.lstat()
            except FileNotFoundError as exc:
                raise RestoreAcceptanceError(
                    f"raw backup {role} is absent but pinned PRESENT"
                ) from exc
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
            ):
                raise RestoreAcceptanceError(
                    f"raw backup {role} must be a singly-linked regular file"
                )
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            descriptors.append(descriptor)
            opened_info = os.fstat(descriptor)
            identity_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            before_identity = tuple(int(getattr(before, name)) for name in identity_fields)
            opened_identity = tuple(
                int(getattr(opened_info, name)) for name in identity_fields
            )
            if opened_identity != before_identity:
                raise RestoreAcceptanceError(
                    f"raw backup {role} changed while its descriptor opened"
                )
            digest = hashlib.sha256()
            os.lseek(descriptor, 0, os.SEEK_SET)
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after_open = os.fstat(descriptor)
            after_identity = tuple(
                int(getattr(after_open, name)) for name in identity_fields
            )
            after_path = path.lstat()
            after_path_identity = tuple(
                int(getattr(after_path, name)) for name in identity_fields
            )
            if (
                after_identity != before_identity
                or after_path_identity != before_identity
            ):
                raise RestoreAcceptanceError(
                    f"raw backup {role} changed while it was fingerprinted"
                )
            actual = {
                "path": str(path),
                "state": "PRESENT",
                "sha256": digest.hexdigest(),
                "size": int(before.st_size),
                "mode": f"0{stat.S_IMODE(before.st_mode):03o}",
                "device": int(before.st_dev),
                "inode": int(before.st_ino),
                "nlink": int(before.st_nlink),
                "mtime_ns": int(before.st_mtime_ns),
                "ctime_ns": int(before.st_ctime_ns),
                "no_symlink": True,
                "fd": descriptor,
            }
            for key in ("state", "sha256", "size", "mode"):
                if actual[key] != pin[key]:
                    raise RestoreAcceptanceError(
                        f"raw backup {role} {key} differs from its mandatory pin"
                    )
            os.lseek(descriptor, 0, os.SEEK_SET)
            opened[role] = actual
        return {
            "schema_version": 1,
            "kind": "pinned-read-only-sqlite-family",
            "roles": opened,
            "source_open_count": sum(
                record["state"] == "PRESENT" for record in opened.values()
            ),
            "source_reopen_allowed": False,
        }, tuple(descriptors)
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _close_descriptors(descriptors: Sequence[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _assert_non_alias(families: dict[str, dict[str, Any]]) -> dict[str, Any]:
    paths: dict[str, str] = {}
    identities: dict[tuple[int, int], str] = {}
    for family_name, family in families.items():
        for role, record in family["members"].items():
            if record["state"] != "PRESENT":
                continue
            owner = f"{family_name}.{role}"
            path = record["canonical_path"]
            if path in paths:
                raise RestoreAcceptanceError(
                    f"database families alias by canonical path: {paths[path]} and {owner}"
                )
            paths[path] = owner
            identity = (record["device"], record["inode"])
            if identity in identities:
                raise RestoreAcceptanceError(
                    f"database families alias by device/inode: "
                    f"{identities[identity]} and {owner}"
                )
            identities[identity] = owner
    return {
        "status": "distinct",
        "present_path_count": len(paths),
        "present_device_inode_count": len(identities),
    }


def _typed_message_value(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", hashlib.sha256(value).hexdigest(), len(value)]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    raise RestoreAcceptanceError(f"unsupported SQLite value type: {type(value)!r}")


def _message_rows_digest(rows: Sequence[Sequence[Any]]) -> str:
    typed = [[_typed_message_value(value) for value in row] for row in rows]
    payload = json.dumps(
        typed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _database_uri(path: Path) -> str:
    return f"file:{quote(path.as_posix(), safe='/')}?mode=ro"


def _capture_message_window(
    database: Path,
    *,
    prefix_max_id: int | None = None,
) -> dict[str, Any]:
    database = _canonical_absolute(database, label="message database")
    started_at = _utc_now()
    uri = _database_uri(database)
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        if query_only != 1:
            raise RestoreAcceptanceError(
                "message window connection did not report query_only=ON"
            )
        connection.execute("BEGIN")
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        ]
        missing = [column for column in MESSAGE_COLUMNS if column not in columns]
        if missing:
            raise RestoreAcceptanceError(
                f"messages table lacks canonical columns: {missing}"
            )
        maximum, count = connection.execute(
            "SELECT MAX(id), COUNT(*) FROM messages"
        ).fetchone()
        maximum_id = int(maximum) if maximum is not None else 0
        row_count = int(count)
        bound = maximum_id if prefix_max_id is None else prefix_max_id
        if bound < 0:
            raise RestoreAcceptanceError("message prefix max id cannot be negative")
        projection = ", ".join(f'"{column}"' for column in MESSAGE_COLUMNS)
        prefix_rows = connection.execute(
            f"SELECT {projection} FROM messages WHERE id <= ? ORDER BY id",
            (bound,),
        ).fetchall()
        prefix_ids = [int(row[0]) for row in prefix_rows]
        if prefix_ids != sorted(set(prefix_ids)):
            raise RestoreAcceptanceError("message ids are not strictly increasing")
        new_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM messages WHERE id > ? ORDER BY id", (bound,)
            ).fetchall()
        ]
        result = {
            "transaction": "one-read-transaction",
            "open_mode": {
                "uri": uri,
                "query_only": query_only,
                "readback": "pragma-query-only-read-from-the-open-connection",
            },
            "started_at": started_at,
            "completed_at": _utc_now(),
            "columns": list(MESSAGE_COLUMNS),
            "canonicalization": MESSAGE_CANONICALIZATION,
            "max_id": maximum_id,
            "count": row_count,
            "prefix_max_id": bound,
            "prefix_count": len(prefix_rows),
            "prefix_sha256": _message_rows_digest(prefix_rows),
            "new_ids": new_ids,
        }
        connection.rollback()
        return result
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _evaluate_production_invariants(
    before_family: dict[str, Any],
    after_family: dict[str, Any],
    before_messages: dict[str, Any],
    after_messages: dict[str, Any],
) -> dict[str, dict[str, bool]]:
    before_max = int(before_messages["max_id"])
    after_max = int(after_messages["max_id"])
    new_ids = [int(value) for value in after_messages["new_ids"]]
    new_ids_are_contiguous = (
        after_max >= before_max
        and len(new_ids) == after_max - before_max
        and all(
            value == before_max + offset
            for offset, value in enumerate(new_ids, start=1)
        )
    )
    invariants = {
        "family_identity_unchanged": before_family == after_family,
        "maximum_did_not_decrease": after_max >= before_max,
        "prefix_bound_is_before_max": after_messages["prefix_max_id"] == before_max,
        "prefix_count_unchanged": (
            before_messages["prefix_count"] == before_messages["count"]
            and after_messages["prefix_count"] == before_messages["count"]
        ),
        "prefix_digest_unchanged": (
            before_messages["prefix_sha256"] == after_messages["prefix_sha256"]
        ),
        "count_delta_matches_new_ids": (
            int(after_messages["count"]) - int(before_messages["count"])
            == len(new_ids)
        ),
    }
    observations = {
        "new_ids_are_contiguous": new_ids_are_contiguous,
    }
    failed = sorted(name for name, passed in invariants.items() if not passed)
    if failed:
        raise RestoreAcceptanceError(
            f"production invariants failed: {', '.join(failed)}"
        )
    return {"invariants": invariants, "observations": observations}


def _run_capture(arguments: list[str], *, timeout: float = 10) -> dict[str, Any]:
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RestoreAcceptanceError(
            f"subprocess deadline expired: {arguments[0]}"
        ) from exc
    except OSError as exc:
        raise RestoreAcceptanceError(
            f"subprocess could not start: {arguments[0]}: {exc}"
        ) from exc
    return {
        "arguments": arguments,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _all_process_records(
    *, timeout_seconds: float = 10,
) -> tuple[list[dict[str, Any]], str]:
    capture = _run_capture(
        ["/bin/ps", "-ww", "-axo", "pid=,ppid=,pgid=,command="],
        timeout=timeout_seconds,
    )
    if capture["returncode"] != 0:
        raise RestoreAcceptanceError(f"ps failed: {capture['stderr'].strip()}")
    records: list[dict[str, Any]] = []
    for line in capture["stdout"].splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            process_id, parent_id, group_id = map(int, parts[:3])
        except ValueError:
            continue
        records.append(
            {
                "pid": process_id,
                "ppid": parent_id,
                "pgid": group_id,
                "command": parts[3],
            }
        )
    return records, capture["stdout"]


def _capture_process_tree(root_pid: int) -> dict[str, Any]:
    records, _raw_all = _all_process_records()
    by_pid = {record["pid"]: record for record in records}
    if root_pid not in by_pid:
        raise RestoreAcceptanceError(f"process tree root disappeared: {root_pid}")
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for record in records:
            if record["ppid"] in selected and record["pid"] not in selected:
                selected.add(record["pid"])
                changed = True
    root_group = by_pid[root_pid]["pgid"]
    selected.update(record["pid"] for record in records if record["pgid"] == root_group)
    tree = [by_pid[process_id] for process_id in sorted(selected) if process_id in by_pid]
    raw_selected = "".join(
        f"{record['pid']:>8} {record['ppid']:>8} {record['pgid']:>8} "
        f"{record['command']}\n"
        for record in tree
    )
    return {
        "root_pid": root_pid,
        "root_pgid": root_group,
        "pids": [record["pid"] for record in tree],
        "records": tree,
        "raw_selected_ps": raw_selected,
    }


def _parse_lsof_fields(raw: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current_pid: int | None = None
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            try:
                current_pid = int(value)
            except ValueError:
                current_pid = None
        elif field == "f":
            if current is not None:
                files.append(current)
            current = {"pid": current_pid, "fd": value}
        elif current is not None:
            if field == "D":
                try:
                    current["device"] = int(value, 0)
                except ValueError:
                    current["device_raw"] = value
            elif field == "i":
                try:
                    current["inode"] = int(value)
                except ValueError:
                    current["inode_raw"] = value
            elif field == "n":
                current["name"] = value
            elif field == "t":
                current["type"] = value
            elif field == "c":
                current["command"] = value
    if current is not None:
        files.append(current)
    return files


def _capture_tree_lsof(tree: dict[str, Any]) -> dict[str, Any]:
    executable = shutil.which("lsof") or "/usr/sbin/lsof"
    if not Path(executable).is_file():
        raise RestoreAcceptanceError("lsof is required for open-file evidence")
    observations: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for process_id in tree["pids"]:
        capture = _run_capture(
            [executable, "-nP", "-a", "-p", str(process_id), "-FpcftDin"],
            timeout=10,
        )
        if capture["returncode"] not in {0, 1}:
            raise RestoreAcceptanceError(
                f"lsof failed for pid {process_id}: {capture['stderr'].strip()}"
            )
        parsed = _parse_lsof_fields(capture["stdout"])
        observations.append(capture)
        files.extend(parsed)
    return {"commands": observations, "files": files}


def _family_matches(
    lsof_files: Sequence[dict[str, Any]], family: dict[str, Any]
) -> dict[str, list[int]]:
    matches: dict[str, list[int]] = {}
    for role, member in family["members"].items():
        if member["state"] != "PRESENT":
            continue
        path = member["canonical_path"]
        identity = (member["device"], member["inode"])
        holders = {
            int(record["pid"])
            for record in lsof_files
            if record.get("pid") is not None
            and (
                record.get("name") == path
                or (record.get("device"), record.get("inode")) == identity
            )
        }
        if holders:
            matches[role] = sorted(holders)
    return matches


def _network_matches_port(
    lsof_files: Sequence[dict[str, Any]], port: int
) -> list[dict[str, Any]]:
    marker = f":{port}"
    return [
        record
        for record in lsof_files
        if record.get("type") in {"IPv4", "IPv6"}
        and marker in str(record.get("name", ""))
    ]


def _assert_tree_open_boundary(
    tree_lsof: dict[str, Any],
    *,
    production_family: dict[str, Any],
    target_family: dict[str, Any] | None,
    isolated_port: int | None,
    require_target: bool,
) -> dict[str, Any]:
    files = tree_lsof["files"]
    production_matches = _family_matches(files, production_family)
    production_network = _network_matches_port(files, PRODUCTION_PORT)
    if production_matches or production_network:
        raise RestoreAcceptanceError(
            "sampled rehearsal process tree observation found the production "
            "database family or listener"
        )
    target_matches = (
        _family_matches(files, target_family) if target_family is not None else {}
    )
    if require_target:
        assert target_family is not None
        expected = {
            role
            for role, record in target_family["members"].items()
            if record["state"] == "PRESENT"
        }
        if set(target_matches) != expected:
            raise RestoreAcceptanceError(
                "candidate server did not hold every restored target family member"
            )
        holders = sorted({pid for values in target_matches.values() for pid in values})
        if len(holders) != 1:
            raise RestoreAcceptanceError(
                "restored target family was not held by exactly one candidate server"
            )
        if isolated_port is None or not _network_matches_port(files, isolated_port):
            raise RestoreAcceptanceError(
                "candidate server process tree lacks the isolated listener positive control"
            )
    return {
        "production_family_matches": production_matches,
        "production_listener_matches": production_network,
        "target_family_matches": target_matches,
        "status": "sampled-isolated-observation",
        "claim_scope": "sampled process-tree lsof observations only",
    }


def _listener_observation(port: int) -> dict[str, Any]:
    executable = shutil.which("lsof") or "/usr/sbin/lsof"
    capture = _run_capture(
        [executable, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=10
    )
    if capture["returncode"] not in {0, 1}:
        raise RestoreAcceptanceError(
            f"listener observation failed: {capture['stderr'].strip()}"
        )
    pid_capture = _run_capture(
        [executable, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        timeout=10,
    )
    if pid_capture["returncode"] not in {0, 1}:
        raise RestoreAcceptanceError(
            f"listener owner observation failed: {pid_capture['stderr'].strip()}"
        )
    pids = sorted({int(value) for value in pid_capture["stdout"].split()})
    return {
        "port": port,
        "method": "lsof-listener-table-no-network-connection",
        "pids": pids,
        "raw_table": capture,
        "raw_owner_query": pid_capture,
    }


async def _read_only_mcp_probe(url: str) -> dict[str, Any]:
    async with Client(url, timeout=5, init_timeout=5) as client:
        tools = await client.list_tools()
        result = await client.call_tool("health_check", {})
    value = result.structured_content
    if value is None:
        value = result.data
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    if not isinstance(value, dict):
        raise RestoreAcceptanceError("health_check did not return an object")
    names = sorted(tool.name for tool in tools)
    return {
        "tool_names": names,
        "tool_count": len(names),
        "health": value,
        "calls": ["list_tools", "health_check"],
        "write_calls": [],
    }


def _wait_read_only_ready(
    process: subprocess.Popen[Any],
    *,
    url: str,
    expected_database_url: str,
    port: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    last_error = "endpoint did not answer"
    while time.monotonic() - started < timeout_seconds:
        if process.poll() is not None:
            raise RestoreAcceptanceError(
                f"candidate server exited before readiness with rc={process.returncode}"
            )
        try:
            observation = asyncio.run(_read_only_mcp_probe(url))
            health = observation["health"]
            if (
                set(observation["tool_names"]) != COMPATIBILITY_TOOLS
                # Count comes from the contract, not a literal. The set and the
                # number are the same claim, and writing the number twice means
                # one copy can be right while the other is stale — which is how
                # publishing a tool turned a green gate red in a place nobody
                # was looking (2026-08-28).
                or observation["tool_count"] != len(COMPATIBILITY_TOOLS)
            ):
                raise RestoreAcceptanceError(
                    "candidate server did not publish the exact "
                    f"{len(COMPATIBILITY_TOOLS)}-tool boundary"
                )
            if health.get("status") != "ok":
                raise RestoreAcceptanceError("candidate health status is not ok")
            if health.get("http_host") != "127.0.0.1" or health.get("http_port") != port:
                raise RestoreAcceptanceError("candidate health binding is unexpected")
            if health.get("database_url") != expected_database_url:
                raise RestoreAcceptanceError("candidate health names the wrong database")
            return {
                **observation,
                "bounded_ready_ms": round((time.monotonic() - started) * 1000, 3),
                "deadline_seconds": timeout_seconds,
                "url": url,
            }
        except RestoreAcceptanceError:
            raise
        except Exception as exc:  # startup races are expected
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
    raise RestoreAcceptanceError(f"candidate readiness deadline expired: {last_error}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_exclusive(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _publish_write_once_pair(
    receipt_path: Path,
    pin_path: Path,
    payload: dict[str, Any],
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    receipt_path = _canonical_absolute(receipt_path, label="terminal receipt")
    pin_path = _canonical_absolute(pin_path, label="terminal receipt pin")
    if receipt_path == pin_path:
        raise RestoreAcceptanceError("terminal receipt and pin must be different paths")
    if receipt_path.parent != pin_path.parent:
        raise RestoreAcceptanceError("terminal receipt and pin must share one parent")
    parent = receipt_path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise RestoreAcceptanceError("terminal output parent must be a real directory")
    for path in (receipt_path, pin_path):
        if path.exists() or path.is_symlink():
            raise RestoreAcceptanceError(f"terminal output must be absent: {path}")

    receipt_bytes = _canonical_json(payload)
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    pin_bytes = f"{receipt_sha256}  {receipt_path.name}\n".encode("ascii")
    token = uuid.uuid4().hex
    receipt_temporary = parent / f".{receipt_path.name}.{token}.prepared"
    pin_temporary = parent / f".{pin_path.name}.{token}.prepared"
    published_pin = False
    published_receipt = False
    committed = False
    post_commit_warning: str | None = None

    def fault(phase: str) -> None:
        if fault_hook is not None:
            fault_hook(phase)

    try:
        fault("before_receipt_prepare")
        _write_bytes_exclusive(receipt_temporary, receipt_bytes, mode=0o600)
        fault("after_receipt_prepare")
        fault("before_pin_prepare")
        _write_bytes_exclusive(pin_temporary, pin_bytes, mode=0o600)
        fault("after_pin_prepare")
        fault("before_receipt_chmod")
        os.chmod(receipt_temporary, 0o400)
        fault("after_receipt_chmod")
        fault("before_pin_chmod")
        os.chmod(pin_temporary, 0o400)
        fault("after_pin_chmod")
        fault("before_pin_link")
        os.link(pin_temporary, pin_path, follow_symlinks=False)
        published_pin = True
        fault("after_pin_link")
        fault("before_pin_directory_fsync")
        _fsync_directory(parent)
        fault("after_pin_directory_fsync")
        # A visible JSON is not committed while either prepared alias remains:
        # readers require both canonical files to have nlink=1.
        fault("before_receipt_link")
        os.link(receipt_temporary, receipt_path, follow_symlinks=False)
        published_receipt = True
        fault("after_receipt_link")
        fault("before_receipt_directory_fsync")
        _fsync_directory(parent)
        fault("after_receipt_directory_fsync")
        fault("before_final_receipt_verification")
        prepared_receipt = receipt_path.lstat()
        if (
            not stat.S_ISREG(prepared_receipt.st_mode)
            or stat.S_ISLNK(prepared_receipt.st_mode)
            or prepared_receipt.st_nlink != 2
            or stat.S_IMODE(prepared_receipt.st_mode) != 0o400
            or _sha256_file(receipt_path) != receipt_sha256
        ):
            raise RestoreAcceptanceError(
                "linked terminal receipt failed prepared-pair verification"
            )
        fault("after_final_receipt_verification")
        fault("before_final_pin_verification")
        prepared_pin = pin_path.lstat()
        if (
            not stat.S_ISREG(prepared_pin.st_mode)
            or stat.S_ISLNK(prepared_pin.st_mode)
            or prepared_pin.st_nlink != 2
            or stat.S_IMODE(prepared_pin.st_mode) != 0o400
            or _sha256_file(pin_path) != hashlib.sha256(pin_bytes).hexdigest()
        ):
            raise RestoreAcceptanceError(
                "linked terminal receipt pin failed prepared-pair verification"
            )
        fault("after_final_pin_verification")
        fault("before_receipt_temporary_unlink")
        receipt_temporary.unlink()
        fault("after_receipt_temporary_unlink")
        fault("before_pin_temporary_unlink")
        try:
            pin_temporary.unlink()
            # Keep the flag assignment inside the same exception boundary as
            # unlink.  A pending signal can be delivered after unlink(2)
            # returns but before the next Python bytecode executes.
            committed = True
        except BaseException as unlink_exc:
            # unlink(2) wrappers can mutate the directory and then raise.  Do
            # not retract an exactly reconciled committed pair.  If observation
            # cannot classify the result, the outer failure path removes the
            # canonical names before the synchronized reader is released.
            try:
                reconciled_receipt = receipt_path.lstat()
                reconciled_pin = pin_path.lstat()
                try:
                    receipt_temporary.lstat()
                    receipt_alias_absent = False
                except FileNotFoundError:
                    receipt_alias_absent = True
                try:
                    reconciled_pin_temporary = pin_temporary.lstat()
                    pin_alias_absent = False
                except FileNotFoundError:
                    reconciled_pin_temporary = None
                    pin_alias_absent = True
            except BaseException:
                committed = False
                raise RestoreAcceptanceError(
                    "terminal publication state is unknown after final alias unlink; "
                    "canonical success names must be retracted"
                ) from unlink_exc

            receipt_is_verified_inode = (
                reconciled_receipt.st_dev == prepared_receipt.st_dev
                and reconciled_receipt.st_ino == prepared_receipt.st_ino
                and stat.S_ISREG(reconciled_receipt.st_mode)
                and not stat.S_ISLNK(reconciled_receipt.st_mode)
                and stat.S_IMODE(reconciled_receipt.st_mode) == 0o400
            )
            pin_is_verified_inode = (
                reconciled_pin.st_dev == prepared_pin.st_dev
                and reconciled_pin.st_ino == prepared_pin.st_ino
                and stat.S_ISREG(reconciled_pin.st_mode)
                and not stat.S_ISLNK(reconciled_pin.st_mode)
                and stat.S_IMODE(reconciled_pin.st_mode) == 0o400
            )
            reconciled_committed = (
                receipt_is_verified_inode
                and pin_is_verified_inode
                and reconciled_receipt.st_nlink == 1
                and reconciled_pin.st_nlink == 1
                and receipt_alias_absent
                and pin_alias_absent
            )
            reconciled_precommit = (
                receipt_is_verified_inode
                and pin_is_verified_inode
                and reconciled_receipt.st_nlink == 1
                and reconciled_pin.st_nlink == 2
                and receipt_alias_absent
                and not pin_alias_absent
                and reconciled_pin_temporary is not None
                and reconciled_pin_temporary.st_dev == prepared_pin.st_dev
                and reconciled_pin_temporary.st_ino == prepared_pin.st_ino
                and reconciled_pin_temporary.st_nlink == 2
            )
            if reconciled_committed:
                committed = True
                post_commit_warning = (
                    f"{type(unlink_exc).__name__}: {unlink_exc}; "
                    "final alias unlink reconciled as committed"
                )
            elif reconciled_precommit:
                raise
            else:
                committed = False
                raise RestoreAcceptanceError(
                    "terminal publication state is unknown after final alias unlink; "
                    "canonical success names must be retracted"
                ) from unlink_exc
        # This is the earliest reader-visible commit state: both canonical
        # files now have the already-verified content/mode and nlink=1.
        fault("after_pin_temporary_unlink")
        fault("before_cleanup_directory_fsync")
        _fsync_directory(parent)
        fault("after_cleanup_directory_fsync")
    except BaseException as exc:
        if committed:
            post_commit_warning = f"{type(exc).__name__}: {exc}"
            try:
                _fsync_directory(parent)
            except BaseException:
                pass
        else:
            quarantine_errors: list[str] = []
            for path in (
                receipt_path if published_receipt else None,
                pin_path if published_pin else None,
                receipt_temporary,
                pin_temporary,
            ):
                if path is None:
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    quarantine = parent / f".{path.name}.{token}.unconfirmed"
                    try:
                        os.replace(path, quarantine)
                    except OSError as quarantine_exc:
                        quarantine_errors.append(
                            f"{path}: unlink={cleanup_exc}; quarantine={quarantine_exc}"
                        )
            try:
                _fsync_directory(parent)
            except OSError as cleanup_exc:
                quarantine_errors.append(f"directory fsync: {cleanup_exc}")
            visible = [
                str(path)
                for path in (receipt_path, pin_path)
                if path.exists() or path.is_symlink()
            ]
            if visible or quarantine_errors:
                raise RestoreAcceptanceError(
                    "failed publication could not retract canonical success outputs: "
                    f"visible={visible}, cleanup={quarantine_errors}"
                ) from exc
            raise
    result = {
        "receipt": str(receipt_path),
        "receipt_sha256": receipt_sha256,
        "pin": str(pin_path),
        "pin_sha256": hashlib.sha256(pin_bytes).hexdigest(),
        "mode": "0400",
        "publication": "pin-first-json-last-nlink1-commit",
    }
    if post_commit_warning is not None:
        result["post_commit_durability_warning"] = post_commit_warning
    return result


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RestoreAcceptanceError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RestoreAcceptanceError(f"{label} must be a JSON object")
    return value


def _public_raw_family_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    return {
        **{key: value for key, value in descriptor.items() if key != "roles"},
        "roles": {
            role: {key: value for key, value in record.items() if key != "fd"}
            for role, record in descriptor["roles"].items()
        },
    }


def _raw_descriptor_family_identity(descriptor: dict[str, Any]) -> dict[str, Any]:
    members: dict[str, dict[str, Any]] = {}
    for role in RAW_FAMILY_ROLES:
        record = descriptor["roles"][role]
        if record["state"] == "ABSENT":
            members[role] = {
                "state": "ABSENT",
                "canonical_path": record["path"],
                "no_symlink": True,
            }
        else:
            members[role] = {
                "state": "PRESENT",
                "canonical_path": record["path"],
                "device": record["device"],
                "inode": record["inode"],
                "mode": int(record["mode"], 8),
                "nlink": record["nlink"],
                "no_symlink": True,
            }
    return {
        "database": descriptor["roles"]["main"]["path"],
        "existence_set": sorted(
            role for role, record in members.items() if record["state"] == "PRESENT"
        ),
        "members": members,
    }


def _copy_inherited_raw_family(
    descriptor: dict[str, Any], destination_directory: Path
) -> dict[str, Any]:
    """Copy accepted bytes only through already-open read-only descriptors."""

    if descriptor.get("kind") != "pinned-read-only-sqlite-family":
        raise RestoreAcceptanceError("raw family descriptor kind is invalid")
    roles = descriptor.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(RAW_FAMILY_ROLES):
        raise RestoreAcceptanceError("raw family descriptor roles are invalid")
    if descriptor.get("source_reopen_allowed") is not False:
        raise RestoreAcceptanceError("raw family descriptor permits source reopening")
    destination_directory = _canonical_absolute(
        destination_directory, label="raw family copy destination"
    )
    if destination_directory.exists() or destination_directory.is_symlink():
        raise RestoreAcceptanceError(
            f"raw family copy destination must be absent: {destination_directory}"
        )
    if (
        not destination_directory.parent.is_dir()
        or destination_directory.parent.is_symlink()
    ):
        raise RestoreAcceptanceError(
            "raw family copy parent must be a real existing directory"
        )
    destination_directory.mkdir(mode=0o700)
    destination_paths = _family_paths(
        destination_directory / migration.COLD_BACKUP_FILE_NAMES["main"]
    )
    try:
        for role in RAW_FAMILY_ROLES:
            record = roles[role]
            if record.get("state") == "ABSENT":
                if record.get("fd") is not None:
                    raise RestoreAcceptanceError(
                        f"ABSENT raw family role has a descriptor: {role}"
                    )
                continue
            descriptor_fd = record.get("fd")
            if type(descriptor_fd) is not int or descriptor_fd < 0:
                raise RestoreAcceptanceError(
                    f"PRESENT raw family role lacks an inherited descriptor: {role}"
                )
            before = os.fstat(descriptor_fd)
            expected_identity = (
                record["device"],
                record["inode"],
                stat.S_IFREG | int(record["mode"], 8),
                record["nlink"],
                record["size"],
                record["mtime_ns"],
                record["ctime_ns"],
            )
            actual_identity = tuple(
                int(getattr(before, field))
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )
            if actual_identity != expected_identity:
                raise RestoreAcceptanceError(
                    f"inherited raw family descriptor identity changed: {role}"
                )
            destination = destination_paths[role]
            output = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            try:
                os.lseek(descriptor_fd, 0, os.SEEK_SET)
                while chunk := os.read(descriptor_fd, 1024 * 1024):
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(output, view)
                        if written <= 0:
                            raise OSError("short write while copying raw family")
                        view = view[written:]
                os.fchmod(output, int(record["mode"], 8))
                os.fsync(output)
            finally:
                os.close(output)
            after = os.fstat(descriptor_fd)
            after_identity = tuple(
                int(getattr(after, field))
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )
            if after_identity != expected_identity:
                raise RestoreAcceptanceError(
                    f"inherited raw family descriptor changed while copied: {role}"
                )
            if digest.hexdigest() != record["sha256"]:
                raise RestoreAcceptanceError(
                    f"inherited raw family descriptor SHA-256 changed: {role}"
                )
        _fsync_directory(destination_directory)
        _fsync_directory(destination_directory.parent)
        copied = migration._artifact_descriptor(destination_paths)
        for role in RAW_FAMILY_ROLES:
            source = roles[role]
            target = copied["files"][role]
            for key in ("state", "size", "sha256"):
                if source.get(key) != target.get(key):
                    raise RestoreAcceptanceError(
                        f"raw family copy differs from inherited descriptor: {role}.{key}"
                    )
            if source["state"] == "PRESENT" and int(source["mode"], 8) != target["mode"]:
                raise RestoreAcceptanceError(
                    f"raw family copy differs from inherited descriptor: {role}.mode"
                )
        return copied
    except Exception:
        raise


def _damage_rehearsal_target_for_observed_family(
    target_paths: dict[str, Path], backup_records: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    if any(backup_records[role]["state"] == "ABSENT" for role in ("wal", "shm")):
        return migration._damage_rehearsal_target(target_paths, backup_records)

    before = migration._database_family_fingerprints(target_paths)
    descriptor = os.open(
        target_paths["main"],
        os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise RestoreAcceptanceError(
                "rehearsal main target is not a singly linked regular file"
            )
        payload = b"agentstack-mail deliberate raw-family restore corruption\n" * 64
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while damaging rehearsal main database")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(target_paths["main"].parent)
    return {
        "plan": "truncate-main-v1-all-sidecars-present",
        "main_action": "truncate_and_replace_content",
        "created_absent_sidecars": [],
        "all_backup_sidecars_present": True,
        "observed_before_physical": before,
        "observed_after_physical": migration._database_family_fingerprints(
            target_paths
        ),
    }


def _restore_worker(
    *, raw_family_descriptor: dict[str, Any], run_directory: Path
) -> dict[str, Any]:
    run_directory = _canonical_absolute(run_directory, label="run directory")
    if not run_directory.is_dir() or run_directory.is_symlink():
        raise RestoreAcceptanceError("worker run directory must already exist")
    raw_root = run_directory / "raw"
    raw_root.mkdir(mode=0o700)
    inherited_descriptors = [
        record["fd"]
        for record in raw_family_descriptor["roles"].values()
        if record.get("fd") is not None
    ]
    try:
        raw_source = _copy_inherited_raw_family(
            raw_family_descriptor, raw_root / "accepted-source"
        )
    finally:
        _close_descriptors(inherited_descriptors)
    raw_source_paths = migration._artifact_paths(raw_source)
    input_logical = migration._logical_artifact_result(
        raw_source, scratch_parent=run_directory.parent
    )
    if input_logical.get("status") != "valid":
        raise RestoreAcceptanceError("accepted raw family is not logically valid")
    input_messages = _capture_message_window(raw_source_paths["main"])

    input_authority = run_directory / "input-authority"
    input_authority_artifact = migration._copy_database_family_artifact(
        raw_source_paths, input_authority
    )
    (input_authority / "archive").mkdir(mode=0o700)
    (input_authority / "signals").mkdir(mode=0o700)
    source_git = migration._create_baseline_git(
        input_authority / "archive",
        authority_state_sha256=input_logical["logical_sha256"],
        timestamp=_utc_now(),
        hook=None,
    )
    source_state = migration.StatePaths.from_root(input_authority)
    migration_generation = run_directory / "migration-generation"
    migration_result = migration.copy_state(source_state, migration_generation)
    migration_verification = migration.verify_copy(
        source_state, migration_generation
    )
    if migration_verification.get("status") != "verified":
        raise RestoreAcceptanceError("tool-generated migration manifest did not verify")
    migration_manifest = migration_generation / migration.MANIFEST_NAME

    backup_directory = run_directory / "generated-cold-backup"
    backup_result = migration.cold_backup_database(
        input_authority / "storage.sqlite3",
        backup_directory,
        services_stopped=True,
    )
    backup = migration._load_cold_backup_receipt(backup_directory)
    backup_receipt_path = backup_directory / migration.COLD_BACKUP_RECEIPT_NAME
    backup_paths = {
        role: backup_directory / record["backup_name"]
        for role, record in backup["files"].items()
    }
    backup_artifact = migration._artifact_descriptor(backup_paths)
    if backup.get("logical_snapshot") != input_logical.get("snapshot"):
        raise RestoreAcceptanceError(
            "tool-generated cold backup differs logically from accepted raw family"
        )

    target_directory = run_directory / "target"
    initial_target = migration._copy_database_family_artifact(
        backup_paths, target_directory
    )
    target_paths = migration._artifact_paths(initial_target)

    damage = _damage_rehearsal_target_for_observed_family(
        target_paths, backup["files"]
    )
    damaged_artifact = migration._copy_database_family_artifact(
        target_paths, raw_root / "damaged"
    )
    damaged_logical = migration._logical_artifact_result(
        damaged_artifact, scratch_parent=run_directory.parent
    )
    if damaged_logical.get("status") != "error" or "file is not a database" not in str(
        damaged_logical.get("error", "")
    ).lower():
        raise RestoreAcceptanceError(
            "deliberately damaged target did not fail with file is not a database"
        )

    restore_receipt_path = run_directory / "cold-restore-receipt.json"
    restore_result = migration.cold_restore_database(
        backup_directory,
        target_paths["main"],
        restore_receipt_path,
        migration_manifest,
        services_stopped=True,
        target_kind="rehearsal-copy",
        fault_injection=damage["plan"],
    )
    restore_receipt = _load_json(restore_receipt_path, label="cold restore receipt")
    if restore_receipt.get("target") != {
        "kind": "rehearsal-copy",
        "database": str(target_paths["main"]),
        "production_source": False,
    }:
        raise RestoreAcceptanceError("cold restore receipt target binding is invalid")
    restored_artifact = migration._copy_database_family_artifact(
        target_paths, raw_root / "restored"
    )
    restored_logical = migration._logical_artifact_result(
        restored_artifact, scratch_parent=run_directory.parent
    )
    restored_messages = _capture_message_window(target_paths["main"])
    if (
        restored_logical.get("status") != "valid"
        or restored_logical.get("snapshot") != input_logical.get("snapshot")
        or restored_messages["prefix_sha256"] != input_messages["prefix_sha256"]
        or restored_messages["max_id"] != input_messages["max_id"]
    ):
        raise RestoreAcceptanceError("restored target differs from the sealed backup")
    return {
        "raw_input": raw_source,
        "input_authority": {
            "database_artifact": input_authority_artifact,
            "tool_generated_source_git": source_git,
        },
        "migration": {
            "result": {
                "status": migration_result.status,
                "destination_root": migration_result.destination_root,
                "operation_id": migration_result.operation_id,
                "state_sha256": migration_result.state_sha256,
            },
            "verification": migration_verification,
            "manifest": str(migration_manifest),
            "manifest_sha256": _sha256_file(migration_manifest),
        },
        "backup": {
            "result": backup_result,
            "receipt": backup,
            "receipt_path": str(backup_receipt_path),
            "receipt_sha256": _sha256_file(backup_receipt_path),
            "artifact": backup_artifact,
        },
        "input": {
            "logical": input_logical,
            "messages": input_messages,
        },
        "damage": {
            "action": damage,
            "artifact": damaged_artifact,
            "logical": damaged_logical,
        },
        "restore": {
            "result": restore_result,
            "receipt": restore_receipt,
            "receipt_path": str(restore_receipt_path),
            "receipt_sha256": _sha256_file(restore_receipt_path),
            "artifact": restored_artifact,
            "logical": restored_logical,
            "messages": restored_messages,
        },
        "target_database": str(target_paths["main"]),
    }


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentstack-mail-restore-acceptance-worker")
    parser.add_argument("--raw-family-descriptor", required=True)
    parser.add_argument("--run-dir", required=True)
    return parser


def _worker_main(argv: Sequence[str]) -> None:
    args = _worker_parser().parse_args(argv)
    print(_WORKER_READY, flush=True)
    if sys.stdin.readline() != "GO\n":
        raise SystemExit(2)
    try:
        raw_family_descriptor = json.loads(args.raw_family_descriptor)
        if not isinstance(raw_family_descriptor, dict):
            raise RestoreAcceptanceError("raw family descriptor must be a JSON object")
        result = _restore_worker(
            raw_family_descriptor=raw_family_descriptor,
            run_directory=Path(args.run_dir),
        )
    except Exception as exc:
        print(f"restore acceptance worker: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    sys.stdout.buffer.write(_canonical_json(result))
    sys.stdout.flush()
    if sys.stdin.readline() != "ACK\n":
        raise SystemExit(2)


def _readline_with_deadline(stream: Any, timeout_seconds: float) -> str:
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        if not selector.select(timeout_seconds):
            raise RestoreAcceptanceError("worker handshake deadline expired")
        return stream.readline()
    finally:
        selector.close()


def _isolated_environment(env_file: Path | None = None) -> dict[str, str]:
    environment: dict[str, str] = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("HOME", "USER", "LOGNAME", "TMPDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    if env_file is not None:
        environment["AGENTSTACK_MAIL_ENV_FILE"] = str(env_file)
    return environment


def _run_restore_worker(
    *,
    raw_family_descriptor: dict[str, Any],
    raw_family_fds: Sequence[int],
    run_directory: Path,
    production_family: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    serialized_descriptor = json.dumps(
        raw_family_descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    command = [
        str(Path(sys.executable).absolute()),
        "-I",
        "-m",
        "agentstack_mail.restore_acceptance",
        "_worker",
        "--raw-family-descriptor",
        serialized_descriptor,
        "--run-dir",
        str(run_directory),
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=run_directory,
            env=_isolated_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            pass_fds=tuple(raw_family_fds),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RestoreAcceptanceError(
            f"restore worker could not start: {type(exc).__name__}: {exc}"
        ) from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    samples: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        ready = _readline_with_deadline(process.stdout, timeout_seconds).rstrip("\n")
        if ready != _WORKER_READY:
            stderr = process.stderr.read() if process.poll() is not None else ""
            raise RestoreAcceptanceError(
                "restore worker did not emit the exact ready marker: "
                f"observed={ready!r}, stderr={stderr.strip()!r}"
            )
        tree = _capture_process_tree(process.pid)
        lsof = _capture_tree_lsof(tree)
        boundary = _assert_tree_open_boundary(
            lsof,
            production_family=production_family,
            target_family=None,
            isolated_port=None,
            require_target=False,
        )
        samples.append({"phase": "ready", "tree": tree, "lsof": lsof, "gate": boundary})
        process.stdin.write("GO\n")
        process.stdin.flush()

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        result_line = ""
        try:
            while time.monotonic() - started < timeout_seconds:
                if selector.select(0.05):
                    result_line = process.stdout.readline()
                    break
                if process.poll() is not None:
                    break
                if len(samples) < 12:
                    tree = _capture_process_tree(process.pid)
                    lsof = _capture_tree_lsof(tree)
                    boundary = _assert_tree_open_boundary(
                        lsof,
                        production_family=production_family,
                        target_family=None,
                        isolated_port=None,
                        require_target=False,
                    )
                    samples.append(
                        {
                            "phase": "running",
                            "tree": tree,
                            "lsof": lsof,
                            "gate": boundary,
                        }
                    )
            else:
                raise RestoreAcceptanceError("restore worker deadline expired")
        finally:
            selector.close()
        if not result_line:
            stderr = process.stderr.read()
            raise RestoreAcceptanceError(
                f"restore worker exited without a result: {stderr.strip()}"
            )
        try:
            result = json.loads(result_line)
        except ValueError as exc:
            raise RestoreAcceptanceError("restore worker result is not JSON") from exc
        if not isinstance(result, dict):
            raise RestoreAcceptanceError("restore worker result is not an object")

        tree = _capture_process_tree(process.pid)
        lsof = _capture_tree_lsof(tree)
        boundary = _assert_tree_open_boundary(
            lsof,
            production_family=production_family,
            target_family=None,
            isolated_port=None,
            require_target=False,
        )
        samples.append(
            {"phase": "completed-awaiting-ack", "tree": tree, "lsof": lsof, "gate": boundary}
        )
        process.stdin.write("ACK\n")
        process.stdin.flush()
        try:
            _stdout_tail, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise RestoreAcceptanceError(
                "restore worker did not exit after the ACK deadline"
            ) from exc
        if process.returncode != 0:
            raise RestoreAcceptanceError(
                f"restore worker failed with rc={process.returncode}: {stderr.strip()}"
            )
        return {
            "command": command,
            "pid": process.pid,
            "exit_code": process.returncode,
            "bounded_ms": round((time.monotonic() - started) * 1000, 3),
            "deadline_seconds": timeout_seconds,
            "stderr": stderr,
            "process_tree_open_file_samples": samples,
            "result": result,
        }
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def _write_server_env(path: Path, state_root: Path, port: int) -> str:
    database_url = f"sqlite+aiosqlite:///{state_root / 'storage.sqlite3'}"
    values = (
        "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE=passthrough",
        "AGENTSTACK_MAIL_HTTP_HOST=127.0.0.1",
        f"AGENTSTACK_MAIL_HTTP_PORT={port}",
        "AGENTSTACK_MAIL_HTTP_PATH=/api/",
        f"AGENTSTACK_MAIL_DATABASE_URL={database_url}",
        f"AGENTSTACK_MAIL_STORAGE_ROOT={state_root / 'archive'}",
        f"AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR={state_root / 'signals'}",
        "AGENTSTACK_MAIL_LOG_RICH_ENABLED=false",
        "AGENTSTACK_MAIL_LOG_JSON_ENABLED=false",
    )
    _write_bytes_exclusive(path, ("\n".join(values) + "\n").encode(), mode=0o600)
    return database_url


def _port_accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.15)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _wait_closed(port: int, *, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    while time.monotonic() - started < timeout_seconds:
        if not _port_accepts(port):
            return {
                "status": "closed",
                "bounded_closed_ms": round((time.monotonic() - started) * 1000, 3),
                "deadline_seconds": timeout_seconds,
            }
        time.sleep(0.05)
    raise RestoreAcceptanceError("isolated endpoint remained reachable after shutdown")


def _remaining_processes(
    pids: Sequence[int],
    pgid: int,
    *,
    timeout_seconds: float = 10,
) -> list[dict[str, Any]]:
    records, _raw = _all_process_records(timeout_seconds=timeout_seconds)
    expected = set(pids)
    return [
        record
        for record in records
        if record["pid"] in expected or record["pgid"] == pgid
    ]


def _shutdown_candidate_server(
    process: subprocess.Popen[Any],
    *,
    port: int,
    process_ids: Sequence[int],
    process_group: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Require exit, endpoint close, and descendant retirement on one deadline."""

    shutdown_started = time.monotonic()
    shutdown_deadline = shutdown_started + timeout_seconds

    def remaining_budget() -> float:
        remaining = shutdown_deadline - time.monotonic()
        if remaining <= 0:
            raise RestoreAcceptanceError(
                "candidate shutdown exceeded the shared deadline"
            )
        return remaining

    process.send_signal(signal.SIGTERM)
    try:
        exit_code = process.wait(timeout=remaining_budget())
    except subprocess.TimeoutExpired as exc:
        raise RestoreAcceptanceError(
            "candidate server did not terminate within the deadline"
        ) from exc
    closed = _wait_closed(port, timeout_seconds=remaining_budget())
    remaining = _remaining_processes(
        process_ids,
        process_group,
        timeout_seconds=remaining_budget(),
    )
    bounded_shutdown_ms = round(
        (time.monotonic() - shutdown_started) * 1000,
        3,
    )
    if bounded_shutdown_ms > timeout_seconds * 1000:
        raise RestoreAcceptanceError(
            "candidate shutdown exceeded the shared deadline"
        )
    if exit_code != 0 or remaining:
        raise RestoreAcceptanceError(
            f"candidate shutdown failed: rc={exit_code}, remaining={remaining}"
        )
    return {
        "signal": "SIGTERM",
        "exit_code": exit_code,
        "endpoint": closed,
        "remaining_processes": remaining,
        "deadline_seconds": timeout_seconds,
        "bounded_shutdown_ms": bounded_shutdown_ms,
    }


def _run_candidate_server(
    *,
    run_directory: Path,
    target_database: Path,
    production_family: dict[str, Any],
    expected_logical: dict[str, Any],
    expected_messages: dict[str, Any],
    port: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    state_root = target_database.parent
    (state_root / "archive").mkdir(mode=0o700)
    (state_root / "signals").mkdir(mode=0o700)
    env_file = run_directory / "server.env"
    database_url = _write_server_env(env_file, state_root, port)
    stdout_path = run_directory / "candidate-server.stdout.log"
    stderr_path = run_directory / "candidate-server.stderr.log"
    stdout_stream = stdout_path.open("xb")
    stderr_stream = stderr_path.open("xb")
    command = [
        str(Path(sys.executable).absolute()),
        "-I",
        "-m",
        "agentstack_mail.cli",
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=run_directory,
            env=_isolated_environment(env_file),
            stdout=stdout_stream,
            stderr=stderr_stream,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stdout_stream.close()
        stderr_stream.close()
        os.chmod(stdout_path, 0o400)
        os.chmod(stderr_path, 0o400)
        raise RestoreAcceptanceError(
            f"candidate server could not start: {type(exc).__name__}: {exc}"
        ) from exc
    started = time.monotonic()
    completed = False
    try:
        url = f"http://127.0.0.1:{port}/api/"
        readiness = _wait_read_only_ready(
            process,
            url=url,
            expected_database_url=database_url,
            port=port,
            timeout_seconds=timeout_seconds,
        )
        tree = _capture_process_tree(process.pid)
        listener = _listener_observation(port)
        if listener["pids"] != [process.pid]:
            raise RestoreAcceptanceError(
                "isolated listener is not owned by the exact candidate server PID"
            )
        target_family = _family_identity(target_database, label="runtime target")
        lsof = _capture_tree_lsof(tree)
        open_boundary = _assert_tree_open_boundary(
            lsof,
            production_family=production_family,
            target_family=target_family,
            isolated_port=port,
            require_target=True,
        )
        runtime_logical = migration.snapshot_database(target_database)
        runtime_messages = _capture_message_window(target_database)
        if runtime_logical != expected_logical:
            raise RestoreAcceptanceError(
                "candidate runtime full logical snapshot differs from restored target"
            )
        if (
            runtime_messages["max_id"] != expected_messages["max_id"]
            or runtime_messages["count"] != expected_messages["count"]
            or runtime_messages["prefix_sha256"]
            != expected_messages["prefix_sha256"]
        ):
            raise RestoreAcceptanceError(
                "candidate runtime watermark differs from restored target"
            )

        shutdown = _shutdown_candidate_server(
            process,
            port=port,
            process_ids=tree["pids"],
            process_group=tree["root_pgid"],
            timeout_seconds=timeout_seconds,
        )
        completed = True
        return {
            "command": command,
            "pid": process.pid,
            "process_tree": tree,
            "readiness": readiness,
            "listener": listener,
            "target_family": target_family,
            "lsof": lsof,
            "open_boundary": open_boundary,
            "runtime_logical": runtime_logical,
            "runtime_messages": runtime_messages,
            "shutdown": {
                **shutdown,
                "bounded_total_ms": round((time.monotonic() - started) * 1000, 3),
            },
        }
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stdout_stream.close()
        stderr_stream.close()
        for path in (stdout_path, stderr_path):
            os.chmod(path, 0o400)
        if not completed:
            try:
                _wait_closed(port, timeout_seconds=5)
            except RestoreAcceptanceError:
                pass


def _file_identity(path: Path, *, label: str) -> dict[str, Any]:
    path = _canonical_absolute(path, label=label)
    if not path.is_file() or path.is_symlink():
        raise RestoreAcceptanceError(f"{label} must be a regular file")
    info = path.lstat()
    if info.st_nlink != 1:
        raise RestoreAcceptanceError(f"{label} must have nlink=1")
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "size": info.st_size,
        "mode": stat.S_IMODE(info.st_mode),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "nlink": int(info.st_nlink),
        "no_symlink": True,
    }


def _require_sha256(value: str, *, label: str) -> str:
    if len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise RestoreAcceptanceError(f"{label} must be one lowercase SHA-256")
    return value


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def run_restore_acceptance(
    *,
    run_directory: Path,
    receipt_path: Path,
    pin_path: Path,
    wheel: Path,
    candidate_repository: Path,
    candidate_commit: str,
    backup_main: Path,
    backup_main_state: str,
    backup_main_sha256: str,
    backup_main_size: int,
    backup_main_mode: str,
    backup_wal: Path,
    backup_wal_state: str,
    backup_wal_sha256: str,
    backup_wal_size: int,
    backup_wal_mode: str,
    backup_shm: Path,
    backup_shm_state: str,
    backup_shm_sha256: str,
    backup_shm_size: int,
    backup_shm_mode: str,
    production_database: Path,
    expected_logical_sha256: str,
    expected_message_max_id: int,
    expected_message_sha256: str,
    port: int,
    timeout_seconds: float = 20,
    worker_timeout_seconds: float = 120,
) -> dict[str, Any]:
    from . import evidence as evidence_runtime

    if not 1 <= timeout_seconds <= 120:
        raise RestoreAcceptanceError("timeout_seconds must be in [1, 120]")
    if not 1 <= worker_timeout_seconds <= 300:
        raise RestoreAcceptanceError("worker_timeout_seconds must be in [1, 300]")
    if port == PRODUCTION_PORT or not 1 <= port <= 65535:
        raise RestoreAcceptanceError("isolated port is invalid or equals production 8765")
    if _port_accepts(port):
        raise RestoreAcceptanceError(f"isolated port is already accepting: {port}")
    expected_hashes = {
        "logical": _require_sha256(
            expected_logical_sha256, label="expected logical SHA-256"
        ),
        "messages": _require_sha256(
            expected_message_sha256, label="expected message SHA-256"
        ),
    }
    if expected_message_max_id < 0:
        raise RestoreAcceptanceError("expected message max id cannot be negative")

    run_directory = _canonical_absolute(run_directory, label="run directory")
    receipt_path = _canonical_absolute(receipt_path, label="terminal receipt")
    pin_path = _canonical_absolute(pin_path, label="terminal receipt pin")
    wheel = _canonical_absolute(wheel, label="wheel")
    candidate_repository = _canonical_absolute(
        candidate_repository, label="candidate repository"
    )
    production_database = _canonical_absolute(
        production_database, label="production database"
    )
    raw_pins = {
        "main": _raw_family_pin(
            path=backup_main,
            state=backup_main_state,
            sha256=backup_main_sha256,
            size=backup_main_size,
            mode=backup_main_mode,
        ),
        "wal": _raw_family_pin(
            path=backup_wal,
            state=backup_wal_state,
            sha256=backup_wal_sha256,
            size=backup_wal_size,
            mode=backup_wal_mode,
        ),
        "shm": _raw_family_pin(
            path=backup_shm,
            state=backup_shm_state,
            sha256=backup_shm_sha256,
            size=backup_shm_size,
            mode=backup_shm_mode,
        ),
    }
    if run_directory.exists() or run_directory.is_symlink():
        raise RestoreAcceptanceError(f"run directory must be absent: {run_directory}")
    if not run_directory.parent.is_dir() or run_directory.parent.is_symlink():
        raise RestoreAcceptanceError("run directory parent must be a real directory")
    for output in (receipt_path, pin_path):
        if output.exists() or output.is_symlink():
            raise RestoreAcceptanceError(f"terminal output must be absent: {output}")
    protected_roots = {
        production_database.parent,
        candidate_repository,
        *(Path(record["path"]).parent for record in raw_pins.values()),
    }
    if any(_path_within(run_directory, root) for root in protected_roots):
        raise RestoreAcceptanceError("run directory overlaps a protected authority root")
    if any(
        _path_within(output, root)
        for root in protected_roots
        for output in (receipt_path, pin_path)
    ):
        raise RestoreAcceptanceError("terminal output overlaps a protected authority root")

    candidate = evidence_runtime._candidate_identity(
        candidate_repository, candidate_commit
    )
    installed_wheel = evidence_runtime._verify_running_from_wheel(
        wheel,
        candidate_repository=candidate_repository,
        candidate_commit=candidate_commit,
    )
    raw_family_descriptor, raw_family_fds = _open_pinned_raw_family(raw_pins)
    inputs = {"raw_family": _public_raw_family_descriptor(raw_family_descriptor)}

    observer = {
        "pid": os.getpid(),
        "role": "read-only-production-observer-and-terminal-publisher",
        "excluded_from_sampled_rehearsal_process_tree_observation": True,
        "production_write_claim_scope": (
            "SQL writes to the production main database only; the -wal and -shm "
            "sidecars are explicitly OUT of scope, because a mode=ro open is "
            "itself allowed to create or rewrite the -shm in the production "
            "directory in order to recover the WAL index (measured 2026-08-12: "
            "with a dirty -wal and no -shm, a mode=ro open succeeds and creates "
            "the -shm when the directory is writable, and fails when it is "
            "not). production.invariants therefore constrain the main database "
            "and its message window, not the sidecar bytes. No write counter is "
            "instrumented; the claim is bounded by "
            "production.before.messages.open_mode / production.after.messages."
            "open_mode (SQLite mode=ro URI plus query_only read back from the "
            "open connection) and by production.invariants over the "
            "before/after window"
        ),
        "production_network_claim_scope": (
            "no request counter is instrumented; the observer reaches the "
            "production port only through the lsof listener table "
            "(production.before.listener.method), and the sampled rehearsal "
            "process tree rejects any connection to it. That process-tree "
            "observation does NOT cover the observer process itself, which this "
            "same record excludes from it: for the observer, the only support "
            "for the no-connection claim is static — one client construction "
            "site, and a port argument that is rejected when it equals the "
            "production port"
        ),
        "open_file_claim_scope": "sampled process-tree lsof observations only",
    }
    try:
        production_before_family = _family_identity(
            production_database, label="production before"
        )
        production_before_messages = _capture_message_window(production_database)
        production_listener_before = _listener_observation(PRODUCTION_PORT)
        if len(production_listener_before["pids"]) != 1:
            raise RestoreAcceptanceError(
                "production listener must have exactly one owner before rehearsal"
            )

        started_at = _utc_now()
        run_directory.mkdir(mode=0o700)
        _fsync_directory(run_directory.parent)
        worker = _run_restore_worker(
            raw_family_descriptor=raw_family_descriptor,
            raw_family_fds=raw_family_fds,
            run_directory=run_directory,
            production_family=production_before_family,
            timeout_seconds=worker_timeout_seconds,
        )
        worker_result = worker["result"]
        target_database = Path(worker_result["target_database"])
        target_family = _family_identity(target_database, label="restored target")
        generated_backup_database = Path(
            worker_result["backup"]["artifact"]["files"]["main"]["path"]
        )
        backup_family = _family_identity(
            generated_backup_database, label="tool-generated cold backup"
        )
        accepted_raw_family = _raw_descriptor_family_identity(
            raw_family_descriptor
        )
        non_alias = _assert_non_alias(
            {
                "production": production_before_family,
                "accepted_raw": accepted_raw_family,
                "generated_backup": backup_family,
                "target": target_family,
            }
        )
        input_messages = worker_result["input"]["messages"]
        input_logical = worker_result["input"]["logical"]
        if (
            input_messages["max_id"] != expected_message_max_id
            or input_messages["prefix_sha256"] != expected_hashes["messages"]
            or input_logical.get("logical_sha256") != expected_hashes["logical"]
        ):
            raise RestoreAcceptanceError(
                "accepted raw family message watermark or logical SHA-256 is unexpected"
            )
        restored = worker_result["restore"]
        if (
            restored["logical"].get("logical_sha256") != expected_hashes["logical"]
            or restored["messages"]["max_id"] != expected_message_max_id
            or restored["messages"]["prefix_sha256"] != expected_hashes["messages"]
        ):
            raise RestoreAcceptanceError(
                "restored target watermark or logical SHA-256 is unexpected"
            )

        server = _run_candidate_server(
            run_directory=run_directory,
            target_database=target_database,
            production_family=production_before_family,
            expected_logical=restored["logical"]["snapshot"],
            expected_messages=restored["messages"],
            port=port,
            timeout_seconds=timeout_seconds,
        )
        production_after_messages = _capture_message_window(
            production_database,
            prefix_max_id=production_before_messages["max_id"],
        )
        production_after_family = _family_identity(
            production_database, label="production after"
        )
        production_listener_after = _listener_observation(PRODUCTION_PORT)
        if (
            production_listener_after["pids"]
            != production_listener_before["pids"]
        ):
            raise RestoreAcceptanceError(
                "production listener owner changed during the rehearsal"
            )
        production_evaluation = _evaluate_production_invariants(
            production_before_family,
            production_after_family,
            production_before_messages,
            production_after_messages,
        )
        gates = {
            "candidate_wheel_bound": True,
            "raw_family_mandatory_pins_bound": True,
            "manifest_and_cold_receipt_generated_by_migration_tool": True,
            "database_families_non_alias": True,
            "production_family_and_message_window_unchanged_except_append": True,
            "production_listener_unchanged_and_no_sampled_contact": True,
            "restore_process_tree_sampled_production_open_absent": True,
            "damage_non_noop_and_restore_exact": True,
            "candidate_target_open_positive_and_sampled_production_open_absent": True,
            "read_only_24_tool_api_path_database_ready": True,
            "runtime_logical_and_watermark_exact": True,
            "sigterm_rc0_port_closed_descendants_zero": True,
        }
        if not all(gates.values()):  # pragma: no cover - values are explicit
            raise RestoreAcceptanceError("one or more terminal gates are false")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "run_id": str(uuid.uuid4()),
            "started_at": started_at,
            "completed_at": _utc_now(),
            "status": "passed",
            "candidate_commit": candidate_commit,
            "candidate_checkout": candidate,
            "wheel": installed_wheel,
            "observer": observer,
            "publication_contract": {
                "reader_synchronization": (
                    "evaluate canonical receipt and pin only after the publisher "
                    "command completes"
                ),
                "reader_commit_predicates": [
                    "receipt_and_external_pin_both_exist",
                    "both_regular_nlink_1_mode_0400",
                    "external_pin_matches_receipt_sha256_and_name",
                ],
                "commit_transition": "second_prepared_alias_unlink_returns",
                "post_commit_directory_fsync": "best_effort_durability_only",
                "known_limitation": (
                    "a directory fsync failure after the nlink-1 transition cannot "
                    "be represented as pre-commit without a third reader-visible marker"
                ),
            },
            "fixed_inputs": inputs,
            "expected": {
                "sha256": expected_hashes,
                "message_max_id": expected_message_max_id,
            },
            "message_contract": {
                "columns": list(MESSAGE_COLUMNS),
                "canonicalization": MESSAGE_CANONICALIZATION,
            },
            "deadlines": {
                "restore_worker_seconds": worker_timeout_seconds,
                "candidate_runtime_seconds": timeout_seconds,
            },
            "non_alias": non_alias,
            "production": {
                "before": {
                    "family": production_before_family,
                    "messages": production_before_messages,
                    "listener": production_listener_before,
                },
                "after": {
                    "family": production_after_family,
                    "messages": production_after_messages,
                    "listener": production_listener_after,
                },
                "invariants": production_evaluation["invariants"],
                "observations": production_evaluation["observations"],
            },
            "restore_worker": worker,
            "candidate_server": server,
            "gates": gates,
        }
        publication = _publish_write_once_pair(receipt_path, pin_path, payload)
        return {
            "status": "completed",
            "candidate_commit": candidate_commit,
            **publication,
        }
    except Exception:
        if receipt_path.exists() or receipt_path.is_symlink():
            raise AssertionError(
                "failed restore acceptance unexpectedly published a terminal receipt"
            )
        raise
    finally:
        _close_descriptors(raw_family_fds)


def add_evidence_subcommand(subparsers: Any) -> None:
    parser = subparsers.add_parser("restore-rehearsal")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--receipt-sha256", required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--candidate-repo", required=True)
    parser.add_argument("--candidate-commit", required=True)
    for role in RAW_FAMILY_ROLES:
        option = role.replace("_", "-")
        parser.add_argument(f"--backup-{option}", required=True)
        parser.add_argument(
            f"--backup-{option}-state",
            choices=("PRESENT", "ABSENT"),
            required=True,
        )
        parser.add_argument(f"--backup-{option}-sha256", required=True)
        parser.add_argument(f"--backup-{option}-size", type=int, required=True)
        parser.add_argument(f"--backup-{option}-mode", required=True)
    parser.add_argument("--production-db", required=True)
    parser.add_argument("--expected-logical-sha256", required=True)
    parser.add_argument("--expected-message-max-id", type=int, required=True)
    parser.add_argument("--expected-message-sha256", required=True)
    parser.add_argument("--port", type=int, default=28770)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--worker-timeout-seconds", type=float, default=120)


def run_from_evidence_args(args: argparse.Namespace) -> dict[str, Any]:
    return run_restore_acceptance(
        run_directory=Path(args.run_dir),
        receipt_path=Path(args.receipt),
        pin_path=Path(args.receipt_sha256),
        wheel=Path(args.wheel),
        candidate_repository=Path(args.candidate_repo),
        candidate_commit=args.candidate_commit,
        backup_main=Path(args.backup_main),
        backup_main_state=args.backup_main_state,
        backup_main_sha256=args.backup_main_sha256,
        backup_main_size=args.backup_main_size,
        backup_main_mode=args.backup_main_mode,
        backup_wal=Path(args.backup_wal),
        backup_wal_state=args.backup_wal_state,
        backup_wal_sha256=args.backup_wal_sha256,
        backup_wal_size=args.backup_wal_size,
        backup_wal_mode=args.backup_wal_mode,
        backup_shm=Path(args.backup_shm),
        backup_shm_state=args.backup_shm_state,
        backup_shm_sha256=args.backup_shm_sha256,
        backup_shm_size=args.backup_shm_size,
        backup_shm_mode=args.backup_shm_mode,
        production_database=Path(args.production_db),
        expected_logical_sha256=args.expected_logical_sha256,
        expected_message_max_id=args.expected_message_max_id,
        expected_message_sha256=args.expected_message_sha256,
        port=args.port,
        timeout_seconds=args.timeout_seconds,
        worker_timeout_seconds=args.worker_timeout_seconds,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "_worker":
        raise SystemExit("restore_acceptance is an internal evidence worker")
    _worker_main(sys.argv[2:])
