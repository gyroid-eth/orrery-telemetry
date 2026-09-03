#!/bin/bash
# check-file-reservation.sh
# PreToolUse hook: require an existing reservation before editing protected files.
# Exit 2 = block, 0 = allow.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reservation-common.sh"

# Both the unidentified and the identified path end up here, so an operator who
# chooses `block` stops all editing during an outage rather than only the
# sessions that happen to have no name.
handle_reservation_outage() {
    local detail="$1"
    local policy_lib="$HOOKS_DIR/session-identity-policy.sh"
    # Without the policy library there is no policy to apply, and the
    # established behaviour for an unanswered first check is to allow. Falling
    # through instead would reach the "invalid count" branch and block.
    [ -f "$policy_lib" ] || exit 0
    # shellcheck disable=SC1090
    . "$policy_lib"
    agentstack_audit_unmanaged "check-file-reservation" "$SESSION_ID" "$detail transport=unreachable policy=$(agentstack_mail_outage_policy)"
    if [ "$(agentstack_mail_outage_policy)" = "warn-open" ]; then
        if agentstack_should_report_outage "$SESSION_ID" "unreachable"; then
            agentstack_emit_visible_warning "$(agentstack_outage_warning_text)"
        fi
        exit 0
    fi
    echo "AGENT MAIL UNREACHABLE: no reservation can be taken or checked while the service is down," >&2
    echo "and this installation is configured to refuse edits rather than continue uncoordinated." >&2
    echo "$(agentstack_recovery_hint)" >&2
    echo "Or set AGENTSTACK_MAIL_OUTAGE_POLICY=warn-open to work through the outage." >&2
    exit 2
}

RENEW_SECONDS="${FILE_RESERVATION_RENEW_SECONDS:-900}"
RETRY_DELAY_SECONDS="${FILE_RESERVATION_RETRY_DELAY_SECONDS:-0.5}"

TOOL_INPUT=$(cat)
reservation_resolve_tool_context "$TOOL_INPUT" || exit 0
# Asked before anything decides which identity wins: the precedence resolver
# returns on AGENT_NAME alone, so asking it about conflicts left named sessions
# unchecked.
if [ -f "$POLICY_LIB_EARLY" ] \
    && [ "$(agentstack_session_binding_conflict "$SESSION_ID" "$RESERVATION_PROJECT_KEY" "${AGENT_NAME:-}")" = "conflict" ]; then
    agentstack_conflict_message "check-file-reservation"
    exit 2
fi

AGENT_RESULT="$(resolve_agent_name)"
AGENT_SRC="${AGENT_RESULT%%|*}"
AGENT="${AGENT_RESULT#*|}"

if [ "$AGENT_SRC" = "identity-conflict" ]; then
    if command -v agentstack_conflict_message >/dev/null 2>&1; then
        agentstack_conflict_message "check-file-reservation"
    else
        # Deployed without the shared policy library: the refusal still has to
        # be legible, and the string it is recognised by has to survive.
        echo "AGENT IDENTITY CONFLICT: more than one identity claims this session." >&2
        echo "Editing under either would misattribute the write; resolve the identity first." >&2
    fi
    exit 2
fi

# The service comes next, for every session and not only the nameless ones:
# while it is not answering, no reservation can be taken or checked by anybody,
# and the registration this guard would otherwise demand cannot succeed either.
if [ -f "$POLICY_LIB_EARLY" ]; then
    TRANSPORT_STATE="$(agentstack_mail_transport_state)"
    if [ "$TRANSPORT_STATE" = "invalid" ]; then
        agentstack_invalid_endpoint_message "check-file-reservation"
        exit 2
    fi
    if [ "$TRANSPORT_STATE" = "unreachable" ]; then
        handle_reservation_outage "agent=${AGENT:-<unbound>} path=$REL_PATH"
    fi
fi

if [ -z "$AGENT" ]; then
    # The two guards must agree here. They ask the same pair of questions in the
    # same order -- is the service answering, and does this session have an
    # identity -- because a session let past one and stopped by the other is
    # still stuck, and the reason it is stuck becomes impossible to read.
    # Registered but unidentifiable is not the same as never registered: the
    # flag proves this session reached the mail MCP, so it can take a
    # reservation like anyone else. This is asked after the service question,
    # because during an outage "call register_agent again" is the impossible
    # instruction this whole change exists to remove.
    if [ -n "$SESSION_ID" ] && [ -f "/tmp/.claude-agent-registered-${SESSION_ID}" ]; then
        echo "AGENT IDENTITY UNRESOLVED: this session is registered, but no identity binding for it was found." >&2
        echo "Call register_agent again so the binding is rewritten, then retry the edit." >&2
        exit 2
    fi

    POLICY_LIB="$HOOKS_DIR/session-identity-policy.sh"
    if [ -f "$POLICY_LIB" ]; then
        # shellcheck disable=SC1090
        . "$POLICY_LIB"

        if agentstack_session_is_unmanaged; then
            agentstack_audit_unmanaged "check-file-reservation" "$SESSION_ID" "path=$REL_PATH identity=none policy=$(agentstack_unmanaged_policy)"
            if [ "$(agentstack_unmanaged_policy)" = "warn-open" ]; then
                if agentstack_first_warning_for_session "$SESSION_ID"; then
                    agentstack_emit_visible_warning "This session runs outside coordination by configuration, so it is editing without a file reservation. Another agent may be editing the same files, and neither side will see the conflict."
                fi
                exit 0
            fi
            agentstack_unmanaged_block_message "check-file-reservation"
            exit 2
        fi
    fi
    echo "AGENT IDENTITY REQUIRED: cannot verify a reservation for $FILE_PATH" >&2
    echo "Set AGENT_NAME or restore exact TMUX_PANE identity metadata before editing." >&2
    exit 2
fi

case "$AGENT" in
    pending-*)
        echo "SESSION SETUP INCOMPLETE: agent '$AGENT' has not completed startup." >&2
        echo "Complete registration and acquire a reservation before editing." >&2
        exit 2
        ;;
esac

if legacy_bearer_enabled; then
    TOKEN="$(get_legacy_http_bearer 2>/dev/null || true)"
else
    bearer_status=$?
    if [ "$bearer_status" -eq 2 ]; then
        exit 2
    fi
    TOKEN=""
fi
ABS_PATH="$FILE_PATH"

renew_reservation() {
    QUERY_PROJECT_KEY="$RESERVATION_PROJECT_KEY" QUERY_AGENT="$AGENT" \
        QUERY_REL_PATH="$REL_PATH" QUERY_ABS_PATH="$ABS_PATH" \
        QUERY_TOKEN="$TOKEN" QUERY_URL="$MCP_URL" \
        QUERY_EXTEND_SECONDS="$RENEW_SECONDS" \
        python3 - <<'PY'
import json
import os
import socket
import unicodedata
import urllib.error
import urllib.request

project_key = os.environ["QUERY_PROJECT_KEY"]
agent = os.environ["QUERY_AGENT"]
rel_path = unicodedata.normalize("NFC", os.environ["QUERY_REL_PATH"])
abs_path = unicodedata.normalize("NFC", os.environ["QUERY_ABS_PATH"])
token = os.environ.get("QUERY_TOKEN", "")
url = os.environ.get("QUERY_URL", "http://127.0.0.1:8765/api/")
extend_seconds = int(os.environ.get("QUERY_EXTEND_SECONDS", "900"))
paths = [rel_path]
if abs_path != rel_path:
    paths.append(abs_path)

# The pinned live schema and AgentStack Mail both omit registration_token from
# reservation tools. A legacy HTTP bearer, when selected above, remains a
# transport credential and must never be copied into tool arguments.
arguments = {
    "project_key": project_key,
    "agent_name": agent,
    "paths": paths,
    "extend_seconds": extend_seconds,
}
payload = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": "reservation-guard",
        "method": "tools/call",
        "params": {"name": "renew_file_reservations", "arguments": arguments},
    }
).encode()
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
if token:
    headers["Authorization"] = "Bearer " + token
request = urllib.request.Request(url, data=payload, headers=headers)
try:
    with urllib.request.urlopen(request, timeout=3) as response:
        body = response.read().decode()
except urllib.error.HTTPError as exc:
    detail = exc.read().decode(errors="replace").strip()
    print(f"HOOK_REJECTED: HTTP {exc.code}: {detail or exc.reason}")
    raise SystemExit(0)
except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
    print(f"HOOK_TRANSPORT_UNREACHABLE: {exc}")
    raise SystemExit(0)
except Exception as exc:
    print(f"HOOK_REJECTED: request or response failure: {exc}")
    raise SystemExit(0)

try:
    document = json.loads(body)
except Exception as exc:
    print(f"HOOK_REJECTED: invalid JSON response: {exc}")
    raise SystemExit(0)
if document.get("error") is not None:
    print("HOOK_REJECTED: MCP error: " + json.dumps(document["error"], sort_keys=True))
    raise SystemExit(0)
result = document.get("result")
if not isinstance(result, dict):
    print("HOOK_REJECTED: MCP tool result reports an error")
    raise SystemExit(0)
is_error = result.get("isError", False)
if not isinstance(is_error, bool) or is_error:
    print("HOOK_REJECTED: MCP tool result has an invalid or true isError field")
    raise SystemExit(0)
structured = result.get("structuredContent")
renewed = structured.get("renewed") if isinstance(structured, dict) else None
if isinstance(renewed, bool) or not isinstance(renewed, int) or renewed < 0:
    print("HOOK_REJECTED: response lacks a valid structuredContent.renewed count")
    raise SystemExit(0)
print(f"HOOK_RENEWED: {renewed}")
PY
}

classify_renew_response() {
    case "$1" in
        "HOOK_RENEWED: "*) printf '%s\n' "${1#HOOK_RENEWED: }" ;;
        "HOOK_TRANSPORT_UNREACHABLE: "*) printf '%s\n' "transport" ;;
        *) printf '%s\n' "rejected" ;;
    esac
}

block_reservation_write() {
    local reason="$1"
    local log_file="$RUNTIME_DIR/logs/file_reservation_failures.log"
    mkdir -p "$(dirname "$log_file")" 2>/dev/null || true
    echo "$(date -u '+%Y-%m-%dT%H:%M:%S') BLOCK agent=$AGENT path=$REL_PATH reason=$reason" >> "$log_file" 2>/dev/null
    echo "FILE RESERVATION REQUIRED: $FILE_PATH" >&2
    echo "$reason" >&2
    echo "Acquire one with macro_file_reservation_cycle before editing." >&2
    exit 2
}

RESPONSE="$(renew_reservation 2>/dev/null || true)"
RENEWED="$(classify_renew_response "$RESPONSE")"
if [ "$RENEWED" = "transport" ]; then
    # No authoritative answer was obtained because the service is not there.
    # This is the same condition the unidentified path already routes through
    # the outage policy, and it has to be the same decision -- an identified
    # agent is not more entitled to write uncoordinated than an anonymous one.
    handle_reservation_outage "agent=$AGENT path=$REL_PATH"
fi
if [ "$RENEWED" = "rejected" ]; then
    block_reservation_write "Reservation service rejected the check: $RESPONSE"
fi
if [[ "$RENEWED" =~ ^[1-9][0-9]*$ ]]; then
    exit 0
fi
if [ "$RENEWED" != "0" ]; then
    block_reservation_write "Reservation service returned an invalid renewed count: $RENEWED"
fi

# Retry a definitive zero once to tolerate an asynchronous reservation commit.
sleep "$RETRY_DELAY_SECONDS"
RESPONSE2="$(renew_reservation 2>/dev/null || true)"
RENEWED2="$(classify_renew_response "$RESPONSE2")"
if [[ "$RENEWED2" =~ ^[1-9][0-9]*$ ]]; then
    exit 0
fi
if [ "$RENEWED2" = "transport" ]; then
    block_reservation_write "Reservation service became unreachable after reporting no reservation."
fi
if [ "$RENEWED2" = "rejected" ]; then
    block_reservation_write "Reservation service rejected the retry: $RESPONSE2"
fi
block_reservation_write "Agent '$AGENT' has no reservation for this protected file."
