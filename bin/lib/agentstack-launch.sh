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
#
# Env it honours:
#   AGENTSTACK_HOME       install dir (default ~/.agentstack); env.sh lives here
#   AGENTSTACK_BASE_DIR   root the fzf picker browses (default $HOME)

AGS_PROG="${AGS_PROG:-agentstack}"

ags_die() { printf '%s: %s\n' "$AGS_PROG" "$*" >&2; exit 1; }

# Pull in installer-written AGENTSTACK_* values. Variables already set in the
# environment win (env.sh uses `export KEY=val`, so we load it first and let the
# caller's explicit overrides be re-applied by the caller if needed).
ags_load_env() {
  local envf="${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh"
  # shellcheck disable=SC1090
  [[ -f "$envf" ]] && . "$envf"
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
