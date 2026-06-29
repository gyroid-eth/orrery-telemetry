#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${AGENTSTACK_HOME:-$HOME/.agentstack}"
MANIFEST="$INSTALL_DIR/install-state.json"

usage() {
  cat <<'EOF'
Usage: doctor.sh [--install-dir PATH]

Checks the core claude-agent-stack install footprint without modifying files.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="$2"
      MANIFEST="$INSTALL_DIR/install-state.json"
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

status=0
check_cmd() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "ok: $1"
  else
    echo "missing: $1" >&2
    status=1
  fi
}

check_cmd python3
check_cmd tmux
check_cmd git
check_cmd uv

if [[ -f "$MANIFEST" ]]; then
  echo "ok: manifest $MANIFEST"
  python3 -m json.tool "$MANIFEST" >/dev/null || status=1
else
  echo "missing: manifest $MANIFEST" >&2
  status=1
fi

if [[ -f "$INSTALL_DIR/env.sh" ]]; then
  echo "ok: env $INSTALL_DIR/env.sh"
  # shellcheck disable=SC1090
  . "$INSTALL_DIR/env.sh"
else
  echo "missing: env $INSTALL_DIR/env.sh" >&2
  status=1
fi

if [[ -x "$INSTALL_DIR/hooks/spawn_child.sh" ]]; then
  echo "ok: hooks installed"
else
  echo "missing: hooks under $INSTALL_DIR/hooks" >&2
  status=1
fi

if [[ -f "$INSTALL_DIR/dashboard/server.py" ]]; then
  echo "ok: dashboard installed"
else
  echo "missing: dashboard under $INSTALL_DIR/dashboard" >&2
  status=1
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

exit "$status"
