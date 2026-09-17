from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess

import pytest

from service_teardown import TEST_LABEL_PREFIX


ROOT = pathlib.Path(__file__).resolve().parent.parent
REREGISTER = ROOT / "bin" / "agentstack-reregister"
REGISTER_LIB = ROOT / "bin" / "lib" / "agentstack-register.sh"
BASH = "/bin/bash"
AGENT_NAME = "FixtureAgent"
OWNER_SECRET = "OWNER_TOKEN_SENTINEL_do_not_print"
BEARER_SECRET = "BEARER_TOKEN_SENTINEL_do_not_print"
SERVER_SECRETS = (
    "LEAK_CURL_STDERR_SENTINEL",
    "LEAK_HTTP_BODY_SENTINEL",
    "LEAK_RPC_MESSAGE_SENTINEL",
    "LEAK_TOOL_TEXT_SENTINEL",
    "LEAK_MALFORMED_SENTINEL",
    "LEAK_RETURNED_NAME_SENTINEL",
    "LEAK_UNRELATED_TEXT_SENTINEL",
)


FAKE_CURL = r'''#!/usr/bin/env python3
import json
import os
import stat
import sys

argv = sys.argv[1:]
raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except Exception:
    payload = {}
params = payload.get("params") if isinstance(payload, dict) else {}
params = params if isinstance(params, dict) else {}
tool = params.get("name", "")
arguments = params.get("arguments")
arguments = arguments if isinstance(arguments, dict) else {}

with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as handle:
    output_path = argv[argv.index("--output") + 1] if "--output" in argv else ""
    diag_path = os.environ.get("AGS_MCP_DIAG_FILE", "")
    handle.write(json.dumps({
        "tool": tool,
        "arguments": arguments,
        "output_mode": stat.S_IMODE(os.stat(output_path).st_mode) if output_path else None,
        "diag_mode": stat.S_IMODE(os.stat(diag_path).st_mode) if diag_path else None,
        "output_path": output_path,
        "diag_path": diag_path,
    }) + "\n")

if tool == "ensure_project":
    mode_key = "FAKE_ENSURE_MODE"
elif tool == "register_agent":
    mode_key = "FAKE_REGISTER_MODE"
elif tool == "whois":
    mode_key = "FAKE_WHOIS_MODE"
else:
    mode_key = "FAKE_POLICY_MODE"
mode = os.environ.get(mode_key, "success")
status = os.environ.get("FAKE_HTTP_STATUS", "200") if mode == "http" else "200"
if mode == "transport":
    sys.stderr.write("LEAK_CURL_STDERR_SENTINEL\n")
    raise SystemExit(int(os.environ.get("FAKE_CURL_EXIT", "7")))
if mode == "http":
    body = "LEAK_HTTP_BODY_SENTINEL"
elif mode == "rpc":
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "error": {"code": -32001, "message": "LEAK_RPC_MESSAGE_SENTINEL"},
    })
elif mode == "rpc-nonnumeric":
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "error": {"code": "LEAK_RPC_MESSAGE_SENTINEL", "message": "LEAK_RPC_MESSAGE_SENTINEL"},
    })
elif mode == "tool":
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"isError": True, "content": [{"type": "text", "text": "LEAK_TOOL_TEXT_SENTINEL"}]},
    })
elif mode == "legacy-tool":
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": "Error calling tool: LEAK_TOOL_TEXT_SENTINEL"}]},
    })
elif mode == "malformed":
    body = "not-json LEAK_MALFORMED_SENTINEL"
elif mode == "null":
    body = "null"
elif mode == "array":
    body = '["LEAK_MALFORMED_SENTINEL"]'
elif mode == "unexpected-result":
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "result": None})
elif mode == "empty-plain":
    body = "{}"
elif mode == "empty-result":
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "result": {}})
elif mode == "unrelated-text":
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": "LEAK_UNRELATED_TEXT_SENTINEL"}]},
    })
elif mode == "project-plain":
    body = json.dumps({"id": 17, "human_key": arguments.get("human_key", "")})
elif mode == "project-result":
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"id": 17, "human_key": arguments.get("human_key", "")},
    })
elif mode == "project-text":
    project = {"id": 17, "human_key": arguments.get("human_key", "")}
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": json.dumps(project)}]},
    })
else:
    if tool == "register_agent":
        name = arguments.get("name") or "FixtureAgent"
        if mode == "identity":
            name = "LEAK_RETURNED_NAME_SENTINEL"
        structured = {"id": 41, "registration_token": "SERVER_OWNER_SENTINEL"}
        if mode != "missing-name":
            structured["name"] = name
    elif tool == "ensure_project":
        structured = {"id": 17, "human_key": arguments.get("human_key", "")}
    elif tool == "whois":
        structured = {"id": 41, "name": arguments.get("agent_name") or "FixtureAgent"}
    else:
        structured = {"status": "ok"}
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"structuredContent": structured},
    })

if "--output" in argv:
    output_path = argv[argv.index("--output") + 1]
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(body)
    write_out = argv[argv.index("--write-out") + 1] if "--write-out" in argv else ""
    sys.stdout.write(write_out.replace("%{http_code}", status))
else:
    sys.stdout.write(body)
'''


def _write_executable(path: pathlib.Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _fixture_env(tmp_path: pathlib.Path, credential_source: str | None = "runtime-file") -> tuple[dict[str, str], pathlib.Path]:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    bindir = tmp_path / "bin"
    temp_dir = tmp_path / "tmp"
    for path in (home, runtime, bindir, temp_dir):
        path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "curl-calls.jsonl"
    _write_executable(bindir / "curl", FAKE_CURL)

    env = dict(os.environ)
    for key in (
        "AGENT_NAME",
        "AGENTSTACK_ENV_FILE",
        "AGENTSTACK_MAIL_ENV",
        "MAIL_ENV",
        "MCP_AGENT_MAIL_TOKEN",
        "CHILD_REGISTRATION_TOKEN",
        "PARENT_AGENT",
        "PROJECT_KEY",
        "AGENTSTACK_PROJECT_REPOSITORY",
        "AGENTSTACK_PROJECT_WORK_DIR",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "AGENTSTACK_HOME": str(home / ".agentstack"),
            "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
            "AGENTSTACK_ENV_FILE": str(tmp_path / "missing-env.sh"),
            "AGENTSTACK_PROJECT_KEY": "fixture-project",
            "AGENTSTACK_PROJECT_WORK_DIR": str(tmp_path),
            "AGENTSTACK_RUNTIME_DIR": str(runtime),
            "AGENTSTACK_REGISTER_LIB": str(REGISTER_LIB),
            "AGENTSTACK_MCP_URL": "http://fixture.invalid/mcp",
            "AGENTSTACK_CONTACT_POLICY": "off",
            "FAKE_CURL_LOG": str(log),
            "MCP_AGENT_MAIL_TOKEN": BEARER_SECRET,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "TMPDIR": str(temp_dir),
        }
    )

    if credential_source == "runtime-file":
        (runtime / f"agent_token_{AGENT_NAME}").write_text(OWNER_SECRET, encoding="utf-8")
    elif credential_source == "inherited":
        env["CHILD_REGISTRATION_TOKEN"] = OWNER_SECRET
    elif credential_source == "child-state":
        child_dir = runtime / "child-agents"
        child_dir.mkdir()
        (child_dir / f"{AGENT_NAME}.json").write_text(
            json.dumps({"registration_token": OWNER_SECRET}), encoding="utf-8"
        )
        env["PARENT_AGENT"] = "FixtureParent"
    elif credential_source is not None:
        raise AssertionError(f"unknown fixture credential source: {credential_source}")
    return env, log


def _run_wrapper(tmp_path: pathlib.Path, credential_source: str | None = "runtime-file", **env_updates: str):
    env, log = _fixture_env(tmp_path, credential_source)
    env.update(env_updates)
    completed = subprocess.run(
        [BASH, str(REREGISTER), AGENT_NAME, "codex", "fixture-model"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    calls = []
    if log.exists():
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert not list((tmp_path / "tmp").iterdir())
    return completed, calls


def _assert_no_secrets(completed: subprocess.CompletedProcess[str]) -> None:
    output = completed.stdout + completed.stderr
    assert OWNER_SECRET not in output
    assert BEARER_SECRET not in output
    assert "SERVER_OWNER_SENTINEL" not in output
    for secret in SERVER_SECRETS:
        assert secret not in output
    assert "Traceback" not in output


def test_success_keeps_exact_stdout_and_cleans_temporary_files(tmp_path: pathlib.Path):
    completed, calls = _run_wrapper(tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"agentstack-reregister: registered {AGENT_NAME}\n"
    assert completed.stderr == ""
    assert [call["tool"] for call in calls] == ["whois", "ensure_project", "register_agent"]
    assert calls[0]["arguments"] == {
        "project_key": "fixture-project",
        "agent_name": AGENT_NAME,
        "registration_token": OWNER_SECRET,
    }
    diagnosed_calls = calls[1:]
    assert all(call["output_mode"] == 0o600 for call in diagnosed_calls)
    assert all(call["diag_mode"] == 0o600 for call in diagnosed_calls)
    assert len({call["output_path"] for call in diagnosed_calls}) == len(diagnosed_calls)
    assert len({call["diag_path"] for call in diagnosed_calls}) == len(diagnosed_calls)
    _assert_no_secrets(completed)


@pytest.mark.parametrize("ensure_mode", ["success", "project-plain", "project-result", "project-text"])
def test_supported_project_response_shapes_reach_registration(
    tmp_path: pathlib.Path, ensure_mode: str
):
    completed, calls = _run_wrapper(tmp_path, FAKE_ENSURE_MODE=ensure_mode)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"agentstack-reregister: registered {AGENT_NAME}\n"
    assert completed.stderr == ""
    assert [call["tool"] for call in calls] == ["whois", "ensure_project", "register_agent"]
    _assert_no_secrets(completed)


@pytest.mark.parametrize(
    ("policy_mode", "policy_calls"),
    [("success", 1), ("array", 1)],
)
def test_contact_policy_response_never_leaks_raw_stderr_from_successful_registration(
    tmp_path: pathlib.Path, policy_mode: str, policy_calls: int
):
    completed, calls = _run_wrapper(
        tmp_path,
        AGENTSTACK_CONTACT_POLICY="open",
        FAKE_POLICY_MODE=policy_mode,
    )
    assert completed.returncode == 0
    assert completed.stdout == f"agentstack-reregister: registered {AGENT_NAME}\n"
    assert completed.stderr == ""
    assert [call["tool"] for call in calls] == [
        "whois",
        "ensure_project",
        "register_agent",
        *("set_contact_policy" for _ in range(policy_calls)),
    ]
    _assert_no_secrets(completed)


def test_missing_owner_credential_fails_locally_without_transport(tmp_path: pathlib.Path):
    completed, calls = _run_wrapper(tmp_path, credential_source=None)
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == "agentstack-reregister: stage=local-token reason=credential-unavailable\n"
    assert calls == []
    _assert_no_secrets(completed)


def test_missing_library_has_a_fixed_local_diagnostic(tmp_path: pathlib.Path):
    env, log = _fixture_env(tmp_path)
    env["AGENTSTACK_REGISTER_LIB"] = str(tmp_path / "missing-register-library.sh")
    completed = subprocess.run(
        [BASH, str(REREGISTER), AGENT_NAME],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == "agentstack-reregister: stage=local-token reason=library-unavailable\n"
    assert not log.exists()
    _assert_no_secrets(completed)


@pytest.mark.parametrize("missing", ["agent", "project"])
def test_missing_required_input_has_a_fixed_local_diagnostic(tmp_path: pathlib.Path, missing: str):
    env, log = _fixture_env(tmp_path)
    argv = [BASH, str(REREGISTER), AGENT_NAME]
    if missing == "agent":
        argv = [BASH, str(REREGISTER)]
    else:
        env.pop("AGENTSTACK_PROJECT_KEY")
    completed = subprocess.run(
        argv,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == "agentstack-reregister: stage=local-token reason=input-missing\n"
    assert not log.exists()
    _assert_no_secrets(completed)


@pytest.mark.parametrize("credential_source", ["runtime-file", "inherited", "child-state"])
def test_failure_reports_only_the_selected_credential_source(tmp_path: pathlib.Path, credential_source: str):
    completed, _ = _run_wrapper(tmp_path, credential_source, FAKE_REGISTER_MODE="transport")
    assert completed.returncode != 0
    assert completed.stderr == (
        "agentstack-reregister: stage=register_agent reason=transport-failed "
        f"credential_source={credential_source} curl_exit=7\n"
    )
    _assert_no_secrets(completed)


@pytest.mark.parametrize("curl_exit", ["7", "28"])
def test_transport_failure_reports_only_the_numeric_curl_exit(tmp_path: pathlib.Path, curl_exit: str):
    completed, _ = _run_wrapper(
        tmp_path,
        FAKE_REGISTER_MODE="transport",
        FAKE_CURL_EXIT=curl_exit,
    )
    assert completed.returncode != 0
    assert completed.stderr == (
        "agentstack-reregister: stage=register_agent reason=transport-failed "
        f"credential_source=runtime-file curl_exit={curl_exit}\n"
    )
    _assert_no_secrets(completed)


@pytest.mark.parametrize("status", ["401", "403", "503"])
def test_http_rejection_reports_status_without_guessing_token_mismatch(tmp_path: pathlib.Path, status: str):
    completed, _ = _run_wrapper(
        tmp_path,
        FAKE_REGISTER_MODE="http",
        FAKE_HTTP_STATUS=status,
    )
    assert completed.returncode != 0
    assert completed.stderr == (
        "agentstack-reregister: stage=register_agent reason=http-rejected "
        f"credential_source=runtime-file http_status={status}\n"
    )
    assert "mismatch" not in completed.stderr
    _assert_no_secrets(completed)


@pytest.mark.parametrize(
    ("mode", "reason", "extra"),
    [
        ("transport", "transport-failed", " curl_exit=7"),
        ("http", "http-rejected", " http_status=503"),
        ("rpc", "rpc-error", " rpc_code=-32001"),
        ("tool", "tool-error", ""),
        ("legacy-tool", "tool-error", ""),
        ("malformed", "invalid-response", ""),
        ("null", "invalid-response", ""),
        ("array", "invalid-response", ""),
        ("unexpected-result", "invalid-response", ""),
        ("empty-plain", "invalid-response", ""),
        ("empty-result", "invalid-response", ""),
        ("unrelated-text", "invalid-response", ""),
    ],
)
def test_ensure_project_failures_stop_before_register_agent(
    tmp_path: pathlib.Path, mode: str, reason: str, extra: str
):
    updates = {"FAKE_ENSURE_MODE": mode}
    if mode == "http":
        updates["FAKE_HTTP_STATUS"] = "503"
    completed, calls = _run_wrapper(tmp_path, **updates)
    assert completed.returncode != 0
    assert completed.stderr == (
        f"agentstack-reregister: stage=ensure_project reason={reason} "
        f"credential_source=runtime-file{extra}\n"
    )
    assert [call["tool"] for call in calls] == ["whois", "ensure_project"]
    _assert_no_secrets(completed)


@pytest.mark.parametrize(
    ("mode", "stage", "reason", "extra"),
    [
        ("rpc", "register_agent", "rpc-error", " rpc_code=-32001"),
        ("rpc-nonnumeric", "register_agent", "rpc-error", ""),
        ("tool", "register_agent", "tool-error", ""),
        ("legacy-tool", "register_agent", "tool-error", ""),
        ("malformed", "register_agent", "invalid-response", ""),
        ("null", "register_agent", "invalid-response", ""),
        ("array", "register_agent", "invalid-response", ""),
        ("unexpected-result", "register_agent", "invalid-response", ""),
        ("missing-name", "response-parse", "invalid-response", ""),
    ],
)
def test_register_failures_are_classified_without_server_text(
    tmp_path: pathlib.Path, mode: str, stage: str, reason: str, extra: str
):
    completed, calls = _run_wrapper(tmp_path, FAKE_REGISTER_MODE=mode)
    assert completed.returncode != 0
    assert completed.stderr == (
        f"agentstack-reregister: stage={stage} reason={reason} "
        f"credential_source=runtime-file{extra}\n"
    )
    assert [call["tool"] for call in calls] == ["whois", "ensure_project", "register_agent"]
    _assert_no_secrets(completed)


def test_reserved_identity_substitution_still_fails_without_returned_name(tmp_path: pathlib.Path):
    completed, _ = _run_wrapper(tmp_path, FAKE_REGISTER_MODE="identity")
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == (
        "agentstack-reregister: stage=identity-check reason=identity-changed "
        "credential_source=runtime-file\n"
    )
    _assert_no_secrets(completed)


def test_shared_library_preserves_return_two_and_resets_diagnostics(tmp_path: pathlib.Path):
    env, _ = _fixture_env(tmp_path, "inherited")
    script = f'''
set -euo pipefail
. {REGISTER_LIB!s}
export FAKE_REGISTER_MODE=identity
set +e
ags_register_session "$AGENTSTACK_PROJECT_KEY" codex fixture-model cx "$AGENTSTACK_PROJECT_WORK_DIR" {AGENT_NAME} reserved >/dev/null
first_status=$?
set -e
printf 'first=%s:%s:%s:%s\n' "$first_status" "$AGS_AGENT_NAME_SUBSTITUTED" "$AGS_REGISTRATION_DIAG_STAGE" "$AGS_REGISTRATION_DIAG_REASON"
export FAKE_REGISTER_MODE=success
ags_register_session "$AGENTSTACK_PROJECT_KEY" codex fixture-model cx "$AGENTSTACK_PROJECT_WORK_DIR" {AGENT_NAME} reserved >/dev/null
printf 'second=%s:%s:%s\n' "$AGS_REGISTERED_AGENT_NAME" "${{AGS_REGISTRATION_DIAG_STAGE:-}}" "${{AGS_REGISTRATION_DIAG_REASON:-}}"
'''
    completed = subprocess.run(
        [BASH, "-c", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (
        "first=2:1:identity-check:identity-changed\n"
        f"second={AGENT_NAME}::\n"
    )
    assert completed.stderr == ""
    _assert_no_secrets(completed)
