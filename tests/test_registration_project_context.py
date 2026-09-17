from __future__ import annotations

import os
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTER_LIB = ROOT / "bin" / "lib" / "agentstack-register.sh"
PROJECT_CONTEXT = ROOT / "hooks" / "project-context.sh"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_registration_rejects_unrelated_physical_project_before_mail(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    calls = tmp_path / "calls"
    script = f'''
source "{REGISTER_LIB}"
ags_mcp_call() {{ printf '%s\\n' "$1" >> "$CALLS"; return 1; }}
CHILD_REGISTRATION_TOKEN=reserved-token
export CHILD_REGISTRATION_TOKEN
ags_register_session "$OTHER" claude-code model cc "$TARGET" Child reserved >/dev/null
status=$?
printf 'status=%s calls=%s\\n' "$status" "$([ -f "$CALLS" ] && wc -l < "$CALLS" | tr -d ' ' || printf 0)"
'''
    result = _bash(
        script,
        {"TARGET": str(target), "OTHER": str(other), "CALLS": str(calls)},
    )
    assert result.stdout.strip() == "status=1 calls=0", (result.stdout, result.stderr)
    assert "not authorized for work directory" in result.stderr


def test_reserved_registration_authenticates_token_in_validated_project(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    calls = tmp_path / "calls"
    script = f'''
source "{REGISTER_LIB}"
ags_mcp_call() {{
  local tool="$1"; shift
  printf '%s\\n' "$tool" >> "$CALLS"
  case "$tool" in
    whois)
      printf '%s\\n' '{{"result":{{"structuredContent":{{"id":7,"name":"Child"}}}}}}'
      ;;
    ensure_project)
      printf '%s\\n' '{{"result":{{"structuredContent":{{"id":1}}}}}}'
      ;;
    register_agent)
      printf '%s\\n' '{{"result":{{"structuredContent":{{"id":7,"name":"Child","registration_token":"reserved-token"}}}}}}'
      ;;
    *)
      return 1
      ;;
  esac
}}
ags_store_registration_token() {{ :; }}
ags_apply_contact_policy() {{ :; }}
CHILD_REGISTRATION_TOKEN=reserved-token
export CHILD_REGISTRATION_TOKEN
ags_register_session "$TARGET" claude-code model cc "$TARGET" Child reserved >/dev/null
status=$?
printf 'status=%s registered=%s\\n' "$status" "$AGS_REGISTERED_AGENT_NAME"
'''
    result = _bash(script, {"TARGET": str(target), "CALLS": str(calls)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "status=0 registered=Child"
    assert calls.read_text(encoding="utf-8").splitlines()[:3] == [
        "whois",
        "ensure_project",
        "register_agent",
    ]


def test_logical_project_key_requires_matching_bound_workspace(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    script = f'''
source "{PROJECT_CONTEXT}"
if agentstack_validate_project_context "$TARGET" logical-project >/dev/null; then
  printf 'accepted\\n'
else
  printf 'rejected\\n'
fi
'''
    rejected = _bash(
        script,
        {
            "TARGET": str(target),
            "AGENTSTACK_PROJECT_KEY": "logical-project",
            "AGENTSTACK_PROJECT_WORK_DIR": str(other),
        },
    )
    assert rejected.stdout.strip() == "rejected"

    accepted = _bash(
        script,
        {
            "TARGET": str(target),
            "AGENTSTACK_PROJECT_KEY": "logical-project",
            "AGENTSTACK_PROJECT_WORK_DIR": str(target),
        },
    )
    assert accepted.stdout.strip() == "accepted"
