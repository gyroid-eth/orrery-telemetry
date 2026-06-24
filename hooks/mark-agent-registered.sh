#!/bin/bash
# mark-agent-registered.sh
# PostToolUse hook: creates flag file after register_agent succeeds.
# Paired with check-agent-registered.sh (PreToolUse).

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}"
INPUT=$(cat)

# Extract session_id
SESSION_ID=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except:
    print('')
" 2>/dev/null)

[ -z "$SESSION_ID" ] && exit 0

FLAG="/tmp/.claude-agent-registered-${SESSION_ID}"
touch "$FLAG"

# Record precise agent-mail-id <-> sessionId <-> transcript map for the
# dashboard's exact session resume (see record-session-index.py). Fire and
# forget; never blocks or fails registration.
if [ -f "$HOOKS_DIR/record-session-index.py" ]; then
    printf '%s' "$INPUT" | python3 "$HOOKS_DIR/record-session-index.py" >/dev/null 2>&1 &
fi

# Resolve the agent name.
#
# Claude Code's PostToolUse payload puts the tool result under `tool_response`
# (NOT `tool_result`). For this MCP server it is a JSON *string* like
#   '{"id":588,"name":"CalmFabre",...}'
# but defensively we also handle: a plain dict, and the MCP content-block
# wrapper {"content":[{"type":"text","text":"{...}"}]}.
#
# Priority: server result (works for server-auto-generated names) ->
#           tool_input.name (explicit-name registrations / spawn_child) .
AGENT_NAME_VAL=$(printf '%s' "$INPUT" | python3 -c "
import sys, json

def extract(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return ''
    if isinstance(v, dict):
        if v.get('name'):
            return v['name']
        content = v.get('content')
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get('type') == 'text':
                    got = extract(blk.get('text', ''))
                    if got:
                        return got
    return ''

try:
    d = json.loads(sys.stdin.read())
    name = extract(d.get('tool_response')) or extract(d.get('tool_result'))
    if not name:
        name = (d.get('tool_input') or {}).get('name', '') or ''
    print(name)
except Exception:
    print('')
" 2>/dev/null)

if [ -n "$AGENT_NAME_VAL" ] && [ -n "$SESSION_ID" ]; then
    if [ -f "$HOOKS_DIR/update-agentfiles-tags.py" ]; then
        printf '{"session_id":"%s","agent_name":"%s"}' "$SESSION_ID" "$AGENT_NAME_VAL" \
            | python3 "$HOOKS_DIR/update-agentfiles-tags.py" &
    fi
fi

# Auto-rename tmux session and copy agent name to clipboard for Ghostty title.
# set-ghostty-title.sh internally guards against renaming sessions that are not
# pending-*, so re-runs on idempotent re-registration are safe no-ops.
#
# GUARD: When a parent agent calls register_agent for a CHILD (via /delegate),
# this hook fires in the PARENT's session. Without this guard, set-ghostty-title.sh
# would write the child's name into the parent's pane metadata, hijacking the
# parent's agent identity and breaking file_reservation hooks for the parent.
# Only call set-ghostty-title.sh when the registered name belongs to THIS session.
if [ -n "$AGENT_NAME_VAL" ]; then
    CURRENT_SESSION=$(tmux display-message -p '#S' 2>/dev/null || echo "")
    if [[ "$CURRENT_SESSION" == pending-* ]] || \
       [[ "$CURRENT_SESSION" == "$AGENT_NAME_VAL" ]] || \
       [[ "${AGENT_NAME:-}" == "$AGENT_NAME_VAL" ]]; then
        bash "$HOOKS_DIR/set-ghostty-title.sh" "$AGENT_NAME_VAL" >/dev/null 2>&1 &
    fi
else
    # Make the failure observable instead of silently leaving the session
    # named pending-*. If this log ever grows, name extraction regressed.
    mkdir -p "$RUNTIME_DIR"
    printf '%s mark-agent-registered: could not resolve agent name; session not renamed (session_id=%s)\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S')" "$SESSION_ID" >> "$RUNTIME_DIR/rename-failures.log"
fi

exit 0
