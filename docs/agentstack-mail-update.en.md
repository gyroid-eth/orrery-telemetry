# Updating a live ORRERY Mail service

> 日本語版: [agentstack-mail-update.md](agentstack-mail-update.md)

This document covers shipping a new build of `agentstack_mail` onto a machine
where the service is already serving traffic. It describes the layout that
`scripts/install.sh` has managed since the bundled service became the only
provider; the earlier hand-run layout under `cutover-maintenance/` is history
and is not used by anything the installer writes.

## What the installer does, and does not do

`install.sh` updates hooks, skills, the dashboard, `bin/`, `env.sh` and the
autostart units on every run. For ORRERY Mail it does one of two things:

- **A healthy listener is answering the configured endpoint** → it adopts the
  render that listener is using, records it in `env.sh`, and does **not**
  touch the service. This is every ordinary re-run, and it means a re-run
  alone never switches the Mail build. The dashboard's `/api/version` reports
  the package version, not the build behind the Mail port.
- **Nothing is answering** → it provisions a candidate for the checkout's
  exact commit, renders a new service env, starts the service through
  `agentstack-mailctl`, points `env.sh` and the autostart unit at the new
  render, and verifies that the served database is the shared one.

An update is therefore "stop the old service, then run the installer".

## Layout

All under the install dir (`~/.agentstack` by default):

| Path | Meaning |
| --- | --- |
| `mail-service/candidates/<commit>/venv` | Immutable candidate: one venv per exact commit, built with `uv` from `packages/agentstack_mail`. Never installed into or edited after creation. |
| `mail-service/renders/<commit>-<hash>/service.env`, `run-agentstack-mail.sh` | One render per deployment. The hash covers venv, endpoint and state root, so a new deployment always gets a new directory. |
| `mail-service/runtime/agentstack-mail.pid` | Two lines: runner pid, then the runner path. |
| `mail-service/runtime/agentstack-mail.log` | The server log of the running build. |
| `mail/storage.sqlite3`, `mail/archive`, `mail/signals` | Shared state. Not part of any candidate; fixed across deployments. |
| `env.sh` → `AGENTSTACK_MAIL_ENV` | Which render the controller and the autostart unit start. |

The autostart unit (`org.agentstack.mail` on launchd every 300 s, the
`.timer` of the same label on systemd) runs `agentstack-mailctl start`, which
reads `env.sh`. There is no second supervisor with a candidate of its own.

## Principles

- **Candidates are immutable.** A new build is a new `candidates/<commit>`
  directory. The previous one stays on disk untouched; it is the rollback.
- **The database is shared state.** A build that would alter the schema on
  start needs its own migration plan and is out of scope here. Check the tree
  diff of `packages/agentstack_mail/src` for DDL before you begin.
- **Verify before the switch, not after.** Everything before the stop runs
  against a scratch port and a scratch copy of the database.
- **Ask the port which build is serving.** Not `launchctl print`, not your
  notes, not the package version.

## Procedure

`REPO` is a clean checkout at the exact commit to deploy; `SHA` is that
commit; `INSTALL` is the install dir.

1. **Gate on the test suite at that commit** (dev venv, see
   `CONTRIBUTING.md`): `PYTHONPATH=. .venv/bin/python -m pytest -q` green in a
   clean single run, and the package's own suite
   `packages/agentstack_mail/tests` green as well.

2. **Build the candidate ahead of time** so the outage does not include a
   `pip install`. This is exactly what the installer would do, and the
   installer reuses a complete candidate it finds at this path:

   ```bash
   PY=$(sed -n 's/^export AGENTSTACK_PYTHON=//p' "$INSTALL/env.sh" | tr -d "'\"")
   V="$INSTALL/mail-service/candidates/$SHA/venv"
   git -C "$REPO" status --porcelain -- packages/agentstack_mail   # must be empty
   uv venv --python "$PY" "$V"
   uv pip install --python "$V/bin/python" "$REPO/packages/agentstack_mail"
   ls "$V/bin/agentstack-mail" "$V/bin/agentstack-mail-service" "$V/bin/agentstack-mail-migrate"
   ```

3. **Verify the candidate offline.** Copy the live `service.env`, change the
   port, and point the database, archive and signals at scratch paths; then
   run the candidate in the foreground and probe what production needs: a
   `health_check` `tools/call` on the canonical path **and on each alias**
   (`/mcp`, `/api`), and one representative read (`whois`). If the change
   touches argument handling or logging, also send a request the tool must
   reject and confirm the rejection carries no caller-supplied value, in the
   response or in the scratch log.

   ```bash
   S=$(mktemp -d); mkdir -p "$S/state/archive" "$S/state/signals"
   cp "$INSTALL/mail/storage.sqlite3" "$S/state/"
   LIVE=$(sed -n 's/^export AGENTSTACK_MAIL_ENV=//p' "$INSTALL/env.sh")
   sed -e 's#^AGENTSTACK_MAIL_HTTP_PORT=.*#AGENTSTACK_MAIL_HTTP_PORT=18799#' \
       -e "s#^AGENTSTACK_MAIL_DATABASE_URL=.*#AGENTSTACK_MAIL_DATABASE_URL=sqlite+aiosqlite:///$S/state/storage.sqlite3#" \
       -e "s#^AGENTSTACK_MAIL_STORAGE_ROOT=.*#AGENTSTACK_MAIL_STORAGE_ROOT=$S/state/archive#" \
       -e "s#^AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=.*#AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=$S/state/signals#" \
       "$LIVE" > "$S/service.env"
   AGENTSTACK_MAIL_ENV_FILE="$S/service.env" "$V/bin/agentstack-mail" > "$S/server.log" 2>&1 &
   # ... probe http://127.0.0.1:18799/mcp and /api, then kill %1
   ```

4. **Take the autostart unit out of the way.** It starts whatever `env.sh`
   names, and during the window that is still the old render; if it fires
   between your stop and the installer's start, the old build comes back and
   the installer then adopts it.

   ```bash
   launchctl bootout "gui/$(id -u)/org.agentstack.mail"      # macOS
   systemctl --user stop org.agentstack.mail.timer              # systemd
   ```

   The installer re-registers the unit at the end of the run.

5. **Stop, then install.** This is the outage window. Measured on one machine
   with the candidate pre-built: 12 s from stop to "ready".

   ```bash
   "$INSTALL/bin/agentstack-mailctl" stop
   cd "$REPO" && bash scripts/install.sh --scoped     # plus the flags your install uses
   ```

   Expected lines in the installer output: `no native listener found`,
   `reuse immutable ORRERY Mail candidate venv .../candidates/$SHA/venv`,
   `render namespaced ORRERY Mail service env .../renders/$SHA-<hash>/service.env`,
   `ORRERY Mail started (pid ..., ready after Ns)`, and
   `ORRERY Mail will restart at login`. A shell that has sourced the previous
   `env.sh` is fine: the installer recognises the managed render path it wrote
   itself and resolves the new one. Any *other* `AGENTSTACK_MAIL_ENV` value
   still stops the run, on purpose.

6. **Prove production, then say done.**

   ```bash
   pid=$(lsof -nP -iTCP:8765 -sTCP:LISTEN | awk 'NR>1{print $2}')
   ps -o command= -p "$pid"                      # .../candidates/$SHA/venv/bin/python
   sed -n 's/^export AGENTSTACK_MAIL_ENV=//p' "$INSTALL/env.sh"   # .../renders/$SHA-<hash>/service.env
   launchctl list | grep org.agentstack.mail     # unit registered again
   "$INSTALL/bin/agentstack-mailctl" status
   "$INSTALL/bin/agentstack-selftest"
   ```

   Repeat the step-3 probes against the real port. `health_check` reports
   `database_url`; it must be the shared database. Record `$SHA`, the old and
   new render paths and the measured outage in a deployment note.

## Rollback

The previous candidate and render are still on disk. Stop the service and
run the installer again with the previous commit pinned as the candidate:

```bash
"$INSTALL/bin/agentstack-mailctl" stop
cd "$REPO" && AGENTSTACK_MAIL_CANDIDATE_ID=<previous commit> bash scripts/install.sh --scoped
```

The installer reuses `candidates/<previous commit>/venv` as it stands, renders
a fresh service env for it, starts it and points `env.sh` and the autostart
unit back. Everything else the installer manages (hooks, dashboard, `bin/`)
still comes from the checkout you run it from, so check out the matching
commit if you want the whole install to move back, not only Mail. Verify with
the step-6 commands; the unit follows `env.sh`, so there is no separate
pointer to update.

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
build once (2026-08-26), which is why the current layout has exactly one
supervisor and it reads `env.sh`. The cutover receipts under
`cutover-maintenance/` pin the candidate that performed the 2026-08 cutover;
they describe events, not the currently active build, and nothing in this
procedure edits them.
