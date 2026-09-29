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
# Sourcing defines functions and two settings only; it runs nothing.
WSL_WINDOWS_MOUNT_ROOT=/mnt
CODEX_VERSION_TIMEOUT_SECONDS=10

running_under_wsl() {
  [[ -r /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null
}

# Succeeds when `$1 --version` exits 0 within the timeout (portable: no
# coreutils `timeout` on macOS).
codex_version_answers() {
  local bin="$1" pid tick=0 limit=$((CODEX_VERSION_TIMEOUT_SECONDS * 10))
  "$bin" --version </dev/null >/dev/null 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [[ "$tick" -ge "$limit" ]]; then
      codex_probe_stop "$pid"
      return 1
    fi
    sleep 0.1
    tick=$((tick + 1))
  done
  wait "$pid"
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
  if ! codex_version_answers "$bin"; then
    echo "'$bin --version' did not succeed within ${CODEX_VERSION_TIMEOUT_SECONDS}s"
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
    problem="$(codex_bin_problem "$candidate")"
    if [[ -z "$problem" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    echo "note: skipping codex $candidate: $problem" >&2
  done
  return 0
}
