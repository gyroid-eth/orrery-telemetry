#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${AGENTSTACK_CODEX_APP_INSTALL_DIR:-$HOME/.agentstack/integrations/codex_app}"
ENV_FILE="$INSTALL_DIR/env.sh"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

SOURCE_ROOT="$PLUGIN_ROOT/src"
if [[ ! -d "$SOURCE_ROOT/agentstack_codex_app" ]]; then
  SOURCE_ROOT="$(cd "$PLUGIN_ROOT/../src" && pwd)"
fi
PYTHON_BIN="${AGENTSTACK_PYTHON:-python3}"
payload="$(</dev/stdin)"

# CLI history binding is deliberately separate from the Codex Desktop bridge.
# It is inert unless a product launcher installed a fresh launch binding in
# this exact process environment, and it never emits model context. Keep the
# recorder beside this trusted runner: a login shell may reset install/runtime
# environment variables before invoking a hook, while PLUGIN_ROOT remains the
# verified plugin payload Codex selected.
RECORDER="$PLUGIN_ROOT/scripts/record-codex-session-index.py"
if [[ -f "$RECORDER" ]]; then
  if ! printf '%s' "$payload" | "$PYTHON_BIN" "$RECORDER"; then
    # Keep the telemetry hook fail-open, but make an interpreter/process
    # failure visible instead of silently discarding it with `|| true`.
    echo "record-codex-session-index: recorder_process_failed" >&2
  fi
fi

printf '%s' "$payload" | exec "$PYTHON_BIN" "$SOURCE_ROOT/agentstack_codex_app/hook_entry.py"
