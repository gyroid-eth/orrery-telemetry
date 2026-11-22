#!/bin/bash
# Private existing-consumer adapter; mode selection remains in the common loader.
_ags_legacy_tmux_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ags_legacy_tmux() {
  if [[ -f "$_ags_legacy_tmux_lib/runtime_tmux_consumer.py" && -f "$_ags_legacy_tmux_lib/runtime_tmux.py" ]]; then
    python3 "$_ags_legacy_tmux_lib/runtime_tmux_consumer.py" "$@"
    return $?
  fi
  # Reuse the existing partial-install admission, never a second selector.
  if declare -F ags_client_missing_admission >/dev/null; then
    local ags_legacy_missing_rc
    ags_client_missing_admission && ags_legacy_missing_rc=0 || ags_legacy_missing_rc=$?
    [[ "$ags_legacy_missing_rc" == 125 ]] || return 2
  else
    printf '%s\n' GLOBAL_TARGET_HELPER_UNAVAILABLE >&2
    return 2
  fi
  command tmux "$@"
}
tmux() { ags_legacy_tmux "$@"; }
