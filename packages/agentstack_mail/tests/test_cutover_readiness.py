"""Mutation tests for the read-only cutover-readiness evaluator."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cutover_readiness import (
    EXPECTED_CONDITION_IDS,
    _canonical_json_sha256,
    _check_pytest_nodes,
    _check_reservation_performance,
    _check_reservation_safety,
    _definition_sha256,
    _evaluate_cutover_core,
    _is_full_git_oid,
    _parse_evidence_index,
    main,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PACKAGE_ROOT / "fixtures" / "differential-expected-divergences-v2.json"
CANDIDATE_COMMIT = "1" * 40


def _manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _evaluate(
    manifest_bytes: bytes,
    *,
    candidate_is_head: bool = True,
    worktree_clean: bool = True,
    evidence: object | None = None,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    return _evaluate_cutover_core(
        manifest_bytes=manifest_bytes,
        candidate_commit=CANDIDATE_COMMIT,
        candidate_is_head=candidate_is_head,
        worktree_clean=worktree_clean,
        evidence=evidence,
        evidence_root=evidence_root,
    )


def test_checked_in_ledger_is_valid_no_go_with_exact_missing_tasks() -> None:
    result = _evaluate(MANIFEST.read_bytes())

    assert result["evaluation_state"] == "valid"
    assert result["cutover_state"] == "no_go"
    assert result["invalid_reasons"] == []
    assert result["condition_count"] == 14
    assert result["passed_condition_ids"] == list(EXPECTED_CONDITION_IDS[:4])
    assert [item["id"] for item in result["missing_conditions"]] == list(
        EXPECTED_CONDITION_IDS[4:]
    )
    approval = result["missing_conditions"][0]
    assert approval["observed"]["not_approved"] == [
        "D1",
        "D10",
        "D11",
        "D12",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D8",
        "D9",
    ]
    assert approval["observed"]["d7_exact_deferred_no_go"] is True
    safety = next(
        item
        for item in result["missing_conditions"]
        if item["id"] == "reservation-probe-safety-release-gate"
    )
    assert safety["reason"] == "digest-verified raw machine evidence is missing"
    http = next(
        item
        for item in result["missing_conditions"]
        if item["id"] == "http-cli-transport-entrypoints"
    )
    assert http["observed"] == "not_implemented"


def test_dirty_or_different_candidate_is_no_go() -> None:
    dirty = _evaluate(MANIFEST.read_bytes(), worktree_clean=False)
    different = _evaluate(MANIFEST.read_bytes(), candidate_is_head=False)

    for result in (dirty, different):
        assert result["evaluation_state"] == "valid"
        assert result["cutover_state"] == "no_go"
        candidate = next(
            item
            for item in result["missing_conditions"]
            if item["id"] == "candidate-source-bound"
        )
        assert candidate["reason"] == "candidate is not the clean evaluator checkout HEAD"


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("1" * 40, True),
        ("a" * 64, True),
        ("HEAD", False),
        ("A" * 40, False),
        ("1" * 39, False),
        ("g" * 40, False),
    ),
)
def test_candidate_commit_requires_full_lowercase_git_oid(
    value: str,
    expected: bool,
) -> None:
    assert _is_full_git_oid(value) is expected


def test_cli_symbolic_candidate_is_structured_invalid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["cutover_readiness.py", "--candidate-commit", "HEAD"],
    )

    assert main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["evaluation_state"] == "invalid"
    assert result["cutover_state"] == "no_go"


def test_cli_post_git_oserror_is_structured_invalid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outputs: list[object] = [CANDIDATE_COMMIT, CANDIDATE_COMMIT, "", OSError("gone")]

    def fake_git_output(*_args: str) -> str:
        value = outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, str)
        return value

    monkeypatch.setattr("cutover_readiness._git_output", fake_git_output)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cutover_readiness.py",
            "--candidate-commit",
            CANDIDATE_COMMIT,
            "--manifest",
            str(MANIFEST),
        ],
    )

    assert main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["evaluation_state"] == "invalid"
    assert result["cutover_state"] == "no_go"
    assert any("post-evaluation Git check failed" in reason for reason in result["invalid_reasons"])


@pytest.mark.parametrize(
    "mutate",
    (
        lambda manifest: manifest["cutover_gate"]["required_condition_ids"].pop(),
        lambda manifest: manifest["cutover_gate"]["conditions"].pop(),
        lambda manifest: manifest["cutover_gate"]["conditions"].clear(),
        lambda manifest: manifest["cutover_gate"]["conditions"].append(
            copy.deepcopy(manifest["cutover_gate"]["conditions"][0])
        ),
        lambda manifest: manifest["cutover_gate"]["conditions"][0].update(
            kind="evidence_gate", evidence_kind="pytest_nodes_v1"
        ),
        lambda manifest: manifest["cutover_gate"].update(unknown_state="go"),
    ),
)
def test_policy_mutations_are_invalid_no_go(
    mutate: Any,
) -> None:
    manifest = _manifest()
    mutate(manifest)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()

    result = _evaluate(manifest_bytes)

    assert result["evaluation_state"] == "invalid"
    assert result["cutover_state"] == "no_go"
    assert result["invalid_reasons"]


def test_manifest_bytes_are_the_only_manifest_input() -> None:
    with pytest.raises(TypeError):
        _evaluate_cutover_core(  # type: ignore[call-arg]
            manifest=_manifest(),
            manifest_bytes=MANIFEST.read_bytes(),
            candidate_commit=CANDIDATE_COMMIT,
            candidate_is_head=True,
            worktree_clean=True,
        )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_exact_pytest_node_report_passes_and_skip_fails(tmp_path: Path) -> None:
    report_path = tmp_path / "nodes.json"
    expected = ["tests/a.py::test_a", "tests/b.py::test_b"]
    report = {
        "schema_version": 1,
        "candidate_commit": CANDIDATE_COMMIT,
        "nodes": [
            {"node_id": node_id, "outcome": "passed"} for node_id in expected
        ],
    }
    _write_json(report_path, report)
    assert (
        _check_pytest_nodes(
            report_path.read_bytes(),
            candidate_commit=CANDIDATE_COMMIT,
            expected_nodes=expected,
        )
        is None
    )

    report["nodes"][1]["outcome"] = "skipped"
    _write_json(report_path, report)
    assert "did not pass" in str(
        _check_pytest_nodes(
            report_path.read_bytes(),
            candidate_commit=CANDIDATE_COMMIT,
            expected_nodes=expected,
        )
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "duplicate",
        "extra",
        "zero",
        "unknown",
    ),
)
def test_pytest_node_inventory_mutations_fail(tmp_path: Path, mutation: str) -> None:
    report_path = tmp_path / "nodes.json"
    expected = ["tests/a.py::test_a", "tests/b.py::test_b"]
    nodes = [{"node_id": node_id, "outcome": "passed"} for node_id in expected]
    if mutation == "missing":
        nodes.pop()
    elif mutation == "duplicate":
        nodes.append(copy.deepcopy(nodes[0]))
    elif mutation == "extra":
        nodes.append({"node_id": "tests/c.py::test_c", "outcome": "passed"})
    elif mutation == "zero":
        nodes.clear()
        expected = []
    else:
        nodes[0]["outcome"] = "green"
    _write_json(
        report_path,
        {
            "schema_version": 1,
            "candidate_commit": CANDIDATE_COMMIT,
            "nodes": nodes,
        },
    )
    assert _check_pytest_nodes(
        report_path.read_bytes(),
        candidate_commit=CANDIDATE_COMMIT,
        expected_nodes=expected,
    )


def _performance_report(script_sha256: str) -> dict[str, Any]:
    input_paths = [f"10_Reference/path-{index:02d}.md" for index in range(57)]
    runs = [
        {
            "index": index,
            "process_id": 1000 + index,
            "wall_seconds": 6.0,
            "matched": 57,
            "probe_complete": 57,
            "results": [
                {
                    "path": path,
                    "matched": True,
                    "probe_complete": True,
                    "filesystem_present": True,
                    "git_present": True,
                }
                for path in input_paths
            ],
        }
        for index in range(5)
    ]
    return {
        "schema_version": 1,
        "candidate_commit": CANDIDATE_COMMIT,
        "gate_id": "reservation-activity-57-path-wall-time",
        "input_paths": input_paths,
        "input_sha256": _canonical_json_sha256(input_paths),
        "result_shape_sha256": _canonical_json_sha256(
            [run["results"] for run in runs]
        ),
        "script_sha256": script_sha256,
        "runs": runs,
    }


def _refresh_result_shape(report: dict[str, Any]) -> None:
    report["result_shape_sha256"] = _canonical_json_sha256(
        [run["results"] for run in report["runs"]]
    )


def _performance_manifest(script: str) -> dict[str, Any]:
    return {"performance_gates": [{
        "id": "reservation-activity-57-path-wall-time",
        "script": script,
        "input": {
            "workspace": "/unused/in/unit/test",
            "count": 57,
            "source_command": "git ls-files -z",
            "preferred_prefix": "10_Reference/",
            "preferred_extensions": [".md", ".png", ".jpg", ".jpeg", ".webp"],
            "ordering": "unicode_codepoint_ascending",
            "sampling": "reviewed exact algorithm",
        },
        "repetitions": 5,
        "fresh_process_each_run": True,
        "threshold_seconds": 6.0,
        "minimum_complete_runs": 3,
    }]}


def test_performance_handler_recomputes_boundary_and_completeness(
    tmp_path: Path,
) -> None:
    script = "packages/agentstack_mail/tests/cutover_readiness.py"
    script_sha = hashlib.sha256((PACKAGE_ROOT.parents[1] / script).read_bytes()).hexdigest()
    manifest = _performance_manifest(script)
    report_path = tmp_path / "performance.json"
    report = _performance_report(script_sha)
    _write_json(report_path, report)
    assert _check_reservation_performance(
        report_path.read_bytes(),
        manifest=manifest,
        candidate_commit=CANDIDATE_COMMIT,
        tracked_paths=report["input_paths"],
    ) is None

    for run in report["runs"][2:]:
        run["probe_complete"] = 56
        run["results"][-1]["probe_complete"] = False
    _refresh_result_shape(report)
    _write_json(report_path, report)
    assert "fewer complete runs" in str(
        _check_reservation_performance(
            report_path.read_bytes(),
            manifest=manifest,
            candidate_commit=CANDIDATE_COMMIT,
            tracked_paths=report["input_paths"],
        )
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("wall_seconds", 6.000001, "median exceeds"),
        ("wall_seconds", float("inf"), "not finite"),
        ("matched", 56, "fewer complete runs"),
        ("probe_complete", 56, "fewer complete runs"),
    ),
)
def test_performance_raw_mutations_fail(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    script = "packages/agentstack_mail/tests/cutover_readiness.py"
    script_sha = hashlib.sha256((PACKAGE_ROOT.parents[1] / script).read_bytes()).hexdigest()
    manifest = _performance_manifest(script)
    report = _performance_report(script_sha)
    for run in report["runs"]:
        run[field] = value
        if field in {"matched", "probe_complete"}:
            run["results"][-1][field] = False
    if field in {"matched", "probe_complete"}:
        _refresh_result_shape(report)
    report_path = tmp_path / "performance.json"
    _write_json(report_path, report)
    assert message in str(
        _check_reservation_performance(
            report_path.read_bytes(),
            manifest=manifest,
            candidate_commit=CANDIDATE_COMMIT,
            tracked_paths=report["input_paths"],
        )
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("input_digest", "input fingerprint mismatch"),
        ("input_inventory", "canonical Git sample"),
        ("result_digest", "result fingerprint mismatch"),
        ("result_path", "result paths changed"),
        ("aggregate", "aggregate is inconsistent"),
        ("run_count", "run count differs"),
        ("duplicate_index", "run indexes differ"),
        ("duplicate_process", "distinct processes"),
    ),
)
def test_performance_inventory_and_fingerprint_mutations_fail(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    script = "packages/agentstack_mail/tests/cutover_readiness.py"
    script_sha = hashlib.sha256((PACKAGE_ROOT.parents[1] / script).read_bytes()).hexdigest()
    manifest = _performance_manifest(script)
    report = _performance_report(script_sha)
    if mutation == "input_digest":
        report["input_sha256"] = "0" * 64
    elif mutation == "input_inventory":
        report["input_paths"][-1] = "10_Reference/replaced.md"
        report["input_sha256"] = _canonical_json_sha256(report["input_paths"])
    elif mutation == "result_digest":
        report["result_shape_sha256"] = "0" * 64
    elif mutation == "result_path":
        report["runs"][0]["results"][0]["path"] = "wrong"
        _refresh_result_shape(report)
    elif mutation == "aggregate":
        report["runs"][0]["matched"] = 56
    elif mutation == "run_count":
        report["runs"].pop()
        _refresh_result_shape(report)
    elif mutation == "duplicate_index":
        report["runs"][4]["index"] = 3
    else:
        report["runs"][4]["process_id"] = report["runs"][3]["process_id"]
    report_path = tmp_path / "performance.json"
    _write_json(report_path, report)
    assert message in str(
        _check_reservation_performance(
            report_path.read_bytes(),
            manifest=manifest,
            candidate_commit=CANDIDATE_COMMIT,
            tracked_paths=[
                f"10_Reference/path-{index:02d}.md" for index in range(57)
            ],
        )
    )


def _safety_report() -> dict[str, Any]:
    controls = []
    for control_id in ("timeout", "error", "filesystem_incomplete"):
        controls.append(
            {
                "id": control_id,
                "probe_complete": False,
                "activity_unknown": True,
                "ttl_expired": False,
                "stale": False,
                "released": False,
            }
        )
    controls.append(
        {
            "id": "ttl_expiry",
            "probe_complete": False,
            "activity_unknown": True,
            "ttl_expired": True,
            "stale": True,
            "released": True,
        }
    )
    return {
        "schema_version": 1,
        "candidate_commit": CANDIDATE_COMMIT,
        "gate_id": "reservation-probe-incomplete-fail-closed",
        "controls": controls,
    }


def test_safety_handler_checks_adverse_and_ttl_positive_controls(tmp_path: Path) -> None:
    report_path = tmp_path / "safety.json"
    report = _safety_report()
    _write_json(report_path, report)
    assert _check_reservation_safety(
        report_path.read_bytes(), candidate_commit=CANDIDATE_COMMIT
    ) is None

    report["controls"][0]["released"] = True
    _write_json(report_path, report)
    assert "not fail-closed" in str(
        _check_reservation_safety(
            report_path.read_bytes(), candidate_commit=CANDIDATE_COMMIT
        )
    )

    report = _safety_report()
    report["controls"][3]["released"] = False
    _write_json(report_path, report)
    assert "positive control failed" in str(
        _check_reservation_safety(
            report_path.read_bytes(), candidate_commit=CANDIDATE_COMMIT
        )
    )


def test_caller_authored_status_field_is_invalid(tmp_path: Path) -> None:
    manifest = _manifest()
    condition = next(
        item
        for item in manifest["cutover_gate"]["conditions"]
        if item["id"] == "selected-behavior-release-gate"
    )
    raw_path = tmp_path / "nodes.json"
    raw_path.write_text("{}", encoding="utf-8")
    evidence = {
        "schema_version": 1,
        "candidate_commit": CANDIDATE_COMMIT,
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "artifacts": [
            {
                "condition_id": condition["id"],
                "definition_sha256": _definition_sha256(condition),
                "kind": condition["evidence_kind"],
                "path": raw_path.name,
                "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "status": "pass",
            }
        ],
    }
    result = _evaluate(
        MANIFEST.read_bytes(),
        evidence=evidence,
        evidence_root=tmp_path,
    )
    assert result["evaluation_state"] == "invalid"
    assert result["cutover_state"] == "no_go"


def test_implementation_state_cannot_green_unimplemented_handler() -> None:
    manifest = _manifest()
    task_id = "http-cli-transport-entrypoints"
    next(item for item in manifest["follow_up_tasks"] if item["id"] == task_id)[
        "implementation_state"
    ] = "implemented"
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()

    result = _evaluate(manifest_bytes)

    assert result["evaluation_state"] == "invalid"
    assert result["cutover_state"] == "no_go"


@pytest.mark.parametrize(
    "mutation",
    ("cutover_go", "implemented", "wrong_order"),
)
def test_d7_deferral_mutations_cannot_green_cutover(mutation: str) -> None:
    manifest = _manifest()
    d7 = next(item for item in manifest["product_decisions"] if item["id"] == "D7")
    if mutation == "cutover_go":
        d7["cutover_state"] = "go"
    elif mutation == "implemented":
        d7["implementation_state"] = "implemented"
    else:
        d7["implementation_order"] = "pre_cutover"

    result = _evaluate(json.dumps(manifest, sort_keys=True).encode())

    assert result["evaluation_state"] == "invalid"
    assert result["cutover_state"] == "no_go"


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_candidate",
        "wrong_manifest",
        "wrong_definition",
        "wrong_artifact_digest",
        "unsafe_path",
        "duplicate_condition",
        "wrong_kind",
        "unknown_condition",
    ),
)
def test_evidence_envelope_binding_mutations_are_invalid(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest = _manifest()
    condition = next(
        item
        for item in manifest["cutover_gate"]["conditions"]
        if item["id"] == "selected-behavior-release-gate"
    )
    nodes = [
        node
        for decision in manifest["product_decisions"]
        if decision["implementation_state"] == "implemented"
        for node in decision["verification"]
    ]
    raw_path = tmp_path / "nodes.json"
    _write_json(
        raw_path,
        {
            "schema_version": 1,
            "candidate_commit": CANDIDATE_COMMIT,
            "nodes": [
                {"node_id": node_id, "outcome": "passed"} for node_id in nodes
            ],
        },
    )
    record = {
        "condition_id": condition["id"],
        "definition_sha256": _definition_sha256(condition),
        "kind": condition["evidence_kind"],
        "path": raw_path.name,
        "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
    }
    evidence = {
        "schema_version": 1,
        "candidate_commit": CANDIDATE_COMMIT,
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "artifacts": [record],
    }
    if mutation == "wrong_candidate":
        evidence["candidate_commit"] = "2" * 40
    elif mutation == "wrong_manifest":
        evidence["manifest_sha256"] = "2" * 64
    elif mutation == "wrong_definition":
        record["definition_sha256"] = "2" * 64
    elif mutation == "wrong_artifact_digest":
        record["sha256"] = "2" * 64
    elif mutation == "unsafe_path":
        record["path"] = "../nodes.json"
    elif mutation == "duplicate_condition":
        evidence["artifacts"].append(copy.deepcopy(record))
    elif mutation == "wrong_kind":
        record["kind"] = "reservation_safety_v1"
    else:
        record["condition_id"] = "unknown"

    result = _evaluate(
        MANIFEST.read_bytes(),
        evidence=evidence,
        evidence_root=tmp_path,
    )

    assert result["evaluation_state"] == "invalid"
    assert result["cutover_state"] == "no_go"


def test_evidence_handlers_receive_the_exact_hashed_bytes(tmp_path: Path) -> None:
    manifest = _manifest()
    condition = next(
        item
        for item in manifest["cutover_gate"]["conditions"]
        if item["id"] == "selected-behavior-release-gate"
    )
    raw_path = tmp_path / "nodes.json"
    original = {
        "schema_version": 1,
        "candidate_commit": CANDIDATE_COMMIT,
        "nodes": [{"node_id": "tests/a.py::test_a", "outcome": "passed"}],
    }
    _write_json(raw_path, original)
    evidence = {
        "schema_version": 1,
        "candidate_commit": CANDIDATE_COMMIT,
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "artifacts": [{
            "condition_id": condition["id"],
            "definition_sha256": _definition_sha256(condition),
            "kind": condition["evidence_kind"],
            "path": raw_path.name,
            "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        }],
    }
    artifacts, errors = _parse_evidence_index(
        evidence,
        evidence_root=tmp_path,
        manifest_sha256=evidence["manifest_sha256"],
        candidate_commit=CANDIDATE_COMMIT,
        conditions={condition["id"]: condition},
    )
    assert errors == []
    _, immutable_content = artifacts[condition["id"]]

    original["nodes"][0]["outcome"] = "failed"
    _write_json(raw_path, original)

    assert _check_pytest_nodes(
        immutable_content,
        candidate_commit=CANDIDATE_COMMIT,
        expected_nodes=["tests/a.py::test_a"],
    ) is None
