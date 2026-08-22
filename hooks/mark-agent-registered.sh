#!/bin/bash
# mark-agent-registered.sh
# PostToolUse hook: creates flag file after register_agent succeeds.
# Paired with check-agent-registered.sh (PreToolUse).

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
INPUT=$(cat)

# Extract session_id
SESSION_ID=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except:
    print('')
" 2>/dev/null)

[ -z "$SESSION_ID" ] && exit 0

# Resolve and validate the agent name before recording any successful state.
#
# Claude Code's PostToolUse payload puts the tool result under `tool_response`
# (NOT `tool_result`). For this MCP server it is a JSON *string* like
#   '{"id":588,"name":"CalmFabre",...}'
# but defensively we also handle: a plain dict, and the MCP content-block
# wrapper {"content":[{"type":"text","text":"{...}"}]}.
#
# The server result is authoritative for omitted-name registrations. When the
# caller explicitly requested a name, however, accepting a different returned
# name would strand the session under an identity its environment does not use.
# Missing or malformed responses therefore fail closed; never fall back to the
# input name when the server result cannot be read.
AGENT_NAME_VAL=$(printf '%s' "$INPUT" | python3 -c '
import sys, json

def inspect_response(v, names, errors):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return
        inspect_response(v, names, errors)
        return
    if isinstance(v, list):
        for item in v:
            inspect_response(item, names, errors)
        return
    if isinstance(v, dict):
        for flag in ("isError", "is_error"):
            if flag in v and v[flag] is not False:
                errors.add("invalid_or_error_flag")
        if "error" in v and v["error"] is not None:
            errors.add("error_object")
        for field in ("name", "agent_name"):
            if field not in v:
                continue
            name = v[field]
            if isinstance(name, str) and name:
                names.add(name)
            else:
                errors.add("invalid_response_name")
        for key in ("structuredContent", "structured_content", "result", "data"):
            if key in v:
                inspect_response(v[key], names, errors)
        content = v.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    inspect_response(block.get("text"), names, errors)

try:
    d = json.loads(sys.stdin.read())
except Exception:
    print(json.dumps({"reason": "invalid_hook_payload"}, sort_keys=True))
    raise SystemExit(20)

if "tool_input" not in d:
    print(json.dumps({"reason": "missing_tool_input"}, sort_keys=True))
    raise SystemExit(21)
tool_input = d["tool_input"]
if not isinstance(tool_input, dict):
    print(json.dumps({"reason": "invalid_tool_input"}, sort_keys=True))
    raise SystemExit(22)

responses = [
    d[key]
    for key in ("tool_response", "tool_result")
    if key in d and d[key] is not None
]
if not responses:
    print(json.dumps({"reason": "missing_tool_response"}, sort_keys=True))
    raise SystemExit(23)
names = set()
response_errors = set()
for response in responses:
    channel_names = set()
    channel_errors = set()
    inspect_response(response, channel_names, channel_errors)
    if not channel_names:
        channel_errors.add("malformed_response_channel")
    names.update(channel_names)
    response_errors.update(channel_errors)
if response_errors:
    reason = (
        "malformed_response_channel"
        if "malformed_response_channel" in response_errors
        else "error_response"
    )
    print(json.dumps({"reason": reason}, sort_keys=True))
    raise SystemExit(24)

if not names:
    print(json.dumps({"reason": "missing_response_name"}, sort_keys=True))
    raise SystemExit(25)
if len(names) != 1:
    print(json.dumps({"reason": "inconsistent_response_names"}, sort_keys=True))
    raise SystemExit(26)
returned_name = next(iter(names))

if "name" in tool_input and tool_input["name"] is not None:
    requested_name = tool_input["name"]
    if not isinstance(requested_name, str):
        print(json.dumps({"reason": "invalid_requested_name"}, sort_keys=True))
        raise SystemExit(27)
    if requested_name != returned_name:
        print(json.dumps({
            "reason": "name_mismatch",
            "requested_name": requested_name,
            "returned_name": returned_name,
        }, sort_keys=True))
        raise SystemExit(28)

print(returned_name)
' 2>/dev/null)
NAME_STATUS=$?

if [ "$NAME_STATUS" -ne 0 ]; then
    mkdir -p "$RUNTIME_DIR"
    printf '%s mark-agent-registered: %s (session_id=%s)\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S')" "$AGENT_NAME_VAL" "$SESSION_ID" \
        >> "$RUNTIME_DIR/registration-failures.log"
    printf 'AGENT REGISTRATION NOT ACCEPTED: %s\n' "$AGENT_NAME_VAL" >&2
    exit 2
fi

# Record the agent-mail-id <-> sessionId <-> transcript map (see
# record-session-index.py) BEFORE the flag exists. The index started as a
# convenience for the dashboard's session resume, but resolve-agent-name.sh now
# reads it as the identity of a session that has no launcher, so the old
# fire-and-forget order left a window where the flag said "registered" while
# the guards could not yet tell who this was -- and a guard that cannot name
# the agent has nothing to check a reservation against.
if [ -f "$HOOKS_DIR/record-session-index.py" ]; then
    # Who is calling matters as much as who was registered: a parent that
    # registers a child fires this same PostToolUse, and binding the child's
    # name to the parent's session would let the parent act as the child. The
    # caller is resolved the same way the guards resolve identity, so a session
    # identified only by tmux is not mistaken for an anonymous self-registration.
    REGISTER_PROJECT_KEY=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    value = (json.loads(sys.stdin.read()).get("tool_input") or {}).get("project_key", "")
    print(value if isinstance(value, str) else "")
except Exception:
    print("")
' 2>/dev/null || echo "")

    # Both halves of the resolution travel to the writer. The name alone hides
    # the difference between "no claim on this session" and "the claim was
    # refused", and the writer must not read a refusal as an anonymous agent
    # registering itself.
    CALLER_RESULT="none|"
    if [ -f "$HOOKS_DIR/resolve-agent-name.sh" ]; then
        CALLER_RESULT="$(
            AGENTSTACK_SESSION_ID="$SESSION_ID" \
            AGENTSTACK_LOOKUP_PROJECT_KEY="$REGISTER_PROJECT_KEY" \
            bash -c '. "$0"; printf "%s|%s" "${RESOLVED_AGENT_SRC:-none}" "${RESOLVED_AGENT:-}"' \
                "$HOOKS_DIR/resolve-agent-name.sh" 2>/dev/null
        )"
        [ -z "$CALLER_RESULT" ] && CALLER_RESULT="none|"
    fi
    AGENTSTACK_REGISTERING_SOURCE="${CALLER_RESULT%%|*}"
    AGENTSTACK_REGISTERING_AGENT="${CALLER_RESULT#*|}"
    export AGENTSTACK_REGISTERING_SOURCE AGENTSTACK_REGISTERING_AGENT

    printf '%s' "$INPUT" | python3 "$HOOKS_DIR/record-session-index.py" >/dev/null 2>&1
    INDEX_STATUS=$?
fi

# The flag says "this session is registered", and the guards answer questions
# about it that only a binding can answer. It is created for a written binding
# and nothing else: a session that registered a child, or whose own identity
# could not be established, has not registered itself.
case "${INDEX_STATUS:-0}" in
    0) ;;
    4)
        echo "note: registration recorded for another agent; this session is still unregistered." >&2
        exit 0
        ;;
    5)
        echo "AGENT IDENTITY UNRESOLVED: this session's own identity could not be established," >&2
        echo "so no session binding was written and the registration flag was not created." >&2
        exit 0
        ;;
    6)
        echo "SESSION BINDING NOT WRITTEN: the identity index could not be updated." >&2
        echo "The remote registration succeeded, but this session stays unregistered locally." >&2
        exit 0
        ;;
    *) ;;
esac

FLAG="/tmp/.claude-agent-registered-${SESSION_ID}"
touch "$FLAG"

if [ -n "$AGENT_NAME_VAL" ] && [ -n "$SESSION_ID" ]; then
    if [ -f "$HOOKS_DIR/update-agentfiles-tags.py" ]; then
        printf '{"session_id":"%s","agent_name":"%s"}' "$SESSION_ID" "$AGENT_NAME_VAL" \
            | python3 "$HOOKS_DIR/update-agentfiles-tags.py" &
    fi
fi

# Auto-rename tmux session and copy agent name to clipboard for Ghostty title.
# set-ghostty-title.sh internally guards against renaming sessions that are not
# pending-*, so re-runs on idempotent re-registration are safe no-ops.
#
# GUARD: When a parent agent calls register_agent for a CHILD (via /delegate),
# this hook fires in the PARENT's session. Without this guard, set-ghostty-title.sh
# would write the child's name into the parent's pane metadata, hijacking the
# parent's agent identity and breaking file_reservation hooks for the parent.
# Only call set-ghostty-title.sh when the registered name belongs to THIS session.
if [ -n "$AGENT_NAME_VAL" ]; then
    CURRENT_SESSION=$(tmux display-message -p '#S' 2>/dev/null || echo "")
    if [[ "$CURRENT_SESSION" == pending-* ]] || \
       [[ "$CURRENT_SESSION" == "$AGENT_NAME_VAL" ]] || \
       [[ "${AGENT_NAME:-}" == "$AGENT_NAME_VAL" ]]; then
        bash "$HOOKS_DIR/set-ghostty-title.sh" "$AGENT_NAME_VAL" >/dev/null 2>&1 &
    fi
else
    # Make the failure observable instead of silently leaving the session
    # named pending-*. If this log ever grows, name extraction regressed.
    mkdir -p "$RUNTIME_DIR"
    printf '%s mark-agent-registered: could not resolve agent name; session not renamed (session_id=%s)\n' \
        "$(date '+%Y-%m-%dT%H:%M:%S')" "$SESSION_ID" >> "$RUNTIME_DIR/rename-failures.log"
fi

exit 0
