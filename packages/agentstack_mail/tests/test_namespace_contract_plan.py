"""Executable PR1 contracts, with no production state or cutover activation."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

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
from agentstack_mail.namespace_replies import (
    CallerEvidence,
    MessageRecord,
    ReplyCatalog,
    ReplyError,
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


def catalog(scenario):
    return ReplyCatalog(
        [
            MessageRecord.from_legacy(
                **{**row, **{key: tuple(row[key]) for key in ("to", "cc", "bcc")}}
            )
            for row in scenario["messages"]
        ]
    )


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
    resolutions = {"unresolved": [], "reply_catalog": catalog(scenario).export()}
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
        assert set(candidate["input_schema"]["required"]) == set(
            schema.get("required", [])
        ) - ignored - ({"thread_id"} if tool["name"] == "summarize_thread" else set())


@pytest.mark.parametrize("tool", sorted(COMPATIBILITY_TOOLS))
def test_old_scope_values_never_reach_candidate_dispatch(tool):
    contract = load_namespace_contract()
    adapter = NamespaceAdapter(contract)
    spec = contract["tools"][tool]
    arguments = {
        key: "fixture-value" for key in spec["input_schema"].get("required", [])
    }
    if tool == "summarize_thread":
        arguments["thread_id"] = "101"
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


def test_per_message_reply_permissions_preserve_distinct_recipients_after_restore(
    scenario,
):
    original = catalog(scenario)
    restored = ReplyCatalog.from_export(json.loads(json.dumps(original.export())))
    assert restored.export() == original.export()
    for value in (original, restored):
        for agent, allowed, denied in ((2, 101, 102), (3, 102, 101)):
            actor = CallerEvidence(agent, True)
            assert value.resolve(actor, reply_to=allowed) == allowed
            with pytest.raises(ReplyError, match="MESSAGE_UNAVAILABLE"):
                value.resolve(actor, reply_to=denied)
        assert value.resolve(CallerEvidence(1, True), reply_to=101) == 101
        assert value.resolve(CallerEvidence(1, True), reply_to=102) == 102
        assert (
            value.resolve(CallerEvidence(4, True), reply_to=201) == 201
        )  # bcc counts for access
        with pytest.raises(ReplyError, match="CALLER_UNCONFIRMED"):
            value.resolve(CallerEvidence(1, False), reply_to=101)
        with pytest.raises(ReplyError, match="MESSAGE_UNAVAILABLE"):
            value.resolve(CallerEvidence(1, True), reply_to=999)


def test_numeric_legacy_inputs_are_roots_and_named_labels_are_read_only(scenario):
    value = catalog(scenario)
    actor = CallerEvidence(1, True)
    assert value.resolve(actor) is None
    assert value.resolve(actor, legacy_thread_id="101") == 101
    assert value.resolve(actor, legacy_thread_id="101", reply_to=101) == 101
    with pytest.raises(ReplyError, match="REPLY_INPUT_CONFLICT"):
        value.resolve(actor, legacy_thread_id="101", reply_to=102)
    with pytest.raises(ReplyError, match="LEGACY_THREAD_READ_ONLY"):
        value.resolve(actor, legacy_thread_id="historic-label")
    assert value.legacy_read("historic-label", actor)[0].message_ids == (201,)
    assert value.legacy_read("historic-label", actor)[1].message_ids == (202,)
    assert value.legacy_read("101,102", actor)[0].message_ids == (101, 102)
    # Old root input need not be accessible to a later recipient. Update to
    # that recipient's actual message, rather than weakening the root check.
    with pytest.raises(ReplyError, match="MESSAGE_UNAVAILABLE"):
        value.resolve(CallerEvidence(3, True), legacy_thread_id="101")
    assert value.resolve(CallerEvidence(3, True), reply_to=102) == 102
    state = value.export()
    assert state["messages"][1]["reply_to"] == 101
    assert state["messages"][2]["legacy_thread_label"] == "historic-label"


def test_adapter_converts_numeric_legacy_reply_and_summary_without_losing_auth():
    adapter = NamespaceAdapter(load_namespace_contract())
    send = dict(
        sender_name="fixture",
        to=["other"],
        subject="fixture",
        body_md="fixture",
        sender_token="dummy-token",
    )
    result = adapter.arguments(
        "send_message", {**send, "thread_id": "101", "project_key": "ignored"}
    )
    assert result == {**send, "reply_to": 101}
    with pytest.raises(ReplyError, match="LEGACY_THREAD_READ_ONLY"):
        adapter.arguments("send_message", {**send, "thread_id": "old-label"})
    with pytest.raises(ReplyError, match="REPLY_INPUT_CONFLICT"):
        adapter.arguments("send_message", {**send, "thread_id": "101", "reply_to": 102})
    assert adapter.arguments("summarize_thread", {"thread_id": "101"}) == {
        "message_id": 101
    }
    assert adapter.arguments("summarize_thread", {"thread_id": "old-label"}) == {
        "thread_id": "old-label"
    }
    assert adapter.arguments("summarize_thread", {"thread_id": "101,102"}) == {
        "thread_id": "101,102"
    }
    with pytest.raises(ReplyError, match="SUMMARY_INPUT_REQUIRED"):
        adapter.arguments("summarize_thread", {})
    with pytest.raises(ReplyError, match="REPLY_INPUT_CONFLICT"):
        adapter.arguments("summarize_thread", {"thread_id": "101", "message_id": 102})


@pytest.mark.parametrize(
    "rows, code",
    [
        ([MessageRecord(1, "source", 1, reply_to=2)], "DANGLING_REPLY"),
        ([MessageRecord(1, "source", 1, reply_to=1)], "REPLY_CYCLE"),
        (
            [
                MessageRecord(1, "source", 1, reply_to=2),
                MessageRecord(2, "source", 1, reply_to=1),
            ],
            "REPLY_CYCLE",
        ),
    ],
)
def test_missing_self_and_cyclic_parents_block_migration_and_restore(rows, code):
    with pytest.raises(ReplyError, match=code):
        ReplyCatalog(rows)
    with pytest.raises(ReplyError, match="INVALID_REPLY_RECEIPT"):
        from dataclasses import asdict

        ReplyCatalog.from_export(
            {"version": 1, "messages": [asdict(row) for row in rows]}
        )


def test_views_and_topics_do_not_expose_hidden_messages_or_join_labels(scenario):
    value = catalog(scenario)
    view = value.conversation(102, CallerEvidence(3, True))
    assert view.message_ids == (102,) and view.incomplete and not view.has_more
    assert value.conversation(101, CallerEvidence(2, True)).message_ids == (101,)
    assert value.conversation(101, CallerEvidence(1, True), limit=1).has_more
    assert value.topic_ids("fixture", CallerEvidence(1, True)) == (101, 102)
    assert value.topic_ids("fixture", CallerEvidence(3, True)) == (102,)
    # Same named label does not make an edge or expose the other source.
    assert value.conversation(201, CallerEvidence(1, True)).message_ids == (201,)
    assert value.conversation(202, CallerEvidence(1, True)).message_ids == (202,)
    assert value.legacy_read("historic-label", CallerEvidence(4, True))[
        0
    ].message_ids == (201,)


def test_direct_reply_and_replay_keep_message_ids_and_reject_changed_state(scenario):
    value = catalog(scenario)
    row = MessageRecord(301, "candidate", 3, (1,), reply_to=102)
    value.record_delivery(row, CallerEvidence(3, True))
    value.record_delivery(row, CallerEvidence(3, True))
    restored = ReplyCatalog.from_export(json.loads(json.dumps(value.export())))
    assert restored.export() == value.export()
    assert restored.conversation(301, CallerEvidence(3, True)).message_ids == (102, 301)
    assert restored.export()["messages"][-1]["reply_to"] == 102
    with pytest.raises(ReplyError, match="MESSAGE_ALREADY_MAPPED"):
        value.record_delivery(replace(row, reply_to=101), CallerEvidence(3, True))
    with pytest.raises(ReplyError, match="INVALID_NEW_MESSAGE"):
        value.record_delivery(replace(row, message_id=302), CallerEvidence(1, True))


def test_purge_holds_transitive_ancestors_and_never_cascades_new_children():
    value = ReplyCatalog(
        [
            MessageRecord(1, "old", 1, (2,)),
            MessageRecord(2, "old", 1, (2,), reply_to=1),
            MessageRecord(3, "new", 1, (2,), reply_to=2),
            MessageRecord(4, "old", 1, (2,), reply_to=1),
        ]
    )
    plan = value.plan_purge([1, 2, 4])
    assert plan.delete_ids == (4,) and plan.held_ids == (1, 2)
    actual = value.purge([1, 2, 4])
    assert actual == plan
    restored = ReplyCatalog.from_export(json.loads(json.dumps(value.export())))
    assert restored.conversation(3, CallerEvidence(2, True)).message_ids == (1, 2, 3)
    assert restored.purge([1, 2, 3]).delete_ids == (1, 2, 3)
    assert restored.export()["messages"] == []


def test_purge_rechecks_reply_race_in_both_commit_orders():
    actor = CallerEvidence(2, True)
    before = ReplyCatalog([MessageRecord(1, "old", 1, (2,))])
    assert before.plan_purge([1]).delete_ids == (1,)
    before.record_delivery(MessageRecord(2, "new", 2, (1,), reply_to=1), actor)
    assert before.purge([1]).held_ids == (1,)  # reply committed first
    after = ReplyCatalog([MessageRecord(1, "old", 1, (2,))])
    assert after.purge([1]).delete_ids == (1,)
    with pytest.raises(ReplyError, match="MESSAGE_UNAVAILABLE"):
        after.record_delivery(MessageRecord(2, "new", 2, (1,), reply_to=1), actor)
    assert after.export()["messages"] == []


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


def test_fence_cannot_shrink_its_own_expected_and_observed_writer_sets(scenario):
    receipt, snapshot, candidate, proof = final_receipt(scenario)
    reduced = replace(
        proof, expected_writers=("mail",), writer_states={"mail": "stopped"}
    )
    restored = MigrationReceipt.from_json(receipt.to_json())
    assert restored.writer_roster == tuple(sorted(scenario["expected_writers"]))
    assert not restored.gate(snapshot, candidate, reduced).ready
    with pytest.raises(PlanningError, match="WRITER_FENCE_UNCONFIRMED"):
        replace(restored, phase="prevalidated").quiesce(reduced)
    with pytest.raises(PlanningError, match="WRITER_FENCE_UNCONFIRMED"):
        replace(restored, phase="quiesced").verify_final(
            snapshot, candidate, evidence(snapshot, candidate), reduced
        )
    # A changed final snapshot roster also requires replanning even if its
    # fence is internally consistent and its final evidence is current.
    changed = SourceSnapshot.capture(
        snapshot.generation, {**scenario, "expected_writers": ["mail"]}
    )
    assert not restored.gate(changed, candidate, reduced).ready
    with pytest.raises(PlanningError, match="WRITER_ROSTER_CHANGED"):
        replace(restored, phase="quiesced").verify_final(
            changed, candidate, evidence(changed, candidate), proof
        )


@pytest.mark.parametrize(
    "roster", [None, [], ["mail", "mail"], ["mail", ""], ["mail", None]]
)
def test_plans_require_a_known_complete_writer_inventory(scenario, roster):
    snapshot = SourceSnapshot.capture(
        scenario["generation"], {**scenario, "expected_writers": roster}
    )
    with pytest.raises(PlanningError, match="INVALID_WRITER_ROSTER"):
        MigrationReceipt.create(snapshot, {"unresolved": []})


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
                assert "reply_to" not in send_schema["properties"]
                candidate_tools = load_namespace_contract()["tools"]
                for tool in tools:
                    properties = dict(
                        candidate_tools[tool.name]["input_schema"]["properties"]
                    )
                    if tool.name == "send_message":
                        properties.pop("reply_to")
                    if tool.name == "summarize_thread":
                        properties.pop("message_id")
                        properties["thread_id"] = tool.inputSchema["properties"][
                            "thread_id"
                        ]
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
