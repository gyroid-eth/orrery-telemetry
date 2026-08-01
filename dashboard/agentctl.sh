#!/bin/bash
# Agent Dashboard 管理スクリプト
#   agentctl.sh install   launchd に登録 (常駐・自動起動・自動再起動)
#   agentctl.sh uninstall launchd から解除
#   agentctl.sh restart   再起動
#   agentctl.sh status     状態確認
#   agentctl.sh open       ブラウザで開く
#   agentctl.sh fg         フォアグラウンドで起動 (デバッグ用)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL_PREFIX="${AGENTSTACK_LABEL_PREFIX:-org.agentstack}"
LABEL="$LABEL_PREFIX.agentdashboard"
PLIST_TEMPLATE="$HERE/agentdashboard.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${AGENTSTACK_PORT:-8770}"
PYTHON="${AGENTSTACK_PYTHON:-/usr/bin/python3}"
TERMINAL="${AGENTSTACK_TERMINAL:-auto}"
MAIL_DB="${AGENTSTACK_MAIL_DB:-~/mcp_agent_mail/storage.sqlite3}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-~/mcp_agent_mail/.env}"
MAIL_HOME="${AGENTSTACK_MAIL_HOME:-~/.mcp_agent_mail}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-~/.mcp_agent_mail/signals}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:8765/mcp}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-}"
PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-$PROJECT_KEY}"
DELIVERABLE_ROOTS="${AGENTSTACK_DELIVERABLE_ROOTS:-}"
HOOKS_DIR="${AGENTSTACK_HOOKS_DIR:-~/.agentstack/hooks}"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-~/.agentstack/runtime}"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-~/.agentstack/runtime/managed_agents.txt}"
VAULT="${AGENTSTACK_VAULT:-}"
PATH_VALUE="${AGENTSTACK_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
URL="http://127.0.0.1:$PORT/"
GUI="gui/$(id -u)"

sed_escape() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

render_plist() {
  sed \
    -e "s|__LABEL_PREFIX__|$(sed_escape "$LABEL_PREFIX")|g" \
    -e "s|__INSTALL_DIR__|$(sed_escape "$HERE")|g" \
    -e "s|__PYTHON__|$(sed_escape "$PYTHON")|g" \
    -e "s|__PORT__|$(sed_escape "$PORT")|g" \
    -e "s|__MAIL_DB__|$(sed_escape "$MAIL_DB")|g" \
    -e "s|__MAIL_ENV__|$(sed_escape "$MAIL_ENV")|g" \
    -e "s|__MAIL_HOME__|$(sed_escape "$MAIL_HOME")|g" \
    -e "s|__SIGNALS_DIR__|$(sed_escape "$SIGNALS_DIR")|g" \
    -e "s|__MCP_URL__|$(sed_escape "$MCP_URL")|g" \
    -e "s|__TERMINAL__|$(sed_escape "$TERMINAL")|g" \
    -e "s|__PROJECT_KEY__|$(sed_escape "$PROJECT_KEY")|g" \
    -e "s|__PROTECTED_ROOTS__|$(sed_escape "$PROTECTED_ROOTS")|g" \
    -e "s|__DELIVERABLE_ROOTS__|$(sed_escape "$DELIVERABLE_ROOTS")|g" \
    -e "s|__HOOKS_DIR__|$(sed_escape "$HOOKS_DIR")|g" \
    -e "s|__RUNTIME_DIR__|$(sed_escape "$RUNTIME_DIR")|g" \
    -e "s|__MANAGED_AGENTS_FILE__|$(sed_escape "$MANAGED_AGENTS_FILE")|g" \
    -e "s|__VAULT__|$(sed_escape "$VAULT")|g" \
    -e "s|__PATH__|$(sed_escape "$PATH_VALUE")|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DST"
}

case "${1:-status}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    render_plist
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$GUI" "$PLIST_DST"
    launchctl enable "$GUI/$LABEL"
    sleep 1
    echo "installed & started -> $URL"
    open "$URL" || true
    ;;
  uninstall)
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "uninstalled"
    ;;
  restart)
    launchctl kickstart -k "$GUI/$LABEL"
    echo "restarted"
    ;;
  status)
    launchctl print "$GUI/$LABEL" 2>/dev/null | grep -E "state =|pid =" || \
      echo "not loaded (run: agentctl.sh install)"
    curl -s -o /dev/null -w "http %{http_code}\n" "$URL" || echo "http: down"
    ;;
  open)
    open "$URL"
    ;;
  fg)
    exec "$PYTHON" "$HERE/server.py"
    ;;
  *)
    echo "usage: agentctl.sh {install|uninstall|restart|status|open|fg}"
    exit 1
    ;;
esac
