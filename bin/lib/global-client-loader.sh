#!/bin/bash
# One loader for partial installations. Normal selection belongs to Python.
if [[ -n "${BASH_SOURCE:-}" ]]; then _ags_client_loader_src="${BASH_SOURCE[0]}"; else _ags_client_loader_src="$0"; fi
AGS_CLIENT_LOADER_DIR="$(cd "$(dirname "$_ags_client_loader_src")" && pwd)"

ags_client_missing_admission() {
  local argument context
  context="$AGS_CLIENT_LOADER_DIR/../../runtime-client.json"
  for argument in "$@"; do
    case "$argument" in --context|--context=*) printf '%s\n' GLOBAL_ENTRY_UNAVAILABLE >&2; return 2 ;; esac
  done
  if [[ -n "${AGENTSTACK_CLIENT_CONFIG:-}" || -e "$context" || -L "$context" ]]; then
    printf '%s\n' GLOBAL_ENTRY_UNAVAILABLE >&2
    return 2
  fi
  return 125
}

ags_client_select() {
  local response ags_selection_rc
  ags_client_missing_admission "$@" 2>/dev/null && ags_selection_rc=0 || ags_selection_rc=$?
  [[ "$ags_selection_rc" != 125 ]] || return 125
  if [[ ! -f "$AGS_CLIENT_LOADER_DIR/runtime_client.py" || ! -f "$AGS_CLIENT_LOADER_DIR/agentstack-register.sh" ]]; then
    ags_client_missing_admission "$@"; return $?
  fi
  response="$(python3 "$AGS_CLIENT_LOADER_DIR/runtime_client.py" select "$@" </dev/null)" && ags_selection_rc=0 || ags_selection_rc=$?
  if [[ "$ags_selection_rc" != 0 ]]; then
    ags_client_missing_admission "$@"; return $?
  fi
  case "$response" in
    "orrery-client-global-v1"$'\n'*)
      export AGENTSTACK_CLIENT_CONFIG="${response#*$'\n'}"
      return 0 ;;
    orrery-client-legacy-v1) return 125 ;;
    *) ags_client_missing_admission "$@" ;;
  esac
}

ags_client_entry() {
  local entry="$1" ags_selection_rc
  shift
  ags_client_select "$@" && ags_selection_rc=0 || ags_selection_rc=$?
  [[ "$ags_selection_rc" == 0 ]] || return "$ags_selection_rc"
  case "$entry" in
    pre-edit|post-edit|end-session|session-start|registered|record-self)
      [[ "${AGENTSTACK_MAIL_DISABLED:-}" != 1 ]] || return 0 ;;
  esac
  . "$AGS_CLIENT_LOADER_DIR/agentstack-register.sh"
  if ! declare -F ags_global_entry >/dev/null; then
    ags_client_missing_admission "$@"; return $?
  fi
  ags_global_entry "$entry" "$@" && ags_selection_rc=0 || ags_selection_rc=$?
  if [[ "$ags_selection_rc" == 125 ]]; then
    ags_client_missing_admission "$@"; return $?
  fi
  return "$ags_selection_rc"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "${1:-}" == select ]]; then shift; ags_client_select "$@"; else ags_client_entry "$@"; fi
fi
