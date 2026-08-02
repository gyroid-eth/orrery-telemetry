#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="$HOME/.agentstack/integrations/codex_app"
if [[ -f "$SCRIPT_DIR/../install-state.json" ]]; then
  DEFAULT_INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
ALLOW_STOPPED=false
REQUEUE_MESSAGE=""
REQUEUE_AGENT=""
CLEANUP_ORPHANS=false

usage() {
  cat <<'EOF'
Usage: doctor-codex-app-integration.sh [options]

Options:
  --allow-stopped     Report a missing Bridge socket as a warning
  --requeue-message ID  Explicitly reset one failed/dead-letter delivery
  --agent-name NAME     Agent binding for --requeue-message
  --cleanup-orphan-bindings
                       Retire/purge bindings without a Desktop rollout
  --install-dir PATH  Default: ~/.agentstack/integrations/codex_app
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-stopped) ALLOW_STOPPED=true; shift ;;
    --requeue-message) REQUEUE_MESSAGE="$2"; shift 2 ;;
    --agent-name) REQUEUE_AGENT="$2"; shift 2 ;;
    --cleanup-orphan-bindings) CLEANUP_ORPHANS=true; shift ;;
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

if [[ -n "$REQUEUE_MESSAGE" || -n "$REQUEUE_AGENT" ]]; then
  [[ "$REQUEUE_MESSAGE" =~ ^[1-9][0-9]*$ ]] \
    || fail "--requeue-message must be a positive integer"
  [[ -n "$REQUEUE_AGENT" ]] || fail "--agent-name is required with --requeue-message"
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

STDOUT_LOG="$RUNTIME_DIR/bridge.stdout.log"
STDERR_LOG="$RUNTIME_DIR/bridge.stderr.log"
PLIST_PATH="$INSTALL_DIR/launchd/${AGENTSTACK_CODEX_APP_LAUNCHD_LABEL:-org.agentstack.codex-app-bridge}.plist"
if [[ -f "$PLIST_PATH" ]] && python3 - "$PLIST_PATH" "$STDOUT_LOG" "$STDERR_LOG" <<'PY'
import pathlib
import plistlib
import sys

plist_path, expected_stdout, expected_stderr = sys.argv[1:]
with pathlib.Path(plist_path).open("rb") as handle:
    payload = plistlib.load(handle)
if payload.get("StandardOutPath") != expected_stdout:
    raise SystemExit("launchd stdout path mismatch")
if payload.get("StandardErrorPath") != expected_stderr:
    raise SystemExit("launchd stderr path mismatch")
PY
then
  ok "launchd log paths"
else
  fail "launchd log paths missing or mismatched"
fi

for log_path in "$STDOUT_LOG" "$STDERR_LOG"; do
  if [[ -L "$log_path" ]]; then
    fail "launchd log must not be a symlink: $log_path"
  elif [[ -e "$log_path" && ! -f "$log_path" ]]; then
    fail "launchd log is not a regular file: $log_path"
  elif [[ -e "$log_path" && ! -w "$log_path" ]]; then
    fail "launchd log is not writable: $log_path"
  fi
done

if [[ -f "$STDERR_LOG" ]] && python3 - "$STDERR_LOG" "$PLIST_PATH" 2>/dev/null <<'PY'
import pathlib
import sys

log_path, plist_path = map(pathlib.Path, sys.argv[1:])
if log_path.stat().st_mtime_ns < plist_path.stat().st_mtime_ns:
    raise SystemExit(1)
text = log_path.read_text(encoding="utf-8", errors="replace")
if '"event":"bridge_launcher_start"' not in text:
    raise SystemExit(1)
if '"event":"bridge_start"' not in text:
    raise SystemExit(1)
PY
then
  ok "Bridge startup diagnostic"
elif [[ "$ALLOW_STOPPED" == true ]]; then
  warn "Bridge startup diagnostic absent (allowed while stopped)"
else
  fail "Bridge startup diagnostic absent or older than installed plist"
fi

shopt -s nullglob
STALE_DRAINS=(
  "$RUNTIME_DIR"/.registration-retry.jsonl.drain-*
  "$RUNTIME_DIR"/.hook-events.jsonl.drain-*
)
shopt -u nullglob
if (( ${#STALE_DRAINS[@]} == 0 )); then
  ok "no stale spool drains"
else
  fail "${#STALE_DRAINS[@]} stale spool drain(s) remain"
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

DELIVERY_DB="${AGENTSTACK_CODEX_APP_DELIVERY_DB:-}"
SNAPSHOT_PATH="${AGENTSTACK_CODEX_APP_SNAPSHOT:-}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
if [[ "$CLEANUP_ORPHANS" == true ]]; then
  if [[ -z "${AGENTSTACK_MCP_URL:-}" || -z "$RUNTIME_DIR" || -z "$SNAPSHOT_PATH" ]]; then
    fail "cleanup requires agent-mail URL, runtime dir, and snapshot path"
  else
    if [[ -z "${MCP_AGENT_MAIL_TOKEN:-}" && -f "${AGENTSTACK_MAIL_ENV:-}" ]]; then
      export MCP_AGENT_MAIL_TOKEN
      IFS= read -r MCP_AGENT_MAIL_TOKEN < <(
        "$PYTHON_BIN" - "${AGENTSTACK_MAIL_ENV}" <<'PY'
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
    if PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$INSTALL_DIR/src" \
      "$PYTHON_BIN" - "$RUNTIME_DIR" "$SNAPSHOT_PATH" \
        "${CODEX_HOME:-$HOME/.codex}/sessions" <<'PY'
import os
import pathlib
import sys

from agentstack_codex_app.agent_mail_client import (
    AgentMailClient,
    HttpJsonRpcTransport,
)
from agentstack_codex_app.daemon import cleanup_orphan_bindings
from agentstack_codex_app.identity_store import IdentityStore
from agentstack_codex_app.snapshot import SnapshotStore

runtime_dir, snapshot_path, sessions_root = sys.argv[1:]
client = AgentMailClient(
    HttpJsonRpcTransport(
        os.environ["AGENTSTACK_MCP_URL"],
        bearer_token=os.environ.get("MCP_AGENT_MAIL_TOKEN"),
    )
)
report = cleanup_orphan_bindings(
    IdentityStore(pathlib.Path(runtime_dir) / "identity"),
    SnapshotStore(snapshot_path),
    client,
    sessions_root=sessions_root,
)
for binding in report.cleaned:
    print(
        "cleaned orphan binding "
        f"{binding['agent_name']} ({binding['external_id']})"
    )
for failure in report.failures:
    print(
        "orphan cleanup failed "
        f"{failure.agent_name} ({failure.external_id}): "
        f"{failure.error_code}",
        file=sys.stderr,
    )
print(
    "cleanup complete: "
    f"{len(report.cleaned)} cleaned, {len(report.failures)} failed"
)
if report.failures:
    raise SystemExit(1)
PY
    then
      ok "orphan binding cleanup"
    else
      fail "orphan binding cleanup"
    fi
  fi
fi

if [[ -n "$DELIVERY_DB" && -f "$DELIVERY_DB" ]]; then
  while IFS=$'\t' read -r DELIVERY_AGENT DELIVERY_MESSAGE DELIVERY_STATUS \
    DELIVERY_ATTEMPTS DELIVERY_ERROR; do
    [[ -n "$DELIVERY_AGENT" ]] || continue
    warn "delivery agent=$DELIVERY_AGENT message=$DELIVERY_MESSAGE status=$DELIVERY_STATUS attempts=$DELIVERY_ATTEMPTS error=$DELIVERY_ERROR"
  done < <(
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$INSTALL_DIR/src" \
      "$PYTHON_BIN" - "$DELIVERY_DB" <<'PY'
import pathlib
import sys
from agentstack_codex_app.delivery import DeliveryManager

manager = DeliveryManager(pathlib.Path(sys.argv[1]))
for row in manager.rows():
    if row["last_error"]:
        print(
            row["agent_name"],
            row["message_id"],
            row["status"],
            row["attempt_count"],
            row["last_error"],
            sep="\t",
        )
PY
  )
fi

if [[ -n "$REQUEUE_MESSAGE" && -n "$REQUEUE_AGENT" ]]; then
  if [[ -z "$DELIVERY_DB" || -z "$SNAPSHOT_PATH" ]]; then
    fail "delivery DB or snapshot path is not configured"
  elif PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$INSTALL_DIR/src" \
    "$PYTHON_BIN" - "$DELIVERY_DB" "$SNAPSHOT_PATH" \
      "${AGENTSTACK_PROJECT_KEY:-}" "$REQUEUE_AGENT" "$REQUEUE_MESSAGE" <<'PY'
import pathlib
import sys
from agentstack_codex_app.delivery import DeliveryManager
from agentstack_codex_app.snapshot import SnapshotStore, read_snapshot

database, snapshot_path, project_key, agent_name, raw_message_id = sys.argv[1:]
message_id = int(raw_message_id)
snapshot = read_snapshot(snapshot_path)
runtime = next(
    (
        item for item in snapshot["runtimes"]
        if item["project_key"] == project_key and item["agent_name"] == agent_name
    ),
    None,
)
if runtime is None:
    raise SystemExit("matching runtime snapshot not found")
if runtime["agent_id"] is not None:
    raise SystemExit("cold-wake requeue is supported only for root runtimes")
manager = DeliveryManager(pathlib.Path(database))
if not manager.requeue(project_key, agent_name, message_id):
    raise SystemExit("delivery is not failed/dead-letter or does not exist")
status = manager.status(project_key, agent_name)
runtime["state"] = "waiting"
runtime["delivery"] = {
    "pending_count": status.pending_count,
    "wake_status": "pending",
    "failed_count": status.failed_count,
    "dead_letter_count": status.dead_letter_count,
    "last_error": None,
    "parent_external_id": None,
}
SnapshotStore(snapshot_path).upsert(runtime)
PY
  then
    ok "requeued delivery message $REQUEUE_MESSAGE for $REQUEUE_AGENT"
  else
    fail "unable to requeue delivery message $REQUEUE_MESSAGE for $REQUEUE_AGENT"
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

IFS=$'\t' read -r SERVICE_KIND SERVICE_IDENTITY < <(
  python3 - "$MANIFEST" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
launchd = data.get("launchd", {})
service = data.get("service", {})
kind = str(service.get("kind") or ("launchd" if launchd.get("enabled") else "disabled"))
if kind == "launchd":
    identity = str(service.get("label") or launchd.get("label", ""))
elif kind == "nohup":
    identity = str(service.get("pidfile", ""))
else:
    identity = ""
print(kind + "\t" + identity)
PY
)
case "${SERVICE_KIND:-disabled}" in
  launchd)
    if launchd_record="$(launchctl print "gui/$(id -u)/$SERVICE_IDENTITY" 2>/dev/null)"; then
      if printf '%s\n' "$launchd_record" | grep -Eq \
        '^[[:space:]]*(state[[:space:]]*=[[:space:]]*running|pid[[:space:]]*=[[:space:]]*[1-9][0-9]*)[[:space:]]*$'
      then
        ok "Bridge service mode launchd (running)"
      else
        fail "launchd Bridge is loaded but not running"
      fi
    else
      fail "launchd Bridge is not registered"
    fi
    ;;
  nohup)
    supervisor_pid="$(sed -n '1p' "$SERVICE_IDENTITY" 2>/dev/null || true)"
    if [[ "$supervisor_pid" =~ ^[0-9]+$ ]] && kill -0 "$supervisor_pid" 2>/dev/null; then
      ok "Bridge service mode supervised-background (pid $supervisor_pid)"
    else
      fail "supervised Bridge pidfile is stale or missing: $SERVICE_IDENTITY"
    fi
    ;;
  disabled)
    ok "launchd registration intentionally disabled"
    ;;
  manual)
    fail "Bridge service requires a manual start"
    ;;
  *)
    fail "unknown Bridge service mode: $SERVICE_KIND"
    ;;
esac

exit "$status"
