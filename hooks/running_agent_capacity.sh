#!/usr/bin/env bash
# Small shell adapter for hooks and top-level launchers.  The Python helper
# owns the lock and state format; this wrapper only finds the current tmux pane
# before a launcher starts its agent CLI.
set -euo pipefail

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
HELPER="$HOOKS_DIR/running_agent_capacity.py"

die() {
  printf 'running-agent capacity: %s\n' "$*" >&2
  exit 5
}

command_name="${1:-}"
shift || true

case "$command_name" in
  reserve)
    purpose="${1:-launch}"
    if [[ -z "${AGENTSTACK_MAX_RUNNING_AGENTS:-}" ]]; then
      printf '\n'
      exit 0
    fi
    [[ -f "$HELPER" ]] || die "missing helper: $HELPER"
    exec "$PYTHON_BIN" "$HELPER" --output lease reserve --purpose "$purpose"
    ;;
  claim)
    program="${1:-}"
    session="${2:-}"
    tmux_bin="${3:-tmux}"
    lease="${AGENTSTACK_RUNNING_AGENT_LEASE:-}"
    [[ -n "$lease" ]] || exit 0
    [[ -n "$program" && -n "$session" ]] || die "claim requires program and session"
    [[ -f "$HELPER" ]] || die "missing helper: $HELPER"
    pane_pid="$("$tmux_bin" display-message -t "=$session" -p '#{pane_pid}' 2>/dev/null || true)"
    [[ "$pane_pid" =~ ^[1-9][0-9]*$ ]] || die "cannot read pane pid for tmux session '$session'"
    exec "$PYTHON_BIN" "$HELPER" claim --lease "$lease" --session "$session" \
      --pane-pid "$pane_pid" --program "$program"
    ;;
  release)
    lease="${AGENTSTACK_RUNNING_AGENT_LEASE:-}"
    [[ -n "$lease" ]] || exit 0
    [[ -f "$HELPER" ]] || die "missing helper: $HELPER"
    exec "$PYTHON_BIN" "$HELPER" release --lease "$lease"
    ;;
  status)
    [[ -f "$HELPER" ]] || die "missing helper: $HELPER"
    exec "$PYTHON_BIN" "$HELPER" status
    ;;
  *)
    die "usage: $0 {reserve [purpose]|claim PROGRAM SESSION [TMUX]|release|status}"
    ;;
esac
