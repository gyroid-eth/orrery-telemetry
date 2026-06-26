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

SCIENTISTS_JSON="$INSTALL_DIR/dashboard/scientist_portraits.json"
MANAGED_AGENTS_FILE="${AGENTSTACK_MANAGED_AGENTS_FILE:-$INSTALL_DIR/runtime/managed_agents.txt}"
if [[ -f "$SCIENTISTS_JSON" && -f "$MANAGED_AGENTS_FILE" ]]; then
  python3 - "$SCIENTISTS_JSON" "$MANAGED_AGENTS_FILE" <<'PY'
import json
import pathlib
import sys

scientists = [
    name for name in json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    if name.isascii() and name.isalpha()
]
names = [
    line.strip()
    for line in pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
bad = [name for name in names if not any(name.endswith(scientist) for scientist in scientists)]
if bad:
    print("warn: managed agent names without scientist suffix: " + ", ".join(bad[:10]))
else:
    print("ok: managed agent names end with bundled scientist keys")
PY
else
  echo "warn: cannot check scientist suffixes; missing $SCIENTISTS_JSON or $MANAGED_AGENTS_FILE"
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
