#!/usr/bin/env python3
"""Hermetic coexistence, migration, rollback, and fault cutover gates.

This executable intentionally reconstructs the frozen live authority from the
operator-supplied provenance bundle.  It never consults a developer checkout, never
uses a production port, and keeps every writable surface below one disposable
work root.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
TESTS_ROOT = PACKAGE_ROOT / "tests"
CORE_SOURCE = PACKAGE_ROOT / "src"
SERVICE_WORKER = Path(__file__).with_name("cutover_service_worker.py")
DECISION_LEDGER = PACKAGE_ROOT / "fixtures" / "differential-expected-divergences-v2.json"
FORBIDDEN_PORTS = frozenset({7333, 8765, 8770})
MCP_PATH = "/mcp"
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_CUTOVER_STATES = {
    **{f"D{index}": "go" for index in range(1, 7)},
    "D7": "no_go",
    **{f"D{index}": "go" for index in range(8, 13)},
}
EXPECTED_REQUIRED_CONDITION_IDS = (
    "product-decisions-selected",
    "pre-cutover-product-decisions-implemented",
    "initial-cutover-difference-set-exact",
    "candidate-source-bound",
    "product-decision-cutover-approval",
    "selected-behavior-release-gate",
    "distribution-artifact-release-gate",
    "reservation-probe-safety-release-gate",
    "http-cli-transport-entrypoints",
    "service-lifecycle-supervision",
    "mcp-client-reregistration-cutover",
)
EXPECTED_DESCOPED_CONDITION_IDS = (
    "data-migration-reconciliation",
    "rollback-revert-procedure",
    "notification-layout-consumer-compatibility",
)
EXPECTED_FOLLOW_UP_TASK_STATES = {
    "reservation-probe-safety-release-gate": "implemented",
    "http-cli-transport-entrypoints": "not_implemented",
    "service-lifecycle-supervision": "not_implemented",
    "mcp-client-reregistration-cutover": "not_implemented",
    **{
        condition_id: "descoped_documentation_only"
        for condition_id in EXPECTED_DESCOPED_CONDITION_IDS
    },
}
EXPECTED_DESCOPED_TASK_METADATA = {
    "data-migration-reconciliation": {
        "date": "2026-08-15",
        "approved_by": "maintainer",
        "disposition": (
            "manual procedure documented for the minority of testers with existing "
            "upstream data; not part of installer or cutover gate"
        ),
    },
    "rollback-revert-procedure": {
        "date": "2026-08-15",
        "approved_by": "maintainer",
        "disposition": (
            "documented one-line rollback: AGENTSTACK_MAIL_PROVIDER=upstream "
            "re-run of install.sh"
        ),
    },
    "notification-layout-consumer-compatibility": {
        "date": "2026-08-15",
        "approved_by": "maintainer",
        "disposition": (
            "one-time verification that the shipped watcher reads per-message "
            "signal layout; both layouts already supported"
        ),
    },
}
EXPECTED_CUTOVER_APPROVAL = {
    "approved_by": "maintainer",
    "approved_date": "2026-08-15",
    "channel": "direct chat instruction to ProOpus",
    "scope": "D1-D6 and D8-D12 cutover_state set to go; D7 remains deferred no_go",
    "decision_note": "vault:09_MCP/mcp-agent-mail/DECISION_cutover承認3点とD7.md",
    "descope": {
        "removed_required_condition_ids": list(EXPECTED_DESCOPED_CONDITION_IDS),
        "rationale": (
            "public release targets fresh tester installs with no legacy data or "
            "prior configuration; migration stays available as a documented manual "
            "procedure (migration.py, proven in the 2026-08-12 live cutover), "
            "rollback is documented as AGENTSTACK_MAIL_PROVIDER=upstream re-run, "
            "notification layout compatibility is a one-time verification that the "
            "shipped watcher reads the per-message layout"
        ),
    },
}

for import_root in (str(TESTS_ROOT), str(CORE_SOURCE)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

from agentstack_mail.migration import (  # noqa: E402
    MANIFEST_NAME,
    StatePaths,
    assess_rollback,
    copy_state,
    snapshot_database,
    snapshot_state,
    verify_copy,
)
from differential_source import (
    LiveBaselineUnavailable,  # noqa: E402
    CORE_NAMESPACE,
    LIVE_NAMESPACE,
    WorkerStateRoots,
    isolated_worker_env,
    reconstruct_live,
)


class GateFailure(RuntimeError):
    """A cutover invariant was not demonstrated."""


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json", by_alias=True, exclude_none=True))
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return repr(value)


def parse_mcp_response_body(body: str) -> dict[str, Any]:
    """Parse an MCP result body without substring-based error detection."""

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GateFailure("MCP result body is not JSON") from exc
    if not isinstance(parsed, dict) or type(parsed.get("isError")) is not bool:
        raise GateFailure("MCP result body lacks a boolean isError field")
    return parsed


async def _call_tool_async(url: str, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from fastmcp import Client

    async with Client(url) as client:
        result = await client.call_tool(
            tool_name,
            dict(arguments),
            raise_on_error=False,
        )
    structured = result.structured_content
    data = structured if structured is not None else result.data
    body = json.dumps(
        {
            "isError": bool(result.is_error),
            "structuredContent": _jsonable(structured),
            "data": _jsonable(data),
            "content": _jsonable(result.content),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return parse_mcp_response_body(body)


def _call_tool(url: str, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return asyncio.run(_call_tool_async(url, tool_name, arguments))


def _require_success(result: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if result.get("isError") is not False:
        raise GateFailure(f"{label} unexpectedly returned isError=true")
    payload = result.get("structuredContent")
    if payload is None:
        payload = result.get("data")
    return payload if isinstance(payload, Mapping) else {"result": payload}


def _activate_error_detector(url: str, missing_project: Path) -> dict[str, int]:
    """Prove both error and success classifications before measurements."""

    deadline = time.monotonic() + 20.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            deliberate_failure = _call_tool(
                url,
                "whois",
                {
                    "project_key": str(missing_project),
                    "agent_name": "MissingAgent",
                    "format": "json",
                },
            )
            if deliberate_failure["isError"] is not True:
                raise GateFailure("deliberate failing call was classified as success")
            break
        except GateFailure:
            raise
        except Exception as exc:  # server may have bound before MCP lifespan is ready
            last_error = exc
            time.sleep(0.05)
    else:
        raise GateFailure(f"MCP detector activation timed out: {last_error}")

    success = _call_tool(url, "health_check", {"format": "json"})
    _require_success(success, "health_check detector control")
    return {"deliberate_errors_detected": 1, "successes_detected": 1}


def _pick_ephemeral_port() -> int:
    for _ in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port not in FORBIDDEN_PORTS:
            return port
    raise GateFailure("could not allocate a non-production ephemeral port")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


@dataclass
class MailService:
    namespace: str
    source_root: Path
    state_root: Path
    port: int | None = None
    process: subprocess.Popen[bytes] | None = None
    _log_handle: Any = None

    @property
    def paths(self) -> StatePaths:
        return StatePaths.from_root(self.state_root)

    @property
    def url(self) -> str:
        if self.port is None:
            raise GateFailure("service has no allocated port")
        return f"http://127.0.0.1:{self.port}{MCP_PATH}"

    def _roots(self) -> WorkerStateRoots:
        return WorkerStateRoots(
            home=self.state_root / "home",
            database=self.paths.database,
            storage=self.paths.archive,
            signals=self.paths.signals,
            temp=self.state_root / "tmp",
            cwd=self.state_root / "cwd",
            pythonpath=(self.source_root.resolve(strict=True),),
        )

    def start(self) -> None:
        if self.process is not None:
            raise GateFailure("service is already started")
        if self.port is None:
            self.port = _pick_ephemeral_port()
        if self.port in FORBIDDEN_PORTS:
            raise GateFailure(f"refusing production-reserved port {self.port}")
        if _port_open(self.port):
            raise GateFailure(f"selected ephemeral port is already in use: {self.port}")

        roots = self._roots()
        environment = isolated_worker_env(os.environ, self.namespace, roots)
        if self.namespace == LIVE_NAMESPACE:
            environment["HTTP_PORT"] = str(self.port)
        else:
            environment["AGENTSTACK_MAIL_HTTP_PORT"] = str(self.port)
            environment["AGENTSTACK_MAIL_ARCHIVE_COMMIT_ASYNC"] = "false"
        log_path = self.state_root / "service.log"
        self._log_handle = log_path.open("wb")
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(SERVICE_WORKER),
                "--namespace",
                self.namespace,
                "--source-root",
                str(self.source_root.resolve(strict=True)),
                "--port",
                str(self.port),
            ],
            cwd=roots.cwd,
            env=environment,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._close_log()
                diagnostic = log_path.read_text(encoding="utf-8", errors="replace")[-5000:]
                raise GateFailure(
                    f"{self.namespace} service exited {self.process.returncode}: {diagnostic}"
                )
            if _port_open(self.port):
                return
            time.sleep(0.05)
        self.stop()
        raise GateFailure(f"{self.namespace} did not bind ephemeral port {self.port}")

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        finally:
            self._close_log()
            self.process = None
        if self.port is not None:
            deadline = time.monotonic() + 5.0
            while _port_open(self.port) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _port_open(self.port):
                raise GateFailure(f"service did not release ephemeral port {self.port}")


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.absolute()
    second = second.absolute()
    return first == second or first in second.parents or second in first.parents


def _validate_authority_separation(first: StatePaths, second: StatePaths) -> None:
    pairs = (
        ("database", first.database, second.database),
        ("archive", first.archive, second.archive),
        ("signals", first.signals, second.signals),
    )
    for label, left, right in pairs:
        if _paths_overlap(left, right):
            raise GateFailure(f"authorities share or overlap writable {label}: {left} / {right}")
        if left.exists() and right.exists() and os.path.samefile(left, right):
            raise GateFailure(f"authorities alias the same writable {label} inode")


def _snapshot(paths: StatePaths, *, require_baseline_git: bool = False) -> dict[str, Any]:
    return snapshot_state(paths, require_baseline_git=require_baseline_git)


def _tree_file_projection(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    projection: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GateFailure(f"authority tree contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if path.name == ".archive.lock" or path.name.endswith(".lock"):
            continue
        payload = path.read_bytes()
        projection[relative] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return projection


def _coexistence_snapshot(paths: StatePaths) -> dict[str, Any]:
    """Observe authority state even before its archive has its first Git write."""

    database = snapshot_database(paths.database)
    comparable = {
        "database": database,
        "archive": _tree_file_projection(paths.archive),
        "signals": _tree_file_projection(paths.signals),
    }
    digest = hashlib.sha256(
        json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**comparable, "snapshot_sha256": digest}


def _require_same_snapshot(before: Mapping[str, Any], after: Mapping[str, Any], label: str) -> None:
    if before.get("snapshot_sha256") != after.get("snapshot_sha256"):
        raise GateFailure(f"{label} was not a write-free identity operation")


def _table_counts(snapshot: Mapping[str, Any]) -> dict[str, int]:
    database = snapshot["database"]
    return {
        str(name): int(state["count"])
        for name, state in database["tables"].items()
    }


def _total_rows(snapshot: Mapping[str, Any]) -> int:
    return sum(_table_counts(snapshot).values())


def _expect_red(label: str, operation: Callable[[], Any]) -> dict[str, Any]:
    try:
        operation()
    except Exception as exc:
        return {
            "name": label,
            "detected": True,
            "error_type": type(exc).__name__,
            "diagnostic": str(exc)[:500],
        }
    raise GateFailure(f"broken-state control stayed green: {label}")


def _call_success(url: str, tool_name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return _require_success(_call_tool(url, tool_name, arguments), tool_name)


def _seed_authority(service: MailService, label: str) -> dict[str, Any]:
    project = (service.state_root / f"{label}-project").resolve()
    project.mkdir(parents=True, exist_ok=True)
    project_key = str(project)
    _call_success(service.url, "ensure_project", {"human_key": project_key, "format": "json"})
    for name, token in (
        ("GreenCastle", f"{label}-green-token"),
        ("BlueLake", f"{label}-blue-token"),
    ):
        _call_success(
            service.url,
            "register_agent",
            {
                "project_key": project_key,
                "program": "cutover-gate",
                "model": "fixture-model",
                "name": name,
                "task_description": f"{label} gate seed",
                "registration_token": token,
                "format": "json",
            },
        )
    _call_success(
        service.url,
        "set_contact_policy",
        {
            "project_key": project_key,
            "agent_name": "BlueLake",
            "policy": "open",
            "format": "json",
        },
    )
    sent = _call_success(
        service.url,
        "send_message",
        {
            "project_key": project_key,
            "sender_name": "GreenCastle",
            "to": ["BlueLake"],
            "subject": f"{label} migration seed",
            "body_md": "Stable IDs, relationships, and counts must survive migration.",
            "sender_token": f"{label}-green-token",
            "format": "json",
        },
    )
    reserved = _call_success(
        service.url,
        "file_reservation_paths",
        {
            "project_key": project_key,
            "agent_name": "GreenCastle",
            "paths": [f"src/{label}-gate.py"],
            "ttl_seconds": 3600,
            "exclusive": True,
            "reason": f"{label} migration seed",
            "format": "json",
        },
    )
    return {
        "project_key": project_key,
        "message_count": int(sent.get("count", 0)),
        "reservation_grants": len(reserved.get("granted", [])),
    }


def _validate_migration(source: Mapping[str, Any], destination: Mapping[str, Any]) -> None:
    source_database = source["database"]
    destination_database = destination["database"]
    if source_database["schema_sha256"] != destination_database["schema_sha256"]:
        raise GateFailure("migration changed SQLite schema identity")
    if source_database["tables"] != destination_database["tables"]:
        raise GateFailure("migration changed table counts or row identities")
    if source_database["relations"] != destination_database["relations"]:
        raise GateFailure("migration changed relational IDs or receipt relationships")
    if source["state_sha256"] != destination["state_sha256"]:
        raise GateFailure("migration changed database/archive/signal authority state")


def _ledger_expectations() -> dict[str, Any]:
    payload = json.loads(DECISION_LEDGER.read_text(encoding="utf-8"))
    decisions = payload.get("product_decisions")
    if not isinstance(decisions, list) or len(decisions) != 12:
        raise GateFailure("decision ledger must contain exactly 12 entries")
    by_id = {entry.get("id"): entry for entry in decisions if isinstance(entry, dict)}
    if set(by_id) != {f"D{index}" for index in range(1, 13)}:
        raise GateFailure("decision ledger IDs are not exactly D1-D12")
    actual_cutover_states = {
        decision_id: by_id[decision_id].get("cutover_state")
        for decision_id in EXPECTED_CUTOVER_STATES
    }
    if actual_cutover_states != EXPECTED_CUTOVER_STATES:
        changed = sorted(
            decision_id
            for decision_id, expected_state in EXPECTED_CUTOVER_STATES.items()
            if actual_cutover_states.get(decision_id) != expected_state
        )
        raise GateFailure(
            "gate refuses decision-ledger cutover states outside the exact "
            f"2026-08-15 owner approval: {changed}"
        )
    if payload.get("cutover_approval") != EXPECTED_CUTOVER_APPROVAL:
        raise GateFailure("decision ledger cutover approval record changed")

    cutover_gate = payload.get("cutover_gate")
    if not isinstance(cutover_gate, dict):
        raise GateFailure("decision ledger cutover gate is missing")
    if cutover_gate.get("required_condition_ids") != list(
        EXPECTED_REQUIRED_CONDITION_IDS
    ):
        raise GateFailure("decision ledger required cutover conditions changed")
    conditions = cutover_gate.get("conditions")
    if not isinstance(conditions, list) or not all(
        isinstance(condition, dict) for condition in conditions
    ):
        raise GateFailure("decision ledger cutover condition definitions changed")
    if [condition.get("id") for condition in conditions] != [
        *EXPECTED_REQUIRED_CONDITION_IDS,
        *EXPECTED_DESCOPED_CONDITION_IDS,
    ]:
        raise GateFailure("decision ledger cutover condition definitions changed")
    condition_by_id = {condition["id"]: condition for condition in conditions}
    if any(
        condition_by_id[condition_id].get("descoped")
        != "2026-08-15_owner_approved_documentation_only"
        for condition_id in EXPECTED_DESCOPED_CONDITION_IDS
    ):
        raise GateFailure("decision ledger descoped condition markers changed")

    follow_up_tasks = payload.get("follow_up_tasks")
    if not isinstance(follow_up_tasks, list) or not all(
        isinstance(task, dict) for task in follow_up_tasks
    ):
        raise GateFailure("decision ledger follow-up tasks changed")
    if [task.get("id") for task in follow_up_tasks] != list(
        EXPECTED_FOLLOW_UP_TASK_STATES
    ):
        raise GateFailure("decision ledger follow-up task IDs changed")
    tasks_by_id = {task["id"]: task for task in follow_up_tasks}
    actual_task_states = {
        task_id: tasks_by_id[task_id].get("implementation_state")
        for task_id in EXPECTED_FOLLOW_UP_TASK_STATES
    }
    if actual_task_states != EXPECTED_FOLLOW_UP_TASK_STATES:
        raise GateFailure("decision ledger follow-up task states changed")
    if any(
        tasks_by_id[task_id].get("descoped") != expected_metadata
        for task_id, expected_metadata in EXPECTED_DESCOPED_TASK_METADATA.items()
    ):
        raise GateFailure("decision ledger documentation-only descope record changed")
    expected = {
        "D8": ("match_frozen_live", "DB persists after archive failure"),
        "D10": ("match_frozen_live", "concurrent reservation winner and SQLite lock semantics"),
        "D12": ("match_frozen_live", "signal cleanup after crash, retirement, or stale consumer"),
    }
    for decision_id, (resolution, title) in expected.items():
        entry = by_id[decision_id]
        if entry.get("resolution") != resolution or entry.get("title") != title:
            raise GateFailure(f"{decision_id} ledger expectation changed")
        if entry.get("implementation_state") != "implemented":
            raise GateFailure(f"{decision_id} is not recorded as implemented")
    return {
        "sha256": hashlib.sha256(DECISION_LEDGER.read_bytes()).hexdigest(),
        "entries": 12,
        "cutover_go_entries": 11,
        "cutover_no_go_entries": 1,
        "deferred_decision": "D7",
        "required_condition_entries": len(EXPECTED_REQUIRED_CONDITION_IDS),
        "descoped_documentation_only_entries": len(
            EXPECTED_DESCOPED_CONDITION_IDS
        ),
        "fault_decisions": ["D8", "D10", "D12"],
    }


def run_coexistence_gate(root: Path, live_source: Path) -> dict[str, Any]:
    legacy = MailService(LIVE_NAMESPACE, live_source, root / "legacy")
    core = MailService(CORE_NAMESPACE, CORE_SOURCE, root / "core")
    _validate_authority_separation(legacy.paths, core.paths)
    detector = {"deliberate_errors_detected": 0, "successes_detected": 0}
    try:
        legacy.start()
        core.start()
        if legacy.process is None or core.process is None or legacy.process.pid == core.process.pid:
            raise GateFailure("coexistence did not produce two live service processes")
        for service in (legacy, core):
            observed = _activate_error_detector(
                service.url,
                service.state_root / "missing-detector-project",
            )
            for key, value in observed.items():
                detector[key] += value

        legacy_before = _coexistence_snapshot(legacy.paths)
        core_before = _coexistence_snapshot(core.paths)
        _call_success(legacy.url, "health_check", {"format": "json"})
        _call_success(core.url, "health_check", {"format": "json"})
        _require_same_snapshot(
            legacy_before, _coexistence_snapshot(legacy.paths), "legacy health no-op"
        )
        _require_same_snapshot(
            core_before, _coexistence_snapshot(core.paths), "Core health no-op"
        )

        legacy_seed = _seed_authority(legacy, "coexistence-legacy")
        legacy_after_write = _coexistence_snapshot(legacy.paths)
        core_after_legacy_write = _coexistence_snapshot(core.paths)
        _require_same_snapshot(core_before, core_after_legacy_write, "Core during legacy write")
        if _total_rows(legacy_after_write) <= _total_rows(legacy_before):
            raise GateFailure("legacy positive write control produced no durable rows")

        core_seed = _seed_authority(core, "coexistence-core")
        core_after_write = _coexistence_snapshot(core.paths)
        legacy_after_core_write = _coexistence_snapshot(legacy.paths)
        _require_same_snapshot(legacy_after_write, legacy_after_core_write, "legacy during Core write")
        if _total_rows(core_after_write) <= _total_rows(core_before):
            raise GateFailure("Core positive write control produced no durable rows")

        control = _expect_red(
            "shared writable database/archive/signals",
            lambda: _validate_authority_separation(legacy.paths, legacy.paths),
        )
        return {
            "status": "pass",
            "simultaneous_processes": 2,
            "ports": sorted([int(legacy.port), int(core.port)]),
            "reserved_ports_used": 0,
            "detector": detector,
            "no_op_snapshots": 2,
            "legacy_row_delta": _total_rows(legacy_after_write) - _total_rows(legacy_before),
            "core_row_delta": _total_rows(core_after_write) - _total_rows(core_before),
            "legacy_seed": legacy_seed,
            "core_seed": core_seed,
            "cross_authority_state_changes": 0,
            "broken_control": control,
        }
    finally:
        core.stop()
        legacy.stop()


def _run_seeded_migration(
    root: Path,
    live_source: Path,
    label: str,
) -> tuple[dict[str, Any], StatePaths, Path, dict[str, Any]]:
    source_root = root / "legacy"
    legacy = MailService(LIVE_NAMESPACE, live_source, source_root)
    try:
        legacy.start()
        detector = _activate_error_detector(
            legacy.url,
            source_root / "missing-detector-project",
        )
        seed = _seed_authority(legacy, label)
    finally:
        legacy.stop()
    source = StatePaths.from_root(source_root)
    return detector, source, source_root, seed


def run_migration_gate(root: Path, live_source: Path) -> dict[str, Any]:
    detector, source, source_root, seed = _run_seeded_migration(
        root, live_source, "migration"
    )
    source_before = _snapshot(source)
    noop = copy_state(source, source_root)
    if noop.status != "noop":
        raise GateFailure("same-authority migration did not return noop")
    _require_same_snapshot(source_before, _snapshot(source), "migration same-root no-op")

    destination_root = root / "core"
    copied = copy_state(source, destination_root)
    verified = verify_copy(source, destination_root)
    destination = StatePaths.from_root(destination_root)
    destination_snapshot = _snapshot(destination, require_baseline_git=True)
    _validate_migration(source_before, destination_snapshot)

    candidate = MailService(CORE_NAMESPACE, CORE_SOURCE, destination_root)
    try:
        candidate.start()
        candidate_detector = _activate_error_detector(
            candidate.url,
            destination_root / "missing-detector-project",
        )
        _call_success(
            candidate.url,
            "whois",
            {"project_key": seed["project_key"], "agent_name": "GreenCastle", "format": "json"},
        )
    finally:
        candidate.stop()

    broken_root = root / "broken-relational-id"
    copy_state(source, broken_root)
    with sqlite3.connect(StatePaths.from_root(broken_root).database) as connection:
        connection.execute(
            "UPDATE messages SET sender_id = "
            "(SELECT id FROM agents WHERE name = 'BlueLake' ORDER BY id LIMIT 1) "
            "WHERE id = (SELECT MIN(id) FROM messages)"
        )
        connection.commit()
    control = _expect_red(
        "changed relational sender ID with unchanged schema/counts",
        lambda: verify_copy(source, broken_root),
    )
    detector_total = {
        key: detector[key] + candidate_detector[key]
        for key in detector
    }
    counts = _table_counts(source_before)
    return {
        "status": "pass",
        "detector": detector_total,
        "no_op_status": noop.status,
        "copy_status": copied.status,
        "verify_status": verified["status"],
        "schema_sha256": source_before["database"]["schema_sha256"],
        "table_counts": counts,
        "total_rows": sum(counts.values()),
        "relation_families": len(source_before["database"]["relations"]),
        "stable_relational_ids": True,
        "state_sha256_equal": source_before["state_sha256"] == destination_snapshot["state_sha256"],
        "candidate_readiness_calls": 2,
        "broken_control": control,
    }


def _rewrite_manifest_paths(manifest_path: Path, source: StatePaths, destination_root: Path) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["source"] = {
        "database": str(source.database.absolute()),
        "archive": str(source.archive.absolute()),
        "signals": str(source.signals.absolute()),
    }
    payload["destination_root"] = str(destination_root.absolute())
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)


def _require_reversible(assessment: Mapping[str, Any], label: str) -> None:
    if assessment.get("status") != "reversible" or assessment.get("data_reversible") is not True:
        raise GateFailure(f"{label} is not reversible: {assessment.get('reason')}")


def run_rollback_gate(root: Path, live_source: Path) -> dict[str, Any]:
    detector, source, source_root, seed = _run_seeded_migration(
        root, live_source, "rollback"
    )
    destination_root = root / "core"
    copy_state(source, destination_root)
    manifest = destination_root / MANIFEST_NAME

    source_before = _snapshot(source)
    destination_before = _snapshot(
        StatePaths.from_root(destination_root), require_baseline_git=True
    )
    c3 = assess_rollback(manifest, "C3_MIGRATION_VERIFIED")
    _require_reversible(c3, "C3 rollback assessment")
    _require_same_snapshot(source_before, _snapshot(source), "C3 source assessment")
    _require_same_snapshot(
        destination_before,
        _snapshot(StatePaths.from_root(destination_root), require_baseline_git=True),
        "C3 destination assessment",
    )

    candidate = MailService(CORE_NAMESPACE, CORE_SOURCE, destination_root)
    try:
        candidate.start()
        candidate_detector = _activate_error_detector(
            candidate.url,
            destination_root / "missing-detector-project",
        )
        _call_success(
            candidate.url,
            "whois",
            {"project_key": seed["project_key"], "agent_name": "BlueLake", "format": "json"},
        )
    finally:
        candidate.stop()
    c4 = assess_rollback(manifest, "C4_NEW_SERVICE_READY")
    _require_reversible(c4, "C4 rollback assessment")

    control_source_root = root / "control-legacy"
    control_destination_root = root / "control-core"
    shutil.copytree(source_root, control_source_root)
    shutil.copytree(destination_root, control_destination_root)
    control_source = StatePaths.from_root(control_source_root)
    control_manifest = control_destination_root / MANIFEST_NAME
    _rewrite_manifest_paths(control_manifest, control_source, control_destination_root)
    control_candidate = MailService(CORE_NAMESPACE, CORE_SOURCE, control_destination_root)
    try:
        control_candidate.start()
        _activate_error_detector(
            control_candidate.url,
            control_destination_root / "missing-detector-project",
        )
        post_baseline_project = (control_destination_root / "post-baseline-write").resolve()
        post_baseline_project.mkdir()
        _call_success(
            control_candidate.url,
            "ensure_project",
            {"human_key": str(post_baseline_project), "format": "json"},
        )
    finally:
        control_candidate.stop()
    control_assessment = assess_rollback(control_manifest, "C5_CLIENT_SWITCHING")
    control = _expect_red(
        "post-baseline new-authority write",
        lambda: _require_reversible(control_assessment, "broken C5 rollback"),
    )
    if control_assessment["destination_matches_baseline"] is not False:
        raise GateFailure("rollback control did not create destination divergence")

    message_count_before = _table_counts(source_before).get("messages", 0)
    restored_legacy = MailService(
        LIVE_NAMESPACE,
        live_source,
        source_root,
        port=_pick_ephemeral_port(),
    )
    try:
        restored_legacy.start()
        restored_detector = _activate_error_detector(
            restored_legacy.url,
            source_root / "missing-rollback-detector-project",
        )
        _call_success(
            restored_legacy.url,
            "health_check",
            {"format": "json"},
        )
        _call_success(
            restored_legacy.url,
            "whois",
            {"project_key": seed["project_key"], "agent_name": "GreenCastle", "format": "json"},
        )
        _call_success(
            restored_legacy.url,
            "send_message",
            {
                "project_key": seed["project_key"],
                "sender_name": "GreenCastle",
                "to": ["BlueLake"],
                "subject": "rollback upstream operational proof",
                "body_md": "The restored upstream authority accepts a durable write.",
                "sender_token": "rollback-green-token",
                "format": "json",
            },
        )
        fetched = _call_success(
            restored_legacy.url,
            "fetch_inbox",
            {
                "project_key": seed["project_key"],
                "agent_name": "BlueLake",
                "limit": 10,
                "include_bodies": False,
                "format": "json",
            },
        )
    finally:
        restored_legacy.stop()
    source_after = _snapshot(source)
    message_count_after = _table_counts(source_after).get("messages", 0)
    if message_count_after != message_count_before + 1:
        raise GateFailure("restored upstream did not persist exactly one proof message")
    inbox = fetched.get("result", [])
    if not isinstance(inbox, list) or not any(
        isinstance(item, dict) and item.get("subject") == "rollback upstream operational proof"
        for item in inbox
    ):
        raise GateFailure("restored upstream could not read back the proof message")

    detector_total = {
        key: detector[key] + candidate_detector[key] + restored_detector[key]
        for key in detector
    }
    return {
        "status": "pass",
        "detector": detector_total,
        "no_op_assessments": 1,
        "c3_status": c3["status"],
        "c4_status": c4["status"],
        "upstream_health_after_rollback": True,
        "upstream_whois_after_rollback": True,
        "upstream_message_delta_after_rollback": message_count_after - message_count_before,
        "upstream_readback_count": len(inbox),
        "post_write_reverse_transform": "not_implemented_fix_forward_only",
        "broken_control_assessment": {
            "status": control_assessment["status"],
            "source_matches_baseline": control_assessment["source_matches_baseline"],
            "destination_matches_baseline": control_assessment["destination_matches_baseline"],
        },
        "broken_control": control,
    }


def _validate_d8_subset(observation: Mapping[str, Any], d8: Any) -> None:
    expected_records = d8._expected_d8_database_records(  # noqa: SLF001
        d8._D8_SUBSET_SUBJECT, d8._D8_SUBSET_BODY  # noqa: SLF001
    )
    required = {
        "seam": "successful_bundle_write_before_next_copy",
        "successful_write_count": 1,
        "database_delivery_records": expected_records,
        "completed_archive_roles": ["canonical"],
        "successful_write_roles": ["canonical"],
        "archive_paths_match_successful_writes": True,
        "git_head_unchanged": True,
        "staged_paths_empty": True,
        "message_commit_absent": True,
    }
    for key, value in required.items():
        if observation.get(key) != value:
            raise GateFailure(f"D8 SIGKILL projection mismatch for {key}")


def _run_d12_core_probe(root: Path, d12: Any) -> dict[str, Any]:
    roots = WorkerStateRoots.under(root, pythonpath=(CORE_SOURCE,))
    environment = isolated_worker_env(os.environ, CORE_NAMESPACE, roots)
    environment.update(
        {
            "DECISION_NAMESPACE": CORE_NAMESPACE,
            "DECISION_STATE_ROOT": str(root),
            "DECISION_DATABASE": str(roots.database),
            "DECISION_SIGNALS": str(roots.signals),
            "DECISION_STORAGE": str(roots.storage),
            "DECISION_SOURCE_ROOT": str(CORE_SOURCE.resolve()),
            # D12 pins the predecessor's immediate signal-consumption
            # projection. The production default now deliberately adds a
            # grace window, so this historical differential opts out.
            "AGENTSTACK_MAIL_SIGNAL_CLEAR_GRACE_SECONDS": "0",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", d12._D12_SERVER_WORKER],  # noqa: SLF001
        cwd=roots.cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    transcript = completed.stdout + completed.stderr
    for token in (
        "d12-green-token",
        "d12-blue-token",
        "d12-red-token",
        "d12-yellow-token",
    ):
        if token in transcript:
            raise GateFailure("D12 probe leaked a fixture registration token")
    if completed.returncode != 0:
        raise GateFailure(f"D12 server probe failed: {transcript[-5000:]}")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise GateFailure("D12 server probe produced no JSON")
    return json.loads(lines[-1])


def _validate_d12_projection(projection: Mapping[str, Any]) -> None:
    expected = {
        "signal_failure_attempts": 2,
        "failed_signal_send_count": 1,
        "failure_message_api_fetchable": True,
        "failure_archive_contains_message": True,
        "failure_message_count": 1,
        "failure_recipient_count": 3,
        "failure_signal_count": 0,
        "routed_send_count": 1,
        "seed_send_count": 2,
        "pre_retire_message_count": 4,
        "pre_retire_recipient_count": 8,
        "pre_retire_signal_count": 5,
        "blue_per_message_signal_count": 3,
        "red_per_message_signal_count": 1,
        "yellow_signal_count": 0,
        "blue_legacy_signal_present": True,
        "retired": True,
        "retire_preserved_signals": True,
        "fetch_count": 1,
        "limited_fetch_preserved_messages": True,
        "limited_fetch_preserved_recipients": True,
        "limited_fetch_cleared_blue_signals": True,
        "limited_fetch_preserved_red_signal": 1,
        "filtered_fetch_count": 0,
        "filtered_fetch_cleared_red_signals": True,
        "filtered_fetch_preserved_messages": True,
        "filtered_fetch_preserved_recipients": True,
        "after_fetch_retired": True,
    }
    if dict(projection) != expected:
        differing = sorted(
            key for key in set(expected) | set(projection) if projection.get(key) != expected.get(key)
        )
        raise GateFailure(f"D12 selected projection mismatch: {differing}")


def run_fault_gate(root: Path) -> dict[str, Any]:
    ledger = _ledger_expectations()
    import test_pending_decision_d8_d9 as d8
    import test_pending_decision_d10 as d10
    import test_pending_decision_d11_d12 as d12

    d8_root = root / "d8-sigkill"
    d8_subset = d8._observe_d8_subset(  # noqa: SLF001
        namespace=CORE_NAMESPACE,
        source=CORE_SOURCE,
        root=d8_root,
        kill_after=1,
    )
    _validate_d8_subset(d8_subset, d8)
    d8_exception = d8._observe_d8_exception(  # noqa: SLF001
        namespace=CORE_NAMESPACE,
        source=CORE_SOURCE,
        root=root / "d8-exception",
    )
    expected_exception_records = d8._expected_d8_database_records(  # noqa: SLF001
        d8._D8_EXCEPTION_SUBJECT, d8._D8_EXCEPTION_BODY  # noqa: SLF001
    )
    if d8_exception != {
        "tool_error": True,
        "injected_bundle_failure": True,
        "database_delivery_records": expected_exception_records,
        "completed_archive_roles": [],
    }:
        raise GateFailure("D8 archive exception did not preserve the selected DB row")

    d10_output = d10._run_worker(  # noqa: SLF001
        namespace=CORE_NAMESPACE,
        scenario="same_process_shared_root",
        root=root / "d10",
        frozen_live_checkout=PACKAGE_ROOT,
    )
    d10_samples = d10_output["evidence"]["samples"]
    if len(d10_samples) != d10.SAME_PROCESS_RACE_TRIALS:
        raise GateFailure("D10 returned an unexpected trial count")
    for sample in d10_samples:
        if not (
            sample["lock_attempts_before_release"] == 2
            and sample["all_calls_ok"] is True
            and len(sample["winners"]) == 1
            and len(sample["losers"]) == 1
            and sample["active_rows"] == 1
        ):
            raise GateFailure(f"D10 concurrency projection failed in trial {sample['trial']}")

    d12_probe = _run_d12_core_probe(root / "d12-server", d12)
    d12_projection = d12._d12_server_projection(d12_probe)  # noqa: SLF001
    _validate_d12_projection(d12_projection)

    watcher_payload = {
        "project": "cutover-fault-project",
        "agent": "BlueLake",
        "message": {
            "id": 12001,
            "subject": "D12 cutover crash recovery",
            "importance": "high",
        },
    }
    watcher_root = root / "d12-watcher-crash"
    crashed, signal_path, _state_file, lease_path = d12._run_watcher_state_machine(  # noqa: SLF001
        watcher_root,
        watcher_payload,
        "after_external_injection",
    )
    if crashed.returncode != -signal.SIGKILL or not signal_path.exists() or not lease_path.exists():
        raise GateFailure("D12 watcher crash did not retain retryable signal and lease")
    (lease_path / "ts").write_text("0\n", encoding="utf-8")
    retried, retried_signal, _state, retried_lease = d12._run_watcher_state_machine(  # noqa: SLF001
        watcher_root,
        watcher_payload,
        "",
        command_log=watcher_root / "fake-external-commands.log",
    )
    if retried.returncode != 0 or retried_signal.exists() or retried_lease.exists():
        raise GateFailure("D12 watcher retry did not clean signal and lease")
    commands = (watcher_root / "fake-external-commands.log").read_text(encoding="utf-8").splitlines()
    d12._assert_injection_commands(commands, expected_count=2)  # noqa: SLF001

    d8_database = WorkerStateRoots.under(d8_root).database
    with sqlite3.connect(d8_database) as connection:
        connection.execute(
            "DELETE FROM message_recipients WHERE message_id IN "
            "(SELECT id FROM messages WHERE subject = ?)",
            (d8._D8_SUBSET_SUBJECT,),  # noqa: SLF001
        )
        connection.execute(
            "DELETE FROM messages WHERE subject = ?",
            (d8._D8_SUBSET_SUBJECT,),  # noqa: SLF001
        )
        connection.commit()

    def validate_deleted_d8_row() -> None:
        actual = d8._read_d8_delivery_records(  # noqa: SLF001
            d8_database, d8._D8_SUBSET_SUBJECT  # noqa: SLF001
        )
        expected = d8._expected_d8_database_records(  # noqa: SLF001
            d8._D8_SUBSET_SUBJECT, d8._D8_SUBSET_BODY  # noqa: SLF001
        )
        if actual != expected:
            raise GateFailure("D8 durable DB row is missing after archive/process fault")

    control = _expect_red("deleted D8 durable DB delivery row", validate_deleted_d8_row)
    return {
        "status": "pass",
        "ledger": ledger,
        "d8": {
            "literal_sigkill_seams": 1,
            "archive_exception_seams": 1,
            "database_delivery_rows_after_sigkill": len(d8_subset["database_delivery_records"]),
            "completed_archive_copies_before_sigkill": d8_subset["successful_write_count"],
            "database_delivery_rows_after_archive_exception": len(
                d8_exception["database_delivery_records"]
            ),
        },
        "d10": {
            "shared_root_trials": len(d10_samples),
            "total_grants": sum(len(sample["winners"]) for sample in d10_samples),
            "total_conflicts": sum(len(sample["losers"]) for sample in d10_samples),
            "active_rows_per_trial": [sample["active_rows"] for sample in d10_samples],
        },
        "d12": {
            "signal_failure_attempts": d12_projection["signal_failure_attempts"],
            "pre_retire_signals": d12_projection["pre_retire_signal_count"],
            "retire_preserved_signals": d12_projection["retire_preserved_signals"],
            "blue_fetch_cleared_signals": d12_projection["limited_fetch_cleared_blue_signals"],
            "filtered_fetch_cleared_remaining_signals": d12_projection[
                "filtered_fetch_cleared_red_signals"
            ],
            "watcher_crash_windows": 1,
            "post_expiry_retry_injections": 2,
            "retry_cleaned_signal_and_lease": True,
        },
        "broken_control": control,
    }


def run_gates(selection: str, work_root: Path) -> dict[str, Any]:
    work_root = work_root.expanduser().absolute()
    work_root = work_root.parent.resolve(strict=True) / work_root.name
    if work_root.exists():
        raise GateFailure(f"work root must be absent: {work_root}")
    work_root.mkdir(mode=0o700, parents=True)
    if stat.S_IMODE(work_root.stat().st_mode) & 0o077:
        raise GateFailure("work root must be private mode 0700")

    names = (
        ("coexistence", "migration", "rollback", "fault")
        if selection == "all"
        else (selection,)
    )
    needs_live = any(name != "fault" for name in names)
    live_source = None
    if needs_live:
        live_checkout = reconstruct_live(PACKAGE_ROOT, work_root / "frozen-live")
        live_source = live_checkout / "src"

    started = time.monotonic()
    results: dict[str, Any] = {}
    for name in names:
        gate_root = work_root / name
        if name == "coexistence":
            assert live_source is not None
            results[name] = run_coexistence_gate(gate_root, live_source)
        elif name == "migration":
            assert live_source is not None
            results[name] = run_migration_gate(gate_root, live_source)
        elif name == "rollback":
            assert live_source is not None
            results[name] = run_rollback_gate(gate_root, live_source)
        elif name == "fault":
            results[name] = run_fault_gate(gate_root)
        else:  # pragma: no cover - argparse constrains this
            raise GateFailure(f"unknown gate {name}")
    controls = [result["broken_control"] for result in results.values()]
    if not all(control.get("detected") is True for control in controls):
        raise GateFailure("one or more broken-state controls were not detected")
    return {
        "schema_version": 1,
        "status": "pass",
        "selection": selection,
        "passed_gates": len(results),
        "broken_controls_detected": len(controls),
        "production_reserved_ports_used": 0,
        "ephemeral_work_root": str(work_root),
        "duration_seconds": round(time.monotonic() - started, 3),
        "gates": results,
    }


def _write_output(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.absolute()
    if path.exists():
        raise GateFailure(f"output path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")


def _candidate_checkout(candidate_commit: str) -> dict[str, Any]:
    if FULL_COMMIT_RE.fullmatch(candidate_commit) is None:
        raise GateFailure("--candidate-commit must be a full lowercase 40-hex commit")

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GateFailure(
                f"candidate Git inspection failed: {(completed.stderr or completed.stdout)[-1000:]}"
            )
        return completed.stdout.strip()

    head = git("rev-parse", "--verify", "HEAD^{commit}")
    if head != candidate_commit:
        raise GateFailure(
            f"candidate commit does not equal checkout HEAD: {candidate_commit} != {head}"
        )
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise GateFailure("candidate checkout is dirty")
    return {"commit": candidate_commit, "checkout_clean": True}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run hermetic coexistence/migration/rollback/fault gates against "
            "the authenticated frozen live authority and AgentStack Mail Core."
        )
    )
    parser.add_argument(
        "--gate",
        choices=("all", "coexistence", "migration", "rollback", "fault"),
        default="all",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        help="absolute absent disposable root; defaults to an auto-removed private temp root",
    )
    parser.add_argument(
        "--candidate-commit",
        help=(
            "optional full lowercase 40-hex commit; when supplied, require it to "
            "equal a clean checkout HEAD before and after the gate"
        ),
    )
    parser.add_argument("--output", type=Path, help="optional absent mode-0600 JSON result")
    args = parser.parse_args()

    try:
        candidate = (
            _candidate_checkout(args.candidate_commit)
            if args.candidate_commit is not None
            else {"commit": None, "checkout_clean": None}
        )
        if args.work_root is not None:
            if not args.work_root.is_absolute():
                raise GateFailure("--work-root must be absolute")
            result = run_gates(args.gate, args.work_root)
        else:
            with tempfile.TemporaryDirectory(prefix="agentstack-mail-cutover-gates-") as temp:
                work_root = Path(temp) / "run"
                result = run_gates(args.gate, work_root)
        if args.candidate_commit is not None:
            if _candidate_checkout(args.candidate_commit) != candidate:
                raise GateFailure("candidate checkout binding changed during gate")
        result["candidate"] = candidate
        if args.output is not None:
            _write_output(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    except LiveBaselineUnavailable as exc:
        # Not a failure: the predecessor this gate compares against is not
        # distributed. Reported with its own status and exit code so a caller
        # cannot read "could not run" as "ran and passed" — or as "failed".
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "unavailable",
                    "selection": args.gate,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        raise SystemExit(3) from exc
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "status": "fail",
            "selection": args.gate,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True, indent=2))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
