#!/bin/bash
# The `claude` a Claude child runs: what the child's login shell finds once
# ~/.local/bin is first on PATH (claude_child_launch_command in spawn_child.sh
# runs exactly this path). The launcher sources this file; the dashboard runs
# it before it turns an omitted model into a formal ID, so both check versions
# against the same binary. Read-only: no registration, tmux or application
# launch. Prints nothing when no usable claude is found.

claude_child_shell() {
    local shell="${AGENTSTACK_CHILD_SHELL:-}"
    if [[ -n "$shell" && -x "$shell" ]]; then
        printf '%s\n' "$shell"
        return 0
    fi
    shell="$(command -v zsh 2>/dev/null || true)"
    [[ -z "$shell" ]] && shell="$(command -v bash 2>/dev/null || true)"
    [[ -n "$shell" ]] || return 1
    printf '%s\n' "$shell"
}

# The guard variables match the child's session, so a profile that checks
# CLAUDECODE or the reserved-identity marker sets PATH as it will there.
claude_child_bin() {
    local shell="${1:-}" found
    [[ -n "$shell" ]] || return 0
    found="$(env CLAUDECODE=1 AGENTSTACK_RESERVED_IDENTITY=1 \
        "$shell" -lc 'export PATH="$HOME/.local/bin:$PATH"; command -v claude' \
        2>/dev/null </dev/null | tail -n 1)" || found=""
    if [[ "$found" == /* && -x "$found" ]]; then
        printf '%s\n' "$found"
    fi
    return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    shell="$(claude_child_shell)" || exit 0
    claude_child_bin "$shell"
fi
