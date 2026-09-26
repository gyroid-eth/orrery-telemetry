# Updating a live ORRERY Mail service

> 日本語版: [agentstack-mail-update.md](agentstack-mail-update.md)

This document covers shipping a new build of `agentstack_mail` onto a machine
where the service is already serving traffic. It describes the layout that
`scripts/install.sh` has managed since the bundled service became the only
provider. The earlier hand-run layout under `cutover-maintenance/` is history:
nothing the installer writes reads it, but a machine that once used it may
still carry its units and pointer file, so the pre-flight below checks for
them instead of assuming they are gone.

**Scope.** The commands assume the default layout: install dir `~/.agentstack`,
no `--install-dir`, and no `AGENTSTACK_MAIL_*` overrides at install time. A
custom install is out of scope: the values this document reads from `env.sh`
are shell variables for your checks and are not passed on to the installer,
which would need its own `--install-dir` and overrides on every call, while
`agentstack-mailctl` locates `env.sh` through `AGENTSTACK_HOME`. None of that
has been exercised here. Note that `env.sh` records the service root as
`AGENTSTACK_MAIL_DIR`, while the installer's input for it is
`AGENTSTACK_MAIL_SERVICE_ROOT`.

**What has been executed.** The switch itself (hold the unit, stop, install,
verify: steps 5 to 7) was performed once on a live machine on 2026-09-18 with
the previous draft of this procedure; the 12 s outage is from that run. The
pre-flight (step 0) and the offline verification (step 3) were then re-run
exactly as written here against the installed service, and the stop
conditions of steps 2 and 3 were exercised in a harmless bash control with
stubbed `uv`, `git` and `lsof`, including a stale value left in the parent shell, a failing `git`, and a missing or failing `lsof`. Steps 4 to 7 as written here follow that live run
but have not been re-executed as a whole. The rollback section is read from
the installer's source and has not been executed.

## What the installer does, and does not do

`install.sh` updates hooks, skills, the dashboard, `bin/`, `env.sh` and the
autostart units on every run. For ORRERY Mail it takes one of two paths:

- **Something answers the configured endpoint.** The installer sends it a
  `health_check`. If the answer carries a `database_url` that resolves to the
  expected state database, the listener is adopted: its render is recorded in
  `env.sh` and the service is **not** touched. If the port is occupied but the
  occupant does not answer as ORRERY Mail, or serves a different database, the
  run stops with an error. This adoption path is every ordinary re-run, which
  is why a re-run alone never switches the Mail build. The dashboard's
  `/api/version` reports the package version, not the build behind the port.
- **Nothing answers.** The installer provisions a candidate for the checkout's
  exact commit (an absent candidate is built; an existing but incomplete one
  stops the run), renders a service env, starts the service through
  `agentstack-mailctl`, points `env.sh` and the autostart unit at the render,
  and checks that the served database is the shared one.

An update is therefore "stop the old service, then run the installer", with
the autostart unit held back so it cannot restart the old build in between.

## Layout

Resolve these from the installed `env.sh`, not from assumptions: the state
root defaults to `~/.agentstack/mail` independently of the install dir, and
the endpoint, label prefix and roots are all configurable.

| Variable in `env.sh` | Meaning |
| --- | --- |
| `AGENTSTACK_MAIL_DIR` (service root) | `candidates/<commit>/venv`: immutable candidate, one venv per exact commit, built with `uv` from `packages/agentstack_mail`. `renders/<commit>-<id>/service.env` and `run-agentstack-mail.sh`: one render per (commit, venv, endpoint, state root); the id is a hash of exactly those inputs, so the same inputs name the same directory and the installer refuses to rewrite an existing one. `runtime/agentstack-mail.pid` (two lines: runner pid, runner path) and `runtime/agentstack-mail.log`. |
| `AGENTSTACK_MAIL_STATE_ROOT` (state root) | `storage.sqlite3` (WAL mode), `archive/`, `signals/`. Shared state, not part of any candidate, fixed across deployments. |
| `AGENTSTACK_MAIL_ENV` | The render the controller and the autostart unit start. |
| `AGENTSTACK_MCP_URL` | The endpoint; its port is the one to watch. |
| `AGENTSTACK_PYTHON` | The interpreter candidates are built with. |

The autostart unit is `<label prefix>.mail` (`org.agentstack.mail` unless
`AGENTSTACK_LABEL_PREFIX` was set): a launchd job every 300 s on macOS, a
`.timer` of the same name on systemd. It runs `agentstack-mailctl start`,
which reads `env.sh`. Within what the installer manages there is no second
supervisor; the pre-flight checks for leftovers of the older layout.

## Principles

- **Candidates are immutable.** A new build is a new `candidates/<commit>`
  directory. Never run `uv venv` or `pip install` against one that exists.
  The previous one stays on disk untouched; it is the rollback.
- **The database is shared state.** A build that would alter the schema on
  start needs its own migration plan and is out of scope here. Check the tree
  diff of `packages/agentstack_mail/src` for DDL before you begin.
- **Verify before the switch, not after.** Everything before the stop runs in
  an isolated process environment against a scratch port and a consistent
  snapshot of the database.
- **Ask the port which build is serving.** Not `launchctl print`, not the
  pidfile, not your notes, not the package version.

## Procedure

Each step is a shell function: define it, then call it as
`step_N || echo "step N failed"` and stop at the first failure. A function
returns non-zero at the first check that fails and runs nothing after it;
this is why the steps are not one long block. Run them in `bash`; the
pre-flight has also been checked under `zsh`.

0. **Pre-flight: read the installed values.** `env.sh` is written with
   `shlex.quote`, so the right-hand sides are unquoted by a shell, in a
   subshell so nothing leaks. Every required value must be present.

   ```bash
   INSTALL=~/.agentstack
   REPO=<clean checkout at the commit to deploy>
   port_free() {  # 0 only when lsof ran and found no listener on TCP port $1
     local out err rc
     command -v lsof >/dev/null 2>&1 || { echo "lsof is not installed" >&2; return 1; }
     err=$(mktemp) || return 1
     out=$(lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>"$err"); rc=$?
     if [ -s "$err" ]; then echo "lsof failed:" >&2; cat "$err" >&2; rm -f "$err"; return 1; fi
     rm -f "$err"
     [ $rc -ne 0 ] || { echo "port $1 is occupied:" >&2; echo "$out" >&2; return 1; }
     [ $rc -eq 1 ] && [ -z "$out" ] || { echo "lsof exited with status $rc without an error message; not treating port $1 as free" >&2; return 1; }
   }
   listener_pid() {  # prints the pid listening on TCP port $1; fails when there is none or lsof failed
     local out err rc pid
     command -v lsof >/dev/null 2>&1 || { echo "lsof is not installed" >&2; return 1; }
     err=$(mktemp) || return 1
     out=$(lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>"$err"); rc=$?
     if [ -s "$err" ]; then echo "lsof failed:" >&2; cat "$err" >&2; rm -f "$err"; return 1; fi
     rm -f "$err"
     [ $rc -eq 0 ] || { echo "nothing is listening on port $1" >&2; return 1; }
     pid=$(printf '%s\n' "$out" | awk 'NR>1{print $2; exit}')
     [ -n "$pid" ] || { echo "lsof reported a listener on port $1 but no pid could be read" >&2; return 1; }
     printf '%s\n' "$pid"
   }
   step_0() {
     local assignments
     [ -f "$INSTALL/env.sh" ] || { echo "no env.sh under $INSTALL" >&2; return 1; }
     assignments=$(
       set +u
       unset AGENTSTACK_PYTHON AGENTSTACK_MCP_URL AGENTSTACK_MAIL_DIR AGENTSTACK_MAIL_STATE_ROOT AGENTSTACK_MAIL_ENV AGENTSTACK_LABEL_PREFIX
       . "$INSTALL/env.sh" >/dev/null || exit 1
       for k in AGENTSTACK_PYTHON AGENTSTACK_MCP_URL AGENTSTACK_MAIL_DIR AGENTSTACK_MAIL_STATE_ROOT AGENTSTACK_MAIL_ENV; do
         eval "v=\${$k:-}"; [ -n "$v" ] || { echo "env.sh does not define $k" >&2; exit 1; }
       done
       for k in AGENTSTACK_PYTHON AGENTSTACK_MCP_URL AGENTSTACK_MAIL_DIR AGENTSTACK_MAIL_STATE_ROOT AGENTSTACK_MAIL_ENV AGENTSTACK_LABEL_PREFIX; do
         eval "v=\${$k:-}"; printf '%s=%q\n' "$k" "$v"
       done
     ) || { echo "could not read the required values from $INSTALL/env.sh" >&2; return 1; }
     eval "$assignments"
     PY=$AGENTSTACK_PYTHON; SVC=$AGENTSTACK_MAIL_DIR; STATE=$AGENTSTACK_MAIL_STATE_ROOT
     OLD_ENV=$AGENTSTACK_MAIL_ENV; LABEL="${AGENTSTACK_LABEL_PREFIX:-org.agentstack}.mail"
     [ -x "$PY" ] || { echo "AGENTSTACK_PYTHON is not executable: $PY" >&2; return 1; }
     [ -f "$OLD_ENV" ] || { echo "current render env is missing: $OLD_ENV" >&2; return 1; }
     PORT=$("$PY" -c 'import sys,urllib.parse;print(urllib.parse.urlparse(sys.argv[1]).port or "")' "$AGENTSTACK_MCP_URL")
     [ -n "$PORT" ] || { echo "no port in AGENTSTACK_MCP_URL" >&2; return 1; }
     SHA=$(git -C "$REPO" rev-parse HEAD) || return 1
     echo "python=$PY service_root=$SVC state_root=$STATE port=$PORT label=$LABEL sha=$SHA"
   }
   step_0 || echo "step 0 failed"
   ```

1. **Gate on the test suite at that commit** (dev venv, see
   `CONTRIBUTING.md`): `PYTHONPATH=. .venv/bin/python -m pytest -q` green in a
   clean single run, and `packages/agentstack_mail/tests` green as well.

2. **Build the candidate ahead of time**, unless it already exists and is
   complete. This keeps `pip install` out of the outage; the installer reuses
   a complete candidate at this path and stops on an incomplete one.

   ```bash
   step_2() {
     local status
     V="$SVC/candidates/$SHA/venv"
     if [ -x "$V/bin/agentstack-mail" ] && [ -x "$V/bin/agentstack-mail-service" ] && [ -x "$V/bin/agentstack-mail-migrate" ]; then
       echo "candidate complete; not touching it"; return 0
     fi
     [ ! -e "$V" ] || { echo "candidate exists but is incomplete: $V" >&2; return 1; }
     status=$(git -C "$REPO" status --porcelain -- packages/agentstack_mail) || { echo "git status failed in $REPO" >&2; return 1; }
     [ -z "$status" ] || { echo "packages/agentstack_mail is dirty" >&2; return 1; }
     uv venv --python "$PY" "$V" || return 1
     uv pip install --python "$V/bin/python" "$REPO/packages/agentstack_mail" || return 1
     [ -x "$V/bin/agentstack-mail" ] && [ -x "$V/bin/agentstack-mail-service" ] && [ -x "$V/bin/agentstack-mail-migrate" ]
   }
   step_2 || echo "step 2 failed"
   ```

3. **Verify the candidate offline, in an isolated environment.** The server
   reads configuration through python-decouple, which prefers the process
   environment over the env file, so a shell that has sourced `env.sh` (or
   any `AGENTSTACK_MAIL_*` variable) would point the scratch server at the
   live database. Start it with an empty environment. Take the database
   snapshot through SQLite's backup API, not `cp`: the live file is in WAL
   mode and a plain copy of the main file can miss committed pages. The
   snapshot contains credentials, so keep it private and delete it after.

   After copying the live render env, rewrite every setting that names a
   location to the scratch directory. A setting left out of the rewrite keeps
   pointing where the live service does, and the scratch server tries to open
   the live resource (when the enrollment management socket was added, this
   made the scratch server fail with `management socket is already active`).
   `scratch_env_isolated` stops when a line copied unchanged names a path under
   an existing directory. If it stops, add a rewrite for that setting to `sed`.

   ```bash
   probe() {  # probe <url> <tool> '<json arguments>'  → prints the JSON-RPC response
     "$PY" - "$1" "$2" "$3" <<'PY'
   import json, sys, urllib.request
   url, tool, args = sys.argv[1:]
   body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": tool, "arguments": json.loads(args)}}
   req = urllib.request.Request(url, data=json.dumps(body).encode(),
         headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
   print(urllib.request.urlopen(req, timeout=15).read().decode())
   PY
   }
   scratch_env_isolated() {  # scratch_env_isolated <live env> <scratch env>
     # 1 when a line copied unchanged from the live env names a real location
     local line key v d bad=0
     while IFS= read -r line; do
       case "$line" in AGENTSTACK_MAIL_*=*) ;; *) continue ;; esac
       grep -qxF -- "$line" "$1" || continue
       key=${line%%=*}; v=${line#*=}; v=${v#[\'\"]}; v=${v%[\'\"]}; v=${v#*:///}
       case "$v" in
         /*) d=$(dirname "$v")
             if [ "$d" != / ] && [ -d "$d" ]; then
               echo "scratch env still points $key at the live location $v; rewrite it to \$S" >&2; bad=1
             fi ;;
       esac
     done < "$2"
     return $bad
   }
   step_3() {
     local S SPORT=18799 SPID rc=1 r
     [ -x "$V/bin/agentstack-mail" ] || { echo "candidate server binary is missing: $V (run step 2)" >&2; return 1; }
     port_free "$SPORT" || return 1
     S=$(mktemp -d) || return 1
     chmod 700 "$S"; mkdir -p "$S/state/archive" "$S/state/signals"
     "$PY" - "$STATE/storage.sqlite3" "$S/state/storage.sqlite3" <<'PY' || { rm -rf "$S"; return 1; }
   import sqlite3, sys
   src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
   src.backup(dst); dst.close(); src.close()
   PY
     sed -e "s#^AGENTSTACK_MAIL_HTTP_PORT=.*#AGENTSTACK_MAIL_HTTP_PORT=$SPORT#" \
         -e "s#^AGENTSTACK_MAIL_DATABASE_URL=.*#AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///$S/state/storage.sqlite3#" \
         -e "s#^AGENTSTACK_MAIL_STORAGE_ROOT=.*#AGENTSTACK_MAIL_STORAGE_ROOT=$S/state/archive#" \
         -e "s#^AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=.*#AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=$S/state/signals#" \
         -e "s#^AGENTSTACK_MAIL_MANAGEMENT_SOCKET=.*#AGENTSTACK_MAIL_MANAGEMENT_SOCKET=$S/mgmt.sock#" \
         "$OLD_ENV" > "$S/service.env"
     grep -qE '^AGENTSTACK_MAIL_HTTP_HOST=(127\.0\.0\.1|localhost|::1)$' "$S/service.env" || { echo "scratch env is not loopback" >&2; rm -rf "$S"; return 1; }
     scratch_env_isolated "$OLD_ENV" "$S/service.env" || { rm -rf "$S"; return 1; }
     env -i HOME="$HOME" PATH=/usr/bin:/bin AGENTSTACK_MAIL_ENV_FILE="$S/service.env" \
       "$V/bin/agentstack-mail" > "$S/server.log" 2>&1 &
     SPID=$!
     for _ in $(seq 1 100); do
       kill -0 "$SPID" 2>/dev/null || { echo "scratch server exited early; see $S/server.log" >&2; return 1; }
       curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$SPORT/api" && break; sleep 0.2
     done
     if r=$(probe "http://127.0.0.1:$SPORT/mcp" health_check '{}') && [ "${r#*$S/state/storage.sqlite3}" != "$r" ] \
        && r=$(probe "http://127.0.0.1:$SPORT/api" health_check '{}') && [ "${r#*$S/state/storage.sqlite3}" != "$r" ] \
        && r=$(probe "http://127.0.0.1:$SPORT/api" whois '{"project_key": "<a project key from the live database>", "agent_name": "<an agent in it>"}') && [ "${r#*\"<an agent in it>\"}" != "$r" ] \
        && r=$(probe "http://127.0.0.1:$SPORT/api" health_check '{"nonce": "canary-value"}') && [ "${r#*canary-value}" = "$r" ] && [ "${r#*isError\":true}" != "$r" ] \
        && [ "$(grep -c canary-value "$S/server.log")" = 0 ]; then
       echo "candidate verified on scratch port $SPORT"; rc=0
     else
       echo "candidate failed offline verification; log kept at $S/server.log" >&2
     fi
     kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null
     [ $rc -eq 0 ] && rm -rf "$S"
     return $rc
   }
   step_3 || echo "step 3 failed"
   ```

   Success is all of: both paths answer `health_check` with the scratch
   `database_url`; the read returns the record; the rejected call reports an
   error that names no caller-supplied value, and the scratch log has none.
   Anything else means the candidate is not ready and the procedure stops.

4. **Record the current deployment and dry-run the installer** while
   everything is still running. The dry run must exit 0 and say it would
   reuse the existing service; if it says anything else, the live state is
   not what you think and the switch must wait.

   ```bash
   step_4() {
     local pid log
     pid=$(listener_pid "$PORT") || return 1
     echo "OLD candidate: $(ps -o command= -p "$pid")"          # keep this line
     echo "OLD render:    $OLD_ENV"                              # keep this line
     log=$(mktemp) || return 1
     ( cd "$REPO" && bash scripts/install.sh --scoped --dry-run ) > "$log" 2>&1 || { echo "dry run failed; see $log" >&2; return 1; }
     grep -q "would reuse existing ORRERY Mail service" "$log" || { echo "dry run did not plan to reuse the running service; see $log" >&2; return 1; }
     rm -f "$log"
   }
   step_4 || echo "step 4 failed"
   ```

5. **Hold the autostart unit.** It starts whatever `env.sh` names, and until
   the installer rewrites `env.sh` that is the old render. If it fires between
   your stop and the installer's start, the old build returns and the
   installer adopts it.

   macOS:

   ```bash
   step_5() {
     launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
     ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || { echo "$LABEL is still loaded" >&2; return 1; }
     ! grep -lE 'agentstack-mail-service|cutover-maintenance|current-deployment\.env' ~/Library/LaunchAgents/*.plist 2>/dev/null | grep . || { echo "older-layout units above must be disabled first" >&2; return 1; }
     ! crontab -l 2>/dev/null | grep -E 'agentstack-mail-service|cutover-maintenance|current-deployment\.env' || { echo "older-layout cron entries above must be disabled first" >&2; return 1; }
   }
   step_5 || echo "step 5 failed"
   ```

   systemd:

   ```bash
   step_5() {
     systemctl --user stop "$LABEL.timer"
     ! systemctl --user is-active --quiet "$LABEL.timer" || { echo "$LABEL.timer is still active" >&2; return 1; }
     ! systemctl --user list-units --all --no-legend 2>/dev/null | grep -E 'agentstack-mail-service|cutover-maintenance' || { echo "older-layout units above must be disabled first" >&2; return 1; }
     ! crontab -l 2>/dev/null | grep -E 'agentstack-mail-service|cutover-maintenance|current-deployment\.env' || { echo "older-layout cron entries above must be disabled first" >&2; return 1; }
   }
   step_5 || echo "step 5 failed"
   ```

6. **Stop, then install.** This is the outage window. Measured once with the
   candidate pre-built: 12 s from stop to "ready". `agentstack-mailctl stop`
   signals the managed runner it recorded and waits until the endpoint no
   longer answers; it refuses to stop a process it did not start.

   ```bash
   step_6() {
     local log
     "$INSTALL/bin/agentstack-mailctl" stop || return 1
     port_free "$PORT" || return 1
     log=$(mktemp) || return 1
     ( cd "$REPO" && bash scripts/install.sh --scoped ) 2>&1 | tee "$log"
     [ "${PIPESTATUS[0]}" -eq 0 ] || { echo "installer failed; see $log" >&2; return 1; }
     ! grep -q "existing ORRERY Mail listener detected" "$log" || { echo "the old build came back during the window and was adopted: not deployed" >&2; return 1; }
     grep -q "candidate venv $SVC/candidates/$SHA/venv" "$log" || { echo "installer did not use candidate $SHA" >&2; return 1; }
     rm -f "$log"
   }
   step_6 || echo "step 6 failed"
   ```

   Expected lines in the installer output, in this order:
   `no native listener found`;
   `reuse immutable ORRERY Mail candidate venv .../candidates/$SHA/venv`;
   `render namespaced ORRERY Mail service env .../renders/$SHA-<id>/service.env`;
   `ORRERY Mail started (pid ..., ready after Ns)`;
   `ORRERY Mail will restart at login`. If instead it reports
   `existing ORRERY Mail listener detected`, something started the old build
   during the window and the installer adopted it: the deployment did **not**
   happen. Find what started it, stop again, and repeat this step.

   A shell that has sourced the previous `env.sh` is fine: the installer
   forgives an inherited `AGENTSTACK_MAIL_ENV` only when it equals the value in
   the installed `env.sh`, sits under the service root's `renders/`, and
   `AGENTSTACK_MAIL_SERVICE_ENV` is not set. Any other value stops the run.

7. **Prove production, then say done.** Every check must hold; if one does
   not, the deployment is not complete.

   ```bash
   step_7() {
     local pid new_env r
     pid=$(listener_pid "$PORT") || return 1
     # the venv's python can show up as Homebrew's Python.app (measured on a second Mac); match the executable under venv/bin/
     ps -o command= -p "$pid" | grep -q "candidates/$SHA/venv/bin/" || { echo "port $PORT is not served by candidate $SHA" >&2; return 1; }
     new_env=$( set +u; . "$INSTALL/env.sh" || exit 1; printf '%s' "$AGENTSTACK_MAIL_ENV" )
     [ "${new_env#$SVC/renders/$SHA-}" != "$new_env" ] || { echo "env.sh does not point at a render of $SHA: $new_env" >&2; return 1; }
     sed -n 2p "$SVC/runtime/agentstack-mail.pid" | grep -q "$(dirname "$new_env")/" || { echo "pidfile runner is not inside the new render" >&2; return 1; }
     if [ "$(uname)" = Darwin ]; then launchctl list | grep -q "$LABEL" || { echo "$LABEL is not registered" >&2; return 1; }
     else systemctl --user is-active --quiet "$LABEL.timer" || { echo "$LABEL.timer is not active" >&2; return 1; }; fi
     "$INSTALL/bin/agentstack-mailctl" status || return 1
     "$INSTALL/bin/agentstack-selftest" || return 1
     r=$(probe "$AGENTSTACK_MCP_URL" health_check '{}') && [ "${r#*$STATE/storage.sqlite3}" != "$r" ] || { echo "live health_check does not report the shared database" >&2; return 1; }
     echo "deployed $SHA; old render was $OLD_ENV, new render is $new_env"
   }
   step_7 || echo "step 7 failed"
   ```

   Repeat the step-3 probes against the real endpoint, including the rejected
   call if the change touched argument handling or logging. Record `$SHA`,
   the old and new render paths, the outage and the probe results in a
   deployment note.

## Rollback

**Read from the installer's source; not executed.** Treat it as a plan to
verify on a scratch install before relying on it.

The previous candidate and render are still on disk. Rolling back is the
forward procedure with the previous commit, with two differences. First,
`AGENTSTACK_MAIL_CANDIDATE_ID` only names the candidate directory: an absent
directory would be built from the *current* checkout's package under the old
name (an existing but incomplete one stops the installer). So verify the old
candidate is complete before stopping anything, and pin it explicitly with
`AGENTSTACK_MAIL_SERVICE_VENV`, which makes the installer stop instead of
building. Second, everything else the installer manages still comes from the
checkout you run it from.

```bash
rollback() {
  local OLD_SHA=$1 OLD_V
  OLD_V="$SVC/candidates/$OLD_SHA/venv"
  [ -x "$OLD_V/bin/agentstack-mail" ] && [ -x "$OLD_V/bin/agentstack-mail-service" ] && [ -x "$OLD_V/bin/agentstack-mail-migrate" ] \
    || { echo "old candidate is not complete: $OLD_V" >&2; return 1; }
  step_5 || return 1
  "$INSTALL/bin/agentstack-mailctl" stop || return 1
  port_free "$PORT" || return 1
  ( cd "$REPO" && AGENTSTACK_MAIL_CANDIDATE_ID="$OLD_SHA" AGENTSTACK_MAIL_SERVICE_VENV="$OLD_V" bash scripts/install.sh --scoped ) || return 1
  SHA=$OLD_SHA step_7
}
rollback <previous commit> || echo "rollback failed"
```

The unit follows `env.sh`, so there is no separate pointer to update.

## Checking which build is actually serving

The deployment you performed and the build answering the port are different
claims. Ask the port:

```bash
pid=$(lsof -nP -iTCP:<port> -sTCP:LISTEN | awk 'NR>1{print $2}')
ps -o command= -p "$pid"          # which candidate's python is this
```

The pid in `agentstack-mail.pid` is the runner, not the server; the server is
its child, and it is the child that holds the descriptors and answers
requests. Measuring the runner and concluding "the fix is live" is a mistake
that has already been made here. So is reading the dashboard's `/api/version`:
that is the package version, and it changes on every re-run whether or not
Mail was switched.

## History

Before the bundled service, deployments were hand-run under
`cutover-maintenance/` with `agentstack-mail-service render/stop/start` and a
supervisor that read a pointer file. That supervisor silently restored an old
build once (2026-08-26), which is why the installer's layout has one
supervisor that reads `env.sh`, and why step 5 looks for leftovers. The
cutover receipts under `cutover-maintenance/` pin the candidate that performed
the 2026-08 cutover; they describe events, not the currently active build, and
nothing in this procedure edits them.
