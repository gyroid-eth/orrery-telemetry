"""Fail-closed checks for the independent-state product-decision ledger."""

from __future__ import annotations

import ast
import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from verify_artifact import _assert_expected_divergences_manifest

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
    _decision(manifest, "D6")["cutover_state"] = "go"


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


def _select_d10(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D10")["decision_state"] = "selected"


def _select_d11(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D11")["decision_state"] = "selected"


def _select_d12(manifest: dict[str, Any]) -> None:
    _decision(manifest, "D12")["decision_state"] = "selected"


def test_canonical_decision_ledger_is_accepted() -> None:
    _verify(_canonical_manifest())


def test_all_decisions_have_independent_required_states() -> None:
    decisions = _canonical_manifest()["product_decisions"]
    assert len(decisions) == 12
    assert {item["id"] for item in decisions} == {f"D{index}" for index in range(1, 13)}
    assert {
        item["id"] for item in decisions if item["decision_state"] == "selected"
    } == {"D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"}
    assert {
        (
            item["decision_state"],
            item["implementation_state"],
            item["cutover_state"],
        )
        for item in decisions
    } == {
        ("selected", "implemented", "no_go"),
        ("selected", "not_implemented", "no_go"),
        ("unselected", "not_implemented", "no_go"),
    }


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
        (_drop_implementation_state, "non-implemented decision D6 must not have origin"),
        (_drop_cutover_state, "selected product decision D6 changed"),
        (_unknown_decision_state, "selected product decision D6 changed"),
        (_unknown_implementation_state, "non-implemented decision D6 must not have origin"),
        (_unknown_cutover_state, "selected product decision D6 changed"),
        (_unselect_d7, "selected product decision D7 changed"),
        (_pretend_d7_is_implemented, "implemented decision D7 has invalid or missing origin"),
        (_approve_d7_cutover, "selected product decision D7 changed"),
        (_regress_d1_implementation, "non-implemented decision D1 must not have origin"),
        (_regress_d2_implementation, "non-implemented decision D2 must not have origin"),
        (_change_d2_resolution, "selected product decision D2 changed"),
        (_change_d2_scope, "selected product decision D2 changed"),
        (_drop_d2_verification, "selected product decision D2 changed"),
        (_drop_implemented_origin, "implemented decision D1 has invalid or missing origin"),
        (_unknown_implemented_origin, "implemented decision D2 has invalid or missing origin"),
        (_add_nonimplemented_origin, "non-implemented decision D7 must not have origin"),
        (_change_d7_scope, "selected product decision D7 changed"),
        (_change_d1_resolution, "selected product decision D1 changed"),
        (_drop_d1_verification, "selected product decision D1 changed"),
        (_weaken_d6_comparator, "selected product decision D6 changed"),
        (_change_d8_scope, "selected product decision D8 changed"),
        (_change_d9_scope, "selected product decision D9 changed"),
        (_drop_d9_verification, "selected product decision D9 changed"),
        (_select_d10, "unselected decision D10 changed"),
        (_select_d11, "unselected decision D11 changed"),
        (_select_d12, "unselected decision D12 changed"),
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
