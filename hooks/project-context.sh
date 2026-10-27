#!/bin/bash
# Shared project-key/protected-root resolution for hooks and installed helpers.
# Keep this file compatible with macOS /bin/bash 3.2.

# Capture the source location while sourcing, before zsh's $0 becomes the
# calling function name. The strict reader must not depend on the caller's cwd.
if [ -n "${BASH_SOURCE:-}" ]; then
    _ags_project_context_src="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    eval '_ags_project_context_src=${(%):-%x}'
else
    _ags_project_context_src="$0"
fi
case "$_ags_project_context_src" in
    */*) _AGS_PROJECT_CONTEXT_DIR="${_ags_project_context_src%/*}" ;;
    *) _AGS_PROJECT_CONTEXT_DIR=. ;;
esac
_AGS_PROJECT_CONTEXT_DIR="$(CDPATH= cd -- "$_AGS_PROJECT_CONTEXT_DIR" && pwd -P)"
unset _ags_project_context_src

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
    local reader="${AGENTSTACK_PYTHON:-python3}"
    command -v "$reader" >/dev/null 2>&1 || reader=python3
    command -v "$reader" >/dev/null 2>&1 || {
        printf 'agentstack: Python is required to read installed configuration\n' >&2
        return 1
    }
    case "$name" in
        AGENTSTACK_EXTRA_PROTECTED_ROOTS|AGENTSTACK_PROTECTED_ROOTS)
            "$reader" "$_AGS_PROJECT_CONTEXT_DIR/installed-env.py" "${3:-value}" "$name" "$env_file"
            return $?
            ;;
    esac
    agentstack_literal_installed_env_value "$name" "$env_file" "$reader" "${3:-}"
}

# Historical non-executing lookup for direct hooks and ordinary settings.
# Existing sessions may have literal ROOTS beside unrelated shell logic. Read
# only that literal declaration; never source/evaluate the surrounding file.
# Managed launches and installer migration keep using the strict wrapper above.
agentstack_literal_installed_env_value() {
    local name="$1"
    local env_file="${2:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local reader="${3:-${AGENTSTACK_PYTHON:-python3}}"
    case "$name" in
        *[!A-Z0-9_]*|"") return 0 ;;
    esac
    [ -f "$env_file" ] || return 0
    command -v "$reader" >/dev/null 2>&1 || reader=python3
    "$reader" - "$env_file" "$name" "${4:-}" <<'PY' 2>/dev/null || { [[ "${4:-}" != --shell ]]; }
import pathlib
import re
import shlex
import sys

path = pathlib.Path(sys.argv[1])
name = sys.argv[2]
shell = sys.argv[3] == '--shell'
def invalid():
    if shell: print('false', end='')
    raise SystemExit(0)
try:
    raw = path.read_text(encoding="utf-8")
except (OSError, UnicodeError):
    invalid()

pattern = re.compile(r"^\s*export\s+" + re.escape(name) + r"=(.*)$")
for line in raw.splitlines():
    match = pattern.match(line)
    if match is None:
        continue
    try:
        values = shlex.split(match.group(1), comments=True, posix=True)
    except ValueError:
        invalid()
    if shell:
        if not values and not match.group(1).strip(): values = ['']
        if len(values) != 1: invalid()
        print('export ' + name + '=' + shlex.quote(values[0]), end='')
    elif len(values) == 1:
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
AGENTSTACK_RUNTIME_MODE
AGENTSTACK_GLOBAL_SERVER_CONFIG
AGENTSTACK_PROJECT_KEY
AGENTSTACK_PROTECTED_ROOTS
AGENTSTACK_EXTRA_PROTECTED_ROOTS
AGENTSTACK_PORT
AGENTSTACK_DASHBOARD_ALLOWED_ORIGINS
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
AGENTSTACK_CHILD_DEFAULT_TOOLS
AGENTSTACK_CODEX_ADD_DIRS
AGENTSTACK_CHILD_RESUME_RETENTION_DAYS
AGENTSTACK_CODEX_BIN
AGENTSTACK_PORTRAITS_DIR
AGENTSTACK_CUSTOM_PORTRAITS
AGENTSTACK_CLAUDE_MODELS
AGENTSTACK_CODEX_MODELS
AGENTSTACK_CODEX_DEFAULT_MODEL
AGENTSTACK_CODEX_DEFAULT_EFFORT
AGENTSTACK_DSH_NODE
AGENTSTACK_MANAGED_SPAWN_CONFIG
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
    local names=() values=() env_status=0
    [ -f "$env_file" ] || return 0
    for name in $AGENTSTACK_INHERITED_SETTINGS; do
        value="${!name:-}"
        if [ -n "$value" ] || { [ "$name" = AGENTSTACK_EXTRA_PROTECTED_ROOTS ] && [ "${AGENTSTACK_EXTRA_PROTECTED_ROOTS+x}" = x ]; }; then
            names[$count]="$name"
            values[$count]="$value"
            count=$((count + 1))
        fi
    done
    live_project_key="${AGENTSTACK_PROJECT_KEY:-${PROJECT_KEY:-}}"
    live_protected_roots="${AGENTSTACK_PROTECTED_ROOTS:-}"
    # shellcheck disable=SC1090
    . "$env_file" || env_status=$?
    while [ "$i" -lt "$count" ]; do
        name="${names[$i]}"
        value="${!name:-}"
        if [ "$name" = AGENTSTACK_EXTRA_PROTECTED_ROOTS ]; then
            export "$name=${values[$i]}"
        else
            export "$name=$(agentstack_pick_setting "${values[$i]}" "$value")"
        fi
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
    return "$env_status"
}

# Explicit extras are configuration; PROTECTED_ROOTS is a runtime result.
# Presence, not non-emptiness, matters: EXTRA_PROTECTED_ROOTS= clears installed
# extras. Never infer extras from a legacy root list or a path-shaped namespace.
agentstack_resolve_extra_protected_roots() {
    local env_file="${1:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    if [ "${AGENTSTACK_EXTRA_PROTECTED_ROOTS+x}" = x ]; then
        printf '%s' "$AGENTSTACK_EXTRA_PROTECTED_ROOTS"
    else
        agentstack_installed_env_value AGENTSTACK_EXTRA_PROTECTED_ROOTS "$env_file"
    fi
}

# Warn about discarded legacy inputs, but do not repeat a warning when the
# bootstrap resolves the exact tuple the top-level launcher just exported.
# EXTRAS marks an installed legacy value as migrated; it does not legitimize
# a different inherited ROOTS value from an older shell or tmux server.
agentstack_warn_legacy_protected_roots() {
    local project="$1" repository="$2" work_dir="$3" worktree="$4" roots="$5" extras="$6"
    local env_file="${7:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local inherited="${AGENTSTACK_PROTECTED_ROOTS:-}" installed="" legacy="" extras_present=""
    if [ "${AGENTSTACK_PROTECTION_CONTEXT:-}" = workspace-v1 ] &&
       [ "${AGENTSTACK_PROJECT_KEY:-}" = "$project" ] &&
       [ "${PROJECT_KEY:-}" = "$project" ] &&
       [ "${AGENTSTACK_PROJECT_REPOSITORY:-}" = "$repository" ] &&
       [ "${AGENTSTACK_PROJECT_WORK_DIR:-}" = "$work_dir" ] &&
       [ "${AGENTSTACK_PROJECT_WORKTREE_ROOT:-}" = "$worktree" ] &&
       [ "${AGENTSTACK_EXTRA_PROTECTED_ROOTS:-}" = "$extras" ] &&
       [ "$inherited" = "$roots" ]; then
        return 0
    fi
    # Explicit extras with no incoming legacy roots need no diagnostic lookup.
    if [ -z "$inherited" ] && [ "${AGENTSTACK_EXTRA_PROTECTED_ROOTS+x}" = x ]; then
        return 0
    fi
    installed="$(agentstack_installed_env_value AGENTSTACK_PROTECTED_ROOTS "$env_file")" || return 1
    if [ -n "$inherited" ] && [ "$inherited" != "$installed" ]; then
        legacy="$inherited"
    else
        extras_present="${AGENTSTACK_EXTRA_PROTECTED_ROOTS+x}"
        if [ -z "$extras_present" ]; then
            extras_present="$(agentstack_installed_env_value AGENTSTACK_EXTRA_PROTECTED_ROOTS "$env_file" present)" || return 1
        fi
        if [ "$extras_present" != x ] && [ "$extras_present" != 1 ]; then
            legacy="${inherited:-$installed}"
        fi
    fi
    if [ -n "$legacy" ]; then
        printf 'agentstack: legacy AGENTSTACK_PROTECTED_ROOTS ignored for this launch: %s\n' "$legacy" >&2
        printf 'agentstack: run scripts/install.sh (install/update) for automatic migration of installed protection settings; inherited shell/tmux roots are ignored.\n' >&2
        printf 'agentstack: finish/release existing reservations before updating, then restart cooperating sessions together after the update.\n' >&2
    fi
}

agentstack_load_top_level_env() {
    local env_file="${1:-${AGENTSTACK_HOME:-$HOME/.agentstack}/env.sh}"
    local extras="" extras_present="${AGENTSTACK_EXTRA_PROTECTED_ROOTS+x}"
    local inherited_roots="${AGENTSTACK_PROTECTED_ROOTS:-}" inherited_roots_set="${AGENTSTACK_PROTECTED_ROOTS+x}"
    extras="$(agentstack_resolve_extra_protected_roots "$env_file" || exit $?; printf .)" || return 1
    extras="${extras%.}"
    if [ -z "$extras_present" ]; then
        extras_present="$(agentstack_installed_env_value AGENTSTACK_EXTRA_PROTECTED_ROOTS "$env_file" present)" || return 1
    fi
    # Reject ambiguous legacy root declarations before the generic source.
    agentstack_installed_env_value AGENTSTACK_PROTECTED_ROOTS "$env_file" >/dev/null || return 1
    if command -v ags_client_bootstrap >/dev/null 2>&1; then
        shift
        ags_client_bootstrap "$env_file" "$@" || return 1
    else
        agentstack_load_installed_env "$env_file" || return 1
    fi
    # The legacy generic loader remains available to direct hooks/installers.
    # Managed launches use only EXTRAS as config, and retain incoming ROOTS
    # solely for the warning before replacing it with the resolved workspace.
    if [ "$inherited_roots_set" = x ]; then
        export AGENTSTACK_PROTECTED_ROOTS="$inherited_roots"
    else
        unset AGENTSTACK_PROTECTED_ROOTS
    fi
    if [ "$extras_present" = x ] || [ "$extras_present" = 1 ]; then
        export AGENTSTACK_EXTRA_PROTECTED_ROOTS="$extras"
    else
        unset AGENTSTACK_EXTRA_PROTECTED_ROOTS
    fi
}

agentstack_physical_dir() {
    [ -n "${1:-}" ] && [ -d "$1" ] || return 1
    (CDPATH= cd -- "$1" 2>/dev/null && pwd -P)
}

# Resolve one top-level invocation into two independent dimensions:
# - project_key is the ORRERY Mail coordination namespace and follows the
#   caller-selected key, including empty. When omitted, the existing hook
#   resolver supplies its usual live > installed > cwd fallback;
# - repository/work_dir/worktree_root describe the actual target workspace.
# A namespace may intentionally span repositories. Workspace provenance and
# default protection comes from TARGET (not its Git root). The launcher separately retains
# configured extra roots; a namespace path is never workspace ownership proof.
agentstack_resolve_invocation_context() {
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || return 2
    local target="$1" explicit_key="${2:-}"
    local work_dir="" worktree_root="" common="" common_abs=""
    local repository="" project_key="" probe=""

    work_dir="$(agentstack_physical_dir "$target")" || {
        printf 'agentstack: invocation target must be an existing directory\n' >&2
        return 1
    }

    command -v git >/dev/null 2>&1 || {
        printf 'agentstack: git is required to resolve invocation target\n' >&2
        return 1
    }
    worktree_root="$(unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR
        git -C "$work_dir" rev-parse --show-toplevel 2>/dev/null)" || worktree_root=""
    common="$(unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR
        git -C "$work_dir" rev-parse --git-common-dir 2>/dev/null)" || common=""
    if [ -z "$worktree_root" ] || [ -z "$common" ]; then
        # Do not reinterpret a broken repository marker as an ordinary non-Git
        # workspace and then authorize an unrelated explicit namespace.
        # Check ancestors too: launching in a subdirectory of a broken
        # worktree must not turn it into an unowned non-Git workspace.
        probe="$work_dir"
        while :; do
            if [ -f "$probe/.git" ] || [ -L "$probe/.git" ] ||
               [ -e "$probe/.git/HEAD" ] || [ -e "$probe/.git/config" ] ||
               [ -d "$probe/.git/objects" ] || [ -d "$probe/.git/refs" ] ||
               { [ "$probe" = "$work_dir" ] && [ -d "$probe/.git" ]; } ||
               [ -n "$common" ]; then
                printf 'agentstack: cannot resolve repository metadata for invocation target\n' >&2
                return 1
            fi
            [ "$probe" != / ] || break
            probe="${probe%/*}"
            [ -n "$probe" ] || probe=/
        done
        worktree_root=""
        common=""
    else
        worktree_root="$(agentstack_physical_dir "$worktree_root")" || return 1
        case "$work_dir/" in
            "${worktree_root%/}/"*) ;;
            *)
                printf 'agentstack: invocation target is outside its resolved Git worktree\n' >&2
                return 1
                ;;
        esac
        case "$common" in
            /*) common_abs="$(agentstack_physical_dir "$common")" || return 1 ;;
            *) common_abs="$(agentstack_physical_dir "$work_dir/$common")" || return 1 ;;
        esac
        if [ "${common_abs##*/}" = ".git" ]; then
            repository="${common_abs%/*}"
            [ -n "$repository" ] || repository=/
        else
            repository="$common_abs"
        fi
    fi

    if [ "$#" -eq 2 ]; then
        # Mail project keys are opaque human keys. Even a path-shaped key is
        # namespace data, not proof that TARGET belongs to that repository.
        project_key="$explicit_key"
    else
        project_key="$(agentstack_resolve_project_key "$work_dir")" || return 1
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
    "protected_roots": [work_dir],
}, separators=(",", ":")))
PY
}

# Recompute the entire runtime tuple at every AI launch boundary. This is also
# used by child and resume launchers, independently of identity registration.
# Validate all data before exporting anything. Ordered extras precede the
# mandatory workspace to retain existing first-matching-root reservation names.
agentstack_apply_workspace_context() {
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || return 2
    local target="$1" selected_key="${2:-}" context="" extras="" decoded="" value=""
    local fields=()
    case "$target$selected_key" in
        *$'\n'*|*$'\r'*) printf 'agentstack: control characters cannot be passed to the launcher\n' >&2; return 1 ;;
    esac
    extras="$(agentstack_resolve_extra_protected_roots || exit $?; printf .)" || return 1
    extras="${extras%.}"
    if [ "$#" -eq 2 ]; then
        context="$(agentstack_resolve_invocation_context "$target" "$selected_key")" || return 1
    else
        context="$(agentstack_resolve_invocation_context "$target")" || return 1
    fi
    decoded="$("${AGENTSTACK_PYTHON:-python3}" - "$context" "$extras" <<'PYCONTEXT'
import json
import os
import sys

try:
    data = json.loads(sys.argv[1])
    fields = [data["project_key"], data["repository_key"] or "",
              data["work_dir"], data["worktree_root"] or ""]
    workspace_roots = data["protected_roots"]
    if not isinstance(workspace_roots, list) or not workspace_roots:
        raise ValueError("protected roots must be a nonempty array")
    if not all(isinstance(value, str) for value in fields + workspace_roots):
        raise ValueError("context fields must be strings")
    if not fields[2] or any(not root for root in workspace_roots):
        raise ValueError("context contains an empty workspace")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in value)
           for value in fields + workspace_roots + [sys.argv[2]]):
        raise ValueError("control characters cannot be passed to the launcher")
    if any(":" in root for root in workspace_roots):
        raise ValueError("protected roots containing ':' cannot use the legacy environment")
    extras = []
    for root in sys.argv[2].split(":"):
        if not root:
            continue
        root = os.path.expanduser(root)
        if not os.path.isabs(root):
            raise ValueError("extra protected roots must be absolute paths (or ~/ paths)")
        # Match the physical workspace coordinate system; collapse aliases and
        # trailing slashes before deduplication without changing root order.
        root = os.path.realpath(root)
        if ":" in root:
            raise ValueError("protected roots containing ':' cannot use the legacy environment")
        if root not in extras:
            extras.append(root)
    roots = list(extras)
    for root in workspace_roots:
        if root not in roots:
            roots.append(root)
    print("\n".join(fields + [":".join(roots), ":".join(extras), "end"]))
except (KeyError, TypeError, ValueError) as exc:
    print(f"agentstack: invalid launch context: {exc}", file=sys.stderr)
    raise SystemExit(1)
PYCONTEXT
)" || return 1
    while IFS= read -r value; do fields+=("$value"); done <<< "$decoded"
    [ "${#fields[@]}" -eq 7 ] || return 1
    agentstack_warn_legacy_protected_roots "${fields[0]}" "${fields[1]}" "${fields[2]}" \
        "${fields[3]}" "${fields[4]}" "${fields[5]}" || return 1
    AGENTSTACK_PROJECT_KEY="${fields[0]}"
    PROJECT_KEY="${fields[0]}"
    AGENTSTACK_PROJECT_REPOSITORY="${fields[1]}"
    AGENTSTACK_PROJECT_WORK_DIR="${fields[2]}"
    AGENTSTACK_PROJECT_WORKTREE_ROOT="${fields[3]}"
    AGENTSTACK_PROTECTED_ROOTS="${fields[4]}"
    AGENTSTACK_EXTRA_PROTECTED_ROOTS="${fields[5]}"
    export AGENTSTACK_PROJECT_KEY PROJECT_KEY AGENTSTACK_PROJECT_REPOSITORY
    export AGENTSTACK_PROJECT_WORK_DIR AGENTSTACK_PROJECT_WORKTREE_ROOT
    export AGENTSTACK_PROTECTED_ROOTS AGENTSTACK_EXTRA_PROTECTED_ROOTS
    export AGENTSTACK_PROTECTION_CONTEXT=workspace-v1
    unset AGENTSTACK_PROJECT_CONTEXT AGENTSTACK_LOOKUP_PROJECT_KEY
    unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR
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
    installed="$(agentstack_literal_installed_env_value AGENTSTACK_PROTECTED_ROOTS "$env_file")"
    printf '%s\n' "${installed:-$resolved_project_key}"
}

if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        resolve-invocation-context)
            shift
            agentstack_resolve_invocation_context "$@"
            ;;
        workspace-context-exports|workspace-context-json)
            output_mode="$1"
            shift
            agentstack_apply_workspace_context "$@" || exit 1
            "${AGENTSTACK_PYTHON:-python3}" - "$output_mode" <<'PYEXPORTS'
import json
import os
import shlex
import sys
names = ("AGENTSTACK_PROJECT_KEY", "PROJECT_KEY", "AGENTSTACK_PROJECT_REPOSITORY",
         "AGENTSTACK_PROJECT_WORK_DIR", "AGENTSTACK_PROJECT_WORKTREE_ROOT",
         "AGENTSTACK_PROTECTED_ROOTS", "AGENTSTACK_EXTRA_PROTECTED_ROOTS",
         "AGENTSTACK_PROTECTION_CONTEXT")
values = {name: os.environ[name] for name in names}
unset = ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "AGENTSTACK_PROJECT_CONTEXT", "AGENTSTACK_LOOKUP_PROJECT_KEY"]
if sys.argv[1] == "workspace-context-json":
    print(json.dumps({"environment": values, "unset": unset}))
else:
    print("unset " + " ".join(unset))
    for name, value in values.items():
        print("export " + name + "=" + shlex.quote(value))
PYEXPORTS
            ;;
        resolve-project-key)
            shift
            agentstack_resolve_project_key "${1:-}" "${2:-}" "${3:-1}"
            ;;
        installed-env-value)
            shift
            agentstack_installed_env_value "${1:-}" "${2:-}" "${3:-value}"
            ;;
        *)
            printf 'usage: project-context.sh {resolve-project-key FALLBACK [ENV_FILE [USE_CWD]]|installed-env-value NAME [ENV_FILE [value|present]]|resolve-invocation-context TARGET [KEY]|workspace-context-exports TARGET [KEY]|workspace-context-json TARGET [KEY]}\n' >&2
            exit 2
            ;;
    esac
fi
