"""Fail-closed checks for the independent-state product-decision ledger."""

from __future__ import annotations

import ast
import copy
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from verify_artifact import (
    _approved_base_from_manifest,
    _assert_approved_base_reachable,
    _assert_checkout_manifest_binding,
    _assert_expected_divergences_manifest,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PACKAGE_ROOT / "fixtures"
MANIFEST = FIXTURES / "differential-expected-divergences-v2.json"


def _canonical_manifest() -> dict[str, Any]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _verify(manifest: dict[str, Any]) -> None:
    _assert_expected_divergences_manifest(
        json.dumps(manifest, sort_keys=True).encode(),
        (FIXTURES / "compatibility-tools-v1.json").read_bytes(),
        (FIXTURES / "live-tools-list.json").read_bytes(),
        (PACKAGE_ROOT / "src" / "agentstack_mail" / "app.py").read_bytes(),
        artifact="decision-manifest-test",
    )


def _decision(manifest: dict[str, Any], decision_id: str) -> dict[str, Any]:
    return next(
        item for item in manifest["product_decisions"] if item["id"] == decision_id
    )


def _follow_up_task(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    return next(item for item in manifest["follow_up_tasks"] if item["id"] == task_id)


def _post_cutover_task(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    return next(
        item for item in manifest["post_cutover_follow_up_tasks"]
        if item["id"] == task_id
    )


def _drop_d1(manifest: dict[str, Any]) -> None:
    manifest["product_decisions"] = [
        item for item in manifest["product_decisions"] if item["id"] != "D1"
    ]


def _add_d13(manifest: dict[str, Any]) -> None:
    extra = copy.deepcopy(_decision(manifest, "D2"))
    extra.update(id="D13", title="unknown decision")
    manifest["product_decisions"].append(extra)


def _duplicate_d6(manifest: dict[str, Any]) -> None:
    manifest["product_decisions"].append(copy.deepcopy(_decision(manifest, "D6")))


def _drop_decision_state(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D6").pop("decision_state")


def _drop_implementation_state(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D6").pop("implementation_state")


def _drop_cutover_state(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D6").pop("cutover_state")


def _unknown_decision_state(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D6")["decision_state"] = "maybe"


def _unknown_implementation_state(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D6")["implementation_state"] = "partial"


def _unknown_cutover_state(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D6")["cutover_state"] = "maybe"


def _regress_d2_cutover_approval(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D2")["cutover_state"] = "no_go"


def _unselect_d7(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D7")["decision_state"] = "unselected"


def _pretend_d7_is_implemented(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D7")["implementation_state"] = "implemented"


def _approve_d7_cutover(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D7")["cutover_state"] = "go"


def _regress_d1_implementation(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D1")["implementation_state"] = "not_implemented"


def _regress_d2_implementation(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D2")["implementation_state"] = "not_implemented"


def _change_d2_resolution(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D2")["resolution"] = "enforce_expiry"


def _change_d2_scope(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D2")["scope"]["local_reply"] = "identical"


def _drop_d2_verification(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D2")["verification"].pop()


def _drop_implemented_origin(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D1").pop("implementation_origin")


def _unknown_implemented_origin(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D2")["implementation_origin"] = "unknown"


def _add_nonimplemented_origin(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D7")["implementation_origin"] = "pre_existing_parity"


def _change_d7_scope(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D7")["scope"]["active_null_token_name_only_retire"] = "allow"


def _change_d7_implementation_order(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D7")["implementation_order"] = "before_cutover"


def _change_d1_resolution(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D1")["resolution"] = "silent_accept"


def _drop_d1_verification(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D1")["verification"].pop()


def _weaken_d6_comparator(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D6")["comparator_disposition"] = "warn"


def _change_d8_scope(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D8")["scope"]["database_survival"] = "unspecified"


def _change_d9_scope(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D9")["scope"]["durable_recipient_state"] = "atomic"


def _drop_d9_verification(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D9")["verification"].pop()


def _change_d10_scope(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D10")["scope"]["two_process_split_roots"][
        "result_per_trial"
    ] = "one_winner"


def _drop_d10_verification(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D10")["verification"].pop()


def _change_d11_scope(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D11")["scope"]["retired_fetch"] = "reject"


def _drop_d11_verification(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D11")["verification"].pop()


def _unselect_d12(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D12")["decision_state"] = "unselected"


def _change_d12_scope(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D12")["scope"]["delivery_guarantee"] = "exactly_once"


def _drop_d12_verification(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D12")["verification"].pop()


def _drop_safety_difference(manifest: dict[str, Any]) -> None:
    manifest["intentional_differences"]["safety_entries"].clear()


def _weaken_safety_difference(manifest: dict[str, Any]) -> None:
    manifest["intentional_differences"]["safety_entries"][0]["product"] = (
        "probe uncertainty may auto-release"
    )


def _drop_performance_gate(manifest: dict[str, Any]) -> None:
    manifest["performance_gates"].clear()


def _weaken_performance_gate(manifest: dict[str, Any]) -> None:
    manifest["performance_gates"][0]["minimum_complete_runs"] = 0


def _drop_d7_cutover_safety_difference(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D7")["scope"]["cutover_intentional_difference_set"] = [
        "D1"
    ]


def _restore_d12_d1_only_basis(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D12")["scope"]["selection_basis"][0] = (
        "match_frozen_live_and_keep_initial_cutover_difference_count_at_D1_only"
    )


def _drop_follow_up_task(manifest: dict[str, Any]) -> None:
    manifest["follow_up_tasks"].pop()


def _pretend_follow_up_task_is_implemented(manifest: dict[str, Any]) -> None:
    _follow_up_task(manifest, "http-cli-transport-entrypoints")[
        "implementation_state"
    ] = "implemented"


def _merge_d10_liveness_and_performance_gates(manifest: dict[str, Any]) -> None:
    _post_cutover_task(manifest, "d10-diagnostic-liveness-timeout")[
        "performance_separation"
    ] = (
        "the outer deadline is the reservation performance threshold"
    )


def _drop_post_cutover_task(manifest: dict[str, Any]) -> None:
    manifest["post_cutover_follow_up_tasks"].pop()


def _make_post_cutover_task_blocking(manifest: dict[str, Any]) -> None:
    manifest["post_cutover_follow_up_tasks"][0]["cutover_blocking"] = True


def _drop_current_gate_activation_requirement(manifest: dict[str, Any]) -> None:
    manifest["current_gate_activation_requirements"].clear()


def _drop_post_cutover_gate_contract_defect(manifest: dict[str, Any]) -> None:
    manifest["post_cutover_gate_contract_defects"].clear()


def _drop_cutover_required_id(manifest: dict[str, Any]) -> None:
    manifest["cutover_gate"]["required_condition_ids"].pop()


def _drop_cutover_condition(manifest: dict[str, Any]) -> None:
    manifest["cutover_gate"]["conditions"].pop()


def _weaken_cutover_unknown_policy(manifest: dict[str, Any]) -> None:
    manifest["cutover_gate"]["unknown_state"] = "go"


def _weaken_cutover_remediation(manifest: dict[str, Any]) -> None:
    manifest["cutover_gate"]["conditions"][0]["remediation"] = "review later"


def _drop_cutover_approval(manifest: dict[str, Any]) -> None:
    manifest.pop("cutover_approval")


def _change_cutover_descope_approval(manifest: dict[str, Any]) -> None:
    manifest["cutover_approval"]["descope"]["removed_required_condition_ids"].pop()


def test_canonical_decision_ledger_is_accepted() -> None:
    _verify(_canonical_manifest())


def test_cutover_consumer_migration_and_provenance_scope_is_explicit() -> None:
    manifest = _canonical_manifest()
    client_cutover = _follow_up_task(manifest, "mcp-client-reregistration-cutover")
    migration = _follow_up_task(manifest, "data-migration-reconciliation")
    provenance = _post_cutover_task(manifest, "cutover-evidence-provenance-gate")

    client_contract = " ".join(
        [
            *client_cutover["scope"],
            *client_cutover["requirements"],
            client_cutover["acceptance"],
        ]
    )
    assert all(
        term in client_contract
        for term in (
            "Orrery",
            "dashboard",
            "absolute path",
            "AGENTSTACK_MAIL_DB",
            "AGENTSTACK_MCP_URL",
            "AGENTSTACK_SIGNALS_DIR",
            "missing, legacy, wrong, malformed, relative-database",
            "cross-authority-mixed",
            "refuses startup",
        )
    )

    migration_contract = " ".join(
        [*migration["scope"], *migration["requirements"], migration["acceptance"]]
    )
    assert all(
        term in migration_contract
        for term in (
            "without legacy Git history",
            "baseline-commit-A",
            "exclude the legacy .git",
            "one baseline root commit",
            "absence of the legacy .git",
        )
    )

    provenance_contract = " ".join(
        [*provenance["scope"], *provenance["requirements"], provenance["acceptance"]]
    )
    assert all(
        term in provenance_contract
        for term in (
            "baseline-commit-A",
            "sealed supported-consumer inventory",
            "content-redacted preview approval",
            "inventory digest",
            "approver",
            "timestamp",
        )
    )
    assert "exact D1-D6 and D8-D12" in provenance_contract
    assert "product-decision-cutover-approval" in provenance_contract
    assert all(
        term not in client_contract
        for term in ("bearer", "retire", "registration_token")
    )


def test_cutover_task_split_keeps_only_first_day_minimums_blocking() -> None:
    manifest = _canonical_manifest()
    pre = manifest["follow_up_tasks"]
    post = manifest["post_cutover_follow_up_tasks"]

    assert [task["id"] for task in pre] == [
        "reservation-probe-safety-release-gate",
        "http-cli-transport-entrypoints",
        "service-lifecycle-supervision",
        "mcp-client-reregistration-cutover",
        "data-migration-reconciliation",
        "rollback-revert-procedure",
        "notification-layout-consumer-compatibility",
    ]
    assert [task["id"] for task in post][13:15] == [
        "post-authority-reverse-transform",
        "client-key-rename-and-stale-selector-cleanup",
    ]
    assert [task["id"] for task in post][-6:] == [
        "reservation-performance-input-tree-binding",
        "reservation-performance-runner-binding",
        "blocking-ci-environment-pinning",
        "fresh-install-network-separation",
        "fresh-install-startup-port-race",
        "selected-pytest-evidence-executor-contract",
    ]
    assert len(post) == 21
    assert all(
        task["implementation_order"] == "post_cutover"
        and task["cutover_blocking"] is False
        and task["activation_condition"]
        for task in post
    )

    process_boundaries = _post_cutover_task(
        manifest, "external-process-boundary-hardening"
    )
    assert len(process_boundaries["scope"]) == 17
    assert "eight currently unbounded" in " ".join(
        process_boundaries["requirements"]
    )
    assert "post-cutover hardening" in process_boundaries["activation_condition"]
    assert "eight unbounded calls become urgent" in process_boundaries[
        "activation_condition"
    ]
    assert process_boundaries["cutover_blocking"] is False

    safety = _follow_up_task(manifest, "reservation-probe-safety-release-gate")
    assert safety["implementation_state"] == "implemented"
    assert all(
        task["implementation_state"] == "not_implemented"
        for task in pre
        if task["id"] in {
            "http-cli-transport-entrypoints",
            "service-lifecycle-supervision",
            "mcp-client-reregistration-cutover",
        }
    )
    descoped_ids = {
        "data-migration-reconciliation",
        "rollback-revert-procedure",
        "notification-layout-consumer-compatibility",
    }
    assert {
        task["id"]
        for task in pre
        if task["implementation_state"] == "descoped_documentation_only"
    } == descoped_ids
    assert all(
        task["descoped"]["approved_by"] == "maintainer"
        and task["descoped"]["date"] == "2026-08-15"
        and task["descoped"]["disposition"]
        for task in pre
        if task["id"] in descoped_ids
    )

    http = _follow_up_task(manifest, "http-cli-transport-entrypoints")
    service = _follow_up_task(manifest, "service-lifecycle-supervision")
    client = _follow_up_task(manifest, "mcp-client-reregistration-cutover")
    migration = _follow_up_task(manifest, "data-migration-reconciliation")
    rollback = _follow_up_task(manifest, "rollback-revert-procedure")
    assert "exact candidate wheel" in " ".join(http["requirements"])
    # Historical: this pins the requirement as written on 2026-08-15. The
    # surface has since grown to 25 (unretire_agent), recorded in the
    # post-cutover ledger rather than by editing what was decided then.
    assert "exact 24-tool boundary" in " ".join(http["requirements"])
    assert "never share the provider identity, port, database, archive, or signals" in " ".join(
        service["requirements"]
    )
    assert "launchd-equivalent SIGTERM" in " ".join(service["requirements"])
    client_contract = " ".join([*client["requirements"], client["acceptance"]])
    assert "preserve Claude mcp-agent-mail and Codex agent-mail" in client_contract
    assert "rather than from a client-visible key" in client_contract
    assert "one bounded read-only collection run" in client_contract
    assert "stopped Claude and Codex child configs" in client_contract
    assert "never have two concurrent writers" in " ".join(migration["requirements"])
    assert "post-authority" not in " ".join(
        [*rollback["scope"], *rollback["requirements"], rollback["acceptance"]]
    )
    reverse = _post_cutover_task(manifest, "post-authority-reverse-transform")
    assert "durable post-baseline records" in reverse["acceptance"]
    cleanup = _post_cutover_task(
        manifest,
        "client-key-rename-and-stale-selector-cleanup",
    )
    assert "21 observed stale Claude allow occurrences" in " ".join(cleanup["scope"])

    assert manifest["cutover_gate"]["required_condition_ids"][-4:] == [
        task["id"] for task in pre if task["id"] not in descoped_ids
    ]
    assert manifest["cutover_approval"]["descope"][
        "removed_required_condition_ids"
    ] == [
        "data-migration-reconciliation",
        "rollback-revert-procedure",
        "notification-layout-consumer-compatibility",
    ]
    assert [
        item["id"] for item in manifest["current_gate_activation_requirements"]
    ] == ["approved-base-persistent-ref-reachability"]
    assert [
        item["id"] for item in manifest["post_cutover_gate_contract_defects"]
    ] == ["reservation-performance-producer-verifier-contract"]


def _git(repository: Path, *arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_approved_base_requires_a_persistent_ref_and_distinct_diagnostics(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Artifact Test")
    _git(repository, "config", "user.email", "artifact@example.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "base")
    reachable = _git(repository, "rev-parse", "HEAD")

    _assert_approved_base_reachable(
        reachable,
        repository_root=repository,
        artifact="test",
    )

    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    unreachable = _git(
        repository,
        "commit-tree",
        tree,
        "-p",
        reachable,
        input_text="unreachable\n",
    )
    _git(repository, "update-ref", "refs/stash", unreachable)
    with pytest.raises(
        SystemExit,
        match="object exists but is unreachable from persistent refs/heads",
    ):
        _assert_approved_base_reachable(
            unreachable,
            repository_root=repository,
            artifact="test",
        )

    _git(repository, "tag", "approved-base-test", unreachable)
    _assert_approved_base_reachable(
        unreachable,
        repository_root=repository,
        artifact="test",
    )

    with pytest.raises(
        SystemExit,
        match="object is unavailable; the checkout may be shallow or the history was not fetched",
    ):
        _assert_approved_base_reachable(
            "f" * 40,
            repository_root=repository,
            artifact="test",
        )


def test_artifact_manifest_must_byte_match_checkout_fixture(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    fixtures = package_root / "fixtures"
    fixtures.mkdir(parents=True)
    canonical = MANIFEST.read_bytes()
    (fixtures / MANIFEST.name).write_bytes(canonical)

    with pytest.raises(
        SystemExit,
        match="does not byte-match the checkout fixture",
    ):
        _assert_checkout_manifest_binding(
            canonical + b"\n",
            repository_root=PACKAGE_ROOT.parents[1],
            package_root=package_root,
            artifact="test",
        )


def test_approved_base_is_fixture_owned_full_sha() -> None:
    manifest_content = MANIFEST.read_bytes()
    approved_base = _approved_base_from_manifest(
        manifest_content,
        artifact="test",
    )
    assert approved_base == _canonical_manifest()["baselines"]["core"]["approved_base"]
    assert re.fullmatch(r"[0-9a-f]{40}", approved_base)

    manifest = _canonical_manifest()
    manifest["baselines"]["core"]["approved_base"] = "not-a-full-sha"
    with pytest.raises(SystemExit, match="must be one full lowercase 40-hex commit"):
        _approved_base_from_manifest(
            json.dumps(manifest).encode(),
            artifact="test",
        )


def test_all_decisions_have_independent_required_states() -> None:
    decisions = _canonical_manifest()["product_decisions"]
    assert len(decisions) == 12
    assert {item["id"] for item in decisions} == {f"D{index}" for index in range(1, 13)}
    assert {
        item["id"] for item in decisions if item["decision_state"] == "selected"
    } == {
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D7",
        "D8",
        "D9",
        "D10",
        "D11",
        "D12",
    }
    assert {
        (
            item["decision_state"],
            item["implementation_state"],
            item["cutover_state"],
        )
        for item in decisions
    } == {
        ("selected", "implemented", "go"),
        ("selected", "not_implemented", "no_go"),
    }
    assert {
        item["id"] for item in decisions if item["cutover_state"] == "go"
    } == {
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D8",
        "D9",
        "D10",
        "D11",
        "D12",
    }
    assert {
        item["id"] for item in decisions if item["cutover_state"] == "no_go"
    } == {"D7"}


def test_implemented_decision_verification_nodes_exist_as_top_level_tests() -> None:
    decisions = _canonical_manifest()["product_decisions"]
    implemented = [
        item for item in decisions if item["implementation_state"] == "implemented"
    ]
    assert [item["id"] for item in implemented] == [
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
        "D8",
        "D9",
        "D10",
        "D11",
        "D12",
    ]
    assert {item["id"]: item["implementation_origin"] for item in implemented} == {
        "D1": "core_change",
        "D2": "pre_existing_parity",
        "D3": "pre_existing_parity",
        "D4": "pre_existing_parity",
        "D5": "pre_existing_parity",
        "D6": "pre_existing_parity",
        "D8": "pre_existing_parity",
        "D9": "pre_existing_parity",
        "D10": "pre_existing_parity",
        "D11": "pre_existing_parity",
        "D12": "pre_existing_parity",
    }
    assert all(
        "implementation_origin" not in item
        for item in decisions
        if item["implementation_state"] != "implemented"
    )
    node_ids = [node_id for item in implemented for node_id in item["verification"]]
    assert len(node_ids) == len(set(node_ids))

    for node_id in node_ids:
        file_text, separator, function_name = node_id.partition("::")
        assert separator == "::"
        assert function_name.startswith("test_")
        relative = Path(file_text)
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        assert relative.parts[:1] == ("tests",)
        source = PACKAGE_ROOT / relative
        assert source.is_file(), node_id
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        top_level_tests = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert function_name in top_level_tests, node_id


@pytest.mark.parametrize(
    ("mutate", "expected_message"),
    (
        (_drop_d1, "product decision ledger ids changed: missing=['D1'], extra=[]"),
        (_add_d13, "product decision ledger ids changed: missing=[], extra=['D13']"),
        (_duplicate_d6, "product decisions contain duplicate ids"),
        (_drop_decision_state, "selected product decision D6 changed"),
        (
            _drop_implementation_state,
            "non-implemented decision D6 must not have origin",
        ),
        (_drop_cutover_state, "selected product decision D6 changed"),
        (_unknown_decision_state, "selected product decision D6 changed"),
        (
            _unknown_implementation_state,
            "non-implemented decision D6 must not have origin",
        ),
        (_unknown_cutover_state, "selected product decision D6 changed"),
        (_regress_d2_cutover_approval, "selected product decision D2 changed"),
        (_unselect_d7, "selected product decision D7 changed"),
        (
            _pretend_d7_is_implemented,
            "implemented decision D7 has invalid or missing origin",
        ),
        (_approve_d7_cutover, "selected product decision D7 changed"),
        (
            _regress_d1_implementation,
            "non-implemented decision D1 must not have origin",
        ),
        (
            _regress_d2_implementation,
            "non-implemented decision D2 must not have origin",
        ),
        (_change_d2_resolution, "selected product decision D2 changed"),
        (_change_d2_scope, "selected product decision D2 changed"),
        (_drop_d2_verification, "selected product decision D2 changed"),
        (
            _drop_implemented_origin,
            "implemented decision D1 has invalid or missing origin",
        ),
        (
            _unknown_implemented_origin,
            "implemented decision D2 has invalid or missing origin",
        ),
        (
            _add_nonimplemented_origin,
            "non-implemented decision D7 must not have origin",
        ),
        (_change_d7_scope, "selected product decision D7 changed"),
        (_change_d7_implementation_order, "selected product decision D7 changed"),
        (_change_d1_resolution, "selected product decision D1 changed"),
        (_drop_d1_verification, "selected product decision D1 changed"),
        (_weaken_d6_comparator, "selected product decision D6 changed"),
        (_change_d8_scope, "selected product decision D8 changed"),
        (_change_d9_scope, "selected product decision D9 changed"),
        (_drop_d9_verification, "selected product decision D9 changed"),
        (_change_d10_scope, "selected product decision D10 changed"),
        (_drop_d10_verification, "selected product decision D10 changed"),
        (_change_d11_scope, "selected product decision D11 changed"),
        (_drop_d11_verification, "selected product decision D11 changed"),
        (_unselect_d12, "selected product decision D12 changed"),
        (_change_d12_scope, "selected product decision D12 changed"),
        (_drop_d12_verification, "selected product decision D12 changed"),
        (_drop_safety_difference, "safety differences changed"),
        (_weaken_safety_difference, "safety differences changed"),
        (_drop_performance_gate, "performance gates changed"),
        (_weaken_performance_gate, "performance gates changed"),
        (_drop_d7_cutover_safety_difference, "selected product decision D7 changed"),
        (_restore_d12_d1_only_basis, "selected product decision D12 changed"),
        (_drop_follow_up_task, "follow-up tasks changed"),
        (_pretend_follow_up_task_is_implemented, "follow-up tasks changed"),
        (_merge_d10_liveness_and_performance_gates, "post-cutover tasks changed"),
        (_drop_post_cutover_task, "post-cutover tasks changed"),
        (_make_post_cutover_task_blocking, "post-cutover tasks changed"),
        (
            _drop_current_gate_activation_requirement,
            "current gate activation requirements changed",
        ),
        (
            _drop_post_cutover_gate_contract_defect,
            "post-cutover gate contract defects changed",
        ),
        (_drop_cutover_required_id, "cutover gate is not fail-closed"),
        (_drop_cutover_condition, "cutover condition ids changed"),
        (_weaken_cutover_unknown_policy, "cutover gate is not fail-closed"),
        (_weaken_cutover_remediation, "cutover gate changed"),
        (
            _drop_cutover_approval,
            "divergence manifest top-level keys do not match v2",
        ),
        (_change_cutover_descope_approval, "cutover approval changed"),
    ),
)
def test_decision_ledger_mutations_fail_closed(
    mutate: Callable[[dict[str, Any]], None],
    expected_message: str,
) -> None:
    manifest = copy.deepcopy(_canonical_manifest())
    mutate(manifest)

    with pytest.raises(SystemExit, match=re.escape(expected_message)):
        _verify(manifest)
