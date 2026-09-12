#!/bin/bash
set -euo pipefail

# A SessionEnd hook may run outside the child process and therefore inherit no
# identity. Capture the launcher's claim before the resolver can infer anything:
# cleanup may retire only an agent named explicitly by the caller or its entry
# environment, never one guessed from surrounding pane/session state.
AGENT_NAME_ENV_AT_ENTRY="${AGENT_NAME:-}"
PROJECT_KEY_ENV_AT_ENTRY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
if [[ -z "${1:-}" && -z "$AGENT_NAME_ENV_AT_ENTRY" ]]; then
    exit 0
fi

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
PROJECT_CONTEXT_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/project-context.sh"
# shellcheck disable=SC1090
. "$PROJECT_CONTEXT_LIB"
if [[ -n "$PROJECT_KEY_ENV_AT_ENTRY" ]]; then
    PROJECT_KEY_ENV_AT_ENTRY="$(agentstack_normalize_project_key "$PROJECT_KEY_ENV_AT_ENTRY")"
fi
STATE_DIR="$RUNTIME_DIR/child-agents"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
PROJECT_KEY_DEFAULT="$(agentstack_resolve_project_key "$(pwd -P)")"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:18765/mcp}}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/.agentstack/mail/.env}"
HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"

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
PROJECT_KEY="${PROJECT_KEY_ENV_AT_ENTRY:-$PROJECT_KEY_DEFAULT}"

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
TOKEN_PROJECT_FILE="$TOKEN_FILE.project"
MCP_CONFIG_FILE="$STATE_DIR/${AGENT_NAME}.mcp.json"
CODEX_HOME_DIR="$STATE_DIR/${AGENT_NAME}.codex-home"
if [[ -f "$STATE_FILE" ]]; then
    if ! STATE_PROJECT_KEY=$(
        python3 - "$STATE_FILE" 2>/dev/null <<'PYEOF'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("project_key", ""))
PYEOF
    ); then
        echo "[cleanup-child-agent] could not read child state for '$AGENT_NAME'; refusing partial cleanup" >&2
        exit 1
    fi
    if [[ -n "$STATE_PROJECT_KEY" ]]; then
        STATE_PROJECT_KEY="$(agentstack_normalize_project_key "$STATE_PROJECT_KEY")"
        if [[ -n "$PROJECT_KEY_ENV_AT_ENTRY" && "$PROJECT_KEY_ENV_AT_ENTRY" != "$STATE_PROJECT_KEY" ]]; then
            echo "[cleanup-child-agent] child state project mismatch for '$AGENT_NAME'; refusing cleanup" >&2
            exit 1
        fi
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

# Prove ownership of every host-global name-keyed artifact before performing
# remote release/retire calls or deleting anything. Keep the normalized name
# binding in place until all other artifacts are gone so another project cannot
# claim the name while this cleanup is still running.
OWNERSHIP_PROVEN=0
if [[ -n "${STATE_PROJECT_KEY:-}" ]]; then
    OWNERSHIP_PROVEN=1
fi
if [[ -f "$TOKEN_PROJECT_FILE" ]]; then
    IFS= read -r TOKEN_PROJECT < "$TOKEN_PROJECT_FILE" || TOKEN_PROJECT=""
    if [[ -n "$TOKEN_PROJECT" ]]; then
        TOKEN_PROJECT="$(agentstack_normalize_project_key "$TOKEN_PROJECT")"
    fi
    if [[ "$TOKEN_PROJECT" != "$PROJECT_KEY" ]]; then
        echo "[cleanup-child-agent] token project metadata mismatch for '$AGENT_NAME'; refusing cleanup" >&2
        exit 1
    fi
    OWNERSHIP_PROVEN=1
fi
BINDING_KEY="$(printf '%s' "$AGENT_NAME" | LC_ALL=C tr -d '-' | LC_ALL=C tr '[:upper:]' '[:lower:]')"
BINDING_FILE="$RUNTIME_DIR/name-bindings/$BINDING_KEY.json"
if [[ -e "$BINDING_FILE" ]]; then
    if ! python3 - "$BINDING_FILE" "$PROJECT_KEY" "$BINDING_KEY" <<'PYEOF'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))

def project_key(value):
    if not isinstance(value, str) or not value:
        return ""
    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.is_dir():
        return str(candidate.resolve())
    return value

bound_name = str(data.get("agent_name") or "")
bound_key = bound_name.replace("-", "").lower()
if project_key(data.get("project_key")) != project_key(sys.argv[2]) or bound_key != sys.argv[3]:
    raise SystemExit(1)
PYEOF
    then
        echo "[cleanup-child-agent] local name binding mismatch for '$AGENT_NAME'; refusing cleanup" >&2
        exit 1
    fi
    OWNERSHIP_PROVEN=1
fi
if [[ "$OWNERSHIP_PROVEN" != "1" ]]; then
    echo "[cleanup-child-agent] no project ownership metadata for '$AGENT_NAME'; refusing credential cleanup" >&2
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
rm -f "$STATE_FILE" "$TOKEN_FILE" "$TOKEN_PROJECT_FILE" "$MCP_CONFIG_FILE"
rm -rf "$CODEX_HOME_DIR"

# Release the comparison-key claim last. Revalidate immediately before unlink
# so an operator replacement can never be removed by this cleanup.
if [[ -e "$BINDING_FILE" ]]; then
    python3 - "$BINDING_FILE" "$PROJECT_KEY" "$BINDING_KEY" <<'PYEOF' || {
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))

def project_key(value):
    if not isinstance(value, str) or not value:
        return ""
    candidate = pathlib.Path(value)
    if candidate.is_absolute() or candidate.is_dir():
        return str(candidate.resolve())
    return value

bound_name = str(data.get("agent_name") or "")
if project_key(data.get("project_key")) != project_key(sys.argv[2]) or bound_name.replace("-", "").lower() != sys.argv[3]:
    raise SystemExit(1)
path.unlink()
PYEOF
        echo "[cleanup-child-agent] local name binding changed during cleanup for '$AGENT_NAME'; leaving it intact" >&2
        exit 1
    }
fi

exit 0
