#!/bin/bash

# Conversation-only resume must not touch ORRERY Mail or owner material.
if [[ "${AGENTSTACK_MAIL_DISABLED:-0}" == "1" ]]; then
    exit 0
fi
# session-start-reminder.sh
# SessionStart hook for startup/resume/clear/compact.
#
# If an existing identity can be resolved, refresh its shell-side registration
# and print route-aware guidance. A bound proxy, raw/direct MCP, and an embedded
# task have different authentication contracts; the reminder must not send all
# three through the raw recovery helper.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
PROJECT_CONTEXT_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/project-context.sh"
# shellcheck disable=SC1090
. "$PROJECT_CONTEXT_LIB"
MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:18765/mcp}}"
HEALTH_URL="${AGENTSTACK_MCP_HEALTH_URL:-${MCP_AGENT_MAIL_HEALTH_URL:-}}"
PROJECT_KEY="$(agentstack_resolve_project_key "$(pwd -P)")"
RESOLVED_AGENT=""
RESOLVED_AGENT_SRC="none"
SHELL_REGISTERED_AGENT=""
SHELL_REGISTRATION_ERROR=""
SHELL_REGISTRATION_REASON=""

if [ -z "$HEALTH_URL" ]; then
    case "$MCP_URL" in
        */mcp) HEALTH_URL="${MCP_URL%/mcp}/health/liveness" ;;
        *) HEALTH_URL="${MCP_URL%/}/health/liveness" ;;
    esac
fi

# Is the mail server answering?
#
# The obvious probe -- GET the liveness URL derived above -- was wrong in a way
# that looked exactly like the server being down. ORRERY Mail serves its MCP
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
    # Cap what a session start will read. This runs on every session, and an
    # unbounded body is free memory amplification for anything on that port.
    response="$(curl -s -m 3 --max-filesize 262144 -X POST "$MCP_URL" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -w '\n__HTTP_STATUS__%{http_code}' \
        -d '{"jsonrpc":"2.0","id":"session-start","method":"tools/call","params":{"name":"health_check","arguments":{}}}' \
        2>/dev/null)" || return 1
    printf '%s' "$response" | python3 -c '
import json, sys

raw = sys.stdin.read(262144 + 64)
marker = "\n__HTTP_STATUS__"
if marker not in raw:
    raise SystemExit(1)
body, _, status = raw.rpartition(marker)
if not status.strip().isdigit() or not 200 <= int(status.strip()) < 300:
    raise SystemExit(1)

def documents(text):
    """The endpoint may answer as JSON or as an SSE stream.

    An SSE event carries its payload across as many ``data:`` lines as it
    likes, joined by newlines -- pretty-printed JSON arrives that way. Reading
    each line as its own document rejects a perfectly valid reply.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        yield stripped
    payload = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload.append(line[5:].lstrip())
        elif not line.strip():
            if payload:
                yield "\n".join(payload)
                payload = []
    if payload:
        yield "\n".join(payload)

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

# The SessionStart payload carries the session id, which is how a client with
# no launcher is identified: resolve-agent-name.sh looks up the binding that
# register_agent recorded for it. Without this the reminder cannot name an
# identity that already exists, and tells an established session it is nobody.
if [ ! -t 0 ]; then
    SESSION_START_INPUT="$(cat)"
    AGENTSTACK_SESSION_ID="$(printf '%s' "$SESSION_START_INPUT" | python3 -c '
import json, sys
try:
    value = json.loads(sys.stdin.read(262144)).get("session_id", "")
    print(value if isinstance(value, str) else "")
except Exception:
    print("")
' 2>/dev/null || echo "")"
    export AGENTSTACK_SESSION_ID
    SESSION_START_SOURCE="$(printf '%s' "$SESSION_START_INPUT" | python3 -c '
import json, sys
try:
    value = json.loads(sys.stdin.read(262144)).get("source", "")
    print(value if isinstance(value, str) else "")
except Exception:
    print("")
' 2>/dev/null || echo "")"
fi

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

# True when launch artifacts show that a per-child proxy was configured. This
# is enough to tailor the reminder, but it does not prove which tool surface
# the model actually received; the printed guidance still makes the real tool
# descriptions and argument schema authoritative.
child_has_mcp_proxy_config() {
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
    if [ -z "$restored_token" ]; then
        SHELL_REGISTRATION_REASON="credential-unavailable"
        return 1
    fi

    CHILD_REGISTRATION_TOKEN="$restored_token"
    export CHILD_REGISTRATION_TOKEN
    work_dir="${PWD:-$PROJECT_KEY}"
    # spawn_child.sh hands the child its model as CLAUDE_CHILD_MODEL; without
    # it this re-registration overwrote the pre-registered model with the
    # program name, and the dashboard lost the provider (no logo, chip said
    # "CLAUDE-CODE" — seen on WSL2, where no pane model is parsed either).
    # The session's own model wins over the install-wide label; a dashboard
    # resume hands the registered one the same way (#144).
    model="${CLAUDE_CHILD_MODEL:-${AGENTSTACK_CLAUDE_MODEL:-claude-code}}"
    ags_register_session "$PROJECT_KEY" "claude-code" "$model" "cc" "$work_dir" "$RESOLVED_AGENT" "reserved" >/dev/null 2>&1
    register_status=$?
    if [ "$register_status" -ne 0 ]; then
        if [ "$register_status" -eq 2 ] && [ "${AGS_AGENT_NAME_SUBSTITUTED:-0}" = "1" ]; then
            SHELL_REGISTRATION_ERROR="ORRERY Mail changed reserved identity '$RESOLVED_AGENT' to '${AGS_SERVER_RETURNED_AGENT_NAME:-unknown}'"
        fi
        return 1
    fi
    SHELL_REGISTERED_AGENT="${AGS_REGISTERED_AGENT_NAME:-$RESOLVED_AGENT}"
    record_shell_registration_index
    restore_retired_identity
    return 0
}

# A session reopened from a terminal (`claude --resume`) came back with its row
# still retired: only the dashboard resume unretired it (TRIAGE 5a, #143). The
# owner credential has just authenticated; unretire on the dashboard's terms,
# which child_resume.py judges without changing anything.
restore_retired_identity() {
    SHELL_UNRETIRE_NOTE=""
    [ -n "${AGS_REGISTERED_RETIRED_AT:-}" ] || return 0
    # Only a session being opened (again) may come back. On /clear or
    # compaction the identity was already running, so a retirement seen here
    # was made deliberately while it ran and must stand (#152 review N-1).
    case "${SESSION_START_SOURCE:-}" in
        startup|resume) ;;
        *)
            SHELL_UNRETIRE_NOTE="この identity は ORRERY Mail で retired です。/clear や compaction では active に戻しません。受信を再開するには dashboard から resume してください。"
            return 0
            ;;
    esac
    local helper="${AGENTSTACK_CHILD_RESUME_HELPER:-$HOOKS_DIR/child_resume.py}" python_bin
    # A terminal session may not carry the installer's interpreter; the
    # helper needs a newer Python than the system one on macOS.
    python_bin="${AGENTSTACK_PYTHON:-$(agentstack_installed_env_value AGENTSTACK_PYTHON 2>/dev/null)}"
    if [ ! -f "$helper" ] || [ -z "${AGS_REGISTERED_AGENT_ID:-}" ] ||
        ! "${python_bin:-python3}" "$helper" resume-eligibility \
            --runtime-dir "$RUNTIME_DIR" --agent-name "$SHELL_REGISTERED_AGENT" \
            --agent-id "$AGS_REGISTERED_AGENT_ID" --project-key "$PROJECT_KEY" \
            --program claude-code >/dev/null 2>&1; then
        SHELL_UNRETIRE_NOTE="この identity は ORRERY Mail で retired のままです（保持期間外・旧形式・purge 済み、または確認できませんでした）。受信するには dashboard から resume してください。"
        return 0
    fi
    if ags_unretire_owned_identity "$PROJECT_KEY" "$SHELL_REGISTERED_AGENT" >/dev/null 2>&1; then
        SHELL_UNRETIRE_NOTE="retired だったこの identity を、保存した owner credential で確かめて active に戻しました。"
    else
        SHELL_UNRETIRE_NOTE="この identity は ORRERY Mail で retired のままです（unretire に失敗しました）。受信するには dashboard から resume してください。"
    fi
}

# Tell exit-time cleanup that this identity is live in this process, however it
# was opened. The lease names the first non-shell ancestor (the provider CLI);
# a dead PID is simply not a live session.
write_live_session_lease() {
    local name="$1" pid="$PPID" comm depth=0 lease
    # Leases outlive their sessions; drop the ones whose CLI has ended, for
    # every identity, so they do not pile up (#152 review P3-a).
    # Also leases from before this machine booted: their PID may now be an
    # unrelated process (child_resume.py prune-leases).
    if [ ! -f "$HOOKS_DIR/child_resume.py" ] ||
        ! "${AGENTSTACK_PYTHON:-python3}" "$HOOKS_DIR/child_resume.py" prune-leases \
            --runtime-dir "$RUNTIME_DIR" >/dev/null 2>&1; then
        for lease in "$RUNTIME_DIR"/live-sessions/*/*; do
            [ -f "$lease" ] || continue
            case "${lease##*/}" in ''|*[!0-9]*) continue ;; esac
            kill -0 "${lease##*/}" 2>/dev/null || rm -f "$lease"
        done
        rmdir "$RUNTIME_DIR"/live-sessions/*/ 2>/dev/null
    fi
    case "$name" in ''|*[!A-Za-z0-9_.-]*) return 0 ;; esac
    while [ "$depth" -lt 6 ] && [ -n "$pid" ] && [ "$pid" -gt 1 ] 2>/dev/null; do
        comm="$(ps -o comm= -p "$pid" 2>/dev/null)"
        comm="${comm##*/}"; comm="${comm#-}"
        case "$comm" in
            ''|bash|sh|zsh|dash|fish|ksh|tcsh|csh) ;;
            *)
                mkdir -p "$RUNTIME_DIR/live-sessions/$name" 2>/dev/null &&
                    : > "$RUNTIME_DIR/live-sessions/$name/$pid" 2>/dev/null
                return 0
                ;;
        esac
        pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
        depth=$((depth + 1))
    done
    return 0
}

# A session registered here never calls register_agent itself, so the
# PostToolUse writer (mark-agent-registered.sh) never runs for it and no
# session index is written. Every pre-registered child is such a session, and
# without the index the dashboard falls back to guessing its transcript by
# name counts -- a parent's transcript mentions the child at least as often as
# the child's own, so "resume" opened the wrong history or none (WSL2 test,
# 2026-09-07). Write the same record from the same SessionStart payload.
record_shell_registration_index() {
    [ -n "${AGENTSTACK_SESSION_ID:-}" ] || return 0
    [ -n "${AGS_REGISTERED_AGENT_ID:-}" ] || return 0
    [ -f "$HOOKS_DIR/record-session-index.py" ] || return 0
    AGS_START_INPUT="${SESSION_START_INPUT:-}" python3 -c '
import json, os, sys
try:
    start = json.loads(os.environ.get("AGS_START_INPUT") or "{}")
except Exception:
    start = {}
agent_id, name, project_key = int(sys.argv[1]), sys.argv[2], sys.argv[3]
print(json.dumps({
    "session_id": start.get("session_id", ""),
    "transcript_path": start.get("transcript_path", ""),
    "cwd": start.get("cwd", ""),
    "tool_response": {"id": agent_id, "name": name},
    "tool_input": {"project_key": project_key, "name": name},
}))
' "$AGS_REGISTERED_AGENT_ID" "$SHELL_REGISTERED_AGENT" "$PROJECT_KEY" 2>/dev/null |
    AGENTSTACK_REGISTERING_SOURCE="${RESOLVED_AGENT_SRC:-env}" \
    AGENTSTACK_REGISTERING_AGENT="$SHELL_REGISTERED_AGENT" \
    AGENTSTACK_RUNTIME_DIR="$RUNTIME_DIR" \
        python3 "$HOOKS_DIR/record-session-index.py" >/dev/null 2>&1 || true
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

write_live_session_lease "${RESOLVED_AGENT:-}"

if mail_server_is_answering; then
    if [ -n "$RESOLVED_AGENT" ] && shell_register_resolved_agent; then
        echo "ORRERY Mail server is running. This session is already registered."
        [ -n "${SHELL_UNRETIRE_NOTE:-}" ] && echo "$SHELL_UNRETIRE_NOTE"
        echo "あなたは「${SHELL_REGISTERED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。shell hook で登録済みです。"
        echo "接続経路は正本の明示契約と、提供された tool の説明・引数 schema で判定し、最初に一致した経路だけを使ってください。"
        echo "正本 embed-task が登録済み・儀式不要を明示している場合はそれが最優先です。parent の有無を問わず task を開始し、起動儀式として inbox を取得しないでください。"
        if child_has_mcp_proxy_config "$SHELL_REGISTERED_AGENT"; then
            # Launch state says a proxy was configured, but the model's actual
            # tool schema remains the authority. Do not turn this hint into a
            # raw registration fallback if the proxy reports an error.
            echo "この identity には child proxy 設定があります。提供 tool が bound proxy schema なら、その schema の引数だけで使ってください。"
            echo "bound proxy では helper / ensure_project / register_agent / token file は不要です。runtime_status は必要な場合だけ使い、local binding の確認であって Mail 到達確認とは扱わないでください。"
            echo "proxy が unbound、identity 不一致、transport/auth failure の場合は停止してその状態を報告し、raw/helper へ自動 fallback しないでください。"
            echo "実際に提供された schema 自体が raw/direct の場合だけ managed raw/direct 経路を使います。proxy call の失敗を raw 判定の根拠にしないでください。"
        else
            echo "proxy 設定は hook から確認できません。server 名や環境変数だけで raw と決めず、提供された tool schema を確認してください。"
            echo "raw/direct schema なら shell 登録済みなので再登録は不要です。model 側 MCP 認証は別のため、初回の raw fetch_inbox/whois だけ $RUNTIME_DIR/agent_token_${SHELL_REGISTERED_AGENT} を読み、registration_token に渡してください。"
            echo "bound proxy schema なら caller identity/project/token を追加せず、その schema の引数だけで使ってください。"
        fi
    elif [ -n "$RESOLVED_AGENT" ]; then
        echo "ORRERY Mail server is running, but shell registration did not complete."
        if [ -n "$SHELL_REGISTRATION_ERROR" ]; then
            echo "ERROR: $SHELL_REGISTRATION_ERROR。identity split を避けるため停止しました。別名を生成・採用せず、この不一致を operator に報告してください。"
        elif [ "$SHELL_REGISTRATION_REASON" = "credential-unavailable" ]; then
            echo "あなたは「${RESOLVED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。local credential がありません。"
            echo "このsessionからenrollを実行せず停止し、operatorに docs/persistent-agents.md#credential-unavailable のinspect→claim/recover確認を依頼してください。"
        elif child_has_mcp_proxy_config "$RESOLVED_AGENT"; then
            echo "あなたは「${RESOLVED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。child proxy 設定があります。"
            echo "提供 tool が bound proxy schema なら、その接続だけを使ってください。unbound/transport/auth failure は報告し、helper・raw registration・token 読取へ fallback しないでください。"
            echo "実際に提供された schema 自体が raw/direct の場合だけ managed raw/direct recovery を使い、proxy failure を経路変更の根拠にしないでください。"
        else
            echo "あなたは「${RESOLVED_AGENT}」です（既存 identity・source: ${RESOLVED_AGENT_SRC}）。提供 tool schema を確認してください。"
            echo "confirmed raw/direct で既存 identity の回復が必要な場合だけ、managed instructions の token-safe helper 経路を使ってください。この失敗だけから token が stale と断定せず、盲目的に再試行・別名登録しないでください。"
        fi
    else
        echo "ORRERY Mail server is running, but no existing identity was resolved."
        echo "提供 tool schema を確認してください。bound proxy なら登録せずその schema に従い、confirmed raw/direct の新規未登録 session だけ ensure_project -> register_agent -> fetch_inbox を使ってください。"
    fi
else
    echo "ORRERY Mail server is not running; skip registration until it is available."
    if [ -n "$RESOLVED_AGENT" ]; then
        # Say who this session already is. Otherwise a resumed session waits out
        # the outage believing it is nobody, and registers a second identity for
        # itself as soon as the service returns.
        echo "あなたは既に「${RESOLVED_AGENT}」です（source: ${RESOLVED_AGENT_SRC}）。復旧後も新しい名前を生成せず、提供された接続 schema に対応する同じ identity の経路だけを使ってください。"
    fi
    echo "この間、ファイル予約は取得も確認もできません。他のエージェントと同じファイルを編集しても衝突は検出されません。"
fi

# A child started with --claude-chrome gets its browser policy again at every
# session start (startup, resume, compaction): the transcript alone may carry an
# older browser selection. The first start binds the launch's pending record to
# this session id. Sessions without a record print nothing.
if [ -n "$RESOLVED_AGENT" ] && [ -f "$HOOKS_DIR/claude_chrome_policy.py" ]; then
    case "$RESOLVED_AGENT" in
        *[!A-Za-z0-9_-]*) ;;
        *)
            python3 "$HOOKS_DIR/claude_chrome_policy.py" session \
                "$RUNTIME_DIR/child-agents" "$RESOLVED_AGENT" \
                "${AGENTSTACK_SESSION_ID:-}" "${AGENTSTACK_CLAUDE_LAUNCH_ID:-}" \
                2>/dev/null || true
            ;;
    esac
fi
