#!/bin/bash
# Agent Dashboard service control for launchd and headless/background installs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${AGENTSTACK_ENV_FILE:-$HERE/../env.sh}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

LABEL_PREFIX="${AGENTSTACK_LABEL_PREFIX:-org.agentstack}"
LABEL="$LABEL_PREFIX.agentdashboard"
PLIST_TEMPLATE="$HERE/agentdashboard.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${AGENTSTACK_PORT:-8770}"
PYTHON="${AGENTSTACK_PYTHON:-/usr/bin/python3}"
TERMINAL="${AGENTSTACK_TERMINAL:-auto}"
MAIL_DB="${AGENTSTACK_MAIL_DB:-$HOME/.agentstack/mail/storage.sqlite3}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/.agentstack/mail/.env}"
MAIL_HOME="${AGENTSTACK_MAIL_HOME:-$HOME/.agentstack/mail}"
MAIL_HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-$MAIL_HOME/signals}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:18765/mcp}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-}"
PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-$PROJECT_KEY}"
DELIVERABLE_ROOTS="${AGENTSTACK_DELIVERABLE_ROOTS:-}"
LANG_SETTING="${AGENTSTACK_LANG:-}"
MURMUR_SETTING="${AGENTSTACK_MURMUR:-}"
HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-$HOME/.agentstack/hooks}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$HOME/.agentstack/runtime}"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
DASHBOARD_LOG="${AGENTSTACK_DASHBOARD_LOG:-$RUNTIME_DIR/dashboard.log}"
DASHBOARD_LOG_MAX_BYTES="${AGENTSTACK_DASHBOARD_LOG_MAX_BYTES:-5242880}"
DASHBOARD_LOG_BACKUPS="${AGENTSTACK_DASHBOARD_LOG_BACKUPS:-3}"
DASHBOARD_RESTART_DELAY="${AGENTSTACK_DASHBOARD_RESTART_DELAY:-5}"
VAULT="${AGENTSTACK_VAULT:-}"
PATH_VALUE="${AGENTSTACK_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
PIDFILE="$RUNTIME_DIR/dashboard.pid"
URL="http://127.0.0.1:$PORT/"
GUI="gui/$(id -u)"

sed_escape() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

render_plist() {
  mkdir -p "$HOME/Library/LaunchAgents"
  sed \
    -e "s|__LABEL_PREFIX__|$(sed_escape "$LABEL_PREFIX")|g" \
    -e "s|__INSTALL_DIR__|$(sed_escape "$HERE")|g" \
    -e "s|__PYTHON__|$(sed_escape "$PYTHON")|g" \
    -e "s|__PORT__|$(sed_escape "$PORT")|g" \
    -e "s|__MAIL_DB__|$(sed_escape "$MAIL_DB")|g" \
    -e "s|__MAIL_ENV__|$(sed_escape "$MAIL_ENV")|g" \
    -e "s|__MAIL_HOME__|$(sed_escape "$MAIL_HOME")|g" \
    -e "s|__MAIL_HTTP_BEARER_MODE__|$(sed_escape "$MAIL_HTTP_BEARER_MODE")|g" \
    -e "s|__SIGNALS_DIR__|$(sed_escape "$SIGNALS_DIR")|g" \
    -e "s|__MCP_URL__|$(sed_escape "$MCP_URL")|g" \
    -e "s|__TERMINAL__|$(sed_escape "$TERMINAL")|g" \
    -e "s|__PROJECT_KEY__|$(sed_escape "$PROJECT_KEY")|g" \
    -e "s|__PROTECTED_ROOTS__|$(sed_escape "$PROTECTED_ROOTS")|g" \
    -e "s|__DELIVERABLE_ROOTS__|$(sed_escape "$DELIVERABLE_ROOTS")|g" \
    -e "s|__LANG__|$(sed_escape "$LANG_SETTING")|g" \
    -e "s|__MURMUR__|$(sed_escape "$MURMUR_SETTING")|g" \
    -e "s|__HOOKS_DIR__|$(sed_escape "$HOOKS_DIR")|g" \
    -e "s|__RUNTIME_DIR__|$(sed_escape "$RUNTIME_DIR")|g" \
    -e "s|__DASHBOARD_LOG__|$(sed_escape "$DASHBOARD_LOG")|g" \
    -e "s|__DASHBOARD_LOG_MAX_BYTES__|$(sed_escape "$DASHBOARD_LOG_MAX_BYTES")|g" \
    -e "s|__DASHBOARD_LOG_BACKUPS__|$(sed_escape "$DASHBOARD_LOG_BACKUPS")|g" \
    -e "s|__DASHBOARD_RESTART_DELAY__|$(sed_escape "$DASHBOARD_RESTART_DELAY")|g" \
    -e "s|__MANAGED_AGENTS_FILE__|$(sed_escape "$MANAGED_AGENTS_FILE")|g" \
    -e "s|__VAULT__|$(sed_escape "$VAULT")|g" \
    -e "s|__PATH__|$(sed_escape "$PATH_VALUE")|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DST"
}

background_pid() {
  sed -n '1p' "$PIDFILE" 2>/dev/null || true
}

background_running() {
  local pid
  pid="$(background_pid)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

launchd_loaded() {
  command -v launchctl >/dev/null 2>&1 && \
    launchctl print "$GUI/$LABEL" >/dev/null 2>&1
}

export_background_env() {
  export AGENTSTACK_PORT="$PORT"
  export AGENTSTACK_LABEL_PREFIX="$LABEL_PREFIX"
  export AGENTSTACK_MAIL_DB="$MAIL_DB"
  export AGENTSTACK_MAIL_ENV="$MAIL_ENV"
  export AGENTSTACK_MAIL_HOME="$MAIL_HOME"
  export AGENTSTACK_MAIL_HTTP_BEARER_MODE="$MAIL_HTTP_BEARER_MODE"
  export AGENTSTACK_SIGNALS_DIR="$SIGNALS_DIR"
  export AGENTSTACK_MCP_URL="$MCP_URL"
  export AGENTSTACK_TERMINAL="$TERMINAL"
  export AGENTSTACK_PROJECT_KEY="$PROJECT_KEY"
  export AGENTSTACK_PROTECTED_ROOTS="$PROTECTED_ROOTS"
  export AGENTSTACK_DELIVERABLE_ROOTS="$DELIVERABLE_ROOTS"
  export AGENTSTACK_LANG="$LANG_SETTING"
  export AGENTSTACK_MURMUR="$MURMUR_SETTING"
  export AGENTSTACK_HOOKS_DIR="$HOOKS_DIR"
  export AGENTSTACK_RUNTIME_DIR="$RUNTIME_DIR"
  export AGENTSTACK_MANAGED_AGENTS_FILE="$MANAGED_AGENTS_FILE"
  export AGENTSTACK_DASHBOARD_LOG="$DASHBOARD_LOG"
  export AGENTSTACK_DASHBOARD_LOG_MAX_BYTES="$DASHBOARD_LOG_MAX_BYTES"
  export AGENTSTACK_DASHBOARD_LOG_BACKUPS="$DASHBOARD_LOG_BACKUPS"
  export AGENTSTACK_DASHBOARD_RESTART_DELAY="$DASHBOARD_RESTART_DELAY"
}

start_background() {
  if background_running; then
    echo "already running in supervised-background mode (pid $(background_pid))"
    return 0
  fi
  rm -f "$PIDFILE"
  mkdir -p "$RUNTIME_DIR"
  export_background_env
  AGENTSTACK_DASHBOARD_SELF_RESTART=1 \
    nohup "$PYTHON" "$HERE/service_runner.py" >> "$DASHBOARD_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$PIDFILE"
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "failed to start supervised-background dashboard; inspect $DASHBOARD_LOG" >&2
    return 1
  fi
  echo "started in supervised-background mode (pid $pid) -> $URL"
}

stop_background() {
  local pid
  pid="$(background_pid)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    local attempts=0
    while kill -0 "$pid" 2>/dev/null && [[ "$attempts" -lt 50 ]]; do
      sleep 0.1
      attempts=$((attempts + 1))
    done
  fi
  rm -f "$PIDFILE"
}

start_any() {
  if background_running; then
    echo "already running in supervised-background mode (pid $(background_pid))"
    return 0
  fi
  if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
    render_plist
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    # bootstrap itself is the capability check: a logged-in Mac can lose the
    # GUI domain while its display sleeps.
    if launchctl bootstrap "$GUI" "$PLIST_DST" && \
       launchctl enable "$GUI/$LABEL" && \
       launchctl kickstart "$GUI/$LABEL"
    then
      echo "started in launchd mode -> $URL"
      return 0
    fi
    echo "warning: launchd bootstrap failed; using supervised-background mode" >&2
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
  fi
  start_background
}

stop_all() {
  stop_background
  # Only unload a label whose plist we wrote under this HOME. launchd labels
  # live in the user domain and ignore HOME, so a run pointed at a scratch
  # HOME -- a test, a trial install, a second checkout -- would otherwise boot
  # out the dashboard the real install owns and leave the machine without one.
  # A missing plist here means this HOME never registered the job.
  if command -v launchctl >/dev/null 2>&1 && [[ -f "$PLIST_DST" ]]; then
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
  fi
}

status_service() {
  local http_code
  http_code="$(curl -s -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || true)"
  if launchd_loaded; then
    echo "service mode: launchd"
    launchctl print "$GUI/$LABEL" 2>/dev/null | grep -E "state =|pid =" || true
  elif background_running; then
    echo "service mode: supervised-background"
    echo "pid = $(background_pid)"
  elif [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "service mode: unmanaged-background (HTTP reachable; no manager record)"
  else
    echo "service mode: none"
  fi
  if [[ -n "$http_code" && "$http_code" != "000" ]]; then
    echo "http $http_code"
  else
    echo "http: down"
  fi
}

case "${1:-status}" in
  install|start)
    start_any
    ;;
  stop)
    stop_all
    echo "stopped"
    ;;
  uninstall)
    stop_all
    rm -f "$PLIST_DST"
    echo "uninstalled"
    ;;
  restart)
    if background_running; then
      stop_background
      start_background
    elif launchd_loaded; then
      launchctl kickstart -k "$GUI/$LABEL"
      echo "restarted in launchd mode"
    else
      start_any
    fi
    ;;
  status)
    status_service
    ;;
  open)
    open "$URL"
    ;;
  fg)
    exec "$PYTHON" "$HERE/server.py"
    ;;
  *)
    echo "usage: agentctl.sh {install|start|stop|uninstall|restart|status|open|fg}"
    exit 1
    ;;
esac
