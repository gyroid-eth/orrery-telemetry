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

# Pull in installer-written AGENTSTACK_* values without letting them replace a
# setting chosen for this command: an explicit value wins over env.sh, which
# wins over the defaults below (issue #33). The order lives in
# hooks/project-context.sh so the installer and the launchers share it.
AGS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ags_load_env() {
  local envf="${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh"
  local ctx="$AGS_LIB_DIR/../../hooks/project-context.sh"
  [[ -f "$ctx" ]] || ctx="${AGENTSTACK_HOME:-$HOME/.agentstack}/hooks/project-context.sh"
  [[ -f "$ctx" ]] || ags_die "missing hooks/project-context.sh next to $AGS_LIB_DIR; re-run install.sh"
  # shellcheck source=../../hooks/project-context.sh
  . "$ctx"
  AGS_PROJECT_CONTEXT_HELPER="$ctx"
  agentstack_load_top_level_env "$envf" "$@"
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

# Keep launch-root resolution shared with child and resume boundaries.
ags_prepare_top_level_context() {
  if ! command -v agentstack_apply_workspace_context >/dev/null 2>&1; then
    # shellcheck source=../../hooks/project-context.sh
    . "${AGS_PROJECT_CONTEXT_HELPER:-$AGS_LIB_DIR/../../hooks/project-context.sh}" || return 1
  fi
  agentstack_apply_workspace_context "$1" "${2:-}"
}

# Emit shell-quoted tmux options outside the pane command's nested quoting.
# Session values override a pre-existing server's stale global environment.
ags_tmux_project_options() {
  local name
  for name in AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_REPOSITORY \
    AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROJECT_WORKTREE_ROOT AGENTSTACK_PROTECTED_ROOTS AGENTSTACK_EXTRA_PROTECTED_ROOTS AGENTSTACK_PROTECTION_CONTEXT; do
    printf -- '-e %q ' "$name=${!name:-}"
  done
  printf '%s' '-e AGENTSTACK_PROJECT_CONTEXT= -e AGENTSTACK_LOOKUP_PROJECT_KEY='
}

# Mark only the session this launcher just created as removing the three Git
# selectors. Merely unsetting them in its first process would let later panes
# inherit the old server-global values again. Do not use set-environment -g.
ags_tmux_clear_git_command() {
  local session="$1" name separator=""
  for name in GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR; do
    printf '%s%q set-environment -r -t %q %q' "$separator" "$TMUX_BIN" "=$session" "$name"
    separator=" && "
  done
}

# After capturing the session options, do not seed a new tmux server globally
# with this invocation's project. This changes only the launching process.
ags_clear_client_project_context() {
  unset AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_REPOSITORY
  unset AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROJECT_WORKTREE_ROOT AGENTSTACK_PROTECTED_ROOTS AGENTSTACK_EXTRA_PROTECTED_ROOTS
  unset AGENTSTACK_PROJECT_CONTEXT AGENTSTACK_LOOKUP_PROJECT_KEY AGENTSTACK_PROTECTION_CONTEXT
}

# Restore the selected tuple after the new pane's shell startup files run. The
# returned text is escaped for the double-quoted pane command in a generated
# launcher script; it must not be used as an unquoted shell command directly.
ags_tmux_restore_workspace_command() {
  local name statement="" result=""
  for name in AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_REPOSITORY \
    AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROJECT_WORKTREE_ROOT \
    AGENTSTACK_PROTECTED_ROOTS AGENTSTACK_EXTRA_PROTECTED_ROOTS AGENTSTACK_PROTECTION_CONTEXT; do
    printf -v statement 'export %s=%q; ' "$name" "${!name:-}"
    result="$result$statement"
  done
  result="$result"'cd -- "$AGENTSTACK_PROJECT_WORK_DIR" || exit 1; unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR AGENTSTACK_PROJECT_CONTEXT AGENTSTACK_LOOKUP_PROJECT_KEY; '
  result="${result//\\/\\\\}"
  result="${result//\$/\\\$}"
  result="${result//\`/\\\`}"
  result="${result//\"/\\\"}"
  printf '%s' "$result"
}
