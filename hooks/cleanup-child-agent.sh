#!/bin/bash
set -euo pipefail

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}"
STATE_DIR="$RUNTIME_DIR/child-agents"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$HOME/.claude/managed_agents.txt}"
PROJECT_KEY_DEFAULT="${AGENTSTACK_PROJECT_KEY:-}"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:8765/mcp}}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/mcp_agent_mail/.env}"

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
if [[ -f "$STATE_FILE" ]]; then
    STATE_PROJECT_KEY=$(python3 -c "
	import json, sys
	data = json.load(open(sys.argv[1], encoding='utf-8'))
print(data.get('project_key', ''))
" "$STATE_FILE" 2>/dev/null || true)
    if [[ -n "$STATE_PROJECT_KEY" ]]; then
        PROJECT_KEY="$STATE_PROJECT_KEY"
    fi
fi

if [[ ! -s "$TOKEN_FILE" && ! -s "$STATE_FILE" && -z "${CHILD_REGISTRATION_TOKEN:-}" ]]; then
    exit 0
fi

if [[ -z "$PROJECT_KEY" ]]; then
    exit 0
fi

TOKEN=$(get_agentstack_token 2>/dev/null || true)
if [[ -z "$TOKEN" ]]; then
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
conn.request("POST", parsed.path, body=payload, headers={
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Authorization": f"Bearer {token}",
    "Connection": "close",
})
resp = conn.getresponse()
print(resp.read().decode())
conn.close()
' "$method" "$MCP_URL"
}

release_args=$(python3 -c "
import json, sys
print(json.dumps({
    'project_key': sys.argv[1],
    'agent_name': sys.argv[2],
}))
" "$PROJECT_KEY" "$AGENT_NAME")
call_mcp "release_file_reservations" "$release_args" > /dev/null 2>&1 || true

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

python3 - "$MANAGED_FILE" "$AGENT_NAME" <<'PYEOF' 2>/dev/null || true
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except OSError:
    raise SystemExit(0)
path.write_text("\n".join(line for line in lines if line != name) + "\n", encoding="utf-8")
PYEOF
rm -f "$STATE_FILE" "$TOKEN_FILE" "$MCP_CONFIG_FILE"
rm -rf "$CODEX_HOME_DIR"

exit 0
