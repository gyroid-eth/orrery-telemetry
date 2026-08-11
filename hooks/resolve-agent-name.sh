#!/bin/bash
# resolve-agent-name.sh
# Resolve the current mcp-agent-mail identity for hook scripts.
# Priority: AGENT_NAME env -> TMUX_PANE metadata -> that pane's tmux session -> empty.
#
# Usage: source this script, then read RESOLVED_AGENT and RESOLVED_AGENT_SRC.

RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
RESOLVED_AGENT=""
RESOLVED_AGENT_SRC="none"

# 1. Environment variable (launcher-created sessions and child agents).
if [ -n "${AGENT_NAME:-}" ]; then
    RESOLVED_AGENT="$AGENT_NAME"
    RESOLVED_AGENT_SRC="env"
    return 0 2>/dev/null || exit 0
fi

# 2. Resolve the exact targeted pane session. Pane metadata is corroboration,
# never authority by itself because stale files survive pane/session reuse.
if [ -n "${TMUX_PANE:-}" ]; then
    PANE_KEY="${TMUX_PANE//%/_}"
    METADATA_FILE="$RUNTIME_DIR/agent_name_${PANE_KEY}"
    PANE_METADATA=""
    if [ -f "$METADATA_FILE" ]; then
        PANE_METADATA=$(tr -d '[:space:]' < "$METADATA_FILE" 2>/dev/null)
    fi
    PANE_SESSION=$(tmux display-message -t "$TMUX_PANE" -p '#S' 2>/dev/null)
    case "$PANE_SESSION" in
        ""|pending-*|warm-*|claimed-*|mail-watcher) PANE_SESSION="" ;;
    esac
    if [ -n "$PANE_SESSION" ] && [ -n "$PANE_METADATA" ] \
        && [ "$PANE_SESSION" != "$PANE_METADATA" ]; then
        RESOLVED_AGENT_SRC="identity-conflict"
    elif [ -n "$PANE_SESSION" ]; then
        RESOLVED_AGENT="$PANE_SESSION"
        if [ -n "$PANE_METADATA" ]; then
            RESOLVED_AGENT_SRC="metafile+tmux-session"
        else
            RESOLVED_AGENT_SRC="tmux-session"
        fi
    elif [ -n "$PANE_METADATA" ]; then
        RESOLVED_AGENT_SRC="unconfirmed-metafile"
    fi
fi

# 3. With no TMUX_PANE, never query an untargeted ambient tmux session.
# Empty means unresolved. Each caller applies its own safety boundary.
