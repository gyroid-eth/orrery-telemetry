#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MANIFEST="$INSTALL_DIR/install-state.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: doctor.sh [--install-dir PATH] [--report]

Checks the core ORRERY Telemetry install footprint without modifying files.

  --report   Also print a paste-ready environment report for a bug report.
             Every failure this project has had came from an environment
             difference, and each one cost several rounds of asking. Values
             only; no tokens, no Authorization headers.
EOF
}

REPORT="${AGENTSTACK_DOCTOR_REPORT:-0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="$2"
      MANIFEST="$INSTALL_DIR/install-state.json"
      shift 2
      ;;
    --report)
      REPORT=1
      shift
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

status=0

if [[ -f "$INSTALL_DIR/env.sh" ]]; then
  echo "ok: env $INSTALL_DIR/env.sh"
  # shellcheck disable=SC1090
  . "$INSTALL_DIR/env.sh"
else
  echo "missing: env $INSTALL_DIR/env.sh" >&2
  status=1
fi

check_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "ok: $1"
  else
    echo "missing: $1" >&2
    status=1
  fi
}

PYTHON_BIN="${AGENTSTACK_PYTHON:-$(command -v python3 2>/dev/null || true)}"
if [[ -x "$PYTHON_BIN" ]] && \
   "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
then
  echo "ok: Python 3.11+ ($PYTHON_BIN)"
else
  echo "missing: Python 3.11+ interpreter" >&2
  status=1
fi
check_cmd tmux
check_cmd git
check_cmd uv

if [[ -f "$MANIFEST" ]]; then
  echo "ok: manifest $MANIFEST"
  "$PYTHON_BIN" -m json.tool "$MANIFEST" >/dev/null || status=1
else
  echo "missing: manifest $MANIFEST" >&2
  status=1
fi

if [[ -x "$INSTALL_DIR/hooks/spawn_child.sh" ]]; then
  echo "ok: hooks installed"
else
  echo "missing: hooks under $INSTALL_DIR/hooks" >&2
  status=1
fi

MAIL_DB_PATH="${AGENTSTACK_MAIL_DB:-}"
if [[ -n "$MAIL_DB_PATH" && -f "$MAIL_DB_PATH" ]]; then
  echo "ok: ORRERY Mail database $MAIL_DB_PATH"
else
  echo "missing: AGENTSTACK_MAIL_DB does not point to an existing file: ${MAIL_DB_PATH:-<unset>}" >&2
  status=1
fi

if [[ "${AGENTSTACK_MAIL_HTTP_BEARER_MODE:-}" == "disabled" ]]; then
  echo "ok: ORRERY Mail transport uses owner tokens without a legacy HTTP bearer"
else
  echo "missing: ORRERY Mail requires AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled" >&2
  status=1
fi
  NATIVE_MAIL_HEALTH="$("$PYTHON_BIN" - \
    "${AGENTSTACK_MCP_URL:-}" "$MAIL_DB_PATH" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys
import urllib.request

url, expected_raw = sys.argv[1:]
expected = pathlib.Path(expected_raw).expanduser().resolve(strict=False)
payload = json.dumps({
    "jsonrpc": "2.0",
    "id": "agentstack-doctor-native-health",
    "method": "tools/call",
    "params": {"name": "health_check", "arguments": {}},
}).encode()
request = urllib.request.Request(
    url,
    data=payload,
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=3) as response:
    raw = response.read().decode("utf-8", errors="replace")
for line in raw.splitlines():
    if line.startswith("data:"):
        raw = line[5:].strip()
        break
body = json.loads(raw)
result = body.get("result") or {}
health = result.get("structuredContent") or {}
if not health:
    for block in result.get("content") or []:
        if block.get("type") == "text":
            health = json.loads(block.get("text") or "{}")
            break
database_url = str(health.get("database_url") or "")
for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
    if database_url.startswith(prefix):
        actual = pathlib.Path(database_url[len(prefix):]).resolve(strict=False)
        break
else:
    raise SystemExit(1)
if health.get("status") != "ok" or actual != expected:
    raise SystemExit(1)
print(actual)
PY
)"
  if [[ -n "$NATIVE_MAIL_HEALTH" ]]; then
    echo "ok: ORRERY Mail health serving $NATIVE_MAIL_HEALTH"
  else
    echo "missing: ORRERY Mail health does not serve the configured native database at ${AGENTSTACK_MCP_URL:-<unset>}" >&2
    status=1
  fi

# What the running Mail cannot do yet. A Mail older than this install answers
# health and passes every check above, while a feature the dashboard or a
# launcher relies on is silently missing (2026-10-02: a resume fell back to a
# conversation without Mail because register_agent had no existing_agent_id).
# This is a warning, not a failure: Mail itself works. The last line is for
# setup.sh to read:
#   mail-features: status=<ok|missing|unknown> missing=<tool.parameter,...> running=<source id>
MAIL_FEATURES_FILE="$SCRIPT_DIR/lib/mail_required_features.json"
[[ -f "$MAIL_FEATURES_FILE" ]] || MAIL_FEATURES_FILE="$SCRIPT_DIR/../scripts/lib/mail_required_features.json"
MAIL_FEATURES="$("$PYTHON_BIN" - "${AGENTSTACK_MCP_URL:-}" "$MAIL_FEATURES_FILE" \
  "${AGENTSTACK_MAIL_DIR:-$INSTALL_DIR/mail-service}" <<'PY' 2>/dev/null || echo "unknown||"
import json
import pathlib
import sys
import urllib.request

url, features_file, service_root = sys.argv[1:]
try:
    pid_text, runner = (pathlib.Path(service_root, "runtime", "agentstack-mail.pid").read_text().splitlines() + ["", ""])[:2]
    render = pathlib.Path(runner).parent
    running = json.loads((render / "deployment.json").read_text()).get("source_id") or render.name.rsplit("-", 1)[0]
except (OSError, ValueError, AttributeError):
    running = ""
try:
    wanted = json.loads(pathlib.Path(features_file).read_text(encoding="utf-8"))["features"]
except (OSError, ValueError, KeyError):
    print(f"unknown||{running}")
    raise SystemExit(0)
request = urllib.request.Request(
    url,
    data=json.dumps({"jsonrpc": "2.0", "id": "agentstack-doctor-features", "method": "tools/list", "params": {}}).encode(),
    headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=3) as response:
        raw = response.read().decode("utf-8", errors="replace")
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    tools = {tool.get("name"): set(((tool.get("inputSchema") or {}).get("properties") or {}))
             for tool in json.loads(raw)["result"]["tools"]}
except Exception:
    print(f"unknown||{running}")
    raise SystemExit(0)
missing = [f for f in wanted if f["parameter"] not in tools.get(f["tool"], set())]
print(("missing" if missing else "ok") + "|" + ",".join(f"{f['tool']}.{f['parameter']}" for f in missing) + "|" + running)
for f in missing:
    print(f"{f['tool']}.{f['parameter']}|{f['needed_for']}|{f['without_it']}")
PY
)"
IFS='|' read -r mail_features_status mail_features_missing mail_features_running <<< "$(head -n 1 <<< "$MAIL_FEATURES")"
case "$mail_features_status" in
  ok) echo "ok: ORRERY Mail has every feature this install relies on" ;;
  missing)
    while IFS='|' read -r name needed_for without_it; do
      echo "warn: ORRERY Mail${mail_features_running:+ (running $mail_features_running)} lacks $name, which this install relies on" >&2
    done < <(tail -n +2 <<< "$MAIL_FEATURES")
    MAIL_NOTICE="$SCRIPT_DIR/lib/mail_update_notice.py"
    [[ -f "$MAIL_NOTICE" ]] || MAIL_NOTICE="$SCRIPT_DIR/../scripts/lib/mail_update_notice.py"
    # The checkout this install came from, so the notice can tell whether the
    # running Mail is older than it (an update from an older checkout goes back).
    MAIL_REPO="$("$PYTHON_BIN" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("repo_root") or "")' "$MANIFEST" 2>/dev/null || true)"
    MAIL_REPO_HEAD=""
    [[ -z "$MAIL_REPO" ]] || MAIL_REPO_HEAD="$(git -C "$MAIL_REPO" rev-parse HEAD 2>/dev/null || true)"
    "$PYTHON_BIN" "$MAIL_NOTICE" stale --running "$mail_features_running" --missing "$mail_features_missing" \
      --features "$MAIL_FEATURES_FILE" ${MAIL_REPO_HEAD:+--repo "$MAIL_REPO" --to "$MAIL_REPO_HEAD"} \
      2>/dev/null | sed 's/^/      /' >&2 || true
    ;;
  *) echo "warn: could not read the running ORRERY Mail's tools; its features were not checked" >&2 ;;
esac
echo "mail-features: status=${mail_features_status:-unknown} missing=$mail_features_missing running=$mail_features_running"

CLAUDE_JSON="${AGENTSTACK_CLAUDE_JSON:-$HOME/.claude.json}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:18765/mcp}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-}"
CLAUDE_MCP_STATE="$("$PYTHON_BIN" - "$CLAUDE_JSON" "$MCP_URL" "$SCRIPT_DIR/lib" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys

sys.path.insert(0, sys.argv[3])
try:
    from mcp_endpoint import same_endpoint
except ImportError:
    # Our own file is missing. Say so, rather than blaming the config we read.
    # (No apostrophes here: bash 3.2 scans this heredoc for the closing paren
    # of the surrounding command substitution and treats one as a quote.)
    print("unavailable")
    raise SystemExit(0)

try:
    config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    entry = config.get("mcpServers", {}).get("orrery-mail")
except (AttributeError, OSError, ValueError):
    print("invalid")
else:
    authorization = (
        (entry.get("headers") or {}).get("Authorization")
        if isinstance(entry, dict)
        else None
    )
    if (
        isinstance(entry, dict)
        and entry.get("type") == "http"
        and same_endpoint(str(entry.get("url", "")), sys.argv[2])
        and authorization is None
    ):
        print("configured")
    else:
        print("missing")
PY
)"
if [[ "$CLAUDE_MCP_STATE" == "configured" ]]; then
  echo "ok: Claude MCP orrery-mail registered in $CLAUDE_JSON"
elif [[ "$CLAUDE_MCP_STATE" == "unavailable" ]]; then
  echo "warn: cannot check the Claude MCP entry: $SCRIPT_DIR/lib/mcp_endpoint.py is missing" >&2
  echo "      This is an incomplete install, not a problem with $CLAUDE_JSON." >&2
  status=1
else
  echo "warn: Claude MCP orrery-mail is not registered for $MCP_URL in $CLAUDE_JSON" >&2
  echo "      /delegate cannot use ORRERY Mail until this fixed-name entry exists." >&2
  MCP_MERGE_HELPER="$INSTALL_DIR/bin/agentstack-merge-claude-mcp"
  printf '      preview: %q %q --dry-run --config %q --mcp-url %q --mail-env %q --backup-dir %q\n' \
    "$PYTHON_BIN" "$MCP_MERGE_HELPER" "$CLAUDE_JSON" "$MCP_URL" "$MAIL_ENV" \
    "$INSTALL_DIR/backups" >&2
  printf '      apply:   %q %q --config %q --mcp-url %q --mail-env %q --backup-dir %q --existing-result %q\n' \
    "$PYTHON_BIN" "$MCP_MERGE_HELPER" "$CLAUDE_JSON" "$MCP_URL" "$MAIL_ENV" \
    "$INSTALL_DIR/backups" "$INSTALL_DIR/runtime/claude-mcp-merge-result.json" >&2
fi

# Claude Code shows its first-run setup (text style, login) in every new
# session until it has been finished once, which ~/.claude.json records as
# hasCompletedOnboarding. A child launched before that stops on the setup
# screen, so say so here. Only whether the flag is set is printed; the launcher
# never answers the setup and nothing here writes it.
if command -v claude >/dev/null 2>&1; then
  CLAUDE_ONBOARDING_STATE="$("$PYTHON_BIN" - "$CLAUDE_JSON" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys

try:
    config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except FileNotFoundError:
    print("not-done")
except (OSError, ValueError):
    print("unreadable")
else:
    print("done" if isinstance(config, dict) and config.get("hasCompletedOnboarding") is True else "not-done")
PY
)"
  case "$CLAUDE_ONBOARDING_STATE" in
    done) echo "ok: Claude Code first-run setup finished" ;;
    not-done)
      echo "warn: Claude Code's first-run setup is not finished ($CLAUDE_JSON has no hasCompletedOnboarding)" >&2
      echo "      Children launched now stop on its setup screens. Run 'claude' once in a terminal and go" >&2
      echo "      through text style, login, Security notes and trusting the folder until the normal" >&2
      echo "      input prompt appears; answer any other one-time question it asks, then /exit." >&2
      ;;
    *) echo "warn: could not read $CLAUDE_JSON to check Claude Code's first-run setup" >&2 ;;
  esac
fi

if [[ -f "$INSTALL_DIR/dashboard/server.py" && \
      -f "$INSTALL_DIR/dashboard/service_runner.py" ]]; then
  echo "ok: dashboard installed"
else
  echo "missing: dashboard under $INSTALL_DIR/dashboard" >&2
  status=1
fi

DASHBOARD_LOG="${AGENTSTACK_DASHBOARD_LOG:-${AGENTSTACK_RUNTIME_DIR:-$INSTALL_DIR/runtime}/dashboard.log}"
if [[ -f "$DASHBOARD_LOG" ]]; then
  echo "ok: dashboard log $DASHBOARD_LOG"
else
  echo "missing: dashboard log $DASHBOARD_LOG" >&2
  echo "         the dashboard service may not have started; inspect the service manager" >&2
  status=1
fi

dashboard_endpoint_serving() {
  local python_bin="$1"
  local port="$2"
  "$python_bin" - "$port" <<'PY' >/dev/null 2>&1
import json
import sys
import urllib.request

try:
    port = int(sys.argv[1])
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/version", timeout=1
    ) as response:
        payload = json.load(response)
    healthy = (
        response.status == 200
        and isinstance(payload, dict)
        and payload.get("name") in ("orrery-telemetry", "claude-agent-stack")
        # Any API generation: this only checks that the port serves ORRERY
        # Telemetry (the generation went from 1 to 2 in 2026.09).
        and isinstance(payload.get("api"), int)
        and not isinstance(payload.get("api"), bool)
        and payload.get("api") >= 1
    )
except (OSError, ValueError, TypeError):
    healthy = False
raise SystemExit(0 if healthy else 1)
PY
}

# Fetches /api/mail-watcher-health from the already-serving dashboard and
# warns using the exact same watcher_running / status fields the cockpit
# shows, so doctor cannot report "ok" while the cockpit shows the watcher red.
report_mail_watcher_health() {
  local python_bin="$1"
  local port="$2"
  local record
  record="$("$python_bin" - "$port" <<'PY' 2>/dev/null || true
import json
import sys
import urllib.request

try:
    port = int(sys.argv[1])
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/mail-watcher-health", timeout=2
    ) as response:
        health = json.load(response)
except Exception:
    health = {}
running = bool(health.get("watcher_running"))
hstatus = health.get("status") or "unknown"
age = health.get("last_success_age_s")
print("|".join((
    "1" if running else "0",
    str(hstatus),
    "" if age is None else str(age),
)))
PY
)"
  local running hstatus age
  IFS='|' read -r running hstatus age <<< "$record"
  if [[ "$running" == "1" ]]; then
    echo "ok: mail watcher running (status: ${hstatus:-unknown})"
  else
    echo "warn: mail watcher is not running (dashboard /api/mail-watcher-health reports watcher_running=false, status=${hstatus:-unknown})" >&2
    echo "      mail delivered to an agent waiting for input will not wake it; see docs/troubleshooting*.md (mail watcher)" >&2
    status=1
  fi
}

report_dashboard_service() {
  local python_bin="${AGENTSTACK_PYTHON:-python3}"
  local port="${AGENTSTACK_PORT:-8770}"
  local record kind identity service_path pid launchd_record
  local endpoint_serving=0 manager_running=0
  if dashboard_endpoint_serving "$python_bin" "$port"; then
    endpoint_serving=1
    echo "ok: dashboard endpoint serving (http://127.0.0.1:$port/api/version)"
  else
    echo "warn: dashboard endpoint is not serving an ORRERY Telemetry API at http://127.0.0.1:$port/api/version"
    status=1
  fi

  # Reuse the dashboard's own /api/mail-watcher-health judgment (same code the
  # cockpit reads) instead of re-deriving it here: all items can be "ok" while
  # the watcher is actually down, which is exactly what went unreported in the
  # 2026-10-05 two-user-on-one-Mac lock collision.
  if [[ "$endpoint_serving" -eq 1 ]]; then
    report_mail_watcher_health "$python_bin" "$port"
  fi
  record="$("$python_bin" - "$MANIFEST" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys

try:
    services = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")).get("services", [])
except (OSError, ValueError):
    services = []
if services:
    service = services[0]
    print("|".join((
        str(service.get("kind", "")),
        str(service.get("label") or service.get("unit") or ""),
        str(service.get("path") or service.get("pidfile") or ""),
    )))
PY
)"
  IFS='|' read -r kind identity service_path <<< "$record"
  case "$kind" in
    launchd)
      if ! command -v launchctl >/dev/null 2>&1; then
        echo "warn: dashboard service mode launchd, but launchctl is unavailable"
        status=1
      elif launchd_record="$(launchctl print "gui/$(id -u)/$identity" 2>/dev/null)"; then
        if printf '%s\n' "$launchd_record" | grep -Eq \
          '^[[:space:]]*(state[[:space:]]*=[[:space:]]*running|pid[[:space:]]*=[[:space:]]*[1-9][0-9]*)[[:space:]]*$'
        then
          manager_running=1
          echo "ok: dashboard service mode launchd (gui/$(id -u)/$identity, running)"
        else
          echo "warn: dashboard service mode launchd, but its launchd job is loaded but not running: gui/$(id -u)/$identity"
          status=1
        fi
      else
        echo "warn: dashboard service mode launchd, but gui/$(id -u)/$identity is not loaded"
        status=1
      fi
      ;;
    systemd-user)
      if command -v systemctl >/dev/null 2>&1 && \
         systemctl --user is-active --quiet "$identity" >/dev/null 2>&1
      then
        manager_running=1
        echo "ok: dashboard service mode systemd-user ($identity)"
      else
        echo "warn: dashboard service mode systemd-user, but $identity is not active"
        status=1
      fi
      ;;
    nohup)
      pid="$(sed -n '1p' "$service_path" 2>/dev/null || true)"
      if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        manager_running=1
        echo "ok: dashboard service mode supervised-background (pid $pid)"
      else
        echo "warn: dashboard service mode supervised-background, but its pidfile is stale or missing: $service_path"
        status=1
      fi
      ;;
    *)
      echo "warn: dashboard service mode manual; no active service manager is recorded"
      ;;
  esac
  if [[ "$endpoint_serving" == 1 && "$manager_running" != 1 ]]; then
    echo "warn: dashboard is serving but is not managed by the recorded service; actual mode is unmanaged-background"
  elif [[ "$endpoint_serving" != 1 && "$manager_running" == 1 ]]; then
    echo "warn: dashboard service manager is running but the dashboard endpoint is unavailable"
  fi
}

if [[ -f "$MANIFEST" ]]; then
  report_dashboard_service
fi

# Without the proxy a spawned child still works, but its ORRERY Mail connection
# is not authenticated as itself: the child has to read its own token instead.
# That degradation is silent at spawn time, so surface it here.
CHILD_MCP_PROXY="$INSTALL_DIR/integrations/codex_app/plugin/scripts/run-mcp.sh"
if [[ -x "$CHILD_MCP_PROXY" ]]; then
  if [[ -d "$INSTALL_DIR/integrations/codex_app/src/agentstack_codex_app" ]]; then
    echo "ok: child MCP proxy installed"
  else
    echo "warn: child MCP proxy runner present but its source tree is missing;" \
         "spawned children will fall back to the shared ORRERY Mail endpoint"
  fi
else
  echo "warn: child MCP proxy missing ($CHILD_MCP_PROXY);" \
       "spawned children fall back to the shared ORRERY Mail endpoint and must" \
       "read their own token. Re-run scripts/install.sh to install it."
fi

# Compare each managed block with what the installed template renders today,
# through the setup script's own --check (the same comparison the installer's
# summary uses). A begin marker alone no longer counts as ok: an old block kept
# agents on superseded instructions while doctor reported it healthy.
check_managed_block() {
  local label="$1" script="$INSTALL_DIR/bin/$2"
  shift 2
  if [[ ! -x "$script" ]]; then
    echo "warn: cannot check $label managed block: $script is missing; re-run scripts/install.sh"
    return 0
  fi
  env "$@" AGENTSTACK_HOME="$INSTALL_DIR" "$script" --check || true
}

# Keep binary resolution aligned with hooks/spawn_child.sh:find_codex_bin.
codex_launcher_search_path() {
  local extra="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.nodebrew/current/bin:/opt/homebrew/bin:/usr/local/bin"
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  local candidate
  for candidate in "$nvm_dir"/versions/node/*/bin; do
    [[ -d "$candidate" ]] && extra="$extra:$candidate"
  done
  printf '%s\n' "$PATH:$extra"
}

resolve_launcher_codex_bin() {
  local codex_bin="${AGENTSTACK_CODEX_BIN:-}"
  if [[ -n "$codex_bin" && ! -x "$codex_bin" ]]; then
    codex_bin=""
  fi
  if [[ -z "$codex_bin" ]]; then
    codex_bin="$(PATH="$(codex_launcher_search_path)" command -v codex 2>/dev/null || true)"
  fi
  printf '%s\n' "$codex_bin"
}

# Same rules as scripts/install.sh "codex launcher resolution": under WSL a
# codex under /mnt/<drive>/ is the Windows npm shim and cannot run, and any
# candidate must answer `--version` within a short time.
WSL_WINDOWS_MOUNT_ROOT=/mnt
CODEX_VERSION_TIMEOUT_SECONDS=10

running_under_wsl() {
  [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null
}

codex_version_answers() {
  local bin="$1" out="${2:-/dev/null}" pid tick=0 limit=$((CODEX_VERSION_TIMEOUT_SECONDS * 10))
  local err status=0
  # Why it failed, for codex_version_failure: a probe that ran out of time and
  # one that exited at once (a wrong node on PATH dies in a few hundredths of a
  # second) need different fixes, and used to read the same.
  CODEX_VERSION_STATUS=""
  CODEX_VERSION_ERROR=""
  CODEX_VERSION_ELAPSED=""
  err="$(mktemp "${TMPDIR:-/tmp}/agentstack-codex-stderr.XXXXXX" 2>/dev/null || true)"
  "$bin" --version </dev/null >"$out" 2>"${err:-/dev/null}" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$tick" -ge "$limit" ]]; then
      codex_probe_stop "$pid"
      CODEX_VERSION_STATUS=timeout
      [[ -n "$err" ]] && rm -f "$err"
      return 1
    fi
    sleep 0.1
    tick=$((tick + 1))
  done
  wait "$pid" || status=$?
  CODEX_VERSION_STATUS="$status"
  CODEX_VERSION_ELAPSED="$((tick / 10)).$((tick % 10))"
  if [[ -n "$err" ]]; then
    # First non-blank line, bounded: it is shown inside a one-line warning.
    CODEX_VERSION_ERROR="$(sed -n '/[^[:space:]]/{p;q;}' "$err" 2>/dev/null | cut -c1-200 || true)"
    rm -f "$err"
  fi
  return "$status"
}

# One line saying why the last codex_version_answers failed.
codex_version_failure() {
  local bin="$1"
  if [[ "$CODEX_VERSION_STATUS" == timeout ]]; then
    echo "'$bin --version' did not finish within ${CODEX_VERSION_TIMEOUT_SECONDS}s and was stopped"
  else
    echo "'$bin --version' exited with status ${CODEX_VERSION_STATUS:-unknown} after ${CODEX_VERSION_ELAPSED:-0.0}s: ${CODEX_VERSION_ERROR:-(no error output)}"
  fi
}

# Stop a probe that overran. Every wait here is a kill -0 poll with a
# deadline (one second of grace, one second for the probe to go after KILL),
# so waiting on `codex --version` and on the cleanup after it is bounded. The
# system commands used on the way (pgrep, ps) are not bounded: if they
# themselves stop responding, so does this function.
#
# The probe itself ($!) is this shell's own child, so it gets TERM and then
# KILL without further checks. Its descendants (an npm wrapper's native codex)
# are recorded first, while they are still attached to it, because a wrapper
# that exits on TERM orphans them out of reach of `pgrep -P`. They get TERM
# too; a descendant seen gone during the grace period is dropped, and KILL goes
# only to one whose `ps -o lstart=` start time (one-second resolution) still
# matches what was recorded. This is a best-effort identity check: a PID reused
# within the same second is not told apart. A still-running descendant whose
# start time cannot be read or no longer matches is left without KILL, with a
# note on stderr; one that received KILL is not checked again. A probe still present after its second of reaping is left with a note.
codex_probe_stop() {
  local pid="$1" level="$1" descendants="" next p t start depth round
  # PID lists are space-separated; a caller may have narrowed IFS (the PATH
  # scan in find_usable_codex_bin splits on ":").
  local IFS=$' \t\n'
  if command -v pgrep >/dev/null 2>&1; then
    for depth in 1 2 3 4; do
      next=""
      for p in $level; do
        next="$next $(pgrep -P "$p" 2>/dev/null | tr '\n' ' ' || true)"
      done
      next="$(echo $next)"
      [[ -n "$next" ]] || break
      for p in $next; do
        descendants="$descendants $p/$(codex_process_start "$p")"
      done
      level="$next"
    done
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for t in $descendants; do
    kill -TERM "${t%%/*}" 2>/dev/null || true
  done
  # One second of grace for everything to exit on TERM.
  for round in 1 2 3 4 5 6 7 8 9 10; do
    next=""
    for t in $descendants; do
      kill -0 "${t%%/*}" 2>/dev/null && next="$next $t"
    done
    descendants="$(echo $next)"
    if [[ -z "$descendants" ]] && ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  kill -KILL "$pid" 2>/dev/null || true
  for t in $descendants; do
    p="${t%%/*}"
    start="${t#*/}"
    if [[ -n "$start" && "$(codex_process_start "$p")" == "$start" ]]; then
      kill -KILL "$p" 2>/dev/null || true
    elif kill -0 "$p" 2>/dev/null; then
      echo "note: left process $p from the codex --version probe (its identity could not be confirmed)" >&2
    fi
  done
  # Reap the probe only once it is gone; never block on it.
  for round in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "note: the codex --version probe (pid $pid) did not exit after KILL; leaving it" >&2
  else
    wait "$pid" 2>/dev/null || true
  fi
}

# The process start time as one word (empty when ps cannot tell), used to
# recognise the same process again under the same PID.
codex_process_start() {
  ps -o lstart= -p "$1" 2>/dev/null | tr -d ' \n' || true
}

# Prints why the launcher's codex cannot run; nothing when it can.
codex_launcher_problem() {
  local bin="$1"
  if running_under_wsl && [[ "$bin" == "$WSL_WINDOWS_MOUNT_ROOT"/?/* ]]; then
    echo "it is the Windows install under $WSL_WINDOWS_MOUNT_ROOT, which cannot run inside WSL"
    return 0
  fi
  if ! codex_version_answers "$bin" "${CODEX_VERSION_OUTPUT:-/dev/null}"; then
    codex_version_failure "$bin"
  fi
}

# GPT-6.1 Sol requires Codex CLI 0.159.0 or later.
CODEX_CLI_MIN_MINOR=159
report_old_codex_cli() {
  local text="$1" version major minor
  version="$(printf '%s' "$text" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
  [[ -n "$version" ]] || return 0
  major="${version%%.*}"
  minor="${version#*.}"
  minor="${minor%%.*}"
  if [[ "$major" -eq 0 && "$minor" -lt "$CODEX_CLI_MIN_MINOR" ]]; then
    echo "note: Codex CLI $version is older than 0.$CODEX_CLI_MIN_MINOR.0; GPT-6.1 Sol requires Codex CLI 0.159.0 or later. Update: npm install -g @openai/codex@latest"
  fi
}

report_codex_history_binding_prereqs() {
  local codex_bin="$1" codex_home="$2"
  local plugin_result plugin_state plugin_id

  if [[ -z "$codex_bin" || ! -x "$codex_bin" ]]; then
    echo "note: Codex launcher binary not found; Codex history binding was not checked (Claude-only use is unaffected)."
    echo "note: Codex launcher CODEX_HOME would be $codex_home"
    return 0
  fi

  local problem version_file
  version_file="$(mktemp "${TMPDIR:-/tmp}/agentstack-codex-version.XXXXXX" 2>/dev/null || true)"
  problem="$(CODEX_VERSION_OUTPUT="${version_file:-/dev/null}" codex_launcher_problem "$codex_bin")"
  if [[ -n "$problem" ]]; then
    # Codex children spawned with this binary end before their prompt arrives.
    echo "warn: Codex launcher binary $codex_bin cannot start Codex: $problem"
    echo "hint: install Codex where this shell can run it (in WSL: npm install -g @openai/codex under your Linux user), then re-run ./scripts/install.sh, or pass --codex-bin /path/to/codex"
    [[ -n "$version_file" ]] && rm -f "$version_file"
    return 0
  fi
  echo "ok: Codex launcher binary $codex_bin"
  if [[ -n "$version_file" ]]; then
    report_old_codex_cli "$(cat "$version_file" 2>/dev/null || true)"
    rm -f "$version_file"
  fi
  echo "ok: Codex launcher CODEX_HOME $codex_home (source for per-child homes)"
  if [[ ! -d "$codex_home" ]]; then
    echo "note: Codex history binding plugin status unknown because CODEX_HOME does not exist: $codex_home (Claude-only use is unaffected)."
    return 0
  fi

  # Query only the public registry. A failed/changed response is unknown, not
  # evidence that the plugin is absent. Bound the CLI so doctor cannot hang.
  plugin_result="$("$PYTHON_BIN" - "$codex_bin" "$codex_home" <<'PYPLUGIN' 2>/dev/null || true
import json
import os
import subprocess
import sys

try:
    result = subprocess.run(
        [sys.argv[1], "plugin", "list", "--json"],
        env=dict(os.environ, CODEX_HOME=sys.argv[2]),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
        timeout=5,
    )
    data = json.loads(result.stdout)
    installed = data["installed"]
    if not isinstance(installed, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("pluginId"), str)
        for item in installed
    ):
        raise ValueError("unrecognised registry shape")
    items = [
        item for item in installed
        if isinstance(item.get("pluginId"), str)
        and item["pluginId"].startswith("agentstack-codex-app@")
    ]
    if not items:
        print("absent")
    else:
        item = next((item for item in items if item.get("enabled") is True), None)
        if item is not None:
            print("enabled", item["pluginId"], sep="\t")
        elif all(item.get("enabled") is False for item in items):
            print("disabled", items[0]["pluginId"], sep="\t")
        else:
            print("unknown")
except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
    print("unknown")
PYPLUGIN
)"
  plugin_state="${plugin_result%%$'\t'*}"
  plugin_id="${plugin_result#*$'\t'}"

  case "$plugin_state" in
    enabled)
      echo "ok: Codex history binding plugin installed and enabled ($plugin_id)"
      echo "note: Codex lifecycle hook approval is unknown; open /hooks in Codex to review/approve the AgentStack hooks, then start a new Codex process before expecting BOUND history."
      ;;
    disabled)
      echo "note: Codex history binding plugin installed but disabled ($plugin_id); Codex CLI history requires enabling it and reviewing its hooks in /hooks before a new process. Claude-only use is unaffected."
      ;;
    absent)
      echo "note: Codex history binding plugin is not installed in $codex_home; Codex CLI history requires this optional plugin; see docs/codex-app.md for setup and /hooks approval. Claude-only use is unaffected."
      ;;
    *)
      echo "note: Codex history binding plugin status is unknown for $codex_bin with CODEX_HOME=$codex_home; inspect 'codex plugin list --json' and /hooks if you use Codex history."
      ;;
  esac
}

report_codex_model_default() {
  local helper="$1" codex_home="$2"
  if [[ -f "$helper" ]]; then
    AGENTSTACK_CODEX_BIN="${CODEX_LAUNCHER_BIN:-}" CODEX_HOME="$codex_home" "$PYTHON_BIN" "$helper" note
  fi
}

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_LAUNCHER_BIN="$(resolve_launcher_codex_bin)"
report_codex_history_binding_prereqs "$CODEX_LAUNCHER_BIN" "$CODEX_HOME"
report_codex_model_default "$INSTALL_DIR/dashboard/codex_models.py" "$CODEX_HOME"
check_managed_block "Codex AGENTS.md" agentstack-codex-setup CODEX_HOME="$CODEX_HOME"

report_claude_model_aliases() {
  local helper="$1"
  if [[ -f "$helper" ]]; then
    "$PYTHON_BIN" "$helper" note || echo "warn: could not resolve the Claude model aliases with $helper" >&2
  fi
}

report_claude_model_aliases "$INSTALL_DIR/dashboard/claude_models.py"

PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-}"
WORKTREE_ROOT="${AGENTSTACK_WORKTREE_ROOT:-$INSTALL_DIR/worktrees}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
CLAUDE_SCOPE="${AGENTSTACK_CLAUDE_MD_SCOPE:-project}"
check_managed_block "Claude CLAUDE.md" agentstack-claude-setup \
  AGENTSTACK_CLAUDE_MD_SCOPE="$CLAUDE_SCOPE" CLAUDE_HOME="$CLAUDE_HOME" \
  AGENTSTACK_PROJECT_KEY="$PROJECT_KEY"

report_orphan_worktrees() {
  local root="$WORKTREE_ROOT" registered_names worktree name orphan_count=0
  if [[ "$root" == "~" ]]; then
    root="$HOME"
  elif [[ "$root" == "~/"* ]]; then
    root="$HOME/${root#\~/}"
  fi
  if [[ ! -d "$root" ]]; then
    echo "ok: worktree root has no entries ($root)"
    return 0
  fi
  if [[ ! -x "$PYTHON_BIN" || ! -f "$MAIL_DB_PATH" || -z "$PROJECT_KEY" ]] || \
     ! command -v tmux >/dev/null 2>&1
  then
    echo "warn: cannot classify worktrees under $root; Python, tmux, Mail DB, and project key are required" >&2
    return 0
  fi
  if ! registered_names="$("$PYTHON_BIN" - "$MAIL_DB_PATH" "$PROJECT_KEY" <<'PY' 2>/dev/null
import pathlib
import sqlite3
import sys

database, project_key = sys.argv[1:]
uri = pathlib.Path(database).resolve().as_uri() + "?mode=ro"
with sqlite3.connect(uri, uri=True) as connection:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(agents)")
    }
    retired_filter = " AND a.retired_at IS NULL" if "retired_at" in columns else ""
    rows = connection.execute(
        "SELECT a.name FROM agents AS a "
        "JOIN projects AS p ON p.id = a.project_id "
        "WHERE p.human_key = ?" + retired_filter,
        (project_key,),
    )
    for (name,) in rows:
        if isinstance(name, str):
            print(name)
PY
)"; then
    echo "warn: cannot read registered agents; worktrees under $root were not classified" >&2
    return 0
  fi

  for worktree in "$root"/*; do
    [[ -d "$worktree" ]] || continue
    name="${worktree##*/}"
    if tmux has-session -t "=$name" >/dev/null 2>&1 || \
       printf '%s\n' "$registered_names" | grep -Fqx -- "$name"
    then
      continue
    fi
    echo "warn: orphaned worktree is neither live nor registered: $worktree" >&2
    echo "      inspect it, then remove it manually from its source repository; doctor will not delete it" >&2
    orphan_count=$((orphan_count + 1))
  done
  if [[ "$orphan_count" -eq 0 ]]; then
    echo "ok: no orphaned worktrees under $root"
  fi
}

report_orphan_worktrees

SCIENTISTS_LIB="$INSTALL_DIR/bin/lib/agentstack-scientists.sh"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$INSTALL_DIR/runtime}"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
CHILD_STATE_DIR="$RUNTIME_DIR/child-agents"

report_child_resume_retention() {
  local expired
  expired="$("$PYTHON_BIN" - "$CHILD_STATE_DIR" <<'PY' 2>/dev/null || true
from datetime import datetime, timezone
import json
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
now = datetime.now(timezone.utc)
for path in root.glob("*.json") if root.is_dir() else ():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("resume_expires_at")
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError):
        continue
    name = data.get("agent_name")
    if (
        data.get("schema_version") == 1
        and data.get("launch_origin") == "child"
        and isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+", name)
        and expires.tzinfo is not None
        and now >= expires.astimezone(timezone.utc)
    ):
        print(name)
PY
)"
  if [[ -n "$expired" ]]; then
    echo "warn: expired child resume material awaits maintenance purge: $(printf '%s' "$expired" | paste -sd, -)" >&2
    echo "      doctor reports only; run agentstack-purge-child-resume --expired or keep the dashboard running" >&2
  else
    echo "ok: no expired child resume material awaiting purge"
  fi
}

report_child_resume_retention

collect_managed_agent_names() {
  if [[ -f "$MANAGED_AGENTS_FILE" ]]; then
    sed '/^[[:space:]]*$/d' "$MANAGED_AGENTS_FILE"
  fi
  if [[ -d "$CHILD_STATE_DIR" ]]; then
    local state
    for state in "$CHILD_STATE_DIR"/*.json; do
      [[ -e "$state" ]] || continue
      python3 - "$state" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
name = data.get("agent_name")
if isinstance(name, str) and name:
    print(name)
PY
    done
  fi
}

if [[ -f "$SCIENTISTS_LIB" ]]; then
  # shellcheck disable=SC1090
  . "$SCIENTISTS_LIB"
  MANAGED_AGENT_NAMES="$(collect_managed_agent_names | sort -u || true)"
  if [[ -z "$MANAGED_AGENT_NAMES" ]]; then
    echo "warn: no managed agent records found for scientist suffix check"
  else
    BAD_MANAGED_NAMES=()
    while IFS= read -r agent_name; do
      [[ -n "$agent_name" ]] || continue
      if ! ags_has_scientist_suffix "$agent_name"; then
        BAD_MANAGED_NAMES+=("$agent_name")
      fi
    done <<< "$MANAGED_AGENT_NAMES"
    if [[ ${#BAD_MANAGED_NAMES[@]} -gt 0 ]]; then
      echo "warn: managed agent names without scientist suffix: ${BAD_MANAGED_NAMES[*]:0:10}"
    else
      echo "ok: managed agent names end with bundled scientist keys"
    fi
  fi
else
  echo "warn: cannot check scientist suffixes; missing $SCIENTISTS_LIB"
fi

# Non-fatal hint: agents run inside tmux, so wheel-scroll only reaches an
# agent's scrollback when tmux mouse mode is on. Check the live server first,
# then fall back to ~/.tmux.conf.
mouse_on=""
if tmux info >/dev/null 2>&1; then
  mouse_on="$(tmux show -gv mouse 2>/dev/null || true)"
elif [[ -f "$HOME/.tmux.conf" ]] && \
  grep -Eq '^[[:space:]]*set(-option)?[[:space:]]+-g[[:space:]]+mouse[[:space:]]+on' "$HOME/.tmux.conf"; then
  mouse_on="on"
fi
if [[ "$mouse_on" != "on" ]]; then
  echo "hint: tmux mouse mode is off — wheel-scroll won't reach agent scrollback."
  echo "      add 'set -g mouse on' to ~/.tmux.conf (see README Troubleshooting)."
fi

# A live tmux server created by an older launcher may retain another session's
# identity in its global environment. Report variable names only; never print an
# owner token value.
if tmux info >/dev/null 2>&1; then
  STALE_IDENTITY_VARS=()
  for identity_var in AGENT_NAME PARENT_AGENT CHILD_REGISTRATION_TOKEN AGENTSTACK_RESERVED_IDENTITY; do
    if tmux show-environment -g "$identity_var" 2>/dev/null | grep -q "^${identity_var}="; then
      STALE_IDENTITY_VARS+=("$identity_var")
    fi
  done
  if [[ ${#STALE_IDENTITY_VARS[@]} -gt 0 ]]; then
    echo "warn: tmux global environment contains session identity variables: ${STALE_IDENTITY_VARS[*]}"
    echo "      restart the tmux server or remove those global variables before creating new sessions."
  else
    echo "ok: tmux global environment has no session identity variables"
  fi
fi

# --- paste-ready environment report -------------------------------------------
# Every defect this project has had so far came from a difference between the
# reporter's machine and the developer's, and each one cost several rounds of
# "which version of that do you have?". These are exactly the fields those
# rounds asked for. Values only: no tokens, no Authorization headers, no
# database contents.
report_tool() {
  ags_report_path="$(command -v "$1" 2>/dev/null || true)"
  if [[ -z "$ags_report_path" ]]; then
    printf -- '- %s: not found\n' "$1"
    return 0
  fi
  ags_report_version="$("$ags_report_path" ${2:---version} 2>&1 | head -n 1 | tr -d '\r')"
  printf -- '- %s: %s (%s)\n' "$1" "${ags_report_version:-unknown}" "$ags_report_path"
}

if [[ "$REPORT" == "1" ]]; then
  echo
  echo "--- copy from here ---"
  echo
  echo '## Environment'
  echo
  printf -- '- stack: %s\n' "$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo 'VERSION not installed')"
  if git -C "${AGENTSTACK_REPO:-$INSTALL_DIR}" rev-parse --short HEAD >/dev/null 2>&1; then
    printf -- '- stack commit: %s\n' "$(git -C "${AGENTSTACK_REPO:-$INSTALL_DIR}" rev-parse --short HEAD)"
  fi
  printf -- '- host: %s %s (%s)\n' "$(uname -s)" "$(uname -r)" "$(uname -m)"
  if [[ "$(uname -s)" == "Darwin" ]] && command -v sw_vers >/dev/null 2>&1; then
    printf -- '- macOS: %s\n' "$(sw_vers -productVersion 2>/dev/null || echo unknown)"
  fi
  # launchd and a login shell disagree about this by four thousand, and that
  # gap is what exhausted a tester's descriptors.
  printf -- '- open file limit (this shell): %s\n' "$(ulimit -n 2>/dev/null || echo unknown)"
  echo
  echo '## Tools'
  echo
  report_tool python3
  report_tool tmux -V
  report_tool git
  report_tool uv
  report_tool claude
  report_tool codex
  echo
  echo '## ORRERY Mail'
  echo
  ags_mail_dir="${AGENTSTACK_MAIL_DIR:-$HOME/.agentstack/mail-service}"
  printf -- '- directory: %s\n' "$ags_mail_dir"
  if git -C "$ags_mail_dir" rev-parse --short HEAD >/dev/null 2>&1; then
    printf -- '- commit: %s\n' "$(git -C "$ags_mail_dir" rev-parse --short HEAD)"
    printf -- '- ahead of origin: %s commit(s)\n' \
      "$(git -C "$ags_mail_dir" rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo 'unknown')"
  else
    echo '- commit: not a git checkout'
  fi
  printf -- '- declared version: %s\n' \
    "$(grep -m1 '^version' "$ags_mail_dir/pyproject.toml" 2>/dev/null | tr -d ' "' || echo unknown)"
  echo '- requested-name handling: honored (passthrough)'
  printf -- '- endpoint: %s\n' "${AGENTSTACK_MCP_URL:-unset}"
  ags_mail_db="${AGENTSTACK_MAIL_DB:-$ags_mail_dir/storage.sqlite3}"
  if [[ -f "$ags_mail_db" ]] && [[ -x "$PYTHON_BIN" ]]; then
    printf -- '- agents.retired_at column: %s\n' \
      "$("$PYTHON_BIN" - "$ags_mail_db" <<'PYEOF' 2>/dev/null || echo unknown
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("present" if any(r[1] == "retired_at"
      for r in con.execute("PRAGMA table_info(agents)")) else "absent")
con.close()
PYEOF
)"
  else
    echo '- agents.retired_at column: database not readable'
  fi
  echo
  echo '## What happened'
  echo
  echo '<!-- What you did, what you expected, what you saw. Paste any error text. -->'
  echo
  echo "--- copy to here ---"
fi

exit "$status"
