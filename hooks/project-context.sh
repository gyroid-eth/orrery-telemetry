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

agentstack_physical_dir() {
    [ -n "${1:-}" ] && [ -d "$1" ] || return 1
    (CDPATH= cd -- "$1" 2>/dev/null && pwd -P)
}

# Resolve one top-level invocation from its actual target directory. Ambient /
# installed project keys and inherited Git selectors are deliberately not
# consulted. An explicit key changes only the Mail namespace; repository,
# worktree and protected-root provenance still come from TARGET.
agentstack_resolve_invocation_context() {
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || return 2
    local target="$1" explicit_key="${2:-}"
    local work_dir="" worktree_root="" common="" common_abs=""
    local repository="" project_key=""

    work_dir="$(agentstack_physical_dir "$target")" || {
        printf 'agentstack: invocation target must be an existing directory\n' >&2
        return 1
    }

    worktree_root="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$work_dir" rev-parse --show-toplevel 2>/dev/null)" || worktree_root=""
    common="$(env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR \
        git -C "$work_dir" rev-parse --git-common-dir 2>/dev/null)" || common=""
    if [ -z "$worktree_root" ] || [ -z "$common" ]; then
        # Do not reinterpret a broken repository marker as an ordinary non-Git
        # workspace and then authorize an unrelated explicit namespace.
        if [ -e "$work_dir/.git" ] || [ -L "$work_dir/.git" ]; then
            printf 'agentstack: cannot resolve repository metadata for invocation target\n' >&2
            return 1
        fi
        worktree_root=""
        common=""
    else
        worktree_root="$(agentstack_physical_dir "$worktree_root")" || return 1
        case "$common" in
            /*) common_abs="$(agentstack_physical_dir "$common")" || return 1 ;;
            *) common_abs="$(agentstack_physical_dir "$work_dir/$common")" || return 1 ;;
        esac
        if [ "$(basename "$common_abs")" = ".git" ]; then
            repository="$(dirname "$common_abs")"
        else
            repository="$common_abs"
        fi
    fi

    if [ -n "$explicit_key" ]; then
        if [ -d "$explicit_key" ]; then
            project_key="$(agentstack_physical_dir "$explicit_key")" || return 1
        else
            project_key="$explicit_key"
        fi
    elif [ -n "$repository" ]; then
        project_key="$repository"
    else
        project_key="$work_dir"
    fi

    "${AGENTSTACK_PYTHON:-python3}" - \
        "$project_key" "$repository" "$work_dir" "$worktree_root" <<'PY'
import json
import sys

project_key, repository, work_dir, worktree_root = sys.argv[1:]
print(json.dumps({
    "project_key": project_key,
    "repository_key": repository or None,
    "work_dir": work_dir,
    "worktree_root": worktree_root or None,
    "protected_roots": [worktree_root or work_dir],
}, separators=(",", ":")))
PY
}

# Priority: live AGENTSTACK_PROJECT_KEY, live PROJECT_KEY, installed env, cwd.
# Legacy consumers retain this behavior; top-level launchers use the invocation
# context function above instead.
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
        resolve-project-key)
            shift
            agentstack_resolve_project_key "${1:-}" "${2:-}" "${3:-1}"
            ;;
        installed-env-value)
            shift
            agentstack_installed_env_value "${1:-}" "${2:-}"
            ;;
        *)
            printf 'usage: project-context.sh {resolve-invocation-context TARGET [EXPLICIT_KEY]|resolve-project-key FALLBACK [ENV_FILE [USE_CWD]]|installed-env-value NAME [ENV_FILE]}\n' >&2
            exit 2
            ;;
    esac
fi
