"""Part 1 product-core checks. HTTP consumer is a TEST harness, NOT a native CLI.

Existing native/caller red specifications remain untouched. These tests inspect
actual generated TOML/settings and execute the real bounded auth runner, but do
not establish native CLI header choice, TTL scheduling or permission semantics.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import ssl
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request

import pytest

from .evidence import credential_source, write_private_json, assert_wire_rotation
from .support import Canary, MODEL, assert_roundtrip, fake_peer, profile_text
from .test_auth_contract import plan as auth_plan
from .test_instruments import request


def configured_auth(workspace, api, provider, command):
    # Test-only explicit deployment, never installed configure or a real secret.
    import local_model_auth
    workspace.runner.chmod(0o600)
    workspace.runner.write_bytes(b"#!" + os.fsencode(sys.executable) + b" -I\n" + Path(local_model_auth.__file__).read_bytes())
    workspace.runner.chmod(0o500)
    workspace.context["auth_runner"]["sha256"] = hashlib.sha256(workspace.runner.read_bytes()).hexdigest()
    helper = Path(command[-1])
    workspace.context["auth_runner"]["dependency_digests"][str(helper)] = hashlib.sha256(helper.read_bytes()).hexdigest()
    text = workspace.profile.read_text()
    marker = 'wire_api = "' + ("responses" if provider == "codex" else "anthropic-messages") + '"'
    workspace.profile.write_text(text.replace(marker, marker + "\nauth_command = " + json.dumps(command)))
    workspace.caller["provider"] = provider
    # Quoting must survive both an apostrophe and a space in the owner path.
    owner = workspace.tmp / "client 'quoted'" / "profile.json"
    workspace.context["intent_owner"] = str(owner)
    intent = api.resolve_launch_intent(workspace.caller, workspace.context)
    write_private_json(owner, {"launch_intent": intent})
    return intent, owner


def helper_command(projection, provider):
    if provider == "codex":
        cfg = projection["provider_config"]
        auth = cfg["model_providers"][cfg["model_provider"]]["auth"]
        assert auth["timeout_ms"] == 5000 and auth["refresh_interval_ms"] == 300000
        return [auth["command"], *auth["args"]]
    return shlex.split(projection["provider_config"]["settings"]["apiKeyHelper"])


def assert_auth_rejected(result, canary):
    # Scan before asserting: a broken runner must not cause pytest to render a
    # CompletedProcess repr containing its credential-bearing stdout/stderr.
    canary.scan({"runner stdout": result.stdout, "runner stderr": result.stderr})
    if result.returncode != 1 or result.stdout != b"" or result.stderr != b"LOCAL_MODEL_AUTH_FAILED\n":
        pytest.fail("runner did not fail with the fixed redacted reason", pytrace=False)


def post_projected(peer, projection, provider, body, token=None, header=None):
    # Deliberately a small independent fixture consumer. No product HTTP client
    # or protocol-conversion gateway is introduced by part 1.
    url = projection["base_url"].rstrip("/") + ("/responses" if provider == "codex" else "/v1/messages")
    headers = {"Content-Type": "application/json", "Anthropic-Version": "2023-06-01"}
    if token is not None:
        headers["Authorization" if header == "bearer" else "X-Api-Key"] = (
            "Bearer " if header == "bearer" else "") + token.decode()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if peer.ca_file:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=peer.ca_file)))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with opener.open(req, timeout=2) as response:
        return json.loads(response.read())


@pytest.mark.parametrize("provider,header", [("codex", "bearer"), ("claude", "bearer"), ("claude", "x-api-key")])
def test_generated_config_and_real_runner_reach_verified_peer_and_rotate(launch_intent, workspace, canary, provider, header):
    protocol = "responses" if provider == "codex" else "messages"
    with credential_source(canary) as source, fake_peer(protocol, canary, auth=header,
                                                       tls_dir=workspace.tmp / "tls") as peer:
        helper = source.helper_argv(workspace.root / "credential.py")
        workspace.profile.write_text(profile_text(peer.base_url + "/v1").replace(
            "https://inference.example.invalid", peer.base_url))
        intent, owner = configured_auth(workspace, launch_intent, provider, helper)
        before = owner.read_bytes()
        projection = launch_intent.materialize_launch_intent(intent, workspace.context, operation="spawn")
        command = helper_command(projection, provider)
        assert projection["requires_native_validation"] is True
        shadow = workspace.tmp / "shadow-modules"
        shadow.mkdir(mode=0o700)
        sentinel = workspace.tmp / "unapproved-import"
        (shadow / "json.py").write_text(
            "from pathlib import Path\n" + f"Path({str(sentinel)!r}).write_text('executed')\n"
            "raise SystemExit('unapproved Python import')\n")
        rotated = Canary()
        for turn, current in ((1, canary), (2, rotated)):
            if turn == 2:
                source.rotate(current)
                peer.rotate_credential(current)
            result = subprocess.run(command, env={"PATH": "/caller/not-used", "HOME": "/caller/not-used",
                                                  "PYTHONPATH": str(shadow)},
                                    cwd=workspace.scratch.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=8)
            if result.returncode != 0 or not current.matches(result.stdout.decode(), "x-api-key"):
                pytest.fail("approved runner did not return credential", pytrace=False)
            current.scan({"helper-stderr": result.stderr, "argv": command, "intent": owner.read_text(),
                          "projection": projection})
            body = request(protocol, turn)
            body.update(model=projection["model"], stream=False)
            response = post_projected(peer, projection, provider, body, result.stdout, header)
            assert response["model"] == MODEL
        assert owner.read_bytes() == before
        assert not sentinel.exists(), "runner imported a caller-selected Python module"
        assert_roundtrip(peer)
        assert_wire_rotation(peer, source)


@pytest.mark.parametrize("authenticated", [False, True])
def test_codex_all_overrides_are_valid_toml_not_json_inline_objects(launch_intent, workspace, authenticated):
    if authenticated:
        script = workspace.root / "never-executed.py"
        script.write_text("# This configuration-only case never obtains a credential.\n")
        intent, _ = configured_auth(workspace, launch_intent, "codex", [sys.executable, str(script)])
    else:
        intent = launch_intent.resolve_launch_intent(workspace.caller, workspace.context)
    result = launch_intent.materialize_launch_intent(intent, workspace.context, operation="spawn")
    argv = result["execution"]["argv"]
    parsed = tomllib.loads("\n".join(argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"))
    expected = deepcopy(result["provider_config"])
    expected.pop("wire_api")  # read projection, never sent as a top-level key
    assert parsed == expected


@pytest.mark.parametrize("case", ["fifo", "ancestor-symlink", "oversize", "huge-integer", "authenticated-http"])
def test_profile_additional_safety_boundaries(profiles, workspace, case):
    if case == "fifo":
        workspace.profile.unlink()
        os.mkfifo(workspace.profile, 0o600)
    elif case == "ancestor-symlink":
        actual = workspace.root.parent.with_name("actual-stack")
        workspace.root.parent.rename(actual)
        workspace.root.parent.symlink_to(actual, target_is_directory=True)
    elif case == "oversize":
        workspace.profile.write_text("#" + "x" * 65536 + "\n" + profile_text())
    elif case == "huge-integer":
        workspace.profile.write_text(profile_text().replace("context_window = 131072", "context_window = 9223372036854775808"))
    else:
        workspace.profile.write_text(profile_text("http://127.0.0.1:1/v1").replace(
            'wire_api = "responses"', 'wire_api = "responses"\nauth_command = ["/fixture/helper"]'))
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        profiles.load_profile(workspace.root, "local:fixture", expected_uid=os.getuid())


@pytest.mark.parametrize("pct", ["0", "101", "-1", "NaN", "85.0"])
def test_compact_percentage_is_bounded_integer_string(profiles, workspace, pct):
    workspace.profile.write_text(profile_text().replace('"85"', json.dumps(pct)))
    with pytest.raises(profiles.LocalModelError, match="LOCAL_MODEL_PROFILE_INVALID"):
        profiles.load_profile(workspace.root, "local:fixture", expected_uid=os.getuid())


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("field", ["model", "context_window", "max_output_tokens", "adapter"])
def test_rehashing_a_corrupt_projection_does_not_override_profile(launch_intent, workspace, provider, field):
    workspace.caller["provider"] = provider
    intent = launch_intent.resolve_launch_intent(workspace.caller, workspace.context)
    resolved = intent["model"]["resolved"]
    if field == "adapter":
        resolved[field]["base_url"] = "https://different.invalid"
    else:
        resolved[field] = "different" if field == "model" else resolved[field] + 1
    intent["intent_digest"] = launch_intent.intent_digest(intent)
    with pytest.raises(launch_intent.LocalModelError, match="LOCAL_MODEL_INTENT_CHANGED"):
        launch_intent.materialize_launch_intent(intent, workspace.context, operation="resume")


@pytest.mark.parametrize("fault", ["mode", "symlink", "hardlink", "parent-mode", "bytes", "owner", "digest", "unisolated"])
def test_runner_cli_rejects_drift_before_credential_command(launch_intent, workspace, canary, fault):
    with credential_source(canary) as source:
        helper = source.helper_argv(workspace.root / "credential.py")
        intent, owner = configured_auth(workspace, launch_intent, "codex", helper)
        command = helper_command(launch_intent.materialize_launch_intent(
            intent, workspace.context, operation="spawn"), "codex")
        if fault == "mode":
            workspace.runner.chmod(0o700)
        elif fault == "parent-mode":
            workspace.runner.parent.chmod(0o755)
        elif fault in ("symlink", "hardlink"):
            actual = workspace.runner.with_name("actual-runner")
            if fault == "symlink":
                workspace.runner.rename(actual)
                workspace.runner.symlink_to(actual)
            else:
                os.link(workspace.runner, actual)
        elif fault == "bytes":
            workspace.dependency.write_text("changed dependency\n")
        elif fault == "owner":
            owner.chmod(0o644)
        elif fault == "digest":
            command[command.index("--digest") + 1] = "0" * 64
        else:
            command = [sys.executable, *command]  # bypass the approved -I shebang
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(workspace.tmp / "home")})
        assert_auth_rejected(result, canary)
        assert source.issued == []


@pytest.mark.parametrize("body", ["import sys; sys.stdout.buffer.write(b'x' * 1000000)",
                                 "import time; time.sleep(20)"])
def test_real_runner_bounds_output_and_time_without_retaining_stderr(auth_runner, workspace, body):
    script = workspace.root / "bounded-command.py"
    script.write_text(body)
    current = auth_plan(workspace)
    current["argv"] = [sys.executable, str(script)]
    started = time.monotonic()
    with pytest.raises(auth_runner.LocalModelError, match="LOCAL_MODEL_AUTH_FAILED"):
        auth_runner.run_auth(current)
    assert time.monotonic() - started < 7


@pytest.mark.parametrize("field", ["timeout_ms", "stdout_limit_bytes", "refresh_interval_ms"])
def test_auth_fixed_limits_are_integers_not_equal_floats(auth_runner, workspace, field):
    current = auth_plan(workspace)
    current[field] = float(current[field])
    with pytest.raises(auth_runner.LocalModelError, match="LOCAL_MODEL_AUTH_FAILED"):
        auth_runner.run_auth(current, executor=lambda *a, **kw: pytest.fail("invalid plan executed command"))


def test_runner_boolean_contract_version_is_not_version_one(auth_runner, workspace):
    current = auth_plan(workspace)
    current["runner_contract_version"] = True
    with pytest.raises(auth_runner.LocalModelError, match="LOCAL_MODEL_AUTH_FAILED"):
        auth_runner.validate_runner(current, observed_digest=current["runner_digest"], observed_contract_version=1)


@pytest.mark.parametrize("change", ["profile", "settings", "helper"])
def test_runner_rechecks_approved_inputs_at_each_refresh(launch_intent, workspace, canary, change):
    with credential_source(canary) as source:
        helper = source.helper_argv(workspace.root / "credential.py")
        intent, owner = configured_auth(workspace, launch_intent, "claude", helper)
        command = helper_command(launch_intent.materialize_launch_intent(
            intent, workspace.context, operation="spawn"), "claude")
        target = {"profile": workspace.profile, "settings": workspace.settings,
                  "helper": Path(helper[-1])}[change]
        target.chmod(0o600)
        target.write_text("changed\n")
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
        assert_auth_rejected(result, canary)
        assert source.issued == []
