#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MANIFEST="$INSTALL_DIR/install-state.json"

usage() {
  cat <<'EOF'
Usage: doctor.sh [--install-dir PATH] [--report]

Checks the core claude-agent-stack install footprint without modifying files.

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
   "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
then
  echo "ok: Python 3.10+ ($PYTHON_BIN)"
else
  echo "missing: Python 3.10+ interpreter" >&2
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
  echo "ok: agent-mail database $MAIL_DB_PATH"
else
  echo "missing: AGENTSTACK_MAIL_DB does not point to an existing file: ${MAIL_DB_PATH:-<unset>}" >&2
  status=1
fi

CLAUDE_JSON="${AGENTSTACK_CLAUDE_JSON:-$HOME/.claude.json}"
MCP_URL="${AGENTSTACK_MCP_URL:-http://127.0.0.1:8765/mcp}"
MAIL_ENV="${AGENTSTACK_MAIL_ENV:-}"
CLAUDE_MCP_STATE="$("$PYTHON_BIN" - "$CLAUDE_JSON" "$MCP_URL" "$MAIL_ENV" <<'PY' 2>/dev/null || true
import json
import pathlib
import sys

try:
    config = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    entry = config.get("mcpServers", {}).get("mcp-agent-mail")
except (AttributeError, OSError, ValueError):
    print("invalid")
else:
    bearer = ""
    try:
        for line in pathlib.Path(sys.argv[3]).read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "HTTP_BEARER_TOKEN":
                bearer = value.strip().strip("'\"")
                break
    except OSError:
        pass
    authorization = (
        (entry.get("headers") or {}).get("Authorization")
        if isinstance(entry, dict)
        else None
    )
    if (
        isinstance(entry, dict)
        and entry.get("type") == "http"
        and entry.get("url") == sys.argv[2]
        and (not bearer or authorization == f"Bearer {bearer}")
    ):
        print("configured")
    else:
        print("missing")
PY
)"
if [[ "$CLAUDE_MCP_STATE" == "configured" ]]; then
  echo "ok: Claude MCP mcp-agent-mail registered in $CLAUDE_JSON"
else
  echo "warn: Claude MCP mcp-agent-mail is not registered for $MCP_URL in $CLAUDE_JSON" >&2
  echo "      /delegate cannot use agent-mail until this fixed-name entry exists." >&2
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

report_dashboard_service() {
  local python_bin="${AGENTSTACK_PYTHON:-python3}"
  local record kind identity service_path pid launchd_record
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
        echo "ok: dashboard service mode systemd-user ($identity)"
      else
        echo "warn: dashboard service mode systemd-user, but $identity is not active"
        status=1
      fi
      ;;
    nohup)
      pid="$(sed -n '1p' "$service_path" 2>/dev/null || true)"
      if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
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
}

if [[ -f "$MANIFEST" ]]; then
  report_dashboard_service
fi

# Without the proxy a spawned child still works, but its agent-mail connection
# is not authenticated as itself: the child has to read its own token instead.
# That degradation is silent at spawn time, so surface it here.
CHILD_MCP_PROXY="$INSTALL_DIR/integrations/codex_app/plugin/scripts/run-mcp.sh"
if [[ -x "$CHILD_MCP_PROXY" ]]; then
  if [[ -d "$INSTALL_DIR/integrations/codex_app/src/agentstack_codex_app" ]]; then
    echo "ok: child MCP proxy installed"
  else
    echo "warn: child MCP proxy runner present but its source tree is missing;" \
         "spawned children will fall back to the shared agent-mail endpoint"
  fi
else
  echo "warn: child MCP proxy missing ($CHILD_MCP_PROXY);" \
       "spawned children fall back to the shared agent-mail endpoint and must" \
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
  echo '## agent-mail'
  echo
  ags_mail_dir="${AGENTSTACK_MAIL_DIR:-$HOME/mcp_agent_mail}"
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
  # The mode decides whether the name you asked for is the name you get.
  printf -- '- AGENT_NAME_ENFORCEMENT_MODE: %s\n' \
    "$(grep -m1 '^AGENT_NAME_ENFORCEMENT_MODE=' "${AGENTSTACK_MAIL_ENV:-$ags_mail_dir/.env}" 2>/dev/null \
       | cut -d= -f2- || echo 'unset (default)')"
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
