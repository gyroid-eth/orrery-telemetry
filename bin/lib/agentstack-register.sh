#!/usr/bin/env bash
# Shared agent-mail registration helpers for agent launchers.
# SOURCE this file; callers are expected to run with set -euo pipefail.

AGS_REGISTER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
  curl -sf --max-time 30 -X POST "$mcp_url" \
    -H "Content-Type: application/json" -H "Accept: application/json" -H "Connection: close" \
    "${auth[@]}" \
    -d "$payload" 2>/dev/null
}

ags_mcp_has_error() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sys.exit(0 if data.get("error") else 1)
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

ags_agent_exists() {
  local project_key="$1" agent_name="$2" response
  response="$(ags_mcp_call "whois" "project_key=$project_key" "agent_name=$agent_name" 2>/dev/null || true)"
  [[ -n "$response" ]] || return 1
  if printf '%s' "$response" | ags_mcp_has_error; then
    return 1
  fi
  [[ -n "$(printf '%s' "$response" | ags_extract_agent_name)" ]]
}

ags_pick_available_agent_name() {
  local project_key="$1" prefix="$2"
  local attempts="${AGENTSTACK_AGENT_NAME_ATTEMPTS:-75}"
  local scientist candidate i

  for ((i = 0; i < attempts; i++)); do
    scientist="$(ags_pick_scientist)" || return 1
    candidate="${prefix}-${scientist}"
    if ! ags_agent_exists "$project_key" "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  for ((i = 2; i < attempts + 200; i++)); do
    scientist="$(ags_pick_scientist)" || return 1
    candidate="${prefix}-${i}-${scientist}"
    if ! ags_agent_exists "$project_key" "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

ags_register_session() {
  local project_key="$1" program="$2" model="$3" prefix="$4" work_dir="$5" requested_name="${6:-}"
  local task_description="Agent session in $work_dir"
  case "$program" in
    claude-code) task_description="Claude session in $work_dir" ;;
    codex) task_description="Codex session in $work_dir" ;;
  esac

  local agent_name="$requested_name"
  if [[ -z "$agent_name" ]]; then
    agent_name="$(ags_pick_available_agent_name "$project_key" "$prefix")" || return 1
  fi

  ags_mcp_call "ensure_project" "human_key=$project_key" >/dev/null

  local register_args=(
    "project_key=$project_key"
    "program=$program"
    "model=$model"
    "name=$agent_name"
    "task_description=$task_description"
  )
  [[ -n "${CHILD_REGISTRATION_TOKEN:-}" ]] && register_args+=("registration_token=$CHILD_REGISTRATION_TOKEN")

  local result registered
  result="$(ags_mcp_call "register_agent" "${register_args[@]}")" || return 1
  if printf '%s' "$result" | ags_mcp_has_error; then
    return 1
  fi
  registered="$(printf '%s' "$result" | ags_extract_agent_name)"
  [[ -n "$registered" ]] || return 1
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
