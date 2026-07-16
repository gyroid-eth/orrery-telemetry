#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="$HOME/.agentstack/integrations/codex_app"
if [[ -f "$SCRIPT_DIR/../install-state.json" ]]; then
  DEFAULT_INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
ALLOW_STOPPED=false

usage() {
  cat <<'EOF'
Usage: doctor-codex-app-integration.sh [options]

Options:
  --allow-stopped     Report a missing Bridge socket as a warning
  --install-dir PATH  Default: ~/.agentstack/integrations/codex_app
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-stopped) ALLOW_STOPPED=true; shift ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

MANIFEST="$INSTALL_DIR/install-state.json"
ENV_FILE="$INSTALL_DIR/env.sh"
status=0

ok() { printf 'ok: %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
fail() { printf 'fail: %s\n' "$*" >&2; status=1; }

if [[ -f "$MANIFEST" ]] && python3 -m json.tool "$MANIFEST" >/dev/null 2>&1; then
  ok "manifest"
else
  fail "manifest missing or invalid"
  exit "$status"
fi

if [[ -f "$ENV_FILE" ]]; then
  mode="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null || true)"
  [[ "$mode" == "600" ]] && ok "env mode 0600" || fail "env mode is ${mode:-unknown}"
  # shellcheck disable=SC1090
  . "$ENV_FILE"
else
  fail "env missing"
fi

for required in \
  "$INSTALL_DIR/src/agentstack_codex_app/daemon.py" \
  "$INSTALL_DIR/schemas/migrations/001_delivery_state.sql" \
  "$INSTALL_DIR/plugin/.codex-plugin/plugin.json" \
  "$INSTALL_DIR/marketplace/.agents/plugins/marketplace.json"; do
  [[ -f "$required" ]] && ok "present $required" || fail "missing $required"
done

RUNTIME_DIR="${AGENTSTACK_CODEX_APP_RUNTIME_DIR:-}"
SOCKET_PATH="${AGENTSTACK_CODEX_APP_SOCKET:-}"
if [[ -n "$RUNTIME_DIR" && -d "$RUNTIME_DIR" ]]; then
  mode="$(stat -f '%Lp' "$RUNTIME_DIR" 2>/dev/null || stat -c '%a' "$RUNTIME_DIR" 2>/dev/null || true)"
  [[ "$mode" == "700" ]] && ok "runtime mode 0700" || fail "runtime mode is ${mode:-unknown}"
else
  fail "runtime directory missing"
fi

if [[ -n "$SOCKET_PATH" && -S "$SOCKET_PATH" ]]; then
  mode="$(stat -f '%Lp' "$SOCKET_PATH" 2>/dev/null || stat -c '%a' "$SOCKET_PATH" 2>/dev/null || true)"
  [[ "$mode" == "600" ]] && ok "Bridge socket mode 0600" || fail "Bridge socket mode is ${mode:-unknown}"
  if python3 - "$SOCKET_PATH" <<'PY'
import socket
import sys

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(0.5)
    client.connect(sys.argv[1])
PY
  then
    ok "Bridge socket accepts connections"
  else
    fail "Bridge socket is stale or unreachable"
  fi
elif [[ "$ALLOW_STOPPED" == true ]]; then
  warn "Bridge socket absent (allowed)"
else
  fail "Bridge socket absent"
fi

if [[ -d "$INSTALL_DIR/src" ]]; then
  if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$INSTALL_DIR/src" \
    python3 - "$RUNTIME_DIR" <<'PY'
import json
import os
import pathlib
import stat
import sys
from agentstack_codex_app.identity_store import validate_binding

identity_root = pathlib.Path(sys.argv[1]) / "identity"
bindings = identity_root / "bindings"
for directory in (identity_root, bindings, identity_root / "secrets"):
    if directory.exists() and stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise SystemExit(f"unsafe identity directory mode: {directory}")
if bindings.is_dir():
    for path in bindings.glob("*.json"):
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise SystemExit(f"unsafe binding mode: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_binding(payload)
PY
  then
    ok "binding store integrity"
  else
    fail "binding store integrity"
  fi
fi

IFS=$'\t' read -r PLUGIN_ENABLED PLUGIN_ID < <(
  python3 - "$MANIFEST" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
plugin = data.get("plugin", {})
print(
    ("true" if plugin.get("enabled") else "false")
    + "\t"
    + str(plugin.get("id", ""))
)
PY
)
CODEX_BIN="${AGENTSTACK_CODEX_BINARY:-codex}"
if [[ "$PLUGIN_ENABLED" != "true" ]]; then
  ok "Codex plugin registration intentionally disabled"
elif [[ -x "$CODEX_BIN" ]] || command -v "$CODEX_BIN" >/dev/null 2>&1; then
  if "$CODEX_BIN" plugin list --json | python3 -c '
import json
import sys
plugin_id = sys.argv[1]
data = json.load(sys.stdin)
if not any(item.get("pluginId") == plugin_id for item in data.get("installed", [])):
    raise SystemExit(1)
' "$PLUGIN_ID"
  then
    ok "Codex plugin registered"
  else
    fail "Codex plugin is not registered"
  fi
else
  fail "codex command missing"
fi

IFS=$'\t' read -r LAUNCHD_ENABLED LAUNCHD_LABEL < <(
  python3 - "$MANIFEST" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
launchd = data.get("launchd", {})
print(
    ("true" if launchd.get("enabled") else "false")
    + "\t"
    + str(launchd.get("label", ""))
)
PY
)
if [[ "${LAUNCHD_ENABLED:-false}" == "true" ]]; then
  if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    ok "launchd service registered"
  else
    fail "launchd service is not registered"
  fi
else
  ok "launchd registration intentionally disabled"
fi

exit "$status"
