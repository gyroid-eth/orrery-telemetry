#!/bin/bash
# session-identity-policy.sh
# Shared policy for sessions that have no identity source at all.
#
# The guards in this repo assume a launcher (agent-start / spawn_child) put an
# identity in the environment before starting the agent. A client that starts
# Claude Code directly -- an IDE's agent panel, the desktop app, a bare `claude`
# -- never gets one.
#
# Having no identity source does NOT mean the session cannot register. A tester
# reported an IDE panel that looked structurally excluded, and the same client
# had registered successfully on other days: what failed that day was the mail
# service, not the launch path. Whether registration is possible is a question
# about the service (see the transport section at the end of this file), not
# about how the client was started.
#
# What this policy decides is narrower: what to do with a session that has no
# identity source while the service is answering. Such a session can register,
# so the default is to require it. The opt-out exists for operators who
# knowingly run a client outside coordination.
#
# Policy is explicit and overridable:
#   AGENTSTACK_UNMANAGED_SESSION_POLICY=block       (default) refuse, and say how to leave
#   AGENTSTACK_UNMANAGED_SESSION_POLICY=warn-open             allow, warn, audit
#
# warn-open trades coordination away: such a session edits without an identity
# and without a reservation, so its writes are invisible to the other agents
# sharing the project. That is a choice to make deliberately, not a default.

AGENTSTACK_POLICY_RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
# The default is a product decision, kept in one place so it can be read and
# changed without hunting through the guards.
AGENTSTACK_UNMANAGED_DEFAULT_POLICY="block"

# One definition of "not an identity", shared by everything that asks. The
# resolver, the guards and this scan each had their own idea, so the same name
# was a claim in one place and nothing in another.
agentstack_is_placeholder_name() {
    case "$1" in
        ""|pending-*|warm-*|claimed-*|mail-watcher) return 0 ;;
        *) return 1 ;;
    esac
}

# Prints the identity source: env, tmux, or none.
agentstack_identity_source() {
    # The same predicate the conflict scan and the flag exemption use. Calling
    # any nonempty AGENT_NAME an identity here made one value mean two things:
    # not an identity when deciding conflicts, an identity when deciding
    # whether the unmanaged policy applies.
    if [ -n "${AGENT_NAME:-}" ] && ! agentstack_is_placeholder_name "$AGENT_NAME"; then
        printf 'env\n'
        return 0
    fi
    # tmux counts only when the targeted pane resolves to a real session name.
    # Being inside tmux is not an identity: a pane still called "pending-4820",
    # or a stale TMUX inherited by a client that is not in tmux at all, left the
    # session classed as managed and shut it out of the explicit opt-out.
    if [ -n "${TMUX_PANE:-}" ]; then
        local exact
        exact="$(tmux display-message -t "$TMUX_PANE" -p '#S' 2>/dev/null)"
        if [ -n "$exact" ] && ! agentstack_is_placeholder_name "$exact"; then
            printf 'tmux\n'
            return 0
        fi
    fi
    printf 'none\n'
}

agentstack_session_is_unmanaged() {
    [ "$(agentstack_identity_source)" = "none" ]
}

agentstack_unmanaged_policy() {
    # Unset and set-to-empty are different statements. Unset means "no opinion",
    # which takes the default; an empty value means somebody wrote the setting
    # and got it wrong, and a misconfigured guard must not resolve to the loose
    # side of the choice.
    if [ -z "${AGENTSTACK_UNMANAGED_SESSION_POLICY+set}" ]; then
        printf '%s\n' "$AGENTSTACK_UNMANAGED_DEFAULT_POLICY"
        return 0
    fi
    case "$AGENTSTACK_UNMANAGED_SESSION_POLICY" in
        block) printf 'block\n' ;;
        warn-open) printf 'warn-open\n' ;;
        *) printf 'block\n' ;;
    esac
}

# Record the degrade. The audit log is the durable half: a warning that only
# reaches stderr on an allowed call is not shown to anyone.
# Bounded on purpose. This machine already produced a 677 MB service log by
# appending forever, and an audit trail that fills a disk stops being one.
AGENTSTACK_AUDIT_MAX_BYTES="${AGENTSTACK_AUDIT_MAX_BYTES:-2097152}"

agentstack_audit_unmanaged() {
    local guard="$1" session_id="$2" detail="$3"
    local log_file="$AGENTSTACK_POLICY_RUNTIME_DIR/logs/unmanaged_sessions.jsonl"
    mkdir -p "$(dirname "$log_file")" 2>/dev/null || true
    # Size check, rotation and append happen inside one lock. As three separate
    # steps they were not a transaction: matching hooks run in parallel, and two
    # writers could both decide to rotate, with the second moving the first's
    # fresh log over the generation it was supposed to keep.
    AGENTSTACK_AUDIT_GUARD="$guard" \
    AGENTSTACK_AUDIT_SESSION="$session_id" \
    AGENTSTACK_AUDIT_DETAIL="$detail" \
    AGENTSTACK_AUDIT_LOG="$log_file" \
    AGENTSTACK_AUDIT_MAX_BYTES="${AGENTSTACK_AUDIT_MAX_BYTES:-2097152}" \
    python3 - <<'AUDITPY' 2>/dev/null || true
import fcntl
import json
import os
import pathlib
import time

log = pathlib.Path(os.environ["AGENTSTACK_AUDIT_LOG"])
raw_limit = os.environ.get("AGENTSTACK_AUDIT_MAX_BYTES", "")
try:
    limit = int(raw_limit)
except ValueError:
    limit = 2 * 1024 * 1024
# A nonsense limit must not silently mean "never rotate" or "rotate every call".
if limit <= 0:
    limit = 2 * 1024 * 1024

record = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "event": "unmanaged_session",
    "guard": os.environ.get("AGENTSTACK_AUDIT_GUARD", ""),
    "session_id": os.environ.get("AGENTSTACK_AUDIT_SESSION", "") or "unknown",
    "cwd": os.getcwd(),
    "detail": os.environ.get("AGENTSTACK_AUDIT_DETAIL", ""),
}

lock_path = log.with_suffix(log.suffix + ".lock")
with open(lock_path, "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        try:
            oversized = log.stat().st_size > limit
        except OSError:
            oversized = False
        if oversized:
            os.replace(log, log.with_suffix(log.suffix + ".1"))
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
AUDITPY
}


# True the first time this session is seen, so one session does not emit a
# warning on every single tool call.
agentstack_first_warning_for_session() {
    local session_id="$1"
    [ -z "$session_id" ] && return 0
    # Matching hooks run in parallel, so test-then-create would let two of them
    # both decide they were first. mkdir either creates or fails, in one step.
    local marker_dir="$AGENTSTACK_POLICY_RUNTIME_DIR/unmanaged-warned"
    mkdir -p "$marker_dir" 2>/dev/null || return 0
    local safe_id
    safe_id=$(printf '%s' "$session_id" | tr -c 'a-zA-Z0-9_-' '_')
    mkdir "$marker_dir/$safe_id" 2>/dev/null || return 1
    return 0
}

# A warning on stderr is not shown for a call that was allowed, so the operator
# would never see the one thing they need to know. Structured output is what
# actually surfaces.
agentstack_emit_visible_warning() {
    local message="$1"
    AGENTSTACK_WARNING_TEXT="$message" python3 - <<'WARNPY' 2>/dev/null || true
import json
import os

print(json.dumps({"systemMessage": os.environ.get("AGENTSTACK_WARNING_TEXT", "")}))
WARNPY
}

# Message shown when the policy is block. It must name an escape that works
# from outside the session, because everything inside it is what got blocked.
agentstack_unmanaged_block_message() {
    local guard="$1"
    echo "UNMANAGED SESSION BLOCKED ($guard): this session has no agent identity." >&2
    echo "The mail service is answering, so registering is possible from here: call the mail MCP's" >&2
    echo "register_agent tool, which records the identity this session will act under." >&2
    echo "If this client has no mail MCP, choose one of these from outside the session:" >&2
    echo "  - start the agent with agent-start, which registers an identity before launching Claude" >&2
    echo "  - export AGENTSTACK_UNMANAGED_SESSION_POLICY=warn-open to run it outside coordination" >&2
    echo "  - remove the guard from the PreToolUse hooks in ~/.claude/settings.json" >&2
}

# --- mail transport ------------------------------------------------------
#
# Two different situations used to be judged by one switch, and they are not
# the same question:
#
#   identity   this client does not participate in coordination even though the
#              service is running. An operator's choice, opted into explicitly.
#   transport  the coordination service is not answering right now. A failure,
#              not a choice, and one that no session can resolve from inside
#              itself: while the service is down, nobody can register and nobody
#              can take or check a reservation.

AGENTSTACK_MAIL_OUTAGE_DEFAULT_POLICY="warn-open"

agentstack_mail_outage_policy() {
    if [ -z "${AGENTSTACK_MAIL_OUTAGE_POLICY+set}" ]; then
        printf '%s\n' "$AGENTSTACK_MAIL_OUTAGE_DEFAULT_POLICY"
        return 0
    fi
    case "$AGENTSTACK_MAIL_OUTAGE_POLICY" in
        block) printf 'block\n' ;;
        warn-open) printf 'warn-open\n' ;;
        *) printf 'block\n' ;;
    esac
}

# One reader for the installed environment, so every setting a raw client needs
# comes from the same place the endpoint does.
agentstack_installed_env_value() {
    local name="$1"
    local env_file="${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh"
    [ -f "$env_file" ] || return 0
    (
        # shellcheck disable=SC1090
        . "$env_file" >/dev/null 2>&1 || exit 0
        eval "printf '%s' \"\${$name:-}\""
    )
}

# The endpoint the guards ask about. A client started without the launcher has
# none of the installer's environment, so falling back to a hard-coded port
# would ask about a service the install may not even use.
agentstack_mail_endpoint() {
    if [ -n "${AGENTSTACK_MCP_URL:-}" ]; then
        printf '%s\n' "$AGENTSTACK_MCP_URL"
        return 0
    fi
    if [ -n "${MCP_URL:-}" ]; then
        printf '%s\n' "$MCP_URL"
        return 0
    fi
    local from_install
    from_install="$(agentstack_installed_env_value AGENTSTACK_MCP_URL)"
    if [ -z "$from_install" ]; then
        from_install="$(agentstack_installed_env_value MCP_URL)"
    fi
    printf '%s\n' "$from_install"
}

# Prints "reachable", "unreachable", or "invalid".
agentstack_mail_transport_state() {
    AGENTSTACK_PROBE_URL="$(agentstack_mail_endpoint)" python3 - <<'PROBEPY' 2>/dev/null || printf 'invalid\n'
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

url = (os.environ.get("AGENTSTACK_PROBE_URL") or "").strip()
parsed = urlparse(url)
if parsed.scheme not in ("http", "https") or not parsed.hostname:
    print("invalid")
    raise SystemExit(0)
try:
    port = parsed.port
except ValueError:
    print("invalid")
    raise SystemExit(0)
if port is not None and not (0 < port < 65536):
    print("invalid")
    raise SystemExit(0)


class _KeepTheFirstAnswer(urllib.request.HTTPRedirectHandler):
    """Do not follow the redirect: a 302 is an answer, and where it points is
    a different question. Following it made a live endpoint in front of a dead
    upstream read as an outage."""

    def redirect_request(self, *_args, **_kwargs):
        return None


opener = urllib.request.build_opener(_KeepTheFirstAnswer)
request = urllib.request.Request(url, method="HEAD")
try:
    opener.open(request, timeout=2.0).close()
except urllib.error.HTTPError:
    print("reachable")
except (urllib.error.URLError, OSError, TimeoutError):
    print("unreachable")
except Exception:
    print("invalid")
else:
    print("reachable")
PROBEPY
}

# True when this session should be told about the outage now. A single warning
# per session hides a failure that lasts for days, so the state and a coarse
# time bucket are both part of the claim.
agentstack_should_report_outage() {
    local session_id="$1" state="$2"
    local marker_dir="$AGENTSTACK_POLICY_RUNTIME_DIR/outage-warned"
    mkdir -p "$marker_dir" 2>/dev/null || return 0
    local bucket safe_id
    bucket=$(( $(date +%s) / 600 ))
    safe_id=$(printf '%s' "${session_id:-unknown}" | tr -c 'a-zA-Z0-9_-' '_')
    mkdir "$marker_dir/${safe_id}-${state}-${bucket}" 2>/dev/null || return 1
    return 0
}

# The endpoint goes into a message the user sees, so strip anything that is not
# an address: credentials and query strings have no business in a warning.
agentstack_safe_endpoint() {
    AGENTSTACK_PROBE_URL="$(agentstack_mail_endpoint)" python3 - <<'SAFEPY' 2>/dev/null || printf 'the configured endpoint'
import os
from urllib.parse import urlparse

parsed = urlparse((os.environ.get("AGENTSTACK_PROBE_URL") or "").strip())
if parsed.scheme and parsed.hostname:
    port = ""
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    print(f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}")
else:
    print("the configured endpoint")
SAFEPY
}

# Recovery has to name something that exists. The first version told operators
# to run agentstack-mailctl, which is not installed on every install -- the same
# shape as the defect this whole change is about: a refusal that points at a
# recovery the reader cannot perform.
agentstack_recovery_hint() {
    local home="${AGENTSTACK_HOME:-$HOME/.agentstack}"
    local mailctl="$home/bin/agentstack-mailctl"
    local doctor="$home/bin/agentstack-doctor"
    local printed=1
    if [ -x "$mailctl" ]; then
        printf 'Run: %s start\n' "$mailctl"
        printed=0
    fi
    if [ -x "$doctor" ]; then
        printf 'Run: %s --report\n' "$doctor"
        printed=0
    fi
    if [ "$printed" -ne 0 ]; then
        printf 'No agentstack tools were found under %s/bin; start the agent-mail service the way this machine installs it.\n' "$home"
    fi
}

agentstack_outage_warning_text() {
    printf '%s' "The agent-mail service at $(agentstack_safe_endpoint) is not answering, so this session cannot register and no file reservation can be taken or checked. Editing continues, uncoordinated: another agent may be changing the same files and neither side will see it. $(agentstack_recovery_hint) Once the service answers, registration is required again."
}

agentstack_invalid_endpoint_message() {
    local guard="$1"
    echo "AGENT MAIL ENDPOINT UNUSABLE ($guard): no usable agent-mail endpoint is configured for this session." >&2
    echo "Checked AGENTSTACK_MCP_URL, MCP_URL, and ${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh." >&2
    echo "Without an endpoint the guards cannot tell a stopped service from a mistyped address, so they" >&2
    echo "refuse rather than assume. Set AGENTSTACK_MCP_URL to the endpoint this install uses." >&2
}


# --- identity conflicts --------------------------------------------------
#
# Asked independently of which source would win the identity. The resolver
# returns as soon as AGENT_NAME is set, so routing the conflict question
# through it meant a named session was never checked -- the check ran, looked
# at nothing, and reported no conflict.
#
# Prints "ok", "conflict", or "none".
agentstack_session_binding_conflict() {
    local session_id="$1" project="$2" env_name="$3"
    case "$session_id" in
        ""|*[!a-zA-Z0-9_-]*) printf 'none\n'; return 0 ;;
    esac
    agentstack_is_placeholder_name "$env_name" && env_name=""

    # The tmux claim is collected here too, independently of which source would
    # win. The precedence resolver returns on AGENT_NAME, so a session whose
    # launcher name disagrees with the tmux session it is actually in was never
    # compared -- and an outage then let it write under either name.
    local tmux_name="" pane_meta=""
    if [ -n "${TMUX_PANE:-}" ]; then
        tmux_name="$(tmux display-message -t "$TMUX_PANE" -p '#S' 2>/dev/null)"
        agentstack_is_placeholder_name "$tmux_name" && tmux_name=""
        # Pane metadata corroborates the tmux session; it is never a claim on
        # its own. A metadata file outlives the pane it describes, so promoting
        # a stale one to an independent claim blocks a session whose identity
        # is not in doubt -- and the refusal names bindings and environments,
        # neither of which the operator would find anything wrong with.
        if [ -n "$tmux_name" ]; then
            local pane_key="${TMUX_PANE//%/_}"
            local meta_file="$AGENTSTACK_POLICY_RUNTIME_DIR/agent_name_${pane_key}"
            if [ -f "$meta_file" ]; then
                pane_meta="$(tr -d '[:space:]' < "$meta_file" 2>/dev/null)"
                agentstack_is_placeholder_name "$pane_meta" && pane_meta=""
            fi
        fi
    fi

    AGENTSTACK_CONFLICT_TMUX_NAME="$tmux_name" \
    AGENTSTACK_CONFLICT_PANE_META="$pane_meta" \
    AGENTSTACK_CONFLICT_SESSION="$session_id" \
    AGENTSTACK_CONFLICT_PROJECT="$project" \
    AGENTSTACK_CONFLICT_ENV_NAME="$env_name" \
    AGENTSTACK_CONFLICT_DIR="$AGENTSTACK_POLICY_RUNTIME_DIR/session_index" \
    python3 - <<'CONFLICTPY' 2>/dev/null || printf 'none\n'
import json
import os
import pathlib

wanted = os.environ.get("AGENTSTACK_CONFLICT_SESSION", "")
project = os.environ.get("AGENTSTACK_CONFLICT_PROJECT", "")
env_name = os.environ.get("AGENTSTACK_CONFLICT_ENV_NAME", "")
directory = pathlib.Path(os.environ.get("AGENTSTACK_CONFLICT_DIR", ""))
names = set()
if directory.is_dir():
    for entry in directory.glob("*.json"):
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("session_id") != wanted:
            continue
        if record.get("schema_version") != 2 or record.get("binding_kind") != "self":
            continue
        caller = record.get("registered_by")
        if not isinstance(caller, str) or (caller and caller != record.get("agent_name")):
            continue
        if project:
            recorded = record.get("project_key")
            if not isinstance(recorded, str) or recorded != project:
                continue
        name = record.get("agent_name")
        if isinstance(name, str) and name:
            names.add(name)
# The claims: a launcher name, the tmux session this pane is really in, and
# what the session registered as. Pane metadata arrives here only when it was
# read alongside a live tmux session, where it corroborates rather than claims.
# Two distinct names means one of them would be writing under the other's.
for claim in (
    env_name,
    os.environ.get("AGENTSTACK_CONFLICT_TMUX_NAME", ""),
    os.environ.get("AGENTSTACK_CONFLICT_PANE_META", ""),
):
    if claim:
        names.add(claim)
print("conflict" if len(names) > 1 else ("ok" if names else "none"))
CONFLICTPY
}

agentstack_conflict_message() {
    local guard="$1"
    echo "AGENT IDENTITY CONFLICT ($guard): more than one identity claims this session." >&2
    echo "Acting under either of them would misattribute the work. Remove the binding for the" >&2
    echo "identity this session is not, under ${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/session_index," >&2
    echo "unset AGENT_NAME if it disagrees with the identity this session registered as, or clear the" >&2
    echo "pane metadata under ${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}/agent_name_* if it names a different agent than this tmux session." >&2
}
