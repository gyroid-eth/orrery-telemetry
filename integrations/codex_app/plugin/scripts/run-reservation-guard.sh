#!/bin/bash
# This decision is synchronous and fail-closed; telemetry's runner is fail-open.
if [[ "${AGENTSTACK_FRESH_PYTHON+x}" == x || -n "${AGENTSTACK_FRESH_LAUNCH_ID:-}" ]]; then
  _ags_env="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fresh-cli-env.sh"
  [[ -f "$_ags_env" ]] || { printf '%s\n' FRESH_CODEX_ENV_UNAVAILABLE >&2; exit 2; }
  . "$_ags_env" || exit 2
fi
if [[ ( "${AGENTSTACK_FRESH_CLIENT_CONTEXT+x}" == x || "${AGENTSTACK_RUNTIME_MODE:-legacy}" == fresh ) && -z "${AGENTSTACK_FRESH_LAUNCH_ID:-}" ]]; then
  _ags_plugin="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  _ags_source="$_ags_plugin/src"
  [[ -d "$_ags_source/agentstack_codex_app" ]] || _ags_source="$(cd "$_ags_plugin/../src" && pwd)"
  export PYTHONPATH="$_ags_source${PYTHONPATH:+:$PYTHONPATH}"
  exec "${AGENTSTACK_PYTHON:-python3}" -m agentstack_codex_app.fresh_bridge guard
fi
_ags_fresh_cli=0
if [[ "${AGENTSTACK_FRESH_CLIENT_CONTEXT+x}" == x || "${AGENTSTACK_RUNTIME_MODE:-legacy}" == fresh ]]; then
  [[ "${AGENTSTACK_PYTHON:-}" == "${AGENTSTACK_FRESH_PYTHON:-}" && -n "${AGENTSTACK_FRESH_PYTHON:-}" ]] || {
    printf '%s\n' FRESH_CODEX_PYTHON_UNAVAILABLE >&2; exit 2;
  }
  _ags_fresh_cli=1
fi
CORE_HOME="${AGENTSTACK_HOME:-$HOME/.agentstack}"
GUARD="$CORE_HOME/hooks/check-codex-file-reservation.py"
if [[ ! -f "$GUARD" ]]; then
  echo "CODEX RESERVATION GUARD unavailable: refresh the core installation and plugin, then review /hooks." >&2
  exit 2
fi
# A conditional also traps launch failures when the caller exported errexit.
if "${AGENTSTACK_PYTHON:-python3}" "$GUARD"; then
  exit 0
else
  _ags_guard_rc=$?
fi
if [[ "$_ags_fresh_cli" == 1 && "$_ags_guard_rc" != 2 ]]; then
  printf '%s\n' FRESH_CODEX_GUARD_UNAVAILABLE >&2
fi
exit 2
