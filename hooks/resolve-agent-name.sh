#!/bin/bash
# resolve-agent-name.sh
# Resolve the current mcp-agent-mail identity for hook scripts.
# Priority: AGENT_NAME env -> TMUX_PANE metadata -> that pane's tmux session -> empty.
#
# Usage: source this script, then read RESOLVED_AGENT and RESOLVED_AGENT_SRC.

RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
RESOLVED_AGENT=""
RESOLVED_AGENT_SRC="none"

# One definition, shared with the guards. Keeping a private copy here meant the
# same value could be "not an identity" to one caller and a claim to another --
# which is how placeholder pane metadata came to contradict a real tmux session
# and refuse a session whose identity was never in doubt.
AGENTSTACK_POLICY_FOR_RESOLVER="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/session-identity-policy.sh"
if [ -f "$AGENTSTACK_POLICY_FOR_RESOLVER" ] && ! command -v agentstack_is_placeholder_name >/dev/null 2>&1; then
    # shellcheck disable=SC1090
    . "$AGENTSTACK_POLICY_FOR_RESOLVER"
fi

is_agent_name_placeholder() {
    if command -v agentstack_is_placeholder_name >/dev/null 2>&1; then
        agentstack_is_placeholder_name "$1"
        return $?
    fi
    # Deployed without the policy library.
    case "$1" in
        ""|pending-*|warm-*|claimed-*|mail-watcher) return 0 ;;
        *) return 1 ;;
    esac
}

# 1. Environment variable (launcher-created sessions and child agents).
#
# A placeholder is remembered as the reason nothing was found, but it does not
# end the search: returning here meant "pending-1234" left in the environment
# could hide a live tmux identity or a session that had registered perfectly
# well, making a known agent unresolvable.
if [ -n "${AGENT_NAME:-}" ]; then
    if is_agent_name_placeholder "$AGENT_NAME"; then
        RESOLVED_AGENT_SRC="placeholder-env"
    else
        RESOLVED_AGENT="$AGENT_NAME"
        RESOLVED_AGENT_SRC="env"
        return 0 2>/dev/null || exit 0
    fi
fi

# 2. Resolve the exact targeted pane session. Pane metadata is corroboration,
# never authority by itself because stale files survive pane/session reuse.
if [ -n "${TMUX_PANE:-}" ]; then
    PANE_KEY="${TMUX_PANE//%/_}"
    METADATA_FILE="$RUNTIME_DIR/agent_name_${PANE_KEY}"
    PANE_METADATA=""
    if [ -f "$METADATA_FILE" ]; then
        PANE_METADATA=$(tr -d '[:space:]' < "$METADATA_FILE" 2>/dev/null)
        # A placeholder in the metadata is not a competing identity; it is a
        # pane that had not been named when the file was written.
        if is_agent_name_placeholder "$PANE_METADATA"; then
            PANE_METADATA=""
        fi
    fi
    PANE_SESSION=$(tmux display-message -t "$TMUX_PANE" -p '#S' 2>/dev/null)
    if is_agent_name_placeholder "$PANE_SESSION"; then
        PANE_SESSION=""
    fi
    if [ -n "$PANE_SESSION" ] && [ -n "$PANE_METADATA" ] \
        && [ "$PANE_SESSION" != "$PANE_METADATA" ]; then
        RESOLVED_AGENT_SRC="identity-conflict"
        # Named on stderr so the caller can report which claim collided without
        # threading a second value back through a command substitution.
        echo "identity conflict: pane metadata ($PANE_METADATA) does not match the exact tmux session ($PANE_SESSION)." >&2
    elif [ -n "$PANE_SESSION" ]; then
        RESOLVED_AGENT="$PANE_SESSION"
        if [ -n "$PANE_METADATA" ]; then
            RESOLVED_AGENT_SRC="metafile+tmux-session"
        else
            RESOLVED_AGENT_SRC="tmux-session"
        fi
    elif [ -n "$PANE_METADATA" ]; then
        RESOLVED_AGENT_SRC="unconfirmed-metafile"
    fi
fi

# 3. A session that registered without a launcher still has an identity: the
# PostToolUse hook on register_agent wrote it into the session index. Clients
# that start Claude Code directly (an IDE panel, the desktop app) never get
# AGENT_NAME and are not inside tmux, so without this they look anonymous even
# after registering, and the reservation guard has nothing to check against.
#
# Two rules keep this from becoming a way to borrow someone else's name:
#
#   one binding      a session that has been recorded under more than one name
#                    is a conflict, not a rename. Nothing here guesses which is
#                    current -- ordering by timestamp would let whichever record
#                    happens to sort last take over the identity.
#   same project     agent names are project-local, so a binding is authority
#                    only inside the project it was made in. The caller passes
#                    the project it is about to act on.
#
# Callers pass AGENTSTACK_SESSION_ID, and AGENTSTACK_LOOKUP_PROJECT_KEY when
# they are enforcing something project-scoped.
if [ -z "$RESOLVED_AGENT" ] && [ "$RESOLVED_AGENT_SRC" != "identity-conflict" ] \
    && [ -n "${AGENTSTACK_SESSION_ID:-}" ]; then
    case "$AGENTSTACK_SESSION_ID" in
        # A session id reaches the filesystem below, so it may only look like
        # one: alphanumerics, dashes, underscores. No separators, no traversal.
        *[!a-zA-Z0-9_-]*) : ;;
        "") : ;;
        *)
            SESSION_INDEX_DIR="$RUNTIME_DIR/session_index"
            if [ -d "$SESSION_INDEX_DIR" ]; then
                INDEX_RESULT=$(
                    AGENTSTACK_LOOKUP_SESSION="$AGENTSTACK_SESSION_ID" \
                    AGENTSTACK_LOOKUP_DIR="$SESSION_INDEX_DIR" \
                    python3 - <<'INDEXPY' 2>/dev/null
import json
import os
import pathlib

wanted = os.environ.get("AGENTSTACK_LOOKUP_SESSION", "")
project = os.environ.get("AGENTSTACK_LOOKUP_PROJECT_KEY", "")
directory = pathlib.Path(os.environ.get("AGENTSTACK_LOOKUP_DIR", ""))
names = set()
if wanted and directory.is_dir():
    for entry in directory.glob("*.json"):
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("session_id") != wanted:
            continue
        name = record.get("agent_name")
        if not isinstance(name, str) or not name:
            continue
        # Authority requires a record that says, in its own schema, that it is
        # a session binding a self-registration produced. Anything older,
        # malformed, or of another kind is ignored rather than half-trusted:
        # a loose read here is what lets a wrong record decide who may write.
        if record.get("schema_version") != 2:
            continue
        if record.get("binding_kind") != "self":
            continue
        caller = record.get("registered_by")
        if not isinstance(caller, str):
            continue
        if caller and caller != name:
            continue
        if project:
            recorded = record.get("project_key")
            # A record from before project keys were stored cannot prove it
            # belongs here, and a record from elsewhere proves it does not.
            if not isinstance(recorded, str) or recorded != project:
                continue
        names.add(name)
if len(names) == 1:
    print("ok:" + names.pop())
elif names:
    # Name them: an operator cannot quarantine a stale binding they cannot find.
    print("conflict:" + ",".join(sorted(names)))
else:
    print("none:")
INDEXPY
                )
                case "$INDEX_RESULT" in
                    ok:*)
                        INDEX_NAME="${INDEX_RESULT#ok:}"
                        if ! is_agent_name_placeholder "$INDEX_NAME"; then
                            RESOLVED_AGENT="$INDEX_NAME"
                            RESOLVED_AGENT_SRC="session-index"
                        fi
                        ;;
                    conflict:*)
                        # Several identities claim this session. Refusing is the
                        # only safe answer: acting as any one of them would let
                        # a write be attributed to an agent that did not make it.
                        RESOLVED_AGENT_SRC="identity-conflict"
                        echo "identity conflict: this session is bound to more than one identity (${INDEX_RESULT#conflict:})." >&2
                        echo "  bindings live in $SESSION_INDEX_DIR; remove the record for the identity this session is not." >&2
                        ;;
                esac
            fi
            ;;
    esac
fi

# 4. With no TMUX_PANE, never query an untargeted ambient tmux session.
# Empty means unresolved. Each caller applies its own safety boundary.
