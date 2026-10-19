"""Green instrument checks plus deliberately wrong implementations as controls."""
from copy import deepcopy
import json

import pytest

from .support import (
    MAIL, MODEL, MESSAGE_TOOL, RESPONSE_TOOL, assert_auth_context, assert_delegation, assert_denial,
    assert_failure, assert_roundtrip, assert_same_launch, canonical_digest,
    fake_peer, post_fixture,
)


def request(protocol, turn=1):
    body = {"model": MODEL, "stream": True, "tools": [RESPONSE_TOOL if protocol == "responses" else MESSAGE_TOOL]}
    if protocol == "responses":
        body["input"] = ([{"role": "user", "content": "echo fixture"}] if turn == 1 else [
            {"type": "function_call", "call_id": "call_fixture", "name": "fixture_echo", "arguments": '{"text":"fixture"}'},
            {"type": "function_call_output", "call_id": "call_fixture", "output": "fixture"}])
    else:
        body["messages"] = ([{"role": "user", "content": "echo fixture"}] if turn == 1 else [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_fixture", "name": "fixture_echo", "input": {"text": "fixture"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_fixture", "content": "fixture"}]}])
    return body


@pytest.mark.parametrize("protocol", ["responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_fake_server_roundtrip_and_bad_result_control(protocol, stream, canary):
    with fake_peer(protocol, canary) as peer:
        bodies = []
        for turn in (1, 2):
            body = request(protocol, turn)
            body["stream"] = stream
            status, payload = post_fixture(peer, body)
            assert status == 200
            if stream:
                events = [json.loads(line[6:]) for line in payload.decode().splitlines() if line.startswith("data: ")]
                assert events[-1]["type"] == ("response.completed" if protocol == "responses" else "message_stop")
                if turn == 1:
                    assert any(e["type"] == ("response.function_call_arguments.done" if protocol == "responses" else "content_block_delta") for e in events)
            else:
                assert json.loads(payload)["model"] == MODEL
            bodies.append(body)
        assert_roundtrip(peer)
        # A broken CLI drops the tool result on turn two.
        peer.requests[1]["body"]["input" if protocol == "responses" else "messages"] = []
        with pytest.raises(AssertionError):
            assert_roundtrip(peer)


@pytest.mark.parametrize("protocol,auth", [("responses", "bearer"), ("messages", "bearer"), ("messages", "x-api-key")])
def test_auth_instrument_rejects_other_header(protocol, auth, canary):
    with fake_peer(protocol, canary, auth=auth) as peer:
        assert post_fixture(peer, request(protocol), auth)[0] == 200
        other = "x-api-key" if auth == "bearer" else "bearer"
        assert post_fixture(peer, request(protocol), other)[0] == 401
        assert peer.requests[0]["auth_form"] == auth
        canary.scan({"sanitized-peer-transcript": peer.requests})


def test_authless_peer_rejects_inherited_credential(canary):
    with fake_peer("responses", canary) as peer:
        assert post_fixture(peer, request("responses"), "bearer")[0] == 401


def test_authenticated_peer_uses_verified_fixture_tls(canary, tmp_path):
    with fake_peer("responses", canary, auth="bearer", tls_dir=tmp_path / "tls") as peer:
        assert peer.base_url.startswith("https://127.0.0.1:")
        assert post_fixture(peer, request("responses"), "bearer")[0] == 200


@pytest.mark.parametrize("mutation", ["model", "path", "tools"])
def test_wire_wrong_implementation_control(mutation, canary):
    with fake_peer("responses", canary) as peer:
        body = request("responses")
        path = None
        if mutation == "model":
            body["model"] = "gpt-fake"
        elif mutation == "path":
            path = "/v1/chat/completions"
        else:
            body["tools"] = [{"type": "code_mode"}]
        assert post_fixture(peer, body, path=path)[0] in (400, 404)
        assert peer.violations


@pytest.mark.parametrize("protocol", ["responses", "messages"])
def test_native_mcp_prefix_is_accepted_without_changing_tool_schema(protocol, canary):
    with fake_peer(protocol, canary) as peer:
        body = request(protocol)
        body["tools"] = [{**body["tools"][0], "name": "mcp__fixture__fixture_echo"}]
        status, payload = post_fixture(peer, body)
        assert status == 200
        assert b"mcp__fixture__fixture_echo" in payload
        body["tools"][0]["parameters" if protocol == "responses" else "input_schema"] = {"type": "object"}
        assert post_fixture(peer, body)[0] == 400


@pytest.mark.parametrize('protocol',['responses','messages'])
@pytest.mark.parametrize('name',['other__fixture_echo','mcp__other__fixture_echo',
                                'mcp__fixture__extra__fixture_echo'])
def test_unapproved_mcp_server_cannot_impersonate_fixture_tool_by_suffix(protocol,canary,name):
    with fake_peer(protocol,canary) as peer:
        body = request(protocol)
        body['tools'] = [{**body['tools'][0],'name':name}]
        assert post_fixture(peer,body)[0] == 400
        assert peer.violations == ['function-tool-missing']


@pytest.mark.parametrize("fault,status", [("401", 401), ("500", 500), ("redirect", 307), ("disconnect", None)])
def test_fault_instrument(fault, status, canary):
    import http.client
    with fake_peer("responses", canary, fault=fault) as peer:
        peer.redirect_url = peer.base_url + "/must-not-follow"
        if status is None:
            with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
                post_fixture(peer, request("responses"))
        else:
            assert post_fixture(peer, request("responses"))[0] == status
        assert len(peer.requests) == 1


SURFACES = ("argv", "process-env", "provider-env-ps", "runner-env", "parent-env", "tmux-env",
            "config-file", "intent-file", "handoff", "stdout", "stderr", "error-log",
            "dashboard", "status", "mail", "bash-env", "hook-env", "mcp-env")


@pytest.mark.parametrize("surface", SURFACES)
def test_canary_detects_leak_without_printing_value(canary, surface):
    canary.scan({surface: "safe"})
    with pytest.raises(AssertionError) as caught:
        canary.scan({surface: canary.header("x-api-key")})
    assert str(caught.value) == "credential exposed on " + surface
    assert "redacted" in repr(canary)
    canary.scan({"failure-message": str(caught.value)})


def test_digest_oracle_includes_both_adapters_and_array_order():
    original = {"model": MODEL, "codex": {"auth_command": ["/fixture/helper", "one", "two"]}, "claude": {"base_url": "https://fixture.invalid"}}
    reordered = {"claude": original["claude"], "codex": original["codex"], "model": MODEL}
    assert canonical_digest(original) == canonical_digest(reordered)
    bad = deepcopy(original)
    bad["claude"]["base_url"] = "https://changed.invalid"
    assert canonical_digest(original) != canonical_digest(bad)
    bad = deepcopy(original)
    bad["codex"]["auth_command"].reverse()
    assert canonical_digest(original) != canonical_digest(bad)


def test_replay_oracle_rejects_caller_env_recomputation():
    baseline = {"intent_digest": "intent", "profile_sha256": "profile", "model": MODEL,
                "base_url": "https://fixture.invalid", "context_window": 131072,
                "max_output_tokens": 16384, "provider_session_id": "native-fixture-session",
                "execution": {"argv": ["fixture-cli", "--model", MODEL], "env": {}, "cwd": "/fixture/cwd"},
                "auth_plan": {"cwd": "/fixture/models", "refresh_interval_ms": 300000}}
    plans = [deepcopy(baseline) for _ in range(3)]
    assert_same_launch(plans)
    plans[2]["execution"]["env"]["HOME"] = "/caller-controlled"
    with pytest.raises(AssertionError, match="saved launch intent drift"):
        assert_same_launch(plans)


@pytest.mark.parametrize("field,value", [("argv", ["/caller/helper"]), ("cwd", "/caller/cwd"),
                                          ("env", {"PATH": "/caller/bin"}), ("shell", True), ("timeout", 60)])
def test_auth_context_oracle_rejects_wrong_implementation(field, value):
    plan = {"argv": ["/approved/helper"], "cwd": "/approved/models",
            "env": {"PATH": "/usr/bin:/bin", "LANG": "C"}, "timeout_ms": 5000}
    call = {"argv": plan["argv"], "cwd": plan["cwd"], "env": plan["env"], "timeout": 5}
    assert_auth_context(call, plan)
    with pytest.raises(AssertionError):
        assert_auth_context({**call, field: value}, plan)


def delegation_trace(workspace):
    events = [{"tool": tool} for tool in [MAIL[0], "Read", MAIL[1], "Edit", MAIL[2], MAIL[3], MAIL[4]]]
    events[3].update(path=str(workspace.scratch), reservation_held=True)
    return {"permission_mode": "dontAsk", "effective_scope": workspace.scope,
            "human_prompts": 0, "reservation_hook_enabled": True,
            "events": events, "active_reservations": []}


@pytest.mark.parametrize("mutation", ["hook", "wide-scope", "prompt", "reservation", "mail"])
def test_effective_scope_oracle_rejects_wrong_implementation(workspace, mutation):
    good = delegation_trace(workspace)
    assert_delegation(good, workspace.scope, workspace.scratch)
    bad = deepcopy(good)
    if mutation == "hook":
        bad["reservation_hook_enabled"] = False
    elif mutation == "wide-scope":
        bad["effective_scope"]["inherited_bash_allow"] = ["Bash"]
    elif mutation == "prompt":
        bad["human_prompts"] = 1
    elif mutation == "reservation":
        bad["events"][3]["reservation_held"] = False
    else:
        bad["events"].pop()
    with pytest.raises(AssertionError):
        assert_delegation(bad, workspace.scope, workspace.scratch)


def test_denial_oracle_rejects_partial_compound_bash_side_effect():
    trace = {"denied": True, "human_prompts": 0, "unapproved_reads": [],
             "unapproved_writes": [], "active_reservations": [], "events": [{"tool": MAIL[4]}]}
    assert_denial(trace)
    trace["unapproved_writes"] = ["/fixture/outside/created-by-second-command"]
    with pytest.raises(AssertionError, match="scope side effect"):
        assert_denial(trace)


def test_failure_oracle_rejects_cloud_fallback_and_false_preexec():
    trace = {"reason": "LOCAL_MODEL_AUTH_FAILED", "cloud_requests": 0,
             "other_origin_requests": 0, "model_success": False,
             "exec_observed": True, "phase": "exec-observed", "effects": []}
    assert_failure(trace, trace["reason"], pre_exec=False)
    for key, value in (("cloud_requests", 1), ("phase", "pre-exec")):
        bad = {**trace, key: value}
        with pytest.raises(AssertionError):
            assert_failure(bad, trace["reason"], pre_exec=False)
