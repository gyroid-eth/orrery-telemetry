#!/bin/bash
# set-ghostty-title.sh
# Compatibility helper: records the current agent name and renames pending tmux
# sessions.
#
# Usage: set-ghostty-title.sh <agent-name>

AGENT_NAME="${1:-${AGENT_NAME:-}}"
if [[ -z "$AGENT_NAME" ]]; then
    exit 0
fi

RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"

# Rename pending tmux sessions only, so child registration cannot overwrite a
# parent's established session name.
if [[ -n "$TMUX" ]]; then
    current_name=$(tmux display-message -p '#S' 2>/dev/null)
    if [[ "$current_name" == pending-* ]]; then
        if ! tmux rename-session "$AGENT_NAME" 2>/dev/null; then
            # Never kill the session holding the name. Sharing a name is not
            # evidence of being stale: the other session may be a live, attached
            # agent, and killing it destroyed real work. Fail closed instead —
            # the identity is not finalized, so the caller must retry with a
            # different name rather than present this session as '$AGENT_NAME'.
            if tmux has-session -t "=$AGENT_NAME" 2>/dev/null; then
                echo "[set-ghostty-title] ERROR: tmux session '$AGENT_NAME' already exists; refusing to rename '$current_name' over it." >&2
                echo "[set-ghostty-title] This session keeps the name '$current_name'. Pick another agent name, or retire the other session explicitly (tmux kill-session -t '$AGENT_NAME') after confirming it is dead." >&2
            else
                echo "[set-ghostty-title] ERROR: failed to rename session from '$current_name' to '$AGENT_NAME'" >&2
            fi
            exit 1
        fi
    fi
fi

# Write TMUX_PANE-based agent-name metadata used by other hooks.
if [[ -n "$TMUX_PANE" ]]; then
    mkdir -p "$RUNTIME_DIR"
    PANE_KEY="${TMUX_PANE//%/_}"
    echo -n "$AGENT_NAME" > "$RUNTIME_DIR/agent_name_${PANE_KEY}"
fi

if ! grep -qxF "$AGENT_NAME" "$MANAGED_FILE" 2>/dev/null; then
    mkdir -p "$(dirname "$MANAGED_FILE")"
    echo "$AGENT_NAME" >> "$MANAGED_FILE"
fi
