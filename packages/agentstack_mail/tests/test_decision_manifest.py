"""Fail-closed checks for the pending/resolved product-decision ledger."""

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
MANIFEST = FIXTURES / "differential-expected-divergences-v1.json"


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


def _drop_resolved_d1(manifest: dict[str, Any]) -> None:
    manifest["resolved_product_decisions"] = []


def _overlap_d1(manifest: dict[str, Any]) -> None:
    manifest["pending_product_decisions"].append(
        {
            "id": "D1",
            "title": "conflicting token registration mutation",
            "status": "pending_no_go",
            "allowlisted": False,
            "comparator_disposition": "fail",
        }
    )


def _drop_pending_d6(manifest: dict[str, Any]) -> None:
    manifest["pending_product_decisions"] = [
        decision
        for decision in manifest["pending_product_decisions"]
        if decision["id"] != "D6"
    ]


def _add_unknown_d13(manifest: dict[str, Any]) -> None:
    manifest["pending_product_decisions"].append(
        {
            "id": "D13",
            "title": "unknown decision",
            "status": "pending_no_go",
            "allowlisted": False,
            "comparator_disposition": "fail",
        }
    )


def _duplicate_pending_d6(manifest: dict[str, Any]) -> None:
    decision = next(
        item for item in manifest["pending_product_decisions"] if item["id"] == "D6"
    )
    manifest["pending_product_decisions"].append(copy.deepcopy(decision))


def _duplicate_resolved_d1(manifest: dict[str, Any]) -> None:
    manifest["resolved_product_decisions"].append(
        copy.deepcopy(manifest["resolved_product_decisions"][0])
    )


def _allowlist_resolved_d1(manifest: dict[str, Any]) -> None:
    entry = copy.deepcopy(manifest["intentional_differences"]["allowlisted_entries"][0])
    entry["id"] = "D1"
    manifest["intentional_differences"]["allowlisted_entries"].append(entry)


def _move_d2_to_resolved(manifest: dict[str, Any]) -> None:
    pending = manifest["pending_product_decisions"]
    decision = next(item for item in pending if item["id"] == "D2")
    pending.remove(decision)
    resolved = copy.deepcopy(decision)
    resolved["status"] = "resolved"
    manifest["resolved_product_decisions"].append(resolved)


def _weaken_resolved_allowlist_flag(manifest: dict[str, Any]) -> None:
    decision = manifest["resolved_product_decisions"][0]
    decision["allowlisted"] = True


def _weaken_resolved_disposition(manifest: dict[str, Any]) -> None:
    decision = manifest["resolved_product_decisions"][0]
    decision["comparator_disposition"] = "allow"


def _change_omitted_token_scope(manifest: dict[str, Any]) -> None:
    manifest["resolved_product_decisions"][0]["scope"]["omitted_token"] = "fail_closed"


def _change_resolved_resolution(manifest: dict[str, Any]) -> None:
    manifest["resolved_product_decisions"][0]["resolution"] = "silent_accept"


def _drop_resolved_verification_node(manifest: dict[str, Any]) -> None:
    manifest["resolved_product_decisions"][0]["verification"].pop()


def _weaken_pending_d6(manifest: dict[str, Any]) -> None:
    decision = next(
        item for item in manifest["pending_product_decisions"] if item["id"] == "D6"
    )
    decision["comparator_disposition"] = "warn"


def test_canonical_decision_ledger_is_accepted() -> None:
    _verify(_canonical_manifest())


def test_resolved_decision_verification_nodes_exist_as_top_level_tests() -> None:
    manifest = _canonical_manifest()
    node_ids = [
        node_id
        for decision in manifest["resolved_product_decisions"]
        for node_id in decision["verification"]
    ]
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
        (
            _drop_resolved_d1,
            "product decision ledger ids changed: missing=['D1'], extra=[]",
        ),
        (_overlap_d1, "product decision ledgers overlap: ['D1']"),
        (
            _drop_pending_d6,
            "product decision ledger ids changed: missing=['D6'], extra=[]",
        ),
        (
            _add_unknown_d13,
            "product decision ledger ids changed: missing=[], extra=['D13']",
        ),
        (
            _duplicate_pending_d6,
            "pending product decisions contain duplicate ids",
        ),
        (
            _duplicate_resolved_d1,
            "resolved product decisions contain duplicate ids",
        ),
        (
            _allowlist_resolved_d1,
            "resolved product decisions must not be allowlisted: ['D1']",
        ),
        (_move_d2_to_resolved, "pending product decision ids changed"),
        (
            _weaken_resolved_allowlist_flag,
            "resolved product decision D1 changed",
        ),
        (
            _weaken_resolved_disposition,
            "resolved product decision D1 changed",
        ),
        (
            _change_omitted_token_scope,
            "resolved product decision D1 changed",
        ),
        (
            _change_resolved_resolution,
            "resolved product decision D1 changed",
        ),
        (
            _drop_resolved_verification_node,
            "resolved product decision D1 changed",
        ),
        (_weaken_pending_d6, "pending decision D6 is not fail-closed"),
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
