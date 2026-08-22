#!/bin/bash
# check-agent-registered.sh
# PreToolUse hook: blocks Edit/Write/Bash if agent has not called register_agent.
# Exit 2 = block, 0 = allow
#
# Flag file: /tmp/.claude-agent-registered-<session_id>
# Created by: mark-agent-registered.sh (PostToolUse on register_agent)

INPUT=$(cat)

HOOKS_DIR_FOR_RESOLVER="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
POLICY_LIB="$HOOKS_DIR_FOR_RESOLVER/session-identity-policy.sh"

# Extract session_id and cwd from hook input. The cwd is the only project
# evidence a client started without the launcher carries: without it the
# identity lookup ranges over every project's bindings, and two unrelated
# sessions in different projects look like one ambiguous session.
SESSION_ID=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except:
    print('')
" 2>/dev/null)
HOOK_CWD=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    value = json.loads(sys.stdin.read()).get('cwd', '')
    print(value if isinstance(value, str) else '')
except:
    print('')
" 2>/dev/null)

LOOKUP_PROJECT="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-$HOOK_CWD}}"

# Three questions, in this order, and none of them is skipped for a session
# that merely has a name:
#
#   1. does more than one identity claim this session? Acting as either would
#      misattribute the work, and this guard covers Bash, which can write any
#      file -- leaving the check to the Edit/Write guard leaves it exploitable.
#   2. can anyone register right now, i.e. is the service answering? While it
#      is not, a refusal demands something nobody can do.
#   3. does this session have an identity source?
#
# The first two used to sit behind "no AGENT_NAME and no flag", which made them
# checks about how a session was launched rather than about what is true.
if [ -f "$POLICY_LIB" ] && [ -n "$SESSION_ID" ]; then
    # shellcheck disable=SC1090
    . "$POLICY_LIB"

    if [ "$(agentstack_session_binding_conflict "$SESSION_ID" "$LOOKUP_PROJECT" "${AGENT_NAME:-}")" = "conflict" ]; then
        agentstack_conflict_message "check-agent-registered"
        exit 2
    fi

    TRANSPORT_STATE="$(agentstack_mail_transport_state)"
    if [ "$TRANSPORT_STATE" = "invalid" ]; then
        agentstack_invalid_endpoint_message "check-agent-registered"
        exit 2
    fi
    if [ "$TRANSPORT_STATE" = "unreachable" ]; then
        agentstack_audit_unmanaged "check-agent-registered" "$SESSION_ID" "transport=unreachable policy=$(agentstack_mail_outage_policy)"
        if [ "$(agentstack_mail_outage_policy)" = "block" ]; then
            echo "AGENT MAIL UNREACHABLE: registration is impossible while the service is down, and this" >&2
            echo "installation is configured to refuse work rather than continue uncoordinated." >&2
            agentstack_recovery_hint >&2
            echo "Or set AGENTSTACK_MAIL_OUTAGE_POLICY=warn-open to work through the outage." >&2
            exit 2
        fi
        if agentstack_should_report_outage "$SESSION_ID" "unreachable"; then
            agentstack_emit_visible_warning "$(agentstack_outage_warning_text)"
        fi
        # Deliberately no flag: when the service answers again, this session is
        # unregistered exactly as it was, and will be asked to register.
        exit 0
    fi
fi

# Channels bot (AGENT_NAME set) → allowed without the registration flag.
# /clear resets session_id, invalidating the flag file and blocking Bash.
# Channels bots need Bash to read AGENT_NAME for re-registration. This exempts
# them from needing the flag, not from the checks above.
#
# A placeholder is not a name: "pending-1234" is a session that has not been
# given an identity yet, and letting it stand in for one turned the exemption
# into "anything non-empty in the environment". The resolver and the conflict
# scan already refuse these; this is the third place that had its own idea.
if [ -n "$AGENT_NAME" ] && [ -f "$POLICY_LIB" ]; then
    if ! agentstack_is_placeholder_name "$AGENT_NAME"; then
        exit 0
    fi
elif [ -n "$AGENT_NAME" ]; then
    exit 0
fi

[ -z "$SESSION_ID" ] && exit 0  # Can't identify session — fail open

FLAG="/tmp/.claude-agent-registered-${SESSION_ID}"

if [ ! -f "$FLAG" ] && [ -f "$POLICY_LIB" ]; then
    TRANSPORT_STATE="${TRANSPORT_STATE:-reachable}"
    if agentstack_session_is_unmanaged; then
        agentstack_audit_unmanaged "check-agent-registered" "$SESSION_ID" "identity=none policy=$(agentstack_unmanaged_policy)"
        if [ "$(agentstack_unmanaged_policy)" = "warn-open" ]; then
            if agentstack_first_warning_for_session "$SESSION_ID"; then
                agentstack_emit_visible_warning "This session runs outside coordination by configuration: it has no agent identity, so it takes no file reservations and its writes are not attributed. Another agent may be editing the same files."
            fi
            exit 0
        fi
        agentstack_unmanaged_block_message "check-agent-registered"
        exit 2
    fi
fi

if [ ! -f "$FLAG" ]; then
    echo "AGENT NOT REGISTERED: call register_agent before using this tool." >&2
    echo "Follow the session startup procedure and register with mcp-agent-mail before working." >&2
    echo "  1. ensure_project -> 2. register_agent -> 3. fetch_inbox" >&2
    exit 2
fi

exit 0
