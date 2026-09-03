#!/bin/bash
# PostToolUse(Edit|Write): after a successful edit, release its reservation.
#
# Release is deliberately debounced. Immediate release makes the next Edit in
# a short sequence fail its PreToolUse reservation check. A token in
# file_release_debounce lets a newer edit or re-reservation invalidate the old
# sleeping worker without killing processes or relying on reservation ids.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reservation-common.sh"

TOOL_OUTPUT=$(cat)
TOOL_SUCCESS=$(printf '%s' "$TOOL_OUTPUT" | python3 -c '
import json
import re
import sys

fail_prefix = re.compile(
    r"^(?:error:|pretooluse:|posttooluse:|blocked(?:\b|:)|permission denied\b)",
    re.IGNORECASE,
)

def looks_failed(value):
    if isinstance(value, dict):
        if value.get("error") not in (None, "", False):
            return True
        status = value.get("status")
        if isinstance(status, str) and status.lower() in {"error", "failed", "blocked"}:
            return True
        if value.get("success") is False:
            return True
        return any(looks_failed(item) for item in value.values())
    if isinstance(value, list):
        return any(looks_failed(item) for item in value)
    if isinstance(value, str):
        return bool(fail_prefix.match(value.strip()))
    return False

try:
    document = json.loads(sys.stdin.read())
except Exception:
    print("false")
    raise SystemExit(0)
failed = document.get("error") not in (None, "", False)
for field in ("tool_result", "tool_response", "tool_output"):
    failed = failed or looks_failed(document.get(field))
print("false" if failed else "true")
' 2>/dev/null || echo "false")
[ "$TOOL_SUCCESS" = "true" ] || exit 0

# This is the same path/project resolver used by the PreToolUse guard. A
# different root or path spelling can make release look successful while it
# releases nothing.
reservation_resolve_tool_context "$TOOL_OUTPUT" || exit 0

if [ -f "$POLICY_LIB_EARLY" ] \
    && [ "$(agentstack_session_binding_conflict "$SESSION_ID" "$RESERVATION_PROJECT_KEY" "${AGENT_NAME:-}")" = "conflict" ]; then
    reservation_failure_log "release session=${SESSION_ID:-<none>} path=$REL_PATH error=identity-conflict"
    exit 0
fi

AGENT_RESULT="$(resolve_agent_name)"
AGENT_SRC="${AGENT_RESULT%%|*}"
AGENT="${AGENT_RESULT#*|}"
if [ "$AGENT_SRC" = "identity-conflict" ] || [ -z "$AGENT" ]; then
    reservation_failure_log "release session=${SESSION_ID:-<none>} path=$REL_PATH error=identity-unresolved source=$AGENT_SRC"
    exit 0
fi
case "$AGENT" in
    pending-*) exit 0 ;;
esac

RELEASE_GRACE_SECONDS="${AGENTSTACK_RELEASE_GRACE_SECONDS:-${FILE_RESERVATION_RELEASE_GRACE_SECONDS:-90}}"
case "$RELEASE_GRACE_SECONDS" in
    ''|*[!0-9]*) RELEASE_GRACE_SECONDS=90 ;;
esac

PATHS_JSON=$(QUERY_REL_PATH="$REL_PATH" QUERY_ABS_PATH="$FILE_PATH" python3 -c '
import json
import os
import unicodedata

relative = unicodedata.normalize("NFC", os.environ["QUERY_REL_PATH"])
absolute = unicodedata.normalize("NFC", os.environ["QUERY_ABS_PATH"])
paths = [relative]
if absolute != relative:
    paths.append(absolute)
print(json.dumps(paths, ensure_ascii=False))
' 2>/dev/null || echo "")
if [ -z "$PATHS_JSON" ]; then
    reservation_failure_log "release agent=$AGENT path=$REL_PATH error=path-encoding-failed"
    exit 0
fi

release_now() {
    reservation_release_request "$AGENT" "$RESERVATION_PROJECT_KEY" "$PATHS_JSON" >/dev/null 2>&1 || true
}

if [ "$RELEASE_GRACE_SECONDS" -le 0 ]; then
    release_now
    exit 0
fi

STATE_DIR="$RUNTIME_DIR/file_release_debounce"
mkdir -p "$STATE_DIR" 2>/dev/null || {
    reservation_failure_log "release agent=$AGENT path=$REL_PATH error=debounce-state-unwritable fallback=immediate"
    release_now
    exit 0
}
STATE_KEY=$(QUERY_AGENT="$AGENT" QUERY_REL_PATH="$REL_PATH" python3 -c '
import hashlib
import os
import unicodedata

agent = os.environ["QUERY_AGENT"]
path = unicodedata.normalize("NFC", os.environ["QUERY_REL_PATH"])
print(hashlib.sha1((agent + "\0" + path).encode("utf-8")).hexdigest())
' 2>/dev/null || echo "")
if [ -z "$STATE_KEY" ]; then
    reservation_failure_log "release agent=$AGENT path=$REL_PATH error=debounce-key-failed fallback=immediate"
    release_now
    exit 0
fi

STATE_FILE="$STATE_DIR/$STATE_KEY"
STATE_TOKEN="$(date +%s).$$"
printf '%s\n' "$STATE_TOKEN" > "$STATE_FILE" 2>/dev/null || {
    reservation_failure_log "release agent=$AGENT path=$REL_PATH error=debounce-state-write-failed fallback=immediate"
    release_now
    exit 0
}

WORKER="$HOOKS_DIR/release-file-reservation-worker.py"
if [ ! -f "$WORKER" ]; then
    reservation_failure_log "release agent=$AGENT path=$REL_PATH error=worker-missing fallback=immediate"
    release_now
    exit 0
fi

nohup env \
    AGENTSTACK_HOOKS_DIR="$HOOKS_DIR" \
    AGENTSTACK_RUNTIME_DIR="$RUNTIME_DIR" \
    AGENTSTACK_PROJECT_KEY="$RESERVATION_PROJECT_KEY" \
    AGENTSTACK_MCP_URL="$MCP_URL" \
    QUERY_AGENT="$AGENT" \
    QUERY_PROJECT_KEY="$RESERVATION_PROJECT_KEY" \
    QUERY_PATHS_JSON="$PATHS_JSON" \
    QUERY_STATE_FILE="$STATE_FILE" \
    QUERY_STATE_TOKEN="$STATE_TOKEN" \
    QUERY_GRACE_SECONDS="$RELEASE_GRACE_SECONDS" \
    QUERY_COMMON_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reservation-common.sh" \
    python3 "$WORKER" >/dev/null 2>&1 &

exit 0
