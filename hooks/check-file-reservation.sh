#!/bin/bash
# check-file-reservation.sh
# PreToolUse hook: ensures agent has a file reservation before editing files
# under configured protected roots.
# Exit 2 = block, 0 = allow

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-}"
if [[ -z "$PROTECTED_ROOTS" && -n "$PROJECT_KEY" ]]; then
    PROTECTED_ROOTS="$PROJECT_KEY"
fi
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:8765/mcp}}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/mcp_agent_mail/.env}"
RENEW_SECONDS="${FILE_RESERVATION_RENEW_SECONDS:-900}"

expand_path() {
    local p="$1"
    if [[ "$p" == "~/"* ]]; then
        printf '%s\n' "$HOME/${p:2}"
    else
        printf '%s\n' "$p"
    fi
}

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
            return 0
        fi
    fi
    return 0
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

# The bearer above is the server-wide HTTP_BEARER_TOKEN, which authorizes the
# HTTP transport but does NOT prove ownership of a specific agent. Stock
# mcp-agent-mail is token-strict for existing names: renew_file_reservations /
# file_reservation_paths require the agent's own registration_token as a *tool
# argument* (the short-lived HTTP session this hook opens is never authenticated
# as the agent). Without it, renew returns renewed=0 and this hook blocks a
# Write/Edit even though the agent holds a valid reservation. Resolve the
# per-agent token from the same runtime files the launchers write.
get_agent_registration_token() {
    local name="$1" key state_file token_file
    [[ -n "$name" ]] || return 0
    # Delegated children store their token in child-agents/<name>.json.
    state_file="$RUNTIME_DIR/child-agents/$name.json"
    if [[ -f "$state_file" ]]; then
        python3 - "$state_file" <<'PY' 2>/dev/null && return 0
import json, sys
try:
    t = json.load(open(sys.argv[1], encoding="utf-8")).get("registration_token")
except Exception:
    t = None
if isinstance(t, str) and t:
    print(t)
PY
    fi
    # Top-level sessions store it in agent_token_<sanitized-name>.
    key="$(printf '%s' "$name" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '_')"
    token_file="$RUNTIME_DIR/agent_token_$key"
    if [[ -f "$token_file" ]]; then
        tr -d '[:space:]' < "$token_file" 2>/dev/null
        return 0
    fi
    return 0
}

# Read tool input JSON from stdin
TOOL_INPUT=$(cat)

# Extract file_path
FILE_PATH=$(printf '%s' "$TOOL_INPUT" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {})
    print(ti.get('file_path', ti.get('path', '')))
except:
    print('')
" 2>/dev/null || echo "")

[ -z "$FILE_PATH" ] && exit 0

# Normalize to absolute path
# Note: case patterns expand ~ (e.g., ~/* becomes /Users/foo/*), so use if/elif instead
if [[ "$FILE_PATH" == /* ]]; then
    : # already absolute
elif [[ "$FILE_PATH" == "~/"* ]]; then
    FILE_PATH="$HOME/${FILE_PATH:2}"
else
    FILE_PATH="$(pwd)/$FILE_PATH"
fi

# Only check files inside configured protected roots. If no protected roots are
# configured, fail open so unrelated edits are never blocked by installation.
MATCHED_ROOT=""
if [[ -n "$PROTECTED_ROOTS" ]]; then
    OLD_IFS="$IFS"
    IFS=":"
    for root in $PROTECTED_ROOTS; do
        root="$(expand_path "$root")"
        [[ -z "$root" ]] && continue
        [[ "$root" != "/" ]] && root="${root%/}"
        case "$FILE_PATH" in
            "$root"|"$root/"*)
                MATCHED_ROOT="$root"
                break
                ;;
        esac
    done
    IFS="$OLD_IFS"
fi

[ -z "$MATCHED_ROOT" ] && exit 0

# Convert to protected-root-relative path (reservations are stored as relative paths)
REL_PATH="${FILE_PATH#$MATCHED_ROOT/}"
if [[ "$REL_PATH" == "$FILE_PATH" ]]; then
    REL_PATH="$(basename "$FILE_PATH")"
fi
RESERVATION_PROJECT_KEY="${PROJECT_KEY:-$MATCHED_ROOT}"

# Get agent name via shared resolver or local fallback.
AGENT="$(resolve_agent_name)"
[ -z "$AGENT" ] && exit 0  # Agent name unknown — fail open (allow the write)

# Block unregistered agents (pending-* = session startup not completed)
case "$AGENT" in
    pending-*)
        echo "SESSION SETUP INCOMPLETE: agent '$AGENT' has not completed startup." >&2
        echo "Complete the session startup procedure in CLAUDE.md first:" >&2
        echo "  1. register_agent (new unique name - NEVER reuse an existing agent's name)" >&2
        echo "  2. rename the tmux session to the registered agent name" >&2
        echo "  3. Then acquire file reservations via macro_file_reservation_cycle" >&2
        exit 2
        ;;
esac

# Get bearer token via env, optional helper, Keychain, or .env.
TOKEN=$(get_agentstack_token 2>/dev/null || true)
[ -z "$TOKEN" ] && exit 0

# Resolve this agent's own registration_token (tool argument) so token-strict
# servers accept the renew/acquire calls below. Empty on lenient servers or when
# no runtime token exists — the calls then behave exactly as before.
REG_TOKEN="$(get_agent_registration_token "$AGENT" 2>/dev/null || true)"

# Check existing reservation via renew_file_reservations (read-only: only extends existing ones)
# If renewed=0, the agent has no pre-existing reservation for this path → block
# Pass both relative and absolute paths to handle reservations stored in either format
ABS_PATH="$FILE_PATH"
RESPONSE=$(QUERY_PROJECT_KEY="$RESERVATION_PROJECT_KEY" QUERY_AGENT="$AGENT" QUERY_REL_PATH="$REL_PATH" QUERY_REG_TOKEN="$REG_TOKEN" \
    QUERY_ABS_PATH="$ABS_PATH" QUERY_TOKEN="$TOKEN" QUERY_URL="$MCP_URL" \
    QUERY_EXTEND_SECONDS="$RENEW_SECONDS" \
    python3 -c "
import os, json, urllib.request, unicodedata

project_key = os.environ['QUERY_PROJECT_KEY']
agent    = os.environ['QUERY_AGENT']
rel_path = unicodedata.normalize('NFC', os.environ['QUERY_REL_PATH'])
abs_path = unicodedata.normalize('NFC', os.environ['QUERY_ABS_PATH'])
token    = os.environ.get('QUERY_TOKEN', '')
reg_token = os.environ.get('QUERY_REG_TOKEN', '')
url      = os.environ.get('QUERY_URL', 'http://127.0.0.1:8765/mcp')
extend_seconds = int(os.environ.get('QUERY_EXTEND_SECONDS', '900'))

# Try both relative and absolute paths so reservations stored in either format match
paths = [rel_path]
if abs_path != rel_path:
    paths.append(abs_path)

# Use renew_file_reservations: only renews existing reservations, never creates new ones
args = {
    'project_key': project_key,
    'agent_name': agent,
    'paths': paths,
    'extend_seconds': extend_seconds
}
if reg_token:
    args['registration_token'] = reg_token
payload = json.dumps({
    'jsonrpc': '2.0',
    'id': '1',
    'method': 'tools/call',
    'params': {
        'name': 'renew_file_reservations',
        'arguments': args
    }
}).encode()

req = urllib.request.Request(url, data=payload,
    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=3) as r:
        print(r.read().decode())
except Exception as e:
    import sys
    print('HOOK_ERROR: ' + str(e), file=sys.stderr)
    print('')
" 2>/dev/null || echo "")

[ -z "$RESPONSE" ] && exit 0  # Server unreachable — fail open

# Check if any reservations were renewed (renewed > 0 means agent holds a reservation)
RENEWED=$(printf '%s' "$RESPONSE" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    sc = d.get('result', {}).get('structuredContent', {})
    print(sc.get('renewed', 0))
except:
    print(-1)
" 2>/dev/null || echo "-1")

if [ "$RENEWED" = "0" ]; then
    # Retry once after 500ms — intermittent failures observed where renewed=0
    # despite active reservation (possible async commit timing issue)
    sleep 0.5
    RESPONSE2=$(QUERY_PROJECT_KEY="$RESERVATION_PROJECT_KEY" QUERY_AGENT="$AGENT" QUERY_REL_PATH="$REL_PATH" QUERY_REG_TOKEN="$REG_TOKEN" \
        QUERY_ABS_PATH="$ABS_PATH" QUERY_TOKEN="$TOKEN" QUERY_URL="$MCP_URL" \
        QUERY_EXTEND_SECONDS="$RENEW_SECONDS" \
        python3 -c "
import os, json, urllib.request, unicodedata

project_key = os.environ['QUERY_PROJECT_KEY']
agent    = os.environ['QUERY_AGENT']
rel_path = unicodedata.normalize('NFC', os.environ['QUERY_REL_PATH'])
abs_path = unicodedata.normalize('NFC', os.environ['QUERY_ABS_PATH'])
token    = os.environ.get('QUERY_TOKEN', '')
reg_token = os.environ.get('QUERY_REG_TOKEN', '')
url      = os.environ.get('QUERY_URL', 'http://127.0.0.1:8765/mcp')
extend_seconds = int(os.environ.get('QUERY_EXTEND_SECONDS', '900'))

paths = [rel_path]
if abs_path != rel_path:
    paths.append(abs_path)

args = {
    'project_key': project_key,
    'agent_name': agent,
    'paths': paths,
    'extend_seconds': extend_seconds
}
if reg_token:
    args['registration_token'] = reg_token
payload = json.dumps({
    'jsonrpc': '2.0',
    'id': '1',
    'method': 'tools/call',
    'params': {
        'name': 'renew_file_reservations',
        'arguments': args
    }
}).encode()

req = urllib.request.Request(url, data=payload,
    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=3) as r:
        print(r.read().decode())
except Exception as e:
    import sys
    print('HOOK_ERROR: ' + str(e), file=sys.stderr)
    print('')
" 2>/dev/null || echo "")

    RENEWED2=$(printf '%s' "$RESPONSE2" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    sc = d.get('result', {}).get('structuredContent', {})
    print(sc.get('renewed', 0))
except:
    print(-1)
" 2>/dev/null || echo "-1")

    if [ "$RENEWED2" != "0" ]; then
        exit 0  # Retry succeeded
    fi

    # Both renew attempts failed — try auto-acquiring a new reservation.
    # This handles the common case where PostToolUse released the reservation
    # after a successful Edit/Write, and the agent edits the same file again.
    # file_reservation_paths will fail if another agent holds a conflicting reservation.
    _FR_LOG="$RUNTIME_DIR/logs/file_reservation_failures.log"
    mkdir -p "$(dirname "$_FR_LOG")" 2>/dev/null || true
    ACQUIRE_RESPONSE=$(QUERY_PROJECT_KEY="$RESERVATION_PROJECT_KEY" QUERY_AGENT="$AGENT" QUERY_REL_PATH="$REL_PATH" QUERY_REG_TOKEN="$REG_TOKEN" \
        QUERY_TOKEN="$TOKEN" QUERY_URL="$MCP_URL" QUERY_EXTEND_SECONDS="$RENEW_SECONDS" \
        python3 -c "
import os, json, urllib.request, unicodedata

project_key = os.environ['QUERY_PROJECT_KEY']
agent    = os.environ['QUERY_AGENT']
rel_path = unicodedata.normalize('NFC', os.environ['QUERY_REL_PATH'])
token    = os.environ.get('QUERY_TOKEN', '')
reg_token = os.environ.get('QUERY_REG_TOKEN', '')
url      = os.environ.get('QUERY_URL', 'http://127.0.0.1:8765/mcp')
extend_seconds = int(os.environ.get('QUERY_EXTEND_SECONDS', '900'))

args = {
    'project_key': project_key,
    'agent_name': agent,
    'paths': [rel_path],
    'ttl_seconds': extend_seconds,
    'reason': 'auto-acquired by check-file-reservation hook'
}
if reg_token:
    args['registration_token'] = reg_token
payload = json.dumps({
    'jsonrpc': '2.0',
    'id': '1',
    'method': 'tools/call',
    'params': {
        'name': 'file_reservation_paths',
        'arguments': args
    }
}).encode()

req = urllib.request.Request(url, data=payload,
    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(r.read().decode())
except Exception as e:
    import sys
    print('HOOK_ERROR: ' + str(e), file=sys.stderr)
    print('')
" 2>/dev/null || echo "")

    # Check if acquisition succeeded
    # file_reservation_paths returns { granted: [...], conflicts: [...] }
    ACQUIRED=$(printf '%s' "$ACQUIRE_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    sc = d.get('result', {}).get('structuredContent', {})
    granted = sc.get('granted', [])
    conflicts = sc.get('conflicts', [])
    if conflicts:
        print('conflict')
    elif len(granted) > 0:
        print('ok')
    else:
        print('fail')
except:
    print('fail')
" 2>/dev/null || echo "fail")

    if [ "$ACQUIRED" = "ok" ]; then
        echo "$(date -u '+%Y-%m-%dT%H:%M:%S') AUTO_ACQUIRED agent=$AGENT path=$REL_PATH" >> "$_FR_LOG" 2>/dev/null
        exit 0  # Auto-acquired successfully
    fi

    # Auto-acquire failed (conflict with another agent, or server error) — block
    echo "$(date -u '+%Y-%m-%dT%H:%M:%S') BLOCK agent=$AGENT path=$REL_PATH acquire=$ACQUIRED" >> "$_FR_LOG" 2>/dev/null
    echo "FILE RESERVATION REQUIRED: $FILE_PATH" >&2
    if [ "$ACQUIRED" = "conflict" ]; then
        echo "Another agent holds a reservation for this file. Wait or coordinate via agent-mail." >&2
    else
        echo "Agent '$AGENT' could not acquire a reservation for this protected file." >&2
        echo "Use mcp__mcp-agent-mail__macro_file_reservation_cycle to acquire a reservation first." >&2
    fi
    exit 2
fi

exit 0
