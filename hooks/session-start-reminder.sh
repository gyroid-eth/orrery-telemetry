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
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
RESOLVED_AGENT=""
RESOLVED_AGENT_SRC="none"
SHELL_REGISTERED_AGENT=""

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

find_register_lib() {
    local candidate
    if [ -n "${AGENTSTACK_REGISTER_LIB:-}" ] && [ -f "$AGENTSTACK_REGISTER_LIB" ]; then
        printf '%s\n' "$AGENTSTACK_REGISTER_LIB"
        return 0
    fi
    for candidate in \
        "${AGENTSTACK_HOME:-$HOME/.agentstack}/bin/lib/agentstack-register.sh" \
        "$HOOKS_DIR/../bin/lib/agentstack-register.sh" \
        "$HOME/.agentstack/bin/lib/agentstack-register.sh"; do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

shell_register_resolved_agent() {
    local register_lib restored_token work_dir model
    [ -n "$RESOLVED_AGENT" ] || return 1
    [ -n "$PROJECT_KEY" ] || return 1
    register_lib="$(find_register_lib)" || return 1
    # shellcheck disable=SC1090
    . "$register_lib" || return 1

    ags_mail_load_token
    restored_token="${CHILD_REGISTRATION_TOKEN:-}"
    if [ -z "$restored_token" ]; then
        restored_token="$(ags_load_registration_token "$RESOLVED_AGENT" 2>/dev/null || true)"
    fi
    [ -n "$restored_token" ] || return 1

    CHILD_REGISTRATION_TOKEN="$restored_token"
    export CHILD_REGISTRATION_TOKEN
    work_dir="${PWD:-$PROJECT_KEY}"
    model="${AGENTSTACK_CLAUDE_MODEL:-claude-code}"
    ags_register_session "$PROJECT_KEY" "claude-code" "$model" "cc" "$work_dir" "$RESOLVED_AGENT" "reserved" >/dev/null 2>&1 || return 1
    SHELL_REGISTERED_AGENT="${AGS_REGISTERED_AGENT_NAME:-$RESOLVED_AGENT}"
    return 0
}

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
    if [ -n "$RESOLVED_AGENT" ] && shell_register_resolved_agent; then
        echo "mcp-agent-mail server is running. This session is already registered."
        echo "あなたは「${SHELL_REGISTERED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。shell hook で登録済みです。"
        echo "新しい名前を生成せず、register_agent を呼び直さず、fetch_inbox から始めてください。"
        echo "1. fetch_inbox (agent_name=\"$SHELL_REGISTERED_AGENT\")"
    elif [ -n "$RESOLVED_AGENT" ]; then
        echo "mcp-agent-mail server is running. Register this session before working."
        echo "あなたの名前は「${RESOLVED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。新しい名前を生成せず、必ず name=\"${RESOLVED_AGENT}\" で register_agent してください。"
        echo "1. ensure_project -> 2. register_agent (name=\"$RESOLVED_AGENT\") -> 3. fetch_inbox"
    else
        echo "mcp-agent-mail server is running. Register this session before working."
        echo "1. ensure_project -> 2. register_agent (new AdjectiveScientist name if needed) -> 3. fetch_inbox"
    fi
else
    echo "mcp-agent-mail server is not running; skip registration until it is available."
fi
