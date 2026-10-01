#!/bin/bash
# Which `codex` a launcher may run. Keep compatible with macOS /bin/bash 3.2.
#
# Under WSL, PATH also carries the Windows PATH (/mnt/c/...). A `codex` found
# there is the Windows npm shim: run by the Linux node it dies at once with
# "Missing optional dependency @openai/codex-linux-x64". A candidate is
# therefore used only if it is not under a Windows drive mount (WSL only) and
# answers `--version` within a short time. These are the same rules as
# scripts/install.sh ("codex launcher resolution") and scripts/doctor.sh.
#
# Sourcing defines functions and two settings only; it runs nothing. A caller
# that runs codex under another PATH sets CODEX_PROBE_RUNNER to a command that
# runs its arguments that way, so the probe sees what the launch will see.
WSL_WINDOWS_MOUNT_ROOT=/mnt
CODEX_VERSION_TIMEOUT_SECONDS=10
# All probes of one resolution share this many seconds (codex_probe_budget_start);
# each probe gets at most what is left. A candidate is judged once per
# resolution: CODEX_JUDGED holds ":path:" for every path already probed.
CODEX_PROBE_BUDGET_SECONDS=15
CODEX_PROBE_DEADLINE=""
CODEX_JUDGED=":"
CODEX_POLICY_FD=""

codex_probe_budget_start() {
  CODEX_PROBE_DEADLINE=$((SECONDS + CODEX_PROBE_BUDGET_SECONDS))
  CODEX_JUDGED=":"
}

running_under_wsl() {
  [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null
}

# Succeeds when `$1 --version` exits 0 within the timeout (portable: no
# coreutils `timeout` on macOS).
codex_version_answers() {
  local bin="$1" pid secs="$CODEX_VERSION_TIMEOUT_SECONDS" end started err="" status=0
  # Why it failed, for codex_version_failure (#121): running out of time and
  # exiting at once with an error need different fixes.
  CODEX_VERSION_STATUS=""
  CODEX_VERSION_ERROR=""
  CODEX_VERSION_ELAPSED=""
  CODEX_VERSION_LIMIT=""
  if [[ -n "$CODEX_PROBE_DEADLINE" ]] && (( CODEX_PROBE_DEADLINE - SECONDS < secs )); then
    secs=$((CODEX_PROBE_DEADLINE - SECONDS))
  fi
  CODEX_VERSION_LIMIT="$secs"
  if (( secs < 1 )); then
    CODEX_VERSION_STATUS=timeout
    return 1
  fi
  started=$SECONDS
  # Wall-clock, not a count of sleeps: the limit holds whatever `sleep` does.
  end=$((SECONDS + secs))
  # CODEX_PROBE_RUNNER, when set, runs the candidate the way the launcher will
  # (spawn_child.sh: the child's login shell and PATH setup).
  # A policy caller captures each probe over a pipe, never a temporary file.
  if [[ -n "${CODEX_POLICY_FD:-}" ]]; then
    printf '\0' >&3
    ${CODEX_PROBE_RUNNER:-} "$bin" --version </dev/null >&3 2>/dev/null &
  else
    # The first stderr line goes into the warning; the policy path above
    # keeps to its pipe and reports the exit status alone.
    err="$(mktemp "${TMPDIR:-/tmp}/agentstack-codex-stderr.XXXXXX" 2>/dev/null || true)"
    ${CODEX_PROBE_RUNNER:-} "$bin" --version </dev/null >"${CODEX_VERSION_OUTPUT:-/dev/null}" 2>"${err:-/dev/null}" &
  fi
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if (( SECONDS >= end )); then
      codex_probe_stop "$pid"
      CODEX_VERSION_STATUS=timeout
      [[ -n "$err" ]] && rm -f "$err"
      return 1
    fi
    sleep 0.1
  done
  wait "$pid" || status=$?
  CODEX_VERSION_STATUS="$status"
  CODEX_VERSION_ELAPSED=$((SECONDS - started))
  if [[ -n "$err" ]]; then
    CODEX_VERSION_ERROR="$(sed -n '/[^[:space:]]/{p;q;}' "$err" 2>/dev/null | cut -c1-200 || true)"
    rm -f "$err"
  fi
  return "$status"
}

# One line saying why the last codex_version_answers failed.
codex_version_failure() {
  local bin="$1"
  if [[ "$CODEX_VERSION_STATUS" == timeout ]]; then
    echo "'$bin --version' did not finish within ${CODEX_VERSION_LIMIT:-$CODEX_VERSION_TIMEOUT_SECONDS}s and was stopped"
  else
    echo "'$bin --version' exited with status ${CODEX_VERSION_STATUS:-unknown} after ${CODEX_VERSION_ELAPSED:-0}s: ${CODEX_VERSION_ERROR:-(no error output)}"
  fi
}

# Stop a probe that overran. Every wait here is a kill -0 poll with a
# deadline (one second of grace, one second for the probe to go after KILL),
# so waiting on `codex --version` and on the cleanup after it is bounded. The
# system commands used on the way (pgrep, ps) are not bounded: if they
# themselves stop responding, so does this function.
#
# The probe itself ($!) is this shell's own child, so it gets TERM and then
# KILL without further checks. Its descendants (an npm wrapper's native codex)
# are recorded first, while they are still attached to it, because a wrapper
# that exits on TERM orphans them out of reach of `pgrep -P`. They get TERM
# too; a descendant seen gone during the grace period is dropped, and KILL goes
# only to one whose `ps -o lstart=` start time (one-second resolution) still
# matches what was recorded. This is a best-effort identity check: a PID reused
# within the same second is not told apart. A still-running descendant whose
# start time cannot be read or no longer matches is left without KILL, with a
# note on stderr; one that received KILL is not checked again. A probe still present after its second of reaping is left with a note.
codex_probe_stop() {
  local pid="$1" level="$1" descendants="" next p t start depth round
  # PID lists are space-separated; a caller may have narrowed IFS (the PATH
  # scan in find_usable_codex_bin splits on ":").
  local IFS=$' \t\n'
  if command -v pgrep >/dev/null 2>&1; then
    for depth in 1 2 3 4; do
      next=""
      for p in $level; do
        next="$next $(pgrep -P "$p" 2>/dev/null | tr '\n' ' ' || true)"
      done
      next="$(echo $next)"
      [[ -n "$next" ]] || break
      for p in $next; do
        descendants="$descendants $p/$(codex_process_start "$p")"
      done
      level="$next"
    done
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for t in $descendants; do
    kill -TERM "${t%%/*}" 2>/dev/null || true
  done
  # One second of grace for everything to exit on TERM.
  for round in 1 2 3 4 5 6 7 8 9 10; do
    next=""
    for t in $descendants; do
      kill -0 "${t%%/*}" 2>/dev/null && next="$next $t"
    done
    descendants="$(echo $next)"
    if [[ -z "$descendants" ]] && ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  kill -KILL "$pid" 2>/dev/null || true
  for t in $descendants; do
    p="${t%%/*}"
    start="${t#*/}"
    if [[ -n "$start" && "$(codex_process_start "$p")" == "$start" ]]; then
      kill -KILL "$p" 2>/dev/null || true
    elif kill -0 "$p" 2>/dev/null; then
      echo "note: left process $p from the codex --version probe (its identity could not be confirmed)" >&2
    fi
  done
  # Reap the probe only once it is gone; never block on it.
  for round in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "note: the codex --version probe (pid $pid) did not exit after KILL; leaving it" >&2
  else
    wait "$pid" 2>/dev/null || true
  fi
}

# The process start time as one word (empty when ps cannot tell), used to
# recognise the same process again under the same PID.
codex_process_start() {
  ps -o lstart= -p "$1" 2>/dev/null | tr -d ' \n' || true
}

# Prints why a codex candidate cannot be used; prints nothing when it can.
codex_bin_problem() {
  local bin="$1"
  if running_under_wsl && [[ "$bin" == "$WSL_WINDOWS_MOUNT_ROOT"/?/* ]]; then
    echo "it is a Windows install under $WSL_WINDOWS_MOUNT_ROOT (install Codex inside WSL; the Windows npm shim cannot run here)"
    return 0
  fi
  if [[ ! -x "$bin" ]]; then
    echo "it is not executable"
    return 0
  fi
  if [[ -n "$CODEX_PROBE_DEADLINE" ]] && (( SECONDS >= CODEX_PROBE_DEADLINE )); then
    echo "not probed: the ${CODEX_PROBE_BUDGET_SECONDS}s budget for trying codex candidates is spent"
    return 0
  fi
  if ! codex_version_answers "$bin"; then
    codex_version_failure "$bin"
  fi
}

# First usable `codex` in a colon-separated search path. Each rejected
# candidate is reported on stderr with its reason, so a launcher error can say
# what it tried.
find_usable_codex_bin_in() {
  local search="$1" dir candidate problem seen=":"
  local IFS=:
  for dir in $search; do
    [[ -n "$dir" && "$seen" != *":$dir:"* ]] || continue
    seen="$seen$dir:"
    candidate="$dir/codex"
    [[ -f "$candidate" || -L "$candidate" ]] || continue
    # Already judged in this resolution (say, the env.sh value): not again.
    [[ "$CODEX_JUDGED" != *":$candidate:"* ]] || continue
    CODEX_JUDGED="$CODEX_JUDGED$candidate:"
    problem="$(codex_bin_problem "$candidate")"
    if [[ -z "$problem" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    echo "note: skipping codex $candidate: $problem" >&2
  done
  return 0
}

# Shared child execution context and binary selection. The spawner and the
# read-only model-policy helper below call these same functions.

codex_launch_shell() {
    local shell="${AGENTSTACK_CHILD_SHELL:-}"
    if [[ -n "$shell" && -x "$shell" ]]; then
        printf '%s\n' "$shell"
        return 0
    fi
    shell="$(command -v zsh 2>/dev/null || true)"
    [[ -z "$shell" ]] && shell="$(command -v bash 2>/dev/null || true)"
    if [[ -z "$shell" ]]; then
        echo "Error: neither zsh nor bash found for the child session; set AGENTSTACK_CHILD_SHELL" >&2
        return 1
    fi
    printf '%s\n' "$shell"
}

codex_launch_runner() {
    env CLAUDECODE=1 AGENTSTACK_RESERVED_IDENTITY=1 \
        "$CHILD_SHELL" -lc "$CODEX_CHILD_PATH_SETUP"'; exec "$0" "$@"' "$@"
}

codex_launch_search_path() {
    local extra="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.nodebrew/current/bin:/opt/homebrew/bin:/usr/local/bin"
    local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
    local candidate
    for candidate in "$nvm_dir"/versions/node/*/bin; do
        [[ -d "$candidate" ]] && extra="$extra:$candidate"
    done
    printf '%s\n' "$PATH:$extra"
}

codex_find_bin() {
    if [[ -n "${CODEX_BIN_PRIMED:-}" ]]; then
        printf '%s\n' "$CODEX_BIN_RESOLVED"
        return 0
    fi
    local codex_bin source problem
    declare -F codex_probe_budget_start >/dev/null && codex_probe_budget_start
    for source in environment env.sh; do
        if [[ "$source" == environment ]]; then
            codex_bin="${AGENTSTACK_CODEX_BIN:-}"
        elif declare -F agentstack_installed_env_value >/dev/null; then
            codex_bin="$(agentstack_installed_env_value AGENTSTACK_CODEX_BIN)"
        else
            codex_bin=""
        fi
        [[ -n "$codex_bin" ]] || continue
        [[ "${CODEX_JUDGED:-:}" != *":$codex_bin:"* ]] || continue
        CODEX_JUDGED="${CODEX_JUDGED:-:}$codex_bin:"
        problem="$(codex_bin_problem "$codex_bin")"
        if [[ -z "$problem" ]]; then
            printf '%s\n' "$codex_bin"
            return 0
        fi
        echo "note: skipping AGENTSTACK_CODEX_BIN from $source ($codex_bin): $problem" >&2
    done
    find_usable_codex_bin_in "$(codex_launch_search_path)"
}


# Read-only entrypoint: no registration, mailbox, tmux or application launch.
# NUL framing keeps paths and version output separate without shell evaluation.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    [[ "${1:-}" == policy ]] || { echo "usage: codex-bin.sh policy" >&2; exit 2; }
    set -euo pipefail
    context="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/project-context.sh"
    [[ -f "$context" ]] && . "$context"
    CHILD_SHELL="$(codex_launch_shell)"
    CODEX_CHILD_PATH_SETUP='export PATH="$HOME/.local/bin:$PATH"'
    CODEX_PROBE_RUNNER=codex_launch_runner
    # Keep the exact spawner defaults (10s / 15s): a slow but usable CLI
    # must not be discarded here and then accepted after preregistration.
    # FD 3 bypasses the nested command substitutions used for candidate
    # errors. Probe boundaries let the reader select only the final candidate
    # output, while failed candidates and timeout handling remain unchanged.
    exec 3>&1
    CODEX_POLICY_FD=3
    printf 'policy-v2\0'
    binary="$(codex_find_bin)"
    printf '\0%s\0%s' "$binary" "$CHILD_SHELL"
fi
