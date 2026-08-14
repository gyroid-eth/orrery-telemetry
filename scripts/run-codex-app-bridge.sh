#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFERRED_INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$INFERRED_INSTALL_DIR}"
ENV_FILE="$INSTALL_DIR/env.sh"
printf '{"event":"bridge_launcher_start","pid":%d,"timestamp":"%s"}\n' \
  "$$" "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')" >&2
[[ -f "$ENV_FILE" ]] || {
  echo "missing Codex App integration env: $ENV_FILE" >&2
  exit 1
}
# shellcheck disable=SC1090
. "$ENV_FILE"

HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
if [[ "$HTTP_BEARER_MODE" == "disabled" ]]; then
  unset MCP_AGENT_MAIL_TOKEN
elif [[ "$HTTP_BEARER_MODE" != "auto" && "$HTTP_BEARER_MODE" != "enabled" ]]; then
  echo "invalid AGENTSTACK_MAIL_HTTP_BEARER_MODE: $HTTP_BEARER_MODE" >&2
  exit 1
elif [[ -z "${MCP_AGENT_MAIL_TOKEN:-}" && -f "${AGENTSTACK_MAIL_ENV:-}" ]]; then
  export MCP_AGENT_MAIL_TOKEN
  IFS= read -r MCP_AGENT_MAIL_TOKEN < <(
    python3 - "${AGENTSTACK_MAIL_ENV}" <<'PY'
import pathlib
import sys

for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and key.strip() == "HTTP_BEARER_TOKEN":
        print(value.strip().strip("\"'"))
        break
PY
  )
fi

PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
export PYTHONPATH="$INSTALL_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
SELF_RESTART="${AGENTSTACK_CODEX_APP_SELF_RESTART:-0}"
RESTART_DELAY="${AGENTSTACK_CODEX_APP_RESTART_DELAY:-5}"
CHILD_PIDFILE="${AGENTSTACK_CODEX_APP_RUNTIME_DIR}/bridge-child.pid"

if [[ "$SELF_RESTART" != "1" ]]; then
  exec "$PYTHON_BIN" -m agentstack_codex_app.daemon
fi

child_pid=""
stop_supervisor() {
  trap - INT TERM
  if [[ "$child_pid" =~ ^[0-9]+$ ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  rm -f "$CHILD_PIDFILE"
  exit 0
}
trap stop_supervisor INT TERM

while true; do
  "$PYTHON_BIN" -m agentstack_codex_app.daemon &
  child_pid=$!
  umask 077
  printf '%s\n' "$child_pid" > "$CHILD_PIDFILE"
  if wait "$child_pid"; then
    child_status=0
  else
    child_status=$?
  fi
  rm -f "$CHILD_PIDFILE"
  child_pid=""
  printf '{"event":"bridge_supervisor_restart","child_status":%d,"delay_seconds":%d,"timestamp":"%s"}\n' \
    "$child_status" "$RESTART_DELAY" \
    "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')" >&2
  if (( RESTART_DELAY > 0 )); then
    sleep "$RESTART_DELAY" &
    child_pid=$!
    wait "$child_pid" || true
    child_pid=""
  fi
done
