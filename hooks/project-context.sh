#!/bin/bash
# Shared project-key/protected-root resolution for hooks and installed helpers.
# Keep this file compatible with macOS /bin/bash 3.2.

# Read one literal `export NAME=value` assignment without sourcing env.sh.
# The installer writes values with Python shlex.quote; shlex reverses that
# quoting without expanding variables, command substitutions, or shell code.
agentstack_installed_env_value() {
    local name="$1"
    local env_file="${2:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    case "$name" in
        *[!A-Z0-9_]*|"") return 0 ;;
    esac
    [ -f "$env_file" ] || return 0
    python3 - "$env_file" "$name" <<'PY' 2>/dev/null || true
import pathlib
import re
import shlex
import sys

path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
try:
    raw = path.read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit(0)

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

# Repository-aware primitives and an explicit invocation selector. The legacy
# resolver below stays unchanged until its launcher/registration consumers
# can be migrated together.
agentstack_physical_dir() {
    local directory="${1:-}"
    [ -n "$directory" ] && [ -d "$directory" ] || return 1
    (CDPATH= cd -- "$directory" 2>/dev/null && pwd -P)
}

# Explicit human keys are normalized as paths only when they are absolute or
# name an existing directory. In particular, an explicit linked-worktree key
# is not collapsed to its repository key and a logical key is not a pathname.
agentstack_normalize_project_key() {
    local project_key="${1:-}"
    local physical=""
    [ -n "$project_key" ] || {
        printf '\n'
        return 0
    }
    if [ -d "$project_key" ]; then
        physical="$(agentstack_physical_dir "$project_key")" || return 1
        printf '%s\n' "$physical"
        return 0
    fi
    case "$project_key" in
        /*)
            "${AGENTSTACK_PYTHON:-python3}" - "$project_key" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).resolve())
PY
            ;;
        *) printf '%s\n' "$project_key" ;;
    esac
}

# git -C alone does not override inherited GIT_DIR / GIT_WORK_TREE. Unset the
# repository selectors in the probe process only, never in the caller.
agentstack_git_worktree_root() {
    local target="${1:-}"
    local root=""
    [ -n "$target" ] && [ -d "$target" ] || return 1
    root="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || return 1
    agentstack_physical_dir "$root"
}

# Main and linked worktrees share a common Git directory. Use the main
# checkout as the human key for the usual .git layout; keep the physical
# common directory for bare/separate-git-dir layouts. A remote URL is not an
# identity: two independent clones of the same remote must stay separate.
agentstack_repository_key() {
    local target="${1:-}"
    local common="" common_abs=""
    [ -n "$target" ] && [ -d "$target" ] || return 1
    common="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$target" rev-parse --git-common-dir 2>/dev/null)" || return 1
    case "$common" in
        /*) common_abs="$(agentstack_physical_dir "$common")" || return 1 ;;
        *) common_abs="$(agentstack_physical_dir "$target/$common")" || return 1 ;;
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

# Internal tri-state probe: 0 = repository (stdout key), 2 = verified non-Git,
# 1 = inspection failure. Callers must distinguish 2 from all other failures.
_agentstack_probe_invocation_repository() {
    local target_dir="$1"
    local repository="" probe_dir="" parent_dir="" failure=""
    command -v git >/dev/null 2>&1 || {
        printf 'agentstack: git is required to resolve invocation context\n' >&2
        return 1
    }
    if repository="$(agentstack_repository_key "$target_dir")"; then
        printf '%s\n' "$repository"
        return 0
    fi

    # Distinguish Git's normal non-repository result from execution/IO errors.
    # The second probe is read-only; unexpected results fail rather than guess.
    if failure="$(LC_ALL=C env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$target_dir" rev-parse --git-common-dir 2>&1)"; then
        printf 'agentstack: repository identity changed or could not be normalized\n' >&2
        return 1
    fi
    case "$failure" in
        'fatal: not a git repository'*) ;;
        *)
            printf 'agentstack: git could not inspect invocation target\n' >&2
            return 1
            ;;
    esac

    # A failed probe is not proof of a non-Git workspace. In particular, do
    # not turn a broken/unreadable .git or bare repository into an install
    # fallback. Only a directory without repository markers may fall back.
    probe_dir="$target_dir"
    while :; do
        if [ -e "$probe_dir/.git" ] || [ -L "$probe_dir/.git" ] ||
           { [ -e "$probe_dir/HEAD" ] && [ -d "$probe_dir/objects" ]; }; then
            printf 'agentstack: cannot resolve repository metadata for invocation target\n' >&2
            return 1
        fi
        parent_dir="$(dirname "$probe_dir")"
        [ "$parent_dir" != "$probe_dir" ] || break
        probe_dir="$parent_dir"
    done
    return 2
}

# Select only from explicit inputs and the already inspected repository.
_agentstack_invocation_key_from_repository() {
    local target_dir="$1"
    local repository="$2"
    local explicit_key="${3:-}"
    local non_git_fallback="${4:-}"
    if [ -n "$explicit_key" ]; then
        agentstack_normalize_project_key "$explicit_key"
    elif [ -n "$repository" ]; then
        printf '%s\n' "$repository"
    elif [ -n "$non_git_fallback" ]; then
        agentstack_normalize_project_key "$non_git_fallback"
    else
        printf '%s\n' "$target_dir"
    fi
}

# Select a top-level invocation's key, never an inherited session identity.
# Preserve the key-only API's explicit selection without Git discovery.
agentstack_resolve_invocation_project_key() {
    local target="${1:-}"
    local explicit_key="${2:-}"
    local non_git_fallback="${3:-}"
    local target_dir="" repository="" probe_status=0
    target_dir="$(agentstack_physical_dir "$target")" || {
        printf 'agentstack: invocation target must be an existing directory\n' >&2
        return 1
    }
    if [ -n "$explicit_key" ]; then
        agentstack_normalize_project_key "$explicit_key"
        return $?
    fi
    if repository="$(_agentstack_probe_invocation_repository "$target_dir")"; then
        :
    else
        probe_status=$?
        [ "$probe_status" -eq 2 ] || return 1
        repository=""
    fi
    _agentstack_invocation_key_from_repository "$target_dir" "$repository" "" "$non_git_fallback"
}

# Read-only top-level workspace context. This is not a delegated ownership
# validator and must never export or attest AGENTSTACK_PROJECT_CONTEXT.
agentstack_resolve_invocation_context() {
    if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
        printf 'usage: resolve-invocation-context TARGET [EXPLICIT_KEY [NON_GIT_FALLBACK]]\n' >&2
        return 2
    fi
    local target="${1:-}"
    local explicit_key="${2:-}"
    local non_git_fallback="${3:-}"
    local work_dir="" repository="" worktree_root="" root_repository=""
    local project_key="" probe_status=0
    work_dir="$(agentstack_physical_dir "$target")" || {
        printf 'agentstack: invocation target must be an existing directory\n' >&2
        return 1
    }
    if repository="$(_agentstack_probe_invocation_repository "$work_dir")"; then
        worktree_root="$(agentstack_git_worktree_root "$work_dir")" || {
            printf 'agentstack: invocation context requires an inspectable Git worktree\n' >&2
            return 1
        }
        root_repository="$(agentstack_repository_key "$worktree_root")" || return 1
        [ "$root_repository" = "$repository" ] || {
            printf 'agentstack: invocation worktree and repository identity disagree\n' >&2
            return 1
        }
    else
        probe_status=$?
        [ "$probe_status" -eq 2 ] || return 1
        repository=""
    fi
    project_key="$(_agentstack_invocation_key_from_repository \
        "$work_dir" "$repository" "$explicit_key" "$non_git_fallback")" || return 1
    "${AGENTSTACK_PYTHON:-python3}" - "$project_key" "$repository" "$work_dir" "$worktree_root" <<'PY'
import json
from pathlib import Path
import sys

project_key, repository, work_dir, worktree_root = sys.argv[1:]
if worktree_root and not Path(work_dir).is_relative_to(Path(worktree_root)):
    print("agentstack: invocation target is outside its Git worktree", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps({
    "project_key": project_key,
    "repository_key": repository or None,
    "work_dir": work_dir,
    "worktree_root": worktree_root or None,
    "protected_roots": [worktree_root or work_dir],
}))
PY
}

# Priority: live AGENTSTACK_PROJECT_KEY, live PROJECT_KEY, installed env, cwd.
agentstack_resolve_project_key() {
    local fallback="${1:-}"
    local env_file="${2:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local use_cwd="${3:-1}"
    local installed=""
    if [ -n "${AGENTSTACK_PROJECT_KEY:-}" ]; then
        printf '%s\n' "$AGENTSTACK_PROJECT_KEY"
        return 0
    fi
    if [ -n "${PROJECT_KEY:-}" ]; then
        printf '%s\n' "$PROJECT_KEY"
        return 0
    fi
    installed="$(agentstack_installed_env_value AGENTSTACK_PROJECT_KEY "$env_file")"
    if [ -n "$installed" ]; then
        printf '%s\n' "$installed"
        return 0
    fi
    if [ -z "$fallback" ] && [ "$use_cwd" != "0" ]; then
        fallback="$(pwd -P)"
    fi
    printf '%s\n' "$fallback"
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
    installed="$(agentstack_installed_env_value AGENTSTACK_PROTECTED_ROOTS "$env_file")"
    printf '%s\n' "${installed:-$resolved_project_key}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        resolve-invocation-context)
            shift
            agentstack_resolve_invocation_context "$@"
            ;;
        resolve-invocation-project-key)
            shift
            if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
                printf 'usage: project-context.sh resolve-invocation-project-key TARGET [EXPLICIT_KEY [NON_GIT_FALLBACK]]\n' >&2
                exit 2
            fi
            agentstack_resolve_invocation_project_key "$1" "${2:-}" "${3:-}"
            ;;
        resolve-project-key)
            shift
            agentstack_resolve_project_key "${1:-}" "${2:-}" "${3:-1}"
            ;;
        installed-env-value)
            shift
            agentstack_installed_env_value "${1:-}" "${2:-}"
            ;;
        *)
            printf 'usage: project-context.sh {resolve-invocation-context TARGET [EXPLICIT_KEY [NON_GIT_FALLBACK]]|resolve-invocation-project-key TARGET [EXPLICIT_KEY [NON_GIT_FALLBACK]]|resolve-project-key FALLBACK [ENV_FILE [USE_CWD]]|installed-env-value NAME [ENV_FILE]}\n' >&2
            exit 2
            ;;
    esac
fi
