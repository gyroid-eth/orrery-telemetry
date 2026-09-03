#!/bin/bash
# SessionEnd: release every file reservation held by this session's agent.
# Failures do not block shutdown, but they are recorded in release-failures.log
# instead of disappearing until the reservation TTL happens to expire.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reservation-common.sh"

TOOL_INPUT=$(cat)
reservation_extract_session_id "$TOOL_INPUT"
AGENT_RESULT="$(resolve_agent_name)"
AGENT_SRC="${AGENT_RESULT%%|*}"
AGENT="${AGENT_RESULT#*|}"
if [ "$AGENT_SRC" = "identity-conflict" ] || [ -z "$AGENT" ]; then
    reservation_failure_log "release-all session=${SESSION_ID:-<none>} error=identity-unresolved source=$AGENT_SRC"
    exit 0
fi
case "$AGENT" in
    pending-*) exit 0 ;;
esac

reservation_release_request "$AGENT" "$RESERVATION_PROJECT_KEY" >/dev/null 2>&1 || true

if [ -n "${TMUX_PANE:-}" ]; then
    PANE_KEY="${TMUX_PANE//%/_}"
    rm -f "$RUNTIME_DIR/agent_name_${PANE_KEY}"
fi

exit 0
