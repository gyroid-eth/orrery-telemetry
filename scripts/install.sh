#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_SETTINGS_SCRIPT="$SCRIPT_DIR/lib/merge_settings.py"
MERGE_CLAUDE_MCP_SCRIPT="$SCRIPT_DIR/lib/merge_claude_mcp.py"

DRY_RUN=false
KEEP_LEGACY_MAIL=false
ASSUME_YES="${AGENTSTACK_ASSUME_YES:-0}"
TIER="tier1"
TIER_OPTION=""
INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MAIL_DIR="${AGENTSTACK_MAIL_DIR:-$HOME/mcp_agent_mail}"
MAIL_HOME="${AGENTSTACK_MAIL_HOME:-$HOME/.mcp_agent_mail}"
MAIL_DB_EXPLICIT="${AGENTSTACK_MAIL_DB+x}"
MAIL_ENV_EXPLICIT="${AGENTSTACK_MAIL_ENV+x}"
MAIL_PROVIDER="${AGENTSTACK_MAIL_PROVIDER:-agentstack}"
MAIL_HTTP_BEARER_MODE="${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-auto}"
MCP_URL_EXPLICIT="${AGENTSTACK_MCP_URL+x}"
PORT="${AGENTSTACK_PORT:-8770}"
LABEL_PREFIX="${AGENTSTACK_LABEL_PREFIX:-org.agentstack}"
TERMINAL="${AGENTSTACK_TERMINAL:-auto}"
PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-$REPO_ROOT}}"
PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-$PROJECT_KEY}"
DELIVERABLE_ROOTS="${AGENTSTACK_DELIVERABLE_ROOTS:-}"
LANG_SETTING="${AGENTSTACK_LANG:-}"
MURMUR_SETTING="${AGENTSTACK_MURMUR:-}"
PYTHON_BIN="${AGENTSTACK_PYTHON:-}"
PATH_VALUE="${AGENTSTACK_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:8765/mcp}"
UPSTREAM_AGENT_MAIL_URL="${AGENTSTACK_AGENT_MAIL_REPO:-https://github.com/Dicklesworthstone/mcp_agent_mail.git}"
# agent-mail is somebody else's repository, and an unpinned clone means the
# version you get depends on the day you installed — a configuration nobody
# chose and nothing recorded. When a tester and this machine then behave
# differently, the first question is which code each is running, and until
# this line existed there was no answer.
# Pin a ref we have actually run against; `AGENTSTACK_AGENT_MAIL_REF=main`
# opts out deliberately. Moving this line is how the version changes.
UPSTREAM_AGENT_MAIL_REF="${AGENTSTACK_AGENT_MAIL_REF:-5e481834ff1c373acda804d28c21d0349a116419}"
# Names this stack asks for only survive registration on a server configured to
# accept them. Set to 0 to leave agent-mail's naming exactly as upstream ships
# it, and to be handed generated names instead.
PASSTHROUGH_ENABLED="${AGENTSTACK_AGENT_MAIL_PASSTHROUGH:-1}"
# Source owned by a running, pre-existing agent-mail is outside --assume-yes.
# Patching it requires a separate, auditable opt-in.
PATCH_EXISTING_AGENT_MAIL="${AGENTSTACK_PATCH_EXISTING_AGENT_MAIL:-0}"

# These match packages/agentstack_mail/pyproject.toml. A regression test keeps
# the shell gate and package metadata in lock-step.
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=10

# CI may bypass one preflight category at a time when it deliberately supplies
# a fake platform boundary. Skipping a check never supplies the dependency the
# rest of the installer actually needs.
PREFLIGHT_SKIP_OS="${AGENTSTACK_PREFLIGHT_SKIP_OS:-0}"
PREFLIGHT_SKIP_PYTHON="${AGENTSTACK_PREFLIGHT_SKIP_PYTHON:-0}"
PREFLIGHT_SKIP_COMMANDS="${AGENTSTACK_PREFLIGHT_SKIP_COMMANDS:-0}"
PREFLIGHT_SKIP_PORT="${AGENTSTACK_PREFLIGHT_SKIP_PORT:-0}"
PREFLIGHT_SKIP_WRITABLE="${AGENTSTACK_PREFLIGHT_SKIP_WRITABLE:-0}"

usage() {
  cat <<'EOF'
Usage: install.sh [--dry-run] [--dashboard-only|--scoped] [options]

Core install only. This creates ~/.agentstack, installs hooks/skills/dashboard assets,
creates env.sh and service files, and writes install-state.json. Tier1 shows a
Claude Code user-settings and MCP dry-run diffs and only merges after explicit approval
(an interactive yes, or a user-selected --assume-yes).
It does not modify shell dotfiles. After Tier1 preview and explicit approval,
it registers the fixed mcp-agent-mail entry in ~/.claude.json and may update
only the managed marker block in project/global CLAUDE.md.

Options:
  --dry-run              Print planned actions without writing files
  -y, --assume-yes       Pre-approve MCP/settings/managed-block prompts only
  --dashboard-only       Tier0 footprint; install dashboard assets only
  --scoped               Tier2 placeholder; no user-settings merge
  --install-dir PATH     Default: ~/.agentstack
  --project-key PATH     Default: AGENTSTACK_PROJECT_KEY, PROJECT_KEY, or repo root
  --port PORT            Default: 8770
  --label-prefix PREFIX  Default: org.agentstack
  --terminal MODE        auto, ghostty, iterm, terminal, or none
  -h, --help             Show this help

--assume-yes is not --force: validation and safety errors remain fatal. It must
be selected explicitly by the user; an agent or automation must not add it on
the user's behalf. AGENTSTACK_ASSUME_YES=1 provides the same explicit opt-in.
It never authorizes edits to an existing agent-mail checkout. That separate
operation requires AGENTSTACK_PATCH_EXISTING_AGENT_MAIL=1.
The bundled AgentStack Mail provider is the default and uses port 18765. Set
AGENTSTACK_MAIL_PROVIDER=upstream to opt out and provision upstream on port 8765.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --keep-legacy-mail)
      # Escape hatch for anyone deliberately running the predecessor alongside
      # this one. It is not the default: leaving both loaded splits identities
      # and reservations across two databases.
      KEEP_LEGACY_MAIL=true
      shift
      ;;
    -y|--assume-yes)
      ASSUME_YES=1
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
CLAUDE_JSON="${AGENTSTACK_CLAUDE_JSON:-$HOME/.claude.json}"
CLAUDE_SKILLS_DIR="$HOME/.claude/skills"
SAFE_MERGE_RESULT_FILE="$RUNTIME_DIR/settings-merge-result.json"
MCP_MERGE_RESULT_FILE="$RUNTIME_DIR/claude-mcp-merge-result.json"
MAIL_DB="${AGENTSTACK_MAIL_DB:-}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-$MAIL_DIR/.env}"
SIGNALS_DIR="${AGENTSTACK_SIGNALS_DIR:-$MAIL_HOME/signals}"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
DASHBOARD_LOG="${AGENTSTACK_DASHBOARD_LOG:-$RUNTIME_DIR/dashboard.log}"
DASHBOARD_LOG_MAX_BYTES="${AGENTSTACK_DASHBOARD_LOG_MAX_BYTES:-5242880}"
DASHBOARD_LOG_BACKUPS="${AGENTSTACK_DASHBOARD_LOG_BACKUPS:-3}"
DASHBOARD_RESTART_DELAY="${AGENTSTACK_DASHBOARD_RESTART_DELAY:-5}"
LABEL="$LABEL_PREFIX.agentdashboard"
URL="http://127.0.0.1:$PORT/"
ACTIVE_SERVICE_KIND=""
SERVICE_PATH=""
SERVICE_HEALTHY=false
SERVICE_FALLBACK_USED=false
EXISTING_AGENT_MAIL_SERVER=false
PROVISION_AGENT_MAIL=false
AGENT_MAIL_LISTENER_PID=""
AGENT_MAIL_LISTENER_CWD=""
AGENT_MAIL_RUNNER="$MAIL_HOME/run-agent-mail.sh"
AGENT_MAIL_PIDFILE="$MAIL_HOME/agent-mail.pid"
AGENT_MAIL_LOG="$MAIL_HOME/agent-mail.log"
AGENT_MAIL_SERVICE_KIND=""
AGENT_MAIL_SERVICE_PATH=""
# Login-time autostart for AgentStack Mail. `agentstack-mailctl start` daemonizes
# with nohup, which does not survive a reboot: the dashboard has had a launchd /
# systemd unit since day one, mail never did. A machine that reboots therefore
# came back with a dashboard and no mail server.
MAIL_AUTOSTART_LABEL="$LABEL_PREFIX.mail"
AGENT_MAIL_AUTOSTART_KIND=""
AGENT_MAIL_AUTOSTART_PATH=""
# systemd needs a second file (service + timer); launchd does it in one plist.
AGENT_MAIL_AUTOSTART_SERVICE_PATH=""
AGENT_MAIL_PASSTHROUGH_CONFIRMED=false
NATIVE_MAIL_EXISTING=false
PROVISION_NATIVE_MAIL=false
NATIVE_MAIL_STATE_ROOT="${AGENTSTACK_MAIL_STATE_ROOT:-$HOME/.agentstack/mail}"
NATIVE_MAIL_SERVICE_ROOT="${AGENTSTACK_MAIL_SERVICE_ROOT:-$INSTALL_DIR/mail-service}"
NATIVE_MAIL_PACKAGE_SOURCE="${AGENTSTACK_MAIL_PACKAGE_SOURCE:-$REPO_ROOT/packages/agentstack_mail}"
NATIVE_MAIL_SOURCE_ID="${AGENTSTACK_MAIL_CANDIDATE_ID:-}"
if [[ -z "$NATIVE_MAIL_SOURCE_ID" ]]; then
  NATIVE_MAIL_SOURCE_ID="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
  NATIVE_MAIL_SOURCE_ID="${NATIVE_MAIL_SOURCE_ID:-source}"
fi
NATIVE_MAIL_VENV_EXPLICIT="${AGENTSTACK_MAIL_SERVICE_VENV+x}"
NATIVE_MAIL_VENV="${AGENTSTACK_MAIL_SERVICE_VENV:-$NATIVE_MAIL_SERVICE_ROOT/candidates/$NATIVE_MAIL_SOURCE_ID/venv}"
NATIVE_MAIL_ENV_EXPLICIT="${AGENTSTACK_MAIL_SERVICE_ENV+x}"
NATIVE_MAIL_ENV="${AGENTSTACK_MAIL_SERVICE_ENV:-$NATIVE_MAIL_SERVICE_ROOT/renders/pending/service.env}"
NATIVE_MAIL_RUNNER="$(dirname "$NATIVE_MAIL_ENV")/run-agentstack-mail.sh"
NATIVE_MAIL_PIDFILE="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.pid"
NATIVE_MAIL_LOG="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.log"
AGENT_MAIL_NAME_CAPABILITY_JSON='{"status":"unknown","evidence":"not-inspected","enforcement_mode":"unknown","mail_dir":"","detail":"installer has not inspected agent-mail naming source","warning":"requested-name handling is unknown"}'
PREFLIGHT_OS=""
PREFLIGHT_ERRORS=()
PYTHON_SELECTION_ERROR=""

if [[ "$MAIL_PROVIDER" == "agentstack" ]]; then
  MAIL_HTTP_BEARER_MODE="disabled"
  if [[ -z "$MCP_URL_EXPLICIT" ]]; then
    MCP_URL="http://127.0.0.1:18765/mcp"
  fi
  MAIL_DIR="$NATIVE_MAIL_SERVICE_ROOT"
  MAIL_HOME="$NATIVE_MAIL_STATE_ROOT"
  MAIL_DB="$NATIVE_MAIL_STATE_ROOT/storage.sqlite3"
  MAIL_ENV="$NATIVE_MAIL_ENV"
  SIGNALS_DIR="$NATIVE_MAIL_STATE_ROOT/signals"
  AGENT_MAIL_RUNNER="$NATIVE_MAIL_RUNNER"
  AGENT_MAIL_PIDFILE="$NATIVE_MAIL_PIDFILE"
  AGENT_MAIL_LOG="$NATIVE_MAIL_LOG"
fi

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

preflight_error() {
  PREFLIGHT_ERRORS[${#PREFLIGHT_ERRORS[@]}]="$1"
}

validate_preflight_switches() {
  local name value
  for name in \
    AGENTSTACK_PREFLIGHT_SKIP_OS \
    AGENTSTACK_PREFLIGHT_SKIP_PYTHON \
    AGENTSTACK_PREFLIGHT_SKIP_COMMANDS \
    AGENTSTACK_PREFLIGHT_SKIP_PORT \
    AGENTSTACK_PREFLIGHT_SKIP_WRITABLE
  do
    value="${!name:-0}"
    if [[ "$value" != "0" && "$value" != "1" ]]; then
      preflight_error "$name must be 0 or 1; set it to 1 only when a test deliberately replaces that boundary."
    fi
  done
}

preflight_required_commands() {
  if [[ "$PREFLIGHT_SKIP_COMMANDS" == "1" ]]; then
    say "preflight: required-command check skipped by AGENTSTACK_PREFLIGHT_SKIP_COMMANDS=1"
    return
  fi

  if ! command -v git >/dev/null 2>&1; then
    preflight_error "git is required. Install it with your OS package manager (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install git)."
  fi
  if ! command -v tmux >/dev/null 2>&1; then
    preflight_error "tmux is required at install time and runtime. Install it with Homebrew (brew install tmux) or your Linux package manager."
  fi
}

preflight_platform() {
  PREFLIGHT_OS="$(uname -s 2>/dev/null || true)"
  if [[ "$PREFLIGHT_SKIP_OS" == "1" ]]; then
    say "preflight: OS check skipped by AGENTSTACK_PREFLIGHT_SKIP_OS=1"
    return
  fi

  case "$PREFLIGHT_OS" in
    Darwin)
      if ! command -v launchctl >/dev/null 2>&1; then
        preflight_error "macOS was detected but launchctl is unavailable. Restore the macOS launchd tools, then re-run install.sh."
      else
        say "preflight: supported OS macOS (launchd)"
      fi
      ;;
    Linux)
      if command -v systemctl >/dev/null 2>&1 && \
         systemctl --user show-environment >/dev/null 2>&1
      then
        say "preflight: supported OS Linux (systemd --user available)"
      else
        say "preflight: supported OS Linux (systemd --user unavailable; supervised background mode will be used)"
      fi
      ;;
    *)
      preflight_error "operating system '${PREFLIGHT_OS:-unknown}' is unsupported. Use macOS or Linux; native Windows and other kernels are not supported."
      ;;
  esac
}

preflight_target_writable() {
  if [[ "$PREFLIGHT_SKIP_WRITABLE" == "1" ]]; then
    say "preflight: install-directory write check skipped by AGENTSTACK_PREFLIGHT_SKIP_WRITABLE=1"
    return
  fi

  local target="$INSTALL_DIR"
  local ancestor="$target"
  if [[ -e "$target" && ! -d "$target" ]]; then
    preflight_error "install target '$target' exists but is not a directory. Move it aside or choose --install-dir PATH."
    return
  fi
  while [[ ! -e "$ancestor" && "$ancestor" != "/" ]]; do
    ancestor="$(dirname "$ancestor")"
  done
  if [[ ! -d "$ancestor" || ! -w "$ancestor" || ! -x "$ancestor" ]]; then
    preflight_error "install target '$target' is not writable. Fix permissions on '$ancestor' or choose --install-dir PATH."
    return
  fi
  if [[ -d "$target" && ( ! -w "$target" || ! -x "$target" ) ]]; then
    preflight_error "existing install directory '$target' is not writable. Fix its ownership/permissions or choose --install-dir PATH."
  fi
}

preflight_finish() {
  local count="${#PREFLIGHT_ERRORS[@]}"
  local item
  if [[ "$count" -eq 0 ]]; then
    say "preflight: passed"
    return 0
  fi
  printf 'error: preflight failed with %s problem(s):\n' "$count" >&2
  for item in "${PREFLIGHT_ERRORS[@]}"; do
    printf '  - %s\n' "$item" >&2
  done
  return 1
}

# An agent-mail the user already had is not ours to move. Say which version is
# actually in play, so a later "it works on mine" has something to compare.
report_agent_mail_ref() {
  local head
  head="$(git -C "$MAIL_DIR" rev-parse HEAD 2>/dev/null || true)"
  [[ -n "$head" ]] || return 0
  if [[ "$head" == "$UPSTREAM_AGENT_MAIL_REF" ]]; then
    say "agent-mail is at the pinned ref ${head:0:12}"
  else
    say "agent-mail is at ${head:0:12}; this stack is tested against ${UPSTREAM_AGENT_MAIL_REF:0:12}"
    say "  leaving it as it is — behaviour may differ from the tested version"
  fi
}

# Which names an agent-mail server honours depends on its version, so a name
# this stack asks for is not always the name the agent ends up with. The
# passthrough mode removes the question. It is opt-in: the patch alone changes
# nothing until AGENT_NAME_ENFORCEMENT_MODE selects it.
passthrough_state() {
  local dir="$1"
  "$PYTHON_BIN" "$REPO_ROOT/scripts/lib/agent_mail_passthrough.py" \
    --mail-dir "$dir" 2>/dev/null || printf 'error'
}

apply_passthrough() {
  local dir="$1"
  "$PYTHON_BIN" "$REPO_ROOT/scripts/lib/agent_mail_passthrough.py" \
    --mail-dir "$dir" --apply >/dev/null
}

inspect_agent_mail_name_capability() {
  local dir="$1"
  "$PYTHON_BIN" "$REPO_ROOT/scripts/lib/agent_mail_passthrough.py" \
    --mail-dir "$dir" --mail-env "$MAIL_ENV" --name-capability
}

# Where a server we did not start keeps its source. The listener's working
# directory is the direct answer, but reading it needs lsof or /proc, and a
# machine with neither would report every existing server as unreadable
# forever. The database we already resolved sits in that same checkout, so fall
# back to its directory when it looks like one.
existing_agent_mail_source_dir() {
  local candidate
  for candidate in "$AGENT_MAIL_LISTENER_CWD" \
                   "${MAIL_DB:+$(dirname "$MAIL_DB")}"; do
    # Judge each candidate by what is in it, not by where it came from. A
    # server started from somewhere other than its checkout has a working
    # directory that is real, readable, and the wrong answer.
    if [[ -n "$candidate" && -d "$candidate/src/mcp_agent_mail" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  printf '%s' ""
}

report_agent_mail_name_capability() {
  local source_dir="$MAIL_DIR"
  local summary status evidence
  if [[ "$MAIL_PROVIDER" == "agentstack" ]]; then
    say "agent-mail requested-name handling: honored (agentstack-cutover-profile)"
    return
  fi
  if [[ "$EXISTING_AGENT_MAIL_SERVER" == true ]]; then
    source_dir="$(existing_agent_mail_source_dir)"
  fi
  AGENT_MAIL_NAME_CAPABILITY_JSON="$(inspect_agent_mail_name_capability "$source_dir")"
  summary="$("$PYTHON_BIN" - "$AGENT_MAIL_NAME_CAPABILITY_JSON" <<'PY'
import json
import sys

try:
    result = json.loads(sys.argv[1])
except (IndexError, TypeError, ValueError):
    result = {"status": "unknown", "evidence": "classifier-error"}
print(f"{result.get('status', 'unknown')}|{result.get('evidence', 'classifier-error')}")
PY
)"
  IFS='|' read -r status evidence <<< "$summary"
  case "$status" in
    honored)
      say "agent-mail requested-name handling: honored ($evidence)"
      ;;
    replaced)
      warn "agent-mail requested-name handling: replaced ($evidence)"
      warn "  requested names will be replaced by generated names"
      ;;
    *)
      warn "agent-mail requested-name handling: unknown ($evidence)"
      warn "  support was not inferred from unavailable or unfamiliar source"
      ;;
  esac
}

set_env_key() {
  local file="$1" key="$2" value="$3"
  "$PYTHON_BIN" - "$file" "$key" "$value" <<'PY'
import pathlib
import sys

path, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
for index, line in enumerate(lines):
    if line.split("=", 1)[0].strip() == key:
        lines[index] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

# A server this installer created is ours to configure. One it merely found is
# not: it may serve other projects, and patching it costs a restart.
confirm_patch_existing_agent_mail() {
  local dir="$1"
  if [[ "$PATCH_EXISTING_AGENT_MAIL" == "1" ]]; then
    say "explicit opt-in: AGENTSTACK_PATCH_EXISTING_AGENT_MAIL=1 approved patching existing agent-mail at $dir"
    return 0
  fi
  warn "existing agent-mail source requires separate opt-in; leaving it untouched"
  warn "  re-run with AGENTSTACK_PATCH_EXISTING_AGENT_MAIL=1 after reviewing the patch plan"
  return 1
}

offer_passthrough_to_existing_server() {
  if [[ "$PASSTHROUGH_ENABLED" != "1" ]]; then
    return 0
  fi
  local dir
  dir="$(existing_agent_mail_source_dir)"
  if [[ -z "$dir" || ! -d "$dir" ]]; then
    say "agent-mail naming mode: source directory unknown; leaving it untouched"
    return 0
  fi
  # Editing an installed package is not an option: the next upgrade removes the
  # change without saying so, and it would affect every project on this machine.
  if [[ ! -d "$dir/.git" ]]; then
    say "agent-mail naming mode: $dir is not a git checkout; leaving it untouched"
    return 0
  fi

  local state
  state="$(passthrough_state "$dir")"
  case "$state" in
    already)
      say "agent-mail already accepts explicit names (passthrough patch present)"
      return 0
      ;;
    applicable) ;;
    *)
      say "agent-mail naming mode: this version does not match the known patch ($state); leaving it untouched"
      return 0
      ;;
  esac

  say ""
  say "The agent-mail server at $MCP_URL decides whether the names this stack"
  say "asks for survive registration. A three-line change lets it accept them."
  say "  what changes: $dir/src/mcp_agent_mail/{app.py,config.py}"
  say "  it is inert   until AGENT_NAME_ENFORCEMENT_MODE=passthrough is set"
  say "  it needs      a restart of that server before it takes effect"
  say "  it is lost    the next time you update that checkout"
  say "  to undo       git -C $dir checkout -- src/mcp_agent_mail"
  say "Declining is fine: the install completes either way."

  if [[ "$DRY_RUN" == true ]]; then
    plan "offer to patch existing agent-mail at $dir (skipped by --dry-run)"
    return 0
  fi
  if ! confirm_patch_existing_agent_mail "$dir"; then
    say "left the existing agent-mail source untouched"
    return 0
  fi
  if ! apply_passthrough "$dir"; then
    warn "the agent-mail patch did not apply; that checkout is unchanged"
    return 0
  fi
  say "patched $dir"
  if [[ -n "$MAIL_ENV" && -f "$MAIL_ENV" ]]; then
    set_env_key "$MAIL_ENV" "AGENT_NAME_ENFORCEMENT_MODE" "passthrough"
    say "set AGENT_NAME_ENFORCEMENT_MODE=passthrough in $MAIL_ENV"
  else
    say "add AGENT_NAME_ENFORCEMENT_MODE=passthrough to that server's .env"
  fi
  say "restart that agent-mail server for it to take effect"
}

validate_assume_yes() {
  [[ "$ASSUME_YES" == "0" || "$ASSUME_YES" == "1" ]] || \
    die "AGENTSTACK_ASSUME_YES must be 0 or 1"
  [[ "$PATCH_EXISTING_AGENT_MAIL" == "0" || "$PATCH_EXISTING_AGENT_MAIL" == "1" ]] || \
    die "AGENTSTACK_PATCH_EXISTING_AGENT_MAIL must be 0 or 1"
}

validate_mail_provider() {
  case "$MAIL_PROVIDER" in
    upstream|agentstack) ;;
    *) die "AGENTSTACK_MAIL_PROVIDER must be 'upstream' or 'agentstack'" ;;
  esac
  if [[ "$MAIL_PROVIDER" != "agentstack" ]]; then
    return
  fi
  [[ "$NATIVE_MAIL_SOURCE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || \
    die "AGENTSTACK_MAIL_CANDIDATE_ID contains unsafe characters"
  if [[ -n "${AGENTSTACK_MAIL_MIGRATION_SOURCE_DB:-}" || \
        -n "${AGENTSTACK_MAIL_MIGRATION_SOURCE_ARCHIVE:-}" || \
        -n "${AGENTSTACK_MAIL_MIGRATION_SOURCE_SIGNALS:-}" ]]; then
    die "automatic mail migration is not part of install.sh; run agentstack-mail-migrate copy and verify manually before installing"
  fi
}

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

resolve_python_candidate() {
  case "$1" in
    */*) printf '%s\n' "$1" ;;
    *) command -v "$1" 2>/dev/null || true ;;
  esac
}

python_version() {
  "$1" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3]))' \
    2>/dev/null || printf '%s\n' unknown
}

python_is_compatible() {
  [[ -n "$1" && -x "$1" ]] || return 1
  "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= ($PYTHON_MIN_MAJOR, $PYTHON_MIN_MINOR) else 1)" \
    >/dev/null 2>&1
}

select_python() {
  local requested="${AGENTSTACK_PYTHON:-}"
  local candidate version
  PYTHON_SELECTION_ERROR=""
  if [[ -n "$requested" ]]; then
    candidate="$(resolve_python_candidate "$requested")"
    if [[ -z "$candidate" || ! -x "$candidate" ]]; then
      PYTHON_SELECTION_ERROR="AGENTSTACK_PYTHON is not an executable Python interpreter: $requested. Install Python $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR or newer and point AGENTSTACK_PYTHON at it."
      return 1
    fi
    version="$(python_version "$candidate")"
    if [[ "$PREFLIGHT_SKIP_PYTHON" != "1" ]] && ! python_is_compatible "$candidate"; then
      PYTHON_SELECTION_ERROR="AGENTSTACK_PYTHON must be Python $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR or newer; found $version at $candidate. Install a current Python or choose another AGENTSTACK_PYTHON."
      return 1
    fi
    PYTHON_BIN="$candidate"
    say "python: $PYTHON_BIN ($version)"
    return
  fi

  local checked=""
  local seen=""
  local raw
  for raw in \
    python3 python3.14 python3.13 python3.12 python3.11 python3.10 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 /opt/local/bin/python3 \
    /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3.10 \
    /usr/local/bin/python3.14 /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
    /usr/local/bin/python3.10
  do
    candidate="$(resolve_python_candidate "$raw")"
    [[ -n "$candidate" ]] || continue
    case " $seen " in
      *" $candidate "*) continue ;;
    esac
    seen="$seen $candidate"
    version="$(python_version "$candidate")"
    if [[ -n "$checked" ]]; then
      checked="$checked, "
    fi
    checked="$checked$candidate ($version)"
    if [[ "$PREFLIGHT_SKIP_PYTHON" == "1" ]] || python_is_compatible "$candidate"; then
      PYTHON_BIN="$candidate"
      say "python: $PYTHON_BIN ($version)"
      return
    fi
  done

  [[ -n "$checked" ]] || checked="no python3 candidates found"
  PYTHON_SELECTION_ERROR="Python $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR or newer is required; checked: $checked. Install a current Python or set AGENTSTACK_PYTHON."
  return 1
}

preflight_python() {
  if [[ "$PREFLIGHT_SKIP_PYTHON" == "1" ]]; then
    say "preflight: Python version check skipped by AGENTSTACK_PREFLIGHT_SKIP_PYTHON=1"
  fi
  if ! select_python; then
    preflight_error "$PYTHON_SELECTION_ERROR"
  fi
}

normalize_path() {
  "$PYTHON_BIN" - "$1" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

mcp_endpoint_parts() {
  "$PYTHON_BIN" - "$MCP_URL" <<'PY'
import sys
import urllib.parse

parsed = urllib.parse.urlparse(sys.argv[1])
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit(1)
port = parsed.port or (443 if parsed.scheme == "https" else 80)
print(f"{parsed.hostname}|{port}")
PY
}

mcp_local_server_parts() {
  "$PYTHON_BIN" - "$MCP_URL" <<'PY'
import sys
import urllib.parse

parsed = urllib.parse.urlparse(sys.argv[1])
if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit(1)
host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
port = parsed.port or 80
path = parsed.path or "/mcp"
print(f"{host}|{port}|{path}")
PY
}

# Three-state probe. "Nothing is listening" and "the probe could not run" are
# different answers, and only ECONNREFUSED proves the first one. Collapsing
# every OSError into "free" made the installer announce an available port while
# a service held it: over SSH on macOS 26 every loopback connect returns
# EADDRNOTAVAIL, including one to a socket the probing process just opened.
# Callers that only ask "can I reach it" keep working, because undetermined is
# still non-zero.
#   0 = listening   1 = refused (nothing there)   2 = undetermined
mcp_endpoint_probe() {
  local parts host port
  parts="$(mcp_endpoint_parts)" || return 2
  IFS='|' read -r host port <<< "$parts"
  "$PYTHON_BIN" - "$host" "$port" <<'PY'
import errno
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.5):
        pass
except ConnectionRefusedError:
    raise SystemExit(1)
except OSError as exc:
    if exc.errno == errno.ECONNREFUSED:
        raise SystemExit(1)
    sys.stderr.write(
        f"port probe inconclusive for {sys.argv[1]}:{sys.argv[2]}: "
        f"[errno {exc.errno}] {exc.strerror}\n"
    )
    raise SystemExit(2)
PY
}

mcp_endpoint_listening() {
  mcp_endpoint_probe >/dev/null 2>&1
}

preflight_agent_mail_port() {
  if [[ "$PREFLIGHT_SKIP_PORT" == "1" ]]; then
    say "preflight: agent-mail port check skipped by AGENTSTACK_PREFLIGHT_SKIP_PORT=1"
    return
  fi
  # Port probing needs the selected interpreter. Its own actionable Python
  # error is already in the aggregate when selection failed.
  [[ -n "$PYTHON_BIN" ]] || return

  local parts host port
  parts="$(mcp_endpoint_parts 2>/dev/null)" || {
    preflight_error "AGENTSTACK_MCP_URL '$MCP_URL' is invalid. Set it to an http(s) endpoint (default: AgentStack Mail on http://127.0.0.1:18765/mcp; upstream opt-out on http://127.0.0.1:8765/mcp)."
    return
  }
  IFS='|' read -r host port <<< "$parts"
  case "$host" in
    127.0.0.1|localhost|::1) ;;
    *)
      say "preflight: remote agent-mail endpoint $host:$port (local port check not applicable)"
      return
      ;;
  esac

  local probe_err probe_state
  probe_err="$(mcp_endpoint_probe 2>&1 >/dev/null)"
  probe_state=$?
  if [[ "$probe_state" == "1" ]]; then
    say "preflight: agent-mail port $port is available"
    return
  fi
  if [[ "$probe_state" != "0" ]]; then
    # Undetermined is not free. Everything downstream — reuse an existing
    # service or start our own — branches on this answer, so guessing "free"
    # here is how an installer ends up fighting a service it never saw.
    preflight_error "could not determine whether agent-mail port $port is in use${probe_err:+ (${probe_err##*: })}. Loopback connections from this shell are failing, which commonly happens over SSH on macOS; run the installer from a local terminal on the target machine, or set AGENTSTACK_PREFLIGHT_SKIP_PORT=1 if you have verified the port yourself."
    return
  fi
  if [[ -f "$MANIFEST" ]]; then
    say "preflight: existing AgentStack install detected; occupied agent-mail port $port will be verified for reuse"
  else
    # A user may intentionally share an already-running agent-mail across
    # projects. Do not guess ownership from a listening socket: the existing
    # resolver verifies its health response/database before the first write.
    say "preflight: agent-mail port $port is occupied; installer will verify that it is a reusable agent-mail service"
  fi
}

run_preflight() {
  PREFLIGHT_ERRORS=()
  validate_preflight_switches
  preflight_required_commands
  preflight_platform
  preflight_python
  preflight_target_writable
  preflight_agent_mail_port
  preflight_finish
}

discover_agent_mail_listener_process() {
  local parts port lsof_bin
  parts="$(mcp_endpoint_parts)" || return 0
  port="${parts##*|}"
  lsof_bin="$(command -v lsof 2>/dev/null || true)"
  if [[ -z "$lsof_bin" && -x /usr/sbin/lsof ]]; then
    lsof_bin=/usr/sbin/lsof
  fi
  if [[ -n "$lsof_bin" ]]; then
    AGENT_MAIL_LISTENER_PID="$("$lsof_bin" -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sed -n '1p')"
  fi
  if [[ "$AGENT_MAIL_LISTENER_PID" =~ ^[0-9]+$ ]]; then
    if [[ -L "/proc/$AGENT_MAIL_LISTENER_PID/cwd" ]]; then
      AGENT_MAIL_LISTENER_CWD="$(readlink "/proc/$AGENT_MAIL_LISTENER_PID/cwd" 2>/dev/null || true)"
    elif [[ -n "$lsof_bin" ]]; then
      AGENT_MAIL_LISTENER_CWD="$("$lsof_bin" -a -p "$AGENT_MAIL_LISTENER_PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | sed -n '1p')"
    fi
  fi
}

probe_agent_mail_database_url() {
  local listener_env=""
  if [[ -n "$AGENT_MAIL_LISTENER_CWD" ]]; then
    listener_env="$AGENT_MAIL_LISTENER_CWD/.env"
  fi
  "$PYTHON_BIN" - "$MCP_URL" "$MAIL_ENV" "$listener_env" <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
tokens = [None]
for raw_path in sys.argv[2:]:
    if not raw_path:
        continue
    try:
        lines = pathlib.Path(raw_path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        continue
    for line in lines:
        if line.startswith("HTTP_BEARER_TOKEN="):
            token = line.split("=", 1)[1].strip().strip("'\"")
            if token and token not in tokens:
                tokens.append(token)
            break

payload = json.dumps({
    "jsonrpc": "2.0",
    "id": "agentstack-installer-probe",
    "method": "tools/call",
    "params": {"name": "health_check", "arguments": {}},
}).encode()
for token in tokens:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        continue
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        data = json.loads(raw)
        result = data.get("result") or {}
        health = result.get("structuredContent") or {}
        if not health:
            for block in result.get("content") or []:
                if block.get("type") == "text":
                    health = json.loads(block.get("text") or "{}")
                    break
        database_url = health.get("database_url")
    except (TypeError, ValueError, AttributeError):
        continue
    if isinstance(database_url, str) and database_url:
        print(database_url)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

database_url_to_path() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import pathlib
import sys
import urllib.parse

database_url, cwd = sys.argv[1:3]
prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
for prefix in prefixes:
    if database_url.startswith(prefix):
        raw = urllib.parse.unquote(database_url[len(prefix):].split("?", 1)[0])
        path = pathlib.Path(raw).expanduser()
        if not path.is_absolute():
            if not cwd:
                raise SystemExit(1)
            path = pathlib.Path(cwd) / path
        print(path.resolve(strict=False))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

listener_open_database() {
  local lsof_bin path
  [[ "$AGENT_MAIL_LISTENER_PID" =~ ^[0-9]+$ ]] || return 1
  lsof_bin="$(command -v lsof 2>/dev/null || true)"
  if [[ -z "$lsof_bin" && -x /usr/sbin/lsof ]]; then
    lsof_bin=/usr/sbin/lsof
  fi
  [[ -n "$lsof_bin" ]] || return 1
  path="$("$lsof_bin" -a -p "$AGENT_MAIL_LISTENER_PID" -Fn 2>/dev/null |
    sed -n 's/^n//p' | awk '/\.sqlite3$/ { print; exit }')"
  [[ -n "$path" && -f "$path" ]] || return 1
  normalize_path "$path"
}

existing_mail_db_candidates() {
  local seen="" raw normalized
  for raw in \
    "${MAIL_DB:-}" \
    "$MAIL_DIR/storage.sqlite3" \
    "$HOME/.local/share/mcp-agent-mail/git_mailbox_repo/storage.sqlite3" \
    "$HOME/.local/share/mcp-agent-mail/storage.sqlite3" \
    "$HOME/.mcp_agent_mail/storage.sqlite3" \
    "${AGENT_MAIL_LISTENER_CWD:+$AGENT_MAIL_LISTENER_CWD/storage.sqlite3}"
  do
    [[ -n "$raw" ]] || continue
    normalized="$(normalize_path "$raw")"
    [[ -f "$normalized" ]] || continue
    case $'\n'"$seen"$'\n' in
      *$'\n'"$normalized"$'\n'*) continue ;;
    esac
    seen="${seen}${seen:+$'\n'}$normalized"
    printf '%s\n' "$normalized"
  done
}

confirm_existing_agent_mail() {
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    say "non-interactive install: using the detected existing agent-mail server"
    return 0
  fi
  printf 'Use the existing agent-mail server and database above? Type yes to continue: ' >&2
  local reply
  read -r reply
  [[ "$reply" == "yes" ]]
}

resolve_agent_mail_connection() {
  local database_url="" resolved_db="" explicit_db="" candidates_text=""
  local candidates=()

  if [[ -n "$MAIL_DB_EXPLICIT" ]]; then
    [[ -n "${AGENTSTACK_MAIL_DB:-}" ]] || die "AGENTSTACK_MAIL_DB was set but empty"
    explicit_db="$(normalize_path "$AGENTSTACK_MAIL_DB")"
  fi

  if mcp_endpoint_listening; then
    say "existing agent-mail listener detected at $MCP_URL"
    discover_agent_mail_listener_process
    database_url="$(probe_agent_mail_database_url || true)"

    # The probe is one way to find the database, not the gate that decides
    # whether this listener is agent-mail. A server too old to report
    # database_url, or one that wants credentials the installer cannot see,
    # still holds its database open — and the operator may simply have told us
    # where it is. Treating a silent probe as "not agent-mail" turned a working
    # install into a dead end whose two suggested escapes both led back here.
    if [[ -n "$database_url" ]]; then
      resolved_db="$(database_url_to_path "$database_url" "$AGENT_MAIL_LISTENER_CWD" || true)"
    fi
    if [[ -z "$resolved_db" || ! -f "$resolved_db" ]]; then
      resolved_db="$(listener_open_database || true)"
    fi
    if [[ -z "$database_url" && -z "$explicit_db" && ( -z "$resolved_db" || ! -f "$resolved_db" ) ]]; then
      die "$MCP_URL is already listening, but it did not answer an agent-mail health check and no SQLite database of its own could be found. Stop that service, point AGENTSTACK_MCP_URL at agent-mail, or set AGENTSTACK_MAIL_DB to the database it uses."
    fi
    # Everything above is evidence from the server itself, so a disagreement
    # with AGENTSTACK_MAIL_DB is worth stopping for. Guesses from well-known
    # locations are not: prefer what the operator told us over a coincidence.
    if [[ -n "$resolved_db" && -f "$resolved_db" ]]; then
      resolved_db="$(normalize_path "$resolved_db")"
      if [[ -n "$explicit_db" && "$explicit_db" != "$resolved_db" ]]; then
        die "AGENTSTACK_MAIL_DB points to '$explicit_db', but the running agent-mail server uses '$resolved_db'"
      fi
    elif [[ -n "$explicit_db" ]]; then
      [[ -f "$explicit_db" ]] || die "AGENTSTACK_MAIL_DB does not exist: $explicit_db"
      resolved_db="$explicit_db"
    else
      while IFS= read -r resolved_db; do
        [[ -n "$resolved_db" ]] && candidates+=("$resolved_db")
      done < <(existing_mail_db_candidates)
      if [[ "${#candidates[@]}" -eq 1 ]]; then
        resolved_db="$(normalize_path "${candidates[0]}")"
      else
        resolved_db=""
      fi
    fi
    [[ -n "$resolved_db" && -f "$resolved_db" ]] || \
      die "agent-mail is running at $MCP_URL, but its SQLite database could not be resolved from '$database_url'. Set AGENTSTACK_MAIL_DB to the existing database."

    say "existing agent-mail database: $resolved_db"
    if [[ "$DRY_RUN" != true ]] && ! confirm_existing_agent_mail; then
      die "existing agent-mail connection declined; set AGENTSTACK_MCP_URL and AGENTSTACK_MAIL_DB explicitly, then re-run"
    fi
    if [[ "$DRY_RUN" != true && "$ASSUME_YES" == "1" ]]; then
      say "assume-yes: approved existing agent-mail server at $MCP_URL"
    fi
    MAIL_DB="$resolved_db"
    EXISTING_AGENT_MAIL_SERVER=true
    if [[ -z "$MAIL_ENV_EXPLICIT" ]]; then
      if [[ -n "$AGENT_MAIL_LISTENER_CWD" && -f "$AGENT_MAIL_LISTENER_CWD/.env" ]]; then
        MAIL_ENV="$AGENT_MAIL_LISTENER_CWD/.env"
      elif [[ -f "$(dirname "$MAIL_DB")/.env" ]]; then
        MAIL_ENV="$(dirname "$MAIL_DB")/.env"
      fi
    fi
    return
  fi

  if [[ -n "$explicit_db" ]]; then
    if [[ ! -f "$explicit_db" ]]; then
      if [[ "$DRY_RUN" == true ]]; then
        warn "AGENTSTACK_MAIL_DB does not exist yet: $explicit_db"
      else
        die "AGENTSTACK_MAIL_DB does not exist: $explicit_db"
      fi
    fi
    MAIL_DB="$explicit_db"
    say "agent-mail database: $MAIL_DB"
    return
  fi

  while IFS= read -r resolved_db; do
    [[ -n "$resolved_db" ]] && candidates+=("$resolved_db")
  done < <(existing_mail_db_candidates)
  if [[ "${#candidates[@]}" -eq 1 ]]; then
    MAIL_DB="${candidates[0]}"
    say "agent-mail database: $MAIL_DB"
    return
  fi
  if [[ "${#candidates[@]}" -gt 1 ]]; then
    candidates_text="$(printf '\n  %s' "${candidates[@]}")"
    die "multiple agent-mail databases exist; set AGENTSTACK_MAIL_DB explicitly:$candidates_text"
  fi

  MAIL_DB="$(normalize_path "$MAIL_DIR/storage.sqlite3")"
  mcp_local_server_parts >/dev/null || \
    die "no running agent-mail server or database was found, and '$MCP_URL' is not a local HTTP endpoint the installer can start. Start agent-mail there or set AGENTSTACK_MCP_URL to a local endpoint."
  PROVISION_AGENT_MAIL=true
  say "no running agent-mail server or database found; installer will provision upstream agent-mail at $MCP_URL"
}

resolve_native_mail_connection() {
  local expected_db resolved_db database_url explicit_db render_id
  expected_db="$(normalize_path "$NATIVE_MAIL_STATE_ROOT/storage.sqlite3")"
  NATIVE_MAIL_STATE_ROOT="$(normalize_path "$NATIVE_MAIL_STATE_ROOT")"
  NATIVE_MAIL_SERVICE_ROOT="$(normalize_path "$NATIVE_MAIL_SERVICE_ROOT")"
  NATIVE_MAIL_PACKAGE_SOURCE="$(normalize_path "$NATIVE_MAIL_PACKAGE_SOURCE")"
  NATIVE_MAIL_VENV="$(normalize_path "$NATIVE_MAIL_VENV")"
  if [[ -n "$NATIVE_MAIL_ENV_EXPLICIT" ]]; then
    NATIVE_MAIL_ENV="$(normalize_path "$NATIVE_MAIL_ENV")"
  else
    render_id="$("$PYTHON_BIN" - \
      "$NATIVE_MAIL_SOURCE_ID" "$NATIVE_MAIL_VENV" "$MCP_URL" \
      "$NATIVE_MAIL_STATE_ROOT" <<'PY'
import hashlib
import sys

payload = "\0".join(sys.argv[1:]).encode()
print(hashlib.sha256(payload).hexdigest()[:20])
PY
)"
    NATIVE_MAIL_ENV="$NATIVE_MAIL_SERVICE_ROOT/renders/$NATIVE_MAIL_SOURCE_ID-$render_id/service.env"
  fi
  NATIVE_MAIL_RUNNER="$(dirname "$NATIVE_MAIL_ENV")/run-agentstack-mail.sh"
  NATIVE_MAIL_PIDFILE="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.pid"
  NATIVE_MAIL_LOG="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail.log"
  MAIL_DIR="$NATIVE_MAIL_SERVICE_ROOT"
  MAIL_HOME="$NATIVE_MAIL_STATE_ROOT"
  MAIL_DB="$expected_db"
  MAIL_ENV="$NATIVE_MAIL_ENV"
  SIGNALS_DIR="$NATIVE_MAIL_STATE_ROOT/signals"
  AGENT_MAIL_RUNNER="$NATIVE_MAIL_RUNNER"
  AGENT_MAIL_PIDFILE="$NATIVE_MAIL_PIDFILE"
  AGENT_MAIL_LOG="$NATIVE_MAIL_LOG"

  if [[ -n "$MAIL_DB_EXPLICIT" ]]; then
    [[ -n "${AGENTSTACK_MAIL_DB:-}" ]] || die "AGENTSTACK_MAIL_DB was set but empty"
    explicit_db="$(normalize_path "$AGENTSTACK_MAIL_DB")"
    [[ "$explicit_db" == "$expected_db" ]] || \
      die "AGENTSTACK_MAIL_DB must equal the native state database '$expected_db'"
  fi
  if [[ -n "$MAIL_ENV_EXPLICIT" ]]; then
    [[ -n "${AGENTSTACK_MAIL_ENV:-}" ]] || die "AGENTSTACK_MAIL_ENV was set but empty"
    [[ "$(normalize_path "$AGENTSTACK_MAIL_ENV")" == "$NATIVE_MAIL_ENV" ]] || \
      die "AGENTSTACK_MAIL_ENV must equal the native service env '$NATIVE_MAIL_ENV'"
  fi

  if mcp_endpoint_listening; then
    say "existing AgentStack Mail listener detected at $MCP_URL"
    database_url="$(probe_agent_mail_database_url || true)"
    [[ -n "$database_url" ]] || \
      die "$MCP_URL is listening but did not answer an AgentStack Mail health check"
    resolved_db="$(database_url_to_path "$database_url" "" || true)"
    [[ -n "$resolved_db" ]] || \
      die "AgentStack Mail health returned an unsupported database URL: $database_url"
    resolved_db="$(normalize_path "$resolved_db")"
    [[ "$resolved_db" == "$expected_db" ]] || \
      die "AgentStack Mail at $MCP_URL uses '$resolved_db', expected isolated database '$expected_db'"
    [[ -f "$resolved_db" ]] || die "AgentStack Mail database does not exist: $resolved_db"
    NATIVE_MAIL_EXISTING=true
    EXISTING_AGENT_MAIL_SERVER=true
    adopt_running_native_mail_render
    say "existing AgentStack Mail database: $resolved_db"
    return
  fi

  PROVISION_NATIVE_MAIL=true
  if [[ -f "$expected_db" && -d "$NATIVE_MAIL_STATE_ROOT/archive" ]]; then
    say "no native listener found; installer will start existing AgentStack Mail state at $NATIVE_MAIL_STATE_ROOT"
  else
    say "no native listener or state found; installer will provision AgentStack Mail at $MCP_URL"
  fi
}

# When a healthy native listener is already running, this run returns before it
# renders anything — but the paths computed from the current checkout are still
# what write_env_file records. On an update (different repo HEAD, MCP URL or
# state root) those paths name a render that never existed, so env.sh points at a
# missing service.env and every later `agentstack-mailctl start` dies with
# "service env is missing" — including the login trigger, which would then fail
# at every boot. Adopt the render the live service is actually using.
# (Found by review on 2026-08-16 with an isolated full install against a running
# listener; the same defect already made the documented manual start command fail
# for updating users, before any autostart existed.)
adopt_running_native_mail_render() {
  local pid runner dir candidate=""
  # The pidfile agentstack-mailctl writes is TWO lines: the pid, then the runner
  # it started (bin/agentstack-mailctl write_pid). Read it exactly the way the
  # controller reads it — a plain `cat` yields "PID\nRUNNER", which fails the
  # numeric test and silently skips this whole function. (That bug shipped in the
  # first version of this helper and was caught by review, because the test
  # fixture had invented a one-line pidfile.)
  pid="$(sed -n '1{s/[[:space:]].*$//;p;}' "$NATIVE_MAIL_PIDFILE" 2>/dev/null || true)"
  runner="$(sed -n '2p' "$NATIVE_MAIL_PIDFILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    if [[ -z "$runner" ]]; then
      # Pidfile from an older controller: recover the runner from the process.
      runner="$(ps -ww -o command= -p "$pid" 2>/dev/null |
        awk '{for (i = 1; i <= NF; i++) if ($i ~ /run-agentstack-mail\.sh$/) { print $i; exit }}')"
    fi
    if [[ -n "$runner" ]]; then
      dir="$(dirname "$runner")"
      [[ -f "$dir/service.env" ]] && candidate="$dir/service.env"
    fi
  fi
  if [[ -z "$candidate" && -f "$INSTALL_DIR/env.sh" ]]; then
    # write_env_file emits `export KEY=<shlex.quote(value)>`, so a raw sed of the
    # right-hand side hands back the quotes as part of the path and the -f test
    # rejects a file that exists. Let a shell do the unquoting, in a subshell so
    # sourcing cannot leak into this one.
    candidate="$(
      set +u
      . "$INSTALL_DIR/env.sh" >/dev/null 2>&1 || exit 0
      printf '%s' "${AGENTSTACK_MAIL_ENV:-}"
    )"
    [[ -n "$candidate" && -f "$candidate" ]] || candidate=""
  fi
  [[ -n "$candidate" ]] || return 0
  NATIVE_MAIL_ENV="$candidate"
  NATIVE_MAIL_RUNNER="$(dirname "$candidate")/run-agentstack-mail.sh"
  MAIL_ENV="$NATIVE_MAIL_ENV"
  say "adopted the running AgentStack Mail service env: $NATIVE_MAIL_ENV"
} # end adopt_running_native_mail_render

check_dependencies() {
  if ! command -v fswatch >/dev/null 2>&1; then
    warn "optional dependency 'fswatch' not found; mail watcher will use polling"
  fi
  if [[ "$PREFLIGHT_OS" == "Darwin" ]] && [[ "$TERMINAL" != "none" ]]; then
    if [[ ! -d /Applications/Ghostty.app && ! -d "$HOME/Applications/Ghostty.app" ]] && ! command -v ghostty >/dev/null 2>&1; then
      warn "Ghostty not found; AGENTSTACK_TERMINAL=auto will fall back when possible"
    fi
  fi
}

check_agent_mail_provisioning_dependencies() {
  if [[ "$PREFLIGHT_SKIP_COMMANDS" == "1" ]]; then
    return 0
  fi
  if [[ "$MAIL_PROVIDER" == "agentstack" ]]; then
    if [[ "$PROVISION_NATIVE_MAIL" != true || -n "$NATIVE_MAIL_VENV_EXPLICIT" ]]; then
      return 0
    fi
  elif [[ "$PROVISION_AGENT_MAIL" != true ]]; then
    return 0
  fi
  if ! command -v uv >/dev/null 2>&1; then
    PREFLIGHT_ERRORS=()
    preflight_error "uv is required to provision the selected mail provider. Install it from https://docs.astral.sh/uv/getting-started/installation/ and re-run install.sh."
    preflight_finish
    return 1
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
  [[ -f "$MERGE_CLAUDE_MCP_SCRIPT" ]] || die "missing scripts/lib/merge_claude_mcp.py"
  [[ -f "$SCRIPT_DIR/lib/agent_mail_passthrough.py" ]] || die "missing scripts/lib/agent_mail_passthrough.py"
  [[ -f "$SCRIPT_DIR/lib/mcp_endpoint.py" ]] || die "missing scripts/lib/mcp_endpoint.py"
  [[ -f "$SCRIPT_DIR/selftest.py" ]] || die "missing scripts/selftest.py"
  if [[ "$MAIL_PROVIDER" == "agentstack" ]]; then
    [[ -f "$NATIVE_MAIL_PACKAGE_SOURCE/pyproject.toml" ]] || \
      die "missing AgentStack Mail package: $NATIVE_MAIL_PACKAGE_SOURCE"
    [[ -f "$REPO_ROOT/bin/agentstack-mailctl" ]] || \
      die "missing AgentStack Mail lifecycle controller: $REPO_ROOT/bin/agentstack-mailctl"
  fi
}

port_in_use() {
  "$PYTHON_BIN" - "$PORT" <<'PY'
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

listener_pids() {
  local lsof_bin
  lsof_bin="$(command -v lsof 2>/dev/null || true)"
  if [[ -z "$lsof_bin" && -x /usr/sbin/lsof ]]; then
    lsof_bin=/usr/sbin/lsof
  fi
  [[ -n "$lsof_bin" ]] || return 1
  "$lsof_bin" -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null |
    sed -n '/^[0-9][0-9]*$/p' | sort -u
}

process_parent_pid() {
  local pid="$1" lsof_bin
  if [[ -r "/proc/$pid/stat" ]]; then
    "$PYTHON_BIN" - "$pid" <<'PY'
import pathlib
import sys

try:
    fields = pathlib.Path(f"/proc/{sys.argv[1]}/stat").read_text().rsplit(")", 1)[1].split()
    parent = int(fields[1])
except (IndexError, OSError, ValueError):
    raise SystemExit(1)
print(parent)
PY
    return
  fi

  lsof_bin="$(command -v lsof 2>/dev/null || true)"
  if [[ -z "$lsof_bin" && -x /usr/sbin/lsof ]]; then
    lsof_bin=/usr/sbin/lsof
  fi
  if [[ -n "$lsof_bin" ]]; then
    "$lsof_bin" -a -p "$pid" -FpR 2>/dev/null |
      sed -n 's/^R//p' | sed -n '1p'
    return
  fi

  ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]'
}

pid_is_same_or_descendant() {
  local candidate="$1" root="$2" parent attempts=0
  [[ "$candidate" =~ ^[0-9]+$ && "$root" =~ ^[0-9]+$ ]] || return 1
  while [[ "$candidate" -gt 1 && "$attempts" -lt 64 ]]; do
    [[ "$candidate" == "$root" ]] && return 0
    parent="$(process_parent_pid "$candidate" || true)"
    [[ "$parent" =~ ^[0-9]+$ && "$parent" != "$candidate" ]] || return 1
    candidate="$parent"
    attempts=$((attempts + 1))
  done
  return 1
}

launchd_dashboard_pid() {
  [[ "$(uname -s)" == "Darwin" ]] || return 1
  command -v launchctl >/dev/null 2>&1 || return 1
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null |
    sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\).*$/\1/p' | sed -n '1p'
}

supervised_dashboard_pid() {
  local pidfile="$RUNTIME_DIR/dashboard.pid" pid
  pid="$(sed -n '1p' "$pidfile" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

MANAGED_SUPERVISED_PID=""

listener_is_managed_dashboard() {
  local listener_pid="$1" manager_pid
  manager_pid="$(launchd_dashboard_pid || true)"
  if [[ -n "$manager_pid" ]] && pid_is_same_or_descendant "$listener_pid" "$manager_pid"; then
    return 0
  fi

  manager_pid="$(supervised_dashboard_pid || true)"
  if [[ -n "$manager_pid" ]] && pid_is_same_or_descendant "$listener_pid" "$manager_pid"; then
    MANAGED_SUPERVISED_PID="$manager_pid"
    return 0
  fi
  return 1
}

check_port() {
  if port_in_use; then
    local listeners listener all_managed=true
    listeners="$(listener_pids || true)"
    if [[ -z "$listeners" ]]; then
      all_managed=false
    else
      while IFS= read -r listener; do
        if ! listener_is_managed_dashboard "$listener"; then
          all_managed=false
          break
        fi
      done <<< "$listeners"
    fi
    if [[ "$all_managed" == true ]]; then
      say "managed dashboard owns port $PORT; replacing it during this install"
      return
    fi
    if [[ "$DRY_RUN" == true ]]; then
      warn "port $PORT is already in use; live install would stop before service registration"
    else
      die "port $PORT is already in use; set AGENTSTACK_PORT or --port"
    fi
  fi
}

detect_service_kind() {
  if [[ "$PREFLIGHT_OS" == "Darwin" ]]; then
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
  "$PYTHON_BIN" - "$legacy_path" "$runtime_path" <<'PY'
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
  "$PYTHON_BIN" - "$legacy_path" "$target_path" <<'PY'
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
  "${PYTHON_BIN:-python3}" - "$source_dir" "$dest_dir" <<'PY'
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
    cp "$SCRIPT_DIR/selftest.py" "$BIN_DIR/agentstack-selftest"
    cp "$MERGE_SETTINGS_SCRIPT" "$BIN_DIR/agentstack-merge-settings"
    cp "$MERGE_CLAUDE_MCP_SCRIPT" "$BIN_DIR/agentstack-merge-claude-mcp"
    mkdir -p "$BIN_DIR/lib"
    cp "$SCRIPT_DIR/lib/agent_mail_passthrough.py" "$BIN_DIR/lib/agent_mail_passthrough.py"
    cp "$SCRIPT_DIR/lib/mcp_endpoint.py" "$BIN_DIR/lib/mcp_endpoint.py"
    cp "$REPO_ROOT/bin/lib/agentstack-launch.sh" "$BIN_DIR/lib/agentstack-launch.sh"
    cp "$REPO_ROOT/bin/lib/agentstack-register.sh" "$BIN_DIR/lib/agentstack-register.sh"
    cp "$REPO_ROOT/bin/lib/agentstack-scientists.sh" "$BIN_DIR/lib/agentstack-scientists.sh"
    cp "$REPO_ROOT/bin/agent-start" "$BIN_DIR/agent-start"
    cp "$REPO_ROOT/bin/agent-start-codex" "$BIN_DIR/agent-start-codex"
    cp "$REPO_ROOT/bin/agentstack-reregister" "$BIN_DIR/agentstack-reregister"
    cp "$REPO_ROOT/bin/agentstack-preregister-child" "$BIN_DIR/agentstack-preregister-child"
    cp "$REPO_ROOT/bin/agentstack-await-reply" "$BIN_DIR/agentstack-await-reply"
    cp "$REPO_ROOT/bin/agentstack-codex-bootstrap" "$BIN_DIR/agentstack-codex-bootstrap"
    cp "$REPO_ROOT/bin/agentstack-codex-setup" "$BIN_DIR/agentstack-codex-setup"
    cp "$REPO_ROOT/bin/agentstack-claude-setup" "$BIN_DIR/agentstack-claude-setup"
    cp "$REPO_ROOT/bin/agentstack-mailctl" "$BIN_DIR/agentstack-mailctl"
    chmod +x "$BIN_DIR/agentstack-uninstall" "$BIN_DIR/agentstack-doctor" \
      "$BIN_DIR/agentstack-selftest" "$BIN_DIR/agentstack-merge-settings" \
      "$BIN_DIR/agentstack-merge-claude-mcp" \
      "$BIN_DIR/agent-start" "$BIN_DIR/agent-start-codex" "$BIN_DIR/agentstack-reregister" \
      "$BIN_DIR/agentstack-preregister-child" "$BIN_DIR/agentstack-await-reply" \
      "$BIN_DIR/agentstack-codex-bootstrap" "$BIN_DIR/agentstack-codex-setup" "$BIN_DIR/agentstack-claude-setup" \
      "$BIN_DIR/agentstack-mailctl"
  fi
}

symlink_points_to() {
  "$PYTHON_BIN" - "$1" "$2" <<'PY'
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
      "$PYTHON_BIN" - "$HOOKS_DIR/settings.template.json" "$HOOKS_DIR" <<'PY'
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
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
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
    "$PYTHON_BIN" "${merge_args[@]}" --dry-run
    return
  fi

  "$PYTHON_BIN" "${merge_args[@]}" --dry-run
  if confirm_safe_merge; then
    "$PYTHON_BIN" "${merge_args[@]}" --result-json "$SAFE_MERGE_RESULT_FILE"
    if [[ "$ASSUME_YES" == "1" ]]; then
      say "assume-yes: applied Tier1 settings merge to $CLAUDE_SETTINGS"
    fi
  else
    say "Skipped Tier1 user-settings merge."
  fi
}

print_claude_mcp_registration_instructions() {
  local helper="$BIN_DIR/agentstack-merge-claude-mcp"
  warn "Claude Code cannot use /delegate until the fixed 'mcp-agent-mail' MCP entry is registered."
  printf 'Preview and apply it manually:\n' >&2
  printf '  %q %q --dry-run --config %q --mcp-url %q --mail-env %q --backup-dir %q\n' \
    "$PYTHON_BIN" "$helper" "$CLAUDE_JSON" "$MCP_URL" "$MAIL_ENV" "$BACKUPS_DIR" >&2
  printf '  %q %q --config %q --mcp-url %q --mail-env %q --backup-dir %q --existing-result %q\n' \
    "$PYTHON_BIN" "$helper" "$CLAUDE_JSON" "$MCP_URL" "$MAIL_ENV" "$BACKUPS_DIR" \
    "$MCP_MERGE_RESULT_FILE" >&2
}

confirm_claude_mcp_merge() {
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    warn "non-interactive shell; skipping Claude MCP user-config merge"
    print_claude_mcp_registration_instructions
    return 1
  fi
  printf "Register the fixed 'mcp-agent-mail' entry in %s? Type yes to continue: " \
    "$CLAUDE_JSON" >&2
  local reply
  read -r reply
  [[ "$reply" == "yes" ]]
}

safe_merge_claude_mcp() {
  if [[ "$TIER" != "tier1" ]]; then
    return
  fi
  local merge_tool="$BIN_DIR/agentstack-merge-claude-mcp"
  if [[ "$DRY_RUN" == true ]]; then
    merge_tool="$MERGE_CLAUDE_MCP_SCRIPT"
  fi
  local merge_args=(
    "$merge_tool"
    --config "$CLAUDE_JSON"
    --mcp-url "$MCP_URL"
    --mail-env "$MAIL_ENV"
    --backup-dir "$BACKUPS_DIR"
    --existing-result "$MCP_MERGE_RESULT_FILE"
  )

  local merge_status
  merge_status="$("$PYTHON_BIN" "${merge_args[@]}" --check)"
  if [[ "$merge_status" == "configured" ]]; then
    say "Claude MCP already registered as mcp-agent-mail in $CLAUDE_JSON"
    return
  fi
  say "Claude MCP user-config safe-merge dry-run: $CLAUDE_JSON"
  "$PYTHON_BIN" "${merge_args[@]}" --dry-run
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi

  if confirm_claude_mcp_merge; then
    "$PYTHON_BIN" "${merge_args[@]}" --result-json "$MCP_MERGE_RESULT_FILE"
    if [[ "$ASSUME_YES" == "1" ]]; then
      say "assume-yes: registered mcp-agent-mail in $CLAUDE_JSON"
    fi
  else
    say "Skipped Claude MCP user-config merge."
  fi
}

confirm_managed_setup() {
  local label="$1"
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
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
    if [[ "$ASSUME_YES" == "1" ]]; then
      say "assume-yes: applied $label managed setup"
    fi
  else
    say "Skipped $label managed setup."
  fi
}

project_key_is_this_checkout() {
  local resolved
  resolved="$(cd "$PROJECT_KEY" 2>/dev/null && pwd -P)" || return 1
  [[ "$resolved" == "$(cd "$REPO_ROOT" && pwd -P)" ]]
}

safe_managed_doc_setups() {
  if [[ "$TIER" != "tier1" ]]; then
    return
  fi
  # Without --project-key the default is this checkout, and the managed block
  # would land in the repo's own CLAUDE.md / AGENTS.md. Those files are tracked,
  # so the write leaves the working tree dirty and `git pull` refuses to update
  # them on every release. The block is meant for the project an agent works in,
  # which is not the copy of the stack it installed from.
  if project_key_is_this_checkout; then
    say "skip managed CLAUDE.md / AGENTS.md blocks: --project-key is this checkout"
    say "  The block belongs in the project you will run agents in. To add it:"
    say "    $BIN_DIR/agentstack-claude-setup   (with AGENTSTACK_PROJECT_KEY set)"
    say "    $BIN_DIR/agentstack-codex-setup"
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
  "$PYTHON_BIN" - "$ENV_FILE" <<PY
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
    "AGENTSTACK_CLAUDE_JSON": "$CLAUDE_JSON",
    "AGENTSTACK_TERMINAL": "$TERMINAL",
    "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
    "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
    "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
    "AGENTSTACK_LANG": "$LANG_SETTING",
    "AGENTSTACK_MURMUR": "$MURMUR_SETTING",
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
if "$MAIL_PROVIDER" == "agentstack":
    values.update({
        "AGENTSTACK_MAIL_PROVIDER": "agentstack",
        "AGENTSTACK_MAIL_DIR": "$NATIVE_MAIL_SERVICE_ROOT",
        "AGENTSTACK_MAIL_STATE_ROOT": "$NATIVE_MAIL_STATE_ROOT",
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "$MAIL_HTTP_BEARER_MODE",
    })
lines = ["# Generated by claude-agent-stack install.sh", "# Do not put secrets in this file.", ""]
for key, value in values.items():
    lines.append(f"export {key}={shlex.quote(value)}")
path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
path.chmod(0o600)
PY
}

render_agent_mail_runner() {
  local uv_bin="$1" host="$2" port="$3" path="$4"
  mkdir -p "$MAIL_HOME"
  "$PYTHON_BIN" - "$AGENT_MAIL_RUNNER" "$uv_bin" "$MAIL_DIR" \
    "$host" "$port" "$path" <<'PY'
import pathlib
import shlex
import sys

runner, uv_bin, mail_dir, host, port, path = sys.argv[1:]
command = [
    uv_bin,
    "--directory", mail_dir,
    "run", "--no-dev", "--no-sync",
    "python", "-m", "mcp_agent_mail.cli", "serve-http",
    "--host", host, "--port", port, "--path", path,
]
text = "\n".join([
    "#!/usr/bin/env bash",
    "set -u",
    "child_pid=''",
    "stop_runner() {",
    "  trap - TERM INT",
    "  if [[ \"$child_pid\" =~ ^[0-9]+$ ]] && kill -0 \"$child_pid\" 2>/dev/null; then",
    "    kill \"$child_pid\" 2>/dev/null || true",
    "    wait \"$child_pid\" 2>/dev/null || true",
    "  fi",
    "  exit 0",
    "}",
    "trap stop_runner TERM INT",
    f"cd {shlex.quote(mail_dir)} || exit 1",
    "while true; do",
    f"  {shlex.join(command)} &",
    "  child_pid=$!",
    "  wait \"$child_pid\" || true",
    "  child_pid=''",
    "  sleep 5",
    "done",
    "",
])
target = pathlib.Path(runner)
target.write_text(text, encoding="utf-8")
target.chmod(0o700)
PY
}

stop_new_agent_mail() {
  local pid
  pid="$(sed -n '1p' "$AGENT_MAIL_PIDFILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$AGENT_MAIL_PIDFILE"
}

start_new_agent_mail() {
  local parts host port path uv_bin pid attempts=0 database_url resolved_db
  parts="$(mcp_local_server_parts)" || \
    die "cannot start agent-mail for non-local endpoint $MCP_URL"
  IFS='|' read -r host port path <<< "$parts"
  uv_bin="$(command -v uv 2>/dev/null || true)"
  [[ -n "$uv_bin" ]] || die "uv is required to prepare a new agent-mail clone; install uv and re-run"

  plan "sync agent-mail dependencies in $MAIL_DIR with uv"
  if [[ "$DRY_RUN" == true ]]; then
    plan "start agent-mail in supervised background mode at $MCP_URL"
    return
  fi
  if ! "$uv_bin" --directory "$MAIL_DIR" sync --no-dev; then
    die "agent-mail dependency setup failed in $MAIL_DIR. Check the uv error above, then re-run the installer."
  fi

  render_agent_mail_runner "$uv_bin" "$host" "$port" "$path"
  pid="$(sed -n '1p' "$AGENT_MAIL_PIDFILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    die "agent-mail endpoint is down, but $AGENT_MAIL_PIDFILE points to live pid $pid. Stop that process or remove the stale setup before re-running."
  fi
  rm -f "$AGENT_MAIL_PIDFILE"
  say "starting agent-mail in supervised background mode at $MCP_URL"
  nohup /bin/bash "$AGENT_MAIL_RUNNER" </dev/null >> "$AGENT_MAIL_LOG" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$AGENT_MAIL_PIDFILE"
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$AGENT_MAIL_PIDFILE"
    die "agent-mail supervisor did not start. Inspect $AGENT_MAIL_LOG, fix the reported error, and re-run."
  fi
  AGENT_MAIL_SERVICE_KIND="nohup"
  AGENT_MAIL_SERVICE_PATH="$AGENT_MAIL_PIDFILE"

  while ! mcp_endpoint_listening && [[ "$attempts" -lt 150 ]]; do
    sleep 0.2
    attempts=$((attempts + 1))
  done
  if ! mcp_endpoint_listening; then
    stop_new_agent_mail
    die "agent-mail did not become reachable at $MCP_URL after dependency setup. Inspect $AGENT_MAIL_LOG, then re-run."
  fi

  discover_agent_mail_listener_process
  database_url="$(probe_agent_mail_database_url || true)"
  resolved_db="$(database_url_to_path "$database_url" "$AGENT_MAIL_LISTENER_CWD" || true)"
  if [[ -z "$resolved_db" || ! -f "$resolved_db" ]]; then
    resolved_db="$(listener_open_database || true)"
  fi
  if [[ -z "$resolved_db" || ! -f "$resolved_db" ]]; then
    stop_new_agent_mail
    die "agent-mail started at $MCP_URL but its SQLite database could not be resolved. Inspect $AGENT_MAIL_LOG and set AGENTSTACK_MAIL_DB before re-running."
  fi
  MAIL_DB="$(normalize_path "$resolved_db")"
  EXISTING_AGENT_MAIL_SERVER=true
  say "agent-mail ready at $MCP_URL (database: $MAIL_DB)"
}

ensure_agent_mail() {
  AGENT_MAIL_PASSTHROUGH_CONFIRMED=false
  if [[ "$EXISTING_AGENT_MAIL_SERVER" == true ]]; then
    plan "reuse existing agent-mail server at $MCP_URL"
    if [[ -f "$MAIL_ENV" ]]; then
      plan "reuse existing agent-mail .env at $MAIL_ENV"
    else
      warn "no agent-mail bearer .env was resolved; localhost must allow unauthenticated access"
    fi
    offer_passthrough_to_existing_server
    return
  fi

  local mail_dir_is_ours=true
  if [[ -e "$MAIL_DIR" ]]; then
    if [[ -d "$MAIL_DIR/.git" ]]; then
      local remote
      remote="$(git -C "$MAIL_DIR" remote get-url origin 2>/dev/null || true)"
      if [[ -n "$remote" && "$remote" != "$UPSTREAM_AGENT_MAIL_URL" ]]; then
        warn "existing agent-mail remote is '$remote' (expected '$UPSTREAM_AGENT_MAIL_URL'); leaving it untouched"
        mail_dir_is_ours=false
      else
        plan "reuse existing agent-mail clone at $MAIL_DIR"
        report_agent_mail_ref
      fi
    else
      die "agent-mail path exists but is not a git clone: $MAIL_DIR"
    fi
  else
    plan "clone agent-mail upstream into $MAIL_DIR at $UPSTREAM_AGENT_MAIL_REF"
    if [[ "$DRY_RUN" != true ]]; then
      if ! git clone "$UPSTREAM_AGENT_MAIL_URL" "$MAIL_DIR"; then
        die "failed to clone agent-mail from $UPSTREAM_AGENT_MAIL_URL. Check network access and the repository URL, then re-run."
      fi
      # Check out the pinned ref rather than whatever the default branch is
      # today. A clone with no ref makes the install date part of the
      # configuration, invisibly.
      if ! git -C "$MAIL_DIR" checkout --quiet "$UPSTREAM_AGENT_MAIL_REF"; then
        die "agent-mail was cloned but ref '$UPSTREAM_AGENT_MAIL_REF' could not be checked out. Set AGENTSTACK_AGENT_MAIL_REF to a ref that exists in $UPSTREAM_AGENT_MAIL_URL, then re-run."
      fi
    fi
  fi

  # A clone this installer made is ours, so its naming behaviour is ours to fix
  # rather than ask about. Without it, the name an agent registers under depends
  # on which upstream version happened to be checked out. A checkout pointing
  # somewhere else was already declared untouched above, and stays that way.
  if [[ "$PASSTHROUGH_ENABLED" != "1" ]]; then
    say "agent-mail naming mode: left as it is (AGENTSTACK_AGENT_MAIL_PASSTHROUGH=0)"
  elif [[ "$mail_dir_is_ours" != true ]]; then
    say "agent-mail naming mode: left as it is, along with the rest of $MAIL_DIR"
  else
    plan "set agent-mail naming mode to passthrough in $MAIL_DIR"
    if [[ "$DRY_RUN" != true ]]; then
      local state
      state="$(passthrough_state "$MAIL_DIR")"
      case "$state" in
        already)
          AGENT_MAIL_PASSTHROUGH_CONFIRMED=true
          say "agent-mail already accepts explicit names"
          ;;
        applicable)
          apply_passthrough "$MAIL_DIR" || \
            die "the agent-mail naming patch did not apply to $MAIL_DIR. Remove that checkout and re-run to get the pinned version, or set AGENTSTACK_AGENT_MAIL_REF to the ref you want."
          if [[ "$(passthrough_state "$MAIL_DIR")" != "already" ]]; then
            die "the agent-mail naming patch returned success but passthrough could not be verified in $MAIL_DIR"
          fi
          AGENT_MAIL_PASSTHROUGH_CONFIRMED=true
          say "patched agent-mail to accept explicit names"
          ;;
        *)
          warn "agent-mail at $MAIL_DIR does not match the known naming patch ($state)"
          warn "  names this stack asks for may be replaced by generated ones"
          ;;
      esac
    fi
  fi

  if [[ -f "$MAIL_ENV" ]]; then
    plan "reuse existing agent-mail .env at $MAIL_ENV"
    if [[ "$DRY_RUN" != true && "$AGENT_MAIL_PASSTHROUGH_CONFIRMED" == true ]]; then
      set_env_key "$MAIL_ENV" "AGENT_NAME_ENFORCEMENT_MODE" "passthrough"
    fi
  else
    plan "create agent-mail .env at $MAIL_ENV (mode 600; token hidden)"
    if [[ "$DRY_RUN" != true ]]; then
      mkdir -p "$(dirname "$MAIL_ENV")"
      umask 077
      "$PYTHON_BIN" - "$MAIL_ENV" "$AGENT_MAIL_PASSTHROUGH_CONFIRMED" <<'PY'
import pathlib
import secrets
import sys
path = pathlib.Path(sys.argv[1])
token = secrets.token_urlsafe(32)
lines = [f"HTTP_BEARER_TOKEN={token}"]
if sys.argv[2] == "true":
    lines.append("AGENT_NAME_ENFORCEMENT_MODE=passthrough")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
    fi
  fi

  if [[ "$PROVISION_AGENT_MAIL" == true ]]; then
    start_new_agent_mail
  fi

}

native_mail_binaries_ready() {
  [[ -x "$NATIVE_MAIL_VENV/bin/agentstack-mail" && \
     -x "$NATIVE_MAIL_VENV/bin/agentstack-mail-service" && \
     -x "$NATIVE_MAIL_VENV/bin/agentstack-mail-migrate" ]]
}

ensure_native_mail_candidate() {
  if native_mail_binaries_ready; then
    plan "reuse immutable AgentStack Mail candidate venv $NATIVE_MAIL_VENV"
    return
  fi
  if [[ -e "$NATIVE_MAIL_VENV" ]]; then
    die "AgentStack Mail candidate venv exists but is incomplete: $NATIVE_MAIL_VENV"
  fi
  if [[ -n "$NATIVE_MAIL_VENV_EXPLICIT" ]]; then
    die "AGENTSTACK_MAIL_SERVICE_VENV does not contain the required AgentStack Mail executables: $NATIVE_MAIL_VENV"
  fi
  if [[ "$NATIVE_MAIL_PACKAGE_SOURCE" == "$REPO_ROOT/packages/agentstack_mail" ]] && \
     git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    local dirty_package
    dirty_package="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all -- packages/agentstack_mail)"
    [[ -z "$dirty_package" ]] || \
      die "bundled AgentStack Mail package is dirty; build a candidate from an exact clean commit"
  fi
  plan "create immutable AgentStack Mail candidate venv $NATIVE_MAIL_VENV"
  plan "install bundled AgentStack Mail package from $NATIVE_MAIL_PACKAGE_SOURCE"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  local uv_bin
  uv_bin="$(command -v uv 2>/dev/null || true)"
  [[ -n "$uv_bin" ]] || die "uv is required to install AgentStack Mail"
  mkdir -p "$(dirname "$NATIVE_MAIL_VENV")"
  if ! "$uv_bin" venv --python "$PYTHON_BIN" "$NATIVE_MAIL_VENV"; then
    die "failed to create AgentStack Mail candidate venv: $NATIVE_MAIL_VENV"
  fi
  if ! "$uv_bin" pip install --python "$NATIVE_MAIL_VENV/bin/python" \
    "$NATIVE_MAIL_PACKAGE_SOURCE"; then
    die "failed to install bundled AgentStack Mail into $NATIVE_MAIL_VENV"
  fi
  native_mail_binaries_ready || \
    die "AgentStack Mail installation completed without the required executables"
}

write_native_mail_env() {
  local parts host port path
  parts="$(mcp_local_server_parts)" || \
    die "AgentStack Mail requires a local HTTP endpoint: $MCP_URL"
  IFS='|' read -r host port path <<< "$parts"
  plan "render namespaced AgentStack Mail service env $NATIVE_MAIL_ENV"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  mkdir -p "$(dirname "$NATIVE_MAIL_ENV")" "$NATIVE_MAIL_SERVICE_ROOT/runtime"
  umask 077
  "$PYTHON_BIN" - "$NATIVE_MAIL_ENV" "$host" "$port" "$path" \
    "$NATIVE_MAIL_STATE_ROOT" <<'PY'
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
host, port, path, raw_root = sys.argv[2:]
root = pathlib.Path(raw_root)
values = {
    "AGENTSTACK_MAIL_HTTP_HOST": host,
    "AGENTSTACK_MAIL_HTTP_PORT": port,
    "AGENTSTACK_MAIL_HTTP_PATH": path,
    "AGENTSTACK_MAIL_HTTP_PATH_ALIASES": "/mcp,/api",
    "AGENTSTACK_MAIL_DATABASE_URL": f"sqlite+aiosqlite:///{root / 'storage.sqlite3'}",
    "AGENTSTACK_MAIL_STORAGE_ROOT": str(root / "archive"),
    "AGENTSTACK_MAIL_NOTIFICATIONS_ENABLED": "true",
    "AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR": str(root / "signals"),
    "AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE": "passthrough",
}
payload = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
if target.exists() and target.read_text(encoding="utf-8") != payload:
    raise SystemExit(f"refusing to rewrite immutable service env: {target}")
target.write_text(payload, encoding="utf-8")
target.chmod(0o600)
PY
}

initialize_native_mail_state() {
  if [[ -f "$MAIL_DB" && -d "$NATIVE_MAIL_STATE_ROOT/archive" ]]; then
    return
  fi
  [[ ! -e "$NATIVE_MAIL_STATE_ROOT" ]] || \
    die "AgentStack Mail state root is partial; refusing initialization: $NATIVE_MAIL_STATE_ROOT"
  plan "initialize empty AgentStack Mail state at $NATIVE_MAIL_STATE_ROOT"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  local bootstrap_pid attempts=0
  AGENTSTACK_MAIL_ENV_FILE="$NATIVE_MAIL_ENV" \
    "$NATIVE_MAIL_VENV/bin/agentstack-mail" >> "$NATIVE_MAIL_LOG" 2>&1 &
  bootstrap_pid=$!
  while ! mcp_endpoint_listening && [[ "$attempts" -lt 150 ]]; do
    sleep 0.2
    attempts=$((attempts + 1))
  done
  if ! mcp_endpoint_listening; then
    kill "$bootstrap_pid" 2>/dev/null || true
    wait "$bootstrap_pid" 2>/dev/null || true
    die "AgentStack Mail bootstrap did not become reachable; inspect $NATIVE_MAIL_LOG"
  fi
  probe_agent_mail_database_url >/dev/null || {
    kill "$bootstrap_pid" 2>/dev/null || true
    wait "$bootstrap_pid" 2>/dev/null || true
    die "AgentStack Mail bootstrap did not return health"
  }
  kill "$bootstrap_pid" 2>/dev/null || true
  wait "$bootstrap_pid" 2>/dev/null || true
  [[ -f "$MAIL_DB" && -d "$NATIVE_MAIL_STATE_ROOT/archive" ]] || \
    die "AgentStack Mail bootstrap did not create its canonical database/archive"
}

render_native_mail_runner() {
  "$PYTHON_BIN" - "$NATIVE_MAIL_RUNNER" \
    "$NATIVE_MAIL_VENV/bin/agentstack-mail-service" \
    "$NATIVE_MAIL_VENV/bin/agentstack-mail" "$NATIVE_MAIL_ENV" \
    "$NATIVE_MAIL_STATE_ROOT" <<'PY'
import pathlib
import shlex
import sys

runner, service, server, env_file, state_root = sys.argv[1:]
command = [
    service, "foreground",
    "--server-executable", server,
    "--env-file", env_file,
    "--state-root", state_root,
]
payload = "\n".join([
    "#!/usr/bin/env bash",
    "set -u",
    "child_pid=''",
    "stop_runner() {",
    "  trap - TERM INT",
    "  if [[ \"$child_pid\" =~ ^[0-9]+$ ]] && kill -0 \"$child_pid\" 2>/dev/null; then",
    "    kill \"$child_pid\" 2>/dev/null || true",
    "    wait \"$child_pid\" 2>/dev/null || true",
    "  fi",
    "  exit 0",
    "}",
    "trap stop_runner TERM INT",
    "while true; do",
    f"  {shlex.join(command)} &",
    "  child_pid=$!",
    "  wait \"$child_pid\" || true",
    "  child_pid=''",
    "  sleep 5",
    "done",
    "",
])
target = pathlib.Path(runner)
if target.exists() and target.read_text(encoding="utf-8") != payload:
    raise SystemExit(f"refusing to rewrite immutable service runner: {target}")
target.write_text(payload, encoding="utf-8")
target.chmod(0o700)
PY
}

start_native_mail() {
  local database_url resolved_db
  plan "start AgentStack Mail with agentstack-mailctl at $MCP_URL"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  render_native_mail_runner
  AGENTSTACK_MAILCTL_SKIP_ENV=1 \
  AGENTSTACK_HOME="$INSTALL_DIR" \
  AGENTSTACK_MAIL_PROVIDER=agentstack \
  AGENTSTACK_MAIL_DIR="$NATIVE_MAIL_SERVICE_ROOT" \
  AGENTSTACK_MAIL_ENV="$NATIVE_MAIL_ENV" \
  AGENTSTACK_MAIL_RUNNER="$NATIVE_MAIL_RUNNER" \
  AGENTSTACK_MAIL_PIDFILE="$NATIVE_MAIL_PIDFILE" \
  AGENTSTACK_MAIL_LOG="$NATIVE_MAIL_LOG" \
  AGENTSTACK_MAIL_DB="$MAIL_DB" \
  AGENTSTACK_MCP_URL="$MCP_URL" \
  AGENTSTACK_PYTHON="$PYTHON_BIN" \
    "$BIN_DIR/agentstack-mailctl" start
  AGENT_MAIL_SERVICE_KIND="nohup"
  AGENT_MAIL_SERVICE_PATH="$NATIVE_MAIL_PIDFILE"
  database_url="$(probe_agent_mail_database_url || true)"
  resolved_db="$(database_url_to_path "$database_url" "" || true)"
  [[ -n "$resolved_db" ]] || {
    stop_new_agent_mail
    die "AgentStack Mail health did not report a local SQLite database"
  }
  resolved_db="$(normalize_path "$resolved_db")"
  if [[ "$resolved_db" != "$MAIL_DB" ]]; then
    stop_new_agent_mail
    die "AgentStack Mail started on '$resolved_db', expected isolated database '$MAIL_DB'"
  fi
  EXISTING_AGENT_MAIL_SERVER=true
  say "AgentStack Mail ready at $MCP_URL (database: $MAIL_DB)"
}

ensure_native_agentstack_mail() {
  AGENT_MAIL_NAME_CAPABILITY_JSON='{"status":"honored","evidence":"agentstack-cutover-profile","enforcement_mode":"passthrough","mail_dir":"","detail":"AgentStack Mail service env requires passthrough","warning":""}'
  if [[ "$NATIVE_MAIL_EXISTING" == true ]]; then
    plan "reuse existing AgentStack Mail service at $MCP_URL"
    return
  fi
  ensure_native_mail_candidate
  write_native_mail_env
  initialize_native_mail_state
  start_native_mail
}

# Deliberately minimal. `agentstack-mailctl` sources $AGENTSTACK_HOME/env.sh
# unless AGENTSTACK_MAILCTL_SKIP_ENV=1, so the unit runs the *same* command a
# user runs by hand and reads the same configuration. Baking the mail paths into
# the unit instead would drift the moment a re-install renders a new service env,
# and the drift would only show up after the next reboot.
mail_autostart_environment() {
  cat <<ENVLIST
HOME=$HOME
AGENTSTACK_HOME=$INSTALL_DIR
PATH=$PATH_VALUE
AGENTSTACK_MAILCTL_SWEEP=1
ENVLIST
} # end mail_autostart_environment

# One-shot on purpose: `agentstack-mailctl start` spawns the runner with nohup and
# exits, so a KeepAlive/Restart=always unit would respawn the controller in a loop
# instead of supervising the server. `start` is idempotent — it reports "already
# running" and exits 0 when the pidfile process is alive and healthy — so running
# it at every login is safe.
render_mail_autostart_unit() {
  local kind="$1"
  # Its own log: the trigger re-checks every few minutes and would otherwise
  # write "already running" into the mail server's own log forever. Derived here
  # because NATIVE_MAIL_SERVICE_ROOT is still being resolved at parse time.
  local autostart_log="$NATIVE_MAIL_SERVICE_ROOT/runtime/agentstack-mail-autostart.log"
  if [[ "$kind" == "launchd" ]]; then
    AGENT_MAIL_AUTOSTART_PATH="$HOME/Library/LaunchAgents/$MAIL_AUTOSTART_LABEL.plist"
    plan "render launchd plist $AGENT_MAIL_AUTOSTART_PATH (AgentStack Mail autostart)"
    [[ "$DRY_RUN" == true ]] && return 0
    mkdir -p "$HOME/Library/LaunchAgents"
    # The env goes through argv, not a pipe: the `<<'PY'` heredoc already owns
    # this command's stdin, so a pipe would be silently discarded and the plist
    # would ship with an empty EnvironmentVariables block.
    "$PYTHON_BIN" - \
      "$AGENT_MAIL_AUTOSTART_PATH" "$MAIL_AUTOSTART_LABEL" \
      "$BIN_DIR/agentstack-mailctl" "$autostart_log" \
      "$(mail_autostart_environment)" <<'PY'
import pathlib
import plistlib
import sys

dst, label, mailctl, logfile, env_blob = sys.argv[1:6]
env = {}
for line in env_blob.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        env[key] = value

plist = {
    "Label": label,
    "ProgramArguments": [mailctl, "start"],
    "RunAtLoad": True,
    # Not KeepAlive: mailctl exits after handing the server to nohup, so launchd
    # would respawn the *controller* in a loop instead of supervising the server.
    "KeepAlive": False,
    # Re-check periodically instead. `start` is idempotent — it reports "already
    # running" and exits 0 — so this is a cheap liveness sweep that also covers
    # the cases RunAtLoad alone cannot: the runner being killed mid-session, and
    # a port that was briefly contended at login.
    "StartInterval": 300,
    "StandardOutPath": logfile,
    "StandardErrorPath": logfile,
    "EnvironmentVariables": env,
}
path = pathlib.Path(dst)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_bytes(plistlib.dumps(plist))
tmp.replace(path)
PY
    return 0
  fi

  AGENT_MAIL_AUTOSTART_PATH="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.timer"
  AGENT_MAIL_AUTOSTART_SERVICE_PATH="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service"
  plan "render systemd user units $AGENT_MAIL_AUTOSTART_SERVICE_PATH and $AGENT_MAIL_AUTOSTART_PATH (AgentStack Mail autostart)"
  [[ "$DRY_RUN" == true ]] && return 0
  mkdir -p "$HOME/.config/systemd/user"
  {
    # NOT After=default.target: a unit installed into default.target.wants/ is
    # already ordered by systemd's own Wants completion, and ordering against
    # default.target as well is a cycle, which systemd resolves by dropping a
    # job. The dashboard unit uses network.target for the same reason.
    printf '[Unit]\nDescription=AgentStack Mail autostart\nAfter=network.target\n\n'
    # No RemainAfterExit: the timer re-runs this unit, and a unit left "active"
    # after exiting would never be started again.
    printf '[Service]\nType=oneshot\n'
    # systemd splits unquoted values on whitespace, so a HOME or install dir
    # containing a space would silently truncate every Environment= value and
    # the ExecStart path. Quote them the way systemd expects.
    "$PYTHON_BIN" - "$BIN_DIR/agentstack-mailctl" "$(mail_autostart_environment)" <<'PY_UNIT'
import sys

mailctl, env_blob = sys.argv[1:3]


def quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


for line in env_blob.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        print(f"Environment={key}={quote(value)}")
print(f"ExecStart={quote(mailctl)} start")
print()
PY_UNIT
    # systemd has no equivalent of launchd's StandardOutPath in the plist, so
    # say it here or the "output goes to a dedicated log" promise is false on
    # Linux (it was).
    printf 'StandardOutput=append:%s\nStandardError=append:%s\n' \
      "$autostart_log" "$autostart_log"
  } > "$AGENT_MAIL_AUTOSTART_SERVICE_PATH.tmp"
  mv "$AGENT_MAIL_AUTOSTART_SERVICE_PATH.tmp" "$AGENT_MAIL_AUTOSTART_SERVICE_PATH"
  # The timer is what gets enabled: it covers boot *and* keeps checking, which a
  # login-only trigger cannot (a runner killed mid-session stays dead until the
  # next login). launchd gets the same behaviour from StartInterval.
  {
    printf '[Unit]\nDescription=AgentStack Mail autostart timer\n\n'
    # No Persistent=: it only affects OnCalendar timers, and implying that a
    # missed monotonic firing is caught up after resume is simply wrong.
    printf '[Timer]\nOnBootSec=1min\nOnUnitActiveSec=5min\nAccuracySec=30s\n'
    printf 'Unit=%s.service\n\n' "$MAIL_AUTOSTART_LABEL"
    printf '[Install]\nWantedBy=timers.target\n'
  } > "$AGENT_MAIL_AUTOSTART_PATH.tmp"
  mv "$AGENT_MAIL_AUTOSTART_PATH.tmp" "$AGENT_MAIL_AUTOSTART_PATH"
} # end render_mail_autostart_unit

retire_legacy_mail_services() {
  # After a cutover the predecessor keeps running unless something retires it,
  # and nothing did: the only code that boots out a previous job is the
  # same-port handoff, which never fires because the new server binds a
  # different port, and the legacy label it looks for is a single guessed
  # string (com.<user>.mcp-agent-mail) that does not match what the older
  # installers actually registered (org.agentstack.mcp-agent-mail). Reported by
  # a tester on 2026-08-17 with both jobs loaded and both listening.
  #
  # Two mail servers is not only waste: they own separate databases, so agent
  # identities and file reservations silently split across two stores depending
  # on which endpoint a client reaches.
  local labels=()
  if [[ -n "${AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS:-}" ]]; then
    IFS=',' read -r -a labels <<< "$AGENTSTACK_MAIL_LEGACY_LAUNCHD_LABELS"
  else
    labels=("com.$(id -un).mcp-agent-mail" "org.agentstack.mcp-agent-mail")
  fi

  if [[ "$(uname -s)" != "Darwin" ]] || ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi

  local label found=0
  for label in "${labels[@]}"; do
    label="${label// /}"
    [[ -n "$label" ]] || continue
    launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 || continue
    found=1
    if [[ "$KEEP_LEGACY_MAIL" == true ]]; then
      warn "legacy mail service '$label' is still loaded and was left alone (--keep-legacy-mail). Two mail servers means two databases; point every client at one of them."
      continue
    fi
    if [[ "$DRY_RUN" == true ]]; then
      say "DRY-RUN would retire legacy mail service: $label"
      continue
    fi
    # Park the plist rather than delete it. Retiring someone's running service
    # is not the kind of thing an installer should make unrecoverable.
    local plist="$HOME/Library/LaunchAgents/$label.plist"
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    if [[ -f "$plist" ]]; then
      mkdir -p "$INSTALL_DIR/parked-launchd"
      mv "$plist" "$INSTALL_DIR/parked-launchd/$label.plist"
      say "retired legacy mail service $label (plist parked at $INSTALL_DIR/parked-launchd/$label.plist)"
    else
      say "retired legacy mail service $label"
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      warn "legacy mail service '$label' is still loaded after bootout; retire it by hand before using mail"
    fi
  done
  if [[ "$found" == 0 ]]; then
    return 0
  fi
}

enable_mail_autostart() {
  local kind=""
  case "$(uname -s)" in
    Darwin) command -v launchctl >/dev/null 2>&1 && kind="launchd" ;;
    Linux)  command -v systemctl >/dev/null 2>&1 && kind="systemd-user" ;;
  esac
  # Never register a trigger that is already known to fail: mailctl reads env.sh
  # and dies if the service env is missing, and a unit that fails only at boot is
  # worse than no unit at all — nothing reports it until mail is silently absent.
  if [[ "$DRY_RUN" != true && ! -f "$MAIL_ENV" ]]; then
    warn "AgentStack Mail service env is missing ($MAIL_ENV); skipping the login trigger because it would fail at boot. Stop the mail server and re-run install.sh to render one, then mail will restart automatically."
    return 0
  fi

  if [[ -z "$kind" ]]; then
    # Say it out loud. A missing autostart is invisible until the machine
    # reboots, which is exactly how this gap survived on the maintainer's Mac.
    warn "no supported service manager found; AgentStack Mail will NOT restart after a reboot. Start it manually with: $BIN_DIR/agentstack-mailctl start"
    return 0
  fi

  # Compute the path first so the existing unit can be preserved: the renderer
  # overwrites it, and a failed re-registration would otherwise leave the machine
  # with neither the old trigger nor the new one — visible only at the next boot.
  local previous=""
  if [[ "$kind" == "launchd" ]]; then
    AGENT_MAIL_AUTOSTART_PATH="$HOME/Library/LaunchAgents/$MAIL_AUTOSTART_LABEL.plist"
  else
    AGENT_MAIL_AUTOSTART_PATH="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.timer"
  fi
  local previous_service=""
  if [[ "$DRY_RUN" != true && -f "$AGENT_MAIL_AUTOSTART_PATH" ]]; then
    previous="$AGENT_MAIL_AUTOSTART_PATH.prev"
    rm -f "$previous"
    cp "$AGENT_MAIL_AUTOSTART_PATH" "$previous"
  fi
  # The systemd timer is useless without the service it triggers: restoring only
  # the timer reports success while leaving nothing to activate.
  if [[ "$DRY_RUN" != true && "$kind" == "systemd-user" ]]; then
    local existing_service="$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service"
    if [[ -f "$existing_service" ]]; then
      previous_service="$existing_service.prev"
      rm -f "$previous_service"
      cp "$existing_service" "$previous_service"
    fi
  fi

  render_mail_autostart_unit "$kind"

  if [[ "$DRY_RUN" == true ]]; then
    if [[ "$kind" == "launchd" ]]; then
      say "DRY-RUN would run: launchctl bootstrap gui/$(id -u) $AGENT_MAIL_AUTOSTART_PATH"
      say "DRY-RUN would run: launchctl enable gui/$(id -u)/$MAIL_AUTOSTART_LABEL"
    else
      say "DRY-RUN would run: systemctl --user enable --now $MAIL_AUTOSTART_LABEL.timer"
    fi
    AGENT_MAIL_AUTOSTART_KIND="$kind"
    return 0
  fi

  if [[ "$kind" == "launchd" ]]; then
    launchctl bootout "gui/$(id -u)/$MAIL_AUTOSTART_LABEL" 2>/dev/null || true
    # As with the dashboard: the bootstrap call is the capability probe. Never
    # infer availability from login metadata.
    if launchctl bootstrap "gui/$(id -u)" "$AGENT_MAIL_AUTOSTART_PATH" && \
       launchctl enable "gui/$(id -u)/$MAIL_AUTOSTART_LABEL"
    then
      AGENT_MAIL_AUTOSTART_KIND="launchd"
      [[ -n "$previous" ]] && rm -f "$previous"
      say "AgentStack Mail will restart at login ($MAIL_AUTOSTART_LABEL)"
      return 0
    fi
    launchctl bootout "gui/$(id -u)/$MAIL_AUTOSTART_LABEL" 2>/dev/null || true
    rm -f "$AGENT_MAIL_AUTOSTART_PATH"
    if [[ -n "$previous" ]]; then
      mv "$previous" "$AGENT_MAIL_AUTOSTART_PATH"
      if launchctl bootstrap "gui/$(id -u)" "$AGENT_MAIL_AUTOSTART_PATH" 2>/dev/null; then
        warn "kept the previous AgentStack Mail login trigger; the new one could not be registered"
        AGENT_MAIL_AUTOSTART_KIND="launchd"
        return 0
      fi
    fi
  else
    # `enable` (not `enable --now`): the server is already running from
    # start_native_mail, and a oneshot start here would only re-probe it.
    # An older install enabled the *service* directly; leave that symlink in
    # place and it keeps firing alongside the timer.
    systemctl --user disable "$MAIL_AUTOSTART_LABEL.service" 2>/dev/null || true
    # `enable` alone only writes the symlink — the timer does not start until the
    # next login, so a re-install would not supervise anything until then.
    if systemctl --user daemon-reload && \
       systemctl --user enable --now "$MAIL_AUTOSTART_LABEL.timer"
    then
      AGENT_MAIL_AUTOSTART_KIND="systemd-user"
      [[ -n "$previous" ]] && rm -f "$previous"
      [[ -n "$previous_service" ]] && rm -f "$previous_service"
      say "AgentStack Mail will restart at login ($MAIL_AUTOSTART_LABEL.timer)"
      return 0
    fi
    systemctl --user disable "$MAIL_AUTOSTART_LABEL.timer" 2>/dev/null || true
    rm -f "$AGENT_MAIL_AUTOSTART_PATH" "$AGENT_MAIL_AUTOSTART_SERVICE_PATH"
    if [[ -n "$previous" ]]; then
      mv "$previous" "$AGENT_MAIL_AUTOSTART_PATH"
      [[ -n "$previous_service" ]] && \
        mv "$previous_service" "$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service"
      if [[ -f "$HOME/.config/systemd/user/$MAIL_AUTOSTART_LABEL.service" ]] && \
         systemctl --user daemon-reload 2>/dev/null && \
         systemctl --user enable --now "$MAIL_AUTOSTART_LABEL.timer" 2>/dev/null; then
        warn "kept the previous AgentStack Mail login trigger; the new one could not be registered"
        AGENT_MAIL_AUTOSTART_KIND="systemd-user"
        return 0
      fi
    fi
    # systemd caches unit files; without this the removed unit lingers in the
    # manager's view until something else reloads it.
    systemctl --user daemon-reload 2>/dev/null || true
  fi

  rm -f "${previous:-/nonexistent}" "${previous_service:-/nonexistent}" 2>/dev/null || true
  AGENT_MAIL_AUTOSTART_PATH=""
  warn "could not register the AgentStack Mail autostart unit; mail will NOT restart after a reboot. Start it manually with: $BIN_DIR/agentstack-mailctl start"
}

render_launchd_plist() {
  local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  plan "render launchd plist $plist"
  if [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    "$PYTHON_BIN" - "$REPO_ROOT/dashboard/agentdashboard.plist.template" "$plist" <<PY
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
    "__MAIL_HTTP_BEARER_MODE__": "$MAIL_HTTP_BEARER_MODE",
    "__SIGNALS_DIR__": "$SIGNALS_DIR",
    "__MCP_URL__": "$MCP_URL",
    "__TERMINAL__": "$TERMINAL",
    "__PROJECT_KEY__": "$PROJECT_KEY",
    "__PROTECTED_ROOTS__": "$PROTECTED_ROOTS",
    "__DELIVERABLE_ROOTS__": "$DELIVERABLE_ROOTS",
    "__LANG__": "$LANG_SETTING",
    "__MURMUR__": "$MURMUR_SETTING",
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
    "$PYTHON_BIN" - "$unit" <<PY
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
env = {
    "AGENTSTACK_PORT": "$PORT",
    "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
    "AGENTSTACK_MAIL_DB": "$MAIL_DB",
    "AGENTSTACK_MAIL_ENV": "$MAIL_ENV",
    "AGENTSTACK_MAIL_HOME": "$MAIL_HOME",
    "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "$MAIL_HTTP_BEARER_MODE",
    "AGENTSTACK_SIGNALS_DIR": "$SIGNALS_DIR",
    "AGENTSTACK_MCP_URL": "$MCP_URL",
    "AGENTSTACK_TERMINAL": "$TERMINAL",
    "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
    "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
    "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
    "AGENTSTACK_LANG": "$LANG_SETTING",
    "AGENTSTACK_MURMUR": "$MURMUR_SETTING",
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
        say "DRY-RUN would run: launchctl bootstrap gui/$(id -u) $SERVICE_PATH"
        say "DRY-RUN would run: launchctl enable gui/$(id -u)/$LABEL"
        say "DRY-RUN would run: launchctl kickstart gui/$(id -u)/$LABEL"
        say "DRY-RUN note: a real run treats those commands as the probe; if the"
        say "  gui/$(id -u) domain refuses them it switches to supervised background mode."
        ACTIVE_SERVICE_KIND="launchd"
        return
      fi
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      # A GUI domain can disappear while the user is logged in (for example
      # while the display is asleep), so the bootstrap operation itself is the
      # capability probe. Never infer availability from login metadata.
      if launchctl bootstrap "gui/$(id -u)" "$SERVICE_PATH" && \
         launchctl enable "gui/$(id -u)/$LABEL" && \
         launchctl kickstart "gui/$(id -u)/$LABEL"
      then
        ACTIVE_SERVICE_KIND="launchd"
        return
      fi
      warn "launchd could not bootstrap $LABEL in gui/$(id -u); falling back to supervised background mode"
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      rm -f "$SERVICE_PATH"
      SERVICE_FALLBACK_USED=true
      start_supervised_background || true
      ;;
    systemd-user)
      render_systemd_unit
      if [[ "$DRY_RUN" == true ]]; then
        run systemctl --user daemon-reload
        run systemctl --user enable --now "$LABEL.service"
        ACTIVE_SERVICE_KIND="systemd-user"
        return
      fi
      if systemctl --user daemon-reload && \
         systemctl --user enable --now "$LABEL.service"
      then
        ACTIVE_SERVICE_KIND="systemd-user"
        return
      fi
      warn "systemd user service setup failed; falling back to supervised background mode"
      systemctl --user disable --now "$LABEL.service" 2>/dev/null || true
      rm -f "$SERVICE_PATH"
      SERVICE_FALLBACK_USED=true
      start_supervised_background || true
      ;;
    nohup)
      start_supervised_background || true
      ;;
    *)
      warn "unknown service kind '$kind'; dashboard was not started"
      ACTIVE_SERVICE_KIND="manual"
      SERVICE_PATH=""
      ;;
  esac
}

start_supervised_background() {
  SERVICE_PATH="$RUNTIME_DIR/dashboard.pid"
  ACTIVE_SERVICE_KIND="nohup"
  plan "start dashboard in supervised background mode, pidfile $SERVICE_PATH"
  if [[ "$DRY_RUN" == true ]]; then
    return 0
  fi
  mkdir -p "$RUNTIME_DIR"
  stop_supervised_background || return 1
  (
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    AGENTSTACK_DASHBOARD_SELF_RESTART=1 \
      nohup "$PYTHON_BIN" "$DASHBOARD_DIR/service_runner.py" >> "$DASHBOARD_LOG" 2>&1 &
    echo $! > "$SERVICE_PATH"
  )
  local supervisor_pid
  supervisor_pid="$(sed -n '1p' "$SERVICE_PATH" 2>/dev/null || true)"
  if [[ ! "$supervisor_pid" =~ ^[0-9]+$ ]] || ! kill -0 "$supervisor_pid" 2>/dev/null; then
    warn "could not start the supervised background dashboard"
    rm -f "$SERVICE_PATH"
    ACTIVE_SERVICE_KIND="manual"
    SERVICE_PATH=""
    return 1
  fi
}

supervised_pid_matches_state() {
  local pid="$1" state_file="$RUNTIME_DIR/dashboard-service.json"
  [[ -f "$state_file" ]] || return 1
  "$PYTHON_BIN" - "$state_file" "$pid" <<'PY'
import json
import pathlib
import sys

try:
    state = json.loads(pathlib.Path(sys.argv[1]).read_text())
    recorded = int(state.get("supervisor_pid", 0))
    expected = int(sys.argv[2])
except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if recorded == expected else 1)
PY
}

stop_supervised_background() {
  local pid attempts=0
  pid="$(sed -n '1p' "$RUNTIME_DIR/dashboard.pid" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    if [[ "$pid" != "$MANAGED_SUPERVISED_PID" ]] && ! supervised_pid_matches_state "$pid"; then
      warn "refusing to stop unverified process $pid from $RUNTIME_DIR/dashboard.pid"
      ACTIVE_SERVICE_KIND="manual"
      SERVICE_PATH=""
      return 1
    fi
    say "stopping supervised background dashboard with pid $pid before replacement"
    kill "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && [[ "$attempts" -lt 50 ]]; do
      sleep 0.1
      attempts=$((attempts + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    attempts=0
    while port_in_use && [[ "$attempts" -lt 50 ]]; do
      sleep 0.1
      attempts=$((attempts + 1))
    done
    if port_in_use; then
      warn "supervised background dashboard did not release port $PORT"
      ACTIVE_SERVICE_KIND="manual"
      SERVICE_PATH=""
      return 1
    fi
  fi
  rm -f "$RUNTIME_DIR/dashboard.pid"
}

verify_dashboard_service() {
  plan "verify dashboard API responds at http://127.0.0.1:$PORT/api/agents"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  if "$PYTHON_BIN" - "$PORT" <<'PY'
import sys
import time
import urllib.error
import urllib.request

port = int(sys.argv[1])
url = f"http://127.0.0.1:{port}/api/agents"
deadline = time.monotonic() + 15
last_error = "no response"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status == 200:
                raise SystemExit(0)
            last_error = f"HTTP {response.status}"
    except (OSError, urllib.error.URLError) as exc:
        last_error = str(exc)
    time.sleep(0.25)
print(f"dashboard health check failed: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
  then
    SERVICE_HEALTHY=true
    say "dashboard healthy: http://127.0.0.1:$PORT/api/agents"
  else
    SERVICE_HEALTHY=false
    warn "dashboard service did not become healthy; install files and managed blocks were still completed"
    warn "inspect $DASHBOARD_LOG and start the dashboard manually"
  fi
}

write_manifest() {
  plan "write manifest $MANIFEST"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  local service_kind="$1"
  local service_path="$2"
  local mail_service_kind="${3:-}"
  local mail_service_path="${4:-}"
  local tmp="$MANIFEST.tmp"
  "$PYTHON_BIN" - "$tmp" "$service_kind" "$service_path" \
    "$mail_service_kind" "$mail_service_path" "$AGENT_MAIL_NAME_CAPABILITY_JSON" \
    "${AGENT_MAIL_AUTOSTART_KIND:-}" "${AGENT_MAIL_AUTOSTART_PATH:-}" \
    "$MAIL_AUTOSTART_LABEL" "${AGENT_MAIL_AUTOSTART_SERVICE_PATH:-}" <<PY
import json
import os
import pathlib
import time
import sys

out = pathlib.Path(sys.argv[1])
service_kind = sys.argv[2]
service_path = sys.argv[3]
mail_service_kind = sys.argv[4]
mail_service_path = sys.argv[5]
requested_name_honoring = json.loads(sys.argv[6])
mail_autostart_kind = sys.argv[7]
mail_autostart_path = sys.argv[8]
mail_autostart_label = sys.argv[9]
mail_autostart_service_path = sys.argv[10]
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
if mail_autostart_path:
    owned_files.append(mail_autostart_path)
if mail_autostart_service_path:
    owned_files.append(mail_autostart_service_path)
if "$MAIL_PROVIDER" == "agentstack":
    for raw in ("$NATIVE_MAIL_ENV", "$NATIVE_MAIL_RUNNER"):
        path = pathlib.Path(raw)
        if path.is_file() or path.is_symlink():
            owned_files.append(str(path))
merge_result_path = pathlib.Path("$SAFE_MERGE_RESULT_FILE")
settings_merge = None
if merge_result_path.exists():
    settings_merge = json.loads(merge_result_path.read_text(encoding="utf-8"))
    owned_files.append(str(merge_result_path))
mcp_merge_result_path = pathlib.Path("$MCP_MERGE_RESULT_FILE")
claude_mcp_merge = None
if mcp_merge_result_path.exists():
    claude_mcp_merge = json.loads(
        mcp_merge_result_path.read_text(encoding="utf-8")
    )
    owned_files.append(str(mcp_merge_result_path))
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
if mail_service_kind == "nohup" and mail_service_path:
    services.append({"kind": "nohup", "pidfile": mail_service_path, "role": "agent-mail"})
# Recorded so uninstall.sh tears the autostart unit down through the same
# services loop it already uses for the dashboard.
if mail_autostart_kind == "launchd" and mail_autostart_path:
    services.append({"kind": "launchd", "label": mail_autostart_label,
                     "path": mail_autostart_path, "role": "agent-mail-autostart"})
elif mail_autostart_kind == "systemd-user" and mail_autostart_path:
    # The timer is the enabled unit; the service it triggers is a plain file.
    services.append({"kind": "systemd-user", "unit": f"{mail_autostart_label}.timer",
                     "path": mail_autostart_path, "role": "agent-mail-autostart"})
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
    "claude_mcp_merge": claude_mcp_merge,
    "agent_mail": {
        "requested_name_honoring": requested_name_honoring,
    },
    "env": {
        "AGENTSTACK_PORT": "$PORT",
        "AGENTSTACK_LABEL_PREFIX": "$LABEL_PREFIX",
        "AGENTSTACK_PROJECT_KEY": "$PROJECT_KEY",
        "AGENTSTACK_PROTECTED_ROOTS": "$PROTECTED_ROOTS",
        "AGENTSTACK_DELIVERABLE_ROOTS": "$DELIVERABLE_ROOTS",
        "AGENTSTACK_LANG": "$LANG_SETTING",
        "AGENTSTACK_MURMUR": "$MURMUR_SETTING",
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
        "AGENTSTACK_CLAUDE_JSON": "$CLAUDE_JSON",
        "AGENTSTACK_TERMINAL": "$TERMINAL",
    },
    "owned_files": owned_files,
    "owned_dirs": owned_dirs,
    "skill_links": skill_links,
    "services": services,
    "backups": [
        merge.get("backup")
        for merge in (settings_merge, claude_mcp_merge)
        if merge and merge.get("backup")
    ],
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
        "Claude MCP user config uses an explicit-confirm, fixed-name structural merge.",
    ],
}
if "$MAIL_PROVIDER" == "agentstack":
    manifest["agent_mail"]["provider"] = "agentstack"
    manifest["agent_mail"]["state_root"] = "$NATIVE_MAIL_STATE_ROOT"
    manifest["agent_mail"]["candidate_venv"] = "$NATIVE_MAIL_VENV"
    manifest["env"].update({
        "AGENTSTACK_MAIL_PROVIDER": "agentstack",
        "AGENTSTACK_MAIL_DIR": "$NATIVE_MAIL_SERVICE_ROOT",
        "AGENTSTACK_MAIL_STATE_ROOT": "$NATIVE_MAIL_STATE_ROOT",
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE": "$MAIL_HTTP_BEARER_MODE",
    })
out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
PY
  "$PYTHON_BIN" -m json.tool "$tmp" >/dev/null
  mv "$tmp" "$MANIFEST"
}

main() {
  say "claude-agent-stack core installer"
  say "tier: $TIER"
  say "install dir: $INSTALL_DIR"
  say "project key: $PROJECT_KEY"
  validate_assume_yes
  validate_mail_provider
  if ! run_preflight; then
    exit 1
  fi
  if [[ "$TIER" == "tier1" ]]; then
    say "Tier1 will show MCP and user-settings dry-run diffs before any merge."
  elif [[ "$TIER" == "tier2" ]]; then
    say "Phase 3a note: Tier2 project enable is a placeholder; no project settings are modified."
  fi
  check_dependencies
  validate_repo_assets
  check_port
  if [[ "$MAIL_PROVIDER" == "agentstack" ]]; then
    resolve_native_mail_connection
  else
    resolve_agent_mail_connection
  fi
  if ! check_agent_mail_provisioning_dependencies; then
    exit 1
  fi
  local service_kind
  service_kind="$(detect_service_kind)"
  # detect_service_kind knows which manager to try, not whether it will work:
  # the bootstrap below is the capability probe. Say "planned" so a dry-run,
  # which never reaches that probe, cannot be read as a promise.
  say "planned service mode: $service_kind (falls back to supervised background if it cannot start)"
  create_layout
  migrate_legacy_annotations
  migrate_legacy_dashboard_log
  install_payload
  install_claude_skill_links
  render_installed_templates
  if [[ "$MAIL_PROVIDER" == "agentstack" ]]; then
    ensure_native_agentstack_mail
  else
    ensure_agent_mail
  fi
  report_agent_mail_name_capability
  write_env_file
  # After write_env_file: the unit runs `agentstack-mailctl start`, which reads
  # env.sh. Registering it earlier would fire RunAtLoad against a config that
  # does not exist yet. This is outside ensure_native_agentstack_mail on purpose
  # — that function returns early when a healthy server is already running, which
  # is precisely the case for every existing user re-running install.sh to
  # update, and they need the autostart most.
  if [[ "$MAIL_PROVIDER" == "agentstack" ]]; then
    retire_legacy_mail_services
    enable_mail_autostart
  elif [[ "$PROVISION_AGENT_MAIL" == true ]]; then
    # The upstream opt-out provisions its own nohup runner, which has the same
    # reboot defect. It has no mailctl-equivalent to invoke from a login trigger,
    # so say so instead of leaving the gap silent. A server we merely *detected*
    # is someone else's to manage and is not mentioned here.
    warn "the upstream agent-mail provider does not restart after a reboot; restart it yourself or switch to the bundled provider (unset AGENTSTACK_MAIL_PROVIDER)"
  fi
  safe_merge_claude_mcp
  safe_merge_settings
  safe_managed_doc_setups
  start_service "$service_kind"
  write_manifest "${ACTIVE_SERVICE_KIND:-manual}" "${SERVICE_PATH:-}" \
    "${AGENT_MAIL_SERVICE_KIND:-}" "${AGENT_MAIL_SERVICE_PATH:-}"
  verify_dashboard_service
  if [[ "$DRY_RUN" == true ]]; then
    say "Dry-run complete: no files were written."
  else
    say "Install complete: $URL"
    say "Manifest: $MANIFEST"
    say "Dashboard log: $DASHBOARD_LOG"
    say "Run doctor: $BIN_DIR/agentstack-doctor"
    say "Verify operation: $BIN_DIR/agentstack-selftest"
    if [[ "$SERVICE_FALLBACK_USED" == true ]]; then
      say "Service mode: supervised background (launchd/systemd unavailable)"
    fi
    if [[ "$SERVICE_HEALTHY" != true ]]; then
      say "Dashboard was not started. Manual supervised start:"
      say "  . $ENV_FILE"
      say "  AGENTSTACK_DASHBOARD_SELF_RESTART=1 nohup $PYTHON_BIN $DASHBOARD_DIR/service_runner.py >> $DASHBOARD_LOG 2>&1 &"
      say "  echo \$! > $RUNTIME_DIR/dashboard.pid"
    fi
    if [[ "$TIER" != "tier0" ]]; then
      say "Recommended managed setup:"
      say "  hooks/settings.json via Tier1 settings merge"
      say "  $BIN_DIR/agentstack-codex-setup    (managed block in ~/.codex/AGENTS.md)"
      say "  $BIN_DIR/agentstack-claude-setup   (managed block in project/global CLAUDE.md)"
    fi
  fi
}

main "$@"
