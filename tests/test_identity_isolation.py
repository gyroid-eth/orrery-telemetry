#!/usr/bin/env python3
"""Regression tests for tmux-scoped agent identity and owner-token isolation.

Runnable two ways (no third-party dependency required):
    python3 tests/test_identity_isolation.py
    pytest tests/test_identity_isolation.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def _run_bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=_ROOT,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def test_top_level_launchers_override_tmux_identity_environment():
    claude = _read("bin/agent-start")
    codex = _read("bin/agent-start-codex")

    assert '-e $(printf \'%q\' "AGENT_NAME=$SESSION_AGENT_NAME")' in claude
    for name in ("PARENT_AGENT", "CHILD_REGISTRATION_TOKEN"):
        assert f"-e {name}=" in claude, name
    for name in (
        "AGENT_NAME",
        "PARENT_AGENT",
        "CHILD_REGISTRATION_TOKEN",
        "AGENTSTACK_CODEX_LAUNCH_BINDING",
        "AGENTSTACK_CODEX_LAUNCH_ID",
    ):
        assert f"-e {name}=" in codex, name
    assert "-e AGENTSTACK_RESERVED_IDENTITY=" in claude
    assert "-e AGENTSTACK_RESERVED_IDENTITY=" in codex

    # The owner token must be restored from its protected runtime file, not
    # expanded into the generated launcher or tmux command line.
    assert "SESSION_TOKEN_FILE" in claude
    assert 'export CHILD_REGISTRATION_TOKEN=$(printf' not in claude
    assert '-e "CHILD_REGISTRATION_TOKEN=$' not in claude


def test_bootstrap_ignores_unmarked_stale_identity_and_preserves_marked_reserved_identity():
    bootstrap = _ROOT / "bin" / "agentstack-codex-bootstrap"
    common = {
        "AGENTSTACK_PROJECT_KEY": "",
        "AGENTSTACK_MANAGED_AGENTS_FILE": "",
        "AGENTSTACK_RESERVED_IDENTITY": "",
        "AGENT_NAME": "Stale-Dirac",
        "PARENT_AGENT": "Stale-Parent",
        "CHILD_REGISTRATION_TOKEN": "stale-owner-token",
        "AGENTSTACK_CODEX_LAUNCH_BINDING": "/parent/codex_launches/1.json",
        "AGENTSTACK_CODEX_LAUNCH_ID": "parent-launch-id",
        "TMUX": "",
    }
    command = (
        f'source "{bootstrap}" . >/dev/null 2>&1; '
        "printf '%s|%s|%s|%s|%s\\n' \"${AGENT_NAME:-}\" "
        "\"${PARENT_AGENT:-}\" \"${CHILD_REGISTRATION_TOKEN:-}\" "
        "\"${AGENTSTACK_CODEX_LAUNCH_BINDING:-}\" "
        "\"${AGENTSTACK_CODEX_LAUNCH_ID:-}\""
    )
    top_level = _run_bash(command, common).stdout.strip().split("|")
    assert top_level[0] and top_level[0] != "Stale-Dirac", top_level
    assert top_level[1:] == ["", "", "", ""], top_level

    reserved_env = dict(common)
    reserved_env["AGENTSTACK_RESERVED_IDENTITY"] = "1"
    reserved = _run_bash(command, reserved_env).stdout.strip().split("|")
    assert reserved == [
        "Stale-Dirac", "Stale-Parent", "stale-owner-token", "", ""
    ], reserved


def test_candidate_registration_rejects_ambient_owner_token():
    register_lib = _ROOT / "bin" / "lib" / "agentstack-register.sh"
    with tempfile.TemporaryDirectory() as tmp:
        capture = pathlib.Path(tmp) / "register-args"
        script = f'''
source "{register_lib}"
ags_mcp_call() {{
  local tool="$1"; shift
  if [[ "$tool" == "register_agent" ]]; then
    printf '%s\\n' "$@" > "$CAPTURE"
    printf '%s\\n' '{{"result":{{"structuredContent":{{"name":"Fresh-Dirac","registration_token":"server-token"}}}}}}'
  else
    printf '%s\\n' '{{"result":{{"structuredContent":{{"id":1}}}}}}'
  fi
}}
ags_agent_exists() {{ return 1; }}
ags_generate_registration_token() {{ printf '%s\\n' fresh-owner-token; }}
ags_store_registration_token() {{ return 0; }}
ags_apply_contact_policy() {{ return 0; }}
CHILD_REGISTRATION_TOKEN=stale-owner-token
export CHILD_REGISTRATION_TOKEN CAPTURE
ags_register_session /project codex model cx /work Fresh-Dirac candidate >/dev/null
'''
        _run_bash(script, {"CAPTURE": str(capture)})
        args = capture.read_text(encoding="utf-8")
        assert "registration_token=fresh-owner-token" in args, args
        assert "stale-owner-token" not in args, args


def test_registration_adopts_the_server_returned_name_on_every_call():
    """Local ORRERY Mail removes hyphens, so response name is the identity."""
    register_lib = _ROOT / "bin" / "lib" / "agentstack-register.sh"
    script = f'''
source "{register_lib}"
ags_mcp_call() {{
  local tool="$1"; shift
  if [[ "$tool" == "register_agent" ]]; then
    printf '%s\\n' '{{"result":{{"structuredContent":{{"name":"FrostyPasteur","registration_token":"stable-owner-token"}}}}}}'
  else
    printf '%s\\n' '{{"result":{{"structuredContent":{{"id":1}}}}}}'
  fi
}}
ags_generate_registration_token() {{ printf '%s\\n' requested-owner-token; }}
ags_store_registration_token() {{ printf '%s|%s\\n' "$1" "$2"; }}
ags_apply_contact_policy() {{ :; }}
for _ in 1 2; do
  ags_register_session /project codex model cx /work Frosty-Pasteur candidate >/dev/null
  printf 'registered=%s token=%s substituted=%s requested=%s returned=%s\\n' \
    "$AGS_REGISTERED_AGENT_NAME" "$AGS_REGISTERED_REGISTRATION_TOKEN" \
    "$AGS_AGENT_NAME_SUBSTITUTED" "$AGS_REQUESTED_AGENT_NAME" \
    "$AGS_SERVER_RETURNED_AGENT_NAME"
done
'''
    result = _run_bash(script)
    assert result.stdout.splitlines() == [
        "registered=FrostyPasteur token=stable-owner-token substituted=1 "
        "requested=Frosty-Pasteur returned=FrostyPasteur",
        "registered=FrostyPasteur token=stable-owner-token substituted=1 "
        "requested=Frosty-Pasteur returned=FrostyPasteur",
    ]


def test_reserved_identity_refuses_a_server_substitution():
    """A child/resume already has inbox and tmux state under its requested name."""
    register_lib = _ROOT / "bin" / "lib" / "agentstack-register.sh"
    script = f'''
source "{register_lib}"
ags_mcp_call() {{
  if [[ "$1" == "register_agent" ]]; then
    printf '%s\\n' '{{"result":{{"structuredContent":{{"name":"OtherAgent","registration_token":"other-token"}}}}}}'
  else
    printf '%s\\n' '{{"result":{{"structuredContent":{{"id":1}}}}}}'
  fi
}}
CHILD_REGISTRATION_TOKEN=reserved-owner-token
export CHILD_REGISTRATION_TOKEN
set +e
ags_register_session /project codex model cx /work Reserved-Curie reserved >/dev/null
status=$?
printf 'status=%s registered=%s substituted=%s requested=%s returned=%s\\n' \
  "$status" "$AGS_REGISTERED_AGENT_NAME" "$AGS_AGENT_NAME_SUBSTITUTED" \
  "$AGS_REQUESTED_AGENT_NAME" "$AGS_SERVER_RETURNED_AGENT_NAME"
'''
    result = _run_bash(script)
    assert result.stdout.strip() == (
        "status=2 registered= substituted=1 requested=Reserved-Curie "
        "returned=OtherAgent"
    )


def _run_root_claude_substitution(*, collision: bool):
    temp = tempfile.TemporaryDirectory()
    tmpdir = pathlib.Path(temp.name)
    bindir = tmpdir / "bin"
    libdir = bindir / "lib"
    libdir.mkdir(parents=True)
    launcher = bindir / "agent-start"
    launcher.write_text(_read("bin/agent-start"), encoding="utf-8")
    launcher.chmod(0o755)
    tmux_log = tmpdir / "tmux.log"
    tmux_state = tmpdir / "tmux.state"
    fake_tmux = tmpdir / "tmux"
    fake_tmux.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{tmux_log}"\n'
        'case "$1" in\n'
        f'  display-message) [[ -f "{tmux_state}" ]] && cat "{tmux_state}" || echo RootBefore ;;\n'
        f'  has-session) exit {0 if collision else 1} ;;\n'
        f'  rename-session) printf "%s\\n" "$2" > "{tmux_state}" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    fake_claude = tmpdir / "claude"
    fake_claude.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    fake_claude.chmod(0o755)
    (libdir / "agentstack-launch.sh").write_text(
        'ags_die() { printf "%s: %s\\n" "$AGS_PROG" "$*" >&2; exit 1; }\n'
        "ags_load_env() { :; }\n"
        'ags_resolve_tmux() { printf "%s\\n" "$FAKE_TMUX"; }\n'
        'ags_choose_dir() { printf "%s\\n" "$1"; }\n',
        encoding="utf-8",
    )
    (libdir / "agentstack-register.sh").write_text(
        'ags_pick_adjective_scientist_name() { printf "Zesty-Einstein\\n"; }\n'
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { :; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_register_session() {\n"
        '  AGS_REGISTERED_AGENT_NAME="MossyEagle"\n'
        '  AGS_REQUESTED_AGENT_NAME="Zesty-Einstein"\n'
        "  AGS_AGENT_NAME_SUBSTITUTED=1\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "TMUX": "/tmp/fake,1,0",
        "FAKE_TMUX": str(fake_tmux),
        "AGENTSTACK_CLAUDE_BIN": str(fake_claude),
        "AGENTSTACK_PROJECT_KEY": "/project",
        "AGENTSTACK_MANAGED_AGENTS_FILE": str(tmpdir / "managed"),
        "AGENTSTACK_HOOKS_DIR": str(tmpdir),
    })
    result = subprocess.run(
        [str(launcher), str(tmpdir)],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    calls = tmux_log.read_text(encoding="utf-8") if tmux_log.exists() else ""
    temp.cleanup()
    return result, calls


def test_root_claude_renames_tmux_to_the_server_returned_identity():
    result, calls = _run_root_claude_substitution(collision=False)
    assert result.returncode == 0, result.stderr
    assert "changed requested identity 'Zesty-Einstein' to 'MossyEagle'" in result.stderr
    assert "rename-session MossyEagle" in calls


def test_root_claude_stops_when_the_returned_tmux_name_is_occupied():
    result, calls = _run_root_claude_substitution(collision=True)
    assert result.returncode != 0
    assert "tmux session already exists" in result.stderr
    assert "rename-session" not in calls


def test_reserved_child_marker_and_rename_failure_are_explicit():
    spawn = _read("hooks/spawn_child.sh")
    bootstrap = _read("bin/agentstack-codex-bootstrap")
    launcher = _read("bin/agent-start-codex")
    assert spawn.count('-e "AGENTSTACK_RESERVED_IDENTITY=1"') >= 2
    assert 'rename-session "$AGENT_NAME" 2>/dev/null || true' not in bootstrap
    assert 'rename-session "$AGENT_NAME" 2>/dev/null || true' not in _read("bin/agent-start")
    assert "refusing an identity split" in _read("bin/agent-start")
    assert "refusing identity registration" in bootstrap
    assert "changed reserved identity" in bootstrap
    assert "agent registration skipped" in bootstrap
    assert "tmux rename-session failed: current session" in bootstrap
    assert '"$TMUX_IDENTITY_MATCHED" == "1"' in bootstrap
    assert 'source $(printf \'%q\' "$BOOTSTRAP") $(printf \'%q\' "$DIR") && $CODEX_CMD' in launcher
    assert "prepare-codex-session-binding.py" in bootstrap
    assert "AGS_REGISTERED_AGENT_ID" in bootstrap


def test_reserved_bootstrap_refuses_to_resume_without_prepare_helper(tmp_path):
    """A registered resume must not start when no fresh generation can persist."""

    bindir = tmp_path / "bin"
    libdir = bindir / "lib"
    libdir.mkdir(parents=True)
    bootstrap = bindir / "agentstack-codex-bootstrap"
    bootstrap.write_text(_read("bin/agentstack-codex-bootstrap"), encoding="utf-8")
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { return 0; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_register_session() {\n"
        "  AGS_REGISTERED_AGENT_NAME=BoundCodex\n"
        "  AGS_REGISTERED_AGENT_ID=73\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    missing_hooks = tmp_path / "missing-hooks"
    script = (
        f'source "{bootstrap}" . >/dev/null 2>"{tmp_path / "stderr"}"; '
        "printf '%s\n' $?"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "AGENTSTACK_PROJECT_KEY": "/project",
            "AGENTSTACK_HOOKS_DIR": str(missing_hooks),
            "AGENTSTACK_RESERVED_IDENTITY": "1",
            "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
            "AGENTSTACK_CODEX_RESUME_SESSION_ID": (
                "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"
            ),
            "AGENTSTACK_CODEX_CHILD_MCP_PROFILE": "inherit",
            "AGENT_NAME": "BoundCodex",
            "TMUX": "",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.stdout.strip() == "1"
    assert "history binding expectation" in (tmp_path / "stderr").read_text(
        encoding="utf-8"
    )


def test_doctor_and_hook_do_not_print_owner_token_value():
    doctor = _read("scripts/doctor.sh")
    reminder = _read("hooks/session-start-reminder.sh")
    assert "show-environment -g \"$identity_var\"" in doctor
    assert "STALE_IDENTITY_VARS" in doctor
    assert "agent_token_${SHELL_REGISTERED_AGENT}" in reminder
    assert "registration_token" in reminder


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except (AssertionError, subprocess.CalledProcessError) as error:
                failures += 1
                print(f"FAIL {name}: {error}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())


def _bootstrap_resume_fixture(
    tmp_path, *, receipt_session_id, state_program="codex-cli", receipt_program="codex-cli"
):
    """A retained codex-cli child plus a bootstrap whose registration is spied on."""

    import json
    import shutil

    session_id = "01d13f58-8e1a-7777-a3d8-e2ba243cdb49"
    project = "/project"
    bindir = tmp_path / "bin"
    libdir = bindir / "lib"
    hooks = tmp_path / "hooks"
    libdir.mkdir(parents=True)
    hooks.mkdir()
    bootstrap = bindir / "agentstack-codex-bootstrap"
    bootstrap.write_text(_read("bin/agentstack-codex-bootstrap"), encoding="utf-8")
    for helper in ("prepare-codex-session-binding.py", "child_resume.py"):
        shutil.copy2(
            pathlib.Path(__file__).resolve().parents[1] / "hooks" / helper, hooks
        )
    marker = tmp_path / "registered"
    (libdir / "agentstack-register.sh").write_text(
        "ags_mail_load_token() { :; }\n"
        "ags_mcp_call() { printf '{\"result\":{}}\\n'; }\n"
        "ags_mcp_has_error() { return 1; }\n"
        "ags_start_mail_watcher() { :; }\n"
        "ags_register_session() {\n"
        f"  printf '%s\\n' \"$2\" >> '{marker}'\n"
        "  AGS_REGISTERED_AGENT_NAME=BoundCodex\n"
        "  AGS_REGISTERED_AGENT_ID=73\n"
        "  return 0\n"
        "}\n"
        "ags_record_managed_agent() { :; }\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    (runtime / "child-agents").mkdir(parents=True)
    (runtime / "session_index").mkdir()
    state = runtime / "child-agents" / "BoundCodex.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "launch_origin": "child",
                "provider": "codex",
                "agent_id": 73,
                "agent_name": "BoundCodex",
                "project_key": project,
                "program": state_program,
                "codex_mcp_profile": "orrery-only",
                "registration_token": "owner-token",
                "retired_at": "2026-09-21T00:00:00Z",
                "resume_expires_at": "2999-09-21T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    token = runtime / "agent_token_BoundCodex"
    token.write_text("owner-token", encoding="utf-8")
    token.chmod(0o600)
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": receipt_session_id}})
        + "\n",
        encoding="utf-8",
    )
    receipt = runtime / "session_index" / "73.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "binding_kind": "self",
                "provider": "codex",
                "program": receipt_program,
                "agent_id": 73,
                "agent_name": "BoundCodex",
                "project_key": project,
                "registered_by": "BoundCodex",
                "launch_id": "prior-launch",
                "receipt_id": "prior-receipt",
                "session_id": receipt_session_id,
                "transcript_path": str(transcript),
                "launch_origin": "child",
                "codex_mcp_profile": "orrery-only",
            }
        ),
        encoding="utf-8",
    )
    script = (
        f'source "{bootstrap}" . >/dev/null 2>"{tmp_path / "stderr"}"; '
        "printf '%s\n' $?"
    )
    env = {
        **os.environ,
        "AGENTSTACK_PROJECT_KEY": project,
        "AGENTSTACK_HOOKS_DIR": str(hooks),
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_RESERVED_IDENTITY": "1",
        "AGENTSTACK_CODEX_LAUNCH_KIND": "resume",
        "AGENTSTACK_CODEX_LAUNCH_ORIGIN": "child",
        "AGENTSTACK_CODEX_RESUME_SESSION_ID": session_id,
        "AGENTSTACK_CODEX_CHILD_MCP_PROFILE": "orrery-only",
        "AGENT_NAME": "BoundCodex",
        "AGENTSTACK_PYTHON": sys.executable,
        "TMUX": "",
    }
    return script, env, marker, state, receipt


def test_refused_child_resume_does_not_re_register_or_touch_the_receipt(tmp_path):
    """#125: a resume refused by the binding check leaves the child resumable."""

    script, env, marker, state, receipt = _bootstrap_resume_fixture(
        tmp_path, receipt_session_id="11111111-2222-3333-4444-555555555555"
    )
    state_before, receipt_before = state.read_bytes(), receipt.read_bytes()

    result = subprocess.run(
        ["bash", "-c", script], env=env, text=True, capture_output=True, check=False
    )

    assert result.stdout.strip() == "1"
    assert "before re-registering" in (tmp_path / "stderr").read_text(encoding="utf-8")
    assert not marker.exists(), "register_agent must not run for a refused resume"
    assert state.read_bytes() == state_before
    assert receipt.read_bytes() == receipt_before
    assert not (receipt.parent.parent / "codex_launches" / "73.json").exists()


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("state_program", "receipt_program"),
    [("codex-cli", "codex-cli"), ("codex", "codex"), ("codex-cli", "codex")],
)
def test_child_resume_keeps_the_verified_receipt_spelling_end_to_end(
    tmp_path, state_program, receipt_program
):
    """#125: the whole bootstrap succeeds and keeps the receipt's spelling.

    Registration, the fresh launch expectation, and the resume state all use
    the program recorded in the verified prior receipt, including the existing
    mixed case where the child state says codex-cli but the receipt says codex.
    """

    import json

    script, env, marker, state, _receipt = _bootstrap_resume_fixture(
        tmp_path,
        receipt_session_id="01d13f58-8e1a-7777-a3d8-e2ba243cdb49",
        state_program=state_program,
        receipt_program=receipt_program,
    )

    result = subprocess.run(
        ["bash", "-c", script], env=env, text=True, capture_output=True, check=False
    )

    stderr = (tmp_path / "stderr").read_text(encoding="utf-8")
    assert result.stdout.strip() == "0", stderr
    assert marker.read_text(encoding="utf-8").split() == [receipt_program]
    launch = json.loads(
        (tmp_path / "runtime" / "codex_launches" / "73.json").read_text(encoding="utf-8")
    )
    assert launch["program"] == receipt_program
    assert launch["launch_kind"] == "resume"
    assert launch["fallback_receipt_id"] == "prior-receipt"
    assert json.loads(state.read_text(encoding="utf-8"))["resume_in_progress_at"]
