#!/bin/bash
# Shared identity, path, project, endpoint, and authentication rules for the
# file-reservation hooks. Keep this file compatible with macOS /bin/bash 3.2.

HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-${HOOKS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-${RUNTIME_DIR:-$HOME/.agentstack/runtime}}"
DEFAULT_PROJECT_KEY="$(pwd -P)"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-$DEFAULT_PROJECT_KEY}}"
PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-$PROJECT_KEY}"

POLICY_LIB_EARLY="$HOOKS_DIR/session-identity-policy.sh"
if [ -f "$POLICY_LIB_EARLY" ]; then
    # shellcheck disable=SC1090
    . "$POLICY_LIB_EARLY"
    # Empty is an answer when the installed policy has no endpoint. Filling it
    # with a legacy default could make sibling hooks talk to different servers.
    MCP_URL="$(agentstack_mail_endpoint)"
else
    MCP_URL="${AGENTSTACK_MCP_URL:-${MCP_URL:-http://127.0.0.1:8765/api/}}"
fi

if command -v agentstack_installed_env_value >/dev/null 2>&1; then
    RESERVATION_INSTALLED_MAIL_ENV="$(agentstack_installed_env_value AGENTSTACK_MAIL_ENV 2>/dev/null)"
    RESERVATION_INSTALLED_BEARER_MODE="$(agentstack_installed_env_value AGENTSTACK_MAIL_HTTP_BEARER_MODE 2>/dev/null)"
else
    RESERVATION_INSTALLED_MAIL_ENV=""
    RESERVATION_INSTALLED_BEARER_MODE=""
fi
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$RESERVATION_INSTALLED_MAIL_ENV}"
MAIL_ENV="${MAIL_ENV:-$HOME/orrery/mail/.env}"
HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-$RESERVATION_INSTALLED_BEARER_MODE}"
HTTP_BEARER_MODE="${HTTP_BEARER_MODE:-auto}"

expand_path() {
    local p="$1"
    if [[ "$p" == "~/"* ]]; then
        printf '%s\n' "$HOME/${p:2}"
    else
        printf '%s\n' "$p"
    fi
}

# Returns "<source>|<name>". Keeping the source distinguishes unresolved from
# an identity conflict, which must remain fail-closed in the reservation guard.
resolve_agent_name() {
    if [[ -f "$HOOKS_DIR/resolve-agent-name.sh" ]]; then
        # shellcheck disable=SC1091
        source "$HOOKS_DIR/resolve-agent-name.sh"
        printf '%s|%s\n' "${RESOLVED_AGENT_SRC:-none}" "${RESOLVED_AGENT:-}"
        return 0
    fi
    if [[ -n "${AGENT_NAME:-}" ]]; then
        printf 'env|%s\n' "$AGENT_NAME"
        return 0
    fi
    printf 'none|\n'
    return 0
}

get_legacy_http_bearer() {
    if [[ -n "${MCP_AGENT_MAIL_TOKEN:-}" ]]; then
        printf '%s' "$MCP_AGENT_MAIL_TOKEN"
        return 0
    fi
    if [[ -x "$HOOKS_DIR/get-mcp-agent-mail-token.sh" ]]; then
        bash "$HOOKS_DIR/get-mcp-agent-mail-token.sh" 2>/dev/null && return 0
    fi
    if command -v security >/dev/null 2>&1; then
        local keychain_token
        keychain_token=$(security find-generic-password -s "mcp-agent-mail" -a "HTTP_BEARER_TOKEN" -w 2>/dev/null || true)
        if [[ -n "$keychain_token" ]]; then
            printf '%s' "$keychain_token"
            return 0
        fi
    fi
    if [[ -f "$MAIL_ENV" ]]; then
        sed -n 's/^HTTP_BEARER_TOKEN=//p' "$MAIL_ENV" | tr -d '[:space:]'
        return 0
    fi
    return 1
}

legacy_bearer_enabled() {
    case "$HTTP_BEARER_MODE" in
        enabled) return 0 ;;
        disabled) return 1 ;;
        auto)
            case "$MCP_URL" in
                http://127.0.0.1:18765/mcp|http://127.0.0.1:18765/mcp/|http://localhost:18765/mcp|http://localhost:18765/mcp/|http://127.0.0.1:18765/api|http://127.0.0.1:18765/api/|http://localhost:18765/api|http://localhost:18765/api/)
                    return 1
                    ;;
                *) return 0 ;;
            esac
            ;;
        *)
            echo "Invalid AGENTSTACK_MAIL_HTTP_BEARER_MODE: $HTTP_BEARER_MODE" >&2
            return 2
            ;;
    esac
}

# Populate SESSION_ID, FILE_PATH, MATCHED_ROOT, REL_PATH, and
# RESERVATION_PROJECT_KEY from an Edit/Write hook document. Return 1 for the
# intentional no-op cases (no file or a file outside all protected roots).
reservation_resolve_tool_context() {
    local tool_document="$1"
    SESSION_ID=$(printf '%s' "$tool_document" | python3 -c '
import json, sys
try:
    print(json.loads(sys.stdin.read()).get("session_id", ""))
except Exception:
    print("")
' 2>/dev/null || echo "")
    export AGENTSTACK_SESSION_ID="$SESSION_ID"
    FILE_PATH=$(printf '%s' "$tool_document" | python3 -c '
import json, sys
try:
    data = json.loads(sys.stdin.read())
    tool_input = data.get("tool_input", {})
    print(tool_input.get("file_path", tool_input.get("path", "")))
except Exception:
    print("")
' 2>/dev/null || echo "")
    [ -n "$FILE_PATH" ] || return 1

    if [[ "$FILE_PATH" == /* ]]; then
        :
    elif [[ "$FILE_PATH" == "~/"* ]]; then
        FILE_PATH="$HOME/${FILE_PATH:2}"
    else
        FILE_PATH="$(pwd)/$FILE_PATH"
    fi

    MATCHED_ROOT=""
    if [[ -n "$PROTECTED_ROOTS" ]]; then
        local old_ifs="$IFS"
        local root
        IFS=":"
        for root in $PROTECTED_ROOTS; do
            root="$(expand_path "$root")"
            [[ -z "$root" ]] && continue
            [[ "$root" != "/" ]] && root="${root%/}"
            case "$FILE_PATH" in
                "$root"|"$root/"*)
                    MATCHED_ROOT="$root"
                    break
                    ;;
            esac
        done
        IFS="$old_ifs"
    fi
    [ -n "$MATCHED_ROOT" ] || return 1

    REL_PATH="${FILE_PATH#$MATCHED_ROOT/}"
    if [[ "$REL_PATH" == "$FILE_PATH" ]]; then
        REL_PATH="$(basename "$FILE_PATH")"
    fi
    RESERVATION_PROJECT_KEY="${PROJECT_KEY:-$MATCHED_ROOT}"
    export AGENTSTACK_LOOKUP_PROJECT_KEY="$RESERVATION_PROJECT_KEY"
    return 0
}

reservation_extract_session_id() {
    local tool_document="$1"
    SESSION_ID=$(printf '%s' "$tool_document" | python3 -c '
import json, sys
try:
    print(json.loads(sys.stdin.read()).get("session_id", ""))
except Exception:
    print("")
' 2>/dev/null || echo "")
    export AGENTSTACK_SESSION_ID="$SESSION_ID"
    RESERVATION_PROJECT_KEY="$PROJECT_KEY"
    export AGENTSTACK_LOOKUP_PROJECT_KEY="$RESERVATION_PROJECT_KEY"
}

reservation_failure_log() {
    local detail="$1"
    local log_file="$RUNTIME_DIR/release-failures.log"
    mkdir -p "$RUNTIME_DIR" 2>/dev/null || return 0
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$detail" >> "$log_file" 2>/dev/null || true
}

# Send release_file_reservations. The third argument is a JSON list of paths;
# omit it to release every reservation owned by the agent. Errors are durable.
reservation_release_request() {
    local agent="$1"
    local project_key="$2"
    local paths_json="${3:-}"
    local token=""
    if legacy_bearer_enabled; then
        token="$(get_legacy_http_bearer 2>/dev/null || true)"
    else
        local bearer_status=$?
        if [ "$bearer_status" -eq 2 ]; then
            reservation_failure_log "release agent=$agent project=$project_key error=invalid-bearer-mode"
            return 1
        fi
    fi
    QUERY_PROJECT_KEY="$project_key" QUERY_AGENT="$agent" \
        QUERY_PATHS_JSON="$paths_json" QUERY_TOKEN="$token" QUERY_URL="$MCP_URL" \
        QUERY_FAILURE_LOG="$RUNTIME_DIR/release-failures.log" python3 - <<'PY'
import datetime
import json
import os
import socket
import urllib.error
import urllib.request

agent = os.environ["QUERY_AGENT"]
project_key = os.environ["QUERY_PROJECT_KEY"]
url = os.environ.get("QUERY_URL", "http://127.0.0.1:8765/api/")
token = os.environ.get("QUERY_TOKEN", "")
paths_json = os.environ.get("QUERY_PATHS_JSON", "")
log_path = os.environ["QUERY_FAILURE_LOG"]

def fail(detail):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe = " ".join(str(detail).splitlines())
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} release agent={agent} project={project_key} error={safe}\n")
    except OSError:
        pass
    print(safe)
    raise SystemExit(1)

arguments = {"project_key": project_key, "agent_name": agent}
if paths_json:
    try:
        paths = json.loads(paths_json)
    except Exception as exc:
        fail(f"invalid paths JSON: {exc}")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        fail("paths must be a JSON string list")
    arguments["paths"] = paths

payload = json.dumps({
    "jsonrpc": "2.0",
    "id": "reservation-release",
    "method": "tools/call",
    "params": {"name": "release_file_reservations", "arguments": arguments},
}).encode()
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
if token:
    headers["Authorization"] = "Bearer " + token
request = urllib.request.Request(url, data=payload, headers=headers)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode()
except urllib.error.HTTPError as exc:
    detail = exc.read().decode(errors="replace").strip()
    fail(f"HTTP {exc.code}: {detail or exc.reason}")
except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
    fail(f"transport: {exc}")
except Exception as exc:
    fail(f"request failure: {exc}")

try:
    document = json.loads(body)
except Exception as exc:
    fail(f"invalid JSON response: {exc}")
if document.get("error") is not None:
    fail("MCP error: " + json.dumps(document["error"], sort_keys=True))
result = document.get("result")
if not isinstance(result, dict):
    fail("MCP result is not an object")
if result.get("isError") is True:
    fail("MCP tool result reports isError=true")
PY
}
