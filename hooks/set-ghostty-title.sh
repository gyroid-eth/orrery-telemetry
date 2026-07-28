#!/bin/bash
# set-ghostty-title.sh
# Compatibility helper: records the current agent name, renames pending tmux
# sessions, and optionally copies the title for terminals that need it.
#
# Usage: set-ghostty-title.sh <agent-name>

AGENT_NAME="${1:-${AGENT_NAME:-}}"
if [[ -z "$AGENT_NAME" ]]; then
    exit 0
fi

RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.claude/runtime}"
MANAGED_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$HOME/.claude/managed_agents.txt}"
TERMINAL_SETTING="${AGENTSTACK_TERMINAL:-auto}"

mac_app_exists() {
    [[ -d "/Applications/$1" || -d "$HOME/Applications/$1" ]]
}

terminal_adapter() {
    local setting
    setting="$(printf '%s' "$TERMINAL_SETTING" | tr '[:upper:]' '[:lower:]')"
    case "$setting" in
        ""|auto)
            if [[ "$(uname -s 2>/dev/null)" != "Darwin" ]]; then
                echo "none"
            elif mac_app_exists "Ghostty.app" || command -v ghostty >/dev/null 2>&1; then
                echo "ghostty"
            elif mac_app_exists "iTerm.app" || mac_app_exists "iTerm2.app"; then
                echo "iterm"
            elif mac_app_exists "Terminal.app" || [[ -d "/System/Applications/Utilities/Terminal.app" ]]; then
                echo "terminal"
            else
                echo "none"
            fi
            ;;
        ghostty|iterm|terminal|none)
            echo "$setting"
            ;;
        *)
            echo "none"
            ;;
    esac
}

# Copy the title only when the selected terminal path can use the macOS
# clipboard. Missing pbcopy is a quiet no-op for Linux/WSL and headless setups.
case "$(terminal_adapter)" in
    ghostty|iterm|terminal)
        if command -v pbcopy >/dev/null 2>&1; then
            printf '%s' "$AGENT_NAME" | pbcopy
        fi
        ;;
esac

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
