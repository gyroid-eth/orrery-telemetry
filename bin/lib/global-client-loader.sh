#!/bin/bash
# One loader for partial installations. Normal selection belongs to Python.
if [[ -n "${BASH_SOURCE:-}" ]]; then _ags_client_loader_src="${BASH_SOURCE[0]}"; else _ags_client_loader_src="$0"; fi
AGS_CLIENT_LOADER_DIR="$(cd "$(dirname "$_ags_client_loader_src")" && pwd)"
AGS_CLIENT_ADMISSION_CONTEXT="$AGS_CLIENT_LOADER_DIR/../../runtime-client.json"

ags_client_missing_admission() {
  local argument context
  context="$AGS_CLIENT_ADMISSION_CONTEXT"
  for argument in "$@"; do
    case "$argument" in --context|--context=*|--runtime-mode|--runtime-mode=*|--server-config|--server-config=*) printf '%s\n' GLOBAL_ENTRY_UNAVAILABLE >&2; return 2 ;; esac
  done
  if [[ "${AGENTSTACK_RUNTIME_MODE-legacy}" != legacy || -n "${AGENTSTACK_GLOBAL_SERVER_CONFIG:-}" || -n "${AGENTSTACK_CLIENT_CONFIG:-}" || -e "$context" || -L "$context" || -e "$AGS_CLIENT_LOADER_DIR/../../clients" || -L "$AGS_CLIENT_LOADER_DIR/../../clients" ]]; then
    printf '%s\n' GLOBAL_ENTRY_UNAVAILABLE >&2
    return 2
  fi
  return 125
}

ags_client_selection_failure() {
  local selection_rc="$1"
  shift
  [[ "$selection_rc" != 64 ]] || return 2
  ags_client_missing_admission "$@"
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
    ags_client_selection_failure "$ags_selection_rc" "$@"; return $?
  fi
  case "$response" in
    orrery-client-global-v1|"orrery-client-global-v1"$'\n'*)
      export AGENTSTACK_RUNTIME_MODE=global
      if [[ "$response" == *$'\n'* ]]; then
        export AGENTSTACK_CLIENT_CONFIG="${response#*$'\n'}"
      else
        unset AGENTSTACK_CLIENT_CONFIG
      fi
      return 0 ;;
    orrery-client-legacy-v1) return 125 ;;
    *) ags_client_missing_admission "$@" ;;
  esac
}

ags_client_bootstrap() {
  local env_file="$1" inherited_mode="${AGENTSTACK_RUNTIME_MODE-}" mode_set="${AGENTSTACK_RUNTIME_MODE+set}" choice admission_rc
  local inherited_config="${AGENTSTACK_GLOBAL_SERVER_CONFIG-}" config_set="${AGENTSTACK_GLOBAL_SERVER_CONFIG+set}"
  shift
  local env_rc=0 errexit_set= setting assignment system= explicit_mode= continuation= supplement_default= literal= argument
  local default_env_file="$env_file"
  for argument in "$@"; do
    case "$argument" in --continue) continuation=1 ;; --system) system=1 ;; --literal) literal=1 ;; --runtime-mode|--runtime-mode=*) explicit_mode=1 ;; esac
  done
  if [[ "$continuation" == 1 && -n "${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}" ]]; then
    # Keep the old project guard. Python supplements only mode/config in a
    # subprocess; installed assignments cannot overwrite this live shell.
    env_file=
    if [[ "$mode_set" != set ]]; then set -- "$@" --bootstrap; supplement_default=1; fi
  fi
  if [[ "$system" == 1 && ( -e "$env_file" || -L "$env_file" ) ]]; then
    unset AGENTSTACK_RUNTIME_MODE AGENTSTACK_GLOBAL_SERVER_CONFIG AGENTSTACK_CLIENT_CONFIG
  fi
  [[ "$-" != *e* ]] || errexit_set=set
  local inherited_context="${AGENTSTACK_CLIENT_CONFIG-}" context_set="${AGENTSTACK_CLIENT_CONFIG+set}"
  if [[ -e "$env_file" || -L "$env_file" ]]; then
    [[ -f "$env_file" && -r "$env_file" ]] || { printf '%s\n' RUNTIME_DEFAULT_UNAVAILABLE >&2; return 2; }
    set +e
    if [[ "$literal" == 1 ]]; then
      # The installer already resolves settings as data. Reuse that reader;
      # never execute unrelated assignments or shell expansions during install.
      if ! declare -F agentstack_installed_env_value >/dev/null; then
        env_rc=2
      else
        for setting in AGENTSTACK_RUNTIME_MODE AGENTSTACK_GLOBAL_SERVER_CONFIG AGENTSTACK_CLIENT_CONFIG; do
          assignment="$(agentstack_installed_env_value "$setting" "$env_file" --shell)" || { env_rc=2; break; }
          eval "$assignment" || { env_rc=2; break; }
        done
      fi
    elif declare -F agentstack_load_installed_env >/dev/null; then
      agentstack_load_installed_env "$env_file" || env_rc=$?
    else
      . "$env_file" || env_rc=$?
    fi
    [[ "$errexit_set" != set ]] || set -e
    if [[ "$env_rc" != 0 ]]; then
      printf '%s\n' RUNTIME_DEFAULT_UNAVAILABLE >&2
      return 2
    fi
  fi
  if [[ "$system" == 1 && ( -e "$env_file" || -L "$env_file" ) ]]; then
    if [[ "$mode_set" == set && "$inherited_mode" != "${AGENTSTACK_RUNTIME_MODE-legacy}" && "$explicit_mode" != 1 ]]; then
      printf '%s\n' RUNTIME_MODE_INHERITED_CONFLICT >&2
      return 2
    fi
    # Installed system defaults are not a continuation of the caller's owner.
    mode_set=set; inherited_mode="${AGENTSTACK_RUNTIME_MODE-legacy}"
    config_set="${AGENTSTACK_GLOBAL_SERVER_CONFIG+set}"; inherited_config="${AGENTSTACK_GLOBAL_SERVER_CONFIG-}"
    context_set=set; inherited_context="${AGENTSTACK_CLIENT_CONFIG-}"
  fi
  if [[ "$mode_set" == set ]]; then export AGENTSTACK_RUNTIME_MODE="$inherited_mode"; fi
  if [[ "$config_set" == set ]]; then export AGENTSTACK_GLOBAL_SERVER_CONFIG="$inherited_config";
  elif [[ "$mode_set" == set ]]; then unset AGENTSTACK_GLOBAL_SERVER_CONFIG; fi
  if [[ "$context_set" == set ]]; then export AGENTSTACK_CLIENT_CONFIG="$inherited_context";
  elif [[ "$mode_set" == set ]]; then unset AGENTSTACK_CLIENT_CONFIG; fi
  ags_client_missing_admission "$@" 2>/dev/null && admission_rc=0 || admission_rc=$?
  if [[ "$admission_rc" == 125 && "$supplement_default" != 1 ]]; then export AGENTSTACK_RUNTIME_MODE=legacy; return 0; fi
  if [[ ! -f "$AGS_CLIENT_LOADER_DIR/runtime_client.py" ]]; then
    admission_rc=2
  elif [[ "$mode_set" != set ]]; then
    choice="$(python3 "$AGS_CLIENT_LOADER_DIR/runtime_client.py" select --installed-default --shell "$@")" && admission_rc=0 || admission_rc=$?
  else
    choice="$(python3 "$AGS_CLIENT_LOADER_DIR/runtime_client.py" select --shell "$@")" && admission_rc=0 || admission_rc=$?
  fi
  if [[ "$admission_rc" != 0 ]]; then
    # A partial helper cannot prove an installed default that was deliberately
    # kept out of this live shell. Unknown selection is never legacy success.
    if [[ "$supplement_default" == 1 && "$admission_rc" != 64 && ( -e "$default_env_file" || -L "$default_env_file" ) ]]; then
      printf '%s\n' RUNTIME_DEFAULT_UNAVAILABLE >&2
      return 2
    fi
    ags_client_selection_failure "$admission_rc" "$@" && admission_rc=0 || admission_rc=$?
    if [[ "$admission_rc" == 125 ]]; then export AGENTSTACK_RUNTIME_MODE=legacy; return 0; fi
    return "$admission_rc"
  fi
  eval "$choice"
}

ags_client_entry() {
  local entry="$1" ags_selection_rc
  shift
  case "$entry" in
    pre-edit|post-edit|end-session|session-start|registered|record-self)
      if [[ "${AGENTSTACK_MAIL_DISABLED:-}" == 1 ]]; then set -- --disabled-hook "$@"; fi ;;
  esac
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
  if [[ "${1:-}" == admit ]]; then
    shift
    # A bundled adapter uses this same admission against its core install root.
    if [[ "${1:-}" == --root ]]; then
      [[ "${2:-}" == /* ]] || { printf '%s\n' GLOBAL_ENTRY_UNAVAILABLE >&2; exit 2; }
      AGS_CLIENT_LOADER_DIR="$2/bin/lib"
      AGS_CLIENT_ADMISSION_CONTEXT="$2/runtime-client.json"
      shift 2
    fi
    ags_client_select "$@" && _ags_admit_rc=0 || _ags_admit_rc=$?
    if [[ "$_ags_admit_rc" == 0 || "$_ags_admit_rc" == 125 ]]; then printf '%s\n' orrery-client-admission-v1; fi
    exit "$_ags_admit_rc"
  elif [[ "${1:-}" == select ]]; then shift; ags_client_select "$@"; else ags_client_entry "$@"; fi
fi
