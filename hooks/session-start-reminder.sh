#!/bin/bash
# session-start-reminder.sh
# SessionStart hook for startup/resume/clear/compact.
#
# If an existing identity can be resolved, print a reminder to re-register with
# the same mcp-agent-mail name instead of generating a fresh identity.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:8765/mcp}}"
HEALTH_URL="${AGENTSTACK_MCP_HEALTH_URL:-${MCP_AGENT_MAIL_HEALTH_URL:-}}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
RESOLVED_AGENT=""
RESOLVED_AGENT_SRC="none"
SHELL_REGISTERED_AGENT=""
SHELL_REGISTRATION_ERROR=""

if [ -z "$HEALTH_URL" ]; then
    case "$MCP_URL" in
        */mcp) HEALTH_URL="${MCP_URL%/mcp}/health/liveness" ;;
        *) HEALTH_URL="${MCP_URL%/}/health/liveness" ;;
    esac
fi

# Is the mail server answering?
#
# The obvious probe -- GET the liveness URL derived above -- was wrong in a way
# that looked exactly like the server being down. AgentStack Mail serves its MCP
# path and the configured aliases and nothing else, so there is no
# /health/liveness route to answer, and `curl -sf` fails on any non-2xx. Every
# healthy install therefore reported "not running" at every session start, every
# session was told to skip registration, and no agent ever refreshed
# last_active_ts. That timestamp is what keeps the staleness sweep off an
# agent's file reservations, so a probe that could never succeed ended in
# reservations being collected out from under agents that were working.
#
# Ask the server what it is, not merely whether something answers. A bare GET
# returning 405/406 only says "some HTTP server is here"; any service that
# rejects GET would pass that. Call health_check over MCP and require the
# structured status, so a different service on the same port is not mistaken
# for this one. The liveness URL is still honoured when it answers, so a
# deployment that fronts the service with a real health route keeps working.
mail_server_is_answering() {
    if [ -n "${AGENTSTACK_MCP_HEALTH_URL:-}${MCP_AGENT_MAIL_HEALTH_URL:-}" ] &&
        curl -sf -m 2 "$HEALTH_URL" >/dev/null 2>&1; then
        # Only an explicitly configured health URL is trusted on status alone;
        # the derived one is a guess, and "some 200" is not this service.
        return 0
    fi
    response="$(curl -s -m 3 -X POST "$MCP_URL" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -w '\n__HTTP_STATUS__%{http_code}' \
        -d '{"jsonrpc":"2.0","id":"session-start","method":"tools/call","params":{"name":"health_check","arguments":{}}}' \
        2>/dev/null)" || return 1
    printf '%s' "$response" | python3 -c '
import json, sys

raw = sys.stdin.read()
marker = "\n__HTTP_STATUS__"
if marker not in raw:
    raise SystemExit(1)
body, _, status = raw.rpartition(marker)
if not status.strip().isdigit() or not 200 <= int(status.strip()) < 300:
    raise SystemExit(1)

def documents(text):
    """The endpoint may answer as JSON or as an SSE stream."""
    stripped = text.strip()
    if stripped.startswith("{"):
        yield stripped
    for line in text.splitlines():
        if line.startswith("data:"):
            yield line[5:].strip()

for document in documents(body):
    try:
        message = json.loads(document)
    except ValueError:
        continue
    if not isinstance(message, dict):
        continue
    # A JSON-RPC reply to the call we made, carrying a healthy status. Anything
    # less -- an error object that happens to contain "ok", a bare status
    # document, someone else true JSON -- is not this server saying it is well.
    if message.get("jsonrpc") != "2.0" or message.get("id") != "session-start":
        continue
    if message.get("error") is not None:
        continue
    result = message.get("result")
    if not isinstance(result, dict):
        continue
    health = result.get("structuredContent")
    if not isinstance(health, dict):
        continue
    if health.get("status") == "ok":
        raise SystemExit(0)
raise SystemExit(1)
' >/dev/null 2>&1
}

if [ -f "$HOOKS_DIR/resolve-agent-name.sh" ]; then
    # shellcheck disable=SC1091
    source "$HOOKS_DIR/resolve-agent-name.sh"
fi

find_register_lib() {
    local candidate
    if [ -n "${AGENTSTACK_REGISTER_LIB:-}" ] && [ -f "$AGENTSTACK_REGISTER_LIB" ]; then
        printf '%s\n' "$AGENTSTACK_REGISTER_LIB"
        return 0
    fi
    for candidate in \
        "${AGENTSTACK_HOME:-$HOME/.agentstack}/bin/lib/agentstack-register.sh" \
        "$HOOKS_DIR/../bin/lib/agentstack-register.sh" \
        "$HOME/.agentstack/bin/lib/agentstack-register.sh"; do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# True when this session's agent-mail MCP server is the per-child proxy that
# spawn_child.sh configured. The proxy injects the token, so the agent must not
# read it: doing so triggers a Bash approval prompt and pulls the secret into
# the model's context for no benefit.
child_uses_mcp_proxy() {
    agent_name="$1"
    [ -n "$agent_name" ] || return 1
    # Claude children get --mcp-config; Codex children get their own CODEX_HOME.
    [ -n "${CLAUDE_CHILD_MCP_CONFIG:-}" ] && return 0
    [ -f "$RUNTIME_DIR/child-agents/${agent_name}.mcp.json" ] && return 0
    [ -f "$RUNTIME_DIR/child-agents/${agent_name}.codex-home/config.toml" ] && return 0
    return 1
}

shell_register_resolved_agent() {
    local register_lib restored_token work_dir model
    [ -n "$RESOLVED_AGENT" ] || return 1
    [ -n "$PROJECT_KEY" ] || return 1
    register_lib="$(find_register_lib)" || return 1
    # shellcheck disable=SC1090
    . "$register_lib" || return 1

    ags_mail_load_token
    restored_token="${CHILD_REGISTRATION_TOKEN:-}"
    if [ -z "$restored_token" ]; then
        restored_token="$(ags_load_registration_token "$RESOLVED_AGENT" 2>/dev/null || true)"
    fi
    [ -n "$restored_token" ] || return 1

    CHILD_REGISTRATION_TOKEN="$restored_token"
    export CHILD_REGISTRATION_TOKEN
    work_dir="${PWD:-$PROJECT_KEY}"
    model="${AGENTSTACK_CLAUDE_MODEL:-claude-code}"
    ags_register_session "$PROJECT_KEY" "claude-code" "$model" "cc" "$work_dir" "$RESOLVED_AGENT" "reserved" >/dev/null 2>&1
    register_status=$?
    if [ "$register_status" -ne 0 ]; then
        if [ "$register_status" -eq 2 ] && [ "${AGS_AGENT_NAME_SUBSTITUTED:-0}" = "1" ]; then
            SHELL_REGISTRATION_ERROR="agent-mail changed reserved identity '$RESOLVED_AGENT' to '${AGS_SERVER_RETURNED_AGENT_NAME:-unknown}'"
        fi
        return 1
    fi
    SHELL_REGISTERED_AGENT="${AGS_REGISTERED_AGENT_NAME:-$RESOLVED_AGENT}"
    return 0
}

mkdir -p "$RUNTIME_DIR" 2>/dev/null
if [ -n "${TMUX_PANE:-}" ]; then
    CURRENT_SESSION=$(tmux display-message -t "$TMUX_PANE" -p '#S' 2>/dev/null)
else
    CURRENT_SESSION=$(tmux display-message -p '#S' 2>/dev/null)
fi
printf '%s src=%s resolved=%q AGENT_NAME=%q TMUX_PANE=%q TMUX=%s sess=%q\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S')" \
    "$RESOLVED_AGENT_SRC" "${RESOLVED_AGENT:-}" "${AGENT_NAME:-}" "${TMUX_PANE:-}" \
    "$([ -n "${TMUX:-}" ] && echo yes || echo no)" \
    "${CURRENT_SESSION:-}" \
    >> "$RUNTIME_DIR/session-start-resolve.log" 2>/dev/null

if mail_server_is_answering; then
    if [ -n "$RESOLVED_AGENT" ] && shell_register_resolved_agent; then
        echo "mcp-agent-mail server is running. This session is already registered."
        echo "あなたは「${SHELL_REGISTERED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。shell hook で登録済みです。"
        echo "新しい名前を生成せず、register_agent を呼び直さず、fetch_inbox から始めてください。"
        echo "1. fetch_inbox (agent_name=\"$SHELL_REGISTERED_AGENT\")"
        if child_uses_mcp_proxy "$SHELL_REGISTERED_AGENT"; then
            # The local proxy holds this agent's token and authenticates every
            # call. Telling the agent to read the token anyway costs a Bash
            # approval prompt for nothing, and puts the secret in its context.
            echo "この接続はローカル MCP proxy 経由で既に認証済みです。token ファイルを読む必要はありません（読まないでください）。"
        else
            echo "初回の fetch_inbox/whois では、$RUNTIME_DIR/agent_token_${SHELL_REGISTERED_AGENT} を読み、registration_token に渡してください。"
        fi
    elif [ -n "$RESOLVED_AGENT" ]; then
        echo "mcp-agent-mail server is running. Register this session before working."
        if [ -n "$SHELL_REGISTRATION_ERROR" ]; then
            echo "ERROR: $SHELL_REGISTRATION_ERROR。identity split を避けるため停止しました。別名を生成・採用せず、この不一致を operator に報告してください。"
        else
            echo "あなたの名前は「${RESOLVED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。新しい名前を生成せず、必ず name=\"${RESOLVED_AGENT}\" で register_agent してください。"
            echo "1. ensure_project -> 2. register_agent (name=\"$RESOLVED_AGENT\") -> 3. fetch_inbox"
        fi
    else
        echo "mcp-agent-mail server is running. Register this session before working."
        echo "1. ensure_project -> 2. register_agent (new AdjectiveScientist name if needed) -> 3. fetch_inbox"
    fi
else
    echo "mcp-agent-mail server is not running; skip registration until it is available."
fi
