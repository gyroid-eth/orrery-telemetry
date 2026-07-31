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
    for name in ("AGENT_NAME", "PARENT_AGENT", "CHILD_REGISTRATION_TOKEN"):
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
        "TMUX": "",
    }
    command = (
        f'source "{bootstrap}" . >/dev/null 2>&1; '
        "printf '%s|%s|%s\\n' \"${AGENT_NAME:-}\" "
        "\"${PARENT_AGENT:-}\" \"${CHILD_REGISTRATION_TOKEN:-}\""
    )
    top_level = _run_bash(command, common).stdout.strip().split("|")
    assert top_level[0] and top_level[0] != "Stale-Dirac", top_level
    assert top_level[1:] == ["", ""], top_level

    reserved_env = dict(common)
    reserved_env["AGENTSTACK_RESERVED_IDENTITY"] = "1"
    reserved = _run_bash(command, reserved_env).stdout.strip().split("|")
    assert reserved == ["Stale-Dirac", "Stale-Parent", "stale-owner-token"], reserved


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
    printf '%s\\n' '{{"result":{{"structuredContent":{{}}}}}}'
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
    """Local agent-mail removes hyphens, so response name is the identity."""
    register_lib = _ROOT / "bin" / "lib" / "agentstack-register.sh"
    script = f'''
source "{register_lib}"
ags_mcp_call() {{
  local tool="$1"; shift
  if [[ "$tool" == "register_agent" ]]; then
    printf '%s\\n' '{{"result":{{"structuredContent":{{"name":"FrostyPasteur","registration_token":"stable-owner-token"}}}}}}'
  else
    printf '%s\\n' '{{"result":{{"structuredContent":{{}}}}}}'
  fi
}}
ags_generate_registration_token() {{ printf '%s\\n' requested-owner-token; }}
ags_store_registration_token() {{ printf '%s|%s\\n' "$1" "$2"; }}
ags_apply_contact_policy() {{ :; }}
for _ in 1 2; do
  ags_register_session /project codex model cx /work Frosty-Pasteur candidate >/dev/null
  printf 'registered=%s token=%s\\n' "$AGS_REGISTERED_AGENT_NAME" "$AGS_REGISTERED_REGISTRATION_TOKEN"
done
'''
    result = _run_bash(script)
    assert result.stdout.splitlines() == [
        "registered=FrostyPasteur token=stable-owner-token",
        "registered=FrostyPasteur token=stable-owner-token",
    ]


def test_reserved_child_marker_and_rename_failure_are_explicit():
    spawn = _read("hooks/spawn_child.sh")
    bootstrap = _read("bin/agentstack-codex-bootstrap")
    launcher = _read("bin/agent-start-codex")
    assert spawn.count('-e "AGENTSTACK_RESERVED_IDENTITY=1"') >= 2
    assert 'rename-session "$AGENT_NAME" 2>/dev/null || true' not in bootstrap
    assert "refusing identity registration" in bootstrap
    assert "agent registration skipped" in bootstrap
    assert "tmux rename-session failed: current session" in bootstrap
    assert '"$TMUX_IDENTITY_MATCHED" == "1"' in bootstrap
    assert 'source $(printf \'%q\' "$BOOTSTRAP") $(printf \'%q\' "$DIR") && $CODEX_CMD' in launcher


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
