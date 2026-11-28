#!/bin/bash
IFS= read -r -d '' PAYLOAD || :
# Codex emits a flat scalar header before tool_name. Parse only that prefix;
# nested fields, duplicate names or a changed shape go to the core.
_scalar='("([^"\\]|\\.)*"|null|true|false|-?[0-9]+)'
_mcp_name='^[[:space:]]*\{[[:space:]]*("[A-Za-z_][A-Za-z_0-9]*"[[:space:]]*:[[:space:]]*'"$_scalar"'[[:space:]]*,[[:space:]]*)*"tool_name"[[:space:]]*:[[:space:]]*"mcp__[A-Za-z0-9_.-]+"[[:space:]]*[,}]'
if [[ "$PAYLOAD" != *'"tool_name"'*'"tool_name"'* && "$PAYLOAD" =~ $_mcp_name ]]; then
  exit 0
fi
if [[ "${AGENTSTACK_FRESH_PYTHON+x}" == x || -n "${AGENTSTACK_FRESH_LAUNCH_ID:-}" ]]; then
  _ags_env="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fresh-cli-env.sh"
  [[ -f "$_ags_env" ]] || { printf '%s\n' FRESH_CODEX_ENV_UNAVAILABLE >&2; exit 2; }
  . "$_ags_env" || exit 2
fi
CORE_HOME="${AGENTSTACK_HOME:-$HOME/.agentstack}"
if [[ ( "${AGENTSTACK_FRESH_CLIENT_CONTEXT+x}" == x || "${AGENTSTACK_RUNTIME_MODE:-legacy}" == fresh ) && ! ( -z "${AGENTSTACK_FRESH_LAUNCH_ID:-}" && "${AGENTSTACK_CODEX_APP_RUNTIME_DIR+x}" == x ) ]]; then
  [[ "${AGENTSTACK_PYTHON:-}" == "${AGENTSTACK_FRESH_PYTHON:-}" && -n "${AGENTSTACK_FRESH_PYTHON:-}" ]] || {
    printf '%s\n' FRESH_CODEX_PYTHON_UNAVAILABLE >&2; exit 2;
  }
  GUARD="$CORE_HOME/hooks/deny-llm-key-read.py"
  if [[ ! -f "$GUARD" ]]; then
    printf '%s\n' FRESH_CODEX_GUARD_UNAVAILABLE >&2; exit 2
  fi
  # CLI fresh must not enter the legacy shell guard's fail-open fallback.
  if "$AGENTSTACK_PYTHON" "$GUARD" <<< "$PAYLOAD"; then
    exit 0
  else
    _ags_guard_rc=$?
  fi
  if [[ "$_ags_guard_rc" != 2 ]]; then
    printf '%s\n' FRESH_CODEX_GUARD_UNAVAILABLE >&2
  fi
  exit 2
fi
GUARD="$CORE_HOME/hooks/deny-llm-key-read.sh"
if [[ -f "$GUARD" ]]; then
  exec /bin/bash "$GUARD" <<< "$PAYLOAD"
fi
# No usable core guard: do not disable ordinary shell commands.
case "$PAYLOAD" in
  *find-generic-password*-w*|*find-generic-password*-g*|*secret-tool*lookup*|*secret-tool*search*)
    echo 'LOCAL_MODEL_KEY_READ_UNAVAILABLE: refresh the core installation and review /hooks.' >&2
    exit 2 ;;
esac
exit 0
