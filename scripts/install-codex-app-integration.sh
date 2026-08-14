#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_DIR="$REPO_ROOT/integrations/codex_app"
DRY_RUN=false
NO_SERVICE=false
NO_PLUGIN=false
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$HOME/.agentstack/integrations/codex_app}"
RUNTIME_DIR="${AGENTSTACK_CODEX_APP_RUNTIME_DIR:-$HOME/.agentstack/runtime/codex-app}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-}"
MCP_URL="${AGENTSTACK_MCP_URL:-}"
# The bearer file lives with the upstream clone.  ~/.mcp_agent_mail is the
# intentionally separate runtime/archive root used for signal data.
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$HOME/mcp_agent_mail/.env}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-$HOME/.mcp_agent_mail/signals}"
HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
LABEL="${AGENTSTACK_CODEX_APP_LAUNCHD_LABEL:-org.agentstack.codex-app-bridge}"
MARKETPLACE_NAME="${AGENTSTACK_CODEX_APP_MARKETPLACE:-agentstack-local}"
WAKE_LIMIT="${AGENTSTACK_CODEX_APP_WAKE_LIMIT_PER_HOUR:-12}"
STALE_AFTER="${AGENTSTACK_CODEX_APP_STALE_AFTER_SECONDS:-3600}"
RETRY_MAX_ATTEMPTS="${AGENTSTACK_CODEX_APP_RETRY_MAX_ATTEMPTS:-12}"
RETRY_MAX_AGE="${AGENTSTACK_CODEX_APP_RETRY_MAX_AGE_SECONDS:-3600}"
RETRY_MAX_BACKOFF="${AGENTSTACK_CODEX_APP_RETRY_MAX_BACKOFF_SECONDS:-300}"
RESTART_DELAY="${AGENTSTACK_CODEX_APP_RESTART_DELAY:-5}"
SKIP_GIT_CHECK="${AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK:-0}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-$(command -v python3 2>/dev/null || true)}"
CODEX_BIN="${AGENTSTACK_CODEX_BINARY:-$(command -v codex 2>/dev/null || true)}"

usage() {
  cat <<'EOF'
Usage: install-codex-app-integration.sh [options]

Installs the Codex App integration source, a token-free env.sh, a local
marketplace snapshot, and an optional persistent Bridge service.

Options:
  --dry-run                 Validate and print actions without writing
  --no-service              Render but do not start launchd or background service
  --no-plugin               Build but do not register/install the Codex plugin
  --install-dir PATH        Default: ~/.agentstack/integrations/codex_app
  --runtime-dir PATH        Default: ~/.agentstack/runtime/codex-app
  --project-key PATH        Required absolute project key
  --agent-mail-url URL      Required agent-mail JSON-RPC /api/ endpoint
  --agent-mail-env PATH     Bearer reference file; token is not copied
  --signals-dir PATH        Agent-mail signals directory
  --label LABEL             launchd label
  --marketplace-name NAME   Default: agentstack-local
  --wake-limit COUNT        Default: 12 per hour
  --stale-after SECONDS     Waiting runtime dormancy threshold; default: 3600
  --retry-max-attempts N    Registration retry call limit; default: 12
  --retry-max-age SECONDS   Registration retry lifetime; default: 3600
  --retry-max-backoff SECONDS
                            Registration retry backoff cap; default: 300
  --skip-git-check          Explicitly allow resume outside a trusted git repo
  --python-bin PATH         Python executable
  --codex-bin PATH          Codex executable
  -h, --help                Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --no-service) NO_SERVICE=true; shift ;;
    --no-plugin) NO_PLUGIN=true; shift ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --runtime-dir) RUNTIME_DIR="$2"; shift 2 ;;
    --project-key) PROJECT_KEY="$2"; shift 2 ;;
    --agent-mail-url) MCP_URL="$2"; shift 2 ;;
    --agent-mail-env) MAIL_ENV="$2"; shift 2 ;;
    --signals-dir) SIGNALS_DIR="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --marketplace-name) MARKETPLACE_NAME="$2"; shift 2 ;;
    --wake-limit) WAKE_LIMIT="$2"; shift 2 ;;
    --stale-after) STALE_AFTER="$2"; shift 2 ;;
    --retry-max-attempts) RETRY_MAX_ATTEMPTS="$2"; shift 2 ;;
    --retry-max-age) RETRY_MAX_AGE="$2"; shift 2 ;;
    --retry-max-backoff) RETRY_MAX_BACKOFF="$2"; shift 2 ;;
    --skip-git-check) SKIP_GIT_CHECK=1; shift ;;
    --python-bin) PYTHON_BIN="$2"; shift 2 ;;
    --codex-bin) CODEX_BIN="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

MARKETPLACE_ROOT="$INSTALL_DIR/marketplace"
MANIFEST="$INSTALL_DIR/install-state.json"
ENV_FILE="$INSTALL_DIR/env.sh"
RUNNER="$INSTALL_DIR/bin/run-bridge"
PLIST_INSTALLED="$INSTALL_DIR/launchd/$LABEL.plist"
PLIST_LIVE="$HOME/Library/LaunchAgents/$LABEL.plist"
BACKGROUND_PIDFILE="$RUNTIME_DIR/bridge-supervisor.pid"
SERVICE_KIND="disabled"
SERVICE_PATH=""

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
plan() {
  if [[ "$DRY_RUN" == true ]]; then
    say "DRY-RUN would $*"
  else
    say "$*"
  fi
}

validate() {
  [[ -d "$SOURCE_DIR/src/agentstack_codex_app" ]] || die "missing integration source"
  [[ -f "$SOURCE_DIR/plugin/.codex-plugin/plugin.json" ]] || die "missing plugin manifest"
  [[ -f "$SOURCE_DIR/launchd/org.agentstack.codex-app-bridge.plist.template" ]] \
    || die "missing launchd template"
  [[ -x "$PYTHON_BIN" ]] || die "python executable is not runnable: $PYTHON_BIN"
  [[ "$PROJECT_KEY" == /* ]] || die "--project-key must be an absolute path"
  [[ "$INSTALL_DIR" == /* ]] || die "--install-dir must be an absolute path"
  [[ "$RUNTIME_DIR" == /* ]] || die "--runtime-dir must be an absolute path"
  [[ "$MAIL_ENV" == /* ]] || die "--agent-mail-env must be an absolute path"
  [[ "$SIGNALS_DIR" == /* ]] || die "--signals-dir must be an absolute path"
  [[ "$MCP_URL" == http://* || "$MCP_URL" == https://* ]] \
    || die "--agent-mail-url must be http(s)"
  [[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid launchd label"
  [[ "$MARKETPLACE_NAME" =~ ^[a-z0-9-]+$ ]] || die "invalid marketplace name"
  [[ "$WAKE_LIMIT" =~ ^[0-9]+$ ]] || die "--wake-limit must be an integer"
  (( WAKE_LIMIT >= 1 && WAKE_LIMIT <= 120 )) || die "--wake-limit must be 1..120"
  [[ "$STALE_AFTER" =~ ^[0-9]+$ ]] || die "--stale-after must be an integer"
  (( STALE_AFTER >= 300 && STALE_AFTER <= 604800 )) \
    || die "--stale-after must be 300..604800"
  [[ "$RETRY_MAX_ATTEMPTS" =~ ^[0-9]+$ ]] \
    || die "--retry-max-attempts must be an integer"
  (( RETRY_MAX_ATTEMPTS >= 1 && RETRY_MAX_ATTEMPTS <= 100 )) \
    || die "--retry-max-attempts must be 1..100"
  [[ "$RETRY_MAX_AGE" =~ ^[0-9]+$ ]] \
    || die "--retry-max-age must be an integer"
  (( RETRY_MAX_AGE >= 60 && RETRY_MAX_AGE <= 604800 )) \
    || die "--retry-max-age must be 60..604800"
  [[ "$RETRY_MAX_BACKOFF" =~ ^[0-9]+$ ]] \
    || die "--retry-max-backoff must be an integer"
  (( RETRY_MAX_BACKOFF >= 1 && RETRY_MAX_BACKOFF <= 3600 )) \
    || die "--retry-max-backoff must be 1..3600"
  [[ "$RESTART_DELAY" =~ ^[0-9]+$ ]] \
    || die "AGENTSTACK_CODEX_APP_RESTART_DELAY must be an integer"
  (( RESTART_DELAY >= 0 && RESTART_DELAY <= 3600 )) \
    || die "AGENTSTACK_CODEX_APP_RESTART_DELAY must be 0..3600"
  [[ "$SKIP_GIT_CHECK" == "0" || "$SKIP_GIT_CHECK" == "1" ]] \
    || die "AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK must be 0 or 1"
  [[ "$HTTP_BEARER_MODE" == "auto" || "$HTTP_BEARER_MODE" == "enabled" || \
     "$HTTP_BEARER_MODE" == "disabled" ]] || \
    die "AGENTSTACK_MAIL_HTTP_BEARER_MODE must be auto, enabled, or disabled"
  if [[ "$NO_PLUGIN" != true ]]; then
    [[ -x "$CODEX_BIN" ]] || die "codex executable is not runnable: $CODEX_BIN"
  fi
  if [[ ! -f "$MAIL_ENV" ]]; then
    warn "bearer reference does not exist yet: $MAIL_ENV"
  fi
  if [[ "$NO_SERVICE" != true && "$(uname -s)" != "Darwin" ]]; then
    die "launchd install is supported only on macOS; use --no-service"
  fi
}

copy_payload() {
  plan "install integration source under $INSTALL_DIR"
  [[ "$DRY_RUN" == true ]] && return
  "$PYTHON_BIN" - "$SOURCE_DIR" "$INSTALL_DIR" <<'PY'
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
destination.mkdir(parents=True, exist_ok=True)
for name in ("src", "schemas", "plugin", "launchd"):
    target = destination / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source / name,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store"),
    )
for name in ("README.md", "pyproject.toml", "env.sh.sample", "export-manifest.txt"):
    item = source / name
    if item.exists():
        shutil.copy2(item, destination / name)
PY
  mkdir -p "$INSTALL_DIR/bin" "$RUNTIME_DIR"
  chmod 700 "$INSTALL_DIR" "$INSTALL_DIR/bin" "$RUNTIME_DIR"
  cp "$SCRIPT_DIR/run-codex-app-bridge.sh" "$RUNNER"
  cp "$SCRIPT_DIR/uninstall-codex-app-integration.sh" \
    "$INSTALL_DIR/bin/uninstall-codex-app-integration"
  cp "$SCRIPT_DIR/doctor-codex-app-integration.sh" \
    "$INSTALL_DIR/bin/doctor-codex-app-integration"
  chmod +x "$RUNNER" \
    "$INSTALL_DIR/bin/uninstall-codex-app-integration" \
    "$INSTALL_DIR/bin/doctor-codex-app-integration" \
    "$INSTALL_DIR/plugin/scripts/run-hook.sh" \
    "$INSTALL_DIR/plugin/scripts/run-mcp.sh"
}

write_env() {
  plan "write token-free env $ENV_FILE with mode 0600"
  [[ "$DRY_RUN" == true ]] && return
  umask 077
  "$PYTHON_BIN" - "$ENV_FILE" "$INSTALL_DIR" "$RUNTIME_DIR" "$PROJECT_KEY" \
    "$MCP_URL" "$MAIL_ENV" "$SIGNALS_DIR" "$LABEL" "$WAKE_LIMIT" \
    "$STALE_AFTER" "$RETRY_MAX_ATTEMPTS" "$RETRY_MAX_AGE" \
    "$RETRY_MAX_BACKOFF" "$MARKETPLACE_NAME" "$SKIP_GIT_CHECK" \
    "$CODEX_BIN" "$PYTHON_BIN" "$RESTART_DELAY" "$HTTP_BEARER_MODE" <<'PY'
import pathlib
import shlex
import sys

(
    output,
    install_dir,
    runtime_dir,
    project_key,
    mcp_url,
    mail_env,
    signals_dir,
    label,
    wake_limit,
    stale_after,
    retry_max_attempts,
    retry_max_age,
    retry_max_backoff,
    marketplace_name,
    skip_git_check,
    codex_bin,
    python_bin,
    restart_delay,
    http_bearer_mode,
) = sys.argv[1:]
values = {
    "AGENTSTACK_CODEX_APP_INSTALL_DIR": install_dir,
    "AGENTSTACK_CODEX_APP_RUNTIME_DIR": runtime_dir,
    "AGENTSTACK_CODEX_APP_SOCKET": str(pathlib.Path(runtime_dir) / "bridge.sock"),
    "AGENTSTACK_CODEX_APP_SNAPSHOT": str(pathlib.Path(runtime_dir) / "snapshot.json"),
    "AGENTSTACK_CODEX_APP_DELIVERY_DB": str(pathlib.Path(runtime_dir) / "delivery.sqlite3"),
    "AGENTSTACK_PROJECT_KEY": project_key,
    "AGENTSTACK_MCP_URL": mcp_url,
    "AGENTSTACK_MAIL_ENV": mail_env,
    "AGENTSTACK_MAIL_HTTP_BEARER_MODE": http_bearer_mode,
    "AGENTSTACK_SIGNALS_DIR": signals_dir,
    "AGENTSTACK_CODEX_APP_LAUNCHD_LABEL": label,
    "AGENTSTACK_CODEX_APP_WAKE_LIMIT_PER_HOUR": wake_limit,
    "AGENTSTACK_CODEX_APP_STALE_AFTER_SECONDS": stale_after,
    "AGENTSTACK_CODEX_APP_RETRY_MAX_ATTEMPTS": retry_max_attempts,
    "AGENTSTACK_CODEX_APP_RETRY_MAX_AGE_SECONDS": retry_max_age,
    "AGENTSTACK_CODEX_APP_RETRY_MAX_BACKOFF_SECONDS": retry_max_backoff,
    "AGENTSTACK_CODEX_APP_PLUGIN_ID": f"agentstack-codex-app@{marketplace_name}",
    "AGENTSTACK_CODEX_APP_SKIP_GIT_CHECK": skip_git_check,
    "AGENTSTACK_CODEX_BINARY": codex_bin,
    "AGENTSTACK_PYTHON": python_bin,
    "AGENTSTACK_CODEX_APP_RESTART_DELAY": restart_delay,
}
lines = [
    "# Generated by install-codex-app-integration.sh",
    "# Token-free: bearer material remains in AGENTSTACK_MAIL_ENV.",
    "",
]
lines.extend(f"export {key}={shlex.quote(value)}" for key, value in values.items())
path = pathlib.Path(output)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

build_marketplace() {
  plan "build local marketplace snapshot at $MARKETPLACE_ROOT"
  [[ "$DRY_RUN" == true ]] && return
  "$PYTHON_BIN" "$SCRIPT_DIR/build-codex-app-marketplace.py" \
    "$INSTALL_DIR" "$MARKETPLACE_ROOT" \
    --marketplace-name "$MARKETPLACE_NAME" >/dev/null
  chmod +x \
    "$MARKETPLACE_ROOT/plugins/agentstack-codex-app/scripts/run-hook.sh" \
    "$MARKETPLACE_ROOT/plugins/agentstack-codex-app/scripts/run-mcp.sh"
}

render_plist() {
  plan "render launchd plist $PLIST_INSTALLED"
  [[ "$DRY_RUN" == true ]] && return
  "$PYTHON_BIN" - \
    "$INSTALL_DIR/launchd/org.agentstack.codex-app-bridge.plist.template" \
    "$PLIST_INSTALLED" "$LABEL" "$RUNNER" "$RUNTIME_DIR" <<'PY'
import pathlib
import plistlib
import sys

source, output, label, runner, runtime_dir = sys.argv[1:]
text = pathlib.Path(source).read_text(encoding="utf-8")
for key, value in {
    "__LABEL__": label,
    "__RUNNER__": runner,
    "__RUNTIME_DIR__": runtime_dir,
}.items():
    text = text.replace(key, value)
path = pathlib.Path(output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(text, encoding="utf-8")
with path.open("rb") as handle:
    plistlib.load(handle)
PY
}

install_plugin() {
  if [[ "$NO_PLUGIN" == true ]]; then
    plan "skip Codex marketplace/plugin registration"
    return
  fi
  plan "register marketplace $MARKETPLACE_NAME and install agentstack-codex-app"
  [[ "$DRY_RUN" == true ]] && return
  "$CODEX_BIN" plugin marketplace add "$MARKETPLACE_ROOT" --json >/dev/null
  "$CODEX_BIN" plugin add "agentstack-codex-app@$MARKETPLACE_NAME" --json >/dev/null
}

install_service() {
  if [[ "$NO_SERVICE" == true ]]; then
    plan "skip launchd registration"
    SERVICE_KIND="disabled"
    return
  fi
  plan "install and bootstrap launchd service $LABEL"
  [[ "$DRY_RUN" == true ]] && return
  stop_supervised_background
  mkdir -p "$HOME/Library/LaunchAgents"
  cp "$PLIST_INSTALLED" "$PLIST_LIVE"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$PLIST_LIVE" && \
     launchctl enable "gui/$(id -u)/$LABEL" && \
     launchctl kickstart -k "gui/$(id -u)/$LABEL"
  then
    SERVICE_KIND="launchd"
    SERVICE_PATH="$PLIST_LIVE"
    return
  fi

  warn "launchd could not start $LABEL in gui/$(id -u); falling back to supervised background mode"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_LIVE"
  start_supervised_background
}

stop_supervised_background() {
  local pid attempts=0
  pid="$(sed -n '1p' "$BACKGROUND_PIDFILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && (( attempts < 50 )); do
      sleep 0.1
      attempts=$((attempts + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$BACKGROUND_PIDFILE"
}

start_supervised_background() {
  local supervisor_pid
  SERVICE_KIND="nohup"
  SERVICE_PATH="$BACKGROUND_PIDFILE"
  plan "start Bridge in supervised background mode, pidfile $BACKGROUND_PIDFILE"
  [[ "$DRY_RUN" == true ]] && return
  mkdir -p "$RUNTIME_DIR"
  (
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    AGENTSTACK_CODEX_APP_SELF_RESTART=1 \
      nohup /bin/bash "$RUNNER" >> "$RUNTIME_DIR/bridge.stdout.log" \
      2>> "$RUNTIME_DIR/bridge.stderr.log" &
    echo $! > "$BACKGROUND_PIDFILE"
  )
  supervisor_pid="$(sed -n '1p' "$BACKGROUND_PIDFILE" 2>/dev/null || true)"
  if [[ "$supervisor_pid" =~ ^[0-9]+$ ]] && kill -0 "$supervisor_pid" 2>/dev/null; then
    return
  fi
  warn "could not start the supervised background Bridge"
  rm -f "$BACKGROUND_PIDFILE"
  SERVICE_KIND="manual"
  SERVICE_PATH=""
}

write_manifest() {
  plan "write install manifest $MANIFEST"
  [[ "$DRY_RUN" == true ]] && return
  "$PYTHON_BIN" - "$MANIFEST" "$INSTALL_DIR" "$RUNTIME_DIR" "$PROJECT_KEY" \
    "$MCP_URL" "$MAIL_ENV" "$SIGNALS_DIR" "$LABEL" "$MARKETPLACE_NAME" \
    "$MARKETPLACE_ROOT" "$PLIST_LIVE" "$NO_PLUGIN" \
    "$CODEX_BIN" "$PYTHON_BIN" "$SERVICE_KIND" "$SERVICE_PATH" <<'PY'
import json
import pathlib
import sys
import time

(
    output,
    install_dir,
    runtime_dir,
    project_key,
    mcp_url,
    mail_env,
    signals_dir,
    label,
    marketplace_name,
    marketplace_root,
    live_plist,
    no_plugin,
    codex_binary,
    python_binary,
    service_kind,
    service_path,
) = sys.argv[1:]
manifest = {
    "schema_version": 1,
    "tool": "agentstack-codex-app",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "install_dir": install_dir,
    "runtime_dir": runtime_dir,
    "project_key": project_key,
    "agent_mail_url": mcp_url,
    "agent_mail_env": mail_env,
    "signals_dir": signals_dir,
    "codex_binary": codex_binary,
    "python_binary": python_binary,
    "launchd": {
        "enabled": service_kind == "launchd",
        "label": label,
        "path": live_plist,
    },
    "service": {
        "kind": service_kind,
        **({"label": label, "path": service_path} if service_kind == "launchd" else {}),
        **({"pidfile": service_path} if service_kind == "nohup" else {}),
    },
    "plugin": {
        "enabled": no_plugin != "true",
        "id": f"agentstack-codex-app@{marketplace_name}",
        "marketplace_name": marketplace_name,
        "marketplace_root": marketplace_root,
    },
    "retained_paths": [runtime_dir],
    "purge_paths": [runtime_dir],
}
path = pathlib.Path(output)
path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
}

main() {
  say "AgentStack Codex App integration installer"
  say "install dir: $INSTALL_DIR"
  say "runtime dir: $RUNTIME_DIR"
  say "project key: $PROJECT_KEY"
  validate
  copy_payload
  write_env
  build_marketplace
  render_plist
  install_plugin
  install_service
  write_manifest
  if [[ "$DRY_RUN" == true ]]; then
    say "Dry-run complete: no files were written."
  else
    say "Install complete."
    case "$SERVICE_KIND" in
      launchd) say "Service mode: launchd" ;;
      nohup) say "Service mode: supervised background (pidfile $BACKGROUND_PIDFILE)" ;;
      disabled) say "Service mode: disabled (--no-service)" ;;
      *)
        say "Service mode: manual"
        say "Manual supervised start:"
        say "  AGENTSTACK_CODEX_APP_SELF_RESTART=1 nohup /bin/bash $RUNNER >> $RUNTIME_DIR/bridge.stdout.log 2>> $RUNTIME_DIR/bridge.stderr.log &"
        ;;
    esac
    say "Doctor: $INSTALL_DIR/bin/doctor-codex-app-integration"
  fi
}

main "$@"
