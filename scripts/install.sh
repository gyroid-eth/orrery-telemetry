#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_SETTINGS_SCRIPT="$SCRIPT_DIR/lib/merge_settings.py"
MERGE_CLAUDE_MCP_SCRIPT="$SCRIPT_DIR/lib/merge_claude_mcp.py"

DRY_RUN=false
ASSUME_YES="${AGENTSTACK_ASSUME_YES:-0}"
TIER="tier1"
TIER_OPTION=""
INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MAIL_DIR="${AGENTSTACK_MAIL_DIR:-$HOME/mcp_agent_mail}"
MAIL_HOME="${AGENTSTACK_MAIL_HOME:-$HOME/.mcp_agent_mail}"
MAIL_DB_EXPLICIT="${AGENTSTACK_MAIL_DB+x}"
MAIL_ENV_EXPLICIT="${AGENTSTACK_MAIL_ENV+x}"
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
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
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

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

validate_assume_yes() {
  [[ "$ASSUME_YES" == "0" || "$ASSUME_YES" == "1" ]] || \
    die "AGENTSTACK_ASSUME_YES must be 0 or 1"
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

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "missing required dependency '$1'. Install it manually, then re-run install.sh."
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
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

select_python() {
  local requested="${AGENTSTACK_PYTHON:-}"
  local candidate version
  if [[ -n "$requested" ]]; then
    candidate="$(resolve_python_candidate "$requested")"
    if [[ -z "$candidate" || ! -x "$candidate" ]]; then
      die "AGENTSTACK_PYTHON is not an executable Python interpreter: $requested"
    fi
    version="$(python_version "$candidate")"
    if ! python_is_compatible "$candidate"; then
      die "AGENTSTACK_PYTHON must be Python 3.10 or newer; found $version at $candidate"
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
    if python_is_compatible "$candidate"; then
      PYTHON_BIN="$candidate"
      say "python: $PYTHON_BIN ($version)"
      return
    fi
  done

  [[ -n "$checked" ]] || checked="no python3 candidates found"
  die "Python 3.10 or newer is required; checked: $checked. Install a current Python or set AGENTSTACK_PYTHON."
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

mcp_endpoint_listening() {
  local parts host port
  parts="$(mcp_endpoint_parts)" || return 1
  IFS='|' read -r host port <<< "$parts"
  "$PYTHON_BIN" - "$host" "$port" <<'PY'
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.5):
        pass
except OSError:
    raise SystemExit(1)
PY
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

check_dependencies() {
  need_cmd tmux
  need_cmd git
  if ! command -v fswatch >/dev/null 2>&1; then
    warn "optional dependency 'fswatch' not found; mail watcher will use polling"
  fi
  if [[ "$(uname -s)" == "Darwin" ]] && [[ "$TERMINAL" != "none" ]]; then
    if [[ ! -d /Applications/Ghostty.app && ! -d "$HOME/Applications/Ghostty.app" ]] && ! command -v ghostty >/dev/null 2>&1; then
      warn "Ghostty not found; AGENTSTACK_TERMINAL=auto will fall back when possible"
    fi
  fi
}

check_agent_mail_provisioning_dependencies() {
  if [[ "$PROVISION_AGENT_MAIL" == true ]]; then
    need_cmd uv
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
  [[ -f "$SCRIPT_DIR/selftest.py" ]] || die "missing scripts/selftest.py"
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
    chmod +x "$BIN_DIR/agentstack-uninstall" "$BIN_DIR/agentstack-doctor" \
      "$BIN_DIR/agentstack-selftest" "$BIN_DIR/agentstack-merge-settings" \
      "$BIN_DIR/agentstack-merge-claude-mcp" \
      "$BIN_DIR/agent-start" "$BIN_DIR/agent-start-codex" "$BIN_DIR/agentstack-reregister" \
      "$BIN_DIR/agentstack-preregister-child" \
      "$BIN_DIR/agentstack-codex-bootstrap" "$BIN_DIR/agentstack-codex-setup" "$BIN_DIR/agentstack-claude-setup"
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
  if [[ "$EXISTING_AGENT_MAIL_SERVER" == true ]]; then
    plan "reuse existing agent-mail server at $MCP_URL"
    if [[ -f "$MAIL_ENV" ]]; then
      plan "reuse existing agent-mail .env at $MAIL_ENV"
    else
      warn "no agent-mail bearer .env was resolved; localhost must allow unauthenticated access"
    fi
    return
  fi

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
    if [[ "$DRY_RUN" != true ]] && ! git clone "$UPSTREAM_AGENT_MAIL_URL" "$MAIL_DIR"; then
      die "failed to clone agent-mail from $UPSTREAM_AGENT_MAIL_URL. Check network access and the repository URL, then re-run."
    fi
  fi

  if [[ -f "$MAIL_ENV" ]]; then
    plan "reuse existing agent-mail .env at $MAIL_ENV"
  else
    plan "create agent-mail .env at $MAIL_ENV (mode 600; token hidden)"
    if [[ "$DRY_RUN" != true ]]; then
      mkdir -p "$(dirname "$MAIL_ENV")"
      umask 077
      "$PYTHON_BIN" - "$MAIL_ENV" <<'PY'
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

  if [[ "$PROVISION_AGENT_MAIL" == true ]]; then
    start_new_agent_mail
  fi

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
    "$mail_service_kind" "$mail_service_path" <<PY
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
  if [[ "$TIER" == "tier1" ]]; then
    say "Tier1 will show MCP and user-settings dry-run diffs before any merge."
  elif [[ "$TIER" == "tier2" ]]; then
    say "Phase 3a note: Tier2 project enable is a placeholder; no project settings are modified."
  fi
  select_python
  check_dependencies
  validate_repo_assets
  check_port
  resolve_agent_mail_connection
  check_agent_mail_provisioning_dependencies
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
  ensure_agent_mail
  write_env_file
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
