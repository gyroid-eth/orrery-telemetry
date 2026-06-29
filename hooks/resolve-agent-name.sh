#!/bin/bash
# resolve-agent-name.sh
# Resolve the current mcp-agent-mail identity for hook scripts.
# Priority: AGENT_NAME env -> TMUX_PANE metadata file -> tmux session name -> empty.
#
# Usage: source this script, then read RESOLVED_AGENT and RESOLVED_AGENT_SRC.

RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}"
RESOLVED_AGENT=""
RESOLVED_AGENT_SRC="none"

# 1. Environment variable (launcher-created sessions and child agents).
if [ -n "${AGENT_NAME:-}" ]; then
    RESOLVED_AGENT="$AGENT_NAME"
    RESOLVED_AGENT_SRC="env"
    return 0 2>/dev/null || exit 0
fi

# 2. TMUX_PANE metadata file written by set-ghostty-title.sh.
if [ -n "${TMUX_PANE:-}" ]; then
    PANE_KEY="${TMUX_PANE//%/_}"
    METADATA_FILE="$RUNTIME_DIR/agent_name_${PANE_KEY}"
    if [ -f "$METADATA_FILE" ]; then
        RESOLVED_AGENT=$(tr -d '[:space:]' < "$METADATA_FILE" 2>/dev/null)
        [ -n "$RESOLVED_AGENT" ] && RESOLVED_AGENT_SRC="metafile"
    fi
fi

# 3. tmux session name. With TMUX_PANE set, always target that pane explicitly;
# otherwise tmux may report an ambient current session from a different agent.
if [ -z "$RESOLVED_AGENT" ]; then
    if [ -n "${TMUX_PANE:-}" ]; then
        _ssn=$(tmux display-message -t "$TMUX_PANE" -p '#S' 2>/dev/null)
    else
        _ssn=$(tmux display-message -p '#S' 2>/dev/null)
    fi
    case "$_ssn" in
        ""|pending-*|warm-*|claimed-*|mail-watcher) : ;;
        *) RESOLVED_AGENT="$_ssn"; RESOLVED_AGENT_SRC="tmux-session" ;;
    esac
fi

# 4. Empty is fail-open; callers decide whether to warn or continue.
