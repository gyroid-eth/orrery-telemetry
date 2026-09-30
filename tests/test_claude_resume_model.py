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


@pytest.mark.parametrize("is_child", [False, True])
def test_resume_session_start_keeps_the_registered_model(resume, mail, tmp_path, is_child):
    """#144: the resumed session's SessionStart re-registration wrote `claude-code`."""
    runtime, registration, launches, calls = resume
    if is_child:
        child(runtime, registration)
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    payload = json.dumps({"session_id": "sess-resume-1", "hook_event_name": "SessionStart",
                          "cwd": str(tmp_path)})
    process = run_resumed(launches, tmp_path,
                          f"printf '%s' {json.dumps(payload)!r} | "
                          f"AGENTSTACK_REGISTER_LIB={ROOT / 'bin/lib/agentstack-register.sh'} "
                          f"/bin/bash {ROOT / 'hooks/session-start-reminder.sh'} >/dev/null\n")
    assert process.returncode == 0, process.stderr
    registers = [arguments for method, arguments in mail if method == "register_agent"]
    assert registers, mail
    assert {arguments["model"] for arguments in registers} == {"fixture-model"}, registers


def test_resume_with_no_registered_model_keeps_the_program_label(resume, mail, tmp_path):
    runtime, registration, launches, calls = resume
    registration["model"] = ""
    result = server.do_resume(NAME, open_terminal=False)
    assert result["ok"], result
    assert "CLAUDE_CHILD_MODEL=" not in launches[-1][-1]
