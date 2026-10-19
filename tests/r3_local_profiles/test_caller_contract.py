"""Actual-caller integration specifications; source labels are not evidence.

The missing caller_acceptance TEST adapter must call each named real entry
and use existing isolated client/Mail fixtures. It may replace external commands
with capture binaries, but never replace the entry/parser/resolver under test.
"""
import hashlib
import json

import pytest

from .evidence import (CALLERS, CALLER_STARTUP_PROMPT, assert_caller_capture,
                       assert_resume_capture, assert_single_owner, assert_tree_unchanged,
                       owner_paths, prepare_caller_invocation, read_fresh_capture, snapshot_tree,
                       caller_client_root, assert_runtime_capture, assert_caller_tool_configuration)


def test_real_callers_preserve_literal_intent_and_same_executed_argv_env(caller_probe, workspace):
    assert_real_callers(caller_probe,workspace,expected_tools={},startup_prompt=CALLER_STARTUP_PROMPT)


def assert_real_callers(caller_probe, workspace, *, expected_tools, startup_prompt):
    captures, intents = [], []
    resume_owner = None
    for entry in CALLERS:
        owner, binary, capture, nonce = prepare_caller_invocation(
            workspace, entry, resume_owner=resume_owner if entry == "resume-child" else None)
        approved_binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
        approved_script_hash = hashlib.sha256(capture.with_suffix('.py').read_bytes()).hexdigest()
        # Each binding reconstructs the same isolated client/name/context and
        # invokes the real entry. It must NOT synthesize the capture/intent or
        # replace the production entry/parser/resolver. All external commands
        # are fixture commands, including transport/tmux (no native CLI needed).
        caller_probe(entry, workspace=workspace, real_entry=CALLERS[entry],
                     provider_binary=binary, owner=owner,
                     prepare_only=(entry == "operator"), fresh_same_context=True,
                     provider_session_id="native-fixture-session", task_delivery="mail",
                     headless=False, fixture_mcp={'name':'fixture_extra',
                         'definition':{'command':'/usr/bin/true','args':[]}} if expected_tools else None)
        # A return value, an old owner or the previous caller's capture is not
        # execution evidence. Check the fresh process artifact first.
        execution = read_fresh_capture(capture, nonce)
        assert hashlib.sha256(binary.read_bytes()).hexdigest() == approved_binary_hash
        assert_runtime_capture(execution, owner.parent/'runtime/launch.json', capture=capture, binary=binary,
                               expected_script_sha256=approved_script_hash)
        saved = json.loads(owner.read_text())["launch_intent"]
        if entry == "shell":
            resume_owner = owner.read_bytes()
        assert saved["model"]["ref"] == "local:fixture"
        assert saved["model"]["resolved"]["model"] == "fixture-glm-model"
        assert saved['tools'] == expected_tools, 'caller changed literal tools'
        for field in ("cwd", "task", "name", "base", "resources", "detached"):
            assert saved[field] == workspace.caller[field]
        argv = execution["argv"]
        models = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--model=")]
        models += [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg in ("--model", "-m")]
        assert models == ["fixture-glm-model"]
        assert execution["cwd"] == str(workspace.scratch.parent)
        assert execution["env"]["HOME"] == str(caller_client_root(workspace) / "runtime/provider-home/codex")
        assert_caller_tool_configuration(execution,owner,expected_tools)
        if captures:
            if entry == "resume-child":
                # Existing interactive Codex command: five fixed option pairs,
                # then resume/session, then all tool/model/provider options.
                # This reviewed golden must change explicitly if R3 changes
                # that command layout; never ask the adapter what to discard.
                assert_resume_capture(execution, captures[0],
                                      startup_prompt=startup_prompt,
                                      envelope_index=10, session_id="native-fixture-session")
            else:
                assert_caller_capture(capture, captures[0])
            assert saved == intents[0]
        captures.append(execution)
        intents.append(saved)
        assert_single_owner(caller_client_root(workspace), owner)


@pytest.mark.parametrize("stage", ["intent-saved", "launch-planned", "exec-observed", "receipt-pending"])
def test_actual_client_reconstruction_replays_old_pending_without_revision(caller_probe, workspace, stage):
    base = caller_client_root(workspace)
    before = []
    def pending_saved():
        before.append(snapshot_tree(base))
    caller_probe("pending-replay", workspace=workspace, interrupt_at=stage,
                 client_root=base, on_pending_saved=pending_saved,
                 reconstruct_runtime_client=True, attempt_new_revision=True,
                 hold_receipt_pending=True,
                 unknown_requires_operator=stage in ("exec-observed", "receipt-pending"))
    assert len(before) == 1, "actual pending-save boundary not exercised"
    assert_tree_unchanged(base, before[0])
    owner, _, _ = owner_paths(base, "global")
    assert_single_owner(base, owner)
