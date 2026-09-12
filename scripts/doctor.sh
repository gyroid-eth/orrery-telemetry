#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MANIFEST="$INSTALL_DIR/install-state.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY_PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
ENTRY_PROJECT_CONTEXT="${AGENTSTACK_PROJECT_CONTEXT:-}"
ENTRY_PROJECT_REPOSITORY="${AGENTSTACK_PROJECT_REPOSITORY:-}"
ENTRY_PROJECT_WORK_DIR="${AGENTSTACK_PROJECT_WORK_DIR:-}"
ENTRY_PROTECTED_ROOTS="${AGENTSTACK_PROTECTED_ROOTS:-}"

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

if [[ -n "$ENTRY_PROJECT_KEY" ]]; then
  AGENTSTACK_PROJECT_KEY="$ENTRY_PROJECT_KEY"
  PROJECT_KEY="$ENTRY_PROJECT_KEY"
  export AGENTSTACK_PROJECT_KEY PROJECT_KEY
fi
if [[ "$ENTRY_PROJECT_CONTEXT" == "1" ]]; then
  AGENTSTACK_PROJECT_CONTEXT=1
  AGENTSTACK_PROJECT_REPOSITORY="$ENTRY_PROJECT_REPOSITORY"
  AGENTSTACK_PROJECT_WORK_DIR="$ENTRY_PROJECT_WORK_DIR"
  AGENTSTACK_PROTECTED_ROOTS="$ENTRY_PROTECTED_ROOTS"
  export AGENTSTACK_PROJECT_CONTEXT AGENTSTACK_PROJECT_REPOSITORY
  export AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROTECTED_ROOTS
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
        and payload.get("api") == 1
    )
except (OSError, ValueError, TypeError):
    healthy = False
raise SystemExit(0 if healthy else 1)
PY
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

warn_managed_block() {
  local label="$1" target="$2" marker="$3"
  if [[ -f "$target" ]] && grep -Fq "$marker" "$target"; then
    echo "ok: $label managed block in $target"
  else
    echo "warn: $label managed block not found in $target"
  fi
}

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
warn_managed_block "Codex AGENTS.md" "$CODEX_HOME/AGENTS.md" \
  "<!-- >>> claude-agent-stack (managed: agentstack-codex-setup) -->"

PROJECT_KEY="${AGENTSTACK_PROJECT_KEY:-}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
CLAUDE_SCOPE="${AGENTSTACK_CLAUDE_MD_SCOPE:-project}"
case "$CLAUDE_SCOPE" in
  project)
    if [[ -n "$PROJECT_KEY" ]]; then
      warn_managed_block "Claude CLAUDE.md" "$PROJECT_KEY/CLAUDE.md" \
        "<!-- >>> claude-agent-stack (managed: agentstack-claude-setup) -->"
    else
      echo "warn: AGENTSTACK_PROJECT_KEY unset; cannot check project CLAUDE.md"
    fi
    ;;
  global)
    warn_managed_block "Claude CLAUDE.md" "$CLAUDE_HOME/CLAUDE.md" \
      "<!-- >>> claude-agent-stack (managed: agentstack-claude-setup) -->"
    ;;
  both)
    if [[ -n "$PROJECT_KEY" ]]; then
      warn_managed_block "Claude project CLAUDE.md" "$PROJECT_KEY/CLAUDE.md" \
        "<!-- >>> claude-agent-stack (managed: agentstack-claude-setup) -->"
    else
      echo "warn: AGENTSTACK_PROJECT_KEY unset; cannot check project CLAUDE.md"
    fi
    warn_managed_block "Claude global CLAUDE.md" "$CLAUDE_HOME/CLAUDE.md" \
      "<!-- >>> claude-agent-stack (managed: agentstack-claude-setup) -->"
    ;;
  *)
    echo "warn: invalid AGENTSTACK_CLAUDE_MD_SCOPE=$CLAUDE_SCOPE; cannot check CLAUDE.md"
    ;;
esac

SCIENTISTS_LIB="$INSTALL_DIR/bin/lib/agentstack-scientists.sh"
RUNTIME_DIR="${AGENTSTACK_RUNTIME_DIR:-$INSTALL_DIR/runtime}"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$RUNTIME_DIR/managed_agents.txt}"
CHILD_STATE_DIR="$RUNTIME_DIR/child-agents"

PROJECT_AUDIT_STATUS=0
PROJECT_AUDIT="$(${PYTHON_BIN:-python3} - \
  "${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}" \
  "${AGENTSTACK_PROJECT_REPOSITORY:-}" \
  "${AGENTSTACK_PROJECT_WORK_DIR:-$PWD}" \
  "${AGENTSTACK_PROTECTED_ROOTS:-}" "$RUNTIME_DIR" \
  "$ENTRY_PROJECT_CONTEXT" "$ENTRY_PROJECT_WORK_DIR" <<'PY' 2>/dev/null
import json
import os
import pathlib
import subprocess
import sys

project_key, repository_hint, work_dir, roots_raw, runtime_raw = sys.argv[1:6]
established_context, established_work_dir = sys.argv[6:8]
runtime = pathlib.Path(runtime_raw)
warnings = []


def real(value):
    try:
        return os.path.realpath(os.path.expanduser(str(value)))
    except (OSError, TypeError, ValueError):
        return ""


def git(args, cwd):
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], env=env, text=True,
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_context(value):
    path = real(value)
    if not path or not os.path.isdir(path):
        return None
    top = git(["rev-parse", "--show-toplevel"], path)
    common = git(["rev-parse", "--git-common-dir"], path)
    if not top or not common:
        return None
    if not os.path.isabs(common):
        # rev-parse reports this relative to the directory queried with -C,
        # including when that directory is below the worktree root.
        common = os.path.join(path, common)
    common = real(common)
    repository = real(os.path.dirname(common) if os.path.basename(common) == ".git" else common)
    return {"worktree": real(top), "repository": repository}


pwd_context = git_context(os.getcwd())
work_context = git_context(work_dir)
repository_context = git_context(repository_hint)
project_context = git_context(project_key)
current = pwd_context or work_context or repository_context or project_context
current_repo = current["repository"] if current else ""
if current_repo:
    for label, context, value in (
        ("configured project_key", project_context, project_key),
        ("bound repository", repository_context, repository_hint),
        ("bound work_dir", work_context, work_dir),
    ):
        if context and context["repository"] != current_repo:
            warnings.append(
                f"warn: {label} repository mismatch with current cwd: {real(value)}"
            )

# A non-Git cwd has no repository to compare, and `current` then falls back to
# the bound work_dir. For an established (inherited) context, the cwd must lie
# inside that physical work_dir, as the hooks require.
if established_context == "1" and established_work_dir and pwd_context is None:
    cwd_path = real(os.getcwd())
    bound_path = real(established_work_dir)
    if cwd_path and bound_path and os.path.isdir(bound_path):
        inside = cwd_path == bound_path or cwd_path.startswith(
            bound_path.rstrip(os.sep) + os.sep
        )
        if not inside:
            warnings.append(
                "warn: established project context work_dir does not contain "
                f"current non-Git cwd: cwd={cwd_path} work_dir={bound_path}"
            )


def record_cwd(data):
    for key in ("work_dir", "cwd", "workspace"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    transcript = data.get("transcript_path")
    if isinstance(transcript, str) and os.path.isfile(transcript):
        try:
            with open(transcript, encoding="utf-8", errors="ignore") as handle:
                for _ in range(60):
                    line = handle.readline()
                    if not line:
                        break
                    item = json.loads(line)
                    value = item.get("cwd")
                    if not isinstance(value, str) and isinstance(item.get("payload"), dict):
                        value = item["payload"].get("cwd")
                    if isinstance(value, str) and value:
                        return value
        except (OSError, ValueError):
            pass
    return ""


def audit_record(label, data):
    if not isinstance(data, dict):
        return
    record_project = data.get("project_key")
    record_repository = data.get("repository")
    cwd = record_cwd(data)
    project_binding = (
        git_context(record_project)
        if isinstance(record_project, str) and record_project
        else None
    )
    repository_binding = (
        git_context(record_repository)
        if isinstance(record_repository, str) and record_repository
        else None
    )
    record_context = git_context(cwd) if cwd else None
    if project_binding and repository_binding:
        if project_binding["repository"] != repository_binding["repository"]:
            warnings.append(
                f"warn: {label} project_key/repository binding mismatch: "
                f"project_key={record_project} repository={real(record_repository)}"
            )
    expected_context = repository_binding or project_binding
    if record_context and expected_context:
        if record_context["repository"] != expected_context["repository"]:
            warnings.append(
                f"warn: {label} work_dir/cwd repository mismatch: "
                f"{real(cwd)} expected={expected_context['repository']}"
            )


for directory, prefix in (
    (runtime / "child-agents", "child agent"),
    (runtime / "session_index", "session index"),
):
    if not directory.is_dir():
        continue
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("agent_name") if isinstance(data, dict) else ""
        audit_record(f"{prefix} {name or path.stem}", data)

snapshot = runtime / "codex-app" / "snapshot.json"
if snapshot.is_file():
    try:
        snapshot_data = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snapshot_data = None
    if isinstance(snapshot_data, dict):
        records = snapshot_data.get("runtimes", snapshot_data)
        if isinstance(records, dict):
            for name, data in records.items():
                audit_record(f"Codex App snapshot {name}", data)
        elif isinstance(records, list):
            for index, data in enumerate(records):
                name = data.get("agent_name", index) if isinstance(data, dict) else index
                audit_record(f"Codex App snapshot {name}", data)

if current and roots_raw:
    roots = [real(value) for value in roots_raw.split(":") if value]
    expected = {current["worktree"], current["repository"]}
    for missing in sorted(expected.difference(roots)):
        warnings.append(f"warn: protected roots missing current project root: {missing}")
    for root in roots:
        root_context = git_context(root)
        if root_context and root_context["repository"] != current_repo:
            warnings.append(f"warn: protected root belongs to another repository: {root}")

for warning in dict.fromkeys(warnings):
    print(warning)
raise SystemExit(1 if warnings else 0)
PY
)" || PROJECT_AUDIT_STATUS=$?
if [[ -n "$PROJECT_AUDIT" ]]; then
  printf '%s\n' "$PROJECT_AUDIT"
fi
if [[ "$PROJECT_AUDIT_STATUS" != "0" ]]; then
  status=1
elif [[ -n "${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}" ]]; then
  echo "ok: project context metadata matches the current repository"
fi

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
  for identity_var in AGENT_NAME PARENT_AGENT CHILD_REGISTRATION_TOKEN AGENTSTACK_RESERVED_IDENTITY \
    AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_CONTEXT \
    AGENTSTACK_PROJECT_REPOSITORY AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROTECTED_ROOTS; do
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
  if env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
      git -C "${AGENTSTACK_REPO:-$INSTALL_DIR}" rev-parse --short HEAD >/dev/null 2>&1; then
    printf -- '- stack commit: %s\n' "$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
      git -C "${AGENTSTACK_REPO:-$INSTALL_DIR}" rev-parse --short HEAD)"
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
  if env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
      git -C "$ags_mail_dir" rev-parse --short HEAD >/dev/null 2>&1; then
    printf -- '- commit: %s\n' "$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
      git -C "$ags_mail_dir" rev-parse --short HEAD)"
    printf -- '- ahead of origin: %s commit(s)\n' \
      "$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$ags_mail_dir" rev-list --count '@{upstream}..HEAD' 2>/dev/null || echo 'unknown')"
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
