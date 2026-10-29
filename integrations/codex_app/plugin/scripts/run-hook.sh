#!/usr/bin/env bash
set -euo pipefail

# CLI-only fresh handoff, before the Desktop installation can replace caller
# context. SessionStart is recorded locally; no Desktop binding is created.
if [[ "${AGENTSTACK_FRESH_CLIENT_CONTEXT+x}" == x || "${AGENTSTACK_RUNTIME_MODE:-legacy}" == fresh ]]; then
  if [[ -z "${AGENTSTACK_FRESH_CLIENT_CONTEXT:-}" || -n "${AGENTSTACK_CLIENT_CONFIG:-}" || -n "${AGENTSTACK_GLOBAL_SERVER_CONFIG:-}" ]]; then
    printf '%s\n' FRESH_CONTEXT_CONFLICT >&2
    exit 2
  fi
  case "${AGENTSTACK_RUNTIME_MODE:-legacy}" in
    legacy|fresh) ;;
    *) printf '%s\n' FRESH_CONTEXT_CONFLICT >&2; exit 2 ;;
  esac
  if [[ -z "${AGENTSTACK_FRESH_LAUNCH_ID:-}" && "${AGENTSTACK_CODEX_APP_RUNTIME_DIR+x}" == x ]]; then
    _ags_app_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    _ags_app_source="$_ags_app_root/src"
    [[ -d "$_ags_app_source/agentstack_codex_app" ]] || _ags_app_source="$(cd "$_ags_app_root/../src" && pwd)"
    exec "${AGENTSTACK_PYTHON:-python3}" "$_ags_app_source/agentstack_codex_app/hook_entry.py"
  fi
  _ags_fresh_payload="$(</dev/stdin)"
  _ags_fresh_event="$(printf '%s' "$_ags_fresh_payload" | "${AGENTSTACK_PYTHON:-python3}" -c '
import json, sys
try:
    value=json.load(sys.stdin)
    if not isinstance(value,dict):raise ValueError()
    print(value.get("hook_event_name", ""))
except (ValueError,TypeError):
    print("FRESH_SESSION_EVENT_INVALID",file=sys.stderr)
    sys.exit(2)
')" || exit 2
  # The remaining CLI telemetry events have a one-second hook budget and
  # neither read owner material nor enter the Desktop bridge.
  [[ "$_ags_fresh_event" == SessionStart ]] || exit 0
  printf '%s' "$_ags_fresh_payload" | "${AGENTSTACK_PYTHON:-python3}" -m agentstack_mail.fresh_client session-hook --context "$AGENTSTACK_FRESH_CLIENT_CONTEXT"
  exit "$?"
fi

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
