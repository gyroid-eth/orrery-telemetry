#!/bin/bash
# Sourced only by CLI fresh runners. No credentials or shell environment snapshot.
_ags_restore_fresh_cli_env() {
  local value
  case "${AGENTSTACK_FRESH_PYTHON:-}" in
    /*) ;;
    *) printf '%s\n' FRESH_CODEX_PYTHON_UNAVAILABLE >&2; return 2 ;;
  esac
  if [[ ! -f "$AGENTSTACK_FRESH_PYTHON" || ! -x "$AGENTSTACK_FRESH_PYTHON" ]]; then
    printf '%s\n' FRESH_CODEX_PYTHON_UNAVAILABLE >&2; return 2
  fi
  if [[ -n "${AGENTSTACK_CLIENT_CONFIG:-}" || -n "${AGENTSTACK_GLOBAL_SERVER_CONFIG:-}" ]]; then
    printf '%s\n' FRESH_CONTEXT_CONFLICT >&2; return 2
  fi
  if [[ -z "${AGENTSTACK_FRESH_LAUNCH_ID:-}" || -z "${AGENTSTACK_FRESH_LAUNCH_NONCE:-}" ]]; then
    printf '%s\n' FRESH_CODEX_ENV_UNAVAILABLE >&2; return 2
  fi
  for value in "${AGENTSTACK_FRESH_CLIENT_CONTEXT:-}" "${AGENTSTACK_FRESH_WRAPPER_ROOT:-}" \
      "${AGENTSTACK_FRESH_RUNTIME_DIR:-}" "${AGENTSTACK_FRESH_CODEX_HOME:-}" "${AGENTSTACK_FRESH_CODEX_BIN:-}"; do
    case "$value" in
      /*) ;;
      *) printf '%s\n' FRESH_CODEX_ENV_UNAVAILABLE >&2; return 2 ;;
    esac
  done
  if [[ ! -d "$AGENTSTACK_FRESH_WRAPPER_ROOT" || ! -d "$AGENTSTACK_FRESH_RUNTIME_DIR" \
      || ! -d "$AGENTSTACK_FRESH_CODEX_HOME" || ! -f "$AGENTSTACK_FRESH_CODEX_BIN" \
      || ! -x "$AGENTSTACK_FRESH_CODEX_BIN" \
      || "$AGENTSTACK_FRESH_RUNTIME_DIR" != "${AGENTSTACK_FRESH_CLIENT_CONTEXT%/*}/launch/runtime" ]]; then
    printf '%s\n' FRESH_CODEX_ENV_UNAVAILABLE >&2; return 2
  fi
  export AGENTSTACK_PYTHON="$AGENTSTACK_FRESH_PYTHON" AGENTSTACK_RUNTIME_MODE=fresh
  export AGENTSTACK_HOME="$AGENTSTACK_FRESH_WRAPPER_ROOT"
  export AGENTSTACK_HOOKS_DIR="$AGENTSTACK_FRESH_WRAPPER_ROOT/hooks"
  export AGENTSTACK_RUNTIME_DIR="$AGENTSTACK_FRESH_RUNTIME_DIR"
  export AGENTSTACK_CODEX_BIN="$AGENTSTACK_FRESH_CODEX_BIN" CODEX_HOME="$AGENTSTACK_FRESH_CODEX_HOME"
}
_ags_restore_fresh_cli_env
