"""Resume does not rewrite the registered Claude model (#144).

The resumed session's SessionStart hook re-registers from the shell. Without
the registered model in its environment it registered the program name (or
whatever model the tmux server's environment carried) over the original.
"""
from __future__ import annotations

import json

import pytest

from dashboard import server
from claude_resume_harness import mail, run_resumed
from test_claude_resume_mail import NAME, ROOT, child, resume


def session_start_models(launches, tmp_path, mail):
    payload = json.dumps({"session_id": "sess-resume-1", "hook_event_name": "SessionStart",
                          "cwd": str(tmp_path)})
    process = run_resumed(launches, tmp_path,
                          f"printf '%s' {json.dumps(payload)!r} | "
                          f"AGENTSTACK_REGISTER_LIB={ROOT / 'bin/lib/agentstack-register.sh'} "
                          f"/bin/bash {ROOT / 'hooks/session-start-reminder.sh'} >/dev/null\n")
    assert process.returncode == 0, process.stderr
    registers = [arguments for method, arguments in mail if method == "register_agent"]
    assert registers, mail
    return {arguments["model"] for arguments in registers}


@pytest.mark.parametrize("is_child", [False, True])
def test_resume_session_start_keeps_the_registered_model(resume, mail, tmp_path, is_child):
    """#144: the resumed session's SessionStart re-registration wrote `claude-code`."""
    runtime, registration, launches, calls = resume
    if is_child:
        child(runtime, registration)
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    assert session_start_models(launches, tmp_path, mail) == {"fixture-model"}


def test_resume_with_no_registered_model_does_not_take_an_ambient_one(resume, mail, tmp_path):
    runtime, registration, launches, calls = resume
    registration["model"] = ""
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    assert session_start_models(launches, tmp_path, mail) == {"claude-code"}


def test_resumed_session_does_not_hand_its_model_to_later_launchers(resume):
    """`agentstack-preregister-child` / `agent-start` default to AGENTSTACK_CLAUDE_MODEL."""
    runtime, registration, launches, calls = resume
    assert server.do_resume(NAME, open_terminal=False)["ok"]
    assert "AGENTSTACK_CLAUDE_MODEL" not in launches[-1][-1]


# --- a session reopened from a terminal (`claude --resume`) ----------------
# The dashboard resume hands the registered model to the session (#144), but a
# terminal has neither CLAUDE_CHILD_MODEL nor AGENTSTACK_CLAUDE_MODEL, and the
# SessionStart re-registration wrote `claude-code` over the model (RUNTHROUGH
# 2026-10-01, problem 2).
import os  # noqa: E402
import subprocess  # noqa: E402

from claude_resume_harness import StandInMail  # noqa: E402

HAIKU = "claude-haiku-4-5-20251001"


def terminal_session_start(tmp_path, mail_url, *, payload_model=None, env=None):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    token = runtime / f"agent_token_{NAME}"
    token.write_text("fixture-owner-token", encoding="utf-8")
    token.chmod(0o600)
    payload = {"session_id": "sess-terminal-1", "hook_event_name": "SessionStart",
               "source": "resume", "cwd": str(tmp_path)}
    if payload_model is not None:
        payload["model"] = payload_model
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AGENTSTACK_", "CLAUDE_CHILD_")) and key not in ("TMUX", "TMUX_PANE")
    }
    environment.update({
        "HOME": str(tmp_path / "home"), "AGENT_NAME": NAME,
        "AGENTSTACK_RUNTIME_DIR": str(runtime), "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
        "AGENTSTACK_REGISTER_LIB": str(ROOT / "bin/lib/agentstack-register.sh"),
        "AGENTSTACK_PROJECT_KEY": str(tmp_path), "PROJECT_KEY": str(tmp_path),
        "AGENTSTACK_MCP_URL": mail_url, "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "disabled",
        **(env or {}),
    })
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(["/bin/bash", str(ROOT / "hooks/session-start-reminder.sh")],
                          input=json.dumps(payload), cwd=tmp_path, env=environment,
                          capture_output=True, text=True, timeout=60)


def registered_models(mail):
    return [arguments["model"] for method, arguments in mail if method == "register_agent"]


@pytest.fixture
def mail_url(mail):
    return os.environ["AGENTSTACK_MCP_URL"]


def test_terminal_resume_keeps_the_model_claude_code_reports(mail, mail_url, tmp_path):
    process = terminal_session_start(tmp_path, mail_url, payload_model=HAIKU)
    assert process.returncode == 0, process.stderr
    assert registered_models(mail) == [HAIKU]


def test_terminal_resume_keeps_the_registered_model_when_claude_code_does_not_say(
        mail, mail_url, tmp_path):
    StandInMail.registered_model = HAIKU
    process = terminal_session_start(tmp_path, mail_url)
    assert process.returncode == 0, process.stderr
    assert registered_models(mail) == [HAIKU]


@pytest.mark.parametrize("payload_model", [None, "", "not a model; rm -rf /", 42])
def test_terminal_resume_without_any_model_still_registers_claude_code(
        mail, mail_url, tmp_path, payload_model):
    process = terminal_session_start(tmp_path, mail_url, payload_model=payload_model)
    assert process.returncode == 0, process.stderr
    assert registered_models(mail) == ["claude-code"]


def test_the_launchers_model_still_wins(mail, mail_url, tmp_path):
    StandInMail.registered_model = "registered-model"
    process = terminal_session_start(tmp_path, mail_url, payload_model=HAIKU,
                                     env={"CLAUDE_CHILD_MODEL": "claude-opus-5-5"})
    assert process.returncode == 0, process.stderr
    assert registered_models(mail) == ["claude-opus-5-5"]


BEDROCK_ARN = ("arn:aws:bedrock:us-east-1:123456789012:"
               "application-inference-profile/sonnet-prod")
VERTEX_ID = "claude-sonnet-4-5@20250929"


@pytest.mark.parametrize("model", [BEDROCK_ARN, VERTEX_ID, "claude-opus-5-5[1m]"])
def test_terminal_resume_keeps_provider_model_ids(mail, mail_url, tmp_path, model):
    """Bedrock ARNs and Vertex ids are model ids too (code.claude.com model-config)."""
    process = terminal_session_start(tmp_path, mail_url, payload_model=model)
    assert process.returncode == 0, process.stderr
    assert registered_models(mail) == [model]


def test_terminal_resume_keeps_a_registered_arn(mail, mail_url, tmp_path):
    StandInMail.registered_model = BEDROCK_ARN
    process = terminal_session_start(tmp_path, mail_url)
    assert process.returncode == 0, process.stderr
    assert registered_models(mail) == [BEDROCK_ARN]


@pytest.mark.parametrize("model", ["a b", "a;b", "a$(id)", "a`id`", "a'b", 'a"b', "a\nb",
                                   "/leading-slash", "x" * 300])
def test_terminal_resume_refuses_model_values_that_are_not_ids(mail, mail_url, tmp_path, model):
    process = terminal_session_start(tmp_path, mail_url, payload_model=model)
    assert process.returncode == 0, process.stderr
    assert registered_models(mail) == ["claude-code"]
