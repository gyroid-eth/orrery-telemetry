"""R3 V3/V6, A12, L6/7/10/13/67 and L33/71/73: one persisted intent."""
from copy import deepcopy
import json
import sys
import tomllib

import pytest

from .support import MODEL, assert_same_launch, canonical_digest
from .evidence import (assert_bound_receipt, assert_intent_digest, assert_single_owner,
                       assert_tree_unchanged, existing_mutation_pending, literal_intent,
                       owner_paths, snapshot_tree, write_private_json)


def resolve(api, workspace, **kwargs):
    return api.resolve_launch_intent(workspace.caller, workspace.context, **kwargs)


@pytest.mark.parametrize("mode", ["global", "legacy"])
def test_resolver_source_label_invariance_and_single_persisted_owner(launch_intent, workspace, mode):
    workspace.context["mode"] = mode
    intents = [resolve(launch_intent, workspace, source=source)
               for source in ("cli", "prepare-child", "operator", "form")]
    assert all(row == intents[0] for row in intents)
    intent = intents[0]
    assert intent["model"]["kind"] == "local-profile"
    assert intent["model"]["ref"] == "local:fixture"
    assert intent["model"]["resolved"]["model"] == MODEL
    assert intent["model"]["profile_sha256"] == canonical_digest(tomllib.loads(workspace.profile.read_text()))
    assert_intent_digest(intent, launch_intent.intent_digest(intent))
    for field in ("cwd", "task", "name", "parent", "tools", "base", "clear_tools", "detached", "resources", "worktree", "chrome"):
        assert intent[field] == workspace.caller[field], field
    base = workspace.tmp / "client"
    owner, receipt_path, _ = owner_paths(base, mode)
    launch_intent.save_launch_intent(owner, intent, mode=mode)
    assert launch_intent.load_launch_intent(owner, mode=mode) == intent
    saved = json.loads(owner.read_text())
    assert saved["launch_intent"] == intent
    receipt = launch_intent.make_launch_receipt(intent, phase="planned")
    assert_bound_receipt(intent, receipt)
    assert not any(field in receipt for field in ("model", "resolved", "auth_plan", "effective_scope"))
    if receipt_path:
        write_private_json(receipt_path, receipt)
    assert_single_owner(base, owner)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_spawn_resume_wake_use_saved_execution_not_caller_environment(launch_intent, workspace, provider):
    workspace.caller["provider"] = provider
    intent = resolve(launch_intent, workspace)
    before = json.dumps(intent, sort_keys=True)
    plans = []
    for operation in ("spawn", "resume", "headless-wake"):
        plans.append(launch_intent.materialize_launch_intent(
            intent, workspace.context, operation=operation,
            provider_session_id="native-fixture-session", caller_env={
                "HOME": "/caller/" + operation, "PATH": "/caller/bin", "TMUX": "/caller/socket,1,0",
                "ANTHROPIC_API_KEY": "unrelated-inherited-key", "OPENAI_API_KEY": "unrelated-key"}))
    assert_same_launch(plans)
    assert json.dumps(intent, sort_keys=True) == before
    for plan in plans:
        assert plan["warm_pool"] is False
        env = plan["execution"]["env"]
        assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
        assert "OPENAI_API_KEY" not in env
        assert plan["model"] == MODEL
        argv = plan["execution"]["argv"]
        models = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--model=")]
        models += [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in ("--model", "-m")]
        assert models == [MODEL], "actual argv must select the real model exactly once"
        if provider == "codex":
            assert plan["provider_config"]["wire_api"] == "responses"
            assert plan["provider_config"]["model_auto_compact_token_limit"] == 114688
            assert plan["provider_config"]["model_context_window"] == 131072
            assert "model_max_output_tokens" not in plan["provider_config"]
            assert "xhigh" not in plan["execution"]["argv"]
        else:
            for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
                assert env[key] == MODEL
            assert env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == "300000"
            assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "131072"
            assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "16384"
            assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
            assert plan["permission_mode"] == "dontAsk"


@pytest.mark.parametrize("mutation,reason", [
    ("missing", "LOCAL_MODEL_PROFILE_UNAVAILABLE"),
    ("changed", "LOCAL_MODEL_PROFILE_CHANGED"),
    ("mode", "LOCAL_MODEL_PROFILE_INVALID"),
    ("broken", "LOCAL_MODEL_PROFILE_INVALID"),
])
@pytest.mark.parametrize("operation", ["spawn", "resume", "headless-wake"])
def test_profile_rechecked_after_prepare_before_any_effect(launch_intent, workspace, mutation, reason, operation):
    intent = resolve(launch_intent, workspace)
    previous = deepcopy(intent)
    if mutation == "missing":
        workspace.profile.unlink()
    elif mutation == "changed":
        workspace.profile.write_text(workspace.profile.read_text().replace(MODEL, "changed-model"))
    elif mutation == "mode":
        workspace.profile.chmod(0o644)
    else:
        workspace.profile.write_text("not valid toml [")
    with pytest.raises(launch_intent.LocalModelError, match=reason):
        launch_intent.materialize_launch_intent(intent, workspace.context, operation=operation,
                                               provider_session_id="native-fixture-session", caller_env={})
    assert workspace.context["effects"] == []
    assert intent == previous


@pytest.mark.parametrize("mutation", ["runner", "dependency", "runner-version", "cli-version", "settings", "broad-allow", "sandbox-auto-allow", "missing-mail", "missing-task-read"])
def test_saved_auth_and_effective_scope_are_revalidated(launch_intent, workspace, mutation):
    workspace.caller["provider"] = "claude"
    command = json.dumps([sys.executable, str(workspace.runner)])
    workspace.profile.write_text(workspace.profile.read_text().replace(
        'wire_api = "anthropic-messages"', 'wire_api = "anthropic-messages"\nauth_command = ' + command))
    intent = resolve(launch_intent, workspace)
    current = deepcopy(workspace.context)
    if mutation == "broad-allow":
        current["effective_scope"]["inherited_bash_allow"] = ["Bash"]
    elif mutation == "sandbox-auto-allow":
        current["effective_scope"]["sandbox_auto_allow"] = True
    elif mutation == "missing-mail":
        current["effective_scope"]["mail_tools"] = current["effective_scope"]["mail_tools"][:-1]
    elif mutation == "missing-task-read":
        current["effective_scope"]["read"] = [str(workspace.scratch.parent)]
    elif mutation == "cli-version":
        current["effective_scope"]["cli_version"] = "changed"
    elif mutation == "settings":
        workspace.settings.write_text('{"permissions":{"allow":["Bash"]}}')
    elif mutation in ("runner", "dependency"):
        path = workspace.runner if mutation == "runner" else workspace.dependency
        path.chmod(0o600)
        path.write_text("replaced after approval\n")
    elif mutation == "runner-version":
        current["auth_runner"]["contract_version"] = 2
    else:
        raise AssertionError("unknown mutation")
    with pytest.raises(launch_intent.LocalModelError):
        launch_intent.materialize_launch_intent(intent, current, operation="resume",
                                               provider_session_id="native-fixture-session", caller_env={})
    assert current["effects"] == []


def test_catalog_does_not_execute_auth_or_publish_endpoint(launch_intent, workspace, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("catalog executed auth/CLI"))
    intent = resolve(launch_intent, workspace)
    row = launch_intent.project_local_model(intent, usage=None)
    assert row["model"] == MODEL and row["kind"] == "local-profile"
    assert row["context_usage"] is None and row["cloud_quota"] is None
    serialized = json.dumps(row)
    assert "base_url" not in serialized and "auth_command" not in serialized
    assert "env" not in row


def test_explicit_revision_without_pending_creates_new_revision(launch_intent, workspace):
    old = resolve(launch_intent, workspace)
    pending = deepcopy(old)
    workspace.profile.write_text(workspace.profile.read_text().replace(MODEL, "new-model"))
    new = launch_intent.revise_launch_intent(old, workspace.caller, workspace.context, confirm=True)
    assert old == pending
    assert new["revision"] != old["revision"]
    assert new["model"]["profile_sha256"] != old["model"]["profile_sha256"]


@pytest.mark.parametrize("phase", ["planned", "exec-unknown"])
def test_revision_preserves_actual_pending_owner_and_receipt(launch_intent, workspace, phase):
    base = workspace.tmp / "client"
    workspace.context.update(client_root=str(base), runtime=str(base / "runtime"))
    old = resolve(launch_intent, workspace)
    owner, receipt, pending = owner_paths(base, "global")
    launch_intent.save_launch_intent(owner, old, mode="global")
    write_private_json(receipt, launch_intent.make_launch_receipt(old, phase=phase))
    # Existing global mutation slot/schema, no local-model-specific ledger.
    pending_row = existing_mutation_pending()
    request_id = pending_row["request_id"]
    write_private_json(pending, pending_row)
    (base / "runtime/child.lock").touch()
    (base / "runtime/mutation.lock").touch()
    (base / "runtime/child.lock").chmod(0o600)
    (base / "runtime/mutation.lock").chmod(0o600)
    before = snapshot_tree(base)
    workspace.profile.write_text(workspace.profile.read_text().replace(MODEL, "new-model"))
    with pytest.raises(launch_intent.LocalModelError, match="PENDING|UNKNOWN|RESOLUTION"):
        launch_intent.revise_launch_intent(old, workspace.caller, workspace.context, confirm=True)
    assert_tree_unchanged(base, before)
    assert json.loads(pending.read_text())["request_id"] == request_id
    # Reconstruct from disk, not from the old live Python dict.
    reloaded = launch_intent.load_launch_intent(owner, mode="global")
    assert reloaded == old
    assert_bound_receipt(reloaded, json.loads(receipt.read_text()))
    assert_single_owner(base, owner)


@pytest.mark.parametrize("field", ["profile", "auth-cwd", "auth-env", "auth-timeout", "auth-ttl",
                                  "runner-digest", "runner-version", "dependency", "scope-read", "scope-bash"])
def test_launch_digest_binds_full_auth_and_scope_independently(launch_intent, workspace, field):
    original = literal_intent(workspace)
    changed = deepcopy(original)
    auth = changed["model"]["resolved"]["auth"]
    if field == "profile":
        changed["model"]["profile_sha256"] = "b" * 64
    elif field == "auth-cwd":
        auth["context"]["cwd"] = "/approved/reconfigured-models"
    elif field == "auth-env":
        auth["context"]["env"]["LANG"] = "C.UTF-8"
    elif field == "auth-timeout":
        auth["context"]["timeout_ms"] = 4000
    elif field == "auth-ttl":
        auth["context"]["refresh_interval_ms"] = 600000
    elif field == "runner-digest":
        auth["runner"]["sha256"] = "b" * 64
    elif field == "runner-version":
        auth["runner"]["contract_version"] = 2
    elif field == "dependency":
        auth["runner"]["dependency_digests"][str(workspace.dependency)] = "b" * 64
    elif field == "scope-read":
        changed["effective_scope"]["read"].append("/approved/new-task.md")
    else:
        changed["effective_scope"]["bash"].append("fixture-new-command")
    for intent in (original, changed):
        actual = launch_intent.intent_digest(intent)
        assert_intent_digest(intent, actual)
        assert_bound_receipt(intent, launch_intent.make_launch_receipt(intent, phase="planned"))
    assert launch_intent.intent_digest(original) != launch_intent.intent_digest(changed)


def test_resolver_includes_approved_auth_and_scope_before_hashing(launch_intent, workspace):
    workspace.caller["provider"] = "claude"
    command = json.dumps([sys.executable, str(workspace.runner)])
    workspace.profile.write_text(workspace.profile.read_text().replace(
        'wire_api = "anthropic-messages"', 'wire_api = "anthropic-messages"\nauth_command = ' + command))
    intent = resolve(launch_intent, workspace)
    auth = intent["model"]["resolved"]["auth"]
    assert auth["context"] == workspace.context["auth_context"]
    assert auth["runner"] == workspace.context["auth_runner"]
    assert intent["effective_scope"] == workspace.context["effective_scope"]
    assert intent["model"]["profile_sha256"] == canonical_digest(tomllib.loads(workspace.profile.read_text()))
    assert_intent_digest(intent, launch_intent.intent_digest(intent))
    assert_bound_receipt(intent, launch_intent.make_launch_receipt(intent, phase="planned"))


def test_normal_launch_does_not_reconfigure_settings_or_trust(launch_intent, workspace):
    home = workspace.tmp / "home"
    before = snapshot_tree(home)
    intent = resolve(launch_intent, workspace)
    owner, _, _ = owner_paths(workspace.tmp / "client", "global")
    launch_intent.save_launch_intent(owner, intent, mode="global")
    # Chain existing save_profile ownership through the caller binding test;
    # this unit case additionally protects all configured input resources.
    launch_intent.materialize_launch_intent(intent, workspace.context, operation="spawn", caller_env={})
    assert_tree_unchanged(home, before)
    assert launch_intent.load_launch_intent(owner, mode="global") == intent


def test_claude_missing_effective_scope_is_not_interactive_default(launch_intent, workspace):
    workspace.caller["provider"] = "claude"
    workspace.context.pop("effective_scope")
    with pytest.raises(launch_intent.LocalModelError, match="LOCAL_MODEL_PERMISSION_PLAN_REQUIRED"):
        resolve(launch_intent, workspace)
    assert workspace.context["effects"] == []


def test_local_does_not_inherit_catalog_effort_default(launch_intent, workspace):
    workspace.caller["effort"] = "ultra"
    with pytest.raises(launch_intent.LocalModelError, match="LOCAL_MODEL_ADAPTER_UNSUPPORTED"):
        resolve(launch_intent, workspace)
    assert workspace.context["effects"] == []


def test_name_owner_mismatch_has_zero_network_and_no_rebinding(launch_intent, workspace):
    workspace.context["identity"] = {"agent_id": 42, "name": "OtherChild", "owner_generation": 1}
    with pytest.raises(launch_intent.LocalModelError):
        resolve(launch_intent, workspace)
    assert workspace.context["effects"] == []


def test_selected_adapter_absence_is_not_cloud_fallback(launch_intent, workspace):
    common, rest = workspace.profile.read_text().split("[codex]", 1)
    workspace.profile.write_text(common + "[claude]" + rest.split("[claude]", 1)[1])
    with pytest.raises(launch_intent.LocalModelError, match="LOCAL_MODEL_ADAPTER_UNSUPPORTED"):
        resolve(launch_intent, workspace)
    assert workspace.context["effects"] == []


def test_codex_strict_output_cap_request_is_explicitly_unsupported(launch_intent, workspace):
    workspace.caller["strict_output_cap"] = True
    with pytest.raises(launch_intent.LocalModelError, match="LOCAL_MODEL_ADAPTER_UNSUPPORTED"):
        resolve(launch_intent, workspace)
