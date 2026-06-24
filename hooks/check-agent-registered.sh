#!/bin/bash
# check-agent-registered.sh
# PreToolUse hook: blocks Edit/Write/Bash if agent has not called register_agent.
# Exit 2 = block, 0 = allow
#
# Flag file: /tmp/.claude-agent-registered-<session_id>
# Created by: mark-agent-registered.sh (PostToolUse on register_agent)

INPUT=$(cat)

# Channels bot (AGENT_NAME set) → always allow
# /clear resets session_id, invalidating the flag file and blocking Bash.
# Channels bots need Bash to read AGENT_NAME for re-registration.
[ -n "$AGENT_NAME" ] && exit 0

# Extract session_id from hook input
SESSION_ID=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except:
    print('')
" 2>/dev/null)

[ -z "$SESSION_ID" ] && exit 0  # Can't identify session — fail open

FLAG="/tmp/.claude-agent-registered-${SESSION_ID}"

if [ ! -f "$FLAG" ]; then
    echo "AGENT NOT REGISTERED: call register_agent before using this tool." >&2
    echo "Follow the session startup procedure and register with mcp-agent-mail before working." >&2
    echo "  1. ensure_project -> 2. register_agent -> 3. fetch_inbox" >&2
    exit 2
fi

exit 0
