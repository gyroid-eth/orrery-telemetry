"""Executed review mutant controls. NOT native/caller conformance runs."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import urllib.request

import pytest

from .evidence import (CALLERS, CALLER_STARTUP_PROMPT, CALLER_MCP_STARTUP_PROMPT, assert_auth_matrix, assert_bound_receipt, assert_caller_capture,
                       assert_intent_digest, assert_single_owner, assert_tree_unchanged,
                       assert_resume_capture, assert_wire_rotation, capture_provider, credential_source,
                       existing_mutation_pending, intent_preimage, literal_intent,
                       owner_paths, prepare_caller_invocation, read_fresh_capture,
                       snapshot_tree, write_private_json)
from .support import Canary, canonical_digest, fake_peer, post_fixture
from .test_instruments import request


def retrieve(source):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{source.server.server_port}/credential", timeout=2) as response:
        raw = response.read()
    if not source.current.matches(raw.decode(), "x-api-key"):
        pytest.fail("fixture credential transport mismatch", pytrace=False)


@pytest.mark.parametrize("mutation", [None, "all-unsupported", "no-helper", "false-success", "false-success-on-500"])
def test_auth_matrix_detects_no_native_success_or_unused_helper(canary, mutation):
    with credential_source(canary) as source:
        if mutation != "no-helper":
            retrieve(source)
        observations = []
        for header in ("bearer", "x-api-key"):
            with fake_peer("messages", canary, auth=header,
                           fault="500" if mutation == "false-success-on-500" else None) as peer:
                post_fixture(peer, request("messages"), None if mutation == "all-unsupported" else "bearer")
                success = peer.requests[0]["auth_ok"] or mutation == "false-success"
                observations.append((peer, {"cli_version": "fixture-version", "model_success": success,
                                            "reason": "LOCAL_MODEL_AUTH_HEADER_UNSUPPORTED"}))
        if mutation is None:
            assert assert_auth_matrix(observations, source) == ["bearer"]
        else:
            with pytest.raises(AssertionError):
                assert_auth_matrix(observations, source)


@pytest.mark.parametrize("mutation", [None, "stale-token", "no-refresh-helper", "no-initial-helper"])
def test_wire_rotation_detects_old_token_reuse_despite_success_claim(canary, mutation):
    rotated = Canary()
    with credential_source(canary) as source, fake_peer("messages", canary, auth="either") as peer:
        if mutation != "no-initial-helper":
            retrieve(source)
        assert post_fixture(peer, request("messages"), "x-api-key")[0] == 200
        source.rotate(rotated)
        peer.rotate_credential(rotated)
        if mutation != "no-refresh-helper":
            retrieve(source)
        if mutation == "stale-token":
            # Construct a real stale HTTP request; never write/log the canary.
            headers = {"Content-Type": "application/json", "Anthropic-Version": "2023-06-01",
                       "X-Api-Key": canary.header("x-api-key")}
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            req = urllib.request.Request(peer.base_url + "/v1/messages", json.dumps(request("messages")).encode(), headers)
            with pytest.raises(urllib.error.HTTPError):
                opener.open(req, timeout=2)
        else:
            assert post_fixture(peer, request("messages"), "x-api-key")[0] == 200
        if mutation is None:
            assert_wire_rotation(peer, source)
        else:
            with pytest.raises(AssertionError):
                assert_wire_rotation(peer, source)


def test_compound_bash_first_approved_side_effect_is_detected(workspace):
    outside = workspace.tmp / "outside"
    outside.mkdir()
    last = outside / "last.txt"
    first = workspace.scratch.parent / "first.txt"
    first.write_text("unchanged\n")
    last.write_text("unchanged\n")
    before_first, before_last = snapshot_tree(first.parent), snapshot_tree(outside)
    assert_tree_unchanged(first.parent, before_first)
    # Wrong admission executes only the approved first component, then rejects
    # the second. Outside file and unapproved_writes=[] cannot detect this.
    first.write_text("wrongly executed first command\n")
    assert_tree_unchanged(outside, before_last)
    with pytest.raises(AssertionError, match="protected file tree changed"):
        assert_tree_unchanged(first.parent, before_first)


@pytest.mark.parametrize("mutation", ["owner", "receipt", "pending", "shadow-profile", "shadow-launch", "shadow-cache"])
def test_persisted_intent_oracle_detects_dict_preserving_overwrite(workspace, mutation):
    base = workspace.tmp / "client"
    owner, receipt, pending = owner_paths(base, "global")
    old = literal_intent(workspace)
    write_private_json(owner, {"launch_intent": old})
    write_private_json(receipt, {"phase": "exec-unknown", "intent_digest": canonical_digest(old)})
    write_private_json(pending, {"request_id": "00000000-0000-4000-8000-000000000042", "request_hash": canonical_digest(old)})
    before = snapshot_tree(base)
    assert_single_owner(base, owner)
    saved_dict = deepcopy(old)
    def wrong_revision(value):
        new = deepcopy(value)
        new["revision"] += 1
        if mutation == "owner":
            write_private_json(owner, {"launch_intent": new})
        elif mutation == "receipt":
            write_private_json(receipt, {"phase": "planned", "intent_digest": canonical_digest(new)})
        elif mutation == "pending":
            write_private_json(pending, {"request_id": "replaced", "request_hash": canonical_digest(new)})
        else:
            path = {"shadow-profile": base / "runtime/child-state.json",
                    "shadow-launch": receipt, "shadow-cache": base / "unexpected/cache.json"}[mutation]
            write_private_json(path, {"launch_intent": value})
        return new
    wrong_revision(old)
    assert old == saved_dict  # The old r0 check still passes.
    with pytest.raises(AssertionError):
        assert_tree_unchanged(base, before)
    if mutation.startswith("shadow"):
        with pytest.raises(AssertionError, match="shadow owner"):
            assert_single_owner(base, owner)


@pytest.mark.parametrize("mutation", ["constant", "omit-auth", "omit-scope", "wrong-profile"])
def test_independent_intent_digest_rejects_mutant_preimage(workspace, mutation):
    intent = literal_intent(workspace)
    correct = canonical_digest(intent_preimage(intent))
    assert_intent_digest(intent, correct)
    assert_bound_receipt(intent, {"intent_digest": correct})
    preimage = deepcopy(intent_preimage(intent))
    if mutation == "constant":
        wrong = "0" * 64
    else:
        if mutation == "omit-auth":
            preimage["model"]["resolved"].pop("auth")
        elif mutation == "omit-scope":
            preimage.pop("effective_scope")
        else:
            preimage["model"]["profile_sha256"] = "b" * 64
        wrong = canonical_digest(preimage)
    for check in (lambda: assert_intent_digest(intent, wrong),
                  lambda: assert_bound_receipt(intent, {"intent_digest": wrong})):
        with pytest.raises(AssertionError):
            check()


def test_single_caller_bypass_is_detected_by_real_capture_binary(workspace):
    capture = workspace.tmp / "capture.json"
    binary = capture_provider(workspace.tmp / "bin/capture-provider", capture)
    env = {"HOME": str(workspace.tmp / "home"), "PATH": "/usr/bin:/bin"}
    cwd = workspace.scratch.parent
    subprocess.run([str(binary), "--model", "fixture-glm-model"], cwd=cwd, env=env, check=True)
    expected = json.loads(capture.read_text())
    assert_caller_capture(capture, expected)
    # Wrong caller independently assembles catalog argv, bypassing the resolver.
    subprocess.run([str(binary), "--model", "gpt-fake"], cwd=cwd, env=env, check=True)
    with pytest.raises(AssertionError, match="caller bypass"):
        assert_caller_capture(capture, expected)


@pytest.mark.parametrize("entry", CALLERS)
@pytest.mark.parametrize("mutation", [None, "no-op", "stale-capture"])
def test_each_caller_requires_its_own_capture_despite_previous_success(workspace, entry, mutation):
    previous_entry = "operator" if entry == "dashboard" else "shell"
    owner, binary, previous, nonce = prepare_caller_invocation(workspace, previous_entry)
    env = {"HOME": str(owner.parent), "PATH": "/usr/bin:/bin"}
    command = [str(binary), "--model", "fixture-glm-model"]
    subprocess.run(command, cwd=workspace.scratch.parent, env=env, check=True)
    first = read_fresh_capture(previous, nonce)
    write_private_json(owner, {"launch_intent": literal_intent(workspace)})
    seed = owner.read_bytes()

    owner, binary, current, current_nonce = prepare_caller_invocation(
        workspace, entry, resume_owner=seed if entry == "resume-child" else None)
    assert previous.exists(), "control must retain earlier successful capture"
    assert current != previous and current_nonce != nonce
    if entry == "resume-child":
        assert owner.read_bytes() == seed  # old state is input, not execution proof
    else:
        assert not owner.exists(), "non-resume caller inherited another caller's owner"

    def probe_mutant():
        if mutation is None:
            subprocess.run(command, cwd=workspace.scratch.parent, env=env, check=True)
        elif mutation == "stale-capture":
            current.write_bytes(previous.read_bytes())
        # no-op returns without launching; old capture/seed must not rescue it.
    probe_mutant()
    if mutation is None:
        observed = read_fresh_capture(current, current_nonce)
        assert observed["argv"] == first["argv"]
        assert observed["env"] == first["env"]  # nonce never changes launch inputs
        assert_caller_capture(current, first)
    else:
        message = "did not execute" if mutation == "no-op" else "earlier caller"
        with pytest.raises(AssertionError, match=message):
            read_fresh_capture(current, current_nonce)


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("mutation", [None, "extra-sandbox", "missing-model", "reorder",
                                     "duplicate-override", "wrong-session", "moved-envelope",
                                     "duplicate-envelope", "extra-exec", "startup-prompt",
                                     "unknown-prompt", "env", "cwd"])
def test_resume_full_argv_rejects_every_non_envelope_difference(headless, mutation):
    # Independent golden vectors, not a product-generated trace or a filter of
    # selected flags. Each branch changes one otherwise valid resume capture.
    prefix = ["-C", "/fixture", "-c", "update=false", "-c", "mail.command=fixture",
              "-c", "mail.args=[]", "-c", "plugin=false"]
    tail = ["--model", "fixture-glm-model", "-c", "provider=fixture", "--sandbox", "workspace-write"]
    prompt = "exact approved fixture startup instruction"
    startup = {"argv": prefix + tail + [prompt], "env": {"HOME": "/fixture/home"}, "cwd": "/fixture"}
    envelope = (["exec"] if headless else []) + ["resume", "fixture-session"]
    observed = deepcopy(startup)
    observed["argv"] = prefix + envelope + tail
    argv = observed["argv"]
    if mutation == "extra-sandbox":
        argv += ["--sandbox", "danger-full-access"]
    elif mutation == "missing-model":
        argv.remove("fixture-glm-model")
    elif mutation == "reorder":
        argv[-2:] = reversed(argv[-2:])
    elif mutation == "duplicate-override":
        argv += ["-c", "provider=fixture"]
    elif mutation == "wrong-session":
        argv[argv.index("fixture-session")] = "wrong-session"
    elif mutation == "moved-envelope":
        observed["argv"] = envelope + prefix + tail
    elif mutation == "duplicate-envelope":
        argv += envelope
    elif mutation == "extra-exec":
        argv.insert(10, "exec")
    elif mutation == "startup-prompt":
        argv.append(prompt)
    elif mutation == "unknown-prompt":
        startup["argv"][-1] = "unapproved prompt"
    elif mutation == "env":
        observed["env"]["HOME"] = "/wrong"
    elif mutation == "cwd":
        observed["cwd"] = "/wrong"
    def check():
        assert_resume_capture(observed, startup, startup_prompt=prompt,
                              envelope_index=10, session_id="fixture-session", headless=headless)
    if mutation is None:
        check()
    else:
        with pytest.raises(AssertionError):
            check()


def oracle_control_binding(workspace, *, no_op_entry=None, drop_tools_entry=None,
                           config_mutation=None, proxy_script=None, mail_command=None):
    """Deliberate synthetic binding ONLY for exercising oracle controls.

    Never install this as caller_acceptance or count it as product acceptance.
    """
    # Synthetic oracle inputs only; actual caller_acceptance never imports
    # this helper. Do not repair changed resources once the control starts.
    trusted=workspace.tmp/'install/bin/lib/runtime_client.py'
    interpreter=workspace.tmp/'delivery-python/bin/python'
    for target,source in ((trusted,Path(__file__).resolve().parents[2]/'bin/lib/runtime_client.py'),
                          (interpreter,Path(sys.executable).resolve(strict=True))):
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists() and not target.is_symlink():shutil.copyfile(source,target)
    seen = []
    def controlled_binding(entry, *, provider_binary, owner, **kwargs):
        seen.append(entry)
        if entry == no_op_entry:
            return  # previous successful capture remains on disk
        intent = deepcopy(workspace.caller)
        intent["model"] = {"kind": "local-profile", "ref": "local:fixture",
                           "resolved": {"model": "fixture-glm-model"}}
        selected_tools = deepcopy(workspace.caller['tools'])
        intent['tools'] = {} if entry == drop_tools_entry else selected_tools
        write_private_json(owner, {"launch_intent": intent})
        prefix = ["-C", str(workspace.scratch.parent), "-c", "update=false",
                  "-c", "mcp_servers.orrery-mail.command="+json.dumps(str(
                      interpreter if mail_command is None else mail_command)),
                  "-c", "mcp_servers.orrery-mail.args="+json.dumps([
                      str(proxy_script if proxy_script is not None else
                          trusted),
                      '--context',str(owner.parent/'runtime-client.json'),'proxy']), "-c", "plugin=false"]
        tail = ["--model", "fixture-glm-model", "-c", 'provider="fixture"']
        if selected_tools:
            tail += ['-c','mcp_servers.fixture_extra.command="/usr/bin/true"',
                     '-c','mcp_servers.fixture_extra.args=[]']
        if config_mutation == 'remove-mail':
            tail += ['-c','mcp_servers.orrery-mail.enabled=false']
        elif config_mutation == 'remove-shell':
            tail += ['-c','features.shell_tool=false']
        elif config_mutation == 'disable-shell':
            tail += ['--disable','shell_tool']
        elif config_mutation == 'disable-shell-equals':
            tail += ['--disable=shell_tool']
        elif config_mutation == 'disable-shell-config-equals':
            tail += ['--config=features.shell_tool=false']
        elif config_mutation == 'remove-extra':
            tail += ['-c','mcp_servers.fixture_extra.enabled=false']
        argv = (prefix + ["resume", "native-fixture-session"] + tail
                if entry == "resume-child" else prefix + tail + [
                    CALLER_MCP_STARTUP_PROMPT if selected_tools else CALLER_STARTUP_PROMPT])
        # Independent oracle control, NOT a production acceptance adapter.
        receipt = owner.parent/'runtime/launch.json'
        receipt.parent.mkdir(parents=True, exist_ok=True)
        write_private_json(receipt, {'launch_id':'188a00a9-330e-412b-81e4-a1af8ae74bd9',
                                    'receipt_nonce':'control-receipt-'+entry})
        subprocess.run(['/bin/sh', '-c', 'export AGENTSTACK_PROVIDER_PID=$$; exec "$@"',
                        'fixture', str(provider_binary), *argv], cwd=workspace.scratch.parent,
                       env={"HOME": str(owner.parent / "runtime/provider-home/codex"),
                            "CODEX_HOME": str(owner.parent / "runtime/provider-home/codex"),
                            "PATH": "/usr/bin:/bin",
                            "AGENTSTACK_LAUNCH_ID":'188a00a9-330e-412b-81e4-a1af8ae74bd9',
                            "AGENTSTACK_LAUNCH_NONCE":'control-receipt-'+entry}, check=True)
    return controlled_binding, seen


@pytest.mark.parametrize("no_op_entry", [None, "prepare-child", "dashboard"])
def test_caller_contract_itself_rejects_no_op_bindings(workspace, no_op_entry):
    # Exercise wiring as well as measurement helpers, never actual callers.
    from .test_caller_contract import test_real_callers_preserve_literal_intent_and_same_executed_argv_env as contract
    controlled_binding, seen = oracle_control_binding(workspace,no_op_entry=no_op_entry)
    if no_op_entry is None:
        contract(controlled_binding, workspace)
        assert seen == list(CALLERS)
    else:
        with pytest.raises(AssertionError, match="current caller did not execute"):
            contract(controlled_binding, workspace)
        assert seen == list(CALLERS)[:list(CALLERS).index(no_op_entry) + 1]


def test_pending_seed_uses_existing_wire_contract():
    row = existing_mutation_pending()
    assert row["request_hash"] == canonical_digest(row["canonical_payload"])
    assert row["canonical_payload"]["arguments"]["task_description"] == "fixture"
    assert "registration_token" not in row["canonical_payload"]["arguments"]


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_only_live_fixture_socket_addresses_are_allowed(canary, method):
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="non-fixture network"):
            getattr(sock, method)(("127.0.0.1", 1))
    with fake_peer("responses", canary) as peer:
        address = ("127.0.0.1", peer._server.server_port)
        assert post_fixture(peer, request("responses"))[0] == 200
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="non-fixture network"):
            getattr(sock, method)(address)


@pytest.mark.parametrize("fault", ["untrusted-ca", "wrong-host"])
def test_tls_rejection_occurs_before_credential_request(canary, tmp_path, fault):
    ip = "127.0.0.2" if fault == "wrong-host" else "127.0.0.1"
    with fake_peer("responses", canary, tls_dir=tmp_path / "tls", cert_ip=ip) as peer:
        if fault == "wrong-host":
            with pytest.raises(urllib.error.URLError):
                post_fixture(peer, request("responses"))
        else:
            # Deliberately omit the fixture CA, never disable verification.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with pytest.raises(urllib.error.URLError):
                opener.open(peer.base_url + "/v1/responses", timeout=2)
        assert peer.requests == []
