"""Executable PR1 contracts, with no production state or cutover activation."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agentstack_mail.contract import COMPATIBILITY_TOOLS, CONTRACT_VERSION
from agentstack_mail.namespace_contract import (
    NamespaceAdapter,
    NamespaceContractError,
    SCOPE_ARGUMENTS,
    load_namespace_contract,
)
from agentstack_mail.namespace_plan import (
    FINAL_CHECKS,
    AgentRecord,
    FenceEvidence,
    FinalValidationEvidence,
    MigrationReceipt,
    PlanningError,
    SourceSnapshot,
    WindowRecord,
    fingerprint,
    plan_agent_names,
    plan_windows,
)
from agentstack_mail.namespace_threads import (
    CallerEvidence,
    LegacyThread,
    ThreadCatalog,
    ThreadDecision,
    ThreadResolutionError,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def temporary_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def scenario():
    return json.loads((FIXTURES / "namespace-plan-v1.json").read_text())


def records(scenario):
    return [
        LegacyThread(
            **{
                **row,
                "participants": frozenset(row["participants"]),
                "message_ids": frozenset(row["message_ids"]),
            }
        )
        for row in scenario["threads"]
    ]


def catalog(scenario):
    value = ThreadCatalog(records(scenario))
    value.apply(
        [
            ThreadDecision((21, 22), "separate", confirmed=True),
            ThreadDecision((31, 32), "group", confirmed=True),
        ]
    )
    return value


def fence(generation="fixture-generation-1"):
    return FenceEvidence(
        generation,
        ("mail", "watcher", "proxy-wake"),
        {"mail": "stopped", "watcher": "stopped", "proxy-wake": "stopped"},
        True,
        True,
        True,
        0,
    )


def evidence(snapshot, candidate, **overrides):
    checks = {key: "pass" for key in FINAL_CHECKS}
    checks.update(overrides)
    return FinalValidationEvidence.record(snapshot, candidate, checks)


def final_receipt(scenario):
    snapshot = SourceSnapshot.capture(scenario["generation"], scenario)
    resolutions = {"unresolved": [], "thread_catalog": catalog(scenario).export()}
    receipt = (
        MigrationReceipt.create(snapshot, resolutions)
        .advance("prepared")
        .advance("prevalidated")
    )
    candidate = {
        "resolutions_digest": fingerprint(resolutions),
        "archive_digest": "fixture-archive",
    }
    proof = fence(snapshot.generation)
    receipt = receipt.quiesce(proof).verify_final(
        snapshot, candidate, evidence(snapshot, candidate), proof
    )
    return receipt, snapshot, candidate, proof


def test_candidate_schema_is_separate_and_audits_all_legacy_scope_fields():
    contract = load_namespace_contract()
    assert CONTRACT_VERSION == 1
    assert contract["contract_version"] == 2
    assert contract["default_runtime"] == "legacy-v1"
    assert contract["activation_enabled"] is False
    assert set(contract["tools"]) == COMPATIBILITY_TOOLS
    legacy = json.loads((FIXTURES / "live-tools-list.json").read_text())
    for tool in legacy["tools"]:
        if tool["name"] not in COMPATIBILITY_TOOLS:
            continue
        schema = tool["inputSchema"]
        candidate = contract["tools"][tool["name"]]
        ignored = set(schema["properties"]) & SCOPE_ARGUMENTS
        assert set(candidate["accepted_ignored_arguments"]) == ignored
        assert not set(candidate["input_schema"]["properties"]) & SCOPE_ARGUMENTS
        assert (
            set(candidate["input_schema"]["required"])
            == set(schema.get("required", [])) - ignored
        )


@pytest.mark.parametrize("tool", sorted(COMPATIBILITY_TOOLS))
def test_old_scope_values_never_reach_candidate_dispatch(tool):
    contract = load_namespace_contract()
    adapter = NamespaceAdapter(contract)
    spec = contract["tools"][tool]
    arguments = {
        key: "fixture-value" for key in spec["input_schema"].get("required", [])
    }
    expected = adapter.dispatch(tool, arguments, lambda name, values: (name, values))
    for value in ("legacy-a", "legacy-b", "", {"not": "a routing input"}):
        scoped = {
            **arguments,
            **{key: value for key in spec["accepted_ignored_arguments"]},
        }
        assert (
            adapter.dispatch(tool, scoped, lambda name, values: (name, values))
            == expected
        )


def test_adapter_preserves_credentials_and_unknown_values_are_not_logged():
    adapter = NamespaceAdapter(load_namespace_contract())
    arguments = {
        "project_key": "ignored",
        "sender_name": "BlueLake",
        "sender_token": "fixture-owner-secret",
        "to": ["GreenPond"],
        "subject": "fixture",
        "body_md": "fixture",
    }
    result = adapter.arguments("send_message", arguments)
    assert result["sender_token"] == arguments["sender_token"]
    assert arguments["project_key"] == "ignored"
    registration = adapter.arguments(
        "register_agent",
        {
            "program": "fixture",
            "model": "fixture",
            "registration_token": "fixture-owner-secret",
            "existing_agent_id": 7,
        },
    )
    assert registration["existing_agent_id"] == 7
    assert registration["registration_token"] == "fixture-owner-secret"
    with pytest.raises(NamespaceContractError) as error:
        adapter.arguments(
            "send_message", {**arguments, "unknown_secret": "must-not-leak"}
        )
    assert "must-not-leak" not in str(error.value)
    assert "unknown_secret" not in str(error.value)
    with pytest.raises(NamespaceContractError):
        adapter.arguments("send_message", {})


def test_retired_and_normalized_agent_names_require_explicit_resolution(scenario):
    rows = [AgentRecord(**row) for row in scenario["agents"]]
    assert plan_agent_names(rows).conflicts == ((1, 2, 3),)
    assert rows[1].retired
    assert plan_agent_names(rows, {2: "GreenPond", 3: "AmberLake"}).conflicts == ()
    with pytest.raises(PlanningError):
        plan_agent_names(rows, {99: "GreenPond"})


def test_window_map_keeps_both_identities_and_generated_ids_replay(scenario):
    rows = [
        WindowRecord(**{**row, "bindings": tuple(row["bindings"])})
        for row in scenario["windows"]
    ]
    assert plan_windows(rows).unresolved == (12, 34)
    choices = {12: "keep", 34: "generate"}
    plan = plan_windows(rows, choices, uuid_factory=lambda: "new-window")
    assert plan.targets == {12: "old-window", 34: "new-window"}
    assert plan.unresolved == ()
    replay = plan_windows(
        rows,
        choices,
        prior_targets=plan.targets,
        uuid_factory=lambda: pytest.fail("reissued window UUID"),
    )
    assert replay == plan
    assert rows[1].bindings == ("session-b",) and rows[1].expired
    assert plan_windows(rows, {12: "keep", 34: "keep"}).unresolved == (12, 34)
    with pytest.raises(PlanningError, match="WINDOW_PLAN_CHANGED"):
        plan_windows(rows, {12: "other", 34: "generate"}, prior_targets=plan.targets)


def test_independent_and_cross_project_threads_preserve_reply_paths(scenario):
    value = catalog(scenario)
    actor = CallerEvidence(1, True)
    assert value.resolve("CROSS-1", actor) == value.resolve(None, actor, reply_to=201)
    assert value.resolve(None, actor, reply_to=201) == value.resolve(
        None, actor, reply_to=202
    )
    with pytest.raises(ThreadResolutionError) as error:
        value.resolve("TKT-1", actor)
    assert error.value.code == "LEGACY_THREAD_AMBIGUOUS"
    assert error.value.candidates == 2
    a = value.resolve("TKT-1", replace(actor, legacy_sources=frozenset({"legacy-a"})))
    b = value.resolve("TKT-1", replace(actor, legacy_sources=frozenset({"legacy-b"})))
    assert a != b
    assert value.resolve(None, actor, reply_to=101) == a
    assert value.resolve(None, actor, reply_to=102) == b
    with pytest.raises(ThreadResolutionError, match="MESSAGE_UNAVAILABLE"):
        value.resolve(None, CallerEvidence(2, True), reply_to=202)
    with pytest.raises(ThreadResolutionError, match="CALLER_UNCONFIRMED"):
        value.resolve("TKT-1", replace(actor, confirmed=False))


def test_duplicate_legacy_threads_are_not_split_without_decisions(scenario):
    value = ThreadCatalog(records(scenario))
    with pytest.raises(ThreadResolutionError, match="THREAD_DECISION_REQUIRED"):
        value.apply([])
    assert value.export()["threads"] == []
    with pytest.raises(ThreadResolutionError, match="THREAD_CONFIRMATION_REQUIRED"):
        value.apply([ThreadDecision((21, 22), "group")])
    assert value.export()["record_targets"] == {}


def test_unknown_relationship_requires_message_reply(scenario):
    value = ThreadCatalog(records(scenario))
    value.apply(
        [
            ThreadDecision((21, 22), "separate", True, True),
            ThreadDecision((31, 32), "group", True),
        ]
    )
    with pytest.raises(ThreadResolutionError, match="LEGACY_THREAD_UPDATE_REQUIRED"):
        value.resolve("TKT-1", CallerEvidence(1, True))
    assert value.resolve(None, CallerEvidence(1, True), reply_to=101) != value.resolve(
        None, CallerEvidence(1, True), reply_to=102
    )


def test_thread_ids_are_unique_and_never_reuse_a_legacy_canonical_shape():
    legacy = LegacyThread(
        1, "legacy-a", "th-" + "0" * 32, frozenset({1}), frozenset({7})
    )
    sequence = iter(["0" * 32, "1" * 32])
    value = ThreadCatalog([legacy], uuid_factory=lambda: next(sequence))
    value.apply([])
    assert value.resolve(legacy.alias, CallerEvidence(1, True)) == "th-" + "1" * 32
    fresh = ThreadCatalog()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(
            pool.map(
                lambda _: fresh.create(CallerEvidence(1, True), label="same-label"),
                range(100),
            )
        )
    assert len(set(ids)) == 100
    with pytest.raises(ThreadResolutionError, match="LEGACY_THREAD_UNKNOWN"):
        fresh.resolve("same-label", CallerEvidence(1, True))


def test_new_deliveries_and_receipt_replay_keep_ids(scenario):
    value = catalog(scenario)
    actor = CallerEvidence(1, True)
    new_id = value.resolve(None, actor, new_thread=True, label="same-label")
    value.record_delivery(new_id, 999, frozenset({1, 2}))
    value.record_delivery(new_id, 999, frozenset({1, 2}))
    restored = ThreadCatalog.from_export(
        records(scenario), json.loads(json.dumps(value.export()))
    )
    assert restored.export() == value.export()
    assert restored.resolve(None, CallerEvidence(2, True), reply_to=999) == new_id
    restored.apply(
        [
            ThreadDecision((21, 22), "separate", True),
            ThreadDecision((31, 32), "group", True),
        ]
    )
    assert restored.export() == value.export()
    changed_records = records(scenario)
    changed_records[0] = replace(changed_records[0], source="different-source")
    with pytest.raises(ThreadResolutionError, match="INVALID_THREAD_RECEIPT"):
        ThreadCatalog.from_export(changed_records, value.export())
    state = value.export()
    state["message_targets"]["999"] = "missing-thread"
    with pytest.raises(ThreadResolutionError, match="INVALID_THREAD_RECEIPT"):
        ThreadCatalog.from_export(records(scenario), state)


def test_final_receipt_round_trip_does_not_activate_cutover(scenario, monkeypatch):
    receipt, snapshot, candidate, proof = final_receipt(scenario)
    restored = MigrationReceipt.from_json(receipt.to_json())
    assert restored == receipt
    monkeypatch.setenv("AGENTSTACK_MAIL_NAMESPACE_MODE", "global")
    gate = restored.gate(snapshot, candidate, proof)
    assert gate.ready
    assert gate.activation_enabled is False
    for phase in ("switched", "committed", "final_verified"):
        with pytest.raises(PlanningError, match="INVALID_PLANNING_TRANSITION"):
            restored.advance(phase)


@pytest.mark.parametrize(
    "mutation", ["agent", "lease", "read_ack", "delivery", "binding", "block"]
)
def test_changes_after_initial_validation_require_final_revalidation(
    scenario, mutation
):
    receipt, original, candidate, proof = final_receipt(scenario)
    final = SourceSnapshot.capture(
        "fixture-generation-2", {**scenario, mutation: "changed after validation"}
    )
    current_fence = fence(final.generation)
    assert not receipt.gate(final, candidate, current_fence).ready
    pending = replace(receipt, phase="quiesced")
    stale_evidence = evidence(original, candidate)
    with pytest.raises(PlanningError, match="FINAL_VALIDATION_GENERATION_CHANGED"):
        pending.verify_final(final, candidate, stale_evidence, current_fence)
    validated = pending.verify_final(
        final, candidate, evidence(final, candidate), current_fence
    )
    assert validated.gate(final, candidate, current_fence).ready
    same_generation_changed = SourceSnapshot.capture(
        final.generation, {"different": "state"}
    )
    assert not validated.gate(same_generation_changed, candidate, current_fence).ready


@pytest.mark.parametrize(
    "change",
    [
        {"common_authority_held": False},
        {"supervisors_suppressed": False},
        {"connections_closed": False},
        {"inflight_wakes": 1},
        {"inflight_wakes": None},
        {
            "writer_states": {
                "mail": "running",
                "watcher": "stopped",
                "proxy-wake": "stopped",
            }
        },
        {"writer_states": {"mail": "stopped"}},
        {"expected_writers": ()},
    ],
)
def test_old_writer_reentry_or_unknown_fence_blocks_switch(scenario, change):
    receipt, snapshot, candidate, proof = final_receipt(scenario)
    bad = replace(proof, **change)
    assert not receipt.gate(snapshot, candidate, bad).ready
    with pytest.raises(PlanningError, match="WRITER_FENCE_UNCONFIRMED"):
        replace(receipt, phase="prevalidated").quiesce(bad)


@pytest.mark.parametrize("result", ["fail", "unknown"])
def test_incomplete_final_checks_and_tampered_receipt_are_rejected(scenario, result):
    receipt, snapshot, candidate, proof = final_receipt(scenario)
    pending = replace(receipt, phase="quiesced")
    with pytest.raises(PlanningError, match="FINAL_VALIDATION_INCOMPLETE"):
        pending.verify_final(
            snapshot,
            candidate,
            evidence(snapshot, candidate, window_mapping_complete=result),
            proof,
        )
    broken = json.loads(receipt.to_json())
    broken["payload"]["resolutions_json"] = "{}"
    with pytest.raises(PlanningError, match="INVALID_RECEIPT"):
        MigrationReceipt.from_json(json.dumps(broken))
    assert not receipt.gate(snapshot, {**candidate, "late_change": True}, proof).ready
    unresolved = replace(pending, resolutions_json=json.dumps({"unresolved": [34]}))
    unresolved_candidate = {"resolutions_digest": fingerprint({"unresolved": [34]})}
    with pytest.raises(PlanningError, match="UNRESOLVED_PLAN"):
        unresolved.verify_final(
            snapshot,
            unresolved_candidate,
            evidence(snapshot, unresolved_candidate),
            proof,
        )


def test_default_mcp_still_routes_by_project_and_preserves_v1_schema(
    monkeypatch, tmp_path
):
    from agentstack_mail import app, config, db
    from fastmcp import Client

    monkeypatch.setenv("AGENTSTACK_MAIL_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'fixture.sqlite3'}",
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_STORAGE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR", str(tmp_path / "signals")
    )
    management_root = Path(tempfile.mkdtemp(prefix="rh213-ms-"))
    monkeypatch.setenv(
        "AGENTSTACK_MAIL_MANAGEMENT_SOCKET", str(management_root / "mail.sock")
    )
    monkeypatch.setenv("AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE", "passthrough")
    monkeypatch.setenv("AGENTSTACK_MAIL_LOG_RICH_ENABLED", "false")
    db.reset_database_state()
    config.clear_settings_cache()

    def payload(result):
        value = result.structured_content
        if value is None:
            value = result.data
        while isinstance(value, dict) and set(value) == {"result"}:
            value = value["result"]
        return value

    async def run():
        try:
            async with Client(app.build_mcp_server()) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools} == COMPATIBILITY_TOOLS
                send_schema = next(
                    tool.inputSchema for tool in tools if tool.name == "send_message"
                )
                assert "project_key" in send_schema["required"]
                assert "new_thread" not in send_schema["properties"]
                candidate_tools = load_namespace_contract()["tools"]
                for tool in tools:
                    properties = dict(
                        candidate_tools[tool.name]["input_schema"]["properties"]
                    )
                    if tool.name == "send_message":
                        properties.pop("new_thread")
                        properties.pop("thread_label")
                    assert properties == {
                        key: value
                        for key, value in tool.inputSchema["properties"].items()
                        if key not in SCOPE_ARGUMENTS
                    }
                projects = [str(tmp_path / "legacy-a"), str(tmp_path / "legacy-b")]
                for project in projects:
                    await client.call_tool("ensure_project", {"human_key": project})
                    for name in ("BlueLake", "GreenPond"):
                        await client.call_tool(
                            "register_agent",
                            {
                                "project_key": project,
                                "name": name,
                                "program": "fixture",
                                "model": "fixture",
                            },
                        )
                await client.call_tool(
                    "send_message",
                    {
                        "project_key": projects[0],
                        "sender_name": "BlueLake",
                        "to": ["GreenPond"],
                        "subject": "fixture",
                        "body_md": "fixture",
                        "thread_id": "TKT-1",
                    },
                )
                a = payload(
                    await client.call_tool(
                        "fetch_inbox",
                        {
                            "project_key": projects[0],
                            "agent_name": "GreenPond",
                            "include_bodies": True,
                        },
                    )
                )
                b = payload(
                    await client.call_tool(
                        "fetch_inbox",
                        {"project_key": projects[1], "agent_name": "GreenPond"},
                    )
                )
                assert len(a) == 1 and a[0]["thread_id"] == "TKT-1"
                assert b == []
        finally:
            await db.dispose_database_for_shutdown()

    try:
        asyncio.run(run())
    finally:
        db.reset_database_state()
        config.clear_settings_cache()
        shutil.rmtree(management_root)
