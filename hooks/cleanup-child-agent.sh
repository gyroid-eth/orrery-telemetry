#!/bin/bash

# Decide explicit global context before loading any legacy environment/state.
_ags_global_loader="$(cd "$(dirname "${BASH_SOURCE[0]}")/../bin/lib" && pwd)/global-client-loader.sh"
. "$_ags_global_loader"
_ags_legacy_consumer="$(dirname "$_ags_global_loader")/legacy-tmux-consumer.sh"
if [[ -f "$_ags_legacy_consumer" ]]; then . "$_ags_legacy_consumer"; fi || exit 2
if declare -F ags_client_entry >/dev/null; then
  if ags_client_entry end-session "$@"; then exit 0; else _ags_global_rc=$?; fi
  if [[ "$_ags_global_rc" != 125 ]]; then
    exit "$_ags_global_rc"
  fi

fi

# Conversation-only resume must not touch ORRERY Mail or owner material.
if [[ "${AGENTSTACK_MAIL_DISABLED:-0}" == "1" ]]; then
    exit 0
fi
set -euo pipefail

# A SessionEnd hook may run outside the child process and therefore inherit no
# identity. Capture the launcher's claim before the resolver can infer anything:
# cleanup may retire only an agent named explicitly by the caller or its entry
# environment, never one guessed from surrounding pane/session state.
AGENT_NAME_ENV_AT_ENTRY="${AGENT_NAME:-}"
if [[ -z "${1:-}" && -z "$AGENT_NAME_ENV_AT_ENTRY" ]]; then
    exit 0
fi

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
PROJECT_CONTEXT_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/project-context.sh"
# shellcheck disable=SC1090
. "$PROJECT_CONTEXT_LIB"
STATE_DIR="$RUNTIME_DIR/child-agents"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
PROJECT_KEY_DEFAULT="$(agentstack_resolve_project_key "$(pwd -P)")"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:18765/mcp}}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/.agentstack/mail/.env}"
HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
CHILD_RESUME_RETENTION_DAYS="${AGENTSTACK_CHILD_RESUME_RETENTION_DAYS:-30}"

resolve_agent_name() {
    if [[ -f "$HOOKS_DIR/resolve-agent-name.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOOKS_DIR/resolve-agent-name.sh"
        printf '%s\n' "${RESOLVED_AGENT:-}"
        return 0
    fi
    if [[ -n "${AGENT_NAME:-}" ]]; then
        printf '%s\n' "$AGENT_NAME"
        return 0
    fi
    if [[ -n "${TMUX_PANE:-}" ]]; then
        local pane_key metadata_file
        pane_key="${TMUX_PANE//%/_}"
        metadata_file="$RUNTIME_DIR/agent_name_${pane_key}"
        if [[ -f "$metadata_file" ]]; then
            tr -d '[:space:]' < "$metadata_file" 2>/dev/null
        fi
    fi
}

get_agentstack_token() {
    if [[ -n "${MCP_AGENT_MAIL_TOKEN:-}" ]]; then
        printf '%s' "$MCP_AGENT_MAIL_TOKEN"
        return 0
    fi
    if [[ -x "$HOOKS_DIR/get-mcp-agent-mail-token.sh" ]]; then
        bash "$HOOKS_DIR/get-mcp-agent-mail-token.sh" 2>/dev/null && return 0
    fi
    if command -v security >/dev/null 2>&1; then
        local keychain_token
        keychain_token=$(security find-generic-password -s "mcp-agent-mail" -a "HTTP_BEARER_TOKEN" -w 2>/dev/null || true)
        if [[ -n "$keychain_token" ]]; then
            printf '%s' "$keychain_token"
            return 0
        fi
    fi
    if [[ -f "$MAIL_ENV" ]]; then
        sed -n 's/^HTTP_BEARER_TOKEN=//p' "$MAIL_ENV" | tr -d '[:space:]'
        return 0
    fi
    return 1
}

legacy_http_bearer_enabled() {
    case "$HTTP_BEARER_MODE" in
        enabled|auto) return 0 ;;
        disabled) return 1 ;;
        *) return 2 ;;
    esac
}

RESOLVED_AGENT="$(resolve_agent_name)"
AGENT_NAME="${1:-${RESOLVED_AGENT:-${AGENT_NAME:-}}}"
PROJECT_KEY="${PROJECT_KEY:-$PROJECT_KEY_DEFAULT}"

if [[ -z "$AGENT_NAME" ]]; then
    exit 0
fi
if [[ ! "$AGENT_NAME" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    # State/config paths below are keyed by the exact register_agent read-back.
    # Reject path separators instead of normalizing an API identity.
    exit 0
fi

STATE_FILE="$STATE_DIR/${AGENT_NAME}.json"
TOKEN_KEY="$(printf '%s' "$AGENT_NAME" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '_')"
TOKEN_FILE="$RUNTIME_DIR/agent_token_$TOKEN_KEY"
MCP_CONFIG_FILE="$STATE_DIR/${AGENT_NAME}.mcp.json"
CODEX_HOME_DIR="$STATE_DIR/${AGENT_NAME}.codex-home"
REGISTRATION_PENDING="$STATE_DIR/.${AGENT_NAME}.registration-pending.json"
if [[ -e "$STATE_FILE" || -L "$STATE_FILE" || -e "$REGISTRATION_PENDING" || -L "$REGISTRATION_PENDING" ]]; then
    if ! STATE_PROJECT_KEY=$(
        python3 - "$STATE_FILE" "$REGISTRATION_PENDING" 2>/dev/null <<'PYEOF'
import json
import os
import stat
import sys

def read(path):
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:return {}
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_size>1048576:
            raise ValueError()
        value=json.loads(os.read(fd,1048577).decode('utf-8'))
        if type(value) is not dict:raise ValueError()
        return value
    finally:os.close(fd)
data=read(sys.argv[1]);pending=read(sys.argv[2])
if ('kind' in pending or any(key in data for key in
        ('launch_intent','local_registration','local_execution','local_resolution'))):
    raise SystemExit(2)
print(data.get("project_key", ""))
PYEOF
    ); then
        echo "[cleanup-child-agent] could not read child state for '$AGENT_NAME'; refusing partial cleanup" >&2
        exit 1
    fi
    if [[ -n "$STATE_PROJECT_KEY" ]]; then
        PROJECT_KEY="$STATE_PROJECT_KEY"
    else
        echo "[cleanup-child-agent] child state for '$AGENT_NAME' has no project key; using the configured project key if available" >&2
    fi
fi

if [[ ! -s "$TOKEN_FILE" && ! -s "$STATE_FILE" && -z "${CHILD_REGISTRATION_TOKEN:-}" ]]; then
    exit 0
fi

if [[ -z "$PROJECT_KEY" ]]; then
    echo "[cleanup-child-agent] project key is unavailable for '$AGENT_NAME'; leaving child state intact" >&2
    exit 1
fi

if legacy_http_bearer_enabled; then
    TOKEN=$(get_agentstack_token 2>/dev/null || true)
    bearer_status=0
else
    bearer_status=$?
    TOKEN=""
fi
if [[ "$bearer_status" == "2" ]]; then
    exit 0
fi
if [[ "$bearer_status" == "0" && -z "$TOKEN" ]]; then
    exit 0
fi

call_mcp() {
    local method="$1"
    local args_json="$2"
    printf '%s\0%s' "$args_json" "$TOKEN" | python3 -c '
import json
import sys
import http.client
from urllib.parse import urlparse

method = sys.argv[1]
url = sys.argv[2]
args_raw, token = sys.stdin.buffer.read().split(b"\0", 1)
args = json.loads(args_raw)
token = token.decode("utf-8")

parsed = urlparse(url)
payload = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": method, "arguments": args},
}).encode()

conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Connection": "close",
}
if token:
    headers["Authorization"] = f"Bearer {token}"
conn.request("POST", parsed.path, body=payload, headers=headers)
resp = conn.getresponse()
print(resp.read().decode())
conn.close()
' "$method" "$MCP_URL"
}

# Is this identity still live in another session? Only the retire-only exit of
# a resumed top-level Claude asks: exiting one copy must not release the
# reservations of, or retire, a copy still in use (#143 review). Evidence is a
# live tmux pane other than ours whose pane metadata names the agent, or a
# SessionStart lease whose provider PID is still running (ours has exited).
# When tmux cannot be read, the answer is unknown: keep the identity.
identity_live_elsewhere() {
    local name="$1" panes pane key lease pid
    if panes="$(tmux list-panes -a -F '#{pane_id}' 2>/dev/null)"; then
        while IFS= read -r pane; do
            [[ -n "$pane" && "$pane" != "${TMUX_PANE:-}" ]] || continue
            key="${pane//%/_}"
            if [[ -f "$RUNTIME_DIR/agent_name_${key}" ]] &&
                [[ "$(tr -d '[:space:]' < "$RUNTIME_DIR/agent_name_${key}" 2>/dev/null)" == "$name" ]]; then
                return 0
            fi
        done <<< "$panes"
    else
        return 0
    fi
    # A lease written before this machine booted names a PID that may now be
    # another process; child_resume.py ignores those (162nd seminar: after a
    # forced shutdown, stale leases made identities look alive).
    local helper="${AGENTSTACK_CHILD_RESUME_HELPER:-$HOOKS_DIR/child_resume.py}" rc=2
    if [[ -f "$helper" ]]; then
        "${AGENTSTACK_PYTHON:-$(agentstack_installed_env_value AGENTSTACK_PYTHON 2>/dev/null || true)}" "$helper" live-lease --runtime-dir "$RUNTIME_DIR" \
            --agent-name "$name" >/dev/null 2>&1 && rc=0 || rc=$?
        case "$rc" in
            0) return 0 ;;
            1) return 1 ;;
        esac
    fi
    for lease in "$RUNTIME_DIR/live-sessions/$name"/*; do
        [[ -f "$lease" ]] || continue
        pid="${lease##*/}"
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$lease"
    done
    return 1
}

if [[ "${AGENTSTACK_CLEANUP_RETIRE_ONLY:-0}" == "1" ]]; then
    if identity_live_elsewhere "$AGENT_NAME"; then
        echo "[cleanup-child-agent] '$AGENT_NAME' is still live in another session; leaving its Mail row and reservations as they are" >&2
        exit 0
    fi
fi

# Reservations of a retire-only exit belong to its SessionEnd hook.
if [[ "${AGENTSTACK_CLEANUP_RETIRE_ONLY:-0}" != "1" ]]; then
release_args=$(python3 -c "
import json, sys
print(json.dumps({
    'project_key': sys.argv[1],
    'agent_name': sys.argv[2],
}))
" "$PROJECT_KEY" "$AGENT_NAME")
call_mcp "release_file_reservations" "$release_args" > /dev/null 2>&1 || true
fi

retire_args=$(python3 -c '
import json
import os
import pathlib
import sys

project_key, agent_name, token_file, state_file = sys.argv[1:5]
token = ""
if pathlib.Path(token_file).is_file():
    token = pathlib.Path(token_file).read_text(encoding="utf-8").strip()
elif pathlib.Path(state_file).is_file():
    token = json.loads(
        pathlib.Path(state_file).read_text(encoding="utf-8")
    ).get("registration_token", "")
else:
    token = os.environ.get("CHILD_REGISTRATION_TOKEN", "")
if not token:
    raise SystemExit(1)
print(json.dumps({
    "project_key": project_key,
    "agent_name": agent_name,
    "registration_token": token,
}))
' "$PROJECT_KEY" "$AGENT_NAME" "$TOKEN_FILE" "$STATE_FILE") || retire_args=""
if [[ -n "$retire_args" ]]; then
    call_mcp "retire_agent" "$retire_args" > /dev/null 2>&1 || true
fi

# A resumed top-level Claude (owner token, no child state) only gives back the
# retirement its resume undid. Its token is not child material to retain or
# delete, and it was never in the managed list.
if [[ "${AGENTSTACK_CLEANUP_RETIRE_ONLY:-0}" == "1" ]]; then
    exit 0
fi

# All local cleanup is one name-locked transaction, after remote effects.
# In particular, do not remove managed/config/home files before rechecking a
# namespace which could have changed while retire was in flight.
case "$CHILD_RESUME_RETENTION_DAYS" in
    ''|*[!0-9]*)
        echo "[cleanup-child-agent] invalid AGENTSTACK_CHILD_RESUME_RETENTION_DAYS; using 30" >&2
        CHILD_RESUME_RETENTION_DAYS=30
        ;;
esac
RESUME_HELPER="${AGENTSTACK_CHILD_RESUME_HELPER:-$HOOKS_DIR/child_resume.py}"
LOCAL_FINALIZER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/child_resume.py"
finalize_args=(cleanup-ordinary --runtime-dir "$RUNTIME_DIR" --agent-name "$AGENT_NAME"
    --managed-file "$MANAGED_FILE" --retention-days "$CHILD_RESUME_RETENTION_DAYS")
if [[ ! -f "$RESUME_HELPER" ]]; then
    # Historical installations without resume retention fully delete ordinary
    # material; the fixed transaction still protects any new local namespace.
    finalize_args+=(--retention-action delete)
elif [[ "$RESUME_HELPER" != "$LOCAL_FINALIZER" ]]; then
    # Preserve explicit legacy custom helper selection OUTSIDE the name lock.
    if RETENTION_ACTION=$(
        "${AGENTSTACK_PYTHON:-python3}" "$RESUME_HELPER" mark-retired \
            --runtime-dir "$RUNTIME_DIR" \
            --agent-name "$AGENT_NAME" \
            --retention-days "$CHILD_RESUME_RETENTION_DAYS"
    ); then
        if [[ "$RETENTION_ACTION" != retained ]]; then RETENTION_ACTION=delete; fi
        finalize_args+=(--retention-action "$RETENTION_ACTION")
    else
        echo "[cleanup-child-agent] could not finalize retained resume state for '$AGENT_NAME'; leaving private state intact for explicit recovery or purge" >&2
        exit 1
    fi
fi
"${AGENTSTACK_PYTHON:-python3}" "$LOCAL_FINALIZER" "${finalize_args[@]}" || exit 1

exit 0
