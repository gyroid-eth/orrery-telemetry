# Updating a live ORRERY Mail service

> 日本語版: [agentstack-mail-update.md](agentstack-mail-update.md)

This document covers shipping a new build of `agentstack_mail` onto a machine
where the service is already serving traffic. It describes the layout that
`scripts/install.sh` has managed since the bundled service became the only
provider. The earlier hand-run layout under `cutover-maintenance/` is history:
nothing the installer writes reads it, but a machine that once used it may
still carry its units and pointer file, so the pre-flight below checks for
them instead of assuming they are gone.

The forward procedure (steps 1 to 7) was run once on a live machine while
shipping the 2026.09.17.1 build, with the results quoted where they appear.
The rollback section is read from the installer's source and has **not** been
executed; it is marked as such.

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
  exact commit (reusing a complete one if present), renders a service env,
  starts the service through `agentstack-mailctl`, points `env.sh` and the
  autostart unit at the render, and checks that the served database is the
  shared one.

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

Read the installed values once, letting a shell unquote them the way the
installer itself does (`env.sh` is written with `shlex.quote`, so a `sed` of
the right-hand side keeps the quotes):

```bash
INSTALL=~/.agentstack                      # or your --install-dir
eval "$(
  set +u; . "$INSTALL/env.sh" >/dev/null 2>&1
  for k in AGENTSTACK_PYTHON AGENTSTACK_MCP_URL AGENTSTACK_MAIL_DIR \
           AGENTSTACK_MAIL_STATE_ROOT AGENTSTACK_MAIL_ENV AGENTSTACK_LABEL_PREFIX; do
    eval "v=\${$k:-}"; printf '%s=%q\n' "$k" "$v"
  done
)"
PY=$AGENTSTACK_PYTHON; SVC=$AGENTSTACK_MAIL_DIR; STATE=$AGENTSTACK_MAIL_STATE_ROOT
OLD_ENV=$AGENTSTACK_MAIL_ENV; LABEL="${AGENTSTACK_LABEL_PREFIX:-org.agentstack}.mail"
PORT=$("$PY" -c 'import sys,urllib.parse;print(urllib.parse.urlparse(sys.argv[1]).port)' "$AGENTSTACK_MCP_URL")
REPO=<clean checkout at the commit to deploy>; SHA=$(git -C "$REPO" rev-parse HEAD)
```

1. **Gate on the test suite at that commit** (dev venv, see
   `CONTRIBUTING.md`): `PYTHONPATH=. .venv/bin/python -m pytest -q` green in a
   clean single run, and `packages/agentstack_mail/tests` green as well.

2. **Build the candidate ahead of time**, unless it already exists and is
   complete. This keeps `pip install` out of the outage; the installer reuses
   a complete candidate at this path and refuses an incomplete one.

   ```bash
   V="$SVC/candidates/$SHA/venv"
   if [ -x "$V/bin/agentstack-mail" ] && [ -x "$V/bin/agentstack-mail-service" ] && [ -x "$V/bin/agentstack-mail-migrate" ]; then
     echo "candidate complete; not touching it"
   elif [ -e "$V" ]; then
     echo "candidate exists but is incomplete; stop and investigate" >&2; false
   else
     [ -z "$(git -C "$REPO" status --porcelain -- packages/agentstack_mail)" ] || { echo "dirty package" >&2; false; }
     uv venv --python "$PY" "$V"
     uv pip install --python "$V/bin/python" "$REPO/packages/agentstack_mail"
   fi
   ```

3. **Verify the candidate offline, in an isolated environment.** The server
   reads configuration through python-decouple, which prefers the process
   environment over the env file, so a shell that has sourced `env.sh` (or
   any `AGENTSTACK_MAIL_*` variable) would point the scratch server at the
   live database. Start it with an empty environment. Take the database
   snapshot through SQLite's backup API, not `cp`: the live file is in WAL
   mode and a plain copy of the main file can miss committed pages. The
   snapshot contains credentials, so keep it private and delete it after.

   ```bash
   S=$(mktemp -d); chmod 700 "$S"; mkdir -p "$S/state/archive" "$S/state/signals"
   "$PY" - "$STATE/storage.sqlite3" "$S/state/storage.sqlite3" <<'PY'
   import sqlite3, sys
   src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
   src.backup(dst); dst.close(); src.close()
   PY
   SPORT=18799; [ -z "$(lsof -nP -iTCP:$SPORT -sTCP:LISTEN)" ] || { echo "scratch port busy" >&2; false; }
   sed -e "s#^AGENTSTACK_MAIL_HTTP_PORT=.*#AGENTSTACK_MAIL_HTTP_PORT=$SPORT#" \
       -e "s#^AGENTSTACK_MAIL_DATABASE_URL=.*#AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///$S/state/storage.sqlite3#" \
       -e "s#^AGENTSTACK_MAIL_STORAGE_ROOT=.*#AGENTSTACK_MAIL_STORAGE_ROOT=$S/state/archive#" \
       -e "s#^AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=.*#AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=$S/state/signals#" \
       "$OLD_ENV" > "$S/service.env"
   grep -E '^AGENTSTACK_MAIL_HTTP_HOST=' "$S/service.env"     # must be a loopback address
   env -i HOME="$HOME" PATH=/usr/bin:/bin AGENTSTACK_MAIL_ENV_FILE="$S/service.env" \
     "$V/bin/agentstack-mail" > "$S/server.log" 2>&1 &
   SPID=$!
   ```

   Probe with the helper below. `PROBE_URL` is the scratch endpoint on each
   published path (`/mcp` and `/api` by default; read
   `AGENTSTACK_MAIL_HTTP_PATH` and `_PATH_ALIASES` from the render to be sure).

   ```bash
   probe() {  # probe <url> <tool> '<json arguments>'  → prints the result text
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
   probe "http://127.0.0.1:$SPORT/mcp" health_check '{}'   # database_url must name $S/state/storage.sqlite3
   probe "http://127.0.0.1:$SPORT/api" health_check '{}'   # same answer on the alias
   probe "http://127.0.0.1:$SPORT/api" whois '{"project_key": "<a project key from the live database>", "agent_name": "<an agent in it>"}'
   probe "http://127.0.0.1:$SPORT/api" health_check '{"nonce": "canary-value"}'   # isError true, text names a count, no "canary-value"
   grep -c canary-value "$S/server.log"                    # 0
   kill "$SPID"; wait "$SPID" 2>/dev/null; rm -rf "$S"
   ```

   Success is all of: both paths answer `health_check` with the scratch
   `database_url`; the read returns the record; a rejected call names no
   caller-supplied value in the response or the scratch log. Anything else
   means the candidate is not ready and the procedure stops here.

4. **Record the current deployment and dry-run the installer** while
   everything is still running. The dry run must say it would reuse the
   existing service; if it says anything else, the live state is not what you
   think and the switch must wait.

   ```bash
   pid=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN | awk 'NR>1{print $2}')
   ps -o command= -p "$pid"                                  # OLD candidate python: keep this line
   echo "$OLD_ENV"                                           # OLD render: keep this line
   ( cd "$REPO" && bash scripts/install.sh --scoped --dry-run ) | grep -E "ORRERY Mail"
   ```

5. **Hold the autostart unit.** It starts whatever `env.sh` names, and until
   the installer rewrites `env.sh` that is the old render. If it fires between
   your stop and the installer's start, the old build returns and the
   installer adopts it.

   macOS:

   ```bash
   launchctl bootout "gui/$(id -u)/$LABEL"
   launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && echo "still loaded" >&2
   ```

   systemd:

   ```bash
   systemctl --user stop "$LABEL.timer"
   systemctl --user is-active "$LABEL.timer" && echo "still active" >&2
   ```

   Also look for leftovers of the older layout before continuing: any other
   user unit or cron entry that names `agentstack-mail-service`,
   `cutover-maintenance` or `current-deployment.env` must be disabled first.

6. **Stop, then install.** This is the outage window. Measured once with the
   candidate pre-built: 12 s from stop to "ready". `agentstack-mailctl stop`
   signals the managed runner it recorded and waits until the endpoint no
   longer answers; it refuses to stop a process it did not start.

   ```bash
   "$INSTALL/bin/agentstack-mailctl" stop
   lsof -nP -iTCP:$PORT -sTCP:LISTEN                         # must print nothing
   ( cd "$REPO" && bash scripts/install.sh --scoped )        # plus the flags your install uses
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

7. **Prove production, then say done.** Every line must hold; if one does
   not, the deployment is not complete.

   ```bash
   pid=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN | awk 'NR>1{print $2}')
   ps -o command= -p "$pid"                                  # .../candidates/$SHA/venv/bin/python
   ( set +u; . "$INSTALL/env.sh"; echo "$AGENTSTACK_MAIL_ENV" ) # .../renders/$SHA-<id>/service.env
   sed -n 2p "$SVC/runtime/agentstack-mail.pid"              # runner inside the same render
   launchctl list | grep "$LABEL"                            # macOS: unit registered again
   systemctl --user is-active "$LABEL.timer"                 # systemd
   "$INSTALL/bin/agentstack-mailctl" status
   "$INSTALL/bin/agentstack-selftest"
   probe "$AGENTSTACK_MCP_URL" health_check '{}'             # database_url = $STATE/storage.sqlite3
   ```

   Repeat the step-3 probes against the real endpoint, including the rejected
   call if the change touched argument handling or logging. Record `$SHA`,
   the old and new render paths, the outage and the probe results in a
   deployment note.

## Rollback

**Read from the installer's source; not yet executed.** Treat it as a plan to
verify on a scratch install before relying on it.

The previous candidate and render are still on disk. Rolling back is the
forward procedure with the previous commit, with two differences. First,
`AGENTSTACK_MAIL_CANDIDATE_ID` only names the candidate directory: if that
directory were missing or incomplete, the installer would build the *current*
checkout's package into it under the old name. So verify the old candidate is
complete before stopping anything, and pin it explicitly with
`AGENTSTACK_MAIL_SERVICE_VENV`, which makes the installer fail instead of
building. Second, everything else the installer manages still comes from the
checkout you run it from.

```bash
OLD_SHA=<previous commit>; OLD_V="$SVC/candidates/$OLD_SHA/venv"
ls "$OLD_V/bin/agentstack-mail" "$OLD_V/bin/agentstack-mail-service" "$OLD_V/bin/agentstack-mail-migrate"   # all three, or stop here
# step 5: hold the autostart unit exactly as above
"$INSTALL/bin/agentstack-mailctl" stop
lsof -nP -iTCP:$PORT -sTCP:LISTEN                                     # nothing
( cd "$REPO" && AGENTSTACK_MAIL_CANDIDATE_ID="$OLD_SHA" AGENTSTACK_MAIL_SERVICE_VENV="$OLD_V" bash scripts/install.sh --scoped )
# step 7 with $OLD_SHA in place of $SHA
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
