#!/usr/bin/env bash
# Shared ORRERY Mail registration helpers for agent launchers.
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
  # The bearer token is optional (tokenless transports set
  # AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled). Without this the last evaluated
  # command is the `[[ -n "$tok" ]]` test, so "no token" returns 1 and the
  # `set -e` in every caller kills the launcher with no output at all.
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
  # `${auth[@]+"${auth[@]}"}` — macOS bash 3.2 treats a plain `"${auth[@]}"` on
  # an empty array as an unbound variable under `set -u`, so a tokenless call
  # would abort here instead of sending no Authorization header.
  printf '%s' "$payload" | curl -sf --max-time 30 -X POST "$mcp_url" \
    -H "Content-Type: application/json" -H "Accept: application/json" -H "Connection: close" \
    ${auth[@]+"${auth[@]}"} \
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

# The ORRERY Mail row id from a register_agent reply (plain, structuredContent,
# or a JSON text block). Empty when the reply carries none. The id is the key
# of the session index, which is why a shell-side registration needs it.
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

ags_registration_token_project_file() {
  local token_file
  token_file="$(ags_registration_token_file "$1")" || return 1
  printf '%s.project\n' "$token_file"
}

ags_local_agent_name_key() {
  printf '%s' "$1" | LC_ALL=C tr -d '-' | LC_ALL=C tr '[:upper:]' '[:lower:]'
}

# Keep local identity metadata comparable with ORRERY Mail's canonical human
# key. Prefer the shared resolver when the caller already sourced it; the
# fallback is identical for this library's standalone consumers.
ags_normalize_project_key() {
  local project_key="${1:-}"
  if declare -F agentstack_normalize_project_key >/dev/null 2>&1; then
    agentstack_normalize_project_key "$project_key"
    return
  fi
  "${AGENTSTACK_PYTHON:-python3}" - "$project_key" <<'PY'
import pathlib
import sys

value = sys.argv[1]
if not value:
    print("")
    raise SystemExit(0)
path = pathlib.Path(value)
if path.is_absolute() or path.is_dir():
    print(path.resolve())
else:
    print(value)
PY
}

ags_project_keys_equal() {
  local left right
  left="$(ags_normalize_project_key "${1:-}")" || return 1
  right="$(ags_normalize_project_key "${2:-}")" || return 1
  [[ "$left" == "$right" ]]
}

# Local tokens, child state, tmux and watcher routing share a host-global name
# namespace even though Mail identities are project-local. Candidate/adoption
# paths therefore treat legacy or corrupt metadata as occupied. An exact
# reserved identity may provisionally reuse a legacy token: register_agent
# authenticates it before the metadata is upgraded.
ags_local_agent_name_conflicts() {
  local project_key="$1" agent_name="$2" mode="${3:-candidate}"
  local runtime_dir requested_key token_file local_name project_file recorded
  local state_file current_tmux
  [[ -n "$project_key" && -n "$agent_name" ]] || return 1
  project_key="$(ags_normalize_project_key "$project_key")" || return 0
  runtime_dir="$(ags_registration_runtime_dir)"
  requested_key="$(ags_local_agent_name_key "$agent_name")"

  state_file="$runtime_dir/name-bindings/$requested_key.json"
  if [[ -e "$state_file" || -L "$state_file" ]]; then
    recorded="$("${AGENTSTACK_PYTHON:-python3}" - "$state_file" "$requested_key" <<'PY' 2>/dev/null || true
import json
import sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    project = data.get("project_key")
    name = data.get("agent_name")
except Exception:
    project = name = None
name_key = name.replace("-", "").lower() if isinstance(name, str) else ""
if isinstance(project, str) and project and name_key == sys.argv[2]:
    print(project)
PY
)"
    [[ -n "$recorded" ]] || return 0
    if ! ags_project_keys_equal "$recorded" "$project_key" || \
       [[ "$mode" != "reserved" && "$mode" != "adopt" ]]; then
      return 0
    fi
  fi

  for token_file in "$runtime_dir"/agent_token_*; do
    [[ -e "$token_file" || -L "$token_file" ]] || continue
    case "$token_file" in *.project) continue ;; esac
    local_name="$(basename "$token_file")"
    local_name="${local_name#agent_token_}"
    [[ "$(ags_local_agent_name_key "$local_name")" == "$requested_key" ]] || continue
    [[ "$local_name" == "$agent_name" ]] || return 0
    project_file="$token_file.project"
    recorded=""
    if [[ -f "$project_file" && ! -L "$project_file" ]]; then
      IFS= read -r recorded < "$project_file" || true
    fi
    [[ -n "$recorded" ]] && ! ags_project_keys_equal "$recorded" "$project_key" && return 0
    if [[ "$mode" == "reserved" ]]; then
      continue
    fi
    [[ "$mode" == "adopt" ]] && ags_project_keys_equal "$recorded" "$project_key" || return 0
  done

  for state_file in "$runtime_dir"/child-agents/*.json; do
    [[ -e "$state_file" || -L "$state_file" ]] || continue
    local_name="$(basename "$state_file" .json)"
    [[ "$(ags_local_agent_name_key "$local_name")" == "$requested_key" ]] || continue
    [[ "$local_name" == "$agent_name" ]] || return 0
    recorded="$("${AGENTSTACK_PYTHON:-python3}" - "$state_file" <<'PY' 2>/dev/null || true
import json
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8")).get("project_key", "")
except Exception:
    value = ""
print(value if isinstance(value, str) else "")
PY
)"
    [[ -n "$recorded" ]] && ! ags_project_keys_equal "$recorded" "$project_key" && return 0
    if [[ "$mode" == "reserved" ]]; then
      continue
    fi
    [[ "$mode" == "adopt" ]] && ags_project_keys_equal "$recorded" "$project_key" || return 0
  done

  if command -v tmux >/dev/null 2>&1; then
    current_tmux="$(tmux display-message -p '#S' 2>/dev/null || true)"
    while IFS= read -r local_name; do
      [[ -n "$local_name" ]] || continue
      [[ "$(ags_local_agent_name_key "$local_name")" == "$requested_key" ]] || continue
      if [[ ( "$mode" == "reserved" || "$mode" == "first" ) \
            && "$local_name" == "$agent_name" && "$local_name" == "$current_tmux" ]]; then
        continue
      fi
      return 0
    done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null || true)
  fi
  return 1
}

# Atomically establish the durable separator-normalized local binding before
# remote registration. The creator removes it on failure and commits it after
# token/state persistence; concurrent projects cannot both pass this boundary.
ags_acquire_local_name_claim() {
  local project_key="$1" agent_name="$2" mode="${3:-candidate}"
  local runtime_dir key claim_root claim_file result
  key="$(ags_local_agent_name_key "$agent_name")"
  [[ -n "$project_key" && -n "$key" ]] || return 1
  project_key="$(ags_normalize_project_key "$project_key")" || return 1
  runtime_dir="$(ags_registration_runtime_dir)"
  claim_root="$runtime_dir/name-bindings"
  claim_file="$claim_root/$key.json"
  mkdir -p "$claim_root" || return 1
  chmod 700 "$claim_root" 2>/dev/null || true
  result="$("${AGENTSTACK_PYTHON:-python3}" - "$claim_file" "$project_key" "$agent_name" "$mode" <<'PY' 2>/dev/null || true
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
project_key, agent_name, mode = sys.argv[2:5]
payload = {"project_key": project_key, "agent_name": agent_name}

def normalize(value):
    if not isinstance(value, str) or not value:
        return ""
    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.is_dir():
        return str(candidate.resolve())
    return value

try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise SystemExit(1)
    existing_name = existing.get("agent_name")
    existing_key = (
        existing_name.replace("-", "").lower()
        if isinstance(existing_name, str) else ""
    )
    requested_key = agent_name.replace("-", "").lower()
    if (mode in {"reserved", "adopt"}
            and normalize(existing.get("project_key")) == normalize(project_key)
            and existing_key == requested_key):
        print("existing")
        raise SystemExit(0)
    raise SystemExit(1)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(path, 0o600)
print("created")
PY
)"
  [[ "$result" == "created" || "$result" == "existing" ]] || return 1
  AGS_LOCAL_NAME_CLAIM_FILE="$claim_file"
  AGS_LOCAL_NAME_CLAIM_KEY="$key"
  AGS_LOCAL_NAME_CLAIM_PROJECT="$project_key"
  AGS_LOCAL_NAME_CLAIM_NAME="$agent_name"
  AGS_LOCAL_NAME_CLAIM_CREATED=0
  [[ "$result" == "created" ]] && AGS_LOCAL_NAME_CLAIM_CREATED=1
  return 0
}

ags_release_local_name_claim() {
  local claim_file="${AGS_LOCAL_NAME_CLAIM_FILE:-}"
  if [[ "${AGS_LOCAL_NAME_CLAIM_CREATED:-0}" == "1" && -n "$claim_file" ]]; then
    "${AGENTSTACK_PYTHON:-python3}" - "$claim_file" \
      "${AGS_LOCAL_NAME_CLAIM_PROJECT:-}" "${AGS_LOCAL_NAME_CLAIM_KEY:-}" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])

def normalize(value):
    if not isinstance(value, str) or not value:
        return ""
    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.is_dir():
        return str(candidate.resolve())
    return value

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except FileNotFoundError:
    raise SystemExit(0)
except Exception:
    raise SystemExit(1)
name = data.get("agent_name")
key = name.replace("-", "").lower() if isinstance(name, str) else ""
if normalize(data.get("project_key")) != normalize(sys.argv[2]) or key != sys.argv[3]:
    raise SystemExit(1)
path.unlink()
PY
  fi
  AGS_LOCAL_NAME_CLAIM_FILE=""
  AGS_LOCAL_NAME_CLAIM_KEY=""
  AGS_LOCAL_NAME_CLAIM_PROJECT=""
  AGS_LOCAL_NAME_CLAIM_NAME=""
  AGS_LOCAL_NAME_CLAIM_CREATED=0
}

ags_update_local_name_claim() {
  local project_key="$1" agent_name="$2" claim_file="${AGS_LOCAL_NAME_CLAIM_FILE:-}"
  local old_key="${AGS_LOCAL_NAME_CLAIM_KEY:-}" new_key new_file result
  [[ -n "$claim_file" && -n "$project_key" && -n "$agent_name" && -n "$old_key" ]] || return 1
  project_key="$(ags_normalize_project_key "$project_key")" || return 1
  new_key="$(ags_local_agent_name_key "$agent_name")"
  [[ -n "$new_key" ]] || return 1
  new_file="$(ags_registration_runtime_dir)/name-bindings/$new_key.json"
  result="$("${AGENTSTACK_PYTHON:-python3}" - "$claim_file" "$new_file" \
    "$project_key" "$agent_name" "$old_key" "${AGS_LOCAL_NAME_CLAIM_CREATED:-0}" <<'PY' 2>/dev/null || true
import json
import os
import pathlib
import sys

old_path, new_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
project_key, agent_name, old_key, created = sys.argv[3:7]
new_key = agent_name.replace("-", "").lower()
data = json.loads(old_path.read_text(encoding="utf-8"))

def normalize(value):
    if not isinstance(value, str) or not value:
        return ""
    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.is_dir():
        return str(candidate.resolve())
    return value

old_name = data.get("agent_name")
existing_key = old_name.replace("-", "").lower() if isinstance(old_name, str) else ""
if normalize(data.get("project_key")) != normalize(project_key) or existing_key != old_key:
    raise SystemExit(1)
payload = {"project_key": project_key, "agent_name": agent_name}
if new_key != old_key:
    # A pre-existing adopted binding is durable ownership, not a temporary
    # claim we may move to a different comparison key.
    if created != "1":
        raise SystemExit(1)
    fd = os.open(new_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(new_path, 0o600)
    old_path.unlink()
    print("moved")
    raise SystemExit(0)
tmp = old_path.with_name(old_path.name + f".{os.getpid()}.tmp")
with open(tmp, "x", encoding="utf-8") as handle:
    json.dump(payload, handle, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, old_path)
print("updated")
PY
)"
  [[ "$result" == "updated" || "$result" == "moved" ]] || return 1
  AGS_LOCAL_NAME_CLAIM_FILE="$new_file"
  AGS_LOCAL_NAME_CLAIM_KEY="$new_key"
  AGS_LOCAL_NAME_CLAIM_PROJECT="$project_key"
  AGS_LOCAL_NAME_CLAIM_NAME="$agent_name"
  [[ "$result" == "moved" ]] && AGS_LOCAL_NAME_CLAIM_CREATED=1
  return 0
}

ags_commit_local_name_claim() {
  AGS_LOCAL_NAME_CLAIM_CREATED=0
  AGS_LOCAL_NAME_CLAIM_FILE=""
  AGS_LOCAL_NAME_CLAIM_KEY=""
  AGS_LOCAL_NAME_CLAIM_PROJECT=""
  AGS_LOCAL_NAME_CLAIM_NAME=""
}

ags_remove_local_name_binding() {
  local project_key="$1" agent_name="$2" runtime_dir key claim_file
  runtime_dir="$(ags_registration_runtime_dir)"
  key="$(ags_local_agent_name_key "$agent_name")"
  claim_file="$runtime_dir/name-bindings/$key.json"
  [[ -n "$project_key" && -n "$key" ]] || return 1
  project_key="$(ags_normalize_project_key "$project_key")" || return 1
  "${AGENTSTACK_PYTHON:-python3}" - "$claim_file" "$project_key" "$agent_name" <<'PY' 2>/dev/null
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])

def normalize(value):
    if not isinstance(value, str) or not value:
        return ""
    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.is_dir():
        return str(candidate.resolve())
    return value

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except FileNotFoundError:
    raise SystemExit(0)
except Exception:
    raise SystemExit(1)
name = data.get("agent_name")
requested_key = sys.argv[3].replace("-", "").lower()
name_key = name.replace("-", "").lower() if isinstance(name, str) else ""
if normalize(data.get("project_key")) != normalize(sys.argv[2]) or name_key != requested_key:
    raise SystemExit(1)
path.unlink()
PY
}

ags_validate_local_name_binding() {
  local project_key="$1" agent_name="$2" runtime_dir key claim_file
  runtime_dir="$(ags_registration_runtime_dir)"
  key="$(ags_local_agent_name_key "$agent_name")"
  claim_file="$runtime_dir/name-bindings/$key.json"
  [[ -n "$project_key" && -n "$key" && -f "$claim_file" && ! -L "$claim_file" ]] || return 1
  project_key="$(ags_normalize_project_key "$project_key")" || return 1
  "${AGENTSTACK_PYTHON:-python3}" - "$claim_file" "$project_key" "$key" <<'PY' 2>/dev/null
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))

def normalize(value):
    if not isinstance(value, str) or not value:
        return ""
    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.is_dir():
        return str(candidate.resolve())
    return value

name = data.get("agent_name")
name_key = name.replace("-", "").lower() if isinstance(name, str) else ""
if normalize(data.get("project_key")) != normalize(sys.argv[2]) or name_key != sys.argv[3]:
    raise SystemExit(1)
PY
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
  local agent_name="$1" registration_token="$2" project_key="${3:-}"
  local runtime_dir token_file project_file token_tmp project_tmp
  local claim_key acquired_claim=false
  [[ -n "$agent_name" && -n "$registration_token" ]] || return 0
  project_key="$(ags_normalize_project_key "$project_key")" || return 1
  claim_key="$(ags_local_agent_name_key "$agent_name")"
  if [[ "${AGS_LOCAL_NAME_CLAIM_KEY:-}" != "$claim_key" ]]; then
    ags_acquire_local_name_claim "$project_key" "$agent_name" reserved || return 1
    acquired_claim=true
  fi
  if ! ags_update_local_name_claim "$project_key" "$agent_name"; then
    ags_release_local_name_claim
    return 1
  fi
  if [[ -n "$project_key" ]] && ags_local_agent_name_conflicts "$project_key" "$agent_name" reserved; then
    echo "agentstack: local identity '$agent_name' already belongs to another project; refusing to overwrite its owner token." >&2
    ags_release_local_name_claim
    return 1
  fi
  runtime_dir="$(ags_registration_runtime_dir)"
  token_file="$(ags_registration_token_file "$agent_name")" || {
    ags_release_local_name_claim
    return 1
  }
  project_file="$(ags_registration_token_project_file "$agent_name")" || {
    ags_release_local_name_claim
    return 1
  }
  mkdir -p "$runtime_dir" || {
    ags_release_local_name_claim
    return 1
  }
  token_tmp="$token_file.tmp.$$"
  project_tmp="$project_file.tmp.$$"
  ( umask 077 && printf '%s' "$registration_token" > "$token_tmp" ) || {
    ags_release_local_name_claim
    return 1
  }
  if [[ -n "$project_key" ]]; then
    ( umask 077 && printf '%s\n' "$project_key" > "$project_tmp" ) || {
      rm -f "$token_tmp"
      ags_release_local_name_claim
      return 1
    }
  fi
  chmod 600 "$token_tmp" 2>/dev/null || true
  if [[ -n "$project_key" ]]; then
    chmod 600 "$project_tmp" 2>/dev/null || true
    mv -f "$project_tmp" "$project_file" || {
      rm -f "$token_tmp"
      ags_release_local_name_claim
      return 1
    }
  fi
  mv -f "$token_tmp" "$token_file" || {
    ags_release_local_name_claim
    return 1
  }
  ags_commit_local_name_claim
  return 0
}

# Record that ORRERY Mail granted a different identity than the one requested.
# The dashboard reads this file and says so on the agent, because the only
# other trace is a missing portrait — which reads as a style, not a fault.
# Best effort: a spawn that otherwise worked must not fail over bookkeeping.
ags_record_name_substitution() {
  local registered="$1" requested="$2"
  local project_key="${3:-${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}}"
  local runtime_dir store
  [[ -n "$registered" && -n "$requested" && "$registered" != "$requested" ]] || return 0
  [[ -n "$project_key" ]] || return 1
  project_key="$(ags_normalize_project_key "$project_key")" || return 1
  runtime_dir="$(ags_registration_runtime_dir)"
  store="$runtime_dir/name-substitutions.json"
  mkdir -p "$runtime_dir" || return 1
  "${AGENTSTACK_PYTHON:-python3}" - "$store" "$registered" "$requested" "$project_key" <<'PY' || return 1
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

store, registered, requested, project_key = (
    pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
)
try:
    data = json.loads(store.read_text(encoding="utf-8"))
except (OSError, ValueError):
    data = {}
if not isinstance(data, dict):
    data = {}
projects = data.get("projects")
if not isinstance(projects, dict):
    projects = {}
    for legacy_name, entry in data.items():
        if not isinstance(legacy_name, str) or not isinstance(entry, dict):
            continue
        owner = entry.get("project_key")
        if isinstance(owner, str) and owner:
            owner_store = projects.setdefault(owner, {})
            if isinstance(owner_store, dict):
                owner_store[legacy_name] = entry
project_store = projects.setdefault(project_key, {})
if not isinstance(project_store, dict):
    project_store = {}
    projects[project_key] = project_store
project_store[registered] = {
    "requested": requested,
    "project_key": project_key,
    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
data["projects"] = projects
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
    closed)
      policy="contacts_only"
      ;;
    skip|none|off|disabled)
      return 0
      ;;
  esac
  # Try WITH the owner token first: legacy deployments gate set_contact_policy
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
  project_key="$(ags_normalize_project_key "$project_key")" || {
    printf 'unknown\n'
    return 0
  }
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
      if ! ags_local_agent_name_conflicts "$project_key" "$preferred_name"; then
        ags_note_scientist_used "$preferred_name" || true
        printf '%s\n' "$preferred_name"
        return 0
      fi
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
        if ags_local_agent_name_conflicts "$project_key" "$candidate"; then
          unknowns=0
          continue
        fi
        # Record only on the claim: the loop discards candidates, and recording
        # those would burn through the roster with surnames nobody is using.
        ags_note_scientist_used "$candidate" || true
        printf '%s\n' "$candidate"; return 0 ;;
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
      available)
        if ags_local_agent_name_conflicts "$project_key" "$candidate"; then
          unknowns=0
          continue
        fi
        # Record only on the claim: the loop discards candidates, and recording
        # those would burn through the roster with surnames nobody is using.
        ags_note_scientist_used "$candidate" || true
        printf '%s\n' "$candidate"; return 0 ;;
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
  project_key="$(ags_normalize_project_key "$project_key")" || return 1
  AGS_REGISTERED_AGENT_NAME=""
  AGS_REGISTERED_AGENT_ID=""
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

  local registration_token=""
  local local_check_mode="$requested_mode"
  if [[ "$requested_mode" == "reserved" && -n "$requested_name" ]]; then
    registration_token="${CHILD_REGISTRATION_TOKEN:-}"
    if [[ -z "$registration_token" ]]; then
      registration_token="$(ags_load_registration_token "$agent_name" 2>/dev/null || true)"
    fi
    if [[ -n "$registration_token" ]]; then
      local reserved_status
      reserved_status="$(ags_agent_name_status "$project_key" "$agent_name")"
      if [[ "$reserved_status" != "occupied" ]]; then
        echo "agentstack: legacy/reserved identity '$agent_name' is not verified in project '$project_key'; refusing to create or migrate it." >&2
        return 1
      fi
    else
      # A preselected top-level name with no owner state may be created when
      # Mail positively reports it free. Any local artifact makes that reuse
      # ambiguous and is therefore treated like a fresh candidate collision.
      local reserved_status
      reserved_status="$(ags_agent_name_status "$project_key" "$agent_name")"
      if [[ "$reserved_status" != "available" ]]; then
        echo "agentstack: unowned reserved identity '$agent_name' is not positively free in project '$project_key'; refusing registration." >&2
        return 1
      fi
      registration_token="$(ags_generate_registration_token)" || return 1
      local_check_mode="first"
    fi
  fi

  if ags_local_agent_name_conflicts "$project_key" "$agent_name" "$local_check_mode"; then
    echo "agentstack: local identity '$agent_name' is already bound to another project; refusing cross-project credential reuse." >&2
    return 1
  fi
  if ! ags_acquire_local_name_claim "$project_key" "$agent_name" "$local_check_mode"; then
    echo "agentstack: local identity '$agent_name' was claimed concurrently; refusing registration." >&2
    return 1
  fi

  ags_mcp_call "ensure_project" "human_key=$project_key" >/dev/null

  # Ambient owner credentials are valid only for an explicitly verified
  # reserved identity. Candidate/top-level registration must not adopt a token
  # inherited from another tmux session.
  # Mint a fresh owner token only for a name the server positively reports as
  # free. An 'unknown' answer must not mint one: that is how an unverified name
  # used to get claimed on top of a live agent.
  if [[ -z "$registration_token" ]] && ags_agent_name_available "$project_key" "$agent_name"; then
    registration_token="$(ags_generate_registration_token)" || {
      ags_release_local_name_claim
      return 1
    }
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
  result="$(ags_mcp_call "register_agent" "${register_args[@]}")" || {
    ags_release_local_name_claim
    return 1
  }
  if printf '%s' "$result" | ags_mcp_has_error; then
    ags_release_local_name_claim
    return 1
  fi
  registered="$(printf '%s' "$result" | ags_extract_agent_name)"
  if [[ -z "$registered" ]]; then
    ags_release_local_name_claim
    return 1
  fi
  AGS_SERVER_RETURNED_AGENT_NAME="$registered"
  if [[ "$registered" != "$agent_name" ]]; then
    AGS_AGENT_NAME_SUBSTITUTED=1
    # A reserved identity already has a parent task, owner token, tmux metadata,
    # and inbox addressed to the requested name. Adopting a replacement here
    # would strand all of those under the old name. Candidate/top-level launch
    # paths may reconcile their not-yet-started tmux session to the read-back,
    # but an existing identity must fail closed.
    if [[ "$requested_mode" == "reserved" ]]; then
      ags_release_local_name_claim
      return 2
    fi
  fi
  registered_token="$(printf '%s' "$result" | ags_extract_registration_token)"
  [[ -n "$registered_token" ]] || registered_token="$registration_token"
  if [[ -n "$registered_token" ]]; then
    CHILD_REGISTRATION_TOKEN="$registered_token"
    AGS_REGISTERED_REGISTRATION_TOKEN="$registered_token"
    export CHILD_REGISTRATION_TOKEN
    ags_store_registration_token "$registered" "$registered_token" "$project_key" || {
      ags_release_local_name_claim
      return 1
    }
    ags_apply_contact_policy "$project_key" "$registered" "$registered_token"
  fi
  ags_commit_local_name_claim
  AGS_REGISTERED_AGENT_NAME="$registered"
  AGS_REGISTERED_AGENT_ID="$(printf '%s' "$result" | ags_extract_agent_id)"
  printf '%s\n' "$registered"
}

ags_start_mail_watcher() {
  local tmux_bin="$1" hooks_dir="$2"
  local watcher_session="${AGENTSTACK_MAIL_WATCHER_SESSION:-mail-watcher}"
  [[ -n "$tmux_bin" && -n "$hooks_dir" && -f "$hooks_dir/watch_agent_mail_signals.sh" ]] || return 0
  # The installer now runs the watcher as a launchd / systemd service. When that
  # (or any other) watcher holds the single-instance lock, a tmux copy would
  # only start, print "duplicate", and exit — so this is a fallback, not the
  # primary path.
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
