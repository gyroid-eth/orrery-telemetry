"""Actual adapter mutation, not a fabricated owner/capture or native proof."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from .caller_acceptance import DeliveryCallerProbe
from .caller_binding import assert_single_replacement, assert_real_delivery_gates
from .evidence import (CALLER_MCP_STARTUP_PROMPT, caller_client_root,
                       assert_runtime_capture, assert_single_owner)
from .test_caller_contract import assert_real_callers


def test_real_nonempty_five_caller_case_detects_adapter_dropping_tools(
        caller_probe, workspace, monkeypatch):
    workspace.caller['tools'] = {'mcp':['fixture_extra']}
    requested = workspace.caller
    requested_before = deepcopy(requested)
    original = caller_probe.invoke_current
    assert original.__func__ is DeliveryCallerProbe.invoke_current
    reached = []
    pins = []

    def drop_selection(entry, *, workspace, **options):
        assert workspace.caller is requested and workspace.caller == requested_before
        incoming = deepcopy(requested)
        incoming['tools'] = {}
        assert {key for key in incoming if incoming[key] != requested[key]} == {'tools'}
        binary = Path(options['provider_binary'])
        dispatch = json.loads(binary.with_name('fixture-provider-dispatch.json').read_text())
        script = Path(dispatch['script'])
        pins.append((binary, script, hashlib.sha256(script.read_bytes()).hexdigest()))
        # This actual adapter then configures AND invokes the real entry with
        # the wrong empty input. Do not edit the saved intent/capture afterwards.
        workspace.caller = incoming
        try:
            result = original(entry, workspace=workspace, **options)
        finally:
            workspace.caller = requested
        reached.append(entry)  # only a completed real delivery counts
        return result

    with monkeypatch.context() as mutation:
        mutation.setattr(caller_probe, 'invoke_current', drop_selection)
        # Reuse the actual all-five positive and its literal oracle UNCHANGED.
        # An unrelated setup/ps/refusal is not this designated detection.
        with pytest.raises(AssertionError, match='^caller changed literal tools') as detected:
            assert_real_callers(caller_probe, workspace,
                expected_tools={'mcp':['fixture_extra']},
                startup_prompt=CALLER_MCP_STARTUP_PROMPT)
        assert str(detected.value).splitlines()[0] == 'caller changed literal tools'
    assert caller_probe.invoke_current == original
    assert workspace.caller is requested and requested == requested_before
    # The all-five checker stops on its first bad caller. This is one actual
    # always-empty mutation, NOT five independently exercised bad entrypoints.
    assert reached == ['shell'] and len(pins) == 1
    assert_single_replacement(caller_probe.last_entry_rows)
    assert_real_delivery_gates(caller_probe.last_entry_rows, caller_probe.install)
    calls = {(row.get('file'), row.get('name')) for row in caller_probe.last_entry_rows
             if row['event'] == 'real-call'}
    for name in ('entry_operation', '_shell_caller_input', '_check_shell_spawn', '_prepare_child_launch'):
        assert (str(caller_probe.install/'bin/lib/runtime_client.py'), name) in calls
    root = caller_client_root(workspace)
    owner = root/'profile.json'
    assert json.loads(owner.read_text())['launch_intent']['tools'] == {}
    assert_single_owner(root, owner)
    binary, script, expected_hash = pins[0]
    assert hashlib.sha256(script.read_bytes()).hexdigest() == expected_hash
    observed = json.loads(script.with_suffix('.json').read_text())
    assert_runtime_capture(observed, root/'runtime/launch.json',
        capture=script.with_suffix('.json'), binary=binary,
        expected_script_sha256=expected_hash)
    receipt = json.loads((root/'runtime/launch.json').read_text())
    assert receipt['phase'] == receipt['termination']['phase'] == 'ended'
    assert receipt['execution']['outcome'] == {'state':'exited', 'returncode':0}
    assert (root/'runtime/task.md').read_bytes() == workspace.task.read_bytes()
