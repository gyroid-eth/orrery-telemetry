"""Design v3 §4: one fixed auth context; never use token-valued env."""
from copy import deepcopy
import subprocess
import sys

import pytest

from .support import assert_auth_context


class CredentialCommand:
    """In-memory substitute for a credential executable, never for either CLI.

    No Keychain command, credential file or real secret is read. A real runner
    must still call its executor with the approved absolute argv/cwd/minimal env.
    """
    def __init__(self, canary, fault=None):
        self.canary = canary
        self.fault = fault
        self.calls = []

    def __repr__(self):
        return "<CredentialCommand redacted>"

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, **kw})
        raw = self.canary.header("x-api-key").encode()
        if self.fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 5, output=raw, stderr=raw)
        if self.fault == "exit":
            return subprocess.CompletedProcess(argv, 1, stdout=raw, stderr=raw)
        stdout = {"empty": b"", "multiline": b"one\ntwo\n", "control": b"bad\x00value",
                  "extra-newline": raw + b"\n\n", "no-newline": raw,
                  "boundary-no-newline": b"x" * 8192, "boundary-with-newline": b"x" * 8191 + b"\n",
                  "oversize": b"x" * 8193, "invalid-utf8": b"\xff"}.get(self.fault, raw + b"\n")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=raw)


def plan(workspace):
    return {**deepcopy(workspace.context["auth_context"]),
            "argv": ["/fixture/approved-credential-command", "service", "fixture-service"],
            "runner_contract_version": 1, "runner_digest": "fixture-approved-digest",
            "dependency_digests": {}, "profile_sha256": "fixture-profile-digest"}


def drain_logs(capsys, caplog):
    # Drain before asserting: pytest must not echo a deliberately faulty
    # implementation's credential-bearing captured stdout/log on failure.
    captured = capsys.readouterr()
    logs = caplog.text
    caplog.clear()
    return {"auth-stdout": captured.out, "auth-stderr": captured.err, "auth-log": logs}


def test_auth_fixed_cwd_minimal_env_and_no_shell(auth_runner, workspace, canary, monkeypatch, capsys, caplog):
    for key, value in {"HOME": "/caller/home", "PATH": "/caller/bin", "TMUX": "caller-socket", "ANTHROPIC_AUTH_TOKEN": "inherited"}.items():
        monkeypatch.setenv(key, value)
    command = CredentialCommand(canary)
    token = auth_runner.run_auth(plan(workspace), executor=command)
    canary.scan(drain_logs(capsys, caplog))
    if not canary.matches(token.decode().rstrip("\n"), "x-api-key"):
        pytest.fail("credential not passed through validated pipe", pytrace=False)
    assert len(command.calls) == 1
    call = command.calls[0]
    assert_auth_context(call, plan(workspace))
    canary.scan({"executor-input": call})


@pytest.mark.parametrize("fault", ["timeout", "exit", "empty", "multiline", "control", "oversize", "invalid-utf8", "extra-newline"])
def test_auth_failure_is_fixed_reason_not_secret_stderr(auth_runner, workspace, canary, fault, capsys, caplog):
    with pytest.raises(auth_runner.LocalModelError) as caught:
        auth_runner.run_auth(plan(workspace), executor=CredentialCommand(canary, fault))
    canary.scan(drain_logs(capsys, caplog))
    if str(caught.value) != "LOCAL_MODEL_AUTH_FAILED":
        pytest.fail("auth failure reason is not fixed/redacted", pytrace=False)
    canary.scan({"auth-error": str(caught.value)})


def test_rotated_token_is_not_an_intent_revision(auth_runner, workspace, canary):
    from .support import Canary, canonical_digest
    auth_plan = plan(workspace)
    before = canonical_digest(auth_plan)
    for current in (canary, Canary()):
        token = auth_runner.run_auth(auth_plan, executor=CredentialCommand(current))
        if not current.matches(token.decode().rstrip("\n"), "x-api-key"):
            pytest.fail("rotated credential not returned", pytrace=False)
    assert canonical_digest(auth_plan) == before


def test_runner_drift_does_not_execute_or_redeploy(auth_runner, workspace, monkeypatch):
    before = workspace.runner.read_bytes()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("runner drift executed a process"))
    with pytest.raises(auth_runner.LocalModelError):
        auth_runner.validate_runner(plan(workspace), observed_digest="changed", observed_contract_version=2)
    assert workspace.runner.read_bytes() == before


@pytest.mark.parametrize("form", ["no-newline", "boundary-no-newline", "boundary-with-newline"])
def test_auth_stdout_limit_and_ending_positive_boundaries(auth_runner, workspace, canary, form, capsys, caplog):
    token = auth_runner.run_auth(plan(workspace), executor=CredentialCommand(canary, form))
    canary.scan(drain_logs(capsys, caplog))
    expected_size = 8192 if form == "boundary-no-newline" else 8191
    if form == "no-newline":
        if not canary.matches(token.decode(), "x-api-key"):
            pytest.fail("zero-newline credential mismatch", pytrace=False)
    else:
        assert len(token.rstrip(b"\n")) == expected_size


def test_real_fake_credential_process_oversize_is_rejected(auth_runner, workspace, capsys, caplog):
    script = workspace.root / "oversize-credential.py"
    script.write_text('import sys\nsys.stdout.buffer.write(b"x" * 8193)\n')
    current = plan(workspace)
    current["argv"] = [sys.executable, str(script)]
    with pytest.raises(auth_runner.LocalModelError, match="LOCAL_MODEL_AUTH_FAILED"):
        auth_runner.run_auth(current)
    drain_logs(capsys, caplog)
