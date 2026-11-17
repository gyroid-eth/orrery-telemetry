"""Red native acceptance specification, NOT synthetic CLI acceptance evidence.

The deliberately missing native_acceptance test adapter must observe real
pinned CLIs and existing hooks/receipt paths, with isolated transports. Do not
make these green by implementing a provider or permission engine in test code.
The fake-peer instruments themselves have separate green negative controls.
"""
import pytest
import json
import secrets
import shlex

from .support import (Canary, MAIL, MODEL, assert_delegation, assert_denial, assert_failure,
                      assert_roundtrip, assert_same_launch, fake_peer)
from .evidence import (assert_auth_matrix, assert_wire_rotation, credential_source,
                       snapshot_tree, assert_tree_unchanged, owner_paths,
                       assert_single_owner, assert_bound_receipt)
from .test_instruments import SURFACES


def observed_native(trace, provider):
    assert trace["provider"] == provider
    assert trace["cli_kind"] == "native", "fake CLI cannot establish native compatibility"
    assert trace["cli_version"], "native CLI version missing"
    assert trace["observations_complete"], "missing process/wire observation"
    assert trace["real_services_used"] == []


@pytest.mark.parametrize("provider,protocol", [("codex", "responses"), ("claude", "messages")])
def test_native_function_roundtrip_model_path_and_both_turn_tools(native_probe, workspace, canary, provider, protocol):
    # No authentication in the HTTP loopback fixture. Authenticated native
    # probes below use verified fixture TLS, not a weakened URL policy.
    with fake_peer(protocol, canary) as peer:
        trace = native_probe("function-roundtrip", workspace=workspace,
                             provider=provider, peer=peer, canary=canary)
        observed_native(trace, provider)
        assert_roundtrip(peer)
        assert all(row["body"]["model"] == MODEL for row in peer.requests)
        assert trace["tool_effects"] == ["echo:fixture"]
        assert trace["model_success"]


@pytest.mark.parametrize("provider,protocol", [("codex", "responses"), ("claude", "messages")])
def test_native_header_matrix_requires_authenticated_success(native_probe, workspace, canary, provider, protocol):
    observations = []
    with credential_source(canary) as source:
        helper = source.helper_argv(workspace.root / "fixture-credential.py")
        for header in ("bearer", "x-api-key"):
            with fake_peer(protocol, canary, auth=header, tls_dir=workspace.tmp / ("tls-" + header)) as peer:
                trace = native_probe("auth-header", workspace=workspace, provider=provider,
                                     peer=peer, canary=canary, auth_command=helper)
                observed_native(trace, provider)
                observations.append((peer, trace))
                if provider == "codex":
                    assert peer.requests and peer.requests[0]["auth_form"] == "bearer"
                if not trace["model_success"]:
                    assert_failure(trace, "LOCAL_MODEL_AUTH_HEADER_UNSUPPORTED", pre_exec=False)
                assert trace["auth_mechanism"] == ("command-backed-auth" if provider == "codex" else "apiKeyHelper")
                assert set(SURFACES) <= set(trace["surfaces"])
                canary.scan(trace["surfaces"])
        # Both Claude cases reporting unsupported, or helper never used, FAIL.
        assert_auth_matrix(observations, source)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_native_refresh_rotation_without_token_in_any_process_env(native_probe, workspace, canary, provider):
    rotated = Canary()
    protocol = "responses" if provider == "codex" else "messages"
    with credential_source(canary) as source, fake_peer(
            protocol, canary, auth="either", tls_dir=workspace.tmp / "rotation-tls") as peer:
        helper = source.helper_argv(workspace.root / "fixture-credential.py")
        saved = workspace.tmp / "saved-auth-intent.json"
        before_refresh = []
        def rotate():
            before_refresh.append(saved.read_bytes())
            source.rotate(rotated)
            peer.rotate_credential(rotated)
        trace = native_probe("auth-refresh", workspace=workspace, provider=provider,
                             peer=peer, auth_command=helper, before_refresh=rotate,
                             canary=canary, fake_clock=True, production_ttl_ms=300000,
                             intent_owner=saved)
        observed_native(trace, provider)
        assert_wire_rotation(peer, source)
        assert trace["production_ttl_ms"] == 300000
        # Preserve the saved intent, not a claimed same_intent_digest boolean.
        assert len(before_refresh) == 1 and before_refresh[0] == saved.read_bytes()
        assert set(SURFACES) <= set(trace["surfaces"])
        canary.scan(trace["surfaces"])
        rotated.scan(trace["surfaces"])


@pytest.mark.parametrize("fault,reason", [
    ("auth-timeout", "LOCAL_MODEL_AUTH_FAILED"),
    ("auth-exit", "LOCAL_MODEL_AUTH_FAILED"),
    ("401", "LOCAL_MODEL_AUTH_FAILED"),
    ("500", "LOCAL_MODEL_REQUEST_FAILED"),
    ("disconnect", "LOCAL_MODEL_REQUEST_FAILED"),
    ("redirect", "LOCAL_MODEL_REQUEST_FAILED"),
    ("unreachable", "LOCAL_MODEL_REQUEST_FAILED"),
    ("invalid-tool-json", "LOCAL_MODEL_PROTOCOL_FAILED"),
])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_native_failure_never_falls_back_or_loses_exec_receipt(native_probe, workspace, canary, provider, fault, reason):
    trace = native_probe("request-failure", workspace=workspace, provider=provider,
                         fault=fault, canary=canary)
    observed_native(trace, provider)
    assert_failure(trace, reason, pre_exec=False)
    assert "runtime_ready" in trace  # readiness and model success are distinct facts
    assert set(SURFACES) <= set(trace["surfaces"])
    canary.scan(trace["surfaces"])


@pytest.mark.parametrize("task_source", ["temporary-task", "vault-handoff"])
def test_claude_external_task_reserve_edit_renew_release_report(native_probe, workspace, canary, task_source):
    trace = native_probe("delegation", workspace=workspace, provider="claude",
                         task_source=task_source, canary=canary,
                         mail_tools=list(MAIL), compound_bash="approved")
    observed_native(trace, "claude")
    assert_delegation(trace, workspace.scope, workspace.scratch)
    assert workspace.scratch.read_text() == "after\n"
    assert trace["compound_bash_effects"] == ["approved-cwd", "approved-pipe"]
    assert trace["external_task_read"] == str(workspace.task if task_source == "temporary-task" else workspace.handoff)


@pytest.mark.parametrize("operation", ["outside-read", "approved-first-outside-second", "outside-pipe", "outside-redirect"])
def test_claude_scope_denial_has_no_partial_effect_and_reports(native_probe, workspace, canary, operation):
    outside = workspace.tmp / "outside"
    outside.mkdir()
    sentinel = outside / "denied.md"
    sentinel.write_text("do not read or edit\n")
    first_sentinel = workspace.scratch.parent / "approved-first-sentinel.txt"
    first_sentinel.write_text("first command must not run\n")
    first_command = f"printf changed >> {shlex.quote(str(first_sentinel))}"
    workspace.context["effective_scope"]["bash"].append(first_command)
    workspace.context["effective_scope"]["edit"].append(str(first_sentinel))
    before_inside = snapshot_tree(workspace.scratch.parent)
    before_outside = snapshot_tree(outside)
    first, last = shlex.quote(str(first_sentinel)), shlex.quote(str(sentinel))
    commands = {"outside-read": None,
                "approved-first-outside-second": f"{first_command} && printf denied >> {last}",
                "outside-pipe": f"printf changed >> {first} | tee {last}",
                "outside-redirect": f"printf changed >> {first} 2>> {last}"}
    trace = native_probe("scope-denial", workspace=workspace, provider="claude",
                         operation=operation, outside=outside, canary=canary,
                         approved_first_sentinel=first_sentinel,
                         compound_command=commands[operation])
    observed_native(trace, "claude")
    assert_denial(trace)
    assert_tree_unchanged(workspace.scratch.parent, before_inside)
    assert_tree_unchanged(outside, before_outside)


@pytest.mark.parametrize("conflict", ["inherited-Bash-allow", "sandbox-auto-allow", "managed-ask-mail", "missing-reservation-tool", "unverifiable-plugin"])
def test_claude_unapproved_effective_scope_rejected_before_exec(native_probe, workspace, canary, conflict):
    before = workspace.settings.read_bytes()
    trace = native_probe("scope-preflight", workspace=workspace, provider="claude",
                         conflict=conflict, canary=canary)
    assert_failure(trace, "LOCAL_MODEL_PERMISSION_PLAN_UNSATISFIABLE", pre_exec=True)
    assert workspace.settings.read_bytes() == before


def test_native_replay_interruption_and_no_shadow_intent(native_probe, workspace, canary):
    base = workspace.tmp / "client"
    owner, receipt, _ = owner_paths(base, "global")
    trace = native_probe("lifecycle", workspace=workspace, provider="codex", canary=canary,
                         operations=["spawn", "end", "resume", "headless-wake"],
                         client_root=base,
                         interrupt_at=["intent-saved", "launch-planned", "exec-observed", "receipt-pending"])
    observed_native(trace, "codex")
    assert_same_launch(trace["launches"])
    assert_single_owner(base, owner)
    saved = json.loads(owner.read_text())["launch_intent"]
    assert_bound_receipt(saved, json.loads(receipt.read_text()))
    assert trace["unknown_outcome_requires_operator"]
    assert trace["caller_fields_dropped"] == []
    canary.scan(trace["surfaces"])


def test_builtin_model_metadata_collision_is_rejected_not_renamed(native_probe, workspace, canary):
    trace = native_probe("model-metadata-collision", workspace=workspace,
                         provider="codex", canary=canary,
                         model_id="gpt-fixture-collision", metadata_override_supported=False,
                         metadata_tool_mode="code_mode")
    assert_failure(trace, "LOCAL_MODEL_ADAPTER_UNSUPPORTED", pre_exec=True)
    assert trace["renamed_model"] is False


def test_auth_lookalike_in_caller_cwd_is_never_executed(native_probe, workspace, canary):
    trace = native_probe("auth-cwd-lookalike", workspace=workspace, provider="claude",
                         canary=canary, operations=["spawn", "resume", "headless-wake"])
    observed_native(trace, "claude")
    assert trace["lookalike_executions"] == 0
    assert trace["auth_contexts"] == [workspace.context["auth_context"]] * 3
    assert set(SURFACES) <= set(trace["surfaces"])
    canary.scan(trace["surfaces"])


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_local_child_preserves_normal_child_skills_and_uses_one(native_probe, workspace, canary, provider):
    home = workspace.tmp / "home"
    roots = ([home / ".claude/skills"] if provider == "claude" else
             [home / ".codex/skills", home / ".agentstack/skills"])
    nonce = "fixture-skill-body-" + secrets.token_hex(8)
    skill = roots[0] / "fixture-skill/SKILL.md"
    skill.parent.mkdir(parents=True, mode=0o700)
    skill.write_text("---\nname: fixture-skill\ndescription: Fixture skill acceptance\n---\n\n" + nonce + "\n")
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    expected_skills = ["fixture-skill"]
    if provider == "codex":
        shared = roots[1] / "fixture-shared-skill/SKILL.md"
        shared.parent.mkdir(mode=0o700)
        shared.write_text("---\nname: fixture-shared-skill\ndescription: Shared fixture source sentinel\n---\n\nShared fixture body.\n")
        expected_skills.append("fixture-shared-skill")
    workspace.context["effective_scope"]["read"].append(str(skill))
    workspace.context["effective_scope"]["skill_tools"] = ["Skill" if provider == "claude" else "exec_command"]
    workspace.context["skill_source_roots"] = [str(root) for root in roots]
    before = snapshot_tree(home)
    output = workspace.tmp / "skill-observations.json"
    transcript = workspace.tmp / "skill-native-transcript.jsonl"
    trace = native_probe("skills", workspace=workspace, provider=provider, canary=canary,
                         skill_roots=roots, skill_name="fixture-skill", skill_path=skill,
                         output=output, transcript=transcript,
                         ordinary_child_control=True, cloud_network_forbidden=True)
    observed_native(trace, provider)
    observation = json.loads(output.read_text())
    assert observation["local_skills"] == observation["ordinary_child_skills"]
    assert set(expected_skills) <= set(observation["local_skills"])
    assert observation["local_sources"] == observation["ordinary_child_sources"] == [str(root) for root in roots]
    # Read the observed native tool event/output, not a 'skill_loaded' boolean.
    events = [json.loads(line) for line in transcript.read_text().splitlines()]
    tools = {"Skill"} if provider == "claude" else {"Read", "exec_command", "functions.exec_command", "shell_command"}
    assert any(row["tool"] in tools and row["ok"] and nonce in row["output"] and
               (provider == "claude" or row["path"] == str(skill)) for row in events)
    after = snapshot_tree(home)
    for name, digest in before.items():
        assert after[name] == digest, "provider configuration or skill source overwritten"
    assert trace["human_prompts"] == 0
    canary.scan(trace["surfaces"])
