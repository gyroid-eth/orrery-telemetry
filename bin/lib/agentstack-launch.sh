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

# Only top-level entry points call these helpers. A delegated/reserved child
# must keep its separately validated context, never pass it as an override.
ags_parse_top_level_args() {
  AGS_EXPLICIT_PROJECT_KEY=""
  AGS_LAUNCH_DIR_ARGS=()
  AGS_LAUNCH_DRY_RUN=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --project-key)
        [[ $# -ge 2 && -n "${2:-}" ]] || ags_die "--project-key requires a non-empty value"
        [[ -z "$AGS_EXPLICIT_PROJECT_KEY" ]] || ags_die "--project-key specified more than once"
        AGS_EXPLICIT_PROJECT_KEY="$2"
        shift 2 ;;
      --dry-run)
        [[ "$AGS_PROG" == agent-start-gemini ]] || ags_die "unknown option: $1"
        AGS_LAUNCH_DRY_RUN=true
        shift ;;
      --)
        shift
        while [[ $# -gt 0 ]]; do AGS_LAUNCH_DIR_ARGS+=("$1"); shift; done ;;
      -*) ags_die "unknown option: $1" ;;
      *) AGS_LAUNCH_DIR_ARGS+=("$1"); shift ;;
    esac
  done
  [[ ${#AGS_LAUNCH_DIR_ARGS[@]} -le 1 ]] || ags_die "expected at most one directory"
  if [[ ${#AGS_LAUNCH_DIR_ARGS[@]} -eq 1 ]]; then
    [[ -n "${AGS_LAUNCH_DIR_ARGS[0]}" && -d "${AGS_LAUNCH_DIR_ARGS[0]}" ]] \
      || ags_die "directory not found: ${AGS_LAUNCH_DIR_ARGS[0]}"
  fi
}

# Decode data, not shell assignments. Validate the complete tuple before
# changing any environment; the legacy roots format cannot represent colons.
ags_prepare_top_level_context() {
  local target="$1"
  local explicit_key="${2:-}"
  local context="" decoded="" value=""
  local fields=()
  case "$target$explicit_key" in
    *$'\n'*|*$'\r'*) printf 'agentstack: control characters cannot be passed to the launcher\n' >&2; return 1 ;;
  esac
  context="$(bash "$BIN_DIR/../hooks/project-context.sh" \
    resolve-invocation-context "$target" "$explicit_key")" || return 1
  decoded="$("${AGENTSTACK_PYTHON:-python3}" - "$context" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
    fields = [data["project_key"], data["repository_key"] or "",
              data["work_dir"], data["worktree_root"] or ""]
    roots = data["protected_roots"]
    if not isinstance(roots, list) or not roots:
        raise ValueError("protected roots must be a nonempty array")
    if not all(isinstance(value, str) for value in fields + roots):
        raise ValueError("context fields must be strings")
    if not fields[0] or not fields[2] or any(not root for root in roots):
        raise ValueError("context contains an empty key or workspace")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in value)
           for value in fields + roots):
        raise ValueError("control characters cannot be passed to the launcher")
    if any(":" in root for root in roots):
        raise ValueError("protected roots containing ':' cannot use the legacy environment")
    print("\n".join(fields + [":".join(roots)]))
except (KeyError, TypeError, ValueError) as exc:
    print(f"agentstack: invalid launch context: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
)" || return 1
  while IFS= read -r value; do fields+=("$value"); done <<< "$decoded"
  [[ ${#fields[@]} -eq 5 ]] || return 1
  AGENTSTACK_PROJECT_KEY="${fields[0]}"
  PROJECT_KEY="${fields[0]}"
  AGENTSTACK_PROJECT_REPOSITORY="${fields[1]}"
  AGENTSTACK_PROJECT_WORK_DIR="${fields[2]}"
  AGENTSTACK_PROJECT_WORKTREE_ROOT="${fields[3]}"
  AGENTSTACK_PROTECTED_ROOTS="${fields[4]}"
  export AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_REPOSITORY
  export AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROJECT_WORKTREE_ROOT AGENTSTACK_PROTECTED_ROOTS
  # These outputs describe a workspace; they do not authenticate an identity.
  unset AGENTSTACK_PROJECT_CONTEXT AGENTSTACK_LOOKUP_PROJECT_KEY
  unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR
}

# Emit shell-quoted tmux options outside the pane command's nested quoting.
# Session values override a pre-existing server's stale global environment.
ags_tmux_project_options() {
  local name
  for name in AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_REPOSITORY \
    AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROJECT_WORKTREE_ROOT AGENTSTACK_PROTECTED_ROOTS; do
    printf -- '-e %q ' "$name=${!name}"
  done
  printf '%s' '-e AGENTSTACK_PROJECT_CONTEXT= -e AGENTSTACK_LOOKUP_PROJECT_KEY='
}

# After capturing the session options, do not seed a new tmux server globally
# with this invocation's project. This changes only the launching process.
ags_clear_client_project_context() {
  unset AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_REPOSITORY
  unset AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROJECT_WORKTREE_ROOT AGENTSTACK_PROTECTED_ROOTS
  unset AGENTSTACK_PROJECT_CONTEXT AGENTSTACK_LOOKUP_PROJECT_KEY
}
