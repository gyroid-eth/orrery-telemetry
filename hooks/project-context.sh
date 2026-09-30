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

# Settings a person chooses at install time, by the name env.sh records them
# under. Each one takes its value in this order, and only this order:
#
#   explicit (an installer option or a variable set before the command ran)
#   > the previous install's env.sh
#   > the product default
#
# The installer resolves each with agentstack_resolve_setting; the launchers
# load env.sh with agentstack_load_installed_env, which keeps an explicit value
# instead of letting env.sh overwrite it (issues #33 and #137).
AGENTSTACK_INHERITED_SETTINGS="
AGENTSTACK_PROJECT_KEY
AGENTSTACK_PROTECTED_ROOTS
AGENTSTACK_PORT
AGENTSTACK_LABEL_PREFIX
AGENTSTACK_MAIL_LAUNCHD_LABEL
AGENTSTACK_TERMINAL
AGENTSTACK_MCP_URL
AGENTSTACK_PATH
AGENTSTACK_PYTHON
AGENTSTACK_MAIL_STATE_ROOT
AGENTSTACK_MAIL_DIR
AGENTSTACK_MAIL_MANAGEMENT_SOCKET
AGENTSTACK_LANG
AGENTSTACK_MURMUR
AGENTSTACK_DELIVERABLE_ROOTS
AGENTSTACK_VAULT
AGENTSTACK_MANAGED_AGENTS_FILE
AGENTSTACK_DASHBOARD_LOG
AGENTSTACK_DASHBOARD_LOG_MAX_BYTES
AGENTSTACK_DASHBOARD_LOG_BACKUPS
AGENTSTACK_DASHBOARD_RESTART_DELAY
AGENTSTACK_AUTO_OPEN_CHILD
AGENTSTACK_SPAWN_DIRS
AGENTSTACK_SPAWN_ROOTS
AGENTSTACK_WORKTREE_ROOT
AGENTSTACK_CODEX_CHILD_APPROVAL
AGENTSTACK_CODEX_CHILD_CONFIG_OVERLAY
AGENTSTACK_CODEX_NETWORK
AGENTSTACK_CODEX_ADD_DIRS
AGENTSTACK_CHILD_RESUME_RETENTION_DAYS
AGENTSTACK_CODEX_BIN
AGENTSTACK_PORTRAITS_DIR
AGENTSTACK_CUSTOM_PORTRAITS
AGENTSTACK_CLAUDE_MODELS
AGENTSTACK_CODEX_MODELS
"

# The order itself, with no I/O: EXPLICIT, else INSTALLED, else DEFAULT.
# An empty value counts as not given.
agentstack_pick_setting() {
    if [ -n "${1:-}" ]; then
        printf '%s\n' "$1"
    elif [ -n "${2:-}" ]; then
        printf '%s\n' "$2"
    else
        printf '%s\n' "${3:-}"
    fi
}

# NAME EXPLICIT DEFAULT [ENV_FILE]: the value NAME takes, reading the previous
# install's env.sh (never sourcing it) only when nothing explicit was given.
agentstack_resolve_setting() {
    local name="$1" explicit="${2:-}" default="${3:-}"
    local env_file="${4:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local installed=""
    if [ -z "$explicit" ]; then
        installed="$(agentstack_installed_env_value "$name" "$env_file")"
    fi
    agentstack_pick_setting "$explicit" "$installed" "$default"
}

# Source env.sh the way the launchers always have, but keep every setting
# above that was already set: env.sh is the previous install's value, not a
# choice made for this command. A live project key (AGENTSTACK_PROJECT_KEY or
# the legacy PROJECT_KEY, as agentstack_resolve_project_key reads them) also
# keeps env.sh from supplying the installed project's protected roots.
agentstack_load_installed_env() {
    local env_file="${1:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local name value count=0 i=0 live_project_key="" live_protected_roots=""
    local names=() values=()
    [ -f "$env_file" ] || return 0
    for name in $AGENTSTACK_INHERITED_SETTINGS; do
        value="${!name:-}"
        if [ -n "$value" ]; then
            names[$count]="$name"
            values[$count]="$value"
            count=$((count + 1))
        fi
    done
    live_project_key="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
    live_protected_roots="${AGENTSTACK_PROTECTED_ROOTS:-}"
    # shellcheck disable=SC1090
    . "$env_file"
    while [ "$i" -lt "$count" ]; do
        name="${names[$i]}"
        value="${!name:-}"
        export "$name=$(agentstack_pick_setting "${values[$i]}" "$value")"
        i=$((i + 1))
    done
    if [ -n "$live_project_key" ]; then
        export AGENTSTACK_PROJECT_KEY="$live_project_key"
        # What env.sh just set belongs to the installed project, not this one.
        AGENTSTACK_PROTECTED_ROOTS="$live_protected_roots"
        value="$(agentstack_resolve_protected_roots \
            "$live_project_key" "$live_project_key" "$env_file")"
        export AGENTSTACK_PROTECTED_ROOTS="$value"
    fi
    return 0
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
        resolve-project-key)
            shift
            agentstack_resolve_project_key "${1:-}" "${2:-}" "${3:-1}"
            ;;
        installed-env-value)
            shift
            agentstack_installed_env_value "${1:-}" "${2:-}"
            ;;
        *)
            printf 'usage: project-context.sh {resolve-project-key FALLBACK [ENV_FILE [USE_CWD]]|installed-env-value NAME [ENV_FILE]}\n' >&2
            exit 2
            ;;
    esac
fi
