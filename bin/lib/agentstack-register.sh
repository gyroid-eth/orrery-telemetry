#!/usr/bin/env bash
# Shared ORRERY Mail registration helpers for agent launchers.
# SOURCE this file; callers are expected to run with set -euo pipefail.

if [ -n "${BASH_SOURCE:-}" ]; then _ags_register_src="${BASH_SOURCE[0]}"; else _ags_register_src="$0"; fi
AGS_REGISTER_LIB_DIR="$(cd "$(dirname "$_ags_register_src")" && pwd)"
# shellcheck source=agentstack-scientists.sh
. "$AGS_REGISTER_LIB_DIR/agentstack-scientists.sh"
AGS_PROJECT_CONTEXT_LIB="${AGENTSTACK_PROJECT_CONTEXT_LIB:-$AGS_REGISTER_LIB_DIR/../../hooks/project-context.sh}"
if [[ -f "$AGS_PROJECT_CONTEXT_LIB" ]] && ! declare -F agentstack_validate_project_context >/dev/null 2>&1; then
  # shellcheck disable=SC1090
  . "$AGS_PROJECT_CONTEXT_LIB"
fi

ags_mail_load_token() {
  local mail_env="${AGENTSTACK_MAIL_ENV:-${MAIL_ENV:-}}"
  if [[ -z "${MCP_AGENT_MAIL_TOKEN:-}" && -n "$mail_env" && -f "$mail_env" ]]; then
    local tok
    tok="$(grep HTTP_BEARER_TOKEN "$mail_env" 2>/dev/null | cut -d= -f2- || true)"
    [[ -n "$tok" ]] && export MCP_AGENT_MAIL_TOKEN="$tok"
  fi
  return 0
}

ags_mcp_call() {
  local tool="$1"; shift
  local mcp_url="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:18765/mcp}}"
  local args_json payload
  args_json="$(python3 - "$@" <<'PY'
import json
import sys
args = {}
for item in sys.argv[1:]:
    key, value = item.split("=", 1)
    args[key] = value
print(json.dumps(args, separators=(",", ":")))
PY
)"
  payload="$(python3 - "$tool" "$args_json" <<'PY'
import json
import sys
print(json.dumps({
    "jsonrpc": "2.0",
    "id": "1",
    "method": "tools/call",
    "params": {"name": sys.argv[1], "arguments": json.loads(sys.argv[2])},
}, separators=(",", ":")))
PY
)"
  local auth=()
  [[ -n "${MCP_AGENT_MAIL_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer $MCP_AGENT_MAIL_TOKEN")
  printf '%s' "$payload" | curl -sf --max-time 30 -X POST "$mcp_url" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Connection: close" \
    ${auth[@]+"${auth[@]}"} \
    --data-binary @- 2>/dev/null
}

ags_mcp_has_error() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if data.get("error"):
    sys.exit(0)
result = data.get("result") if isinstance(data, dict) else None
if isinstance(result, dict):
    if result.get("isError") is True:
        sys.exit(0)
    content = result.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.startswith("Error calling tool"):
                    sys.exit(0)
sys.exit(1)
'
}

ags_extract_agent_id() {
  python3 -c '
import json, sys
def candidate_id(obj):
    if isinstance(obj, dict) and isinstance(obj.get("id"), int):
        return obj["id"]
    return None
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)
found = candidate_id(data)
result = data.get("result") if isinstance(data, dict) else None
if found is None:
    found = candidate_id(result)
if found is None and isinstance(result, dict):
    found = candidate_id(result.get("structuredContent"))
    if found is None and isinstance(result.get("content"), list):
        for part in result["content"]:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                try:
                    found = candidate_id(json.loads(part["text"]))
                except Exception:
                    found = None
                if found is not None:
                    break
print("" if found is None else found)
'
}

ags_extract_agent_name() {
  python3 -c '
import json, sys
def candidate_names(obj):
    if isinstance(obj, dict):
        for key in ("name", "agent_name"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                yield value
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)
for name in candidate_names(data):
    print(name)
    sys.exit(0)
result = data.get("result") if isinstance(data, dict) else None
for name in candidate_names(result):
    print(name)
    sys.exit(0)
if isinstance(result, dict):
    structured = result.get("structuredContent")
    for name in candidate_names(structured):
        print(name)
        sys.exit(0)
    content = result.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            for name in candidate_names(obj):
                print(name)
                sys.exit(0)
print("")
'
}

ags_extract_registration_token() {
  python3 -c '
import json, sys
def candidate_tokens(obj):
    if isinstance(obj, dict):
        value = obj.get("registration_token")
        if isinstance(value, str) and value:
            yield value
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)
for token in candidate_tokens(data):
    print(token)
    sys.exit(0)
result = data.get("result") if isinstance(data, dict) else None
for token in candidate_tokens(result):
    print(token)
    sys.exit(0)
if isinstance(result, dict):
    structured = result.get("structuredContent")
    for token in candidate_tokens(structured):
        print(token)
        sys.exit(0)
    content = result.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            for token in candidate_tokens(obj):
                print(token)
                sys.exit(0)
print("")
'
}

ags_generate_registration_token() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

ags_registration_runtime_dir() {
  printf '%s\n' "${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
}

ags_agent_token_key() {
  printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '_'
}

ags_registration_token_file() {
  local agent_name="$1" runtime_dir key
  [[ -n "$agent_name" ]] || return 1
  runtime_dir="$(ags_registration_runtime_dir)"
  key="$(ags_agent_token_key "$agent_name")"
  [[ -n "$key" ]] || return 1
  printf '%s/agent_token_%s\n' "$runtime_dir" "$key"
}

ags_load_registration_token() {
  local agent_name="$1" token_file token
  token_file="$(ags_registration_token_file "$agent_name")" || return 1
  [[ -f "$token_file" ]] || return 1
  IFS= read -r token < "$token_file" || true
  [[ -n "$token" ]] || return 1
  printf '%s\n' "$token"
}

ags_store_registration_token() {
  local agent_name="$1" registration_token="$2" runtime_dir token_file
  [[ -n "$agent_name" && -n "$registration_token" ]] || return 0
  runtime_dir="$(ags_registration_runtime_dir)"
  token_file="$(ags_registration_token_file "$agent_name")" || return 1
  mkdir -p "$runtime_dir" || return 1
  ( umask 077 && printf '%s' "$registration_token" > "$token_file" ) || return 1
  chmod 600 "$token_file" 2>/dev/null || true
}

ags_record_name_substitution() {
  local registered="$1" requested="$2" runtime_dir store
  [[ -n "$registered" && -n "$requested" && "$registered" != "$requested" ]] || return 0
  runtime_dir="$(ags_registration_runtime_dir)"
  store="$runtime_dir/name-substitutions.json"
  mkdir -p "$runtime_dir" || return 1
  "${AGENTSTACK_PYTHON:-python3}" - "$store" "$registered" "$requested" <<'PY' || return 1
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
store, registered, requested = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    data = json.loads(store.read_text(encoding="utf-8"))
except (OSError, ValueError):
    data = {}
if not isinstance(data, dict):
    data = {}
data[registered] = {
    "requested": requested,
    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
tmp = store.with_name(store.name + f".{os.getpid()}.tmp")
tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
tmp.replace(store)
PY
}

ags_apply_contact_policy() {
  local project_key="$1" agent_name="$2" registration_token="${3:-}" policy
  [[ -n "$project_key" && -n "$agent_name" && -n "$registration_token" ]] || return 0
  if [[ ${AGENTSTACK_CONTACT_POLICY+x} ]]; then
    policy="$AGENTSTACK_CONTACT_POLICY"
  else
    policy="open"
  fi
  [[ -n "$policy" ]] || return 0
  policy="$(printf '%s' "$policy" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
  case "$policy" in
    closed) policy="contacts_only" ;;
    skip|none|off|disabled) return 0 ;;
  esac
  local resp
  resp="$(ags_mcp_call "set_contact_policy" \
    "project_key=$project_key" \
    "agent_name=$agent_name" \
    "policy=$policy" \
    "registration_token=$registration_token" 2>/dev/null)"
  if [[ -z "$resp" ]] || printf '%s' "$resp" | ags_mcp_has_error; then
    ags_mcp_call "set_contact_policy" \
      "project_key=$project_key" \
      "agent_name=$agent_name" \
      "policy=$policy" >/dev/null 2>&1 || true
  fi
}

ags_agent_name_status() {
  local project_key="$1" agent_name="$2" response
  response="$(ags_mcp_call "whois" "project_key=$project_key" "agent_name=$agent_name" 2>/dev/null || true)"
  if [[ -z "$response" ]]; then
    printf 'unknown\n'
    return 0
  fi
  if printf '%s' "$response" | ags_mcp_has_error; then
    if printf '%s' "$response" | grep -qiE "requires[ _]registration_token|already authenticated"; then
      printf 'occupied\n'
    elif printf '%s' "$response" | grep -qiE "not found|does not exist|no such agent|unknown agent"; then
      printf 'available\n'
    else
      printf 'unknown\n'
    fi
    return 0
  fi
  if [[ -n "$(printf '%s' "$response" | ags_extract_agent_name)" ]]; then
    printf 'occupied\n'
  else
    printf 'available\n'
  fi
}

ags_agent_exists() {
  local name_status
  name_status="$(ags_agent_name_status "$1" "$2")"
  [[ "$name_status" == "occupied" ]]
}

ags_agent_name_available() {
  local name_status
  name_status="$(ags_agent_name_status "$1" "$2")"
  [[ "$name_status" == "available" ]]
}

ags_verify_registration_token() {
  local project_key="$1" agent_name="$2" registration_token="$3" response returned
  [[ -n "$project_key" && -n "$agent_name" && -n "$registration_token" ]] || return 1
  response="$(ags_mcp_call "whois" \
    "project_key=$project_key" \
    "agent_name=$agent_name" \
    "registration_token=$registration_token" 2>/dev/null || true)"
  [[ -n "$response" ]] || return 1
  ! printf '%s' "$response" | ags_mcp_has_error || return 1
  returned="$(printf '%s' "$response" | ags_extract_agent_name)"
  [[ "$returned" == "$agent_name" ]]
}

ags_pick_scientist_name() {
  local _prefix="${1:-}"
  ags_pick_adjective_scientist_name
}

AGS_NAME_UNKNOWN_LIMIT="${AGENTSTACK_NAME_UNKNOWN_LIMIT:-3}"

ags_pick_available_agent_name() {
  local project_key="$1" _prefix="$2" preferred_name="${3:-}"
  local attempts="${AGENTSTACK_AGENT_NAME_ATTEMPTS:-75}"
  local adjective scientist candidate i name_status
  local unknowns=0

  if [[ -n "$preferred_name" ]]; then
    name_status="$(ags_agent_name_status "$project_key" "$preferred_name")"
    if [[ "$name_status" == "available" ]]; then
      ags_note_scientist_used "$preferred_name" || true
      printf '%s\n' "$preferred_name"
      return 0
    fi
    if [[ "$name_status" == "unknown" ]]; then
      echo "agentstack: cannot verify whether '$preferred_name' is free (ORRERY Mail unreachable or refusing whois); refusing to claim it." >&2
      return 1
    fi
  fi

  for ((i = 0; i < attempts; i++)); do
    candidate="$(ags_pick_adjective_scientist_name)" || return 1
    name_status="$(ags_agent_name_status "$project_key" "$candidate")"
    case "$name_status" in
      available)
        ags_note_scientist_used "$candidate" || true
        printf '%s\n' "$candidate"; return 0 ;;
      occupied) unknowns=0 ;;
      *)
        unknowns=$((unknowns + 1))
        if (( unknowns >= AGS_NAME_UNKNOWN_LIMIT )); then
          echo "agentstack: name availability checks failed $unknowns times in a row; refusing to pick a name that may already be in use." >&2
          return 1
        fi ;;
    esac
  done

  for ((i = 2; i < attempts + 200; i++)); do
    adjective="$(ags_pick_adjective)" || return 1
    scientist="$(ags_pick_scientist)" || return 1
    candidate="${adjective}-${i}-${scientist}"
    name_status="$(ags_agent_name_status "$project_key" "$candidate")"
    case "$name_status" in
      available)
        ags_note_scientist_used "$candidate" || true
        printf '%s\n' "$candidate"; return 0 ;;
      occupied) unknowns=0 ;;
      *)
        unknowns=$((unknowns + 1))
        if (( unknowns >= AGS_NAME_UNKNOWN_LIMIT )); then
          echo "agentstack: name availability checks failed $unknowns times in a row; refusing to pick a name that may already be in use." >&2
          return 1
        fi ;;
    esac
  done
  return 1
}

ags_register_session() {
  local project_key="$1" program="$2" model="$3" prefix="$4" work_dir="$5" requested_name="${6:-}" requested_mode="${7:-reserved}"
  AGS_REGISTERED_AGENT_NAME=""
  AGS_REGISTERED_AGENT_ID=""
  AGS_REGISTERED_REGISTRATION_TOKEN=""
  AGS_REQUESTED_AGENT_NAME=""
  AGS_SERVER_RETURNED_AGENT_NAME=""
  AGS_AGENT_NAME_SUBSTITUTED=0

  local context_json="" registration_token=""
  declare -F agentstack_validate_project_context >/dev/null 2>&1 || {
    echo "agentstack: project context validator is unavailable; refusing registration." >&2
    return 1
  }
  context_json="$(agentstack_validate_project_context "$work_dir" "$project_key")" || {
    echo "agentstack: project '$project_key' is not authorized for work directory '$work_dir'." >&2
    return 1
  }
  project_key="$(agentstack_context_field "$context_json" project_key)" || return 1

  # A reserved identity may reuse its token only after Mail proves that token
  # belongs to the same name in the validated target project. This replaces a
  # second local ownership database: a same-name token from another project is
  # simply unusable here.
  if [[ "$requested_mode" == "reserved" && -n "$requested_name" ]]; then
    registration_token="${CHILD_REGISTRATION_TOKEN:-}"
    if [[ -z "$registration_token" ]]; then
      registration_token="$(ags_load_registration_token "$requested_name" 2>/dev/null || true)"
    fi
    [[ -n "$registration_token" ]] || return 1
    ags_verify_registration_token "$project_key" "$requested_name" "$registration_token" || {
      echo "agentstack: reserved identity '$requested_name' is not authenticated in '$project_key'." >&2
      return 1
    }
  fi

  local task_description="Agent session in $work_dir"
  case "$program" in
    claude-code) task_description="Claude session in $work_dir" ;;
    codex) task_description="Codex session in $work_dir" ;;
  esac

  local agent_name="$requested_name"
  if [[ -z "$agent_name" ]]; then
    agent_name="$(ags_pick_available_agent_name "$project_key" "$prefix")" || return 1
  elif [[ "$requested_mode" == "candidate" ]]; then
    agent_name="$(ags_pick_available_agent_name "$project_key" "$prefix" "$agent_name")" || return 1
  fi
  AGS_REQUESTED_AGENT_NAME="$agent_name"

  ags_mcp_call "ensure_project" "human_key=$project_key" >/dev/null

  if [[ -z "$registration_token" ]] && ags_agent_name_available "$project_key" "$agent_name"; then
    registration_token="$(ags_generate_registration_token)" || return 1
  fi

  local register_args=(
    "project_key=$project_key"
    "program=$program"
    "model=$model"
    "name=$agent_name"
    "task_description=$task_description"
  )
  [[ -n "$registration_token" ]] && register_args+=("registration_token=$registration_token")

  local result registered registered_token
  result="$(ags_mcp_call "register_agent" "${register_args[@]}")" || return 1
  if printf '%s' "$result" | ags_mcp_has_error; then
    return 1
  fi
  registered="$(printf '%s' "$result" | ags_extract_agent_name)"
  [[ -n "$registered" ]] || return 1
  AGS_SERVER_RETURNED_AGENT_NAME="$registered"
  if [[ "$registered" != "$agent_name" ]]; then
    AGS_AGENT_NAME_SUBSTITUTED=1
    if [[ "$requested_mode" == "reserved" ]]; then
      return 2
    fi
  fi
  registered_token="$(printf '%s' "$result" | ags_extract_registration_token)"
  [[ -n "$registered_token" ]] || registered_token="$registration_token"
  if [[ -n "$registered_token" ]]; then
    CHILD_REGISTRATION_TOKEN="$registered_token"
    AGS_REGISTERED_REGISTRATION_TOKEN="$registered_token"
    export CHILD_REGISTRATION_TOKEN
    ags_store_registration_token "$registered" "$registered_token" || true
    ags_apply_contact_policy "$project_key" "$registered" "$registered_token"
  fi
  AGS_REGISTERED_AGENT_NAME="$registered"
  AGS_REGISTERED_AGENT_ID="$(printf '%s' "$result" | ags_extract_agent_id)"
  printf '%s\n' "$registered"
}

ags_start_mail_watcher() {
  local tmux_bin="$1" hooks_dir="$2"
  local watcher_session="${AGENTSTACK_MAIL_WATCHER_SESSION:-mail-watcher}"
  [[ -n "$tmux_bin" && -n "$hooks_dir" && -f "$hooks_dir/watch_agent_mail_signals.sh" ]] || return 0
  local pidfile="${AGENTSTACK_MAIL_WATCHER_PIDFILE:-${AGENTSTACK_MAIL_WATCHER_LOCK_DIR:-/tmp/orrery-mail-watcher.lock}/watcher.pid}"
  if [[ -f "$pidfile" ]]; then
    local watcher_pid
    watcher_pid="$(head -n 1 "$pidfile" 2>/dev/null | tr -d '[:space:]')"
    if [[ "$watcher_pid" =~ ^[0-9]+$ ]] && kill -0 "$watcher_pid" 2>/dev/null; then
      return 0
    fi
  fi
  if ! "$tmux_bin" has-session -t "$watcher_session" 2>/dev/null; then
    "$tmux_bin" new-session -d -s "$watcher_session" \
      "bash '$hooks_dir/watch_agent_mail_signals.sh'" >/dev/null 2>&1 \
      && echo "info: started mail-watcher" >&2 || true
  fi
}

ags_record_managed_agent() {
  local managed_file="$1" agent_name="$2"
  [[ -n "$managed_file" && -n "$agent_name" ]] || return 0
  mkdir -p "$(dirname "$managed_file")" 2>/dev/null || true
  grep -qxF "$agent_name" "$managed_file" 2>/dev/null || echo "$agent_name" >> "$managed_file"
}

ags_tcc_dir_is_protected() {
  local dir="$1" p
  local dirs="${AGENTSTACK_TCC_DIRS:-$HOME/Desktop:$HOME/Downloads:$HOME/Documents}"
  if [[ "$dirs" == *:* ]]; then
    local IFS=:
    for p in $dirs; do
      [[ -n "$p" ]] || continue
      [[ "$dir" == "$p" || "$dir" == "$p"/* ]] && return 0
    done
    return 1
  fi
  for p in $dirs; do
    [[ -n "$p" ]] || continue
    [[ "$dir" == "$p" || "$dir" == "$p"/* ]] && return 0
  done
  return 1
}

ags_warn_tcc_access() {
  local dir="$1" probe
  [[ "${AGENTSTACK_TCC_GUARD:-1}" != "0" ]] || return 0
  [[ "$(uname -s 2>/dev/null)" == "Darwin" ]] || return 0
  [[ -n "$dir" && -d "$dir" ]] || return 0
  ags_tcc_dir_is_protected "$dir" || return 0
  probe="$(find "$dir" -maxdepth 1 -type f 2>/dev/null | head -1)"
  [[ -n "$probe" ]] || return 0
  head -c 1 "$probe" >/dev/null 2>&1 && return 0
  {
    printf '\n⚠️  agentstack: cannot read files under a macOS privacy-protected folder:\n'
    printf '      %s\n' "$dir"
    printf '    This agent inherited a terminal identity WITHOUT access to that folder\n'
    printf '    (typically a non-Full-Disk-Access terminal such as Terminal.app somewhere up\n'
    printf '    the launch chain). Files here will fail with "Operation not permitted" (EPERM).\n'
    printf '    Fix (either one):\n'
    printf '      • Relaunch the ROOT agent from a Full-Disk-Access terminal (e.g. Ghostty), or\n'
    printf '      • Move the project outside ~/Desktop, ~/Downloads, ~/Documents.\n'
    printf "    A running agent's access context cannot be changed in place — recreate the session.\n\n"
  } >&2
  return 0
}
