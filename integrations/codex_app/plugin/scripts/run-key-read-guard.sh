#!/bin/bash
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
  if "$AGENTSTACK_PYTHON" "$GUARD"; then
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
  exec /bin/bash "$GUARD"
fi
# No usable core guard: do not disable ordinary shell commands.
PAYLOAD="$(cat)"
case "$PAYLOAD" in
  *find-generic-password*-w*|*find-generic-password*-g*|*secret-tool*lookup*|*secret-tool*search*)
    echo 'LOCAL_MODEL_KEY_READ_UNAVAILABLE: refresh the core installation and review /hooks.' >&2
    exit 2 ;;
esac
exit 0
