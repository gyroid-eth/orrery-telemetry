from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PACKAGE_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import cutover_gates as gates  # noqa: E402
from differential_source import reconstruct_live  # noqa: E402


@pytest.fixture(scope="module")
def frozen_live_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    checkout = reconstruct_live(
        PACKAGE_ROOT,
        tmp_path_factory.mktemp("cutover-gates-frozen-live"),
    )
    return checkout / "src"


def test_mcp_error_detector_parses_boolean_instead_of_substring() -> None:
    success = gates.parse_mcp_response_body(
        '{"isError": false, "content": [{"text": "isError is present"}]}'
    )
    failure = gates.parse_mcp_response_body('{"isError": true, "content": []}')
    assert success["isError"] is False
    assert failure["isError"] is True
    with pytest.raises(gates.GateFailure, match="boolean isError"):
        gates.parse_mcp_response_body('{"isError": "false"}')


def test_ledger_expectations_accept_exact_owner_approval() -> None:
    result = gates._ledger_expectations()  # noqa: SLF001

    assert result["cutover_go_entries"] == 11
    assert result["cutover_no_go_entries"] == 1
    assert result["deferred_decision"] == "D7"
    assert result["required_condition_entries"] == 11
    assert result["descoped_documentation_only_entries"] == 3


@pytest.mark.parametrize(
    ("decision_id", "cutover_state"),
    (("D7", "go"), ("D2", "no_go")),
)
def test_ledger_expectations_reject_unapproved_cutover_state_change(
    decision_id: str,
    cutover_state: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = json.loads(gates.DECISION_LEDGER.read_text(encoding="utf-8"))
    decision = next(
        entry for entry in payload["product_decisions"] if entry["id"] == decision_id
    )
    decision["cutover_state"] = cutover_state
    mutated_ledger = tmp_path / "unapproved-ledger.json"
    mutated_ledger.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(gates, "DECISION_LEDGER", mutated_ledger)

    with pytest.raises(
        gates.GateFailure,
        match="outside the exact 2026-08-15 owner approval",
    ):
        gates._ledger_expectations()  # noqa: SLF001


@pytest.mark.parametrize("gate_name", ["coexistence", "migration", "rollback"])
def test_state_cutover_gate_detects_its_broken_control(
    gate_name: str,
    frozen_live_source: Path,
    tmp_path: Path,
) -> None:
    functions = {
        "coexistence": gates.run_coexistence_gate,
        "migration": gates.run_migration_gate,
        "rollback": gates.run_rollback_gate,
    }
    result = functions[gate_name](tmp_path.resolve() / gate_name, frozen_live_source)
    assert result["status"] == "pass"
    assert result["broken_control"]["detected"] is True


def test_fault_cutover_gate_detects_its_broken_control(tmp_path: Path) -> None:
    result = gates.run_fault_gate(tmp_path.resolve() / "fault")
    assert result["status"] == "pass"
    assert result["broken_control"]["detected"] is True
    assert result["d8"]["database_delivery_rows_after_sigkill"] == 1
    assert result["d10"] == {
        "shared_root_trials": 4,
        "total_grants": 4,
        "total_conflicts": 4,
        "active_rows_per_trial": [1, 1, 1, 1],
    }
    assert result["d12"]["retry_cleaned_signal_and_lease"] is True
