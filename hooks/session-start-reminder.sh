#!/bin/bash
# session-start-reminder.sh
# SessionStart hook for startup/resume/clear/compact.
#
# If an existing identity can be resolved, print a reminder to re-register with
# the same mcp-agent-mail name instead of generating a fresh identity.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:8765/mcp}}"
HEALTH_URL="${AGENTSTACK_MCP_HEALTH_URL:-${MCP_AGENT_MAIL_HEALTH_URL:-}}"
RESOLVED_AGENT=""
RESOLVED_AGENT_SRC="none"

if [ -z "$HEALTH_URL" ]; then
    case "$MCP_URL" in
        */mcp) HEALTH_URL="${MCP_URL%/mcp}/health/liveness" ;;
        *) HEALTH_URL="${MCP_URL%/}/health/liveness" ;;
    esac
fi

if [ -f "$HOOKS_DIR/resolve-agent-name.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOOKS_DIR/resolve-agent-name.sh"
fi

mkdir -p "$RUNTIME_DIR" 2>/dev/null
if [ -n "${TMUX_PANE:-}" ]; then
    CURRENT_SESSION=$(tmux display-message -t "$TMUX_PANE" -p '#S' 2>/dev/null)
else
    CURRENT_SESSION=$(tmux display-message -p '#S' 2>/dev/null)
fi
printf '%s src=%s resolved=%q AGENT_NAME=%q TMUX_PANE=%q TMUX=%s sess=%q\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S')" \
    "$RESOLVED_AGENT_SRC" "${RESOLVED_AGENT:-}" "${AGENT_NAME:-}" "${TMUX_PANE:-}" \
    "$([ -n "${TMUX:-}" ] && echo yes || echo no)" \
    "${CURRENT_SESSION:-}" \
    >> "$RUNTIME_DIR/session-start-resolve.log" 2>/dev/null

if curl -sf -m 2 "$HEALTH_URL" >/dev/null 2>&1; then
    echo "mcp-agent-mail server is running. Register this session before working."
    if [ -n "$RESOLVED_AGENT" ]; then
        echo "あなたの名前は「${RESOLVED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。新しい名前を生成せず、必ず name=\"${RESOLVED_AGENT}\" で register_agent してください。"
        echo "1. ensure_project -> 2. register_agent (name=\"$RESOLVED_AGENT\") -> 3. fetch_inbox"
    else
        echo "1. ensure_project -> 2. register_agent (new AdjectiveScientist name if needed) -> 3. fetch_inbox"
    fi
else
    echo "mcp-agent-mail server is not running; skip registration until it is available."
fi
