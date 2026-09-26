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


def test_registration_allows_project_namespace_outside_target_repository(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "target"
    namespace = tmp_path / "coordination-project"
    target.mkdir()
    namespace.mkdir()
    calls = tmp_path / "calls"
    script = f'''
source "{REGISTER_LIB}"
ags_mcp_call() {{
  local tool="$1"; shift
  printf '%s|%s\\n' "$tool" "$*" >> "$CALLS"
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
    *) return 1 ;;
  esac
}}
ags_store_registration_token() {{ :; }}
ags_apply_contact_policy() {{ :; }}
CHILD_REGISTRATION_TOKEN=reserved-token
export CHILD_REGISTRATION_TOKEN
ags_register_session "$NAMESPACE" claude-code model cc "$TARGET" Child reserved >/dev/null
status=$?
printf 'status=%s registered=%s\\n' "$status" "$AGS_REGISTERED_AGENT_NAME"
'''
    result = _bash(
        script,
        {"TARGET": str(target), "NAMESPACE": str(namespace), "CALLS": str(calls)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "status=0 registered=Child"
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert [line.split("|", 1)[0] for line in lines] == [
        "whois",
        "ensure_project",
        "register_agent",
    ]
    assert f"project_key={namespace}" in lines[0]
    assert f"human_key={namespace}" in lines[1]
    assert f"project_key={namespace}" in lines[2]


def test_reserved_registration_checks_existing_identity_then_authenticates_on_register(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    calls = tmp_path / "calls"
    script = f'''
source "{REGISTER_LIB}"
ags_mcp_call() {{
  local tool="$1"; shift
  printf '%s|%s\\n' "$tool" "$*" >> "$CALLS"
  case "$tool" in
    whois)
      [[ "$*" != *registration_token=* ]] || return 1
      printf '%s\\n' '{{"result":{{"structuredContent":{{"id":7,"name":"Child"}}}}}}'
      ;;
    ensure_project)
      printf '%s\\n' '{{"result":{{"structuredContent":{{"id":1}}}}}}'
      ;;
    register_agent)
      [[ "$*" == *registration_token=reserved-token* ]] || return 1
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
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert [line.split("|", 1)[0] for line in lines[:3]] == [
        "whois",
        "ensure_project",
        "register_agent",
    ]
    assert "registration_token=" not in lines[0]
    assert "registration_token=reserved-token" in lines[2]


def test_reserved_registration_refuses_missing_identity_before_mutation(
    tmp_path: pathlib.Path,
) -> None:
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
      printf '%s\\n' '{{"result":{{"isError":true,"content":[{{"type":"text","text":"not found"}}]}}}}'
      ;;
    *)
      return 1
      ;;
  esac
}}
CHILD_REGISTRATION_TOKEN=foreign-project-token
export CHILD_REGISTRATION_TOKEN
ags_register_session "$TARGET" claude-code model cc "$TARGET" Child reserved >/dev/null
status=$?
printf 'status=%s\\n' "$status"
'''
    result = _bash(script, {"TARGET": str(target), "CALLS": str(calls)})
    assert result.returncode == 0
    assert result.stdout.strip() == "status=1"
    assert calls.read_text(encoding="utf-8").splitlines() == ["whois"]
    assert "does not exist" in result.stderr


def test_logical_project_key_rebinds_workspace_provenance_without_rejecting_namespace(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "target"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    script = f'''
source "{PROJECT_CONTEXT}"
agentstack_validate_project_context "$TARGET" logical-project
'''
    result = _bash(
        script,
        {
            "TARGET": str(target),
            "AGENTSTACK_PROJECT_KEY": "logical-project",
            "AGENTSTACK_PROJECT_WORK_DIR": str(other),
            "AGENTSTACK_PROJECT_REPOSITORY": str(other),
        },
    )
    assert result.returncode == 0, result.stderr

    import json

    context = json.loads(result.stdout)
    assert context["project_key"] == "logical-project"
    assert context["work_dir"] == str(target)
    assert context["repository_key"] is None
    assert context["protected_roots"] == [str(target)]


def test_register_library_loads_project_validator_when_sourced_from_zsh() -> None:
    result = subprocess.run(
        [
            "/bin/zsh",
            "-c",
            f'source "{REGISTER_LIB}"; command -v agentstack_validate_project_context',
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "agentstack_validate_project_context" in result.stdout


def test_reserved_registration_runs_when_library_is_sourced_from_zsh(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = f'''
source "{REGISTER_LIB}"
ags_mcp_call() {{
  local tool="$1"; shift
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
    *) return 1 ;;
  esac
}}
ags_store_registration_token() {{ :; }}
ags_apply_contact_policy() {{ :; }}
CHILD_REGISTRATION_TOKEN=reserved-token
export CHILD_REGISTRATION_TOKEN
ags_register_session logical-project claude-code model cc "$TARGET" Child reserved >/dev/null
printf 'registered=%s\\n' "$AGS_REGISTERED_AGENT_NAME"
'''
    env = os.environ.copy()
    env["TARGET"] = str(target)
    result = subprocess.run(
        ["/bin/zsh", "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "registered=Child"
