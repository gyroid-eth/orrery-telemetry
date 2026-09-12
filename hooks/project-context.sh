#!/bin/bash
# Shared project-key/protected-root resolution for hooks and installed helpers.
# Keep this file compatible with macOS /bin/bash 3.2.

# Read one literal `export NAME=value` assignment without sourcing env.sh.
# The installer writes values with Python shlex.quote; shlex reverses that
# quoting without expanding variables, command substitutions, or shell code.
# A missing file is "no value" (status 0); an existing file that cannot be read,
# or an interpreter that cannot run, is an error (non-zero) callers propagate.
agentstack_installed_env_value() {
    local name="$1"
    local env_file="${2:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    case "$name" in
        *[!A-Z0-9_]*|"") return 0 ;;
    esac
    [ -f "$env_file" ] || return 0
    "${AGENTSTACK_PYTHON:-python3}" - "$env_file" "$name" <<'PY' 2>/dev/null
import pathlib
import re
import shlex
import sys

path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
try:
    raw = path.read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit(1)

pattern = re.compile(r"^\s*export\s+" + re.escape(name) + r"=(.*)$")
for line in raw.splitlines():
    match = pattern.match(line)
    if match is None:
        continue
    try:
        values = shlex.split(match.group(1), comments=True, posix=True)
    except ValueError:
        raise SystemExit(0)
    if len(values) == 1:
        print(values[0], end="")
    raise SystemExit(0)
PY
}

# Print an existing directory as a physical absolute path.  Callers use this
# before comparing roots so symlink spellings cannot create a second project
# identity or escape a protected root.
agentstack_physical_dir() {
    local directory="${1:-}"
    [ -n "$directory" ] || return 1
    [ -d "$directory" ] || return 1
    (cd "$directory" 2>/dev/null && pwd -P)
}

# Match ORRERY Mail's filesystem-key spelling without changing logical names.
# Absolute paths are resolved even when their final component does not exist,
# so macOS aliases such as /tmp/new-project and /private/tmp/new-project share
# one namespace. Existing relative directories are physicalized as well.
# This is deliberately path normalization only: explicit linked-worktree keys
# remain that worktree and are not collapsed to the Git common directory.
agentstack_normalize_project_key() {
    local project_key="${1:-}"
    local physical=""
    [ -n "$project_key" ] || {
        printf '\n'
        return 0
    }
    case "$project_key" in
        /*)
            # Normal installation targets already exist. Resolve those with
            # shell builtins so the installer can report a missing or
            # incompatible Python through its aggregated preflight instead of
            # failing inside project-key setup before preflight runs.
            if [ -d "$project_key" ]; then
                physical="$(agentstack_physical_dir "$project_key")" || return 1
                printf '%s\n' "$physical"
                return 0
            fi
            "${AGENTSTACK_PYTHON:-python3}" - "$project_key" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).resolve())
PY
            ;;
        *)
            if [ -d "$project_key" ]; then
                physical="$(agentstack_physical_dir "$project_key")" || return 1
                printf '%s\n' "$physical"
            else
                printf '%s\n' "$project_key"
            fi
            ;;
    esac
}

# Print the physical root of the linked worktree containing TARGET.
#
# Git-related environment from a caller working in another checkout must not
# redirect these probes.  In particular, inherited GIT_DIR/GIT_WORK_TREE made
# an otherwise correct `git -C` query describe the caller's old repository.
agentstack_git_worktree_root() {
    local target="${1:-}"
    local root=""
    [ -n "$target" ] || return 1
    [ -d "$target" ] || return 1
    root="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || return 1
    agentstack_physical_dir "$root"
}

# Print one stable repository identity for TARGET.  A normal repository and
# every linked worktree share --git-common-dir.  For the standard non-bare
# layout, use the main checkout (the parent of its .git directory) as the Mail
# human key; for uncommon separate/bare layouts the common directory itself is
# still a stable identity shared by all worktrees.
agentstack_repository_key() {
    local target="${1:-}"
    local common="" common_abs=""
    [ -n "$target" ] || return 1
    [ -d "$target" ] || return 1
    common="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$target" rev-parse --git-common-dir 2>/dev/null)" || return 1
    case "$common" in
        /*) common_abs="$(agentstack_physical_dir "$common")" || return 1 ;;
        *) common_abs="$(cd "$target" 2>/dev/null && cd "$common" 2>/dev/null && pwd -P)" || return 1 ;;
    esac
    if [ "$(basename "$common_abs")" = ".git" ]; then
        dirname "$common_abs"
    else
        printf '%s\n' "$common_abs"
    fi
}

agentstack_same_repository() {
    local left="${1:-}"
    local right="${2:-}"
    local left_key="" right_key=""
    left_key="$(agentstack_repository_key "$left" 2>/dev/null)" || return 1
    right_key="$(agentstack_repository_key "$right" 2>/dev/null)" || return 1
    [ -n "$left_key" ] && [ "$left_key" = "$right_key" ]
}

# True when the inherited established context is bound to TARGET.  The project
# key itself may be an explicit human key unrelated to a path, so repository
# provenance is stored separately.  Non-Git workspaces are bound to the
# selected workspace and its descendants.
agentstack_context_matches_target() {
    local target="${1:-}"
    local target_repo="" bound_repo="" target_dir="" bound_dir=""
    [ "${AGENTSTACK_PROJECT_CONTEXT:-}" = "1" ] || return 1
    [ -n "${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}" ] || return 1
    [ -n "$target" ] || return 1

    target_repo="$(agentstack_repository_key "$target" 2>/dev/null || true)"
    if [ -n "$target_repo" ]; then
        [ -n "${AGENTSTACK_PROJECT_REPOSITORY:-}" ] || return 1
        bound_repo="$(agentstack_physical_dir "$AGENTSTACK_PROJECT_REPOSITORY" 2>/dev/null || true)"
        [ -n "$bound_repo" ] && [ "$target_repo" = "$bound_repo" ]
        return $?
    fi

    [ -n "${AGENTSTACK_PROJECT_WORK_DIR:-}" ] || return 1
    target_dir="$(agentstack_physical_dir "$target" 2>/dev/null || true)"
    bound_dir="$(agentstack_physical_dir "$AGENTSTACK_PROJECT_WORK_DIR" 2>/dev/null || true)"
    [ -n "$target_dir" ] && [ -n "$bound_dir" ] || return 1
    case "$target_dir" in
        "$bound_dir"|"$bound_dir"/*) return 0 ;;
    esac
    return 1
}

# Priority: explicit per-invocation selection, repository-bound established
# context, canonical Git repository, live non-Git project context, installed
# env fallback, target/cwd fallback.  The fourth argument deliberately keeps
# explicit selection separate from inheritable environment variables.
agentstack_resolve_project_key() {
    local target="${1:-}"
    local env_file="${2:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local use_cwd="${3:-1}"
    local explicit_key="${4:-}"
    local established="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
    local repository="" installed=""

    if [ -n "$explicit_key" ]; then
        agentstack_normalize_project_key "$explicit_key"
        return $?
    fi
    if [ -z "$target" ] && [ "$use_cwd" != "0" ]; then
        target="$(pwd -P)"
    fi
    if [ -n "$established" ] && agentstack_context_matches_target "$target"; then
        agentstack_normalize_project_key "$established"
        return $?
    fi
    repository="$(agentstack_repository_key "$target" 2>/dev/null || true)"
    if [ -n "$repository" ]; then
        printf '%s\n' "$repository"
        return 0
    fi
    if [ -n "$established" ]; then
        agentstack_normalize_project_key "$established"
        return $?
    fi
    installed="$(agentstack_installed_env_value AGENTSTACK_PROJECT_KEY "$env_file")" || return $?
    if [ -n "$installed" ]; then
        agentstack_normalize_project_key "$installed"
        return $?
    fi
    agentstack_normalize_project_key "$target"
}

# Export a resolved context for launch/bootstrap/child processes.  Call this in
# the current shell (not command substitution) so later hooks can distinguish
# an established explicit selection from stale ambient values.
agentstack_export_project_context() {
    local resolved_project_key="$1"
    local target="$2"
    local repository="" work_dir="" roots=""
    # Every fallible step returns before any tuple field is assigned. The
    # explicit `|| return` also holds when called under `if`/`!`, where errexit
    # does not stop at a failed assignment.
    resolved_project_key="$(agentstack_normalize_project_key "$resolved_project_key")" \
        || return $?
    repository="$(agentstack_repository_key "$target" 2>/dev/null || true)"
    work_dir="$(agentstack_git_worktree_root "$target" 2>/dev/null || true)"
    if [ -z "$work_dir" ]; then
        work_dir="$(agentstack_physical_dir "$target" 2>/dev/null || true)"
    fi

    # Resolve roots before replacing the inherited binding below.  For Git,
    # the target worktree/common-dir always wins.  For the legacy non-Git
    # single-project mode this preserves the installed/custom protected-root
    # set instead of reducing it to the newly assigned work directory.
    roots="$(agentstack_resolve_runtime_protected_roots \
        "$resolved_project_key" "$target")" || return $?

    AGENTSTACK_PROJECT_KEY="$resolved_project_key"
    PROJECT_KEY="$resolved_project_key"
    AGENTSTACK_PROJECT_CONTEXT=1
    AGENTSTACK_PROJECT_REPOSITORY="$repository"
    AGENTSTACK_PROJECT_WORK_DIR="$work_dir"
    export AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_CONTEXT
    export AGENTSTACK_PROJECT_REPOSITORY AGENTSTACK_PROJECT_WORK_DIR

    AGENTSTACK_PROTECTED_ROOTS="$roots"
    export AGENTSTACK_PROTECTED_ROOTS
}

# Print TARGET followed by the existing protected roots, using physical paths
# and omitting duplicates.  TARGET is always first so a non-Git invocation is
# protected even when an older install supplied only auxiliary roots.
agentstack_merge_non_git_roots() {
    local target="${1:-}"
    local supplied="${2:-}"
    local target_dir="" result="" old_ifs="" item="" root=""
    target_dir="$(agentstack_physical_dir "$target" 2>/dev/null || true)"
    result="$target_dir"
    old_ifs="$IFS"
    IFS=:
    for item in $supplied; do
        root="$(agentstack_physical_dir "$item" 2>/dev/null || true)"
        [ -n "$root" ] || continue
        case ":$result:" in
            *":$root:"*) ;;
            *) result="${result:+$result:}$root" ;;
        esac
    done
    IFS="$old_ifs"
    printf '%s\n' "${result:-$supplied}"
}

# True when one root is TARGET itself or an ancestor of it.  This lets an old
# installed non-Git configuration remain useful when an explicit human key is
# introduced without treating roots from an unrelated ambient project as safe.
agentstack_roots_cover_target() {
    local target="${1:-}"
    local supplied="${2:-}"
    local target_dir="" old_ifs="" item="" root=""
    target_dir="$(agentstack_physical_dir "$target" 2>/dev/null || true)"
    [ -n "$target_dir" ] || return 1
    old_ifs="$IFS"
    IFS=:
    for item in $supplied; do
        root="$(agentstack_physical_dir "$item" 2>/dev/null || true)"
        case "$target_dir" in
            "$root"|"$root"/*)
                [ -n "$root" ] || continue
                IFS="$old_ifs"
                return 0
                ;;
        esac
    done
    IFS="$old_ifs"
    return 1
}

# Runtime protected roots are derived from the actual target.  A linked
# worktree needs both its own root (so edits are guarded) and the canonical main
# checkout (so the repository identity remains visible), with the worktree
# first so relative reservation paths normalize identically across worktrees.
agentstack_resolve_runtime_protected_roots() {
    local resolved_project_key="$1"
    local target="${2:-}"
    local env_file="${3:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local worktree="" repository="" installed="" installed_key=""
    local ambient_key="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
    # Git detection is optional and wins outright, so a Git target never
    # depends on ambient normalization or installed env reads below.
    worktree="$(agentstack_git_worktree_root "$target" 2>/dev/null || true)"
    repository="$(agentstack_repository_key "$target" 2>/dev/null || true)"
    if [ -n "$worktree" ]; then
        if [ -n "$repository" ] && [ "$repository" != "$worktree" ]; then
            printf '%s:%s\n' "$worktree" "$repository"
        else
            printf '%s\n' "$worktree"
        fi
        return 0
    fi

    if [ -n "${AGENTSTACK_PROTECTED_ROOTS:-}" ]; then
        if [ -n "$ambient_key" ]; then
            ambient_key="$(agentstack_normalize_project_key "$ambient_key")" \
                || return $?
        fi
        if [ -n "$ambient_key" ] && [ "$ambient_key" = "$resolved_project_key" ]; then
            agentstack_merge_non_git_roots "$target" "$AGENTSTACK_PROTECTED_ROOTS"
        else
            # A different explicit selection must not retain roots from the
            # ambient project.  The selected workspace is the only root whose
            # ownership is known at this boundary.
            agentstack_merge_non_git_roots "$target" ""
        fi
        return 0
    fi
    installed="$(agentstack_installed_env_value AGENTSTACK_PROTECTED_ROOTS "$env_file")" \
        || return $?
    if [ -n "$installed" ]; then
        installed_key="$(agentstack_installed_env_value AGENTSTACK_PROJECT_KEY "$env_file")" \
            || return $?
        if [ -n "$installed_key" ]; then
            installed_key="$(agentstack_normalize_project_key "$installed_key")" \
                || return $?
        fi
    fi
    if [ -n "$installed" ] && { [ "$installed_key" = "$resolved_project_key" ] || \
        agentstack_roots_cover_target "$target" "$installed"; }; then
        agentstack_merge_non_git_roots "$target" "$installed"
        return 0
    fi
    if [ -d "$target" ]; then
        agentstack_merge_non_git_roots "$target" ""
    else
        printf '%s\n' "$resolved_project_key"
    fi
}

# A live project selection must not inherit roots from an older installed
# project. Only sessions relying on the installed project key inherit the
# installed protected-root set.
agentstack_resolve_protected_roots() {
    local resolved_project_key="$1"
    local live_project_key="${2:-}"
    local env_file="${3:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local installed=""
    if [ -n "${AGENTSTACK_PROTECTED_ROOTS:-}" ]; then
        printf '%s\n' "$AGENTSTACK_PROTECTED_ROOTS"
        return 0
    fi
    if [ -n "$live_project_key" ]; then
        printf '%s\n' "$resolved_project_key"
        return 0
    fi
    installed="$(agentstack_installed_env_value AGENTSTACK_PROTECTED_ROOTS "$env_file")" \
        || return $?
    printf '%s\n' "${installed:-$resolved_project_key}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        resolve-project-key)
            shift
            agentstack_resolve_project_key "${1:-}" "${2:-}" "${3:-1}" "${4:-}"
            ;;
        repository-key)
            shift
            agentstack_repository_key "${1:-${PWD:-}}"
            ;;
        worktree-root)
            shift
            agentstack_git_worktree_root "${1:-${PWD:-}}"
            ;;
        resolve-runtime-protected-roots)
            shift
            agentstack_resolve_runtime_protected_roots "${1:-}" "${2:-}" "${3:-}"
            ;;
        installed-env-value)
            shift
            agentstack_installed_env_value "${1:-}" "${2:-}"
            ;;
        *)
            printf 'usage: project-context.sh {resolve-project-key TARGET [ENV_FILE [USE_CWD [EXPLICIT_KEY]]]|repository-key [TARGET]|worktree-root [TARGET]|resolve-runtime-protected-roots PROJECT_KEY TARGET [ENV_FILE]|installed-env-value NAME [ENV_FILE]}\n' >&2
            exit 2
            ;;
    esac
fi
