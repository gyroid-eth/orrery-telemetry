#!/bin/bash
# This decision is synchronous and fail-closed; telemetry's runner is fail-open.
if [[ ( "${AGENTSTACK_FRESH_CLIENT_CONTEXT+x}" == x || "${AGENTSTACK_RUNTIME_MODE:-legacy}" == fresh ) && -z "${AGENTSTACK_FRESH_LAUNCH_ID:-}" ]]; then
  _ags_plugin="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  _ags_source="$_ags_plugin/src"
  [[ -d "$_ags_source/agentstack_codex_app" ]] || _ags_source="$(cd "$_ags_plugin/../src" && pwd)"
  export PYTHONPATH="$_ags_source${PYTHONPATH:+:$PYTHONPATH}"
  exec "${AGENTSTACK_PYTHON:-python3}" -m agentstack_codex_app.fresh_bridge guard
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
fi
exit 2
