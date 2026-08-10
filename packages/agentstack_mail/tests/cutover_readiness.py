#!/usr/bin/env python3
"""Read-only, fail-closed cutover-readiness evaluation.

The evaluator parses one exact manifest byte stream, validates it with the
distribution's canonical manifest validator, binds it to an explicitly named
clean Git candidate, and recomputes supported gates from digest-verified raw
artifacts.  It never changes approval state or performs a cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from verify_artifact import (
    _assert_expected_divergences_manifest,
    verify_sdist,
    verify_wheel,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_MANIFEST = (
    PACKAGE_ROOT / "fixtures" / "differential-expected-divergences-v2.json"
)

EXPECTED_CONDITION_IDS = (
    "product-decisions-selected",
    "pre-cutover-product-decisions-implemented",
    "initial-cutover-difference-set-exact",
    "candidate-source-bound",
    "product-decision-cutover-approval",
    "selected-behavior-release-gate",
    "distribution-artifact-release-gate",
    "d2-d3-worker-progress-diagnostics",
    "d2-d3-timeout-process-group-cleanup",
    "d10-diagnostic-liveness-timeout",
    "provenance-regression-sync",
    "reservation-probe-safety-release-gate",
    "reservation-performance-release-gate",
    "http-cli-transport-entrypoints",
    "service-lifecycle-supervision",
    "installer-core-integration",
    "mcp-client-reregistration-cutover",
    "data-migration-reconciliation",
    "rollback-revert-procedure",
    "coexistence-fault-soak-gates",
    "full-performance-load-soak-matrix",
    "notification-layout-consumer-compatibility",
    "full-repository-release-gate",
    "installed-wheel-contract-release-gate",
    "cutover-evidence-provenance-gate",
    "cutover-documentation-consistency",
)
EXPECTED_DECISION_IDS = tuple(f"D{index}" for index in range(1, 13))
INITIAL_APPROVAL_IDS = tuple(
    decision_id for decision_id in EXPECTED_DECISION_IDS if decision_id != "D7"
)
EXPECTED_DIFFERENCE_IDS = (
    "D1",
    "reservation-probe-incomplete-fail-closed",
)
EXPECTED_D7_ORDER = "post_cutover_with_null_token_creation_stop_as_one_change"
CONDITION_KEYS = {
    "id",
    "kind",
    "evidence_kind",
    "source",
    "predicate",
    "remediation",
}
ARTIFACT_INDEX_KEYS = {
    "condition_id",
    "definition_sha256",
    "kind",
    "path",
    "sha256",
}
SUPPORTED_EVIDENCE_KINDS = {
    "pytest_nodes_v1",
    "distribution_artifacts_v1",
    "reservation_performance_v1",
    "reservation_safety_v1",
}
HEX_DIGITS = frozenset("0123456789abcdef")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not (set(value) - HEX_DIGITS)
    )


def _is_full_git_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and not (set(value) - HEX_DIGITS)
    )


def _definition_sha256(condition: dict[str, Any]) -> str:
    encoded = json.dumps(
        condition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _missing(
    condition: dict[str, Any],
    reason: str,
    *,
    observed: object | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": condition.get("id", "unknown"),
        "kind": condition.get("kind", "unknown"),
        "source": condition.get("source", "unknown"),
        "reason": reason,
        "remediation": condition.get("remediation", "repair the condition"),
    }
    if observed is not None:
        result["observed"] = observed
    return result


def _safe_artifact_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("artifact path must be a non-empty string")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe artifact path: {value!r}")
    resolved_root = root.resolve()
    resolved = (resolved_root / Path(*pure.parts)).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"artifact path escapes evidence root: {value!r}")
    if not resolved.is_file():
        raise ValueError(f"artifact file does not exist: {value!r}")
    return resolved


def _json_artifact(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact {label} must contain an object")
    return value


def _canonical_manifest_error(manifest_bytes: bytes) -> str | None:
    try:
        _assert_expected_divergences_manifest(
            manifest_bytes,
            (PACKAGE_ROOT / "fixtures" / "compatibility-tools-v1.json").read_bytes(),
            (PACKAGE_ROOT / "fixtures" / "live-tools-list.json").read_bytes(),
            (PACKAGE_ROOT / "src" / "agentstack_mail" / "app.py").read_bytes(),
            artifact="cutover-readiness",
        )
    except (OSError, SystemExit) as exc:
        return str(exc)
    return None


def _parse_evidence_index(
    evidence: object | None,
    *,
    evidence_root: Path | None,
    manifest_sha256: str,
    candidate_commit: str,
    conditions: dict[str, dict[str, Any]],
) -> tuple[dict[str, tuple[dict[str, Any], bytes]], list[str]]:
    if evidence is None:
        return {}, []
    if evidence_root is None:
        return {}, ["evidence_root is required when evidence is supplied"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema_version",
        "candidate_commit",
        "manifest_sha256",
        "artifacts",
    }:
        return {}, ["evidence envelope shape is invalid"]

    errors: list[str] = []
    if evidence["schema_version"] != 1:
        errors.append("evidence schema_version is not 1")
    if evidence["candidate_commit"] != candidate_commit:
        errors.append("evidence candidate_commit does not match the designated candidate")
    if evidence["manifest_sha256"] != manifest_sha256:
        errors.append("evidence manifest_sha256 does not match exact manifest bytes")
    raw_artifacts = evidence["artifacts"]
    if not isinstance(raw_artifacts, list):
        return {}, [*errors, "evidence artifacts must be a list"]

    artifacts: dict[str, tuple[dict[str, Any], bytes]] = {}
    for index, record in enumerate(raw_artifacts):
        if not isinstance(record, dict) or set(record) != ARTIFACT_INDEX_KEYS:
            errors.append(f"evidence artifact {index} has invalid keys")
            continue
        condition_id = record["condition_id"]
        if not isinstance(condition_id, str) or condition_id not in conditions:
            errors.append(f"unknown evidence condition id: {condition_id!r}")
            continue
        if condition_id in artifacts:
            errors.append(f"duplicate evidence condition id: {condition_id}")
            continue
        condition = conditions[condition_id]
        expected_kind = condition.get("evidence_kind")
        if expected_kind not in SUPPORTED_EVIDENCE_KINDS:
            errors.append(f"condition {condition_id} has no supported raw handler")
            continue
        if record["kind"] != expected_kind:
            errors.append(f"evidence kind mismatch for {condition_id}")
            continue
        if record["definition_sha256"] != _definition_sha256(condition):
            errors.append(f"condition definition digest mismatch for {condition_id}")
            continue
        if not _is_sha256(record["sha256"]):
            errors.append(f"invalid artifact digest for {condition_id}")
            continue
        try:
            artifact_path = _safe_artifact_path(evidence_root, record["path"])
            content = artifact_path.read_bytes()
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        if _sha256(content) != record["sha256"]:
            errors.append(f"artifact digest mismatch for {condition_id}")
            continue
        artifacts[condition_id] = (record, content)
    return artifacts, errors


def _expected_selected_nodes(manifest: dict[str, Any]) -> list[str]:
    nodes: list[str] = []
    for decision in manifest["product_decisions"]:
        if decision.get("implementation_state") == "implemented":
            verification = decision.get("verification")
            if not isinstance(verification, list) or not all(
                isinstance(node, str) for node in verification
            ):
                return []
            nodes.extend(verification)
    return nodes


def _check_pytest_nodes(
    content: bytes,
    *,
    candidate_commit: str,
    expected_nodes: list[str],
) -> str | None:
    report = _json_artifact(content, label="pytest_nodes_v1")
    if set(report) != {"schema_version", "candidate_commit", "nodes"}:
        return "pytest node report shape is invalid"
    if report["schema_version"] != 1 or report["candidate_commit"] != candidate_commit:
        return "pytest node report is not bound to the candidate"
    nodes = report["nodes"]
    if not isinstance(nodes, list):
        return "pytest node report nodes must be a list"
    observed: dict[str, str] = {}
    for record in nodes:
        if not isinstance(record, dict) or set(record) != {"node_id", "outcome"}:
            return "pytest node record shape is invalid"
        node_id = record["node_id"]
        outcome = record["outcome"]
        if not isinstance(node_id, str) or node_id in observed:
            return "pytest node ids are invalid or duplicated"
        if outcome not in {"passed", "failed", "error", "skipped", "xfailed", "cancelled"}:
            return "pytest node outcome is unknown"
        observed[node_id] = outcome
    if list(observed) != expected_nodes:
        return "pytest node inventory is missing, extra, duplicated, or reordered"
    nonpassing = sorted(node for node, outcome in observed.items() if outcome != "passed")
    if nonpassing:
        return f"required pytest nodes did not pass: {nonpassing}"
    if not observed:
        return "zero-test pytest evidence is not a pass"
    return None


def _check_distribution_artifacts(
    content: bytes,
    *,
    evidence_root: Path,
    candidate_commit: str,
) -> str | None:
    report = _json_artifact(content, label="distribution_artifacts_v1")
    if set(report) != {"schema_version", "candidate_commit", "wheel", "sdist"}:
        return "distribution report shape is invalid"
    if report["schema_version"] != 1 or report["candidate_commit"] != candidate_commit:
        return "distribution report is not bound to the candidate"
    immutable: dict[str, bytes] = {}
    for kind in ("wheel", "sdist"):
        record = report[kind]
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            return f"distribution {kind} record shape is invalid"
        if not _is_sha256(record["sha256"]):
            return f"distribution {kind} digest is invalid"
        try:
            artifact = _safe_artifact_path(evidence_root, record["path"])
            content = artifact.read_bytes()
        except (OSError, ValueError) as exc:
            return str(exc)
        if _sha256(content) != record["sha256"]:
            return f"distribution {kind} digest mismatch"
        immutable[kind] = content
    try:
        with tempfile.TemporaryDirectory(prefix="agentstack-mail-cutover-") as tmp:
            wheel = Path(tmp) / "candidate.whl"
            sdist = Path(tmp) / "candidate.tar.gz"
            wheel.write_bytes(immutable["wheel"])
            sdist.write_bytes(immutable["sdist"])
            verify_wheel(wheel)
            verify_sdist(sdist)
    except (OSError, SystemExit) as exc:
        return f"distribution verifier failed: {exc}"
    return None


def _evenly_sample(values: list[str], count: int) -> list[str]:
    if count < 0 or count > len(values):
        raise ValueError("performance input sample count exceeds its source pool")
    if count == 0:
        return []
    if count == 1:
        return [values[0]]
    return [
        values[(index * (len(values) - 1)) // (count - 1)]
        for index in range(count)
    ]


def _canonical_performance_input_paths(
    gate: dict[str, Any],
    *,
    tracked_paths: list[str] | None = None,
) -> list[str]:
    policy = gate.get("input")
    if not isinstance(policy, dict) or set(policy) != {
        "workspace",
        "count",
        "source_command",
        "preferred_prefix",
        "preferred_extensions",
        "ordering",
        "sampling",
    }:
        raise ValueError("reservation performance input policy shape is invalid")
    count = policy["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("reservation performance input count is invalid")
    if tracked_paths is None:
        workspace = policy["workspace"]
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise ValueError("reservation performance workspace is not absolute")
        if policy["source_command"] != "git ls-files -z":
            raise ValueError("reservation performance source command changed")
        try:
            completed = subprocess.run(
                ["git", "-C", workspace, "ls-files", "-z"],
                check=True,
                capture_output=True,
                timeout=30,
            )
            decoded = completed.stdout.decode("utf-8")
        except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
            raise ValueError(
                f"cannot enumerate canonical reservation performance inputs: {exc}"
            ) from exc
        tracked_paths = [value for value in decoded.split("\0") if value]
    if (
        not isinstance(tracked_paths, list)
        or not all(isinstance(value, str) and value for value in tracked_paths)
        or len(set(tracked_paths)) != len(tracked_paths)
    ):
        raise ValueError("reservation performance tracked path inventory is invalid")
    if policy["ordering"] != "unicode_codepoint_ascending":
        raise ValueError("reservation performance input ordering changed")
    prefix = policy["preferred_prefix"]
    extensions = policy["preferred_extensions"]
    if (
        not isinstance(prefix, str)
        or not isinstance(extensions, list)
        or not extensions
        or not all(isinstance(value, str) and value for value in extensions)
    ):
        raise ValueError("reservation performance preference policy is invalid")
    ordered = sorted(tracked_paths)
    preferred = [
        value
        for value in ordered
        if value.startswith(prefix) and any(value.endswith(ext) for ext in extensions)
    ]
    if len(preferred) >= count:
        return _evenly_sample(preferred, count)
    preferred_set = set(preferred)
    remaining = [value for value in ordered if value not in preferred_set]
    return [*preferred, *_evenly_sample(remaining, count - len(preferred))]


def _check_reservation_performance(
    content: bytes,
    *,
    manifest: dict[str, Any],
    candidate_commit: str,
    tracked_paths: list[str] | None = None,
) -> str | None:
    report = _json_artifact(content, label="reservation_performance_v1")
    expected_keys = {
        "schema_version",
        "candidate_commit",
        "gate_id",
        "input_paths",
        "input_sha256",
        "result_shape_sha256",
        "script_sha256",
        "runs",
    }
    if set(report) != expected_keys:
        return "reservation performance report shape is invalid"
    if report["schema_version"] != 1 or report["candidate_commit"] != candidate_commit:
        return "reservation performance report is not bound to the candidate"
    gate = manifest.get("performance_gates", [{}])[0]
    if report["gate_id"] != gate.get("id"):
        return "reservation performance gate id changed"
    gate_input = gate.get("input")
    if not isinstance(gate_input, dict):
        return "reservation performance input policy is absent"
    input_count = gate_input.get("count")
    repetitions = gate.get("repetitions")
    fresh_process = gate.get("fresh_process_each_run")
    threshold = gate.get("threshold_seconds")
    minimum_complete = gate.get("minimum_complete_runs")
    if (
        isinstance(input_count, bool)
        or not isinstance(input_count, int)
        or input_count <= 0
        or isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or repetitions <= 0
        or fresh_process is not True
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) < 0
        or isinstance(minimum_complete, bool)
        or not isinstance(minimum_complete, int)
        or minimum_complete <= 0
        or minimum_complete > repetitions
    ):
        return "reservation performance policy values are invalid"
    input_paths = report["input_paths"]
    if (
        not isinstance(input_paths, list)
        or len(input_paths) != input_count
        or not all(isinstance(value, str) and value for value in input_paths)
        or len(set(input_paths)) != input_count
    ):
        return "reservation performance input path inventory is invalid"
    try:
        expected_input_paths = _canonical_performance_input_paths(
            gate,
            tracked_paths=tracked_paths,
        )
    except ValueError as exc:
        return str(exc)
    if input_paths != expected_input_paths:
        return "reservation performance inputs differ from the canonical Git sample"
    if report["input_sha256"] != _canonical_json_sha256(input_paths):
        return "reservation performance input fingerprint mismatch"
    if not _is_sha256(report["result_shape_sha256"]):
        return "reservation performance result fingerprint is invalid"
    if not _is_sha256(report["script_sha256"]):
        return "reservation performance script fingerprint is invalid"
    script_value = gate.get("script")
    if not isinstance(script_value, str):
        return "reservation performance script is not declared"
    script_path = REPOSITORY_ROOT / script_value
    if not script_path.is_file():
        return "reservation performance script is absent from the candidate"
    if _sha256(script_path.read_bytes()) != report["script_sha256"]:
        return "reservation performance script digest mismatch"
    runs = report["runs"]
    if not isinstance(runs, list) or len(runs) != repetitions:
        return "reservation performance run count differs from the ledger"
    times: list[float] = []
    complete = 0
    indexes: list[int] = []
    process_ids: list[int] = []
    result_shapes: list[list[dict[str, object]]] = []
    for run in runs:
        if not isinstance(run, dict) or set(run) != {
            "index",
            "process_id",
            "wall_seconds",
            "matched",
            "probe_complete",
            "results",
        }:
            return "reservation performance run shape is invalid"
        index = run["index"]
        process_id = run["process_id"]
        wall_seconds = run["wall_seconds"]
        matched = run["matched"]
        probe_complete = run["probe_complete"]
        if isinstance(index, bool) or not isinstance(index, int):
            return "reservation performance run index is invalid"
        if (
            isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id <= 0
        ):
            return "reservation performance process identity is invalid"
        if isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float)):
            return "reservation performance wall time is invalid"
        wall = float(wall_seconds)
        if not math.isfinite(wall) or wall < 0:
            return "reservation performance wall time is not finite and nonnegative"
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (matched, probe_complete)
        ):
            return "reservation performance counts are invalid"
        raw_results = run["results"]
        if not isinstance(raw_results, list) or len(raw_results) != input_count:
            return "reservation performance per-path result count is invalid"
        normalized_results: list[dict[str, object]] = []
        for result_index, result in enumerate(raw_results):
            if not isinstance(result, dict) or set(result) != {
                "path",
                "matched",
                "probe_complete",
                "filesystem_present",
                "git_present",
            }:
                return "reservation performance per-path result shape is invalid"
            if result["path"] != input_paths[result_index]:
                return "reservation performance result paths changed or reordered"
            if not all(
                isinstance(result[field], bool)
                for field in (
                    "matched",
                    "probe_complete",
                    "filesystem_present",
                    "git_present",
                )
            ):
                return "reservation performance per-path flags are invalid"
            normalized_results.append(result)
        if matched != sum(bool(item["matched"]) for item in normalized_results):
            return "reservation performance matched aggregate is inconsistent"
        if probe_complete != sum(
            bool(item["probe_complete"]) for item in normalized_results
        ):
            return "reservation performance completeness aggregate is inconsistent"
        indexes.append(index)
        process_ids.append(process_id)
        times.append(wall)
        result_shapes.append(normalized_results)
        if matched == input_count and probe_complete == input_count:
            complete += 1
    if sorted(indexes) != list(range(repetitions)) or len(set(indexes)) != repetitions:
        return "reservation performance run indexes differ from the ledger"
    if len(set(process_ids)) != repetitions:
        return "reservation performance runs did not use distinct processes"
    if report["result_shape_sha256"] != _canonical_json_sha256(result_shapes):
        return "reservation performance result fingerprint mismatch"
    if statistics.median(times) > float(threshold):
        return "reservation performance median exceeds the ledger threshold"
    if complete < minimum_complete:
        return "reservation performance has fewer complete runs than the ledger requires"
    return None


def _check_reservation_safety(
    content: bytes,
    *,
    candidate_commit: str,
) -> str | None:
    report = _json_artifact(content, label="reservation_safety_v1")
    if set(report) != {"schema_version", "candidate_commit", "gate_id", "controls"}:
        return "reservation safety report shape is invalid"
    if report["schema_version"] != 1 or report["candidate_commit"] != candidate_commit:
        return "reservation safety report is not bound to the candidate"
    if report["gate_id"] != "reservation-probe-incomplete-fail-closed":
        return "reservation safety gate id changed"
    controls = report["controls"]
    if not isinstance(controls, list):
        return "reservation safety controls must be a list"
    observed: dict[str, dict[str, Any]] = {}
    expected_keys = {
        "id",
        "probe_complete",
        "activity_unknown",
        "ttl_expired",
        "stale",
        "released",
    }
    for control in controls:
        if not isinstance(control, dict) or set(control) != expected_keys:
            return "reservation safety control shape is invalid"
        control_id = control["id"]
        if not isinstance(control_id, str) or control_id in observed:
            return "reservation safety control ids are invalid or duplicated"
        observed[control_id] = control
    if list(observed) != ["timeout", "error", "filesystem_incomplete", "ttl_expiry"]:
        return "reservation safety controls are missing, extra, or reordered"
    for control_id in ("timeout", "error", "filesystem_incomplete"):
        if observed[control_id] != {
            "id": control_id,
            "probe_complete": False,
            "activity_unknown": True,
            "ttl_expired": False,
            "stale": False,
            "released": False,
        }:
            return f"reservation safety control {control_id} is not fail-closed"
    if observed["ttl_expiry"] != {
        "id": "ttl_expiry",
        "probe_complete": False,
        "activity_unknown": True,
        "ttl_expired": True,
        "stale": True,
        "released": True,
    }:
        return "reservation safety TTL positive control failed"
    return None


def _evaluate_cutover_core(
    *,
    manifest_bytes: bytes,
    candidate_commit: str,
    candidate_is_head: bool,
    worktree_clean: bool,
    evidence: object | None = None,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    """Pure evaluation core; the CLI supplies its own pre/post Git observations."""

    manifest_sha256 = _sha256(manifest_bytes)
    invalid_reasons: list[str] = []
    if not _is_full_git_oid(candidate_commit):
        invalid_reasons.append("candidate_commit is not an explicit full Git object id")
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        manifest = None
        invalid_reasons.append(f"manifest is invalid JSON: {exc}")
    canonical_error = _canonical_manifest_error(manifest_bytes)
    if canonical_error is not None:
        invalid_reasons.append(canonical_error)
    if not isinstance(manifest, dict):
        return {
            "schema_version": 1,
            "candidate_commit": candidate_commit,
            "manifest_sha256": manifest_sha256,
            "evaluation_state": "invalid",
            "cutover_state": "no_go",
            "condition_count": len(EXPECTED_CONDITION_IDS),
            "passed_condition_ids": [],
            "missing_conditions": [],
            "invalid_reasons": sorted(set(invalid_reasons)),
        }

    gate = manifest.get("cutover_gate")
    expected_gate_keys = {
        "schema_version",
        "authority_effect",
        "default_state",
        "unknown_state",
        "go_rule",
        "evidence_contract",
        "required_condition_ids",
        "conditions",
    }
    if not isinstance(gate, dict) or set(gate) != expected_gate_keys:
        invalid_reasons.append("cutover_gate shape is invalid")
        gate = {}
    if gate.get("schema_version") != 1:
        invalid_reasons.append("cutover_gate schema_version is not 1")
    if gate.get("default_state") != "no_go" or gate.get("unknown_state") != "no_go":
        invalid_reasons.append("cutover_gate does not fail closed")
    if gate.get("required_condition_ids") != list(EXPECTED_CONDITION_IDS):
        invalid_reasons.append("required cutover condition ids changed")

    raw_conditions = gate.get("conditions")
    if not isinstance(raw_conditions, list):
        raw_conditions = []
        invalid_reasons.append("cutover conditions must be a list")
    conditions: dict[str, dict[str, Any]] = {}
    for index, condition in enumerate(raw_conditions):
        if not isinstance(condition, dict) or set(condition) != CONDITION_KEYS:
            invalid_reasons.append(f"cutover condition {index} has invalid keys")
            continue
        condition_id = condition["id"]
        if not isinstance(condition_id, str) or condition_id in conditions:
            invalid_reasons.append(f"cutover condition {index} has invalid or duplicate id")
            continue
        expected_kind = "none" if condition["kind"] in {
            "manifest_predicate",
            "checkout_predicate",
        } else condition["evidence_kind"]
        if condition["evidence_kind"] != expected_kind:
            invalid_reasons.append(f"condition {condition_id} has inconsistent evidence kind")
        if condition["kind"] not in {
            "manifest_predicate",
            "checkout_predicate",
            "evidence_gate",
            "follow_up_task",
        }:
            invalid_reasons.append(f"unknown condition kind for {condition_id}")
        conditions[condition_id] = condition
    if tuple(conditions) != EXPECTED_CONDITION_IDS:
        invalid_reasons.append("cutover condition definitions are missing, extra, or reordered")

    artifacts, evidence_errors = _parse_evidence_index(
        evidence,
        evidence_root=evidence_root,
        manifest_sha256=manifest_sha256,
        candidate_commit=candidate_commit,
        conditions=conditions,
    )
    invalid_reasons.extend(evidence_errors)

    raw_decisions = manifest.get("product_decisions")
    decisions: dict[str, dict[str, Any]] = {}
    if isinstance(raw_decisions, list):
        for decision in raw_decisions:
            if not isinstance(decision, dict) or not isinstance(decision.get("id"), str):
                invalid_reasons.append("product decision has invalid shape")
                continue
            decision_id = decision["id"]
            if decision_id in decisions:
                invalid_reasons.append(f"duplicate product decision id: {decision_id}")
                continue
            decisions[decision_id] = decision
    else:
        invalid_reasons.append("product_decisions must be a list")

    raw_tasks = manifest.get("follow_up_tasks")
    tasks: dict[str, dict[str, Any]] = {}
    if isinstance(raw_tasks, list):
        for task in raw_tasks:
            if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                invalid_reasons.append("follow-up task has invalid shape")
                continue
            task_id = task["id"]
            if task_id in tasks:
                invalid_reasons.append(f"duplicate follow-up task id: {task_id}")
                continue
            tasks[task_id] = task
    else:
        invalid_reasons.append("follow_up_tasks must be a list")

    passed: list[str] = []
    missing_conditions: list[dict[str, Any]] = []
    for condition_id in EXPECTED_CONDITION_IDS:
        condition = conditions.get(condition_id)
        if condition is None:
            missing_conditions.append(
                {
                    "id": condition_id,
                    "kind": "missing_definition",
                    "source": "cutover_gate.conditions",
                    "reason": "required condition definition is absent",
                    "remediation": "restore the reviewed condition definition",
                }
            )
            continue

        if condition_id == "product-decisions-selected":
            unselected = sorted(
                decision_id
                for decision_id, decision in decisions.items()
                if decision.get("decision_state") != "selected"
            )
            if tuple(decisions) == EXPECTED_DECISION_IDS and not unselected:
                passed.append(condition_id)
            else:
                missing_conditions.append(
                    _missing(
                        condition,
                        "decision ids or selected states are incomplete",
                        observed={"ids": list(decisions), "unselected": unselected},
                    )
                )
            continue

        if condition_id == "pre-cutover-product-decisions-implemented":
            blockers = sorted(
                decision_id
                for decision_id, decision in decisions.items()
                if decision_id != "D7"
                and decision.get("implementation_state") != "implemented"
            )
            d7 = decisions.get("D7", {})
            d7_exact = (
                d7.get("implementation_state") == "not_implemented"
                and d7.get("implementation_order") == EXPECTED_D7_ORDER
            )
            if tuple(decisions) == EXPECTED_DECISION_IDS and not blockers and d7_exact:
                passed.append(condition_id)
            else:
                missing_conditions.append(
                    _missing(
                        condition,
                        "a pre-cutover decision is not implemented or D7 deferral changed",
                        observed={"blockers": blockers, "d7_exact": d7_exact},
                    )
                )
            continue

        if condition_id == "initial-cutover-difference-set-exact":
            d7_scope = decisions.get("D7", {}).get("scope", {})
            actual_difference_ids = (
                d7_scope.get("cutover_intentional_difference_set")
                if isinstance(d7_scope, dict)
                else None
            )
            intentional = manifest.get("intentional_differences", {})
            safety_entries = (
                intentional.get("safety_entries", [])
                if isinstance(intentional, dict)
                else []
            )
            safety_ids = [
                item.get("id") for item in safety_entries if isinstance(item, dict)
            ]
            if (
                actual_difference_ids == list(EXPECTED_DIFFERENCE_IDS)
                and safety_ids == [EXPECTED_DIFFERENCE_IDS[1]]
            ):
                passed.append(condition_id)
            else:
                missing_conditions.append(
                    _missing(
                        condition,
                        "the initial difference set or safety entry changed",
                        observed={
                            "difference_ids": actual_difference_ids,
                            "safety_ids": safety_ids,
                        },
                    )
                )
            continue

        if condition_id == "candidate-source-bound":
            if candidate_is_head and worktree_clean:
                passed.append(condition_id)
            else:
                missing_conditions.append(
                    _missing(
                        condition,
                        "candidate is not the clean evaluator checkout HEAD",
                        observed={
                            "candidate_is_head": candidate_is_head,
                            "worktree_clean": worktree_clean,
                        },
                    )
                )
            continue

        if condition_id == "product-decision-cutover-approval":
            not_approved = sorted(
                decision_id
                for decision_id in INITIAL_APPROVAL_IDS
                if decisions.get(decision_id, {}).get("cutover_state") != "go"
            )
            d7 = decisions.get("D7", {})
            d7_deferred = (
                d7.get("cutover_state") == "no_go"
                and d7.get("implementation_state") == "not_implemented"
                and d7.get("implementation_order") == EXPECTED_D7_ORDER
            )
            if not not_approved and d7_deferred:
                passed.append(condition_id)
            else:
                missing_conditions.append(
                    _missing(
                        condition,
                        "initial decisions lack approval or D7 is not exactly deferred",
                        observed={
                            "not_approved": not_approved,
                            "d7_exact_deferred_no_go": d7_deferred,
                        },
                    )
                )
            continue

        if condition.get("kind") == "follow_up_task":
            task = tasks.get(condition_id)
            if task is None:
                missing_conditions.append(
                    _missing(condition, "the required follow-up task is absent")
                )
                continue
            if task.get("implementation_order") != "pre_cutover":
                missing_conditions.append(
                    _missing(
                        condition,
                        "follow-up task is not explicitly ordered pre-cutover",
                        observed=task.get("implementation_order"),
                    )
                )
                continue
            if task.get("implementation_state") != "implemented":
                missing_conditions.append(
                    _missing(
                        condition,
                        "follow-up task is not implemented",
                        observed=task.get("implementation_state"),
                    )
                )
                continue
            if task.get("verification_gate") != condition_id:
                missing_conditions.append(
                    _missing(
                        condition,
                        "follow-up task verification gate does not match its condition",
                        observed=task.get("verification_gate"),
                    )
                )
                continue
            if condition.get("evidence_kind") == "unimplemented_v1":
                missing_conditions.append(
                    _missing(
                        condition,
                        "no versioned raw evidence handler exists for this task",
                    )
                )
                continue

        artifact_entry = artifacts.get(condition_id)
        if artifact_entry is None:
            missing_conditions.append(
                _missing(condition, "digest-verified raw machine evidence is missing")
            )
            continue
        _, artifact_content = artifact_entry
        evidence_kind = condition["evidence_kind"]
        try:
            if evidence_kind == "pytest_nodes_v1":
                failure = _check_pytest_nodes(
                    artifact_content,
                    candidate_commit=candidate_commit,
                    expected_nodes=_expected_selected_nodes(manifest),
                )
            elif evidence_kind == "distribution_artifacts_v1":
                if evidence_root is None:
                    failure = "evidence root is absent"
                else:
                    failure = _check_distribution_artifacts(
                        artifact_content,
                        evidence_root=evidence_root,
                        candidate_commit=candidate_commit,
                    )
            elif evidence_kind == "reservation_performance_v1":
                failure = _check_reservation_performance(
                    artifact_content,
                    manifest=manifest,
                    candidate_commit=candidate_commit,
                )
            elif evidence_kind == "reservation_safety_v1":
                failure = _check_reservation_safety(
                    artifact_content,
                    candidate_commit=candidate_commit,
                )
            else:
                failure = "condition has no supported raw evidence handler"
        except (OSError, ValueError) as exc:
            failure = str(exc)
        if failure is None:
            passed.append(condition_id)
        else:
            missing_conditions.append(_missing(condition, failure))

    if invalid_reasons:
        evaluation_state = "invalid"
        cutover_state = "no_go"
    elif tuple(passed) == EXPECTED_CONDITION_IDS and not missing_conditions:
        evaluation_state = "valid"
        cutover_state = "go"
    else:
        evaluation_state = "valid"
        cutover_state = "no_go"
    return {
        "schema_version": 1,
        "candidate_commit": candidate_commit,
        "manifest_sha256": manifest_sha256,
        "evaluation_state": evaluation_state,
        "cutover_state": cutover_state,
        "condition_count": len(EXPECTED_CONDITION_IDS),
        "passed_condition_ids": passed,
        "missing_conditions": missing_conditions,
        "invalid_reasons": sorted(set(invalid_reasons)),
    }


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args()
    try:
        if not _is_full_git_oid(args.candidate_commit):
            raise ValueError("--candidate-commit must be a full lowercase Git object id")
        manifest_bytes = args.manifest.read_bytes()
        evidence = json.loads(args.evidence.read_bytes()) if args.evidence else None
        candidate_commit = _git_output(
            "rev-parse", "--verify", f"{args.candidate_commit}^{{commit}}"
        )
        if candidate_commit != args.candidate_commit:
            raise ValueError("--candidate-commit did not resolve to the exact supplied id")
        head_commit = _git_output("rev-parse", "HEAD")
        worktree_status = _git_output(
            "status", "--porcelain=v1", "--untracked-files=all"
        )
        worktree_clean = not worktree_status
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "evaluation_state": "invalid",
                    "cutover_state": "no_go",
                    "invalid_reasons": [str(exc)],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    result = _evaluate_cutover_core(
        manifest_bytes=manifest_bytes,
        candidate_commit=candidate_commit,
        candidate_is_head=candidate_commit == head_commit,
        worktree_clean=worktree_clean,
        evidence=evidence,
        evidence_root=args.evidence_root,
    )
    try:
        post_head = _git_output("rev-parse", "HEAD")
        post_status = _git_output(
            "status", "--porcelain=v1", "--untracked-files=all"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        post_head = ""
        post_status = "post-check-failed"
        result["invalid_reasons"].append(f"post-evaluation Git check failed: {exc}")
    if post_head != candidate_commit or post_status != worktree_status:
        result["evaluation_state"] = "invalid"
        result["cutover_state"] = "no_go"
        result["invalid_reasons"] = sorted(
            set(
                [
                    *result["invalid_reasons"],
                    "candidate checkout changed or became dirty during evaluation",
                ]
            )
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["evaluation_state"] == "invalid":
        return 2
    return 0 if result["cutover_state"] == "go" else 1


if __name__ == "__main__":
    raise SystemExit(main())
