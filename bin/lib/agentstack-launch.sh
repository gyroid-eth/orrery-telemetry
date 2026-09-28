#!/usr/bin/env bash
# Shared helpers for the agent-start / agent-start-codex launchers.
# SOURCE this file ( . agentstack-launch.sh ); it is not meant to be executed.
#
# Provides:
#   ags_load_env            source the installer-written env.sh (AGENTSTACK_*)
#   ags_die MSG             print error (prefixed with $AGS_PROG) and exit 1
#   ags_resolve_tmux        print the tmux binary path (or empty)
#   ags_abspath DIR         print the absolute path of DIR
#   ags_choose_dir [DIR]    resolve the working dir (arg / fzf picker / cwd)
#   ags_parse_top_level_args parse --project-key and top-level-only options
#   ags_prepare_top_level_launch show/validate the selected launch context
#
# Env it honours:
#   AGENTSTACK_HOME       install dir (default ~/.agentstack); env.sh lives here
#   AGENTSTACK_BASE_DIR   root the fzf picker browses (default $HOME)

AGS_PROG="${AGS_PROG:-agentstack}"

ags_die() { printf '%s: %s\n' "$AGS_PROG" "$*" >&2; exit 1; }

# Pull in installer-written AGENTSTACK_* values while preserving a project
# selector supplied by the launching shell. env.sh is still sourced for the
# other installed settings, but it must not silently replace that selector.
ags_load_env() {
  local envf="${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh"
  local live_agentstack_project_key="${AGENTSTACK_PROJECT_KEY:-}"
  local live_project_key="${PROJECT_KEY:-}"
  # shellcheck disable=SC1090
  [[ -f "$envf" ]] && . "$envf"

  AGS_PROJECT_KEY_SOURCE="unset"
  if [[ -n "$live_agentstack_project_key" ]]; then
    AGENTSTACK_PROJECT_KEY="$live_agentstack_project_key"
    PROJECT_KEY="$live_agentstack_project_key"
    AGS_PROJECT_KEY_SOURCE="AGENTSTACK_PROJECT_KEY"
  elif [[ -n "$live_project_key" ]]; then
    AGENTSTACK_PROJECT_KEY="$live_project_key"
    PROJECT_KEY="$live_project_key"
    AGS_PROJECT_KEY_SOURCE="PROJECT_KEY"
  elif [[ -n "${AGENTSTACK_PROJECT_KEY:-}" ]]; then
    PROJECT_KEY="$AGENTSTACK_PROJECT_KEY"
    AGS_PROJECT_KEY_SOURCE="installed env.sh"
  elif [[ -n "${PROJECT_KEY:-}" ]]; then
    AGENTSTACK_PROJECT_KEY="$PROJECT_KEY"
    AGS_PROJECT_KEY_SOURCE="installed env.sh"
  fi
  export AGENTSTACK_PROJECT_KEY PROJECT_KEY
  return 0
}

ags_resolve_tmux() {
  local c
  for c in /opt/homebrew/bin/tmux /usr/local/bin/tmux; do
    [[ -x "$c" ]] && { printf '%s\n' "$c"; return 0; }
  done
  command -v tmux 2>/dev/null || true
}

ags_abspath() { (cd "$1" 2>/dev/null && pwd) || return 1; }

# fzf one-level directory navigator rooted at $AGENTSTACK_BASE_DIR.
#   Right: descend   Left: parent (stops at base)   Enter: select   Esc: cancel
ags_pick_dir() {
  local base current pick key sel children parent
  base="${AGENTSTACK_BASE_DIR:-$HOME}"
  base="$(ags_abspath "$base")" || ags_die "AGENTSTACK_BASE_DIR is not a directory: ${AGENTSTACK_BASE_DIR:-$HOME}"
  current="$base"
  while true; do
    pick="$(find "$current" -maxdepth 1 -type d -not -name '.*' | sort \
      | fzf --height 50% --reverse \
            --prompt="${current/#$HOME/\~}/ > " \
            --header="<-:parent  ->:enter  Enter:select  Esc:cancel" \
            --expect="right,left" --no-multi)" || true
    key="$(printf '%s\n' "$pick" | sed -n '1p')"
    sel="$(printf '%s\n' "$pick" | sed -n '2p')"
    [[ -z "$key" && -z "$sel" ]] && return 1   # Esc / empty -> cancel
    case "$key" in
      right)
        if [[ -n "$sel" && -d "$sel" ]]; then
          children="$(find "$sel" -maxdepth 1 -type d -not -name '.*' -not -path "$sel" 2>/dev/null | head -1)"
          [[ -n "$children" ]] && current="$sel"
        fi ;;
      left)
        parent="$(dirname "$current")"
        [[ "$current" != "$base" ]] && current="$parent" ;;
      *)
        [[ -n "$sel" ]] && { printf '%s\n' "$sel"; return 0; } ;;
    esac
  done
}

# Resolve the working directory: explicit arg > fzf picker > current dir.
# Prints the chosen absolute path on stdout. Returns nonzero only on cancel.
ags_choose_dir() {
  if [[ $# -gt 0 && -n "$1" ]]; then
    [[ -d "$1" ]] || ags_die "directory not found: $1"
    ags_abspath "$1"; return 0
  fi
  if command -v fzf >/dev/null 2>&1; then
    ags_pick_dir; return $?
  fi
  printf '%s\n' "$PWD"
  echo "$AGS_PROG: fzf not installed; using current directory ($PWD)" >&2
  echo "          pass a path ($AGS_PROG DIR) or install fzf to browse a vault" >&2
  return 0
}

# Parse options shared by the interactive top-level launchers. Child launchers
# and resume paths intentionally do not call this helper.
ags_parse_top_level_args() {
  AGS_EXPLICIT_PROJECT_KEY=""
  AGS_EXPLICIT_PROJECT_KEY_SET=0
  AGS_LAUNCH_DIR=""
  AGS_LAUNCH_DIR_SET=0
  AGS_LAUNCH_DRY_RUN=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project-key)
        [[ $# -ge 2 && -n "${2:-}" ]] || ags_die "--project-key requires a non-empty value"
        [[ "$AGS_EXPLICIT_PROJECT_KEY_SET" == 0 ]] || ags_die "--project-key specified more than once"
        AGS_EXPLICIT_PROJECT_KEY="$2"
        AGS_EXPLICIT_PROJECT_KEY_SET=1
        shift 2
        ;;
      --dry-run)
        [[ "$AGS_PROG" == "agent-start-gemini" ]] || ags_die "unknown option: $1"
        AGS_LAUNCH_DRY_RUN=true
        shift
        ;;
      --)
        shift
        [[ $# -le 1 ]] || ags_die "expected at most one directory"
        if [[ $# -eq 1 ]]; then
          AGS_LAUNCH_DIR="$1"
          AGS_LAUNCH_DIR_SET=1
          shift
        fi
        ;;
      -*)
        ags_die "unknown option: $1"
        ;;
      *)
        [[ "$AGS_LAUNCH_DIR_SET" == 0 ]] || ags_die "expected at most one directory"
        AGS_LAUNCH_DIR="$1"
        AGS_LAUNCH_DIR_SET=1
        shift
        ;;
    esac
  done
}

ags_choose_top_level_dir() {
  if [[ "$AGS_LAUNCH_DIR_SET" == 1 ]]; then
    ags_choose_dir "$AGS_LAUNCH_DIR"
  else
    ags_choose_dir
  fi
}

# Apply --project-key after env.sh has been loaded, optionally require that
# explicit option, and show the effective context before registration. This
# never infers a project from the repository and never rewrites protected roots.
ags_prepare_top_level_launch() {
  local work_dir="$1"
  local project_key=""
  local project_source="${AGS_PROJECT_KEY_SOURCE:-unset}"
  local protected_roots=""

  if [[ "$AGS_EXPLICIT_PROJECT_KEY_SET" == 1 ]]; then
    AGENTSTACK_PROJECT_KEY="$AGS_EXPLICIT_PROJECT_KEY"
    PROJECT_KEY="$AGS_EXPLICIT_PROJECT_KEY"
    export AGENTSTACK_PROJECT_KEY PROJECT_KEY
    project_source="--project-key"
  fi

  if [[ "${AGENTSTACK_REQUIRE_EXPLICIT_PROJECT_KEY:-0}" == "1" &&
        "$AGS_EXPLICIT_PROJECT_KEY_SET" != 1 ]]; then
    printf '%s: --project-key is required because AGENTSTACK_REQUIRE_EXPLICIT_PROJECT_KEY=1\n' "$AGS_PROG" >&2
    printf 'usage: %s --project-key KEY [DIR]\n' "$AGS_PROG" >&2
    return 2
  fi

  project_key="${AGENTSTACK_PROJECT_KEY:-}"
  protected_roots="${AGENTSTACK_PROTECTED_ROOTS:-${AGENTSTACK_PROJECT_KEY:-}}"
  [[ -n "$project_key" ]] || project_key="<unset>"
  [[ -n "$protected_roots" ]] || protected_roots="<unset>"

  printf '%s: launch context: project=%s source=%s\n' \
    "$AGS_PROG" "$project_key" "$project_source" >&2
  printf '%s: launch context: workdir=%s protected_roots=%s\n' \
    "$AGS_PROG" "$work_dir" "$protected_roots" >&2
}
