#!/usr/bin/env bash
# Shared agent-mail registration helpers for agent launchers.
# SOURCE this file; callers are expected to run with set -euo pipefail.

if [ -n "${BASH_SOURCE:-}" ]; then _ags_register_src="${BASH_SOURCE[0]}"; else _ags_register_src="$0"; fi
AGS_REGISTER_LIB_DIR="$(cd "$(dirname "$_ags_register_src")" && pwd)"
# shellcheck source=agentstack-scientists.sh
. "$AGS_REGISTER_LIB_DIR/agentstack-scientists.sh"

ags_mail_load_token() {
  local mail_env="${AGENTSTACK_MAIL_ENV:-${MAIL_ENV:-}}"
  if [[ -z "${MCP_AGENT_MAIL_TOKEN:-}" && -n "$mail_env" && -f "$mail_env" ]]; then
    local tok
    tok="$(grep HTTP_BEARER_TOKEN "$mail_env" 2>/dev/null | cut -d= -f2- || true)"
    [[ -n "$tok" ]] && export MCP_AGENT_MAIL_TOKEN="$tok"
  fi
}

ags_mcp_call() {
  local tool="$1"; shift
  local mcp_url="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:8765/mcp}}"
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
    -H "Content-Type: application/json" -H "Accept: application/json" -H "Connection: close" \
    "${auth[@]}" \
    --data-binary @- 2>/dev/null
}

ags_mcp_has_error() {
  # Exit 0 (== "has error") when the MCP response signals failure at EITHER the
  # JSON-RPC layer (top-level "error") OR the tool layer. A tool failure comes
  # back as a normal JSON-RPC result with result.isError == true and a
  # content[].text like "Error calling tool '...': ...", which a top-level
  # "error" check alone silently passes through.
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
    closed)
      policy="contacts_only"
      ;;
    skip|none|off|disabled)
      return 0
      ;;
  esac
  # Try WITH the owner token first: stock mcp-agent-mail gates set_contact_policy
  # on the agent's registration_token, so omitting it makes the call fail and
  # (previously with a bare || true) silently leave the policy at the server
  # default instead of 'open'. But older/lenient servers whose set_contact_policy
  # signature has no registration_token param reject the extra kwarg — so if the
  # token-bearing call errors, retry without it. Both paths are best-effort.
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

# Three-valued name availability: a name we cannot check is NOT a free name.
# Prints one of: available | occupied | unknown
#
# whois signals all three through an error channel, so the error TEXT decides:
#   - "Agent '<name>' not found in project ..."   -> available (server answered)
#   - "requires registration_token for agent ..." -> occupied (token-strict
#     server confirming the agent exists but refusing an unauthenticated read)
#   - empty response, transport failure, anything else -> unknown
#
# Previously every error mapped to "does not exist", so an auth error or a
# timeout read as "this name is free" and a fresh session could register under
# a live agent's identity. Availability decisions must be fail-closed.
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

# Back-compat wrapper: true only for a positively confirmed existing agent.
# Callers deciding whether a name is FREE must use ags_agent_name_available
# instead, so that 'unknown' is never mistaken for 'available'.
ags_agent_exists() {
  local name_status
  name_status="$(ags_agent_name_status "$1" "$2")"
  [[ "$name_status" == "occupied" ]]
}

# Fail-closed availability check: only an explicit 'available' passes.
ags_agent_name_available() {
  local name_status
  name_status="$(ags_agent_name_status "$1" "$2")"
  [[ "$name_status" == "available" ]]
}

ags_pick_scientist_name() {
  local _prefix="${1:-}"
  ags_pick_adjective_scientist_name
}

# Consecutive 'unknown' answers mean the availability check itself is broken
# (server down mid-run, auth wall, timeouts). Handing out a name we could not
# verify is exactly the failure mode this guard exists to prevent, so stop.
AGS_NAME_UNKNOWN_LIMIT="${AGENTSTACK_NAME_UNKNOWN_LIMIT:-3}"

ags_pick_available_agent_name() {
  local project_key="$1" _prefix="$2" preferred_name="${3:-}"
  local attempts="${AGENTSTACK_AGENT_NAME_ATTEMPTS:-75}"
  local adjective scientist candidate i name_status
  local unknowns=0

  if [[ -n "$preferred_name" ]]; then
    name_status="$(ags_agent_name_status "$project_key" "$preferred_name")"
    if [[ "$name_status" == "available" ]]; then
      printf '%s\n' "$preferred_name"
      return 0
    fi
    if [[ "$name_status" == "unknown" ]]; then
      echo "agentstack: cannot verify whether '$preferred_name' is free (agent-mail unreachable or refusing whois); refusing to claim it." >&2
      return 1
    fi
  fi

  for ((i = 0; i < attempts; i++)); do
    candidate="$(ags_pick_adjective_scientist_name)" || return 1
    name_status="$(ags_agent_name_status "$project_key" "$candidate")"
    case "$name_status" in
      available) printf '%s\n' "$candidate"; return 0 ;;
      occupied)  unknowns=0 ;;
      *)
        unknowns=$((unknowns + 1))
        if (( unknowns >= AGS_NAME_UNKNOWN_LIMIT )); then
          echo "agentstack: name availability checks failed $unknowns times in a row; refusing to pick a name that may already be in use." >&2
          return 1
        fi
        ;;
    esac
  done

  for ((i = 2; i < attempts + 200; i++)); do
    adjective="$(ags_pick_adjective)" || return 1
    scientist="$(ags_pick_scientist)" || return 1
    candidate="${adjective}-${i}-${scientist}"
    name_status="$(ags_agent_name_status "$project_key" "$candidate")"
    case "$name_status" in
      available) printf '%s\n' "$candidate"; return 0 ;;
      occupied)  unknowns=0 ;;
      *)
        unknowns=$((unknowns + 1))
        if (( unknowns >= AGS_NAME_UNKNOWN_LIMIT )); then
          echo "agentstack: name availability checks failed $unknowns times in a row; refusing to pick a name that may already be in use." >&2
          return 1
        fi
        ;;
    esac
  done

  return 1
}

ags_register_session() {
  local project_key="$1" program="$2" model="$3" prefix="$4" work_dir="$5" requested_name="${6:-}" requested_mode="${7:-reserved}"
  AGS_REGISTERED_AGENT_NAME=""
  AGS_REGISTERED_REGISTRATION_TOKEN=""
  AGS_REQUESTED_AGENT_NAME=""
  AGS_SERVER_RETURNED_AGENT_NAME=""
  AGS_AGENT_NAME_SUBSTITUTED=0

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

  # Ambient owner credentials are valid only for an explicitly verified
  # reserved identity. Candidate/top-level registration must not adopt a token
  # inherited from another tmux session.
  local registration_token=""
  if [[ "$requested_mode" == "reserved" && -n "$requested_name" ]]; then
    registration_token="${CHILD_REGISTRATION_TOKEN:-}"
    if [[ -z "$registration_token" ]]; then
      registration_token="$(ags_load_registration_token "$agent_name" 2>/dev/null || true)"
    fi
  fi
  # Mint a fresh owner token only for a name the server positively reports as
  # free. An 'unknown' answer must not mint one: that is how an unverified name
  # used to get claimed on top of a live agent.
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
    # A reserved identity already has a parent task, owner token, tmux metadata,
    # and inbox addressed to the requested name. Adopting a replacement here
    # would strand all of those under the old name. Candidate/top-level launch
    # paths may reconcile their not-yet-started tmux session to the read-back,
    # but an existing identity must fail closed.
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
  printf '%s\n' "$registered"
}

ags_start_mail_watcher() {
  local tmux_bin="$1" hooks_dir="$2"
  local watcher_session="${AGENTSTACK_MAIL_WATCHER_SESSION:-mail-watcher}"
  [[ -n "$tmux_bin" && -n "$hooks_dir" && -f "$hooks_dir/watch_agent_mail_signals.sh" ]] || return 0
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

# --- macOS TCC (privacy-protected folder) access guard ------------------------
# When an agent's working directory sits inside a macOS privacy-protected folder
# (~/Desktop, ~/Downloads, ~/Documents by default) AND the agent cannot read
# files there, macOS is denying access based on the *terminal identity* this
# process inherited — not on file permissions. On a delegate chain that identity
# propagates from the ancestor that launched the ROOT agent: if that ancestor ran
# in a terminal WITHOUT Full Disk Access (e.g. Terminal.app), every descendant
# inherits it (via the env carried into `tmux new-session`) and hits a bare
# `EPERM` ("Operation not permitted") that is almost impossible to diagnose,
# because the tmux server may be owned by an FDA terminal (e.g. Ghostty) while an
# individual pane still carries the non-FDA identity.
#
# This is a DETECT-AND-WARN guard only: it never blocks a spawn and it fires only
# on an actual read failure inside a protected folder (a functional probe → zero
# false positives when access works). Override the protected-dir list with
# AGENTSTACK_TCC_DIRS (colon-separated; legacy whitespace lists also work) or
# disable with AGENTSTACK_TCC_GUARD=0.
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
  # Compatibility with pre-0.9 examples that used a whitespace list.
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
  # Functional probe: read one byte from the first regular file in the dir.
  probe="$(find "$dir" -maxdepth 1 -type f 2>/dev/null | head -1)"
  [[ -n "$probe" ]] || return 0
  head -c 1 "$probe" >/dev/null 2>&1 && return 0   # readable → no TCC problem
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
