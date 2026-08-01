#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_SETTINGS_SCRIPT="$SCRIPT_DIR/lib/merge_settings.py"

DRY_RUN=false
TIER="tier1"
TIER_OPTION=""
INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MAIL_DIR="${AGENTSTACK_MAIL_DIR:-$HOME/mcp_agent_mail}"
MAIL_HOME="${AGENTSTACK_MAIL_HOME:-$HOME/.mcp_agent_mail}"
PORT="${AGENTSTACK_PORT:-8770}"
LABEL_PREFIX="${AGENTSTACK_LABEL_PREFIX:-org.agentstack}"
TERMINAL="${AGENTSTACK_TERMINAL:-auto}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-$REPO_ROOT}}"
PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-$PROJECT_KEY}"
DELIVERABLE_ROOTS="${AGENTSTACK_DELIVERABLE_ROOTS:-}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-$(command -v python3 2>/dev/null || true)}"
PATH_VALUE="${AGENTSTACK_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:8765/mcp}"
UPSTREAM_AGENT_MAIL_URL="${AGENTSTACK_AGENT_MAIL_REPO:-https://github.com/Dicklesworthstone/mcp_agent_mail.git}"

usage() {
  cat <<'EOF'
Usage: install.sh [--dry-run] [--dashboard-only|--scoped] [options]

Core install only. This creates ~/.agentstack, installs hooks/skills/dashboard assets,
creates env.sh and service files, and writes install-state.json. Tier1 shows a
Claude Code user-settings dry-run diff and only merges after explicit approval.
It does not modify ~/.claude.json or shell dotfiles. After Tier1 preview and
explicit approval, only the managed marker block in project/global CLAUDE.md
may be updated; other project files are not changed.

Options:
  --dry-run              Print planned actions without writing files
  --dashboard-only       Tier0 footprint; install dashboard assets only
  --scoped               Tier2 placeholder; no user-settings merge
  --install-dir PATH     Default: ~/.agentstack
  --project-key PATH     Default: AGENTSTACK_PROJECT_KEY, PROJECT_KEY, or repo root
  --port PORT            Default: 8770
  --label-prefix PREFIX  Default: org.agentstack
  --terminal MODE        auto, ghostty, iterm, terminal, or none
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --dashboard-only)
      if [[ -n "$TIER_OPTION" && "$TIER_OPTION" != "dashboard-only" ]]; then
        echo "error: --dashboard-only and --scoped are mutually exclusive" >&2
        exit 2
      fi
      TIER="tier0"
      TIER_OPTION="dashboard-only"
      shift
      ;;
    --scoped)
      if [[ -n "$TIER_OPTION" && "$TIER_OPTION" != "scoped" ]]; then
        echo "error: --dashboard-only and --scoped are mutually exclusive" >&2
        exit 2
      fi
      TIER="tier2"
      TIER_OPTION="scoped"
      shift
      ;;
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --project-key)
      PROJECT_KEY="$2"
      PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-$PROJECT_KEY}"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --label-prefix)
      LABEL_PREFIX="$2"
      shift 2
      ;;
    --terminal)
      TERMINAL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

HOOKS_DIR="$INSTALL_DIR/hooks"
SKILLS_DIR="$INSTALL_DIR/skills"
DASHBOARD_DIR="$INSTALL_DIR/dashboard"
BIN_DIR="$INSTALL_DIR/bin"
RUNTIME_DIR="$INSTALL_DIR/runtime"
BACKUPS_DIR="$INSTALL_DIR/backups"
ENV_FILE="$INSTALL_DIR/env.sh"
MANIFEST="$INSTALL_DIR/install-state.json"
CLAUDE_SETTINGS="${AGENTSTACK_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
CLAUDE_SKILLS_DIR="$HOME/.claude/skills"
SAFE_MERGE_RESULT_FILE="$RUNTIME_DIR/settings-merge-result.json"
MAIL_DB="${AGENTSTACK_MAIL_DB:-$MAIL_DIR/storage.sqlite3}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$MAIL_DIR/.env}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-$MAIL_HOME/signals}"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
DASHBOARD_LOG="${AGENTSTACK_DASHBOARD_LOG:-$RUNTIME_DIR/dashboard.log}"
DASHBOARD_LOG_MAX_BYTES="${AGENTSTACK_DASHBOARD_LOG_MAX_BYTES:-5242880}"
DASHBOARD_LOG_BACKUPS="${AGENTSTACK_DASHBOARD_LOG_BACKUPS:-3}"
DASHBOARD_RESTART_DELAY="${AGENTSTACK_DASHBOARD_RESTART_DELAY:-5}"
LABEL="$LABEL_PREFIX.agentdashboard"
URL="http://127.0.0.1:$PORT/"

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

run() {
  if [[ "$DRY_RUN" == true ]]; then
    say "DRY-RUN would run: $*"
  else
    "$@"
  fi
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "missing required dependency '$1'. Install it manually, then re-run install.sh."
  fi
}

check_dependencies() {
  need_cmd python3
  need_cmd tmux
  need_cmd git
  need_cmd uv
  if ! command -v fswatch >/dev/null 2>&1; then
    warn "optional dependency 'fswatch' not found; mail watcher will use polling"
  fi
  if [[ "$(uname -s)" == "Darwin" ]] && [[ "$TERMINAL" != "none" ]]; then
    if [[ ! -d /Applications/Ghostty.app && ! -d "$HOME/Applications/Ghostty.app" ]] && ! command -v ghostty >/dev/null 2>&1; then
      warn "Ghostty not found; AGENTSTACK_TERMINAL=auto will fall back when possible"
    fi
  fi
}

validate_repo_assets() {
  [[ -f "$REPO_ROOT/dashboard/server.py" ]] || die "missing dashboard/server.py"
  [[ -f "$REPO_ROOT/dashboard/index.html" ]] || die "missing dashboard/index.html"
  [[ -d "$REPO_ROOT/dashboard/assets" ]] || die "missing dashboard/assets"
  [[ -d "$REPO_ROOT/dashboard/portraits_64" ]] || die "missing dashboard/portraits_64"
  [[ -f "$REPO_ROOT/dashboard/scientist_portraits.json" ]] || die "missing dashboard/scientist_portraits.json"
  if [[ "$TIER" != "tier0" ]]; then
    [[ -f "$REPO_ROOT/hooks/check-file-reservation.sh" ]] || die "missing hooks/check-file-reservation.sh"
    [[ -f "$REPO_ROOT/hooks/settings.template.json" ]] || die "missing hooks/settings.template.json"
    [[ -d "$REPO_ROOT/skills" ]] || die "missing skills directory"
    [[ -f "$REPO_ROOT/claude/CLAUDE.md" ]] || die "missing claude/CLAUDE.md"
  fi
  [[ -f "$MERGE_SETTINGS_SCRIPT" ]] || die "missing scripts/lib/merge_settings.py"
}

port_in_use() {
  python3 - "$PORT" <<'PY'
import socket
import sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(0.3)
try:
    sys.exit(0 if s.connect_ex(("127.0.0.1", port)) == 0 else 1)
finally:
    s.close()
PY
}

check_port() {
  if port_in_use; then
    if [[ "$DRY_RUN" == true ]]; then
      warn "port $PORT is already in use; live install would stop before service registration"
    else
      die "port $PORT is already in use; set AGENTSTACK_PORT or --port"
    fi
  fi
}

detect_service_kind() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "launchd"
    return
  fi
  if [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    echo "nohup"
    return
  fi
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd-user"
    return
  fi
  echo "nohup"
}

create_layout() {
  plan "create install layout under $INSTALL_DIR"
  run mkdir -p "$HOOKS_DIR" "$SKILLS_DIR" "$DASHBOARD_DIR" "$BIN_DIR" "$RUNTIME_DIR" "$BACKUPS_DIR"
}

migrate_legacy_annotations() {
  local legacy_path="$DASHBOARD_DIR/annotations.json"
  local runtime_path="$RUNTIME_DIR/annotations.json"
  if [[ ! -f "$legacy_path" || -e "$runtime_path" ]]; then
    return
  fi
  plan "migrate dashboard annotations $legacy_path -> $runtime_path"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  python3 - "$legacy_path" "$runtime_path" <<'PY'
import os
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_name(target.name + ".tmp")
shutil.copy2(source, temporary)
os.replace(temporary, target)
try:
    source.unlink()
except OSError as exc:
    print(f"warning: migrated annotations but could not remove legacy copy {source}: {exc}", file=sys.stderr)
PY
}

migrate_legacy_dashboard_log() {
  local legacy_path="$DASHBOARD_DIR/dashboard.log"
  local target_path="$DASHBOARD_LOG"
  local suffix=1
  if [[ ! -f "$legacy_path" ]]; then
    return
  fi
  if [[ -e "$target_path" ]]; then
    target_path="$RUNTIME_DIR/dashboard.legacy.log"
  fi
  while [[ -e "$target_path" ]]; do
    target_path="$RUNTIME_DIR/dashboard.legacy.$suffix.log"
    suffix=$((suffix + 1))
  done
  plan "migrate dashboard log $legacy_path -> $target_path"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  python3 - "$legacy_path" "$target_path" <<'PY'
import os
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
try:
    os.replace(source, target)
except OSError:
    temporary = target.with_name(target.name + ".tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)
    source.unlink()
PY
}

copy_tree() {
  local src="$1"
  local dst="$2"
  plan "copy $src -> $dst"
  if [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$dst"
    cp -R "$src/." "$dst/"
  fi
}

# The child MCP proxy that spawn_child.sh points each child at. It lives under
# integrations/codex_app because the Codex App bridge introduced it, but a
# spawned child's authenticated agent-mail connection is a CORE feature: without
# this, hooks/spawn_child.sh silently falls back to the shared endpoint and the
# child must read its own token instead of the proxy injecting it.
#
# Only the runtime subset ships here (the runner plus the package it imports).
# The full optional bridge — daemon, launchd, marketplace — still comes from
# scripts/install-codex-app-integration.sh into this same directory.
install_child_mcp_proxy() {
  local source_dir="$REPO_ROOT/integrations/codex_app"
  local dest_dir="$INSTALL_DIR/integrations/codex_app"
  if [[ ! -f "$source_dir/plugin/scripts/run-mcp.sh" ]]; then
    warn "child MCP proxy not found in the repo; spawned children will fall back to the shared agent-mail endpoint"
    return 0
  fi
  plan "install child MCP proxy -> $dest_dir"
  if [[ "$DRY_RUN" == true ]]; then
    return 0
  fi
  python3 - "$source_dir" "$dest_dir" <<'PY'
import pathlib
import shutil
import sys

source, dest = (pathlib.Path(p) for p in sys.argv[1:3])
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
for name in ("plugin", "src"):
    src = source / name
    if not src.is_dir():
        continue
    dst = dest / name
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=ignore)
PY
  chmod +x "$dest_dir/plugin/scripts/run-mcp.sh" 2>/dev/null || true
}

install_payload() {
  if [[ "$TIER" != "tier0" ]]; then
    copy_tree "$REPO_ROOT/hooks" "$HOOKS_DIR"
    copy_tree "$REPO_ROOT/skills" "$SKILLS_DIR"
    copy_tree "$REPO_ROOT/codex" "$INSTALL_DIR/codex"
    copy_tree "$REPO_ROOT/claude" "$INSTALL_DIR/claude"
    install_child_mcp_proxy
  else
    plan "skip hooks copy for --dashboard-only"
    plan "skip skills copy for --dashboard-only"
  fi
  copy_tree "$REPO_ROOT/dashboard" "$DASHBOARD_DIR"
  plan "copy VERSION -> $INSTALL_DIR/VERSION"
  if [[ "$DRY_RUN" != true ]]; then
    cp "$REPO_ROOT/VERSION" "$INSTALL_DIR/VERSION"
  fi
  plan "install helper scripts into $BIN_DIR"
  if [[ "$DRY_RUN" != true ]]; then
    cp "$SCRIPT_DIR/uninstall.sh" "$BIN_DIR/agentstack-uninstall"
    cp "$SCRIPT_DIR/doctor.sh" "$BIN_DIR/agentstack-doctor"
    cp "$MERGE_SETTINGS_SCRIPT" "$BIN_DIR/agentstack-merge-settings"
    mkdir -p "$BIN_DIR/lib"
    cp "$REPO_ROOT/bin/lib/agentstack-launch.sh" "$BIN_DIR/lib/agentstack-launch.sh"
    cp "$REPO_ROOT/bin/lib/agentstack-register.sh" "$BIN_DIR/lib/agentstack-register.sh"
    cp "$REPO_ROOT/bin/lib/agentstack-scientists.sh" "$BIN_DIR/lib/agentstack-scientists.sh"
    cp "$REPO_ROOT/bin/agent-start" "$BIN_DIR/agent-start"
    cp "$REPO_ROOT/bin/agent-start-codex" "$BIN_DIR/agent-start-codex"
    cp "$REPO_ROOT/bin/agentstack-reregister" "$BIN_DIR/agentstack-reregister"
    cp "$REPO_ROOT/bin/agentstack-preregister-child" "$BIN_DIR/agentstack-preregister-child"
    cp "$REPO_ROOT/bin/agentstack-codex-bootstrap" "$BIN_DIR/agentstack-codex-bootstrap"
    cp "$REPO_ROOT/bin/agentstack-codex-setup" "$BIN_DIR/agentstack-codex-setup"
    cp "$REPO_ROOT/bin/agentstack-claude-setup" "$BIN_DIR/agentstack-claude-setup"
    chmod +x "$BIN_DIR/agentstack-uninstall" "$BIN_DIR/agentstack-doctor" "$BIN_DIR/agentstack-merge-settings" \
      "$BIN_DIR/agent-start" "$BIN_DIR/agent-start-codex" "$BIN_DIR/agentstack-reregister" \
      "$BIN_DIR/agentstack-preregister-child" \
      "$BIN_DIR/agentstack-codex-bootstrap" "$BIN_DIR/agentstack-codex-setup" "$BIN_DIR/agentstack-claude-setup"
  fi
}

symlink_points_to() {
  python3 - "$1" "$2" <<'PY'
import os
import pathlib
import sys

link = pathlib.Path(sys.argv[1])
expected = pathlib.Path(sys.argv[2])
try:
    target = pathlib.Path(os.readlink(link))
except OSError:
    raise SystemExit(1)
if not target.is_absolute():
    target = link.parent / target
raise SystemExit(0 if target.resolve(strict=False) == expected.resolve(strict=False) else 1)
PY
}

install_claude_skill_links() {
  if [[ "$TIER" == "tier0" ]]; then
    plan "skip Claude skill links for --dashboard-only"
    return
  fi

  if [[ -e "$CLAUDE_SKILLS_DIR" && ! -d "$CLAUDE_SKILLS_DIR" ]]; then
    warn "Claude skills path exists but is not a directory; leaving it untouched: $CLAUDE_SKILLS_DIR"
    return
  fi
  plan "create Claude standard skills directory $CLAUDE_SKILLS_DIR"
  run mkdir -p "$CLAUDE_SKILLS_DIR"

  local discovery_root="$SKILLS_DIR"
  if [[ "$DRY_RUN" == true ]]; then
    discovery_root="$REPO_ROOT/skills"
  fi
  local skill_file skill_name source_path link_path
  while IFS= read -r -d '' skill_file; do
    skill_name="$(basename "$(dirname "$skill_file")")"
    source_path="$SKILLS_DIR/$skill_name"
    link_path="$CLAUDE_SKILLS_DIR/$skill_name"

    if [[ -e "$link_path" || -L "$link_path" ]]; then
      if [[ -L "$link_path" ]] && symlink_points_to "$link_path" "$source_path"; then
        plan "reuse Claude skill link $link_path -> $source_path"
      else
        warn "Claude skill '$skill_name' already exists; leaving it untouched: $link_path"
      fi
      continue
    fi

    plan "link Claude skill $link_path -> $source_path"
    run ln -s "$source_path" "$link_path"
  done < <(find "$discovery_root" -mindepth 2 -maxdepth 2 -name SKILL.md -type f -print0)
}

render_installed_templates() {
  if [[ "$TIER" != "tier0" ]]; then
    plan "render hook settings template token -> $HOOKS_DIR"
    if [[ "$DRY_RUN" != true ]]; then
      python3 - "$HOOKS_DIR/settings.template.json" "$HOOKS_DIR" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
hooks_dir = sys.argv[2]
text = path.read_text(encoding="utf-8").replace("__AGENTSTACK_HOOKS_DIR__", hooks_dir)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(text, encoding="utf-8")
tmp.replace(path)
PY
    fi
  fi
}

confirm_safe_merge() {
  if [[ ! -t 0 ]]; then
    warn "non-interactive shell; skipping Tier1 user-settings merge"
    return 1
  fi
  printf 'Apply this claude-agent-stack settings merge to %s? Type yes to continue: ' "$CLAUDE_SETTINGS" >&2
  local reply
  read -r reply
  [[ "$reply" == "yes" ]]
}

safe_merge_settings() {
  if [[ "$TIER" != "tier1" ]]; then
    return
  fi

  local template="$HOOKS_DIR/settings.template.json"
  if [[ "$DRY_RUN" == true ]]; then
    template="$REPO_ROOT/hooks/settings.template.json"
  fi
  local merge_args=(
    "$MERGE_SETTINGS_SCRIPT"
    --settings "$CLAUDE_SETTINGS"
    --template "$template"
    --hooks-dir "$HOOKS_DIR"
    --bin-dir "$BIN_DIR"
    --skills-dir "$SKILLS_DIR"
    --backup-dir "$BACKUPS_DIR"
  )

  say "Tier1 settings safe-merge dry-run: $CLAUDE_SETTINGS"
  if [[ "$DRY_RUN" == true ]]; then
    python3 "${merge_args[@]}" --dry-run
    return
  fi

  python3 "${merge_args[@]}" --dry-run
  if confirm_safe_merge; then
    python3 "${merge_args[@]}" --result-json "$SAFE_MERGE_RESULT_FILE"
  else
    say "Skipped Tier1 user-settings merge."
  fi
}

confirm_managed_setup() {
  local label="$1"
  if [[ ! -t 0 ]]; then
    warn "non-interactive shell; skipping $label managed setup"
    return 1
  fi
  printf 'Apply this claude-agent-stack %s managed setup? Type yes to continue: ' "$label" >&2
  local reply
  read -r reply
  [[ "$reply" == "yes" ]]
}

run_managed_setup() {
  local label="$1" script_name="$2"
  local script_path setup_home
  if [[ "$DRY_RUN" == true ]]; then
    script_path="$REPO_ROOT/bin/$script_name"
    setup_home="$INSTALL_DIR"
  else
    script_path="$BIN_DIR/$script_name"
    setup_home="$INSTALL_DIR"
  fi

  say "$label managed setup dry-run:"
  AGENTSTACK_HOME="$setup_home" AGENTSTACK_TEMPLATE_HOME="$REPO_ROOT" AGENTSTACK_PROJECT_KEY="$PROJECT_KEY" "$script_path" --print
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi

  if confirm_managed_setup "$label"; then
    AGENTSTACK_HOME="$INSTALL_DIR" AGENTSTACK_PROJECT_KEY="$PROJECT_KEY" "$script_path"
  else
    say "Skipped $label managed setup."
  fi
}

safe_managed_doc_setups() {
  if [[ "$TIER" != "tier1" ]]; then
    return
  fi
  run_managed_setup "Codex AGENTS.md" "agentstack-codex-setup"
  run_managed_setup "Claude CLAUDE.md" "agentstack-claude-setup"
}

write_env_file() {
  plan "write $ENV_FILE (mode 600, token-free)"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  umask 077
  python3 - "$ENV_FILE" <<PY
import pathlib
import shlex
import sys

path = pathlib.Path(sys.argv[1])
values = {
    "AGENTSTACK_PORT": "$PORT",
    "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
    "AGENTSTACK_MAIL_DB": "$MAIL_DB",
    "AGENTSTACK_MAIL_ENV": "$MAIL_ENV",
    "AGENTSTACK_MAIL_HOME": "$MAIL_HOME",
    "AGENTSTACK_SIGNALS_DIR": "$SIGNALS_DIR",
    "AGENTSTACK_MCP_URL": "$MCP_URL",
    "AGENTSTACK_TERMINAL": "$TERMINAL",
    "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
    "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
    "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
    "AGENTSTACK_HOOKS_DIR": "$HOOKS_DIR",
    "AGENTSTACK_SKILLS_DIR": "$SKILLS_DIR",
    "AGENTSTACK_RUNTIME_DIR": "$RUNTIME_DIR",
    "AGENTSTACK_MANAGED_AGENTS_FILE": "$MANAGED_AGENTS_FILE",
    "AGENTSTACK_DASHBOARD_LOG": "$DASHBOARD_LOG",
    "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": "$DASHBOARD_LOG_MAX_BYTES",
    "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "$DASHBOARD_LOG_BACKUPS",
    "AGENTSTACK_DASHBOARD_RESTART_DELAY": "$DASHBOARD_RESTART_DELAY",
    "AGENTSTACK_VAULT": "",
    "AGENTSTACK_PYTHON": "$PYTHON_BIN",
    "AGENTSTACK_PATH": "$PATH_VALUE",
}
lines = ["# Generated by claude-agent-stack install.sh", "# Do not put secrets in this file.", ""]
for key, value in values.items():
    lines.append(f"export {key}={shlex.quote(value)}")
path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
path.chmod(0o600)
PY
}

ensure_agent_mail() {
  if [[ -e "$MAIL_DIR" ]]; then
    if [[ -d "$MAIL_DIR/.git" ]]; then
      local remote
      remote="$(git -C "$MAIL_DIR" remote get-url origin 2>/dev/null || true)"
      if [[ -n "$remote" && "$remote" != "$UPSTREAM_AGENT_MAIL_URL" ]]; then
        warn "existing agent-mail remote is '$remote' (expected '$UPSTREAM_AGENT_MAIL_URL'); leaving it untouched"
      else
        plan "reuse existing agent-mail clone at $MAIL_DIR"
      fi
    else
      die "agent-mail path exists but is not a git clone: $MAIL_DIR"
    fi
  else
    plan "clone agent-mail upstream into $MAIL_DIR"
    run git clone "$UPSTREAM_AGENT_MAIL_URL" "$MAIL_DIR"
  fi

  if [[ -f "$MAIL_ENV" ]]; then
    plan "reuse existing agent-mail .env at $MAIL_ENV"
  else
    plan "create agent-mail .env at $MAIL_ENV (mode 600; token hidden)"
    if [[ "$DRY_RUN" != true ]]; then
      mkdir -p "$(dirname "$MAIL_ENV")"
      umask 077
      python3 - "$MAIL_ENV" <<'PY'
import pathlib
import secrets
import sys
path = pathlib.Path(sys.argv[1])
token = secrets.token_urlsafe(32)
path.write_text(f"HTTP_BEARER_TOKEN={token}\n", encoding="utf-8")
path.chmod(0o600)
PY
    fi
  fi

}

render_launchd_plist() {
  local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  plan "render launchd plist $plist"
  if [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    python3 - "$REPO_ROOT/dashboard/agentdashboard.plist.template" "$plist" <<PY
import pathlib
import sys

src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])
repl = {
    "__LABEL_PREFIX__": "$LABEL_PREFIX",
    "__INSTALL_DIR__": "$DASHBOARD_DIR",
    "__PYTHON__": "$PYTHON_BIN",
    "__PORT__": "$PORT",
    "__MAIL_DB__": "$MAIL_DB",
    "__MAIL_ENV__": "$MAIL_ENV",
    "__MAIL_HOME__": "$MAIL_HOME",
    "__SIGNALS_DIR__": "$SIGNALS_DIR",
    "__MCP_URL__": "$MCP_URL",
    "__TERMINAL__": "$TERMINAL",
    "__PROJECT_KEY__": "$PROJECT_KEY",
    "__PROTECTED_ROOTS__": "$PROTECTED_ROOTS",
    "__DELIVERABLE_ROOTS__": "$DELIVERABLE_ROOTS",
    "__HOOKS_DIR__": "$HOOKS_DIR",
    "__RUNTIME_DIR__": "$RUNTIME_DIR",
    "__DASHBOARD_LOG__": "$DASHBOARD_LOG",
    "__DASHBOARD_LOG_MAX_BYTES__": "$DASHBOARD_LOG_MAX_BYTES",
    "__DASHBOARD_LOG_BACKUPS__": "$DASHBOARD_LOG_BACKUPS",
    "__DASHBOARD_RESTART_DELAY__": "$DASHBOARD_RESTART_DELAY",
    "__MANAGED_AGENTS_FILE__": "$MANAGED_AGENTS_FILE",
    "__VAULT__": "",
    "__PATH__": "$PATH_VALUE",
}
text = src.read_text(encoding="utf-8")
for key, value in repl.items():
    text = text.replace(key, value)
tmp = dst.with_suffix(dst.suffix + ".tmp")
tmp.write_text(text, encoding="utf-8")
tmp.replace(dst)
PY
  fi
  SERVICE_PATH="$plist"
}

render_systemd_unit() {
  local dir="$HOME/.config/systemd/user"
  local unit="$dir/$LABEL.service"
  plan "render systemd user unit $unit"
  if [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$dir"
    python3 - "$unit" <<PY
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
env = {
    "AGENTSTACK_PORT": "$PORT",
    "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
    "AGENTSTACK_MAIL_DB": "$MAIL_DB",
    "AGENTSTACK_MAIL_ENV": "$MAIL_ENV",
    "AGENTSTACK_MAIL_HOME": "$MAIL_HOME",
    "AGENTSTACK_SIGNALS_DIR": "$SIGNALS_DIR",
    "AGENTSTACK_MCP_URL": "$MCP_URL",
    "AGENTSTACK_TERMINAL": "$TERMINAL",
    "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
    "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
    "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
    "AGENTSTACK_HOOKS_DIR": "$HOOKS_DIR",
    "AGENTSTACK_SKILLS_DIR": "$SKILLS_DIR",
    "AGENTSTACK_RUNTIME_DIR": "$RUNTIME_DIR",
    "AGENTSTACK_MANAGED_AGENTS_FILE": "$MANAGED_AGENTS_FILE",
    "AGENTSTACK_DASHBOARD_LOG": "$DASHBOARD_LOG",
    "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": "$DASHBOARD_LOG_MAX_BYTES",
    "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "$DASHBOARD_LOG_BACKUPS",
    "AGENTSTACK_DASHBOARD_RESTART_DELAY": "$DASHBOARD_RESTART_DELAY",
    "AGENTSTACK_VAULT": "",
    "PATH": "$PATH_VALUE",
}
def esc(v):
    return str(v).replace("\\\\", "\\\\\\\\").replace('"', '\\"')
lines = [
    "[Unit]",
    "Description=claude-agent-stack dashboard",
    "After=network.target",
    "",
    "[Service]",
    "Type=simple",
    f"WorkingDirectory={esc('$DASHBOARD_DIR')}",
]
for key, value in env.items():
    lines.append(f'Environment="{key}={esc(value)}"')
lines.extend([
    f"ExecStart={esc('$PYTHON_BIN')} {esc('$DASHBOARD_DIR/service_runner.py')}",
    "Restart=always",
    "RestartSec=5",
    "",
    "[Install]",
    "WantedBy=default.target",
    "",
])
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\\n".join(lines), encoding="utf-8")
tmp.replace(path)
PY
  fi
  SERVICE_PATH="$unit"
}

start_service() {
  local kind="$1"
  case "$kind" in
    launchd)
      render_launchd_plist
      if [[ "$DRY_RUN" == true ]]; then
        say "DRY-RUN would run: launchctl bootout gui/$(id -u)/$LABEL"
      else
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      fi
      run launchctl bootstrap "gui/$(id -u)" "$SERVICE_PATH"
      run launchctl enable "gui/$(id -u)/$LABEL"
      ;;
    systemd-user)
      render_systemd_unit
      run systemctl --user daemon-reload
      run systemctl --user enable --now "$LABEL.service"
      ;;
    nohup)
      SERVICE_PATH="$RUNTIME_DIR/dashboard.pid"
      plan "start dashboard with nohup fallback, pidfile $SERVICE_PATH"
      if [[ "$DRY_RUN" != true ]]; then
        (
          # shellcheck disable=SC1090
          . "$ENV_FILE"
          AGENTSTACK_DASHBOARD_SELF_RESTART=1 \
            nohup "$PYTHON_BIN" "$DASHBOARD_DIR/service_runner.py" >> "$DASHBOARD_LOG" 2>&1 &
          echo $! > "$SERVICE_PATH"
        )
      fi
      ;;
    *)
      die "unknown service kind: $kind"
      ;;
  esac
}

write_manifest() {
  plan "write manifest $MANIFEST"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  local service_kind="$1"
  local service_path="$2"
  local tmp="$MANIFEST.tmp"
  python3 - "$tmp" "$service_kind" "$service_path" <<PY
import json
import os
import pathlib
import time
import sys

out = pathlib.Path(sys.argv[1])
service_kind = sys.argv[2]
service_path = sys.argv[3]
install_dir = pathlib.Path("$INSTALL_DIR")
claude_skills_dir = pathlib.Path("$CLAUDE_SKILLS_DIR")
owned_files = []
for rel in ("hooks", "skills", "dashboard", "bin", "codex", "claude", "integrations"):
    base = install_dir / rel
    if base.exists():
        for path in base.rglob("*"):
            if path == install_dir / "dashboard" / "annotations.json":
                # Pre-runtime releases wrote user state into the payload tree.
                # It is migrated before payload installation and is never an
                # installer-owned file if a best-effort cleanup leaves it here.
                continue
            if path.is_file() or path.is_symlink():
                owned_files.append(str(path))
version_path = install_dir / "VERSION"
if version_path.is_file() or version_path.is_symlink():
    owned_files.append(str(version_path))
owned_files.extend([str(pathlib.Path("$ENV_FILE")), str(pathlib.Path("$MANIFEST"))])
if service_path:
    owned_files.append(service_path)
merge_result_path = pathlib.Path("$SAFE_MERGE_RESULT_FILE")
settings_merge = None
if merge_result_path.exists():
    settings_merge = json.loads(merge_result_path.read_text(encoding="utf-8"))
    owned_files.append(str(merge_result_path))
skill_links = []
skills_root = install_dir / "skills"
if skills_root.is_dir():
    for skill_file in sorted(skills_root.glob("*/SKILL.md")):
        source = skill_file.parent
        link = claude_skills_dir / source.name
        if not link.is_symlink():
            continue
        raw_target = pathlib.Path(os.readlink(link))
        target = raw_target if raw_target.is_absolute() else link.parent / raw_target
        if target.resolve(strict=False) != source.resolve(strict=False):
            continue
        skill_links.append({"path": str(link), "target": str(source)})
        owned_files.append(str(link))
owned_files = sorted(dict.fromkeys(owned_files))
owned_dir_paths = {
    install_dir / rel
    for rel in ("hooks", "skills", "dashboard", "bin", "runtime", "backups")
}
owned_dir_paths.add(install_dir)
for raw in owned_files:
    path = pathlib.Path(raw)
    try:
        path.relative_to(install_dir)
    except ValueError:
        # Service definitions live outside the install tree; their parent
        # directories belong to the user/system and are never installer-owned.
        continue
    parent = path.parent
    while True:
        owned_dir_paths.add(parent)
        if parent == install_dir:
            break
        parent = parent.parent
owned_dirs = sorted(str(path) for path in owned_dir_paths)
services = []
if service_kind == "launchd":
    services.append({"kind": "launchd", "label": "$LABEL", "path": service_path})
elif service_kind == "systemd-user":
    services.append({"kind": "systemd-user", "unit": "$LABEL.service", "path": service_path})
elif service_kind == "nohup":
    services.append({"kind": "nohup", "pidfile": service_path})
manifest = {
    "schema_version": 1,
    "tool": "claude-agent-stack",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "install_dir": "$INSTALL_DIR",
    "repo_root": "$REPO_ROOT",
    "tier": "$TIER",
    "safe_merge_performed": bool(
        settings_merge
        and settings_merge.get("operation") == "merge"
        and settings_merge.get("changed")
    ),
    "settings_merge": settings_merge,
    "env": {
        "AGENTSTACK_PORT": "$PORT",
        "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
        "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
        "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
        "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
        "AGENTSTACK_HOOKS_DIR": "$HOOKS_DIR",
        "AGENTSTACK_SKILLS_DIR": "$SKILLS_DIR",
        "AGENTSTACK_RUNTIME_DIR": "$RUNTIME_DIR",
        "AGENTSTACK_DASHBOARD_LOG": "$DASHBOARD_LOG",
        "AGENTSTACK_DASHBOARD_LOG_MAX_BYTES": "$DASHBOARD_LOG_MAX_BYTES",
        "AGENTSTACK_DASHBOARD_LOG_BACKUPS": "$DASHBOARD_LOG_BACKUPS",
        "AGENTSTACK_DASHBOARD_RESTART_DELAY": "$DASHBOARD_RESTART_DELAY",
        "AGENTSTACK_MAIL_DB": "$MAIL_DB",
        "AGENTSTACK_MAIL_ENV": "$MAIL_ENV",
        "AGENTSTACK_MAIL_HOME": "$MAIL_HOME",
        "AGENTSTACK_SIGNALS_DIR": "$SIGNALS_DIR",
        "AGENTSTACK_MCP_URL": "$MCP_URL",
        "AGENTSTACK_TERMINAL": "$TERMINAL",
    },
    "owned_files": owned_files,
    "owned_dirs": owned_dirs,
    "skill_links": skill_links,
    "services": services,
    "backups": [settings_merge.get("backup")] if settings_merge and settings_merge.get("backup") else [],
    "settings_backups": [settings_merge.get("backup")] if settings_merge and settings_merge.get("backup") else [],
    "retained_paths": [
        "$MAIL_DIR",
        "$MAIL_HOME",
        "$MAIL_DB",
        "$MAIL_ENV",
        "$RUNTIME_DIR",
    ],
    "purge_paths": [
        "$MAIL_DIR",
        "$MAIL_HOME",
        "$RUNTIME_DIR",
    ],
    "notes": [
        "Tier1 user-settings merge is JSON-parser based, explicit-confirm only, and manifest recorded.",
        "Claude skills use manifest-owned symlinks under ~/.claude/skills; existing conflicts are preserved.",
        "Dashboard service logs persist under runtime with bounded rotation and crash restart diagnostics.",
        "Installer does not modify Claude MCP user config.",
    ],
}
out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
PY
  python3 -m json.tool "$tmp" >/dev/null
  mv "$tmp" "$MANIFEST"
}

main() {
  say "claude-agent-stack core installer"
  say "tier: $TIER"
  say "install dir: $INSTALL_DIR"
  say "project key: $PROJECT_KEY"
  if [[ "$TIER" == "tier1" ]]; then
    say "Tier1 will show a user-settings dry-run diff before any merge."
  elif [[ "$TIER" == "tier2" ]]; then
    say "Phase 3a note: Tier2 project enable is a placeholder; no project settings are modified."
  fi
  check_dependencies
  validate_repo_assets
  check_port
  local service_kind
  service_kind="$(detect_service_kind)"
  say "service mode: $service_kind"
  create_layout
  migrate_legacy_annotations
  migrate_legacy_dashboard_log
  install_payload
  install_claude_skill_links
  render_installed_templates
  write_env_file
  ensure_agent_mail
  start_service "$service_kind"
  safe_merge_settings
  safe_managed_doc_setups
  write_manifest "$service_kind" "${SERVICE_PATH:-}"
  if [[ "$DRY_RUN" == true ]]; then
    say "Dry-run complete: no files were written."
  else
    say "Install complete: $URL"
    say "Manifest: $MANIFEST"
    say "Dashboard log: $DASHBOARD_LOG"
    say "Run doctor: $BIN_DIR/agentstack-doctor"
    if [[ "$TIER" != "tier0" ]]; then
      say "Recommended managed setup:"
      say "  hooks/settings.json via Tier1 settings merge"
      say "  $BIN_DIR/agentstack-codex-setup    (managed block in ~/.codex/AGENTS.md)"
      say "  $BIN_DIR/agentstack-claude-setup   (managed block in project/global CLAUDE.md)"
    fi
  fi
}

main "$@"
